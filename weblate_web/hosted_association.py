# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from weblate_web.categories import ServiceCategory
from weblate_web.hosted_users import iter_chunks
from weblate_web.models import Service, ServiceKind, Subscription
from weblate_web.payments.models import Payment

if TYPE_CHECKING:
    from uuid import UUID

BATCH_SIZE = 400


class HostedAssociationConflict(Exception):
    def __init__(self, reason: str, **identifiers: object) -> None:
        super().__init__(reason)
        self.details = {"error": reason, **identifiers}


def validate_linked_payments(customer_id: int, references: list[UUID]) -> None:
    conflicts: list[tuple[UUID, int]] = []
    for batch in iter_chunks(references, BATCH_SIZE):
        conflicts.extend(
            Payment.objects.filter(pk__in=batch, state=Payment.PROCESSED)
            .exclude(customer_id=customer_id)
            .order_by()
            .values_list("pk", "customer_id")
        )
    if conflicts:
        raise HostedAssociationConflict(
            "payment_customer_conflict",
            customer_id=customer_id,
            payment_ids=sorted(str(pk) for pk, _payer_id in conflicts),
            payer_ids=sorted({payer_id for _pk, payer_id in conflicts}),
        )


def historical_payments(billing: int, references: list[UUID]) -> dict[UUID, Payment]:
    processed = Payment.objects.filter(state=Payment.PROCESSED).order_by()
    tagged = processed.filter(
        Q(extra__billing=billing) | Q(extra__billing=str(billing))
    )
    payments = {payment.pk: payment for payment in tagged.iterator()}
    for reference_batch in iter_chunks(set(references) - payments.keys(), BATCH_SIZE):
        payments.update(processed.in_bulk(reference_batch))
    if missing := set(references) - payments.keys():
        raise HostedAssociationConflict(
            "payment_unavailable", payment_ids=sorted(map(str, missing))
        )

    frontier = list(payments.values())
    while frontier:
        related: dict[UUID, Payment] = {}
        for batch in iter_chunks(frontier, BATCH_SIZE):
            parents = {payment.repeat_id for payment in batch if payment.repeat_id}
            children = [payment.pk for payment in batch]
            related.update(
                (payment.pk, payment)
                for payment in processed.filter(
                    Q(pk__in=parents) | Q(repeat_id__in=children)
                )
            )
        frontier = [payment for pk, payment in related.items() if pk not in payments]
        payments.update(related)
    return payments


def historical_service(
    billing: int, references: list[UUID]
) -> tuple[Service | None, int | None]:
    payments = historical_payments(billing, references)
    if not payments:
        return None, None

    for payment in payments.values():
        tag = payment.extra.get("billing") if isinstance(payment.extra, dict) else None
        if tag is not None and (
            type(tag) not in {int, str} or str(tag) != str(billing)
        ):
            raise HostedAssociationConflict(
                "payment_billing_conflict",
                payment_id=str(payment.pk),
                payment_billing=tag,
            )

    customers = {payment.customer_id for payment in payments.values()}
    if len(customers) != 1:
        raise HostedAssociationConflict(
            "multiple_payers",
            customer_ids=sorted(customers),
            payment_ids=sorted(map(str, payments)),
        )
    customer_id = next(iter(customers))

    subscriptions: dict[int, Subscription] = {}
    for batch in iter_chunks(payments, BATCH_SIZE):
        subscriptions.update(
            (subscription.pk, subscription)
            for subscription in Subscription.objects.filter(
                Q(payment_id__in=batch) | Q(past_payments__in=batch)
            )
            .select_related("service", "package")
            .distinct()
        )
    services = {
        subscription.service_id: subscription.service
        for subscription in subscriptions.values()
    }
    if len(services) > 1 or any(
        subscription.package.category != ServiceCategory.SHARED
        or subscription.service.kind != ServiceKind.SERVICE
        or subscription.service.customer_id != customer_id
        or subscription.service.hosted_billing is not None
        for subscription in subscriptions.values()
    ):
        raise HostedAssociationConflict(
            "service_conflict",
            service_ids=sorted(services),
            customer_id=customer_id,
            payment_ids=sorted(map(str, payments)),
        )
    return next(iter(services.values()), None), customer_id
