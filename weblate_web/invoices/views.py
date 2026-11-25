from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import (
    login_required,
    permission_required,
    user_passes_test,
)
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext

from weblate_web.models import Service
from weblate_web.payments.models import Payment

from .models import HostedRenewalValidationError, Invoice, InvoiceKind

if TYPE_CHECKING:
    from weblate_web.views import AuthenticatedHttpRequest


@login_required
@user_passes_test(lambda u: u.is_staff)
@permission_required("invoices.view_invoice", raise_exception=True)
def download_invoice(request: AuthenticatedHttpRequest, pk: str):
    invoice = get_object_or_404(Invoice, pk=pk)
    if "receipt" in request.GET:
        if not invoice.is_paid:
            raise Http404("Receipt not available")
        try:
            return FileResponse(
                invoice.receipt_path.open("rb"),
                as_attachment=True,
                filename=invoice.receipt_filename,
                content_type="application/pdf",
            )
        except (OSError, ValueError) as error:
            raise Http404("Receipt not available") from error

    return FileResponse(
        invoice.path.open("rb"),
        as_attachment=True,
        filename=invoice.filename,
        content_type="application/pdf",
    )


def get_linked_hosted_billing(invoice: Invoice) -> int | None:
    service_id = invoice.extra.get("service")
    linked_service = Q(pk__in=[])
    if isinstance(service_id, int) and not isinstance(service_id, bool):
        linked_service |= Q(pk=service_id)
    subscription_ids = [
        value
        for key in ("subscription", "subscription_upgrade")
        if isinstance(value := invoice.extra.get(key), int)
        and not isinstance(value, bool)
    ]
    linked_service |= Q(subscription__pk__in=subscription_ids)
    return (
        Service.objects.filter(
            linked_service, customer_id=invoice.customer_id, hosted_billing__gt=0
        )
        .values_list("hosted_billing", flat=True)
        .first()
    )


@transaction.atomic
def pay_invoice(request: AuthenticatedHttpRequest, pk: str):
    invoice = get_object_or_404(
        Invoice.objects.select_for_update(), pk=pk, kind=InvoiceKind.INVOICE
    )
    subscription_id = invoice.extra.get("subscription")
    hosted_renewal = invoice.extra.get("hosted_renewal") is True
    recurring = None
    if hosted_renewal:
        try:
            recurring = invoice.get_hosted_renewal_period()
        except HostedRenewalValidationError as error:
            raise Http404("Invalid Hosted renewal invoice") from error
    elif billing := get_linked_hosted_billing(invoice):
        return redirect(f"https://hosted.weblate.org/billing/{billing}/")
    if not invoice.can_be_paid():
        if invoice.paid_payment_set.exists():
            messages.info(
                request,
                gettext(
                    "This invoice has already been paid. Please sign in to view details."
                ),
            )
            return redirect("home")

        raise Http404("Cannot be paid")
    if (
        recurring is None
        and isinstance(subscription_id, int)
        and not isinstance(subscription_id, bool)
        and "subscription_upgrade" not in invoice.extra
        and (package := invoice.get_package()) is not None
    ):
        recurring = package.get_repeat()
    payments = invoice.draft_payment_set.filter(
        state__in=[Payment.NEW, Payment.PENDING, Payment.REJECTED]
    )
    if existing := payments.first():
        try:
            invoice.validate_hosted_renewal_payment(existing)
        except HostedRenewalValidationError as error:
            raise Http404("Payment does not match Hosted renewal invoice") from error
        payment = existing
        if (
            recurring is not None
            and payment.state == Payment.NEW
            and payment.paid_invoice_id is None
            and payment.recurring != recurring
        ):
            payment.recurring = recurring
            payment.save(update_fields=["recurring"])
    else:
        payment = invoice.create_payment(recurring=recurring or "")
        payment.extra["exclude_backends"] = ["fio-bank"]
        payment.save(update_fields=["extra"])
    return redirect(payment)
