from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

TIMEZONES = [
    ("Asia/Shanghai", _("China (Asia/Shanghai, UTC+8)")),
    ("Asia/Karachi", _("Pakistan (Asia/Karachi, UTC+5)")),
    ("Asia/Dubai", _("UAE (Asia/Dubai, UTC+4)")),
    ("Asia/Singapore", _("Singapore (UTC+8)")),
    ("Europe/London", _("United Kingdom (Europe/London)")),
    ("UTC", _("UTC")),
]


class Organization(models.Model):
    """A company taking part in the lane: the Shipper, the Receiver or the Oversight body."""

    class Kind(models.TextChoices):
        SHIPPER = "shipper", _("Shipper")
        RECEIVER = "receiver", _("Receiver")
        OVERSIGHT = "oversight", _("Oversight")

    name = models.CharField(_("name"), max_length=200)
    name_local = models.CharField(_("name in local language"), max_length=200, blank=True)
    kind = models.CharField(_("role"), max_length=20, choices=Kind.choices)
    country = models.CharField(_("country (ISO code)"), max_length=2, blank=True)
    default_language = models.CharField(
        _("default language"), max_length=10, choices=settings.LANGUAGES, default="en"
    )
    wecom_webhook_url = models.URLField(
        _("WeCom group-bot webhook"),
        blank=True,
        help_text=_("Optional. Reminders are also posted to this 企业微信 group robot."),
    )
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["kind", "name"]
        verbose_name = _("organisation")
        verbose_name_plural = _("organisations")

    def __str__(self):
        return self.name


class Profile(models.Model):
    """Links a login to an organisation and sets what the person may do there."""

    class Level(models.TextChoices):
        MANAGER = "manager", _("Manager")
        OPERATOR = "operator", _("Operator")
        VIEWER = "viewer", _("Viewer")

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="members", verbose_name=_("organisation")
    )
    level = models.CharField(
        _("access level"),
        max_length=20,
        choices=Level.choices,
        default=Level.OPERATOR,
        help_text=_(
            "Manager: everything incl. archiving and waiving. Operator: create, edit, upload, scan. Viewer: read only."
        ),
    )
    language = models.CharField(_("language"), max_length=10, choices=settings.LANGUAGES, default="en")
    timezone = models.CharField(_("time zone"), max_length=50, choices=TIMEZONES, default="UTC")
    phone = models.CharField(_("phone"), max_length=40, blank=True)
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_enabled = models.BooleanField(_("two-factor authentication"), default=False)
    totp_last_step = models.BigIntegerField(default=0)
    must_change_password = models.BooleanField(
        _("must choose a new password"),
        default=False,
        help_text=_("Set when an administrator creates the account or resets its password."),
    )

    class Meta:
        verbose_name = _("user profile")
        verbose_name_plural = _("user profiles")

    def __str__(self):
        return f"{self.user.get_username()} ({self.organization})"

    @property
    def party(self):
        """'shipper', 'receiver' or 'oversight'."""
        return self.organization.kind

    @property
    def can_edit(self):
        return self.party in (Organization.Kind.SHIPPER, Organization.Kind.RECEIVER) and self.level in (
            self.Level.MANAGER,
            self.Level.OPERATOR,
        )

    @property
    def is_manager(self):
        return self.can_edit and self.level == self.Level.MANAGER
