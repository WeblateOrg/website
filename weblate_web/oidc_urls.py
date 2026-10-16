# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from django.db import transaction
from django.urls import path
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView
from social_django import views

app_name = "social"

urlpatterns = [
    path(
        "",
        never_cache(TemplateView.as_view(template_name="oidc.html")),
        name="login",
    ),
    path("login/<str:backend>/", views.auth, name="begin"),
    path(
        "complete/<str:backend>/", transaction.atomic(views.complete), name="complete"
    ),
]
