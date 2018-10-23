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

from __future__ import unicode_literals

from django import forms
from django.conf import settings
from django.urls import reverse

from weblate.billing.models import Plan
from weblate.utils.site import get_site_url

from wlhosted.models import Payment, Customer


class ChooseBillingForm(forms.Form):
    plan = forms.ChoiceField(choices=[])
    period = forms.ChoiceField(choices=[('y', 'y'), ('m', 'm')])
    extra_domain = forms.BooleanField(required=False)

    def __init__(self, *args, **kwargs):
        super(ChooseBillingForm, self).__init__(*args, **kwargs)
        self.fields['plan'].choices = [
            (p, p) for p in Plan.objects.values_list('id', flat=True)
        ]

    def create_payment(self, user):
        customer = Customer.objects.get_or_create(
            origin=get_site_url(reverse('create-billing')),
            user_id=user.id,
            defaults={
                'email': user.email,
            }
        )[0]

        plan = Plan.objects.get(pk=self.cleaned_data['plan'])
        period = self.cleaned_data['period']
        description = 'Weblate hosting ({}, {})'.format(
            plan.name,
            'Monthly' if period == 'm' else 'Yearly'
        )
        amount = plan.price if period == 'm' else plan.yearly_price
        if self.cleaned_data['extra_domain']:
            amount += 100
            description += ' + Custom domain'
        return Payment.objects.create(
            amount=amount,
            description=description,
            recurring=self.cleaned_data['period'],
            customer=customer,
        )
