from django.contrib import messages
from django.http import Http404

from logistics import permissions as perms
from logistics.models import Consignment, HandlingUnit, Item
from logistics.services import ServiceError


def get_consignment(user, code):
    consignment = Consignment.objects.select_related("shipper", "receiver").filter(code=code).first()
    if consignment is None or not perms.can_view(user, consignment):
        raise Http404
    return consignment


def get_unit(user, code):
    unit = HandlingUnit.objects.select_related("consignment__shipper", "consignment__receiver", "parent").filter(code=code).first()
    if unit is None or not perms.can_view(user, unit.consignment):
        raise Http404
    return unit


def get_item(user, uid):
    item = Item.objects.select_related("consignment__shipper", "consignment__receiver", "item_type", "unit").filter(uid=uid).first()
    if item is None or not perms.can_view(user, item.consignment):
        raise Http404
    return item


def run(http_request, operation, /, *args, success=None, **kwargs):
    """
    Run a service call; turn a ServiceError into a message. Returns (ok, result).
    The first two parameters are positional-only so `request=` can be passed on to the service.
    """
    try:
        result = operation(*args, **kwargs)
    except ServiceError as exc:
        messages.error(http_request, str(exc))
        return False, None
    if success:
        messages.success(http_request, success)
    return True, result


def consignment_context(user, consignment, active_tab):
    return {
        "consignment": consignment,
        "active_tab": active_tab,
        "party": perms.party_in(user, consignment),
        "can_edit_header": perms.can_edit_header(user, consignment),
        "can_edit_packing": perms.can_edit_packing(user, consignment),
        "can_receive": perms.can_receive(user, consignment),
        "can_comment": perms.can_comment(user, consignment),
        "open_threads": consignment.threads.exclude(state="resolved").count(),
    }
