import shutil
import tempfile
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone, translation

from core.models import Notification, TextTranslation
from logistics import services
from logistics.importer import import_packing_list
from logistics.models import (
    Consignment,
    DocumentRequirement,
    DocumentType,
    HandlingUnit,
    Item,
    MilestoneEvent,
    Movement,
    ReminderLog,
    Thread,
)
from logistics.reminders import run_reminders
from logistics.tests.factories import advance, consignment, pdf, world

S = Consignment.Status
TEMP_MEDIA = tempfile.mkdtemp(prefix="portal-test-media-")


def tearDownModule():
    shutil.rmtree(TEMP_MEDIA, ignore_errors=True)


class ConsignmentTests(TestCase):
    def setUp(self):
        self.w = world()

    def test_code_is_generated_and_required_documents_follow_the_mode(self):
        sea = consignment(self.w, "sea")
        air = consignment(self.w, "air")
        road = consignment(self.w, "road")
        year = timezone.localdate().year
        self.assertEqual(sea.code, f"CSG-{year}-0001")
        self.assertEqual(road.code, f"CSG-{year}-0003")
        codes = lambda c: set(c.requirements.values_list("doc_type__code", flat=True))  # noqa: E731
        self.assertEqual(codes(sea), {"CI", "PL", "BL", "PAY"})
        self.assertEqual(codes(air), {"CI", "PL", "AWB", "PAY"})
        self.assertEqual(codes(road), {"CI", "PL", "RCN", "PAY"})
        self.assertEqual(sea.status, S.DRAFT)
        self.assertTrue(MilestoneEvent.objects.filter(consignment=sea, status=S.DRAFT).exists())

    def test_only_the_shipper_creates_consignments(self):
        for actor in (self.w.r_mgr, self.w.o_view, self.w.s_view):
            with self.assertRaises(PermissionDenied):
                services.create_consignment(actor, shipper=self.w.shipper, receiver=self.w.receiver,
                                            transport_mode="sea", origin="A", destination="B")
        with self.assertRaises(services.ServiceError):
            services.create_consignment(self.w.x_mgr, shipper=self.w.shipper, receiver=self.w.receiver,
                                        transport_mode="sea", origin="A", destination="B")

    def test_milestone_ownership(self):
        c = consignment(self.w)
        with self.assertRaises(PermissionDenied):
            services.set_status(self.w.r_mgr, c, S.READY)
        advance(self.w, c, S.READY, S.DISPATCHED)
        with self.assertRaises(PermissionDenied):
            services.set_status(self.w.s_mgr, c, S.ARRIVED)
        # The Receiver can take over from "dispatched" even if the Shipper forgot "in transit".
        services.set_status(self.w.r_mgr, c, S.ARRIVED)
        with self.assertRaises(PermissionDenied):
            services.set_status(self.w.s_mgr, c, S.IN_TRANSIT)
        with self.assertRaises(PermissionDenied):
            services.set_status(self.w.o_view, c, S.DELIVERED)
        c.refresh_from_db()
        self.assertEqual(c.status, S.ARRIVED)

    def test_status_change_notifies_the_other_party_and_oversight(self):
        c = consignment(self.w)
        services.set_status(self.w.s_mgr, c, S.READY)
        recipients = set(Notification.objects.filter(kind="status").values_list("user__username", flat=True))
        self.assertTrue({"r_mgr", "r_ops", "o_view"} <= recipients)
        self.assertNotIn("s_mgr", recipients)


class PackingTests(TestCase):
    def setUp(self):
        self.w = world()
        self.c = consignment(self.w)

    def test_the_57_carton_example(self):
        container = services.create_units(self.w.s_ops, self.c, "container", 1, container_no="MSKU1234565")[0]
        cartons = services.create_units(self.w.s_ops, self.c, "carton", 57, parent=container)
        self.assertEqual(cartons[0].code, f"{self.c.code}-CTN-001")
        self.assertEqual(cartons[-1].code, f"{self.c.code}-CTN-057")
        items = services.add_items(self.w.s_ops, self.c, self.w.dcjm, 40, units=cartons[:3])
        self.assertEqual([i.uid for i in items[:2]], [f"{self.c.code}-DCJM-001", f"{self.c.code}-DCJM-002"])
        self.assertEqual(items[-1].uid, f"{self.c.code}-DCJM-040")
        per_carton = [Item.objects.filter(unit=carton).count() for carton in cartons[:3]]
        self.assertEqual(per_carton, [14, 13, 13])
        self.c.refresh_from_db()
        self.assertEqual(self.c.status, S.PACKING)  # packing started automatically
        # Controlled item -> end-user certificate and export licence become required.
        required = set(self.c.requirements.values_list("doc_type__code", flat=True))
        self.assertTrue({"EUC", "EXL"} <= required)

    def test_moving_an_item_keeps_its_id_and_logs_the_move(self):
        cartons = services.create_units(self.w.s_ops, self.c, "carton", 3)
        services.add_items(self.w.s_ops, self.c, self.w.dcjm, 40, units=cartons)
        item = Item.objects.get(uid=f"{self.c.code}-DCJM-017")
        self.assertEqual(item.unit, cartons[1])
        services.move(self.w.s_ops, item, cartons[2], reason="customs inspection")
        item.refresh_from_db()
        self.assertEqual(item.uid, f"{self.c.code}-DCJM-017")
        self.assertEqual(item.unit, cartons[2])
        movement = item.movements.first()
        self.assertEqual((movement.action, movement.from_code, movement.to_code),
                         (Movement.Action.MOVE, cartons[1].code, cartons[2].code))
        with self.assertRaises(services.ServiceError):
            services.move(self.w.s_ops, item, cartons[2])  # already there

    def test_nesting_rules(self):
        box = services.create_units(self.w.s_ops, self.c, "box", 1)[0]
        carton = services.create_units(self.w.s_ops, self.c, "carton", 1)[0]
        pallet = services.create_units(self.w.s_ops, self.c, "pallet", 1)[0]
        with self.assertRaises(services.ServiceError):
            services.create_units(self.w.s_ops, self.c, "carton", 1, parent=box)
        with self.assertRaises(services.ServiceError):
            services.move(self.w.s_ops, pallet, carton)
        with self.assertRaises(services.ServiceError):
            services.move(self.w.s_ops, carton, carton)
        services.move(self.w.s_ops, box, carton)
        services.move(self.w.s_ops, carton, pallet)
        box.refresh_from_db()
        self.assertEqual([u.pk for u in box.ancestors()], [pallet.pk, carton.pk])

    def test_units_cannot_cross_consignments(self):
        other = consignment(self.w)
        mine = services.create_units(self.w.s_ops, self.c, "carton", 1)[0]
        theirs = services.create_units(self.w.s_ops, other, "pallet", 1)[0]
        with self.assertRaises(services.ServiceError):
            services.move(self.w.s_ops, mine, theirs)

    def test_batch_line_split(self):
        cartons = services.create_units(self.w.s_ops, self.c, "carton", 2)
        line = services.add_items(self.w.s_ops, self.c, self.w.cbl, 20, units=cartons[:1])[0]
        movement = services.move(self.w.s_ops, line, cartons[1], quantity=5)
        line.refresh_from_db()
        self.assertEqual(line.quantity, 15)
        self.assertEqual(movement.item.quantity, 5)
        self.assertEqual(movement.item.unit, cartons[1])
        self.assertNotEqual(movement.item.uid, line.uid)

    def test_capacity_distribution(self):
        self.assertEqual(services.distribute(40, 3), [14, 13, 13])
        self.assertEqual(services.distribute(40, 3, per_unit=15), [15, 15, 10])
        self.assertEqual(services.distribute(7, 0), [7])
        with self.assertRaises(services.ServiceError):
            services.distribute(40, 2, per_unit=10)

    def test_custody_rules(self):
        cartons = services.create_units(self.w.s_ops, self.c, "carton", 2)
        items = services.add_items(self.w.s_ops, self.c, self.w.psu, 2, units=cartons[:1])
        with self.assertRaises(PermissionDenied):  # receiver does not have the goods yet
            services.move(self.w.r_ops, items[0], cartons[1])
        with self.assertRaises(PermissionDenied):  # viewers and oversight never edit
            services.move(self.w.s_view, items[0], cartons[1])
        advance(self.w, self.c, S.READY, S.DISPATCHED, S.EXPORT_CLEARED, S.IN_TRANSIT)
        for item in items:
            item.refresh_from_db()  # each request loads fresh objects; do the same here
        with self.assertRaises(PermissionDenied):  # nobody repacks in transit
            services.move(self.w.s_ops, items[0], cartons[1])
        advance(self.w, self.c, S.ARRIVED)
        for item in items:
            item.refresh_from_db()
        services.move(self.w.r_ops, items[0], cartons[1], reason="Pakistan customs examination")
        with self.assertRaises(PermissionDenied):
            services.move(self.w.s_ops, items[1], cartons[1])

    def test_receiving_and_reconciliation(self):
        pallet = services.create_units(self.w.s_ops, self.c, "pallet", 1)[0]
        cartons = services.create_units(self.w.s_ops, self.c, "carton", 2, parent=pallet)
        services.add_items(self.w.s_ops, self.c, self.w.psu, 4, units=cartons)
        with self.assertRaises(PermissionDenied):
            services.receive(self.w.r_ops, pallet)
        advance(self.w, self.c, S.READY, S.DISPATCHED, S.ARRIVED)
        pallet.refresh_from_db()
        services.receive(self.w.r_ops, cartons[1], condition="damaged")
        units, items = services.receive(self.w.r_ops, pallet, include_contents=True)
        self.assertEqual((units, items), (2, 4))  # pallet + the one carton not yet received; all 4 items
        rec = services.reconciliation(self.c)
        self.assertEqual((rec["units_received"], rec["units_total"]), (3, 3))
        self.assertEqual(rec["items_received"], 4)
        self.assertEqual([u.pk for u in rec["units_damaged"]], [cartons[1].pk])


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class DocumentTests(TestCase):
    def setUp(self):
        self.w = world()
        self.c = consignment(self.w)
        self.ci = DocumentType.objects.get(code="CI")

    def test_upload_versions_and_fingerprint(self):
        first = services.upload_document(self.w.s_ops, self.c, self.ci, pdf())
        second = services.upload_document(self.w.s_ops, self.c, self.ci, pdf("invoice-v2.pdf"))
        self.assertEqual((first.version, second.version), (1, 2))
        self.assertEqual(len(first.sha256), 64)
        self.assertEqual(first.uploaded_by_party, "shipper")
        requirement = self.c.requirements.get(doc_type=self.ci)
        self.assertEqual(requirement.current_document(), second)
        self.assertEqual(requirement.state(), DocumentRequirement.State.UPLOADED)

    def test_upload_rules(self):
        with self.assertRaises(PermissionDenied):  # Receiver does not own the invoice
            services.upload_document(self.w.r_mgr, self.c, self.ci, pdf())
        with self.assertRaises(services.ServiceError):
            services.upload_document(self.w.s_ops, self.c, self.ci, SimpleUploadedFile("x.exe", b"MZ..."))
        with self.assertRaises(services.ServiceError):  # a renamed executable is not a PDF
            services.upload_document(self.w.s_ops, self.c, self.ci, SimpleUploadedFile("x.pdf", b"MZ\x90\x00"))
        with self.assertRaises(services.ServiceError):
            services.upload_document(self.w.s_ops, self.c, self.ci, SimpleUploadedFile("empty.pdf", b""))

    def test_archiving_is_soft_and_restricted(self):
        document = services.upload_document(self.w.s_ops, self.c, self.ci, pdf())
        with self.assertRaises(PermissionDenied):  # operators cannot archive
            services.archive_document(self.w.s_ops, document, "wrong file")
        with self.assertRaises(PermissionDenied):  # the other party cannot archive
            services.archive_document(self.w.r_mgr, document, "wrong file")
        with self.assertRaises(services.ServiceError):
            services.archive_document(self.w.s_mgr, document, "  ")
        services.archive_document(self.w.s_mgr, document, "wrong file")
        document.refresh_from_db()
        self.assertTrue(document.is_archived)
        self.assertTrue(document.file.storage.exists(document.file.name))  # nothing is deleted
        self.assertEqual(self.c.requirements.get(doc_type=self.ci).state(), DocumentRequirement.State.PENDING)

    def test_requirement_due_dates_follow_milestones(self):
        requirement = self.c.requirements.get(doc_type__code="BL")  # due 3 days after "in transit"
        self.assertEqual(requirement.state(), DocumentRequirement.State.PENDING)
        advance(self.w, self.c, S.READY, S.DISPATCHED)
        departed = timezone.now() - timedelta(days=10)
        services.set_status(self.w.s_mgr, self.c, S.IN_TRANSIT, at=departed)
        requirement = DocumentRequirement.objects.get(pk=requirement.pk)
        self.assertEqual(requirement.due_date(), timezone.localdate(departed) + timedelta(days=3))
        self.assertEqual(requirement.state(), DocumentRequirement.State.OVERDUE)
        self.assertEqual(requirement.days_overdue(), 7)

    def test_waiving_needs_a_manager_and_a_reason(self):
        requirement = self.c.requirements.get(doc_type__code="PAY")
        with self.assertRaises(PermissionDenied):
            services.waive_requirement(self.w.s_ops, requirement, "prepaid")
        services.waive_requirement(self.w.r_mgr, requirement, "Paid by LC, documents via bank")
        requirement.refresh_from_db()
        self.assertEqual(requirement.state(), DocumentRequirement.State.WAIVED)


class ReminderTests(TestCase):
    def setUp(self):
        self.w = world()
        self.c = consignment(self.w)
        advance(self.w, self.c, S.READY)  # CI and PL are due from "ready"

    def test_escalation_ladder(self):
        today = timezone.localdate()
        sent = run_reminders(today)
        self.assertEqual(sorted(r.doc_type.code for r, level in sent), ["CI", "PL"])
        self.assertEqual({level for _r, level in sent}, {1})
        self.assertEqual(run_reminders(today), [])  # idempotent on the same day
        run_reminders(today + timedelta(days=2))
        self.assertEqual(ReminderLog.objects.filter(level=2).count(), 2)
        self.assertFalse(Notification.objects.filter(kind="doc_escalated").exists())
        run_reminders(today + timedelta(days=5))
        escalated = Notification.objects.filter(kind="doc_escalated")
        self.assertEqual(set(escalated.values_list("user__username", flat=True)), {"o_view"})
        self.assertEqual(ReminderLog.objects.filter(level=3).count(), 2)

    def test_skipped_levels_are_recorded_and_uploaded_documents_are_left_alone(self):
        with override_settings(MEDIA_ROOT=TEMP_MEDIA):
            services.upload_document(self.w.s_ops, self.c, DocumentType.objects.get(code="CI"), pdf())
        sent = run_reminders(timezone.localdate() + timedelta(days=9))
        self.assertEqual([(r.doc_type.code, level) for r, level in sent], [("PL", 3)])
        self.assertEqual(sorted(ReminderLog.objects.values_list("level", flat=True)), [1, 2, 3])

    def test_reminder_text_is_rendered_in_each_readers_language(self):
        run_reminders(timezone.localdate())
        notification = Notification.objects.get(user=self.w.s_mgr, kind="doc_due", params__doc_type="CI")
        with translation.override("zh-hans"):
            self.assertIn("商业发票", notification.text())
        with translation.override("ur"):
            self.assertIn("کمرشل انوائس", notification.text())
        with translation.override("en"):
            self.assertIn("Commercial Invoice", notification.text())
            self.assertIn("is due", notification.text())


@override_settings(TRANSLATION={"PROVIDER": "dummy"})
class ThreadTests(TestCase):
    def setUp(self):
        from core import translation as mt

        mt.reset_cooldown()
        self.w = world()
        self.c = consignment(self.w)

    def test_conversation_states_and_translation(self):
        thread = services.create_thread(self.w.r_mgr, self.c, category=Thread.Category.QUERY,
                                        subject="Where is the TT copy?", body="Please share the TT copy.")
        self.assertEqual(thread.state, Thread.State.OPEN)
        self.assertEqual(thread.awaiting_party(), "shipper")
        self.assertEqual(TextTranslation.objects.count(), 4)  # subject + body, into zh-hans and ur
        services.reply(self.w.s_mgr, thread, "已发送，请查收。")
        thread.refresh_from_db()
        self.assertEqual(thread.state, Thread.State.ANSWERED)
        self.assertEqual(thread.messages.last().lang, "zh-hans")
        with self.assertRaises(services.ServiceError):  # only the raiser resolves
            services.set_thread_state(self.w.s_mgr, thread, Thread.State.RESOLVED)
        services.set_thread_state(self.w.r_ops, thread, Thread.State.RESOLVED)
        thread.refresh_from_db()
        self.assertEqual(thread.state, Thread.State.RESOLVED)
        services.reply(self.w.s_ops, thread, "One more thing")
        thread.refresh_from_db()
        self.assertEqual(thread.state, Thread.State.OPEN)

    def test_oversight_cannot_post(self):
        with self.assertRaises(PermissionDenied):
            services.create_thread(self.w.o_view, self.c, category="remark", subject="x", body="y")


class ImportTests(TestCase):
    def setUp(self):
        self.w = world()
        self.c = consignment(self.w)

    def test_import_builds_the_hierarchy(self):
        csv_text = (
            "carton,item_code,quantity,oem_serial,batch_no,pallet,container\n"
            "1,DCJM,1,SN-1,,1,MSKU1234565\n"
            "1,DCJM,1,SN-2,,1,MSKU1234565\n"
            "3,PSU,4,,,1,MSKU1234565\n"
            "5,CBL,20,,B7,2,MSKU1234565\n"
        )
        result = import_packing_list(self.w.s_ops, self.c, csv_text.encode("utf-8"))
        self.assertEqual(result["errors"], [])
        self.assertEqual((result["cartons"], result["pallets"], result["containers"]), (3, 2, 1))
        carton5 = HandlingUnit.objects.get(code=f"{self.c.code}-CTN-005")
        self.assertEqual(carton5.parent.code, f"{self.c.code}-PLT-002")
        self.assertEqual(carton5.parent.parent.container_no, "MSKU1234565")
        self.assertEqual(Item.objects.get(oem_serial="SN-2").uid, f"{self.c.code}-DCJM-002")
        # Explicit carton numbers advance the counter: the next new carton is CTN-006.
        self.assertEqual(services.create_units(self.w.s_ops, self.c, "carton", 1)[0].code, f"{self.c.code}-CTN-006")

    def test_chinese_excel_encoding_and_all_or_nothing(self):
        good = "carton,item_code,quantity\n1,PSU,2\n"
        bad = "carton,item_code,quantity\n1,PSU,2\n2,NOPE,1\n3,PSU,zero\n"
        result = import_packing_list(self.w.s_ops, self.c, bad.encode("gb18030"))
        self.assertEqual(len(result["errors"]), 2)
        self.assertFalse(Item.objects.filter(consignment=self.c).exists())
        result = import_packing_list(self.w.s_ops, self.c, ("﻿" + good).encode("utf-8"))
        self.assertEqual(result["items"], 2)

    def test_missing_columns(self):
        with self.assertRaises(services.ServiceError):
            import_packing_list(self.w.s_ops, self.c, b"box,code\n1,PSU\n")
