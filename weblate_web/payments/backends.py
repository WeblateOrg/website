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

import re
from decimal import Decimal
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

import fiobank
import requests
import sentry_sdk
from django.conf import settings
from django.db import transaction
from django.shortcuts import redirect
from django.utils.http import http_date
from django.utils.translation import get_language, gettext, gettext_lazy

from .models import Payment
from .utils import send_notification

if TYPE_CHECKING:
    from collections.abc import Generator
    from datetime import date, datetime

    from django.http import HttpRequest, HttpResponseRedirect
    from django_stubs_ext import StrOrPromise
    from fakturace.invoices import Invoice

BACKENDS: dict[str, type[Backend]] = {}
PROFORMA_RE = re.compile("50[0-9]{8}")


def get_backend(name):
    backend = BACKENDS[name]
    if backend.debug and not settings.PAYMENT_DEBUG:
        raise KeyError("Invalid backend")
    return backend


def list_backends(exclude_names: set[str] | None = None) -> list[type[Backend]]:
    result = [
        backend
        for backend in BACKENDS.values()
        if (not backend.debug or settings.PAYMENT_DEBUG)
        and (exclude_names is None or backend.name not in exclude_names)
    ]
    return sorted(result, key=lambda x: x.name)


class InvalidState(ValueError):
    pass


class PaymentError(Exception):
    pass


def register_backend(backend: type[Backend]) -> type[Backend]:
    BACKENDS[backend.name] = backend
    return backend


class Backend:
    name: str = ""
    debug: bool = False
    verbose: StrOrPromise = ""
    description: str = ""
    recurring: bool = False

    def __init__(self, payment: Payment):
        select = Payment.objects.filter(pk=payment.pk).select_for_update()
        self.payment: Payment = select[0]
        self.invoice: Invoice | None = None

    @property
    def image_name(self) -> str:
        return f"payment/{self.name}.png"

    def perform(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> None | HttpResponseRedirect:
        """Perform payment and optionally redirects user."""
        raise NotImplementedError

    def collect(self, request: HttpRequest | None) -> bool | None:
        """Collect payment information."""
        raise NotImplementedError

    def get_instructions(self) -> list[tuple[StrOrPromise, StrOrPromise]]:
        """Payment instructions for manual methods."""
        return []

    def initiate(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> None | HttpResponseRedirect:
        """
        Initiate payment and optionally redirects user.

        Raises
        ------
            InvalidState: In case the payment can not be initiated.

        """
        if self.payment.state != Payment.NEW:
            raise InvalidState(self.payment.get_state_display())

        if self.payment.repeat and not self.recurring:
            raise InvalidState(self.payment.get_state_display())

        result = self.perform(request, back_url, complete_url)

        # Update payment state
        self.payment.state = Payment.PENDING
        self.payment.backend = self.name
        self.payment.save()

        return result

    def complete(self, request: HttpRequest | None) -> bool:
        """
        Payment completion called from returned request.

        Raises
        ------
            InvalidState: In case the payment can not be completed.

        """
        if self.payment.state not in {Payment.PENDING, Payment.REJECTED}:
            raise InvalidState(self.payment.get_state_display())

        status = self.collect(request)
        if status is None:
            return False
        if status:
            self.success()
            return True
        self.failure()
        return False

    @transaction.atomic
    def generate_invoice(self, *, proforma: bool = False) -> None:
        from weblate_web.invoices.models import (  # noqa: PLC0415
            Currency,
            Invoice,
            InvoiceCategory,
            InvoiceKind,
        )

        if self.payment.paid_invoice:
            raise ValueError("Invoice already exists!")
        invoice_kind = InvoiceKind.PROFORMA if proforma else InvoiceKind.INVOICE
        if self.payment.draft_invoice:
            # Is there already draft proforma?
            if proforma and self.payment.draft_invoice.kind == invoice_kind:
                return
            # Finalize draft if present
            invoice = self.payment.draft_invoice.duplicate(
                kind=invoice_kind,
                prepaid=not proforma,
            )
        else:
            category = InvoiceCategory.HOSTING
            if self.payment.extra.get("category") == "donate":
                category = InvoiceCategory.DONATE
            # Generate manually if no draft is present (hosted integration)
            invoice = Invoice.objects.create(
                kind=invoice_kind,
                customer=self.payment.customer,
                vat_rate=self.payment.customer.vat_rate,
                currency=Currency.EUR,
                prepaid=not proforma,
                category=category,
            )
            invoice.invoiceitem_set.create(
                description=self.payment.description,
                unit_price=round(Decimal(self.payment.amount_without_vat), 3),
            )
        if proforma:
            self.payment.draft_invoice = invoice
        else:
            self.payment.paid_invoice = invoice

        # Generate PDF
        invoice.generate_files()

        # Update reference
        self.payment.save(update_fields=["paid_invoice", "draft_invoice"])

    def send_notification(
        self, notification: str, include_invoice: bool = True
    ) -> None:
        kwargs: dict[str, Any] = {"backend": self}
        if self.invoice:
            kwargs["invoice"] = self.invoice
        if self.payment:
            kwargs["payment"] = self.payment
        send_notification(notification, [self.payment.customer.email], **kwargs)

    def get_invoice_kwargs(self):
        return {"payment_id": str(self.payment.pk), "payment_method": self.description}

    def success(self) -> None:
        self.payment.state = Payment.ACCEPTED
        if not self.recurring:
            self.payment.recurring = ""

        self.generate_invoice()
        self.payment.save()

        self.send_notification("payment_completed")

    def failure(self) -> None:
        self.payment.state = Payment.REJECTED
        self.payment.save()

        self.send_notification("payment_failed")


@register_backend
class DebugPay(Backend):
    name = "pay"
    debug = True
    verbose = "Pay"
    description = "Paid (TEST)"
    recurring = True

    def perform(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> None | HttpResponseRedirect:
        return None

    def collect(self, request: HttpRequest | None) -> bool:
        # Example data from The Pay API docs
        self.payment.card_info = {
            "number": "515735******2654",
            "expiration_date": "2022-05",
            "brand": "MASTERCARD",
            "type": "debit",
        }
        return True


@register_backend
class DebugReject(DebugPay):
    name = "reject"
    verbose = "Reject"
    description = "Reject (TEST)"
    recurring = False

    def collect(self, request: HttpRequest | None) -> bool:
        self.payment.details["reject_reason"] = "Debug reject"
        return False


@register_backend
class DebugPending(DebugPay):
    name = "pending"
    verbose = "Pending"
    description = "Pending (TEST)"
    recurring = False

    def perform(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> None | HttpResponseRedirect:
        return redirect("https://cihar.com/?url=" + complete_url)

    def collect(self, request: HttpRequest | None) -> bool:
        return True


class FioBankAPI(fiobank.FioBank):
    """
    Fio API wrapper.

    Backported API from
    https://github.com/honzajavorek/fiobank/pull/38.

    TODO: Remove this wrapper once it is released.
    """

    def _fetch_last(
        self, from_id: str | None = None, from_date: str | date | datetime | None = None
    ) -> dict:
        if from_id and from_date:
            raise ValueError("Only one constraint is allowed.")

        if from_id:
            self._request("set-last-id", from_id=from_id)
        elif from_date:
            self._request("set-last-date", from_date=fiobank.coerce_date(from_date))

        return self._request("last")

    def last(
        self, from_id: str | None = None, from_date: str | date | datetime | None = None
    ) -> Generator[dict]:
        return self._parse_transactions(self._fetch_last(from_id, from_date))

    def last_transactions(
        self, from_id: str | None = None, from_date: str | date | datetime | None = None
    ) -> tuple[dict, Generator[dict]]:
        data = self._fetch_last(from_id, from_date)
        return (self._parse_info(data), self._parse_transactions(data))


@register_backend
class FioBank(Backend):
    name = "fio-bank"
    verbose = gettext_lazy("IBAN bank transfer")
    description = "Bank transfer"
    recurring = False

    def collect(self, request: HttpRequest | None) -> bool | None:
        # We do not actually collect here, it is done in background
        if self.payment.state == Payment.PENDING:
            return None
        return True

    def perform(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> None | HttpResponseRedirect:
        # Generate proforma invoice and link it to this payment
        self.generate_invoice(proforma=True)
        # Notify user
        self.send_notification("payment_pending")
        return redirect(complete_url)

    def get_instructions(self) -> list[tuple[StrOrPromise, StrOrPromise]]:
        from weblate_web.invoices.models import Invoice  # noqa: PLC0415

        return [
            (
                gettext("Issuing bank"),
                "Fio banka, a.s., Na Florenci 2139/2, 11000 Praha, Czechia",
            ),
            (gettext("Account holder"), "Weblate s.r.o."),
            (gettext("Account number"), "2302907395 / 2010"),
            (gettext("SWIFT code"), "FIOBCZPPXXX"),
            (gettext("IBAN"), "CZ30 2010 0000 0023 0290 7395"),
            (gettext("Reference"), cast(Invoice, self.payment.draft_invoice).number),
        ]

    @classmethod
    def fetch_payments(cls, from_date: str | None = None) -> None:
        from weblate_web.invoices.models import Invoice, InvoiceKind  # noqa: PLC0415

        tokens: list[str]
        if isinstance(settings.FIO_TOKEN, str):
            tokens = [settings.FIO_TOKEN]
        else:
            tokens = settings.FIO_TOKEN
        for token in tokens:
            # TODO: change to fiobank.FioBank(token=token, decimal=True) with fiobank 4.0
            client = FioBankAPI(token=token)
            info, transactions = client.last_transactions(from_date=from_date)
            currency = info["currency"]
            for entry in transactions:
                matches = []
                # Extract from message
                if entry["recipient_message"]:
                    matches.extend(PROFORMA_RE.findall(entry["recipient_message"]))
                # Extract from variable symbol
                if entry["variable_symbol"]:
                    matches.extend(PROFORMA_RE.findall(entry["variable_symbol"]))
                # Extract from sender reference
                if entry.get("reference", None):
                    matches.extend(PROFORMA_RE.findall(entry["reference"]))
                # Extract from comment for manual pairing
                if entry["comment"]:
                    matches.extend(PROFORMA_RE.findall(entry["comment"]))
                # Process all matches
                for invoice in Invoice.objects.filter(
                    number__in=matches, kind=InvoiceKind.PROFORMA
                ):
                    # Match validation
                    if invoice.paid_payment_set.exists():
                        print(f"{invoice.number}: skipping, already paid")
                        continue
                    expected_currency = invoice.get_currency_display()
                    if expected_currency != currency:
                        print(
                            f"{invoice.number}: skipping, currency mismatch, {currency} instead of {expected_currency}"
                        )
                        continue
                    if entry["amount"] < invoice.total_amount:
                        print(
                            f"{invoice.number}: skipping, underpaid, {entry['amount']} instead of {invoice.total_amount}"
                        )
                        continue

                    # Fetch payment(s)
                    payments = invoice.draft_payment_set.all()
                    if len(payments) != 1:
                        print(
                            f"{invoice.number}: skipping, has {len(payments)} draft payments"
                        )
                        continue
                    payment = payments[0]
                    if payment.backend != cls.name:
                        print(
                            f"{invoice.number}: skipping, wrong backend: {payment.backend}"
                        )
                        continue

                    print(f"{invoice.number}: received payment")
                    # Instantionate backend (does SELECT FOR UPDATE)
                    backend = payment.get_payment_backend()
                    # Store transaction details
                    backend.payment.details["transaction"] = entry
                    backend.success()


@register_backend
class ThePay2Card(Backend):
    name = "thepay2-card"
    verbose = gettext_lazy("Payment card")
    description = "Payment Card (The Pay)"
    recurring = True

    def get_headers(self) -> dict[str, str]:
        timestamp = http_date()
        payload = f"{settings.THEPAY_MERCHANT_ID}{settings.THEPAY_PASSWORD}{timestamp}"
        hash256 = sha256(payload.encode(), usedforsecurity=True)
        return {"SignatureDate": timestamp, "Signature": hash256.hexdigest()}

    def request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json: dict | None = None,
        api_version: str = "v1",
    ) -> dict:
        if params is None:
            params = {}
        params["merchant_id"] = settings.THEPAY_MERCHANT_ID

        headers = self.get_headers()

        base_url = f"https://{settings.THEPAY_SERVER}/{api_version}/projects/{settings.THEPAY_PROJECT_ID}"

        response = requests.request(
            method,
            f"{base_url}/{url}",
            params=params,
            json=json,
            headers=headers,
            timeout=60,
        )

        # Use service specific error message if available
        if 500 > response.status_code >= 400:
            try:
                payload = response.json()
            except requests.RequestException:
                pass
            else:
                if message := payload.get("message"):
                    raise PaymentError(message)

        # Fallback to standardad requests handing
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            sentry_sdk.capture_exception()
            raise PaymentError(str(error)) from error

        return response.json()

    def perform(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> None | HttpResponseRedirect:
        payload: dict[str, str | dict[str, str | dict[str, str]] | int | bool]
        if self.payment.repeat:
            # Handle recurring payments
            payload = {
                "uid": str(self.payment.pk),
                "value": {
                    "amount": str(int(self.payment.vat_amount * 100)),
                    "currency": "EUR",
                },
                "notif_url": complete_url,
            }
            response = self.request(
                "post",
                f"payments/{self.payment.repeat.pk}/savedauthorization",
                json=payload,
                api_version="v2",
            )
            # This is processed in the collect() method
            self.payment.details["repeat_response"] = response
            return None

        # Payment payload
        payload = {
            "can_customer_change_method": False,
            "payment_method_code": "card",
            "amount": int(self.payment.vat_amount * 100),
            "currency_code": "EUR",
            "uid": str(self.payment.pk),
            "description_for_customer": self.payment.description,
            "return_url": complete_url,
            "notif_url": complete_url,
            "save_authorization": bool(self.payment.recurring),
            "language_code": get_language(),
            "customer": {
                "name": self.payment.customer.name,
                "surname": "",
                "email": self.payment.customer.email,
                "billing_address": {
                    "country_code": self.payment.customer.country.code,
                    "city": self.payment.customer.city,
                    "zip": self.payment.customer.postcode,
                    "street": self.payment.customer.address,
                },
            },
        }

        # Create payment
        response = self.request("post", "payments", json=payload)

        # Store payment URL for later
        pay_url = response["pay_url"]
        self.payment.details["pay_url"] = pay_url

        # Redirect user to perform the payment
        return redirect(pay_url)

    def collect(self, request: HttpRequest | None) -> bool | None:
        # Handle repeated payments
        if self.payment.repeat:
            if "repeat_response" not in self.payment.details:
                raise ValueError("Recurring payment without a recurring response")

            response = self.payment.details["repeat_response"]
            if response["state"] == "paid":
                return True
            self.payment.details["reject_reason"] = response.get("message")
            if not self.payment.details["reject_reason"]:
                if not response.get("parent", {}).get(
                    "recurring_payments_available", True
                ):
                    self.payment.details["reject_reason"] = (
                        "Recurring payment is no longer available"
                    )
                else:
                    self.payment.details["reject_reason"] = "Recurring payment failed"
            return False

        # Get payment state
        response = self.request("get", f"payments/{self.payment.pk}")

        self.payment.details["response"] = response

        # Extract state
        state: str = response["state"]

        # Payment completed
        if state == "paid":
            # Store card info
            if response["card"]:
                self.payment.card_info = response["card"]
            return True

        # Pending payment
        if state in {"waiting_for_payment", "waiting_for_confirmation"}:
            return None

        # All other states are assumed to be an error

        # Get error detail
        reason = ""
        if events := response.get("events"):
            last_event = events[-1]
            if event_data := last_event.get("data"):
                reason = event_data
            elif event_type := last_event.get("type"):
                if event_type == "payment_error":
                    reason = gettext("Payment error")
                elif event_type == "payment_cancelled":
                    reason = gettext("Payment cancelled")
                else:
                    # Not user friendly, but gives some clue
                    reason = event_type
        if reason:
            self.payment.details["reject_reason"] = reason
        else:
            self.payment.details["reject_reason"] = state

        return False
