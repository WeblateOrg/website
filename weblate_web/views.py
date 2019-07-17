# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2019 Michal Čihař <michal@cihar.com>
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

import django.views.defaults
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import SuspiciousOperation, ValidationError
from django.core.mail import mail_admins, send_mail
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext as _, override
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_POST
from django.views.generic.dates import ArchiveIndexView
from django.views.generic.detail import DetailView, SingleObjectMixin
from django.views.generic.edit import FormView, UpdateView
from weblate.utils.django_hacks import monkey_patch_translate
from wlhosted.payments.backends import get_backend, list_backends
from wlhosted.payments.forms import CustomerForm
from wlhosted.payments.models import Customer, Payment
from wlhosted.payments.validators import cache_vies_data, validate_vatin

from weblate_web.forms import DonateForm, EditLinkForm, MethodForm, SubscribeForm
from weblate_web.models import PAYMENTS_ORIGIN, Donation, Post, Reward, process_payment
from weblate_web.remote import get_activity


def show_form_errors(request, form):
    """Show all form errors as a message."""
    for error in form.non_field_errors():
        messages.error(request, error)
    for field in form:
        for error in field.errors:
            messages.error(
                request,
                _('Error in parameter %(field)s: %(error)s') % {
                    'field': field.name,
                    'error': error
                }
            )


@require_POST
def fetch_vat(request):
    if 'payment' not in request.POST or 'vat' not in request.POST:
        raise SuspiciousOperation('Missing needed parameters')
    payment = Payment.objects.filter(
        pk=request.POST['payment'], state=Payment.NEW
    )
    if not payment.exists():
        raise SuspiciousOperation('Already processed payment')
    vat = cache_vies_data(request.POST['vat'])
    return JsonResponse(data=getattr(vat, 'vies_data', {'valid': False}))


class PaymentView(FormView, SingleObjectMixin):
    model = Payment
    form_class = MethodForm
    template_name = 'payment/payment.html'
    check_customer = True

    def redirect_origin(self):
        return redirect(
            '{}?payment={}'.format(
                self.object.customer.origin,
                self.object.pk,
            )
        )

    def get_context_data(self, **kwargs):
        kwargs = super().get_context_data(**kwargs)
        kwargs['can_pay'] = self.can_pay
        kwargs['backends'] = [x(self.object) for x in list_backends()]
        return kwargs

    def validate_customer(self, customer):
        if not self.check_customer:
            return None
        if customer.is_empty:
            messages.info(
                self.request,
                _(
                    'Please provide your billing information to '
                    'complete the payment.'
                )
            )
            return redirect('payment-customer', pk=self.object.pk)
        if customer.vat:
            try:
                validate_vatin(customer.vat)
            except ValidationError:
                messages.warning(
                    self.request,
                    _('The VAT ID is no longer valid, please update it.')
                )
                return redirect('payment-customer', pk=self.object.pk)
        return None

    def dispatch(self, request, *args, **kwargs):
        with transaction.atomic(using='payments_db'):
            self.object = self.get_object()
            customer = self.object.customer
            self.can_pay = not customer.is_empty
            # Redirect already processed payments to origin in case
            # the web redirect was aborted
            if self.object.state != Payment.NEW:
                return self.redirect_origin()
            result = self.validate_customer(customer)
            if result is not None:
                return result
            return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form):
        if self.form_class == MethodForm:
            messages.error(self.request, _('Please choose a payment method to continue.'))
        else:
            messages.error(
                self.request,
                _(
                    'Please provide your billing information to '
                    'complete the payment.'
                )
            )
        return super().form_invalid(form)

    def form_valid(self, form):
        if not self.can_pay:
            return redirect('payment', pk=self.object.pk)
        # Actualy call the payment backend
        method = form.cleaned_data['method']
        backend = get_backend(method)(self.object)
        result = backend.initiate(
            self.request,
            self.request.build_absolute_uri(
                reverse('payment', kwargs={'pk': self.object.pk})
            ),
            self.request.build_absolute_uri(
                reverse('payment-complete', kwargs={'pk': self.object.pk})
            ),
        )
        if result is not None:
            return result
        backend.complete(self.request)
        return self.redirect_origin()


class CustomerView(PaymentView):
    form_class = CustomerForm
    template_name = 'payment/customer.html'
    check_customer = False

    def form_valid(self, form):
        form.save()
        return redirect('payment', pk=self.object.pk)

    def get_form_kwargs(self):
        """Return the keyword arguments for instantiating the form."""
        kwargs = super().get_form_kwargs()
        kwargs['instance'] = self.object.customer
        return kwargs


class CompleteView(PaymentView):
    def dispatch(self, request, *args, **kwargs):
        with transaction.atomic(using='payments_db'):
            self.object = self.get_object()
            if self.object.state == Payment.NEW:
                return redirect('payment', pk=self.object.pk)
            if self.object.state != Payment.PENDING:
                return self.redirect_origin()

            backend = get_backend(self.object.backend)(self.object)
            backend.complete(self.request)
            return self.redirect_origin()


@method_decorator(login_required, name='dispatch')
class DonateView(FormView):
    form_class = DonateForm
    template_name = 'donate/form.html'

    def get_form_kwargs(self):
        result = super().get_form_kwargs()
        result['initial'] = self.request.GET
        return result

    def redirect_payment(self, **kwargs):
        kwargs['customer'] = Customer.objects.get_or_create(
            origin=PAYMENTS_ORIGIN,
            user_id=self.request.user.id,
            defaults={
                'email': self.request.user.email,
            }
        )[0]
        payment = Payment.objects.create(**kwargs)
        return redirect(payment.get_payment_url())

    def form_invalid(self, form):
        show_form_errors(self.request, form)
        return super().form_invalid(form)

    def form_valid(self, form):
        data = form.cleaned_data
        if data['reward'] and int(data['reward']):
            tmp = Donation(reward_new=int(data['reward']))
            with override('en'):
                description = 'Weblate donation: {}'.format(tmp.get_reward_new_display())
        else:
            description = 'Weblate donation'
        return self.redirect_payment(
            amount=data['amount'],
            amount_fixed=True,
            description=description,
            recurring=data['recurring'],
            extra={
                'reward': data['reward'],
            }
        )


@login_required
def process_donation(request):
    try:
        payment = Payment.objects.get(
            pk=request.GET['payment'],
            customer__origin=PAYMENTS_ORIGIN,
            customer__user_id=request.user.id
        )
    except (KeyError, Payment.DoesNotExist):
        return redirect(reverse('user'))

    # Create donation
    if payment.state in (Payment.NEW, Payment.PENDING):
        messages.error(
            request,
            _('Payment not yet processed, please retry.')
        )
    elif payment.state == Payment.REJECTED:
        messages.error(
            request,
            _('The payment was rejected: {}').format(
                payment.details.get('reject_reason', _('Unknown reason'))
            )
        )
    elif payment.state == Payment.ACCEPTED:
        messages.success(request, _('Thank you for your donation.'))
        donation = process_payment(payment)
        if donation.reward_new:
            return redirect(donation)

    return redirect(reverse('user'))


@login_required
def download_invoice(request, pk):
    payment = get_object_or_404(
        Payment,
        pk=pk,
        customer__origin=PAYMENTS_ORIGIN,
        customer__user_id=request.user.id
    )

    if not payment.invoice_filename_valid:
        raise Http404(
            'File {0} does not exist!'.format(payment.invoice_filename)
        )

    with open(payment.invoice_full_filename, 'rb') as handle:
        data = handle.read()

    response = HttpResponse(
        data,
        content_type='application/pdf'
    )
    response['Content-Disposition'] = 'attachment; filename={0}'.format(
        payment.invoice_filename
    )
    response['Content-Length'] = len(data)

    return response


@require_POST
@login_required
def disable_repeat(request, pk):
    donation = get_object_or_404(Donation, pk=pk, user=request.user)
    payment = donation.payment_obj
    payment.recurring = ''
    payment.save()
    return redirect(reverse('donate'))


@method_decorator(login_required, name='dispatch')
class EditLinkView(UpdateView):
    form_class = EditLinkForm
    template_name = 'donate/edit.html'
    success_url = '/user/'

    def get_form_class(self):
        # TODO: load form based on reward
        print(self.object.reward_new)
        return EditLinkForm

    def get_queryset(self):
        return Donation.objects.filter(user=self.request.user, reward_new__gt=0)

    def form_valid(self, form):
        """If the form is valid, save the associated model."""
        mail_admins(
            'Weblate: link changed',
            'New link: {link_url}\nNew text: {link_text}\n'.format(
                **form.cleaned_data
            )
        )
        return super().form_valid(form)


@require_POST
def subscribe(request, name):
    addresses = {
        'hosted': 'hosted-weblate-announce-join@lists.cihar.com',
        'users': 'weblate-join@lists.cihar.com',
    }
    form = SubscribeForm(request.POST)
    if form.is_valid():
        send_mail(
            'subscribe',
            'subscribe',
            form.cleaned_data['email'],
            [addresses[name]],
            fail_silently=True,
        )
        messages.success(
            request,
            _(
                'Subscription was initiated, '
                'you will shortly receive email to confirm it.'
            )
        )
    else:
        messages.error(
            request,
            _('Failed to process subscription request.')
        )

    return redirect('support')


class NewsArchiveView(ArchiveIndexView):
    model = Post
    date_field = 'timestamp'
    paginate_by = 10
    ordering = ('-timestamp',)


class NewsView(NewsArchiveView):
    paginate_by = 5
    template_name = 'news.html'


class PostView(DetailView):
    model = Post

    def get_object(self, queryset=None):
        result = super().get_object(queryset)
        if (not self.request.user.is_superuser
                and result.timestamp >= timezone.now()):
            raise Http404('Future entry')
        return result

    def get_context_data(self, **kwargs):
        kwargs['related'] = Post.objects.filter(
            topic=self.object.topic
        ).exclude(
            pk=self.object.pk
        ).order_by(
            '-timestamp'
        )[:3]
        return kwargs


# pylint: disable=unused-argument
def not_found(request, exception=None):
    """Error handler showing list of available projects."""
    return render(request, "404.html", status=404)


def server_error(request):
    # pylint: disable=broad-except
    """Error handler for server errors."""
    try:
        return render(request, "500.html", status=500)
    except Exception:
        return django.views.defaults.server_error(request)


@cache_control(max_age=3600)
def activity_svg(request):
    bars = []
    opacities = {
        0: '.1',
        1: '.3',
        2: '.5',
        3: '.7',
    }
    data = get_activity()
    top_count = max(data)
    for i, count in enumerate(data):
        height = int(76 * count / top_count)
        item = {
            'rx': 2,
            'width': 6,
            'height': height,
            'id': 'b{}'.format(i),
            'x': 10 * i,
            'y': 86 - height,
        }
        if height < 20:
            item['fill'] = '#f6664c'
        elif height < 45:
            item['fill'] = '#38f'
        else:
            item['fill'] = '#2eccaa'
        if i in opacities:
            item['opacity'] = opacities[i]

        bars.append(item)

    return render(
        request,
        'svg/activity.svg',
        {'bars': bars},
        content_type='image/svg+xml; charset=utf-8'
    )


monkey_patch_translate()
