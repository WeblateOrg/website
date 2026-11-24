from __future__ import annotations

from django.apps import AppConfig

from .sentry import initialize_sentry


class WebsiteConfig(AppConfig):
    name = "weblate_web"

    def ready(self) -> None:
        initialize_sentry()
