"""Packing list and ID-range helpers."""
from collections import defaultdict

from logistics.models import HandlingUnit


def compress_ranges(numbers):
    """[1,2,3,5,7,8] -> '001–003, 005, 007–008'."""
    numbers = sorted(set(numbers))
    if not numbers:
        return ""
    parts = []
    start = previous = numbers[0]
    for number in numbers[1:] + [None]:
        if number is not None and number == previous + 1:
            previous = number
            continue
        parts.append(f"{start:03d}" if start == previous else f"{start:03d}–{previous:03d}")
        if number is not None:
            start = previous = number
    return ", ".join(parts)


def packing_rows(consignment):
    """
    One row per (holding unit, item type) — the classic packing list — with
    the full path to the outermost unit and the item ID ranges it holds.
    """
    units = {unit.pk: unit for unit in consignment.units.filter(is_archived=False)}
    grouped = defaultdict(list)
    for item in consignment.items.filter(is_archived=False).select_related("item_type"):
        grouped[(item.unit_id, item.item_type_id)].append(item)

    def path(unit):
        chain = []
        seen = set()
        while unit is not None and unit.pk not in seen:
            seen.add(unit.pk)
            chain.append(unit)
            unit = units.get(unit.parent_id)
        return list(reversed(chain))

    kind_order = {kind: index for index, kind in enumerate(HandlingUnit.Kind.values)}
    rows = []
    for (unit_id, _type_id), items in grouped.items():
        unit = units.get(unit_id)
        chain = path(unit) if unit else []
        item_type = items[0].item_type
        rows.append(
            {
                "unit": unit,
                "path": chain,
                "container": next((u for u in chain if u.kind == HandlingUnit.Kind.CONTAINER), None),
                "pallet": next((u for u in chain if u.kind == HandlingUnit.Kind.PALLET), None),
                "item_type": item_type,
                "quantity": sum(item.quantity for item in items),
                "ids": f"{item_type.code}-" + compress_ranges([item.seq for item in items]),
                "serials": [item.oem_serial for item in items if item.oem_serial],
                "items": items,
            }
        )
    rows.sort(
        key=lambda row: (
            row["unit"] is None,
            kind_order.get(row["unit"].kind, 99) if row["unit"] else 99,
            row["unit"].seq if row["unit"] else 0,
            row["item_type"].code,
        )
    )
    return rows
