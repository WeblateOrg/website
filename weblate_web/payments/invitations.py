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

from __future__ import annotations

import logging
from datetime import timedelta
from itertools import islice
from typing import TYPE_CHECKING, TypeVar

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import transaction
from django.utils import timezone

from weblate_web.utils import get_site_url

from .mailboxes import mailbox_address
from .models import Customer, CustomerOwnerInvitation
from .utils import send_notification

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.models import Model, QuerySet

LOGGER = logging.getLogger(__name__)
ModelT = TypeVar("ModelT", bound="Model")

INVITATION_MAX_AGE = timedelta(days=7)
INVITATION_SIGNING_SALT = "weblate.customer-owner-invitation"
INVITATION_RATE_LIMIT = 10
INVITATION_RATE_PERIOD = 3600


def mailbox_candidates(objects: QuerySet[ModelT], address: str) -> QuerySet[ModelT]:
    # A case-insensitive collation can return extra rows. Compare the full
    # canonical address in Python before using any candidate.
    local, _, _ = address.rpartition("@")
    return objects.filter(email__startswith=f"{local}@")


def matching_users(users: QuerySet[User], address: str) -> Iterator[User]:
    return (
        user
        for user in mailbox_candidates(users.only("pk", "email"), address).iterator()
        if mailbox_address(user.email) == address
    )


def matching_invitations(
    invitations: QuerySet[CustomerOwnerInvitation], address: str
) -> Iterator[CustomerOwnerInvitation]:
    return (
        invitation
        for invitation in mailbox_candidates(invitations, address).iterator()
        if mailbox_address(invitation.email) == address
    )


def invitation_matches_user(invitation: CustomerOwnerInvitation, user: User) -> bool:
    address = mailbox_address(invitation.email)
    return address is not None and mailbox_address(user.email) == address


def check_invitation_rate_limit(user: User) -> bool:
    key = f"customer-owner-invitation:{user.pk}"
    try:
        if cache.add(key, 1, INVITATION_RATE_PERIOD):
            return True
        return cache.incr(key) <= INVITATION_RATE_LIMIT
    except Exception:  # pylint: disable=broad-exception-caught
        LOGGER.exception("Could not update customer owner invitation rate limit")
        return False


def get_invitation_token(invitation: CustomerOwnerInvitation) -> str:
    return dumps(str(invitation.pk), salt=INVITATION_SIGNING_SALT)


def resolve_invitation(token: str) -> CustomerOwnerInvitation | None:
    try:
        invitation_id = loads(
            token,
            salt=INVITATION_SIGNING_SALT,
            max_age=INVITATION_MAX_AGE,
        )
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(invitation_id, str):
        return None
    try:
        return (
            CustomerOwnerInvitation.objects.select_related("customer")
            .filter(pk=invitation_id)
            .first()
        )
    except (TypeError, ValueError):
        return None


def create_invitation(
    customer: Customer, email: str, invited_by: User
) -> CustomerOwnerInvitation | None:
    # Avoid a model-level import cycle.
    from weblate_web.crm.models import Interaction  # ruff: ignore[import-outside-top-level]

    email = email.strip()
    address = mailbox_address(email)
    if address is None:
        return None
    now = timezone.now()
    with transaction.atomic():
        customer = Customer.objects.select_for_update().get(pk=customer.pk)
        if customer.hosted_workspaces:
            return None
        pending = customer.owner_invitations.filter(
            status=CustomerOwnerInvitation.Status.PENDING
        )
        if next(matching_users(customer.owners.all(), address), None) is not None:
            invitation_ids = [
                invitation.pk for invitation in matching_invitations(pending, address)
            ]
            pending.filter(pk__in=invitation_ids).update(
                status=CustomerOwnerInvitation.Status.REVOKED,
                acted_at=now,
                acted_by=invited_by,
            )
            customer.interaction_set.create(
                origin=Interaction.Origin.CUSTOMER_OWNER,
                summary="Skipped customer owner invitation",
                content=f"Did not invite existing customer owner {email}.",
                details={
                    "action": "skip_existing_owner",
                    "invitations": [str(pk) for pk in invitation_ids],
                    "email": email,
                },
                user=invited_by,
            )
            return None
        invitation = next(
            matching_invitations(pending.filter(expires_at__gt=now), address), None
        )
        if invitation is not None:
            invitation.expires_at = now + INVITATION_MAX_AGE
            invitation.save(update_fields=("expires_at",))
            summary = "Resent customer owner invitation"
            content = f"Resent the customer owner invitation to {email}."
            action = "resend_invitation"
        else:
            invitation = customer.owner_invitations.create(
                email=email,
                invited_by=invited_by,
                expires_at=now + INVITATION_MAX_AGE,
            )
            summary = "Invited customer owner"
            content = f"Invited {email} to become a customer owner."
            action = "invite"
        customer.interaction_set.create(
            origin=Interaction.Origin.CUSTOMER_OWNER,
            summary=summary,
            content=content,
            details={
                "action": action,
                "invitation": str(invitation.pk),
                "email": email,
            },
            user=invited_by,
        )

    attempt_expires_at = invitation.expires_at
    try:
        send_notification(
            "customer_owner_invitation",
            [address],
            customer=customer,
            invitation=invitation,
            invitation_url=get_site_url(
                "customer-owner-invitation",
                strip_language=False,
                token=get_invitation_token(invitation),
            ),
        )
    except Exception:  # pylint: disable=broad-exception-caught
        LOGGER.exception("Could not send customer owner invitation")
        acted_at = timezone.now()
        updated = CustomerOwnerInvitation.objects.filter(
            pk=invitation.pk,
            status=CustomerOwnerInvitation.Status.PENDING,
            expires_at=attempt_expires_at,
        ).update(
            status=CustomerOwnerInvitation.Status.DELIVERY_FAILED,
            acted_at=acted_at,
        )
        if updated:
            invitation.status = CustomerOwnerInvitation.Status.DELIVERY_FAILED
            invitation.acted_at = acted_at
    return invitation


def accept_invitation(
    invitation: CustomerOwnerInvitation, user: User
) -> CustomerOwnerInvitation | None:
    # Avoid a model-level import cycle.
    from weblate_web.crm.models import Interaction  # ruff: ignore[import-outside-top-level]

    now = timezone.now()
    with transaction.atomic():
        customer = Customer.objects.select_for_update().get(pk=invitation.customer_id)
        if customer.hosted_workspaces:
            return None
        invitation = CustomerOwnerInvitation.objects.select_for_update().get(
            pk=invitation.pk
        )
        if (
            invitation.status != CustomerOwnerInvitation.Status.PENDING
            or invitation.expires_at <= now
            or (address := mailbox_address(invitation.email)) is None
        ):
            return None
        users = list(islice(matching_users(User.objects.all(), address), 2))
        if len(users) != 1 or users[0].pk != user.pk:
            return None
        invitation.customer.owners.add(user)
        invitation.status = CustomerOwnerInvitation.Status.ACCEPTED
        invitation.acted_at = now
        invitation.acted_by = user
        invitation.save(update_fields=("status", "acted_at", "acted_by"))
        invitation.customer.interaction_set.create(
            origin=Interaction.Origin.CUSTOMER_OWNER,
            summary="Customer owner invitation accepted",
            content=f"{invitation.email} accepted a customer owner invitation.",
            details={
                "action": "accept_invitation",
                "invitation": str(invitation.pk),
                "owner": user.pk,
                "email": invitation.email,
            },
            user=user,
        )
    return invitation


def revoke_invitation(
    invitation: CustomerOwnerInvitation, user: User
) -> CustomerOwnerInvitation | None:
    # Avoid a model-level import cycle.
    from weblate_web.crm.models import Interaction  # ruff: ignore[import-outside-top-level]

    with transaction.atomic():
        locked_invitation = (
            CustomerOwnerInvitation.objects.select_for_update()
            .filter(pk=invitation.pk, customer_id=invitation.customer_id)
            .first()
        )
        if (
            locked_invitation is None
            or locked_invitation.status != CustomerOwnerInvitation.Status.PENDING
        ):
            return None
        invitation = locked_invitation
        invitation.status = CustomerOwnerInvitation.Status.REVOKED
        invitation.acted_at = timezone.now()
        invitation.acted_by = user
        invitation.save(update_fields=("status", "acted_at", "acted_by"))
        invitation.customer.interaction_set.create(
            origin=Interaction.Origin.CUSTOMER_OWNER,
            summary="Revoked customer owner invitation",
            content=(f"Revoked the customer owner invitation for {invitation.email}."),
            details={
                "action": "revoke_invitation",
                "invitation": str(invitation.pk),
                "email": invitation.email,
            },
            user=user,
        )
    return invitation


def fulfill_pending_invitations_by_staff(
    customer: Customer, email: str, staff: User
) -> None:
    # Avoid a model-level import cycle.
    from weblate_web.crm.models import Interaction  # ruff: ignore[import-outside-top-level]

    address = mailbox_address(email)
    if address is None:
        return
    invitations = customer.owner_invitations.filter(
        status=CustomerOwnerInvitation.Status.PENDING
    )
    invitation_ids = [
        invitation.pk for invitation in matching_invitations(invitations, address)
    ]
    invitations.filter(pk__in=invitation_ids).update(
        status=CustomerOwnerInvitation.Status.FULFILLED_BY_STAFF,
        acted_at=timezone.now(),
        acted_by=staff,
    )
    if invitation_ids:
        customer.interaction_set.create(
            origin=Interaction.Origin.CUSTOMER_OWNER,
            summary="Customer owner invitation fulfilled by staff",
            content=f"Staff fulfilled the customer owner invitation for {email}.",
            details={
                "action": "fulfill_invitation_by_staff",
                "invitations": [str(pk) for pk in invitation_ids],
                "email": email,
            },
            user=staff,
        )
