# Copyright (C) Michal Cihar <michal@weblate.org>
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

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from weblate_web.invoices.models import Invoice, InvoiceKind, QuoteStatus
from weblate_web.models import Subscription
from weblate_web.payments.models import Customer, Payment

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.db.models import QuerySet


UNPAID_INVOICE_FOLLOW_UP_DAYS = 7
STALE_QUOTE_FOLLOW_UP_DAYS = 14
DASHBOARD_WORK_QUEUE_LIMIT = 10

WORK_QUEUE_GROUP_FOLLOWUPS = "followups"
WORK_QUEUE_GROUP_BILLING = "billing"
WORK_QUEUE_GROUP_SERVICES = "services"
WORK_QUEUE_GROUP_ORDER = (
    WORK_QUEUE_GROUP_FOLLOWUPS,
    WORK_QUEUE_GROUP_BILLING,
    WORK_QUEUE_GROUP_SERVICES,
)


@dataclass(frozen=True)
class CRMWorkItem:
    group: str
    label: str
    title: str
    summary: str
    url: str
    date: date | datetime
    severity: int
    attention: bool = True

    @property
    def sort_key(self) -> tuple[int, datetime, str]:
        return (self.severity, sort_datetime(self.date), self.title)


@dataclass(frozen=True)
class CRMWorkQueueSection:
    key: str
    title: str
    items: list[CRMWorkItem]


def sort_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return timezone.make_aware(datetime.combine(value, time.min))


def get_unpaid_invoice_queryset() -> QuerySet[Invoice]:
    return (
        Invoice.objects.exclude(paid_payment_set__state=Payment.PROCESSED)
        .filter(
            Q(paid_payment_set__state__in={Payment.NEW, Payment.PENDING})
            | Q(paid_payment_set=None),
            kind=InvoiceKind.INVOICE,
        )
        .distinct()
    )


def get_stale_quote_queryset() -> QuerySet[Invoice]:
    cutoff = timezone.localdate() - timedelta(days=STALE_QUOTE_FOLLOW_UP_DAYS)
    converted_quotes = Invoice.objects.filter(parent__isnull=False).values("parent_id")
    return (
        Invoice.objects.filter(
            kind=InvoiceKind.QUOTE,
            issue_date__lte=cutoff,
            quote_status=QuoteStatus.OPEN,
        )
        .exclude(pk__in=converted_quotes)
        .order_by("issue_date", "number")
    )


def get_expired_service_subscriptions() -> list[Subscription]:
    possible_subscriptions = (
        Subscription.objects.customer_services()
        .filter(expires__lte=timezone.now(), enabled=True)
        .exclude(payment=None)
        .select_related("package", "service", "service__customer")
        .order_by("expires", "service__customer__name", "pk")
    )
    subscriptions = []
    for subscription in possible_subscriptions:
        if not subscription.package.get_repeat():
            continue
        if subscription.could_be_obsolete():
            continue
        subscriptions.append(subscription)
    return subscriptions


def get_expired_service_ids() -> list[int]:
    return [
        subscription.service_id for subscription in get_expired_service_subscriptions()
    ]


def get_crm_work_items(user: User) -> list[CRMWorkItem]:
    items: list[CRMWorkItem] = []
    if user.has_perm("payments.view_customer"):
        items.extend(get_customer_follow_up_items())
    if user.has_perm("invoices.view_invoice"):
        items.extend(get_invoice_follow_up_items())
        items.extend(get_quote_follow_up_items())
    if user.has_perm("weblate_web.change_service"):
        items.extend(get_service_follow_up_items())
    return sorted(items, key=lambda item: item.sort_key)


def get_crm_work_queue_sections(user: User) -> list[CRMWorkQueueSection]:
    grouped_items: dict[str, list[CRMWorkItem]] = {
        key: [] for key in WORK_QUEUE_GROUP_ORDER
    }
    for item in get_crm_work_items(user):
        grouped_items[item.group].append(item)

    section_titles = {
        WORK_QUEUE_GROUP_FOLLOWUPS: _("Follow-ups"),
        WORK_QUEUE_GROUP_BILLING: _("Billing"),
        WORK_QUEUE_GROUP_SERVICES: _("Services"),
    }
    return [
        CRMWorkQueueSection(key=key, title=section_titles[key], items=items)
        for key, items in grouped_items.items()
        if items
    ]


def get_customer_follow_up_items() -> list[CRMWorkItem]:
    items = []
    for customer in Customer.objects.due_followups():
        follow_up_at = customer.follow_up_at
        if follow_up_at is None:
            continue
        items.append(
            CRMWorkItem(
                group=WORK_QUEUE_GROUP_FOLLOWUPS,
                label=_("Manual follow-up"),
                title=customer.verbose_name,
                summary=customer.follow_up_note or _("Customer follow-up is due."),
                url=customer.get_absolute_url(),
                date=follow_up_at,
                severity=10,
            )
        )
    for customer in Customer.objects.upcoming_followups():
        follow_up_at = customer.follow_up_at
        if follow_up_at is None:
            continue
        items.append(
            CRMWorkItem(
                group=WORK_QUEUE_GROUP_FOLLOWUPS,
                label=_("Upcoming follow-up"),
                title=customer.verbose_name,
                summary=customer.follow_up_note or _("Scheduled customer follow-up."),
                url=customer.get_absolute_url(),
                date=follow_up_at,
                severity=50,
                attention=False,
            )
        )
    return items


def get_invoice_follow_up_items() -> list[CRMWorkItem]:
    cutoff = timezone.localdate() - timedelta(days=UNPAID_INVOICE_FOLLOW_UP_DAYS)
    return [
        CRMWorkItem(
            group=WORK_QUEUE_GROUP_BILLING,
            label=_("Unpaid invoice"),
            title=_("Invoice %(number)s") % {"number": invoice.number},
            summary=invoice.customer.verbose_name,
            url=invoice.get_absolute_url(),
            date=invoice.issue_date,
            severity=20,
        )
        for invoice in get_unpaid_invoice_queryset()
        .filter(issue_date__lte=cutoff)
        .select_related("customer")
        .order_by("issue_date", "number")
    ]


def get_quote_follow_up_items() -> list[CRMWorkItem]:
    return [
        CRMWorkItem(
            group=WORK_QUEUE_GROUP_BILLING,
            label=_("Stale quote"),
            title=_("Quote %(number)s") % {"number": quote.number},
            summary=quote.customer.verbose_name,
            url=quote.get_absolute_url(),
            date=quote.issue_date,
            severity=40,
        )
        for quote in get_stale_quote_queryset().select_related("customer")
    ]


def get_service_follow_up_items() -> list[CRMWorkItem]:
    items = []
    seen_services: set[int] = set()
    for subscription in get_expired_service_subscriptions():
        if subscription.service_id in seen_services:
            continue
        seen_services.add(subscription.service_id)
        service = subscription.service
        items.append(
            CRMWorkItem(
                group=WORK_QUEUE_GROUP_SERVICES,
                label=_("Expired service"),
                title=service.customer.verbose_name,
                summary=service.site_title,
                url=service.get_absolute_url(),
                date=subscription.expires,
                severity=30,
            )
        )
    return items
