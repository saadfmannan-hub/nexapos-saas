"""Server-side PDF generation (xhtml2pdf — pure Python, Windows-friendly)."""
import io

from django.template.loader import render_to_string
from xhtml2pdf import pisa


class PdfError(Exception):
    pass


def render_pdf(template_name, context) -> bytes:
    html = render_to_string(template_name, context)
    buffer = io.BytesIO()
    result = pisa.CreatePDF(io.StringIO(html), dest=buffer, encoding="utf-8")
    if result.err:
        raise PdfError("PDF generation failed.")
    return buffer.getvalue()
