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

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.db import migrations, models

PAYMENT_QUANTUM = Decimal("0.01")
EU_COUNTRIES = {
    "AT",
    "BE",
    "BG",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "ES",
    "FI",
    "FR",
    "GR",
    "HR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
}


def round_money(amount):
    return Decimal(amount).quantize(PAYMENT_QUANTUM, rounding=ROUND_HALF_UP)


def round_legacy(amount, max_decimals=3):
    if amount % Decimal("0.01"):
        return round(amount, max_decimals)
    if not amount % Decimal(1):
        return round(amount, 0)
    if not amount % Decimal("0.1"):
        return round(amount, 1)
    return round(amount, 2)


def get_gross(tax_basis, vat_rate):
    vat = round_money(tax_basis * Decimal(vat_rate) / Decimal(100))
    return tax_basis + vat


def get_compliant_amount(requested_amount, vat_rate):
    requested = round_money(requested_amount)
    tax_basis = (requested * Decimal(100) / Decimal(100 + vat_rate)).quantize(
        PAYMENT_QUANTUM, rounding=ROUND_FLOOR
    )
    while get_gross(tax_basis, vat_rate) > requested:
        tax_basis -= PAYMENT_QUANTUM
    while get_gross(tax_basis + PAYMENT_QUANTUM, vat_rate) <= requested:
        tax_basis += PAYMENT_QUANTUM
    return get_gross(tax_basis, vat_rate)


def customer_needs_vat(customer):
    country = str(customer.country or "").upper()
    vat = str(customer.vat or "").upper()
    vat_country = vat[:2]
    return not country or vat_country == "CZ" or (country in EU_COUNTRIES and not vat)


def get_invoice_total(invoice):
    if invoice.kind != 0:
        total = sum(
            (item.unit_price * item.quantity for item in invoice.invoiceitem_set.all()),
            start=Decimal(0),
        )
        positive = sum(
            (
                item.unit_price * item.quantity
                for item in invoice.invoiceitem_set.all()
                if item.unit_price > 0
            ),
            start=Decimal(0),
        )
        if invoice.discount_id:
            total -= round(positive * invoice.discount.percents / Decimal(100), 0)
        tax_basis = round_legacy(total)
        vat = round_legacy(tax_basis * invoice.vat_rate / Decimal(100))
        return round_legacy(tax_basis + vat, max_decimals=2)

    positive = Decimal("0.00")
    allowances = Decimal("0.00")
    for item in invoice.invoiceitem_set.all():
        total = round_money(item.unit_price * item.quantity)
        if total >= 0:
            positive += total
        else:
            allowances -= total
    if invoice.discount_id:
        allowances += round_money(positive * invoice.discount.percents / Decimal(100))
    tax_basis = round_money(positive - allowances)
    vat = round_money(tax_basis * invoice.vat_rate / Decimal(100))
    return tax_basis + vat


def populate_requested_amount(apps, _schema_editor):
    Payment = apps.get_model("payments", "Payment")
    payments = Payment.objects.filter(amount_fixed=True).select_related(
        "customer", "draft_invoice__discount"
    )
    for payment in payments.iterator():
        requested = round_money(payment.amount)
        amount = requested
        if payment.state == 1:
            if payment.draft_invoice_id:
                amount = requested = get_invoice_total(payment.draft_invoice)
            elif payment.currency != 1 and customer_needs_vat(payment.customer):
                amount = get_compliant_amount(requested, 21)
        payment.amount = amount
        payment.requested_amount = requested
        payment.save(update_fields=("amount", "requested_amount"))


class Migration(migrations.Migration):
    dependencies = [("payments", "0003_rename_customer_users_owners")]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="amount",
            field=models.DecimalField(decimal_places=2, max_digits=12),
        ),
        migrations.AddField(
            model_name="payment",
            name="requested_amount",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True
            ),
        ),
        migrations.RunPython(populate_requested_amount, migrations.RunPython.noop),
    ]
