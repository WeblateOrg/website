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

from django.conf import settings
from django.contrib.staticfiles import finders
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration

SIGNATURE_URL = "signature:"
INVOICES_URL = "invoices:"
LEGAL_URL = "legal:"
STATIC_URL = "static:"
INVOICES_TEMPLATES_PATH = Path(__file__).parent / "invoices" / "templates"
LEGAL_TEMPLATES_PATH = Path(__file__).parent / "legal" / "templates"


def url_fetcher(url: str) -> dict[str, str | bytes]:
    path_obj: Path
    result: dict[str, str | bytes]

    if url == SIGNATURE_URL:
        if settings.AGREEMENTS_SIGNATURE_PATH is None:
            raise ValueError("Signature not configured!")
        path_obj = settings.AGREEMENTS_SIGNATURE_PATH
    elif url.startswith(INVOICES_URL):
        path_obj = INVOICES_TEMPLATES_PATH / url.removeprefix(INVOICES_URL)
    elif url.startswith(LEGAL_URL):
        path_obj = LEGAL_TEMPLATES_PATH / url.removeprefix(LEGAL_URL)
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
    fonts_css = finders.find("pdf/fonts.css")
    if fonts_css is None:
        raise ValueError("Could not load fonts CSS")
    font_style = CSS(
        filename=fonts_css,
        font_config=font_config,
        url_fetcher=url_fetcher,
    )
    renderer.write_pdf(
        output,
        stylesheets=[font_style],
        font_config=font_config,
    )
