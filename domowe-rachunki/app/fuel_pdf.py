"""Odczyt faktur za paliwo (ORLEN Pay, stacje ORLEN) z PDF-a: numer, data, kwota, litry, pozycje."""
import re

from app import db
from app import water_mail as w

A = w.AMOUNT
RE_NUMBER = re.compile(r"faktura\s+nr:?\s*([A-Z]{1,3}\s?\d+/\d+/\d+)", re.I)               # "F 19178/7297/26"
RE_TOTAL = re.compile(r"nale[żz]no[śs][ćc]\s+og[óo][łl]em:?\s*" + A, re.I)
RE_STATION_NO = re.compile(r"\bNR\s+(\d{3,5})\b")
RE_STREET_CITY = re.compile(r"(UL\.\s*[A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż.\s]+?\d+\w*)\s+\d{2}-\d{3}\s+([A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż]+)")
# wiersz pozycji: Lp nazwa jm ilość cena_brutto rabat cena_po_rabacie netto VAT% kwota_VAT brutto
RE_ROW = re.compile(
    r"^\d+\s+(?P<name>.+?)\s+(?P<unit>L|l|kg|szt|m3)\s+(?P<qty>\d[\d ]*,\d{1,3})\s+(?P<price>\d+,\d{2,4})\s+"
    r"(?P<disc>\d+,\d{2})\s+(?P<after>\d+,\d{2,4})\s+(?P<net>\d[\d ]*,\d{2})\s+(?P<vat>\d{1,2})\s+"
    r"(?P<vatamt>\d[\d ]*,\d{2})\s+(?P<gross>\d[\d ]*,\d{2})\s*$")
FUEL_KINDS = [("lpg", "lpg"), ("pb 95", "pb95"), ("pb95", "pb95"), ("benzyna 95", "pb95"), ("95", "pb95"),
              ("pb 98", "pb98"), ("pb98", "pb98"), ("98", "pb98"), ("diesel", "diesel"), ("on ", "diesel"),
              ("olej napędowy", "diesel"), ("adblue", "adblue")]


def _kind(name):
    low = name.lower() + " "
    for needle, kind in FUEL_KINDS:
        if needle in low:
            return kind
    return "inne"            # np. kawa, myjnia


def parse_items(text):
    items = []
    date = (w.RE_ISSUE.search(text) or [None, None])
    iso = w._iso(date.group(1)) if hasattr(date, "group") else None
    for line in (l.strip() for l in text.splitlines()):
        m = RE_ROW.match(line)
        if not m:
            continue
        qty, vat = w._num(m["qty"]), float(m["vat"])
        gross, net = w._num(m["gross"]), w._num(m["net"])
        items.append({"meter": "paliwo", "kind": _kind(m["name"]), "quantity": qty,
                      "unit": "l" if m["unit"].lower() == "l" else m["unit"].lower(),
                      "unit_price": round(w._num(m["after"]) / (1 + vat / 100), 5),   # cena netto po rabacie
                      "vat_rate": vat, "amount_net": net, "amount_gross": gross,
                      "period_start": iso, "period_end": iso})
    return items


def station_note(text):
    """Krótki opis stacji z nagłówka faktury: '7297 Cerekwica, ul. Szamotulska 11'."""
    no = RE_STATION_NO.search(text)
    sc = RE_STREET_CITY.search(text)
    street = re.sub(r"\s+", " ", sc.group(1)).strip().title().replace("Ul.", "ul.") if sc else None
    parts = [p for p in ((no.group(1) + (" " + sc.group(2).title() if sc else "")) if no else (sc.group(2).title() if sc else None), street) if p]
    return ", ".join(parts) or None


def parse_text(text):
    out = {}
    if m := RE_NUMBER.search(text):
        out["invoice_no"] = re.sub(r"\s+", " ", m.group(1)).upper()
    if m := w.RE_ISSUE.search(text):
        iso = w._iso(m.group(1))
        out["issue_date"] = out["period_start"] = out["period_end"] = iso   # zakup jednego dnia
    if m := RE_TOTAL.search(text):
        out["amount_gross"] = w._num(m.group(1))
    items = parse_items(text)
    if "amount_gross" not in out and items:
        out["amount_gross"] = round(sum(i["amount_gross"] for i in items), 2)
    liters = [i["quantity"] for i in items if i["unit"] == "l"]
    if liters:
        out["consumption"], out["unit"] = round(sum(liters), 3), "l"
    if note := station_note(text):
        out["note"] = note
    return out


def apply(bill_id, text):
    """Dopisuje do istniejącej faktury litry i pozycje odczytane z PDF-a."""
    fields, items = parse_text(text), parse_items(text)
    with db.conn() as c:
        row = c.execute("SELECT invoice_no, amount_gross FROM bills WHERE id=?", [bill_id]).fetchone()
    if row:
        w.check_items(items, row["amount_gross"], f"{row['invoice_no']}:")
    if items:
        db.replace_items(bill_id, items)
    return fields, items
