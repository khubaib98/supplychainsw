"""Hash-chained audit trail helpers."""
import hashlib
import json

from django.db import transaction
from django.utils import timezone

from core.models import AuditHead, AuditLog


def _canonical(record):
    payload = {
        "prev": record.prev_hash,
        "at": record.at.isoformat(),
        "user": record.username,
        "org": record.organization,
        "action": record.action,
        "type": record.target_type,
        "id": record.target_id,
        "label": record.target_label,
        "consignment": record.consignment_code,
        "details": record.details,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def compute_hash(record):
    return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()


def client_ip(request):
    if request is None:
        return None
    # Behind Caddy the real client address arrives in X-Forwarded-For.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR")


def log(user, action, target=None, *, consignment=None, details=None, request=None, label=None):
    """Append one audit record. Safe to call inside or outside a transaction."""
    username = ""
    organization = ""
    if user is not None and getattr(user, "is_authenticated", False):
        username = user.get_username()
        profile = getattr(user, "profile", None)
        if profile is not None:
            organization = profile.organization.name
    target_type = target._meta.label if target is not None else ""
    target_id = str(target.pk) if target is not None and target.pk is not None else ""
    target_label = label if label is not None else (str(target) if target is not None else "")
    if consignment is None and target is not None:
        consignment = getattr(target, "consignment", None)
        if consignment is None and target._meta.model_name == "consignment":
            consignment = target
    code = getattr(consignment, "code", "") or ""
    with transaction.atomic():
        head, _ = AuditHead.objects.select_for_update().get_or_create(pk=1)
        record = AuditLog(
            at=timezone.now(),
            user=user if username else None,
            username=username,
            organization=organization,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_label=target_label[:200],
            consignment_code=code,
            details=json.loads(json.dumps(details or {}, default=str)),
            ip=client_ip(request),
            prev_hash=head.last_hash,
        )
        record.hash = compute_hash(record)
        record.save()
        head.last_hash = record.hash
        head.count += 1
        head.save(update_fields=["last_hash", "count"])
    return record


def verify_chain():
    """Return (ok, checked_count, first_bad_id)."""
    previous = "0" * 64
    checked = 0
    for record in AuditLog.objects.order_by("id").iterator():
        if record.prev_hash != previous or compute_hash(record) != record.hash:
            return False, checked, record.id
        previous = record.hash
        checked += 1
    head = AuditHead.objects.filter(pk=1).first()
    if head and checked and head.last_hash != previous:
        return False, checked, None
    return True, checked, None
