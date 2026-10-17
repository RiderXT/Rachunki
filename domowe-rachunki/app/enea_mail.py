"""Powiadomienia Enea o nowej fakturze (mail) -> rekord faktury, do którego dołączasz potem PDF.

Mail z powiadomienia zawiera numer faktury i datę, ale nie zawiera kwoty brutto ani okresu (jest tylko
"należność", czyli ile zostało do zapłaty), więc rekord ma status "niepełne" do czasu wgrania PDF-a
(zakładka "Wgraj PDF"). Numer z PDF-a jest dopasowywany do rekordu automatycznie.

Włączane przez ENEA_SENDER w .env (np. powiadomienia@ebok.enea.pl); korzysta z tych samych IMAP_*.
"""
import email.utils
import logging
import os
import re

from app import db, mailbox
from app import water_mail as w

log = logging.getLogger("enea")
RE_NUMBER = re.compile(r"numerze\s+([A-Z]{1,4}/\d+/\d+/\d+)", re.I)


def parse_notification(msg):
    """Numer faktury i data wystawienia (z nagłówka maila) albo None, jeśli to nie powiadomienie o fakturze."""
    text = mailbox.text_of(msg)
    m = RE_NUMBER.search(text)
    if not m:
        return None
    issue = None
    if msg["Date"]:
        try:
            issue = email.utils.parsedate_to_datetime(msg["Date"]).date().isoformat()
        except (TypeError, ValueError):
            pass
    return {"invoice_no": m.group(1).upper(), "issue_date": issue}


NS = "enea2:"          # nowy przedrostek znaczników: maile pominięte starszą wersją są sprawdzane ponownie


def handle_message(msg, mid):
    info = parse_notification(msg)
    created = False
    if info:
        created = db.add_bill(provider="Enea", utility="prad", source="mail-enea", **info)
        log.info("Mail %s: %s %s", mid, "dodano fakturę (czeka na PDF)" if created else "faktura już była", info)
    else:
        text = mailbox.text_of(msg).lower()
        if "faktur" in text and "numer" in text:       # wygląda na powiadomienie, ale nie rozpoznano: nie "zapominamy"
            log.warning("Mail %s wygląda na powiadomienie o fakturze, ale nie rozpoznano numeru (nie oznaczam jako "
                        "przetworzony)", mid)
            return False
        log.info("Mail %s: to nie powiadomienie o fakturze, pomijam", mid)
    mailbox.mark_processed(NS + mid)
    return created


def process_mailbox():
    sender = os.environ.get("ENEA_SENDER")
    if not sender:
        return
    for mid, msg in mailbox.new_messages(sender, os.environ.get("ENEA_SINCE_DAYS", "400"), NS, os.environ.get("ENEA_FOLDER")):
        handle_message(msg, mid)
