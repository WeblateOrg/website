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
import uuid
from decimal import Decimal
from pathlib import Path
from shutil import copyfile
from typing import TYPE_CHECKING, Literal, cast

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Cast, Concat, Extract, LPad
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
from django.utils.timezone import now
from django.utils.translation import gettext, override
from lxml import etree

from weblate_web.const import (
    COMPANY_ADDRESS,
    COMPANY_CITY,
    COMPANY_COUNTRY,
    COMPANY_ID,
    COMPANY_NAME,
    COMPANY_VAT_ID,
    COMPANY_ZIP,
)
from weblate_web.exchange_rates import ExchangeRates
from weblate_web.pdf import render_pdf
from weblate_web.utils import get_site_url

if TYPE_CHECKING:
    from collections.abc import Generator

    from django_stubs_ext import StrOrPromise

    from weblate_web.models import Package
    from weblate_web.payments.models import Payment

INVOICES_URL = "invoices:"
STATIC_URL = "static:"
TEMPLATES_PATH = Path(__file__).parent / "templates"


def date_format(value: datetime.datetime | datetime.date) -> str:
    return value.strftime("%-d %b %Y")


def round_decimal(num: Decimal, max_decimals: int = 3) -> Decimal:
    if num % Decimal("0.01"):
        return round(num, max_decimals)
    if not num % Decimal(1):
        return round(num, 0)
    if not num % Decimal("0.1"):
        return round(num, 1)
    return round(num, 2)


def url_fetcher(url: str) -> dict[str, str | bytes]:
    path_obj: Path
    result: dict[str, str | bytes]
    if url.startswith(INVOICES_URL):
        path_obj = TEMPLATES_PATH / url.removeprefix(INVOICES_URL)
    elif url.startswith(STATIC_URL):
        fullname = url.removeprefix(STATIC_URL)
        match = finders.find(fullname)
        if match is None:
            raise ValueError(f"Could not find {fullname}")
        path_obj = Path(match)
    else:
        raise ValueError(f"Unsupported URL: {url}")
    result = {
        "filename": path_obj.name,
        "string": path_obj.read_bytes(),
    }
    if path_obj.suffix == ".css":
        result["mime_type"] = "text/css"
        result["encoding"] = "utf-8"
    return result


class QuantityUnit(models.IntegerChoices):
    BLANK = 0, ""
    HOURS = 1, "hours"


class Currency(models.IntegerChoices):
    EUR = 0, "EUR"
    CZK = 1, "CZK"
    USD = 2, "USD"
    GBP = 3, "GBP"

    @staticmethod
    def from_str(value: str) -> Currency:
        return Currency(int(value))


# Map Currency object to currencies used by the payments
# TODO: payments model should be migrated to use Currency
CURRENCY_MAP: dict[Currency, int] = {
    Currency.EUR: 0,
    Currency.CZK: 3,
    Currency.USD: 2,
    Currency.GBP: 4,
}
CURRENCY_MAP_FROM_PAYMENT: dict[int, Currency] = {
    value: key for key, value in CURRENCY_MAP.items()
}


InfoType = Literal["number", "short_number", "iban", "bic", "bank", "holder"]


class BankAccountInfo:
    def __init__(  # noqa: PLR0913
        self,
        *,
        number: str,
        bank: str,
        iban: str,
        bic: str,
        holder: str = COMPANY_NAME,
        short_list: tuple[InfoType, ...],
    ) -> None:
        self._number = number
        self._bank = bank
        self._iban = iban
        self._bic = bic
        self._holder = holder
        self._short_list = short_list

    @property
    def number(self) -> str:
        return self._number

    @property
    def short_number(self) -> str:
        return self._number.split("/")[0].strip()

    @property
    def bank(self) -> str:
        return self._bank

    @property
    def iban(self) -> str:
        return self._iban

    @property
    def raw_iban(self) -> str:
        return self._iban.replace(" ", "")

    @property
    def bic(self) -> str:
        return self._bic

    @property
    def holder(self) -> str:
        return self._holder

    def get_info(self, *items: InfoType) -> Generator[tuple[StrOrPromise, str]]:
        for item in items:
            if item == "iban":
                yield (gettext("IBAN"), self._iban)
            elif item == "number":
                yield (gettext("Account number"), self._number)
            elif item == "short_number":
                yield (gettext("Account number"), self.short_number)
            elif item == "bic":
                yield (gettext("BIC/SWIFT"), self._bic)
            elif item == "bank":
                yield (gettext("Issuing bank"), self._bank)
            elif item == "holder":
                yield (gettext("Account holder"), self._holder)
            else:
                raise ValueError(f"Unknown info type: {item}")

    def get_full_info(self) -> list[tuple[StrOrPromise, str]]:
        return list(self.get_info("iban", "number", "bic", "bank", "holder"))

    def get_short_info(self) -> list[tuple[StrOrPromise, str]]:
        return list(self.get_info(*self._short_list))


FIO_BANK: str = "Fio banka, a.s., Na Florenci 2139/2, 11000 Praha, Czechia"
FIO_BIC: str = "FIOBCZPPXXX"


BANK_ACCOUNTS: dict[Currency, BankAccountInfo] = {
    Currency.EUR: BankAccountInfo(
        number="2302907395 / 2010",
        bank=FIO_BANK,
        iban="CZ30 2010 0000 0023 0290 7395",
        bic=FIO_BIC,
        short_list=("iban",),
    ),
    Currency.CZK: BankAccountInfo(
        number="2002907393 / 2010",
        bank=FIO_BANK,
        iban="CZ49 2010 0000 0020 0290 7393",
        bic=FIO_BIC,
        short_list=("number",),
    ),
    Currency.USD: BankAccountInfo(
        number="2603015278 / 2010",
        bank=FIO_BANK,
        iban="CZ37 2010 0000 0026 0301 5278",
        bic=FIO_BIC,
        short_list=("short_number", "bic"),
    ),
    Currency.GBP: BankAccountInfo(
        number="2803015280 / 2010",
        bank=FIO_BANK,
        iban="CZ71 2010 0000 0028 0301 5280",
        bic=FIO_BIC,
        short_list=("short_number", "bic"),
    ),
}


class InvoiceKind(models.IntegerChoices):
    DRAFT = 0, "Draft"
    INVOICE = 10, "Invoice"
    PROFORMA = 50, "Pro Forma Invoice"
    QUOTE = 90, "Quote"

    @staticmethod
    def from_str(value: str) -> InvoiceKind:
        return InvoiceKind(int(value))


class InvoiceCategory(models.IntegerChoices):
    HOSTING = 1, "Hosting"
    SUPPORT = 2, "Support"
    DEVEL = 3, "Development / Consultations"
    DONATE = 4, "Donation"


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


class Invoice(models.Model):  # noqa: PLR0904
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sequence = models.IntegerField(editable=False)
    number = models.GeneratedField(
        expression=Concat(
            LPad(Cast("kind", models.CharField()), 2, models.Value("0")),
            Cast(Extract("issue_date", "year") % 2000, models.CharField()),
            LPad(Cast("sequence", models.CharField()), 6, models.Value("0")),
        ),
        output_field=models.CharField(max_length=20),
        db_persist=True,
        unique=True,
        help_text="Invoice number is automatically generated",
    )
    issue_date = models.DateField(default=datetime.date.today)
    due_date = models.DateField(
        blank=True,
        help_text="Due date / Quote validity, keep blank unless specific terms are needed",
    )
    tax_date = models.DateField(
        blank=True,
        help_text="Date of taxable supply, keep blank for issue date",
    )

    kind = models.IntegerField(choices=InvoiceKind)
    category = models.IntegerField(
        choices=InvoiceCategory, help_text="Helps to categorize income"
    )
    customer = models.ForeignKey("payments.Customer", on_delete=models.deletion.PROTECT)
    customer_reference = models.CharField(
        max_length=100,
        blank=True,
        help_text="Text will be shown on the generated invoice",
    )
    customer_note = models.TextField(
        blank=True,
        help_text="Text will be shown on the generated invoice",
    )
    discount = models.ForeignKey(
        Discount,
        on_delete=models.deletion.PROTECT,
        blank=True,
        null=True,
        help_text="Automatically applied to all invoice items",
    )
    vat_rate = models.IntegerField(
        default=0,
        verbose_name="VAT rate",
        help_text="VAT rate in percents to apply on the invoice",
    )
    currency = models.IntegerField(choices=Currency, default=Currency.EUR)

    # Invoice chaining Proforma -> Invoice, or Draft -> Invoice
    parent = models.ForeignKey(
        "Invoice",
        on_delete=models.deletion.PROTECT,
        blank=True,
        null=True,
        verbose_name="Parent invoice",
        help_text="Invoices tracking, use for issuing invoice from quote",
    )

    prepaid = models.BooleanField(
        default=False,
        verbose_name="Already paid",
        help_text="Invoices paid in advance (card payment, invoices issued after paying pro forma)",
    )

    # Passed to payment
    extra = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "sequence", Extract("issue_date", "year"), "kind", name="unique_number"
            )
        ]
        ordering = ["-issue_date"]
        permissions = [
            ("view_income", "Can view income tracking"),
        ]

    def __str__(self) -> str:
        return f"{self.number}: {self.customer} {self.total_amount}"

    def save(  # type: ignore[override]
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using=None,
        update_fields=None,
    ) -> None:
        if self.extra is None:
            self.extra = {}
        extra_fields: list[str] = []
        if not self.tax_date:
            self.tax_date = self.issue_date
            extra_fields.append("tax_date")
        if not self.due_date:
            self.due_date = self.issue_date + datetime.timedelta(
                days=self.get_due_delta()
            )
            extra_fields.append("due_date")
        if not self.sequence:
            try:
                self.sequence = (
                    Invoice.objects.filter(
                        kind=self.kind, issue_date__year=self.issue_date.year
                    )
                    .order_by("-sequence")[0]
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

    def get_absolute_url(self) -> str:
        return reverse("crm:invoice-detail", kwargs={"pk": self.pk})

    def is_editable(self) -> bool:
        if self.kind == InvoiceKind.INVOICE:
            return self.issue_date.month == now().month
        return not self.invoice_set.exists()

    def get_due_delta(self) -> int:
        if self.prepaid:
            return 0
        if self.is_draft:
            return 30
        return 14

    @property
    def is_draft(self):
        return self.kind in {InvoiceKind.DRAFT, InvoiceKind.QUOTE}

    def get_package(self) -> Package | None:
        if not self.all_items:
            return None
        return self.all_items[0].package

    def render_amount(self, amount: int | Decimal) -> str:
        if self.currency == Currency.EUR:
            return f"€{amount}"
        return f"{amount} {self.get_currency_display()}"

    @cached_property
    def exchange_rate_czk(self) -> Decimal:
        """Exchange rate from currency to CZK."""
        return ExchangeRates.get(self.get_currency_display(), self.tax_date)

    @cached_property
    def bank_account(self) -> BankAccountInfo:
        return BANK_ACCOUNTS[cast("Currency", self.currency)]

    @cached_property
    def exchange_rate_eur(self) -> Decimal:
        """Exchange rate from currency to EUR."""
        return ExchangeRates.get("EUR", self.tax_date) / self.exchange_rate_czk

    @cached_property
    def total_items_amount(self) -> Decimal:
        return sum(
            (item.unit_price * item.quantity for item in self.all_items),
            start=Decimal(0),
        )

    @cached_property
    def total_plus_items_amount(self) -> Decimal:
        return sum(
            (
                item.unit_price * item.quantity
                for item in self.all_items
                if item.unit_price > 0
            ),
            start=Decimal(0),
        )

    @cached_property
    def total_discount(self) -> Decimal:
        if not self.discount:
            return Decimal(0)
        return round(-self.total_plus_items_amount * self.discount.percents / 100, 0)

    @property
    def display_total_discount(self) -> str:
        return self.render_amount(self.total_discount)

    @cached_property
    def total_amount_no_vat(self) -> Decimal:
        return round_decimal(self.total_items_amount + self.total_discount)

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
        return round_decimal(self.total_amount_no_vat * self.vat_rate / 100)

    @property
    def total_vat_czk(self) -> Decimal:
        return round_decimal(self.total_vat * self.exchange_rate_czk, max_decimals=2)

    @property
    def display_total_vat(self) -> str:
        return self.render_amount(self.total_vat)

    @cached_property
    def total_amount(self) -> Decimal:
        return round_decimal(self.total_amount_no_vat + self.total_vat, max_decimals=2)

    @property
    def total_amount_czk(self) -> Decimal:
        return round_decimal(self.total_amount * self.exchange_rate_czk, max_decimals=2)

    @property
    def display_total_amount(self) -> str:
        return self.render_amount(self.total_amount)

    @cached_property
    def all_items(self) -> models.QuerySet[InvoiceItem]:
        return self.invoiceitem_set.order_by("id")

    def get_description(self) -> str:
        if self.all_items:
            return self.all_items[0].description
        return ""

    def render_html(self, *, is_receipt: bool = False) -> str:
        with override("en_GB"):
            return render_to_string(
                "invoice-template.html",
                {
                    "invoice": self,
                    "is_receipt": is_receipt,
                    "company_name": COMPANY_NAME,
                    "company_address": COMPANY_ADDRESS,
                    "company_zip": COMPANY_ZIP,
                    "company_city": COMPANY_CITY,
                    "company_country": COMPANY_COUNTRY,
                    "company_vat_id": COMPANY_VAT_ID,
                    "company_id": COMPANY_ID,
                },
            )

    def get_filename(self, extension: str, *, kind_override: str = ""):
        return f"Weblate_{kind_override or self.get_kind_display()}_{self.number}.{extension}".replace(
            " ", "_"
        )

    @property
    def filename(self) -> str:
        """PDF filename."""
        return self.get_filename("pdf")

    @property
    def receipt_filename(self) -> str:
        if not self.is_paid:
            raise ValueError("Unpaid invoices do not have a receipt")
        return self.get_filename("pdf", kind_override="Receipt")

    @property
    def path(self) -> Path:
        """PDF path object."""
        return settings.INVOICES_PATH / self.filename

    @property
    def receipt_path(self) -> Path:
        """PDF receipt path object."""
        return settings.INVOICES_PATH / self.receipt_filename

    @property
    def xml_path(self) -> Path:
        """XML path object."""
        return settings.INVOICES_PATH / self.get_filename("xml")

    def generate_files(self) -> None:
        self.generate_money_s3_xml()
        self.generate_pdf()
        self.sync_files()

    def generate_receipt(self) -> None:
        self._generate_pdf(self.receipt_filename, is_receipt=True)

    def sync_files(self) -> None:
        if self.kind == InvoiceKind.INVOICE and settings.INVOICES_COPY_PATH:
            output_dir = (
                settings.INVOICES_COPY_PATH
                / f"{self.issue_date.year:d}"
                / f"{self.issue_date.month:02d}"
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            copyfile(self.path, output_dir / self.filename)
            copyfile(self.xml_path, output_dir / self.get_filename("xml"))

    def get_money_s3_xml_tree(self, invoices: etree._Element) -> None:  # noqa: PLR0915,C901
        """Create XML tree for Money S3 invoice XML."""

        def add_element(root, name: str, text: str | Decimal | int | None = None):
            added = etree.SubElement(root, name)
            if text is not None:
                added.text = str(text)
            return added

        def add_amounts(root, in_czk: bool = False) -> None:
            dph = add_element(root, "SouhrnDPH")
            if in_czk:
                castka_zaklad = self.total_amount_no_vat_czk
                castka_dph = self.total_vat_czk
                castka_celkem = self.total_amount_czk
            else:
                castka_zaklad = self.total_amount_no_vat
                castka_dph = self.total_vat
                castka_celkem = self.total_amount

            fixed_rates: dict[int, int] = {0: 0, 11: 5, 21: 22}
            if self.vat_rate in fixed_rates:
                vat_name = fixed_rates[self.vat_rate]
                add_element(dph, f"Zaklad{vat_name}", castka_zaklad)
                if self.vat_rate > 0:
                    add_element(dph, f"DPH{vat_name}", castka_dph)
                fixed_rates.pop(self.vat_rate)
                for rate in fixed_rates.values():
                    add_element(dph, f"Zaklad{rate}", "0")
                for rate in fixed_rates.values():
                    if rate > 0:
                        add_element(dph, f"DPH{rate}", "0")
            else:
                dalsi = add_element(dph, "SeznamDalsiSazby")
                sazba = add_element(dalsi, "DalsiSazba")
                add_element(sazba, "Sazba", self.vat_rate)
                add_element(sazba, "Zaklad", castka_zaklad)
                add_element(sazba, "DPH", castka_dph)
            add_element(root, "Celkem", castka_celkem)

        output = etree.SubElement(invoices, "FaktVyd")
        add_element(output, "Doklad", self.number)
        add_element(output, "CisRada", self.kind)
        add_element(output, "Popis", self.get_description())
        add_element(output, "Vystaveno", self.issue_date.isoformat())
        add_element(output, "DatUcPr", self.tax_date.isoformat())
        add_element(output, "PlnenoDPH", self.tax_date.isoformat())
        add_element(output, "Splatno", self.due_date.isoformat())
        add_element(output, "DatSkPoh", self.tax_date.isoformat())
        if self.customer.country == "CZ":
            add_element(output, "KodDPH", "19Ř01,02")
        elif self.customer.vat:
            add_element(output, "KodDPH", "19Ř21")
        elif self.customer.is_eu_enduser:
            add_element(output, "KodDPH", "19Ř01,02")
        else:
            add_element(output, "KodDPH", "19Ř26")
        add_element(output, "ZjednD", "0")
        add_element(output, "VarSymbol", self.number)

        # Druh (N: normální, L: zálohová, F: proforma, D: doklad k přijaté platbě)
        # All is "N" for now (proformas are not exported and D would require additional
        # invoice to be issued)
        add_element(output, "Druh", "N")
        add_element(output, "Dobropis", "0" if self.total_amount > 0 else "1")
        add_element(output, "ZpVypDPH", "1")
        add_element(output, "SazbaDPH1", "12")
        add_element(output, "SazbaDPH2", "21")
        add_element(output, "Proplatit", self.total_amount_czk)
        add_element(output, "Vyuctovano", "0")
        add_amounts(output, in_czk=True)
        if self.currency != Currency.CZK:
            valuty = add_element(output, "Valuty")
            mena = add_element(valuty, "Mena")
            add_element(mena, "Kod", self.get_currency_display())
            add_element(mena, "Mnozstvi", "1")
            add_element(mena, "Kurs", self.exchange_rate_czk)
            add_amounts(valuty)

        add_element(output, "PriUhrZbyv", "0")
        if self.currency != Currency.CZK:
            add_element(output, "ValutyProp", self.total_amount)
        add_element(output, "SumZaloha", "0")
        add_element(output, "SumZalohaC", "0")

        prijemce = add_element(output, "DodOdb")
        add_element(prijemce, "ObchNazev", self.customer.name)
        adresa = add_element(prijemce, "ObchAdresa")
        add_element(adresa, "Ulice", self.customer.address)
        add_element(adresa, "Misto", self.customer.city)
        add_element(adresa, "PSC", self.customer.postcode)
        add_element(adresa, "Stat", self.customer.country)
        add_element(prijemce, "FaktNazev", self.customer.name)
        if self.customer.tax and self.customer.country == "CZ":
            add_element(prijemce, "ICO", self.customer.tax)
        if self.customer.vat:
            add_element(prijemce, "DIC", self.customer.vat.replace(" ", ""))
        adresa = add_element(prijemce, "FaktAdresa")
        add_element(adresa, "Ulice", self.customer.address)
        add_element(adresa, "Misto", self.customer.city)
        add_element(adresa, "PSC", self.customer.postcode)
        add_element(adresa, "Stat", self.customer.country)
        if self.customer.vat:
            add_element(prijemce, "PlatceDPH", "1")
            add_element(prijemce, "FyzOsoba", "0")

        seznam = add_element(output, "SeznamPolozek")
        for item in self.all_items:
            polozka = add_element(seznam, "Polozka")
            add_element(polozka, "Popis", item.description)
            add_element(polozka, "PocetMJ", item.quantity)
            if self.currency == Currency.CZK:
                add_element(polozka, "Cena", item.total_price)
            else:
                add_element(polozka, "Valuty", item.total_price)

    @staticmethod
    def get_invoice_xml_root() -> tuple[etree._Element, etree._Element]:
        document = etree.Element("MoneyData")
        invoices = etree.SubElement(document, "SeznamFaktVyd")
        return document, invoices

    @staticmethod
    def save_invoice_xml(document: etree._Element, path: Path) -> None:
        etree.indent(document)
        etree.ElementTree(document).write(path, encoding="utf-8", xml_declaration=True)

    def generate_money_s3_xml(self) -> None:
        """Create XML file for Money S3 invoice XML."""
        document, invoices = self.get_invoice_xml_root()
        self.get_money_s3_xml_tree(invoices)
        settings.INVOICES_PATH.mkdir(exist_ok=True)
        self.save_invoice_xml(document, self.xml_path)

    def _generate_pdf(self, filename: str, *, is_receipt: bool = False) -> None:
        """Render invoice as PDF."""
        # Create directory to store invoices
        settings.INVOICES_PATH.mkdir(exist_ok=True)
        render_pdf(
            html=self.render_html(is_receipt=is_receipt),
            output=settings.INVOICES_PATH / filename,
        )

    def generate_pdf(self) -> None:
        """Render invoice as PDF."""
        self._generate_pdf(self.filename)

    def duplicate(  # noqa: PLR0913
        self,
        *,
        kind: InvoiceKind,
        prepaid: bool = False,
        start_date: datetime.datetime | None = None,
        end_date: datetime.datetime | None = None,
        extra: dict[str, int] | None = None,
        customer_reference: str | None = None,
        customer_note: str | None = None,
        tax_date: datetime.date | None = None,
    ) -> Invoice:
        """Create a final invoice from draft/proforma upon payment."""
        invoice = Invoice.objects.create(
            kind=kind,
            category=self.category,
            customer=self.customer,
            customer_reference=customer_reference or self.customer_reference,
            customer_note=customer_note or self.customer_note,
            discount=self.discount,
            vat_rate=self.vat_rate,
            currency=self.currency,
            tax_date=cast("datetime.date", tax_date),
            parent=self,
            prepaid=prepaid,
            extra=extra if extra is not None else self.extra,
        )
        for item in self.all_items:
            if kind == InvoiceKind.DRAFT and item.package:
                # Load description and price from the package
                invoice.invoiceitem_set.create(
                    quantity=item.quantity,
                    quantity_unit=item.quantity_unit,
                    package=item.package,
                    start_date=start_date or item.start_date,
                    end_date=end_date or item.end_date,
                )
            else:
                invoice.invoiceitem_set.create(
                    description=item.description,
                    quantity=item.quantity,
                    quantity_unit=item.quantity_unit,
                    unit_price=item.unit_price,
                    package=item.package,
                    start_date=start_date or item.start_date,
                    end_date=end_date or item.end_date,
                )
        return invoice

    def create_payment(
        self, *, recurring: str = "", backend: str = "", repeat: Payment | None = None
    ) -> Payment:
        if not self.can_be_paid(InvoiceKind.DRAFT):
            raise ValueError("Payment already exists for this invoice!")
        return self.draft_payment_set.create(
            amount=float(self.total_amount),
            amount_fixed=True,
            description=self.get_description(),
            recurring=recurring,
            extra=self.extra,
            customer=self.customer,
            currency=CURRENCY_MAP[cast("Currency", self.currency)],
            backend=backend,
            repeat=repeat,
        )

    def can_be_paid(self, *state: InvoiceKind) -> bool:
        return (
            self.kind in {InvoiceKind.INVOICE, *state}
            and not self.prepaid
            and not self.is_paid
        )

    @property
    def is_final(self):
        return self.kind == InvoiceKind.INVOICE

    @property
    def is_proforma(self):
        return self.kind == InvoiceKind.PROFORMA

    def get_payment_url(self) -> str | None:
        if self.can_be_paid():
            return get_site_url("invoice-pay", pk=self.pk)
        return None

    def get_payment_qrcode(self) -> str:
        if not self.can_be_paid(InvoiceKind.PROFORMA):
            return ""
        if self.currency == Currency.EUR:
            data = f"""BCD
001
1
SCT
{self.bank_account.bic}
{self.bank_account.holder}
{self.bank_account.raw_iban}
EUR{self.total_amount}

{self.number}


"""
        elif self.currency == Currency.CZK:
            data = f"SPD*1.0*ACC:{self.bank_account.raw_iban}*AM:{self.total_amount}*CC:CZK*RF:{self.number}*RN:{self.bank_account.holder}"
        else:
            return ""

        return mark_safe(  # noqa: S308
            qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage).to_string(
                encoding="unicode"
            )
        )

    @property
    def is_payable(self) -> bool:
        return self.kind == InvoiceKind.INVOICE

    @property
    def is_paid(self) -> bool:
        return self.paid_payment_set.exists()

    def get_payment(self) -> Payment:
        return self.paid_payment_set.all()[0]

    @property
    def has_pdf(self):
        return self.kind != InvoiceKind.DRAFT

    def get_download_url(self) -> str | None:
        if not self.has_pdf:
            return None
        return reverse("invoice-pdf", kwargs={"pk": self.pk})


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.deletion.CASCADE)
    package = models.ForeignKey(
        "weblate_web.Package",
        on_delete=models.deletion.SET_NULL,
        null=True,
        blank=True,
        help_text="Selecting package will automatically fill in description and price",
        limit_choices_to={"hidden": False},
    )
    description = models.CharField(max_length=200, blank=True)
    quantity = models.IntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(50)]
    )
    quantity_unit = models.IntegerField(
        choices=QuantityUnit, default=QuantityUnit.BLANK
    )
    unit_price = models.DecimalField(decimal_places=3, max_digits=8, blank=True)
    start_date = models.DateField(blank=True, null=True)
    end_date = models.DateField(blank=True, null=True)

    def __str__(self) -> str:
        return f"{self.description} ({self.display_quantity}) {self.display_price}"

    def save(  # type: ignore[override]
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using=None,
        update_fields=None,
    ) -> None:
        from weblate_web.models import get_period_delta  # noqa: PLC0415

        extra_fields: list[str] = []

        if self.package:
            if not self.unit_price:
                if self.invoice.currency == Currency.EUR:
                    self.unit_price = self.package.price
                else:
                    self.unit_price = ExchangeRates.convert_from_eur(
                        self.package.price,
                        self.invoice.get_currency_display(),
                        self.invoice.tax_date,
                    )
                extra_fields.append("unit_price")
            if not self.description:
                self.description = self.package.verbose
                extra_fields.append("description")

                if (start_date := self.invoice.extra.get("start_date")) and (
                    repeat := self.package.get_repeat()
                ):
                    # Include subscription period
                    if isinstance(start_date, str):
                        start_date = datetime.datetime.fromisoformat(start_date)
                    end_date = (
                        start_date
                        + get_period_delta(repeat)
                        - datetime.timedelta(days=1)
                    )
                    self.start_date = start_date.date()
                    self.end_date = end_date.date()
                    extra_fields.extend(("start_date", "end_date"))

        if extra_fields and update_fields is not None:
            update_fields = tuple(set(update_fields).union(extra_fields))

        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    @property
    def has_date_range(self) -> bool:
        return self.start_date is not None and self.end_date is not None

    def get_date_range_display(self) -> str:
        if self.start_date is not None and self.end_date is not None:
            return f"{date_format(self.start_date)} - {date_format(self.end_date)}"
        return ""

    def clean(self) -> None:
        if not self.description and not self.package:
            raise ValidationError(
                {"description": "Description needs to be provided if not using package"}
            )
        if not self.unit_price and not self.package:
            raise ValidationError(
                {"unit_price": "Price needs to be provided if not using package"}
            )

    @property
    def total_price(self) -> Decimal:
        return round_decimal(self.unit_price * self.quantity)

    @property
    def display_price(self) -> str:
        if self.unit_price is None:
            return self.invoice.render_amount(0)
        return self.invoice.render_amount(round_decimal(self.unit_price))

    @property
    def display_total_price(self) -> str:
        return self.invoice.render_amount(self.total_price)

    def get_quantity_unit_display(self) -> str:  # type: ignore[no-redef]
        # Correctly handle singulars
        if self.quantity_unit == QuantityUnit.HOURS and self.quantity == 1:
            return "hour"
        # This is what original get_quantity_unit_display() would have done
        return self._get_FIELD_display(  # type: ignore[attr-defined]
            field=self._meta.get_field("quantity_unit")
        )

    @property
    def display_quantity(self) -> str:
        if self.quantity_unit:
            return f"{self.quantity} {self.get_quantity_unit_display()}"
        return f"{self.quantity}"
