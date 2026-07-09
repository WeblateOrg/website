#
# Copyright © Michal Čihař <michal@weblate.org>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from __future__ import annotations

import json
import random
import re
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import django.views.defaults
import sentry_sdk
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.exceptions import BadRequest, SuspiciousOperation, ValidationError
from django.core.mail import mail_admins
from django.core.signing import BadSignature, SignatureExpired, loads
from django.db import DataError, IntegrityError, connection, transaction
from django.db.models import Q
from django.http import (
    FileResponse,
    Http404,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext, override
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, TemplateView
from django.views.generic.dates import ArchiveIndexView
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.edit import CreateView, FormView, UpdateView

from weblate_web.forms import (
    AddDiscoveryForm,
    AgreementForm,
    DiscoveryRegistrationForm,
    DonateForm,
    EditDiscoveryForm,
    EditImageForm,
    EditLinkForm,
    EditNameForm,
    MethodForm,
    get_discovery_callback_url,
)
from weblate_web.invoices.models import InvoiceKind
from weblate_web.legal.models import Agreement, AgreementKind
from weblate_web.models import (
    REWARD_LEVELS,
    TOPIC_DICT,
    DiscoveryActivation,
    Package,
    PackageCategory,
    Post,
    Project,
    Service,
    Subscription,
    UnprocessablePaymentError,
    add_subscription_past_payments,
    get_donation_package_verbose,
    get_donation_reward_package_names,
    is_pending_discovery_activation,
    normalize_site_url_for_lock,
    process_donation,
    process_subscription,
)
from weblate_web.payments.backends import (
    PaymentError,
    get_backend,
    list_backends,
)
from weblate_web.payments.forms import CustomerForm
from weblate_web.payments.models import Customer, Payment
from weblate_web.payments.validators import cache_vies_data
from weblate_web.remote import get_activity
from weblate_web.saml import sync_saml_payload
from weblate_web.schema import get_blog_post_schema
from weblate_web.utils import (
    AUTO_ORIGIN,
    FOSDEM_ORIGIN,
    PAYMENTS_ORIGIN,
    show_form_errors,
)

from .const import FOSDEM_DONATION_DESCRIPTION

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django import forms
    from django.core.paginator import Page
    from django.http import HttpRequest, HttpResponse

    from weblate_web.payments.backends import Backend
    from weblate_web.utils import AuthenticatedHttpRequest

ON_EACH_SIDE = 3
ON_ENDS = 2
DOT = "."
USER_AGENT_RE = re.compile(r"Weblate/([0-9.]{3,9})")


def get_page_range(page_obj: Page) -> list[int | str]:
    paginator = page_obj.paginator
    page_num = page_obj.number - 1
    num_pages = paginator.num_pages

    page_range: Iterable[int | str]

    # If there are 10 or fewer pages, display links to every page.
    # Otherwise, do some fancy
    if num_pages <= 10:
        page_range = range(num_pages)
    else:
        # Insert "smart" pagination links, so that there are always ON_ENDS
        # links at either end of the list of pages, and there are always
        # ON_EACH_SIDE links at either end of the "current page" link.
        page_range = []
        if page_num > (ON_EACH_SIDE + ON_ENDS):
            page_range += [
                *range(ON_ENDS),
                DOT,
                *range(page_num - ON_EACH_SIDE, page_num + 1),
            ]
        else:
            page_range.extend(range(page_num + 1))
        if page_num < (num_pages - ON_EACH_SIDE - ON_ENDS - 1):
            page_range += [
                *range(page_num + 1, page_num + ON_EACH_SIDE + 1),
                DOT,
                *range(num_pages - ON_ENDS, num_pages),
            ]
        else:
            page_range.extend(range(page_num + 1, num_pages))
    return [page + 1 if isinstance(page, int) else page for page in page_range]


def get_customer(
    request: AuthenticatedHttpRequest, obj: Service | None = None
) -> Customer:
    # Get from service objects
    if obj and obj.pk and obj.customer:
        return obj.customer

    # Use existing customer for user
    customers = Customer.objects.for_user(request.user)
    if len(customers) == 1:
        return customers[0]

    # Create new customer object for an user
    customer = Customer.objects.get_or_create(
        origin=PAYMENTS_ORIGIN,
        user_id=request.user.id,
        defaults={"email": request.user.email},
    )[0]
    customer.users.add(request.user)
    return customer


def prepare_service_for_render(service: Service) -> Service:
    service.is_pending_discovery_activation = is_pending_discovery_activation(service)
    return service


def get_support_payload(service: Service, *, in_limits: bool) -> dict[str, object]:
    return {
        "name": service.status,
        "package": service.current_subscription.package.verbose
        if service.current_subscription
        else "",
        "expiry": service.expires,
        "backup_repository": service.backup_repository,
        "in_limits": in_limits,
        "limits": service.get_limits(),
        "has_subscription": service.latest_subscription is not None,
    }


@require_POST
@csrf_exempt
def api_user(request: HttpRequest) -> JsonResponse:
    try:
        payload = loads(
            request.POST.get("payload", ""),
            key=settings.PAYMENT_SECRET,
            max_age=300,
            salt="weblate.user",
        )
    except (BadSignature, SignatureExpired) as error:
        sentry_sdk.capture_exception()
        raise BadRequest("Invalid signature") from error
    if not isinstance(payload, dict):
        raise BadRequest("Invalid user payload")

    try:
        user, created = sync_saml_payload(payload)
    except (
        KeyError,
        TypeError,
        ValueError,
        DataError,
        IntegrityError,
    ) as error:
        sentry_sdk.capture_exception()
        raise BadRequest("Invalid user payload") from error

    if user is None:
        raise BadRequest("User could not be synchronized")
    return JsonResponse({"status": f"User {'created' if created else 'updated'}"})


def extract_weblate_version(request: HttpRequest) -> str:
    user_agent = request.headers.get("User-Agent", "")
    match = USER_AGENT_RE.match(user_agent)
    if match:
        return match[1]
    raise BadRequest("Invalid User-Agent")


@require_POST
@csrf_exempt
def api_hosted(request: HttpRequest) -> JsonResponse:
    try:
        payload = loads(
            request.POST.get("payload", ""),
            key=settings.PAYMENT_SECRET,
            max_age=300,
            salt="weblate.hosted",
        )
    except (BadSignature, SignatureExpired) as error:
        sentry_sdk.capture_exception()
        raise BadRequest("Invalid signature") from error

    billing_id: int = payload["billing"]

    # TODO: This is temporary hack for payments migration period
    payments = [
        payment
        for payment in Payment.objects.order_by("end").iterator()
        if payment.extra.get("billing", -1) == billing_id
    ]

    # Get/create service for this billing
    try:
        service = Service.objects.get(hosted_billing=billing_id)
    except Service.DoesNotExist:
        if payments:
            customer = payments[0].customer
        else:
            # TODO: It has to be existing, verify
            customer = Customer.objects.create(user_id=-1)
        service = Service.objects.create(customer=customer, hosted_billing=billing_id)

    if payments:
        package = Package.objects.get(name=payload["package"])
        expires = (
            timezone.make_aware(datetime.combine(payments[-1].end, time.max))
            if payments[-1].end
            else timezone.now()
        )
        # Create/update subscription
        subscription = service.subscription_set.get_or_create(
            defaults={
                "payment": payments[-1],
                "package": package,
                "expires": expires,
            }
        )[0]
        if subscription.package_id != package.pk:
            subscription.package = package
            subscription.save(update_fields=["package"])
        if subscription.payment_id and subscription.payment_id != payments[-1].pk:
            # Include current payment in past payments
            add_subscription_past_payments(subscription, subscription.payment_obj)

            # Update current subscription payment
            subscription.payment = payments[-1]
            subscription.expires = expires
            subscription.save(update_fields=["payment", "expires"])

        # Link past payments
        add_subscription_past_payments(subscription, *payments[:-1])

    # Link users which are supposed to have access
    for user in payload["users"]:
        service.customer.users.add(User.objects.get_or_create(username=user)[0])

    # Collect stats
    report = service.report_set.create(
        site_url="https://hosted.weblate.org/",
        site_title="Hosted Weblate",
        projects=payload["projects"],
        components=payload["components"],
        languages=payload["languages"],
        source_strings=payload["source_strings"],
        hosted_words=payload["words"],
        hosted_strings=payload.get("strings", 0),
        version=extract_weblate_version(request),
    )
    service.update_status()
    return JsonResponse(
        data={
            "name": service.status,
            "expiry": service.expires,
            "backup_repository": service.backup_repository,
            "in_limits": report.is_valid_site_url() and service.check_in_limits(),
            "limits": service.get_limits(),
        }
    )


@require_POST
@csrf_exempt
def api_support(request: HttpRequest) -> JsonResponse:
    service = get_object_or_404(Service, secret=request.POST.get("secret", ""))
    report = service.report_set.create(
        site_url=request.POST.get("site_url", ""),
        site_title=request.POST.get("site_title", ""),
        ssh_key=request.POST.get("ssh_key", ""),
        users=request.POST.get("users", 0),
        projects=request.POST.get("projects", 0),
        components=request.POST.get("components", 0),
        languages=request.POST.get("languages", 0),
        source_strings=request.POST.get("source_strings", 0),
        hosted_words=request.POST.get("words", 0),
        hosted_strings=request.POST.get("strings", 0),
        version=extract_weblate_version(request),
        discoverable=bool(request.POST.get("discoverable")),
    )
    is_valid_site_url = report.is_valid_site_url()
    service.update_status()
    service.create_backup()
    if is_valid_site_url and "public_projects" in request.POST:
        current_projects = set(service.project_set.values_list("name", "url", "web"))
        for project in json.loads(request.POST["public_projects"]):
            # Skip unexpected data
            if set(project) != {"name", "web", "url"}:
                continue
            item = (project["name"], project["url"], project["web"])
            # Non-changed project
            if item in current_projects:
                current_projects.remove(item)
                continue
            # New project
            service.project_set.create(**project)
        # Remove stale projects
        for name, url, web in current_projects:
            service.project_set.filter(name=name, url=url, web=web).delete()

    return JsonResponse(
        data=get_support_payload(
            service, in_limits=is_valid_site_url and service.check_in_limits()
        )
    )


@require_POST
@csrf_exempt
def api_support_activation(request: HttpRequest) -> JsonResponse:
    code = request.POST.get("code", "")
    if not code:
        raise Http404
    try:
        activation = DiscoveryActivation.exchange(code)
    except DiscoveryActivation.DoesNotExist as error:
        raise Http404 from error
    except ValidationError as error:
        return JsonResponse({"error": error.messages}, status=400)

    service = activation.service
    payload = get_support_payload(service, in_limits=service.check_in_limits())
    payload["secret"] = service.secret
    return JsonResponse(data=payload)


@require_POST
@login_required
def fetch_vat(request: AuthenticatedHttpRequest) -> JsonResponse:
    if "vat" not in request.POST:
        raise SuspiciousOperation("Missing needed parameters")
    _vatin, vies_data = cache_vies_data(request.POST["vat"])
    return JsonResponse(data=vies_data)


class PaymentView(FormView, SingleObjectMixin):
    model = Payment
    form_class: type[forms.BaseForm] = MethodForm
    template_name = "payment/payment.html"
    check_customer = True

    def redirect_origin(self) -> HttpResponse:
        if self.object.customer.origin in {PAYMENTS_ORIGIN, AUTO_ORIGIN}:
            return HttpResponseRedirect(
                f"{reverse('donate-process')}?payment={self.object.pk}"
            )
        if self.object.customer.origin == FOSDEM_ORIGIN:
            self.object.state = Payment.PROCESSED
            self.object.save(update_fields=["state"])
            messages.info(
                self.request,
                gettext("Thank you for your donation and enjoy FOSDEM."),
            )
            return HttpResponseRedirect(FOSDEM_ORIGIN)
        return HttpResponseRedirect(
            f"{self.object.customer.origin}?payment={self.object.pk}"
        )

    def is_draft_invoice_paid(self) -> bool:
        draft_invoice = self.object.draft_invoice
        return draft_invoice is not None and draft_invoice.is_paid

    def redirect_paid_draft_invoice(self) -> HttpResponse:
        messages.info(
            self.request,
            gettext(
                "This invoice has already been paid. Please sign in to view details."
            ),
        )
        return redirect("home")

    def get_context_data(self, **kwargs):
        kwargs = super().get_context_data(**kwargs)
        kwargs["can_pay"] = self.can_pay
        kwargs["backends"] = [
            backend(self.object)
            for backend in list_backends(
                exclude_names=self.object.extra.get("exclude_backends"),
                currency=self.object.get_currency_display(),
            )
        ]
        return kwargs

    def validate_customer(self, customer: Customer) -> HttpResponse | None:
        if not self.check_customer:
            return None
        if customer.is_empty:
            if self.object.customer.origin == FOSDEM_ORIGIN:
                messages.info(
                    self.request,
                    gettext(
                        "Please provide your name to complete the payment. Include other billing information if you need it on the receipt."
                    ),
                )
            else:
                messages.info(
                    self.request,
                    gettext(
                        "Please provide your billing information to complete the payment."
                    ),
                )
            return redirect("payment-customer", pk=self.object.pk)
        # This should not happen, but apparently validation service is
        # often broken, so all repeating payments without a validation
        if customer.vat and not self.object.repeat:
            try:
                customer.prepayment_validation()
            except ValidationError:
                messages.warning(
                    self.request,
                    gettext("The VAT ID is no longer valid, please update it."),
                )
                return redirect("payment-customer", pk=self.object.pk)
        return None

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        with transaction.atomic():
            self.object = self.get_object()
            if self.object.state == Payment.NEW and self.is_draft_invoice_paid():
                return self.redirect_paid_draft_invoice()
            customer = self.object.customer
            self.can_pay = not customer.is_empty
            result = self.validate_customer(customer)
            if result is not None:
                return result
            return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form):
        if self.form_class == MethodForm:
            messages.error(self.request, gettext("Please choose a payment method."))
        else:
            messages.error(
                self.request,
                gettext(
                    "Please provide your billing information to complete the payment."
                ),
            )
        return super().form_invalid(form)

    def get(self, request, *args, **kwargs) -> HttpResponse:
        if self.object.state != Payment.NEW:
            return redirect(self.object.get_complete_url())
        return super().get(request, *args, **kwargs)

    def form_valid(self, form):
        if not self.can_pay:
            return redirect("payment", pk=self.object.pk)
        # Actually call the payment backend
        method = form.cleaned_data["method"]
        backend = get_backend(method)(self.object)
        # Use backend payment here because it is selected again for update
        if backend.payment.state != Payment.NEW:
            return redirect(self.object.get_complete_url())
        try:
            result = backend.initiate(
                self.request,
                self.object.get_payment_url(),
                self.object.get_complete_url(),
            )
        except PaymentError as error:
            sentry_sdk.capture_exception()
            messages.error(
                self.request, gettext("Could not perform payment: %s") % error
            )
            return super().form_invalid(form)
        if result is not None:
            return result
        try:
            backend.complete(self.request)
        except PaymentError as error:
            sentry_sdk.capture_exception()
            messages.error(
                self.request, gettext("Could not complete payment: %s") % error
            )
            return super().form_invalid(form)
        return self.redirect_origin()


class CustomerView(PaymentView):
    form_class: type[forms.BaseForm] = CustomerForm
    template_name = "payment/customer.html"
    check_customer = False

    def form_valid(self, form):
        form.save()
        return redirect("payment", pk=self.object.pk)

    def get_form_kwargs(self):
        """Return the keyword arguments for instantiating the form."""
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.object.customer
        return kwargs


class CustomerBaseView(DetailView):
    model = Customer
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        return super().get_queryset().for_user(self.request.user)  # type: ignore[attr-defined]


@method_decorator(login_required, name="dispatch")
class EditCustomerView(CustomerBaseView, UpdateView):  # type: ignore[misc]
    # Unlike CustomerView, this is not bound to payment (allows editing all
    # user customer contacts) and returns to /user
    template_name = "payment/customer.html"
    success_url = "/user/"
    form_class = CustomerForm


@method_decorator(login_required, name="dispatch")
class CustomerDPAView(CustomerBaseView, FormView):
    template_name = "payment/customer-agreement.html"
    form_class = AgreementForm

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super().post(request, *args, **kwargs)

    @transaction.atomic
    def form_valid(self, form):
        customer = self.object
        if customer.agreement_set.filter(
            kind=AgreementKind.DPA, signed__gte=timezone.now() - timedelta(days=30)
        ).exists():
            messages.error(
                self.request,
                gettext(
                    "Please use already generated agreement, you can generate new one after 30 days."
                ),
            )
        else:
            customer.agreement_set.create(kind=AgreementKind.DPA)
            messages.info(
                self.request,
                gettext("New agreement created, you can download it below."),
            )
        return redirect("customer-agreement", pk=customer.pk)


@login_required
def agreement_download_view(request: AuthenticatedHttpRequest, pk: int):
    if request.user.is_staff:
        agreement = get_object_or_404(Agreement, pk=pk)
    else:
        agreement = get_object_or_404(
            Agreement, pk=pk, customer__in=Customer.objects.for_user(request.user)
        )
    return FileResponse(
        agreement.path.open("rb"),
        as_attachment=True,
        filename=agreement.filename,
        content_type="application/pdf",
    )


class CompleteView(PaymentView):
    def dispatch(self, request, *args, **kwargs):
        with transaction.atomic():
            self.object = self.get_object()

            # User should choose method for new payment
            if self.object.state == Payment.NEW:
                if self.is_draft_invoice_paid():
                    return self.redirect_paid_draft_invoice()
                return redirect("payment", pk=self.object.pk)

            # Get backend and refetch payment from the database
            try:
                backend: Backend = get_backend(self.object.backend)(self.object)
            except KeyError as error:
                raise Http404("Non-existing backend") from error

            # Make sure we use the processed object
            self.object = backend.payment

            # Allow reprocessing of rejected payments. User might choose
            # to retry in the payment gateway and previously rejected payment
            # can be now completed.
            if backend.payment.state not in {Payment.PENDING, Payment.REJECTED}:
                return self.redirect_origin()

            backend.complete(self.request)
            # If payment is still pending, display info page
            if backend.payment.state == Payment.PENDING:
                if self.is_draft_invoice_paid():
                    return self.redirect_paid_draft_invoice()
                if backend.name == "fio-bank":
                    messages.info(
                        request,
                        gettext(
                            "New company, new bank details! Pay with attention to the current invoice."
                        ),
                    )
                return render(
                    request,
                    "payment/pending.html",
                    {"object": backend.payment, "backend": backend},
                )
            return self.redirect_origin()


@method_decorator(login_required, name="dispatch")
class DonateView(FormView):
    form_class = DonateForm
    template_name = "donate/form.html"
    request: AuthenticatedHttpRequest

    def get_form_kwargs(self):
        result = super().get_form_kwargs()
        result["initial"] = self.request.GET
        return result

    def get_context_data(self, **kwargs):
        result = super().get_context_data(**kwargs)
        result["reward_levels"] = REWARD_LEVELS
        return result

    def form_invalid(self, form):
        show_form_errors(self.request, form)
        return super().form_invalid(form)

    def form_valid(self, form):
        data = form.cleaned_data
        with override("en"):
            description = get_donation_package_verbose(int(data["reward"] or "0"))
        payment = Payment.objects.create(
            amount=data["amount"],
            amount_fixed=True,
            description=description,
            recurring=data["recurring"],
            extra={"reward": data["reward"], "category": "donate"},
            customer=get_customer(self.request),
        )
        return redirect(payment.get_payment_url())


@login_required
def process_payment(request):
    try:
        payment = Payment.objects.get(
            pk=request.GET["payment"],
            customer__origin=PAYMENTS_ORIGIN,
            customer__users=request.user,
        )
    except (KeyError, Payment.DoesNotExist):
        return redirect(reverse("user"))

    # Create donation
    if payment.state in {Payment.NEW, Payment.PENDING}:
        messages.error(request, gettext("Payment not yet processed, please retry."))
    elif payment.state == Payment.REJECTED:
        messages.error(
            request,
            gettext("The payment was rejected: {}").format(
                payment.details.get("reject_reason", gettext("Unknown reason"))
            ),
        )
    elif payment.state == Payment.ACCEPTED:
        if "subscription" in payment.extra or "subscription_upgrade" in payment.extra:
            try:
                process_subscription(payment)
            except UnprocessablePaymentError as error:
                sentry_sdk.capture_exception(error)
                messages.error(
                    request,
                    gettext(
                        "Payment was processed, but the subscription could not be "
                        "updated. Please contact us."
                    ),
                )
            else:
                messages.success(request, gettext("Thank you for your subscription."))
        else:
            messages.success(
                request,
                # Translators: This is shown after a successful donation. You can use
                # a culturally natural, poetic compliment instead of translating this
                # literally; keep it short and use original or public-domain wording.
                gettext("You are the heart of Weblate. Thank you for your donation."),
            )
            donation = process_donation(payment)
            subscription = donation.donation_subscription
            if subscription is not None and subscription.package.donation_reward:
                return redirect(donation)

    return redirect(reverse("user"))


def can_download_payment_invoice(
    request: AuthenticatedHttpRequest, payment: Payment
) -> bool:
    user = request.user
    if payment.customer.users.filter(pk=user.pk).exists():
        return True

    if (
        payment.customer.origin == PAYMENTS_ORIGIN
        and payment.customer.user_id == user.id
    ):
        return True

    return Service.objects.filter(
        Q(customer__users=user)
        & (Q(subscription__payment=payment) | Q(subscription__past_payments=payment))
    ).exists()


@login_required
def download_payment_invoice(request: AuthenticatedHttpRequest, pk):
    payment = get_object_or_404(
        Payment.objects.select_related("customer", "draft_invoice", "paid_invoice"),
        pk=pk,
    )

    if not can_download_payment_invoice(request, payment):
        raise Http404("Invoice not accessible to current user!")

    # New invoice model
    if payment.paid_invoice:
        if "receipt" in request.GET:
            if not payment.paid_invoice.is_paid:
                raise Http404("Receipt not available")
            try:
                return FileResponse(
                    payment.paid_invoice.receipt_path.open("rb"),
                    as_attachment=True,
                    filename=payment.paid_invoice.receipt_filename,
                    content_type="application/pdf",
                )
            except (OSError, ValueError) as error:
                raise Http404("Receipt not available") from error
        return FileResponse(
            payment.paid_invoice.path.open("rb"),
            as_attachment=True,
            filename=payment.paid_invoice.filename,
            content_type="application/pdf",
        )
    if payment.draft_invoice:
        return FileResponse(
            payment.draft_invoice.path.open("rb"),
            as_attachment=True,
            filename=payment.draft_invoice.filename,
            content_type="application/pdf",
        )

    # Legacy payments storage
    if not payment.invoice_filename_valid:
        raise Http404(f"File {payment.invoice_filename} does not exist!")

    return FileResponse(
        open(payment.invoice_full_filename, "rb"),
        as_attachment=True,
        filename=payment.invoice,
        content_type="application/pdf",
    )


@require_POST
@login_required
def disable_repeat(request, pk):
    donation = get_object_or_404(
        Service.objects.donations(),
        pk=pk,
        customer__users=request.user,
    )
    subscription = donation.donation_subscription
    if subscription is None:
        raise Http404("Nothing to disable")
    payment = subscription.payment_obj
    if payment is None:
        raise Http404("Nothing to disable")
    payment.recurring = ""
    payment.save()
    return redirect(reverse("user"))


@method_decorator(login_required, name="dispatch")
class EditLinkView(UpdateView):
    template_name = "donate/edit.html"
    success_url = "/user/"
    request: AuthenticatedHttpRequest

    def get_form_class(self):
        subscription = self.object.donation_subscription
        reward = 0 if subscription is None else subscription.package.donation_reward
        if reward == 2:
            return EditLinkForm
        if reward == 3:
            return EditImageForm
        return EditNameForm

    def get_queryset(self):
        return (
            Service.objects.donations()
            .filter(
                customer__users=self.request.user,
                subscription__package__name__in=get_donation_reward_package_names(),
            )
            .distinct()
        )

    def form_valid(self, form):
        """If the form is valid, save the associated model."""
        link_url = form.cleaned_data.get("donation_link_url", "N/A")
        link_text = form.cleaned_data.get("donation_link_text", "N/A")
        mail_admins(
            "Weblate: link changed",
            f"New link: {link_url}\nNew text: {link_text}\n",
        )
        return super().form_valid(form)


@method_decorator(login_required, name="dispatch")
class EditDiscoveryView(UpdateView):
    template_name = "subscription/discovery.html"
    success_url = "/user/"
    form_class = EditDiscoveryForm
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        return Service.objects.customer_services().filter(
            customer__users=self.request.user,
        )

    def form_valid(self, form):
        """If the form is valid, save the associated model."""
        discover_url = form.instance.site_url
        discover_text = form.cleaned_data.get("discover_text", "N/A")
        mail_admins(
            "Weblate: discovery description changed",
            f"Service link: {discover_url}\nNew text: {discover_text}\n",
        )
        return super().form_valid(form)


@method_decorator(login_required, name="dispatch")
class AddDiscoveryView(CreateView):
    template_name = "subscription/discovery-add.html"
    success_url = "/user/"
    form_class = AddDiscoveryForm
    request: AuthenticatedHttpRequest

    def form_valid(self, form):
        """If the form is valid, save the associated model."""
        instance = form.instance
        instance.customer = get_customer(self.request, instance)
        instance.site_url = instance.site_url.rstrip("/")
        instance.site_url_lock = True
        discover_url = instance.site_url
        discover_text = form.cleaned_data.get("discover_text", "N/A")
        mail_admins(
            "Weblate: discovery registered",
            f"Service link: {discover_url}\nNew text: {discover_text}\n",
        )
        result = super().form_valid(form)
        instance.customer.users.add(self.request.user)
        messages.info(
            self.request,
            gettext(
                "Activate the listing from your Weblate management page using the "
                "activation token shown below."
            ),
        )
        return result


@method_decorator(login_required, name="dispatch")
class DiscoveryRegistrationView(FormView):
    template_name = "subscription/discovery-register.html"
    form_class = DiscoveryRegistrationForm
    request: AuthenticatedHttpRequest

    def get_initial(self):
        initial = super().get_initial()
        initial.update(
            {
                "site_url": self.request.GET.get("site_url", ""),
                "state": self.request.GET.get("state", ""),
            }
        )
        return initial

    def form_valid(self, form):
        """Create the discovery service and return a one-time code."""
        instance = form.save(commit=False)
        instance.customer = get_customer(self.request, instance)
        instance.site_url = normalize_site_url_for_lock(instance.site_url)
        instance.site_url_lock = True
        discover_url = instance.site_url
        discover_text = form.cleaned_data.get("discover_text", "N/A")
        mail_admins(
            "Weblate: discovery registered",
            f"Service link: {discover_url}\nNew text: {discover_text}\n",
        )
        instance.save()
        form.save_m2m()
        instance.customer.users.add(self.request.user)
        activation = DiscoveryActivation.create_for_service(
            instance,
            state=form.cleaned_data["state"],
            callback_url=get_discovery_callback_url(instance.site_url),
        )
        query = urlencode({"code": activation.code, "state": activation.state})
        return redirect(f"{activation.callback_url}?{query}")


class NewsArchiveView(ArchiveIndexView):
    model = Post
    date_field = "timestamp"
    paginate_by = 10
    ordering = ("-timestamp",)

    def get_context_data(self, **kwargs):
        result = super().get_context_data(**kwargs)
        result["page_range"] = get_page_range(result["page_obj"])
        return result


class NewsView(NewsArchiveView):
    paginate_by = 5
    template_name = "news.html"


class TopicArchiveView(NewsArchiveView):
    def get_queryset(self):
        return super().get_queryset().filter(topic=self.kwargs["slug"])

    # pylint: disable=arguments-differ
    def get_context_data(self, **kwargs):
        result = super().get_context_data(**kwargs)
        result["topic"] = TOPIC_DICT[self.kwargs["slug"]]
        return result


class MilestoneArchiveView(NewsArchiveView):
    def get_queryset(self):
        return super().get_queryset().filter(milestone=True)

    # pylint: disable=arguments-differ
    def get_context_data(self, **kwargs):
        result = super().get_context_data(**kwargs)
        result["topic"] = gettext("Milestones")
        return result


class PostView(DetailView):
    model = Post

    def get_object(self, queryset=None):
        result = super().get_object(queryset)
        if not self.request.user.is_staff and result.timestamp >= timezone.now():
            raise Http404("Future entry")
        return result

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["related"] = (
            Post.objects.filter(topic=self.object.topic)
            .exclude(pk=self.object.pk)
            .order_by("-timestamp")[:3]
        )
        context["extra_schema_json_ld"] = [get_blog_post_schema(self.object)]
        return context


# pylint: disable=unused-argument
def not_found(request, exception=None):
    """Error handler showing list of available projects."""
    return render(request, "404.html", status=404)


def server_error(request):
    # pylint: disable=broad-except
    """Error handler for server errors."""
    try:
        return render(
            request,
            "500.html",
            {"sentry_event_id": sentry_sdk.last_event_id()},
            status=500,
        )
    except Exception:
        return django.views.defaults.server_error(request)


@cache_control(max_age=3600)
def activity_svg(request):
    bars = []
    opacities = {0: ".1", 1: ".3", 2: ".5", 3: ".7"}
    data = get_activity()
    top_count = max(data) if data else 0
    for i, count in enumerate(data):
        height = int(76 * count / top_count)
        item = {
            "rx": 2,
            "width": 6,
            "height": height,
            "id": f"b{i}",
            "x": 10 * i,
            "y": 86 - height,
        }
        if height < 20:
            item["fill"] = "#f6664c"
        elif height < 45:
            item["fill"] = "#38f"
        else:
            item["fill"] = "#2eccaa"
        if i in opacities:
            item["opacity"] = opacities[i]

        bars.append(item)

    return render(
        request,
        "svg/activity.svg",
        {"bars": bars},
        content_type="image/svg+xml; charset=utf-8",
    )


@require_POST
@login_required
def subscription_disable_repeat(request, pk):
    subscription = get_object_or_404(
        Subscription, pk=pk, service__customer__users=request.user
    )
    payment = subscription.payment_obj
    payment.recurring = ""
    payment.save()
    return redirect(reverse("user"))


@require_POST
@login_required
def service_token(request, pk):
    service = get_object_or_404(Service, pk=pk, customer__users=request.user)
    service.regenerate()
    return redirect(reverse("user"))


@require_POST
@login_required
def customer_user(request, pk):
    customer = get_object_or_404(Customer, pk=pk, users=request.user)
    try:
        user = User.objects.get(email__iexact=request.POST.get("email"))
    except User.DoesNotExist:
        messages.error(request, gettext("User not found!"))
    else:
        if "remove" in request.POST:
            customer.users.remove(user)
        else:
            customer.users.add(user)
    return redirect(reverse("user"))


@login_required
@user_passes_test(lambda u: u.is_superuser)
def subscription_view(request, pk):
    service = get_object_or_404(Service, pk=pk)
    prepare_service_for_render(service)
    return render(request, "service.html", {"service": service})


@require_POST
@login_required
def subscription_pay(request, pk):
    subscription = get_object_or_404(
        Subscription, pk=pk, service__customer__users=request.user
    )
    if "switch_yearly" in request.POST and subscription.yearly_package:
        subscription.package = subscription.yearly_package
        subscription.save(update_fields=["package"])
    with override("en"):
        invoice = subscription.create_invoice(kind=InvoiceKind.DRAFT)
        payment = invoice.create_payment(recurring=subscription.package.get_repeat())
    return redirect(payment.get_payment_url())


@require_POST
@login_required
def subscription_upgrade(request, pk):
    subscription = get_object_or_404(
        Subscription, pk=pk, service__customer__users=request.user
    )
    package = None
    if package_name := request.POST.get("package"):
        package = Package.objects.filter(name=package_name).first()
        if package is None:
            messages.error(
                request,
                gettext(
                    "This subscription can not be upgraded to the selected package."
                ),
            )
            return redirect("user")
    try:
        if not subscription.upgrade_requires_payment(package):
            subscription.upgrade_without_payment(package)
            return redirect("user")
    except ValueError:
        messages.error(
            request,
            gettext("This subscription can not be upgraded to the selected package."),
        )
        return redirect("user")
    with override("en"):
        try:
            invoice = subscription.create_upgrade_invoice(
                kind=InvoiceKind.DRAFT, package=package
            )
        except ValueError:
            messages.error(
                request,
                gettext(
                    "This subscription can not be upgraded to the selected package."
                ),
            )
            return redirect("user")
        payment = invoice.create_payment()
    return redirect(payment.get_payment_url())


@require_POST
@login_required
def donate_pay(request, pk):
    donation = get_object_or_404(
        Service.objects.donations(),
        pk=pk,
        customer__users=request.user,
    )
    subscription = donation.donation_subscription
    if subscription is None or subscription.payment is None:
        raise Http404("Nothing to pay")
    with override("en"):
        payment = Payment.objects.create(
            amount=donation.get_donation_amount(),
            description=donation.get_donation_payment_description(),
            recurring=subscription.payment_obj.recurring,
            extra={"donation_service": donation.pk, "category": "donate"},
            customer=get_customer(request, donation),
            amount_fixed=True,
        )
    return redirect(payment.get_payment_url())


@login_required
def subscription_new(request):
    plan = request.GET.get("plan")
    try:
        package = Package.objects.get(name=plan)
    except Package.DoesNotExist:
        return redirect("support")
    service: Service | None
    if "service" in request.GET:
        service = get_object_or_404(
            Service, pk=request.GET["service"], customer__users=request.user
        )
    else:
        service = None

    customer = get_customer(request, service)
    recurring = package.get_repeat()
    with override("en"):
        if service:
            for subscription in service.support_subscriptions:
                if subscription.can_upgrade_to(package):
                    if not subscription.upgrade_requires_payment(package):
                        messages.info(
                            request,
                            gettext(
                                "Please confirm the upgrade from your account page."
                            ),
                        )
                        return redirect("user")
                    invoice = subscription.create_upgrade_invoice(
                        kind=InvoiceKind.DRAFT, package=package
                    )
                    recurring = ""
                    break
            else:
                invoice = Subscription.new_subscription_invoice(
                    kind=InvoiceKind.DRAFT,
                    customer=customer,
                    package=package,
                    service=service,
                )
        else:
            invoice = Subscription.new_subscription_invoice(
                kind=InvoiceKind.DRAFT,
                customer=customer,
                package=package,
                service=service,
            )
        payment = invoice.create_payment(recurring=recurring)
    return redirect(payment.get_payment_url())


class DiscoverView(TemplateView):
    template_name = "discover.html"

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        services: Iterable[Service]
        projects: Iterable[Project]

        discoverable_services = Service.objects.customer_services().filter(
            discoverable=True
        )
        services = discoverable_services.prefetch_related("project_set")
        query = self.request.GET.get("q", "").strip().lower()
        if query:
            projects = Project.objects.filter(
                service__in=discoverable_services,
            ).prefetch_related("service")
            if connection.vendor == "mysql":
                projects = projects.filter(name__search=query.replace("*", ""))
            else:
                projects = projects.filter(name__icontains=query)
            services_dict: dict[int, Service] = {}
            for project in projects:
                service = services_dict[project.service_id] = project.service
                if not hasattr(service, "matched_projects"):
                    service.matched_projects = []
                service.matched_projects.append(project)

            services = list(services_dict.values())
        else:
            for service in services:
                projects = list(service.project_set.all())
                if len(projects) > 20:
                    projects = random.sample(projects, 20)
                service.matched_projects = projects
        for service in services:
            service.non_matched_projects_count = service.site_projects - len(
                service.matched_projects
            )

        data["discoverable_services"] = services
        data["query"] = query
        if self.request.user.is_authenticated:
            data["user_services"] = set(
                Service.objects.customer_services()
                .filter(
                    customer__users=self.request.user,
                )
                .values_list("pk", flat=True)
            )
        else:
            data["user_services"] = set()

        return data


@method_decorator(login_required, name="dispatch")
class UserView(TemplateView):
    template_name = "user.html"
    request: AuthenticatedHttpRequest

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        data["user_services"] = [
            prepare_service_for_render(service)
            for service in Service.objects.customer_services().filter(
                customer__users=self.request.user,
            )
        ]
        data["user_donations"] = Service.objects.donations().filter(
            customer__users=self.request.user,
        )
        return data


class HostingView(TemplateView):
    template_name = "hosting.html"

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)

        data["hosted_package"] = (
            Package.objects.filter(category=PackageCategory.PACKAGE_SHARED)
            .filter(name__regex="^hosted:[0-9.]+[km]$")
            .order_by("price")[0]
        )
        data["dedicated_package"] = (
            Package.objects.filter(category=PackageCategory.PACKAGE_DEDICATED)
            .filter(name__regex="^dedicated:[0-9.]+[km]$")
            .order_by("price")[0]
        )
        data["basic_support_package"] = Package.objects.get(name="basic")
        data["extended_support_package"] = Package.objects.get(name="extended")
        return data


class SupportView(TemplateView):
    template_name = "support.html"

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        for package in Package.objects.filter(
            Q(category=PackageCategory.PACKAGE_SUPPORT) | Q(name="install:linux")
        ):
            if package.name == "install:linux":
                data["install_package"] = package
            else:
                data[f"{package.name}_support_package"] = package
        return data


def fosdem_donation(request):
    # Validate and parse the amount parameter
    amount_str = request.GET.get("amount", "30")

    try:
        amount = int(amount_str)
    except (ValueError, TypeError) as error:
        raise BadRequest("Invalid amount parameter") from error

    # Validate amount is within reasonable bounds
    if amount < 5 or amount > 100:
        raise BadRequest("Invalid amount parameter")

    # Create customer (or use existing for authenticated users)
    if request.user.is_authenticated:
        customer = Customer.objects.get_or_create(
            origin=PAYMENTS_ORIGIN,
            user_id=request.user.id,
            defaults={"email": request.user.email},
        )[0]
    else:
        customer = Customer.objects.create(
            origin=FOSDEM_ORIGIN, user_id=-1, country="BE"
        )
    # Create payment
    payment = Payment.objects.create(
        customer=customer,
        description=FOSDEM_DONATION_DESCRIPTION,
        amount_fixed=True,
        amount=amount,
        extra={"category": "donate"},
    )
    # Redirect to payment
    return redirect(payment.get_absolute_url())
