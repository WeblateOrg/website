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

from django import forms
from django.utils.translation import gettext
from vies.forms.fields import VATINField
from vies.forms.widgets import VATINWidget
from vies.types import VIES_COUNTRY_CHOICES

from weblate_web.utils import FOSDEM_ORIGIN

from .models import Customer


class BootstrapVATINWidget(VATINWidget):
    template_name = "widgets/vatin.html"

    def __init__(self, choices=VIES_COUNTRY_CHOICES, attrs=None) -> None:
        select_attrs = {"class": "form-control custom-select"}
        input_attrs = {"class": "form-control"}
        if attrs is not None:
            select_attrs.update(attrs)
            input_attrs.update(attrs)
        widgets = (
            forms.Select(choices=choices, attrs=select_attrs),
            forms.TextInput(attrs=input_attrs),
        )
        # We intentionally skip VATINWidget constructor
        # pylint: disable=E1003
        super(VATINWidget, self).__init__(widgets, attrs)


class BootstrapVATINField(VATINField):
    # This serves as workaround for
    # https://github.com/codingjoe/django-vies/pull/157
    widget = BootstrapVATINWidget


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
        )
        field_classes = {"vat": BootstrapVATINField}
        widgets = {"country": forms.Select(attrs={"class": "custom-select"})}

    @property
    def is_fosdem(self):
        return self.instance and self.instance.origin == FOSDEM_ORIGIN

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if self.is_fosdem:
            self.fields["address"].required = False
            self.fields["postcode"].required = False
            self.fields["city"].required = False
            self.fields["email"].help_text = gettext("You will receive a receipt here.")
            self.fields["email"].label = gettext("E-mail address")
