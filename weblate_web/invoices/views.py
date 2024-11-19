from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect

from .models import Invoice, InvoiceKind

if TYPE_CHECKING:
    from weblate_web.views import AuthenticatedHttpRequest


@login_required
@user_passes_test(lambda u: u.is_staff)
def download_invoice(request: AuthenticatedHttpRequest, pk: str):
    invoice = get_object_or_404(Invoice, pk=pk)

    return FileResponse(
        invoice.path.open("rb"),
        as_attachment=True,
        filename=invoice.filename,
        content_type="application/pdf",
    )


@transaction.atomic
def pay_invoice(request: AuthenticatedHttpRequest, pk: str):
    invoice = get_object_or_404(
        Invoice, pk=pk, kind=InvoiceKind.INVOICE, paid_payment_set=None
    )
    if not invoice.can_be_paid():
        raise Http404("Cannot be paid")
    if invoice.draft_payment_set.exists():
        payment = invoice.draft_payment_set.all()[0]
    else:
        payment = invoice.create_payment()
        payment.extra["exclude_backends"] = ["fio-bank"]
        payment.save(update_fields=["extra"])
    return redirect(payment)
