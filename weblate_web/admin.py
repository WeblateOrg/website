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

from weblate_web.models import (
    Donation,
    Image,
    Package,
    PastPayments,
    Post,
    Project,
    Service,
    Subscription,
)

if TYPE_CHECKING:
    from django.forms import ModelForm
    from django.http import HttpRequest


def format_user(obj):
    return f"{obj.username}: {obj.first_name} {obj.last_name} <{obj.email}>"


@admin.site(Donation)
class DonationAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "reward",
        "created",
        "expires",
        "get_amount",
        "link_text",
        "link_url",
        "active",
    )
    list_filter = [
        "reward",
        "active",
    ]
    autocomplete_fields = ("user", "customer")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "url", "web")
    autocomplete_fields = ("service",)


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = [
        "site_title",
        "site_url",
        "site_version",
        "note",
        "projects_limit",
        "languages_limit",
        "source_strings_limit",
        "hosted_words_limit",
        "hosted_strings_limit",
        "status",
        "user_emails",
        "expires",
        "discoverable",
    ]
    list_filter = ("status", "discoverable")
    search_fields = (
        "users__email",
        "report__site_url",
        "report__site_title",
        "site_url",
        "note",
    )
    date_hierarchy = "created"
    autocomplete_fields = ("users", "customer")
    inlines = (ProjectAdmin,)

    def get_form(
        self,
        request: HttpRequest,
        obj: Any | None = None,
        change: bool = False,
        **kwargs: Any,
    ) -> type[ModelForm[Any]]:
        form = super().get_form(request=request, obj=obj, change=change, **kwargs)
        form.base_fields["users"].label_from_instance = format_user  # type: ignore[attr-defined]
        return form


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("service", "package", "created", "expires", "price")
    search_fields = (
        "service__users__email",
        "service__report__site_url",
        "service__report__site_title",
        "service__site_url",
        "service__note",
    )
    autocomplete_fields = ("service",)


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ["title", "timestamp", "topic", "image", "milestone"]
    list_filter = [("author", admin.RelatedOnlyFieldListFilter), "topic", "milestone"]
    prepopulated_fields = {"slug": ("title",)}
    search_fields = ["title", "slug"]
    ordering = ("-timestamp",)
    date_hierarchy = "timestamp"

    def save_model(self, request, obj, form, change):
        if getattr(obj, "author", None) is None:
            obj.author = request.user
        obj.save()


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = [
        "verbose",
        "name",
        "price",
        "category",
        "limit_projects",
        "limit_languages",
        "limit_source_strings",
        "limit_hosted_words",
    ]
    list_filter = ["category"]


@admin.register(PastPayments)
class PastPaymentsAdmin(admin.ModelAdmin):
    list_display = ["subscription", "payment"]
