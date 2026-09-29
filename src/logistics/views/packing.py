import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from logistics import permissions as perms
from logistics import services
from logistics.forms import AddItemsForm, AddUnitsForm, ImportForm, ItemEditForm, MoveForm, ReceiveForm, UnitEditForm
from logistics.importer import import_packing_list
from logistics.models import Consignment, HandlingUnit, Item, Movement
from logistics.reports import packing_rows
from logistics.tree import build_tree, layer_map
from logistics.views.common import consignment_context, get_consignment, get_item, get_unit, run


@login_required
def packing(request, code):
    consignment = get_consignment(request.user, code)
    tree = build_tree(consignment)
    can_edit = perms.can_edit_packing(request.user, consignment)
    highlight = request.GET.get("hl", "")
    highlight_path = set()
    if highlight:
        target = (
            HandlingUnit.objects.filter(code=highlight, consignment=consignment).first()
            or Item.objects.filter(uid=highlight, consignment=consignment).select_related("unit").first()
        )
        if isinstance(target, Item):
            highlight_path = {u.code for u in (target.unit.ancestors() + [target.unit])} if target.unit else set()
        elif target is not None:
            highlight_path = {u.code for u in target.ancestors()}
    context = {
        **consignment_context(request.user, consignment, "packing"),
        "tree": tree,
        "kind_totals": [(label, tree.kind_totals[kind]) for kind, label in HandlingUnit.Kind.choices if tree.kind_totals.get(kind)],
        "show_items": request.GET.get("items", "1") != "0",
        "highlight": highlight,
        "highlight_path": highlight_path,
        "units_form": AddUnitsForm(consignment=consignment) if can_edit else None,
        "items_form": AddItemsForm(consignment=consignment) if can_edit else None,
        "import_form": ImportForm() if can_edit else None,
        "custody_note": consignment.custodian,
    }
    return render(request, "logistics/packing.html", context)


@login_required
@require_POST
def add_units(request, code):
    consignment = get_consignment(request.user, code)
    perms.require(perms.can_edit_packing(request.user, consignment))
    form = AddUnitsForm(request.POST, consignment=consignment)
    if form.is_valid():
        data = form.cleaned_data
        attrs = {key: data[key] for key in ("container_no", "seal_no", "gross_kg", "length_cm", "width_cm", "height_cm") if data.get(key)}
        ok, units = run(request, services.create_units, request.user, consignment, data["kind"], data["count"],
                        parent=data["parent"], request=request, **attrs)
        if ok:
            messages.success(request, _("Created %(first)s … %(last)s.") % {"first": units[0].short, "last": units[-1].short})
    else:
        messages.error(request, _("Please correct the form: %(errors)s") % {"errors": form.errors.as_text()})
    return redirect("logistics:packing", code=code)


@login_required
@require_POST
def add_items(request, code):
    consignment = get_consignment(request.user, code)
    perms.require(perms.can_edit_packing(request.user, consignment))
    form = AddItemsForm(request.POST, consignment=consignment)
    if form.is_valid():
        data = form.cleaned_data
        ok, items = run(
            request, services.add_items, request.user, consignment, data["item_type"], data["quantity"],
            units=data["units"], per_unit=data["per_unit"], oem_serials=data["oem_serials"], batch_no=data["batch_no"],
            request=request,
        )
        if ok:
            messages.success(request, _("Registered %(first)s … %(last)s.") % {"first": items[0].short, "last": items[-1].short})
    else:
        messages.error(request, _("Please correct the form: %(errors)s") % {"errors": form.errors.as_text()})
    return redirect("logistics:packing", code=code)


@login_required
def import_csv(request, code):
    consignment = get_consignment(request.user, code)
    perms.require(perms.can_edit_packing(request.user, consignment))
    form = ImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        if upload.size > 5 * 1024 * 1024:
            messages.error(request, _("The file is larger than %(mb)s MB.") % {"mb": 5})
        else:
            try:
                result = import_packing_list(request.user, consignment, upload.read(), request=request)
            except services.ServiceError as exc:
                result = {"errors": [str(exc)]}
            if not result["errors"]:
                messages.success(
                    request,
                    _("Imported %(rows)s rows: %(items)s item IDs in %(cartons)s cartons.")
                    % {"rows": result["rows"], "items": result["items"], "cartons": result["cartons"]},
                )
                return redirect("logistics:packing", code=code)
    context = {**consignment_context(request.user, consignment, "packing"), "form": form, "result": result}
    return render(request, "logistics/import.html", context)


@login_required
def packing_list(request, code):
    consignment = get_consignment(request.user, code)
    rows = packing_rows(consignment)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{consignment.code}-packing-list.csv"'
        response.write("﻿")  # BOM so Excel shows Chinese/Urdu correctly
        writer = csv.writer(response)
        writer.writerow(["item_id", "item_code", "description_en", "description_zh", "quantity", "oem_serial",
                         "batch_no", "in_unit", "path", "received", "condition"])
        items = consignment.items.filter(is_archived=False).select_related("item_type", "unit").order_by("item_type__code", "seq")
        for item in items:
            path = " > ".join(u.short for u in (item.unit.ancestors() + [item.unit])) if item.unit else ""
            writer.writerow([
                item.uid, item.item_type.code, item.item_type.name_en, item.item_type.name_zh, item.quantity,
                item.oem_serial, item.batch_no, item.unit.code if item.unit else "", path,
                timezone.localtime(item.received_at).strftime("%Y-%m-%d %H:%M") if item.received_at else "",
                item.condition,
            ])
        return response
    totals = {
        "quantity": sum(row["quantity"] for row in rows),
        "packages": consignment.units.filter(is_archived=False, kind__in=[HandlingUnit.Kind.CARTON, HandlingUnit.Kind.CRATE]).count(),
        "gross": sum((u.gross_kg or 0) for u in consignment.units.filter(is_archived=False, kind__in=[HandlingUnit.Kind.CARTON, HandlingUnit.Kind.CRATE])),
    }
    return render(request, "logistics/packing_list.html", {"consignment": consignment, "rows": rows, "totals": totals,
                                                           "printed_at": timezone.now()})


@login_required
def labels(request, code):
    """Printable labels. ?what=units&kind=carton | ?what=items&unit=<code> | ?id=<code>."""
    consignment = get_consignment(request.user, code)
    single = request.GET.get("id")
    units, items = [], []
    if single:
        units = list(consignment.units.filter(code=single))
        items = list(consignment.items.filter(uid=single).select_related("item_type"))
        if not units and not items:
            raise Http404
    elif request.GET.get("what") == "items":
        queryset = consignment.items.filter(is_archived=False).select_related("item_type")
        within = request.GET.get("unit")
        if within:
            holder = consignment.units.filter(code=within).first()
            if holder is None:
                raise Http404
            holders = [holder] + holder.descendants()
            queryset = queryset.filter(unit__in=holders)
        items = list(queryset.order_by("item_type__code", "seq"))
    else:
        queryset = consignment.units.filter(is_archived=False)
        kind = request.GET.get("kind")
        if kind in HandlingUnit.Kind.values:
            queryset = queryset.filter(kind=kind)
        units = list(queryset.order_by("kind", "seq"))
    kind_totals = {}
    for unit in consignment.units.filter(is_archived=False).values_list("kind", flat=True):
        kind_totals[unit] = kind_totals.get(unit, 0) + 1
    for unit in units:
        unit.total_of_kind = kind_totals.get(unit.kind, 0)
        unit.contents_count = unit.items.count()
    return render(
        request,
        "labels/labels.html",
        {"consignment": consignment, "units": units, "items": items, "show_contents": request.GET.get("contents") == "1"},
    )


def _history_for_unit(unit):
    return Movement.objects.filter(Q(unit=unit) | Q(from_unit=unit) | Q(to_unit=unit)).select_related("item", "unit", "by")[:50]


@login_required
def unit_detail(request, code):
    unit = get_unit(request.user, code)
    consignment = unit.consignment
    can_edit = perms.can_edit_packing(request.user, consignment)
    children = list(unit.children.filter(is_archived=False).order_by("kind", "seq"))
    items = list(unit.items.filter(is_archived=False).select_related("item_type").order_by("item_type__code", "seq"))
    context = {
        **consignment_context(request.user, consignment, "packing"),
        "unit": unit,
        "layers": layer_map(unit),
        "children": children,
        "items": items,
        "history": _history_for_unit(unit),
        "move_form": MoveForm(obj=unit) if can_edit else None,
        "edit_form": UnitEditForm(instance=unit) if can_edit else None,
        "receive_form": ReceiveForm(initial={"condition": unit.condition, "include_contents": True})
        if perms.can_receive(request.user, consignment) else None,
        "threads": unit.threads.all(),
    }
    return render(request, "logistics/unit_detail.html", context)


@login_required
def item_detail(request, uid):
    item = get_item(request.user, uid)
    consignment = item.consignment
    can_edit = perms.can_edit_packing(request.user, consignment)
    same_type = consignment.items.filter(item_type=item.item_type, is_archived=False)
    context = {
        **consignment_context(request.user, consignment, "packing"),
        "item": item,
        "layers": layer_map(item),
        "position": same_type.filter(seq__lte=item.seq).count(),
        "of_total": same_type.count(),
        "history": item.movements.select_related("by")[:50],
        "move_form": MoveForm(obj=item) if can_edit else None,
        "edit_form": ItemEditForm(instance=item) if can_edit else None,
        "receive_form": ReceiveForm(initial={"condition": item.condition}) if perms.can_receive(request.user, consignment) else None,
        "threads": item.threads.all(),
    }
    return render(request, "logistics/item_detail.html", context)


def _target(request, kind, code):
    return get_unit(request.user, code) if kind == "unit" else get_item(request.user, code)


def _detail_redirect(obj):
    if isinstance(obj, HandlingUnit):
        return redirect("logistics:unit", code=obj.code)
    return redirect("logistics:item", uid=obj.uid)


@login_required
@require_POST
def move(request, kind, code):
    obj = _target(request, kind, code)
    perms.require(perms.can_edit_packing(request.user, obj.consignment))
    form = MoveForm(request.POST, obj=obj)
    if form.is_valid():
        data = form.cleaned_data
        ok, movement = run(request, services.move, request.user, obj, data["to_unit"], reason=data["reason"],
                           place=data["place"], quantity=data.get("quantity"), request=request)
        if ok:
            messages.success(request, _("Moved. The ID stays the same; only its location changed."))
            if movement.item_id and movement.item_id != getattr(obj, "pk", None) and isinstance(obj, Item):
                return redirect("logistics:item", uid=movement.item.uid)
    else:
        messages.error(request, _("Please correct the form: %(errors)s") % {"errors": form.errors.as_text()})
    return _detail_redirect(obj)


@login_required
@require_POST
def edit(request, kind, code):
    obj = _target(request, kind, code)
    perms.require(perms.can_edit_packing(request.user, obj.consignment))
    form_class = UnitEditForm if isinstance(obj, HandlingUnit) else ItemEditForm
    fresh = type(obj).objects.get(pk=obj.pk)
    form = form_class(request.POST, instance=obj)
    if form.is_valid():
        service = services.update_unit if isinstance(obj, HandlingUnit) else services.update_item
        run(request, service, request.user, fresh, form.cleaned_data, request=request, success=_("Saved."))
    else:
        messages.error(request, _("Please correct the form: %(errors)s") % {"errors": form.errors.as_text()})
    return _detail_redirect(obj)


@login_required
@require_POST
def receive(request, kind, code):
    obj = _target(request, kind, code)
    perms.require(perms.can_receive(request.user, obj.consignment))
    form = ReceiveForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        ok, counts = run(request, services.receive, request.user, obj, condition=data["condition"],
                         include_contents=data["include_contents"], place=data["place"], request=request)
        if ok:
            messages.success(request, _("Receipt recorded: %(units)s unit(s), %(items)s item(s).")
                             % {"units": counts[0], "items": counts[1]})
    return _detail_redirect(obj)


@login_required
def receiving(request, code):
    consignment = get_consignment(request.user, code)
    context = {
        **consignment_context(request.user, consignment, "receiving"),
        "rec": services.reconciliation(consignment),
        "arrived": consignment.status_reached(Consignment.Status.ARRIVED),
    }
    return render(request, "logistics/receiving.html", context)
