from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.admin.sites import AdminSite
from django.contrib.admin.views.autocomplete import AutocompleteJsonView
from django.contrib.auth.models import User

if TYPE_CHECKING:
    from django.db.models import Model


class UserAutocompleteJsonView(AutocompleteJsonView):
    def serialize_result(self, obj: Model, to_field_name: str) -> dict[str, str]:
        result = super().serialize_result(obj, to_field_name)  # type: ignore[misc]
        if isinstance(obj, User):
            result["text"] = f"{obj.first_name} {obj.last_name} <{obj.email}>"
        return result


class CustomAdminSite(AdminSite):
    def autocomplete_view(self, request):
        return UserAutocompleteJsonView.as_view(admin_site=self)(request)


custom_admin_site = CustomAdminSite()
