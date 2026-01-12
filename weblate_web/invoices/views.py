from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext

from .models import Invoice, InvoiceKind

if TYPE_CHECKING:
    from weblate_web.views import AuthenticatedHttpRequest


@login_required
@user_passes_test(lambda u: u.is_staff)
def download_invoice(request: AuthenticatedHttpRequest, pk: str):
    invoice = get_object_or_404(Invoice, pk=pk)
    if "receipt" in request.GET:
        return FileResponse(
            invoice.receipt_path.open("rb"),
            as_attachment=True,
            filename=invoice.receipt_filename,
            content_type="application/pdf",
        )

    return FileResponse(
        invoice.path.open("rb"),
        as_attachment=True,
        filename=invoice.filename,
        content_type="application/pdf",
    )


@transaction.atomic
def pay_invoice(request: AuthenticatedHttpRequest, pk: str):
    invoice = get_object_or_404(Invoice, pk=pk, kind=InvoiceKind.INVOICE)
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
    if invoice.draft_payment_set.exists():
        payment = invoice.draft_payment_set.all()[0]
    else:
        payment = invoice.create_payment()
        payment.extra["exclude_backends"] = ["fio-bank"]
        payment.save(update_fields=["extra"])
    return redirect(payment)
