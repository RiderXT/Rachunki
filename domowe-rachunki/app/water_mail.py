"""Konektor wodociągów: mail z linkiem -> PDF (bez logowania) -> parsowanie -> baza.

Uruchomienie:
  python -m app.water_mail            # jednorazowo
  python -m app.water_mail --loop     # w pętli co SYNC_INTERVAL_HOURS
  python -m app.water_mail --dump plik.pdf   # pokaż tekst z PDF-a i co parser z niego wyciągnął
  python -m app.water_mail --reparse         # ponownie sparsuj zapisane PDF-y (po zmianie wzorców)
  python -m app.water_mail --reset --yes     # usuń faktury z maila i pobierz od nowa
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
# Obsługiwane układy faktur ZGK: daty "16-09-2026" / "16.09.2026" oraz "2025-12-18".
DATE_ANY = r"(\d{2}[.\-/]\d{2}[.\-/]\d{4}|\d{4}-\d{2}-\d{2})"
AMOUNT = r"(\d[\d\s\u00a0]*[,.]\d{2})"

# Kolejność ma znaczenie: pierwszy pasujący wzorzec wygrywa
RE_AMOUNT_ZL_GR = re.compile(r"do\s+zap[łl]aty:?\s*(\d[\d\s\u00a0]*?)\s*z[łl]\s*(\d{2})\s*gr", re.I)  # "361 zł 82 gr"
RE_AMOUNTS = [re.compile(w + r"[^\d]{0,40}" + AMOUNT, re.I) for w in (
    r"do\s+zap[łl]aty",
    r"nale[żz]no[śs][ćc]:",          # starszy układ: "Należność: 170,76 PLN"
    r"warto[śs][ćc]\s+brutto",
    r"razem\s+brutto",
)]
# Okres zużycia: "Okres: 12.08.2026...10.09.2026" albo "Okres: 2025-11-13...2025-12-15".
RE_USAGE_PERIOD = re.compile(
    r"okres:?\s*" + DATE_ANY + r"\s*(?:\.{2,3}|…|-|–|—|do)\s*" + DATE_ANY, re.I)
# Abonament ma skrót: "Okres: 1...30.09.2026" albo "Okres: 2025-12-01...31".
RE_SUBSCR_DMY = re.compile(r"okres:?\s*(\d{1,2})\s*\.{2,3}\s*(\d{2})[.\-/](\d{2})[.\-/](\d{4})", re.I)
RE_SUBSCR_ISO = re.compile(r"okres:?\s*(\d{4})-(\d{2})-(\d{2})\s*\.{2,3}\s*(\d{1,2})\b", re.I)
RE_ISSUE = re.compile(r"data\s+wystawienia:?[^\d]{0,20}" + DATE_ANY, re.I)
RE_INVOICE_NO = re.compile(r"\b([A-Z]{1,4}/\d{4}/\d{3,})\b")          # np. R/2026/063407
RE_INVOICE_NR = re.compile(r"faktura\s*(?:vat\s*)?nr[:.\s]*([A-Za-z0-9/\-_.]+)", re.I)


def _num(s):
    return float(re.sub(r"[\s\u00a0]", "", s).replace(",", "."))


def _iso(s):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    d, m, y = re.split(r"[.\-/]", s)
    return f"{y}-{m}-{d}"


def _sub_dmy(m):
    sd, ed, mo, y = m.groups()
    return f"{y}-{mo}-{int(sd):02d}", f"{y}-{mo}-{ed}"


def _sub_iso(m):
    y, mo, sd, ed = m.groups()
    return f"{y}-{mo}-{sd}", f"{y}-{mo}-{int(ed):02d}"


def find_period(text):
    """Pierwszy okres z tekstu: najpierw zużycie (pełne daty), potem skrót abonamentowy."""
    if r := RE_USAGE_PERIOD.search(text):
        return _iso(r.group(1)), _iso(r.group(2))
    if r := RE_SUBSCR_DMY.search(text):
        return _sub_dmy(r)
    if r := RE_SUBSCR_ISO.search(text):
        return _sub_iso(r)
    return None


def parse_text(text):
    out = {}
    items = parse_items(text)
    if m := RE_AMOUNT_ZL_GR.search(text):
        out["amount_gross"] = float(f"{_num(m.group(1)):.0f}.{m.group(2)}")
    else:
        for rx in RE_AMOUNTS:
            if m := rx.search(text):
                out["amount_gross"] = _num(m.group(1))
                break
    if "amount_gross" not in out and items:  # ostatnia deska ratunku: suma pozycji
        out["amount_gross"] = round(sum(i["amount_gross"] for i in items), 2)
    # okres rozliczeniowy: najwcześniejszy początek i najpóźniejszy koniec pozycji zużycia
    ranges = [(_iso(a), _iso(b)) for a, b in RE_USAGE_PERIOD.findall(text)]
    if not ranges:  # tylko abonament -> od podanego dnia do końca podanego miesiąca
        ranges = [_sub_dmy(m) for m in RE_SUBSCR_DMY.finditer(text)] + \
                 [_sub_iso(m) for m in RE_SUBSCR_ISO.finditer(text)]
    if ranges:
        out["period_start"] = min(r[0] for r in ranges)
        out["period_end"] = max(r[1] for r in ranges)
    if m := RE_ISSUE.search(text):
        out["issue_date"] = _iso(m.group(1))
    if m := RE_INVOICE_NO.search(text) or RE_INVOICE_NR.search(text):
        out["invoice_no"] = m.group(1).rstrip(".")
    water = [i["quantity"] for i in items if i["kind"] == "woda" and i["unit"] == "m3"]
    if water:  # ścieki pomijamy: liczą tę samą wodę drugi raz
        out["consumption"], out["unit"] = round(sum(water), 2), "m3"
    return out


# Pozycje faktury: przypisanie do licznika po fragmentach tytułu i opisu licznika (małe litery),
# sprawdzane po kolei. "/podlicznik" (a nie "podliczniki:") odróżnia podlicznik od linii
# "Podliczniki: 45;" na fakturze licznika głównego. Dopasuj, jeśli wodociągi zmienią nazewnictwo.
METER_RULES = [("/podlicznik", "ogrod"), ("pozostali odbiorcy", "ogrod"),
               ("gospodarstw", "dom"), ("licznik:", "dom")]
RE_ITEM_HEAD = re.compile(r"^(woda\s+-\s|[śs]cieki\s+-\s|op[łl]ata\s+abonamentowa\b)", re.I)
RE_ITEM_LINE = re.compile(
    r"(?<![\d,.])(\d+(?:[,.]\d+)?)\s*(m\s*[3³]|szt\.?|kpl\.?|mies\.?)\s+"
    r"(\d[\d\s\u00a0]*[,.]\d{2})\s*PLN\s+(?:(\d[\d\s\u00a0]*[,.]\d{2})\s*PLN\s+)?"
    r"(\d+)\s*%\s+(\d[\d\s\u00a0]*[,.]\d{2})", re.I)


def _meter_for(title, body=""):
    t = f"{title} {body}".lower()
    for fragment, meter in METER_RULES:
        if fragment in t:
            return meter
    return "inny"


def parse_items(text):
    """Pozycje faktury: licznik (dom/ogród), rodzaj (woda/ścieki/abonament), ilość, kwota brutto, okres."""
    blocks, cur = [], None
    for line in (l.strip() for l in text.splitlines()):
        if RE_ITEM_HEAD.match(line):
            cur = {"title": line, "lines": []}
            blocks.append(cur)
        elif re.match(r"^(suma:|stawka\s+vat)", line, re.I):
            cur = None
        elif cur is not None:
            cur["lines"].append(line)
    items = []
    for b in blocks:
        m = next((RE_ITEM_LINE.search(l) for l in b["lines"] if RE_ITEM_LINE.search(l)), None)
        if not m:
            continue
        title = b["title"].lower()
        kind = ("scieki" if re.match(r"[śs]cieki", title) else
                "abonament" if re.match(r"op[łl]ata", title) else "woda")
        body = "\n".join(b["lines"])
        period = find_period(body)
        items.append({"meter": _meter_for(b["title"], body), "kind": kind, "quantity": _num(m.group(1)),
                      "unit": "m3" if re.match(r"m\s*[3³]", m.group(2), re.I) else "szt",
                      "amount_gross": _num(m.group(6)),
                      "period_start": period[0] if period else None,
                      "period_end": period[1] if period else None})
    return items


def check_items(items, amount, label=""):
    if items and amount is not None and abs(sum(i["amount_gross"] for i in items) - amount) > 0.05:
        log.warning("%s suma pozycji %.2f != kwota faktury %.2f", label, sum(i["amount_gross"] for i in items), amount)


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

    s.headers["Referer"] = r.url
    page = parse_page(r.text)
    text_rx = re.compile(os.environ.get("WATER_DOWNLOAD_TEXT", r"pobierz"), re.I)
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
            log.warning("próba %s: status %s, Content-Type %s (to nie PDF)",
                        label, resp.status_code, resp.headers.get("Content-Type"))
        except Exception as e:  # noqa: BLE001
            log.debug("próba %s: %s", label, e)
        return None

    for a in page.anchors:
        if not text_rx.search(a["text"]):  # tylko po tekście; linki do instrukcji .pdf ignorujemy
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
            variants = [
                {**hidden, i["name"]: i.get("value", "")},                      # klik przycisku
                {**hidden, "__EVENTTARGET": i["name"], "__EVENTARGUMENT": ""},  # __doPostBack z JS
            ]
            for n, data in enumerate(variants, 1):
                if got := attempt(f"przycisk {i['name']} (wariant {n})",
                                  lambda d=data: s.post(form_url, data=d, timeout=30)):
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


def _pdf_name(invoice_no, content):
    if invoice_no:
        return re.sub(r"[^A-Za-z0-9_-]+", "_", invoice_no) + ".pdf"   # R/2026/063407 -> R_2026_063407.pdf
    return f"{datetime.now():%Y%m%d}_{hashlib.sha1(content).hexdigest()[:10]}.pdf"


def handle_message(msg, mid, pdf_dir, link_regex):
    links = extract_links(msg, link_regex)
    if not links:
        log.warning("Mail %s: brak linków pasujących do WATER_LINK_REGEX (pomijam, to nie faktura?)", mid)
        with db.conn() as c:
            c.execute("INSERT OR IGNORE INTO processed_mail (message_id) VALUES (?)", [mid])
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

    text = pdf_text(content)
    fields = parse_text(text)
    items = parse_items(text)
    check_items(items, fields.get("amount_gross"), f"Mail {mid}:")
    path = pdf_dir / _pdf_name(fields.get("invoice_no"), content)
    path.write_bytes(content)
    fields.setdefault("issue_date", email.utils.parsedate_to_datetime(msg["Date"]).date().isoformat()
                      if msg["Date"] else None)
    created = db.add_bill(provider="Wodociągi", utility="woda", source="mail-woda",
                          pdf_path=str(path), items=items, **fields)
    log.info("Mail %s: %s %s (pozycji: %d)", mid, "dodano" if created else "duplikat", fields, len(items))
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO processed_mail (message_id) VALUES (?)", [mid])


EDITABLE = ["provider", "utility", "invoice_no", "issue_date", "period_start", "period_end",
            "amount_gross", "consumption", "unit"]


def reparse():
    """Ponownie parsuje zapisane PDF-y (po poprawie wzorców): uzupełnia pola, pozycje i nazywa pliki numerem faktury."""
    with db.conn() as c:
        rows = c.execute("SELECT id, pdf_path FROM bills WHERE source='mail-woda' "
                         "AND pdf_path IS NOT NULL").fetchall()
    for r in rows:
        try:
            with open(r["pdf_path"], "rb") as fh:
                content = fh.read()
            text = pdf_text(content)
            fields, items = parse_text(text), parse_items(text)
            with db.conn() as c:
                cur = c.execute("SELECT * FROM bills WHERE id=?", [r["id"]]).fetchone()
            merged = {k: cur[k] for k in EDITABLE}
            merged.update(fields)
            db.update_bill(r["id"], **merged)
            db.replace_items(r["id"], items)
            check_items(items, merged["amount_gross"], f"ID {r['id']}:")
            new_path = Path(r["pdf_path"]).with_name(_pdf_name(merged["invoice_no"], content))
            if str(new_path) != r["pdf_path"] and not new_path.exists():
                os.rename(r["pdf_path"], new_path)
                with db.conn() as c:
                    c.execute("UPDATE bills SET pdf_path=? WHERE id=?", [str(new_path), r["id"]])
            log.info("ID %s: %s | pozycji: %d | %s", r["id"], merged["invoice_no"], len(items),
                     "OK" if db.compute_status(merged) == "ok" else "NIEPEŁNE: " + ", ".join(
                         k for k in ("amount_gross", "period_start", "period_end") if not merged[k]))
        except Exception as e:  # noqa: BLE001
            log.warning("ID %s: %s", r["id"], e)


def reset_water():
    """Usuwa faktury pobrane z maila (i ich PDF-y) oraz historię przetworzonych maili."""
    with db.conn() as c:
        paths = [r["pdf_path"] for r in c.execute(
            "SELECT pdf_path FROM bills WHERE source='mail-woda' AND pdf_path IS NOT NULL")]
        n_bills = c.execute("DELETE FROM bills WHERE source='mail-woda'").rowcount
        n_mail = c.execute("DELETE FROM processed_mail").rowcount
    for p in set(paths):
        try:
            os.remove(p)
        except OSError:
            pass
    log.info("Usunięto faktur: %d, wpisów o mailach: %d, plików PDF: %d", n_bills, n_mail, len(set(paths)))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if "--inspect" in sys.argv:
        inspect_url(sys.argv[sys.argv.index("--inspect") + 1])
        return
    if "--reparse" in sys.argv:
        reparse()
        return
    if "--reset" in sys.argv:
        if "--yes" not in sys.argv:
            print("To usuwa wszystkie faktury pobrane z maila. Dodaj --yes, żeby potwierdzić.")
            return
        reset_water()
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
        if os.environ.get("ENEA_SENDER"):
            try:
                from app import enea_mail
                enea_mail.process_mailbox()
            except Exception:  # noqa: BLE001
                log.exception("Błąd synchronizacji powiadomień Enea")
        if not loop:
            break
        time.sleep(hours * 3600)


if __name__ == "__main__":
    main()
