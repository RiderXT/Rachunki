"""Konektor PGNiG (nieoficjalne API eBOK, to samo, z którego korzysta aplikacja).

Na razie tylko diagnostyka:
  python -m app.pgnig --probe

Robi JEDNO logowanie i wypisuje strukturę odpowiedzi (nazwy pól, typy, skrócone wartości)
z zamaskowanymi danymi osobowymi. Ten wynik wystarczy, żeby dopasować właściwy konektor.
Dane logowania w .env: PGNIG_LOGIN (e-mail/identyfikator) i PGNIG_PASSWORD (hasło do eBOK).
"""
import json
import os
import re
import sys

import requests

BASE = "https://ebok.pgnig.pl"
API = "api-version=3.0"
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


def _show(title, resp):
    print(f"\n=== {title} -> HTTP {resp.status_code} ===")
    try:
        print(json.dumps(redact(resp.json()), ensure_ascii=False, indent=2))
    except ValueError:
        print("(odpowiedź nie jest JSON-em) " + resp.text[:300].replace("\n", " "))


def login(session):
    resp = session.post(f"{BASE}/auth/login?{API}", json={
        "identificator": os.environ["PGNIG_LOGIN"],
        "accessPin": os.environ["PGNIG_PASSWORD"],
        "rememberLogin": False,
        "DeviceId": os.environ.get("PGNIG_DEVICE_ID", "rachunki-domowe"),
        "DeviceName": "Rachunki domowe",
        "DeviceType": "Web",
    }, timeout=30)
    return resp


def probe():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Accept": "application/json",
                      "User-Agent": "Mozilla/5.0 (rachunki-domowe)"})
    resp = login(s)
    _show("logowanie", resp)
    try:
        token = resp.json().get("Token")
    except ValueError:
        token = None
    if not token:
        print("\nBrak tokenu: logowanie nie zadziałało (zła para login/hasło, captcha albo zmiana API).")
        return
    s.headers["AuthToken"] = token

    ppg = s.get(f"{BASE}/crm/get-ppg-list?{API}", timeout=30)
    _show("lista punktów poboru gazu", ppg)
    try:
        ppg_id = ppg.json()["PpgList"][0]["IdPPG"]
        _show("odczyty licznika", s.get(
            f"{BASE}/crm/get-all-ppg-readings-for-meter?pageSize=3&pageNumber=1&{API}&idPpg={ppg_id}", timeout=30))
    except (ValueError, KeyError, IndexError, TypeError):
        print("\n(pomijam odczyty: brak listy punktów poboru)")
    _show("faktury", s.get(f"{BASE}/crm/get-invoices-v2?pageNumber=1&pageSize=6&{API}", timeout=30))


def main():
    if "--probe" in sys.argv:
        missing = [k for k in ("PGNIG_LOGIN", "PGNIG_PASSWORD") if not os.environ.get(k)]
        if missing:
            sys.exit("Ustaw w .env: " + ", ".join(missing))
        probe()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
