#
# Copyright © 2012–2021 Michal Čihař <michal@cihar.com>
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


def format_user(obj):
    return "{}: {} {} <{}>".format(
        obj.username, obj.first_name, obj.last_name, obj.email
    )


class DonationAdmin(admin.ModelAdmin):
    list_display = ("user", "reward", "created", "expires", "get_amount")


class ProjectAdmin(admin.TabularInline):
    model = Project


class ServiceAdmin(admin.ModelAdmin):
    list_display = [
        "site_title",
        "site_url",
        "site_version",
        "note",
        "projects_limit",
        "languages_limit",
        "source_strings_limit",
        "status",
        "user_emails",
        "expires",
        "discoverable",
    ]
    list_filter = ("status", "discoverable")
    search_fields = ("users__email", "report__site_url", "report__site_title")
    date_hierarchy = "created"
    filter_horizontal = ("users",)
    inlines = (ProjectAdmin,)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields["users"].label_from_instance = format_user
        return form


class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("service", "package", "created", "expires", "get_amount")


class ImageAdmin(admin.ModelAdmin):
    search_fields = ("name",)


class PostAdmin(admin.ModelAdmin):
    list_display = ["title", "timestamp", "slug", "image"]
    list_filter = [("author", admin.RelatedOnlyFieldListFilter), "topic"]
    prepopulated_fields = {"slug": ("title",)}
    search_fields = ["title", "slug"]
    ordering = ("-timestamp",)
    date_hierarchy = "timestamp"

    def save_model(self, request, obj, form, change):
        if getattr(obj, "author", None) is None:
            obj.author = request.user
        obj.save()


class PackageAdmin(admin.ModelAdmin):
    list_display = [
        "verbose",
        "name",
        "price",
        "limit_projects",
        "limit_languages",
        "limit_source_strings",
    ]


class PastPaymentsAdmin(admin.ModelAdmin):
    list_display = ["subscription", "payment"]


admin.site.register(Image, ImageAdmin)
admin.site.register(Post, PostAdmin)
admin.site.register(Donation, DonationAdmin)
admin.site.register(Subscription, SubscriptionAdmin)
admin.site.register(Service, ServiceAdmin)
admin.site.register(Package, PackageAdmin)
admin.site.register(PastPayments, PastPaymentsAdmin)
