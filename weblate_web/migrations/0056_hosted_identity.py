# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def rename_identity_permissions(
    apps, schema_editor, old: str, new: str, label: str
) -> None:
    """Preserve content types and grants when changing the identity model name."""
    alias = schema_editor.connection.alias
    content_types = apps.get_model("contenttypes", "ContentType").objects.using(alias)
    content_type = content_types.filter(app_label="weblate_web", model=old).first()
    if content_type is None:
        content_type = content_types.filter(app_label="weblate_web", model=new).first()
    if content_type is None:
        # On a fresh database, post_migrate creates the new permissions.
        return
    if content_type.model != new:
        content_type.model = new
        content_type.save(using=alias, update_fields=["model"])
    permissions = apps.get_model("auth", "Permission").objects.using(alias)
    for action in ("add", "change", "delete", "view"):
        permissions.filter(
            content_type_id=content_type.pk, codename=f"{action}_{old}"
        ).update(codename=f"{action}_{new}", name=f"Can {action} {label}")


def forwards(apps, schema_editor) -> None:
    rename_identity_permissions(
        apps, schema_editor, "samlidentity", "hostedidentity", "Hosted identity"
    )


def backwards(apps, schema_editor) -> None:
    rename_identity_permissions(
        apps, schema_editor, "hostedidentity", "samlidentity", "SAML identity"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0055_service_category"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="samlidentity", name="weblate_web_saml_identity_unique"
        ),
        migrations.RemoveConstraint(
            model_name="samlidentity", name="weblate_web_saml_identity_user_unique"
        ),
        migrations.RenameModel(old_name="SamlIdentity", new_name="HostedIdentity"),
        migrations.AlterModelOptions(
            name="hostedidentity",
            options={
                "verbose_name": "Hosted identity",
                "verbose_name_plural": "Hosted identities",
            },
        ),
        migrations.AlterField(
            model_name="hostedidentity",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="hosted_identities",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="hostedidentity",
            constraint=models.UniqueConstraint(
                fields=("provider", "external_id"),
                name="weblate_web_hosted_identity_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="hostedidentity",
            constraint=models.UniqueConstraint(
                fields=("provider", "user"),
                name="weblate_web_hosted_identity_user_unique",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
