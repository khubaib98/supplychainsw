from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from accounts.models import Profile
from core.models import AuditLog, Notification
from core.notify import render_notification
from logistics import services
from logistics.importer import import_packing_list
from logistics.models import Consignment, ConsignmentTag, DocumentRequirement, DocumentType, HandlingUnit
from logistics.tests.factories import advance, consignment, user, world

S = Consignment.Status
DELAY, HOLD, PRIORITY, UPDATE = (ConsignmentTag.Kind.DELAY, ConsignmentTag.Kind.HOLD, ConsignmentTag.Kind.PRIORITY,
                                 ConsignmentTag.Kind.UPDATE)


class TagTests(TestCase):
    def setUp(self):
        self.w = world()
        self.w.o_mgr = user("o_mgr", self.w.oversight, Profile.Level.MANAGER)
        self.c = consignment(self.w, eta=timezone.localdate() + timedelta(days=20))
        advance(self.w, self.c, S.READY, S.DISPATCHED, S.IN_TRANSIT)

    def test_receiver_tags_a_delay_with_a_revised_eta(self):
        revised = timezone.localdate() + timedelta(days=27)
        self.assertFalse(self.c.is_delayed)
        tag = services.add_tag(self.w.r_mgr, self.c, DELAY, note="Feeder vessel missed Colombo", revised_eta=revised)
        self.assertEqual((tag.added_by_party, tag.revised_eta), ("receiver", revised))
        fresh = Consignment.objects.get(pk=self.c.pk)
        self.assertTrue(fresh.is_delayed)
        self.assertEqual(fresh.delay_tag(), tag)
        told = set(Notification.objects.filter(kind="tag_added").values_list("user__username", flat=True))
        self.assertEqual(told, {"s_mgr", "s_ops", "o_view", "o_mgr"})
        self.assertTrue(AuditLog.objects.filter(action="tag.added", consignment_code=self.c.code).exists())

    def test_rules_for_tags(self):
        with self.assertRaises(services.ServiceError):
            services.add_tag(self.w.r_mgr, self.c, DELAY)  # a delay needs a reason
        services.add_tag(self.w.r_mgr, self.c, PRIORITY)  # priority does not
        with self.assertRaises(services.ServiceError):
            services.add_tag(self.w.s_mgr, self.c, PRIORITY, note="again")  # one active tag of a kind
        services.add_tag(self.w.s_ops, self.c, UPDATE, note="Booked on the next feeder")
        services.add_tag(self.w.s_ops, self.c, UPDATE, note="Loaded at Colombo")
        tag = services.add_tag(self.w.s_ops, self.c, UPDATE, note="x", revised_eta=timezone.localdate())
        self.assertIsNone(tag.revised_eta)  # only delays carry a revised ETA
        self.assertEqual([t.kind for t in Consignment.objects.get(pk=self.c.pk).active_tags()], [PRIORITY])

    def test_who_may_tag(self):
        services.add_tag(self.w.o_mgr, self.c, HOLD, note="Waiting for payment confirmation")
        for actor in (self.w.o_view, self.w.s_view, self.w.x_mgr):
            with self.assertRaises(PermissionDenied):
                services.add_tag(actor, self.c, UPDATE, note="x")
        told = set(Notification.objects.filter(kind="tag_added").values_list("user__username", flat=True))
        self.assertEqual(told, {"s_mgr", "s_ops", "r_mgr", "r_ops", "o_view"})  # both parties when Oversight acts

    def test_remove_and_arrival_clear_tags(self):
        hold = services.add_tag(self.w.r_mgr, self.c, HOLD, note="Customs inspection")
        services.remove_tag(self.w.s_mgr, hold)
        hold.refresh_from_db()
        self.assertFalse(hold.is_active)
        with self.assertRaises(services.ServiceError):
            services.remove_tag(self.w.s_mgr, hold)
        delay = services.add_tag(self.w.r_mgr, self.c, DELAY, note="Port congestion")
        advance(self.w, self.c, S.ARRIVED)
        delay.refresh_from_db()
        self.assertIsNotNone(delay.removed_at)
        self.assertFalse(Consignment.objects.get(pk=self.c.pk).is_delayed)

    def test_notification_reads_in_each_language(self):
        params = {"code": self.c.code, "user": "Ahmed", "tag": "delay", "note": ""}
        with translation.override("en"):
            self.assertEqual(render_notification("tag_added", params), f"Ahmed tagged {self.c.code}: Delayed.")


class DocumentRequestTests(TestCase):
    def setUp(self):
        self.w = world()
        self.w.o_mgr = user("o_mgr", self.w.oversight, Profile.Level.MANAGER)
        self.c = consignment(self.w)
        self.coo = DocumentType.objects.get(code="COO")
        self.ins = DocumentType.objects.get(code="INS")

    def test_receiver_manager_request_notifies_the_shipper(self):
        services.add_requirement(self.w.r_mgr, self.c, self.coo, due_override=timezone.localdate())
        self.assertTrue(DocumentRequirement.objects.filter(consignment=self.c, doc_type=self.coo).exists())
        told = set(Notification.objects.filter(kind="doc_required").values_list("user__username", flat=True))
        self.assertEqual(told, {"s_mgr", "s_ops", "o_view", "o_mgr"})

    def test_oversight_managers_may_request_documents_viewers_may_not(self):
        services.add_requirement(self.w.o_mgr, self.c, self.ins)
        with self.assertRaises(PermissionDenied):
            services.add_requirement(self.w.o_view, self.c, self.coo)
        with self.assertRaises(PermissionDenied):
            services.add_requirement(self.w.r_ops, self.c, self.coo)


class TagViewTests(TestCase):
    def setUp(self):
        self.w = world()
        self.c = consignment(self.w)
        advance(self.w, self.c, S.READY, S.DISPATCHED)

    def test_tag_from_the_consignment_page(self):
        self.client.force_login(self.w.r_mgr)
        url = reverse("logistics:add_tag", args=[self.c.code])
        revised = (timezone.localdate() + timedelta(days=9)).isoformat()
        response = self.client.post(url, {"tag-kind": "delay", "tag-note": "Vessel rolled over", "tag-revised_eta": revised})
        self.assertRedirects(response, reverse("logistics:consignment", args=[self.c.code]))
        page = self.client.get(reverse("logistics:consignment", args=[self.c.code]))
        self.assertContains(page, "Vessel rolled over")
        self.assertContains(page, revised)
        tag = ConsignmentTag.objects.get(consignment=self.c)
        self.client.post(reverse("logistics:remove_tag", args=[tag.pk]))
        tag.refresh_from_db()
        self.assertIsNotNone(tag.removed_at)

    def test_require_a_document_from_the_documents_page(self):
        self.client.force_login(self.w.r_mgr)
        page = self.client.get(reverse("logistics:documents", args=[self.c.code]))
        self.assertContains(page, 'id="id_req-doc_type"')
        coo = DocumentType.objects.get(code="COO")
        due = timezone.localdate() + timedelta(days=5)
        self.client.post(reverse("logistics:add_requirement", args=[self.c.code]),
                         {"req-doc_type": coo.pk, "req-due_override": due.isoformat()})
        requirement = DocumentRequirement.objects.get(consignment=self.c, doc_type=coo)
        self.assertEqual(requirement.due_override, due)

    def test_read_only_users_cannot_tag(self):
        self.client.force_login(self.w.o_view)
        response = self.client.post(reverse("logistics:add_tag", args=[self.c.code]), {"tag-kind": "update", "tag-note": "x"})
        self.assertEqual(response.status_code, 403)
        page = self.client.get(reverse("logistics:consignment", args=[self.c.code]))
        self.assertNotContains(page, reverse("logistics:add_tag", args=[self.c.code]))


class ImportMeasurementTests(TestCase):
    def setUp(self):
        self.w = world()
        self.c = consignment(self.w)

    def test_carton_weight_and_size(self):
        text = (
            "carton,item_code,quantity,pallet,gross_kg,length_cm,width_cm,height_cm\n"
            "1,PSU,2,1,16.8,60,40,35\n"
            "1,PSU,1,1,,,,\n"
            "2,CBL,30,1,18,60,40,40\n"
        )
        result = import_packing_list(self.w.s_ops, self.c, text.encode("utf-8"))
        self.assertEqual(result["errors"], [])
        carton = HandlingUnit.objects.get(code=f"{self.c.code}-CTN-001")
        self.assertEqual((float(carton.gross_kg), float(carton.length_cm)), (16.8, 60.0))

    def test_conflicting_weights_are_refused(self):
        text = "carton,item_code,quantity,gross_kg\n1,PSU,1,10\n1,PSU,1,12\n2,PSU,1,heavy\n"
        result = import_packing_list(self.w.s_ops, self.c, text.encode("utf-8"))
        self.assertEqual(len(result["errors"]), 2)
        self.assertFalse(HandlingUnit.objects.filter(consignment=self.c).exists())
