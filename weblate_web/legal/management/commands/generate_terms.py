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

from pathlib import Path

from django.core.management.base import BaseCommand
from django.template.loader import render_to_string

from weblate_web.pdf import render_pdf

HOSTED_ACCOUNT = "Hosted Weblate account"

OUT_DIR = Path(__file__).parent.parent.parent / "static"
GTC_NAME = "Weblate_General_Terms_and_Conditions.pdf"
PRIVACY_NAME = "Weblate_Privacy_Policy.pdf"
DPA_SAMPLE_NAME = "Weblate_Data_Processing_Agreement_Sample.pdf"


class Command(BaseCommand):
    help = "generates legal PDFs"
    client = None

    def handle(self, *args, **options):
        render_pdf(
            html=render_to_string(
                "pdf/terms.html",
                {
                    "title": "General Terms and Conditions",
                    "privacy_url": f"https://weblate.org/static/{PRIVACY_NAME}",
                },
            ),
            output=OUT_DIR / GTC_NAME,
        )

        render_pdf(
            html=render_to_string(
                "pdf/privacy.html",
                {
                    "title": "Privacy Policy",
                    "terms_url": f"https://weblate.org/static/{GTC_NAME}",
                },
            ),
            output=OUT_DIR / PRIVACY_NAME,
        )

        render_pdf(
            html=render_to_string(
                "pdf/dpa.html",
                {
                    "title": "Data Processing Agreement",
                    "sample": True,
                },
            ),
            output=OUT_DIR / DPA_SAMPLE_NAME,
        )
