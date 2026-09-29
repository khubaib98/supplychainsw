from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from accounts.models import TIMEZONES


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
