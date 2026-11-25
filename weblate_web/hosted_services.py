# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from weblate_web.hosted_association import (
    HostedAssociationConflict,
    historical_service,
    validate_linked_payments,
)
from weblate_web.hosted_users import get_default_hosted_provider, iter_chunks
from weblate_web.models import (
    ExternalSyncState,
    HostedBillingAdministrator,
    HostedIdentity,
    Report,
    Service,
)
from weblate_web.payments.models import Customer
from weblate_web.utils import HOSTED_ORIGIN

if TYPE_CHECKING:
    from datetime import datetime


SYNC_LOCK = "hosted-service-sync"
HOSTED_URL = "https://hosted.weblate.org"
MAX_COUNT = 2**31 - 1
USAGE_FIELDS = (
    "projects",
    "components",
    "languages",
    "source_strings",
    "hosted_strings",
    "hosted_words",
)


@dataclass(frozen=True)
class HostedSnapshot:
    billing: int
    timestamp: datetime
    payment_ids: list[UUID]
    administrators: list[str]
    metadata: dict[str, str | bool]
    usage: dict[str, int]
    version: str


def read_string(value: object, max_length: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or (not empty and not value.strip())
        or "\0" in value
    ):
        raise ValueError("Invalid snapshot string")
    value.encode()
    return value


def read_counter(value: object, *, minimum: int = 0) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= MAX_COUNT
    ):
        raise ValueError("Invalid snapshot counter")
    return value


def validate_hosted_snapshot(payload: object) -> HostedSnapshot:
    if not isinstance(payload, dict):
        raise TypeError("Expected a snapshot object")
    timestamp = read_string(payload["snapshot_at"], 32)
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|\+00:00)",
        timestamp,
    ):
        raise ValueError("Expected a UTC snapshot timestamp")
    parsed = parse_datetime(timestamp)
    if parsed is None:
        raise ValueError("Invalid snapshot timestamp")

    if parsed > timezone.now() + timedelta(minutes=5):
        raise ValueError("Snapshot timestamp is too far in the future")

    payment_ids = payload["payment_ids"]
    if not isinstance(payment_ids, list):
        raise TypeError("Expected payment IDs as a list")

    administrators = payload["administrators"]
    if (
        not isinstance(administrators, list)
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,254}", value)
            for value in administrators
        )
        or len(set(administrators)) != len(administrators)
    ):
        raise ValueError("Invalid snapshot administrators")

    state = read_string(payload["state"], 10)
    mode = read_string(payload["payment_mode"], 8)
    paid = payload["paid"]
    plan = read_string(payload["plan"], 100)
    if (
        state not in {"active", "trial", "terminated"}
        or mode not in {"local", "external"}
        or not isinstance(paid, bool)
        or not re.fullmatch(r"[-a-zA-Z0-9_]+", plan)
    ):
        raise ValueError("Invalid Hosted operational state")

    usage = payload["usage"]
    if not isinstance(usage, dict):
        raise TypeError("Invalid snapshot usage")
    return HostedSnapshot(
        billing=read_counter(payload["billing"], minimum=1),
        timestamp=parsed,
        payment_ids=list(
            dict.fromkeys(UUID(read_string(value, 36)) for value in payment_ids)
        ),
        administrators=administrators,
        metadata={
            "workspace_id": str(UUID(read_string(payload["workspace_id"], 36))),
            "workspace_name": read_string(payload["workspace_name"], 100),
            "customer_name": read_string(payload["customer_name"], 200, empty=True),
            "plan": plan,
            "state": state,
            "payment_mode": mode,
            "paid": paid,
        },
        usage={field: read_counter(usage[field]) for field in USAGE_FIELDS},
        version=read_string(payload["version"], 32),
    )


def lock_hosted_sync() -> None:
    # Take the lock as the first database operation, including on SQLite.
    if not ExternalSyncState.objects.filter(key=SYNC_LOCK).update(cursor=""):
        ExternalSyncState.objects.select_for_update().get_or_create(key=SYNC_LOCK)


def apply_pending_hosted_owners(external_ids: set[str]) -> None:
    for batch in iter_chunks(external_ids):
        apply_pending_hosted_owner_batch(batch)


@transaction.atomic
def apply_pending_hosted_owner_batch(external_ids: list[str]) -> None:
    lock_hosted_sync()
    pending = HostedBillingAdministrator.objects.filter(
        external_id__in=external_ids, user__isnull=True, service__hosted_billing__gt=0
    ).select_related("service__customer")
    identities = dict(
        HostedIdentity.objects.filter(
            provider=get_default_hosted_provider(),
            external_id__in=pending.values("external_id"),
            user__is_active=True,
        ).values_list("external_id", "user_id")
    )
    if not identities:
        return
    resolved = list(pending.filter(external_id__in=identities))
    owners_by_customer: dict[Customer, set[int]] = defaultdict(set)
    for entry in resolved:
        user_id = identities[entry.external_id]
        entry.user_id = user_id
        owners_by_customer[entry.service.customer].add(user_id)
    for customer, user_ids in owners_by_customer.items():
        customer.owners.add(*user_ids)
    HostedBillingAdministrator.objects.bulk_update(resolved, ["user"])


def synchronize_hosted_owners(service: Service, external_ids: list[str]) -> None:
    administrators = service.hosted_administrators
    existing = set(administrators.values_list("external_id", flat=True))
    removed = administrators.exclude(external_id__in=external_ids)
    removed_ids = set(removed.exclude(user=None).values_list("user_id", flat=True))
    removed_ids.update(
        HostedIdentity.objects.filter(
            provider=get_default_hosted_provider(),
            external_id__in=removed.filter(user=None).values("external_id"),
        ).values_list("user_id", flat=True)
    )
    removed.delete()
    if removed_ids:
        retained = HostedBillingAdministrator.objects.filter(
            service__customer_id=service.customer_id, service__hosted_billing__gt=0
        )
        retained_ids = set(retained.values_list("user_id", flat=True))
        retained_ids.update(
            HostedIdentity.objects.filter(
                provider=get_default_hosted_provider(),
                external_id__in=retained.values("external_id"),
            ).values_list("user_id", flat=True)
        )
        service.customer.owners.remove(*(removed_ids - retained_ids))

    administrators.bulk_create(
        [
            HostedBillingAdministrator(service=service, external_id=external_id)
            for external_id in set(external_ids) - existing
        ]
    )
    pending = administrators.filter(user=None)
    identities = dict(
        HostedIdentity.objects.filter(
            provider=get_default_hosted_provider(),
            external_id__in=pending.values("external_id"),
            user__is_active=True,
        ).values_list("external_id", "user_id")
    )
    if identities:
        resolved = list(pending.filter(external_id__in=identities))
        for entry in resolved:
            entry.user_id = identities[entry.external_id]
        administrators.bulk_update(resolved, ["user"])
        service.customer.owners.add(*identities.values())


@transaction.atomic
def synchronize_hosted_service(snapshot: HostedSnapshot) -> tuple[int, int, datetime]:
    lock_hosted_sync()
    service = Service.objects.filter(hosted_billing=snapshot.billing).first()
    if (
        service is not None
        and service.hosted_snapshot_at
        and service.hosted_snapshot_at >= snapshot.timestamp
    ):
        return service.pk, service.customer_id, service.hosted_snapshot_at
    if service is None:
        service, customer_id = historical_service(
            snapshot.billing, snapshot.payment_ids
        )
        if customer_id is not None:
            Customer.objects.select_for_update().get(pk=customer_id)
        if service is None:
            if customer_id is None:
                customer_id = Customer.objects.create(
                    user_id=-1,
                    name=snapshot.metadata["customer_name"],
                    origin=HOSTED_ORIGIN,
                ).pk
            service = Service.objects.create(
                customer_id=customer_id, hosted_billing=snapshot.billing
            )
        else:
            linked = Service.objects.filter(
                pk=service.pk,
                customer_id=service.customer_id,
                hosted_billing__isnull=True,
            ).update(hosted_billing=snapshot.billing)
            if not linked:
                raise HostedAssociationConflict(
                    "service_conflict", service_ids=[service.pk]
                )
            service.hosted_billing = snapshot.billing
    else:
        validate_linked_payments(service.customer_id, snapshot.payment_ids)

    synchronize_hosted_owners(service, snapshot.administrators)
    service.hosted_snapshot = snapshot.metadata
    service.hosted_snapshot_at = snapshot.timestamp
    service.hosted_synced_at = timezone.now()
    service.site_title = str(snapshot.metadata["workspace_name"])
    service.site_version = snapshot.version
    service.site_projects = snapshot.usage["projects"]
    fields = [
        "hosted_snapshot",
        "hosted_snapshot_at",
        "hosted_synced_at",
        "site_title",
        "site_version",
        "site_projects",
    ]
    if not service.site_url_lock:
        service.site_url = HOSTED_URL
        fields.append("site_url")
    service.save(update_fields=fields)
    # Hosted usage must not trigger support URL, discovery, or dedicated follow-ups.
    Report.objects.bulk_create(
        [
            Report(
                service=service,
                timestamp=snapshot.timestamp,
                site_url=service.site_url,
                site_title=service.site_title,
                version=snapshot.version,
                **snapshot.usage,
            )
        ]
    )
    return service.pk, service.customer_id, snapshot.timestamp
