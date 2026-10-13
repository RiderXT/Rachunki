"""Wspólne czytanie skrzynki IMAP dla konektorów mailowych (tylko odczyt, bez zmiany statusu "przeczytane")."""
import email
import hashlib
import imaplib
import os

from app import db
from app import water_mail as w


def new_messages(sender, since_days):
    """Zwraca (message_id, wiadomość) dla maili od `sender`, których jeszcze nie przetworzono."""
    env = os.environ
    imap = imaplib.IMAP4_SSL(env["IMAP_HOST"])
    imap.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
    try:
        imap.select(env.get("IMAP_FOLDER", "INBOX"), readonly=True)
        typ, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", w._imap_since(int(since_days)))
        for num in data[0].split():
            typ, raw = imap.fetch(num, "(RFC822)")
            raw_bytes = raw[0][1]
            msg = email.message_from_bytes(raw_bytes)
            mid = msg.get("Message-ID") or hashlib.sha1(raw_bytes).hexdigest()
            with db.conn() as c:
                if c.execute("SELECT 1 FROM processed_mail WHERE message_id=?", [mid]).fetchone():
                    continue
            yield mid, msg
    finally:
        imap.logout()


def mark_processed(mid):
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO processed_mail (message_id) VALUES (?)", [mid])


def pdf_attachments(msg):
    """Załączniki PDF wiadomości jako lista bajtów."""
    out = []
    for part in msg.walk():
        name = (part.get_filename() or "").lower()
        if part.get_content_type() == "application/pdf" or name.endswith(".pdf"):
            data = part.get_payload(decode=True)
            if data and data.startswith(b"%PDF"):
                out.append(data)
    return out
