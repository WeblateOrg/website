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

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Cast, Concat, Extract, LPad
from django.template.loader import render_to_string
from django.utils.functional import cached_property
from django.utils.translation import override
from fakturace.rates import DecimalRates
from lxml import etree
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration

INVOICES_URL = "invoices:"
STATIC_URL = "static:"
TEMPLATES_PATH = Path(__file__).parent / "templates"


def round_decimal(num: Decimal) -> Decimal:
    if num % Decimal("0.01"):
        return round(num, 3)
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
        raise ValueError(f"Usupported URL: {url}")
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


class InvoiceKind(models.IntegerChoices):
    DRAFT = 0, "Draft"
    INVOICE = 10, "Invoice"
    PROFORMA = 50, "Pro Forma Invoice"
    QUOTE = 90, "Quote"


class InvoiceCategory(models.IntegerChoices):
    HOSTING = 1, "Hosting"
    SUPPORT = 2, "Support"
    DEVEL = 3, "Development"
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


class Invoice(models.Model):
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
    )
    issue_date = models.DateField(default=datetime.date.today)
    due_date = models.DateField(blank=True)
    kind = models.IntegerField(choices=InvoiceKind)
    category = models.IntegerField(
        choices=InvoiceCategory, default=InvoiceCategory.HOSTING
    )
    customer = models.ForeignKey("payments.Customer", on_delete=models.deletion.PROTECT)
    customer_reference = models.CharField(max_length=100, blank=True)
    discount = models.ForeignKey(
        Discount, on_delete=models.deletion.PROTECT, blank=True, null=True
    )
    vat_rate = models.IntegerField(default=0)
    currency = models.IntegerField(choices=Currency, default=Currency.EUR)

    # Invoice chaining Proforma -> Invoice, or Draft -> Invoice
    parent = models.ForeignKey(
        "Invoice", on_delete=models.deletion.PROTECT, blank=True, null=True
    )

    prepaid = models.BooleanField(
        default=False, help_text="Invoices paid in advance (card payment, pro forma)"
    )

    # Passed to payment
    extra = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "sequence", Extract("issue_date", "year"), "kind", name="unique_number"
            )
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
    ):
        extra_fields: list[str] = []
        if not self.due_date:
            self.due_date = self.issue_date + datetime.timedelta(
                days=30 if self.is_draft else 14
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

    @property
    def is_draft(self):
        return self.kind in {InvoiceKind.DRAFT, InvoiceKind.QUOTE}

    def render_amount(self, amount: int | Decimal) -> str:
        if self.currency == Currency.EUR:
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

    def get_description(self) -> str:
        return self.all_items[0].description

    def render_html(self) -> str:
        with override("en_GB"):
            return render_to_string(
                "invoice-template.html",
                {
                    "invoice": self,
                },
            )

    def get_filename(self, extension: str):
        return f"Weblate_{self.get_kind_display()}_{self.number}.{extension}"

    @property
    def filename(self) -> str:
        """PDF filename."""
        return self.get_filename("pdf")

    @property
    def path(self) -> Path:
        """PDF path object."""
        return settings.INVOICES_PATH / self.filename

    @property
    def xml_path(self) -> Path:
        """XML path object."""
        return settings.INVOICES_PATH / self.get_filename("xml")

    def generate_files(self) -> None:
        self.generate_xml()
        self.generate_pdf()
        if self.kind == InvoiceKind.INVOICE and settings.INVOICES_COPY_PATH:
            output_dir = (
                settings.INVOICES_COPY_PATH
                / f"{self.issue_date.year:d}"
                / f"{self.issue_date.month:02d}"
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            copyfile(self.path, output_dir / self.filename)
            copyfile(self.xml_path, output_dir / self.get_filename("xml"))

    def get_xml_tree(self, invoices: etree._Element) -> None:  # noqa: PLR0915,C901
        def add_element(root, name: str, text: str | Decimal | int | None = None):
            added = etree.SubElement(root, name)
            if text is not None:
                added.text = str(text)
            return added

        def add_amounts(root, in_czk: bool = False):
            dph = add_element(root, "SouhrnDPH")
            if in_czk:
                castka_zaklad = self.total_amount_no_vat_czk
                castka_dph = self.total_vat_czk
                castka_celkem = self.total_amount_czk
            else:
                castka_zaklad = self.total_amount_no_vat
                castka_dph = self.total_vat
                castka_celkem = self.total_amount

            fixed_rates = {0, 5, 22}
            if self.vat_rate in fixed_rates:
                add_element(dph, f"Zaklad{self.vat_rate}", castka_zaklad)
                if self.vat_rate > 0:
                    add_element(dph, f"DPH{self.vat_rate}", castka_dph)
                fixed_rates.remove(self.vat_rate)
                for rate in fixed_rates:
                    add_element(dph, f"Zaklad{rate}", "0")
                for rate in fixed_rates:
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
        add_element(output, "DatUcPr", self.issue_date.isoformat())
        add_element(output, "PlnenoDPH", self.issue_date.isoformat())
        add_element(output, "Splatno", self.due_date.isoformat())
        add_element(output, "DatSkPoh", self.issue_date.isoformat())
        add_element(output, "KodDPH", "19Ř21")
        add_element(output, "ZjednD", "0")
        add_element(output, "VarSymbol", self.number)

        # Druh (N: normální, L: zálohová, F: proforma, D: doklad k přijaté platbě)
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
            add_element(prijemce, "DIC", self.customer.vat)
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

    def generate_xml(self):
        document = etree.Element("MoneyData")
        invoices = etree.SubElement(document, "SeznamFaktVyd")

        self.get_xml_tree(invoices)

        etree.indent(document)

        settings.INVOICES_PATH.mkdir(exist_ok=True)
        etree.ElementTree(document).write(
            self.xml_path, encoding="utf-8", xml_declaration=True
        )

    def generate_pdf(self) -> None:
        # Create directory to store invoices
        settings.INVOICES_PATH.mkdir(exist_ok=True)
        font_config = FontConfiguration()

        renderer = HTML(
            string=self.render_html(),
            url_fetcher=url_fetcher,
        )
        font_style = CSS(
            string="""
            @font-face {
              font-family: Source Sans Pro;
              font-weight: 400;
              src: url("static:vendor/font-source/TTF/SourceSans3-Regular.ttf");
            }
            @font-face {
              font-family: Source Sans Pro;
              font-weight: 700;
              src: url("static:vendor/font-source/TTF/SourceSans3-Bold.ttf");
            }
        """,
            font_config=font_config,
            url_fetcher=url_fetcher,
        )
        renderer.write_pdf(
            settings.INVOICES_PATH / self.filename,
            stylesheets=[font_style],
            font_config=font_config,
        )

    def finalize(
        self,
        *,
        kind: InvoiceKind = InvoiceKind.INVOICE,
        prepaid: bool = True,
    ) -> Invoice:
        """Create a final invoice from draft/proforma upon payment."""
        invoice = Invoice.objects.create(
            kind=kind,
            category=self.category,
            customer=self.customer,
            customer_reference=self.customer_reference,
            discount=self.discount,
            vat_rate=self.vat_rate,
            currency=self.currency,
            parent=self,
            prepaid=prepaid,
            extra=self.extra,
        )
        for item in self.all_items:
            invoice.invoiceitem_set.create(
                description=item.description,
                quantity=item.quantity,
                quantity_unit=item.quantity_unit,
                unit_price=item.unit_price,
            )
        return invoice


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.deletion.CASCADE)
    description = models.CharField(max_length=200, blank=True)
    quantity = models.IntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(50)]
    )
    quantity_unit = models.IntegerField(
        choices=QuantityUnit, default=QuantityUnit.BLANK
    )
    unit_price = models.DecimalField(decimal_places=3, max_digits=8, blank=True)
    package = models.ForeignKey(
        "weblate_web.Package", on_delete=models.deletion.SET_NULL, null=True, blank=True
    )

    def __str__(self) -> str:
        return f"{self.description} ({self.display_quantity}) {self.display_price}"

    def save(  # type: ignore[override]
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using=None,
        update_fields=None,
    ):
        extra_fields: list[str] = []

        if self.package:
            if not self.unit_price:
                self.unit_price = self.package.price
                extra_fields.append("unit_price")
            if not self.description:
                self.description = self.package.verbose
                extra_fields.append("description")

        if extra_fields and update_fields is not None:
            update_fields = tuple(set(update_fields).union(extra_fields))

        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def clean(self):
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
        return self.invoice.render_amount(round_decimal(self.unit_price))

    @property
    def display_total_price(self) -> str:
        return self.invoice.render_amount(self.total_price)

    def get_quantity_unit_display(self) -> str:  # type: ignore[no-redef]
        # Correcly handle singulars
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
