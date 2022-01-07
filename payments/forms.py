#
# Copyright © 2012–2022 Michal Čihař <michal@cihar.com>
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
from vies.forms.fields import VATINField
from vies.forms.widgets import VATINWidget
from vies.types import VIES_COUNTRY_CHOICES

from .models import Customer


class BootstrapVATINWidget(VATINWidget):
    template_name = "widgets/vatin.html"

    def __init__(self, choices=VIES_COUNTRY_CHOICES, attrs=None):
        select_attrs = {"class": "form-control custom-select"}
        input_attrs = {"class": "form-control"}
        if attrs is not None:
            select_attrs.update(attrs)
            input_attrs.update(attrs)
        widgets = (
            forms.Select(choices=choices, attrs=select_attrs),
            forms.TextInput(attrs=input_attrs),
        )
        # We intentioanlly skip VATINWidget contructor
        # pylint: disable=E1003
        super(VATINWidget, self).__init__(widgets, attrs)


class BootstrapVATINField(VATINField):
    # This serves as workaround for
    # https://github.com/codingjoe/django-vies/pull/157
    widget = BootstrapVATINWidget


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ("vat", "tax", "name", "address", "city", "country")
        field_classes = {"vat": BootstrapVATINField}
        widgets = {"country": forms.Select(attrs={"class": "custom-select"})}
