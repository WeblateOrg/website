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

from typing import TypedDict

from django.conf import settings
from django.core.management.base import BaseCommand
from zammad_py import ZammadAPI

from weblate_web.payments.models import Customer

HOSTED_ACCOUNT = "Hosted Weblate account"


class Organization(TypedDict, total=False):
    id: int
    name: str
    crm: str
    last_payment: str
    service_link: str
    premium_support: bool
    support: bool
    plan: str


class Command(BaseCommand):
    help = "synchronizes customer data to Zammad"
    client: ZammadAPI

    def handle(self, *args, **options) -> None:
        self.client = ZammadAPI(
            url="https://care.weblate.org/api/v1/",
            http_token=settings.ZAMMAD_TOKEN,
        )
        self.handle_hosted_account()
        self.handle_organizations()

    def handle_hosted_account(self) -> None:
        """Define link to search account on Hosted Weblate for all users."""
        self.client.user.per_page = 100  # type: ignore[union-attr]
        users = self.client.user.search(  # type: ignore[union-attr]
            {"query": f"!hosted_account:{HOSTED_ACCOUNT!r}", "limit": 100}
        )
        # We intentionally ignore pagination here as the sync is expected to run
        # regularly and fetch remaining ones in next run
        for user in users:
            self.client.user.update(  # type: ignore[union-attr]
                user["id"],
                {"hosted_account": HOSTED_ACCOUNT},
            )
            self.stdout.write(f"Updating {user['login']}")

    def handle_organizations(self) -> None:
        # Fetch all active customers
        customers: dict[int, Customer] = {
            customer.pk: customer
            for customer in Customer.objects.active().prefetch_related("service_set")
        }
        # Fetch all
        organizations: list[Organization] = list(self.client.organization.all())
        # Find existing maps
        mapped: set[int] = {
            int(crm_id)
            for crm_id in (organization.get("crm") for organization in organizations)
            if crm_id and crm_id.lower() != "none"
        }
        pending: set[int] = set(customers.keys()) - mapped

        # Try to map missing organizations
        for organization in organizations:
            if organization.get("crm"):
                continue
            name = organization["name"]
            match: int | None = None
            for customer_id in pending:
                customer = customers[customer_id]
                if name in customer.name or name in customer.end_client:
                    self.stdout.write(
                        f"Map {customer} to {organization['id']} ({organization['name']})"
                    )
                    match = customer.pk
                    break

            if match:
                organization["crm"] = str(match)
                pending.remove(match)

        # Create pending ones
        for pk in pending:
            customer = customers[pk]
            self.stdout.write(f"Create {customer}")

        # Update attributes
        for organization in organizations:
            crm_id = organization.get("crm")
            if not crm_id and crm_id.lower() != "none":
                self.stderr.write(
                    f"No match found for {organization['id']} ({organization['name']})"
                )
                continue
            customer = customers[int(crm_id)]
            services = customer.service_set.all()
            if len(services) != 1:
                self.stderr.write(
                    f"ERROR: Wrong services count for customer {customer} ({len(services)}"
                )
                continue
            service = services[0]
            subscription = service.latest_subscription
            if subscription is None:
                self.stderr.write(
                    f"ERROR: Missing subscription for customer {customer} ({service})"
                )
                continue
            data: Organization = {
                "last_payment": subscription.expires.date().isoformat(),
                "service_link": service.site_url,
                "premium_support": subscription.package.name == "premium",
                "support": not subscription.is_expired,
                "plan": subscription.package.verbose,
            }
            for name, value in data.items():
                if organization.get(name) != value:
                    organization[name] = value  # type: ignore[literal-required]
                    self.stdout.write(f"Updating {customer}: {name}={value}")
