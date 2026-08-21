#
# Copyright © Michal Čihař <michal@cihar.com>
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


def use_current_calculation_for_drafts(apps, _schema_editor):
    Invoice = apps.get_model("invoices", "Invoice")
    Payment = apps.get_model("payments", "Payment")
    in_flight_drafts = Payment.objects.filter(
        state__in=(2, 4), draft_invoice_id__isnull=False
    ).values("draft_invoice_id")
    Invoice.objects.filter(kind=0).exclude(pk__in=in_flight_drafts).update(
        calculation_version=1
    )


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0002_initial"),
        ("payments", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="calculation_version",
            field=models.IntegerField(
                choices=[(0, "Legacy"), (1, "EN 16931")],
                default=0,
                editable=False,
            ),
        ),
        migrations.RunPython(
            use_current_calculation_for_drafts, migrations.RunPython.noop
        ),
        migrations.AlterField(
            model_name="invoice",
            name="calculation_version",
            field=models.IntegerField(
                choices=[(0, "Legacy"), (1, "EN 16931")],
                default=1,
                editable=False,
            ),
        ),
    ]
