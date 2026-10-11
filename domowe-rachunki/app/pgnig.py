"""Konektor PGNiG / myORLEN eBOK (gaz). Nieoficjalne API tego samego serwisu, którego używa przeglądarka.

  python -m app.pgnig --probe             # diagnostyka: logowanie + struktura odpowiedzi (dane osobowe zamaskowane)
  python -m app.pgnig --sync --dry-run    # pokaż, co zostałoby zaimportowane (nic nie zapisuje)
  python -m app.pgnig --sync              # zaimportuj faktury i pobierz PDF-y
  python -m app.pgnig --loop              # sync co PGNIG_INTERVAL_HOURS (domyślnie 24)

Dane logowania w .env: PGNIG_LOGIN (login do eBOK) i PGNIG_PASSWORD. Token działa ok. godziny; każdy
przebieg to jedno logowanie. Obsługiwane jest logowanie bezpośrednie (konto eBOK, dawniej PGNiG).
"""
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

import requests

from app import db

log = logging.getLogger("pgnig")
BASE = os.environ.get("PGNIG_BASE", "https://ebok.myorlen.pl")
API = {"api-version": "3.0"}
PROVIDER, UTILITY = "PGNiG", "gaz"

# Maskujemy WARTOŚCI pól o wrażliwych nazwach; struktura (nazwy pól) zostaje, bo jej potrzebujemy.
SENSITIVE = re.compile(
    r"token|passw|accesspin|^pin$|mail|phone|telefon|^tel$|address|adres|street|ulica|city|miasto|"
    r"zip|postal|surname|name$|pesel|nip|iban|account|konto|ppg|meternumber|contract|agreement|"
    r"customer|login|identif", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
IBAN = re.compile(r"(?:PL)?\s?\d{2}(?:\s?\d{4}){6}")


def redact(obj, key=""):
    """Struktura z maskowaniem wrażliwych wartości i skracaniem list (do wklejenia w czacie)."""
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        shown = [redact(v, key) for v in obj[:2]]
        return shown + ([f"... (łącznie {len(obj)})"] if len(obj) > 2 else [])
    if SENSITIVE.search(key or ""):
        return "***"
    if isinstance(obj, str):
        obj = IBAN.sub("***", EMAIL.sub("***", obj))
        return obj if len(obj) <= 60 else obj[:57] + "..."
    return obj


def _device_id():
    """Stały identyfikator urządzenia (zapisany obok bazy), żeby logowania wyglądały jak z jednego urządzenia."""
    path = Path(os.environ.get("DB_PATH", "/data/rachunki.sqlite")).parent / "pgnig_device_id"
    try:
        if path.exists():
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        value = uuid.uuid4().hex
        path.write_text(value)
        return value
    except OSError:
        return uuid.uuid4().hex


class PgnigError(Exception):
    pass


class Client:
    def __init__(self, login, password):
        self.login_name, self.password = login, password
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (rachunki-domowe)", "Accept": "application/json",
                               "Content-Type": "application/json", "Origin": BASE})
        self.token = None

    def login(self):
        r = self.s.post(f"{BASE}/auth/login", params=API, timeout=30, json={
            "identificator": self.login_name, "accessPin": self.password, "rememberLogin": False,
            "DeviceId": _device_id(), "DeviceName": "Rachunki domowe", "DeviceType": "Web"})
        if r.status_code != 200:
            raise PgnigError(f"logowanie: HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("Code", -1) != 0 or not data.get("Token"):
            raise PgnigError("logowanie nieudane: " + str(data.get("Message") or data.get("EndUserMessage")
                                                          or f"Code={data.get('Code')}"))
        self.token = data["Token"]
        self.s.headers["AuthToken"] = self.token
        return data

    def get(self, path, **params):
        r = self.s.get(f"{BASE}{path}", params={**params, **API}, timeout=30)
        if r.status_code != 200:
            raise PgnigError(f"{path}: HTTP {r.status_code}")
        return r

    def json(self, path, **params):
        data = self.get(path, **params).json()
        if isinstance(data, dict) and data.get("Code", 0) != 0:
            raise PgnigError(f"{path}: {data.get('Message') or data.get('EndUserMessage') or data.get('Code')}")
        return data

    def invoices(self, page=1, size=12):
        data = self.json("/crm/get-invoices-v2", pageNumber=page, pageSize=size)
        if isinstance(data, list):
            return data
        return data.get("InvoicesList") or data.get("InvoiceList") or data.get("Invoices") or []

    def pdf(self, number):
        content = self.get("/crm/get-invoice-pdf", invoiceNumber=number).content
        if not content.startswith(b"%PDF"):
            raise PgnigError("odpowiedź nie jest PDF-em")
        return content


def _d(v):
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


def _client():
    missing = [k for k in ("PGNIG_LOGIN", "PGNIG_PASSWORD") if not os.environ.get(k)]
    if missing:
        sys.exit("Ustaw w .env: " + ", ".join(missing))
    c = Client(os.environ["PGNIG_LOGIN"], os.environ["PGNIG_PASSWORD"])
    c.login()
    return c


def sync(dry_run=False):
    c = _client()
    skip = tuple(x.strip().upper() for x in os.environ.get("PGNIG_SKIP_PREFIXES", "N").split(",") if x.strip())
    pdf_dir = Path(os.environ.get("PDF_DIR", "/pdf")) / UTILITY
    rows = []
    for page in range(1, int(os.environ.get("PGNIG_MAX_PAGES", "4")) + 1):
        chunk = c.invoices(page, 12)
        rows += chunk
        if len(chunk) < 12:
            break
    added = skipped = 0
    for inv in rows:
        no = inv.get("Number") or ""
        if not no or no.upper().startswith(skip):
            skipped += 1
            continue
        with db.conn() as conn:
            row = conn.execute("SELECT id, pdf_path FROM bills WHERE provider=? AND invoice_no=?",
                               [PROVIDER, no]).fetchone()
        desc = (f"{no} | wystawiona {_d(inv.get('Date'))} | {inv.get('GrossAmount')} zł | "
                f"okres {_d(inv.get('StartDate'))} – {_d(inv.get('EndDate'))} | {inv.get('WearKWH')} kWh")
        if dry_run:
            log.info("%s | %s", desc, "JUŻ W BAZIE" if row else "DODAŁBYM")
            continue
        pdf_path = row["pdf_path"] if row else None
        if not pdf_path and inv.get("PDFPrintAllowed", True):
            try:
                content = c.pdf(no)
                path = pdf_dir / (re.sub(r"[^A-Za-z0-9_-]+", "_", no) + ".pdf")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                pdf_path = str(path)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: nie pobrano PDF: %s", no, e)
        if row is None:
            db.add_bill(provider=PROVIDER, utility=UTILITY, invoice_no=no, issue_date=_d(inv.get("Date")),
                        period_start=_d(inv.get("StartDate")), period_end=_d(inv.get("EndDate")),
                        amount_gross=float(inv["GrossAmount"]) if inv.get("GrossAmount") is not None else None,
                        consumption=inv.get("WearKWH"), unit="kWh" if inv.get("WearKWH") is not None else None,
                        source="pgnig-api", pdf_path=pdf_path)
            added += 1
            log.info("dodano: %s", desc)
        elif pdf_path and not row["pdf_path"]:
            db.set_pdf(row["id"], pdf_path)
            log.info("dołączono PDF: %s", no)
    log.info("Faktur na liście: %d, nowych: %d, pominięte (np. noty): %d", len(rows), added, skipped)


def _show(title, resp_json):
    print(f"\n=== {title} ===")
    print(json.dumps(redact(resp_json), ensure_ascii=False, indent=2))


def probe():
    login, password = os.environ["PGNIG_LOGIN"], os.environ["PGNIG_PASSWORD"]
    c = Client(login, password)
    r = c.s.post(f"{BASE}/auth/login", params=API, timeout=30, json={
        "identificator": login, "accessPin": password, "rememberLogin": False,
        "DeviceId": _device_id(), "DeviceName": "Rachunki domowe", "DeviceType": "Web"})
    print(f"\n=== logowanie -> HTTP {r.status_code} ({BASE}) ===")
    try:
        data = r.json()
        print(json.dumps(redact(data), ensure_ascii=False, indent=2))
    except ValueError:
        print("(nie JSON) " + r.text[:300].replace("\n", " "))
        return
    if data.get("Code", -1) != 0 or not data.get("Token"):
        print("\nLogowanie nie zadziałało. Jeśli konto jest kontem OrlenID (wspólnym dla grupy Orlen), "
              "daj znać: to osobny sposób logowania.")
        return
    c.s.headers["AuthToken"] = data["Token"]
    for title, path, params in (("saldo", "/crm/get-balance", {}),
                                ("faktury", "/crm/get-invoices-v2", {"pageNumber": 1, "pageSize": 6})):
        try:
            _show(title, c.get(path, **params).json())
        except Exception as e:  # noqa: BLE001
            print(f"\n=== {title}: błąd: {e} ===")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if "--probe" in sys.argv:
        if not (os.environ.get("PGNIG_LOGIN") and os.environ.get("PGNIG_PASSWORD")):
            sys.exit("Ustaw w .env: PGNIG_LOGIN, PGNIG_PASSWORD")
        probe()
    elif "--loop" in sys.argv:
        hours = float(os.environ.get("PGNIG_INTERVAL_HOURS", "24"))
        while True:
            try:
                sync()
            except Exception:  # noqa: BLE001
                log.exception("Błąd synchronizacji PGNiG")
            time.sleep(hours * 3600)
    elif "--sync" in sys.argv:
        sync(dry_run="--dry-run" in sys.argv)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
