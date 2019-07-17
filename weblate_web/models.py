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

import uuid

import html2text
from django.contrib.auth.models import User
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import ugettext, ugettext_lazy
from markupfield.fields import MarkupField
from wlhosted.payments.models import RECURRENCE_CHOICES, Payment, get_period_delta

PAYMENTS_ORIGIN = 'https://weblate.org/donate/process/'


class Reward(models.Model):
    uuid = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    recurring = models.CharField(
        choices=RECURRENCE_CHOICES,
        default='',
        blank=True,
        max_length=10,
    )
    amount = models.PositiveIntegerField()
    has_link = models.BooleanField(blank=True)
    thanks_link = models.BooleanField(blank=True, db_index=True)
    third_party = models.BooleanField(blank=True)
    active = models.BooleanField(blank=True)
    name = models.CharField(max_length=200)

    class Meta:
        index_together = [
            ('active', 'third_party'),
        ]

    def get_absolute_url(self):
        return reverse('donate-reward', kwargs={'pk': self.pk})

    def get_display_name(self):
        return ugettext(self.name)

    def __str__(self):
        return self.name


class Donation(models.Model):
    user = models.ForeignKey(User, on_delete=models.deletion.CASCADE)
    payment = models.UUIDField()
    reward = models.ForeignKey(
        Reward, on_delete=models.deletion.CASCADE, null=True, blank=True
    )
    reward_new = models.IntegerField(
        choices=(
            (0, ugettext_lazy('No reward')),
            (1, ugettext_lazy('Name placement in the list of supporters')),
            (2, ugettext_lazy('Link placement in the list of supporters')),
            (3, ugettext_lazy('Logo & link placement on the Weblate website')),
        )
    )
    link_text = models.CharField(
        verbose_name=ugettext_lazy('Link text'),
        max_length=200, blank=True
    )
    link_url = models.URLField(
        verbose_name=ugettext_lazy('Link URL'),
        blank=True
    )
    link_image = models.ImageField(
        verbose_name=ugettext_lazy('Link image'),
        blank=True,
        upload_to='donations/'
    )
    created = models.DateTimeField(auto_now_add=True)
    expires = models.DateTimeField()
    active = models.BooleanField(blank=True, db_index=True)

    @cached_property
    def payment_obj(self):
        return Payment.objects.get(pk=self.payment)

    def list_payments(self):
        initial = Payment.objects.filter(pk=self.payment)
        return initial | initial[0].payment_set.all()

    def get_absolute_url(self):
        return reverse('donate-edit', kwargs={'pk': self.pk})

    def get_amount(self):
        return self.payment_obj.amount

    def __str__(self):
        return '{}:{}'.format(self.user, self.reward)


def process_payment(payment):
    if payment.state != Payment.ACCEPTED:
        raise ValueError('Can not process not accepted payment')
    if payment.repeat:
        # Update existing
        donation = Donation.objects.get(payment=payment.repeat.pk)
        donation.expires += get_period_delta(payment.repeat.recurring)
        donation.save()
    else:
        user = User.objects.get(pk=payment.customer.user_id)
        reward = None
        if 'reward' in payment.extra:
            reward = Reward.objects.get(pk=payment.extra['reward'])
        # Calculate expiry
        expires = timezone.now()
        if reward:
            expires += get_period_delta(reward.recurring)
        elif payment.recurring:
            expires += get_period_delta(payment.recurring)
        # Create new
        donation = Donation.objects.create(
            user=user,
            payment=payment.pk,
            reward=reward,
            expires=expires,
            active=True,
        )
    # Flag payment as processed
    payment.state = Payment.PROCESSED
    payment.save()
    return donation


class Image(models.Model):
    name = models.CharField(max_length=100, unique=True)
    image = models.ImageField(upload_to='images/')

    def __str__(self):
        return self.name


class Post(models.Model):
    title = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    timestamp = models.DateTimeField(db_index=True)
    author = models.ForeignKey(
        User, editable=False, on_delete=models.deletion.SET_NULL, null=True
    )
    topic = models.CharField(
        max_length=100,
        db_index=True,
        choices=(
            ('release', ugettext_lazy('Release')),
            ('feature', ugettext_lazy('Features')),
            ('announce', ugettext_lazy('Announcement')),
            ('conferences', ugettext_lazy('Conferences')),
            ('hosting', ugettext_lazy('Hosted Weblate')),
            ('development', ugettext_lazy('Development')),
            ('localization', ugettext_lazy('Localization')),
        )
    )
    body = MarkupField(default_markup_type='markdown')
    summary = models.TextField(
        blank=True,
        help_text='Will be generated from first body paragraph if empty'
    )
    image = models.ForeignKey(
        Image, on_delete=models.deletion.SET_NULL, blank=True, null=True
    )

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        # Need to save first as rendered value is available only then
        super().save(force_insert, force_update, using, update_fields)
        if not self.summary:
            h2t = html2text.HTML2Text()
            h2t.body_width = 0
            h2t.ignore_images = True
            h2t.ignore_links = True
            h2t.ignore_emphasis = True
            text = h2t.handle(self.body.rendered)  # pylint: disable=no-member
            self.summary = text.splitlines()[0]
            if self.summary:
                super().save(update_fields=['summary'])

    def get_absolute_url(self):
        return reverse('post', kwargs={'slug': self.slug})

    def __str__(self):
        return self.title
