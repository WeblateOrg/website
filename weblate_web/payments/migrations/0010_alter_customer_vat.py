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
        ("payments", "0009_alter_payment_state"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customer",
            name="vat",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Include the country prefix for European Union VAT IDs. Leave blank if not applicable.",
                max_length=200,
                null=True,
                verbose_name="VAT ID",
            ),
        ),
    ]
