from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from accounts.models import Organization, Profile


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "country", "default_language", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "name_local")


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    fields = ("organization", "level", "language", "timezone", "phone", "totp_enabled")
    readonly_fields = ("totp_enabled",)


User = get_user_model()
admin.site.unregister(User)


@admin.register(User)
class PortalUserAdmin(UserAdmin):
    inlines = [ProfileInline]
    list_display = ("username", "email", "first_name", "last_name", "organization", "level", "is_active")
    list_select_related = ("profile__organization",)

    @admin.display(description="Organisation")
    def organization(self, user):
        profile = getattr(user, "profile", None)
        return profile.organization if profile else "—"

    @admin.display(description="Level")
    def level(self, user):
        profile = getattr(user, "profile", None)
        return profile.get_level_display() if profile else "—"
