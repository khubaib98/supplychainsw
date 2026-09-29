"""
Managing the people who use the portal, without the Django admin: accounts are
created with a temporary password that the person replaces at first sign-in.
Nobody is deleted; leavers are deactivated so their history stays readable.
"""
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import translation
from django.utils.translation import gettext as _

from accounts.models import Organization, Profile
from core import audit

# No 0/o, 1/l/i: temporary passwords are read out or retyped from a chat message.
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

COUNTRY_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Shanghai",
    "PK": "Asia/Karachi",
    "AE": "Asia/Dubai",
    "SG": "Asia/Singapore",
    "GB": "Europe/London",
}


def temporary_password():
    """Readable one-time password, e.g. k7mp-x2qd-9twb (about 59 bits)."""
    return "-".join("".join(secrets.choice(PASSWORD_ALPHABET) for _i in range(4)) for _j in range(3))


def default_timezone(organization):
    return COUNTRY_TIMEZONES.get((organization.country or "").upper(), "UTC")


def access_urls():
    """Where people open the portal: the launcher's addresses, or the configured public address."""
    return settings.PORTAL["ACCESS_URLS"] or [{"kind": "public", "url": settings.PORTAL["BASE_URL"]}]


def shareable_url():
    """The address to send to someone who is not sitting at this computer (None if there is none)."""
    urls = access_urls()
    for kind in ("public", "network"):
        for entry in urls:
            if entry["kind"] == kind:
                return entry["url"]
    return None


@transaction.atomic
def create_person(admin, data, request=None):
    """Create a login + profile. Returns (user, temporary password)."""
    organization = data["organization"]
    password = temporary_password()
    user = get_user_model().objects.create_user(
        data["username"],
        email=data.get("email", ""),
        password=password,
        first_name=data["first_name"],
        last_name=data.get("last_name", ""),
    )
    Profile.objects.create(
        user=user,
        organization=organization,
        level=data["level"],
        language=data.get("language") or organization.default_language,
        timezone=data.get("timezone") or default_timezone(organization),
        must_change_password=True,
    )
    audit.log(admin, "people.created", user, request=request, label=user.get_username(),
              details={"organization": organization.name, "level": data["level"]})
    return user, password


@transaction.atomic
def update_person(admin, user, data, request=None):
    profile = user.profile
    user.username = data["username"]
    user.first_name = data["first_name"]
    user.last_name = data.get("last_name", "")
    user.email = data.get("email", "")
    user.save(update_fields=["username", "first_name", "last_name", "email"])
    profile.organization = data["organization"]
    profile.level = data["level"]
    profile.language = data.get("language") or data["organization"].default_language
    profile.timezone = data.get("timezone") or default_timezone(data["organization"])
    profile.save(update_fields=["organization", "level", "language", "timezone"])
    audit.log(admin, "people.updated", user, request=request, label=user.get_username(),
              details={"organization": profile.organization.name, "level": profile.level})


@transaction.atomic
def reset_password(admin, user, request=None):
    """New temporary password; the person must choose their own at the next sign-in."""
    password = temporary_password()
    user.set_password(password)  # also signs the person out everywhere
    user.save(update_fields=["password"])
    user.profile.must_change_password = True
    user.profile.save(update_fields=["must_change_password"])
    audit.log(admin, "people.password_reset", user, request=request, label=user.get_username())
    return password


@transaction.atomic
def reset_two_factor(admin, user, request=None):
    """For a lost or replaced phone: the person sets up the authenticator app again."""
    profile = user.profile
    profile.totp_enabled = False
    profile.totp_secret = ""
    profile.totp_last_step = 0
    profile.save(update_fields=["totp_enabled", "totp_secret", "totp_last_step"])
    audit.log(admin, "people.2fa_reset", user, request=request, label=user.get_username())


@transaction.atomic
def set_active(admin, user, active, request=None):
    user.is_active = active
    user.save(update_fields=["is_active"])
    audit.log(admin, "people.reactivated" if active else "people.deactivated", user, request=request,
              label=user.get_username())


@transaction.atomic
def complete_setup(data, request=None):
    """First start of the desktop edition: the administrator and the three organisations."""
    User = get_user_model()
    if User.objects.exists():
        raise ValueError("The portal is already set up.")
    admin = User.objects.create_superuser(
        data["username"], email=data.get("email", ""), password=data["password1"], first_name=data["name"]
    )
    organizations = [
        Organization.objects.create(
            name=data["shipper_name"], name_local=data.get("shipper_name_local", ""),
            kind=Organization.Kind.SHIPPER, country="CN", default_language="zh-hans",
        ),
        Organization.objects.create(
            name=data["receiver_name"], name_local=data.get("receiver_name_local", ""),
            kind=Organization.Kind.RECEIVER, country="PK", default_language="en",
        ),
        Organization.objects.create(
            name=data["oversight_name"], kind=Organization.Kind.OVERSIGHT, country="PK", default_language="en",
        ),
    ]
    audit.log(admin, "setup.completed", admin, request=request, label=admin.get_username(),
              details={"organizations": [org.name for org in organizations]})
    return admin


def welcome_message(user, password, url):
    """The text to send the person (WeChat, WhatsApp, e-mail), in their own language."""
    profile = getattr(user, "profile", None)
    language = profile.language if profile else settings.LANGUAGE_CODE
    with translation.override(language):
        lines = [
            _("Hello %(name)s,") % {"name": user.get_full_name() or user.get_username()},
            _("you now have an account on %(portal)s.") % {"portal": settings.PORTAL["NAME"]},
            "",
            _("Address: %(url)s") % {"url": url or "—"},
            _("Username: %(username)s") % {"username": user.get_username()},
            _("Temporary password: %(password)s") % {"password": password},
            "",
            _("You will be asked to choose your own password when you first sign in."),
        ]
    return "\n".join(lines)
