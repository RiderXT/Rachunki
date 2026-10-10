"""Konektor wodociągów: mail z linkiem -> PDF (bez logowania) -> parsowanie -> baza.

Uruchomienie:
  python -m app.water_mail            # jednorazowo
  python -m app.water_mail --loop     # w pętli co SYNC_INTERVAL_HOURS
  python -m app.water_mail --dump plik.pdf   # pokaż tekst z PDF-a i co parser z niego wyciągnął
  python -m app.water_mail --inspect URL     # pokaż linki i pola na stronie pośredniej z linku w mailu
"""
import email
import email.utils
import hashlib
import html
import imaplib
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path

from app import db

log = logging.getLogger("woda")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# --- Parsowanie tekstu faktury -------------------------------------------------
# UWAGA: to są wzorce "na oko". Układ faktury wodociągów trzeba sprawdzić na
# prawdziwym PDF-ie (tryb --dump) i ewentualnie poprawić poniższe regexy.
DATE = r"(\d{2}[.\-/]\d{2}[.\-/]\d{4})"
AMOUNT = r"(\d[\d\s\u00a0]*[,.]\d{2})"
# Kolejność ma znaczenie: pierwszy pasujący wzorzec wygrywa ("do zapłaty" ważniejsze niż "razem brutto")
RE_AMOUNTS = [re.compile(w + r"[^\d]{0,40}" + AMOUNT, re.I) for w in (
    r"do\s+zap[łl]aty",
    r"warto[śs][ćc]\s+brutto",
    r"razem\s+brutto",
)]
RE_PERIOD = re.compile(
    r"okres[^\d]{0,40}" + DATE + r"\s*(?:-|–|—|do)\s*" + DATE, re.I)
RE_ISSUE = re.compile(r"data\s+wystawienia[^\d]{0,20}" + DATE, re.I)
RE_INVOICE = re.compile(r"faktura\s*(?:vat\s*)?nr[:.\s]*([A-Za-z0-9/\-_.]+)", re.I)
RE_CONSUMPTION = re.compile(r"zu[żz]ycie[^\d]{0,40}(\d[\d\s\u00a0]*(?:[,.]\d+)?)\s*m\s*[3³]", re.I)


def _num(s):
    return float(re.sub(r"[\s\u00a0]", "", s).replace(",", "."))


def _iso(s):
    d, m, y = re.split(r"[.\-/]", s)
    return f"{y}-{m}-{d}"


def parse_text(text):
    out = {}
    for rx in RE_AMOUNTS:
        if m := rx.search(text):
            out["amount_gross"] = _num(m.group(1))
            break
    if m := RE_PERIOD.search(text):
        out["period_start"], out["period_end"] = _iso(m.group(1)), _iso(m.group(2))
    if m := RE_ISSUE.search(text):
        out["issue_date"] = _iso(m.group(1))
    if m := RE_INVOICE.search(text):
        out["invoice_no"] = m.group(1).rstrip(".")
    if m := RE_CONSUMPTION.search(text):
        out["consumption"], out["unit"] = _num(m.group(1)), "m3"
    return out


def pdf_text(path_or_bytes):
    import io
    import pdfplumber
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    with pdfplumber.open(src) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


# --- Poczta ---------------------------------------------------------------------
def message_bodies(msg: Message):
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload:
                yield payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def extract_links(msg: Message, link_regex):
    seen, links = set(), []
    flt = re.compile(link_regex, re.I)
    for body in message_bodies(msg):
        for url in re.findall(r"https?://[^\s\"'<>)]+", body):
            url = html.unescape(url).rstrip(".,;")
            if url not in seen and flt.search(url):
                seen.add(url)
                links.append(url)
    return links


class _Page(HTMLParser):
    """Wyciąga z HTML-a linki, pola formularza i adres akcji formularza."""

    def __init__(self):
        super().__init__()
        self.anchors, self.inputs, self.form_action, self._a = [], [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self._a = {"href": a.get("href") or "", "text": ""}
            self.anchors.append(self._a)
        elif tag == "input":
            self.inputs.append(a)
        elif tag == "form" and self.form_action is None:
            self.form_action = a.get("action", "")

    def handle_data(self, data):
        if self._a is not None:
            self._a["text"] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self._a = None


def _is_pdf(resp):
    return resp.content.startswith(b"%PDF")


def parse_page(text):
    page = _Page()
    page.feed(text)
    return page


def download_pdf(url):
    """Pobiera PDF. Obsługuje link bezpośredni oraz stronę pośrednią z przyciskiem/linkiem
    "Pobierz dokument" (zwykły link, ASP.NET __doPostBack albo przycisk submit)."""
    import requests
    from urllib.parse import urljoin
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (rachunki-domowe)"
    r = s.get(url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    if _is_pdf(r):
        return r.content

    page = parse_page(r.text)
    text_rx = re.compile(os.environ.get("WATER_DOWNLOAD_TEXT", r"pobierz"), re.I)
    href_rx = re.compile(r"download|\.pdf|getfile|attachment|DownloadDocument", re.I)
    hidden = {i["name"]: i.get("value", "") for i in page.inputs
              if i.get("name") and i.get("type", "").lower() == "hidden"}
    form_url = urljoin(r.url, page.form_action) if page.form_action else r.url
    tried = []

    def attempt(label, fn):
        tried.append(label)
        try:
            resp = fn()
            if resp.ok and _is_pdf(resp):
                return resp.content
        except Exception as e:  # noqa: BLE001
            log.debug("próba %s: %s", label, e)
        return None

    for a in page.anchors:
        if not (text_rx.search(a["text"]) or href_rx.search(a["href"])):
            continue
        href = a["href"].strip()
        if href.lower().startswith("javascript:"):
            m = re.search(r"__doPostBack\('([^']*)'\s*,\s*'([^']*)'\)", href)
            if m:
                data = {**hidden, "__EVENTTARGET": m.group(1), "__EVENTARGUMENT": m.group(2)}
                if got := attempt(f"postback {m.group(1)}",
                                  lambda d=data: s.post(form_url, data=d, timeout=30)):
                    return got
        elif href and not href.startswith("#"):
            if got := attempt(href[:60], lambda h=href: s.get(urljoin(r.url, h), timeout=30)):
                return got
    for i in page.inputs:
        if i.get("type", "").lower() in ("submit", "image", "button") and i.get("name") \
                and text_rx.search(i.get("value", "") + i.get("alt", "")):
            data = {**hidden, i["name"]: i.get("value", "")}
            if got := attempt(f"submit {i['name']}", lambda d=data: s.post(form_url, data=d, timeout=30)):
                return got
    raise ValueError(f"nie znalazłem PDF-a na stronie (próby: {tried or 'brak pasujących linków'}); "
                     "użyj --inspect <url>")


def inspect_url(url):
    import requests
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    print("status:", r.status_code, "| Content-Type:", r.headers.get("Content-Type"), "| URL:", r.url)
    if _is_pdf(r):
        print("to bezpośredni PDF")
        return
    page = parse_page(r.text)
    print("form action:", page.form_action)
    print("--- linki ---")
    for a in page.anchors:
        print(repr(a["text"].strip()), "->", a["href"][:150])
    print("--- pola formularza ---")
    for i in page.inputs:
        v = (i.get("value") or "")[:40]
        print(i.get("type"), i.get("name"), repr(v))


def _imap_since(days):
    d = datetime.now() - timedelta(days=days)
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"  # bez locale


def process_mailbox():
    env = os.environ
    pdf_dir = Path(env.get("PDF_DIR", "/pdf")) / "woda"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    link_regex = env.get("WATER_LINK_REGEX", r"faktur|\.pdf|download|pobierz|DocumentForm")

    imap = imaplib.IMAP4_SSL(env["IMAP_HOST"])
    imap.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
    try:
        imap.select(env.get("IMAP_FOLDER", "INBOX"), readonly=True)  # nie zmienia statusu "przeczytane"
        since = _imap_since(int(env.get("WATER_SINCE_DAYS", "400")))
        typ, data = imap.search(None, "FROM", f'"{env["WATER_SENDER"]}"', "SINCE", since)
        ids = data[0].split()
        log.info("Znaleziono %d maili od %s", len(ids), env["WATER_SENDER"])
        for num in ids:
            typ, raw = imap.fetch(num, "(RFC822)")
            raw_bytes = raw[0][1]
            msg = email.message_from_bytes(raw_bytes)
            mid = msg.get("Message-ID") or hashlib.sha1(raw_bytes).hexdigest()
            with db.conn() as c:
                if c.execute("SELECT 1 FROM processed_mail WHERE message_id=?", [mid]).fetchone():
                    continue
            handle_message(msg, mid, pdf_dir, link_regex)
    finally:
        imap.logout()


def handle_message(msg, mid, pdf_dir, link_regex):
    links = extract_links(msg, link_regex)
    if not links:
        log.warning("Mail %s: brak linków pasujących do WATER_LINK_REGEX", mid)
        return
    content, used = None, None
    for url in links:
        try:
            content, used = download_pdf(url), url
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Mail %s: link %s odrzucony: %s", mid, url[:80], e)
    if content is None:
        return  # nie oznaczamy jako przetworzony -> kolejna próba przy następnym przebiegu

    path = pdf_dir / f"{datetime.now():%Y%m%d}_{hashlib.sha1(content).hexdigest()[:10]}.pdf"
    path.write_bytes(content)
    fields = parse_text(pdf_text(content))
    fields.setdefault("issue_date", email.utils.parsedate_to_datetime(msg["Date"]).date().isoformat()
                      if msg["Date"] else None)
    created = db.add_bill(provider="Wodociągi", utility="woda", source="mail-woda",
                          pdf_path=str(path), **fields)
    log.info("Mail %s: %s %s", mid, "dodano" if created else "duplikat", fields)
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO processed_mail (message_id) VALUES (?)", [mid])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if "--inspect" in sys.argv:
        inspect_url(sys.argv[sys.argv.index("--inspect") + 1])
        return
    if "--dump" in sys.argv:
        text = pdf_text(sys.argv[sys.argv.index("--dump") + 1])
        print(text, "\n--- parser wyciągnął ---\n", parse_text(text))
        return
    loop = "--loop" in sys.argv
    hours = float(os.environ.get("SYNC_INTERVAL_HOURS", "6"))
    while True:
        try:
            process_mailbox()
        except Exception:  # noqa: BLE001
            log.exception("Błąd synchronizacji")
        if not loop:
            break
        time.sleep(hours * 3600)


if __name__ == "__main__":
    main()
