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

from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext

from weblate_web.crm.models import Interaction
from weblate_web.payments.models import Payment

from .models import Invoice, InvoiceCalculationVersion, InvoiceKind, round_money

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

    from django.contrib.auth.models import User

CORRECTION_PERMISSION = "invoices.manage_invoice_corrections"


def is_settled(invoice: Invoice) -> bool:
    return (
        invoice.prepaid
        or invoice.paid_payment_set.filter(
            state__in=[Payment.ACCEPTED, Payment.PROCESSED]
        ).exists()
    )


def validate_duplicate(
    invoice: Invoice, duplicate: Invoice | None, amount: Decimal, *, paid: bool
) -> None:
    message = gettext(
        "Select the paid invoice for the same customer and correct the full remaining amount."
    )
    if paid or duplicate is None or amount != invoice.remaining_amount:
        raise ValidationError(message)
    if duplicate.pk == invoice.pk or duplicate.customer_id != invoice.customer_id:
        raise ValidationError(message)
    if (
        duplicate.kind != InvoiceKind.INVOICE
        or duplicate.correction_of_id
        or duplicate.is_credited
        or duplicate.total_amount <= 0
        or not is_settled(duplicate)
    ):
        raise ValidationError(message)


def validate_legacy_rounding(
    invoice: Invoice,
    *,
    net: Decimal,
    vat: Decimal,
    amount: Decimal,
    exchange_rate: Decimal,
) -> None:
    """Allow legacy documents only when current rounding preserves their amounts."""
    if invoice.calculation_version != InvoiceCalculationVersion.LEGACY:
        return
    amounts = (
        invoice.total_amount_no_vat,
        invoice.total_vat,
        invoice.total_amount,
        net,
        vat,
        -amount,
    )
    if any(
        value != round_money(value)
        or round(value * exchange_rate, 2) != round_money(value * exchange_rate)
        for value in amounts
    ):
        raise ValidationError(
            gettext(
                "This legacy invoice cannot be corrected because its rounding "
                "differs from current invoice calculations."
            )
        )


@transaction.atomic
def issue_correction(  # ruff:ignore[too-many-arguments]
    *,
    invoice: Invoice,
    user: User,
    amount: Decimal,
    reason: str,
    note: str,
    issue_date: date,
    tax_date: date,
    token: UUID,
    duplicate: Invoice | None = None,
) -> Invoice:
    """Issue one immutable correction, serializing all corrections of a source."""
    if not user.has_perm(CORRECTION_PERMISSION):
        raise PermissionDenied
    locked = {
        item.pk: item
        for item in Invoice.objects.select_for_update()
        .filter(pk__in=(invoice.pk, duplicate.pk) if duplicate else (invoice.pk,))
        .order_by("pk")
    }
    invoice = locked[invoice.pk]
    if duplicate is not None:
        duplicate = locked.get(duplicate.pk)
    if existing := Invoice.objects.filter(correction_token=token).first():
        if existing.correction_of_id != invoice.pk:
            raise ValidationError(gettext("This request has already been used."))
        return existing
    if (
        invoice.kind != InvoiceKind.INVOICE
        or invoice.correction_of_id
        or amount <= 0
        or amount != round_money(amount)
        or amount > invoice.remaining_amount
    ):
        raise ValidationError(
            gettext("Choose a positive amount within the remaining invoice amount.")
        )
    if reason not in {"price", "duplicate"} or (reason == "price" and not note.strip()):
        raise ValidationError(
            gettext("A correction reason and explanation are required.")
        )
    if issue_date < invoice.issue_date or tax_date < invoice.tax_date:
        raise ValidationError(
            gettext("Correction dates cannot precede the original invoice dates.")
        )
    if (
        invoice.draft_payment_set.filter(
            state__in=[Payment.NEW, Payment.PENDING, Payment.REJECTED]
        ).exists()
        or invoice.paid_payment_set.filter(
            state__in=[Payment.NEW, Payment.PENDING, Payment.REJECTED]
        ).exists()
    ):
        raise ValidationError(
            gettext("Resolve the pending payment before issuing a correction.")
        )
    paid = is_settled(invoice)
    if reason == "duplicate":
        validate_duplicate(invoice, duplicate, amount, paid=paid)
        if not note.strip() and duplicate is not None:
            note = f"Duplicate of invoice {duplicate.number}."
    elif not paid:
        raise ValidationError(gettext("Only paid invoices can be refunded."))
    elif duplicate is not None:
        raise ValidationError(
            gettext("Only duplicate corrections can reference a replacement invoice.")
        )

    previous = list(invoice.corrections.all())
    # Allocate cumulatively so many small refunds cannot accumulate tax drift.
    proportion = (invoice.credited_amount + amount) / invoice.total_amount
    net = -round_money(invoice.total_amount_no_vat * proportion) - sum(
        (item.correction_net for item in previous), Decimal(0)
    )
    vat = -round_money(invoice.total_vat * proportion) - sum(
        (item.correction_vat for item in previous), Decimal(0)
    )
    exchange_rate = invoice.exchange_rate_czk
    validate_legacy_rounding(
        invoice, net=net, vat=vat, amount=amount, exchange_rate=exchange_rate
    )
    if abs(net) > Decimal("99999.99"):
        raise ValidationError(
            gettext("The correction amount excluding VAT cannot exceed 99,999.99.")
        )
    correction = Invoice(
        kind=InvoiceKind.INVOICE,
        category=invoice.category,
        customer=invoice.customer,
        currency=invoice.currency,
        vat_rate=invoice.vat_rate,
        issue_date=issue_date,
        tax_date=tax_date,
        correction_of=invoice,
        correction_token=token,
        correction_reason=reason,
        correction_note=note.strip(),
        duplicate_of=duplicate,
        correction_net=net,
        correction_vat=vat,
        correction_rounding=-amount - net - vat,
        prepaid=not paid,
        customer_reference=invoice.customer_reference,
        extra={
            "correction": {
                "exchange_rate_czk": str(exchange_rate),
            }
        },
    )
    correction.save(force_insert=True)
    correction.invoiceitem_set.create(
        description=f"Correction of invoice {invoice.number}",
        unit_price=net,
    )
    invoice.credited_amount += amount
    invoice.fully_credited = invoice.remaining_amount <= 0
    invoice.save(update_fields=["credited_amount", "fully_credited"])
    invoice.customer.interaction_set.create(
        origin=Interaction.Origin.INVOICE_CORRECTION,
        summary=f"Credit note {correction.number} for invoice {invoice.number}",
        content=note.strip(),
        user=user,
        details={
            "invoice": invoice.number,
            "correction": correction.number,
            "reason": reason,
            "amount": str(amount),
            "duplicate_of": duplicate.number if duplicate else None,
        },
    )
    correction.generate_files()
    return correction


@transaction.atomic
def set_uncollectible(*, invoice: Invoice, user: User, note: str, value: bool) -> None:
    if not user.has_perm(CORRECTION_PERMISSION):
        raise PermissionDenied
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if (
        invoice.kind != InvoiceKind.INVOICE
        or invoice.correction_of_id
        or invoice.total_amount <= 0
    ):
        raise ValidationError(
            gettext("Only positive final invoices can be marked uncollectible.")
        )
    if value and (is_settled(invoice) or invoice.is_credited):
        raise ValidationError(gettext("This invoice has already been settled."))
    if not note.strip():
        raise ValidationError(gettext("An internal explanation is required."))
    if invoice.uncollectible == value:
        return
    previous_note = invoice.uncollectible_note
    invoice.uncollectible = value
    invoice.uncollectible_note = note.strip()
    invoice.save(update_fields=["uncollectible", "uncollectible_note"])
    invoice.customer.interaction_set.create(
        origin=Interaction.Origin.INVOICE_CORRECTION,
        summary=f"Invoice {invoice.number}: {'uncollectible' if value else 'collection resumed'}",
        content=note.strip(),
        user=user,
        details={
            "invoice": invoice.number,
            "uncollectible": value,
            "previous_note": previous_note,
        },
    )
