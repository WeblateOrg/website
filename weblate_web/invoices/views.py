from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext

from weblate_web.payments.models import Payment

from .models import Invoice, InvoiceKind

if TYPE_CHECKING:
    from weblate_web.views import AuthenticatedHttpRequest


@login_required
@user_passes_test(lambda u: u.is_staff)
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


@transaction.atomic
def pay_invoice(request: AuthenticatedHttpRequest, pk: str):
    invoice = get_object_or_404(
        Invoice.objects.select_for_update(), pk=pk, kind=InvoiceKind.INVOICE
    )
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
    recurring = None
    subscription_id = invoice.extra.get("subscription")
    if (
        isinstance(subscription_id, int)
        and not isinstance(subscription_id, bool)
        and "subscription_upgrade" not in invoice.extra
        and (package := invoice.get_package()) is not None
    ):
        recurring = package.get_repeat()
    payments = invoice.draft_payment_set.filter(
        state__in=[Payment.NEW, Payment.PENDING, Payment.REJECTED]
    )
    if existing := payments.first():
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
