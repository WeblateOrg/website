from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import FileResponse
from django.shortcuts import get_object_or_404

from weblate_web.views import AuthenticatedHttpRequest

from .models import Invoice


@login_required
@user_passes_test(lambda u: u.is_superuser)
def download_invoice(request: AuthenticatedHttpRequest, pk: int):
    invoice = get_object_or_404(Invoice, pk=pk)

    return FileResponse(
        invoice.path.open("rb"),
        as_attachment=True,
        filename=invoice.filename,
        content_type="application/pdf",
    )
