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

from typing import TYPE_CHECKING, Any, cast

from django.contrib import admin, messages
from django.utils import timezone
from django.utils.translation import gettext

from .forms import InvoiceAdminForm
from .models import Discount, Invoice, InvoiceItem, InvoiceKind

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http.request import HttpRequest


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = ("description", "percents")
    search_fields = ("description",)


class InvoiceItemAdmin(admin.TabularInline):
    model = InvoiceItem
    min_num = 1
    autocomplete_fields = ("package",)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    form = InvoiceAdminForm
    date_hierarchy = "issue_date"
    ordering = ("-issue_date",)
    autocomplete_fields = ("customer", "parent")
    list_display = ("number", "kind", "category", "customer", "total_amount")
    list_filter = ["kind", "category"]
    search_fields = (
        "customer__name",
        "number",
        "invoiceitem__description",
    )
    inlines = (InvoiceItemAdmin,)

    def save_related(
        self, request: HttpRequest, form: Any, formsets: Any, change: Any
    ) -> None:
        super().save_related(
            request=request, form=form, formsets=formsets, change=change
        )
        invoice: Invoice = form.instance
        if invoice.kind != InvoiceKind.DRAFT:
            # Negative amounts (refunds are automatically prepaid)
            if invoice.total_amount < 0:
                invoice.prepaid = True
                invoice.save(update_fields=["prepaid"])
            invoice.generate_files()
        if form.vat_validation_warning is not None:
            invoice.customer.record_stale_vat_issuance(
                invoice=invoice,
                user=cast("User", request.user),
                warning=form.vat_validation_warning,
            )
            validated = timezone.localtime(invoice.customer.vat_validated).strftime(
                "%Y-%m-%d %H:%M %Z"
            )
            self.message_user(
                request,
                gettext(
                    "VIES is temporarily unavailable. The invoice was issued using "
                    "the VAT validation from %(validated)s."
                )
                % {"validated": validated},
                level=messages.WARNING,
            )

    def view_on_site(self, obj: Invoice) -> str | None:
        return obj.get_download_url()

    def has_delete_permission(
        self, request: HttpRequest, obj: Invoice | None = None
    ) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Invoice | None = None
    ) -> bool:
        if obj is None:
            return False
        return obj.is_editable()

    def get_readonly_fields(
        self, request: HttpRequest, obj: Invoice | None = None
    ) -> list[str]:
        fields = ["number", "prepaid"]
        if obj:
            fields.extend(("kind", "issue_date"))
        return fields
