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

from dateutil import relativedelta

from appconf import AppConf

from django.utils import timezone

from weblate.billing.models import Plan, Billing, Invoice
from weblate.auth.models import User

from wlhosted.payments.models import Payment


def end_interval(payment, start):
    if payment.repeat:
        period = payment.repeat.recurring
    else:
        period = payment.recurring
    if period == 'y':
        return start + relativedelta.relativedelta(years=1)
    elif period == 'm':
        return start + relativedelta.relativedelta(months=1)
    raise ValueError('Invalid payment period!')


def handle_received_payment(payment):
    params = {
        'plan': Plan.objects.get(pk=payment.extra['plan']),
        'state': Billing.STATE_ACTIVE,
    }
    if 'billing' in payment.extra:
        billing = Billing.objects.get(pk=payment.extra['billing'])
        for key, value in params.items():
            setattr(billing, key, value)
    else:
        billing = Billing.objects.create(**params)
        billing.owners.add(User.objects.get(pk=payment.customer.user_id))

    # Initial payment
    if payment.recurring:
        billing.payment['initial'] = payment.pk
    # Store all payment links
    if 'all' not in billing.payment:
        billing.payment['all'] = []
    billing.payment['all'].append(payment.pk)

    billing.save()

    start = timezone.now()

    Invoice.objects.create(
        billing=billing,
        start=start,
        end=end_interval(payment, start),
        payment=payment.amount,
        currency=Invoice.CURRENCY_EUR,
        ref=payment.invoice,
    )

    payment.state = Payment.PROCESSED
    payment.save()

    return billing


class HostedConf(AppConf):
    PAYMENT_REDIRECT_URL = 'https://weblate.org/{language}/payment/{uuid}/'

    class Meta(object):
        prefix = ''
