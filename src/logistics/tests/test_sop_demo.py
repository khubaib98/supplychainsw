"""The SOP demo dataset (docs/sop-demo) stays importable and gives the codes the SOP shows."""
from decimal import Decimal

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from logistics.importer import import_packing_list
from logistics.models import HandlingUnit, Item, ItemType
from logistics.tests.factories import consignment, world

DEMO = settings.BASE_DIR.parent / "docs" / "sop-demo"


class SopDemoDatasetTests(TestCase):
    def setUp(self):
        self.w = world()  # already has DCJM, PSU and CBL: the item master file updates them
        call_command("loaddata", DEMO / "item-master.json", verbosity=0)

    def test_item_master_can_be_loaded_again(self):
        call_command("loaddata", DEMO / "item-master.json", verbosity=0)
        self.assertEqual(ItemType.objects.filter(code__in=["DCJM", "PSU", "CTRL", "ANT", "CBL"]).count(), 5)
        dcjm = ItemType.objects.get(code="DCJM")
        self.assertEqual((dcjm.controlled, dcjm.name_zh, dcjm.hs_code), (True, "双通道干扰模块", "8543.70"))

    def test_packing_list_gives_the_codes_in_the_sop(self):
        c = consignment(self.w)
        result = import_packing_list(self.w.s_ops, c, (DEMO / "packing-list-12-cartons.csv").read_bytes())
        self.assertEqual(result["errors"], [])
        units = HandlingUnit.objects.filter(consignment=c)
        self.assertEqual(units.filter(kind=HandlingUnit.Kind.PALLET).count(), 2)
        cartons = units.filter(kind=HandlingUnit.Kind.CARTON)
        self.assertEqual(cartons.count(), 12)
        self.assertEqual(sum(carton.gross_kg for carton in cartons), Decimal("173.20"))
        self.assertEqual(Item.objects.filter(consignment=c).count(), 40)
        item = Item.objects.get(uid=f"{c.code}-DCJM-005")
        self.assertEqual((item.oem_serial, item.unit.code, item.unit.parent.code),
                         ("DJ26C00105", f"{c.code}-CTN-002", f"{c.code}-PLT-001"))
        cables = Item.objects.filter(consignment=c, item_type__code="CBL").order_by("uid")
        self.assertEqual([(i.uid[-7:], i.quantity, i.batch_no, i.unit.code[-7:]) for i in cables],
                         [("CBL-001", 30, "B2610", "CTN-011"), ("CBL-002", 30, "B2610", "CTN-012")])
