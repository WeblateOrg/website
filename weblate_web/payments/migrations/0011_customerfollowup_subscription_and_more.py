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

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0010_alter_customer_vat"),
        ("weblate_web", "0057_subscription_renewal_follow_up_dismissed_for"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerfollowup",
            name="subscription",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="renewal_follow_up",
                to="weblate_web.subscription",
            ),
        ),
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
                    (6, "Activity drop"),
                    (7, "Service renewal"),
                ],
                db_index=True,
                default=1,
                verbose_name="Follow-up type",
            ),
        ),
    ]
