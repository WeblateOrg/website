#
# Copyright © 2012–2022 Michal Čihař <michal@cihar.com>
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

import json
import re
import subprocess
from math import floor

import fiobank
import sentry_sdk
import thepay.config
import thepay.dataApi
import thepay.gateApi
import thepay.payment
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.shortcuts import redirect
from django.utils.translation import gettext, gettext_lazy, override
from fakturace.storage import InvoiceStorage, ProformaStorage

from .models import Payment
from .utils import send_notification

BACKENDS = {}
PROFORMA_RE = re.compile("20[0-9]{7}")


def get_backend(name):
    backend = BACKENDS[name]
    if backend.debug and not settings.PAYMENT_DEBUG:
        raise KeyError("Invalid backend")
    return backend


def list_backends():
    result = []
    for backend in BACKENDS.values():
        if not backend.debug or settings.PAYMENT_DEBUG:
            result.append(backend)
    return sorted(result, key=lambda x: x.name)


class InvalidState(ValueError):
    pass


def register_backend(backend):
    BACKENDS[backend.name] = backend
    return backend


class Backend:
    name = None
    debug = False
    verbose = None
    description = ""
    recurring = False

    def __init__(self, payment):
        select = Payment.objects.filter(pk=payment.pk).select_for_update()
        self.payment = select[0]
        self.invoice = None

    @property
    def image_name(self):
        return f"payment/{self.name}.png"

    def perform(self, request, back_url, complete_url):
        """Perform payment and optionally redirects user."""
        raise NotImplementedError()

    def collect(self, request):
        """Collect payment information."""
        raise NotImplementedError()

    def get_instructions(self):
        """Payment instructions for manual methods."""
        return []

    def initiate(self, request, back_url, complete_url):
        """Initiate payment and optionally redirects user."""
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

    def complete(self, request):
        """Payment completion called from returned request."""
        if self.payment.state not in (Payment.PENDING, Payment.REJECTED):
            raise InvalidState(self.payment.get_state_display())

        status = self.collect(request)
        if status is None:
            return False
        if status:
            self.success()
            return True
        self.failure()
        return False

    def generate_invoice(self, storage_class=InvoiceStorage, paid=True):
        """Generate an invoice."""
        if settings.PAYMENT_FAKTURACE is None:
            return
        storage = storage_class(settings.PAYMENT_FAKTURACE)
        customer = self.payment.customer
        customer_id = f"web-{customer.pk}"
        with override("en"):
            contact_file = storage.update_contact(
                customer_id,
                customer.name,
                customer.address,
                customer.city,
                customer.country.name,
                customer.email,
                customer.tax if customer.tax else "",
                customer.vat if customer.vat else "",
                "EUR",
                "weblate",
            )
        invoice_file = storage.create(
            customer_id,
            0,
            rate=f"{self.payment.amount_without_vat:f}",
            item=self.payment.description,
            vat=str(customer.vat_rate),
            category=self.payment.extra.get("category", "weblate"),
            **self.get_invoice_kwargs(),
        )
        invoice = storage.get(invoice_file)
        invoice.write_tex()
        invoice.build_pdf()
        files = [contact_file, invoice_file, invoice.tex_path, invoice.pdf_path]
        if paid:
            invoice.mark_paid(
                json.dumps(self.payment.details, indent=2, cls=DjangoJSONEncoder)
            )
            files.append(invoice.paid_path)

        self.payment.invoice = invoice.invoiceid
        self.invoice = invoice

        # Commit to git
        self.git_commit(files, invoice)

    def git_commit(self, files, invoice):
        subprocess.run(
            ["git", "add", "--"] + files, check=True, cwd=settings.PAYMENT_FAKTURACE
        )
        subprocess.run(
            ["git", "commit", "-m", f"Invoice {invoice.invoiceid}"],
            check=True,
            cwd=settings.PAYMENT_FAKTURACE,
        )

    def send_notification(self, notification, include_invoice=True):
        kwargs = {"backend": self}
        if self.invoice:
            kwargs["invoice"] = self.invoice
        if self.payment:
            kwargs["payment"] = self.payment
        send_notification(notification, [self.payment.customer.email], **kwargs)

    def get_invoice_kwargs(self):
        return {"payment_id": str(self.payment.pk), "payment_method": self.description}

    def success(self):
        self.payment.state = Payment.ACCEPTED
        if not self.recurring:
            self.payment.recurring = ""

        self.generate_invoice()
        self.payment.save()

        self.send_notification("payment_completed")

    def failure(self):
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

    def perform(self, request, back_url, complete_url):
        return None

    def collect(self, request):
        return True


@register_backend
class DebugReject(DebugPay):
    name = "reject"
    verbose = "Reject"
    description = "Reject (TEST)"
    recurring = False

    def collect(self, request):
        self.payment.details["reject_reason"] = "Debug reject"
        return False


@register_backend
class DebugPending(DebugPay):
    name = "pending"
    verbose = "Pending"
    description = "Pending (TEST)"
    recurring = False

    def perform(self, request, back_url, complete_url):
        return redirect("https://cihar.com/?url=" + complete_url)

    def collect(self, request):
        return True


@register_backend
class ThePayCard(Backend):
    name = "thepay-card"
    verbose = gettext_lazy("Payment card")
    description = "Payment Card (The Pay)"
    recurring = True
    thepay_method = 31

    def __init__(self, payment):
        super().__init__(payment)
        self.config = thepay.config.Config()
        if settings.PAYMENT_THEPAY_MERCHANTID:
            self.config.setCredentials(
                settings.PAYMENT_THEPAY_MERCHANTID,
                settings.PAYMENT_THEPAY_ACCOUNTID,
                settings.PAYMENT_THEPAY_PASSWORD,
                settings.PAYMENT_THEPAY_DATAAPI,
            )

    def perform(self, request, back_url, complete_url):
        if self.payment.repeat:
            api = thepay.gateApi.GateApi(self.config)
            try:
                api.cardCreateRecurrentPayment(
                    str(self.payment.repeat.pk),
                    str(self.payment.pk),
                    self.payment.vat_amount,
                )
            except thepay.gateApi.GateError as error:
                sentry_sdk.capture_exception()
                self.payment.details = {"errorDescription": error.args[0]}
                # Failure is handled in collect using API
            return None

        payment = thepay.payment.Payment(self.config)

        payment.setCurrency("EUR")
        payment.setValue(self.payment.vat_amount)
        payment.setMethodId(self.thepay_method)
        payment.setCustomerEmail(self.payment.customer.email)
        payment.setDescription(self.payment.description)
        payment.setReturnUrl(complete_url)
        payment.setMerchantData(str(self.payment.pk))
        if self.payment.recurring:
            payment.setIsRecurring(1)
        return redirect(payment.getCreateUrl())

    def collect(self, request):
        if self.payment.repeat:
            data = thepay.dataApi.DataApi(self.config)
            response = data.getPayments(merchant_data=str(self.payment.pk))
            if response.payments == "":
                # Something went wrong
                status = 4
            else:
                payment = response.payments.payment[0]
                self.payment.details = dict(payment)
                status = int(payment.state)
        else:
            return_payment = thepay.payment.ReturnPayment(self.config)
            return_payment.parseData(request.GET)

            # Check params signature
            try:
                return_payment.checkSignature()
            except thepay.payment.ReturnPayment.InvalidSignature:
                sentry_sdk.capture_exception()
                return False

            # Check we got correct payment
            if return_payment.getMerchantData() != str(self.payment.pk):
                return False

            # Store payment details
            self.payment.details = dict(return_payment.data)

            status = return_payment.getStatus()

        if status == 2:
            return True
        if status == 7:
            return None
        reason = f"Unknown: {status}"
        if status == 3:
            reason = gettext("Payment cancelled")
        elif status == 4:
            reason = gettext("Payment error")
        elif status == 6:
            reason = "Underpaid"
        elif status == 9:
            reason = "Deposit confirmed"
        self.payment.details["reject_reason"] = reason
        return False


# @register_backend
class ThePayBitcoin(ThePayCard):
    name = "thepay-bitcoin"
    verbose = gettext_lazy("Bitcoin")
    description = "Bitcoin (The Pay)"
    recurring = False
    thepay_method = 29


@register_backend
class FioBank(Backend):
    name = "fio-bank"
    verbose = gettext_lazy("IBAN bank transfer")
    description = "Bank transfer"
    recurring = False

    def collect(self, request):
        # We do not actually collect here, it is done in background
        if self.payment.state == Payment.PENDING:
            return None
        return True

    def perform(self, request, back_url, complete_url):
        self.generate_invoice(storage_class=ProformaStorage, paid=False)
        self.payment.details["proforma"] = self.payment.invoice
        self.send_notification("payment_pending")
        return redirect(complete_url)

    def get_proforma(self):
        storage = ProformaStorage(settings.PAYMENT_FAKTURACE)
        return storage.get(self.payment.details["proforma"])

    def get_invoice_kwargs(self):
        if self.payment.state == Payment.ACCEPTED:
            # Inject proforma ID to generated invoice
            invoice = self.get_proforma()
            return {"payment_id": invoice.invoiceid, "bank_suffix": "proforma"}
        return {}

    def get_instructions(self):
        invoice = self.get_proforma()
        return [
            (gettext("Issuing bank"), invoice.bank["bank"]),
            (gettext("Account holder"), invoice.bank["holder"]),
            (gettext("Account number"), invoice.bank["account"]),
            (gettext("SWIFT code"), invoice.bank["swift"]),
            (gettext("IBAN"), invoice.bank["iban"]),
            (gettext("Reference"), invoice.invoiceid),
        ]

    @classmethod
    def fetch_payments(cls, from_date=None):
        client = fiobank.FioBank(token=settings.FIO_TOKEN)
        for transaction in client.last(from_date=from_date):
            matches = []
            # Extract from message
            if transaction["recipient_message"]:
                matches.extend(PROFORMA_RE.findall(transaction["recipient_message"]))
            # Extract from variable symbol
            if transaction["variable_symbol"]:
                matches.extend(PROFORMA_RE.findall(transaction["variable_symbol"]))
            # Extract from sender reference
            if transaction.get("reference", None):
                matches.extend(PROFORMA_RE.findall(transaction["reference"]))
            # Extract from comment for manual pairing
            if transaction["comment"]:
                matches.extend(PROFORMA_RE.findall(transaction["comment"]))
            # Process all matches
            for proforma_id in matches:
                proforma_id = f"P{proforma_id}"
                try:
                    related = Payment.objects.get(
                        backend=cls.name, invoice=proforma_id, state=Payment.PENDING
                    )
                    backend = cls(related)
                    proforma = backend.get_proforma()
                    proforma.mark_paid(
                        json.dumps(transaction, indent=2, cls=DjangoJSONEncoder)
                    )
                    backend.git_commit([proforma.paid_path], proforma)
                    if floor(float(proforma.total_amount)) <= transaction["amount"]:
                        print(f"Received payment for {proforma_id}")
                        backend.payment.details["transaction"] = transaction
                        backend.success()
                    else:
                        print(
                            "Underpaid {}: received={}, expected={}".format(
                                proforma_id,
                                transaction["amount"],
                                proforma.total_amount,
                            )
                        )
                except Payment.DoesNotExist:
                    print(f"No matching payment for {proforma_id} found")
