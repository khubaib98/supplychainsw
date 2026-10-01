"""
Who may see and do what.

Three organisation roles (Shipper, Receiver, Oversight) x three access levels
(Manager, Operator, Viewer). On top of that, *ownership* decides which party
may act on which record:

  * each party only sees consignments it is part of; Oversight sees all
  * packing may only be changed by the party that has custody of the goods
    (Shipper until export clearance, Receiver from arrival, nobody in transit)
  * documents are uploaded by the party that owes them; only the uploading
    party's managers may archive them; nothing is ever hard-deleted
  * Oversight changes no data. Its managers may request (or waive) documents,
    and its managers and operators may tag consignments and post status
    updates; Oversight viewers only read
"""
from django.core.exceptions import PermissionDenied

from accounts.models import Organization, Profile
from logistics.models import Consignment, DocumentType

SHIPPER = Organization.Kind.SHIPPER
RECEIVER = Organization.Kind.RECEIVER
OVERSIGHT = Organization.Kind.OVERSIGHT


def profile_of(user):
    return getattr(user, "profile", None) if user is not None and user.is_authenticated else None


def sees_everything(user):
    profile = profile_of(user)
    return bool(user.is_superuser or (profile and profile.party == OVERSIGHT))


def visible_consignments(user, include_archived=False):
    queryset = Consignment.objects.select_related("shipper", "receiver")
    if not include_archived:
        queryset = queryset.filter(is_archived=False)
    if sees_everything(user):
        return queryset
    profile = profile_of(user)
    if profile is None:
        return queryset.none()
    if profile.party == SHIPPER:
        return queryset.filter(shipper=profile.organization)
    if profile.party == RECEIVER:
        return queryset.filter(receiver=profile.organization)
    return queryset.none()


def party_in(user, consignment):
    """'shipper' / 'receiver' if the user's organisation is that party of this consignment."""
    profile = profile_of(user)
    if profile is None:
        return None
    if profile.party == SHIPPER and consignment.shipper_id == profile.organization_id:
        return SHIPPER
    if profile.party == RECEIVER and consignment.receiver_id == profile.organization_id:
        return RECEIVER
    return None


def can_view(user, consignment):
    return sees_everything(user) or party_in(user, consignment) is not None


def can_edit(user, consignment):
    profile = profile_of(user)
    return bool(profile and profile.can_edit and party_in(user, consignment))


def is_manager_of(user, consignment):
    profile = profile_of(user)
    return bool(profile and profile.is_manager and party_in(user, consignment))


def can_create_consignment(user):
    profile = profile_of(user)
    return bool(profile and profile.can_edit and profile.party == SHIPPER)


def can_edit_header(user, consignment):
    return can_edit(user, consignment) and party_in(user, consignment) == SHIPPER and consignment.is_active


def can_edit_packing(user, consignment):
    return can_edit(user, consignment) and party_in(user, consignment) == consignment.custodian


def can_receive(user, consignment):
    return (
        can_edit(user, consignment)
        and party_in(user, consignment) == RECEIVER
        and consignment.status_reached(Consignment.Status.ARRIVED)
        and consignment.status != Consignment.Status.CLOSED
    )


def allowed_statuses(user, consignment):
    """
    Milestones this user may record on this consignment. The Shipper records
    milestones up to "in transit" and loses that right once the Receiver has
    taken over; the Receiver may take over from "dispatched" onwards (so a
    shipment is never stuck because the other side forgot an update).
    """
    party = party_in(user, consignment)
    if not can_edit(user, consignment) or consignment.is_archived:
        return []
    index = consignment.status_index
    order = Consignment.STATUS_ORDER
    if party == SHIPPER and index > order[Consignment.Status.IN_TRANSIT]:
        return []
    if party == RECEIVER and index < order[Consignment.Status.DISPATCHED]:
        return []
    return [
        value
        for value in Consignment.Status.values
        if Consignment.STATUS_OWNER[value] == party and value != consignment.status
    ]


def can_upload(user, consignment, doc_type):
    party = party_in(user, consignment)
    if not can_edit(user, consignment):
        return False
    return doc_type.responsible in (party, DocumentType.Party.ANY)


def uploadable_types(user, consignment):
    return [dt for dt in DocumentType.objects.filter(is_active=True) if can_upload(user, consignment, dt)]


def can_archive_document(user, document):
    return is_manager_of(user, document.consignment) and party_in(user, document.consignment) == document.uploaded_by_party


def is_oversight(user, levels=(Profile.Level.MANAGER,)):
    profile = profile_of(user)
    return bool(profile and profile.party == OVERSIGHT and profile.level in levels)


def can_manage_requirements(user, consignment):
    """Add or waive required documents: either party's managers, and Oversight managers."""
    return is_manager_of(user, consignment) or is_oversight(user)


def can_tag(user, consignment):
    """Tags (Delayed, On hold, Priority) and status updates: managers and operators of both parties and Oversight."""
    if consignment.is_archived:
        return False
    return can_edit(user, consignment) or is_oversight(user, (Profile.Level.MANAGER, Profile.Level.OPERATOR))


def can_comment(user, consignment):
    return can_edit(user, consignment)


def can_manage_catalog(user):
    profile = profile_of(user)
    return bool(profile and profile.is_manager)


def sees_audit(user):
    profile = profile_of(user)
    return bool(user.is_superuser or (profile and (profile.party == OVERSIGHT or profile.level == Profile.Level.MANAGER)))


def require(condition):
    if not condition:
        raise PermissionDenied
