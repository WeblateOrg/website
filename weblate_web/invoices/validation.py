"""
EN 16931 Invoice XML Validator.

- Support only CII (Cross Industry Invoice) format.
- This is not intended to be complete validation, just validates subset of rules.
- In the CI this is accompanied by a Java validation service.
"""

import re
from datetime import datetime

from lxml import etree


class ValidationError:
    def __init__(self, rule: str, message: str, severity: str = "error"):
        self.rule = rule
        self.message = message
        self.severity = severity

    def __repr__(self):
        return f"[{self.severity.upper()}] {self.rule}: {self.message}"


class EN16931Validator:
    def __init__(self):
        self.errors: list[ValidationError] = []
        self.warnings: list[ValidationError] = []

        # Namespace definitions
        self.namespaces = {
            "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
            "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
            "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
        }

    def validate_bytes(
        self, xml_bytes: bytes
    ) -> tuple[bool, list[ValidationError], list[ValidationError]]:
        """Validate an EN 16931 invoice XML from bytes."""
        try:
            tree = etree.fromstring(xml_bytes)
            return self.validate_tree(etree.ElementTree(tree))
        except etree.XMLSyntaxError as e:
            self.errors.append(ValidationError("XML-SYNTAX", f"Invalid XML: {e!s}"))
            return False, self.errors, self.warnings

    def validate_tree(
        self, tree: etree.ElementTree
    ) -> tuple[bool, list[ValidationError], list[ValidationError]]:
        """Validate an EN 16931 invoice XML tree."""
        self.errors = []
        self.warnings = []

        root = tree.getroot()

        # Detect format (only CII is supported)
        if "CrossIndustryInvoice" not in root.tag:  # type: ignore[operator]
            self.errors.append(
                ValidationError("FORMAT", "Unknown invoice format. Expected CII.")
            )
            return False, self.errors, self.warnings

        self._validate_cii(root)
        is_valid = len(self.errors) == 0
        return is_valid, self.errors, self.warnings

    def _validate_cii(self, root):
        """Validate CII (Cross Industry Invoice) format."""
        # Basic CII validation structure
        header = root.find(".//rsm:ExchangedDocument", self.namespaces)
        if header is None:
            self.errors.append(
                ValidationError("CII-01", "ExchangedDocument is mandatory")
            )
            return

        self._validate_header(header)

        transaction = root.find(".//rsm:SupplyChainTradeTransaction", self.namespaces)
        if transaction is None:
            self.errors.append(
                ValidationError("CII-02", "SupplyChainTradeTransaction is mandatory")
            )
            return

        # Validate seller and buyer
        self._validate_seller_buyer_cii(transaction)

        # Validate invoice lines
        lines = transaction.findall(
            ".//ram:IncludedSupplyChainTradeLineItem", self.namespaces
        )
        if not lines:
            self.errors.append(
                ValidationError(
                    "BR-16", "At least one invoice line (BG-25) is mandatory"
                )
            )
        else:
            for idx, line in enumerate(lines, 1):
                self._validate_invoice_line_cii(line, idx)

        # Validate monetary totals and BR-CO-* rules
        self._validate_amounts_cii(transaction)

        # Validate payment terms
        self._validate_payment_terms(transaction)

    def _validate_header(self, header):
        """Validate document header."""
        # BR-01: Invoice number
        invoice_id = header.find(".//ram:ID", self.namespaces)
        if invoice_id is None or not invoice_id.text:
            self.errors.append(
                ValidationError("BR-01", "Invoice number (BT-1) is mandatory")
            )

        # BR-02: Issue date
        issue_date = header.find(
            ".//ram:IssueDateTime/udt:DateTimeString", self.namespaces
        )
        if issue_date is None or not issue_date.text:
            self.errors.append(
                ValidationError("BR-02", "Issue date (BT-2) is mandatory")
            )
        else:
            self._validate_date_format(issue_date.text, "BR-02", "Issue date")

        # BR-03: Type code
        type_code = header.find(".//ram:TypeCode", self.namespaces)
        if type_code is None or not type_code.text:
            self.errors.append(
                ValidationError("BR-03", "Invoice type code (BT-3) is mandatory")
            )
        else:
            # BR-04: Invoice type code must be valid
            self._validate_type_code(type_code.text)

    def _validate_type_code(self, code: str):
        """Validate invoice type code."""
        valid_codes = {
            "325",  # Proforma invoice
            "380",  # Commercial invoice
            "381",  # Credit note
            "384",  # Corrected invoice
            "389",  # Self-billed invoice
            "751",  # Invoice information for accounting purposes
        }
        if code not in valid_codes:
            self.warnings.append(
                ValidationError(
                    "BR-CL-01",
                    f"Invoice type code {code} is not in the recommended list",
                    "warning",
                )
            )

    def _validate_date_format(self, date_str: str, rule: str, field_name: str):
        """Validate date format."""
        # CII uses format 102 (YYYYMMDD) or other formats
        # Try to parse common formats
        formats = ["%Y%m%d", "%Y-%m-%d", "%Y%m%d%H%M%S"]
        parsed = False
        for fmt in formats:
            try:
                datetime.strptime(date_str, fmt)  # noqa: DTZ007
                parsed = True
                break
            except ValueError:
                continue

        if not parsed:
            self.warnings.append(
                ValidationError(
                    rule,
                    f"{field_name} has unusual format: {date_str}",
                    "warning",
                )
            )

    def _validate_currency_code(self, code: str, bt_code: str = "BT-5"):
        """Validate ISO 4217 currency code."""
        if not re.match(r"^[A-Z]{3}$", code):
            self.errors.append(
                ValidationError(
                    f"BR-CL-{bt_code}",
                    f"Invalid currency code: {code}. Must be ISO 4217 3-letter code",
                )
            )

    def _get_decimal(self, element) -> float:
        """Safely extract decimal value from element."""
        if element is None or not element.text:
            return 0.0
        try:
            return float(element.text)
        except (ValueError, TypeError):
            return 0.0

    def _amounts_equal(
        self, amount1: float, amount2: float, tolerance: float = 0.02
    ) -> bool:
        """Check if two amounts are equal within a tolerance (for rounding differences)."""
        return abs(amount1 - amount2) < tolerance

    def _validate_seller_buyer_cii(self, transaction):
        """Validate seller and buyer information for CII format."""
        # Seller validation
        seller = transaction.find(
            ".//ram:ApplicableHeaderTradeAgreement/ram:SellerTradeParty",
            self.namespaces,
        )
        if seller is None:
            self.errors.append(ValidationError("BR-06", "Seller (BG-4) is mandatory"))
        else:
            # BR-27: Seller name
            seller_name = seller.find(".//ram:Name", self.namespaces)
            if seller_name is None or not seller_name.text:
                self.errors.append(
                    ValidationError("BR-27", "Seller name (BT-27) is mandatory")
                )

            # BR-28: Seller postal address
            seller_address = seller.find(".//ram:PostalTradeAddress", self.namespaces)
            if seller_address is not None:
                # BR-AE-01: Seller country code is mandatory
                country = seller_address.find(".//ram:CountryID", self.namespaces)
                if country is None or not country.text:
                    self.errors.append(
                        ValidationError(
                            "BR-AE-01", "Seller country code (BT-40) is mandatory"
                        )
                    )
                else:
                    self._validate_country_code(country.text, "BT-40")

            # BR-30: Seller electronic address
            seller_email = seller.find(
                ".//ram:URIUniversalCommunication/ram:URIID", self.namespaces
            )
            if seller_email is None or not seller_email.text:
                self.warnings.append(
                    ValidationError(
                        "BR-30",
                        "Seller electronic address (BT-34) is recommended",
                        "warning",
                    )
                )

        # Buyer validation
        buyer = transaction.find(
            ".//ram:ApplicableHeaderTradeAgreement/ram:BuyerTradeParty", self.namespaces
        )
        if buyer is None:
            self.errors.append(ValidationError("BR-07", "Buyer (BG-7) is mandatory"))
        else:
            # BR-08: Buyer name
            buyer_name = buyer.find(".//ram:Name", self.namespaces)
            if buyer_name is None or not buyer_name.text:
                self.errors.append(
                    ValidationError("BR-08", "Buyer name (BT-44) is mandatory")
                )

            # BR-09: Buyer postal address
            buyer_address = buyer.find(".//ram:PostalTradeAddress", self.namespaces)
            if buyer_address is None:
                self.errors.append(
                    ValidationError("BR-09", "Buyer postal address (BG-8) is mandatory")
                )
            else:
                # BR-AE-02: Buyer country code is mandatory
                country = buyer_address.find(".//ram:CountryID", self.namespaces)
                if country is None or not country.text:
                    self.errors.append(
                        ValidationError(
                            "BR-AE-02", "Buyer country code (BT-55) is mandatory"
                        )
                    )
                else:
                    self._validate_country_code(country.text, "BT-55")

    def _validate_country_code(self, code: str, bt_code: str):
        """Validate ISO 3166-1 alpha-2 country code."""
        if not re.match(r"^[A-Z]{2}$", code):
            self.errors.append(
                ValidationError(
                    f"BR-CL-{bt_code}",
                    f"Invalid country code: {code}. Must be ISO 3166-1 alpha-2 code",
                )
            )

    def _validate_invoice_line_cii(self, line, line_num):
        """Validate individual invoice line for CII format."""
        # BR-21: Line ID
        line_id = line.find(
            ".//ram:AssociatedDocumentLineDocument/ram:LineID", self.namespaces
        )
        if line_id is None or not line_id.text:
            self.errors.append(
                ValidationError(
                    "BR-21", f"Line {line_num}: Line ID (BT-126) is mandatory"
                )
            )

        # BR-22: Line quantity
        quantity = line.find(
            ".//ram:SpecifiedLineTradeDelivery/ram:BilledQuantity", self.namespaces
        )
        if quantity is None or not quantity.text:
            self.errors.append(
                ValidationError(
                    "BR-22", f"Line {line_num}: Invoiced quantity (BT-129) is mandatory"
                )
            )
        else:
            # BR-23: Unit code
            unit_code = quantity.get("unitCode")
            if not unit_code:
                self.errors.append(
                    ValidationError(
                        "BR-23", f"Line {line_num}: Unit code (BT-130) is mandatory"
                    )
                )
            else:
                self._validate_unit_code(unit_code, line_num)

        # BR-24: Line net amount
        settlement = line.find(".//ram:SpecifiedLineTradeSettlement", self.namespaces)
        if settlement is not None:
            monetary = settlement.find(
                ".//ram:SpecifiedTradeSettlementLineMonetarySummation", self.namespaces
            )
            if monetary is not None:
                line_amount = monetary.find(".//ram:LineTotalAmount", self.namespaces)
                if line_amount is None or not line_amount.text:
                    self.errors.append(
                        ValidationError(
                            "BR-24",
                            f"Line {line_num}: Line net amount (BT-131) is mandatory",
                        )
                    )

        # BR-26: Item name
        product = line.find(".//ram:SpecifiedTradeProduct/ram:Name", self.namespaces)
        if product is None or not product.text:
            self.errors.append(
                ValidationError(
                    "BR-26", f"Line {line_num}: Item name (BT-153) is mandatory"
                )
            )

        # BR-28: Item price
        price = line.find(
            ".//ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount",
            self.namespaces,
        )
        if price is None or not price.text:
            # Try gross price as fallback
            price = line.find(
                ".//ram:SpecifiedLineTradeAgreement/ram:GrossPriceProductTradePrice/ram:ChargeAmount",
                self.namespaces,
            )
            if price is None or not price.text:
                self.errors.append(
                    ValidationError(
                        "BR-28", f"Line {line_num}: Item price (BT-146) is mandatory"
                    )
                )

        # BR-AE-03: Line VAT category code is mandatory
        if settlement is not None:
            vat_category = settlement.find(
                ".//ram:ApplicableTradeTax/ram:CategoryCode", self.namespaces
            )
            if vat_category is None or not vat_category.text:
                self.errors.append(
                    ValidationError(
                        "BR-AE-03",
                        f"Line {line_num}: VAT category code (BT-151) is mandatory",
                    )
                )
            else:
                self._validate_vat_category_code(vat_category.text, line_num)

    def _validate_unit_code(self, code: str, line_num: int):
        """Validate unit code (should be from UN/ECE Recommendation 20)."""
        # This is a subset of common codes
        common_codes = {
            "C62",
            "MTR",
            "KGM",
            "LTR",
            "H87",
            "DAY",
            "HUR",
            "MTK",
            "MTQ",
            "P1",
            "EA",
            "SET",
            "TNE",  # codespell:ignore
            "CMT",
            "DMT",
            "GRM",
        }
        if code not in common_codes:
            self.warnings.append(
                ValidationError(
                    "BR-CL-23",
                    f"Line {line_num}: Unit code {code} is not in common UN/ECE Rec. 20 codes",
                    "warning",
                )
            )

    def _validate_vat_category_code(self, code: str, line_num: int | None = None):
        """Validate VAT category code."""
        valid_codes = {
            "S",  # Standard rate
            "Z",  # Zero rated
            "E",  # Exempt
            "AE",  # Reverse charge
            "K",  # Intra-community
            "G",  # Free export
            "O",  # Outside scope
            "L",  # Canary Islands
            "M",  # IGIC (Canary Islands)
        }
        if code not in valid_codes:
            location = f"Line {line_num}: " if line_num else ""
            self.errors.append(
                ValidationError(
                    "BR-CL-05",
                    f"{location}Invalid VAT category code: {code}",
                )
            )

    def _validate_amounts_cii(self, transaction):
        """Validate invoice totals and amounts for CII format with BR-CO-* rules."""
        settlement = transaction.find(
            ".//ram:ApplicableHeaderTradeSettlement", self.namespaces
        )
        if settlement is None:
            self.errors.append(
                ValidationError(
                    "CII-03", "ApplicableHeaderTradeSettlement is mandatory"
                )
            )
            return

        monetary = settlement.find(
            ".//ram:SpecifiedTradeSettlementHeaderMonetarySummation", self.namespaces
        )
        if monetary is None:
            self.errors.append(
                ValidationError(
                    "CII-04",
                    "SpecifiedTradeSettlementHeaderMonetarySummation is mandatory",
                )
            )
            return

        # BR-05: Currency code
        currency = settlement.find(".//ram:InvoiceCurrencyCode", self.namespaces)
        if currency is None or not currency.text:
            self.errors.append(
                ValidationError("BR-05", "Document currency code (BT-5) is mandatory")
            )
        else:
            self._validate_currency_code(currency.text)

        # Extract amounts
        line_total = monetary.find(".//ram:LineTotalAmount", self.namespaces)
        tax_basis_total = monetary.find(".//ram:TaxBasisTotalAmount", self.namespaces)
        grand_total = monetary.find(".//ram:GrandTotalAmount", self.namespaces)
        due_payable = monetary.find(".//ram:DuePayableAmount", self.namespaces)

        # Validate mandatory fields
        if line_total is None or not line_total.text:
            self.errors.append(
                ValidationError(
                    "BR-12", "Sum of line net amounts (BT-106) is mandatory"
                )
            )

        if tax_basis_total is None or not tax_basis_total.text:
            self.errors.append(
                ValidationError(
                    "BR-13", "Invoice total without VAT (BT-109) is mandatory"
                )
            )

        if grand_total is None or not grand_total.text:
            self.errors.append(
                ValidationError("BR-14", "Invoice total with VAT (BT-112) is mandatory")
            )

        if due_payable is None or not due_payable.text:
            self.errors.append(
                ValidationError("BR-15", "Amount due for payment (BT-115) is mandatory")
            )

        # Validate VAT breakdown
        self._validate_vat_breakdown(settlement)

        # Now perform BR-CO-* calculations
        self._validate_brco_rules_cii(transaction, monetary)

    def _validate_vat_breakdown(self, settlement):
        """Validate VAT breakdown (BR-AE-*)."""
        vat_breakdowns = settlement.findall(
            ".//ram:ApplicableTradeTax", self.namespaces
        )

        if not vat_breakdowns:
            self.errors.append(
                ValidationError(
                    "BR-AE-10",
                    "At least one VAT breakdown (BG-23) is mandatory",
                )
            )
            return

        for idx, vat in enumerate(vat_breakdowns, 1):
            # BR-AE-11: VAT category code
            category = vat.find(".//ram:CategoryCode", self.namespaces)
            if category is None or not category.text:
                self.errors.append(
                    ValidationError(
                        "BR-AE-11",
                        f"VAT breakdown {idx}: Category code (BT-118) is mandatory",
                    )
                )
            else:
                self._validate_vat_category_code(category.text)

            # BR-AE-12: VAT category taxable amount
            basis = vat.find(".//ram:BasisAmount", self.namespaces)
            if basis is None or not basis.text:
                self.errors.append(
                    ValidationError(
                        "BR-AE-12",
                        f"VAT breakdown {idx}: Taxable amount (BT-116) is mandatory",
                    )
                )

            # BR-AE-13: VAT category tax amount
            tax_amount = vat.find(".//ram:CalculatedAmount", self.namespaces)
            if tax_amount is None or not tax_amount.text:
                self.errors.append(
                    ValidationError(
                        "BR-AE-13",
                        f"VAT breakdown {idx}: Tax amount (BT-117) is mandatory",
                    )
                )

            # BR-AE-14: For standard rate, rate must be present
            if category is not None and category.text == "S":
                rate = vat.find(".//ram:RateApplicablePercent", self.namespaces)
                if rate is None or not rate.text:
                    self.errors.append(
                        ValidationError(
                            "BR-AE-14",
                            f"VAT breakdown {idx}: VAT rate (BT-119) is mandatory for standard rate",
                        )
                    )

    def _validate_payment_terms(self, transaction):
        """Validate payment terms."""
        settlement = transaction.find(
            ".//ram:ApplicableHeaderTradeSettlement", self.namespaces
        )
        if settlement is None:
            return

        # BR-20: Payment means code
        payment_means = settlement.find(
            ".//ram:SpecifiedTradeSettlementPaymentMeans", self.namespaces
        )
        if payment_means is not None:
            type_code = payment_means.find(".//ram:TypeCode", self.namespaces)
            if type_code is None or not type_code.text:
                self.warnings.append(
                    ValidationError(
                        "BR-20",
                        "Payment means code (BT-81) is recommended",
                        "warning",
                    )
                )

    def _validate_brco_rules_cii(self, transaction, monetary):
        """Validate BR-CO-* calculation rules for CII format."""
        # Extract all amounts
        line_ext_amount = self._get_decimal(
            monetary.find(".//ram:LineTotalAmount", self.namespaces)
        )
        allowance_total = self._get_decimal(
            monetary.find(".//ram:AllowanceTotalAmount", self.namespaces)
        )
        charge_total = self._get_decimal(
            monetary.find(".//ram:ChargeTotalAmount", self.namespaces)
        )
        tax_basis_amount = self._get_decimal(
            monetary.find(".//ram:TaxBasisTotalAmount", self.namespaces)
        )

        # Get tax total - CII can have multiple TaxTotalAmount elements
        settlement = transaction.find(
            ".//ram:ApplicableHeaderTradeSettlement", self.namespaces
        )
        tax_totals = settlement.findall(
            ".//ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:TaxTotalAmount",
            self.namespaces,
        )

        # Use the one that matches invoice currency
        currency_elem = settlement.find(".//ram:InvoiceCurrencyCode", self.namespaces)
        invoice_currency = currency_elem.text if currency_elem is not None else None

        tax_total = 0.0
        for tax_elem in tax_totals:
            currency_id = tax_elem.get("currencyID")
            if currency_id == invoice_currency or not currency_id:
                tax_total = self._get_decimal(tax_elem)
                break

        grand_total_amount = self._get_decimal(
            monetary.find(".//ram:GrandTotalAmount", self.namespaces)
        )
        prepaid_amount = self._get_decimal(
            monetary.find(".//ram:TotalPrepaidAmount", self.namespaces)
        )
        rounding_amount = self._get_decimal(
            monetary.find(".//ram:RoundingAmount", self.namespaces)
        )
        due_payable_amount = self._get_decimal(
            monetary.find(".//ram:DuePayableAmount", self.namespaces)
        )

        # BR-CO-10: Sum of Invoice line net amounts = Σ(Invoice line net amount)
        lines = transaction.findall(
            ".//ram:IncludedSupplyChainTradeLineItem", self.namespaces
        )
        calculated_line_total = 0.0
        for line in lines:
            line_settlement = line.find(
                ".//ram:SpecifiedLineTradeSettlement", self.namespaces
            )
            if line_settlement is not None:
                line_monetary = line_settlement.find(
                    ".//ram:SpecifiedTradeSettlementLineMonetarySummation",
                    self.namespaces,
                )
                if line_monetary is not None:
                    line_amt = line_monetary.find(
                        ".//ram:LineTotalAmount", self.namespaces
                    )
                    calculated_line_total += self._get_decimal(line_amt)

        if not self._amounts_equal(line_ext_amount, calculated_line_total):
            self.errors.append(
                ValidationError(
                    "BR-CO-10",
                    f"Sum of Invoice line net amount (BT-106) = {line_ext_amount:.2f} must equal sum of line amounts = {calculated_line_total:.2f}",
                )
            )

        # BR-CO-11: Invoice total amount without VAT =
        #           Σ Invoice line net amount - Sum of allowances + Sum of charges
        calculated_tax_basis = line_ext_amount - allowance_total + charge_total
        if not self._amounts_equal(tax_basis_amount, calculated_tax_basis):
            self.errors.append(
                ValidationError(
                    "BR-CO-11",
                    f"Invoice total without VAT (BT-109) = {tax_basis_amount:.2f} must equal "
                    f"{line_ext_amount:.2f} - {allowance_total:.2f} + {charge_total:.2f} = {calculated_tax_basis:.2f}",
                )
            )

        # BR-CO-12: Invoice total amount with VAT =
        #           Invoice total amount without VAT + Invoice total VAT amount
        calculated_grand_total = tax_basis_amount + tax_total
        if not self._amounts_equal(grand_total_amount, calculated_grand_total):
            self.errors.append(
                ValidationError(
                    "BR-CO-12",
                    f"Invoice total with VAT (BT-112) = {grand_total_amount:.2f} must equal "
                    f"{tax_basis_amount:.2f} + {tax_total:.2f} = {calculated_grand_total:.2f}",
                )
            )

        # BR-CO-13: Amount due for payment =
        #           Invoice total with VAT - Paid amount + Rounding amount
        calculated_due = grand_total_amount - prepaid_amount + rounding_amount
        if not self._amounts_equal(due_payable_amount, calculated_due):
            self.errors.append(
                ValidationError(
                    "BR-CO-13",
                    f"Amount due for payment (BT-115) = {due_payable_amount:.2f} must equal "
                    f"{grand_total_amount:.2f} - {prepaid_amount:.2f} + {rounding_amount:.2f} = {calculated_due:.2f}",
                )
            )

        # BR-CO-14: Invoice total VAT amount = Σ(VAT category tax amount)
        trade_tax_totals = settlement.findall(
            ".//ram:ApplicableTradeTax", self.namespaces
        )
        calculated_vat_total = 0.0
        for tax in trade_tax_totals:
            calculated_vat_total += self._get_decimal(
                tax.find(".//ram:CalculatedAmount", self.namespaces)
            )

        if not self._amounts_equal(tax_total, calculated_vat_total):
            self.errors.append(
                ValidationError(
                    "BR-CO-14",
                    f"Invoice total VAT amount (BT-110) = {tax_total:.2f} must equal "
                    f"sum of VAT category tax amounts = {calculated_vat_total:.2f}",
                )
            )

        # BR-CO-15: VAT category taxable amount validation
        for tax in trade_tax_totals:
            self._validate_vat_category_brco15_cii(transaction, tax)

        # BR-CO-16: Amount due for payment must not be negative
        if due_payable_amount < 0:
            self.errors.append(
                ValidationError(
                    "BR-CO-16",
                    f"Amount due for payment (BT-115) = {due_payable_amount:.2f} must not be negative",
                )
            )

        # Validate invoice line calculations (BR-CO-03, BR-CO-04)
        for idx, line in enumerate(lines, 1):
            self._validate_line_calculations_cii(line, idx)

        # Validate document level allowances/charges (BR-CO-01, BR-CO-02)
        self._validate_allowances_charges_cii(settlement)

    def _validate_vat_category_brco15_cii(self, transaction, trade_tax):
        """BR-CO-15: VAT category taxable amount validation for CII."""
        category_code = trade_tax.find(".//ram:CategoryCode", self.namespaces)
        if category_code is None:
            return

        category = category_code.text
        taxable_amount = self._get_decimal(
            trade_tax.find(".//ram:BasisAmount", self.namespaces)
        )

        # Sum line amounts for this VAT category
        lines = transaction.findall(
            ".//ram:IncludedSupplyChainTradeLineItem", self.namespaces
        )
        category_line_total = 0.0

        for line in lines:
            line_tax = line.find(
                ".//ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax/ram:CategoryCode",
                self.namespaces,
            )
            if line_tax is not None and line_tax.text == category:
                line_settlement = line.find(
                    ".//ram:SpecifiedLineTradeSettlement", self.namespaces
                )
                if line_settlement is not None:
                    line_monetary = line_settlement.find(
                        ".//ram:SpecifiedTradeSettlementLineMonetarySummation",
                        self.namespaces,
                    )
                    if line_monetary is not None:
                        line_amt = line_monetary.find(
                            ".//ram:LineTotalAmount", self.namespaces
                        )
                        category_line_total += self._get_decimal(line_amt)

        # Add document level allowances/charges for this category
        settlement = transaction.find(
            ".//ram:ApplicableHeaderTradeSettlement", self.namespaces
        )
        all_doc_ac = settlement.findall(
            ".//ram:SpecifiedTradeAllowanceCharge", self.namespaces
        )

        for ac in all_doc_ac:
            charge_indicator = ac.find(
                ".//ram:ChargeIndicator/udt:Indicator", self.namespaces
            )
            ac_tax = ac.find(
                ".//ram:CategoryTradeTax/ram:CategoryCode", self.namespaces
            )
            ac_amount = self._get_decimal(
                ac.find(".//ram:ActualAmount", self.namespaces)
            )

            if (
                ac_tax is not None
                and ac_tax.text == category
                and charge_indicator is not None
                and charge_indicator.text
            ):
                if charge_indicator.text.lower() == "false":
                    category_line_total -= ac_amount
                elif charge_indicator.text.lower() == "true":
                    category_line_total += ac_amount

        if not self._amounts_equal(taxable_amount, category_line_total):
            self.warnings.append(
                ValidationError(
                    "BR-CO-15",
                    f"VAT category {category} taxable amount (BT-116) = {taxable_amount:.2f} "
                    f"should equal sum of line amounts in category = {category_line_total:.2f}",
                    "warning",
                )
            )

    def _validate_line_calculations_cii(self, line, line_num):
        """Validate BR-CO-03 and BR-CO-04 for invoice lines (CII format)."""
        # Extract quantity and price
        quantity_elem = line.find(
            ".//ram:SpecifiedLineTradeDelivery/ram:BilledQuantity", self.namespaces
        )
        quantity = self._get_decimal(quantity_elem)

        # Try net price first, then gross price
        price_elem = line.find(
            ".//ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount",
            self.namespaces,
        )
        if price_elem is None:
            price_elem = line.find(
                ".//ram:SpecifiedLineTradeAgreement/ram:GrossPriceProductTradePrice/ram:ChargeAmount",
                self.namespaces,
            )
        price = self._get_decimal(price_elem)

        # Get line amount
        line_settlement = line.find(
            ".//ram:SpecifiedLineTradeSettlement", self.namespaces
        )
        if line_settlement is None:
            return

        line_monetary = line_settlement.find(
            ".//ram:SpecifiedTradeSettlementLineMonetarySummation", self.namespaces
        )
        if line_monetary is None:
            return

        line_amount_elem = line_monetary.find(".//ram:LineTotalAmount", self.namespaces)
        line_amount = self._get_decimal(line_amount_elem)

        # Get line level allowances and charges
        all_line_ac = line_settlement.findall(
            ".//ram:SpecifiedTradeAllowanceCharge", self.namespaces
        )

        total_allowances = 0.0
        total_charges = 0.0

        for ac in all_line_ac:
            charge_indicator = ac.find(
                ".//ram:ChargeIndicator/udt:Indicator", self.namespaces
            )
            ac_amount = self._get_decimal(
                ac.find(".//ram:ActualAmount", self.namespaces)
            )

            if charge_indicator is not None and charge_indicator.text:
                if charge_indicator.text.lower() == "false":
                    total_allowances += ac_amount
                elif charge_indicator.text.lower() == "true":
                    total_charges += ac_amount

        # BR-CO-03: Invoice line net amount = (quantity × price) - line allowances + line charges
        calculated_line_amount = (quantity * price) - total_allowances + total_charges

        if not self._amounts_equal(line_amount, calculated_line_amount):
            self.errors.append(
                ValidationError(
                    "BR-CO-03",
                    f"Line {line_num}: Net amount (BT-131) = {line_amount:.2f} must equal "
                    f"({quantity} × {price:.2f}) - {total_allowances:.2f} + {total_charges:.2f} = {calculated_line_amount:.2f}",
                )
            )

        # BR-CO-04: Invoice line net amount must not be negative
        if line_amount < 0:
            self.errors.append(
                ValidationError(
                    "BR-CO-04",
                    f"Line {line_num}: Net amount (BT-131) = {line_amount:.2f} must not be negative",
                )
            )

    def _validate_allowances_charges_cii(self, settlement):
        """Validate BR-CO-01 and BR-CO-02 for document level allowances/charges (CII format)."""
        # Get all document level allowances and charges
        all_ac = settlement.findall(
            ".//ram:SpecifiedTradeAllowanceCharge", self.namespaces
        )

        allowance_idx = 0
        charge_idx = 0

        for ac in all_ac:
            charge_indicator = ac.find(
                ".//ram:ChargeIndicator/udt:Indicator", self.namespaces
            )
            if charge_indicator is None:
                continue

            base_amount = self._get_decimal(
                ac.find(".//ram:BasisAmount", self.namespaces)
            )
            percentage = self._get_decimal(
                ac.find(".//ram:CalculationPercent", self.namespaces)
            )
            amount = self._get_decimal(ac.find(".//ram:ActualAmount", self.namespaces))

            if base_amount > 0 and percentage > 0:
                calculated_amount = base_amount * (percentage / 100)

                if charge_indicator.text.lower() == "false":
                    # BR-CO-01: Document level allowance
                    allowance_idx += 1
                    if not self._amounts_equal(amount, calculated_amount):
                        self.warnings.append(
                            ValidationError(
                                "BR-CO-01",
                                f"Document allowance {allowance_idx}: Amount = {amount:.2f} should equal "
                                f"{base_amount:.2f} × {percentage}% = {calculated_amount:.2f}",
                                "warning",
                            )
                        )
                elif charge_indicator.text.lower() == "true":
                    # BR-CO-02: Document level charge
                    charge_idx += 1
                    if not self._amounts_equal(amount, calculated_amount):
                        self.warnings.append(
                            ValidationError(
                                "BR-CO-02",
                                f"Document charge {charge_idx}: Amount = {amount:.2f} should equal "
                                f"{base_amount:.2f} × {percentage}% = {calculated_amount:.2f}",
                                "warning",
                            )
                        )
