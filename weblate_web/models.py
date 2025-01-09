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

import sys
from datetime import datetime, timedelta
from io import BytesIO
from typing import TYPE_CHECKING
from uuid import uuid4

import html2text
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.db.models import IntegerChoices, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.functional import cached_property
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, override, pgettext_lazy
from django_countries import countries
from markupfield.fields import MarkupField
from PIL import Image as PILImage

from weblate_web.payments.fields import Char32UUIDField
from weblate_web.payments.models import Customer, Payment
from weblate_web.payments.utils import send_notification
from weblate_web.zammad import create_dedicated_hosting_ticket

from .hetzner import create_storage_folder, create_storage_subaccount, generate_ssh_url
from .packages import PACKAGE_UPGRADES

if TYPE_CHECKING:
    from fakturace.invoices import Invoice

ALLOWED_IMAGES = {"image/jpeg", "image/png"}


REWARDS = (
    (0, gettext_lazy("No reward")),
    (1, gettext_lazy("Name in the list of supporters")),
    (2, gettext_lazy("Link in the list of supporters")),
    (3, gettext_lazy("Logo and link on the Weblate website")),
)
REWARD_LEVELS = {
    0: 0,
    1: 100,
    2: 400,
    3: 800,
}

TOPICS = (
    ("release", gettext_lazy("Release")),
    ("feature", gettext_lazy("Features")),
    ("announce", gettext_lazy("Announcement")),
    ("conferences", gettext_lazy("Conferences")),
    ("hosting", gettext_lazy("Hosted Weblate")),
    ("development", gettext_lazy("Development")),
    ("localization", gettext_lazy("Localization")),
)

TOPIC_DICT = dict(TOPICS)


class UnprocessablePaymentError(Exception):
    pass


def get_period_delta(period):
    if period == "y":
        return relativedelta(years=1)
    if period == "b":
        return relativedelta(months=6)
    if period == "q":
        return relativedelta(months=3)
    if period == "m":
        return relativedelta(months=1)
    raise ValueError(f"Invalid payment period {period!r}!")


def validate_bitmap(value):
    """
    Validate a bitmap.

    Based on django.forms.fields.ImageField and
    weblate.utils.validators.validate_bitmap.

    Raises
    ------
        ValidationError: If invame is not valid to be used.

    """
    if value is None:
        return

    # Ensure we have image object and content type
    # Pretty much copy from django.forms.fields.ImageField:

    # We need to get a file object for Pillow. We might have a path or we
    # might have to read the data into memory.
    if hasattr(value, "temporary_file_path"):
        content = value.temporary_file_path()
    elif hasattr(value, "read"):
        content = BytesIO(value.read())
    else:
        content = BytesIO(value["content"])

    try:
        # load() could spot a truncated JPEG, but it loads the entire
        # image in memory, which is a DoS vector. See #3848 and #18520.
        image = PILImage.open(content)
        # verify() must be called immediately after the constructor.
        image.verify()

        # Pillow doesn't detect the MIME type of all formats. In those
        # cases, content_type will be None.
        value.file.content_type = PILImage.MIME.get(
            image.format  # type: ignore[arg-type]
        )
    except Exception as error:
        # Pillow doesn't recognize it as an image.
        raise ValidationError(_("Invalid image!"), code="invalid_image").with_traceback(
            sys.exc_info()[2]
        ) from error

    try:
        if hasattr(value.file, "seek") and callable(value.file.seek):
            value.file.seek(0)

        # Check image type
        if value.file.content_type not in ALLOWED_IMAGES:
            raise ValidationError(
                _("Unsupported image type: %s") % value.file.content_type
            )

        # Check dimensions
        if image.size != (570, 260):
            raise ValidationError(
                _("Please upload an image with a resolution of 570 x 260 pixels.")
            )

    finally:
        image.close()


class MySQLSearchLookup(models.Lookup):
    lookup_name = "search"

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = lhs_params + rhs_params
        return f"MATCH ({lhs}) AGAINST ({rhs} IN NATURAL LANGUAGE MODE)", params


models.CharField.register_lookup(MySQLSearchLookup)


class Donation(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.deletion.PROTECT)
    payment = Char32UUIDField(blank=True, null=True)
    reward = models.IntegerField(choices=REWARDS, default=0)
    link_text = models.CharField(
        verbose_name=gettext_lazy("Link text"), max_length=200, blank=True
    )
    link_url = models.URLField(verbose_name=gettext_lazy("Link URL"), blank=True)
    link_image = models.ImageField(
        verbose_name=gettext_lazy("Link image"), blank=True, upload_to="donations/"
    )
    created = models.DateTimeField(auto_now_add=True)
    expires = models.DateTimeField()
    active = models.BooleanField(blank=True, db_index=True)

    class Meta:
        verbose_name = "Donation"
        verbose_name_plural = "Donations"

    def __str__(self):
        return f"{self.customer}:{self.reward}"

    def get_absolute_url(self):
        return reverse("donate-edit", kwargs={"pk": self.pk})

    @cached_property
    def payment_obj(self):
        if not self.payment:
            return None
        return Payment.objects.get(pk=self.payment)

    def list_payments(self):
        past = set(self.pastpayments_set.values_list("payment", flat=True))
        query = Q(pk=self.payment)
        if past:
            query |= Q(pk__in=past)
            query |= Q(repeat__pk__in=past)
        if self.payment:
            query |= Q(repeat__pk=self.payment)
        return Payment.objects.filter(query).distinct()

    def get_amount(self):
        if not self.payment:
            return 0
        return self.payment_obj.amount

    def get_payment_description(self):
        if self.reward:
            return f"Weblate donation: {self.get_reward_display()}"
        return "Weblate donation"

    def send_notification(self, notification: str):
        send_notification(
            notification,
            self.customer.get_notify_emails(),
            donation=self,
        )


def process_donation(payment):
    if payment.state != Payment.ACCEPTED:
        raise ValueError("Cannot process non-accepted payment")
    if payment.repeat:
        # Update existing
        donation = Donation.objects.get(payment=payment.repeat.pk)
        payment.start = donation.expires
        donation.expires += get_period_delta(payment.repeat.recurring)
        payment.end = donation.expires
        donation.active = True
        donation.save()
    elif "donation" in payment.extra:
        donation = Donation.objects.get(pk=payment.extra["donation"])
        if donation.payment:
            donation.pastpayments_set.create(payment=donation.payment)
        payment.start = donation.expires
        donation.expires += get_period_delta(payment.recurring or "y")
        payment.end = donation.expires
        donation.payment = payment.pk
        donation.save()
    else:
        reward = payment.extra.get("reward", 0)
        # Calculate expiry
        expires = timezone.now()
        if payment.recurring:
            payment.start = expires
            expires += get_period_delta(payment.recurring)
            payment.end = expires
        elif reward:
            payment.start = expires
            expires += get_period_delta("y")
            payment.end = expires
        # Create new
        donation = Donation.objects.create(
            customer=payment.customer,
            payment=payment.pk,
            reward=int(reward),
            expires=expires,
            active=True,
        )
    # Flag payment as processed
    payment.state = Payment.PROCESSED
    payment.save()
    return donation


def get_service(payment: Payment, customer: Customer):
    if payment.extra.get("service") is None:
        service = customer.service_set.create()
        service.was_created = True
        return service
    return Service.objects.get(pk=payment.extra["service"], customer=customer)


def process_repeated_payment(payment: Payment, repeated: Payment) -> Subscription:
    subscription = Subscription.objects.get(payment=repeated.pk)
    payment.start = subscription.expires
    subscription.expires += get_period_delta(repeated.recurring)
    payment.end = subscription.expires
    subscription.save()
    return subscription


def process_renewal_payment(payment: Payment) -> Subscription:
    subscription = Subscription.objects.get(pk=payment.extra["subscription"])
    if subscription.payment:
        subscription.pastpayments_set.create(payment=subscription.payment)
    payment.start = subscription.expires
    subscription.expires += get_period_delta(subscription.package.get_repeat())
    payment.end = subscription.expires
    subscription.payment = payment.pk
    subscription.save()
    return subscription


def process_new_payment(payment: Payment) -> Subscription:
    service = get_service(payment, payment.customer)
    if payment.paid_invoice and (paid_package := payment.paid_invoice.get_package()):
        package = paid_package
    elif payment.draft_invoice and (
        draft_package := payment.draft_invoice.get_package()
    ):
        package = draft_package
    else:
        package = Package.objects.get(name=payment.extra["subscription"])
    repeat = package.get_repeat()
    if (
        package.category == PackageCategory.PACKAGE_DEDICATED
        and service.hosted_subscriptions
    ):
        # Package upgrade / downgrade
        subscription = service.hosted_subscriptions[0]
        if subscription.payment:
            subscription.pastpayments_set.create(payment=subscription.payment)
        subscription.package = package
        subscription.expires += get_period_delta(repeat)
        subscription.payment = payment.pk
        subscription.save()
    else:
        if start_date := payment.extra.get("start_date"):
            expires = datetime.fromisoformat(start_date)
        else:
            expires = timezone.now()
        # Calculate expiry
        if repeat:
            payment.start = expires
            expires += get_period_delta(repeat)
            payment.end = expires
        # Create new
        subscription = Subscription.objects.create(
            service=service,
            payment=payment.pk,
            package=package,
            expires=expires,
        )
    with override("en"):
        send_notification(
            "new_subscription",
            settings.NOTIFY_SUBSCRIPTION,
            subscription=subscription,
            service=subscription.service,
        )
    if service.was_created and service.needs_token:
        subscription.send_notification("subscription_intro")

    if (
        service.was_created
        and subscription.package.category == PackageCategory.PACKAGE_DEDICATED
    ):
        create_dedicated_hosting_ticket(subscription)
    return subscription


def process_subscription(payment: Payment) -> Subscription:
    if payment.state != Payment.ACCEPTED:
        raise ValueError("Can not process not accepted payment")
    if payment.repeat:
        # Update existing
        subscription = process_repeated_payment(payment, payment.repeat)
    elif isinstance(payment.extra["subscription"], int):
        # Payment for current subscription
        subscription = process_renewal_payment(payment)
    else:
        subscription = process_new_payment(payment)

    # Flag payment as processed
    payment.state = Payment.PROCESSED
    payment.save()
    return subscription


def process_payment(payment: Payment):
    if not payment.extra:
        raise UnprocessablePaymentError
    if (
        "subscription" in payment.extra
        or (payment.paid_invoice and payment.paid_invoice.get_package())
        or (payment.draft_invoice and payment.draft_invoice.get_package())
    ):
        process_subscription(payment)
    elif "billing" in payment.extra:
        # Hosted subscription, currently not handled here
        raise UnprocessablePaymentError
    else:
        # donation or reward should be in extra here
        process_donation(payment)


class Image(models.Model):
    name = models.CharField(max_length=100, unique=True)
    image = models.ImageField(
        upload_to="images/", help_text="Article image, 1200x630 pixels"
    )

    class Meta:
        verbose_name = "Image"
        verbose_name_plural = "Images"

    def __str__(self):
        return self.name


class Post(models.Model):
    title = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    timestamp = models.DateTimeField(db_index=True)
    author = models.ForeignKey(
        User, editable=False, on_delete=models.deletion.SET_NULL, null=True
    )
    topic = models.CharField(max_length=100, db_index=True, choices=TOPICS, default="")
    body = MarkupField(default_markup_type="markdown")
    summary = models.TextField(
        blank=True, help_text="Will be generated from first body paragraph if empty"
    )
    image = models.ForeignKey(
        Image, on_delete=models.deletion.SET_NULL, blank=True, null=True
    )
    milestone = models.BooleanField(
        blank=True,
        db_index=True,
        default=False,
        help_text="Important milestone, shown in the milestones archive",
    )

    class Meta:
        verbose_name = "Blog post"
        verbose_name_plural = "Blog posts"

    def __str__(self):
        return self.title

    def save(  # type: ignore[override]
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using=None,
        update_fields=None,
    ):
        # Need to save first as rendered value is available only then
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
        if not self.summary:
            h2t = html2text.HTML2Text()
            h2t.body_width = 0
            h2t.ignore_images = True
            h2t.ignore_links = True
            h2t.ignore_emphasis = True
            text = h2t.handle(self.body.rendered)  # pylint: disable=no-member
            self.summary = text.splitlines()[0]
            if self.summary:
                super().save(update_fields=["summary"])

    def get_absolute_url(self):
        return reverse("post", kwargs={"slug": self.slug})


def generate_secret():
    return get_random_string(64)


class PackageCategory(IntegerChoices):
    PACKAGE_NONE = 0, pgettext_lazy("Package category", "None")
    PACKAGE_DEDICATED = 10, pgettext_lazy("Package category", "Dedicated hosting")
    PACKAGE_SHARED = 20, pgettext_lazy("Package category", "Shared hosting")
    PACKAGE_SUPPORT = 30, pgettext_lazy("Package category", "Self-hosted support")


class Package(models.Model):
    name = models.CharField(max_length=150, unique=True)
    verbose = models.CharField(max_length=400)
    price = models.IntegerField()
    limit_projects = models.IntegerField(default=0)
    limit_languages = models.IntegerField(default=0)
    limit_source_strings = models.IntegerField(default=0)
    limit_hosted_words = models.IntegerField(default=0)
    limit_hosted_strings = models.IntegerField(default=0)
    category = models.IntegerField(
        default=PackageCategory.PACKAGE_NONE, choices=PackageCategory
    )
    hidden = models.BooleanField(blank=True, db_index=True, default=False)

    class Meta:
        verbose_name = "Service package"
        verbose_name_plural = "Service packages"

    def __str__(self):
        return self.verbose

    def get_repeat(self):
        if self.name in {"basic", "extended", "premium", "backup"}:
            return "y"
        if self.category in {
            PackageCategory.PACKAGE_DEDICATED,
            PackageCategory.PACKAGE_SHARED,
        }:
            if self.name.endswith("-m"):
                return "m"
            return "y"
        return ""


class Service(models.Model):
    secret = models.CharField(max_length=100, default=generate_secret, db_index=True)
    customer = models.ForeignKey(Customer, on_delete=models.deletion.PROTECT)
    status = models.CharField(
        max_length=150,
        choices=(
            ("community", gettext_lazy("Community supported")),
            ("hosted", gettext_lazy("Dedicated hosted service")),
            ("shared", gettext_lazy("Hosted service")),
            ("basic", gettext_lazy("Basic self-hosted support")),
            ("extended", gettext_lazy("Extended self-hosted support")),
            ("premium", gettext_lazy("Premium self-hosted support")),
        ),
        default="community",
    )
    backup_repository = models.CharField(max_length=500, default="", blank=True)
    backup_box = models.IntegerField(default=0)
    backup_directory = models.CharField(max_length=50, default="", blank=True)
    backup_size = models.BigIntegerField(default=0)
    backup_timestamp = models.DateTimeField(blank=True, null=True)
    limit_languages = models.IntegerField(default=0)
    limit_projects = models.IntegerField(default=0)
    limit_source_strings = models.IntegerField(default=0)
    limit_hosted_words = models.IntegerField(default=0)
    limit_hosted_strings = models.IntegerField(default=0)
    created = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True)
    hosted_billing = models.IntegerField(default=0, db_index=True)
    discoverable = models.BooleanField(default=False)
    site_url = models.URLField(
        verbose_name=gettext_lazy("Server URL"), default="", blank=True
    )
    site_title = models.TextField(default="Weblate")
    site_version = models.TextField(default="", blank=True)
    site_users = models.IntegerField(default=0)
    site_projects = models.IntegerField(default=0)

    discover_text = models.CharField(
        verbose_name=gettext_lazy("Server description"), max_length=200, blank=True
    )
    discover_image = models.ImageField(
        verbose_name=gettext_lazy("Server image"),
        blank=True,
        upload_to="discover/",
        help_text=gettext_lazy(
            "PNG or JPG image with a resolution of 570 x 260 pixels."
        ),
        validators=[
            FileExtensionValidator(["jpg", "jpeg", "png"]),
            validate_bitmap,
        ],
    )

    # Discover integration
    matched_projects: list[Project]
    non_matched_projects_count: int

    class Meta:
        verbose_name = "Customer service"
        verbose_name_plural = "Customer services"

    def __str__(self):
        return f"Service:{self.pk} {self.get_status_display()}: {self.user_emails}: {self.site_domain}"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.was_created = False

    def get_absolute_url(self):
        return reverse("crm:service-detail", kwargs={"pk": self.pk})

    def get_discover_text(self):
        return _(self.discover_text)

    @property
    def site_domain(self) -> str:
        """Extract domain from site_url."""
        parts = self.site_url.split("//")
        if len(parts) > 1:
            return parts[1]
        return self.site_url

    @property
    def needs_token(self):
        return (
            self.status not in {"hosted", "shared", "community"}
            or self.backup_subscriptions
        )

    def projects_limit(self):
        report = self.last_report
        if report:
            if self.limit_projects:
                return f"{report.projects}/{self.limit_projects}"
            return f"{report.projects}"
        return "0"

    projects_limit.short_description = "Projects"  # type: ignore[attr-defined]

    def languages_limit(self):
        report = self.last_report
        if report:
            if self.limit_languages:
                return f"{report.languages}/{self.limit_languages}"
            return f"{report.languages}"
        return "0"

    languages_limit.short_description = "Languages"  # type: ignore[attr-defined]

    def source_strings_limit(self):
        report = self.last_report
        if report:
            if self.limit_source_strings:
                return f"{report.source_strings}/{self.limit_source_strings}"
            return f"{report.source_strings}"
        return "0"

    source_strings_limit.short_description = "Source strings"  # type: ignore[attr-defined]

    def hosted_words_limit(self):
        report = self.last_report
        if report:
            if self.limit_hosted_words:
                return f"{report.hosted_words}/{self.limit_hosted_words}"
            return f"{report.hosted_words}"
        return "0"

    hosted_words_limit.short_description = "Hosted words"  # type: ignore[attr-defined]

    def hosted_strings_limit(self):
        report = self.last_report
        if report:
            if self.limit_hosted_strings:
                return f"{report.hosted_strings}/{self.limit_hosted_strings}"
            return f"{report.hosted_strings}"
        return "0"

    hosted_strings_limit.short_description = "Hosted strings"  # type: ignore[attr-defined]

    @cached_property
    def user_emails(self) -> str:
        if not self.pk or not self.customer:
            return ""
        return ", ".join(self.customer.get_notify_emails())

    @cached_property
    def last_report(self) -> Report | None:
        try:
            return self.report_set.latest("timestamp")
        except Report.DoesNotExist:
            return None

    @cached_property
    def hosted_subscriptions(self) -> models.QuerySet[Subscription]:
        return self.subscription_set.filter(
            package__category=PackageCategory.PACKAGE_DEDICATED
        ).order_by("-expires")

    @cached_property
    def shared_subscriptions(self) -> models.QuerySet[Subscription]:
        return self.subscription_set.filter(
            package__category=PackageCategory.PACKAGE_SHARED
        ).order_by("-expires")

    @cached_property
    def basic_subscriptions(self) -> models.QuerySet[Subscription]:
        return self.subscription_set.filter(package__name="basic").order_by("-expires")

    @cached_property
    def extended_subscriptions(self) -> models.QuerySet[Subscription]:
        return self.subscription_set.filter(package__name="extended").order_by(
            "-expires"
        )

    @cached_property
    def premium_subscriptions(self) -> models.QuerySet[Subscription]:
        return self.subscription_set.filter(package__name="premium").order_by(
            "-expires"
        )

    @cached_property
    def support_subscriptions(self) -> models.QuerySet[Subscription]:
        return (
            self.hosted_subscriptions
            | self.shared_subscriptions
            | self.basic_subscriptions
            | self.extended_subscriptions
            | self.premium_subscriptions
        ).order_by("-expires")

    @cached_property
    def backup_subscriptions(self) -> models.QuerySet[Subscription]:
        return self.subscription_set.filter(package__name="backup").order_by("-expires")

    @cached_property
    def latest_subscription(self) -> Subscription | None:
        try:
            return self.support_subscriptions[0]
        except IndexError:
            return None

    @cached_property
    def current_subscription(self) -> Subscription | None:
        if (
            self.latest_subscription is None
            or self.latest_subscription.expires < timezone.now()
        ):
            return None
        return self.latest_subscription

    @cached_property
    def expires(self):
        if self.current_subscription is None:
            return timezone.now()
        return self.current_subscription.expires

    def get_suggestions(self):
        result = []
        if not self.support_subscriptions:
            result.append(
                (
                    "basic",
                    _("Basic support"),
                    _("Never get held back by a problem."),
                    _("Set priority for all your questions and reported bugs."),
                    "img/Support-Basic.svg",
                    _("Get support"),
                )
            )

        if not self.hosted_subscriptions and not self.shared_subscriptions:
            if not self.premium_subscriptions:
                result.append(
                    (
                        "premium",
                        _("Premium support"),
                        _("Don’t wait with your work on hold."),
                        _("This guarantees you answers the NBD at the latest."),
                        "img/Support-Premium.svg",
                        _("Be Premium"),
                    )
                )

            if not self.extended_subscriptions:
                result.append(
                    (
                        "extended",
                        _("Extended support"),
                        _("Don’t settle with Basic, get a worry-free package."),
                        _("We will manage upgrades for you."),
                        "img/Support-Plus.svg",
                        _("Stay updated"),
                    )
                )

            if not self.backup_subscriptions:
                result.append(
                    (
                        "backup",
                        _("Backup service"),
                        _("Easily put your backups in a safe place."),
                        _("Encrypted and automatic, always available."),
                        "img/Support-Backup.svg",
                        _("Back up daily"),
                    )
                )
        if (
            self.hosted_subscriptions
            and self.hosted_subscriptions[0].package.name in PACKAGE_UPGRADES
        ):
            package_name = PACKAGE_UPGRADES[self.hosted_subscriptions[0].package.name]
            package = Package.objects.get(name=package_name)
            result.append(
                (
                    package.name,
                    _("Upgrade to %s") % package,
                    _("Increase service limits to translate more content."),
                    "",
                    "img/Support-Plus.svg",
                    _("Upgrade"),
                )
            )

        return result

    def update_status(self):
        status = "community"
        package = Package.objects.get(name="community")
        if self.hosted_subscriptions.filter(expires__gt=timezone.now()).exists():
            status = "hosted"
            package = self.hosted_subscriptions.latest("expires").package
        elif self.shared_subscriptions.filter(expires__gt=timezone.now()).exists():
            status = "shared"
            package = self.shared_subscriptions.latest("expires").package
        elif self.premium_subscriptions.filter(expires__gt=timezone.now()).exists():
            status = "premium"
        elif self.extended_subscriptions.filter(expires__gt=timezone.now()).exists():
            status = "extended"
        elif self.basic_subscriptions.filter(expires__gt=timezone.now()).exists():
            status = "basic"

        if (
            status != self.status
            or package.limit_source_strings != self.limit_source_strings
            or package.limit_hosted_words != self.limit_hosted_words
            or package.limit_hosted_strings != self.limit_hosted_strings
        ):
            self.status = status
            self.limit_source_strings = package.limit_source_strings
            self.limit_hosted_words = package.limit_hosted_words
            self.limit_hosted_strings = package.limit_hosted_strings
            self.limit_languages = package.limit_languages
            self.limit_projects = package.limit_projects
            self.save()

    def has_paid_backup(self) -> bool:
        subscriptions = self.hosted_subscriptions | self.backup_subscriptions
        return subscriptions.filter(expires__gt=timezone.now()).exists()

    def create_backup(self):
        if (
            not self.backup_repository
            and self.has_paid_backup()
            and (last_report := self.last_report)
        ):
            self.create_backup_repository(last_report)

    def create_backup_repository(self, last_report: Report):
        """
        Configure backup repository.

        - create filesystem folders
        - store SSH key
        - create subaccount
        """
        dirname = str(uuid4())

        # Create folder and SSH key
        create_storage_folder(dirname, self, self.customer, last_report)

        # Create account on the service
        data = create_storage_subaccount(dirname, self)

        self.backup_repository = generate_ssh_url(data)
        self.backup_box = settings.STORAGE_BOX
        self.backup_directory = dirname
        self.save(update_fields=["backup_repository", "backup_box", "backup_directory"])

    def get_limits(self):
        return {
            "hosted_words": self.limit_hosted_words,
            "hosted_strings": self.limit_hosted_strings,
            "source_strings": self.limit_source_strings,
            "projects": self.limit_projects,
            "languages": self.limit_languages,
        }

    def check_in_limits(self):
        last_report = self.last_report
        return last_report is not None and (
            (
                not self.limit_hosted_strings
                or last_report.hosted_strings <= self.limit_hosted_strings
            )
            and (
                not self.limit_hosted_words
                or last_report.hosted_words <= self.limit_hosted_words
            )
            and (
                not self.limit_source_strings
                or last_report.source_strings <= self.limit_source_strings
            )
            and (not self.limit_projects or last_report.projects <= self.limit_projects)
            and (
                not self.limit_languages
                or last_report.languages <= self.limit_languages
            )
        )

    def regenerate(self):
        self.secret = generate_secret()
        self.save(update_fields=["secret"])


class Subscription(models.Model):
    service = models.ForeignKey(Service, on_delete=models.deletion.PROTECT)
    payment = Char32UUIDField(blank=True, null=True)
    package = models.ForeignKey(Package, on_delete=models.deletion.PROTECT)
    created = models.DateTimeField(auto_now_add=True)
    expires = models.DateTimeField()
    enabled = models.BooleanField(default=True, blank=True)

    class Meta:
        verbose_name = "Customer’s subscription"
        verbose_name_plural = "Customer’s subscriptions"

    def __str__(self):
        return f"{self.package}: {self.service}"

    def save(  # type: ignore[override]
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using=None,
        update_fields=None,
    ):
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
        self.service.update_status()

    def get_absolute_url(self):
        return reverse("subscription-view", kwargs={"pk": self.pk})

    @cached_property
    def yearly_package(self):
        if self.package.name.endswith("-m"):
            return Package.objects.get(name=self.package.name[:-2])
        return None

    def active(self):
        return self.expires >= timezone.now()

    @property
    def price(self):
        return self.package.price

    @cached_property
    def payment_obj(self):
        return Payment.objects.get(pk=self.payment)

    def list_payments(self):
        # pylint: disable=no-member
        past = set(self.pastpayments_set.values_list("payment", flat=True))
        query = Q(pk=self.payment)
        if past:
            query |= Q(pk__in=past)
            query |= Q(repeat__pk__in=past)
        if self.payment:
            query |= Q(repeat__pk=self.payment)
        return Payment.objects.filter(query).distinct()

    def send_notification(self, notification):
        send_notification(
            notification, self.service.customer.get_notify_emails(), subscription=self
        )
        with override("en"):
            send_notification(
                notification,
                settings.NOTIFY_SUBSCRIPTION,
                subscription=self,
            )

    def could_be_obsolete(self):
        expires = timezone.now() + timedelta(days=3)
        return (
            self.package.name in {"basic", "extended", "premium"}
            and self.service.support_subscriptions.exclude(pk=self.pk)
            .filter(expires__gt=expires)
            .exists()
        )

    def add_payment(self, invoice: Invoice, period: str):
        # Calculate new expiry
        start = self.expires + timedelta(days=1)
        end = start - timedelta(days=1) + get_period_delta(period)

        # Fetch customer object from last payment here
        if self.payment:
            customer = self.payment_obj.customer
        elif invoice.invoice["contact"].startswith("web-"):
            customer = Customer.objects.get(
                pk=invoice.invoice["contact"].replace("web-", "")
            )
        else:
            customer = Customer.objects.create(
                vat=invoice.contact.get("vat_reg", None),
                name=invoice.contact["name"],
                address=invoice.contact["address"],
                city=invoice.contact["city"],
                country=countries.by_name(invoice.contact["country"]),
                user_id=-1,
                origin="https://weblate.org/auto",
            )

        # Create payment based on the invoice and customer
        payment = Payment.objects.create(
            amount=float(invoice.amount),
            currency=Payment.CURRENCY_EUR,
            description=invoice.invoice["item"],
            state=Payment.PROCESSED,
            backend="manual",
            invoice=invoice.invoiceid,
            start=start,
            end=end,
            customer=customer,
        )

        # Move current payment to past payments
        if self.payment:
            self.pastpayments_set.create(payment=self.payment)

        # Update current payment info
        self.payment = payment.pk
        # Extend validity for period
        self.expires = end
        self.save()


class PastPayments(models.Model):
    subscription = models.ForeignKey(
        Subscription, on_delete=models.deletion.PROTECT, null=True, blank=True
    )
    donation = models.ForeignKey(
        Donation, on_delete=models.deletion.PROTECT, null=True, blank=True
    )
    payment = Char32UUIDField()

    class Meta:
        verbose_name = "Past payment"
        verbose_name_plural = "Past payments"

    def __str__(self):
        return f"{self.subscription}: {self.payment}"


class Report(models.Model):
    service = models.ForeignKey(Service, on_delete=models.deletion.CASCADE)
    site_url = models.URLField(default="")
    site_title = models.TextField(default="")
    version = models.TextField(default="")
    ssh_key = models.TextField(default="")
    users = models.IntegerField(default=0)
    projects = models.IntegerField(default=0)
    components = models.IntegerField(default=0)
    languages = models.IntegerField(default=0)
    source_strings = models.IntegerField(default=0)
    hosted_strings = models.IntegerField(default=0)
    hosted_words = models.IntegerField(default=0)
    timestamp = models.DateTimeField(auto_now_add=True)
    discoverable = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Weblate report"
        verbose_name_plural = "Weblate reports"

    def __str__(self):
        return self.site_url

    def save(  # type: ignore[override]
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using=None,
        update_fields=None,
    ):
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
        self.service.discoverable = self.discoverable
        self.service.site_url = self.site_url
        self.service.site_title = self.site_title
        self.service.site_version = self.version
        self.service.site_users = self.users
        self.service.site_projects = self.projects
        self.service.save(
            update_fields=[
                "discoverable",
                "site_url",
                "site_title",
                "site_version",
                "site_users",
                "site_projects",
            ]
        )


class Project(models.Model):
    service = models.ForeignKey(Service, on_delete=models.deletion.CASCADE)

    name = models.CharField(max_length=60, db_index=True)
    url = models.CharField(max_length=120)
    web = models.URLField()

    class Meta:
        verbose_name = "Weblate project"
        verbose_name_plural = "Weblate projects"

    def __str__(self):
        return f"{self.service.site_title}: {self.name}"
