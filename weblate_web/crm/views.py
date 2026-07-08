from __future__ import annotations

import calendar
import math
from datetime import date
from decimal import Decimal
from operator import attrgetter
from typing import TYPE_CHECKING, ClassVar, TypedDict, cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import DataError, IntegrityError, transaction
from django.db.models import (
    Case,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
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
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext, override
from django.views.generic import DetailView, ListView, TemplateView

from weblate_web.crm.forms import (
    CRMSearchForm,
    CustomerFollowUpForm,
    CustomerMergeForm,
    CustomerUserForm,
    InvoiceConfirmationForm,
    ManualInteractionForm,
    QuoteStatusForm,
    RefundConfirmationForm,
    ServiceMaintenanceWindowForm,
    ServiceSubscriptionActionForm,
)
from weblate_web.crm.hosted import HostedUserEnsureError, ensure_hosted_user
from weblate_web.crm.workqueue import (
    DASHBOARD_WORK_QUEUE_LIMIT,
    get_crm_work_items,
    get_crm_work_queue_sections,
    get_expired_service_ids,
    get_unpaid_invoice_queryset,
)
from weblate_web.forms import NewSubscriptionForm
from weblate_web.invoices.forms import CustomerReferenceForm
from weblate_web.invoices.models import (
    CURRENCY_MAP,
    Invoice,
    InvoiceCategory,
    InvoiceKind,
    QuoteStatus,
)
from weblate_web.models import (
    PackageCategory,
    SamlIdentity,
    Service,
    Subscription,
)
from weblate_web.payments.models import Customer, CustomerFollowUp, Payment
from weblate_web.saml import (
    get_default_saml_provider,
    normalize_external_id,
    sync_saml_payload,
)
from weblate_web.utils import show_form_errors

from .models import Interaction

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest

    from weblate_web.invoices.models import Currency
    from weblate_web.views import AuthenticatedHttpRequest


class InvoiceSummaryRow(TypedDict):
    pk: UUID
    category: int
    period: date | int
    total_no_vat: Decimal


def has_invoice_confirmation(request: HttpRequest) -> bool:
    form = InvoiceConfirmationForm(request.POST)
    if form.is_valid():
        return True
    show_form_errors(request, form)
    return False


class CustomerHostedUserContext(TypedDict):
    email: str
    hosted_created: bool
    email_users: list[User]


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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        work_queue_items = get_crm_work_items(self.request.user)
        context["work_queue_items"] = work_queue_items[:DASHBOARD_WORK_QUEUE_LIMIT]
        context["work_queue_more_count"] = max(
            len(work_queue_items) - DASHBOARD_WORK_QUEUE_LIMIT, 0
        )
        return context


class WorkQueueView(CRMMixin, TemplateView):  # type: ignore[misc]
    template_name = "crm/work_queue.html"
    title = "Today"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        context["work_queue_sections"] = get_crm_work_queue_sections(self.request.user)
        return context


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
        qs = Service.objects.customer_services().prefetch_related("subscription_set")
        match self.kwargs["kind"]:
            case "all":
                return sorted(qs, key=attrgetter("package_kind"))
            case "expired":
                expired = qs.filter(pk__in=get_expired_service_ids()).distinct()
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
        context["subscription_action_form"] = ServiceSubscriptionActionForm(
            service=self.object
        )
        context["invoice_kind_quote"] = int(InvoiceKind.QUOTE)
        context["invoice_kind_invoice"] = int(InvoiceKind.INVOICE)
        context["invoice_confirm_dialog"] = True
        if self.object.has_active_extended_support:
            context["maintenance_window_form"] = ServiceMaintenanceWindowForm(
                instance=self.object
            )
        return context

    def update_maintenance_window(self, request, service):
        if not service.has_active_extended_support:
            raise PermissionDenied
        maintenance_window = service.maintenance_window
        maintenance_window_form = ServiceMaintenanceWindowForm(
            request.POST, instance=service
        )
        if not maintenance_window_form.is_valid():
            show_form_errors(self.request, maintenance_window_form)
            return redirect(service)

        new_maintenance_window = maintenance_window_form.cleaned_data[
            "maintenance_window"
        ]
        if new_maintenance_window != maintenance_window:
            service.maintenance_window = new_maintenance_window
            service.save(update_fields=["maintenance_window"])
            service.customer.interaction_set.create(
                origin=Interaction.Origin.MAINTENANCE_WINDOW,
                summary=f"Maintenance window updated for service {service.pk}",
                content=new_maintenance_window,
                details={
                    "service_id": service.pk,
                    "service_title": service.site_title,
                    "service_url": service.site_url,
                    "old_value": maintenance_window,
                    "new_value": new_maintenance_window,
                },
                user=request.user,
            )
        return redirect(service)

    def get_upgrade_invoice_package(self, request, service, subscription, package):
        try:
            if not subscription.upgrade_requires_payment(package):
                subscription.upgrade_without_payment(package)
                return package, redirect(service)
        except ValueError:
            messages.error(
                request,
                gettext(
                    "This subscription can not be upgraded to the selected package."
                ),
            )
            return package, redirect(service)

        return package, None

    def create_subscription_invoice(
        self,
        request,
        service: Service,
        form: ServiceSubscriptionActionForm,
        *,
        upgrade: bool,
    ):
        subscription = form.cleaned_data["subscription"]
        kind = form.cleaned_data["kind"]
        if upgrade:
            create_invoice = subscription.create_upgrade_invoice
        else:
            create_invoice = subscription.create_invoice

        package = None
        if upgrade:
            package, response = self.get_upgrade_invoice_package(
                request, service, subscription, form.cleaned_data["package"]
            )
            if response is not None:
                return response

        with override("en"):
            kwargs = {
                "kind": kind,
                "customer_reference": form.cleaned_data["customer_reference"],
                "customer_note": form.cleaned_data["customer_note"],
            }
            if package is not None:
                kwargs["package"] = package
            try:
                invoice = create_invoice(**kwargs)
            except ValueError:
                messages.error(
                    request,
                    gettext(
                        "This subscription can not be upgraded to the selected package."
                    ),
                )
                return redirect(service)
        return redirect(invoice)

    def post(self, request, *args, **kwargs):
        service = self.get_object()
        if "update_maintenance_window" in request.POST:
            return self.update_maintenance_window(request, service)

        form = ServiceSubscriptionActionForm(request.POST, service=service)
        if not form.is_valid():
            show_form_errors(request, form)
            return redirect(service)

        action = form.cleaned_data["action"]
        if action == ServiceSubscriptionActionForm.ACTION_UPGRADE:
            return self.create_subscription_invoice(
                request, service, form, upgrade=True
            )
        if action == ServiceSubscriptionActionForm.ACTION_RENEWAL:
            return self.create_subscription_invoice(
                request, service, form, upgrade=False
            )
        if action == ServiceSubscriptionActionForm.ACTION_DISABLE:
            subscription = form.cleaned_data["subscription"]
            subscription.enabled = False
            subscription.save(update_fields=["enabled"])
            return redirect(service)

        raise ValueError("Missing action!")


class InvoiceListView(CRMMixin, ListView[Invoice]):  # type: ignore[misc]
    model = Invoice
    permission = "invoices.view_invoice"
    title = "Invoices"
    paginate_by = 100
    _search_form: CRMSearchForm | None = None

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

    def get_search_form(self) -> CRMSearchForm:
        if self._search_form is None:
            self._search_form = CRMSearchForm(self.request.GET)
        return self._search_form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        search_form = self.get_search_form()
        context["search_form"] = search_form
        context["query"] = search_form["q"].value() or ""
        return context

    def get_queryset(self):
        qs = super().get_queryset().order_by("-number")
        if self.kwargs["kind"] in {"all", "quote"}:
            qs = qs.annotate(
                has_child_invoice=Exists(Invoice.objects.filter(parent=OuterRef("pk")))
            )
        match self.kwargs["kind"]:
            case "unpaid":
                qs = get_unpaid_invoice_queryset().order_by("-number")
            case "quote":
                qs = qs.filter(kind=InvoiceKind.QUOTE)
            case "invoice":
                qs = qs.filter(kind=InvoiceKind.INVOICE)
            case "all":
                pass
            case _:
                raise ValueError(self.kwargs["kind"])

        search_form = self.get_search_form()
        if search_form.is_valid() and (query := search_form.cleaned_data["q"]):
            qs = qs.filter(
                Q(number__icontains=query)
                | Q(customer__name__icontains=query)
                | Q(customer__email__icontains=query)
                | Q(invoiceitem__description__icontains=query)
            ).distinct()
        return qs


class InvoiceDetailView(CRMMixin, DetailView[Invoice]):  # type: ignore[misc]
    model = Invoice
    permission = "invoices.view_invoice"
    refund_permission = "payments.add_payment"
    quote_status_permission = "invoices.change_invoice"
    title = "Invoice detail"

    def get_title(self) -> str:
        return f"{self.object.get_kind_display()} {self.object.number}"

    def is_unconverted_quote(self) -> bool:
        return (
            self.object.kind == InvoiceKind.QUOTE and not self.object.is_converted_quote
        )

    def can_convert(self) -> bool:
        return (
            self.is_unconverted_quote() and self.object.quote_status == QuoteStatus.OPEN
        )

    def can_update_quote_status_permission(self) -> bool:
        return self.request.user.has_perm(self.quote_status_permission)

    def can_close_quote(self) -> bool:
        return (
            self.can_update_quote_status_permission()
            and self.is_unconverted_quote()
            and self.object.quote_status == QuoteStatus.OPEN
        )

    def can_reopen_quote(self) -> bool:
        return (
            self.can_update_quote_status_permission()
            and self.is_unconverted_quote()
            and self.object.quote_status != QuoteStatus.OPEN
        )

    def can_confirm_refund(self) -> bool:
        return (
            self.object.kind == InvoiceKind.INVOICE
            and self.object.total_amount <= 0
            and not self.object.is_paid
        )

    def can_confirm_refund_permission(self) -> bool:
        return self.request.user.has_perm(self.refund_permission)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        context["invoice_kind_quote"] = int(InvoiceKind.QUOTE)
        context["invoice_kind_invoice"] = int(InvoiceKind.INVOICE)
        if self.can_convert():
            context["convert_form"] = CustomerReferenceForm(
                self.request.POST if self.request.method == "POST" else None,
                initial={
                    "customer_reference": self.object.customer_reference,
                    "customer_note": self.object.customer_note,
                },
            )
            context["invoice_confirm_dialog"] = True
        if self.can_confirm_refund() and self.can_confirm_refund_permission():
            context["refund_form"] = RefundConfirmationForm(
                self.request.POST
                if self.request.method == "POST"
                and "confirm_refund" in self.request.POST
                else None
            )
        if self.can_close_quote():
            context["quote_status_form"] = QuoteStatusForm(
                self.request.POST
                if self.request.method == "POST" and "close_quote" in self.request.POST
                else None
            )
        context["can_reopen_quote"] = self.can_reopen_quote()
        return context

    def create_quote_status_interaction(
        self, *, previous_status: QuoteStatus, previous_note: str
    ) -> None:
        quote_status = QuoteStatus(self.object.quote_status)
        self.object.customer.interaction_set.create(
            origin=Interaction.Origin.QUOTE_STATUS,
            summary=f"Quote {self.object.number} marked as {quote_status.label}",
            content=self.object.quote_status_note,
            details={
                "invoice": self.object.number,
                "quote_status": str(quote_status.label),
                "quote_status_note": self.object.quote_status_note,
                "previous_quote_status": str(previous_status.label),
                "previous_quote_status_note": previous_note,
            },
            user=self.request.user,
        )

    def close_quote(self, request, *args, **kwargs):
        if not self.can_update_quote_status_permission():
            raise PermissionDenied
        if not self.can_close_quote():
            return redirect(self.object)

        form = QuoteStatusForm(request.POST)
        if not form.is_valid():
            show_form_errors(request, form)
            return self.get(request, *args, **kwargs)

        previous_status = QuoteStatus(self.object.quote_status)
        previous_note = self.object.quote_status_note
        self.object.quote_status = form.cleaned_data["quote_status"]
        self.object.quote_status_note = form.cleaned_data["quote_status_note"]
        self.object.save(update_fields=["quote_status", "quote_status_note"])
        self.create_quote_status_interaction(
            previous_status=previous_status,
            previous_note=previous_note,
        )
        return redirect(self.object)

    def reopen_quote(self, request):
        if not self.can_update_quote_status_permission():
            raise PermissionDenied
        if not self.can_reopen_quote():
            return redirect(self.object)

        previous_status = QuoteStatus(self.object.quote_status)
        previous_note = self.object.quote_status_note
        self.object.quote_status = QuoteStatus.OPEN
        self.object.quote_status_note = ""
        self.object.save(update_fields=["quote_status", "quote_status_note"])
        self.create_quote_status_interaction(
            previous_status=previous_status,
            previous_note=previous_note,
        )
        return redirect(self.object)

    def confirm_refund(self, description: str) -> Payment | None:
        if not self.can_confirm_refund_permission():
            raise PermissionDenied

        with transaction.atomic():
            self.object = Invoice.objects.select_for_update().get(pk=self.object.pk)
            if not self.can_confirm_refund():
                return None

            description = description.strip()
            payment_description = (
                description or f"Refund for invoice {self.object.number}"
            )
            confirmed_at = timezone.now()
            payment = Payment.objects.create(
                amount=int(self.object.total_amount),
                amount_fixed=True,
                backend="manual",
                currency=CURRENCY_MAP[cast("Currency", self.object.currency)],
                customer=self.object.customer,
                description=payment_description,
                extra={
                    "source": "crm",
                    "action": "refund-confirmed",
                    "invoice": self.object.number,
                    "confirmed_at": confirmed_at.isoformat(),
                    "user": self.request.user.get_username(),
                    "user_id": self.request.user.pk,
                    **({"description": description} if description else {}),
                },
                invoice=self.object.number,
                paid_invoice=self.object,
                state=Payment.PROCESSED,
            )
            self.object.generate_receipt()

            summary = f"Refund confirmed for invoice {self.object.number}"
            if description:
                summary = f"{summary}: {description}"

            self.object.customer.interaction_set.create(
                origin=Interaction.Origin.MANUAL_PAYMENT,
                summary=summary[:200],
                content=description or self.object.display_total_amount,
                details={
                    "invoice": self.object.number,
                    "amount": self.object.display_total_amount,
                    "payment_id": str(payment.pk),
                    "confirmed_by": self.request.user.get_username(),
                    "confirmed_at": timezone.localtime(confirmed_at).isoformat(),
                    **({"description": description} if description else {}),
                },
                user=self.request.user,
            )

        return payment

    def handle_quote_status_post(self, request, *args, **kwargs):
        if "close_quote" in request.POST:
            return self.close_quote(request, *args, **kwargs)
        if "reopen_quote" in request.POST:
            return self.reopen_quote(request)
        return None

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if quote_status_response := self.handle_quote_status_post(
            request, *args, **kwargs
        ):
            return quote_status_response

        if "confirm_refund" in request.POST:
            if not self.can_confirm_refund_permission():
                raise PermissionDenied
            if not self.can_confirm_refund():
                return redirect(self.object)
            refund_form = RefundConfirmationForm(request.POST)
            if refund_form.is_valid():
                self.confirm_refund(refund_form.cleaned_data["description"])
                return redirect(self.object)
            show_form_errors(self.request, refund_form)
            return self.get(request, *args, **kwargs)

        quote = self.object
        convert_form = CustomerReferenceForm(request.POST)
        if (
            convert_form.is_valid()
            and self.can_convert()
            and has_invoice_confirmation(request)
        ):
            with override("en"):
                invoice = quote.duplicate(
                    kind=InvoiceKind.INVOICE,
                    customer_reference=convert_form.cleaned_data["customer_reference"],
                    customer_note=convert_form.cleaned_data["customer_note"],
                )
                invoice.generate_files()
            return redirect(invoice)
        return self.get(request, *args, **kwargs)


class CustomerListView(CRMMixin, ListView):  # type: ignore[misc]
    model = Customer
    permission = "payments.view_customer"
    title = "Customers"
    template_name = "payments/customer_list.html"
    paginate_by = 100
    _search_form: CRMSearchForm | None = None

    def get_title(self) -> str:
        match self.kwargs["kind"]:
            case "active":
                return "Active customer"
            case "followups":
                return "Customer follow-ups"
            case "all":
                return "All customers"
        raise ValueError(self.kwargs["kind"])

    def get_search_form(self) -> CRMSearchForm:
        if self._search_form is None:
            self._search_form = CRMSearchForm(self.request.GET)
        return self._search_form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        search_form = self.get_search_form()
        context["search_form"] = search_form
        context["query"] = search_form["q"].value() or ""
        context["kind"] = self.kwargs["kind"]
        return context

    def get_customer_queryset(self):
        qs = Customer.objects.order()
        search_form = self.get_search_form()
        if search_form.is_valid() and (query := search_form.cleaned_data["q"]):
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(email__icontains=query)
                | Q(users__email__icontains=query)
                | Q(end_client__icontains=query)
            ).distinct()
        return qs

    def get_followup_queryset(self):
        qs = CustomerFollowUp.objects.order()
        search_form = self.get_search_form()
        if search_form.is_valid() and (query := search_form.cleaned_data["q"]):
            qs = qs.filter(
                Q(customer__name__icontains=query)
                | Q(customer__email__icontains=query)
                | Q(customer__users__email__icontains=query)
                | Q(customer__end_client__icontains=query)
                | Q(note__icontains=query)
            ).distinct()
        return qs

    def get_queryset(self):
        qs = self.get_customer_queryset()
        match self.kwargs["kind"]:
            case "active":
                return qs.active()
            case "followups":
                return self.get_followup_queryset()
            case "all":
                return qs
        raise ValueError(self.kwargs["kind"])


class CustomerDetailView(CRMMixin, DetailView[Customer]):  # type: ignore[misc]
    model = Customer
    permission = "payments.view_customer"
    add_customer_user_permission = "payments.change_customer"
    change_customer_permission = "payments.change_customer"
    title = "Customer detail"
    valid_tabs: ClassVar[frozenset[str]] = frozenset(
        {"overview", "interactions", "invoices", "payments"}
    )

    def get_active_tab(self) -> str:
        tab = self.request.GET.get("tab", "overview")
        if tab in self.valid_tabs:
            return tab
        return "overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        active_tab = self.get_active_tab()
        add_manual_note = (
            self.request.method == "POST" and "add_manual_note" in self.request.POST
        )
        add_customer_user = (
            self.request.method == "POST" and "add_customer_user" in self.request.POST
        )
        set_follow_up = (
            self.request.method == "POST" and "set_follow_up" in self.request.POST
        )
        context["new_subscription_form"] = NewSubscriptionForm(
            self.request.POST
            if self.request.method == "POST"
            and not add_manual_note
            and not add_customer_user
            and not set_follow_up
            else None
        )
        context["invoice_kind_quote"] = int(InvoiceKind.QUOTE)
        context["invoice_kind_invoice"] = int(InvoiceKind.INVOICE)
        context["manual_interaction_form"] = ManualInteractionForm(
            self.request.POST if add_manual_note else None
        )
        context["customer_user_form"] = CustomerUserForm(
            self.request.POST if add_customer_user else None
        )
        context["follow_up_form"] = CustomerFollowUpForm(
            self.request.POST if set_follow_up else None,
            customer=self.object,
        )
        context["followups"] = CustomerFollowUp.objects.filter(
            customer=self.object
        ).order()
        context["can_add_customer_user"] = self.request.user.has_perm(
            self.add_customer_user_permission
        )
        context["can_change_customer"] = self.request.user.has_perm(
            self.change_customer_permission
        )
        context["merge_form"] = CustomerMergeForm(customer=self.object)
        services = self.object.service_set.customer_services().order()
        context["services"] = services
        context["invoice_confirm_dialog"] = active_tab == "overview" and not services
        context["donations"] = self.object.service_set.donations().order()
        context["customer_users"] = self.object.ordered_users
        context["active_tab"] = active_tab
        return context

    @staticmethod
    def get_tab_url(customer: Customer, tab: str) -> str:
        return f"{customer.get_absolute_url()}?tab={tab}"

    @classmethod
    def check_add_customer_user_permission(
        cls, request: AuthenticatedHttpRequest
    ) -> None:
        if not request.user.has_perm(cls.add_customer_user_permission):
            raise PermissionDenied

    @classmethod
    def check_change_customer_permission(
        cls, request: AuthenticatedHttpRequest
    ) -> None:
        if not request.user.has_perm(cls.change_customer_permission):
            raise PermissionDenied

    def get_title(self) -> str:
        return self.object.verbose_name

    @staticmethod
    def get_email_users(email: str) -> list[User]:
        return list(User.objects.filter(email__iexact=email).order_by("pk"))

    @staticmethod
    def get_username_users(username: object) -> list[User]:
        if not isinstance(username, str) or not username:
            return []
        return list(User.objects.filter(username__iexact=username).order_by("pk"))

    @staticmethod
    def has_hosted_identity(hosted_payload) -> bool:
        provider = str(hosted_payload.get("provider", get_default_saml_provider()))
        external_id = normalize_external_id(hosted_payload.get("external_id"))
        if not external_id:
            return False
        return SamlIdentity.objects.filter(
            provider=provider, external_id=external_id
        ).exists()

    @staticmethod
    def get_user_admin_links(users: list[User]) -> str:
        return format_html_join(
            ", ",
            '<a href="{}">{} ({})</a>',
            (
                (
                    reverse("admin:auth_user_change", kwargs={"object_id": user.pk}),
                    user.username,
                    user.pk,
                )
                for user in users
            ),
        )

    def reject_username_only_hosted_match(
        self, hosted_payload, context: CustomerHostedUserContext
    ) -> None:
        if context["email_users"] or self.has_hosted_identity(hosted_payload):
            return
        profile = hosted_payload.get("profile")
        if not isinstance(profile, dict):
            return
        if self.get_username_users(profile.get("username")):
            raise HostedUserEnsureError(
                gettext("Hosted username matches a local user with a different e-mail.")
            )

    def link_customer_hosted_user(
        self,
        request,
        customer: Customer,
        hosted_payload,
        context: CustomerHostedUserContext,
    ) -> bool:
        self.reject_username_only_hosted_match(hosted_payload, context)
        with transaction.atomic():
            user, created = sync_saml_payload(hosted_payload)
            if user is None:
                raise HostedUserEnsureError(gettext("Hosted user was not linked."))
            if context["email_users"] and user.pk != context["email_users"][0].pk:
                raise HostedUserEnsureError(
                    gettext("Hosted user is already linked to a different local user.")
                )
            if customer.users.filter(pk=user.pk).exists():
                return False
            customer.users.add(user)
            customer.interaction_set.create(
                origin=Interaction.Origin.MANUAL_NOTE,
                summary=gettext("Added customer user"),
                content=gettext(
                    "Added %(email)s to customer users. Hosted user was %(state)s."
                )
                % {
                    "email": context["email"],
                    "state": gettext("created")
                    if context["hosted_created"]
                    else gettext("linked"),
                },
                details={
                    "user": user.pk,
                    "email": context["email"],
                    "hosted_created": context["hosted_created"],
                    "local_created": created,
                },
                user=request.user,
            )
            return True

    def add_customer_user(self, request, customer: Customer, *args, **kwargs):
        self.check_add_customer_user_permission(request)
        form = CustomerUserForm(request.POST)
        if not form.is_valid():
            show_form_errors(request, form)
            return self.get(request, *args, **kwargs)

        email = form.cleaned_data["email"]
        full_name = form.cleaned_data["full_name"]
        email_users = self.get_email_users(email)
        if len(email_users) > 1:
            messages.error(
                request,
                format_html(
                    "{}: {}",
                    gettext("Multiple local users use this e-mail address"),
                    self.get_user_admin_links(email_users),
                ),
            )
            return self.get(request, *args, **kwargs)

        try:
            hosted_payload, hosted_created = ensure_hosted_user(email, full_name)
            added = self.link_customer_hosted_user(
                request,
                customer,
                hosted_payload,
                {
                    "email": email,
                    "hosted_created": hosted_created,
                    "email_users": email_users,
                },
            )
        except (
            DataError,
            HostedUserEnsureError,
            IntegrityError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            messages.error(
                request,
                gettext("Could not add customer user: %(error)s") % {"error": error},
            )
            return self.get(request, *args, **kwargs)

        if added:
            messages.success(
                request,
                gettext("Added %(email)s to customer users.") % {"email": email},
            )
        else:
            messages.info(
                request,
                gettext("%(email)s is already a customer user.") % {"email": email},
            )
        return redirect(customer)

    def create_follow_up_interaction(
        self,
        customer: Customer,
        *,
        followup: CustomerFollowUp,
        summary: str,
        content: str,
    ) -> None:
        customer.interaction_set.create(
            origin=Interaction.Origin.MANUAL_NOTE,
            summary=summary[:200],
            content=content,
            details={
                "follow_up_id": followup.pk,
                "follow_up_at": followup.follow_up_at.isoformat(),
                "follow_up_note": followup.note,
                "follow_up_type": followup.get_type_display(),
            },
            user=self.request.user,
        )

    def set_follow_up(self, request, customer: Customer, *args, **kwargs):
        self.check_change_customer_permission(request)
        form = CustomerFollowUpForm(request.POST, customer=customer)
        if not form.is_valid():
            show_form_errors(request, form)
            return self.get(request, *args, **kwargs)

        followup = form.save()
        local_follow_up_at = timezone.localtime(followup.follow_up_at)
        content = gettext("Follow-up scheduled for %(date)s.") % {
            "date": local_follow_up_at,
        }
        if followup.note:
            content = f"{content}\n{followup.note}"
        self.create_follow_up_interaction(
            customer,
            followup=followup,
            summary=gettext("Follow-up set"),
            content=content,
        )
        messages.success(request, gettext("Follow-up added."))
        return redirect(customer)

    def clear_follow_up(self, request, customer: Customer):
        self.check_change_customer_permission(request)
        followup = get_object_or_404(
            CustomerFollowUp, customer=customer, pk=request.POST.get("follow_up")
        )
        self.create_follow_up_interaction(
            customer,
            followup=followup,
            summary=gettext("Follow-up cleared"),
            content=gettext("Follow-up cleared."),
        )
        followup.delete()
        messages.success(request, gettext("Follow-up cleared."))
        return redirect(customer)

    def add_manual_note(self, request, customer: Customer, *args, **kwargs):
        manual_form = ManualInteractionForm(request.POST)
        if manual_form.is_valid():
            note = manual_form.cleaned_data["note"].strip()
            customer.interaction_set.create(
                origin=Interaction.Origin.MANUAL_NOTE,
                summary=note.splitlines()[0][:200],
                content=note,
                user=request.user,
            )
            return redirect(self.get_tab_url(customer, "interactions"))
        return self.get(request, *args, **kwargs)

    def create_new_subscription(self, request, customer: Customer, *args, **kwargs):
        subscription_form = NewSubscriptionForm(request.POST)
        if subscription_form.is_valid():
            with override("en"):
                invoice = Subscription.new_subscription_invoice(
                    kind=subscription_form.cleaned_data["kind"],
                    customer=customer,
                    package=subscription_form.cleaned_data["package"],
                    currency=subscription_form.cleaned_data["currency"],
                    customer_reference=subscription_form.cleaned_data[
                        "customer_reference"
                    ],
                    customer_note=subscription_form.cleaned_data["customer_note"],
                    skip_intro=subscription_form.cleaned_data.get("skip_intro", False),
                )
            return redirect(invoice)

        return self.get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        customer = self.get_object()
        if "add_customer_user" in request.POST:
            return self.add_customer_user(request, customer, *args, **kwargs)

        if "set_follow_up" in request.POST:
            return self.set_follow_up(request, customer, *args, **kwargs)

        if "clear_follow_up" in request.POST:
            return self.clear_follow_up(request, customer)

        if "add_manual_note" in request.POST:
            return self.add_manual_note(request, customer, *args, **kwargs)

        return self.create_new_subscription(request, customer, *args, **kwargs)


class CustomerMergeView(CustomerDetailView):
    template_name = "payments/customer_merge.html"

    merge: Customer
    merge_form: CustomerMergeForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)  # type:ignore[misc]
        context["merge"] = self.merge
        context["merge_form"] = self.merge_form
        return context

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = CustomerMergeForm(request.GET, customer=self.object)
        if not form.is_valid():
            show_form_errors(request, form)
            return redirect(self.object)
        self.merge_form = CustomerMergeForm(
            initial={"merge": form.cleaned_data["merge"].pk},
            customer=self.object,
            hidden=True,
        )
        self.merge = form.cleaned_data["merge"]
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        customer = self.get_object()
        form = CustomerMergeForm(request.POST, customer=customer)
        if not form.is_valid():
            show_form_errors(request, form)
            return redirect(customer)
        merge = form.cleaned_data["merge"]
        merge.merge(customer, user=self.request.user)
        return redirect(merge)


class InteractionDetailView(CRMMixin, DetailView[Interaction]):  # type: ignore[misc]
    model = Interaction
    permission = "payments.view_customer"
    template_name = "crm/interaction_detail.html"
    title = "Interaction detail"

    def get_title(self) -> str:
        return self.object.summary


class InteractionDownloadView(InteractionDetailView):
    def render_to_response(self, context, **response_kwargs):
        if not self.object.attachment or not self.object.attachment.name:
            raise Http404("No attachment")
        return FileResponse(
            self.object.attachment.open(),
            as_attachment=True,
            filename=self.object.attachment_filename,
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
    PIE_CHART_WIDTH = 400
    PIE_CHART_HEIGHT = 400
    PIE_CHART_MARGIN = 6
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

        width = self.PIE_CHART_WIDTH
        height = self.PIE_CHART_HEIGHT
        radius = min(width, height) / 2 - self.PIE_CHART_MARGIN
        center_x = width / 2
        center_y = height / 2

        total = sum(data.values())

        # Count non-zero categories
        non_zero_items = [(cat, val) for cat, val in data.items() if val > 0]
        non_zero_categories = [cat for cat, _val in non_zero_items]

        svg_parts = [
            (
                f'<svg viewBox="0 0 {width} {height}" '
                f'width="{width}" height="{height}" '
                'xmlns="http://www.w3.org/2000/svg" class="pie-chart">'
            ),
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
            self._append_pie_label(
                svg_parts,
                category.label,
                f"€{value:,.0f} (100%)",
                center_x,
                center_y,
            )
        else:
            # Draw pie slices for multiple categories
            start_angle: float = 0
            label_positions: list[tuple[str, str, float, float]] = []
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

                label_angle = math.radians(start_angle + angle / 2 - 90)
                label_radius = radius * 0.58
                label_positions.append(
                    (
                        category.label,
                        f"€{value:,.0f} ({value / total * 100:.0f}%)",
                        center_x + label_radius * math.cos(label_angle),
                        center_y + label_radius * math.sin(label_angle),
                    )
                )
                start_angle = end_angle

            for label, value_label, label_x, label_y in label_positions:
                self._append_pie_label(svg_parts, label, value_label, label_x, label_y)

        svg_parts.append("</svg>")
        return "".join(svg_parts)

    def _append_pie_label(
        self,
        svg_parts: list[str],
        label: str,
        value_label: str,
        x: float,
        y: float,
    ) -> None:
        label_parts = label.split(" / ", 1)
        line_height = 12
        first_y = y - line_height * (len(label_parts) / 2)

        svg_parts.append(
            f'<text class="pie-chart-label" x="{x}" y="{first_y}" '
            'text-anchor="middle" font-size="10" font-weight="700" '
            'fill="#fff" stroke="#253342" stroke-width="1.2" '
            'stroke-opacity=".6" paint-order="stroke">'
        )
        for idx, label_part in enumerate(label_parts):
            svg_parts.append(
                f'<tspan x="{x}" dy="{line_height if idx else 0}">{label_part}</tspan>'
            )
        svg_parts.append(
            f'<tspan x="{x}" dy="{line_height}" font-size="9">{value_label}</tspan>'
        )
        svg_parts.append("</text>")

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
            bar_keys = [str(day) for day in range(1, num_bars + 1)]
            bar_labels = bar_keys
            label_prefix = "Day"
            chart_class = "stacked-bar-chart daily-income-chart"
        else:
            # Yearly view: show monthly bars
            num_bars = 12
            bar_keys = [f"{month_number:02d}" for month_number in range(1, 13)]
            bar_labels = [str(month_number) for month_number in range(1, 13)]
            label_prefix = ""
            chart_class = "stacked-bar-chart monthly-income-chart"

        # Get max value for scaling
        max_value = max(monthly_data.values()) if monthly_data.values() else Decimal(1)
        if max_value <= 0:
            max_value = self.MIN_CHART_VALUE

        svg_parts = [
            (
                f'<svg viewBox="0 0 {width} {height}" '
                f'width="{width}" height="{height}" '
                f'xmlns="http://www.w3.org/2000/svg" class="{chart_class}">'
            ),
        ]

        # Calculate bar properties
        bar_slot = chart_width / num_bars
        bar_width = bar_slot * (0.72 if month else 0.62)

        # Draw each bar
        for idx in range(num_bars):
            bar_key = bar_keys[idx]
            bar_label = bar_labels[idx]
            x: float = padding + bar_slot * idx + (bar_slot - bar_width) / 2
            category_totals = period_category_data.get(bar_key, {})

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

    def _count_months_inclusive(self, start_month: date, end_month: date) -> int:
        return (
            (end_month.year - start_month.year) * 12
            + end_month.month
            - start_month.month
            + 1
        )

    def _get_month_totals_in_range(
        self, start_month: date | None, end_month: date
    ) -> tuple[dict[date, Decimal], date | None]:
        if start_month is None or start_month > end_month:
            return {}, None

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

        earliest_month: date | None = None
        for row in rows:
            month_start = cast("date", row["period"])
            if earliest_month is None or month_start < earliest_month:
                earliest_month = month_start
            month_totals[month_start] += cast("Decimal", row["total_no_vat"])
        return month_totals, earliest_month

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

    def _get_last_complete_month_start(self) -> date | None:
        return self._shift_month(self._get_current_month_start(), -1)

    def _build_rolling_window_summary(
        self,
        month_totals: dict[date, Decimal],
        period_start: date,
        earliest_month: date | None,
    ) -> dict[str, int | Decimal | bool] | None:
        if earliest_month is None or earliest_month > period_start:
            return None

        available_months = self._count_months_inclusive(earliest_month, period_start)
        if available_months < 2:
            return None

        window_months = 12 if available_months >= 24 else available_months // 2
        rolling_start = self._shift_month(period_start, -(window_months - 1))
        if rolling_start is None:
            return None

        previous_end = self._shift_month(rolling_start, -1)
        previous_start = self._shift_month(rolling_start, -window_months)
        if previous_start is None or previous_end is None:
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
            "window_months": window_months,
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
        if period_start >= self._get_current_month_start():
            return None
        lookback_start = self._shift_month(period_start, -23)
        month_totals, earliest_month = self._get_month_totals_in_range(
            lookback_start, period_start
        )
        return self._build_rolling_window_summary(
            month_totals, period_start, earliest_month
        )

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

        last_complete_month = self._get_last_complete_month_start()
        if last_complete_month is None or first_month > last_complete_month:
            return rows

        trend_end_month = min(last_month, last_complete_month)
        month_totals, earliest_month = self._get_month_totals_in_range(
            self._shift_month(first_month, -23),
            trend_end_month,
        )
        for row in rows:
            period_start = self._get_month_start(year, cast("int", row["month_number"]))
            if period_start > trend_end_month:
                continue

            rolling_summary = self._build_rolling_window_summary(
                month_totals, period_start, earliest_month
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
        if year < current_year:
            trend_month = 12
        elif year > current_year:
            return None
        else:
            last_complete_month = self._get_last_complete_month_start()
            if last_complete_month is None or last_complete_month.year != year:
                return None
            trend_month = last_complete_month.month

        if not 1 <= trend_month <= len(monthly_breakdown_rows):
            return None

        trend_row = monthly_breakdown_rows[trend_month - 1]
        if not cast("bool", trend_row["trend_available"]):
            return None

        return {
            "period_year": cast("int", trend_row["period_year"]),
            "period_month": cast("int", trend_row["period_month"]),
            "window_months": cast("int", trend_row["window_months"]),
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
