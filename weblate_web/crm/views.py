from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import (
    PermissionDenied,
)
from django.shortcuts import redirect
from django.utils.translation import gettext
from django.views.generic import DetailView, ListView, TemplateView

from weblate_web.forms import AddPaymentForm
from weblate_web.invoices.models import Invoice, InvoiceKind
from weblate_web.models import Service
from weblate_web.payments.models import Payment
from weblate_web.utils import show_form_errors

if TYPE_CHECKING:
    from django.http import HttpRequest


class CRMMixin:
    title: str = "CRM"
    permission: str | None = None

    def get_title(self) -> str:
        return self.title

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        context["title"] = self.get_title()
        return context

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(settings.LOGIN_URL)
        if not request.user.is_staff:
            raise PermissionDenied
        if self.permission is not None and not request.user.has_perm(self.permission):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)  # type:ignore[misc]


class IndexView(CRMMixin, TemplateView):
    template_name = "crm/index.html"


class ServiceListView(CRMMixin, ListView):
    model = Service
    permission = "weblate_web.view_service"
    title = "Services"


class ServiceDetailView(CRMMixin, DetailView):
    model = Service
    permission = "weblate_web.change_service"
    title = "Service detail"

    def get_payment_form(self, **kwargs):
        form = AddPaymentForm(**kwargs)
        form.fields["subscription"].queryset = self.object.subscription_set.all()  # type: ignore[attr-defined]
        return form

    def post(self, request, **kwargs):
        self.object = self.get_object()
        action = request.POST.get("action")
        if action == "payment":
            form = self.get_payment_form(data=request.POST)
            if form.is_valid():
                form.cleaned_data["subscription"].add_payment(
                    form.cleaned_data["invoice"], form.cleaned_data["period"]
                )
                messages.info(request, gettext("Payment was added."))
            else:
                show_form_errors(request, form)
        return redirect(self.object)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["add_payment_form"] = self.get_payment_form()
        return context


class InvoiceListView(CRMMixin, ListView):
    model = Invoice
    permission = "invoices.view_invoice"
    title = "Invoices"

    def get_title(self) -> str:
        match self.kwargs["kind"]:
            case "unpaid":
                return "Unpaid invoices"
            case "quote":
                return "Quotes"
            case "invoice":
                return "Invoices"
        raise ValueError(self.kwargs["kind"])

    def get_queryset(self):
        qs = super().get_queryset()
        match self.kwargs["kind"]:
            case "unpaid":
                return qs.filter(
                    kind=InvoiceKind.INVOICE,
                    paid_payment_set__state__in={Payment.NEW, Payment.PENDING},
                )
            case "quote":
                return qs.filter(kind=InvoiceKind.QUOTE)
            case "invoice":
                return qs.filter(kind=InvoiceKind.INVOICE)
        raise ValueError(self.kwargs["kind"])
