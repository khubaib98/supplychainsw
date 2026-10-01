from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core.models import AuditLog
from logistics import permissions as perms
from logistics import services
from logistics.forms import ConsignmentForm, StatusForm, TagForm
from logistics.models import Consignment, ConsignmentTag, DocumentRequirement, HandlingUnit
from logistics.tree import build_tree
from logistics.views.common import consignment_context, get_consignment, run
from logistics.views.dashboard import latest_audit_id


@login_required
def consignment_list(request):
    queryset = perms.visible_consignments(request.user, include_archived=True).prefetch_related(
        "milestones", "documents", "requirements", "threads", "tags"
    )
    scope = request.GET.get("scope", "active")
    if scope == "active":
        queryset = queryset.filter(is_archived=False).exclude(status=Consignment.Status.CLOSED)
    elif scope == "closed":
        queryset = queryset.filter(Q(status=Consignment.Status.CLOSED) | Q(is_archived=True))
    status = request.GET.get("status", "")
    if status in Consignment.Status.values:
        queryset = queryset.filter(status=status)
    mode = request.GET.get("mode", "")
    if mode in Consignment.Mode.values:
        queryset = queryset.filter(transport_mode=mode)
    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(
            Q(code__icontains=query) | Q(reference__icontains=query) | Q(description__icontains=query)
            | Q(transport_ref__icontains=query) | Q(units__container_no__icontains=query)
        ).distinct()
    today = timezone.localdate()
    rows = []
    for consignment in queryset.order_by("-created_at")[:300]:
        requirements = [r for r in consignment.requirements.all()]
        for requirement in requirements:
            requirement.consignment = consignment
        states = [r.state(today) for r in requirements]
        counted = [s for s in states if s != DocumentRequirement.State.WAIVED]
        rows.append(
            {
                "c": consignment,
                "docs_done": sum(1 for s in counted if s == DocumentRequirement.State.UPLOADED),
                "docs_total": len(counted),
                "docs_overdue": sum(1 for s in counted if s == DocumentRequirement.State.OVERDUE),
                "open_threads": sum(1 for t in consignment.threads.all() if t.is_open),
            }
        )
    context = {
        "rows": rows,
        "scope": scope,
        "status": status,
        "mode": mode,
        "q": query,
        "statuses": Consignment.Status.choices,
        "modes": Consignment.Mode.choices,
        "can_create": perms.can_create_consignment(request.user),
    }
    return render(request, "logistics/consignment_list.html", context)


@login_required
def consignment_create(request):
    perms.require(perms.can_create_consignment(request.user))
    form = ConsignmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        receiver = data.pop("receiver")
        ok, consignment = run(
            request, services.create_consignment, request.user,
            shipper=request.user.profile.organization, receiver=receiver, request=request, **data,
        )
        if ok:
            messages.success(request, _("Consignment %(code)s created.") % {"code": consignment.code})
            return redirect("logistics:consignment", code=consignment.code)
    return render(request, "logistics/consignment_form.html", {"form": form, "creating": True})


@login_required
def consignment_edit(request, code):
    consignment = get_consignment(request.user, code)
    perms.require(perms.can_edit_header(request.user, consignment))
    form = ConsignmentForm(request.POST or None, instance=consignment, editing=True)
    if request.method == "POST" and form.is_valid():
        # ModelForm has already written the values onto the instance; re-read so the
        # service sees the old values and can log a proper before/after diff.
        data = dict(form.cleaned_data)
        fresh = Consignment.objects.get(pk=consignment.pk)
        ok, _changes = run(request, services.update_consignment, request.user, fresh, data, request=request,
                           success=_("Saved."))
        if ok:
            return redirect("logistics:consignment", code=code)
    context = {"form": form, "creating": False, **consignment_context(request.user, consignment, "overview")}
    return render(request, "logistics/consignment_form.html", context)


@login_required
def consignment_detail(request, code):
    consignment = get_consignment(request.user, code)
    user = request.user
    today = timezone.localdate()
    milestones = list(consignment.milestones.select_related("recorded_by"))
    reached = {}
    for event in milestones:
        reached.setdefault(event.status, event)
    labels = dict(Consignment.Status.choices)
    timeline = []
    for status in Consignment.Status.values:
        index = Consignment.STATUS_ORDER[status]
        timeline.append(
            {
                "status": status,
                "label": labels[status],
                "event": reached.get(status),
                "done": index <= consignment.status_index,
                "current": status == consignment.status,
                "owner": Consignment.STATUS_OWNER[status],
            }
        )
    requirements = list(consignment.requirements.select_related("doc_type"))
    for requirement in requirements:
        requirement.consignment = consignment
    statuses = perms.allowed_statuses(user, consignment)
    tree = build_tree(consignment)
    tags = list(consignment.tags.select_related("added_by", "removed_by"))
    context = {
        **consignment_context(user, consignment, "overview"),
        "active_tags": [tag for tag in tags if tag.is_active],
        "updates": [tag for tag in tags if tag.is_update],
        "can_tag": perms.can_tag(user, consignment),
        # Prefixed: the milestone form on the same page also has a note field.
        "tag_form": TagForm(prefix="tag") if perms.can_tag(user, consignment) else None,
        "timeline": timeline,
        "history": list(reversed(milestones)),
        "requirements": [(r, r.state(today), r.due_date()) for r in requirements],
        "status_form": StatusForm(statuses=statuses) if statuses else None,
        "tree": tree,
        "kind_totals": [
            (label, tree.kind_totals[kind]) for kind, label in HandlingUnit.Kind.choices if tree.kind_totals.get(kind)
        ],
        "threads": list(consignment.threads.select_related("raised_by")[:5]),
        "movements": list(consignment.movements.select_related("item", "unit", "by")[:8]),
        "reconciliation": services.reconciliation(consignment)
        if consignment.status_reached(Consignment.Status.ARRIVED) else None,
        "audit": AuditLog.objects.filter(consignment_code=consignment.code).order_by("-at", "-id")[:10]
        if perms.sees_audit(user) else None,
        "watch_since": latest_audit_id(user, consignment),
    }
    return render(request, "logistics/consignment_detail.html", context)


@login_required
@require_POST
def consignment_status(request, code):
    consignment = get_consignment(request.user, code)
    statuses = perms.allowed_statuses(request.user, consignment)
    perms.require(statuses)
    form = StatusForm(request.POST, statuses=statuses)
    if form.is_valid():
        at = form.cleaned_data["at"]
        run(
            request, services.set_status, request.user, consignment, form.cleaned_data["status"],
            at=at, note=form.cleaned_data["note"], request=request, success=_("Milestone recorded."),
        )
    else:
        messages.error(request, _("Please choose a milestone."))
    return redirect("logistics:consignment", code=code)


@login_required
@require_POST
def add_tag(request, code):
    consignment = get_consignment(request.user, code)
    perms.require(perms.can_tag(request.user, consignment))
    form = TagForm(request.POST, prefix="tag")
    if form.is_valid():
        data = form.cleaned_data
        run(request, services.add_tag, request.user, consignment, data["kind"], note=data["note"],
            revised_eta=data["revised_eta"], request=request, success=_("Saved. Both parties and Oversight can see it."))
    else:
        messages.error(request, _("Please correct the form: %(errors)s") % {"errors": form.errors.as_text()})
    return redirect("logistics:consignment", code=code)


@login_required
@require_POST
def remove_tag(request, pk):
    tag = get_object_or_404(ConsignmentTag.objects.select_related("consignment"), pk=pk)
    consignment = get_consignment(request.user, tag.consignment.code)
    run(request, services.remove_tag, request.user, tag, request=request, success=_("Tag removed."))
    return redirect("logistics:consignment", code=consignment.code)
