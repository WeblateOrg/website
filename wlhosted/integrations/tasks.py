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

from __future__ import absolute_import, unicode_literals

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.http import urlencode

from six.moves.urllib.request import Request, urlopen

from weblate import USER_AGENT
from weblate.billing.models import Billing
from weblate.celery import app

from wlhosted.payments.backends import get_backend
from wlhosted.payments.models import Payment
from wlhosted.integrations.models import handle_received_payment
from wlhosted.integrations.utils import get_origin, get_payment_url


@app.task
def pending_payments():
    with transaction.atomic(using='payments_db'):
        payments = Payment.objects.filter(
            customer__origin=get_origin(),
            state=Payment.ACCEPTED,
        ).select_for_update()
        for payment in payments:
            handle_received_payment(payment)


@app.task
def recurring_payments():
    with transaction.atomic(using='payments_db'):
        cutoff = timezone.now().date() + timedelta(days=1)
        for billing in Billing.objects.filter(state=Billing.STATE_ACTIVE):
            if 'recurring' not in billing.payment:
                continue
            last_invoice = billing.invoice_set.order_by('-start')[0]
            if last_invoice.end > cutoff:
                continue

            original = Payment.objects.get(pk=billing.payment['recurring'])

            # Check if backend is still valid
            try:
                get_backend(original.details['backend'])
            except KeyError:
                continue

            # Create new payment object
            payment = Payment.objects.create(
                amount=original.amount,
                description=original.description,
                recurring='',
                customer=original.customer,
                repeat=original,
                extra={
                    'plan': original.extra['plan'],
                    'billing': billing.pk,
                    'period': original.extra['period'],
                }
            )

            # Trigger payment processing
            request = Request(get_payment_url(payment))
            request.add_header('User-Agent', USER_AGENT)
            handle = urlopen(
                request,
                urlencode({
                    'method': original.details['backend'],
                    'secret': settings.PAYMENT_SECRET,
                }).encode('utf-8')
            )
            handle.read()
            handle.close()

    # We have created bunch of pending payments, process them now
    pending_payments()


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(
        300,
        pending_payments.s(),
        name='pending-payments',
    )
    sender.add_periodic_task(
        86400,
        recurring_payments.s(),
        name='recurring-payments',
    )
