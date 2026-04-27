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

import html

# pylint: disable=protected-access
import re

import mistletoe
from django.utils.html import linebreaks
from mistletoe import span_token


class SkipHtmlSpan(span_token.HtmlSpan):
    """Strip raw HTML tags from Markdown input."""

    pattern = re.compile(  # pylint: disable=protected-access
        f"{span_token._open_tag}|{span_token._closing_tag}"
    )
    parse_inner = False
    content: str

    def __init__(self, match) -> None:
        super().__init__(match)
        self.content = ""


class PlainAutoLink(span_token.AutoLink):
    """Autolink only plain HTTP(S) URLs."""

    pattern = re.compile(
        r"\b(https?://[A-Za-z0-9.!#$%&'*+/=?^_`{|}()~:-]*"
        r"[A-Za-z0-9/#%&=+_~:-])(?=\W|$)"
    )


class SafeHtmlRenderer(mistletoe.HtmlRenderer):
    """Render Markdown while rejecting raw HTML and unsafe URLs."""

    _allowed_url_re = re.compile(r"^https?://", re.IGNORECASE)
    _allowed_email_re = re.compile(
        r"^(mailto:)?[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    )

    def __init__(self) -> None:
        super().__init__(SkipHtmlSpan, PlainAutoLink, process_html_tokens=False)

    def render_skip_html_span(self, token: SkipHtmlSpan) -> str:
        return token.content

    def render_plain_auto_link(self, token: PlainAutoLink) -> str:
        return self.render_auto_link(token)

    def render_link(self, token: span_token.Link) -> str:
        if self.check_url(token.target):
            return super().render_link(token)
        return self.escape_html_text(f"[{self.render_to_plain(token)}]({token.target})")

    def render_auto_link(self, token: span_token.AutoLink | PlainAutoLink) -> str:
        if self.check_url(token.target) or self.check_email(token.target):
            return super().render_auto_link(token)
        return self.escape_html_text(f"<{token.target}>")

    def render_image(self, token: span_token.Image) -> str:
        if self.check_url(token.src):
            title = f' title="{html.escape(token.title)}"' if token.title else ""
            return (
                f'<img src="{self.escape_url(token.src)}" '
                f'alt="{self.render_to_plain(token)}"{title} />'
            )
        return self.escape_html_text(f"![{self.render_to_plain(token)}]({token.src})")

    def check_url(self, url: str) -> bool:
        return bool(self._allowed_url_re.match(url))

    def check_email(self, email: str) -> bool:
        return bool(self._allowed_email_re.match(email))


def render_markdown(text: str) -> str:
    """Render Markdown as safe HTML."""
    try:
        with SafeHtmlRenderer() as renderer:
            return renderer.render(mistletoe.Document(text))
    except Exception:  # pylint: disable=broad-exception-caught
        return linebreaks(text, autoescape=True)
