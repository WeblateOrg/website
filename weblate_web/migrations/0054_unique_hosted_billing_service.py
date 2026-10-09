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
from django.db.models import Count


def clear_unassociated_billing(apps, schema_editor) -> None:
    Service = apps.get_model("weblate_web", "Service")
    services = Service.objects.using(schema_editor.connection.alias)
    duplicate = (
        services.filter(hosted_billing__gt=0)
        .values("hosted_billing")
        .annotate(count=Count("pk"))
        .filter(count__gt=1)
        .order_by("hosted_billing")
        .first()
    )
    if duplicate:
        raise RuntimeError(
            f"Resolve duplicate Hosted billing ID {duplicate['hosted_billing']} before migrating"
        )
    services.filter(hosted_billing=0).update(hosted_billing=None)


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0053_service_activity_drop_state_serviceactivity"),
    ]

    operations = [
        migrations.AlterField(
            model_name="service",
            name="hosted_billing",
            field=models.IntegerField(db_index=True, default=0, null=True),
        ),
        migrations.RunPython(clear_unassociated_billing, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="service",
            name="hosted_billing",
            field=models.PositiveIntegerField(blank=True, null=True, unique=True),
        ),
    ]
