import segno
from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from core import translation as mt
from core.i18n import current_lang
from core.ids import short_code

register = template.Library()

PARTY_LABELS = {"shipper": _("Shipper"), "receiver": _("Receiver"), "oversight": _("Oversight"), "any": _("Either party")}


@register.simple_tag
def qr_svg(data, scale=4, border=2):
    """Inline SVG QR code (error correction M: survives scuffed labels)."""
    code = segno.make(str(data), error="m", micro=False)
    return mark_safe(code.svg_inline(scale=scale, border=border, omitsize=True, svgclass="qr"))


@register.filter
def short(code):
    return short_code(str(code))


@register.filter
def localname(obj):
    """ItemType / DocumentType name in the reader's language (falls back to English)."""
    if obj is None:
        return ""
    return obj.name(current_lang())


@register.filter
def party(value):
    return PARTY_LABELS.get(str(value), value)


def when_text(value):
    """Unambiguous date-time in the reader's time zone: 2026-09-29 14:05 PKT (plain text)."""
    if not value:
        return ""
    local = timezone.localtime(value)
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()}"


def day_text(value):
    if not value:
        return ""
    if hasattr(value, "hour"):
        value = timezone.localtime(value).date()
    return f"{value:%Y-%m-%d}"


# Dates are wrapped in an LTR-isolated <bdi>: next to Urdu (Arabic-script) text the
# Unicode bidi algorithm would otherwise display 2026-09-28 as 28-09-2026.
@register.filter
def when(value):
    text = when_text(value)
    return format_html('<bdi dir="ltr" class="dt">{}</bdi>', text) if text else ""


@register.filter
def day(value):
    text = day_text(value)
    return format_html('<bdi dir="ltr" class="dt">{}</bdi>', text) if text else ""


@register.filter
def percent(part, whole):
    try:
        return int(round(100 * float(part) / float(whole))) if whole else 0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0


@register.simple_tag
def free_text(text, source_lang):
    """
    Free text in the reader's language when a machine translation exists.
    Returns {"text", "original", "translated", "source_lang"}.
    """
    target = current_lang()
    translation = mt.lookup(text, source_lang, target) if text else None
    if translation is None:
        return {"text": text, "original": text, "translated": False, "source_lang": source_lang}
    return {"text": translation.text, "original": text, "translated": True, "source_lang": source_lang}


LANG_NAMES = {"en": "English", "zh-hans": "中文", "ur": "اردو"}


@register.filter
def lang_name(code):
    return LANG_NAMES.get(code, code)


ACTION_LABELS = {
    "consignment.created": _("created the consignment"),
    "consignment.updated": _("updated consignment details"),
    "consignment.status": _("recorded a milestone"),
    "packing.units_created": _("added packaging units"),
    "packing.items_added": _("registered items"),
    "packing.moved": _("moved"),
    "packing.unit_updated": _("updated a unit"),
    "packing.item_updated": _("updated an item"),
    "receiving.received": _("recorded receipt"),
    "document.uploaded": _("uploaded a document"),
    "document.downloaded": _("downloaded a document"),
    "document.archived": _("archived a document"),
    "document.required": _("added a required document"),
    "document.waived": _("waived a document"),
    "document.rejected_malware": _("upload rejected by the virus scanner"),
    "thread.created": _("raised a remark or query"),
    "thread.replied": _("replied"),
    "thread.state": _("changed the state of a query"),
    "catalog.saved": _("updated the item master"),
    "account.preferences": _("updated preferences"),
    "auth.login": _("signed in"),
    "auth.login_failed": _("failed sign-in attempt"),
    "auth.logout": _("signed out"),
    "auth.2fa_enabled": _("turned on two-factor authentication"),
    "auth.2fa_disabled": _("turned off two-factor authentication"),
    "auth.password_changed": _("changed password"),
    "setup.completed": _("set up the portal"),
    "people.created": _("created an account"),
    "people.updated": _("updated an account"),
    "people.password_reset": _("reset a password"),
    "people.2fa_reset": _("reset two-factor authentication"),
    "people.deactivated": _("deactivated an account"),
    "people.reactivated": _("reactivated an account"),
    "settings.computer": _("changed the settings of the portal computer"),
    "settings.backup": _("made a backup"),
}


@register.filter
def action_label(action):
    return ACTION_LABELS.get(action, action)


@register.simple_tag(takes_context=True)
def nav_active(context, names):
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    return "active" if match is not None and match.url_name in names.split() else ""


@register.simple_tag
def icon(name, css=""):
    return format_html('<svg class="icon {}" aria-hidden="true"><use href="#i-{}"></use></svg>', css, name)
