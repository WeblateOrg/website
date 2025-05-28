# Register your models here.
from django.contrib import admin

from .models import Interaction


@admin.register(Interaction)
class InteractionAdmin(admin.ModelAdmin):
    list_display = ("summary", "timestamp", "origin", "user")
    search_fields = ("custumer__name",)
    date_hierarchy = "timestamp"
    autocomplete_fields = ("customer",)
