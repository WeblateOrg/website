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

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from weblate_web.const import SERVICE_RENEWAL_FOLLOW_UP_DAYS
from weblate_web.models import ServiceKind, Subscription
from weblate_web.payments.models import CustomerFollowUp

if TYPE_CHECKING:
    from datetime import datetime


def reconcile_renewal_follow_ups() -> None:
    now = timezone.now()
    cutoff = now + timedelta(days=SERVICE_RENEWAL_FOLLOW_UP_DAYS)
    subscriptions = Subscription.objects.filter(
        Q(
            service__kind=ServiceKind.SERVICE,
            enabled=True,
            expires__gt=now,
            expires__lte=cutoff,
        )
        | Q(renewal_follow_up__isnull=False)
    )
    for subscription_id in subscriptions.values_list("pk", flat=True):
        reconcile_subscription_renewal_follow_up(subscription_id, now, cutoff)


def needs_renewal_follow_up(subscription: Subscription) -> bool:
    payment = subscription.payment
    has_automatic_payment = payment is not None and bool(payment.recurring)
    return (
        subscription.service.kind == ServiceKind.SERVICE
        and subscription.package.get_repeat() == "y"
        and not has_automatic_payment
        and not subscription.could_be_obsolete()
        and next(subscription.get_renewal_invoices(), None) is None
    )


@transaction.atomic
def reconcile_subscription_renewal_follow_up(
    subscription_id: int, now: datetime, cutoff: datetime
) -> None:
    subscription = (
        Subscription.objects.select_for_update().filter(pk=subscription_id).first()
    )
    if subscription is None:
        return

    followups = CustomerFollowUp.objects.filter(subscription=subscription)
    if (
        not subscription.enabled
        or not now < subscription.expires <= cutoff
        or subscription.renewal_follow_up_dismissed_for == subscription.expires
        or not needs_renewal_follow_up(subscription)
    ):
        followups.delete()
        return

    follow_up_at = subscription.expires - timedelta(days=SERVICE_RENEWAL_FOLLOW_UP_DAYS)
    # A task for an old expiry must have a different ID so a stale form cannot
    # dismiss the task for the new expiry.
    followups.exclude(follow_up_at=follow_up_at).delete()
    followups.update_or_create(
        subscription=subscription,
        defaults={
            "type": CustomerFollowUp.Type.SERVICE_RENEWAL,
            "service_id": subscription.service_id,
            "customer_id": subscription.service.customer_id,
            "follow_up_at": follow_up_at,
        },
    )
