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
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0050_subscription_payment_fk"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ExternalSyncState",
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
                ("key", models.CharField(max_length=100, unique=True)),
                ("cursor", models.CharField(blank=True, max_length=255)),
                ("updated", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "External sync state",
                "verbose_name_plural": "External sync states",
            },
        ),
        migrations.CreateModel(
            name="SamlIdentity",
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
                ("provider", models.CharField(max_length=255)),
                ("external_id", models.CharField(max_length=255)),
                (
                    "last_seen",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "raw_attrs",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="saml_identities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "SAML identity",
                "verbose_name_plural": "SAML identities",
            },
        ),
        migrations.AddConstraint(
            model_name="samlidentity",
            constraint=models.UniqueConstraint(
                fields=("provider", "external_id"),
                name="weblate_web_saml_identity_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="samlidentity",
            constraint=models.UniqueConstraint(
                fields=("provider", "user"),
                name="weblate_web_saml_identity_user_unique",
            ),
        ),
    ]
