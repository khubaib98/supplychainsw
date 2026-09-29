import json
import shutil
import tempfile
from pathlib import Path

import polib
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import AuditLog
from logistics import services
from logistics.models import Consignment, DocumentType, Item
from logistics.tests.factories import advance, consignment, pdf, world

S = Consignment.Status
TEMP_MEDIA = tempfile.mkdtemp(prefix="portal-test-media-")


def tearDownModule():
    shutil.rmtree(TEMP_MEDIA, ignore_errors=True)


class Base(TestCase):
    def setUp(self):
        self.w = world()
        self.c = consignment(self.w)
        self.cartons = services.create_units(self.w.s_ops, self.c, "carton", 3)
        services.add_items(self.w.s_ops, self.c, self.w.dcjm, 40, units=self.cartons)
        self.item = Item.objects.get(uid=f"{self.c.code}-DCJM-017")

    def login(self, user):
        self.client.force_login(user)


class VisibilityTests(Base):
    def test_every_party_sees_its_pages(self):
        pages = [
            reverse("logistics:dashboard"),
            reverse("logistics:consignments"),
            reverse("logistics:consignment", args=[self.c.code]),
            reverse("logistics:packing", args=[self.c.code]),
            reverse("logistics:documents", args=[self.c.code]),
            reverse("logistics:threads", args=[self.c.code]),
            reverse("logistics:receiving", args=[self.c.code]),
            reverse("logistics:unit", args=[self.cartons[0].code]),
            reverse("logistics:item", args=[self.item.uid]),
            reverse("logistics:packing_list", args=[self.c.code]),
            reverse("logistics:labels", args=[self.c.code]) + "?kind=carton",
            reverse("logistics:scan"),
            reverse("logistics:records"),
            reverse("logistics:catalog"),
            reverse("logistics:coding_help"),
        ]
        for user in (self.w.s_mgr, self.w.s_ops, self.w.r_mgr, self.w.r_ops, self.w.o_view):
            self.login(user)
            for page in pages:
                with self.subTest(user=user.username, page=page):
                    self.assertEqual(self.client.get(page).status_code, 200)

    def test_other_shippers_cannot_see_or_guess_urls(self):
        self.login(self.w.x_mgr)
        self.assertNotContains(self.client.get(reverse("logistics:consignments")), self.c.code)
        for page in (reverse("logistics:consignment", args=[self.c.code]),
                     reverse("logistics:item", args=[self.item.uid])):
            self.assertEqual(self.client.get(page).status_code, 404)
        response = self.client.post(reverse("logistics:scan_api"), json.dumps({"code": self.item.uid}),
                                    content_type="application/json")
        self.assertFalse(response.json()["ok"])

    def test_oversight_is_read_only(self):
        self.login(self.w.o_view)
        response = self.client.post(reverse("logistics:consignment_status", args=[self.c.code]),
                                    {"status": S.READY})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse("logistics:add_units", args=[self.c.code]), {"kind": "carton", "count": 1})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(reverse("logistics:consignment_new")).status_code, 403)

    def test_audit_log_is_for_managers_and_oversight(self):
        self.login(self.w.s_ops)
        self.assertEqual(self.client.get(reverse("logistics:records_table", args=["audit"])).status_code, 404)
        self.login(self.w.o_view)
        self.assertEqual(self.client.get(reverse("logistics:records_table", args=["audit"])).status_code, 200)


class WorkflowViewTests(Base):
    def test_status_form_and_timeline(self):
        self.login(self.w.s_mgr)
        response = self.client.post(reverse("logistics:consignment_status", args=[self.c.code]),
                                    {"status": S.READY, "note": "all packed"}, follow=True)
        self.assertContains(response, "节点已记录。")  # s_mgr reads Chinese
        self.c.refresh_from_db()
        self.assertEqual(self.c.status, S.READY)

    def test_move_via_form_keeps_id(self):
        self.login(self.w.s_ops)
        response = self.client.post(reverse("logistics:move_item", args=[self.item.uid]),
                                    {"to_unit": self.cartons[2].pk, "reason": "inspection"})
        self.assertRedirects(response, reverse("logistics:item", args=[self.item.uid]))
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit, self.cartons[2])

    def test_create_consignment_via_form(self):
        self.login(self.w.s_mgr)
        response = self.client.post(reverse("logistics:consignment_new"), {
            "receiver": self.w.receiver.pk, "transport_mode": "air", "origin": "Guangzhou", "destination": "Lahore",
            "etd": "2026-10-01", "eta": "2026-09-01",
        })
        self.assertContains(response, "预计到达时间不能早于预计离港时间。")
        response = self.client.post(reverse("logistics:consignment_new"), {
            "receiver": self.w.receiver.pk, "transport_mode": "air", "origin": "Guangzhou", "destination": "Lahore",
        })
        created = Consignment.objects.exclude(pk=self.c.pk).get()
        self.assertRedirects(response, reverse("logistics:consignment", args=[created.code]))

    def test_invalid_container_number_is_rejected(self):
        self.login(self.w.s_ops)
        self.client.post(reverse("logistics:add_units", args=[self.c.code]),
                         {"kind": "container", "count": 1, "container_no": "MSKU1234566"})
        self.assertFalse(self.c.units.filter(kind="container").exists())
        self.client.post(reverse("logistics:add_units", args=[self.c.code]),
                         {"kind": "container", "count": 1, "container_no": "msku 123456-5"})
        self.assertEqual(self.c.units.get(kind="container").container_no, "MSKU1234565")


class ScanApiTests(Base):
    def scan(self, **payload):
        return self.client.post(reverse("logistics:scan_api"), json.dumps(payload), content_type="application/json").json()

    def test_lookup_shows_where_an_item_is(self):
        self.login(self.w.r_ops)
        data = self.scan(code="ｃｓｇ" + self.item.uid[3:], mode="lookup")  # full-width + lower case
        self.assertTrue(data["ok"])
        self.assertEqual(data["code"], self.item.uid)
        self.assertIn("CTN-002", data["where"])
        self.assertTrue(self.scan(code="DCJM-017", consignment=self.c.code)["ok"])  # short ID in context
        self.assertFalse(self.scan(code="DCJM-017")["ok"])  # ambiguous without context

    def test_scan_to_move_and_receive(self):
        self.login(self.w.s_ops)
        target = self.scan(code=self.cartons[2].code, mode="target")
        self.assertTrue(target["ok"])
        moved = self.scan(code=self.item.uid, mode="move", target=self.cartons[2].code)
        self.assertTrue(moved["ok"], moved)
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit, self.cartons[2])
        refused = self.scan(code=self.item.uid, mode="receive")
        self.assertFalse(refused["ok"])  # shipper cannot receive
        advance(self.w, self.c, S.READY, S.DISPATCHED, S.ARRIVED)
        self.login(self.w.r_ops)
        received = self.scan(code=self.cartons[0].code, mode="receive", include_contents=True)
        self.assertTrue(received["ok"], received)
        self.assertEqual(Item.objects.filter(unit=self.cartons[0], received_at__isnull=False).count(), 14)


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class DocumentViewTests(Base):
    def test_upload_download_and_audit(self):
        self.login(self.w.s_ops)
        response = self.client.post(reverse("logistics:upload", args=[self.c.code]), {
            "doc_type": DocumentType.objects.get(code="CI").pk, "file": pdf(), "note": "v1",
        })
        self.assertRedirects(response, reverse("logistics:documents", args=[self.c.code]))
        document = self.c.documents.get()
        self.login(self.w.r_ops)
        download = self.client.get(reverse("logistics:download", args=[document.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")
        self.assertTrue(AuditLog.objects.filter(action="document.downloaded", username="r_ops").exists())
        self.login(self.w.x_mgr)
        self.assertEqual(self.client.get(reverse("logistics:download", args=[document.pk])).status_code, 404)

    def test_receiver_cannot_upload_shipper_documents(self):
        self.login(self.w.r_ops)
        self.client.post(reverse("logistics:upload", args=[self.c.code]), {
            "doc_type": DocumentType.objects.get(code="CI").pk, "file": pdf(),
        })
        self.assertFalse(self.c.documents.exists())


class ExportAndLanguageTests(Base):
    def test_csv_exports_are_excel_friendly(self):
        self.login(self.w.o_view)
        response = self.client.get(reverse("logistics:records_table", args=["items"]) + "?format=csv")
        body = response.content.decode("utf-8")
        self.assertTrue(body.startswith("﻿"))
        self.assertIn(self.item.uid, body)
        packing = self.client.get(reverse("logistics:packing_list", args=[self.c.code]) + "?format=csv")
        self.assertIn("双通道干扰模块", packing.content.decode("utf-8"))

    def test_pages_render_in_chinese_and_urdu(self):
        self.login(self.w.s_mgr)  # profile language zh-hans
        page = self.client.get(reverse("logistics:item", args=[self.item.uid]))
        self.assertContains(page, "逐层位置")
        self.assertContains(page, 'lang="zh-hans"')
        self.login(self.w.r_mgr)  # profile language ur
        page = self.client.get(reverse("logistics:item", args=[self.item.uid]))
        self.assertContains(page, 'dir="rtl"')
        self.assertContains(page, "یہ کہاں ہے، تہہ بہ تہہ")

    def test_changes_endpoint_reports_other_peoples_updates(self):
        self.login(self.w.r_ops)
        since = AuditLog.objects.order_by("-id").first().id
        empty = self.client.get(reverse("logistics:changes") + f"?since={since}&consignment={self.c.code}").json()
        self.assertEqual(empty["count"], 0)
        services.set_status(self.w.s_mgr, self.c, S.READY)
        changed = self.client.get(reverse("logistics:changes") + f"?since={since}&consignment={self.c.code}").json()
        self.assertEqual(changed["count"], 1)


class TranslationCatalogueTests(TestCase):
    """Every interface string must be translated before release."""

    def test_no_untranslated_strings(self):
        for folder in ("zh_Hans", "ur"):
            path = Path(settings.BASE_DIR) / "locale" / folder / "LC_MESSAGES" / "django.po"
            catalogue = polib.pofile(str(path))
            untranslated = [entry.msgid for entry in catalogue.untranslated_entries()]
            fuzzy = [entry.msgid for entry in catalogue.fuzzy_entries()]
            with self.subTest(language=folder):
                self.assertEqual(untranslated, [])
                self.assertEqual(fuzzy, [])
