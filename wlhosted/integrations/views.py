# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2018 Michal Čihař <michal@cihar.com>
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

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext as _
from django.views.generic.edit import FormView

from weblate.billing.models import Plan, Billing
from weblate.utils import messages
from weblate.utils.views import show_form_errors

from wlhosted.integrations.forms import BillingForm, ChooseBillingForm
from wlhosted.integrations.models import handle_received_payment
from wlhosted.payments.models import Payment
from wlhosted.integrations.utils import get_origin, get_payment_url


def get_default_billing(user):
    """Get trial billing for user to be ugpraded.
    """
    billings = Billing.objects.for_user(user).filter(
        state=Billing.STATE_TRIAL
    )
    if billings.count() == 1:
        return billings[0]
    return None


@method_decorator(login_required, name='dispatch')
class CreateBillingView(FormView):
    template_name = 'hosted/create.html'
    form_class = BillingForm

    def get_form_kwargs(self):
        result = super(CreateBillingView, self).get_form_kwargs()
        result['user'] = self.request.user
        return result

    def handle_payment(self, request):
        try:
            payment = Payment.objects.select_for_update().get(
                uuid=request.GET['payment'],
                customer__user_id=request.user.id,
                customer__origin=get_origin(),
            )
        except (Payment.DoesNotExist, ValidationError):
            messages.error(request, _('No matching payment found.'))
            return redirect('create-billing')

        if payment.state in (Payment.ACCEPTED, Payment.PROCESSED):
            if payment.state == Payment.ACCEPTED:
                handle_received_payment(payment)

            messages.success(
                request,
                _('Thank you for purchasing hosting plan, it is now active.')
            )
            return redirect('billing')
        elif payment.state in (Payment.PENDING, Payment.PROCESSED):
            messages.info(
                request,
                _(
                    'Thank you for purchasing hosting plan, the payment is '
                    'pending and will be processed in the background.'
                )
            )
            return redirect('billing')
        elif payment.state == Payment.REJECTED:
            messages.error(
                request,
                _('The payment was rejected: {}').format(
                    payment.details.get('reject_reason', _('Unknown reason'))
                )
            )
        elif payment.state == Payment.NEW:
            return HttpResponseRedirect(get_payment_url(payment))
        return redirect('create-billing')

    def get(self, request, *args, **kwargs):
        if 'payment' in request.GET:
            with transaction.atomic(using='payments_db'):
                return self.handle_payment(request)
        return super(CreateBillingView, self).get(request, *args, **kwargs)

    def form_valid(self, form):
        if not settings.PAYMENT_ENABLED:
            messages.error(
                self.request, _('Payments are temporarily disabled.')
            )
            return redirect('create-billing')
        with transaction.atomic(using='payments_db'):
            payment = form.create_payment(self.request.user)
            return HttpResponseRedirect(get_payment_url(payment))

    def form_invalid(self, form):
        show_form_errors(self.request, form)
        return super(CreateBillingView, self).form_invalid(form)

    def get_context_data(self, **kwargs):
        kwargs = super(CreateBillingView, self).get_context_data(**kwargs)
        kwargs['plans'] = list(Plan.objects.public())
        default_billing = get_default_billing(self.request.user)
        if 'billing' in self.request.GET:
            data = self.request.GET
        else:
            data = None
        form = ChooseBillingForm(
            self.request.user,
            data,
            initial={'billing': default_billing},
        )
        if form.is_valid():
            kwargs['billing'] = form.cleaned_data['billing']
        elif data is None:
            kwargs['billing'] = default_billing
        else:
            kwargs['billing'] = None
        kwargs['choose_billing'] = form
        if kwargs['billing']:
            for plan in kwargs['plans']:
                plan.would_fit = kwargs['billing'].in_display_limits(plan)
        # TODO:
        # - do not show billing form if user is coming from specific billing
        #   to simplify upgrade/pay scenario
        return kwargs
