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
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

import fiobank
import requests
import sentry_sdk
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.utils.http import http_date
from django.utils.timezone import make_aware, now
from django.utils.translation import get_language, gettext, gettext_lazy
from django.views.decorators.debug import sensitive_variables

from weblate_web.categories import ServiceCategory, category_from_metadata
from weblate_web.utils import get_site_url

from .models import CustomerFollowUp, Payment

if TYPE_CHECKING:
    from collections.abc import Callable

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
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_message: str | None = None,
        unavailable_until: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.provider_message = provider_message
        self.unavailable_until = unavailable_until
        if status_code is not None:
            message = f"{message} (HTTP {status_code})"
        if provider_message:
            message = f"{message}: {provider_message}"
        if unavailable_until:
            message = f"{message} (Unavailable-Until: {unavailable_until})"
        super().__init__(message)


class PaymentMethodUnavailable(PaymentError):
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
    verifies_cancelled_payments: bool = False
    supported_currencies: set[str] = {"EUR", "CZK", "USD", "GBP"}

    def __init__(self, payment: Payment) -> None:
        from weblate_web.invoices.models import Invoice  # ruff:ignore[import-outside-top-level]

        # Match correction and bank reconciliation lock order: invoices, then payment.
        related = Payment.objects.get(pk=payment.pk)
        list(
            Invoice.objects.select_for_update()
            .filter(pk__in=[related.draft_invoice_id, related.paid_invoice_id])
            .order_by("pk")
        )
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

        if (
            self.payment.amount_fixed
            and not self.payment.draft_invoice_id
            and not self.payment.paid_invoice_id
        ):
            self.payment.normalize_fixed_amount()
            self.payment.save(update_fields=["amount", "requested_amount"])

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
        return self._complete(lambda: self.collect(request))

    def _complete(self, collect: Callable[[], bool | None]) -> bool:
        if self.payment.state == Payment.CANCELLED:
            if self.verifies_cancelled_payments:
                status = collect()
                self.payment.save(update_fields=["details", "card_info"])
                if status:
                    self.success()
            return False
        if self.payment.state not in {Payment.PENDING, Payment.REJECTED}:
            raise InvalidState(self.payment.get_state_display())

        status = collect()
        if status is None:
            self.payment.save(update_fields=["state", "details", "card_info"])
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
        from weblate_web.invoices.models import (  # ruff:ignore[import-outside-top-level]
            Currency,
            Invoice,
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
            from weblate_web.models import (  # ruff:ignore[import-outside-top-level]
                Package,
                Subscription,
            )

            subscription_id = self.payment.extra.get("subscription")
            subscriptions = (
                dict(
                    Subscription.objects.filter(pk=subscription_id).values_list(
                        "pk", "package__category"
                    )
                )
                if isinstance(subscription_id, int)
                and not isinstance(subscription_id, bool)
                else {}
            )
            category = category_from_metadata(
                self.payment.extra,
                dict(Package.objects.values_list("name", "category")),
                subscriptions,
            )
            if category is None:
                category = ServiceCategory.HOSTING
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
                unit_price=self.payment.amount_without_vat.quantize(
                    Decimal("0.001"), rounding=ROUND_HALF_UP
                ),
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
        # ruff:ignore[import-outside-top-level]
        from weblate_web.crm.models import Interaction

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

        details = {
            "duplicate_payment_key": record.duplicate_key,
            "invoice": invoice.number,
            "existing_payment_id": str(paid_payment.pk) if paid_payment else "",
            "existing_payment_backend": paid_payment.backend if paid_payment else "",
            "existing_payment_state": paid_payment.state if paid_payment else "",
            **record.details,
        }
        customer.interaction_set.create(
            origin=Interaction.Origin.MANUAL_NOTE,
            summary=record.summary,
            content=record.content,
            details=details,
        )
        CustomerFollowUp.objects.create(
            customer=customer,
            follow_up_at=now(),
            note=record.summary,
            type=CustomerFollowUp.Type.DUPLICATE_PAYMENT,
            details=details,
        )
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
        invoice = self.payment.draft_invoice or self.payment.paid_invoice
        if self.payment.state == Payment.CANCELLED or (
            invoice is not None and invoice.is_credited
        ):
            if invoice is not None:
                self.record_duplicate_payment(
                    invoice,
                    DuplicatePaymentRecord(
                        duplicate_key=f"cancelled-payment:{self.payment.pk}",
                        summary=f"Payment received for cancelled invoice payment {self.payment.pk}",
                        content=(
                            "The payment has not been applied. Reconcile the received amount manually."
                        ),
                        details={
                            "payment_id": str(self.payment.pk),
                            "backend": self.payment.backend,
                            "amount": str(self.payment.vat_amount),
                            "currency": self.payment.get_currency_display(),
                            "response": self.payment.details,
                        },
                    ),
                )
            self.payment.state = Payment.CANCELLED
            self.payment.save(update_fields=["state", "details", "card_info"])
            return False
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
        already_rejected = self.payment.state == Payment.REJECTED
        self.payment.state = Payment.REJECTED
        self.payment.save()

        if notify and not already_rejected:
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

    def collect(self, request: HttpRequest | None) -> None:
        # Only bank statement reconciliation can confirm a transfer.
        return None

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
            invoice.validate_hosted_renewal_payment(payment)
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
    def record_credited_invoice_bank_payment(
        cls, invoice: Invoice, entry: dict[str, Any], amount: Decimal, currency: str
    ) -> None:
        """Keep the whole transfer for manual reconciliation after a correction."""
        Backend.record_duplicate_payment(
            invoice,
            DuplicatePaymentRecord(
                duplicate_key=cls.get_duplicate_bank_payment_key(
                    invoice, entry, amount, currency
                ),
                summary=f"Bank transfer for fully credited invoice {invoice.number}",
                content=(
                    "The transfer has not been applied because the invoice is fully "
                    "credited. Reconcile the received amount manually."
                ),
                details={
                    "amount": str(amount),
                    "currency": currency,
                    "remaining_amount": "0.00",
                    "excess_amount": str(amount),
                    "transaction": entry,
                },
            ),
        )

    @staticmethod
    def matches_bank_payment(
        invoice: Invoice, entry: dict[str, Any], currency: str
    ) -> bool:
        expected_currency = invoice.get_currency_display()
        if expected_currency != currency:
            print(
                f"{invoice.number}: skipping, currency mismatch, {currency} instead of {expected_currency}"
            )
            return False
        # Only bank staff can edit this comment, unlike the sender's message.
        comment = entry.get("comment") or ""
        amount = entry["amount"]
        if (
            not invoice.is_credited
            and amount < invoice.total_amount
            and "[underpaid]" not in comment
        ):
            print(
                f"{invoice.number}: skipping, underpaid, {amount} instead of {invoice.total_amount}"
            )
            return False
        return True

    @classmethod
    @transaction.atomic
    def reconcile_bank_transfer(cls, entry: dict[str, Any], currency: str) -> None:
        """Lock matched invoices before checking and applying an incoming transfer."""
        from weblate_web.invoices.models import (  # ruff:ignore[import-outside-top-level]
            HostedRenewalValidationError,
            Invoice,
            InvoiceKind,
        )

        amount: Decimal = entry["amount"]
        # Extract possible invoice IDs
        matches: list[str] = []
        for field in EXTRACTABLE_FIELDS:
            if value := entry.get(field):
                matches.extend(
                    match.replace(" ", "") for match in INVOICE_MATCH_RE.findall(value)
                )

        processed = False
        duplicate_matches: list[Invoice] = []
        invalid_renewals: list[tuple[Invoice, str]] = []

        # Process all matches
        for invoice in (
            Invoice.objects.select_for_update()
            .filter(
                number__in=matches,
                correction_of=None,
                kind__in=(InvoiceKind.PROFORMA, InvoiceKind.INVOICE),
            )
            .order_by("pk")
        ):
            if not cls.matches_bank_payment(invoice, entry, currency):
                continue
            if (
                invoice.paid_payment_set.exists()
                or invoice.draft_payment_set.filter(paid_invoice__isnull=False).exists()
            ):
                duplicate_matches.append(invoice)
                continue

            if invoice.is_credited:
                cls.record_credited_invoice_bank_payment(
                    invoice, entry, amount, currency
                )
                processed = True
                break

            # Fetch payment(s)
            try:
                payment = cls.get_invoice_payment(invoice)
            except HostedRenewalValidationError as error:
                invalid_renewals.append((invoice, str(error)))
                print(f"{invoice.number}: skipping, {error}")
                continue

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
                cls.record_duplicate_bank_payment(invoice, entry, amount, currency)
            processed = True

        if not processed and invalid_renewals:
            invoice = invalid_renewals[0][0]
            Backend.record_duplicate_payment(
                invoice,
                DuplicatePaymentRecord(
                    duplicate_key=cls.get_duplicate_bank_payment_key(
                        invoice, entry, amount, currency
                    ),
                    summary=f"Unapplied bank transfer for Hosted renewal invoice {invoice.number}",
                    content="Review the Hosted renewal details before applying this transfer.",
                    details={
                        "transaction": entry,
                        "amount": str(amount),
                        "currency": currency,
                        "rejected_invoices": [
                            {"invoice_id": str(rejected.pk), "reason": reason}
                            for rejected, reason in invalid_renewals
                        ],
                    },
                ),
            )
            processed = True

        # Warn about not processed payment
        if not processed and entry["account_number"] not in SKIP_ACCOUNTS:
            print(
                f"Unprocessed incoming payment {amount} {currency}  {entry['account_name']}"
            )

    @classmethod
    @method_decorator(sensitive_variables("tokens", "token"))
    def fetch_payments(cls, from_date: str | None = None) -> None:
        tokens: list[str]
        if isinstance(settings.FIO_TOKEN, str):
            tokens = [settings.FIO_TOKEN]
        else:
            tokens = settings.FIO_TOKEN
        for token in tokens:
            client = fiobank.FioBank(token=token, decimal=True)
            try:
                info, transactions = client.last_transactions(from_date=from_date)
            except (fiobank.ThrottlingError, requests.RequestException) as error:
                sentry_sdk.capture_exception()
                print(f"Failed to fetch payments: {error}")
                continue

            currency: str = info["currency"]
            for entry in transactions:
                amount: Decimal = entry["amount"]

                # Skip outgoing payments
                if amount < 0:
                    continue

                cls.reconcile_bank_transfer(entry, currency)


@register_backend
class ThePay2Card(Backend):
    verifies_cancelled_payments = True
    name = "thepay2-card"
    verbose = gettext_lazy("Payment card")
    description = "Payment Card (The Pay)"
    recurring = True
    thepay_method_code = "card"
    supported_currencies: set[str] = {"EUR", "CZK", "GBP", "USD"}
    recurring_states = frozenset({"paid", "error", "waiting_for_confirmation"})
    pending_payment_states = frozenset(
        {"waiting_for_payment", "waiting_for_confirmation", "preauthorized"}
    )
    failed_payment_states = frozenset(
        {
            "error",
            "expired",
            # Older checkout flows can report these cancellation states.
            "cancelled",
            "payment_cancelled",
        }
    )
    closed_preauthorization_states = frozenset({"preauth_cancelled", "preauth_expired"})
    refund_states = frozenset({"partially_refunded", "refunded"})
    validate_pay_url = URLValidator(schemes=["http", "https"])

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
    ) -> dict[str, Any]:
        base_url = f"https://{settings.THEPAY_SERVER}/{api_version}/projects/{settings.THEPAY_PROJECT_ID}"

        transport_error: requests.RequestException | None = None
        try:
            response = requests.request(
                method,
                f"{base_url}/{url}",
                params={**(params or {}), "merchant_id": settings.THEPAY_MERCHANT_ID},
                json=json,
                headers=cls.get_headers(),
                timeout=60,
            )
        except requests.RequestException as error:
            if error.response is None:
                raise PaymentError("Could not reach ThePay") from error
            response = error.response
            transport_error = error

        decode_error: ValueError | None = None
        try:
            payload = response.json()
        except ValueError as error:
            payload = None
            decode_error = error

        status_code = response.status_code
        if transport_error is not None:
            diagnostic = "ThePay request failed"
        elif not 200 <= status_code < 300:
            diagnostic = "ThePay returned an unexpected HTTP status"
        elif decode_error is not None:
            diagnostic = "ThePay returned invalid JSON"
        elif not isinstance(payload, dict):
            diagnostic = "ThePay response must be a JSON object"
        elif field := cls.invalid_response_field(method.lower(), url, payload):
            diagnostic = f"Invalid or missing ThePay response field: {field}"
        else:
            return payload

        message = payload.get("message") if isinstance(payload, dict) else None
        method_code = json.get("payment_method_code") if json is not None else None
        error_type = PaymentError
        if (
            status_code == HTTPStatus.FORBIDDEN
            and method.lower() == "post"
            and url == "payments"
            and isinstance(method_code, str)
            and message
            == f"Payment method `{method_code}` is not available for this payment."
        ):
            error_type = PaymentMethodUnavailable
        raise error_type(
            diagnostic,
            status_code=status_code,
            provider_message=message
            if isinstance(message, str) and message.strip()
            else None,
            unavailable_until=response.headers.get("Unavailable-Until"),
        ) from (transport_error or decode_error)

    @classmethod
    def invalid_response_field(
        cls, method: str, url: str, payload: dict[str, Any]
    ) -> str | None:
        if method == "post" and url == "payments":
            pay_url = payload.get("pay_url")
            if not isinstance(pay_url, str):
                return "pay_url"
            try:
                cls.validate_pay_url(pay_url)
            except ValidationError:
                return "pay_url"
        elif method == "post" and url.endswith("/savedauthorization"):
            return cls.invalid_recurring_result_field(payload)
        elif method == "get" and url.startswith("payments/"):
            return cls.invalid_payment_detail_field(payload)
        return None

    @classmethod
    def invalid_payment_detail_field(cls, payload: dict[str, Any]) -> str | None:
        state = payload.get("state")
        if not isinstance(state, str) or not state.strip():
            return "state"
        if state == "paid":
            card = payload.get("card")
            if card is not None and (
                not isinstance(card, dict)
                or not isinstance(number := card.get("number"), str)
                or not re.fullmatch(r"[0-9*]{1,19}", number)
            ):
                return "card.number"
        events = payload.get("events", [])
        if not isinstance(events, list) or any(
            not isinstance(event, dict)
            or not isinstance(event.get("type"), str)
            or not event["type"].strip()
            or (event.get("data") is not None and not isinstance(event["data"], str))
            for event in events
        ):
            return "events"
        return cls.invalid_recurring_metadata_field(payload, allow_null_parent=True)

    @classmethod
    def invalid_recurring_result_field(cls, payload: dict[str, Any]) -> str | None:
        state = payload.get("state")
        if not isinstance(state, str) or state not in cls.recurring_states:
            return "state"
        return cls.invalid_recurring_metadata_field(payload)

    @staticmethod
    def invalid_recurring_metadata_field(
        payload: dict[str, Any], *, allow_null_parent: bool = False
    ) -> str | None:
        message = payload.get("message")
        if message is not None and not isinstance(message, str):
            return "message"
        if "parent" in payload:
            parent = payload["parent"]
            if parent is None and allow_null_parent:
                return None
            if not isinstance(parent, dict) or not isinstance(
                parent.get("recurring_payments_available"), bool
            ):
                return "parent.recurring_payments_available"
        return None

    def get_language(self) -> str:
        language = get_language().lower().replace("_", "-").split("-")[0]
        if language not in THEPAY_LANGUAGES:
            return "en"
        return language

    def saved_authorization_payload(self) -> dict[str, Any]:
        amount = self.payment.vat_amount_minor
        currency = self.payment.get_currency_display()
        if (invoice := self.payment.draft_invoice) is not None and (
            amount != int(invoice.total_amount * 100)
            or currency != invoice.get_currency_display()
        ):
            raise PaymentError("Recurring charge does not match the invoice")
        return {
            "uid": str(self.payment.pk),
            "value": {"amount": str(amount), "currency": currency},
            "notif_url": get_site_url("thepay-notification", pk=self.payment.pk),
        }

    def charge_saved_authorization(self, payload: dict[str, Any]) -> None:
        response = self.request(
            "post",
            f"payments/{self.payment.repeat_id}/savedauthorization",
            json=payload,
            api_version="v2",
        )
        self.payment.details["repeat_response"] = response
        self.record_recurring_availability(
            response, failed=response["state"] == "error"
        )

    def perform(
        self, request: HttpRequest | None, back_url: str, complete_url: str
    ) -> HttpResponseRedirect | None:
        self.payment.details["thepay_language"] = (
            get_language() or settings.LANGUAGE_CODE
        )
        if self.payment.repeat:
            self.charge_saved_authorization(self.saved_authorization_payload())
            return None

        # Payment payload
        payload: dict[str, str | dict[str, str | dict[str, str]] | int | bool] = {
            "can_customer_change_method": False,
            "is_customer_notification_enabled": False,
            "payment_method_code": self.thepay_method_code,
            "amount": self.payment.vat_amount_minor,
            "currency_code": self.payment.get_currency_display(),
            "uid": str(self.payment.pk),
            "description_for_customer": self.payment.description,
            "return_url": complete_url,
            "notif_url": get_site_url("thepay-notification", pk=self.payment.pk),
            "save_authorization": bool(self.payment.recurring and self.recurring),
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
        # The inline completion uses the result of charging the requested value.
        if (
            self.payment.repeat
            and not self.payment.is_under_settlement_review
            and (
                request is None
                or (
                    self.payment.state == Payment.REJECTED
                    and self.payment.has_unavailable_recurring_authorization
                )
            )
        ):
            return self.collect_recurring_result()

        return self._collect_payment_detail()

    def complete_notification(self) -> bool:
        return self._complete(self._collect_payment_detail)

    def _collect_payment_detail(self) -> bool | None:
        response = self.request("get", f"payments/{self.payment.pk}")
        previous_response = self.payment.details.get("response")
        self.payment.details["response"] = response
        state: str = response["state"]
        self.record_recurring_availability(
            response,
            failed=(
                state in self.failed_payment_states
                and not self.payment.is_under_settlement_review
                and self.payment.state != Payment.CANCELLED
            ),
        )

        if state == "paid":
            if not self.validate_settlement(response):
                return None
            # Store card info
            if card_info := response.get("card"):
                self.payment.card_info = card_info
            return True

        if state in self.refund_states:
            if not self.payment.is_under_settlement_review:
                self.hold_settlement(
                    {"reason": state},
                    content="The payment provider reported a refund. Reconcile the payment and refund before applying the payment.",
                )
        elif state in self.closed_preauthorization_states:
            if (
                not self.payment.is_under_settlement_review
                and self.payment.state != Payment.CANCELLED
            ):
                self.payment.details["reject_reason"] = state
                self.payment.state = Payment.REJECTED
        elif state in self.failed_payment_states:
            if not self.payment.is_under_settlement_review:
                if self.payment.repeat:
                    self.record_recurring_failure(response)
                else:
                    self.payment.details["reject_reason"] = self.payment_failure_reason(
                        response
                    )
                return False
        elif state not in self.pending_payment_states and (
            not isinstance(previous_response, dict)
            or previous_response.get("state") != state
        ):
            sentry_sdk.capture_message(
                "Unrecognized ThePay payment-detail state",
                level="warning",
                extras={"state": state, "payment_id": str(self.payment.pk)},
            )
        return None

    @staticmethod
    def payment_failure_reason(response: dict[str, Any]) -> str:
        if response["state"] == "expired":
            return "expired"
        if events := response.get("events"):
            last_event = events[-1]
            if event_data := last_event.get("data"):
                return event_data
            match last_event["type"]:
                case "payment_error":
                    return "Payment error"
                case "payment_cancelled":
                    return "Payment cancelled"
                case event_type:
                    return event_type
        return response["state"]

    def collect_recurring_result(self) -> bool | None:
        if self.payment.has_unavailable_recurring_authorization:
            return False
        response = self.payment.details.get("repeat_response", {})
        if not isinstance(response, dict) or self.invalid_recurring_result_field(
            response
        ):
            sentry_sdk.capture_message(
                "Malformed stored ThePay recurring payment response"
            )
            return None
        state = response["state"]
        self.record_recurring_availability(response, failed=state == "error")
        if state == "paid":
            return True
        if state == "error":
            self.record_recurring_failure(response)
            return False
        return None

    def record_recurring_availability(
        self, response: dict[str, Any], *, failed: bool
    ) -> None:
        parent = response.get("parent") or {}
        if (
            parent.get("recurring_payments_available") is False
            and (parent_payment := self.payment.repeat) is not None
        ):
            self.payment.details[Payment.RECURRING_AUTHORIZATION_FAILURE] = True
            parent_payment.disable_recurring(
                self.payment,
                reason=Payment.RECURRING_AUTHORIZATION_FAILURE,
                failed=failed,
            )

    def record_recurring_failure(self, response: dict[str, Any]) -> None:
        unavailable = self.payment.details.get(Payment.RECURRING_AUTHORIZATION_FAILURE)
        if unavailable and self.payment.state != Payment.CANCELLED:
            self.payment.details["failure_code"] = (
                Payment.RECURRING_AUTHORIZATION_FAILURE
            )

        if response["state"] == "expired":
            reason = "expired"
        elif message := response.get("message"):
            reason = message
        elif unavailable:
            reason = "Recurring payment is no longer available"
        else:
            reason = "Recurring payment failed"
        self.payment.details["reject_reason"] = reason

    def validate_settlement(self, response: dict[str, Any]) -> bool:
        """Check received money against the invoice, independently of live VAT data."""
        invoice = self.payment.draft_invoice
        if invoice is None:
            return True

        expected_amount = int(invoice.total_amount * 100)
        expected_currency = invoice.get_currency_display()
        paid_amount = response.get("paid_amount")
        currency = response.get("currency")
        reason = ""
        if (
            not isinstance(paid_amount, int)
            or isinstance(paid_amount, bool)
            or paid_amount < 0
        ):
            reason = "invalid_paid_amount"
        elif not isinstance(currency, str):
            reason = "invalid_currency"
        elif currency != expected_currency:
            reason = "currency_mismatch"
        elif paid_amount < expected_amount:
            reason = "underpayment"

        if reason:
            self.hold_settlement(
                {
                    "reason": reason,
                    "expected_amount": expected_amount,
                    "expected_currency": expected_currency,
                    "paid_amount": paid_amount,
                    "currency": currency,
                }
            )
            return False

        if self.payment.is_under_settlement_review:
            del self.payment.details["settlement_review"]
            self.payment.customer.followups.filter(
                type=CustomerFollowUp.Type.MANUAL,
                details__settlement_review_payment=str(self.payment.pk),
            ).delete()
        return True

    def hold_settlement(
        self,
        review: dict[str, Any],
        *,
        content: str = "The received amount or currency could not be matched to the invoice. Review the provider response before applying the payment.",
    ) -> None:
        # ruff:ignore[import-outside-top-level]
        from weblate_web.crm.models import Interaction

        self.payment.details["settlement_review"] = review
        if self.payment.state != Payment.CANCELLED:
            self.payment.state = Payment.PENDING
        details = {
            "settlement_review_payment": str(self.payment.pk),
            "invoice_id": str(self.payment.draft_invoice_id),
            "backend": self.name,
            **review,
        }
        summary = f"Payment {self.payment.pk} requires settlement review"
        # Backend initialization locks the payment, serializing repeat callbacks.
        interactions = self.payment.customer.interaction_set.filter(
            origin=Interaction.Origin.MANUAL_NOTE,
            details__settlement_review_payment=str(self.payment.pk),
        )
        if not interactions.exists():
            self.payment.customer.interaction_set.create(
                origin=Interaction.Origin.MANUAL_NOTE,
                summary=summary,
                content=content,
                details=details,
            )
        self.payment.customer.followups.update_or_create(
            type=CustomerFollowUp.Type.MANUAL,
            details__settlement_review_payment=str(self.payment.pk),
            defaults={"note": summary, "details": details},
            create_defaults={
                "note": summary,
                "details": details,
                "follow_up_at": now(),
            },
        )


@register_backend
class ThePay2Bitcoin(ThePay2Card):
    name = "thepay2-bitcoin"
    verbose = gettext_lazy("Crypto payment")
    description = "Bitcoin (The Pay)"
    recurring = False
    thepay_method_code = "bitcoin"
    supported_currencies: set[str] = set()


@register_backend
class ThePay2GooglePay(ThePay2Card):
    name = "thepay2-gpay"
    verbose = gettext_lazy("Google Pay")
    description = "Google Pay (The Pay)"
    recurring = False
    thepay_method_code = "google_pay"


@register_backend
class ThePay2ApplePay(ThePay2Card):
    name = "thepay2-apay"
    verbose = gettext_lazy("Apple Pay")
    description = "Apple Pay (The Pay)"
    recurring = False
    thepay_method_code = "apple_pay"
