"""Faktury za paliwo z maili ORLEN Pay: po tankowaniu przychodzi mail z e-fakturą (i załączonym PDF-em).

Dane bierzemy z dwóch niezależnych źródeł: z treści maila (numer, stacja z godziną, data, kwota) i z załączonego
PDF-a (numer, kwota, litry, rodzaj paliwa). Wystarczy któreś z nich; gdy są oba, PDF ma pierwszeństwo.
Włączane przez FUEL_SENDER w .env (np. orlenpay@orlen.pl); korzysta z tych samych IMAP_*.

  python -m app.fuel_mail --check     # co konektor widzi w skrzynce i jak to odczytuje (nic nie zapisuje)
"""
import logging
import os
import re
import sys
from pathlib import Path

from app import db, fuel_pdf, mailbox
from app import water_mail as w

log = logging.getLogger("paliwo")
NS = "fuel2:"                          # nowy przedrostek znaczników: maile pominięte starszą wersją są sprawdzane ponownie
RE_NUMBER = re.compile(r"numer\s+[„\"“”«]?\s*([A-Z]{1,3}\s?\d+/\d+/\d+)", re.I)
RE_NUMBER_ANY = re.compile(r"\b([A-Z]\s\d{3,7}/\d{3,5}/\d{2})\b")
RE_STATION = re.compile(r"stacji\s+paliw\s+(.+?),\s+w\s+dniu", re.I)
RE_WHEN = re.compile(r"w\s+dniu\s+(\d{2}\.\d{2}\.\d{4})\s+o\s+godzinie\s+(\d{2}:\d{2})", re.I)
RE_AMOUNT = re.compile(r"na\s+kwot[ęe]\s+" + w.AMOUNT + r"\s*z[łl]", re.I)


def parse_text_fields(text):
    """Pola z tekstu maila (numer, data, kwota, stacja z godziną) albo None, jeśli nie ma numeru e-faktury."""
    m = RE_NUMBER.search(text) or RE_NUMBER_ANY.search(text)
    if not m:
        return None
    out = {"invoice_no": re.sub(r"\s+", " ", m.group(1)).upper()}
    when = None
    if m := RE_WHEN.search(text):
        out["issue_date"] = out["period_start"] = out["period_end"] = w._iso(m.group(1))
        when = m.group(2)
    if m := RE_AMOUNT.search(text):
        out["amount_gross"] = w._num(m.group(1))
    if m := RE_STATION.search(text):
        out["note"] = m.group(1).strip() + (f", {when}" if when else "")
    return out


def parse_body(msg):
    return parse_text_fields(mailbox.text_of(msg))


def _from_pdf(msg):
    """(bajty PDF-a, pola, pozycje) z pierwszego załącznika PDF; puste, gdy brak lub nie da się odczytać."""
    for data in mailbox.pdf_attachments(msg)[:1]:
        try:
            text = w.pdf_text(data)
            return data, fuel_pdf.parse_text(text), fuel_pdf.parse_items(text)
        except Exception as e:  # noqa: BLE001
            log.warning("nie udało się odczytać PDF z maila: %s", e)
            return data, {}, []
    return None, {}, []


def handle_message(msg, mid, pdf_dir):
    body = parse_body(msg) or {}
    data, from_pdf, items = _from_pdf(msg)
    if not (body.get("invoice_no") or from_pdf.get("invoice_no")):
        if "faktur" in mailbox.text_of(msg).lower():     # wygląda na fakturę, ale nie umiemy jej odczytać: nie "zapominamy"
            log.warning("Mail %s wygląda na fakturę, ale nie rozpoznano numeru (nie oznaczam jako przetworzony; "
                        "sprawdź: python -m app.fuel_mail --check)", mid)
        else:
            log.info("Mail %s: to nie e-faktura, pomijam", mid)
            mailbox.mark_processed(NS + mid)
        return False
    fields = {**body, **{k: v for k, v in from_pdf.items() if k != "note"}}
    fields["note"] = body.get("note") or from_pdf.get("note")      # stacja z godziną z maila jest dokładniejsza
    pdf_path = None
    if data:
        path = Path(pdf_dir) / (re.sub(r"[^A-Za-z0-9_-]+", "_", fields["invoice_no"]) + ".pdf")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        pdf_path = str(path)
    created = db.add_bill(provider="ORLEN", utility="paliwo", source="mail-orlenpay",
                          pdf_path=pdf_path, items=items, **fields)
    log.info("Mail %s: %s %s (litry: %s, źródło danych: %s)", mid, "dodano" if created else "faktura już była",
             fields["invoice_no"], fields.get("consumption"), "PDF + treść" if body and from_pdf else "PDF" if from_pdf else "treść")
    mailbox.mark_processed(NS + mid)
    return created


def process_mailbox():
    sender = os.environ.get("FUEL_SENDER")
    if not sender:
        return
    pdf_dir = Path(os.environ.get("PDF_DIR", "/pdf")) / "paliwo"
    for mid, msg in mailbox.new_messages(sender, os.environ.get("FUEL_SINCE_DAYS", "400"), NS,
                                         os.environ.get("FUEL_FOLDER")):
        handle_message(msg, mid, pdf_dir)


def check():
    """Diagnostyka: pokazuje maile od nadawcy i to, jak konektor je odczytuje. Niczego nie zapisuje."""
    from email.header import decode_header, make_header
    env = os.environ
    sender = env.get("FUEL_SENDER") or "orlenpay@orlen.pl"
    since = env.get("FUEL_SINCE_DAYS", "400")
    folder = env.get("FUEL_FOLDER") or env.get("IMAP_FOLDER", "INBOX")
    print(f"Konto: {env.get('IMAP_USER')} | folder: {folder} | nadawca zawiera: {sender} | z ostatnich {since} dni")
    if not env.get("FUEL_SENDER"):
        print("UWAGA: FUEL_SENDER nie jest ustawiony w tym kontenerze (konektor jest wyłączony). Dopisz go do .env "
              "i odtwórz kontener: docker compose up -d --force-recreate water-sync")
    n = 0
    for mid, msg, done in mailbox.all_messages(sender, since, NS, env.get("FUEL_FOLDER")):
        n += 1
        subject = str(make_header(decode_header(msg.get("Subject", ""))))[:50]
        body = parse_body(msg)
        _, from_pdf, items = _from_pdf(msg)
        with db.conn() as c:
            no = (body or from_pdf or {}).get("invoice_no")
            in_db = bool(no and c.execute("SELECT 1 FROM bills WHERE provider='ORLEN' AND invoice_no=?", [no]).fetchone())
        print(f"- {msg.get('Date', '?')[:25]} | {subject} | PDF: {len(mailbox.pdf_attachments(msg))} | "
              f"treść: {body.get('invoice_no') + ' ' + str(body.get('amount_gross', '?')) + ' zł' if body else 'NIE ROZPOZNANO'} | "
              f"z PDF: {from_pdf.get('invoice_no', '-')} {from_pdf.get('consumption', '-')} l, pozycji {len(items)} | "
              f"przetworzony: {'tak' if done else 'nie'} | w bazie: {'tak' if in_db else 'nie'}")
    if n == 0:
        print("\nNie znaleziono żadnych maili. Sprawdzam, czy w skrzynce są maile od podobnych nadawców (ORLEN):")
        seen = {}
        for _, msg in mailbox._iter("orlen", since, env.get("FUEL_FOLDER")):
            seen[msg.get("From", "?")] = seen.get(msg.get("From", "?"), 0) + 1
        print(seen or "brak. Być może maile są w innym folderze (ustaw FUEL_FOLDER, np. \"[Gmail]/Wszystkie\") "
                      "albo nadawca jest inny.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if "--check" in sys.argv:
        check()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
