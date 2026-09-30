from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class AccountGateMiddleware:
    """
    Keeps a signed-in user on the one page they must complete first:
      1. choose their own password (after an administrator created or reset the account);
      2. set up two-factor authentication (when PORTAL_REQUIRE_2FA is on).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            target = self.required_page(request.user)
            if target and not self.allowed(request.path, target):
                return redirect(target)
        return self.get_response(request)

    @staticmethod
    def required_page(user):
        profile = getattr(user, "profile", None)
        if profile is None:
            return None
        if profile.must_change_password:
            return reverse("accounts:password")
        if settings.PORTAL["REQUIRE_2FA"] and not profile.totp_enabled:
            return reverse("accounts:security")
        return None

    @staticmethod
    def allowed(path, target):
        always = (target, reverse("accounts:logout"), reverse("set_language"), settings.STATIC_URL)
        return path.startswith(always)
