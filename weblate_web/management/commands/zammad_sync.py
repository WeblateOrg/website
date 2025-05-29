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

from typing import TYPE_CHECKING, TypedDict

from django.conf import settings
from django.core.management.base import BaseCommand
from zammad_py import ZammadAPI

from weblate_web.payments.models import Customer

if TYPE_CHECKING:
    from weblate_web.models import Service, Subscription

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


class InvalidSubscriptionError(Exception):
    pass


class Command(BaseCommand):
    help = "synchronizes customer data to Zammad"
    client: ZammadAPI

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.customers: dict[int, Customer] = {}

    def handle(self, *args, **options) -> None:
        self.client = ZammadAPI(
            url="https://care.weblate.org/api/v1/",
            http_token=settings.ZAMMAD_TOKEN,
        )
        self.handle_hosted_account()
        self.handle_organizations()

    def handle_hosted_account(self) -> None:
        """Define link to search account on Hosted Weblate for all users."""
        users = self.client.user.search(f"!(hosted_account:{HOSTED_ACCOUNT!r})")
        # We intentionally ignore pagination here as the sync is expected to run
        # regularly and fetch remaining ones in next run
        for user in users:
            self.client.user.update(
                user["id"],
                {"hosted_account": HOSTED_ACCOUNT},
            )
            self.stdout.write(f"Updating user {user['login']}")

    def get_customer_service(self, customer: Customer) -> tuple[Service, Subscription]:
        services = customer.service_set.all()
        if len(services) != 1:
            self.stdout.write(
                f"WARNING: Wrong services count for customer {customer} ({len(services)})"
            )
            raise InvalidSubscriptionError
        service = services[0]
        subscription = service.latest_subscription
        if subscription is None:
            self.stdout.write(
                f"WARNING: Missing subscription for customer {customer} ({service})"
            )
            raise InvalidSubscriptionError
        return service, subscription

    def get_organization_subscription(
        self, service: Service, subscription: Subscription
    ) -> Organization:
        return {
            "last_payment": subscription.expires.date().isoformat(),
            "service_link": service.site_url,
            "premium_support": subscription.package.name == "premium",
            "support": not subscription.is_expired,
            "plan": subscription.package.verbose,
        }

    def fetch_customers(self) -> None:
        self.customers = {
            customer.pk: customer
            for customer in Customer.objects.active().prefetch_related("service_set")
        }

    def get_customer(self, pk: int) -> Customer:
        try:
            customer = self.customers[pk]
        except KeyError:
            customer = Customer.objects.get(pk=pk)
            self.stdout.write(f"WARNING: Fetched inactive customer {customer}")
            self.customers[customer.pk] = customer
        return customer

    def update_zammad_id(self, customer: Customer, zammad_id: int) -> None:
        if zammad_id and customer.zammad_id != zammad_id:
            customer.zammad_id = zammad_id
            self.stdout.write(f"Updating zammad_id for {customer}: {zammad_id}")
            customer.save(update_fields=["zammad_id"])

    def handle_organizations(self) -> None:  # noqa: PLR0915,C901
        # Fetch all active customers
        self.fetch_customers()
        # Fetch organizations all using pagination
        organizations: list[Organization] = []
        results = self.client.organization.all()
        while len(results):
            organizations.extend(results)
            results = results.next_page()
        # Find existing maps
        mapped: set[int] = set()
        for organization in organizations:
            crm_id = organization.get("crm")
            if crm_id and crm_id.lower() != "none":
                mapped.add(int(crm_id))
                # Ensure we have the customer and link it
                customer = self.get_customer(int(crm_id))
                self.update_zammad_id(customer, organization["id"])

        pending: set[int] = set(self.customers.keys()) - mapped

        # Try to map missing organizations
        for organization in organizations:
            if organization.get("crm"):
                continue
            name = organization["name"]
            match: int | None = None
            for customer_id in pending:
                customer = self.get_customer(customer_id)
                if name in customer.name or name in customer.end_client:
                    self.stdout.write(
                        f"Map {customer} to {organization['id']} ({organization['name']})"
                    )
                    match = customer.pk
                    self.update_zammad_id(customer, organization["id"])
                    break

            if match:
                organization["crm"] = str(match)
                self.client.organization.update(
                    organization["id"], {"crm": organization["crm"]}
                )
                pending.remove(match)

        # Create pending ones
        for pk in pending:
            customer = self.get_customer(pk)
            try:
                service, subscription = self.get_customer_service(customer)
            except InvalidSubscriptionError:
                continue
            organization = self.get_organization_subscription(service, subscription)
            organization["name"] = customer.end_client or customer.name
            if not organization["name"]:
                self.stderr.write(f"Customer has no name {customer}")
                continue
            organization["crm"] = str(customer.pk)
            self.stdout.write(f"Creating {organization}")
            organization = self.client.organization.create(organization)
            self.update_zammad_id(customer, organization["id"])

        # Update attributes
        for organization in organizations:
            crm_id = organization.get("crm")
            if not crm_id or crm_id.lower() == "none":
                self.stdout.write(
                    f"WARNING: No match found for {organization['id']} ({organization['name']})"
                )
                continue
            customer = self.get_customer(int(crm_id))
            try:
                service, subscription = self.get_customer_service(customer)
            except InvalidSubscriptionError:
                continue
            data: Organization = self.get_organization_subscription(
                service, subscription
            )
            for name, value in data.items():
                if organization.get(name) != value:
                    organization[name] = value  # type: ignore[literal-required]
                    self.stdout.write(f"Updating {customer}: {name}={value}")
                    self.client.organization.update(organization["id"], {name: value})
