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

import uuid

from appconf import AppConf

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _

from django_countries.fields import CountryField

from vies.models import VATINField
from vies.validators import VATINValidator

from weblate.utils.fields import JSONField
from weblate.utils.validators import validate_email

EU_VAT_RATES = {
    'BE': 21,
    'BG': 20,
    'CZ': 21,
    'DK': 25,
    'DE': 19,
    'EE': 20,
    'IE': 23,
    'EL': 24,
    'ES': 21,
    'FR': 20,
    'HR': 25,
    'IT': 22,
    'CY': 19,
    'LV': 21,
    'LT': 21,
    'LU': 17,
    'HU': 27,
    'MT': 18,
    'NL': 21,
    'AT': 20,
    'PL': 23,
    'PT': 23,
    'RO': 19,
    'SI': 22,
    'SK': 20,
    'FI': 24,
    'SE': 25,
    'UK': 20,
}


@python_2_unicode_compatible
class Customer(models.Model):
    vat = VATINField(
        validators=[VATINValidator(verify=True, validate=True)],
        blank=True, null=True,
        verbose_name=_('European VAT ID'),
        help_text=_(
            'Please fill in European Union VAT ID, '
            'leave blank if not applicable.'
        ),
    )
    tax = models.CharField(
        max_length=200, blank=True,
        verbose_name=_('Tax registration'),
        help_text=_(
            'Please fill in your tax registration if it shoud '
            'appear on the invoice.'
        )
    )
    name = models.CharField(
        max_length=200, null=True,
        verbose_name=_('Company name'),
    )
    address = models.CharField(
        max_length=200, null=True,
        verbose_name=_('Address'),
    )
    city = models.CharField(
        max_length=200, null=True,
        verbose_name=_('Postcode and city'),
    )
    country = CountryField(
        null=True,
        verbose_name=_('Country'),
    )
    email = models.EmailField(
        blank=False,
        max_length=190,
        validators=[validate_email],
    )
    origin = models.URLField(max_length=300)
    user_id = models.IntegerField()

    def __str__(self):
        if self.name:
            return '{} ({})'.format(self.name, self.email)
        return self.email

    @property
    def country_code(self):
        if self.country:
            return self.country.code.upper()
        return None

    @property
    def vat_country_code(self):
        if self.vat:
            if hasattr(self.vat, 'country_code'):
                return self.vat.country_code.upper()
            return self.vat[:2].upper()
        return None

    def clean(self):
        if self.vat:
            if self.vat_country_code != self.country_code:
                raise ValidationError(
                    {'country': _('The country has to match your VAT code')}
                )

    @property
    def is_empty(self):
        return not (self.name and self.address and self.city and self.country)

    @property
    def is_eu_enduser(self):
        return (self.country_code in EU_VAT_RATES and not self.vat)

    @property
    def needs_vat(self):
        return self.vat_country_code == 'CZ' or self.is_eu_enduser

    @property
    def vat_rate(self):
        if self.needs_vat:
            return EU_VAT_RATES[self.country_code]
        return 0


class Payment(models.Model):
    NEW = 1
    PENDING = 2
    REJECTED = 3
    ACCEPTED = 4
    PROCESSED = 5

    uuid = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    amount = models.IntegerField()
    description = models.TextField()
    recurring = models.CharField(
        choices=[
            ('y', 'Yearly'),
            ('m', 'Monthly'),
            ('', 'None'),
        ],
        default='',
        max_length=10,
    )
    created = models.DateTimeField(auto_now_add=True)
    state = models.IntegerField(
        choices=[
            (NEW, 'New'),
            (PENDING, 'Pending'),
            (REJECTED, 'Rejected'),
            (ACCEPTED, 'Accepted'),
            (PROCESSED, 'Processed'),
        ],
        db_index=True,
        default=NEW
    )
    processor = models.CharField(max_length=100, default='')
    details = JSONField(editable=False, default={})
    extra = JSONField(editable=False, default={})
    customer = models.ForeignKey(
        Customer, on_delete=models.deletion.CASCADE, blank=True
    )
    repeat = models.ForeignKey(
        'Payment',
        on_delete=models.deletion.CASCADE,
        null=True, blank=True
    )
    invoice = models.CharField(max_length=20, blank=True, default='')

    @property
    def vat_amount(self):
        if self.customer.needs_vat:
            rate = 100 + self.customer.vat_rate
            return round(1.0 * rate * self.amount / 100, 2)
        return self.amount


class PaymentConf(AppConf):
    DEBUG = False
    SECRET = 'secret'
    FAKTURACE = None
    THEPAY_MERCHANTID = None
    THEPAY_ACCOUNTID = None
    THEPAY_PASSWORD = None
    THEPAY_DATAAPI = None

    class Meta(object):
        prefix = 'PAYMENT'
