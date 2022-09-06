#
# Copyright © 2012–2022 Michal Čihař <michal@cihar.com>
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
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from django.conf import settings
from django.utils.translation import get_language
from weblate_language_data.docs import DOCUMENTATION_LANGUAGES

SENTRY_KEY = "f4089b47246947759114d23fc884d56e"
SENTRY_URL = f"https://sentry.weblate.org/api/3/security/?sentry_key={SENTRY_KEY}"

CSP_TEMPLATE = (
    "default-src 'self'; "
    "style-src {style}; "
    "img-src {image}; "
    "script-src {script}; "
    "connect-src {connect}; "
    "object-src 'none'; "
    "font-src {font}; "
    "frame-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action {form};"
    "report-uri {report}"
)


class SecurityMiddleware:
    """Middleware that sets various security related headers.

    - Disables CSRF when payment secret is provided
    - Content-Security-Policy
    - X-XSS-Protection
    """

    def __init__(self, get_response=None):
        self.get_response = get_response

    def adjust_doc_links(self, response):
        lang = get_language()
        if lang in DOCUMENTATION_LANGUAGES:
            response.content = response.content.replace(
                b"https://docs.weblate.org/en/",
                f"https://docs.weblate.org/{DOCUMENTATION_LANGUAGES[lang]}/".encode(),
            )

    def __call__(self, request):
        # Skip CSRF validation for requests with valid secret
        # This is used to process automatic payments
        if request.POST.get("secret") == settings.PAYMENT_SECRET:
            request._dont_enforce_csrf_checks = True  # noqa: SF01

        response = self.get_response(request)
        if response["Content-Type"] == "text/html; charset=utf-8":
            self.adjust_doc_links(response)
        # No CSP for debug mode (to allow djdt or error pages)
        if settings.DEBUG:
            return responsef4089b47246947759114d23fc884d56e

        style = ["'self'", "s.weblate.org"]
        script = ["'self'"]
        connect = ["'self'"]
        image = ["'self'", "data:"]
        font = ["'self'", "s.weblate.org"]
        form = ["'self'", "weblate.org", "hosted.weblate.org"]

        # Sentry/Raven
        script.append("cdn.ravenjs.com")

        # Matomo/Piwik
        script.append("stats.cihar.com")
        image.append("stats.cihar.com")
        connect.append("stats.cihar.com")

        # Hosted Weblate widget
        image.append("hosted.weblate.org")

        # Old blog entries
        image.append("blog.cihar.com")

        # The Pay
        image.append("www.thepay.cz")
        form.append("www.thepay.cz")

        # GitHub avatars
        image.append("*.githubusercontent.com")

        response["Content-Security-Policy"] = CSP_TEMPLATE.format(
            style=" ".join(style),
            image=" ".join(image),
            script=" ".join(script),
            font=" ".join(font),
            connect=" ".join(connect),
            form=" ".join(form),
            report=SENTRY_URL,
        )
        response["Expect-CT"] = f'max-age=86400, enforce, report-uri="{SENTRY_URL}"'
        response["X-XSS-Protection"] = "1; mode=block"
        # Opt-out from Google FLoC
        response["Permissions-Policy"] = "interest-cohort=()"
        return response
