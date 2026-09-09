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

from .models import Invoice, InvoiceKind


class InvoiceAdminForm(forms.ModelForm):
    vat_validation_warning: ValidationError | None = None

    class Meta:
        fields = (
            "issue_date",
            "due_date",
            "tax_date",
            "kind",
            "category",
            "customer",
            "customer_reference",
            "customer_note",
            "quote_status",
            "quote_status_note",
            "discount",
            "vat_rate",
            "currency",
            "parent",
            "prepaid",
            "extra",
        )
        model = Invoice

    def clean(self):
        cleaned_data = super().clean() or {}
        customer = cleaned_data.get("customer")
        if (
            self.instance._state.adding  # pylint: disable=protected-access
            and cleaned_data.get("kind") == InvoiceKind.INVOICE
            and customer is not None
        ):
            try:
                self.vat_validation_warning = customer.validate_vat_for_issuance()
            except ValidationError:
                self.add_error(
                    "customer",
                    gettext(
                        "The customer's VAT ID could not be validated, so the "
                        "invoice was not issued. Update the customer data or try "
                        "again later."
                    ),
                )
        return cleaned_data


class CustomerReferenceForm(forms.ModelForm):
    class Meta:
        fields = ["customer_reference", "customer_note"]
        model = Invoice
