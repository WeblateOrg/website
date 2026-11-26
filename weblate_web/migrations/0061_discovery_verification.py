# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F


def keep_existing_listings(apps, schema_editor) -> None:
    service = apps.get_model("weblate_web", "Service")
    service.objects.using(schema_editor.connection.alias).filter(
        discoverable=True
    ).update(discovery_verified_url=F("site_url"))


class Migration(migrations.Migration):
    dependencies = [("weblate_web", "0060_hosted_administrator_owners")]

    operations = [
        migrations.AddField(
            model_name="service",
            name="discovery_verified_url",
            field=models.URLField(blank=True, default="", editable=False),
        ),
        migrations.CreateModel(
            name="DiscoveryVerification",
            fields=[
                (
                    "service",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        serialize=False,
                        to="weblate_web.service",
                    ),
                ),
                (
                    "report",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="weblate_web.report",
                    ),
                ),
            ],
        ),
        migrations.RunPython(keep_existing_listings, migrations.RunPython.noop),
    ]
