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
        ("CRM", "0007_alter_interaction_origin"),
    ]

    operations = [
        migrations.AlterField(
            model_name="interaction",
            name="origin",
            field=models.IntegerField(
                choices=[
                    (1, "Outbound e-mail"),
                    (2, "Merged customer"),
                    (3, "Attachment exchanged in Zammad"),
                    (4, "VIES validation"),
                    (5, "Manual payment"),
                    (6, "Maintenance window"),
                ],
                verbose_name="Origin",
            ),
        ),
    ]
