from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class EnforceTwoFactorMiddleware:
    """When PORTAL_REQUIRE_2FA is on, users must set up 2FA before using the portal."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.PORTAL["REQUIRE_2FA"] and request.user.is_authenticated:
            profile = getattr(request.user, "profile", None)
            needs_setup = profile is not None and not profile.totp_enabled
            allowed = (
                reverse("accounts:security"),
                reverse("accounts:logout"),
                reverse("set_language"),
            )
            if needs_setup and not request.path.startswith(allowed) and not request.path.startswith(settings.STATIC_URL):
                return redirect("accounts:security")
        return self.get_response(request)
