# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from social_django.models import UserSocialAuth

from weblate_web.hosted_users import EMAIL_ALLOCATION_LOCK_KEY
from weblate_web.models import ExternalSyncState, HostedIdentity
from weblate_web.payments.models import Customer


class HostedIdentityMigrationTest(TransactionTestCase):
    previous = ("weblate_web", "0055_service_category")
    current = ("weblate_web", "0056_hosted_identity")

    def migrate(self, target: tuple[str, str]):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        ContentType.objects.clear_cache()
        return executor.loader.project_state([target]).apps

    def test_preserves_identity_and_permissions_in_both_directions(self) -> None:
        historical = self.migrate(self.previous)
        self.addCleanup(self.migrate, self.current)

        user = User.objects.create_user(username="existing-owner", is_staff=True)
        other = User.objects.create_user(username="other")
        seen = timezone.now()
        attrs = {"username": "existing-owner", "external_id": "42"}
        identity = historical.get_model("weblate_web", "SamlIdentity").objects.create(
            provider=settings.HOSTED_IDENTITY_PROVIDER,
            external_id="42",
            user_id=user.pk,
            last_seen=seen,
            raw_attrs=attrs,
        )
        social = UserSocialAuth.objects.create(
            provider="weblate", uid="42", id_key="sub", user=user
        )
        customer = Customer.objects.create(name="Existing customer", user_id=user.pk)
        customer.owners.add(user)
        for key in ("hosted-users", EMAIL_ALLOCATION_LOCK_KEY):
            ExternalSyncState.objects.create(key=key, cursor="preserved-cursor")

        content_type, _created = ContentType.objects.get_or_create(
            app_label="weblate_web", model="samlidentity"
        )
        permissions = {}
        for action in ("add", "change", "delete", "view"):
            permissions[action], _created = Permission.objects.get_or_create(
                content_type=content_type,
                codename=f"{action}_samlidentity",
                defaults={"name": f"Can {action} SAML identity"},
            )
        user.user_permissions.add(permissions["change"])
        group = Group.objects.create(name="Identity administrators")
        group.permissions.add(permissions["view"])
        user.groups.add(group)

        self.migrate(self.current)
        renamed = HostedIdentity.objects.get(pk=identity.pk)
        self.assertEqual(
            (
                renamed.user_id,
                renamed.provider,
                renamed.external_id,
                renamed.last_seen,
                renamed.raw_attrs,
            ),
            (user.pk, settings.HOSTED_IDENTITY_PROVIDER, "42", seen, attrs),
        )
        self.assertEqual(user.hosted_identities.get(), renamed)
        self.assertEqual(UserSocialAuth.objects.get(pk=social.pk).user_id, user.pk)
        self.assertTrue(customer.owners.filter(pk=user.pk).exists())
        content_type.refresh_from_db()
        self.assertEqual(content_type.model, "hostedidentity")
        for action, permission in permissions.items():
            permission.refresh_from_db()
            self.assertEqual(permission.codename, f"{action}_hostedidentity")
            self.assertEqual(permission.content_type_id, content_type.pk)
        refreshed = User.objects.get(pk=user.pk)
        self.assertTrue(refreshed.has_perm("weblate_web.change_hostedidentity"))
        self.assertTrue(refreshed.has_perm("weblate_web.view_hostedidentity"))
        self.assertEqual(
            set(ExternalSyncState.objects.values_list("cursor", flat=True)),
            {"preserved-cursor"},
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            HostedIdentity.objects.create(
                provider=renamed.provider, external_id="42", user=other
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            HostedIdentity.objects.create(
                provider=renamed.provider, external_id="43", user=user
            )

        historical = self.migrate(self.previous)
        original = historical.get_model("weblate_web", "SamlIdentity").objects.get(
            pk=identity.pk
        )
        self.assertEqual(
            (
                original.user_id,
                original.provider,
                original.external_id,
                original.last_seen,
                original.raw_attrs,
            ),
            (user.pk, settings.HOSTED_IDENTITY_PROVIDER, "42", seen, attrs),
        )
        content_type.refresh_from_db()
        self.assertEqual(content_type.model, "samlidentity")
        refreshed = User.objects.get(pk=user.pk)
        self.assertTrue(refreshed.has_perm("weblate_web.change_samlidentity"))
        self.assertTrue(refreshed.has_perm("weblate_web.view_samlidentity"))
        self.migrate(self.current)
        self.assertEqual(HostedIdentity.objects.get(pk=identity.pk).user_id, user.pk)

    def test_migrate_command_preserves_content_type_and_grants(self) -> None:
        self.migrate(self.previous)
        self.addCleanup(lambda: self.migrate(self.current))
        content_type, _created = ContentType.objects.get_or_create(
            app_label="weblate_web", model="samlidentity"
        )
        permission, _created = Permission.objects.get_or_create(
            content_type=content_type,
            codename="change_samlidentity",
            defaults={"name": "Can change SAML identity"},
        )
        user = User.objects.create_user(username="identity-administrator")
        user.user_permissions.add(permission)
        call_command(
            "migrate", "weblate_web", self.current[1], interactive=False, verbosity=0
        )
        content_type.refresh_from_db()
        permission.refresh_from_db()
        self.assertEqual(content_type.model, "hostedidentity")
        self.assertEqual(permission.content_type_id, content_type.pk)
        self.assertEqual(permission.codename, "change_hostedidentity")
        self.assertTrue(
            User.objects.get(pk=user.pk).has_perm("weblate_web.change_hostedidentity")
        )
