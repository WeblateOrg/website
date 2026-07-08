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

import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models


def migrate_customer_followups(apps, schema_editor) -> None:
    customer_model = apps.get_model("payments", "Customer")
    followup_model = apps.get_model("payments", "CustomerFollowUp")
    followups = [
        followup_model(
            customer_id=customer.pk,
            follow_up_at=customer.follow_up_at,
            note=customer.follow_up_note,
            type=1,
        )
        for customer in customer_model.objects.exclude(
            follow_up_at__isnull=True
        ).iterator(chunk_size=1000)
    ]
    followup_model.objects.bulk_create(followups, batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0060_customer_follow_up"),
        ("weblate_web", "0051_samlidentity_externalsyncstate"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerFollowUp",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "follow_up_at",
                    models.DateTimeField(
                        db_index=True,
                        help_text="Date and time when this customer needs attention.",
                        verbose_name="Follow-up date",
                    ),
                ),
                (
                    "note",
                    models.CharField(
                        blank=True,
                        help_text="Short description of the next action.",
                        max_length=200,
                        verbose_name="Follow-up note",
                    ),
                ),
                (
                    "type",
                    models.PositiveSmallIntegerField(
                        choices=[
                            (1, "Manual"),
                            (2, "Duplicate payment"),
                            (3, "Locked site URL"),
                        ],
                        db_index=True,
                        default=1,
                        verbose_name="Follow-up type",
                    ),
                ),
                (
                    "details",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="followups",
                        to="payments.customer",
                    ),
                ),
                (
                    "service",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="followups",
                        to="weblate_web.service",
                    ),
                ),
            ],
            options={
                "verbose_name": "Customer follow-up",
                "verbose_name_plural": "Customer follow-ups",
                "ordering": ["follow_up_at"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("service__isnull", False),
                            ("type", 3),
                        ),
                        fields=("service", "type"),
                        name="unique_locked_site_url_followup_per_service",
                    )
                ],
            },
        ),
        migrations.RunPython(migrate_customer_followups, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="customer",
            name="follow_up_at",
        ),
        migrations.RemoveField(
            model_name="customer",
            name="follow_up_note",
        ),
    ]
