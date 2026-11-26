# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.utils import timezone

from weblate_web.discovery import process_discovery_verification
from weblate_web.models import DiscoveryVerification


class Command(BaseCommand):
    help = "Verify pending Discover Weblate listings"

    def handle(self, *args: object, **options: object) -> None:
        pending = DiscoveryVerification.objects.filter(
            next_attempt__lte=timezone.now()
        ).select_related("service", "report")
        outcomes: Counter[str] = Counter(
            process_discovery_verification(verification)
            for verification in pending.order_by("next_attempt", "pk")[:25]
        )
        if outcomes:
            self.stdout.write(
                "Discover verification: "
                + ", ".join(
                    f"{outcomes[outcome]} {outcome}"
                    for outcome in ("verified", "retry", "discarded", "superseded")
                )
            )
