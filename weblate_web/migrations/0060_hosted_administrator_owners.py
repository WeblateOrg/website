# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def apply_existing_administrators(apps, schema_editor) -> None:
    alias = schema_editor.connection.alias
    administrator_model = apps.get_model("weblate_web", "HostedBillingAdministrator")
    identity_model = apps.get_model("weblate_web", "HostedIdentity")
    identities = dict(
        identity_model.objects.using(alias)
        .filter(provider=settings.HOSTED_IDENTITY_PROVIDER, user__is_active=True)
        .values_list("external_id", "user_id")
    )
    entries = (
        administrator_model.objects.using(alias)
        .filter(service__hosted_billing__gt=0)
        .select_related("service__customer")
    )
    for entry in entries.iterator():
        if user_id := identities.get(entry.external_id):
            entry.service.customer.owners.add(user_id)
            entry.user_id = user_id
            entry.save(using=alias, update_fields=["user"])


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0059_hosted_report_timestamps"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="hostedbillingadministrator",
            name="user",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(apply_existing_administrators, migrations.RunPython.noop),
    ]
