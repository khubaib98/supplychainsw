from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def now():
    # Looked up at call time (not bound at import) so the demo seeder can back-date history.
    return timezone.now()


class Sequence(models.Model):
    """Named counters used to hand out consignment, unit and item numbers."""

    key = models.CharField(max_length=120, unique=True)
    value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.key}={self.value}"


class AuditHead(models.Model):
    """Single row holding the tip of the audit hash chain (also serialises writers)."""

    last_hash = models.CharField(max_length=64, default="0" * 64)
    count = models.PositiveBigIntegerField(default=0)


class AuditLog(models.Model):
    """
    Append-only record of every change. Each row carries the hash of the
    previous row, so editing or deleting history breaks the chain and is
    detected by `manage.py verify_audit`.
    """

    at = models.DateTimeField(_("time"), default=now, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    username = models.CharField(_("user"), max_length=150, blank=True)
    organization = models.CharField(_("organisation"), max_length=200, blank=True)
    action = models.CharField(_("action"), max_length=60)
    target_type = models.CharField(_("object type"), max_length=60, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    target_label = models.CharField(_("object"), max_length=200, blank=True)
    consignment_code = models.CharField(_("consignment"), max_length=40, blank=True, db_index=True)
    details = models.JSONField(_("details"), default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    prev_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = _("audit record")
        verbose_name_plural = _("audit log")

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.username} {self.action} {self.target_label}"


class Notification(models.Model):
    """In-app notification. Text is rendered at display time in the reader's language."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=40)
    params = models.JSONField(default=dict, blank=True)
    consignment = models.ForeignKey(
        "logistics.Consignment", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    url = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=now, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def text(self):
        from core.notify import render_notification

        return render_notification(self.kind, self.params)


class TextTranslation(models.Model):
    """Machine translation cache for free text (keyed by a hash of the source)."""

    source_hash = models.CharField(max_length=64, db_index=True)
    source_lang = models.CharField(max_length=10)
    target_lang = models.CharField(max_length=10)
    text = models.TextField()
    provider = models.CharField(max_length=40)
    created_at = models.DateTimeField(default=now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_hash", "target_lang"], name="uniq_translation_target"),
        ]
