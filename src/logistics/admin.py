from django.contrib import admin

from logistics.models import (
    Consignment,
    ConsignmentDocument,
    DocumentRequirement,
    DocumentType,
    HandlingUnit,
    Item,
    ItemType,
    Message,
    MilestoneEvent,
    Movement,
    ReminderLog,
    Thread,
)


@admin.register(ItemType)
class ItemTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name_en", "name_zh", "name_ur", "tracking", "controlled", "is_active")
    list_filter = ("tracking", "controlled", "is_active")
    search_fields = ("code", "name_en", "name_zh", "name_ur", "hs_code")


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name_en", "responsible", "default_required", "modes", "due_status", "due_offset_days", "sort_order")
    list_editable = ("default_required", "sort_order")
    list_filter = ("responsible", "default_required")


class MilestoneInline(admin.TabularInline):
    model = MilestoneEvent
    extra = 0


class RequirementInline(admin.TabularInline):
    model = DocumentRequirement
    extra = 0


@admin.register(Consignment)
class ConsignmentAdmin(admin.ModelAdmin):
    list_display = ("code", "reference", "shipper", "receiver", "transport_mode", "status", "etd", "eta", "is_archived")
    list_filter = ("status", "transport_mode", "is_archived")
    search_fields = ("code", "reference", "description", "transport_ref")
    inlines = [MilestoneInline, RequirementInline]
    readonly_fields = ("code",)


@admin.register(HandlingUnit)
class HandlingUnitAdmin(admin.ModelAdmin):
    list_display = ("code", "kind", "parent", "container_no", "received_at", "condition")
    list_filter = ("kind", "condition")
    search_fields = ("code", "container_no", "seal_no")
    raw_id_fields = ("consignment", "parent")


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("uid", "item_type", "quantity", "unit", "oem_serial", "received_at", "condition")
    list_filter = ("item_type", "condition")
    search_fields = ("uid", "oem_serial", "batch_no")
    raw_id_fields = ("consignment", "unit")


@admin.register(Movement)
class MovementAdmin(admin.ModelAdmin):
    list_display = ("at", "action", "subject_code", "from_code", "to_code", "quantity", "by")
    list_filter = ("action",)
    search_fields = ("from_code", "to_code", "item__uid", "unit__code")


@admin.register(ConsignmentDocument)
class ConsignmentDocumentAdmin(admin.ModelAdmin):
    list_display = ("consignment", "doc_type", "version", "original_name", "uploaded_at", "uploaded_by", "is_archived")
    list_filter = ("doc_type", "is_archived")
    search_fields = ("consignment__code", "original_name", "sha256")


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0


@admin.register(Thread)
class ThreadAdmin(admin.ModelAdmin):
    list_display = ("consignment", "category", "subject", "state", "raised_by", "updated_at")
    list_filter = ("category", "state")
    inlines = [MessageInline]


@admin.register(ReminderLog)
class ReminderLogAdmin(admin.ModelAdmin):
    list_display = ("requirement", "level", "sent_at", "recipients")
