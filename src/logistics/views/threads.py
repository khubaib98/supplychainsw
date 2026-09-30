from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core.ids import normalize_scan
from logistics import permissions as perms
from logistics import services
from logistics.forms import ReplyForm, ThreadForm
from logistics.models import Thread
from logistics.views.common import consignment_context, get_consignment, run


def _resolve_about(consignment, text):
    """Accept a full ID, a short one (CTN-017 / DCJM-017) or a manufacturer serial."""
    code = normalize_scan(text)
    if not code:
        return None, None
    candidates = [code, f"{consignment.code}-{code}"]
    unit = consignment.units.filter(code__in=candidates).first()
    if unit:
        return unit, None
    item = consignment.items.filter(uid__in=candidates).first() or consignment.items.filter(oem_serial__iexact=text.strip()).first()
    return None, item


@login_required
def thread_list(request, code):
    consignment = get_consignment(request.user, code)
    can_comment = perms.can_comment(request.user, consignment)
    form = ThreadForm(request.POST or None, initial={"about": request.GET.get("about", "")})
    if request.method == "POST":
        perms.require(can_comment)
        if form.is_valid():
            data = form.cleaned_data
            unit, item = _resolve_about(consignment, data["about"])
            if data["about"] and unit is None and item is None:
                form.add_error("about", _("No unit or item with that ID in this consignment."))
            else:
                ok, thread = run(request, services.create_thread, request.user, consignment, category=data["category"],
                                 subject=data["subject"], body=data["body"], unit=unit, item=item, request=request)
                if ok:
                    messages.success(request, _("Posted. The other party has been notified."))
                    return redirect("logistics:thread", pk=thread.pk)
    state = request.GET.get("state", "open")
    threads = consignment.threads.select_related("raised_by", "unit", "item")
    if state == "open":
        threads = threads.exclude(state=Thread.State.RESOLVED)
    context = {
        **consignment_context(request.user, consignment, "threads"),
        "threads": threads,
        "state": state,
        "form": form if can_comment else None,
    }
    return render(request, "logistics/thread_list.html", context)


def _get_thread(user, pk):
    thread = get_object_or_404(Thread.objects.select_related("consignment__shipper", "consignment__receiver"), pk=pk)
    if not perms.can_view(user, thread.consignment):
        raise Http404
    return thread


@login_required
def thread_detail(request, pk):
    thread = _get_thread(request.user, pk)
    consignment = thread.consignment
    can_comment = perms.can_comment(request.user, consignment)
    form = ReplyForm(request.POST or None)
    if request.method == "POST":
        perms.require(can_comment)
        if form.is_valid():
            ok, _message = run(request, services.reply, request.user, thread, form.cleaned_data["body"], request=request)
            if ok:
                return redirect("logistics:thread", pk=thread.pk)
    party = perms.party_in(request.user, consignment)
    context = {
        **consignment_context(request.user, consignment, "threads"),
        "thread": thread,
        "messages_list": thread.messages.select_related("author"),
        "form": form if can_comment else None,
        "can_resolve": can_comment and party == thread.raised_by_party and thread.is_open,
        "can_reopen": can_comment and not thread.is_open,
        "about_unit": thread.unit,
        "about_item": thread.item,
    }
    return render(request, "logistics/thread_detail.html", context)


@login_required
@require_POST
def thread_state(request, pk):
    thread = _get_thread(request.user, pk)
    state = request.POST.get("state")
    if state not in (Thread.State.RESOLVED, Thread.State.OPEN):
        raise Http404
    run(request, services.set_thread_state, request.user, thread, state, request=request,
        success=_("Marked resolved.") if state == Thread.State.RESOLVED else _("Reopened."))
    return redirect("logistics:thread", pk=pk)

