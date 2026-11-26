# Copyright © Michal Čihař <michal@weblate.org>
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from .invitations import get_invitation_token
from .models import Customer, CustomerOwnerInvitation


class OwnerInvitationMigrationTest(TransactionTestCase):
    @staticmethod
    def restore_latest_migrations() -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_revokes_legacy_pending_invitations(self) -> None:
        previous = ("payments", "0012_thepaynotification")
        self.addCleanup(self.restore_latest_migrations)
        executor = MigrationExecutor(connection)
        executor.migrate([previous])
        historical = executor.loader.project_state([previous]).apps
        invitations = historical.get_model("payments", "CustomerOwnerInvitation")

        owner = User.objects.create_user(username="owner", email="owner@example.com")
        recipient = User.objects.create_user(
            username="recipient", email="recipient@example.com"
        )
        customer = Customer.objects.create(name="Customer", user_id=owner.pk)
        customer.owners.add(owner)
        now = timezone.now()
        pending = invitations.objects.create(
            customer_id=customer.pk,
            email=recipient.email,
            invited_by_id=owner.pk,
            status="pending",
            expires_at=now + timedelta(days=1),
        )
        accepted = invitations.objects.create(
            customer_id=customer.pk,
            email=owner.email,
            invited_by_id=owner.pk,
            status="accepted",
            expires_at=now + timedelta(days=1),
            acted_at=now,
            acted_by_id=owner.pk,
        )
        token = get_invitation_token(CustomerOwnerInvitation.objects.get(pk=pending.pk))

        self.restore_latest_migrations()

        pending.refresh_from_db()
        accepted.refresh_from_db()
        self.assertEqual(pending.status, "revoked")
        self.assertIsNotNone(pending.acted_at)
        self.assertEqual(pending.email, recipient.email)
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(accepted.acted_at, now)
        self.assertEqual(accepted.acted_by_id, owner.pk)
        self.client.force_login(recipient)
        with override("en"):
            response = self.client.post(
                reverse("customer-owner-invitation", kwargs={"token": token})
            )
            self.assertRedirects(response, reverse("user"))
        self.assertFalse(customer.owners.filter(pk=recipient.pk).exists())
