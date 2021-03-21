#
# Copyright © 2012–2021 Michal Čihař <michal@cihar.com>
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

from django import forms

from payments.backends import list_backends
from payments.models import RECURRENCE_CHOICES
from weblate_web.models import REWARDS, Donation, Service


class MethodForm(forms.Form):
    method = forms.ChoiceField(
        choices=[],
        required=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].choices = [
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
    reward = forms.ChoiceField(choices=REWARDS, initial=0, required=False)


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
