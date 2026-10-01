"""
Import a packing list from CSV (the Shipper usually has one in Excel already).

Columns (header row required, extra columns ignored):
    carton        carton number, e.g. 1 … 57                     (required)
    item_code     item-master code, e.g. DCJM                     (required)
    quantity      number of units / pieces                        (required)
    oem_serial    manufacturer serial no. (serialised items, qty 1)
    batch_no      batch / lot number
    pallet        pallet number the carton sits on
    container     container number (ISO 6346, e.g. MSKU1234565)
    gross_kg      carton gross weight in kg (any row of that carton)
    length_cm, width_cm, height_cm   carton dimensions in cm

Excel on Chinese Windows saves CSV as GB18030/GBK, so both that and UTF-8
are accepted. The import is all-or-nothing: any error and nothing is saved.
"""
import csv
import io
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.translation import gettext as _

from core import ids
from logistics import permissions as perms
from logistics.models import HandlingUnit, ItemType
from logistics.services import ServiceError, add_items, ensure_unit

REQUIRED = ("carton", "item_code", "quantity")
# Optional carton measurements: column -> (largest value accepted, precision stored).
MEASUREMENTS = {
    "gross_kg": (Decimal("10000"), Decimal("0.01")),
    "length_cm": (Decimal("1000"), Decimal("0.1")),
    "width_cm": (Decimal("1000"), Decimal("0.1")),
    "height_cm": (Decimal("1000"), Decimal("0.1")),
}


def decode(raw):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ServiceError(_("The file is not UTF-8 or GB18030 text."))


def _positive_int(value, row_number, column, errors):
    try:
        number = int(str(value).strip())
        if number < 1:
            raise ValueError
        return number
    except (TypeError, ValueError):
        errors.append(_("Row %(row)s: %(column)s must be a whole number ≥ 1.") % {"row": row_number, "column": column})
        return None


def _measurement(value, row_number, column, errors):
    try:
        largest, precision = MEASUREMENTS[column]
        number = Decimal(str(value).strip().replace(",", "."))
        if not Decimal("0") < number <= largest:
            raise InvalidOperation
        return number.quantize(precision)
    except InvalidOperation:
        errors.append(_("Row %(row)s: %(column)s must be a positive number.") % {"row": row_number, "column": column})
        return None


def parse(text):
    reader = csv.DictReader(io.StringIO(text))
    headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    missing = [column for column in REQUIRED if column not in headers]
    if missing:
        raise ServiceError(_("Missing column(s): %(cols)s") % {"cols": ", ".join(missing)})
    reader.fieldnames = headers
    types = {item_type.code: item_type for item_type in ItemType.objects.filter(is_active=True)}
    rows, errors = [], []
    carton_specs = {}
    for row_number, raw in enumerate(reader, start=2):
        values = {key: (value or "").strip() for key, value in raw.items() if key}
        if not any(values.values()):
            continue
        carton = _positive_int(values.get("carton"), row_number, "carton", errors)
        quantity = _positive_int(values.get("quantity"), row_number, "quantity", errors)
        pallet = _positive_int(values["pallet"], row_number, "pallet", errors) if values.get("pallet") else None
        code = values.get("item_code", "").upper()
        item_type = types.get(code)
        if item_type is None:
            errors.append(_("Row %(row)s: unknown item code “%(code)s”.") % {"row": row_number, "code": code})
        specs = {
            column: _measurement(values[column], row_number, column, errors)
            for column in MEASUREMENTS
            if values.get(column)
        }
        known = carton_specs.setdefault(carton, {})
        for column, number in specs.items():
            if number is not None and known.get(column, number) != number:
                errors.append(
                    _("Row %(row)s: carton %(carton)s already has a different %(column)s.")
                    % {"row": row_number, "carton": carton, "column": column}
                )
            elif number is not None:
                known[column] = number
        serial = values.get("oem_serial", "")
        if serial and item_type is not None and item_type.is_serial and quantity not in (None, 1):
            errors.append(_("Row %(row)s: a serial number needs quantity 1.") % {"row": row_number})
        rows.append(
            {
                "row": row_number,
                "carton": carton,
                "pallet": pallet,
                "container": values.get("container", "").upper(),
                "item_type": item_type,
                "quantity": quantity,
                "serial": serial,
                "batch": values.get("batch_no", ""),
                "specs": known,
            }
        )
    if not rows and not errors:
        errors.append(_("The file has no data rows."))
    return rows, errors


def import_packing_list(user, consignment, raw_bytes, request=None):
    perms.require(perms.can_edit_packing(user, consignment))
    rows, errors = parse(decode(raw_bytes))
    if errors:
        return {"errors": errors}
    summary = {"rows": len(rows), "items": 0, "cartons": set(), "pallets": set(), "containers": set()}
    containers = {}
    with transaction.atomic():
        for row in rows:
            container = None
            if row["container"]:
                container = containers.get(row["container"])
                if container is None:
                    container = HandlingUnit.objects.filter(
                        consignment=consignment, kind=HandlingUnit.Kind.CONTAINER, container_no=row["container"]
                    ).first()
                    if container is None:
                        prefix = HandlingUnit.PREFIX[HandlingUnit.Kind.CONTAINER]
                        seq = ids.reserve(f"{consignment.code}:{prefix}")
                        container = ensure_unit(user, consignment, HandlingUnit.Kind.CONTAINER, seq)
                        container.container_no = row["container"]
                        container.save(update_fields=["container_no"])
                    containers[row["container"]] = container
                summary["containers"].add(container.code)
            pallet = None
            if row["pallet"]:
                pallet = ensure_unit(user, consignment, HandlingUnit.Kind.PALLET, row["pallet"], parent=container)
                summary["pallets"].add(pallet.code)
            carton = ensure_unit(
                user, consignment, HandlingUnit.Kind.CARTON, row["carton"], parent=pallet or container
            )
            changed = [column for column, number in row["specs"].items() if getattr(carton, column) != number]
            if changed:
                for column in changed:
                    setattr(carton, column, row["specs"][column])
                carton.save(update_fields=changed)
            summary["cartons"].add(carton.code)
            created = add_items(
                user,
                consignment,
                row["item_type"],
                row["quantity"],
                units=[carton],
                oem_serials=[row["serial"]] if row["serial"] else (),
                batch_no=row["batch"],
                request=request,
            )
            summary["items"] += len(created)
    return {
        "errors": [],
        "rows": summary["rows"],
        "items": summary["items"],
        "cartons": len(summary["cartons"]),
        "pallets": len(summary["pallets"]),
        "containers": len(summary["containers"]),
    }
