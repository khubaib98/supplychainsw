"""
Pages for the person who looks after the portal: the first-run set-up of the
desktop edition, the People page (accounts with temporary passwords) and the
settings of the computer the desktop edition runs on.
"""
import hmac
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from accounts import people
from accounts.forms import ComputerSettingsForm, PersonForm, SetupForm
from accounts.models import Organization, Profile
from core import audit, desktop
from core.templatetags.portal import qr_svg
from core.views import BOOT_ID

SETUP_SESSION_KEY = "setup-token"
KIND_ORDER = {Organization.Kind.SHIPPER: 0, Organization.Kind.RECEIVER: 1, Organization.Kind.OVERSIGHT: 2}
URL_LABELS = {
    "local": gettext_lazy("On this computer"),
    "network": gettext_lazy("In the office network"),
    "public": gettext_lazy("From anywhere (internet)"),
}


def superuser_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _same_token(supplied, expected):
    return hmac.compare_digest(str(supplied).encode("utf-8"), str(expected).encode("utf-8"))


def setup_view(request):
    """First start of the desktop edition. Only works with the one-time link from the launcher window."""
    token = settings.PORTAL["SETUP_TOKEN"]
    if not token:
        raise Http404
    if get_user_model().objects.exists():
        return redirect("accounts:login")
    supplied = request.GET.get("token")
    if supplied is not None and _same_token(supplied, token):
        request.session[SETUP_SESSION_KEY] = token
        return redirect("accounts:setup")  # keep the key out of the address bar and history
    if not _same_token(request.session.get(SETUP_SESSION_KEY, ""), token):
        return render(request, "accounts/setup_locked.html", status=403)
    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            admin = people.complete_setup(form.cleaned_data, request=request)
        except ValueError:  # someone else finished first
            return redirect("accounts:login")
        login(request, admin, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, _("The portal is ready. Now add the people who will use it."))
        return redirect("accounts:people")
    return render(request, "accounts/setup.html", {"form": form})


def _access_urls():
    rows = []
    for entry in people.access_urls():
        rows.append({
            **entry,
            "label": URL_LABELS.get(entry["kind"], entry["kind"]),
            "qr": qr_svg(entry["url"], scale=3, border=1) if entry["kind"] != "local" else "",
        })
    return rows


@superuser_required
def people_view(request):
    User = get_user_model()
    profiles = list(
        Profile.objects.select_related("user", "organization").order_by(
            "-user__is_active", "user__first_name", "user__username"
        )
    )
    organizations = sorted(Organization.objects.all(), key=lambda org: (KIND_ORDER.get(org.kind, 9), org.name))
    groups = [
        {"organization": org, "members": [p for p in profiles if p.organization_id == org.pk]} for org in organizations
    ]
    context = {
        "groups": groups,
        "admins": User.objects.filter(is_superuser=True).order_by("username"),
        "unassigned": User.objects.filter(is_superuser=False, profile__isnull=True).order_by("username"),
        "urls": _access_urls(),
        "shareable": people.shareable_url(),
        "desktop": settings.PORTAL["DESKTOP"],
    }
    if settings.PORTAL["DESKTOP"]:
        home = settings.DESKTOP_HOME
        try:
            values = desktop.load_settings(home)
        except desktop.SettingsError as exc:
            messages.error(request, str(exc))
            values = desktop.DEFAULTS
        latest = desktop.latest_backup(home)
        context.update({
            "home": home,
            "backups_folder": desktop.backup_folder(home),
            "latest_backup": latest[0] if latest else None,
            "computer_form": ComputerSettingsForm(initial={
                "network_access": values["network_access"],
                "public_url": values["public_url"],
                "require_two_factor": values["require_two_factor"],
            }),
        })
    return render(request, "accounts/people.html", context)


@superuser_required
def person_create(request):
    form = PersonForm(request.POST or None, initial={"organization": request.GET.get("organization")})
    if request.method == "POST" and form.is_valid():
        user, password = people.create_person(request.user, form.cleaned_data, request=request)
        request.session[f"people:password:{user.pk}"] = password
        return redirect("accounts:person_welcome", pk=user.pk)
    return render(request, "accounts/person_form.html", {"form": form, "person": None})


def _person(pk):
    return get_object_or_404(get_user_model().objects.select_related("profile__organization"), pk=pk)


@superuser_required
def person_edit(request, pk):
    person = _person(pk)
    profile = getattr(person, "profile", None)
    if profile is None:  # administrators are managed in system administration
        return redirect("admin:auth_user_change", person.pk)
    initial = {
        "first_name": person.first_name,
        "last_name": person.last_name,
        "username": person.username,
        "email": person.email,
        "organization": profile.organization,
        "level": profile.level,
        "language": profile.language,
        "timezone": profile.timezone,
    }
    form = PersonForm(request.POST or None, initial=initial, person=person)
    if request.method == "POST" and form.is_valid():
        people.update_person(request.user, person, form.cleaned_data, request=request)
        messages.success(request, _("Saved."))
        return redirect("accounts:people")
    return render(request, "accounts/person_form.html", {"form": form, "person": person, "profile": profile})


@superuser_required
def person_welcome(request, pk):
    person = _person(pk)
    password = request.session.pop(f"people:password:{pk}", None)
    url = people.shareable_url()
    profile = getattr(person, "profile", None)
    context = {
        "person": person,
        "password": password,
        "url": url,
        "language": profile.language if profile else settings.LANGUAGE_CODE,
    }
    if password:
        context["message"] = people.welcome_message(person, password, url)
    return render(request, "accounts/person_welcome.html", context)


@superuser_required
@require_POST
def person_action(request, pk):
    person = _person(pk)
    action = request.POST.get("action")
    name = person.get_full_name() or person.get_username()
    if person == request.user:
        messages.error(request, _("You cannot do this to your own account."))
        return redirect("accounts:people")
    has_profile = getattr(person, "profile", None) is not None
    if action == "reset_password" and has_profile:
        request.session[f"people:password:{person.pk}"] = people.reset_password(request.user, person, request=request)
        return redirect("accounts:person_welcome", pk=person.pk)
    if action == "reset_2fa" and has_profile:
        people.reset_two_factor(request.user, person, request=request)
        messages.success(request, _("Two-factor authentication was reset for %(name)s.") % {"name": name})
    elif action == "deactivate":
        people.set_active(request.user, person, False, request=request)
        messages.success(request, _("%(name)s can no longer sign in. Their history is kept.") % {"name": name})
    elif action == "reactivate":
        people.set_active(request.user, person, True, request=request)
        messages.success(request, _("%(name)s can sign in again.") % {"name": name})
    else:
        messages.error(request, _("This action is not available."))
    return redirect("accounts:people")


def _desktop_home():
    if not settings.PORTAL["DESKTOP"]:
        raise Http404
    return settings.DESKTOP_HOME


@superuser_required
@require_POST
def computer_settings(request):
    home = _desktop_home()
    form = ComputerSettingsForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect("accounts:people")
    try:
        desktop.update_settings(home, **form.cleaned_data)
    except desktop.SettingsError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:people")
    audit.log(request.user, "settings.computer", None, request=request, label=desktop.SETTINGS_FILE,
              details=form.cleaned_data)
    if desktop.request_restart():
        context = {"next": reverse("accounts:people"), "boot": BOOT_ID}
        return render(request, "accounts/restarting.html", context)
    messages.success(request, _("Saved. Restart the portal to use the new settings."))
    return redirect("accounts:people")


@superuser_required
@require_POST
def backup_now(request):
    home = _desktop_home()
    try:
        values = desktop.load_settings(home)
    except desktop.SettingsError:
        values = desktop.DEFAULTS
    result = desktop.make_backup(home, reason="manual", keep_days=values["keep_days"], copy_to=values["copy_to"])
    audit.log(request.user, "settings.backup", None, request=request, label=result.path.name)
    messages.success(request, _("Backup saved: %(file)s") % {"file": result.path.name})
    if result.copy_error:
        messages.warning(request, result.copy_error)
    return redirect("accounts:people")
