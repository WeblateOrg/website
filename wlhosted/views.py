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

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.utils.decorators import method_decorator
from django.utils.translation import get_language
from django.views.generic.edit import FormView

from weblate.billing.models import Plan

from wlhosted.forms import ChooseBillingForm

# List of supported languages on weblate.org
SUPPORTED_LANGUAGES = frozenset((
    'ar', 'az', 'be', 'be@latin', 'bg', 'br', 'ca', 'cs', 'da', 'de', 'en',
    'el', 'en-gb', 'es', 'fi', 'fr', 'gl', 'he', 'hu', 'id', 'it', 'ja', 'ko',
    'nb', 'nl', 'pl', 'pt', 'pt-br', 'ru', 'sk', 'sl', 'sq', 'sr', 'sv', 'tr',
    'uk', 'zh-hans', 'zh-hant',
))


@method_decorator(login_required, name='dispatch')
class CreateBillingView(FormView):
    template_name = 'hosted/create.html'
    form_class = ChooseBillingForm
    success_url = 'https://weblate.org/{}/payment/?uuid={}'

    def get_success_url(self, payment):
        language = get_language()
        if language not in SUPPORTED_LANGUAGES:
            language = 'en'
        return self.success_url.format('en', payment.uuid)

    def form_valid(self, form):
        payment = form.create_payment(self.request.user)
        return HttpResponseRedirect(self.get_success_url(payment))

    def get_context_data(self, **kwargs):
        kwargs = super(CreateBillingView, self).get_context_data(**kwargs)
        kwargs['plans'] = Plan.objects.filter(public=True).order_by('price')
        return kwargs
