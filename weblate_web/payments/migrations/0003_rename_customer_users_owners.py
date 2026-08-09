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

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0002_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="customer",
            old_name="users",
            new_name="owners",
        ),
        migrations.AlterField(
            model_name="customer",
            name="owners",
            field=models.ManyToManyField(
                blank=True,
                help_text=(
                    "Owners have full control over the customer account, including "
                    "billing, services, and other owners."
                ),
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
