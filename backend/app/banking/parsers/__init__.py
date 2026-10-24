# SA bank statement parsers (South African banks). Each returns rows of
# {date, description, amount (positive), type: credit|debit, reference, [category, fee, balance]}.
import io
import logging

from .capitec import CapitecParser
from .generic import GenericParser
from .gotyme import GoTymeParser, GoTymeTextParser, is_gotyme
from .tymebank import TymeBankLegacyParser

log = logging.getLogger(__name__)

__all__ = ["parse_pdf", "extract_text", "open_pdf"]


def open_pdf(pdf_bytes, passwords=()):
    """(PdfReader, password that worked). Tries no password, blank, then each saved password."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if not reader.is_encrypted:
        return reader, None
    for pw in ("", *passwords):
        try:
            if reader.decrypt(pw):
                return reader, pw
        except Exception:
            continue
    raise ValueError("statement is password-protected and none of the saved passwords opened it")


def extract_text(pdf_bytes, passwords=()):
    reader, pw = open_pdf(pdf_bytes, passwords)
    return "\n".join(page.extract_text() or "" for page in reader.pages), pw


TEXT_PARSERS = {"tymebank": TymeBankLegacyParser, "capitec": CapitecParser,
                "generic": GenericParser}


def parse_text(text, bank_name):
    """The bank's own parser first; when it finds nothing, whichever other text parser finds the most."""
    first = TEXT_PARSERS.get(bank_name, GenericParser)().parse(text)
    if first:
        return first
    best = []
    for name, parser in TEXT_PARSERS.items():
        if name != bank_name:
            rows = parser().parse(text)
            if len(rows) > len(best):
                best = rows
    return best


def parse_pdf(pdf_bytes, bank_name, passwords=()):
    """(rows, text)."""
    text, pw = extract_text(pdf_bytes, passwords)
    if bank_name == "gotyme" or is_gotyme(text):
        rows = GoTymeParser().parse(pdf_bytes, pw)
        if rows:
            return rows, text
    return parse_text(text, bank_name), text
