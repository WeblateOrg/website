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
    dependencies = [
        ("payments", "0008_customerownerinvitation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="state",
            field=models.IntegerField(
                choices=[
                    (1, "New payment"),
                    (2, "Awaiting payment"),
                    (3, "Payment rejected"),
                    (4, "Payment accepted"),
                    (5, "Payment processed"),
                    (6, "Cancelled"),
                ],
                db_index=True,
                default=1,
            ),
        ),
    ]
