# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


def create_sync_lock(apps, schema_editor) -> None:
    state = apps.get_model("weblate_web", "ExternalSyncState")
    state.objects.using(schema_editor.connection.alias).get_or_create(
        key="hosted-service-sync"
    )


def delete_sync_lock(apps, schema_editor) -> None:
    state = apps.get_model("weblate_web", "ExternalSyncState")
    state.objects.using(schema_editor.connection.alias).filter(
        key="hosted-service-sync"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0057_subscription_renewal_follow_up_dismissed_for"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="hosted_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.AddField(
            model_name="service",
            name="hosted_snapshot_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.CreateModel(
            name="HostedBillingAdministrator",
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
                ("external_id", models.CharField(max_length=255)),
                (
                    "service",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="hosted_administrators",
                        to="weblate_web.service",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("service", "external_id"),
                        name="unique_hosted_service_administrator",
                    )
                ],
            },
        ),
        migrations.RunPython(create_sync_lock, delete_sync_lock),
    ]
