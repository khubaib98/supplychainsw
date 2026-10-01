from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import AuditLog, Notification
from logistics import permissions as perms
from logistics.models import Consignment, DocumentRequirement, HandlingUnit, Thread


def _visible_codes(user):
    return list(perms.visible_consignments(user).values_list("code", flat=True))


def latest_audit_id(user, consignment=None):
    queryset = AuditLog.objects.exclude(action__startswith="auth.")
    if consignment is not None:
        queryset = queryset.filter(consignment_code=consignment.code)
    elif not perms.sees_everything(user):
        queryset = queryset.filter(consignment_code__in=_visible_codes(user))
    latest = queryset.order_by("-id").values_list("id", flat=True).first()
    return latest or 0


@login_required
def dashboard(request):
    user = request.user
    today = timezone.localdate()
    consignments = list(
        perms.visible_consignments(user).prefetch_related(
            "milestones", "documents", "requirements__doc_type", "threads", "units", "tags"
        )
    )
    active = [c for c in consignments if c.is_active]
    labels = dict(Consignment.Status.choices)
    pipeline = [
        {"status": status, "label": labels[status], "count": sum(1 for c in active if c.status == status)}
        for status in Consignment.Status.values
        if status != Consignment.Status.CLOSED
    ]
    pipeline_max = max([row["count"] for row in pipeline] + [1])

    required = uploaded = 0
    overdue, due_now = [], []
    rows = []
    for consignment in sorted(active, key=lambda c: (c.status_index, c.code)):
        done = total = late = 0
        for requirement in consignment.requirements.all():
            state = requirement.state(today)
            if state == DocumentRequirement.State.WAIVED:
                continue
            total += 1
            if state == DocumentRequirement.State.UPLOADED:
                done += 1
            elif state == DocumentRequirement.State.OVERDUE:
                late += 1
                overdue.append(requirement)
            elif state == DocumentRequirement.State.DUE:
                due_now.append(requirement)
        required += total
        uploaded += done
        rows.append({
            "c": consignment,
            "docs_done": done,
            "docs_total": total,
            "docs_overdue": late,
            "open_threads": sum(1 for t in consignment.threads.all() if t.is_open),
        })
    overdue.sort(key=lambda r: -r.days_overdue(today))

    open_threads = [t for c in active for t in c.threads.all() if t.is_open]
    open_threads.sort(key=lambda t: t.created_at)
    issues = [t for t in open_threads if t.category in (Thread.Category.DISCREPANCY, Thread.Category.DAMAGE)]
    delayed = [c for c in active if c.is_delayed]

    receiving = []
    for consignment in active:
        if consignment.status_reached(Consignment.Status.ARRIVED):
            units = list(consignment.units.all())
            receiving.append(
                {
                    "consignment": consignment,
                    "total": len(units),
                    "received": sum(1 for u in units if u.received_at),
                    "damaged": sum(1 for u in units if u.condition == HandlingUnit.Condition.DAMAGED),
                }
            )

    party = perms.profile_of(user).party if perms.profile_of(user) else None
    my_documents = [
        r for r in overdue + due_now if r.doc_type.responsible in (party, "any")
    ] if party in (perms.SHIPPER, perms.RECEIVER) else []
    my_threads = [t for t in open_threads if t.awaiting_party() == party] if party in (perms.SHIPPER, perms.RECEIVER) else []

    activity = AuditLog.objects.exclude(action__startswith="auth.").exclude(action="document.downloaded")
    if not perms.sees_everything(user):
        activity = activity.filter(consignment_code__in=[c.code for c in consignments])

    received_units = sum(row["received"] for row in receiving)
    total_units = sum(row["total"] for row in receiving)
    context = {
        "active": active,
        "rows": rows,
        "in_transit": sum(1 for c in active if c.stage == "transit"),
        "received_units": received_units,
        "total_units": total_units,
        "oldest_overdue": overdue[0].days_overdue(today) if overdue else 0,
        "can_create": perms.can_create_consignment(user),
        "sees_audit": perms.sees_audit(user),
        "pipeline": pipeline,
        "pipeline_max": pipeline_max,
        "doc_required": required,
        "doc_uploaded": uploaded,
        "overdue": overdue,
        "due_now": due_now,
        "open_threads": open_threads,
        "issues": issues,
        "delayed": delayed,
        "receiving": receiving,
        "my_documents": my_documents,
        "my_threads": my_threads,
        "activity": activity.order_by("-at", "-id")[:12],
        "watch_since": latest_audit_id(user),
        "today": today,
        "party": party,
    }
    return render(request, "logistics/dashboard.html", context)


@login_required
def changes(request):
    """Near-real-time polling: has anyone else changed something I can see since `since`?"""
    try:
        since = int(request.GET.get("since", "0"))
    except ValueError:
        since = 0
    queryset = AuditLog.objects.filter(id__gt=since).exclude(action__startswith="auth.").exclude(user=request.user)
    code = request.GET.get("consignment")
    if code:
        consignment = Consignment.objects.filter(code=code).first()
        if consignment is None or not perms.can_view(request.user, consignment):
            return JsonResponse({"count": 0, "latest": since})
        queryset = queryset.filter(consignment_code=code)
    elif not perms.sees_everything(request.user):
        queryset = queryset.filter(consignment_code__in=_visible_codes(request.user))
    latest = queryset.order_by("-id").first()
    return JsonResponse(
        {
            "count": queryset.count(),
            "latest": latest.id if latest else since,
            "by": latest.username if latest else "",
            "unread": request.user.notifications.filter(read_at__isnull=True).count(),
        }
    )


@login_required
def notifications(request):
    items = list(request.user.notifications.select_related("consignment")[:100])
    return render(request, "core/notifications.html", {"items": items})


@login_required
def open_notification(request, pk):
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
    target = notification.url if notification.url.startswith("/") and not notification.url.startswith("//") else "/"
    return redirect(target)


@login_required
@require_POST
def mark_all_read(request):
    request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
    return redirect("logistics:notifications")
