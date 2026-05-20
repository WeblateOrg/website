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

from typing import TYPE_CHECKING, Any

from django.templatetags.static import static
from django.utils.translation import override

from weblate_web.const import COMPANY_INFO_EMAIL, COMPANY_NAME

if TYPE_CHECKING:
    from weblate_web.models import Post

SITE_URL = "https://weblate.org"
SITE_NAME = "Weblate"
ORGANIZATION_ID = f"{SITE_URL}/#organization"
WEBSITE_ID = f"{SITE_URL}/#website"

SAME_AS_URLS = (
    "https://fosstodon.org/@weblate",
    "https://www.linkedin.com/company/weblate/",
    "https://x.com/WeblateOrg",
    "https://www.facebook.com/WeblateOrg",
    "https://github.com/WeblateOrg",
)


def absolute_site_url(path: str) -> str:
    if path.startswith(("https://", "http://")):
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{SITE_URL}{path}"


def canonical_post_path(post: Post) -> str:
    with override("en"):
        path = post.get_absolute_url()
    if path.startswith("/en/"):
        return path[3:]
    return path


def get_organization_schema() -> dict[str, Any]:
    return {
        "@type": "Organization",
        "@id": ORGANIZATION_ID,
        "name": COMPANY_NAME,
        "alternateName": SITE_NAME,
        "url": f"{SITE_URL}/",
        "logo": absolute_site_url(static("weblate-512.png")),
        "email": COMPANY_INFO_EMAIL,
        "sameAs": list(SAME_AS_URLS),
    }


def get_website_schema(language: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "@type": "WebSite",
        "@id": WEBSITE_ID,
        "name": SITE_NAME,
        "url": f"{SITE_URL}/",
        "publisher": {"@id": ORGANIZATION_ID},
    }
    if language:
        result["inLanguage"] = language
    return result


def get_site_schema(language: str | None = None) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@graph": [
            get_organization_schema(),
            get_website_schema(language),
        ],
    }


def get_post_author_schema(post: Post) -> dict[str, str]:
    if not post.author_id or post.author is None:
        return {"@id": ORGANIZATION_ID}

    name = post.author.get_full_name() or post.author.last_name or post.author.username
    return {
        "@type": "Person",
        "name": name,
    }


def get_blog_post_schema(post: Post) -> dict[str, Any]:
    url = absolute_site_url(canonical_post_path(post))
    result: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "@id": f"{url}#blogposting",
        "headline": post.title,
        "description": post.summary,
        "url": url,
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": url,
        },
        "author": get_post_author_schema(post),
        "publisher": {"@id": ORGANIZATION_ID},
        "isPartOf": {"@id": WEBSITE_ID},
        "datePublished": post.timestamp,
        "dateModified": post.timestamp,
        "inLanguage": "en",
    }

    topic = str(post.get_topic_display())
    if topic:
        result["articleSection"] = topic

    if post.image_id and post.image is not None:
        result["image"] = absolute_site_url(post.image.image.url)

    return result
