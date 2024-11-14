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

from pathlib import Path

from django.contrib.staticfiles import finders
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration

INVOICES_URL = "invoices:"
STATIC_URL = "static:"
TEMPLATES_PATH = Path(__file__).parent / "invoices" / "templates"


def url_fetcher(url: str) -> dict[str, str | bytes]:
    path_obj: Path
    result: dict[str, str | bytes]
    if url.startswith(INVOICES_URL):
        path_obj = TEMPLATES_PATH / url.removeprefix(INVOICES_URL)
    elif url.startswith(STATIC_URL):
        fullname = url.removeprefix(STATIC_URL)
        match = finders.find(fullname)
        if match is None:
            raise ValueError(f"Could not find {fullname}")
        path_obj = Path(match)
    else:
        raise ValueError(f"Usupported URL: {url}")
    result = {
        "filename": path_obj.name,
        "string": path_obj.read_bytes(),
    }
    if path_obj.suffix == ".css":
        result["mime_type"] = "text/css"
        result["encoding"] = "utf-8"
    return result


def render_pdf(*, html: str, output: Path) -> None:
    font_config = FontConfiguration()

    renderer = HTML(
        string=html,
        url_fetcher=url_fetcher,
    )
    font_style = CSS(
        string="""
        @font-face {
          font-family: Source Sans Pro;
          font-weight: 400;
          src: url("static:vendor/font-source/TTF/SourceSans3-Regular.ttf");
        }
        @font-face {
          font-family: Source Sans Pro;
          font-weight: 700;
          src: url("static:vendor/font-source/TTF/SourceSans3-Bold.ttf");
        }
    """,
        font_config=font_config,
        url_fetcher=url_fetcher,
    )
    renderer.write_pdf(
        output,
        stylesheets=[font_style],
        font_config=font_config,
    )
