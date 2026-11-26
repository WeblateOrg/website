# Copyright © Michal Čihař <michal@weblate.org>
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import migrations
from django.utils import timezone

if TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def revoke_legacy_invitations(
    apps: Apps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    invitation = apps.get_model("payments", "CustomerOwnerInvitation")
    # The original mailbox cannot be reconstructed from a case-folded address.
    invitation.objects.using(schema_editor.connection.alias).filter(
        status="pending"
    ).update(status="revoked", acted_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [("payments", "0012_thepaynotification")]
    operations = [
        migrations.RunPython(revoke_legacy_invitations, migrations.RunPython.noop)
    ]
