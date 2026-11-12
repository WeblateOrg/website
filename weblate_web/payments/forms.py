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

import json
from typing import cast

from django import forms
from django.utils.translation import gettext, gettext_lazy
from vies.types import VIES_OPTIONS

from weblate_web.utils import FOSDEM_ORIGIN

from .models import EU_VAT_RATES, VAT_COUNTRY_CODE_ALIASES, Customer


class CustomerOwnerInvitationForm(forms.Form):
    email = forms.EmailField(label=gettext_lazy("E-mail"))


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = (
            "name",
            "vat",
            "tax",
            "address",
            "address_2",
            "postcode",
            "city",
            "country",
            "email",
            "contact_point",
            "accounting_reference",
        )
        widgets = {"country": forms.Select(attrs={"class": "custom-select"})}

    @property
    def is_fosdem(self):
        return self.instance and self.instance.origin == FOSDEM_ORIGIN

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        country_field = cast("forms.ChoiceField", self.fields["country"])
        country_field.choices = [
            ("", gettext_lazy("Select a country")),
            *Customer._meta.get_field("country").get_choices(include_blank=False),
        ]
        self.fields["vat"].widget.attrs["data-eu-countries"] = ",".join(EU_VAT_RATES)
        self.fields["vat"].widget.attrs["data-eu-vat-patterns"] = json.dumps(
            {
                prefix: options[1].pattern
                for prefix, options in VIES_OPTIONS.items()
                if VAT_COUNTRY_CODE_ALIASES.get(prefix, prefix) in EU_VAT_RATES
            }
        )
        if self.is_fosdem:
            self.fields["address"].required = False
            self.fields["postcode"].required = False
            self.fields["city"].required = False
            self.fields["email"].help_text = gettext("You will receive a receipt here.")
            self.fields["email"].label = gettext("E-mail address")
