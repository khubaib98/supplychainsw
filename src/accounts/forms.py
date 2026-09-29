from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import TIMEZONES, Organization, Profile
from core.desktop import SettingsError, normalize_public_url


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

    ADMIN_FIELDS = ("name", "username", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].initial = "admin"
        self.fields["password1"].help_text = password_validation.password_validators_help_text_html()

    def admin_fields(self):
        return [self[name] for name in self.ADMIN_FIELDS]

    def organization_fields(self):
        return [self[name] for name in self.fields if name not in self.ADMIN_FIELDS]

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
    public_url = forms.CharField(
        label=_("Public address (HTTPS tunnel)"),
        required=False,
        help_text=_("Only if the portal is published through a tunnel, e.g. https://portal.example.com"),
        widget=forms.TextInput(attrs={"placeholder": "https://portal.example.com", "dir": "ltr"}),
    )
    require_two_factor = forms.BooleanField(
        label=_("Everyone must use an authenticator app to sign in"), required=False
    )

    def clean_public_url(self):
        try:
            return normalize_public_url(self.cleaned_data["public_url"])
        except SettingsError as exc:
            raise ValidationError(_("Enter an address like https://portal.example.com (without a path).")) from exc
