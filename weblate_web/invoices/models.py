from weasyprint import HTML


def generate_invoice():
    HTML(
        "/home/nijel/weblate/website/weblate_web/invoices/templates/invoice.html"
    ).write_pdf("invoice.pdf")
