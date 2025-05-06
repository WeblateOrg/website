from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic import DetailView, ListView, TemplateView

from weblate_web.invoices.models import Invoice, InvoiceKind
from weblate_web.models import Service, Subscription
from weblate_web.payments.models import Customer, Payment

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

    def get_queryset(self):
        qs = super().get_queryset().order_by("name")
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
