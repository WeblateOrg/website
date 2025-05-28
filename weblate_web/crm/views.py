from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic import DetailView, ListView, TemplateView

from weblate_web.invoices.models import Invoice, InvoiceKind
from weblate_web.models import Service, Subscription
from weblate_web.payments.models import Customer, Payment

from .models import Interaction

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

    @method_decorator(login_required)
    def dispatch(self, request: HttpRequest, *args, **kwargs):
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

    def get_title(self) -> str:
        match self.kwargs["kind"]:
            case "all":
                return "All services"
            case "expired":
                return "Expired services"
            case "extended":
                return "Extended support services"
        raise ValueError(self.kwargs["kind"])

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related("subscription_set")
        match self.kwargs["kind"]:
            case "all":
                return qs
            case "expired":
                possible_subscriptions = Subscription.objects.filter(
                    expires__lte=timezone.now(), enabled=True
                ).exclude(payment=None)
                subscriptions = []
                for subscription in possible_subscriptions:
                    # Skip one-time payments and the ones with recurrence configured
                    if not subscription.package.get_repeat():
                        continue
                    if not subscription.could_be_obsolete():
                        subscriptions.append(subscription.pk)
                return qs.filter(subscription__id__in=subscriptions).distinct()
            case "extended":
                return qs.filter(
                    subscription__expires__gte=timezone.now(),
                    subscription__package__name="extended",
                    subscription__enabled=True,
                ).distinct()
        raise ValueError(self.kwargs["kind"])


class ServiceDetailView(CRMMixin, DetailView):
    model = Service
    permission = "weblate_web.change_service"
    title = "Service detail"

    def post(self, request, *args, **kwargs):
        service = self.get_object()
        subscription = service.subscription_set.get(pk=request.POST["subscription"])
        reference = request.POST.get("customer_reference", "")
        if "quote" in request.POST:
            kind = InvoiceKind.QUOTE
        elif "invoice" in request.POST:
            kind = InvoiceKind.INVOICE
        else:
            raise ValueError("Missing renewal type!")
        invoice = subscription.create_invoice(kind=kind, customer_reference=reference)
        return redirect(invoice)


class InvoiceListView(CRMMixin, ListView):
    model = Invoice
    permission = "invoices.view_invoice"
    title = "Invoices"
    paginate_by = 100

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
        qs = super().get_queryset().order_by("-number")
        match self.kwargs["kind"]:
            case "unpaid":
                return qs.filter(
                    Q(paid_payment_set__state__in={Payment.NEW, Payment.PENDING})
                    | Q(paid_payment_set=None),
                    kind=InvoiceKind.INVOICE,
                )
            case "quote":
                return qs.filter(kind=InvoiceKind.QUOTE)
            case "invoice":
                return qs.filter(kind=InvoiceKind.INVOICE)
        raise ValueError(self.kwargs["kind"])


class InvoiceDetailView(CRMMixin, DetailView):
    model = Invoice
    permission = "invoices.view_invoice"
    title = "Invoice detail"


class CustomerListView(CRMMixin, ListView):
    model = Customer
    permission = "payments.view_customer"
    title = "Customers"
    paginate_by = 100

    def get_title(self) -> str:
        match self.kwargs["kind"]:
            case "active":
                return "Active customer"
            case "all":
                return "All customers"
        raise ValueError(self.kwargs["kind"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        context["query"] = self.request.GET.get("q", "")
        return context

    def get_queryset(self):
        qs = super().get_queryset().order_by("name", "email")
        if query := self.request.GET.get("q"):
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(email__icontains=query)
                | Q(users__email__icontains=query)
                | Q(end_client__icontains=query)
            ).distinct()
        match self.kwargs["kind"]:
            case "active":
                return qs.filter(
                    service__subscription__expires__gte=timezone.now()
                    - timedelta(days=3 * 365)
                ).distinct()
            case "all":
                return qs
        raise ValueError(self.kwargs["kind"])


class CustomerDetailView(CRMMixin, DetailView):
    model = Customer
    permission = "payments.view_customer"
    title = "Customer detail"

    def get_title(self) -> str:
        return self.object.name


class CustomerMergeView(CustomerDetailView):
    template_name = "payments/customer_merge.html"

    def get_merged(self) -> Customer:
        return Customer.objects.get(
            pk=self.request.POST["merge"]
            if self.request.method == "POST"
            else self.request.GET["merge"]
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        context["merge"] = self.get_merged()
        return context

    def post(self, request, *args, **kwargs):
        customer = self.get_object()
        merge = self.get_merged()
        customer.merge(merge, user=self.request.user)
        return redirect(customer)


class InteractionDetailView(CRMMixin, DetailView):
    model = Interaction
    permission = "payments.view_customer"

    def render_to_response(self, context, **response_kwargs):
        return HttpResponse(self.object.content, content_type="text/html")


class InteractionDownloadView(InteractionDetailView):
    def render_to_response(self, context, **response_kwargs):
        return FileResponse(
            self.object.attachment.open(),
            as_attachment=True,
            filename=self.object.attachment.name,
        )
