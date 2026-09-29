"""Build the packing hierarchy for display: nested tree and layer-by-layer map."""
from collections import defaultdict
from dataclasses import dataclass, field

from django.utils.translation import gettext_lazy as _

from logistics.models import HandlingUnit, Item


@dataclass
class Node:
    unit: HandlingUnit
    children: list = field(default_factory=list)
    items: list = field(default_factory=list)
    depth: int = 0

    @property
    def item_total(self):
        own = sum(item.quantity for item in self.items)
        return own + sum(child.item_total for child in self.children)

    @property
    def unit_total(self):
        return len(self.children) + sum(child.unit_total for child in self.children)

    def item_groups(self):
        """Items of this node grouped by type: [(item_type, [items], total quantity)]."""
        groups = defaultdict(list)
        for item in self.items:
            groups[item.item_type].append(item)
        return [
            (item_type, items, sum(item.quantity for item in items))
            for item_type, items in sorted(groups.items(), key=lambda pair: pair[0].code)
        ]


@dataclass
class Tree:
    roots: list
    loose_items: list
    kind_totals: dict
    item_total: int
    by_code: dict

    def find(self, code):
        return self.by_code.get(code)


def build_tree(consignment):
    units = list(consignment.units.filter(is_archived=False).order_by("kind", "seq"))
    items = list(
        consignment.items.filter(is_archived=False).select_related("item_type").order_by("item_type__code", "seq")
    )
    nodes = {unit.pk: Node(unit=unit) for unit in units}
    roots = []
    for unit in units:
        node = nodes[unit.pk]
        parent = nodes.get(unit.parent_id)
        (parent.children if parent else roots).append(node)
    loose = []
    for item in items:
        holder = nodes.get(item.unit_id)
        (holder.items if holder else loose).append(item)

    order = {kind: index for index, kind in enumerate(HandlingUnit.Kind.values)}

    def sort_and_depth(node_list, depth):
        node_list.sort(key=lambda n: (order[n.unit.kind], n.unit.seq))
        for node in node_list:
            node.depth = depth
            sort_and_depth(node.children, depth + 1)

    sort_and_depth(roots, 0)
    kind_totals = defaultdict(int)
    for unit in units:
        kind_totals[unit.kind] += 1
    return Tree(
        roots=roots,
        loose_items=loose,
        kind_totals=dict(kind_totals),
        item_total=sum(item.quantity for item in items),
        by_code={node.unit.code: node for node in nodes.values()},
    )


def kind_label(kind):
    return HandlingUnit.Kind(kind).label


def layer_map(obj):
    """
    One row per layer from the outermost unit down to `obj`, each row listing
    the siblings at that layer with the current one marked, e.g.

        Container  [CNT-001]
        Pallet     [PLT-001] [PLT-002] [PLT-003*]
        Carton     [CTN-015] [CTN-016] [CTN-017*]
        Item       [DCJM-015] [DCJM-016] [DCJM-017*]
    """
    if isinstance(obj, Item):
        chain = (obj.unit.ancestors() + [obj.unit]) if obj.unit else []
    else:
        chain = obj.ancestors() + [obj]
    consignment = obj.consignment
    layers = []
    parent = None
    for unit in chain:
        if parent is None:
            siblings = list(consignment.units.filter(parent__isnull=True, is_archived=False).order_by("kind", "seq"))
        else:
            siblings = list(parent.children.filter(is_archived=False).order_by("kind", "seq"))
        layers.append({"label": kind_label(unit.kind), "kind": unit.kind, "current": unit, "siblings": siblings})
        parent = unit
    if isinstance(obj, Item):
        holder = obj.unit
        queryset = holder.items if holder else consignment.items.filter(unit__isnull=True)
        siblings = list(queryset.filter(is_archived=False).select_related("item_type").order_by("item_type__code", "seq"))
        layers.append({"label": _("Item"), "kind": "item", "current": obj, "siblings": siblings})
    for depth, layer in enumerate(layers, start=1):
        layer["depth"] = depth
    return layers
