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

from typing import cast

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy

from weblate_web.invoices.forms import CustomerReferenceForm
from weblate_web.invoices.models import InvoiceKind, QuoteStatus
from weblate_web.models import Package, Service, Subscription
from weblate_web.payments.models import Customer

FINAL_INVOICE_CONFIRMATION_ERROR = gettext_lazy(
    "Please confirm that you want to issue a final invoice."
)
INVALID_UPGRADE_PACKAGE_ERROR = gettext_lazy(
    "This subscription can not be upgraded to the selected package."
)


class InvoiceConfirmationForm(forms.Form):
    confirm_invoice = forms.BooleanField(required=False)

    def clean(self):
        super().clean()
        if not self.cleaned_data.get("confirm_invoice"):
            raise ValidationError(FINAL_INVOICE_CONFIRMATION_ERROR)
        return self.cleaned_data


class CRMSearchForm(forms.Form):
    q = forms.CharField(
        label=gettext_lazy("Search"),
        required=False,
        widget=forms.TextInput(attrs={"type": "search"}),
    )


class CustomerMergeForm(forms.Form):
    merge = forms.ModelChoiceField(
        label=gettext_lazy("Merge with following customer object"),
        queryset=Customer.objects.none(),
        widget=forms.NumberInput(),
        error_messages={
            "invalid_choice": gettext_lazy("Select a valid customer to merge into.")
        },
    )

    def __init__(self, *args, customer: Customer, hidden: bool = False, **kwargs):
        self.customer = customer
        super().__init__(*args, **kwargs)
        merge_field = cast("forms.ModelChoiceField", self.fields["merge"])
        merge_field.queryset = Customer.objects.all()
        if hidden:
            merge_field.widget = forms.HiddenInput()

    def clean_merge(self) -> Customer:
        merge = self.cleaned_data["merge"]
        if merge == self.customer:
            raise ValidationError(
                gettext_lazy("A customer can not be merged into itself.")
            )
        return merge


class ServiceSubscriptionActionForm(CustomerReferenceForm):
    ACTION_RENEWAL = "renewal"
    ACTION_UPGRADE = "upgrade"
    ACTION_DISABLE = "disable"

    DOCUMENT_ACTIONS = (
        ACTION_RENEWAL,
        ACTION_UPGRADE,
    )

    action = forms.ChoiceField(
        choices=(
            (ACTION_RENEWAL, gettext_lazy("Renewal")),
            (ACTION_UPGRADE, gettext_lazy("Upgrade")),
        ),
        required=False,
        widget=forms.HiddenInput,
    )
    kind = forms.TypedChoiceField(
        choices=(
            (InvoiceKind.QUOTE, gettext_lazy("Quote")),
            (InvoiceKind.INVOICE, gettext_lazy("Invoice")),
        ),
        coerce=InvoiceKind.from_str,
        initial=InvoiceKind.QUOTE,
        required=False,
        widget=forms.RadioSelect,
    )
    subscription = forms.ModelChoiceField(queryset=Subscription.objects.none())
    package = forms.ModelChoiceField(
        queryset=Package.objects.all(),
        required=False,
        to_field_name="name",
        error_messages={"invalid_choice": INVALID_UPGRADE_PACKAGE_ERROR},
    )
    confirm_invoice = forms.BooleanField(required=False)

    def __init__(self, *args, service: Service, **kwargs):
        self.service = service
        super().__init__(*args, **kwargs)
        subscription_field = cast("forms.ModelChoiceField", self.fields["subscription"])
        subscription_field.queryset = service.subscription_set.all()

    def clean(self):
        super().clean()
        if self.ACTION_DISABLE in self.data:
            if self.cleaned_data.get("action"):
                raise ValidationError(gettext_lazy("Missing action."))
            self.cleaned_data["action"] = self.ACTION_DISABLE
            return self.cleaned_data

        action = self.cleaned_data.get("action")
        if action not in self.DOCUMENT_ACTIONS:
            raise ValidationError(gettext_lazy("Missing action."))
        self.cleaned_data["action"] = action
        kind = self.cleaned_data.get("kind") or InvoiceKind.QUOTE
        if kind == InvoiceKind.INVOICE and not self.cleaned_data.get("confirm_invoice"):
            raise ValidationError(FINAL_INVOICE_CONFIRMATION_ERROR)
        self.cleaned_data["kind"] = kind
        return self.cleaned_data


class RefundConfirmationForm(forms.Form):
    description = forms.CharField(
        label=gettext_lazy("Refund description"),
        required=False,
        max_length=200,
        help_text=gettext_lazy("Optional note describing how the refund was done."),
        widget=forms.TextInput(),
    )


class QuoteStatusForm(forms.Form):
    quote_status = forms.TypedChoiceField(
        label=gettext_lazy("Quote status"),
        help_text=gettext_lazy("Why this quote should no longer be followed up."),
        choices=(
            (QuoteStatus.LOST, QuoteStatus.LOST.label),
            (QuoteStatus.SUPERSEDED, QuoteStatus.SUPERSEDED.label),
            (QuoteStatus.ACCEPTED_ELSEWHERE, QuoteStatus.ACCEPTED_ELSEWHERE.label),
            (QuoteStatus.ARCHIVED, QuoteStatus.ARCHIVED.label),
        ),
        coerce=QuoteStatus.from_str,
    )
    quote_status_note = forms.CharField(
        label=gettext_lazy("Quote status note"),
        help_text=gettext_lazy(
            "Optional internal note, such as a rejection reason or accepted "
            "alternative invoice."
        ),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class ManualInteractionForm(forms.Form):
    note = forms.CharField(
        label=gettext_lazy("Note"),
        widget=forms.Textarea(attrs={"rows": 4}),
    )


class CustomerFollowUpForm(forms.Form):
    follow_up_at = forms.DateTimeField(
        label=gettext_lazy("Follow-up date"),
        input_formats=("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"),
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    follow_up_note = forms.CharField(
        label=gettext_lazy("Follow-up note"),
        max_length=200,
        required=False,
        widget=forms.TextInput(),
    )

    def __init__(self, *args, instance: Customer, **kwargs):
        self.instance = instance
        kwargs.setdefault(
            "initial",
            {
                "follow_up_at": instance.follow_up_at,
                "follow_up_note": instance.follow_up_note,
            },
        )
        super().__init__(*args, **kwargs)

    def save(self, *, commit: bool = True) -> Customer:
        self.instance.follow_up_at = self.cleaned_data["follow_up_at"]
        self.instance.follow_up_note = self.cleaned_data["follow_up_note"]
        if commit:
            self.instance.save(
                update_fields=["follow_up_at", "follow_up_note"],
            )
        return self.instance


class CustomerUserForm(forms.Form):
    email = forms.EmailField(label=gettext_lazy("E-mail"))
    full_name = forms.CharField(label=gettext_lazy("Full name"), max_length=150)


class ServiceMaintenanceWindowForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ("maintenance_window",)
