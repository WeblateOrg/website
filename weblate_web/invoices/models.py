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

import datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Cast, Concat, Extract, LPad
from django.template.loader import render_to_string
from django.utils.functional import cached_property
from django.utils.translation import override
from fakturace.rates import DecimalRates
from weasyprint import HTML

INVOICES_URL = "invoices:"
TEMPLATES_PATH = Path(__file__).parent / "templates"


def url_fetcher(url: str) -> dict[str, str | bytes]:
    if not url.startswith(INVOICES_URL):
        raise ValueError(f"Usupported URL: {url}")
    filename = url.removeprefix(INVOICES_URL)
    result = {
        "filename": filename,
        "string": (TEMPLATES_PATH / filename).read_bytes(),
    }
    if filename.endswith("css"):
        result["mime_type"] = "text/css"
        result["encoding"] = "utf-8"
    return result


class QuantityUnitChoices(models.IntegerChoices):
    BLANK = 0, ""
    HOURS = 1, "hours"


class CurrencyChoices(models.IntegerChoices):
    EUR = 0, "EUR"


class InvoiceKindChoices(models.IntegerChoices):
    INVOICE = 10, "Invoice"
    PROFORMA = 20, "Proforma"
    QUOTE = 30, "Quote"
    DRAFT = 40, "Draft"


class Discount(models.Model):
    description = models.CharField(max_length=200, unique=True)
    percents = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(99)]
    )

    def __str__(self) -> str:
        return f"{self.description}: {self.display_percents}"

    @property
    def display_percents(self) -> str:
        return f"{self.percents}%"


class Invoice(models.Model):
    sequence = models.IntegerField(editable=False)
    number = models.GeneratedField(
        expression=Concat(
            Cast("kind", models.CharField()),
            Cast(Extract("issue_date", "year") % 2000, models.CharField()),
            LPad(Cast("sequence", models.CharField()), 6, models.Value("0")),
        ),
        output_field=models.CharField(max_length=20),
        db_persist=True,
    )
    issue_date = models.DateField(default=datetime.date.today)
    due_date = models.DateField(blank=True)
    kind = models.IntegerField(choices=InvoiceKindChoices)
    customer = models.ForeignKey("payments.Customer", on_delete=models.deletion.PROTECT)
    customer_reference = models.CharField(max_length=100, blank=True)
    discount = models.ForeignKey(
        Discount, on_delete=models.deletion.PROTECT, blank=True, null=True
    )
    vat_rate = models.IntegerField(default=0)
    currency = models.IntegerField(choices=CurrencyChoices, default=CurrencyChoices.EUR)

    # Invoice chaining Proforma -> Invoice, or Draft -> Invoice
    parent = models.ForeignKey(
        "Invoice", on_delete=models.deletion.PROTECT, blank=True, null=True
    )

    prepaid = models.BooleanField(
        default=False, help_text="Invoices paid in advance (card payment, pro forma)"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Extract("issue_date", "year"), "kind", "sequence", name="unique_number"
            )
        ]

    def __str__(self) -> str:
        return f"{self.number}: {self.customer} {self.total_amount}"

    def save(
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using=None,
        update_fields=None,
    ):
        extra_fields: list[str] = []
        if not self.due_date:
            self.due_date = self.issue_date + datetime.timedelta(days=14)
            extra_fields.append("due_date")
        if not self.sequence:
            try:
                self.sequence = (
                    Invoice.objects.filter(
                        kind=self.kind, issue_date__year=self.issue_date.year
                    )
                    .order_by("-id")[0]
                    .sequence
                    + 1
                )
            except IndexError:
                self.sequence = 1
            extra_fields.append("sequence")
        if extra_fields and update_fields is not None:
            update_fields = tuple(set(update_fields).union(extra_fields))

        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def render_amount(self, amount: int | Decimal) -> str:
        if self.currency == CurrencyChoices.EUR:
            return f"€{amount}"
        return f"{amount} {self.get_currency_display()}"

    @cached_property
    def exchange_rate_czk(self) -> Decimal:
        return DecimalRates.get(
            self.issue_date.isoformat(), self.get_currency_display()
        )

    @cached_property
    def total_items_amount(self) -> Decimal:
        return sum(
            (item.unit_price * item.quantity for item in self.all_items),
            start=Decimal(0),
        )

    @cached_property
    def total_discount(self) -> Decimal:
        if not self.discount:
            return Decimal(0)
        return -self.total_items_amount * self.discount.percents / 100

    @property
    def display_total_discount(self) -> str:
        return self.render_amount(self.total_discount)

    @cached_property
    def total_amount_no_vat(self) -> Decimal:
        return self.total_items_amount + self.total_discount

    @property
    def total_amount_no_vat_czk(self) -> Decimal:
        return round(self.total_amount_no_vat * self.exchange_rate_czk, 2)

    @property
    def display_total_amount_no_vat(self) -> str:
        return self.render_amount(self.total_amount_no_vat)

    @cached_property
    def total_vat(self) -> Decimal:
        if not self.vat_rate:
            return Decimal(0)
        return self.total_amount_no_vat * self.vat_rate / 100

    @property
    def total_vat_czk(self) -> Decimal:
        return round(self.total_vat * self.exchange_rate_czk, 2)

    @property
    def display_total_vat(self) -> str:
        return self.render_amount(self.total_vat)

    @cached_property
    def total_amount(self) -> Decimal:
        return self.total_amount_no_vat + self.total_vat

    @property
    def total_amount_czk(self) -> Decimal:
        return round(self.total_amount * self.exchange_rate_czk, 2)

    @property
    def display_total_amount(self) -> str:
        return self.render_amount(self.total_amount)

    @cached_property
    def all_items(self) -> models.QuerySet[InvoiceItem]:
        return self.invoiceitem_set.order_by("id")

    def render_html(self) -> str:
        with override("en_GB"):
            return render_to_string(
                "invoice-template.html",
                {
                    "invoice": self,
                },
            )

    @property
    def filename(self) -> str:
        return f"Weblate {self.get_kind_display()} {self.number}.pdf"

    def generate_pdf(self):
        # Create directory to store invoices
        settings.INVOICES_PATH.mkdir(exist_ok=True)

        renderer = HTML(string=self.render_html(), url_fetcher=url_fetcher)
        renderer.write_pdf(settings.INVOICES_PATH / self.filename)


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.deletion.CASCADE)
    description = models.CharField(max_length=200)
    quantity = models.IntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(50)]
    )
    quantity_unit = models.IntegerField(
        choices=QuantityUnitChoices, default=QuantityUnitChoices.BLANK
    )
    unit_price = models.DecimalField(decimal_places=2, max_digits=7)

    def __str__(self) -> str:
        return f"{self.description} ({self.display_quantity}) {self.display_price}"

    @property
    def display_price(self):
        return self.invoice.render_amount(self.unit_price)

    @property
    def display_total_price(self):
        return self.invoice.render_amount(self.unit_price * self.quantity)

    def get_quantity_unit_display(self) -> str:
        # Correcly handle singulars
        if self.quantity_unit == QuantityUnitChoices.HOURS and self.quantity == 1:
            return "hour"
        # This is what original get_quantity_unit_display() would have done
        return self._get_FIELD_display(field=self._meta.get_field("quantity_unit"))

    @property
    def display_quantity(self):
        if self.quantity_unit:
            return f"{self.quantity} {self.get_quantity_unit_display()}"
        return f"{self.quantity}"
