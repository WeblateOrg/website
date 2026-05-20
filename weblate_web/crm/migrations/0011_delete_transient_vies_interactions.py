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

from django.db import migrations
from django.db.models import Q

VIES_ORIGIN = 4
RETRYABLE_VIES_FAULT_MESSAGES = ("MS_UNAVAILABLE", "MS_MAX_CONCURRENT_REQ", "TIMEOUT")


def delete_transient_vies_interactions(apps, schema_editor) -> None:
    interaction_model = apps.get_model("CRM", "Interaction")
    transient_summary = Q(summary__in=RETRYABLE_VIES_FAULT_MESSAGES)
    for fault_message in RETRYABLE_VIES_FAULT_MESSAGES:
        transient_summary |= Q(summary__endswith=f": {fault_message}")
    interaction_model.objects.filter(
        origin=VIES_ORIGIN,
        details__automated=True,
    ).filter(transient_summary).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("CRM", "0010_alter_interaction_origin"),
    ]

    operations = [
        migrations.RunPython(
            delete_transient_vies_interactions,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
