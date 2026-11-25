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

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0011_customerfollowup_subscription_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ThePayNotification",
            fields=[
                ("payment_id", models.UUIDField(primary_key=True, serialize=False)),
                ("revision", models.UUIDField(default=uuid.uuid4)),
                ("ready_at", models.DateTimeField(db_index=True)),
                ("legacy_language", models.CharField(blank=True, max_length=32)),
            ],
        ),
    ]
