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

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0046_alter_service_site_url_lock"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameField(
                    model_name="post",
                    old_name="_body_rendered",
                    new_name="body_rendered",
                ),
                migrations.AlterField(
                    model_name="post",
                    name="body_rendered",
                    field=models.TextField(
                        blank=True, db_column="_body_rendered", editable=False
                    ),
                ),
            ],
        ),
        migrations.RemoveField(
            model_name="post",
            name="body_markup_type",
        ),
    ]
