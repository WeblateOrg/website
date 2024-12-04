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

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.urls import reverse
from django.utils.timezone import now

from .models import Discount, Invoice, InvoiceItem, InvoiceKind

if TYPE_CHECKING:
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
    date_hierarchy = "issue_date"
    ordering = ("-issue_date",)
    autocomplete_fields = ("customer", "parent")
    list_display = ("number", "kind", "category", "customer", "total_amount")
    list_filter = ["kind", "category"]
    search_fields = (
        "customer__name",
        "number",
    )
    inlines = (InvoiceItemAdmin,)

    def save_related(
        self, request: HttpRequest, form: Any, formsets: Any, change: Any
    ) -> None:
        super().save_related(
            request=request, form=form, formsets=formsets, change=change
        )
        if form.instance.kind != InvoiceKind.DRAFT:
            form.instance.generate_files()

    def view_on_site(self, obj: Invoice) -> str | None:
        if obj.kind == InvoiceKind.DRAFT:
            return None
        return reverse("invoice-pdf", kwargs={"pk": obj.pk})

    def has_delete_permission(
        self, request: HttpRequest, obj: Invoice | None = None
    ) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Invoice | None = None
    ) -> bool:
        if obj is None:
            return False
        return obj.issue_date.month == now().month

    def get_readonly_fields(
        self, request: HttpRequest, obj: Invoice | None = None
    ) -> list[str]:
        fields = ["number", "prepaid"]
        if obj:
            fields.extend(("kind", "issue_date"))
        return fields
