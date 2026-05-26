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
from django.utils.translation import gettext_lazy

from weblate_web.models import Service


class RefundConfirmationForm(forms.Form):
    description = forms.CharField(
        label=gettext_lazy("Refund description"),
        required=False,
        max_length=200,
        help_text=gettext_lazy("Optional note describing how the refund was done."),
        widget=forms.TextInput(),
    )


class ManualInteractionForm(forms.Form):
    note = forms.CharField(
        label=gettext_lazy("Note"),
        widget=forms.Textarea(attrs={"rows": 4}),
    )


class CustomerUserForm(forms.Form):
    email = forms.EmailField(label=gettext_lazy("E-mail"))
    full_name = forms.CharField(label=gettext_lazy("Full name"), max_length=150)


class ServiceMaintenanceWindowForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ("maintenance_window",)
