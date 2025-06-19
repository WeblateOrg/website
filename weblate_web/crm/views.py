from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import override
from django.views.generic import DetailView, ListView, TemplateView

from weblate_web.forms import CustomerReferenceForm, NewSubscriptionForm
from weblate_web.invoices.models import Invoice, InvoiceKind
from weblate_web.models import Service, Subscription
from weblate_web.payments.models import Customer, CustomerQuerySet, Payment

from .models import Interaction

if TYPE_CHECKING:
    from django.http import HttpRequest

    from weblate_web.views import AuthenticatedHttpRequest


class CRMMixin:
    title: str = "CRM"
    permission: str | None = None
    request: AuthenticatedHttpRequest

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


class IndexView(CRMMixin, TemplateView):  # type: ignore[misc]
    template_name = "crm/index.html"


class ServiceListView(CRMMixin, ListView[Service]):  # type: ignore[misc]
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


class ServiceDetailView(CRMMixin, DetailView[Service]):  # type: ignore[misc]
    model = Service
    permission = "weblate_web.change_service"
    title = "Service detail"

    def post(self, request, *args, **kwargs):
        service = self.get_object()
        subscription = service.subscription_set.get(pk=request.POST["subscription"])
        reference = request.POST.get("customer_reference", "")
        if "quote" in request.POST or "invoice" in request.POST:
            kind = InvoiceKind.QUOTE if "quote" in request.POST else InvoiceKind.INVOICE
            with override("en"):
                invoice = subscription.create_invoice(
                    kind=kind, customer_reference=reference
                )
            return redirect(invoice)
        if "disable" in request.POST:
            subscription.enabled = False
            subscription.save(update_fields=["enabled"])
            return redirect(service)

        raise ValueError("Missing action!")


class InvoiceListView(CRMMixin, ListView[Invoice]):  # type: ignore[misc]
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
            case "all":
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
            case "all":
                return qs
        raise ValueError(self.kwargs["kind"])


class InvoiceDetailView(CRMMixin, DetailView[Invoice]):  # type: ignore[misc]
    model = Invoice
    permission = "invoices.view_invoice"
    title = "Invoice detail"

    def get_title(self) -> str:
        return f"{self.object.get_kind_display()} {self.object.number}"

    def can_convert(self) -> bool:
        return (
            self.object.kind == InvoiceKind.QUOTE
            and not self.object.invoice_set.exists()
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        if self.can_convert():
            context["convert_form"] = CustomerReferenceForm(
                self.request.POST if self.request.method == "POST" else None,
                initial={"customer_reference": self.object.customer_reference},
            )
        return context

    def post(self, request, *args, **kwargs):
        self.object = quote = self.get_object()
        form = CustomerReferenceForm(request.POST)
        if form.is_valid() and self.can_convert():
            with override("en"):
                invoice = quote.duplicate(
                    kind=InvoiceKind.INVOICE,
                    customer_reference=form.cleaned_data["customer_reference"],
                )
            return redirect(invoice)

        return self.get(request, *args, **kwargs)


class CustomerListView(CRMMixin, ListView[Customer]):  # type: ignore[misc]
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

    def get_queryset(self) -> CustomerQuerySet:
        qs = cast("CustomerQuerySet", super().get_queryset().order_by("name", "email"))
        if query := self.request.GET.get("q"):
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(email__icontains=query)
                | Q(users__email__icontains=query)
                | Q(end_client__icontains=query)
            ).distinct()
        match self.kwargs["kind"]:
            case "active":
                return qs.active()
            case "all":
                return qs
        raise ValueError(self.kwargs["kind"])


class CustomerDetailView(CRMMixin, DetailView[Customer]):  # type: ignore[misc]
    model = Customer
    permission = "payments.view_customer"
    title = "Customer detail"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        context["new_subscription_form"] = NewSubscriptionForm(
            self.request.POST if self.request.method == "POST" else None
        )
        return context

    def get_title(self) -> str:
        return self.object.name

    def post(self, request, *args, **kwargs):
        customer = self.get_object()
        form = NewSubscriptionForm(request.POST)
        if form.is_valid():
            with override("en"):
                invoice = Subscription.new_subscription_invoice(
                    kind=form.cleaned_data["kind"],
                    customer=customer,
                    package=form.cleaned_data["package"],
                    currency=form.cleaned_data["currency"],
                    customer_reference=form.cleaned_data["customer_reference"],
                )
            return redirect(invoice)

        return self.get(request, *args, **kwargs)


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
        merge.merge(customer, user=self.request.user)
        return redirect(merge)


class InteractionDetailView(CRMMixin, DetailView[Interaction]):  # type: ignore[misc]
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
