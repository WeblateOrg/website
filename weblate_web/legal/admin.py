from __future__ import annotations

from django.contrib import admin

from .models import Agreement


@admin.register(Agreement)
class AgreementAdmin(admin.ModelAdmin):
    list_display = ("customer", "kind", "signed")
    date_hierarchy = "signed"
    list_filter = ("kind",)
    search_fields = ("customer__name",)
    autocomplete_fields = ("customer",)
