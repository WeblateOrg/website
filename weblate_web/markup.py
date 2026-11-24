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

from django.utils.html import linebreaks
from markdown_it import MarkdownIt


def render_markdown(text: str) -> str:
    """Render Markdown as safe HTML."""
    try:
        return MarkdownIt("js-default", {"linkify": True}).render(text)
    except Exception:  # pylint: disable=broad-exception-caught
        return linebreaks(text, autoescape=True)
