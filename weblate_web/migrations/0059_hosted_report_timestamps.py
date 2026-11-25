# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0058_hosted_service_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="hosted_synced_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="report",
            name="timestamp",
            field=models.DateTimeField(
                default=django.utils.timezone.now, editable=False
            ),
        ),
    ]
