"""Wspólne czytanie skrzynki IMAP dla konektorów mailowych (tylko odczyt, bez zmiany statusu "przeczytane")."""
import email
import hashlib
import html
import imaplib
import logging
import os
import re

from app import db
from app import water_mail as w

log = logging.getLogger("poczta")


def text_of(msg):
    """Czysty tekst maila: bez tagów HTML, z rozkodowanymi encjami (&nbsp;, &bdquo;...), pojedyncze spacje."""
    raw = " ".join(w.message_bodies(msg))
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _iter(sender, since_days, folder=None):
    """Wszystkie maile od `sender` z ostatnich `since_days` dni: (message_id, wiadomość)."""
    env = os.environ
    folder = folder or env.get("IMAP_FOLDER", "INBOX")
    imap = imaplib.IMAP4_SSL(env["IMAP_HOST"])
    imap.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
    try:
        imap.select(folder, readonly=True)
        typ, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", w._imap_since(int(since_days)))
        ids = data[0].split()
        log.info("Znaleziono %d maili od %s (folder %s)", len(ids), sender, folder)
        for num in ids:
            typ, raw = imap.fetch(num, "(RFC822)")
            raw_bytes = raw[0][1]
            msg = email.message_from_bytes(raw_bytes)
            yield (msg.get("Message-ID") or hashlib.sha1(raw_bytes).hexdigest()), msg
    finally:
        imap.logout()


def is_processed(key):
    with db.conn() as c:
        return c.execute("SELECT 1 FROM processed_mail WHERE message_id=?", [key]).fetchone() is not None


def new_messages(sender, since_days, namespace="", folder=None):
    """Maile od `sender`, których jeszcze nie przetworzono. Znacznik przetworzenia to namespace + message_id
    (zmiana przedrostka namespace powoduje ponowne przejrzenie wszystkich maili)."""
    for mid, msg in _iter(sender, since_days, folder):
        if not is_processed(namespace + mid):
            yield mid, msg


def all_messages(sender, since_days, namespace="", folder=None):
    """Jak new_messages, ale zwraca wszystkie maile wraz ze znacznikiem 'przetworzony' (do diagnostyki)."""
    for mid, msg in _iter(sender, since_days, folder):
        yield mid, msg, is_processed(namespace + mid)


def mark_processed(key):
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO processed_mail (message_id) VALUES (?)", [key])


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
