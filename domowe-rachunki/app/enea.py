"""Enea eBOK (prąd): diagnostyka logowania i struktury strony. Tylko odczyt, jedno logowanie.

  python -m app.enea --probe

Loguje się tak jak przeglądarka (formularz: e-mail, hasło, token ze strony) i opisuje, co się dzieje:
kolejne przekierowania, czy pojawia się krok z kodem SMS, jakie linki ma panel (faktury, dokumenty).
Wypisuje tylko strukturę (ścieżki bez parametrów, tytuły, nazwy pól); liczby w tekstach są maskowane.
Dane w .env: ENEA_LOGIN (e-mail) i ENEA_PASSWORD. Niczego nie zapisuje ani nie pobiera.
"""
import os
import re
import sys
from urllib.parse import urljoin, urlparse

import requests

from app.water_mail import parse_page

BASE = os.environ.get("ENEA_BASE", "https://ebok.enea.pl")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
SMS_HINT = re.compile(r"sms|kod\s+(weryfikacyjny|jednorazowy|autoryzacyjny)|weryfikac|autoryzac", re.I)
LINK_HINT = re.compile(r"faktur|dokument|rozlicz|p[łl]atno|rachun|invoice|document", re.I)


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html)).strip()


def _mask(s):
    return re.sub(r"\d{3,}", "***", s)  # numery telefonów, konta, identyfikatory


def _path(url):
    return _mask(urlparse(url).path or "/")


def describe(title, r):
    """Opis strony bez danych osobowych: status, ścieżka, tytuł, pola formularza, wskazówki o SMS."""
    page = parse_page(r.text)
    t = re.search(r"(?is)<title>(.*?)</title>", r.text)
    fields = [f"{i.get('type', 'text')}:{i.get('name')}" for i in page.inputs if i.get("name")]
    print(f"\n=== {title}: HTTP {r.status_code} | {_path(r.url)} | tytuł: {_mask(t.group(1).strip()) if t else '-'} ===")
    print("pola formularza:", fields or "brak")
    text = _text(r.text)
    if m := SMS_HINT.search(text):
        lo = max(0, m.start() - 80)
        print("UWAGA, na stronie jest wzmianka o SMS/weryfikacji:", _mask(text[lo:m.end() + 120]))
    page.needs_code = any(re.search(r"sms|otp|code|kod", i.get("name") or "", re.I) for i in page.inputs) or \
        bool(re.search(r"(wpisz|podaj|wprowad[źz]).{0,40}kod", text, re.I))
    return page


def probe():
    login, password = os.environ["ENEA_LOGIN"], os.environ["ENEA_PASSWORD"]
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(f"{BASE}/logowanie", timeout=30)
    page = describe("formularz logowania", r)
    m = re.search(r'name="token" value="([^"]+)"', r.text)
    if not m:
        print("\nNie znalazłem pola 'token' na stronie logowania: serwis mógł zmienić formularz.")
        return
    r = s.post(f"{BASE}/logowanie", timeout=30, allow_redirects=False,
               data={"email": login, "password": password, "token": m.group(1)})
    print(f"\n=== wysłanie logowania: HTTP {r.status_code} -> {_path(r.headers['Location']) if r.headers.get('Location') else '(bez przekierowania)'} ===")
    hops = 0
    while r.is_redirect and hops < 6:
        r = s.get(urljoin(r.url, r.headers["Location"]), timeout=30, allow_redirects=False)
        hops += 1
        print(f"  przekierowanie {hops}: HTTP {r.status_code} | {_path(r.url)}" +
              (f" -> {_path(r.headers['Location'])}" if r.headers.get("Location") else ""))
    page = describe("strona po logowaniu", r)
    if any(i.get("type") == "password" for i in page.inputs):
        print("\nWynik: NIE zalogowano (strona nadal ma pole hasła: złe dane albo zmieniony formularz).")
        return
    if page.needs_code:
        print("\nWynik: WYMAGANY DODATKOWY KROK Z KODEM (SMS). Skrypt nie przejdzie dalej bez Twojego udziału; "
              "nie próbuję tego obchodzić.")
        return
    print("\nWynik: wygląda na zalogowanie. Linki panelu pasujące do faktur/dokumentów:")
    seen = set()
    for a in page.anchors:
        label = " ".join(a["text"].split())
        if a["href"] and (LINK_HINT.search(label) or LINK_HINT.search(a["href"])) and (_path(a["href"]), label) not in seen:
            seen.add((_path(a["href"]), label))
            print("  ", _mask(label)[:60], "->", _path(a["href"]))
    target = next((a["href"] for a in page.anchors if re.search(r"faktur", a["href"], re.I)), None)
    if target:
        r2 = s.get(urljoin(r.url, target), timeout=30)
        p2 = describe("strona faktur", r2)
        print("linki do plików/pobierania na tej stronie:")
        for a in p2.anchors[:200]:
            if re.search(r"pdf|pobierz|download|zalacznik|attachment|faktur", a["href"] + a["text"], re.I):
                print("  ", _mask(" ".join(a["text"].split()))[:50], "->", _path(a["href"]))


def main():
    if "--probe" in sys.argv:
        if not (os.environ.get("ENEA_LOGIN") and os.environ.get("ENEA_PASSWORD")):
            sys.exit("Ustaw w .env: ENEA_LOGIN, ENEA_PASSWORD")
        probe()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
