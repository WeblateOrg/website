from __future__ import annotations

import calendar
import math
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
from weblate_web.models import PackageCategory, Service, Subscription
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
            case "dedicated":
                return "Dedicated hosting services"
        raise ValueError(self.kwargs["kind"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        context["kind"] = self.kwargs["kind"]
        return context

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related("subscription_set")
        match self.kwargs["kind"]:
            case "all":
                return sorted(qs, key=attrgetter("package_kind"))
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
            case "dedicated":
                return qs.filter(
                    subscription__package__category=PackageCategory.PACKAGE_DEDICATED
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

    # Chart configuration
    CHART_WIDTH = 800
    CHART_HEIGHT = 400
    CHART_PADDING = 60
    MIN_CHART_VALUE = Decimal(1)

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

    def generate_svg_bar_chart(  # noqa: PLR0914
        self, data: dict[str, Decimal], max_value: Decimal | None = None
    ) -> str:
        """Generate a simple SVG bar chart without inline styles."""
        if not data:
            return ""

        # Chart dimensions
        width = self.CHART_WIDTH
        height = self.CHART_HEIGHT
        padding = self.CHART_PADDING
        chart_width = width - 2 * padding
        chart_height = height - 2 * padding

        # Calculate max value if not provided
        if max_value is None:
            max_value = max(data.values()) if data.values() else Decimal(0)

        # Ensure max_value is at least MIN_CHART_VALUE to avoid division by zero
        if max_value <= 0:
            max_value = self.MIN_CHART_VALUE

        # Start SVG without inline styles
        svg_parts = [
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" class="income-chart">',
        ]

        # Draw axes
        svg_parts.append(
            f'<line x1="{padding}" y1="{padding}" x2="{padding}" '
            f'y2="{height - padding}" stroke="#ccc" stroke-width="1"/>'
        )
        svg_parts.append(
            f'<line x1="{padding}" y1="{height - padding}" '
            f'x2="{width - padding}" y2="{height - padding}" stroke="#ccc" stroke-width="1"/>'
        )

        # Calculate bar properties
        num_bars = len(data)
        bar_spacing = chart_width / (num_bars * 2 + 1)
        bar_width = bar_spacing * 0.8

        # Draw bars
        for i, (label, value) in enumerate(data.items()):
            x = padding + bar_spacing * (2 * i + 1)
            bar_height = float(value / max_value * chart_height) if value > 0 else 0
            y = height - padding - bar_height

            # Bar
            svg_parts.append(
                f'<rect x="{x}" y="{y}" width="{bar_width}" '
                f'height="{bar_height}" fill="#417690">'
                f"<title>{label}: €{value:,.0f}</title>"
                f"</rect>"
            )

            # Label
            label_x = x + bar_width / 2
            label_y = height - padding + 15
            svg_parts.append(
                f'<text x="{label_x}" y="{label_y}" '
                f'text-anchor="middle" font-size="12" fill="#666">{label}</text>'
            )

            # Value
            if bar_height > 20:
                value_y = y + bar_height / 2 + 4
                svg_parts.append(
                    f'<text x="{label_x}" y="{value_y}" '
                    f'text-anchor="middle" font-size="11" fill="#fff">{value:,.0f}</text>'
                )

        svg_parts.append("</svg>")
        return "".join(svg_parts)

    def generate_svg_pie_chart(self, data: dict[str, Decimal]) -> str:  # noqa: PLR0914
        """Generate a simple SVG pie chart for category distribution with legend."""
        if not data or sum(data.values()) == 0:
            return ""

        width = 500
        height = 400
        radius = 120
        center_x = 200
        center_y = 200

        # Category colors
        colors = {
            "Hosting": "#417690",
            "Support": "#79aec8",
            "Development / Consultations": "#5b80b2",
            "Donation": "#9fc5e8",
        }

        total = sum(data.values())

        # Count non-zero categories
        non_zero_categories = [cat for cat, val in data.items() if val > 0]

        svg_parts = [
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" class="pie-chart">',
        ]

        # Special case: if only one category, draw a circle instead of a pie slice
        if len(non_zero_categories) == 1:
            category = non_zero_categories[0]
            value = data[category]
            color = colors.get(category, "#999")
            svg_parts.append(
                f'<circle cx="{center_x}" cy="{center_y}" r="{radius}" '
                f'fill="{color}" stroke="white" stroke-width="2">'
                f"<title>{category}: €{value:,.0f} (100.0%)</title>"
                f"</circle>"
            )
        else:
            # Draw pie slices for multiple categories
            start_angle = 0
            for category, value in data.items():
                if value == 0:
                    continue

                angle = float(value / total * 360)
                end_angle = start_angle + angle

                # Convert to radians
                start_rad = math.radians(start_angle - 90)
                end_rad = math.radians(end_angle - 90)

                # Calculate path
                start_x = center_x + radius * math.cos(start_rad)
                start_y = center_y + radius * math.sin(start_rad)
                end_x = center_x + radius * math.cos(end_rad)
                end_y = center_y + radius * math.sin(end_rad)

                large_arc = 1 if angle > 180 else 0

                color = colors.get(category, "#999")
                svg_parts.append(
                    f'<path d="M{center_x},{center_y} L{start_x},{start_y} '
                    f'A{radius},{radius} 0 {large_arc},1 {end_x},{end_y} Z" '
                    f'fill="{color}" stroke="white" stroke-width="2">'
                    f"<title>{category}: €{value:,.0f} ({value / total * 100:.1f}%)</title>"
                    f"</path>"
                )

                start_angle = end_angle

        # Add legend
        legend_x = 420
        legend_y = 50
        legend_spacing = 25

        idx = 0
        for category, value in data.items():
            if value == 0:
                continue

            color = colors.get(category, "#999")
            y_pos = legend_y + idx * legend_spacing

            # Legend color box
            svg_parts.append(
                f'<rect x="{legend_x}" y="{y_pos}" width="15" height="15" '
                f'fill="{color}" stroke="white" stroke-width="1"/>'
            )

            # Legend text
            svg_parts.append(
                f'<text x="{legend_x + 20}" y="{y_pos + 12}" font-size="11" fill="#333">'
                f"{category}: €{value:,.0f} ({value / total * 100:.0f}%)</text>"
            )

            idx += 1

        svg_parts.append("</svg>")
        return "".join(svg_parts)

    def generate_svg_stacked_bar_chart(  # noqa: PLR0914
        self, monthly_data: dict, invoices: list
    ) -> str:
        """Generate a stacked bar chart showing monthly totals by category."""
        if not monthly_data:
            return ""

        width = self.CHART_WIDTH
        height = self.CHART_HEIGHT
        padding = self.CHART_PADDING
        chart_width = width - 2 * padding
        chart_height = height - 2 * padding

        # Category colors
        colors = {
            InvoiceCategory.HOSTING.value: "#417690",
            InvoiceCategory.SUPPORT.value: "#79aec8",
            InvoiceCategory.DEVEL.value: "#5b80b2",
            InvoiceCategory.DONATE.value: "#9fc5e8",
        }

        # Pre-calculate invoice totals in EUR
        invoice_totals = {inv.pk: inv.total_amount_no_vat for inv in invoices}

        # Get max value for scaling
        max_value = max(monthly_data.values()) if monthly_data.values() else Decimal(1)
        if max_value <= 0:
            max_value = self.MIN_CHART_VALUE

        svg_parts = [
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" class="stacked-bar-chart">',
        ]

        # Calculate bar properties
        num_bars = 12
        bar_spacing = chart_width / (num_bars * 1.5)
        bar_width = bar_spacing * 0.8

        # Draw each month
        for month_idx in range(1, 13):
            month_key = f"{month_idx:02d}"
            x = padding + bar_spacing * (month_idx - 0.5)

            # Get invoices for this month by category
            month_invoices = [
                inv for inv in invoices if inv.issue_date.month == month_idx
            ]

            # Stack bars by category
            y_offset = height - padding
            for category in InvoiceCategory:
                category_total = sum(
                    (
                        invoice_totals[inv.pk]
                        for inv in month_invoices
                        if inv.category == category.value
                    ),
                    start=Decimal(0),
                )

                if category_total > 0:
                    bar_height = float(category_total / max_value * chart_height)
                    y = y_offset - bar_height

                    svg_parts.append(
                        f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_height}" '
                        f'fill="{colors.get(category.value, "#999")}" stroke="white" stroke-width="1">'
                        f"<title>{category.label} - {month_key}: €{category_total:,.0f}</title>"
                        f"</rect>"
                    )
                    y_offset = y

            # Month label
            label_x = x + bar_width / 2
            label_y = height - padding + 15
            svg_parts.append(
                f'<text x="{label_x}" y="{label_y}" text-anchor="middle" '
                f'font-size="10" fill="#666">{month_key}</text>'
            )

        svg_parts.append("</svg>")
        return "".join(svg_parts)

    def generate_svg_daily_chart(  # noqa: PLR0914
        self, year: int, month: int, invoices: list
    ) -> str:
        """Generate a daily bar chart for a specific month."""
        width = self.CHART_WIDTH
        height = 300
        padding = 40
        chart_width = width - 2 * padding
        chart_height = height - 2 * padding

        # Get number of days in month
        num_days = calendar.monthrange(year, month)[1]

        # Pre-calculate invoice totals in EUR
        invoice_totals = {inv.pk: inv.total_amount_no_vat for inv in invoices}

        # Calculate daily totals
        daily_totals = {}
        for day in range(1, num_days + 1):
            daily_invoices = [inv for inv in invoices if inv.issue_date.day == day]
            daily_totals[day] = sum(
                (invoice_totals[inv.pk] for inv in daily_invoices),
                start=Decimal(0),
            )

        max_value = max(daily_totals.values()) if daily_totals.values() else Decimal(1)
        if max_value <= 0:
            max_value = self.MIN_CHART_VALUE

        svg_parts = [
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" class="daily-chart">',
        ]

        # Draw bars
        bar_width = chart_width / num_days * 0.8
        for day, value in daily_totals.items():
            x = padding + (day - 1) * (chart_width / num_days)
            bar_height = float(value / max_value * chart_height) if value > 0 else 0
            y = height - padding - bar_height

            svg_parts.append(
                f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_height}" '
                f'fill="#417690">'
                f"<title>Day {day}: €{value:,.0f}</title>"
                f"</rect>"
            )

            # Show all day labels
            label_x = x + bar_width / 2
            label_y = height - padding + 12
            svg_parts.append(
                f'<text x="{label_x}" y="{label_y}" text-anchor="middle" '
                f'font-size="9" fill="#666">{day}</text>'
            )

        svg_parts.append("</svg>")
        return "".join(svg_parts)

    def get_income_data(self, year: int, month: int | None = None):
        """Get income data aggregated by category."""
        # Build query with proper database-level filtering
        query = Invoice.objects.filter(kind=InvoiceKind.INVOICE, issue_date__year=year)
        if month:
            query = query.filter(issue_date__month=month)

        invoices = list(query.prefetch_related("invoiceitem_set"))

        # Pre-calculate totals in EUR to avoid exchange rate fluctuations
        invoice_totals = {inv.pk: inv.total_amount_no_vat for inv in invoices}

        # Aggregate by category manually since total_amount_no_vat is a property
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

        # Pre-calculate totals in EUR to avoid exchange rate fluctuations
        invoice_totals = {inv.pk: inv.total_amount_no_vat for inv in invoices}

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

        return monthly_totals, invoices

    def get_monthly_category_data(self, year: int):
        """Get monthly income data split by category for stacked chart."""
        invoices = list(
            Invoice.objects.filter(
                kind=InvoiceKind.INVOICE, issue_date__year=year
            ).prefetch_related("invoiceitem_set")
        )

        # Pre-calculate totals in EUR to avoid exchange rate fluctuations
        invoice_totals = {inv.pk: inv.total_amount_no_vat for inv in invoices}

        # Group by month and category
        monthly_category_data = {}
        for month in range(1, 13):
            month_key = f"{month:02d}"
            monthly_category_data[month_key] = {}

            for category in InvoiceCategory:
                total = sum(
                    (
                        invoice_totals[inv.pk]
                        for inv in invoices
                        if inv.issue_date.month == month
                        and inv.category == category.value
                    ),
                    start=Decimal(0),
                )
                monthly_category_data[month_key][category.label] = total

        return monthly_category_data, invoices

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

        # Generate charts
        # Always generate pie chart for both views
        context["pie_chart_svg"] = self.generate_svg_pie_chart(income_data)

        if month:
            # For monthly view, show daily chart
            # Get all invoices for the month
            month_invoices = list(
                Invoice.objects.filter(
                    kind=InvoiceKind.INVOICE,
                    issue_date__year=year,
                    issue_date__month=month,
                ).prefetch_related("invoiceitem_set")
            )

            context["daily_chart_svg"] = self.generate_svg_daily_chart(
                year, month, month_invoices
            )
            context["is_monthly"] = True
        else:
            # For yearly view, show stacked monthly chart
            monthly_data, invoices = self.get_monthly_data(year)
            context["chart_svg"] = self.generate_svg_stacked_bar_chart(
                monthly_data, invoices
            )
            context["monthly_data"] = monthly_data
            context["monthly_category_data"], _ = self.get_monthly_category_data(year)
            context["is_monthly"] = False

        return context
