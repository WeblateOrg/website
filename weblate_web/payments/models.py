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

import os.path
import re
import uuid
from datetime import timedelta
from email.message import Message
from typing import TYPE_CHECKING

from appconf import AppConf
from django.conf import settings
from django.contrib.auth.models import User
from django.core import serializers
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.mail import EmailAlternative
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy, pgettext_lazy
from django_countries.fields import CountryField
from unidecode import unidecode
from vies.models import VATINField

from weblate_web.utils import get_site_url

from .fields import Char32UUIDField
from .utils import send_notification, validate_email
from .validators import validate_vatin

if TYPE_CHECKING:
    from weblate_web.invoices.models import Invoice

SHORT_NAME_DISCARD = re.compile(r"[^a-zA-Z0-9_\s-]")
SHORT_NAME_SPACE = re.compile(r"[\s_-]+")
SHORT_NAME_SPLIT = re.compile(r"[,(-]")

EU_VAT_RATES = {
    "BE": 21,
    "BG": 20,
    "CZ": 21,
    "DK": 25,
    "DE": 19,
    "EE": 20,
    "IE": 23,
    "GR": 24,
    "ES": 21,
    "FR": 20,
    "HR": 25,
    "IT": 22,
    "CY": 19,
    "LV": 21,
    "LT": 21,
    "LU": 17,
    "HU": 27,
    "MT": 18,
    "NL": 21,
    "AT": 20,
    "PL": 23,
    "PT": 23,
    "RO": 19,
    "SI": 22,
    "SK": 20,
    "FI": 24,
    "SE": 25,
}

VAT_RATE = 21


class CustomerQuerySet(models.QuerySet["Customer"]):
    def for_user(self, user: User) -> CustomerQuerySet:
        return self.filter(users=user).distinct()

    def active(self) -> CustomerQuerySet:
        return self.filter(
            service__subscription__expires__gte=timezone.now() - timedelta(days=4 * 365)
        ).distinct()


class Customer(models.Model):
    vat = VATINField(
        validators=[validate_vatin],
        blank=True,
        null=True,
        default="",
        verbose_name=gettext_lazy("European VAT ID"),
        help_text=gettext_lazy(
            "Please fill in European Union VAT ID, leave blank if not applicable."
        ),
    )
    tax = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=gettext_lazy("Company identification number"),
        help_text=gettext_lazy(
            "Please fill in your company registration number if it should "
            "appear on the invoice."
        ),
    )
    name = models.CharField(
        max_length=200,
        default="",
        verbose_name=gettext_lazy("Company or individual name"),
        db_index=True,
    )
    address = models.CharField(
        max_length=200,
        default="",
        verbose_name=gettext_lazy("Address"),
    )
    address_2 = models.CharField(
        max_length=200,
        default="",
        verbose_name=gettext_lazy("Additional address information"),
        blank=True,
    )
    city = models.CharField(
        max_length=200,
        default="",
        verbose_name=gettext_lazy("City"),
    )
    postcode = models.CharField(
        max_length=20,
        default="",
        verbose_name=gettext_lazy("Postcode"),
    )
    country = CountryField(
        default="",
        verbose_name=gettext_lazy("Country"),
    )
    email = models.EmailField(
        blank=True,
        max_length=190,
        validators=[validate_email],
        verbose_name=gettext_lazy("Billing e-mail"),
        help_text=gettext_lazy("Additional e-mail to receive billing notifications"),
    )
    origin = models.URLField(max_length=300)
    user_id = models.IntegerField()
    discount = models.ForeignKey(
        "invoices.Discount",
        on_delete=models.deletion.SET_NULL,
        blank=True,
        null=True,
    )
    end_client = models.CharField(
        max_length=200,
        default="",
        blank=True,
        verbose_name="End client name",
    )
    note = models.TextField(blank=True, verbose_name="Note")
    users = models.ManyToManyField(User, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    zammad_id = models.IntegerField(default=0, editable=False)

    objects = CustomerQuerySet.as_manager()

    class Meta:
        verbose_name = "Customer"
        verbose_name_plural = "Customers"

    def __str__(self) -> str:
        if self.name:
            if self.email:
                return f"{self.name} ({self.email})"
            return self.name
        if self.email:
            return self.email
        return f"Customer:{self.pk}"

    def get_absolute_url(self) -> str:
        return reverse("crm:customer-detail", kwargs={"pk": self.pk})

    @property
    def short_filename(self) -> str:
        """Short customer name suitable for file names."""
        return SHORT_NAME_SPACE.sub(
            "_",
            SHORT_NAME_DISCARD.sub(
                "", SHORT_NAME_SPLIT.split(unidecode(self.name), 1)[0]
            ),
        ).strip("_")

    @property
    def country_code(self):
        if self.country:
            return self.country.code.upper()
        return None

    @property
    def legacy_address(self):
        if self.address_2:
            return f"{self.address} {self.address_2}"
        return self.address

    @property
    def legacy_city(self):
        if self.postcode:
            return f"{self.postcode} {self.city}"
        return self.city

    @property
    def vat_country_code(self):
        if self.vat:
            if hasattr(self.vat, "country_code"):
                return self.vat.country_code.upper()
            return self.vat[:2].upper()
        return None

    def clean(self) -> None:
        if self.vat and self.vat_country_code != self.country_code:
            raise ValidationError(
                {"country": gettext_lazy("The country has to match your VAT code")}
            )

    @property
    def is_empty(self) -> bool:
        return (
            not self.name
            or not self.address
            or not self.city
            or not self.postcode
            or not self.country
        )

    @property
    def is_eu_enduser(self):
        return self.country_code in EU_VAT_RATES and not self.vat

    @property
    def needs_vat(self) -> bool:
        return self.vat_country_code == "CZ" or self.is_eu_enduser

    @property
    def vat_rate(self) -> int:
        if self.needs_vat:
            return VAT_RATE
        return 0

    def get_notify_emails(self) -> list[str]:
        mails = {self.email, *self.users.values_list("email", flat=True)}
        mails.discard("")
        return list(mails)

    def send_notification(
        self, notification: str, invoice: Invoice | None = None, **kwargs
    ) -> None:
        from weblate_web.crm.models import Interaction  # noqa: PLC0415

        recipients = self.get_notify_emails()
        email = send_notification(notification, recipients, invoice=invoice, **kwargs)

        # Extract HTML content
        content = ""
        for alternative in email.alternatives:
            if (
                isinstance(alternative, (Message, EmailAlternative))
                and alternative.mimetype.startswith("text/html")
                and isinstance(alternative.content, str)
            ):
                content = alternative.content

        # Store interaction log
        interaction = self.interaction_set.create(
            origin=Interaction.Origin.EMAIL, summary=str(email.subject), content=content
        )
        # Store e-mail as attachment
        interaction.attachment.save(
            f"{notification}-{self.short_filename}-{interaction.timestamp.date().isoformat()}.eml",
            ContentFile(email.message().as_bytes()),
        )

    def merge(self, other: Customer, *, user: User | None = None) -> None:
        from weblate_web.crm.models import Interaction  # noqa: PLC0415

        other.payment_set.update(customer=self)
        other.invoice_set.update(customer=self)
        other.agreement_set.update(customer=self)
        other.donation_set.update(customer=self)
        other.service_set.update(customer=self)
        other.interaction_set.update(customer=self)
        users = list(other.users.all())
        if users:
            self.users.add(*users)
        interaction = self.interaction_set.create(
            origin=Interaction.Origin.MERGE,
            summary=f"Merged with {other.name} ({other.pk})",
            user=user,
        )
        interaction.attachment.save(
            f"customer-{other.pk}.json",
            ContentFile(serializers.serialize("json", [other])),
        )
        other.delete()


RECURRENCE_CHOICES = [
    ("y", gettext_lazy("Annual")),
    ("b", gettext_lazy("Biannual")),
    ("q", gettext_lazy("Quarterly")),
    ("m", gettext_lazy("Monthly")),
    ("", gettext_lazy("One-time")),
]


class Payment(models.Model):
    NEW = 1
    PENDING = 2
    REJECTED = 3
    ACCEPTED = 4
    PROCESSED = 5

    CURRENCY_EUR = 0
    CURRENCY_BTC = 1
    CURRENCY_USD = 2
    CURRENCY_CZK = 3
    CURRENCY_GBP = 4

    uuid = Char32UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amount = models.IntegerField()
    currency = models.IntegerField(
        choices=(
            (CURRENCY_EUR, "EUR"),
            (CURRENCY_BTC, "BTC"),
            (CURRENCY_USD, "USD"),
            (CURRENCY_CZK, "CZK"),
            (CURRENCY_GBP, "GBP"),
        ),
        default=CURRENCY_EUR,
    )
    description = models.TextField()
    recurring = models.CharField(
        choices=RECURRENCE_CHOICES, default="", blank=True, max_length=10
    )
    created = models.DateTimeField(auto_now_add=True)
    state = models.IntegerField(
        choices=[
            (NEW, pgettext_lazy("Payment state", "New payment")),
            (PENDING, pgettext_lazy("Payment state", "Awaiting payment")),
            (REJECTED, pgettext_lazy("Payment state", "Payment rejected")),
            (ACCEPTED, pgettext_lazy("Payment state", "Payment accepted")),
            (PROCESSED, pgettext_lazy("Payment state", "Payment processed")),
        ],
        db_index=True,
        default=NEW,
    )
    backend = models.CharField(max_length=100, default="", blank=True)
    # Payment details from the gateway
    details = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    # Payment extra information from the origin
    extra = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    customer = models.ForeignKey(Customer, on_delete=models.deletion.PROTECT)
    repeat = models.ForeignKey(
        "Payment", on_delete=models.deletion.PROTECT, null=True, blank=True
    )
    invoice = models.CharField(max_length=20, blank=True, default="")
    draft_invoice = models.ForeignKey(
        "invoices.Invoice",
        on_delete=models.deletion.PROTECT,
        blank=True,
        null=True,
        related_name="draft_payment_set",
    )
    paid_invoice = models.ForeignKey(
        "invoices.Invoice",
        on_delete=models.deletion.PROTECT,
        blank=True,
        null=True,
        related_name="paid_payment_set",
    )
    amount_fixed = models.BooleanField(blank=True, default=False)
    start = models.DateField(blank=True, null=True)
    end = models.DateField(blank=True, null=True)
    card_info = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = "Payment"
        verbose_name_plural = "Payments"

    def __str__(self) -> str:
        return f"payment:{self.pk}"

    def get_absolute_url(self):
        return reverse("payment", kwargs={"pk": self.pk})

    @property
    def is_waiting_for_user(self):
        """Whether payment is waiting for user action."""
        return self.state in {self.NEW, self.PENDING}

    @cached_property
    def invoice_filename(self) -> str:
        return f"{self.invoice}.pdf"

    @cached_property
    def invoice_full_filename(self):
        return os.path.join(
            settings.PAYMENT_FAKTURACE,
            "proforma" if self.state == self.PENDING else "pdf",
            self.invoice_filename,
        )

    @cached_property
    def invoice_filename_valid(self):
        return os.path.exists(self.invoice_full_filename)

    def get_amount_display(self):
        if self.currency == self.CURRENCY_BTC:
            return self.amount / 100000000
        return self.amount

    @property
    def vat_amount(self):
        if self.customer.needs_vat and not self.amount_fixed:
            rate = 100 + self.customer.vat_rate
            return round(1.0 * rate * self.amount / 100, 2)
        return self.amount

    @property
    def amount_without_vat(self):
        if self.customer.needs_vat and self.amount_fixed:
            return 100.0 * self.amount / (100 + self.customer.vat_rate)
        return self.amount

    def get_payment_url(self) -> str:
        return get_site_url("payment", strip_language=False, pk=self.pk)

    def get_complete_url(self) -> str:
        return get_site_url("payment-complete", strip_language=False, pk=self.pk)

    def is_backend_valid(self) -> bool:
        try:
            self.get_payment_backend_class()
        except KeyError:
            return False
        return True

    def get_payment_backend_class(self):
        from .backends import get_backend  # noqa: PLC0415

        return get_backend(self.backend)

    def get_payment_backend(self):
        return self.get_payment_backend_class()(self)

    def get_card_number(self) -> str:
        if self.card_info:
            card_number = self.card_info["number"]
            return " ".join(
                [card_number[i : i + 4] for i in range(0, len(card_number), 4)]
            )
        return ""

    def get_payment_description(self) -> str:
        backend_name = self.get_payment_backend_class().verbose
        if self.card_info:
            return f"{backend_name} ({self.get_card_number()})"
        return backend_name

    def repeat_payment(
        self,
        skip_previous: bool = False,
        amount: int | None = None,
        extra: dict[str, int] | None = None,
        **kwargs,
    ):
        # Check if backend is still valid
        try:
            self.get_payment_backend_class()
        except KeyError:
            return False

        with transaction.atomic():
            # Check for failed payments
            previous = Payment.objects.filter(repeat=self)
            if not skip_previous and previous.exists():
                failures = previous.filter(state=Payment.REJECTED)
                try:
                    last_good = previous.filter(state=Payment.PROCESSED).order_by(
                        "-created"
                    )[0]
                    failures = failures.filter(created__gt=last_good.created)
                except IndexError:
                    pass
                if failures.count() >= 3:
                    return False

            # Create new payment object
            if extra is None:
                extra = {}
                extra.update(self.extra)
                extra.update(kwargs)
            return Payment.objects.create(
                amount=self.amount if amount is None else amount,
                backend=self.backend,
                description=self.description,
                recurring="",
                customer=self.customer,
                amount_fixed=self.amount_fixed,
                repeat=self,
                extra=extra,
            )

    def trigger_recurring(self) -> None:
        """Trigger recurring payment."""
        from weblate_web.models import process_payment  # noqa: PLC0415

        # Initiate the payment
        with transaction.atomic():
            backend = self.get_payment_backend()
            result = backend.initiate(
                None, self.get_payment_url(), self.get_complete_url()
            )
        if result is not None:
            raise ValueError(f"Recurring backend did not complete: {result}")
        # Collect payment information
        with transaction.atomic():
            backend.complete(None)

        # Refresh from the database as initial/complete fetch a copy for select_for_update
        self.refresh_from_db()

        # Process payment
        with transaction.atomic():
            process_payment(self)


class PaymentConf(AppConf):
    DEBUG = False
    SECRET = "secret"  # noqa: S105
    FAKTURACE = None
    THEPAY_MERCHANTID = None
    THEPAY_ACCOUNTID = None
    THEPAY_PASSWORD = None
    THEPAY_DATAAPI = None
    FIO_TOKEN = None

    class Meta:
        prefix = "PAYMENT"
