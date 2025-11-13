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
from datetime import timedelta
from typing import TYPE_CHECKING

import django.views.defaults
import sentry_sdk
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.exceptions import BadRequest, SuspiciousOperation, ValidationError
from django.core.mail import mail_admins
from django.core.signing import BadSignature, SignatureExpired, loads
from django.db import connection, transaction
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
    DonateForm,
    EditDiscoveryForm,
    EditImageForm,
    EditLinkForm,
    EditNameForm,
    MethodForm,
)
from weblate_web.invoices.models import InvoiceKind
from weblate_web.legal.models import Agreement, AgreementKind
from weblate_web.models import (
    REWARD_LEVELS,
    TOPIC_DICT,
    Donation,
    Package,
    PackageCategory,
    Post,
    Project,
    Service,
    Subscription,
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
from weblate_web.payments.validators import cache_vies_data, validate_vatin
from weblate_web.remote import get_activity
from weblate_web.utils import (
    AUTO_ORIGIN,
    FOSDEM_ORIGIN,
    PAYMENTS_ORIGIN,
    show_form_errors,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django import forms
    from django.core.paginator import Page
    from django.http import (
        HttpRequest,
    )

    from weblate_web.payments.backends import (
        Backend,
    )
    from weblate_web.utils import (
        AuthenticatedHttpRequest,
    )

ON_EACH_SIDE = 3
ON_ENDS = 2
DOT = "."
USER_AGENT_RE = re.compile(r"Weblate/([0-9.]{3,6})")


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
    request: AuthenticatedHttpRequest, obj: Service | Donation | None = None
) -> Customer:
    # Get from Service / Donation objects
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

    try:
        user = User.objects.get(username=payload["username"])
    except User.DoesNotExist:
        User.objects.create(**payload["create"])
        return JsonResponse({"status": "User created"})

    # Cycle unused passwords to invalidate existing sessions
    if not user.has_usable_password():
        user.set_unusable_password()

    # Update attributes
    for key, value in payload.get("changes", {}).items():
        if key not in {"username", "email", "last_name"}:
            continue
        setattr(user, key, value)

    # Save to the database
    user.save()

    return JsonResponse({"status": "User updated"})


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
        # Create/update subscription
        subscription = service.subscription_set.get_or_create(
            defaults={
                "payment": payments[-1].pk,
                "package": payload["package"],
            }
        )[0]
        if subscription.package != payload["package"]:
            subscription.package = Package.objects.get(name=payload["package"])
            subscription.save(update_fields=["package"])
        if subscription.payment and subscription.payment != payments[-1].pk:
            # Include current payment in past payments
            subscription.pastpayments_set.get_or_create(
                payment=subscription.payment_obj
            )

            # Update current subscription payment
            subscription.payment = payments[-1].pk
            subscription.save(update_fields=["payment"])

        # Link past payments
        for payment in payments[:-1]:
            subscription.pastpayments_set.get_or_create(payment=payment.pk)

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
    service.report_set.create(
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
    service.update_status()
    service.create_backup()
    if "public_projects" in request.POST:
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
        data={
            "name": service.status,
            "package": service.current_subscription.package.verbose
            if service.current_subscription
            else "",
            "expiry": service.expires,
            "backup_repository": service.backup_repository,
            "in_limits": service.check_in_limits(),
            "limits": service.get_limits(),
            "has_subscription": service.latest_subscription is not None,
        }
    )


@require_POST
@login_required
def fetch_vat(request: AuthenticatedHttpRequest) -> JsonResponse:
    if "vat" not in request.POST:
        raise SuspiciousOperation("Missing needed parameters")
    vat = cache_vies_data(request.POST["vat"])
    return JsonResponse(data=getattr(vat, "vies_data", {"valid": False}))


class PaymentView(FormView, SingleObjectMixin):
    model = Payment
    form_class: type[forms.BaseForm] = MethodForm
    template_name = "payment/payment.html"
    check_customer = True

    def redirect_origin(self):
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

    def validate_customer(self, customer):
        if not self.check_customer:
            return None
        if customer.is_empty:
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
                validate_vatin(customer.vat)
            except ValidationError:
                messages.warning(
                    self.request,
                    gettext("The VAT ID is no longer valid, please update it."),
                )
                return redirect("payment-customer", pk=self.object.pk)
        return None

    def dispatch(self, request, *args, **kwargs):
        with transaction.atomic():
            self.object = self.get_object()
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

    def get(self, request, *args, **kwargs):
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
        tmp = Donation(reward=int(data["reward"] or "0"))
        with override("en"):
            description = tmp.get_payment_description()
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
        if "subscription" in payment.extra:
            messages.success(request, gettext("Thank you for your subscription."))
            process_subscription(payment)
        else:
            messages.success(request, gettext("Thank you for your donation."))
            donation = process_donation(payment)
            if donation.reward:
                return redirect(donation)

    return redirect(reverse("user"))


@login_required
def download_payment_invoice(request, pk):
    # Allow downloading own invoices of pending ones (for proforma invoices)
    payment = get_object_or_404(Payment, pk=pk)

    if (
        not payment.state == Payment.PENDING
        and not Donation.objects.filter(
            Q(customer__users=request.user)
            & (Q(payment=payment.uuid) | Q(pastpayments__payment=payment.uuid))
        ).exists()
        and not Service.objects.filter(
            Q(customer__users=request.user)
            & (
                Q(subscription__payment=payment.uuid)
                | Q(subscription__pastpayments__payment=payment.uuid)
            )
        ).exists()
        and not payment.customer.origin == PAYMENTS_ORIGIN
        and not payment.customer.user_id == request.user.id
    ):
        raise Http404("Invoice not accessible to current user!")

    # New invoice model
    if payment.paid_invoice:
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
    donation = get_object_or_404(Donation, pk=pk, customer__users=request.user)
    payment = donation.payment_obj
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
        reward = self.object.reward
        if reward == 2:
            return EditLinkForm
        if reward == 3:
            return EditImageForm
        return EditNameForm

    def get_queryset(self):
        return Donation.objects.filter(customer__users=self.request.user, reward__gt=0)

    def form_valid(self, form):
        """If the form is valid, save the associated model."""
        mail_admins(
            "Weblate: link changed",
            "New link: {link_url}\nNew text: {link_text}\n".format(
                link_url=form.cleaned_data.get("link_url", "N/A"),
                link_text=form.cleaned_data.get("link_text", "N/A"),
            ),
        )
        return super().form_valid(form)


@method_decorator(login_required, name="dispatch")
class EditDiscoveryView(UpdateView):
    template_name = "subscription/discovery.html"
    success_url = "/user/"
    form_class = EditDiscoveryForm
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        return Service.objects.filter(customer__users=self.request.user)

    def form_valid(self, form):
        """If the form is valid, save the associated model."""
        mail_admins(
            "Weblate: discovery description changed",
            "Service link: {discover_url}\nNew text: {discover_text}\n".format(
                discover_url=form.instance.site_url,
                discover_text=form.cleaned_data.get("discover_text", "N/A"),
            ),
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
        mail_admins(
            "Weblate: discovery registered",
            "Service link: {discover_url}\nNew text: {discover_text}\n".format(
                discover_url=instance.site_url,
                discover_text=form.cleaned_data.get("discover_text", "N/A"),
            ),
        )
        result = super().form_valid(form)
        instance.customer.users.add(self.request.user)
        if instance.site_url:
            url = instance.site_url.rstrip("/")
            return redirect(f"{url}/manage/?activation={instance.secret}")
        return result


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
        kwargs["related"] = (
            Post.objects.filter(topic=self.object.topic)
            .exclude(pk=self.object.pk)
            .order_by("-timestamp")[:3]
        )
        return kwargs


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
def donate_pay(request, pk):
    donation = get_object_or_404(Donation, pk=pk, customer__users=request.user)
    if donation.payment_obj is None:
        raise Http404("Nothing to pay")
    with override("en"):
        payment = Payment.objects.create(
            amount=donation.get_amount(),
            description=donation.get_payment_description(),
            recurring=donation.payment_obj.recurring,
            extra={"donation": donation.pk, "category": "donate"},
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
    with override("en"):
        invoice = Subscription.new_subscription_invoice(
            kind=InvoiceKind.DRAFT, customer=customer, package=package, service=service
        )
        payment = invoice.create_payment(recurring=package.get_repeat())
    return redirect(payment.get_payment_url())


class DiscoverView(TemplateView):
    template_name = "discover.html"

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        services: Iterable[Service]
        projects: Iterable[Project]

        services = Service.objects.filter(discoverable=True).prefetch_related(
            "project_set"
        )
        query = self.request.GET.get("q", "").strip().lower()
        if query:
            projects = Project.objects.filter(
                service__discoverable=True
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
                Service.objects.filter(customer__users=self.request.user).values_list(
                    "pk", flat=True
                )
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
        data["user_services"] = Service.objects.filter(
            customer__users=self.request.user
        )
        data["user_donations"] = Donation.objects.filter(
            customer__users=self.request.user
        )
        return data


class HostingView(TemplateView):
    template_name = "hosting.html"

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        data["hosting_packages"] = (
            Package.objects.filter(category=PackageCategory.PACKAGE_SHARED)
            .exclude(name__endswith="-m")
            .order_by("price")[:6]
        )
        return data


def fosdem_donation(request):
    # Create customer
    customer = Customer.objects.create(origin=FOSDEM_ORIGIN, user_id=-1)
    # Create payment
    payment = Payment.objects.create(
        customer=customer,
        description="FOSDEM donation",
        amount_fixed=True,
        amount=30,
        extra={"category": "donate"},
    )
    # Redirect to payment
    return redirect(payment.get_absolute_url())
