#
# Copyright © Michal Čihař <michal@weblate.org>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from __future__ import annotations

from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext, gettext_lazy

from weblate_web.invoices.models import Currency, InvoiceKind
from weblate_web.models import (
    REWARD_LEVELS,
    REWARDS,
    Package,
    Service,
    normalize_site_url_for_lock,
)
from weblate_web.payments.backends import list_backends
from weblate_web.payments.models import RECURRENCE_CHOICES


class NewSubscriptionForm(forms.Form):
    kind = forms.TypedChoiceField(
        choices=(
            (InvoiceKind.QUOTE, gettext_lazy("Quote")),
            (InvoiceKind.INVOICE, gettext_lazy("Invoice")),
        ),
        initial=InvoiceKind.QUOTE,
        coerce=InvoiceKind.from_str,
        widget=forms.RadioSelect,
    )
    package = forms.ModelChoiceField(Package.objects.all())
    currency = forms.TypedChoiceField(
        choices=Currency, initial=Currency.EUR, coerce=Currency.from_str
    )
    customer_reference = forms.CharField(required=False)
    customer_note = forms.CharField(required=False, widget=forms.Textarea)
    confirm_invoice = forms.BooleanField(required=False, widget=forms.HiddenInput)
    skip_intro = forms.BooleanField(
        required=False,
        label="Skip sending introduction/creating Zammad ticket upon purchase",
    )

    def clean(self):
        super().clean()
        is_invoice = self.cleaned_data.get("kind") == InvoiceKind.INVOICE
        if is_invoice and not self.cleaned_data.get("confirm_invoice"):
            raise ValidationError(
                gettext_lazy("Please confirm that you want to issue a final invoice.")
            )
        return self.cleaned_data


class MethodForm(forms.Form):
    method = forms.ChoiceField(
        choices=[],
        required=True,
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["method"].choices = [  # type: ignore[attr-defined]
            (backend.name, backend.verbose) for backend in list_backends()
        ]


class DonateForm(forms.Form):
    recurring = forms.ChoiceField(
        choices=RECURRENCE_CHOICES,
        initial="",
        required=False,
    )
    amount = forms.IntegerField(
        min_value=5,
        initial=10,
    )
    reward = forms.TypedChoiceField(
        choices=REWARDS, initial=0, required=False, coerce=int
    )

    def clean_reward(self):
        if "reward" not in self.cleaned_data:
            self.cleaned_data["reward"] = 0
        elif (
            self.cleaned_data.get("amount", 0)
            < REWARD_LEVELS[self.cleaned_data["reward"]]
        ):
            raise ValidationError(gettext("Insufficient donation for selected reward!"))

        return self.cleaned_data["reward"]


class EditNameForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ("donation_link_text",)


class EditLinkForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ("donation_link_text", "donation_link_url")


class EditImageForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ("donation_link_text", "donation_link_url", "donation_link_image")


class EditDiscoveryForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ("discover_text", "discover_image")
        widgets = {"discover_text": forms.Textarea}


class AddDiscoveryForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ("site_url", "discover_text", "discover_image")
        widgets = {"discover_text": forms.Textarea}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["site_url"].required = True
        self.fields["discover_text"].required = True


DISCOVERY_CALLBACK_PATH = "/manage/discovery/callback/"


def normalize_discovery_url(url: str, message: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ValidationError(message) from error

    invalid_url = parts.scheme not in {"http", "https"} or not parts.netloc
    has_delimiter = "?" in url or "#" in url
    has_extra_parts = any((parts.username, parts.password, parts.query, parts.fragment))
    if invalid_url or has_delimiter or has_extra_parts or port == 0:
        raise ValidationError(message)

    return normalize_site_url_for_lock(url)


def get_discovery_callback_url(site_url: str) -> str:
    return f"{site_url}{DISCOVERY_CALLBACK_PATH}"


class DiscoveryRegistrationForm(AddDiscoveryForm):
    state = forms.CharField(max_length=400, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["site_url"].widget.attrs["readonly"] = "readonly"

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data is None:
            return {}
        site_url = cleaned_data.get("site_url")
        if not site_url:
            return cleaned_data

        site_url = normalize_discovery_url(site_url, gettext("Invalid server URL."))
        cleaned_data["site_url"] = site_url

        if not settings.DEBUG and urlsplit(site_url).scheme != "https":
            raise ValidationError(gettext("Server URL must use HTTPS."))

        return cleaned_data


class AgreementForm(forms.Form):
    consent = forms.BooleanField(label="I consent to the agreement", required=True)
