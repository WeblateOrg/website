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

from typing import TYPE_CHECKING

from django.db.models import IntegerChoices
from django.utils.translation import pgettext_lazy

if TYPE_CHECKING:
    from collections.abc import Mapping


class ServiceCategory(IntegerChoices):
    UNCATEGORIZED = 0, pgettext_lazy("Service category", "Uncategorized")
    HOSTING = 1, pgettext_lazy("Service category", "Hosting")
    SUPPORT = 2, pgettext_lazy("Service category", "Support")
    DEVEL = 3, pgettext_lazy("Service category", "Development / Consultations")
    DONATE = 4, pgettext_lazy("Service category", "Donation")
    DEDICATED = 10, pgettext_lazy("Service category", "Dedicated hosting")
    SHARED = 20, pgettext_lazy("Service category", "Shared hosting")


def category_from_metadata(
    extra: object,
    packages: Mapping[str, int],
    subscriptions: Mapping[int, int],
) -> ServiceCategory | None:
    """Resolve transaction-specific packages before mutable subscriptions."""
    if not isinstance(extra, dict):
        return None
    package_name = extra.get("package", extra.get("subscription"))
    plan = extra.get("plan")
    billing = extra.get("billing")
    subscription = extra.get("subscription")
    category = None
    if isinstance(package_name, str):
        category = packages.get(package_name)
    elif "package" in extra or "subscription_upgrade" in extra:
        return None
    elif extra.get("category") == "donate" or any(
        key in extra for key in ("donation", "donation_service", "reward")
    ):
        category = ServiceCategory.DONATE
    elif isinstance(billing, int) and not isinstance(billing, bool) and billing > 0:
        # A null plan leaves the plan unchanged; billing identifies hosted payments.
        category = ServiceCategory.SHARED
    elif isinstance(plan, int) and not isinstance(plan, bool) and plan > 0:
        category = ServiceCategory.SHARED
    elif isinstance(plan, str):
        category = packages.get(plan)
    elif isinstance(subscription, int) and not isinstance(subscription, bool):
        category = subscriptions.get(subscription)
    return ServiceCategory(category) if category else None
