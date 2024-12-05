from django.contrib.admin.apps import AdminConfig


class CustomAdminConfig(AdminConfig):
    default_site = "weblate_web.admin_site.CustomAdminSite"
