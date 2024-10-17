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

from django.contrib import admin

from .models import Discount, Invoice, InvoiceItem


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = ("description", "percents")
    search_fields = ("description",)


class InvoiceItemAdmin(admin.TabularInline):
    model = InvoiceItem


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    date_hierarchy = "issue_date"
    autocomplete_fields = ("customer",)
    list_display = ("number", "customer", "total_amount")
    search_fields = (
        "customer__name",
        "number",
    )
    inlines = (InvoiceItemAdmin,)
