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

import django.contrib.sitemaps.views
import django.views.static
from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.contrib.sitemaps import Sitemap
from django.contrib.syndication.views import Feed
from django.urls import include, path, re_path
from django.utils import timezone
from django.views.decorators.cache import cache_page
from django.views.generic import RedirectView, TemplateView

import weblate_web.crm.urls
from weblate_web.invoices.views import download_invoice, pay_invoice
from weblate_web.models import Post
from weblate_web.views import (
    AddDiscoveryView,
    CompleteView,
    CustomerDPAView,
    CustomerView,
    DiscoverView,
    DonateView,
    EditCustomerView,
    EditDiscoveryView,
    EditLinkView,
    HostingView,
    MilestoneArchiveView,
    NewsArchiveView,
    NewsView,
    PaymentView,
    PostView,
    SupportView,
    TopicArchiveView,
    UserView,
    activity_svg,
    agreement_download_view,
    api_hosted,
    api_support,
    api_user,
    customer_user,
    disable_repeat,
    donate_pay,
    download_payment_invoice,
    fetch_vat,
    fosdem_donation,
    not_found,
    process_payment,
    server_error,
    service_token,
    subscription_disable_repeat,
    subscription_new,
    subscription_pay,
    subscription_view,
)


class LatestEntriesFeed(Feed):
    title = "Weblate blog"
    link = "/news/"
    description = "News about Weblate and localization."

    def items(self):
        return Post.objects.filter(timestamp__lt=timezone.now()).order_by("-timestamp")[
            :10
        ]

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.body.rendered

    def item_pubdate(self, item):
        return item.timestamp


class PagesSitemap(Sitemap):
    """Sitemap of static pages for one language."""

    def __init__(self, language) -> None:
        super().__init__()
        self.language = language

    def items(self):
        return (
            ("/", 1.0, "weekly"),
            ("/features/", 0.9, "weekly"),
            ("/download/", 0.5, "daily"),
            ("/try/", 0.5, "weekly"),
            ("/hosting/", 0.8, "monthly"),
            ("/contribute/", 0.7, "monthly"),
            ("/donate/", 0.7, "weekly"),
            ("/discover/", 0.7, "weekly"),
            ("/careers/", 0.7, "weekly"),
            ("/support/", 0.7, "monthly"),
            ("/terms/", 0.2, "monthly"),
            ("/privacy/", 0.2, "monthly"),
            ("/news/", 0.9, "daily"),
        )

    def location(self, item) -> str:
        return f"/{self.language}{item[0]}"

    def priority(self, item):
        if self.language == "en":
            return item[1]
        return item[1] * 3 / 4

    def changefreq(self, obj):
        return obj[2]


class NewsSitemap(Sitemap):
    priority = 0.8

    def items(self):
        return Post.objects.filter(timestamp__lt=timezone.now()).order_by("-timestamp")

    def lastmod(self, item):
        return item.timestamp


# create each section in all languages
SITEMAPS: dict[str, Sitemap] = {
    lang[0]: PagesSitemap(lang[0]) for lang in settings.LANGUAGES
}
SITEMAPS["news"] = NewsSitemap()


urlpatterns = [
    *i18n_patterns(
        path("", TemplateView.as_view(template_name="index.html"), name="home"),
        path(
            "features/",
            TemplateView.as_view(template_name="features.html"),
            name="features",
        ),
        path("tour/", RedirectView.as_view(url="/hosting/", permanent=True)),
        path(
            "download/",
            TemplateView.as_view(template_name="download.html"),
            name="download",
        ),
        path("try/", RedirectView.as_view(url="/hosting/", permanent=True)),
        path("hosting/", HostingView.as_view(), name="hosting"),
        path("discover/", DiscoverView.as_view(), name="discover"),
        path("hosting/free/", RedirectView.as_view(url="/hosting/", permanent=True)),
        path("hosting/ordered/", RedirectView.as_view(url="/hosting/", permanent=True)),
        path(
            "contribute/",
            TemplateView.as_view(template_name="contribute.html"),
            name="contribute",
        ),
        path("user/", UserView.as_view(), name="user"),
        path(
            "donate/", TemplateView.as_view(template_name="donate.html"), name="donate"
        ),
        path("donate/process/", process_payment, name="donate-process"),
        path("donate/new/", DonateView.as_view(), name="donate-new"),
        path("donate/edit/<int:pk>/", EditLinkView.as_view(), name="donate-edit"),
        path("donate/pay/<int:pk>/", donate_pay, name="donate-pay"),
        path("user/invoice/<uuid:pk>/", download_payment_invoice, name="user-invoice"),
        path("donate/disable/<int:pk>/", disable_repeat, name="donate-disable"),
        path(
            "subscription/disable/<int:pk>/",
            subscription_disable_repeat,
            name="subscription-disable",
        ),
        path("subscription/token/<int:pk>/", service_token, name="service-token"),
        path(
            "subscription/discovery/<int:pk>/",
            EditDiscoveryView.as_view(),
            name="service-discovery",
        ),
        path(
            "subscription/discovery/",
            AddDiscoveryView.as_view(),
            name="service-discovery-add",
        ),
        path("subscription/pay/<int:pk>/", subscription_pay, name="subscription-pay"),
        path(
            "subscription/view/<int:pk>/",
            subscription_view,
            name="subscription-view",
        ),
        path("subscription/new/", subscription_new, name="subscription-new"),
        path("news/", NewsView.as_view(), name="news"),
        path("news/archive/", NewsArchiveView.as_view(), name="news-archive"),
        path(
            "news/topic/milestone/",
            MilestoneArchiveView.as_view(),
            name="milestone-archive",
        ),
        path(
            "news/topic/<slug:slug>/", TopicArchiveView.as_view(), name="topic-archive"
        ),
        path("news/archive/<slug:slug>/", PostView.as_view(), name="post"),
        path("about/", TemplateView.as_view(template_name="about.html"), name="about"),
        path(
            "careers/",
            TemplateView.as_view(template_name="careers.html"),
            name="careers",
        ),
        path("support/", SupportView.as_view(), name="support"),
        path("thanks/", RedirectView.as_view(url="/donate/", permanent=True)),
        path("terms/", TemplateView.as_view(template_name="terms.html"), name="terms"),
        path(
            "privacy/",
            TemplateView.as_view(template_name="privacy.html"),
            name="privacy",
        ),
        path("payment/<uuid:pk>/", PaymentView.as_view(), name="payment"),
        path(
            "payment/<uuid:pk>/edit/", CustomerView.as_view(), name="payment-customer"
        ),
        path(
            "payment/<uuid:pk>/complete/",
            CompleteView.as_view(),
            name="payment-complete",
        ),
        path("customer/<int:pk>/", EditCustomerView.as_view(), name="edit-customer"),
        path("customer/<int:pk>/users/", customer_user, name="customer-user"),
        path(
            "customer/<int:pk>/agreement/",
            CustomerDPAView.as_view(),
            name="customer-agreement",
        ),
        path(
            "agreement/<int:pk>/pdf/",
            agreement_download_view,
            name="agreement-download",
        ),
        path("invoice/<uuid:pk>/pdf/", download_invoice, name="invoice-pdf"),
        path("invoice/<uuid:pk>/pay/", pay_invoice, name="invoice-pay"),
        # FOSDEM short link
        re_path(
            r"^FOSDEM/|fosdem/$",
            RedirectView.as_view(url="/news/archive/fosdem-2026/", permanent=False),
        ),
        path("fosdem/donate/", fosdem_donation, name="fosdem-donate"),
        # Compatibility with disabled languages
        re_path(r"^[a-z][a-z]/$", RedirectView.as_view(url="/", permanent=False)),
        re_path(
            r"^[a-z][a-z]_[A-Z][A-Z]/$", RedirectView.as_view(url="/", permanent=False)
        ),
        # Broken links
        re_path(r"^https?:/.*$", RedirectView.as_view(url="/", permanent=True)),
        path("index.html", RedirectView.as_view(url="/", permanent=True)),
        re_path(
            r"^index\.([a-z][a-z])\.html$",
            RedirectView.as_view(url="/", permanent=True),
        ),
        re_path(
            r"^[a-z][a-z]/index\.html$", RedirectView.as_view(url="/", permanent=True)
        ),
        re_path(
            r"^[a-z][a-z]_[A-Z][A-Z]/index\.html$",
            RedirectView.as_view(url="/", permanent=True),
        ),
    ),
    path(
        "sitemap.xml",
        cache_page(3600)(django.contrib.sitemaps.views.index),
        {"sitemaps": SITEMAPS, "sitemap_url_name": "sitemap"},
        name="sitemap-index",
    ),
    path(
        "sitemap-<slug:section>.xml",
        cache_page(1800)(django.contrib.sitemaps.views.sitemap),
        {"sitemaps": SITEMAPS},
        name="sitemap",
    ),
    path("feed/", LatestEntriesFeed(), name="feed"),
    path("js/vat/", fetch_vat, name="js-vat"),
    path("api/support/", api_support),
    path("api/user/", api_user),
    path("api/hosted/", api_hosted),
    path("img/activity.svg", activity_svg),
    path("logout/", LogoutView.as_view(next_page="/"), name="logout"),
    # Aliases for static files
    re_path(
        r"^(android-chrome|favicon)-(?P<size>192|512)x(?P=size)\.png$",
        RedirectView.as_view(
            url=f"{settings.STATIC_URL}weblate-%(size)s.png", permanent=True
        ),
    ),
    path(
        "apple-touch-icon.png",
        RedirectView.as_view(
            url=f"{settings.STATIC_URL}weblate-180.png", permanent=True
        ),
    ),
    re_path(
        r"^(?P<name>favicon\.ico|robots\.txt)$",
        RedirectView.as_view(url=f"{settings.STATIC_URL}%(name)s", permanent=True),
    ),
    path(
        "browserconfig.xml",
        TemplateView.as_view(
            template_name="browserconfig.xml", content_type="application/xml"
        ),
    ),
    path(
        "site.webmanifest",
        TemplateView.as_view(
            template_name="site.webmanifest", content_type="application/json"
        ),
    ),
    path(
        "funding.json",
        TemplateView.as_view(
            template_name="funding.json", content_type="application/json"
        ),
    ),
    path(
        "security.txt",
        RedirectView.as_view(url="/.well-known/security.txt", permanent=True),
    ),
    path(
        ".well-known/security.txt",
        TemplateView.as_view(template_name="security.txt", content_type="text/plain"),
    ),
    path(
        ".well-known/keybase.txt",
        TemplateView.as_view(template_name="keybase.txt", content_type="text/plain"),
    ),
    # SAML
    path("saml2/", include("djangosaml2.urls")),
    # Admin
    path(
        "admin/login/"
        if settings.LOGIN_URL == "/saml2/login/"
        else "admin/login/saml2/",
        RedirectView.as_view(
            pattern_name="saml2_login", permanent=True, query_string=True
        ),
    ),
    path("admin/", admin.site.urls),
    path("crm/", include((weblate_web.crm.urls, "weblate_web.crm"), namespace="crm")),
    # Media files on devel server
    re_path(
        r"^media/(?P<path>.*)$",
        django.views.static.serve,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
handler404 = not_found
handler500 = server_error
