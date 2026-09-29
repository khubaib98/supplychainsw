from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import PasswordChangeView
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.i18n import set_language as django_set_language

from accounts import totp
from accounts.forms import OneTimeCodeForm, PreferencesForm
from core import audit
from core.templatetags.portal import qr_svg

PENDING_USER = "2fa_pending_user"
PENDING_BACKEND = "2fa_pending_backend"
PENDING_NEXT = "2fa_pending_next"


def _lock_keys(request, username):
    ip = audit.client_ip(request) or "unknown"
    return f"login-fail:user:{(username or '').lower()}", f"login-fail:ip:{ip}"


def _is_locked(request, username):
    limit = settings.PORTAL["LOGIN_MAX_FAILURES"]
    return any((cache.get(key) or 0) >= limit for key in _lock_keys(request, username))


def _record_failure(request, username):
    timeout = settings.PORTAL["LOGIN_LOCK_MINUTES"] * 60
    for key in _lock_keys(request, username):
        cache.set(key, (cache.get(key) or 0) + 1, timeout)


def _clear_failures(request, username):
    cache.delete_many(list(_lock_keys(request, username)))


def _safe_next(request, value):
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return value
    return reverse_lazy(settings.LOGIN_REDIRECT_URL)


def login_view(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    next_url = request.POST.get("next") or request.GET.get("next", "")
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST":
        username = request.POST.get("username", "")
        if _is_locked(request, username):
            form = AuthenticationForm(request)
            messages.error(request, _("Too many failed attempts. Please wait 15 minutes and try again."))
        elif form.is_valid():
            user = form.get_user()
            _clear_failures(request, username)
            profile = getattr(user, "profile", None)
            if profile is not None and profile.totp_enabled:
                request.session[PENDING_USER] = user.pk
                request.session[PENDING_BACKEND] = user.backend
                request.session[PENDING_NEXT] = str(_safe_next(request, next_url))
                return redirect("accounts:login_2fa")
            login(request, user)
            audit.log(user, "auth.login", user, request=request, label=user.get_username())
            return redirect(_safe_next(request, next_url))
        else:
            _record_failure(request, username)
            audit.log(None, "auth.login_failed", None, request=request, label=username[:150])
    return render(request, "accounts/login.html", {"form": form, "next": next_url})


def login_2fa_view(request):
    user_id = request.session.get(PENDING_USER)
    if not user_id:
        return redirect("accounts:login")
    user = get_user_model().objects.select_related("profile").filter(pk=user_id, is_active=True).first()
    if user is None:
        request.session.flush()
        return redirect("accounts:login")
    form = OneTimeCodeForm(request.POST or None)
    if request.method == "POST":
        if _is_locked(request, user.get_username()):
            request.session.flush()
            messages.error(request, _("Too many failed attempts. Please wait 15 minutes and try again."))
            return redirect("accounts:login")
        if form.is_valid():
            profile = user.profile
            step = totp.verify(profile.totp_secret, form.cleaned_data["code"], profile.totp_last_step)
            if step is not None:
                profile.totp_last_step = step
                profile.save(update_fields=["totp_last_step"])
                backend = request.session.get(PENDING_BACKEND)
                next_url = request.session.get(PENDING_NEXT) or settings.LOGIN_REDIRECT_URL
                for key in (PENDING_USER, PENDING_BACKEND, PENDING_NEXT):
                    request.session.pop(key, None)
                user.backend = backend
                login(request, user)
                audit.log(user, "auth.login", user, request=request, label=user.get_username(), details={"2fa": True})
                return redirect(next_url)
        _record_failure(request, user.get_username())
        form.add_error("code", _("That code is not valid. Check the time on your phone and try again."))
    return render(request, "accounts/login_2fa.html", {"form": form})


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        audit.log(request.user, "auth.logout", request.user, request=request, label=request.user.get_username())
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


def set_language(request):
    """Django's set_language, plus remembering the choice on the user's profile."""
    response = django_set_language(request)
    language = request.POST.get("language")
    profile = getattr(request.user, "profile", None) if request.user.is_authenticated else None
    if profile is not None and language in dict(settings.LANGUAGES):
        profile.language = language
        profile.save(update_fields=["language"])
    return response


@login_required
def account_view(request):
    profile = getattr(request.user, "profile", None)
    if profile is None:
        messages.info(request, _("This administrator account has no organisation profile. Use the admin site."))
        return redirect("admin:index")
    initial = {
        "language": profile.language,
        "timezone": profile.timezone,
        "email": request.user.email,
        "phone": profile.phone,
    }
    form = PreferencesForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        profile.language = form.cleaned_data["language"]
        profile.timezone = form.cleaned_data["timezone"]
        profile.phone = form.cleaned_data["phone"]
        profile.save(update_fields=["language", "timezone", "phone"])
        request.user.email = form.cleaned_data["email"]
        request.user.save(update_fields=["email"])
        audit.log(request.user, "account.preferences", request.user, request=request, label=request.user.get_username())
        messages.success(request, _("Preferences saved."))
        response = redirect("accounts:account")
        response.set_cookie(settings.LANGUAGE_COOKIE_NAME, profile.language, samesite="Lax")
        return response
    return render(request, "accounts/account.html", {"form": form, "profile": profile})


@login_required
def security_view(request):
    profile = getattr(request.user, "profile", None)
    if profile is None:
        return redirect("admin:index")
    form = OneTimeCodeForm(request.POST or None)
    context = {"profile": profile, "form": form, "required": settings.PORTAL["REQUIRE_2FA"]}
    if profile.totp_enabled:
        if request.method == "POST" and form.is_valid():
            step = totp.verify(profile.totp_secret, form.cleaned_data["code"], profile.totp_last_step)
            if step is None:
                form.add_error("code", _("That code is not valid."))
            elif settings.PORTAL["REQUIRE_2FA"]:
                messages.error(request, _("Two-factor authentication is mandatory on this portal."))
            else:
                profile.totp_enabled = False
                profile.totp_secret = ""
                profile.save(update_fields=["totp_enabled", "totp_secret"])
                audit.log(request.user, "auth.2fa_disabled", request.user, request=request, label=request.user.get_username())
                messages.success(request, _("Two-factor authentication is now off."))
                return redirect("accounts:security")
        return render(request, "accounts/security.html", context)

    secret = request.session.get("2fa_setup_secret") or totp.new_secret()
    request.session["2fa_setup_secret"] = secret
    uri = totp.provisioning_uri(secret, request.user.get_username(), settings.PORTAL["NAME"])
    if request.method == "POST" and form.is_valid():
        step = totp.verify(secret, form.cleaned_data["code"])
        if step is None:
            form.add_error("code", _("That code is not valid. Check the time on your phone and try again."))
        else:
            profile.totp_secret = secret
            profile.totp_enabled = True
            profile.totp_last_step = step
            profile.save(update_fields=["totp_secret", "totp_enabled", "totp_last_step"])
            request.session.pop("2fa_setup_secret", None)
            audit.log(request.user, "auth.2fa_enabled", request.user, request=request, label=request.user.get_username())
            messages.success(request, _("Two-factor authentication is on. You will need a code at every login."))
            return redirect("accounts:security")
    context.update({"secret": secret, "qr": qr_svg(uri, scale=4)})
    return render(request, "accounts/security.html", context)


class PortalPasswordChangeView(PasswordChangeView):
    template_name = "accounts/password.html"
    success_url = reverse_lazy("accounts:account")

    def form_valid(self, form):
        response = super().form_valid(form)
        audit.log(self.request.user, "auth.password_changed", self.request.user, request=self.request,
                  label=self.request.user.get_username())
        messages.success(self.request, _("Password changed."))
        return response
