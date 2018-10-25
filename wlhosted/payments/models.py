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

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _

from django_countries.fields import CountryField

from vies.models import VATINField
from vies.validators import VATINValidator

from weblate.utils.fields import JSONField
from weblate.utils.validators import validate_email


@python_2_unicode_compatible
class Customer(models.Model):
    vat = VATINField(
        validators=[VATINValidator(verify=True, validate=True)],
        blank=True, null=True,
        verbose_name=_('European VAT ID'),
        help_text=_(
            'Please fill in Europe Union VAT ID, '
            'keep the field blank if not applicable.'
        ),
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
        verbose_name=_('Post code code and city'),
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

    def clean(self):
        if self.vat:
            if self.vat[:2].lower() != self.country.code.lower():
                raise ValidationError(
                    {'country': _('Country has to match your VAT code')}
                )

    def empty(self):
        return not (self.name and self.address and self.city and self.country)

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
