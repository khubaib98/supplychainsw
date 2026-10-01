"""
Business operations. Views, the scanner API and the CSV importer all go
through these functions, so the same rules (permissions, custody, nesting,
audit trail, notifications) apply however a change is made.
"""
import hashlib
import mimetypes
import os
import socket
import struct
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import Organization, Profile
from core import audit, ids, notify
from core import translation as mt
from core.i18n import detect_language
from logistics import permissions as perms
from logistics.models import (
    Consignment,
    ConsignmentDocument,
    ConsignmentTag,
    DocumentRequirement,
    DocumentType,
    HandlingUnit,
    Item,
    Message,
    MilestoneEvent,
    Movement,
    Thread,
)


class ServiceError(Exception):
    """A broken business rule, with a message that can be shown to the user."""


def _require(condition):
    perms.require(condition)


# ---------------------------------------------------------------------------
# People to notify
# ---------------------------------------------------------------------------

def members(organization, levels=(Profile.Level.MANAGER, Profile.Level.OPERATOR)):
    return list(
        get_user_model()
        .objects.filter(is_active=True, profile__organization=organization, profile__level__in=levels)
        .select_related("profile")
    )


def oversight_members():
    return list(
        get_user_model()
        .objects.filter(is_active=True, profile__organization__kind=Organization.Kind.OVERSIGHT)
        .select_related("profile")
    )


def party_org(consignment, party):
    return consignment.shipper if party == Organization.Kind.SHIPPER else consignment.receiver


def other_party(party):
    return Organization.Kind.RECEIVER if party == Organization.Kind.SHIPPER else Organization.Kind.SHIPPER


def _inform(actor, consignment, kind, params, url, *, email_counterpart=True):
    """
    Tell the other party (in-app + e-mail + WeCom) and Oversight (in-app) about
    a change. When Oversight acts, both parties are told.
    """
    party = perms.party_in(actor, consignment)
    params = {"code": consignment.code, "user": actor.get_full_name() or actor.get_username(), **params}
    told = [party_org(consignment, other_party(party))] if party else [consignment.shipper, consignment.receiver]
    for organization in told:
        notify.notify(
            [u for u in members(organization) if u.pk != actor.pk],
            kind,
            params,
            consignment=consignment,
            url=url,
            email=email_counterpart,
            organizations=[organization] if email_counterpart else (),
        )
    notify.notify([u for u in oversight_members() if u.pk != actor.pk], kind, params, consignment=consignment,
                  url=url, email=False)


# ---------------------------------------------------------------------------
# Consignments and milestones
# ---------------------------------------------------------------------------

def create_consignment(user, *, shipper, receiver, transport_mode, origin, destination, request=None, **fields):
    _require(perms.can_create_consignment(user))
    if shipper.kind != Organization.Kind.SHIPPER or receiver.kind != Organization.Kind.RECEIVER:
        raise ServiceError(_("Choose a shipper and a receiver organisation."))
    if user.profile.organization_id != shipper.pk:
        raise ServiceError(_("You can only create consignments for your own organisation."))
    now = timezone.now()
    with transaction.atomic():
        code = ids.next_consignment_code(timezone.localdate().year)
        consignment = Consignment.objects.create(
            code=code,
            shipper=shipper,
            receiver=receiver,
            transport_mode=transport_mode,
            origin=origin,
            destination=destination,
            created_by=user,
            status_changed_at=now,
            **fields,
        )
        MilestoneEvent.objects.create(consignment=consignment, status=Consignment.Status.DRAFT, at=now, recorded_by=user)
        for doc_type in DocumentType.objects.filter(is_active=True, default_required=True):
            if doc_type.applies_to(transport_mode):
                DocumentRequirement.objects.create(consignment=consignment, doc_type=doc_type)
        audit.log(user, "consignment.created", consignment, request=request,
                  details={"mode": transport_mode, "origin": origin, "destination": destination})
    return consignment


HEADER_FIELDS = [
    "reference", "description", "transport_mode", "incoterm", "origin", "destination", "carrier",
    "transport_ref", "etd", "eta", "planned_packages", "notes",
]


def update_consignment(user, consignment, data, request=None):
    _require(perms.can_edit_header(user, consignment))
    changes = {}
    for field in HEADER_FIELDS:
        if field in data and getattr(consignment, field) != data[field]:
            changes[field] = [getattr(consignment, field), data[field]]
            setattr(consignment, field, data[field])
    if changes:
        with transaction.atomic():
            consignment.save()
            if "transport_mode" in changes:
                _sync_mode_requirements(user, consignment)
            audit.log(user, "consignment.updated", consignment, request=request, details={"changes": changes})
    return changes


def _sync_mode_requirements(user, consignment):
    """After a mode change, add the default documents that now apply (e.g. AWB instead of B/L)."""
    for doc_type in DocumentType.objects.filter(is_active=True, default_required=True):
        if doc_type.applies_to(consignment.transport_mode):
            DocumentRequirement.objects.get_or_create(consignment=consignment, doc_type=doc_type)


def set_status(user, consignment, status, *, at=None, note="", request=None, _system=False):
    if not _system:
        _require(status in perms.allowed_statuses(user, consignment))
    if status not in Consignment.Status.values:
        raise ServiceError(_("Unknown status."))
    at = at or timezone.now()
    if at > timezone.now() + timedelta(minutes=5):
        raise ServiceError(_("A milestone cannot be recorded in the future."))
    previous = consignment.status
    with transaction.atomic():
        MilestoneEvent.objects.create(consignment=consignment, status=status, at=at, note=note, recorded_by=user)
        consignment.status = status
        consignment.status_changed_at = timezone.now()
        consignment.save(update_fields=["status", "status_changed_at", "updated_at"])
        if hasattr(consignment, "_milestones"):
            del consignment._milestones
        audit.log(user, "consignment.status", consignment, request=request,
                  details={"from": previous, "to": status, "at": at.isoformat(), "note": note})
        if consignment.status_reached(Consignment.Status.ARRIVED):
            # Once the goods are there, a "Delayed" tag no longer describes them.
            consignment.tags.filter(kind=ConsignmentTag.Kind.DELAY, removed_at__isnull=True).update(
                removed_at=timezone.now(), removed_by=user
            )
    if not _system:
        _inform(user, consignment, "status", {"status": status},
                f"/consignments/{consignment.code}/")
    return consignment


def _start_packing(user, consignment):
    if consignment.status == Consignment.Status.DRAFT:
        set_status(user, consignment, Consignment.Status.PACKING, note="auto", _system=True)


# ---------------------------------------------------------------------------
# Packing structure
# ---------------------------------------------------------------------------

def _check_nesting(child_kind, parent):
    if parent is None:
        return
    if HandlingUnit.RANK[parent.kind] <= HandlingUnit.RANK[child_kind]:
        raise ServiceError(
            _("A %(child)s cannot be placed inside a %(parent)s.")
            % {
                "child": HandlingUnit.Kind(child_kind).label.lower(),
                "parent": HandlingUnit.Kind(parent.kind).label.lower(),
            }
        )


def create_units(user, consignment, kind, count, *, parent=None, request=None, **attrs):
    _require(perms.can_edit_packing(user, consignment))
    if kind not in HandlingUnit.Kind.values:
        raise ServiceError(_("Unknown unit type."))
    if not 1 <= count <= 1000:
        raise ServiceError(_("Create between 1 and 1000 units at a time."))
    if parent is not None and parent.consignment_id != consignment.pk:
        raise ServiceError(_("Items can only be moved within the same consignment."))
    _check_nesting(kind, parent)
    prefix = HandlingUnit.PREFIX[kind]
    with transaction.atomic():
        first = ids.reserve(f"{consignment.code}:{prefix}", count)
        units = [
            HandlingUnit(
                code=ids.child_code(consignment.code, prefix, number),
                consignment=consignment,
                kind=kind,
                seq=number,
                parent=parent,
                created_by=user,
                **attrs,
            )
            for number in range(first, first + count)
        ]
        HandlingUnit.objects.bulk_create(units)
        units = list(HandlingUnit.objects.filter(consignment=consignment, kind=kind, seq__gte=first, seq__lt=first + count))
        if parent is not None:
            Movement.objects.bulk_create(
                [
                    Movement(consignment=consignment, unit=unit, action=Movement.Action.PACK, to_unit=parent,
                             to_code=parent.code, by=user)
                    for unit in units
                ]
            )
        _start_packing(user, consignment)
        audit.log(user, "packing.units_created", consignment, request=request,
                  details={"kind": kind, "count": count, "first": units[0].code, "last": units[-1].code,
                           "inside": parent.code if parent else ""})
    return units


def ensure_unit(user, consignment, kind, seq, *, parent=None):
    """Get or create the unit with an explicit number (used by the CSV import)."""
    prefix = HandlingUnit.PREFIX[kind]
    unit = HandlingUnit.objects.filter(consignment=consignment, kind=kind, seq=seq).first()
    if unit is None:
        unit = HandlingUnit.objects.create(
            code=ids.child_code(consignment.code, prefix, seq), consignment=consignment, kind=kind, seq=seq,
            created_by=user,
        )
        ids.bump_to(f"{consignment.code}:{prefix}", seq)
    if parent is not None and unit.parent_id != parent.pk:
        _check_nesting(kind, parent)
        previous = unit.parent
        unit.parent = parent
        unit.save(update_fields=["parent"])
        Movement.objects.create(
            consignment=consignment, unit=unit, action=Movement.Action.PACK if previous is None else Movement.Action.MOVE,
            from_unit=previous, from_code=previous.code if previous else "", to_unit=parent, to_code=parent.code, by=user,
        )
    return unit


def distribute(quantity, slots, per_unit=None):
    """Split `quantity` over `slots` units: evenly (40 over 3 -> 14, 13, 13) or filling `per_unit` each."""
    if slots <= 0:
        return [quantity]
    if per_unit:
        if per_unit * slots < quantity:
            raise ServiceError(
                _("%(qty)s units do not fit: %(slots)s cartons × %(per)s per carton.")
                % {"qty": quantity, "slots": slots, "per": per_unit}
            )
        counts, remaining = [], quantity
        for _slot in range(slots):
            take = min(per_unit, remaining)
            counts.append(take)
            remaining -= take
        return counts
    base, extra = divmod(quantity, slots)
    return [base + (1 if index < extra else 0) for index in range(slots)]


def add_items(user, consignment, item_type, quantity, *, units=(), per_unit=None, oem_serials=(), batch_no="",
              request=None):
    """
    Register `quantity` of `item_type` and pack them into `units`.
    Serialised types get one ID per unit (…-DCJM-001 … -040); batch types get one line per carton.
    """
    _require(perms.can_edit_packing(user, consignment))
    units = list(units)
    if quantity < 1 or quantity > 10000:
        raise ServiceError(_("Quantity must be between 1 and 10,000."))
    if any(unit.consignment_id != consignment.pk for unit in units):
        raise ServiceError(_("Items can only be moved within the same consignment."))
    oem_serials = [serial.strip() for serial in oem_serials if serial and serial.strip()]
    if item_type.is_serial and len(oem_serials) > quantity:
        raise ServiceError(_("More serial numbers than units."))
    counts = distribute(quantity, len(units), per_unit)
    slots = units or [None]
    key = f"{consignment.code}:{item_type.code}"
    with transaction.atomic():
        new_items = []
        if item_type.is_serial:
            number = ids.reserve(key, quantity)
            serials = iter(oem_serials)
            for unit, count in zip(slots, counts):
                for _index in range(count):
                    new_items.append(
                        Item(
                            uid=ids.child_code(consignment.code, item_type.code, number),
                            consignment=consignment, item_type=item_type, seq=number, quantity=1, unit=unit,
                            oem_serial=next(serials, ""), batch_no=batch_no, created_by=user,
                        )
                    )
                    number += 1
        else:
            lines = [(unit, count) for unit, count in zip(slots, counts) if count > 0]
            number = ids.reserve(key, len(lines))
            for unit, count in lines:
                new_items.append(
                    Item(
                        uid=ids.child_code(consignment.code, item_type.code, number),
                        consignment=consignment, item_type=item_type, seq=number, quantity=count, unit=unit,
                        batch_no=batch_no, created_by=user,
                    )
                )
                number += 1
        Item.objects.bulk_create(new_items)
        new_items = list(
            Item.objects.filter(consignment=consignment, item_type=item_type, uid__in=[i.uid for i in new_items])
            .select_related("unit")
            .order_by("seq")
        )
        Movement.objects.bulk_create(
            [
                Movement(consignment=consignment, item=item, action=Movement.Action.PACK, to_unit=item.unit,
                         to_code=item.unit.code, quantity=item.quantity, by=user)
                for item in new_items
                if item.unit_id
            ]
        )
        if item_type.controlled:
            ensure_requirements(user, consignment, settings.PORTAL["CONTROLLED_ITEM_DOCS"], reason="controlled item")
        _start_packing(user, consignment)
        audit.log(user, "packing.items_added", consignment, request=request,
                  details={"item_type": item_type.code, "quantity": quantity, "first": new_items[0].uid,
                           "last": new_items[-1].uid, "units": [u.code for u in units]})
    return new_items


def move(user, obj, to_unit, *, reason="", place="", quantity=None, request=None):
    """
    Put an item or a unit into `to_unit` (None = take it out, loose).
    The object's ID never changes; only its location does, and the move is logged.
    """
    consignment = obj.consignment
    _require(perms.can_edit_packing(user, consignment))
    if to_unit is not None and to_unit.consignment_id != consignment.pk:
        raise ServiceError(_("Items can only be moved within the same consignment."))
    action = Movement.Action.MOVE if to_unit is not None else Movement.Action.UNPACK
    with transaction.atomic():
        if isinstance(obj, HandlingUnit):
            if to_unit is not None and to_unit.pk == obj.pk:
                raise ServiceError(_("A unit cannot be placed inside itself."))
            _check_nesting(obj.kind, to_unit)
            from_unit = obj.parent
            if (from_unit.pk if from_unit else None) == (to_unit.pk if to_unit else None):
                raise ServiceError(_("It is already there."))
            obj.parent = to_unit
            obj.save(update_fields=["parent"])
            movement = Movement.objects.create(
                consignment=consignment, unit=obj, action=action, from_unit=from_unit,
                from_code=from_unit.code if from_unit else "", to_unit=to_unit, to_code=to_unit.code if to_unit else "",
                reason=reason, place=place, by=user,
            )
            subject = obj.code
        else:
            from_unit = obj.unit
            if (from_unit.pk if from_unit else None) == (to_unit.pk if to_unit else None):
                raise ServiceError(_("It is already there."))
            if quantity is not None and not 1 <= quantity <= obj.quantity:
                raise ServiceError(_("Quantity must be between 1 and %(max)s.") % {"max": obj.quantity})
            if quantity is not None and quantity < obj.quantity:
                # Part of a batch line moves: it becomes a new line with its own ID.
                number = ids.reserve(f"{consignment.code}:{obj.item_type.code}")
                moved = Item.objects.create(
                    uid=ids.child_code(consignment.code, obj.item_type.code, number), consignment=consignment,
                    item_type=obj.item_type, seq=number, quantity=quantity, unit=to_unit, batch_no=obj.batch_no,
                    created_by=user,
                )
                obj.quantity -= quantity
                obj.save(update_fields=["quantity"])
                Movement.objects.create(
                    consignment=consignment, item=obj, action=Movement.Action.SPLIT, from_unit=from_unit,
                    from_code=from_unit.code if from_unit else "", quantity=quantity,
                    reason=f"-> {moved.uid} ({quantity})", place=place,
                    by=user,
                )
                obj = moved
            else:
                obj.unit = to_unit
                obj.save(update_fields=["unit"])
            movement = Movement.objects.create(
                consignment=consignment, item=obj, action=action, from_unit=from_unit,
                from_code=from_unit.code if from_unit else "", to_unit=to_unit, to_code=to_unit.code if to_unit else "",
                quantity=obj.quantity, reason=reason, place=place, by=user,
            )
            subject = obj.uid
        audit.log(user, "packing.moved", obj, consignment=consignment, request=request,
                  details={"from": movement.from_code, "to": movement.to_code, "reason": reason, "place": place,
                           "quantity": movement.quantity},
                  label=subject)
    return movement


def update_unit(user, unit, data, request=None):
    _require(perms.can_edit_packing(user, unit.consignment))
    fields = ["container_no", "seal_no", "length_cm", "width_cm", "height_cm", "gross_kg", "marks"]
    changes = {}
    for field in fields:
        if field in data and getattr(unit, field) != data[field]:
            changes[field] = [getattr(unit, field), data[field]]
            setattr(unit, field, data[field])
    if changes:
        unit.save()
        audit.log(user, "packing.unit_updated", unit, request=request, details={"changes": changes})
    return changes


def update_item(user, item, data, request=None):
    _require(perms.can_edit_packing(user, item.consignment))
    changes = {}
    for field in ("oem_serial", "batch_no"):
        if field in data and getattr(item, field) != data[field]:
            changes[field] = [getattr(item, field), data[field]]
            setattr(item, field, data[field])
    if changes:
        item.save()
        audit.log(user, "packing.item_updated", item, request=request, details={"changes": changes})
    return changes


def receive(user, obj, *, condition="good", include_contents=False, place="", request=None):
    """Receiver confirms a unit or item physically arrived (and in what condition)."""
    consignment = obj.consignment
    _require(perms.can_receive(user, consignment))
    now = timezone.now()
    targets_units, targets_items = [], []
    if isinstance(obj, HandlingUnit):
        targets_units.append(obj)
        if include_contents:
            nested = obj.descendants()
            targets_units.extend(unit for unit in nested if unit.received_at is None)
            holder_ids = [obj.pk] + [unit.pk for unit in nested]
            targets_items.extend(Item.objects.filter(unit_id__in=holder_ids, received_at__isnull=True, is_archived=False))
    else:
        targets_items.append(obj)
    with transaction.atomic():
        for target in targets_units + targets_items:
            target.received_at = now
            target.received_by = user
            if target is obj:
                target.condition = condition
            target.save(update_fields=["received_at", "received_by", "condition"])
            Movement.objects.create(
                consignment=consignment,
                unit=target if isinstance(target, HandlingUnit) else None,
                item=target if isinstance(target, Item) else None,
                action=Movement.Action.RECEIVE,
                quantity=getattr(target, "quantity", 1),
                reason=target.condition if target is obj else "",
                place=place,
                by=user,
                at=now,
            )
        audit.log(user, "receiving.received", obj, consignment=consignment, request=request,
                  details={"condition": condition, "units": len(targets_units), "items": len(targets_items)},
                  label=getattr(obj, "code", None) or obj.uid)
    return len(targets_units), len(targets_items)


def reconciliation(consignment):
    """Expected vs. received, for the Receiver's check and the Oversight dashboard."""
    units = list(consignment.units.filter(is_archived=False))
    items = list(consignment.items.filter(is_archived=False).select_related("item_type", "unit"))
    units_damaged = [unit for unit in units if unit.condition == HandlingUnit.Condition.DAMAGED]
    items_damaged = [item for item in items if item.condition == Item.Condition.DAMAGED]
    return {
        "units_total": len(units),
        "units_received": sum(1 for unit in units if unit.received_at),
        "units_damaged": units_damaged,
        "units_missing": [unit for unit in units if not unit.received_at],
        "items_total": len(items),
        "items_received": sum(1 for item in items if item.received_at),
        "items_damaged": items_damaged,
        "items_missing": [item for item in items if not item.received_at],
        "damaged_total": len(units_damaged) + len(items_damaged),
    }


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

MAGIC = {
    ".pdf": (b"%PDF",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".xlsx": (b"PK\x03\x04",),
    ".docx": (b"PK\x03\x04",),
    ".xls": (b"\xd0\xcf\x11\xe0",),
    ".doc": (b"\xd0\xcf\x11\xe0",),
    ".csv": None,
    ".txt": None,
}


def validate_upload(uploaded):
    extension = os.path.splitext(uploaded.name)[1].lower()
    if extension not in MAGIC:
        raise ServiceError(
            _("File type %(ext)s is not accepted. Use PDF, JPG, PNG, TIFF, Excel, Word, CSV or TXT.") % {"ext": extension or "?"}
        )
    limit = settings.PORTAL["UPLOAD_MAX_MB"] * 1024 * 1024
    if uploaded.size > limit:
        raise ServiceError(_("The file is larger than %(mb)s MB.") % {"mb": settings.PORTAL["UPLOAD_MAX_MB"]})
    if uploaded.size == 0:
        raise ServiceError(_("The file is empty."))
    uploaded.seek(0)
    head = uploaded.read(8)
    uploaded.seek(0)
    signatures = MAGIC[extension]
    if signatures is not None and not any(head.startswith(signature) for signature in signatures):
        raise ServiceError(_("The file content does not match its %(ext)s extension.") % {"ext": extension})
    if signatures is None and b"\x00" in head:
        raise ServiceError(_("The file content does not match its %(ext)s extension.") % {"ext": extension})


def clamav_scan(uploaded):
    """Stream the file to a clamd daemon (INSTREAM). Returns None if clean, else the signature name."""
    target = settings.PORTAL["CLAMAV"]
    if not target:
        return None
    host, _sep, port = target.rpartition(":")
    uploaded.seek(0)
    with socket.create_connection((host or "clamav", int(port or 3310)), timeout=30) as conn:
        conn.sendall(b"zINSTREAM\0")
        for chunk in uploaded.chunks(64 * 1024):
            conn.sendall(struct.pack("!L", len(chunk)) + chunk)
        conn.sendall(struct.pack("!L", 0))
        reply = conn.recv(4096).decode("utf-8", "replace").strip("\0\n ")
    uploaded.seek(0)
    if reply.endswith("OK"):
        return None
    return reply.rsplit(":", 1)[-1].replace("FOUND", "").strip() or reply


def upload_document(user, consignment, doc_type, uploaded, *, note="", request=None):
    _require(perms.can_upload(user, consignment, doc_type))
    validate_upload(uploaded)
    try:
        infection = clamav_scan(uploaded)
    except OSError as exc:
        raise ServiceError(_("The virus scanner is unavailable; please try again later.")) from exc
    if infection:
        audit.log(user, "document.rejected_malware", consignment, request=request,
                  details={"name": uploaded.name, "signature": infection})
        raise ServiceError(_("The file was rejected by the virus scanner."))
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)
    party = perms.party_in(user, consignment)
    with transaction.atomic():
        # Lock the consignment row so two uploads cannot get the same version number.
        Consignment.objects.select_for_update().filter(pk=consignment.pk).first()
        latest = (
            ConsignmentDocument.objects.filter(consignment=consignment, doc_type=doc_type)
            .aggregate(v=Max("version"))["v"]
        )
        document = ConsignmentDocument(
            consignment=consignment,
            doc_type=doc_type,
            version=(latest or 0) + 1,
            original_name=os.path.basename(uploaded.name)[:255],
            content_type=mimetypes.guess_type(uploaded.name)[0] or "application/octet-stream",
            size=uploaded.size,
            sha256=digest.hexdigest(),
            note=note,
            uploaded_by=user,
            uploaded_by_party=party,
        )
        document.file.save(uploaded.name, uploaded, save=False)
        document.save()
        audit.log(user, "document.uploaded", document, request=request,
                  details={"type": doc_type.code, "version": document.version, "file": document.original_name,
                           "size": document.size, "sha256": document.sha256})
    _inform(user, consignment, "doc_uploaded", {"doc_type": doc_type.code, "version": document.version},
            f"/consignments/{consignment.code}/documents/")
    return document


def archive_document(user, document, reason, request=None):
    _require(perms.can_archive_document(user, document))
    if not reason.strip():
        raise ServiceError(_("Please give a reason."))
    document.is_archived = True
    document.archived_by = user
    document.archived_at = timezone.now()
    document.archive_reason = reason.strip()
    document.save(update_fields=["is_archived", "archived_by", "archived_at", "archive_reason"])
    audit.log(user, "document.archived", document, request=request, details={"reason": reason})


def ensure_requirements(user, consignment, codes, reason=""):
    added = []
    for doc_type in DocumentType.objects.filter(code__in=codes, is_active=True):
        requirement, created = DocumentRequirement.objects.get_or_create(
            consignment=consignment, doc_type=doc_type, defaults={"added_by": user}
        )
        if created:
            added.append(doc_type.code)
    if added:
        audit.log(user, "document.required", consignment, details={"added": added, "reason": reason})
    return added


def add_requirement(user, consignment, doc_type, due_override=None, request=None):
    _require(perms.can_manage_requirements(user, consignment))
    requirement, created = DocumentRequirement.objects.get_or_create(
        consignment=consignment, doc_type=doc_type, defaults={"added_by": user, "due_override": due_override}
    )
    if not created and due_override:
        requirement.due_override = due_override
        requirement.save(update_fields=["due_override"])
    audit.log(user, "document.required", consignment, request=request,
              details={"added": [doc_type.code], "due": str(due_override or "")})
    if created:
        _inform(user, consignment, "doc_required", {"doc_type": doc_type.code},
                f"/consignments/{consignment.code}/documents/")
    return requirement


def waive_requirement(user, requirement, reason, request=None):
    _require(perms.can_manage_requirements(user, requirement.consignment))
    if not reason.strip():
        raise ServiceError(_("Please give a reason."))
    requirement.waived = True
    requirement.waived_reason = reason.strip()
    requirement.waived_by = user
    requirement.save(update_fields=["waived", "waived_reason", "waived_by"])
    audit.log(user, "document.waived", requirement, consignment=requirement.consignment, request=request,
              details={"type": requirement.doc_type.code, "reason": reason})


# ---------------------------------------------------------------------------
# Tags and status updates
# ---------------------------------------------------------------------------

def add_tag(user, consignment, kind, *, note="", revised_eta=None, request=None):
    """Tag a consignment (Delayed, On hold, Priority) or post a status update; both parties and Oversight see it."""
    _require(perms.can_tag(user, consignment))
    if kind not in ConsignmentTag.Kind.values:
        raise ServiceError(_("Unknown tag."))
    note = (note or "").strip()
    if not note and kind != ConsignmentTag.Kind.PRIORITY:
        raise ServiceError(_("Please write what happened."))
    if kind != ConsignmentTag.Kind.DELAY:
        revised_eta = None
    if kind != ConsignmentTag.Kind.UPDATE and any(tag.kind == kind for tag in consignment.active_tags()):
        raise ServiceError(_("This consignment already has that tag. Post a status update, or remove the tag first."))
    party = perms.party_in(user, consignment) or user.profile.party
    with transaction.atomic():
        tag = ConsignmentTag.objects.create(
            consignment=consignment, kind=kind, note=note, note_lang=detect_language(note) if note else "",
            revised_eta=revised_eta, added_by=user, added_by_party=party,
        )
        audit.log(user, "tag.update" if tag.is_update else "tag.added", consignment, request=request,
                  details={"tag": kind, "note": note, "revised_eta": str(revised_eta or "")})
    if note:
        _translate_later((note, tag.note_lang))
    _inform(user, consignment, "status_update" if tag.is_update else "tag_added",
            {"tag": kind, "note": note}, f"/consignments/{consignment.code}/")
    return tag


def remove_tag(user, tag, request=None):
    consignment = tag.consignment
    _require(perms.can_tag(user, consignment))
    if not tag.is_active:
        raise ServiceError(_("This tag has already been removed."))
    tag.removed_at = timezone.now()
    tag.removed_by = user
    tag.save(update_fields=["removed_at", "removed_by"])
    audit.log(user, "tag.removed", consignment, request=request, details={"tag": tag.kind, "tag_id": tag.pk})
    _inform(user, consignment, "tag_removed", {"tag": tag.kind}, f"/consignments/{consignment.code}/")


# ---------------------------------------------------------------------------
# Remarks, queries and replies
# ---------------------------------------------------------------------------

def _translate_later(*texts):
    for text, lang in texts:
        mt.translate_and_store(text, lang)


def create_thread(user, consignment, *, category, subject, body, unit=None, item=None, document=None, request=None):
    _require(perms.can_comment(user, consignment))
    subject, body = subject.strip(), body.strip()
    if not subject or not body:
        raise ServiceError(_("Subject and message are required."))
    party = perms.party_in(user, consignment)
    now = timezone.now()
    with transaction.atomic():
        thread = Thread.objects.create(
            consignment=consignment, category=category, subject=subject, subject_lang=detect_language(subject),
            raised_by=user, raised_by_party=party, unit=unit, item=item, document=document,
            created_at=now, updated_at=now,
        )
        message = Message.objects.create(thread=thread, author=user, author_party=party, body=body,
                                         lang=detect_language(body), created_at=now)
        audit.log(user, "thread.created", thread, request=request, details={"category": category, "subject": subject})
    _translate_later((thread.subject, thread.subject_lang), (message.body, message.lang))
    _inform(user, consignment, "thread_new", {"category": category, "subject": subject},
            f"/threads/{thread.pk}/")
    return thread


def reply(user, thread, body, request=None):
    consignment = thread.consignment
    _require(perms.can_comment(user, consignment))
    body = body.strip()
    if not body:
        raise ServiceError(_("Message is required."))
    party = perms.party_in(user, consignment)
    now = timezone.now()
    with transaction.atomic():
        message = Message.objects.create(thread=thread, author=user, author_party=party, body=body,
                                         lang=detect_language(body), created_at=now)
        if thread.state == Thread.State.OPEN and party != thread.raised_by_party:
            thread.state = Thread.State.ANSWERED
        elif thread.state == Thread.State.ANSWERED and party == thread.raised_by_party:
            thread.state = Thread.State.OPEN
        elif thread.state == Thread.State.RESOLVED:
            thread.state = Thread.State.OPEN
            thread.resolved_at = None
        thread.updated_at = now
        thread.save(update_fields=["state", "updated_at", "resolved_at"])
        audit.log(user, "thread.replied", thread, request=request, details={"state": thread.state})
    _translate_later((message.body, message.lang))
    _inform(user, consignment, "thread_reply", {"subject": thread.subject}, f"/threads/{thread.pk}/")
    return message


def set_thread_state(user, thread, state, request=None):
    consignment = thread.consignment
    _require(perms.can_comment(user, consignment))
    party = perms.party_in(user, consignment)
    if state == Thread.State.RESOLVED and party != thread.raised_by_party:
        raise ServiceError(_("Only the party that raised this can mark it resolved."))
    thread.state = state
    thread.updated_at = timezone.now()
    thread.resolved_at = timezone.now() if state == Thread.State.RESOLVED else None
    thread.resolved_by = user if state == Thread.State.RESOLVED else None
    thread.save(update_fields=["state", "updated_at", "resolved_at", "resolved_by"])
    audit.log(user, "thread.state", thread, request=request, details={"state": state})
    _inform(user, consignment, "thread_state", {"subject": thread.subject, "state": state},
            f"/threads/{thread.pk}/", email_counterpart=False)
