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

from weblate_web.models import (
    Image,
    Package,
    Post,
    Project,
    SamlIdentity,
    Service,
    Subscription,
    SubscriptionPastPayment,
)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "url", "web")
    autocomplete_fields = ("service",)


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = [
        "site_title",
        "site_url",
        "note",
        "maintenance_window",
        "projects_limit",
        "languages_limit",
        "source_strings_limit",
        "hosted_words_limit",
        "hosted_strings_limit",
        "status",
        "user_emails",
        "expires",
        "discoverable",
        "backup_size",
    ]
    list_filter = ("kind", "status", "discoverable")
    search_fields = (
        "customer__email",
        "customer__users__email",
        "report__site_url",
        "report__site_title",
        "customer__name",
        "site_url",
        "note",
        "maintenance_window",
        "donation_link_text",
        "donation_link_url",
    )
    date_hierarchy = "created"
    autocomplete_fields = ("customer",)
    readonly_fields = (
        "backup_box",
        "backup_subaccount",
        "backup_directory",
        "backup_size",
        "backup_timestamp",
        "backup_removed",
        "site_version",
    )


class SubscriptionPastPaymentInline(admin.TabularInline):
    model = SubscriptionPastPayment
    autocomplete_fields = ("payment",)
    extra = 0


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "service__site_url",
        "service__customer__name",
        "package",
        "created",
        "expires",
        "price",
    )
    search_fields = (
        "service__customer__email",
        "service__customer__name",
        "service__customer__users__email",
        "service__report__site_url",
        "service__report__site_title",
        "service__site_url",
        "service__note",
    )
    list_filter = ("enabled",)
    autocomplete_fields = ("service", "payment")
    inlines = (SubscriptionPastPaymentInline,)


@admin.register(SubscriptionPastPayment)
class SubscriptionPastPaymentAdmin(admin.ModelAdmin):
    list_display = ("subscription", "payment")
    autocomplete_fields = ("subscription", "payment")


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(SamlIdentity)
class SamlIdentityAdmin(admin.ModelAdmin):
    list_display = ("provider", "external_id", "user", "last_seen")
    search_fields = ("provider", "external_id", "user__username", "user__email")
    autocomplete_fields = ("user",)


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ["title", "timestamp", "topic", "image", "milestone"]
    list_filter = [("author", admin.RelatedOnlyFieldListFilter), "topic", "milestone"]
    prepopulated_fields = {"slug": ("title",)}
    search_fields = ["title", "slug"]
    ordering = ("-timestamp",)
    date_hierarchy = "timestamp"

    def save_model(self, request, obj, form, change) -> None:
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
        "limit_hosted_strings",
        "limit_projects",
        "limit_languages",
        "limit_source_strings",
    ]
    list_filter = ("category", "hidden")
    ordering = ("verbose",)
    search_fields = ("verbose", "name")
