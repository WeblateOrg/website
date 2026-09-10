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

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0001_squashed_0052_discoveryactivation"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="activity_drop_state",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.CreateModel(
            name="ServiceActivity",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("month", models.DateField()),
                ("changes", models.PositiveBigIntegerField()),
                (
                    "service",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activity",
                        to="weblate_web.service",
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "Service activity",
                "ordering": ["month"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("service", "month"),
                        name="unique_service_activity_month",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("month__day", 1)),
                        name="service_activity_month_start",
                    ),
                ],
            },
        ),
    ]
