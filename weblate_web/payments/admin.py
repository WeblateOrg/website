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

from django.contrib import admin

from .models import Customer, Payment


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "country", "vat", "origin")
    list_filter = ("country", "origin")
    search_fields = ("name", "email", "users__email", "end_client")
    ordering = ("name",)
    autocomplete_fields = ("users",)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "description",
        "amount",
        "customer",
        "state",
        "backend",
        "repeat",
        "start",
        "end",
        "created",
        "uuid",
    )
    list_filter = ("state", "backend")
    search_fields = (
        "description",
        "customer__name",
        "customer__email",
        "invoice",
        "draft_invoice__number",
        "paid_invoice__number",
    )
    readonly_fields = ("created",)
    date_hierarchy = "created"
    ordering = ("-created",)
    autocomplete_fields = ("customer", "draft_invoice", "paid_invoice")
