from django.contrib import admin

from core.models import AuditLog, Notification, Sequence, TextTranslation


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("at", "username", "organization", "action", "target_label", "consignment_code")
    list_filter = ("action", "organization")
    search_fields = ("username", "target_label", "consignment_code", "action")
    date_hierarchy = "at"

    # The audit trail is append-only, even for administrators.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Sequence)
class SequenceAdmin(admin.ModelAdmin):
    list_display = ("key", "value")
    search_fields = ("key",)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "kind", "consignment", "read_at")
    list_filter = ("kind",)


@admin.register(TextTranslation)
class TextTranslationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "source_lang", "target_lang", "provider", "text")
    list_filter = ("provider", "target_lang")
