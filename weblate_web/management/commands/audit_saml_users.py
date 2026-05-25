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

from dataclasses import dataclass

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q
from django.db.models.functions import Lower

from weblate_web.crm.models import Interaction
from weblate_web.models import Service
from weblate_web.payments.models import Customer

DUPLICATE_EMAIL_PREFIX = "duplicate"


@dataclass
class UserAudit:
    user: User
    customer_ids: list[int]
    service_count: int
    interaction_count: int
    identities: list[str]
    group_count: int
    permission_count: int
    has_local_password: bool

    @property
    def value_reasons(self) -> list[str]:
        reasons = []
        if self.user.last_login is not None:
            reasons.append("last-login")
        if self.customer_ids:
            reasons.append("customers")
        if self.service_count:
            reasons.append("services")
        if self.interaction_count:
            reasons.append("crm-interactions")
        if self.identities:
            reasons.append("saml-identities")
        if self.user.is_staff or self.user.is_superuser:
            reasons.append("staff")
        if self.group_count:
            reasons.append("groups")
        if self.permission_count:
            reasons.append("permissions")
        if self.has_local_password:
            reasons.append("local-password")
        return reasons

    @property
    def has_value(self) -> bool:
        return bool(self.value_reasons)


def audit_user(user: User) -> UserAudit:
    customers = Customer.objects.filter(Q(users=user) | Q(user_id=user.pk)).distinct()
    customer_ids = list(customers.values_list("id", flat=True))
    return UserAudit(
        user=user,
        customer_ids=customer_ids,
        service_count=Service.objects.filter(customer__in=customers).count(),
        interaction_count=Interaction.objects.filter(user=user).count(),
        identities=[
            f"{identity.provider}:{identity.external_id}"
            for identity in user.saml_identities.all()
        ],
        group_count=user.groups.count(),
        permission_count=user.user_permissions.count(),
        has_local_password=bool(user.password) and user.has_usable_password(),
    )


def get_duplicate_email(user: User) -> str:
    result = f"{DUPLICATE_EMAIL_PREFIX}-{user.pk}-{user.email}"
    max_length = User._meta.get_field("email").max_length  # pylint: disable=protected-access
    if max_length is not None and len(result) > max_length:
        raise ValueError("prefixed e-mail would exceed field length")
    return result


class Command(BaseCommand):
    help = "audits users before switching SAML to persistent hosted IDs"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--cleanup-unused",
            action="store_true",
            help="prefix e-mails on unused duplicate users",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="apply cleanup changes instead of showing a dry-run plan",
        )

    def handle(self, *args, **options) -> None:
        cleanup_unused = options["cleanup_unused"]
        apply = options["apply"]
        if apply and not cleanup_unused:
            raise CommandError("--apply requires --cleanup-unused")

        cleanup_count = 0
        duplicates = (
            User.objects.exclude(email="")
            .annotate(email_lower=Lower("email"))
            .values("email_lower")
            .annotate(count=Count("id"))
            .filter(count__gt=1)
            .order_by("email_lower")
        )

        for duplicate in duplicates:
            email = duplicate["email_lower"]
            self.stdout.write(f"Duplicate email: {email}")
            audits = [
                audit_user(user)
                for user in User.objects.filter(email__iexact=email).order_by("id")
            ]
            for audit in audits:
                user = audit.user
                self.stdout.write(
                    "  "
                    f"id={user.id} username={user.username!r} "
                    f"last_login={user.last_login} "
                    f"customers={audit.customer_ids} services={audit.service_count} "
                    f"crm_interactions={audit.interaction_count} "
                    f"saml_identities={audit.identities}"
                )
            if cleanup_unused:
                cleanup_count += self.cleanup_unused_users(audits, apply=apply)

        if cleanup_unused:
            action = "Prefixed" if apply else "Would prefix"
            self.stdout.write(f"{action} duplicate email on {cleanup_count} users")

    def cleanup_unused_users(self, audits: list[UserAudit], *, apply: bool) -> int:
        valued = [audit for audit in audits if audit.has_value]
        if len(valued) > 1:
            self.stdout.write(
                "  Cleanup skipped: multiple users have activity or related data "
                f"({', '.join(format_user_reasons(audit) for audit in valued)})"
            )
            return 0

        keeper = valued[0] if valued else audits[0]
        self.stdout.write(
            f"  Cleanup keeper: id={keeper.user.pk} username={keeper.user.username!r}"
        )
        cleanup_count = 0
        for audit in audits:
            if audit.user.pk == keeper.user.pk:
                continue
            try:
                duplicate_email = get_duplicate_email(audit.user)
            except ValueError as error:
                self.stdout.write(
                    f"  Cleanup skipped: id={audit.user.pk} "
                    f"username={audit.user.username!r}: {error}"
                )
                continue
            action = "Prefixing" if apply else "Would prefix"
            self.stdout.write(
                f"  {action} duplicate email on "
                f"id={audit.user.pk} username={audit.user.username!r}: "
                f"{audit.user.email} -> {duplicate_email}"
            )
            cleanup_count += 1
            if apply:
                audit.user.email = duplicate_email
                audit.user.save(update_fields=["email"])
        return cleanup_count


def format_user_reasons(audit: UserAudit) -> str:
    return f"id={audit.user.pk}: {', '.join(audit.value_reasons)}"
