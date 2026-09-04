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

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0005_customerfollowup_over_limit")]

    operations = [
        migrations.AlterField(
            model_name="customerfollowup",
            name="type",
            field=models.PositiveSmallIntegerField(
                choices=[
                    (1, "Manual"),
                    (2, "Duplicate payment"),
                    (3, "Locked site URL"),
                    (4, "Service over limits"),
                    (5, "Expired dedicated service"),
                ],
                db_index=True,
                default=1,
                verbose_name="Follow-up type",
            ),
        ),
        migrations.AddConstraint(
            model_name="customerfollowup",
            constraint=models.UniqueConstraint(
                condition=models.Q(("service__isnull", False), ("type", 5)),
                fields=("service", "type"),
                name="unique_expired_dedicated_followup_per_service",
            ),
        ),
    ]
