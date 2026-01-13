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
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.staticfiles import finders
from weasyprint import CSS, HTML
from weasyprint.pdf.metadata import generate_rdf_metadata
from weasyprint.text.fonts import FontConfiguration

if TYPE_CHECKING:
    from weasyprint import Attachment

SIGNATURE_URL = "signature:"
INVOICES_URL = "invoices:"
LEGAL_URL = "legal:"
STATIC_URL = "static:"
INVOICES_TEMPLATES_PATH = Path(__file__).parent / "invoices" / "templates"
LEGAL_TEMPLATES_PATH = Path(__file__).parent / "legal" / "templates"

FACTURX_RDF_METADATA = """
<x:xmpmeta
    xmlns:x="adobe:ns:meta/"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:pdf="http://ns.adobe.com/pdf/1.3/"
    xmlns:fx="urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#"
    xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/"
    xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#"
    xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">
  {}
  <rdf:RDF>
    <rdf:Description rdf:about="">
      <fx:ConformanceLevel>EN 16931</fx:ConformanceLevel>
      <fx:DocumentFileName>factur-x.xml</fx:DocumentFileName>
      <fx:DocumentType>INVOICE</fx:DocumentType>
      <fx:Version>1.0</fx:Version>
    </rdf:Description>
    <rdf:Description rdf:about="">
      <pdfaExtension:schemas>
        <rdf:Bag>
          <rdf:li rdf:parseType="Resource">
            <pdfaSchema:schema>Factur-X PDFA Extension Schema</pdfaSchema:schema>
            <pdfaSchema:namespaceURI>urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#</pdfaSchema:namespaceURI>
            <pdfaSchema:prefix>fx</pdfaSchema:prefix>
            <pdfaSchema:property>
              <rdf:Seq>
                <rdf:li rdf:parseType="Resource">
                  <pdfaProperty:name>DocumentFileName</pdfaProperty:name>
                  <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                  <pdfaProperty:category>external</pdfaProperty:category>
                  <pdfaProperty:description>name of the embedded XML invoice file</pdfaProperty:description>
                </rdf:li>
                <rdf:li rdf:parseType="Resource">
                  <pdfaProperty:name>DocumentType</pdfaProperty:name>
                  <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                  <pdfaProperty:category>external</pdfaProperty:category>
                  <pdfaProperty:description>INVOICE</pdfaProperty:description>
                </rdf:li>
                <rdf:li rdf:parseType="Resource">
                  <pdfaProperty:name>Version</pdfaProperty:name>
                  <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                  <pdfaProperty:category>external</pdfaProperty:category>
                  <pdfaProperty:description>The actual version of the Factur-X XML schema</pdfaProperty:description>
                </rdf:li>
                <rdf:li rdf:parseType="Resource">
                  <pdfaProperty:name>ConformanceLevel</pdfaProperty:name>
                  <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                  <pdfaProperty:category>external</pdfaProperty:category>
                  <pdfaProperty:description>The conformance level of the embedded Factur-X data</pdfaProperty:description>
                </rdf:li>
              </rdf:Seq>
            </pdfaSchema:property>
          </rdf:li>
        </rdf:Bag>
      </pdfaExtension:schemas>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
"""


def generate_faxturx_rdf_metadata(metadata, variant, version, conformance) -> bytes:
    original_rdf = generate_rdf_metadata(metadata, variant, version, conformance)
    return FACTURX_RDF_METADATA.format(original_rdf.decode("utf-8")).encode("utf-8")


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
        raise ValueError(f"Unsupported URL: {url}")
    result = {
        "filename": path_obj.name,
        "string": path_obj.read_bytes(),
    }
    if path_obj.suffix == ".css":
        result["mime_type"] = "text/css"
        result["encoding"] = "utf-8"
    return result


def render_pdf(
    *,
    html: str,
    output: Path,
    attachments: list[Attachment] | None = None,
    factur_x: bool = False,
) -> None:
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
    document = renderer.render(
        stylesheets=[font_style],
        font_config=font_config,
        pdf_variant="pdf/a-3b",
    )
    if factur_x:
        document.metadata.generate_rdf_metadata = generate_faxturx_rdf_metadata
    if attachments:
        document.metadata.attachments = attachments
    document.write_pdf(
        output,
        pdf_variant="pdf/a-3b",
    )
