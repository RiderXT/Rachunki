"""Odczyt faktur za gaz (myORLEN / dawniej PGNiG) z PDF-a: dane faktury, stan konta i pozycje kosztów."""
import re

from app import db
from app import water_mail as w

DATE_DMY = r"(\d{2}\.\d{2}\.\d{4})"
RE_ISSUE = re.compile(r"z\s+dnia\s+" + w.DATE_ANY, re.I)
RE_TOTAL = re.compile(r"sprzeda[żz]\s+og[óo][łl]em\s+" + w.AMOUNT + r"\s+" + w.AMOUNT + r"\s+" + w.AMOUNT, re.I)
RE_PERIOD = re.compile(r"okresie\s+rozliczeniowym\s+od\s+" + w.DATE_ANY + r"\s+do\s+" + w.DATE_ANY, re.I)
RE_KWH = re.compile(r"razem\s+zu[żz]ycie[^\[]*\[m[3³]\]\s*(\d[\d \u00a0]*(?:,\d+)?)\s*\[kwh\]", re.I)
# "Stan konta po wykonaniu rozliczenia:2 nadpłata 2 155,85 zł" (cyfra to odnośnik do przypisu)
RE_BALANCE = re.compile(
    r"stan\s+konta\s+po\s+wykonaniu\s+rozliczenia:?\s*\d?\s*"
    r"(nadp[łl]ata|niedop[łl]ata|zaleg[łl]o[śs][ćc]|zad[łl]u[żz]enie)\s+" + w.AMOUNT, re.I)
# koniec wiersza pozycji: ilość, jednostka, cena netto, VAT %, wartość netto
RE_TAIL = re.compile(
    r"(?<![\d,.])(\d{1,3}(?:[ \u00a0]\d{3})+(?:,\d+)?|\d+(?:,\d+)?)\s+(kWh|mc|m³|m3|szt)\s+"
    r"(\d+,\d+)\s+(\d{1,2})\s+(-?\d[\d \u00a0]*,\d{2})\s*$", re.I)
KINDS = [("opłata abonamentowa", "abonament"), ("paliwo gazowe", "paliwo"),
         ("dystrybucyjna stała", "dystr_stala"), ("dystrybucyjna zmienna", "dystr_zmienna")]


def _kind(line):
    low = line.lower()
    for prefix, kind in KINDS:
        if low.startswith(prefix):
            return kind
    return "inne" if low.startswith(("opłata", "oplata", "dystrybucyjna")) else None


def parse_items(text):
    """Pozycje faktury: abonament, paliwo, dystrybucja stała i zmienna (ilość, cena jedn., okres, kwoty)."""
    items = []
    for line in (l.strip() for l in text.splitlines()):
        kind = _kind(line)
        m = RE_TAIL.search(line) if kind else None
        if not m:
            continue
        dates = re.findall(DATE_DMY, line)
        net, vat = w._num(m.group(5)), float(m.group(4))
        items.append({
            "meter": "gaz", "kind": kind, "quantity": w._num(m.group(1)),
            "unit": "kWh" if m.group(2).lower() == "kwh" else m.group(2).lower().replace("³", "3"),
            "unit_price": w._num(m.group(3)), "vat_rate": vat, "amount_net": net,
            "amount_gross": round(net * (1 + vat / 100), 2),
            "period_start": w._iso(dates[0]) if len(dates) >= 1 else None,
            "period_end": w._iso(dates[1]) if len(dates) >= 2 else None})
    return items


def parse_text(text):
    out = {}
    if m := w.RE_INVOICE_NR.search(text):
        out["invoice_no"] = m.group(1).rstrip(".")
    if m := RE_ISSUE.search(text):
        out["issue_date"] = w._iso(m.group(1))
    if m := RE_TOTAL.search(text):
        out["amount_gross"] = w._num(m.group(3))
    if m := RE_PERIOD.search(text):
        out["period_start"], out["period_end"] = w._iso(m.group(1)), w._iso(m.group(2))
    if m := RE_KWH.search(text):
        out["consumption"], out["unit"] = w._num(m.group(1)), "kWh"
    if m := RE_BALANCE.search(text):
        value = w._num(m.group(2))
        out["account_balance"] = value if m.group(1).lower().startswith("nadp") else -value
    return out


def apply(bill_id, text):
    """Dopisuje do istniejącej faktury stan konta i pozycje kosztów odczytane z PDF-a."""
    fields, items = parse_text(text), parse_items(text)
    with db.conn() as c:
        row = c.execute("SELECT invoice_no, amount_gross FROM bills WHERE id=?", [bill_id]).fetchone()
    if row:  # ostrzeżenie w logu, gdy suma pozycji nie zgadza się z kwotą faktury (np. inny układ PDF)
        w.check_items(items, row["amount_gross"], f"{row['invoice_no']}:")
    if "account_balance" in fields:
        db.update_columns(bill_id, account_balance=fields["account_balance"])
    if items:
        db.replace_items(bill_id, items)
    return fields, items
