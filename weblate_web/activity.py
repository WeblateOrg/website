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

from typing import TYPE_CHECKING

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from weblate_web.models import PackageCategory, Service, ServiceActivity, ServiceKind
from weblate_web.payments.models import CustomerFollowUp

if TYPE_CHECKING:
    from datetime import date

    from weblate_web.models import Report


@transaction.atomic
def record_activity(report: Report, activity: dict[date, int]) -> None:
    """Merge reported months while serializing activity and episode updates."""
    service = Service.objects.select_for_update().get(pk=report.service_id)
    report.service = service
    if not activity or not report.is_valid_site_url():
        return
    for month, changes in activity.items():
        ServiceActivity.objects.update_or_create(
            service=service, month=month, defaults={"changes": changes}
        )
    if (
        service.kind != ServiceKind.SERVICE
        or not service.subscription_set.filter(
            Q(
                package__category__in=[
                    PackageCategory.PACKAGE_DEDICATED,
                    PackageCategory.PACKAGE_SHARED,
                    PackageCategory.PACKAGE_SUPPORT,
                ]
            )
            | Q(package__name="backup"),
            enabled=True,
            expires__gte=timezone.now(),
        ).exists()
    ):
        return
    reconcile_activity_drop(report)


def reconcile_activity_drop(report: Report) -> None:
    """Open one follow-up per sustained drop, until actual activity recovers."""
    service = report.service
    latest_month = timezone.localdate().replace(day=1) - relativedelta(months=1)
    months = [
        latest_month - relativedelta(months=offset) for offset in range(4, -1, -1)
    ]
    counts = dict(
        service.activity.filter(month__in=months).values_list("month", "changes")
    )
    if latest_month not in counts:
        return
    state = service.activity_drop_state
    followups = service.followups.filter(type=CustomerFollowUp.Type.ACTIVITY_DROP)
    if "baseline_sum" in state:
        # Keep the original baseline: a shrinking rolling average is not recovery.
        recovery_month = (
            service.activity.filter(
                month__gt=state["detected_month"],
                month__lte=latest_month,
                changes__gt=state["baseline_sum"] // 6,
            )
            .order_by("-month")
            .values_list("month", flat=True)
            .first()
        )
        if recovery_month is not None:
            followups.delete()
            state = service.activity_drop_state = {
                "recovered_month": recovery_month.isoformat()
            }
            service.save(update_fields=["activity_drop_state"])
        else:
            followups.update(
                details={
                    **state,
                    "report_id": report.pk,
                    "latest_month": latest_month.isoformat(),
                    "latest_changes": counts[latest_month],
                }
            )
            return
    if latest_month.isoformat() <= state.get("recovered_month", "") or len(counts) != 5:
        return
    baseline_sum = sum(counts[month] for month in months[:3])
    if baseline_sum < 300 or any(
        counts[month] * 6 > baseline_sum for month in months[-2:]
    ):
        return
    details = {
        "service_id": service.pk,
        "report_id": report.pk,
        "site_url": report.site_url,
        "detected_month": latest_month.isoformat(),
        "baseline_sum": baseline_sum,
        "baseline_average": baseline_sum / 3,
        "baseline_months": [
            {"month": month.isoformat(), "changes": counts[month]}
            for month in months[:3]
        ],
        "low_months": [
            {
                "month": month.isoformat(),
                "changes": counts[month],
                "decrease_percent": 100
                * (baseline_sum - 3 * counts[month])
                / baseline_sum,
            }
            for month in months[-2:]
        ],
        "latest_month": latest_month.isoformat(),
        "latest_changes": counts[latest_month],
    }
    CustomerFollowUp.objects.get_or_create(
        service=service,
        type=CustomerFollowUp.Type.ACTIVITY_DROP,
        defaults={
            "customer": service.customer,
            "follow_up_at": timezone.now(),
            "details": details,
        },
    )
    service.activity_drop_state = details
    service.save(update_fields=["activity_drop_state"])
