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
import re
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

import fiobank
import requests
import sentry_sdk
from django.conf import settings
from django.db import transaction
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.utils.http import http_date
from django.utils.timezone import make_aware, now
from django.utils.translation import get_language, gettext, gettext_lazy
from django.views.decorators.debug import sensitive_variables

from .models import Payment

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseRedirect
    from django_stubs_ext import StrOrPromise

    from weblate_web.invoices.models import Invoice


@dataclass(frozen=True)
class DuplicatePaymentRecord:
    duplicate_key: str
    summary: str
    content: str
    details: dict[str, Any]
    paid_payment: Payment | None = None


BACKENDS: dict[str, type[Backend]] = {}
INVOICE_MATCH_RE = re.compile(
    r"(?:\b|(?<=[^0-9]))[15]0(?:[0-9] *){7}[0-9](?:\b|(?=[^0-9]))"
)
EXTRACTABLE_FIELDS: tuple[str, ...] = (
    # Extract from message
    "recipient_message",
    # Extract from variable symbol
    "variable_symbol",
    # Extract from sender reference
    "reference",
    # Extract from comment for manual pairing
    "comment",
)
SKIP_ACCOUNTS: set[str] = {
    "2400163692",
    "705-77628461",
    "CZ4906000000001610116101",
}
THEPAY_LANGUAGES = {
    "ab",
    "aa",
    "af",
    "ak",
    "sq",
    "am",
    "ar",
    "an",
    "hy",
    "as",
    "av",
    "ae",
    "ay",
    "az",
    "bm",
    "ba",
    "eu",
    "be",
    "bn",
    "bi",
    "bs",
    "br",
    "bg",
    "my",
    "ca",
    "km",
    "ch",
    "ce",
    "ny",
    "zh",
    "cu",
    "cv",
    "kw",
    "co",
    "cr",
    "hr",
    "cs",
    "da",
    "dv",
    "nl",
    "dz",
    "en",
    "eo",
    "et",
    "ee",
    "fo",  # codespell:ignore
    "fj",
    "fi",
    "fr",
    "ff",
    "gd",
    "gl",
    "lg",
    "ka",
    "de",
    "el",
    "gn",
    "gu",
    "ht",
    "ha",
    "he",
    "hz",
    "hi",
    "ho",
    "hu",
    "is",
    "io",
    "ig",
    "id",
    "ia",
    "ie",
    "iu",
    "ik",
    "ga",
    "it",
    "ja",
    "jv",
    "kl",
    "kn",
    "kr",
    "ks",
    "kk",
    "ki",
    "rw",
    "ky",
    "kv",
    "kg",
    "ko",
    "kj",
    "ku",
    "lo",
    "la",
    "lv",
    "li",
    "ln",
    "lt",
    "lu",
    "lb",
    "mk",
    "mg",
    "ms",
    "ml",
    "mt",
    "gv",
    "mi",
    "mr",
    "mh",
    "mn",
    "na",
    "nv",
    "ng",
    "ne",
    "nd",  # codespell:ignore
    "se",
    "no",
    "nb",
    "nn",
    "oc",
    "oj",
    "or",
    "om",
    "os",
    "pi",
    "ps",
    "fa",
    "pl",
    "pt",
    "pa",
    "qu",
    "ro",
    "rm",
    "rn",
    "ru",
    "sm",
    "sg",
    "sa",
    "sc",
    "sr",
    "sn",
    "ii",
    "sd",
    "si",
    "sk",
    "sl",
    "so",
    "nr",
    "st",
    "es",
    "su",
    "sw",
    "ss",
    "sv",
    "tl",
    "ty",
    "tg",
    "ta",
    "tt",
    "te",  # codespell:ignore
    "th",
    "bo",
    "ti",
    "to",
    "ts",
    "tn",
    "tr",
    "tk",
    "tw",
    "ug",
    "uk",
    "ur",
    "uz",
    "ve",
    "vi",
    "vo",
    "wa",
    "cy",
    "fy",
    "wo",
    "xh",
    "yi",
    "yo",
    "za",
    "zu",
}


def get_backend(name: str) -> type[Backend]:
    backend = BACKENDS[name]
    if backend.debug and not settings.PAYMENT_DEBUG:
        raise KeyError("Invalid backend")
    return backend


def list_backends(
    *, exclude_names: set[str] | None = None, currency: str = "EUR"
) -> list[type[Backend]]:
    return [
        backend
        for backend in BACKENDS.values()
        if (not backend.debug or settings.PAYMENT_DEBUG)
        and currency in backend.supported_currencies
        and (exclude_names is None or backend.name not in exclude_names)
    ]


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
    supported_currencies: set[str] = {"EUR", "CZK", "USD", "GBP"}

    def __init__(self, payment: Payment) -> None:
        select = Payment.objects.filter(pk=payment.pk).select_for_update()
        self.payment: Payment = select[0]
        self.invoice: Invoice | None = None

    @property
    def image_name(self) -> str:
        return f"payment/{self.name}.png"

    def perform(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> HttpResponseRedirect | None:
        """Perform payment and optionally redirects user."""
        raise NotImplementedError

    def collect(self, request: HttpRequest | None) -> bool | None:
        """Collect payment information."""
        raise NotImplementedError

    def get_instructions(self) -> list[tuple[StrOrPromise, str]]:
        """Payment instructions for manual methods."""
        return []

    def initiate(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> HttpResponseRedirect | None:
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
            return self.success()
        if self.get_duplicate_invoice_payment():
            self.failure(notify=False)
            return False
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

        generate = True

        if self.payment.paid_invoice:
            raise ValueError("Invoice already exists!")
        invoice_kind = InvoiceKind.PROFORMA if proforma else InvoiceKind.INVOICE
        if self.payment.draft_invoice:
            # Is there already draft proforma?
            if proforma and self.payment.draft_invoice.kind in {
                InvoiceKind.PROFORMA,
                InvoiceKind.INVOICE,
            }:
                return
            if not proforma and self.payment.draft_invoice.kind == invoice_kind:
                # This is a payment for existing invoice
                invoice = self.payment.draft_invoice
                generate = False
            else:
                # Finalize draft if present
                invoice = self.payment.draft_invoice.duplicate(
                    kind=invoice_kind,
                    prepaid=not proforma,
                    tax_date=self.payment.created.date(),
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
                tax_date=self.payment.created.date(),
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
            self.payment.invoice = invoice.number

        # Update the payment object so that the invoice rendering
        # can use it
        self.payment.save()

        # Generate PDF
        if generate:
            invoice.generate_files()
        if not proforma:
            invoice.generate_receipt()

    def send_notification(
        self, notification: str, include_invoice: bool = True
    ) -> None:
        kwargs: dict[str, Any] = {"backend": self}
        invoice: Invoice | None = None
        if self.payment.paid_invoice:
            invoice = self.payment.paid_invoice
        elif self.payment.draft_invoice:
            invoice = self.payment.draft_invoice
        if self.payment:
            kwargs["payment"] = self.payment
        self.payment.customer.send_notification(
            notification,
            invoice=invoice,
            **kwargs,
        )

    def get_invoice_kwargs(self):
        return {"payment_id": str(self.payment.pk), "payment_method": self.description}

    @classmethod
    def get_paid_invoice_payment(
        cls, invoice: Invoice, *, exclude_payment: Payment | None = None
    ) -> Payment | None:
        paid_payments = invoice.paid_payment_set.order_by("-created")
        if exclude_payment:
            paid_payments = paid_payments.exclude(pk=exclude_payment.pk)
        if paid_payment := paid_payments.first():
            return paid_payment

        draft_payments = invoice.draft_payment_set.filter(
            paid_invoice__isnull=False
        ).order_by("-created")
        if exclude_payment:
            draft_payments = draft_payments.exclude(pk=exclude_payment.pk)
        return draft_payments.first()

    @staticmethod
    def get_duplicate_payment_key(invoice: Invoice, payment: Payment) -> str:
        return f"{invoice.number}:payment:{payment.pk}"

    @staticmethod
    def record_duplicate_payment(
        invoice: Invoice,
        record: DuplicatePaymentRecord,
    ) -> bool:
        from weblate_web.crm.models import Interaction  # noqa: PLC0415

        paid_payment = record.paid_payment
        if paid_payment is None:
            paid_payment = Backend.get_paid_invoice_payment(invoice)

        customer = invoice.customer
        interactions = customer.interaction_set.filter(
            origin=Interaction.Origin.MANUAL_NOTE,
            details__duplicate_payment_key=record.duplicate_key,
        )
        if interactions.exists():
            return False

        follow_up_at = now()
        previous_follow_up_at = customer.follow_up_at
        previous_follow_up_note = customer.follow_up_note
        details = {
            "duplicate_payment_key": record.duplicate_key,
            "invoice": invoice.number,
            "existing_payment_id": str(paid_payment.pk) if paid_payment else "",
            "existing_payment_backend": paid_payment.backend if paid_payment else "",
            "existing_payment_state": paid_payment.state if paid_payment else "",
            "follow_up_at": follow_up_at.isoformat(),
            "follow_up_note": record.summary,
            "previous_follow_up_at": previous_follow_up_at.isoformat()
            if previous_follow_up_at
            else "",
            "previous_follow_up_note": previous_follow_up_note,
            **record.details,
        }
        customer.interaction_set.create(
            origin=Interaction.Origin.MANUAL_NOTE,
            summary=record.summary,
            content=record.content,
            details=details,
        )
        customer.follow_up_at = follow_up_at
        customer.follow_up_note = record.summary
        customer.save(update_fields=["follow_up_at", "follow_up_note"])
        return True

    def reject_duplicate_payment(self, invoice: Invoice, paid_payment: Payment) -> None:
        reason = gettext("Invoice already paid")
        summary = f"Duplicate payment for paid invoice {invoice.number}"
        self.payment.state = Payment.REJECTED
        self.payment.details["reject_reason"] = reason
        self.payment.save(update_fields=["state", "details"])
        self.record_duplicate_payment(
            invoice,
            DuplicatePaymentRecord(
                duplicate_key=Backend.get_duplicate_payment_key(invoice, self.payment),
                summary=summary,
                content=(
                    "Successful payment notification matched an invoice that already "
                    "has a paid payment."
                ),
                details={
                    "duplicate_payment_id": str(self.payment.pk),
                    "duplicate_payment_backend": self.payment.backend,
                    "duplicate_payment_state": self.payment.state,
                    "duplicate_payment_amount": str(self.payment.vat_amount),
                    "duplicate_payment_currency": self.payment.get_currency_display(),
                },
                paid_payment=paid_payment,
            ),
        )

    def get_duplicate_invoice_payment(self) -> tuple[Invoice, Payment] | None:
        if self.payment.draft_invoice is None:
            return None
        paid_payment = self.get_paid_invoice_payment(
            self.payment.draft_invoice, exclude_payment=self.payment
        )
        if paid_payment is None:
            return None
        return self.payment.draft_invoice, paid_payment

    def success(self) -> bool:
        if duplicate_invoice_payment := self.get_duplicate_invoice_payment():
            invoice, paid_payment = duplicate_invoice_payment
            self.reject_duplicate_payment(invoice, paid_payment)
            return False

        self.payment.state = Payment.ACCEPTED
        if not self.recurring:
            self.payment.recurring = ""

        self.generate_invoice()
        self.payment.save()

        self.send_notification("payment_completed")
        return True

    def failure(self, *, notify: bool = True) -> None:
        self.payment.state = Payment.REJECTED
        self.payment.save()

        if notify:
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
    ) -> HttpResponseRedirect | None:
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
    ) -> HttpResponseRedirect | None:
        return redirect(f"https://cihar.com/?url={complete_url}")

    def collect(self, request: HttpRequest | None) -> bool:
        return True


@register_backend
class ManualPayment(Backend):
    name = "manual"
    verbose = gettext_lazy("Manual payment")
    description = "Manual payment"
    recurring = False
    supported_currencies: set[str] = set()

    def perform(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> HttpResponseRedirect | None:
        raise PaymentError("Manual payments cannot be initiated")

    def collect(self, request: HttpRequest | None) -> bool | None:
        raise PaymentError("Manual payments cannot be completed")


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
    ) -> HttpResponseRedirect | None:
        # Generate proforma invoice and link it to this payment
        self.generate_invoice(proforma=True)
        # Notify user
        self.send_notification("payment_pending")
        return redirect(complete_url)

    def get_instructions(self) -> list[tuple[StrOrPromise, str]]:
        invoice = cast("Invoice", self.payment.draft_invoice)
        instructions = invoice.bank_account.get_full_info()
        instructions.append((gettext("Reference"), invoice.number))
        return instructions

    @classmethod
    def get_invoice_payment(cls, invoice: Invoice) -> Payment:
        payment = (
            invoice.draft_payment_set.filter(
                backend=cls.name,
                paid_invoice__isnull=True,
                state__in={Payment.NEW, Payment.PENDING, Payment.REJECTED},
            )
            .order_by("-created", "-pk")
            .first()
        )
        if payment is not None:
            return payment
        return invoice.create_payment(backend=cls.name)

    @classmethod
    def get_duplicate_bank_payment_key(
        cls, invoice: Invoice, entry: dict[str, Any], amount: Decimal, currency: str
    ) -> str:
        if transaction_id := entry.get("transaction_id"):
            return f"{invoice.number}:{transaction_id}"
        account = entry.get("account_number_full") or entry.get("account_number") or ""
        message = entry.get("recipient_message") or ""
        return f"{invoice.number}:{entry['date'].isoformat()}:{amount}:{currency}:{account}:{message}"

    @staticmethod
    def is_same_bank_transaction(
        payment: Payment, entry: dict[str, Any], currency: str
    ) -> bool:
        stored_transaction = payment.details.get("transaction")
        if not isinstance(stored_transaction, dict):
            return False
        if transaction_id := entry.get("transaction_id"):
            return stored_transaction.get("transaction_id") == transaction_id
        stored_account = stored_transaction.get(
            "account_number_full"
        ) or stored_transaction.get("account_number")
        account = entry.get("account_number_full") or entry.get("account_number")
        return (
            str(stored_transaction.get("date")) == entry["date"].isoformat()
            and str(stored_transaction.get("amount")) == str(entry["amount"])
            and payment.details.get("transaction_currency") == currency
            and stored_account == account
            and stored_transaction.get("recipient_message")
            == entry.get("recipient_message")
        )

    @classmethod
    def record_duplicate_bank_payment(
        cls, invoice: Invoice, entry: dict[str, Any], amount: Decimal, currency: str
    ) -> None:
        paid_payment = cls.get_paid_invoice_payment(invoice)
        if (
            paid_payment is not None
            and paid_payment.backend == cls.name
            and cls.is_same_bank_transaction(paid_payment, entry, currency)
        ):
            print(f"{invoice.number}: skipping, already paid")
            return

        duplicate_key = cls.get_duplicate_bank_payment_key(
            invoice, entry, amount, currency
        )
        summary = f"Duplicate bank transfer for paid invoice {invoice.number}"
        created = Backend.record_duplicate_payment(
            invoice,
            DuplicatePaymentRecord(
                duplicate_key=duplicate_key,
                summary=summary,
                content=(
                    "Incoming bank transfer matched an invoice that already has "
                    "a paid payment."
                ),
                details={
                    "duplicate_bank_payment_key": duplicate_key,
                    "amount": str(amount),
                    "currency": currency,
                    "date": entry["date"].isoformat(),
                    "transaction_id": entry.get("transaction_id"),
                    "account_number": entry.get("account_number"),
                    "account_number_full": entry.get("account_number_full"),
                    "account_name": entry.get("account_name"),
                    "bank_code": entry.get("bank_code"),
                    "transaction": entry,
                },
                paid_payment=paid_payment,
            ),
        )
        if created:
            print(f"{invoice.number}: duplicate bank payment noted")
        else:
            print(f"{invoice.number}: skipping, already paid (duplicate noted)")

    @classmethod
    @method_decorator(sensitive_variables("tokens", "token"))
    def fetch_payments(cls, from_date: str | None = None) -> None:
        from weblate_web.invoices.models import Invoice, InvoiceKind  # noqa: PLC0415

        tokens: list[str]
        if isinstance(settings.FIO_TOKEN, str):
            tokens = [settings.FIO_TOKEN]
        else:
            tokens = settings.FIO_TOKEN
        for token in tokens:
            client = fiobank.FioBank(token=token, decimal=True)
            try:
                info, transactions = client.last_transactions(from_date=from_date)
            except requests.RequestException as error:
                sentry_sdk.capture_exception()
                print(f"Failed to fetch payments: {error}")
                continue

            currency: str = info["currency"]
            for entry in transactions:
                amount: Decimal = entry["amount"]

                # Skip outgoing payments
                if amount < 0:
                    continue

                # Extract possible invoice IDs
                matches: list[str] = []
                for field in EXTRACTABLE_FIELDS:
                    if value := entry.get(field, None):
                        matches.extend(
                            match.replace(" ", "")
                            for match in INVOICE_MATCH_RE.findall(value)
                        )

                processed = False
                duplicate_matches: list[Invoice] = []

                # Process all matches
                for invoice in Invoice.objects.filter(
                    number__in=matches,
                    kind__in=(InvoiceKind.PROFORMA, InvoiceKind.INVOICE),
                ):
                    # Match validation
                    expected_currency = invoice.get_currency_display()
                    if expected_currency != currency:
                        print(
                            f"{invoice.number}: skipping, currency mismatch, {currency} instead of {expected_currency}"
                        )
                        continue
                    comment = entry.get("comment") or ""
                    if amount < invoice.total_amount and "[underpaid]" not in comment:
                        print(
                            f"{invoice.number}: skipping, underpaid, {amount} instead of {invoice.total_amount}"
                        )
                        continue
                    if (
                        invoice.paid_payment_set.exists()
                        or invoice.draft_payment_set.filter(
                            paid_invoice__isnull=False
                        ).exists()
                    ):
                        duplicate_matches.append(invoice)
                        continue

                    # Fetch payment(s)
                    payment = cls.get_invoice_payment(invoice)

                    print(f"{invoice.number}: received payment")

                    # Instantionate backend (does SELECT FOR UPDATE)
                    backend = payment.get_payment_backend()

                    # Sync payment date with the actual payment
                    if backend.payment.created.date() != entry["date"]:
                        # Make timezone aware datetime out of date object
                        backend.payment.created = make_aware(
                            datetime.datetime.combine(entry["date"], datetime.time.min)
                        )
                        # Saved later via backend.success()

                    # Store transaction details
                    backend.payment.details["transaction"] = entry
                    backend.payment.details["transaction_currency"] = currency
                    # Saved later via backend.success()

                    # Complete processing and save updated payment
                    backend.success()
                    processed = True
                    break

                if not processed and duplicate_matches:
                    for invoice in duplicate_matches:
                        cls.record_duplicate_bank_payment(
                            invoice, entry, amount, currency
                        )
                    processed = True

                # Warn about not processed payment
                if not processed and entry["account_number"] not in SKIP_ACCOUNTS:
                    print(
                        f"Unprocessed incoming payment {amount} {currency}  {entry['account_name']}"
                    )


@register_backend
class ThePay2Card(Backend):
    name = "thepay2-card"
    verbose = gettext_lazy("Payment card")
    description = "Payment Card (The Pay)"
    recurring = True
    thepay_method_code = "card"
    supported_currencies: set[str] = {"EUR", "CZK", "GBP", "USD"}

    @staticmethod
    def get_headers() -> dict[str, str]:
        timestamp = http_date()
        payload = f"{settings.THEPAY_MERCHANT_ID}{settings.THEPAY_PASSWORD}{timestamp}"
        hash256 = sha256(payload.encode(), usedforsecurity=True)
        return {"SignatureDate": timestamp, "Signature": hash256.hexdigest()}

    @classmethod
    def request(
        cls,
        method: str,
        url: str,
        params: dict | None = None,
        json: dict | None = None,
        api_version: str = "v1",
    ) -> dict:
        if params is None:
            params = {}
        params["merchant_id"] = settings.THEPAY_MERCHANT_ID

        headers = cls.get_headers()

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

    def get_language(self) -> str:
        language = get_language().lower().replace("_", "-").split("-")[0]
        if language not in THEPAY_LANGUAGES:
            return "en"
        return language

    def perform(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> HttpResponseRedirect | None:
        payload: dict[str, str | dict[str, str | dict[str, str]] | int | bool]
        if self.payment.repeat:
            # Handle recurring payments
            payload = {
                "uid": str(self.payment.pk),
                "value": {
                    "amount": str(int(self.payment.vat_amount * 100)),
                    "currency": self.payment.get_currency_display(),
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
            "is_customer_notification_enabled": False,
            "payment_method_code": self.thepay_method_code,
            "amount": int(self.payment.vat_amount * 100),
            "currency_code": self.payment.get_currency_display(),
            "uid": str(self.payment.pk),
            "description_for_customer": self.payment.description,
            "return_url": complete_url,
            "notif_url": complete_url,
            "save_authorization": bool(self.payment.recurring),
            "language_code": self.get_language(),
            "customer": {
                "name": self.payment.customer.name,
                "surname": "",
                "email": self.payment.customer.email,
            },
        }
        if self.payment.customer.address:
            payload["customer"]["billing_address"] = {  # type: ignore[index]
                "country_code": self.payment.customer.country.code,
                "city": self.payment.customer.city,
                "zip": self.payment.customer.postcode,
                "street": self.payment.customer.address,
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
            if card_info := response.get("card"):
                self.payment.card_info = card_info
            return True

        # Pending payment, expired link
        if state in {"waiting_for_payment", "waiting_for_confirmation", "expired"}:
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


@register_backend
class ThePay2Bitcoin(ThePay2Card):
    name = "thepay2-bitcoin"
    verbose = gettext_lazy("Crypto payment")
    description = "Bitcoin (The Pay)"
    recurring = False
    thepay_method_code = "bitcoin"
    supported_currencies: set[str] = {"EUR", "CZK"}


@register_backend
class ThePay2GooglePay(ThePay2Card):
    name = "thepay2-gpay"
    verbose = gettext_lazy("Google Pay")
    description = "Google Pay (The Pay)"
    thepay_method_code = "google_pay"


@register_backend
class ThePay2ApplePay(ThePay2Card):
    name = "thepay2-apay"
    verbose = gettext_lazy("Apple Pay")
    description = "Apple Pay (The Pay)"
    thepay_method_code = "apple_pay"
