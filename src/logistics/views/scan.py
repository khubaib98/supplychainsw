import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core.ids import normalize_scan
from logistics import permissions as perms
from logistics import services
from logistics.models import Consignment, HandlingUnit, Item


@login_required
def scanner(request):
    profile = perms.profile_of(request.user)
    context_code = normalize_scan(request.GET.get("c", ""))
    if context_code and not perms.visible_consignments(request.user).filter(code=context_code).exists():
        context_code = ""
    return render(request, "logistics/scan.html", {
        "can_edit": profile is not None and profile.can_edit,
        "context_code": context_code,
    })


def resolve(user, raw, context_code=""):
    """
    Find what a scanned/typed code refers to, limited to what the user may see.
    With a consignment context, short IDs such as CTN-017 or DCJM-017 also work.
    """
    code = normalize_scan(raw)
    if not code:
        return None
    visible = perms.visible_consignments(user)
    candidates = [code]
    context_code = normalize_scan(context_code)
    if context_code and not code.startswith(context_code):
        candidates.append(f"{context_code}-{code}")
    unit = HandlingUnit.objects.select_related("consignment", "parent").filter(code__in=candidates, consignment__in=visible).first()
    if unit:
        return unit
    item = Item.objects.select_related("consignment", "item_type", "unit").filter(uid__in=candidates, consignment__in=visible).first()
    if item:
        return item
    consignment = visible.filter(code=code).first()
    if consignment:
        return consignment
    # Last resort: a manufacturer serial number printed by the factory.
    return (
        Item.objects.select_related("consignment", "item_type", "unit")
        .filter(oem_serial__iexact=str(raw).strip(), consignment__in=visible)
        .first()
    )


def describe(obj):
    if isinstance(obj, Consignment):
        return {
            "kind": "consignment",
            "code": obj.code,
            "label": f"{obj.code} · {obj.get_status_display()}",
            "where": f"{obj.origin} → {obj.destination}",
            "url": reverse("logistics:consignment", args=[obj.code]),
        }
    if isinstance(obj, HandlingUnit):
        path = " › ".join(u.short for u in obj.ancestors()) or _("not inside anything")
        return {
            "kind": "unit",
            "code": obj.code,
            "label": f"{obj.short} · {obj.get_kind_display()}",
            "where": f"{obj.consignment.code} › {path}",
            "received": bool(obj.received_at),
            "url": reverse("logistics:unit", args=[obj.code]),
        }
    unit = obj.unit
    path = " › ".join(u.short for u in unit.ancestors() + [unit]) if unit else _("not inside anything")
    return {
        "kind": "item",
        "code": obj.uid,
        "label": f"{obj.short} · {obj.item_type.name()}" + (f" × {obj.quantity}" if obj.quantity > 1 else ""),
        "where": f"{obj.consignment.code} › {path}",
        "received": bool(obj.received_at),
        "url": reverse("logistics:item", args=[obj.uid]),
    }


def _error(message, status=200):
    return JsonResponse({"ok": False, "message": message}, status=status)


@login_required
@require_POST
def scan_api(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _error(_("Bad request."), status=400)
    mode = payload.get("mode", "lookup")
    context_code = payload.get("consignment", "")
    obj = resolve(request.user, payload.get("code", ""), context_code)
    if obj is None:
        return _error(_("Not found: %(code)s") % {"code": normalize_scan(payload.get("code", "")) or "—"})
    info = describe(obj)
    try:
        if mode == "lookup":
            return JsonResponse({"ok": True, "message": info["label"], **info})
        if mode == "target":
            if not isinstance(obj, HandlingUnit):
                return _error(_("Scan a container, pallet, crate, carton or box as the destination."))
            if not perms.can_edit_packing(request.user, obj.consignment):
                return _error(_("You cannot change the packing of this consignment at its current stage."))
            return JsonResponse({"ok": True, "message": _("Destination: %(label)s") % {"label": info["label"]}, **info})
        if mode == "move":
            target = resolve(request.user, payload.get("target", ""), context_code)
            if not isinstance(target, HandlingUnit):
                return _error(_("Scan the destination first."))
            if isinstance(obj, Consignment):
                return _error(_("Scan a unit or an item."))
            if obj.pk == target.pk and isinstance(obj, HandlingUnit):
                return _error(_("A unit cannot be placed inside itself."))
            services.move(request.user, obj, target, reason=payload.get("reason", ""),
                          place=payload.get("place", ""), request=request)
            obj.refresh_from_db()
            info = describe(obj)
            return JsonResponse({"ok": True, "message": _("%(what)s → %(where)s") % {"what": info["label"], "where": target.short}, **info})
        if mode == "receive":
            if isinstance(obj, Consignment):
                return _error(_("Scan a unit or an item."))
            units, items = services.receive(
                request.user, obj, condition=payload.get("condition", "good"),
                include_contents=bool(payload.get("include_contents")), place=payload.get("place", ""), request=request,
            )
            return JsonResponse({"ok": True, "message": _("Received: %(label)s (%(units)s unit(s), %(items)s item(s))")
                                 % {"label": info["label"], "units": units, "items": items}, **info})
    except services.ServiceError as exc:
        return _error(str(exc))
    except PermissionDenied:
        return _error(_("You are not allowed to do that on this consignment at its current stage."))
    return _error(_("Unknown mode."), status=400)
