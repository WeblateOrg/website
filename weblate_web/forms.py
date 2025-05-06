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

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext

from weblate_web.models import REWARD_LEVELS, REWARDS, Donation, Service
from weblate_web.payments.backends import list_backends
from weblate_web.payments.models import RECURRENCE_CHOICES


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
        model = Donation
        fields = ("link_text",)


class EditLinkForm(forms.ModelForm):
    class Meta:
        model = Donation
        fields = ("link_text", "link_url")


class EditImageForm(forms.ModelForm):
    class Meta:
        model = Donation
        fields = ("link_text", "link_url", "link_image")


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


class AgreementForm(forms.Form):
    consent = forms.BooleanField(label="I consent to the agreement", required=True)
