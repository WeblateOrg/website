from __future__ import annotations

import calendar
import math
from datetime import date
from decimal import Decimal
from operator import attrgetter
from typing import TYPE_CHECKING, ClassVar, TypedDict, cast

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import (
    Case,
    DecimalField,
    ExpressionWrapper,
    F,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import (
    Coalesce,
    ExtractDay,
    ExtractMonth,
    Round,
    TruncMonth,
)
from django.http import FileResponse, Http404, HttpResponse
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
    from uuid import UUID

    from django.http import HttpRequest

    from weblate_web.payments.models import CustomerQuerySet
    from weblate_web.views import AuthenticatedHttpRequest


class InvoiceSummaryRow(TypedDict):
    pk: UUID
    category: int
    period: date | int
    total_no_vat: Decimal


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
            case "premium":
                return "Premium support services"
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
                    subscription__package__category=PackageCategory.PACKAGE_DEDICATED,
                    subscription__expires__gte=timezone.now(),
                    subscription__enabled=True,
                ).distinct()
            case "premium":
                return qs.filter(
                    subscription__package__name="premium",
                    subscription__expires__gte=timezone.now(),
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
                return redirect(service)
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
        if not self.object.attachment or not self.object.attachment.name:
            raise Http404("No attachment")
        return FileResponse(
            self.object.attachment.open(),
            as_attachment=True,
            filename=self.object.attachment.name,
        )


class IncomeView(CRMMixin, TemplateView):  # type: ignore[misc]
    template_name = "crm/income.html"
    permission = "invoices.view_income"
    title = "Income Tracking"
    MAX_MONTH_INDEX = date.max.year * 12 - 1

    # Chart configuration
    CHART_WIDTH = 800
    CHART_HEIGHT = 400
    CHART_PADDING = 60
    MIN_CHART_VALUE = Decimal(1)
    DECIMAL_OUTPUT_FIELD: ClassVar[DecimalField] = DecimalField(
        max_digits=16, decimal_places=3
    )

    # Category colors shared across all charts (keyed by category enum)
    CATEGORY_COLORS = {
        InvoiceCategory.HOSTING: "#417690",
        InvoiceCategory.SUPPORT: "#79aec8",
        InvoiceCategory.DEVEL: "#5b80b2",
        InvoiceCategory.DONATE: "#9fc5e8",
    }

    def get_year(self) -> int:
        """Get the year from URL kwargs or default to current year."""
        return self.kwargs.get("year", self._get_current_date().year)

    def get_month(self) -> int | None:
        """Get the month from URL kwargs if present."""
        return self.kwargs.get("month")

    def get_title(self) -> str:
        year = self.get_year()
        month = self.get_month()
        if month:
            return f"Income Tracking - {year}/{month:02d}"
        return f"Income Tracking - {year}"

    def generate_svg_pie_chart(self, data: dict[InvoiceCategory, Decimal]) -> str:  # noqa: PLR0914
        """Generate a simple SVG pie chart for category distribution with legend."""
        if not data or sum(data.values()) == 0:
            return ""

        # Calculate required width based on legend text
        # Longest category name is "Development / Consultations"
        # Estimate: 420 (legend_x) + 20 (icon) + 300 (text) = 740, round to 750
        width = 750
        height = 400
        radius = 120
        center_x = 200
        center_y = 200

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
            color = self.CATEGORY_COLORS.get(category, "#999")
            svg_parts.append(
                f'<circle cx="{center_x}" cy="{center_y}" r="{radius}" '
                f'fill="{color}" stroke="white" stroke-width="2">'
                f"<title>{category.label}: €{value:,.0f} (100.0%)</title>"
                f"</circle>"
            )
        else:
            # Draw pie slices for multiple categories
            start_angle: float = 0
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

                color = self.CATEGORY_COLORS.get(category, "#999")
                svg_parts.append(
                    f'<path d="M{center_x},{center_y} L{start_x},{start_y} '
                    f'A{radius},{radius} 0 {large_arc},1 {end_x},{end_y} Z" '
                    f'fill="{color}" stroke="white" stroke-width="2">'
                    f"<title>{category.label}: €{value:,.0f} ({value / total * 100:.1f}%)</title>"
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

            color = self.CATEGORY_COLORS.get(category, "#999")
            y_pos = legend_y + idx * legend_spacing

            # Legend color box
            svg_parts.append(
                f'<rect x="{legend_x}" y="{y_pos}" width="15" height="15" '
                f'fill="{color}" stroke="white" stroke-width="1"/>'
            )

            # Legend text
            svg_parts.append(
                f'<text x="{legend_x + 20}" y="{y_pos + 12}" font-size="11" fill="#333">'
                f"{category.label}: €{value:,.0f} ({value / total * 100:.0f}%)</text>"
            )

            idx += 1

        svg_parts.append("</svg>")
        return "".join(svg_parts)

    def generate_svg_stacked_bar_chart(  # noqa: PLR0914
        self,
        monthly_data: dict[str, Decimal],
        period_category_data: dict[str, dict[InvoiceCategory, Decimal]],
        year: int,
        month: int | None = None,
    ) -> str:
        """
        Generate a stacked bar chart showing totals by category.

        For yearly view (month=None): shows 12 monthly bars
        For monthly view (month=int): shows daily bars for that month
        """
        if not monthly_data:
            return ""

        width = self.CHART_WIDTH
        height = self.CHART_HEIGHT
        padding = self.CHART_PADDING
        chart_width = width - 2 * padding
        chart_height = height - 2 * padding

        # Determine number of bars and labels based on view type
        if month:
            # Monthly view: show daily bars
            num_bars = calendar.monthrange(year, month)[1]
            bar_labels = [str(d) for d in range(1, num_bars + 1)]
            label_prefix = "Day"
        else:
            # Yearly view: show monthly bars
            num_bars = 12
            bar_labels = [f"{m:02d}" for m in range(1, 13)]
            label_prefix = ""

        # Get max value for scaling
        max_value = max(monthly_data.values()) if monthly_data.values() else Decimal(1)
        if max_value <= 0:
            max_value = self.MIN_CHART_VALUE

        svg_parts = [
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" class="stacked-bar-chart">',
        ]

        # Calculate bar properties
        bar_spacing = chart_width / (num_bars * 1.5)
        bar_width = bar_spacing * 0.8

        # Draw each bar
        for idx in range(num_bars):
            bar_label = bar_labels[idx]
            x: float = padding + bar_spacing * (idx + 0.5)
            category_totals = period_category_data.get(bar_label, {})

            # Stack bars by category
            y_offset: float = height - padding
            for category in InvoiceCategory:
                category_total = category_totals.get(category, Decimal(0))

                if category_total > 0:
                    bar_height = float(category_total / max_value * chart_height)
                    y = y_offset - bar_height

                    title_label = (
                        f"{label_prefix} {bar_label}" if label_prefix else bar_label
                    )
                    svg_parts.append(
                        f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_height}" '
                        f'fill="{self.CATEGORY_COLORS.get(category, "#999")}" stroke="white" stroke-width="1">'
                        f"<title>{category.label} - {title_label}: €{category_total:,.0f}</title>"
                        f"</rect>"
                    )
                    y_offset = y

            # Bar label
            label_x = x + bar_width / 2
            label_y = height - padding + 15
            font_size = (
                "9" if month else "10"
            )  # Smaller font for daily view (more bars)
            svg_parts.append(
                f'<text x="{label_x}" y="{label_y}" text-anchor="middle" '
                f'font-size="{font_size}" fill="#666">{bar_label}</text>'
            )

        svg_parts.append("</svg>")
        return "".join(svg_parts)

    def _get_month_start(self, year: int, month: int) -> date:
        return date(year, month, 1)

    def _shift_month(self, month_start: date, offset: int) -> date | None:
        month_index = (month_start.year - 1) * 12 + month_start.month - 1 + offset
        if month_index < 0 or month_index > self.MAX_MONTH_INDEX:
            return None
        year, zero_based_month = divmod(month_index, 12)
        return date(year + 1, zero_based_month + 1, 1)

    def _iter_month_starts(
        self, start_month: date | None, end_month: date
    ) -> list[date]:
        if start_month is None or start_month > end_month:
            return []

        month_starts = []
        current_month: date | None = start_month
        while current_month is not None and current_month <= end_month:
            month_starts.append(current_month)
            current_month = self._shift_month(current_month, 1)
        return month_starts

    def _get_invoice_summary_rows(
        self,
        year: int | None = None,
        month: int | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        period_expr=None,
    ) -> list[InvoiceSummaryRow]:
        """Fetch per-invoice totals for the report using SQL aggregation."""
        zero = Value(Decimal(0), output_field=self.DECIMAL_OUTPUT_FIELD)
        line_total = ExpressionWrapper(
            F("invoiceitem__unit_price") * F("invoiceitem__quantity"),
            output_field=self.DECIMAL_OUTPUT_FIELD,
        )

        query = Invoice.objects.filter(
            kind=InvoiceKind.INVOICE,
        )
        if year is not None:
            query = query.filter(issue_date__year=year)
        if month:
            query = query.filter(issue_date__month=month)
        if start_date is not None:
            query = query.filter(issue_date__gte=start_date)
        if end_date is not None:
            query = query.filter(issue_date__lt=end_date)

        if period_expr is None:
            period_expr = (
                ExtractDay("issue_date") if month else ExtractMonth("issue_date")
            )

        return cast(
            "list[InvoiceSummaryRow]",
            list(
                query.annotate(period=period_expr)
                .values("pk", "category", "period")
                .annotate(
                    items_total=Coalesce(Sum(line_total), zero),
                    positive_items_total=Coalesce(
                        Sum(
                            Case(
                                When(invoiceitem__unit_price__gt=0, then=line_total),
                                default=zero,
                                output_field=self.DECIMAL_OUTPUT_FIELD,
                            )
                        ),
                        zero,
                    ),
                )
                .annotate(
                    discount_amount=Case(
                        When(
                            discount__isnull=False,
                            then=Round(
                                ExpressionWrapper(
                                    -F("positive_items_total")
                                    * F("discount__percents")
                                    / Value(100),
                                    output_field=self.DECIMAL_OUTPUT_FIELD,
                                ),
                                precision=0,
                            ),
                        ),
                        default=zero,
                        output_field=self.DECIMAL_OUTPUT_FIELD,
                    )
                )
                .annotate(
                    total_no_vat=ExpressionWrapper(
                        F("items_total") + F("discount_amount"),
                        output_field=self.DECIMAL_OUTPUT_FIELD,
                    )
                )
                .values("pk", "category", "period", "total_no_vat")
            ),
        )

    def _get_empty_category_totals(self) -> dict[InvoiceCategory, Decimal]:
        return {category: Decimal(0) for category in InvoiceCategory}

    def _aggregate_period_totals(
        self,
        summary_rows: list[InvoiceSummaryRow],
        period_keys: list[str],
    ) -> tuple[dict[str, Decimal], dict[str, dict[InvoiceCategory, Decimal]]]:
        period_totals = {key: Decimal(0) for key in period_keys}
        period_category_data = {
            key: self._get_empty_category_totals() for key in period_keys
        }
        use_zero_padding = len(period_keys[0]) == 2

        for row in summary_rows:
            key = (
                f"{row['period']:02d}"
                if use_zero_padding
                else str(cast("int", row["period"]))
            )
            category = InvoiceCategory(cast("int", row["category"]))
            total = cast("Decimal", row["total_no_vat"])
            period_totals[key] += total
            period_category_data[key][category] += total

        return period_totals, period_category_data

    def _get_month_totals_in_range(
        self, start_month: date | None, end_month: date
    ) -> dict[date, Decimal]:
        if start_month is None or start_month > end_month:
            return {}

        month_totals = {
            month_start: Decimal(0)
            for month_start in self._iter_month_starts(start_month, end_month)
        }
        end_date = self._shift_month(end_month, 1)
        if end_date is None:
            rows = self._get_invoice_summary_rows(
                start_date=start_month,
                period_expr=TruncMonth("issue_date"),
            )
        else:
            rows = self._get_invoice_summary_rows(
                start_date=start_month,
                end_date=end_date,
                period_expr=TruncMonth("issue_date"),
            )

        for row in rows:
            month_start = cast("date", row["period"])
            month_totals[month_start] += cast("Decimal", row["total_no_vat"])
        return month_totals

    def _sum_month_totals(
        self,
        month_totals: dict[date, Decimal],
        start_month: date | None,
        end_month: date | None,
    ) -> Decimal:
        if start_month is None or end_month is None or start_month > end_month:
            return Decimal(0)

        total = Decimal(0)
        current_month: date | None = start_month
        while current_month is not None and current_month <= end_month:
            total += month_totals.get(current_month, Decimal(0))
            current_month = self._shift_month(current_month, 1)
        return total

    def _get_current_date(self) -> date:
        return timezone.localdate()

    def _get_current_month_start(self) -> date:
        return self._get_current_date().replace(day=1)

    def _build_rolling_window_summary(
        self, month_totals: dict[date, Decimal], period_start: date
    ) -> dict[str, int | Decimal | bool] | None:
        rolling_start = self._shift_month(period_start, -11)
        previous_start = self._shift_month(period_start, -23)
        previous_end = self._shift_month(period_start, -12)
        if rolling_start is None or previous_start is None or previous_end is None:
            return None

        rolling_total = self._sum_month_totals(
            month_totals,
            rolling_start,
            period_start,
        )
        previous_total = self._sum_month_totals(
            month_totals,
            previous_start,
            previous_end,
        )
        change_amount = rolling_total - previous_total
        has_change_percent = previous_total > 0
        return {
            "period_year": period_start.year,
            "period_month": period_start.month,
            "rolling_total": rolling_total,
            "previous_total": previous_total,
            "change_amount": change_amount,
            "change_percent": (
                change_amount / previous_total * Decimal(100)
                if has_change_percent
                else Decimal(0)
            ),
            "has_change_percent": has_change_percent,
        }

    def get_rolling_window_summary(
        self, year: int, month: int
    ) -> dict[str, int | Decimal | bool] | None:
        try:
            period_start = self._get_month_start(year, month)
        except ValueError:
            return None
        if period_start > self._get_current_month_start():
            return None
        lookback_start = self._shift_month(period_start, -23)
        month_totals = self._get_month_totals_in_range(lookback_start, period_start)
        return self._build_rolling_window_summary(month_totals, period_start)

    def get_yearly_breakdown_rows(
        self, year: int, monthly_data: dict[str, Decimal]
    ) -> list[dict[str, str | int | Decimal | bool]]:
        rows: list[dict[str, str | int | Decimal | bool]] = [
            {
                "month": f"{month:02d}",
                "month_number": month,
                "amount": monthly_data[f"{month:02d}"],
                "trend_available": False,
            }
            for month in range(1, 13)
        ]

        try:
            first_month = self._get_month_start(year, 1)
            last_month = self._get_month_start(year, 12)
        except ValueError:
            return rows

        current_month_start = self._get_current_month_start()
        if first_month > current_month_start:
            return rows

        trend_end_month = min(last_month, current_month_start)
        month_totals = self._get_month_totals_in_range(
            self._shift_month(first_month, -23),
            trend_end_month,
        )
        for row in rows:
            period_start = self._get_month_start(year, cast("int", row["month_number"]))
            if period_start > current_month_start:
                continue

            rolling_summary = self._build_rolling_window_summary(
                month_totals, period_start
            )
            if rolling_summary is None:
                continue

            row["trend_available"] = True
            row.update(rolling_summary)
        return rows

    def get_yearly_rolling_summary(
        self,
        year: int,
        monthly_breakdown_rows: list[dict[str, str | int | Decimal | bool]],
        current_year: int,
    ) -> dict[str, int | Decimal | bool] | None:
        trend_month = 12 if year < current_year else self._get_current_date().month
        if not 1 <= trend_month <= len(monthly_breakdown_rows):
            return None

        trend_row = monthly_breakdown_rows[trend_month - 1]
        if not cast("bool", trend_row["trend_available"]):
            return None

        return {
            "period_year": cast("int", trend_row["period_year"]),
            "period_month": cast("int", trend_row["period_month"]),
            "rolling_total": cast("Decimal", trend_row["rolling_total"]),
            "previous_total": cast("Decimal", trend_row["previous_total"]),
            "change_amount": cast("Decimal", trend_row["change_amount"]),
            "change_percent": cast("Decimal", trend_row["change_percent"]),
            "has_change_percent": cast("bool", trend_row["has_change_percent"]),
        }

    def get_income_data(
        self, year: int, month: int | None = None
    ) -> dict[InvoiceCategory, Decimal]:
        """Get income data aggregated by category."""
        category_data = self._get_empty_category_totals()
        for row in self._get_invoice_summary_rows(year, month):
            category = InvoiceCategory(cast("int", row["category"]))
            category_data[category] += cast("Decimal", row["total_no_vat"])

        return category_data

    def get_monthly_data(
        self, year: int
    ) -> tuple[dict[str, Decimal], dict[str, dict[InvoiceCategory, Decimal]]]:
        """Get monthly income data for the year."""
        monthly_keys = [f"{month:02d}" for month in range(1, 13)]
        return self._aggregate_period_totals(
            self._get_invoice_summary_rows(year),
            monthly_keys,
        )

    def get_daily_data(
        self, year: int, month: int
    ) -> tuple[dict[str, Decimal], dict[str, dict[InvoiceCategory, Decimal]]]:
        """Get daily income data for a specific month."""
        num_days = calendar.monthrange(year, month)[1]
        daily_keys = [str(day) for day in range(1, num_days + 1)]
        return self._aggregate_period_totals(
            self._get_invoice_summary_rows(year, month),
            daily_keys,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        year = self.get_year()
        month = self.get_month()

        # Get income data (returns dict with InvoiceCategory keys)
        income_data = self.get_income_data(year, month)

        # Convert to label-keyed dict for template display
        income_data_labels = {cat.label: amount for cat, amount in income_data.items()}
        context["income_data"] = income_data_labels
        context["total_income"] = sum(income_data.values())

        # Navigation years (show last 5 years and next year)
        current_year = self._get_current_date().year
        context["years"] = list(range(current_year - 5, current_year + 2))
        context["current_year"] = year
        context["current_month"] = month

        # Generate charts (pie chart uses enum-keyed dict)
        # Always generate pie chart for both views
        context["pie_chart_svg"] = self.generate_svg_pie_chart(income_data)

        if month:
            # For monthly view, show daily stacked chart
            daily_data, daily_category_data = self.get_daily_data(year, month)
            context["daily_chart_svg"] = self.generate_svg_stacked_bar_chart(
                daily_data, daily_category_data, year, month
            )
            context["is_monthly"] = True
            context["rolling_trend"] = self.get_rolling_window_summary(year, month)
        else:
            # For yearly view, show monthly stacked chart
            monthly_data, monthly_category_data = self.get_monthly_data(year)
            context["chart_svg"] = self.generate_svg_stacked_bar_chart(
                monthly_data, monthly_category_data, year
            )
            context["monthly_data"] = monthly_data
            context["monthly_category_data"] = monthly_category_data
            context["monthly_breakdown_rows"] = self.get_yearly_breakdown_rows(
                year, monthly_data
            )
            context["rolling_trend"] = self.get_yearly_rolling_summary(
                year, context["monthly_breakdown_rows"], current_year
            )
            context["is_monthly"] = False

        return context
