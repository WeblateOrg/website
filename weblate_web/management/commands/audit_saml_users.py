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

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.db.models.functions import Lower

from weblate_web.crm.models import Interaction
from weblate_web.models import Service
from weblate_web.payments.models import Customer


class Command(BaseCommand):
    help = "audits users before switching SAML to persistent hosted IDs"

    def handle(self, *args, **options) -> None:
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
            users = User.objects.filter(email__iexact=email).order_by("id")
            for user in users:
                customers = Customer.objects.filter(users=user)
                customer_ids = list(customers.values_list("id", flat=True))
                service_count = Service.objects.filter(customer__in=customers).count()
                interaction_count = Interaction.objects.filter(user=user).count()
                identities = [
                    f"{identity.provider}:{identity.external_id}"
                    for identity in user.saml_identities.all()
                ]
                self.stdout.write(
                    "  "
                    f"id={user.id} username={user.username!r} "
                    f"last_login={user.last_login} "
                    f"customers={customer_ids} services={service_count} "
                    f"crm_interactions={interaction_count} "
                    f"saml_identities={identities}"
                )
