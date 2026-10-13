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
from django.db.models import F
from django.utils import timezone
from django.utils.translation import override
from requests.exceptions import HTTPError

from weblate_web.crm.models import Interaction
from weblate_web.models import Subscription
from weblate_web.zammad import (
    create_invoice_request_article,
    create_invoice_request_ticket,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import User


def submit_invoice_request(
    subscription: Subscription, user: User, reference: str
) -> str:
    """Reserve a renewal request before contacting care; keep retries on one ticket."""
    with transaction.atomic():
        # A no-op write also serializes reservations on SQLite, where
        # select_for_update() does not acquire a lock.
        Subscription.objects.filter(pk=subscription.pk).update(expires=F("expires"))
        subscription = Subscription.objects.select_related(
            "service__customer", "package"
        ).get(pk=subscription.pk, service__customer__owners=user)
        interaction = (
            Interaction.objects.filter(
                origin=Interaction.Origin.INVOICE_REQUEST,
                customer=subscription.service.customer,
                details__subscription_id=subscription.pk,
                timestamp__gte=timezone.now() - timedelta(hours=24),
            )
            .order_by("-timestamp", "-pk")
            .first()
        )
        if interaction is not None:
            status = interaction.details["status"]
            if status != "failed":
                return "existing" if status == "submitted" else status
        else:
            with override("en"):
                invoices = ", ".join(
                    invoice.number for invoice in subscription.get_renewal_invoices()
                )
                content = (
                    f"Please issue an invoice for renewal of {subscription.package.verbose}.\n"
                    f"Service: {subscription.service} (ID {subscription.service_id})\n"
                    f"Subscription: {subscription.pk}\n"
                    f"Billing customer: {subscription.service.customer.verbose_name}\n"
                    f"Billing interval: {'Monthly' if subscription.package.get_repeat() == 'm' else 'Annual'}\n"
                    f"Expires: {subscription.expires.isoformat()}\n"
                    f"Customer reference: {reference or '(not set)'}\n"
                    f"Existing renewal invoices: {invoices or '(none)'}"
                )
            interaction = Interaction.objects.create(
                origin=Interaction.Origin.INVOICE_REQUEST,
                customer=subscription.service.customer,
                user=user,
                summary="renewal-invoice-request",
                content=content,
                details={
                    "service_id": subscription.service_id,
                    "subscription_id": subscription.pk,
                    "package": subscription.package.verbose,
                    "customer_reference": reference,
                    "requester_email": user.email,
                },
            )
        interaction.details["status"] = "pending"
        interaction.save(update_fields=["details"])

    try:
        _send_invoice_request(interaction)
    except Exception as error:
        # Only a definite rejection permits retry; timeouts can have remote effects.
        definite = (
            isinstance(error, HTTPError)
            and error.response is not None
            and 400 <= error.response.status_code < 500
            and error.response.status_code != 408
        )
        interaction.details["status"] = "failed" if definite else "uncertain"
        interaction.save(update_fields=["details"])
        raise
    interaction.details["status"] = "submitted"
    interaction.save(update_fields=["details"])
    return "submitted"


def _send_invoice_request(interaction: Interaction) -> None:
    if not interaction.remote_id:
        ticket = create_invoice_request_ticket(interaction)
        interaction.remote_id = int(ticket["id"])
        interaction.details["ticket_number"] = str(ticket["number"])
        interaction.save(update_fields=["remote_id", "details"])
    create_invoice_request_article(interaction)
