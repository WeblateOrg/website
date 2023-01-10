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
from django.conf.urls import include, url
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LogoutView
from django.contrib.sitemaps import Sitemap
from django.contrib.syndication.views import Feed
from django.urls import path
from django.utils import timezone
from django.views.decorators.cache import cache_page
from django.views.generic import RedirectView, TemplateView

from weblate_web.models import Post
from weblate_web.views import (
    AddDiscoveryView,
    CompleteView,
    CustomerView,
    DiscoverView,
    DonateView,
    EditDiscoveryView,
    EditLinkView,
    MilestoneArchiveView,
    NewsArchiveView,
    NewsView,
    PaymentView,
    PostView,
    TopicArchiveView,
    activity_svg,
    api_hosted,
    api_support,
    api_user,
    disable_repeat,
    donate_pay,
    download_invoice,
    fetch_vat,
    not_found,
    process_payment,
    server_error,
    service_token,
    service_user,
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
        # pylint: disable=no-self-use
        return Post.objects.filter(timestamp__lt=timezone.now()).order_by("-timestamp")[
            :10
        ]

    def item_title(self, item):
        # pylint: disable=no-self-use
        return item.title

    def item_description(self, item):
        # pylint: disable=no-self-use
        return item.body.rendered

    def item_pubdate(self, item):
        # pylint: disable=no-self-use
        return item.timestamp


class PagesSitemap(Sitemap):
    """Sitemap of static pages for one language."""

    def __init__(self, language):
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
            ("/news/", 0.9, "daily"),
        )

    def location(self, obj):
        return f"/{self.language}{obj[0]}"

    def priority(self, obj):
        if self.language == "en":
            return obj[1]
        return obj[1] * 3 / 4

    def changefreq(self, obj):
        # pylint: disable=no-self-use
        return obj[2]


class NewsSitemap(Sitemap):
    priority = 0.8

    def items(self):
        # pylint: disable=no-self-use
        return Post.objects.filter(timestamp__lt=timezone.now()).order_by("-timestamp")

    def lastmod(self, item):
        # pylint: disable=no-self-use
        return item.timestamp


# create each section in all languages
SITEMAPS = {lang[0]: PagesSitemap(lang[0]) for lang in settings.LANGUAGES}
SITEMAPS["news"] = NewsSitemap()
UUID = r"(?P<pk>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"


urlpatterns = i18n_patterns(
    url(r"^$", TemplateView.as_view(template_name="index.html"), name="home"),
    url(
        r"^features/$",
        TemplateView.as_view(template_name="features.html"),
        name="features",
    ),
    url(r"^tour/$", RedirectView.as_view(url="/hosting/", permanent=True)),
    url(
        r"^download/$",
        TemplateView.as_view(template_name="download.html"),
        name="download",
    ),
    url(r"^try/$", RedirectView.as_view(url="/hosting/", permanent=True)),
    url(
        r"^hosting/$",
        TemplateView.as_view(template_name="hosting.html"),
        name="hosting",
    ),
    url(
        r"^discover/$",
        DiscoverView.as_view(),
        name="discover",
    ),
    url(r"^hosting/free/$", RedirectView.as_view(url="/hosting/", permanent=True)),
    url(r"^hosting/ordered/$", RedirectView.as_view(url="/hosting/", permanent=True)),
    url(
        r"^contribute/$",
        TemplateView.as_view(template_name="contribute.html"),
        name="contribute",
    ),
    url(
        r"^user/$",
        login_required(TemplateView.as_view(template_name="user.html")),
        name="user",
    ),
    url(r"^donate/$", TemplateView.as_view(template_name="donate.html"), name="donate"),
    url(r"^donate/process/$", process_payment, name="donate-process"),
    url(r"^donate/new/$", DonateView.as_view(), name="donate-new"),
    url(r"^donate/edit/(?P<pk>[0-9]+)/$", EditLinkView.as_view(), name="donate-edit"),
    url(r"^donate/pay/(?P<pk>[0-9]+)/$", donate_pay, name="donate-pay"),
    url(r"^user/invoice/" + UUID + "/$", download_invoice, name="user-invoice"),
    url(r"^donate/disable/(?P<pk>[0-9]+)/$", disable_repeat, name="donate-disable"),
    url(
        r"^subscription/disable/(?P<pk>[0-9]+)/$",
        subscription_disable_repeat,
        name="subscription-disable",
    ),
    url(r"^subscription/token/(?P<pk>[0-9]+)/$", service_token, name="service-token"),
    url(r"^subscription/users/(?P<pk>[0-9]+)/$", service_user, name="service-user"),
    url(
        r"^subscription/discovery/(?P<pk>[0-9]+)/$",
        EditDiscoveryView.as_view(),
        name="service-discovery",
    ),
    url(
        r"^subscription/discovery/$",
        AddDiscoveryView.as_view(),
        name="service-discovery-add",
    ),
    url(
        r"^subscription/pay/(?P<pk>[0-9]+)/$", subscription_pay, name="subscription-pay"
    ),
    url(
        r"^subscription/view/(?P<pk>[0-9]+)/$",
        subscription_view,
        name="subscription-view",
    ),
    url(r"^subscription/new/$", subscription_new, name="subscription-new"),
    url(r"^news/$", NewsView.as_view(), name="news"),
    url(r"^news/archive/$", NewsArchiveView.as_view(), name="news-archive"),
    url(
        r"^news/topic/milestone/$",
        MilestoneArchiveView.as_view(),
        name="milestone-archive",
    ),
    url(
        r"^news/topic/(?P<slug>[-a-zA-Z0-9_]+)/$",
        TopicArchiveView.as_view(),
        name="topic-archive",
    ),
    url(r"^news/archive/(?P<slug>[-a-zA-Z0-9_]+)/$", PostView.as_view(), name="post"),
    url(r"^about/$", TemplateView.as_view(template_name="about.html"), name="about"),
    url(
        r"^careers/$",
        TemplateView.as_view(template_name="careers.html"),
        name="careers",
    ),
    url(
        r"^support/$",
        TemplateView.as_view(template_name="support.html"),
        name="support",
    ),
    url(r"^thanks/$", RedirectView.as_view(url="/donate/", permanent=True)),
    url(r"^terms/$", TemplateView.as_view(template_name="terms.html"), name="terms"),
    url(r"^payment/" + UUID + "/$", PaymentView.as_view(), name="payment"),
    url(
        r"^payment/" + UUID + "/edit/$", CustomerView.as_view(), name="payment-customer"
    ),
    url(
        r"^payment/" + UUID + "/complete/$",
        CompleteView.as_view(),
        name="payment-complete",
    ),
    # FOSDEM short link
    url(
        r"^FOSDEM/|fosdem/$",
        RedirectView.as_view(
            url="/news/archive/meet-weblate-fosdem-2020/", permanent=False
        ),
    ),
    # Compatibility with disabled languages
    url(r"^[a-z][a-z]/$", RedirectView.as_view(url="/", permanent=False)),
    url(r"^[a-z][a-z]_[A-Z][A-Z]/$", RedirectView.as_view(url="/", permanent=False)),
    # Broken links
    url(r"^https?:/.*$", RedirectView.as_view(url="/", permanent=True)),
    url(r"^index\.html$", RedirectView.as_view(url="/", permanent=True)),
    url(r"^index\.([a-z][a-z])\.html$", RedirectView.as_view(url="/", permanent=True)),
    url(r"^[a-z][a-z]/index\.html$", RedirectView.as_view(url="/", permanent=True)),
    url(
        r"^[a-z][a-z]_[A-Z][A-Z]/index\.html$",
        RedirectView.as_view(url="/", permanent=True),
    ),
) + [
    url(
        r"^sitemap\.xml$",
        cache_page(3600)(django.contrib.sitemaps.views.index),
        {"sitemaps": SITEMAPS, "sitemap_url_name": "sitemap"},
        name="sitemap-index",
    ),
    url(
        r"^sitemap-(?P<section>.+)\.xml$",
        cache_page(1800)(django.contrib.sitemaps.views.sitemap),
        {"sitemaps": SITEMAPS},
        name="sitemap",
    ),
    path("feed/", LatestEntriesFeed(), name="feed"),
    url(r"^js/vat/$", fetch_vat),
    url(r"^api/support/$", api_support),
    url(r"^api/user/$", api_user),
    url(r"^api/hosted/$", api_hosted),
    url(r"^img/activity.svg$", activity_svg),
    url(r"^logout/$", LogoutView.as_view(next_page="/"), name="logout"),
    # Aliases for static files
    url(
        r"^(android-chrome|favicon)-(?P<size>192|512)x(?P=size)\.png$",
        RedirectView.as_view(
            url=settings.STATIC_URL + "weblate-%(size)s.png", permanent=True
        ),
    ),
    url(
        r"^apple-touch-icon\.png$",
        RedirectView.as_view(
            url=settings.STATIC_URL + "weblate-180.png", permanent=True
        ),
    ),
    url(
        r"^(?P<name>favicon\.ico|robots\.txt)$",
        RedirectView.as_view(url=settings.STATIC_URL + "%(name)s", permanent=True),
    ),
    url(
        r"^browserconfig\.xml$", TemplateView.as_view(template_name="browserconfig.xml")
    ),
    url(r"^site\.webmanifest$", TemplateView.as_view(template_name="site.webmanifest")),
    url(
        r"^security\.txt$",
        RedirectView.as_view(url="/.well-known/security.txt", permanent=True),
    ),
    url(
        r"^\.well-known/security\.txt$",
        TemplateView.as_view(template_name="security.txt", content_type="text/plain"),
    ),
    url(
        r"^\.well-known/keybase\.txt$",
        TemplateView.as_view(template_name="keybase.txt", content_type="text/plain"),
    ),
    # SAML
    url(r"^saml2/", include("djangosaml2.urls")),
    # Admin
    url(
        r"^admin/login/$",
        RedirectView.as_view(
            pattern_name="saml2_login", permanent=True, query_string=True
        ),
    ),
    url(r"^admin/", admin.site.urls),
    # Media files on devel server
    url(
        r"^media/(?P<path>.*)$",
        django.views.static.serve,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
handler404 = not_found
handler500 = server_error
