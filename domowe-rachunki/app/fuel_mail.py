"""Faktury za paliwo z maili ORLEN Pay: po tankowaniu przychodzi mail z e-fakturą (i załączonym PDF-em).

Z maila bierzemy numer, stację, datę i godzinę oraz kwotę; jeśli jest załącznik PDF, jest zapisywany
i odczytywany (litry, rodzaj paliwa, pozycje), a jego dane mają pierwszeństwo.
Włączane przez FUEL_SENDER w .env (np. orlenpay@orlen.pl); korzysta z tych samych IMAP_*.
"""
import logging
import os
import re
from pathlib import Path

from app import db, fuel_pdf, mailbox
from app import water_mail as w

log = logging.getLogger("paliwo")
RE_NUMBER = re.compile(r"numer\s+[„\"“]?\s*([A-Z]{1,3}\s?\d+/\d+/\d+)", re.I)
RE_STATION = re.compile(r"stacji\s+paliw\s+(.+?),\s+w\s+dniu", re.I)
RE_WHEN = re.compile(r"w\s+dniu\s+(\d{2}\.\d{2}\.\d{4})\s+o\s+godzinie\s+(\d{2}:\d{2})", re.I)
RE_AMOUNT = re.compile(r"na\s+kwot[ęe]\s+" + w.AMOUNT + r"\s*z[łl]", re.I)


def parse_body(msg):
    """Pola z treści maila (numer, data, kwota, stacja z godziną) albo None, jeśli to nie e-faktura."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(w.message_bodies(msg))))
    m = RE_NUMBER.search(text)
    if not m:
        return None
    out = {"invoice_no": re.sub(r"\s+", " ", m.group(1)).upper()}
    if m := RE_WHEN.search(text):
        out["issue_date"] = out["period_start"] = out["period_end"] = w._iso(m.group(1))
        when = m.group(2)
    else:
        when = None
    if m := RE_AMOUNT.search(text):
        out["amount_gross"] = w._num(m.group(1))
    if m := RE_STATION.search(text):
        out["note"] = m.group(1).strip() + (f", {when}" if when else "")
    return out


def handle_message(msg, mid, pdf_dir):
    fields = parse_body(msg)
    if not fields:
        log.info("Mail %s: to nie e-faktura, pomijam", mid)
        mailbox.mark_processed(mid)
        return False
    items, pdf_path = [], None
    for data in mailbox.pdf_attachments(msg)[:1]:
        try:
            text = w.pdf_text(data)
            from_pdf = fuel_pdf.parse_text(text)
            items = fuel_pdf.parse_items(text)
            note = fields.get("note")                      # stacja z godziną z maila jest dokładniejsza niż z PDF-a
            fields = {**fields, **{k: v for k, v in from_pdf.items() if k != "note"}}
            fields["note"] = note or from_pdf.get("note")
        except Exception as e:  # noqa: BLE001
            log.warning("Mail %s: nie udało się odczytać PDF: %s", mid, e)
        path = Path(pdf_dir) / (re.sub(r"[^A-Za-z0-9_-]+", "_", fields["invoice_no"]) + ".pdf")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        pdf_path = str(path)
    created = db.add_bill(provider="ORLEN", utility="paliwo", source="mail-orlenpay",
                          pdf_path=pdf_path, items=items, **fields)
    log.info("Mail %s: %s %s (litry: %s)", mid, "dodano" if created else "faktura już była",
             fields.get("invoice_no"), fields.get("consumption"))
    mailbox.mark_processed(mid)
    return created


def process_mailbox():
    sender = os.environ.get("FUEL_SENDER")
    if not sender:
        return
    pdf_dir = Path(os.environ.get("PDF_DIR", "/pdf")) / "paliwo"
    for mid, msg in mailbox.new_messages(sender, os.environ.get("FUEL_SINCE_DAYS", "400")):
        handle_message(msg, mid, pdf_dir)
