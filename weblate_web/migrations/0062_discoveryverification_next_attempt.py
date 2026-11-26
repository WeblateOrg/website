# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [("weblate_web", "0061_discovery_verification")]

    operations = [
        migrations.AddField(
            model_name="discoveryverification",
            name="next_attempt",
            field=models.DateTimeField(db_index=True, default=timezone.now),
        ),
    ]
