from __future__ import annotations

from decimal import Decimal
from operator import attrgetter
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

from weblate_web.forms import NewSubscriptionForm
from weblate_web.invoices.forms import CustomerReferenceForm
from weblate_web.invoices.models import Invoice, InvoiceCategory, InvoiceKind
from weblate_web.models import Service, Subscription
from weblate_web.payments.models import Customer, Payment
from weblate_web.utils import show_form_errors

from .models import Interaction

if TYPE_CHECKING:
    from django.http import HttpRequest

    from weblate_web.payments.models import CustomerQuerySet
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
    template_name = "weblate_web/service_list.html"

    def get_title(self) -> str:
        match self.kwargs["kind"]:
            case "all":
                return "All services"
            case "expired":
                return "Expired services"
            case "extended":
                return "Extended support services"
        raise ValueError(self.kwargs["kind"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        context["kind"] = self.kwargs["kind"]
        return context

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
                expired = qs.filter(subscription__id__in=subscriptions).distinct()
                return sorted(expired, key=attrgetter("package_kind"))
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["reference_form"] = CustomerReferenceForm()
        return context

    def post(self, request, *args, **kwargs):
        service = self.get_object()
        subscription = service.subscription_set.get(pk=request.POST["subscription"])
        if "quote" in request.POST or "invoice" in request.POST:
            form = CustomerReferenceForm(request.POST)
            if not form.is_valid():
                show_form_errors(self.request, form)
            kind = InvoiceKind.QUOTE if "quote" in request.POST else InvoiceKind.INVOICE
            with override("en"):
                invoice = subscription.create_invoice(
                    kind=kind,
                    customer_reference=form.cleaned_data["customer_reference"],
                    customer_note=form.cleaned_data["customer_note"],
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
                initial={
                    "customer_reference": self.object.customer_reference,
                    "customer_note": self.object.customer_note,
                },
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
                    customer_note=form.cleaned_data["customer_note"],
                )
                invoice.generate_files()
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
        return self.object.verbose_name

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
                    customer_note=form.cleaned_data["customer_note"],
                    skip_intro=form.cleaned_data.get("skip_intro", False),
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


class IncomeView(CRMMixin, TemplateView):  # type: ignore[misc]
    template_name = "crm/income.html"
    permission = "invoices.view_income"
    title = "Income Tracking"

    def get_year(self) -> int:
        """Get the year from URL kwargs or default to current year."""
        return self.kwargs.get("year", timezone.now().year)

    def get_month(self) -> int | None:
        """Get the month from URL kwargs if present."""
        return self.kwargs.get("month")

    def get_title(self) -> str:
        year = self.get_year()
        month = self.get_month()
        if month:
            return f"Income Tracking - {year}/{month:02d}"
        return f"Income Tracking - {year}"

    def generate_svg_bar_chart(
        self, data: dict[str, Decimal], max_value: Decimal | None = None
    ) -> str:
        """Generate a simple SVG bar chart."""
        if not data:
            return ""

        # Chart dimensions
        width = 800
        height = 400
        padding = 60
        chart_width = width - 2 * padding
        chart_height = height - 2 * padding

        # Calculate max value if not provided
        if max_value is None:
            max_value = max(data.values()) if data.values() else Decimal(0)

        # Ensure max_value is at least 1 to avoid division by zero
        if max_value <= 0:
            max_value = Decimal(1)

        # Start SVG
        svg_parts = [
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
            '<style>',
            '.bar { fill: #417690; }',
            '.bar:hover { fill: #79aec8; }',
            '.label { font-family: Arial, sans-serif; font-size: 12px; }',
            '.value { font-family: Arial, sans-serif; font-size: 11px; fill: #666; }',
            '.axis { stroke: #ccc; stroke-width: 1; }',
            '</style>',
        ]

        # Draw axes
        svg_parts.append(
            f'<line x1="{padding}" y1="{padding}" x2="{padding}" '
            f'y2="{height - padding}" class="axis"/>'
        )
        svg_parts.append(
            f'<line x1="{padding}" y1="{height - padding}" '
            f'x2="{width - padding}" y2="{height - padding}" class="axis"/>'
        )

        # Calculate bar properties
        num_bars = len(data)
        bar_spacing = chart_width / (num_bars * 2 + 1)
        bar_width = bar_spacing * 0.8

        # Draw bars
        for i, (label, value) in enumerate(data.items()):
            x = padding + bar_spacing * (2 * i + 1)
            bar_height = (
                float(value / max_value * chart_height) if value > 0 else 0
            )
            y = height - padding - bar_height

            # Bar
            svg_parts.append(
                f'<rect x="{x}" y="{y}" width="{bar_width}" '
                f'height="{bar_height}" class="bar">'
                f'<title>{label}: {value:,.0f} CZK</title>'
                f'</rect>'
            )

            # Label
            label_x = x + bar_width / 2
            label_y = height - padding + 15
            svg_parts.append(
                f'<text x="{label_x}" y="{label_y}" '
                f'text-anchor="middle" class="label">{label}</text>'
            )

            # Value
            if bar_height > 20:
                value_y = y + bar_height / 2 + 4
                svg_parts.append(
                    f'<text x="{label_x}" y="{value_y}" '
                    f'text-anchor="middle" class="value">{value:,.0f}</text>'
                )

        svg_parts.append("</svg>")
        return "".join(svg_parts)

    def get_income_data(self, year: int, month: int | None = None):
        """Get income data aggregated by category."""
        invoices = list(
            Invoice.objects.filter(
                kind=InvoiceKind.INVOICE, issue_date__year=year
            ).prefetch_related("invoiceitem_set")
        )

        if month:
            invoices = [inv for inv in invoices if inv.issue_date.month == month]

        # Pre-calculate totals to leverage cached_property and avoid repeated calculations
        invoice_totals = {inv.pk: inv.total_amount_no_vat_czk for inv in invoices}

        # Aggregate by category manually since total_amount_no_vat_czk is a property
        # Group invoices by category to avoid N+1 queries
        category_data = {}
        for category in InvoiceCategory:
            total = sum(
                (
                    invoice_totals[inv.pk]
                    for inv in invoices
                    if inv.category == category.value
                ),
                start=Decimal(0),
            )
            category_data[category.label] = total

        return category_data

    def get_monthly_data(self, year: int):
        """Get monthly income data for the year."""
        # Fetch all invoices for the year at once to avoid 12 separate queries
        invoices = list(
            Invoice.objects.filter(
                kind=InvoiceKind.INVOICE, issue_date__year=year
            ).prefetch_related("invoiceitem_set")
        )

        # Pre-calculate totals to leverage cached_property and avoid repeated calculations
        invoice_totals = {inv.pk: inv.total_amount_no_vat_czk for inv in invoices}

        # Group by month in Python
        monthly_totals = {}
        for month in range(1, 13):
            total = sum(
                (
                    invoice_totals[inv.pk]
                    for inv in invoices
                    if inv.issue_date.month == month
                ),
                start=Decimal(0),
            )
            monthly_totals[f"{month:02d}"] = total

        return monthly_totals

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        year = self.get_year()
        month = self.get_month()

        # Get income data
        income_data = self.get_income_data(year, month)
        context["income_data"] = income_data
        context["total_income"] = sum(income_data.values())

        # Navigation years (show last 5 years and next year)
        current_year = timezone.now().year
        context["years"] = list(range(current_year - 5, current_year + 2))
        context["current_year"] = year
        context["current_month"] = month

        # Generate chart
        if month:
            # For monthly view, show category breakdown
            context["chart_svg"] = self.generate_svg_bar_chart(income_data)
            context["is_monthly"] = True
        else:
            # For yearly view, show monthly totals
            monthly_data = self.get_monthly_data(year)
            context["chart_svg"] = self.generate_svg_bar_chart(monthly_data)
            context["monthly_data"] = monthly_data
            context["is_monthly"] = False

        return context
