from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import TIMEZONES, Organization, Profile
from core.desktop import SettingsError, normalize_public_url, normalize_tunnel_token


class OneTimeCodeForm(forms.Form):
    code = forms.CharField(
        label=_("6-digit code from your authenticator app"),
        max_length=8,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True}),
    )


class PreferencesForm(forms.Form):
    language = forms.ChoiceField(label=_("Language"), choices=settings.LANGUAGES)
    timezone = forms.ChoiceField(label=_("Time zone"), choices=TIMEZONES)
    email = forms.EmailField(label=_("E-mail for reminders"), required=False)
    phone = forms.CharField(label=_("Phone"), max_length=40, required=False)


def _username_field():
    return forms.CharField(
        label=_("Username"),
        max_length=150,
        validators=[UnicodeUsernameValidator()],
        help_text=_("Used to sign in, for example li.wei or ahmed.raza."),
        widget=forms.TextInput(attrs={"autocomplete": "off", "autocapitalize": "none", "spellcheck": "false"}),
    )


class SetupForm(forms.Form):
    """First start of the desktop edition: who looks after the portal, and the three organisations."""

    name = forms.CharField(label=_("Your name"), max_length=150)
    username = _username_field()
    email = forms.EmailField(label=_("E-mail for reminders"), required=False)
    password1 = forms.CharField(
        label=_("Password"), strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )
    password2 = forms.CharField(
        label=_("Password again"), strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )
    shipper_name = forms.CharField(label=_("Shipper (company in China)"), max_length=200)
    shipper_name_local = forms.CharField(label=_("Shipper's name in Chinese"), max_length=200, required=False)
    receiver_name = forms.CharField(label=_("Receiver (company in Pakistan)"), max_length=200)
    receiver_name_local = forms.CharField(label=_("Receiver's name in Urdu"), max_length=200, required=False)
    oversight_name = forms.CharField(label=_("Oversight (top management)"), max_length=200, initial=_("Top management"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].initial = "admin"

    def password_help(self):
        return password_validation.password_validators_help_text_html()

    def clean(self):
        data = super().clean()
        first, second = data.get("password1"), data.get("password2")
        if first and second and first != second:
            self.add_error("password2", _("The two passwords are not the same."))
        elif first:
            candidate = get_user_model()(
                username=data.get("username", ""), email=data.get("email", ""), first_name=data.get("name", "")
            )
            try:
                password_validation.validate_password(first, candidate)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return data


class OrganizationChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, organization):
        return f"{organization.name} ({organization.get_kind_display()})"


class PersonForm(forms.Form):
    first_name = forms.CharField(label=_("First name"), max_length=150)
    last_name = forms.CharField(label=_("Last name"), max_length=150, required=False)
    username = _username_field()
    email = forms.EmailField(label=_("E-mail for reminders"), required=False)
    organization = OrganizationChoiceField(label=_("Organisation"), queryset=Organization.objects.filter(is_active=True))
    level = forms.ChoiceField(
        label=_("Access level"),
        choices=Profile.Level.choices,
        initial=Profile.Level.OPERATOR,
        help_text=Profile._meta.get_field("level").help_text,
    )
    language = forms.ChoiceField(
        label=_("Language"), required=False, choices=[("", _("The organisation's language"))] + list(settings.LANGUAGES)
    )
    timezone = forms.ChoiceField(
        label=_("Time zone"), required=False, choices=[("", _("From the organisation's country"))] + TIMEZONES
    )

    def __init__(self, *args, person=None, **kwargs):
        self.person = person
        super().__init__(*args, **kwargs)
        if person is not None:
            # Keep an existing person's organisation selectable even if it was deactivated.
            self.fields["organization"].queryset = Organization.objects.filter(is_active=True) | Organization.objects.filter(
                pk=person.profile.organization_id
            )

    def clean_username(self):
        username = self.cleaned_data["username"]
        taken = get_user_model().objects.filter(username__iexact=username)
        if self.person is not None:
            taken = taken.exclude(pk=self.person.pk)
        if taken.exists():
            raise ValidationError(_("This username is already taken."))
        return username


class ComputerSettingsForm(forms.Form):
    """The desktop settings an administrator may change from the browser."""

    network_access = forms.BooleanField(
        label=_("Other computers in the office network may open the portal"), required=False
    )
    tunnel = forms.ChoiceField(
        label=_("Share over the internet (Cloudflare Tunnel)"),
        choices=[
            ("off", _("No")),
            ("temporary", _("Temporary address (no account needed)")),
            ("token", _("Own address (Cloudflare account)")),
        ],
        initial="off",
        required=False,  # like the tick boxes: not sent means off
        help_text=_("A temporary address works at once but changes every time the portal starts. "
                    "An own address needs a Cloudflare account and a domain."),
    )
    tunnel_token = forms.CharField(
        label=_("Tunnel token (own address)"),
        required=False,
        help_text=_("From the Cloudflare dashboard, when you create the tunnel. You may paste the whole "
                    "install command. Leave empty to keep the saved token."),
        widget=forms.PasswordInput(attrs={"autocomplete": "off", "dir": "ltr", "spellcheck": "false"}),
    )
    public_url = forms.CharField(
        label=_("Public address (HTTPS tunnel)"),
        required=False,
        help_text=_("Your own address, e.g. https://portal.example.com. Not needed for a temporary address."),
        widget=forms.TextInput(attrs={"placeholder": "https://portal.example.com", "dir": "ltr"}),
    )
    require_two_factor = forms.BooleanField(
        label=_("Everyone must use an authenticator app to sign in"), required=False
    )

    def __init__(self, *args, has_token=False, **kwargs):
        self.has_token = has_token
        super().__init__(*args, **kwargs)
        if has_token:
            self.fields["tunnel_token"].help_text = _("A token is saved. Paste a new one only to replace it.")

    def clean_tunnel(self):
        return self.cleaned_data["tunnel"] or "off"

    def clean_tunnel_token(self):
        try:
            return normalize_tunnel_token(self.cleaned_data["tunnel_token"])
        except SettingsError as exc:
            raise ValidationError(_("This is not a tunnel token. In the Cloudflare dashboard, copy the token "
                                    "(it starts with eyJ) or the whole install command.")) from exc

    def clean_public_url(self):
        try:
            return normalize_public_url(self.cleaned_data["public_url"])
        except SettingsError as exc:
            raise ValidationError(_("Enter an address like https://portal.example.com (without a path).")) from exc

    def clean(self):
        data = super().clean()
        if data.get("tunnel") == "token":
            if "tunnel_token" in data and not (data["tunnel_token"] or self.has_token):
                self.add_error("tunnel_token", _("Paste the tunnel token from the Cloudflare dashboard."))
            if "public_url" in data and not data["public_url"]:
                self.add_error("public_url", _("Enter the address you gave the tunnel in Cloudflare."))
        return data

    def changes(self):
        """The new settings; an empty token field keeps the saved token."""
        data = dict(self.cleaned_data)
        if not data["tunnel_token"]:
            del data["tunnel_token"]
        return data
