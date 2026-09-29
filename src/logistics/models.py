import os
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import Organization
from core.i18n import localized_attr
from core.ids import short_code

def now():
    # Looked up at call time (not bound at import) so the demo seeder can back-date history.
    return timezone.now()


PARTY_CHOICES = [
    (Organization.Kind.SHIPPER, _("Shipper")),
    (Organization.Kind.RECEIVER, _("Receiver")),
]


class ItemType(models.Model):
    """Item master: one row per product, with names in all three languages."""

    class Tracking(models.TextChoices):
        SERIAL = "serial", _("Serialised: one ID and label per unit")
        BATCH = "batch", _("Batch: tracked as a quantity")

    code = models.CharField(
        _("item code"),
        max_length=8,
        unique=True,
        validators=[RegexValidator(r"^[A-Z][A-Z0-9]{1,7}$", _("2–8 capital letters/digits, starting with a letter."))],
        help_text=_("Short code used inside IDs, e.g. DCJM."),
    )
    name_en = models.CharField(_("name (English)"), max_length=150)
    name_zh = models.CharField(_("name (Chinese)"), max_length=150, blank=True)
    name_ur = models.CharField(_("name (Urdu)"), max_length=150, blank=True)
    hs_code = models.CharField(_("HS code"), max_length=12, blank=True)
    tracking = models.CharField(_("tracking"), max_length=10, choices=Tracking.choices, default=Tracking.SERIAL)
    uom = models.CharField(_("unit of measure"), max_length=10, default="pcs")
    controlled = models.BooleanField(
        _("controlled item"),
        default=False,
        help_text=_("Needs end-user authorisation / export licence documents."),
    )
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["code"]
        verbose_name = _("item type")
        verbose_name_plural = _("item master")

    def __str__(self):
        return f"{self.code} – {self.name_en}"

    def name(self, lang=None):
        return localized_attr(self, "name", lang)

    @property
    def is_serial(self):
        return self.tracking == self.Tracking.SERIAL


class Consignment(models.Model):
    class Mode(models.TextChoices):
        SEA = "sea", _("Sea")
        AIR = "air", _("Air")
        ROAD = "road", _("Road (Khunjerab)")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PACKING = "packing", _("Packing")
        READY = "ready", _("Packed, ready to ship")
        DISPATCHED = "dispatched", _("Dispatched from origin")
        EXPORT_CLEARED = "export_cleared", _("Export customs cleared")
        IN_TRANSIT = "in_transit", _("In transit")
        ARRIVED = "arrived", _("Arrived at port of entry")
        IMPORT_CLEARED = "import_cleared", _("Import customs cleared")
        DELIVERED = "delivered", _("Delivered to receiver")
        RECEIVED = "received", _("Received and verified")
        CLOSED = "closed", _("Closed")

    class Incoterm(models.TextChoices):
        EXW = "EXW", "EXW"
        FCA = "FCA", "FCA"
        FOB = "FOB", "FOB"
        CFR = "CFR", "CFR"
        CIF = "CIF", "CIF"
        CPT = "CPT", "CPT"
        CIP = "CIP", "CIP"
        DAP = "DAP", "DAP"
        DPU = "DPU", "DPU"
        DDP = "DDP", "DDP"

    code = models.CharField(_("consignment no."), max_length=32, unique=True, editable=False)
    reference = models.CharField(_("PO / contract reference"), max_length=100, blank=True)
    description = models.CharField(_("description"), max_length=200, blank=True)
    shipper = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="shipments", verbose_name=_("shipper"),
        limit_choices_to={"kind": Organization.Kind.SHIPPER},
    )
    receiver = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="receipts", verbose_name=_("receiver"),
        limit_choices_to={"kind": Organization.Kind.RECEIVER},
    )
    transport_mode = models.CharField(_("transport mode"), max_length=10, choices=Mode.choices, default=Mode.SEA)
    incoterm = models.CharField(_("Incoterm"), max_length=3, choices=Incoterm.choices, blank=True)
    origin = models.CharField(_("origin"), max_length=120)
    destination = models.CharField(_("destination"), max_length=120)
    carrier = models.CharField(_("carrier / forwarder"), max_length=120, blank=True)
    transport_ref = models.CharField(_("B/L, AWB or truck no."), max_length=60, blank=True)
    etd = models.DateField(_("ETD"), null=True, blank=True)
    eta = models.DateField(_("ETA"), null=True, blank=True)
    planned_packages = models.PositiveIntegerField(_("planned no. of packages"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.DRAFT)
    status_changed_at = models.DateTimeField(default=now)
    notes = models.TextField(_("notes"), blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_archived = models.BooleanField(_("archived"), default=False)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("consignment")
        verbose_name_plural = _("consignments")

    def __str__(self):
        return self.code

    # -- milestones ---------------------------------------------------------
    STATUS_ORDER = {value: index for index, value in enumerate(Status.values)}
    # Who records which milestone: the Shipper up to "in transit", the Receiver afterwards.
    STATUS_OWNER = {
        value: (Organization.Kind.SHIPPER if index <= 5 else Organization.Kind.RECEIVER)
        for index, value in enumerate(Status.values)
    }
    # Who has physical custody (and may repack) in each phase. Nobody while in transit or closed.
    CUSTODY = {
        Status.DRAFT: Organization.Kind.SHIPPER,
        Status.PACKING: Organization.Kind.SHIPPER,
        Status.READY: Organization.Kind.SHIPPER,
        Status.DISPATCHED: Organization.Kind.SHIPPER,
        Status.EXPORT_CLEARED: Organization.Kind.SHIPPER,
        Status.ARRIVED: Organization.Kind.RECEIVER,
        Status.IMPORT_CLEARED: Organization.Kind.RECEIVER,
        Status.DELIVERED: Organization.Kind.RECEIVER,
        Status.RECEIVED: Organization.Kind.RECEIVER,
    }

    @property
    def status_index(self):
        return self.STATUS_ORDER[self.status]

    @property
    def custodian(self):
        return self.CUSTODY.get(self.status)

    @property
    def stage(self):
        """Coarse phase for display: origin, transit, destination or done."""
        index = self.status_index
        if index <= 2:
            return "origin"
        if index <= 5:
            return "transit"
        if index <= 8:
            return "destination"
        return "done"

    def status_reached(self, status):
        return self.status_index >= self.STATUS_ORDER[status]

    def reached_at(self, status):
        """When the consignment first reached `status` (or a later milestone)."""
        order = self.STATUS_ORDER[status]
        events = [event for event in self._milestone_cache() if self.STATUS_ORDER[event.status] >= order]
        return min((event.at for event in events), default=None)

    def _milestone_cache(self):
        if not hasattr(self, "_milestones"):
            self._milestones = list(self.milestones.all())
        return self._milestones

    @property
    def is_delayed(self):
        """ETA has passed and the goods have not arrived."""
        return bool(
            self.eta
            and self.eta < timezone.localdate()
            and self.status_index < self.STATUS_ORDER[self.Status.ARRIVED]
        )

    @property
    def is_active(self):
        return not self.is_archived and self.status != self.Status.CLOSED


class MilestoneEvent(models.Model):
    consignment = models.ForeignKey(Consignment, on_delete=models.CASCADE, related_name="milestones")
    status = models.CharField(_("milestone"), max_length=20, choices=Consignment.Status.choices)
    at = models.DateTimeField(_("date and time"), default=now)
    note = models.CharField(_("note"), max_length=300, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]
        verbose_name = _("milestone")
        verbose_name_plural = _("milestones")

    def __str__(self):
        return f"{self.consignment.code} {self.status}"


class HandlingUnit(models.Model):
    """Anything that holds goods: container/truck, pallet, crate, carton, inner box."""

    class Kind(models.TextChoices):
        CONTAINER = "container", _("Container / truck")
        PALLET = "pallet", _("Pallet")
        CRATE = "crate", _("Crate")
        CARTON = "carton", _("Carton")
        BOX = "box", _("Inner box")

    class Condition(models.TextChoices):
        GOOD = "good", _("Good")
        DAMAGED = "damaged", _("Damaged")

    PREFIX = {
        Kind.CONTAINER: "CNT",
        Kind.PALLET: "PLT",
        Kind.CRATE: "CRT",
        Kind.CARTON: "CTN",
        Kind.BOX: "BOX",
    }
    # A unit may only go inside a unit of a strictly higher rank. This keeps
    # the hierarchy sensible (no pallet inside a carton) and makes cycles impossible.
    RANK = {Kind.CONTAINER: 40, Kind.PALLET: 30, Kind.CRATE: 25, Kind.CARTON: 20, Kind.BOX: 10}

    code = models.CharField(_("unit ID"), max_length=48, unique=True, editable=False)
    consignment = models.ForeignKey(Consignment, on_delete=models.CASCADE, related_name="units")
    kind = models.CharField(_("type"), max_length=12, choices=Kind.choices)
    seq = models.PositiveIntegerField()
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children", verbose_name=_("inside")
    )
    container_no = models.CharField(_("container no. (ISO 6346)"), max_length=20, blank=True)
    seal_no = models.CharField(_("seal no."), max_length=30, blank=True)
    length_cm = models.DecimalField(_("length (cm)"), max_digits=7, decimal_places=1, null=True, blank=True)
    width_cm = models.DecimalField(_("width (cm)"), max_digits=7, decimal_places=1, null=True, blank=True)
    height_cm = models.DecimalField(_("height (cm)"), max_digits=7, decimal_places=1, null=True, blank=True)
    gross_kg = models.DecimalField(_("gross weight (kg)"), max_digits=9, decimal_places=2, null=True, blank=True)
    marks = models.CharField(_("extra marks"), max_length=200, blank=True)
    received_at = models.DateTimeField(_("received"), null=True, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    condition = models.CharField(_("condition"), max_length=10, choices=Condition.choices, default=Condition.GOOD)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["kind", "seq"]
        constraints = [models.UniqueConstraint(fields=["consignment", "kind", "seq"], name="uniq_unit_seq")]
        verbose_name = _("handling unit")
        verbose_name_plural = _("handling units")

    def __str__(self):
        return self.code

    @property
    def short(self):
        return short_code(self.code)

    @property
    def rank(self):
        return self.RANK[self.kind]

    def ancestors(self):
        """Outermost first: [container, pallet, …, parent]."""
        chain = []
        node = self.parent
        seen = set()
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            chain.append(node)
            node = node.parent
        return list(reversed(chain))

    def descendants(self):
        """All units nested (at any depth) inside this one."""
        result = []
        frontier = [self]
        while frontier:
            children = list(HandlingUnit.objects.filter(parent__in=frontier, is_archived=False))
            result.extend(children)
            frontier = children
        return result


class Item(models.Model):
    """One serialised unit of an item type, or one batch line (quantity > 1)."""

    class Condition(models.TextChoices):
        GOOD = "good", _("Good")
        DAMAGED = "damaged", _("Damaged")

    uid = models.CharField(_("item ID"), max_length=48, unique=True, editable=False)
    consignment = models.ForeignKey(Consignment, on_delete=models.CASCADE, related_name="items")
    item_type = models.ForeignKey(ItemType, on_delete=models.PROTECT, related_name="items", verbose_name=_("item type"))
    seq = models.PositiveIntegerField()
    quantity = models.PositiveIntegerField(_("quantity"), default=1)
    unit = models.ForeignKey(
        HandlingUnit, null=True, blank=True, on_delete=models.SET_NULL, related_name="items", verbose_name=_("inside")
    )
    oem_serial = models.CharField(_("manufacturer serial no."), max_length=80, blank=True, db_index=True)
    batch_no = models.CharField(_("batch / lot no."), max_length=60, blank=True)
    received_at = models.DateTimeField(_("received"), null=True, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    condition = models.CharField(_("condition"), max_length=10, choices=Condition.choices, default=Condition.GOOD)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["item_type__code", "seq"]
        constraints = [models.UniqueConstraint(fields=["consignment", "item_type", "seq"], name="uniq_item_seq")]
        verbose_name = _("item")
        verbose_name_plural = _("items")

    def __str__(self):
        return self.uid

    @property
    def short(self):
        return short_code(self.uid)


class Movement(models.Model):
    """Event log of every placement: packing, moving, splitting, receiving (EPCIS-style)."""

    class Action(models.TextChoices):
        PACK = "pack", _("Packed")
        MOVE = "move", _("Moved")
        UNPACK = "unpack", _("Taken out")
        SPLIT = "split", _("Split")
        RECEIVE = "receive", _("Received")

    consignment = models.ForeignKey(Consignment, on_delete=models.CASCADE, related_name="movements")
    item = models.ForeignKey(Item, null=True, blank=True, on_delete=models.CASCADE, related_name="movements")
    unit = models.ForeignKey(HandlingUnit, null=True, blank=True, on_delete=models.CASCADE, related_name="movements")
    action = models.CharField(_("action"), max_length=10, choices=Action.choices)
    from_unit = models.ForeignKey(HandlingUnit, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    to_unit = models.ForeignKey(HandlingUnit, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    from_code = models.CharField(_("from"), max_length=48, blank=True)
    to_code = models.CharField(_("to"), max_length=48, blank=True)
    quantity = models.PositiveIntegerField(_("quantity"), default=1)
    reason = models.CharField(_("reason"), max_length=300, blank=True)
    place = models.CharField(_("location"), max_length=120, blank=True)
    at = models.DateTimeField(_("time"), default=now)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-at", "-id"]
        verbose_name = _("movement")
        verbose_name_plural = _("movements")

    def __str__(self):
        subject = self.item.uid if self.item_id else (self.unit.code if self.unit_id else "?")
        return f"{self.action} {subject}"

    @property
    def subject_code(self):
        if self.item_id:
            return self.item.uid
        if self.unit_id:
            return self.unit.code
        return ""


class DocumentType(models.Model):
    """Configurable catalogue of documents and who owes them, when."""

    class Party(models.TextChoices):
        SHIPPER = "shipper", _("Shipper")
        RECEIVER = "receiver", _("Receiver")
        ANY = "any", _("Either party")

    code = models.CharField(_("code"), max_length=10, unique=True)
    name_en = models.CharField(_("name (English)"), max_length=120)
    name_zh = models.CharField(_("name (Chinese)"), max_length=120, blank=True)
    name_ur = models.CharField(_("name (Urdu)"), max_length=120, blank=True)
    responsible = models.CharField(_("uploaded by"), max_length=10, choices=Party.choices)
    default_required = models.BooleanField(_("required by default"), default=False)
    modes = models.CharField(
        _("transport modes"), max_length=40, blank=True, help_text=_("Comma-separated (sea,air,road). Empty = all.")
    )
    due_status = models.CharField(
        _("due when status reaches"), max_length=20, choices=Consignment.Status.choices, default=Consignment.Status.READY
    )
    due_offset_days = models.IntegerField(_("… plus days"), default=0)
    sort_order = models.PositiveIntegerField(default=100)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["sort_order", "code"]
        verbose_name = _("document type")
        verbose_name_plural = _("document types")

    def __str__(self):
        return f"{self.code} – {self.name_en}"

    def name(self, lang=None):
        return localized_attr(self, "name", lang)

    def applies_to(self, mode):
        allowed = [part.strip() for part in self.modes.split(",") if part.strip()]
        return not allowed or mode in allowed


class DocumentRequirement(models.Model):
    """A document that must be on file for one consignment."""

    class State(models.TextChoices):
        PENDING = "pending", _("Not yet due")
        DUE = "due", _("Due")
        OVERDUE = "overdue", _("Overdue")
        UPLOADED = "uploaded", _("Uploaded")
        WAIVED = "waived", _("Waived")

    consignment = models.ForeignKey(Consignment, on_delete=models.CASCADE, related_name="requirements")
    doc_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name="+", verbose_name=_("document"))
    due_override = models.DateField(_("due date (override)"), null=True, blank=True)
    waived = models.BooleanField(_("waived"), default=False)
    waived_reason = models.CharField(_("reason"), max_length=300, blank=True)
    waived_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["doc_type__sort_order", "doc_type__code"]
        constraints = [models.UniqueConstraint(fields=["consignment", "doc_type"], name="uniq_requirement")]
        verbose_name = _("required document")
        verbose_name_plural = _("required documents")

    def __str__(self):
        return f"{self.consignment.code} {self.doc_type.code}"

    def due_date(self):
        if self.due_override:
            return self.due_override
        reached = self.consignment.reached_at(self.doc_type.due_status)
        if reached is None:
            return None
        return timezone.localdate(reached) + timedelta(days=self.doc_type.due_offset_days)

    def current_document(self):
        docs = getattr(self, "_docs", None)
        if docs is None:
            docs = [
                doc
                for doc in self.consignment.documents.all()
                if doc.doc_type_id == self.doc_type_id and not doc.is_archived
            ]
        return max(docs, key=lambda doc: doc.version, default=None)

    def state(self, today=None):
        if self.current_document() is not None:
            return self.State.UPLOADED
        if self.waived:
            return self.State.WAIVED
        due = self.due_date()
        if due is None:
            return self.State.PENDING
        today = today or timezone.localdate()
        return self.State.OVERDUE if due < today else self.State.DUE

    def days_overdue(self, today=None):
        due = self.due_date()
        if due is None:
            return 0
        return max(((today or timezone.localdate()) - due).days, 0)


def document_upload_path(instance, filename):
    extension = os.path.splitext(filename)[1].lower()[:10]
    return f"documents/{instance.consignment.code}/{uuid.uuid4().hex}{extension}"


class ConsignmentDocument(models.Model):
    consignment = models.ForeignKey(Consignment, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name="+", verbose_name=_("document type"))
    version = models.PositiveIntegerField(_("version"), default=1)
    file = models.FileField(upload_to=document_upload_path, max_length=255)
    original_name = models.CharField(_("file name"), max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField(_("size"))
    sha256 = models.CharField(_("SHA-256 fingerprint"), max_length=64)
    note = models.CharField(_("note"), max_length=300, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_by_party = models.CharField(max_length=10, choices=PARTY_CHOICES)
    uploaded_at = models.DateTimeField(_("uploaded"), default=now)
    is_archived = models.BooleanField(_("archived"), default=False)
    archived_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    archived_at = models.DateTimeField(null=True, blank=True)
    archive_reason = models.CharField(_("reason for archiving"), max_length=300, blank=True)

    class Meta:
        ordering = ["doc_type__sort_order", "-version"]
        verbose_name = _("document")
        verbose_name_plural = _("documents")

    def __str__(self):
        return f"{self.consignment.code} {self.doc_type.code} v{self.version}"


class ReminderLog(models.Model):
    requirement = models.ForeignKey(DocumentRequirement, on_delete=models.CASCADE, related_name="reminders")
    level = models.PositiveSmallIntegerField()
    sent_at = models.DateTimeField(default=now)
    recipients = models.TextField(blank=True)

    class Meta:
        ordering = ["-sent_at"]
        constraints = [models.UniqueConstraint(fields=["requirement", "level"], name="uniq_reminder_level")]


class Thread(models.Model):
    """A remark, query or issue on a consignment, answered in a conversation."""

    class Category(models.TextChoices):
        REMARK = "remark", _("Remark")
        QUERY = "query", _("Query")
        DISCREPANCY = "discrepancy", _("Discrepancy")
        DAMAGE = "damage", _("Damage")
        DOCUMENT = "document", _("Document issue")
        PAYMENT = "payment", _("Payment")

    class State(models.TextChoices):
        OPEN = "open", _("Open")
        ANSWERED = "answered", _("Answered")
        RESOLVED = "resolved", _("Resolved")

    consignment = models.ForeignKey(Consignment, on_delete=models.CASCADE, related_name="threads")
    category = models.CharField(_("type"), max_length=12, choices=Category.choices, default=Category.REMARK)
    subject = models.CharField(_("subject"), max_length=200)
    subject_lang = models.CharField(max_length=10, default="en")
    state = models.CharField(_("state"), max_length=10, choices=State.choices, default=State.OPEN)
    raised_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    raised_by_party = models.CharField(max_length=10, choices=PARTY_CHOICES)
    document = models.ForeignKey(ConsignmentDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name="threads")
    unit = models.ForeignKey(HandlingUnit, null=True, blank=True, on_delete=models.SET_NULL, related_name="threads")
    item = models.ForeignKey(Item, null=True, blank=True, on_delete=models.SET_NULL, related_name="threads")
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(default=now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = _("remark / query")
        verbose_name_plural = _("remarks / queries")

    def __str__(self):
        return f"{self.consignment.code}: {self.subject}"

    @property
    def is_open(self):
        return self.state != self.State.RESOLVED

    def awaiting_party(self):
        """Whose move it is: the other party answers an open thread; the raiser resolves an answered one."""
        if self.state == self.State.OPEN:
            return Organization.Kind.RECEIVER if self.raised_by_party == Organization.Kind.SHIPPER else Organization.Kind.SHIPPER
        if self.state == self.State.ANSWERED:
            return self.raised_by_party
        return None


class Message(models.Model):
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    author_party = models.CharField(max_length=10, choices=PARTY_CHOICES)
    body = models.TextField(_("message"), max_length=4000)
    lang = models.CharField(max_length=10, default="en")
    created_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.body[:60]
