"""
Notifications: in-app (always), e-mail (when the user has an address) and an
optional WeCom (企业微信) group-bot webhook per organisation.

Why WeCom: Gmail and WhatsApp are blocked in mainland China, so a Chinese
shipper's team is most reliably reached in WeCom/WeChat. For the Pakistani
side e-mail (and later WhatsApp Business) works.

Messages are stored as a kind + parameters and rendered in each reader's own
language at the moment they are shown or sent.
"""
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.utils import translation
from django.utils.translation import gettext_lazy as _

from core.models import Notification

logger = logging.getLogger(__name__)

TEMPLATES = {
    "doc_due": _("%(doc)s for %(code)s is due. Please upload it."),
    "doc_overdue": _("Reminder: %(doc)s for %(code)s is %(days)s day(s) overdue."),
    "doc_escalated": _("Escalation: %(doc)s for %(code)s is %(days)s day(s) overdue. Responsible: %(org)s."),
    "doc_uploaded": _("%(user)s uploaded %(doc)s (version %(version)s) for %(code)s."),
    "thread_new": _("%(user)s raised a %(category)s on %(code)s: %(subject)s"),
    "thread_reply": _("%(user)s replied on %(code)s: %(subject)s"),
    "thread_state": _("%(user)s marked “%(subject)s” on %(code)s as %(state)s."),
    "status": _("%(code)s is now: %(status)s."),
}


class _Params(dict):
    def __missing__(self, key):
        return ""


def render_notification(kind, params):
    """Render a notification in the currently active language."""
    from logistics.models import Consignment, DocumentType, Thread

    values = _Params(params or {})
    if values.get("doc_type"):
        doc_type = DocumentType.objects.filter(code=values["doc_type"]).first()
        values["doc"] = doc_type.name() if doc_type else values["doc_type"]
    if values.get("status"):
        values["status"] = dict(Consignment.Status.choices).get(values["status"], values["status"])
    if values.get("category"):
        values["category"] = dict(Thread.Category.choices).get(values["category"], values["category"])
    if values.get("state"):
        values["state"] = dict(Thread.State.choices).get(values["state"], values["state"])
    template = TEMPLATES.get(kind)
    if template is None:
        return kind
    return str(template) % values


def _user_language(user):
    profile = getattr(user, "profile", None)
    return profile.language if profile else settings.LANGUAGE_CODE


def absolute(url):
    if not url or url.startswith("http"):
        return url
    return settings.PORTAL["BASE_URL"].rstrip("/") + url


def notify(users, kind, params, *, consignment=None, url="", email=True, organizations=()):
    """Create notifications for `users` and fan out to e-mail / WeCom."""
    users = [user for user in {user.pk: user for user in users if user is not None and user.is_active}.values()]
    Notification.objects.bulk_create(
        [Notification(user=user, kind=kind, params=params, consignment=consignment, url=url) for user in users]
    )
    if email:
        _send_emails(users, kind, params, url)
    for organization in organizations:
        if organization.wecom_webhook_url:
            with translation.override(organization.default_language):
                text = f"{settings.PORTAL['NAME']}\n{render_notification(kind, params)}\n{absolute(url)}"
            post_wecom(organization.wecom_webhook_url, text)


def _send_emails(users, kind, params, url):
    messages = []
    for user in users:
        if not user.email:
            continue
        with translation.override(_user_language(user)):
            text = render_notification(kind, params)
            subject = f"[{settings.PORTAL['NAME']}] {params.get('code', '')} {text}"[:180]
            body = f"{text}\n\n{absolute(url)}\n"
        messages.append(EmailMessage(subject=subject, body=body, to=[user.email]))
    if not messages:
        return
    try:
        get_connection().send_messages(messages)
    except Exception:  # noqa: BLE001 - a mail outage must never break the portal
        logger.exception("Sending %d notification e-mail(s) failed", len(messages))


def post_wecom(webhook_url, text):
    """Send a plain-text message to a WeCom group robot (企业微信群机器人)."""
    payload = json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("WeCom webhook failed: %s", exc)
