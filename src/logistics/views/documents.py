from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import audit
from logistics import permissions as perms
from logistics import services
from logistics.forms import ReasonForm, RequirementForm, UploadForm
from logistics.models import ConsignmentDocument, DocumentRequirement
from logistics.views.common import consignment_context, get_consignment, run

INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg"}


@login_required
def documents(request, code):
    consignment = get_consignment(request.user, code)
    user = request.user
    today = timezone.localdate()
    requirements = list(consignment.requirements.select_related("doc_type", "waived_by"))
    all_docs = list(consignment.documents.select_related("doc_type", "uploaded_by", "archived_by"))
    rows = []
    required_type_ids = set()
    for requirement in requirements:
        requirement.consignment = consignment
        requirement._docs = [d for d in all_docs if d.doc_type_id == requirement.doc_type_id and not d.is_archived]
        required_type_ids.add(requirement.doc_type_id)
        current = requirement.current_document()
        rows.append(
            {
                "req": requirement,
                "state": requirement.state(today),
                "due": requirement.due_date(),
                "days_overdue": requirement.days_overdue(today),
                "current": current,
                "can_archive": current is not None and perms.can_archive_document(user, current),
                "versions": [d for d in all_docs if d.doc_type_id == requirement.doc_type_id],
                "reminders": list(requirement.reminders.all()),
                "can_upload": perms.can_upload(user, consignment, requirement.doc_type),
            }
        )
    others = [d for d in all_docs if d.doc_type_id not in required_type_ids]
    uploadable = perms.uploadable_types(user, consignment)
    context = {
        **consignment_context(user, consignment, "documents"),
        "rows": rows,
        "others": others,
        "upload_form": UploadForm(doc_types=uploadable) if uploadable else None,
        "requirement_form": RequirementForm() if perms.can_manage_requirements(user, consignment) else None,
        "can_manage": perms.can_manage_requirements(user, consignment),
        "max_mb": settings.PORTAL["UPLOAD_MAX_MB"],
    }
    return render(request, "logistics/documents.html", context)


@login_required
@require_POST
def upload(request, code):
    consignment = get_consignment(request.user, code)
    uploadable = perms.uploadable_types(request.user, consignment)
    perms.require(uploadable)
    form = UploadForm(request.POST, request.FILES, doc_types=uploadable)
    if form.is_valid():
        data = form.cleaned_data
        ok, document = run(request, services.upload_document, request.user, consignment, data["doc_type"],
                           data["file"], note=data["note"], request=request)
        if ok:
            messages.success(request, _("%(doc)s uploaded as version %(version)s.")
                             % {"doc": document.doc_type.name(), "version": document.version})
    else:
        messages.error(request, _("Please correct the form: %(errors)s") % {"errors": form.errors.as_text()})
    return redirect("logistics:documents", code=code)


@login_required
def download(request, pk):
    document = get_object_or_404(ConsignmentDocument.objects.select_related("consignment", "doc_type"), pk=pk)
    if not perms.can_view(request.user, document.consignment):
        raise Http404
    try:
        handle = document.file.open("rb")
    except FileNotFoundError as exc:
        raise Http404 from exc
    audit.log(request.user, "document.downloaded", document, request=request,
              details={"type": document.doc_type.code, "version": document.version})
    inline = request.GET.get("view") == "1" and document.content_type in INLINE_TYPES
    response = FileResponse(handle, as_attachment=not inline, filename=document.original_name,
                            content_type=document.content_type)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    return response


@login_required
@require_POST
def archive(request, pk):
    document = get_object_or_404(ConsignmentDocument.objects.select_related("consignment"), pk=pk)
    if not perms.can_view(request.user, document.consignment):
        raise Http404
    form = ReasonForm(request.POST)
    if form.is_valid():
        run(request, services.archive_document, request.user, document, form.cleaned_data["reason"], request=request,
            success=_("Document archived. It stays in the history and audit log."))
    else:
        messages.error(request, _("Please give a reason."))
    return redirect("logistics:documents", code=document.consignment.code)


@login_required
@require_POST
def add_requirement(request, code):
    consignment = get_consignment(request.user, code)
    form = RequirementForm(request.POST)
    if form.is_valid():
        run(request, services.add_requirement, request.user, consignment, form.cleaned_data["doc_type"],
            due_override=form.cleaned_data["due_override"], request=request, success=_("Requirement added."))
    return redirect("logistics:documents", code=code)


@login_required
@require_POST
def waive_requirement(request, pk):
    requirement = get_object_or_404(DocumentRequirement.objects.select_related("consignment", "doc_type"), pk=pk)
    if not perms.can_view(request.user, requirement.consignment):
        raise Http404
    form = ReasonForm(request.POST)
    if form.is_valid():
        run(request, services.waive_requirement, request.user, requirement, form.cleaned_data["reason"],
            request=request, success=_("Requirement waived."))
    else:
        messages.error(request, _("Please give a reason."))
    return redirect("logistics:documents", code=requirement.consignment.code)
