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
from django.utils.translation import gettext_lazy


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0047_replace_markupfield"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="maintenance_window",
            field=models.CharField(
                blank=True,
                max_length=200,
                verbose_name=gettext_lazy("Maintenance window"),
            ),
        ),
    ]
