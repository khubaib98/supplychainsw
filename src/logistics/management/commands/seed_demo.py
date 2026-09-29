"""
Load a realistic demo so the portal can be shown to the Shipper, Receiver and
Oversight before real data exists. History is back-dated relative to today.

The flagship consignment reproduces the example from the requirements:
57 cartons, 40 Dual Channel Jamming Modules (DCJM) packed in CTN-001..003,
one module moved between cartons during export inspection, a missing payment
document that escalates to Oversight, and a damaged carton on arrival.

    python manage.py seed_demo            # on an empty database
    python manage.py seed_demo --flush    # wipe everything first (demo only!)
"""
import contextlib
from datetime import timedelta
from importlib import import_module
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from accounts.models import Organization, Profile
from core.models import TextTranslation
from core.translation import source_hash
from logistics import services
from logistics.models import Consignment, DocumentType, HandlingUnit, Item, ItemType, Thread
from logistics.reminders import run_reminders

S = Consignment.Status


@contextlib.contextmanager
def clock(moment):
    """Pretend the current time is `moment` while seeding (back-dated history)."""
    with mock.patch("django.utils.timezone.now", return_value=moment):
        yield


def demo_pdf(title, lines):
    """A tiny valid one-page PDF (ASCII text) standing in for a real scanned document."""
    def escape(text):
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    rows = [f"BT /F1 20 Tf 56 770 Td ({escape(title)}) Tj ET",
            "BT /F1 11 Tf 56 745 Td (DEMO DOCUMENT - generated sample, not a real trade document) Tj ET"]
    y = 710
    for line in lines:
        rows.append(f"BT /F1 12 Tf 56 {y} Td ({escape(line)}) Tj ET")
        y -= 18
    stream = "\n".join(rows).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


ITEM_TYPES = [
    # code, English, Chinese, Urdu, tracking, uom, controlled
    ("DCJM", "Dual Channel Jamming Module", "双通道干扰模块", "ڈوئل چینل جیمنگ ماڈیول", "serial", "pcs", True),
    ("PSU", "Power Supply Unit 48 V", "48V 电源模块", "48 وولٹ پاور سپلائی یونٹ", "serial", "pcs", False),
    ("CTRL", "Control Unit", "控制单元", "کنٹرول یونٹ", "serial", "pcs", False),
    ("ANT", "Omni Antenna Set", "全向天线套件", "اومنی اینٹینا سیٹ", "serial", "set", False),
    ("CBL", "RF Cable 5 m", "射频线缆 5 米", "آر ایف کیبل 5 میٹر", "batch", "pcs", False),
    ("KIT", "Installation Kit", "安装配件包", "تنصیبی کٹ", "batch", "pcs", False),
]

# Human-quality translations for the seeded conversations: (text, lang) -> {target: translation}
SEED_TRANSLATIONS = {
    ("请提供付款水单（TT 副本）", "zh-hans"): {
        "en": "Please provide the payment slip (TT copy)",
        "ur": "براہ کرم ادائیگی کی رسید (ٹی ٹی کاپی) فراہم کریں",
    },
    ("我方财务需要核对 PO-2026-0142 的付款。请上传或发送 TT 水单副本，以便我们上传付款单据。", "zh-hans"): {
        "en": "Our finance team needs to verify the payment for PO-2026-0142. Please upload or send a copy of the "
              "TT slip so that we can upload the payment documents.",
        "ur": "ہماری فنانس ٹیم کو PO-2026-0142 کی ادائیگی کی تصدیق کرنی ہے۔ براہ کرم ٹی ٹی رسید کی کاپی اپ لوڈ کریں یا "
              "بھیجیں تاکہ ہم ادائیگی کی دستاویزات اپ لوڈ کر سکیں۔",
    },
    ("ٹی ٹی کی کاپی بینک کی تصدیق کے بعد بھیج دی جائے گی۔ متوقع تاریخ اگلا ہفتہ ہے۔", "ur"): {
        "en": "The TT copy will be sent after the bank's confirmation. The expected date is next week.",
        "zh-hans": "银行确认后将发送 TT 副本。预计日期为下周。",
    },
    ("CTN-017 outer carton crushed", "en"): {
        "zh-hans": "CTN-017 外箱被压坏",
        "ur": "CTN-017 کا بیرونی کارٹن پچکا ہوا ہے",
    },
    ("CTN-017 (Omni Antenna Set, 4 sets) arrived with one corner of the outer carton crushed. The contents look "
     "intact but we will test them. Photos to follow.", "en"): {
        "zh-hans": "CTN-017（全向天线套件，4 套）到货时外箱一角被压坏。内件看起来完好，但我们会进行测试。照片随后提供。",
        "ur": "CTN-017 (اومنی اینٹینا سیٹ، 4 سیٹ) اس حالت میں پہنچا کہ بیرونی کارٹن کا ایک کونا پچکا ہوا تھا۔ اندر کا سامان "
              "بظاہر ٹھیک ہے لیکن ہم اس کی جانچ کریں گے۔ تصاویر بعد میں بھیجی جائیں گی۔",
    },
    ("收到。CTN-017 装柜时完好（有装柜照片）。请上传照片，我们将向保险公司提出索赔。", "zh-hans"): {
        "en": "Received. CTN-017 was intact when it was loaded into the container (we have loading photos). Please "
              "upload the photos and we will file a claim with the insurer.",
        "ur": "موصول ہوا۔ CTN-017 کنٹینر میں لوڈ کرتے وقت ٹھیک حالت میں تھا (ہمارے پاس لوڈنگ کی تصاویر ہیں)۔ براہ کرم "
              "تصاویر اپ لوڈ کریں، ہم انشورنس کمپنی کے پاس کلیم دائر کریں گے۔",
    },
    ("DCJM-017 found in CTN-003, not CTN-002", "en"): {
        "zh-hans": "DCJM-017 在 CTN-003 中发现，而不是 CTN-002",
        "ur": "DCJM-017 کارٹن CTN-003 میں ملا، CTN-002 میں نہیں",
    },
    ("The first packing list (v1) shows DCJM-017 in CTN-002, but we found it in CTN-003.", "en"): {
        "zh-hans": "第一版装箱单（v1）显示 DCJM-017 在 CTN-002 中，但我们在 CTN-003 中找到了它。",
        "ur": "پہلی پیکنگ لسٹ (v1) کے مطابق DCJM-017 کارٹن CTN-002 میں تھا، لیکن ہمیں یہ CTN-003 میں ملا۔",
    },
    ("正确。出口查验当天该模块被移至 CTN-003，移动记录可在门户中 DCJM-017 的历史中查看。", "zh-hans"): {
        "en": "Correct. On the day of the export inspection the module was moved to CTN-003; the move is recorded "
              "in the portal history of DCJM-017.",
        "ur": "درست ہے۔ ایکسپورٹ معائنے کے دن یہ ماڈیول CTN-003 میں منتقل کیا گیا تھا؛ یہ منتقلی پورٹل میں DCJM-017 کی "
              "ہسٹری میں درج ہے۔",
    },
}


class Command(BaseCommand):
    help = "Load demo organisations, users and consignments (for demonstrations only)."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="Demo-Portal-2026", help="Password for all demo users.")
        parser.add_argument("--flush", action="store_true", help="Delete ALL data first. Never use in production.")

    def handle(self, *args, **options):
        if options["flush"]:
            call_command("flush", interactive=False, verbosity=0)
            # flush also empties the document-type catalogue created by a data migration.
            self._restore_document_types()
        if Organization.objects.exists() or Consignment.objects.exists():
            raise CommandError("The database already has data. Use --flush on a demo database, never in production.")
        self.password = options["password"]
        self.base = timezone.now().replace(minute=0, second=0, microsecond=0)
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), transaction.atomic():
            self._people()
            self._catalog()
            self._closed_air()
            self._flagship()
            self._road_packing()
            self._delayed_sea()
            self._draft_air()
            self._translations()
        self._report()

    # -- helpers ---------------------------------------------------------------
    def t(self, days, hours=0):
        return self.base - timedelta(days=days) + timedelta(hours=hours)

    def _restore_document_types(self):
        module = import_module("logistics.migrations.0002_default_document_types")
        module.create_types(apps, None)

    def doc(self, user, consignment, code, lines, note=""):
        name = f"{consignment.code}-{code}.pdf"
        title = DocumentType.objects.get(code=code).name_en
        upload = SimpleUploadedFile(name, demo_pdf(f"{title} - {consignment.code}", lines), content_type="application/pdf")
        return services.upload_document(user, consignment, DocumentType.objects.get(code=code), upload, note=note)

    def user(self, username, first, last, org, level, language, tz):
        user = get_user_model().objects.create_user(
            username, email=f"{username}@example.com", password=self.password, first_name=first, last_name=last
        )
        Profile.objects.create(user=user, organization=org, level=level, language=language, timezone=tz)
        return user

    # -- data ------------------------------------------------------------------
    def _people(self):
        self.shipper = Organization.objects.create(
            name="Demo Shipper Co., Ltd. (Shenzhen)", name_local="深圳示例发货有限公司", kind=Organization.Kind.SHIPPER,
            country="CN", default_language="zh-hans",
        )
        self.receiver = Organization.objects.create(
            name="Demo Receiver (Pvt) Ltd., Rawalpindi", name_local="ڈیمو ریسیور (پرائیویٹ) لمیٹڈ",
            kind=Organization.Kind.RECEIVER, country="PK", default_language="en",
        )
        self.oversight = Organization.objects.create(
            name="Oversight – Top Management", kind=Organization.Kind.OVERSIGHT, country="PK", default_language="en",
        )
        manager, operator, viewer = Profile.Level.MANAGER, Profile.Level.OPERATOR, Profile.Level.VIEWER
        self.s_mgr = self.user("shipper.manager", "Zhang", "Wei", self.shipper, manager, "zh-hans", "Asia/Shanghai")
        self.s_ops = self.user("shipper.ops", "Liu", "Yang", self.shipper, operator, "zh-hans", "Asia/Shanghai")
        self.r_mgr = self.user("receiver.manager", "Ahmed", "Raza", self.receiver, manager, "en", "Asia/Karachi")
        self.r_ops = self.user("receiver.ops", "Bilal", "Khan", self.receiver, operator, "ur", "Asia/Karachi")
        self.o_view = self.user("oversight", "Top", "Management", self.oversight, viewer, "en", "Asia/Karachi")
        admin = get_user_model().objects.create_superuser("admin", "admin@example.com", self.password)
        admin.first_name = "System"
        admin.save()

    def _catalog(self):
        self.types = {}
        for code, en, zh, ur, tracking, uom, controlled in ITEM_TYPES:
            self.types[code] = ItemType.objects.create(
                code=code, name_en=en, name_zh=zh, name_ur=ur, tracking=tracking, uom=uom, controlled=controlled
            )

    def _closed_air(self):
        with clock(self.t(95)):
            c = services.create_consignment(
                self.s_mgr, shipper=self.shipper, receiver=self.receiver, transport_mode="air",
                origin="Guangzhou (CAN)", destination="Islamabad (ISB)", reference="PO-2026-0107",
                description="Pilot air shipment", incoterm="CIP", carrier="Demo Air Cargo",
                transport_ref="999-12345675", etd=self.t(91).date(), eta=self.t(90).date(), planned_packages=6,
            )
        with clock(self.t(94)):
            cartons = services.create_units(self.s_ops, c, "carton", 6, gross_kg=14.5, length_cm=60, width_cm=40, height_cm=35)
            services.add_items(self.s_ops, c, self.types["DCJM"], 10, units=cartons[0:2],
                               oem_serials=[f"DJ26A{n:05d}" for n in range(1, 11)])
            services.add_items(self.s_ops, c, self.types["PSU"], 10, units=cartons[2:4])
            services.add_items(self.s_ops, c, self.types["CBL"], 50, units=cartons[4:6], batch_no="B2605")
        with clock(self.t(94, 3)):
            self.doc(self.r_mgr, c, "EUC", ["End user: Demo end user (authorised)", "NOC ref: DEMO-NOC-0107"])
        with clock(self.t(93)):
            for code in ("CI", "PL", "EXL"):
                self.doc(self.s_mgr, c, code, ["PO-2026-0107", "Items: DCJM x10, PSU x10, CBL x50"])
            services.set_status(self.s_mgr, c, S.READY)
        with clock(self.t(92)):
            services.set_status(self.s_mgr, c, S.DISPATCHED)
            self.doc(self.s_mgr, c, "PAY", ["TT copy, 100% advance"])
        with clock(self.t(92, 6)):
            services.set_status(self.s_mgr, c, S.EXPORT_CLEARED)
        with clock(self.t(91)):
            services.set_status(self.s_mgr, c, S.IN_TRANSIT, note="Flight departed CAN")
            self.doc(self.s_mgr, c, "AWB", ["AWB 999-12345675"])
        with clock(self.t(90)):
            services.set_status(self.r_mgr, c, S.ARRIVED, note="Arrived ISB cargo terminal")
        with clock(self.t(88)):
            services.set_status(self.r_mgr, c, S.IMPORT_CLEARED)
        with clock(self.t(87)):
            services.set_status(self.r_mgr, c, S.DELIVERED)
        with clock(self.t(86)):
            for carton in cartons:
                services.receive(self.r_ops, carton, include_contents=True, place="Rawalpindi warehouse")
            services.set_status(self.r_mgr, c, S.RECEIVED)
        with clock(self.t(85)):
            services.set_status(self.r_mgr, c, S.CLOSED, note="All items verified")

    def _flagship(self):
        with clock(self.t(40)):
            c = services.create_consignment(
                self.s_mgr, shipper=self.shipper, receiver=self.receiver, transport_mode="sea",
                origin="Shenzhen (Yantian port)", destination="Karachi (Port Qasim) → Rawalpindi",
                reference="PO-2026-0142", description="Phase 2 equipment – 57 packages", incoterm="CIF",
                carrier="Demo Shipping Line", etd=self.t(27).date(), eta=self.t(9).date(), planned_packages=57,
            )
        self.flagship = c
        with clock(self.t(39)):
            container = services.create_units(self.s_ops, c, "container", 1, container_no="MSKU1234565",
                                              seal_no="CN2026A0457", marks="40' HC")[0]
            pallets = services.create_units(self.s_ops, c, "pallet", 5, parent=container, length_cm=120, width_cm=100)
            cartons = []
            for pallet, count in zip(pallets, (12, 12, 12, 12, 9)):
                cartons += services.create_units(self.s_ops, c, "carton", count, parent=pallet, gross_kg=18.5,
                                                 length_cm=60, width_cm=40, height_cm=40)
            services.add_items(self.s_ops, c, self.types["DCJM"], 40, units=cartons[0:3],
                               oem_serials=[f"DJ26A{n:05d}" for n in range(101, 141)])
            services.add_items(self.s_ops, c, self.types["PSU"], 40, units=cartons[3:7],
                               oem_serials=[f"PSU48-{n:04d}" for n in range(2001, 2041)])
            services.add_items(self.s_ops, c, self.types["CTRL"], 20, units=cartons[7:11])
            services.add_items(self.s_ops, c, self.types["ANT"], 40, units=cartons[11:21])
            services.add_items(self.s_ops, c, self.types["CBL"], 400, units=cartons[21:41], batch_no="B2608")
            services.add_items(self.s_ops, c, self.types["KIT"], 160, units=cartons[41:57], batch_no="K2608")
        with clock(self.t(38)):
            services.add_requirement(self.r_mgr, c, DocumentType.objects.get(code="COO"))
            self.doc(self.r_mgr, c, "EUC", ["End user: Demo end user (authorised)", "NOC ref: DEMO-NOC-0142"])
        with clock(self.t(33)):
            self.doc(self.s_mgr, c, "CI", ["PO-2026-0142", "57 packages, CIF Karachi"])
            self.doc(self.s_ops, c, "PL", ["57 cartons on 5 pallets in MSKU1234565"])
            self.doc(self.s_mgr, c, "EXL", ["Export licence (demo) for DCJM x40"])
            services.set_status(self.s_mgr, c, S.READY)
        with clock(self.t(32)):
            services.set_status(self.s_mgr, c, S.DISPATCHED, note="Container gated out of Shenzhen warehouse")
        dcjm_017 = Item.objects.get(uid=f"{c.code}-DCJM-017")
        with clock(self.t(31)):
            services.move(self.s_ops, dcjm_017, cartons[2],
                          reason="Repacked after export customs inspection; CTN-002 inner tray cracked",
                          place="Yantian customs inspection area")
            self.doc(self.s_mgr, c, "CI", ["PO-2026-0142", "v2: corrected unit price, line 2 (PSU)"],
                     note="Corrected unit price for PSU (line 2)")
        with clock(self.t(30)):
            services.set_status(self.s_mgr, c, S.EXPORT_CLEARED)
        with clock(self.t(27)):
            services.set_status(self.s_mgr, c, S.IN_TRANSIT, note="Vessel departed Yantian")
            services.update_consignment(self.s_mgr, c, {"transport_ref": "DSLU2026SZ0457"})
        with clock(self.t(26)):
            self.doc(self.s_mgr, c, "BL", ["B/L DSLU2026SZ0457", "Port of discharge: Port Qasim"])
            self.doc(self.s_mgr, c, "COO", ["CPFTA Form P (demo)"])
        for days in (25, 23, 20):  # the missing payment documents climb the reminder ladder
            with clock(self.t(days, 1)):
                run_reminders()
        with clock(self.t(24)):
            payment = services.create_thread(
                self.s_mgr, c, category=Thread.Category.PAYMENT, subject="请提供付款水单（TT 副本）",
                body="我方财务需要核对 PO-2026-0142 的付款。请上传或发送 TT 水单副本，以便我们上传付款单据。",
            )
        with clock(self.t(22)):
            services.reply(self.r_ops, payment, "ٹی ٹی کی کاپی بینک کی تصدیق کے بعد بھیج دی جائے گی۔ متوقع تاریخ اگلا ہفتہ ہے۔")
        with clock(self.t(9)):
            services.set_status(self.r_mgr, c, S.ARRIVED, note="Discharged at Port Qasim")
        with clock(self.t(9, 3)):
            services.add_requirement(self.r_mgr, c, DocumentType.objects.get(code="GD"))
        with clock(self.t(6)):
            self.doc(self.r_mgr, c, "GD", ["Goods Declaration (demo) filed via WeBOC/PSW"])
        with clock(self.t(5)):
            services.set_status(self.r_mgr, c, S.IMPORT_CLEARED, note="GD assessed; released by customs")
        with clock(self.t(2)):
            services.set_status(self.r_mgr, c, S.DELIVERED, note="Container delivered to Rawalpindi warehouse")
        with clock(self.t(1)):
            services.receive(self.r_ops, container, place="Rawalpindi warehouse")
            services.receive(self.r_ops, cartons[16], condition=HandlingUnit.Condition.DAMAGED, include_contents=True,
                             place="Rawalpindi warehouse")
            for pallet in pallets[:3]:
                services.receive(self.r_ops, pallet, include_contents=True, place="Rawalpindi warehouse")
        with clock(self.t(1, 1)):
            damage = services.create_thread(
                self.r_mgr, c, category=Thread.Category.DAMAGE, subject="CTN-017 outer carton crushed",
                body="CTN-017 (Omni Antenna Set, 4 sets) arrived with one corner of the outer carton crushed. The "
                     "contents look intact but we will test them. Photos to follow.",
                unit=cartons[16],
            )
        with clock(self.t(1, 2)):
            discrepancy = services.create_thread(
                self.r_ops, c, category=Thread.Category.DISCREPANCY, subject="DCJM-017 found in CTN-003, not CTN-002",
                body="The first packing list (v1) shows DCJM-017 in CTN-002, but we found it in CTN-003.",
                item=dcjm_017,
            )
        with clock(self.t(1, 4)):
            services.reply(self.s_ops, discrepancy,
                           "正确。出口查验当天该模块被移至 CTN-003，移动记录可在门户中 DCJM-017 的历史中查看。")
        with clock(self.t(1, 5)):
            services.reply(self.s_mgr, damage, "收到。CTN-017 装柜时完好（有装柜照片）。请上传照片，我们将向保险公司提出索赔。")
        with clock(self.t(1, 6)):
            services.set_thread_state(self.r_ops, discrepancy, Thread.State.RESOLVED)

    def _road_packing(self):
        with clock(self.t(6)):
            c = services.create_consignment(
                self.s_mgr, shipper=self.shipper, receiver=self.receiver, transport_mode="road",
                origin="Kashgar (via Khunjerab Pass)", destination="Sost dry port → Islamabad",
                reference="PO-2026-0171", description="Spares by road", incoterm="DAP", carrier="Demo Road Freight",
                etd=(self.base + timedelta(days=5)).date(), eta=(self.base + timedelta(days=12)).date(),
                planned_packages=24,
            )
        with clock(self.t(5)):
            truck = services.create_units(self.s_ops, c, "container", 1, marks="Truck – plate to be assigned")[0]
            cartons = services.create_units(self.s_ops, c, "carton", 10, parent=truck, gross_kg=16)
            services.add_items(self.s_ops, c, self.types["PSU"], 20, units=cartons[0:4])
            services.add_items(self.s_ops, c, self.types["ANT"], 10, units=cartons[4:6])
        with clock(self.t(3)):
            self.doc(self.s_mgr, c, "CI", ["PO-2026-0171", "Draft invoice"])

    def _delayed_sea(self):
        with clock(self.t(50)):
            c = services.create_consignment(
                self.s_mgr, shipper=self.shipper, receiver=self.receiver, transport_mode="sea",
                origin="Ningbo", destination="Karachi (KICT)", reference="PO-2026-0133", description="Cable stock",
                incoterm="FOB", carrier="Demo Shipping Line", etd=self.t(32).date(), eta=self.t(4).date(),
                planned_packages=8,
            )
        with clock(self.t(49)):
            container = services.create_units(self.s_ops, c, "container", 1, container_no="TGHU8374215", seal_no="NB2026B1180")[0]
            cartons = services.create_units(self.s_ops, c, "carton", 8, parent=container, gross_kg=22)
            services.add_items(self.s_ops, c, self.types["CBL"], 160, units=cartons, batch_no="B2607")
        with clock(self.t(35)):
            self.doc(self.s_mgr, c, "CI", ["PO-2026-0133"])
            self.doc(self.s_mgr, c, "PL", ["8 cartons, CBL x160"])
            services.set_status(self.s_mgr, c, S.READY)
        with clock(self.t(33)):
            services.set_status(self.s_mgr, c, S.DISPATCHED)
            self.doc(self.s_mgr, c, "PAY", ["TT copy, 30% advance"])
        with clock(self.t(32)):
            services.set_status(self.s_mgr, c, S.EXPORT_CLEARED)
        with clock(self.t(31)):
            services.set_status(self.s_mgr, c, S.IN_TRANSIT, note="Vessel departed Ningbo; transhipment at Colombo")
        with clock(self.t(30)):
            self.doc(self.s_mgr, c, "BL", ["B/L DSLU2026NB0331"])

    def _draft_air(self):
        with clock(self.t(1)):
            services.create_consignment(
                self.s_mgr, shipper=self.shipper, receiver=self.receiver, transport_mode="air",
                origin="Beijing (PEK)", destination="Lahore (LHE)", reference="PO-2026-0180",
                description="Urgent replacement modules", incoterm="CPT",
                etd=(self.base + timedelta(days=14)).date(), planned_packages=12,
            )

    def _translations(self):
        for (text, lang), targets in SEED_TRANSLATIONS.items():
            for target, translated in targets.items():
                TextTranslation.objects.update_or_create(
                    source_hash=source_hash(text, lang), target_lang=target,
                    defaults={"source_lang": lang, "text": translated, "provider": "seed (human)"},
                )

    def _report(self):
        self.stdout.write(self.style.SUCCESS("Demo data loaded."))
        self.stdout.write(f"Flagship consignment: {self.flagship.code} (57 cartons, 40 × DCJM in CTN-001…003)")
        self.stdout.write(f"All demo users share the password: {self.password}")
        for username, role in [
            ("shipper.manager", "Shipper manager (中文)"),
            ("shipper.ops", "Shipper operator (中文)"),
            ("receiver.manager", "Receiver manager (English)"),
            ("receiver.ops", "Receiver operator (اردو)"),
            ("oversight", "Oversight, read-only (English)"),
            ("admin", "System administrator (/admin/)"),
        ]:
            self.stdout.write(f"  {username:<18} {role}")
