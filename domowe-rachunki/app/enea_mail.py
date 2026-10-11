"""Powiadomienia Enea o nowej fakturze (mail) -> rekord faktury, do którego dołączasz potem PDF.

Mail z powiadomienia zawiera numer faktury i datę, ale nie zawiera kwoty brutto ani okresu (jest tylko
"należność", czyli ile zostało do zapłaty), więc rekord ma status "niepełne" do czasu wgrania PDF-a
(zakładka "Wgraj PDF"). Numer z PDF-a jest dopasowywany do rekordu automatycznie.

Włączane przez ENEA_SENDER w .env (np. powiadomienia@ebok.enea.pl); korzysta z tych samych IMAP_*.
"""
import email
import email.utils
import hashlib
import imaplib
import logging
import os
import re

from app import db
from app import water_mail as w

log = logging.getLogger("enea")
RE_NUMBER = re.compile(r"numerze\s+([A-Z]{1,4}/\d+/\d+/\d+)", re.I)


def parse_notification(msg):
    """Numer faktury i data wystawienia (z nagłówka maila) albo None, jeśli to nie powiadomienie o fakturze."""
    text = re.sub(r"<[^>]+>", " ", " ".join(w.message_bodies(msg)))
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


def handle_message(msg, mid):
    info = parse_notification(msg)
    created = False
    if info:
        created = db.add_bill(provider="Enea", utility="prad", source="mail-enea", **info)
        log.info("Mail %s: %s %s", mid, "dodano fakturę (czeka na PDF)" if created else "faktura już była", info)
    else:
        log.info("Mail %s: to nie powiadomienie o fakturze, pomijam", mid)
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO processed_mail (message_id) VALUES (?)", [mid])
    return created


def process_mailbox():
    env = os.environ
    sender = env.get("ENEA_SENDER")
    if not sender:
        return
    imap = imaplib.IMAP4_SSL(env["IMAP_HOST"])
    imap.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
    try:
        imap.select(env.get("IMAP_FOLDER", "INBOX"), readonly=True)
        since = w._imap_since(int(env.get("ENEA_SINCE_DAYS", "400")))
        typ, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", since)
        ids = data[0].split()
        log.info("Znaleziono %d maili od %s", len(ids), sender)
        for num in ids:
            typ, raw = imap.fetch(num, "(RFC822)")
            raw_bytes = raw[0][1]
            msg = email.message_from_bytes(raw_bytes)
            mid = msg.get("Message-ID") or hashlib.sha1(raw_bytes).hexdigest()
            with db.conn() as c:
                if c.execute("SELECT 1 FROM processed_mail WHERE message_id=?", [mid]).fetchone():
                    continue
            handle_message(msg, mid)
    finally:
        imap.logout()
