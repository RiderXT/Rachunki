"""Odczyt faktur Enei (energia elektryczna i dystrybucja) z PDF-a: dane faktury, wynik rozliczenia, pozycje kosztów."""
import re

from app import db
from app import water_mail as w

A = w.AMOUNT
RE_TOTAL = re.compile(r"podsumowanie:?\s+" + A + r"\s+" + A + r"\s+" + A, re.I)            # netto, VAT, brutto
RE_DUE = re.compile(r"nale[żz]no[śs][ćc]\s+z\s+rozliczenia\s+" + A, re.I)
RE_PERIOD = re.compile(r"za\s+okres\s+od\s+" + w.DATE_ANY + r"\s+do\s+" + w.DATE_ANY, re.I)
RE_KWH = re.compile(r"og[óo][łl]em\s+zu[żz]ycie:?\s*(\d[\d \u00a0]*(?:,\d+)?)\s*kwh", re.I)
# "Wynik rozliczenia ( 1 + 2 - 3 ) -86,48": ujemny = nadpłata po rozliczeniu, dodatni = do zapłaty
RE_RESULT = re.compile(r"wynik\s+rozliczenia\s*\(\s*1\s*\+\s*2\s*-\s*3\s*\)\s*([-−–]?\s*\d[\d \u00a0]*,\d{2})", re.I)
RE_REFUND = re.compile(r"do\s+zwrotu\s+" + A, re.I)
RE_PAY = re.compile(r"do\s+zap[łl]aty\s+" + A + r"\s*z[łl]", re.I)
# wiersz pozycji: [strefa] jednostka [data] ilość cena_netto należność_netto VAT
RE_ROW = re.compile(
    r"(?<![\d,.])(?P<unit>kWh|zł/mc|mc|szt|kW)\s+(?:(?P<date>\d{2}[./]\d{2}[./]\d{4})\s+)?"
    r"(?P<qty>\d{1,3}(?:[ \u00a0]\d{3})+(?:,\d+)?|\d+(?:,\d+)?)\s+(?P<price>\d+,\d{2,5})\s+"
    r"(?P<net>-?\d[\d \u00a0]*,\d{2})\s+(?P<vat>\d{1,2})\s*$", re.I)
NAME_KINDS = [("energia elektryczna", "energia"), ("opłata stała sieciowa", "dystr_stala"),
              ("składnik stały stawki sieciowej", "dystr_stala"), ("składnik zmienny stawki sieciowej", "dystr_zmienna"),
              ("opłata handlowa", "handlowa"), ("składnik", "inne"),
              ("opłata mocowa", "mocowa"), ("opłata zmienna sieciowa", "dystr_zmienna"),
              ("opłata jakościowa", "jakosciowa"), ("opłata oze", "oze"),
              ("opłata kogeneracyjna", "kogeneracyjna"), ("opłata abonamentowa", "abonament"),
              ("opłata przejściowa", "przejsciowa"), ("opłata", "inne"), ("energia", "energia")]


def _kind(line):
    low = line.lower()
    for prefix, kind in NAME_KINDS:
        if low.startswith(prefix):
            return kind
    return None


def _signed(s):
    return w._num(s.replace("−", "-").replace("–", "-").replace(" ", "", 1) if s.strip()[0] in "-−–" else s)


def parse_items(text):
    """Pozycje: energia czynna oraz opłaty dystrybucyjne (stała, mocowa, zmienna, jakościowa, OZE, kogeneracyjna, abonament)."""
    items, name, period = [], None, (None, None)
    for line in (l.strip() for l in text.splitlines()):
        if m := RE_PERIOD.search(line):
            period = (w._iso(m.group(1)), w._iso(m.group(2)))
        row = RE_ROW.search(line)
        if row and _kind(line[:row.start()].strip()):      # nazwa pozycji jest w tej samej linii co wartości
            name = line[:row.start()].strip()
        if row and name:
            net, vat = w._num(row["net"]), float(row["vat"])
            unit = "kWh" if row["unit"].lower() == "kwh" else "mc" if "mc" in row["unit"].lower() else row["unit"].lower()
            items.append({"meter": "prad", "kind": _kind(name), "quantity": w._num(row["qty"]), "unit": unit,
                          "unit_price": w._num(row["price"]), "vat_rate": vat, "amount_net": net,
                          "amount_gross": round(net * (1 + vat / 100), 2),
                          "period_start": period[0], "period_end": period[1]})
            # nazwa zostaje: pod jedną pozycją bywa kilka wierszy (np. energia w okresie ze zmianą ceny)
        elif not row and re.match(r"(?i)(og[óo][łl]em\s+warto|rozliczenie\s*-)", line):
            name = None                                    # koniec sekcji: kolejne wiersze nie należą do poprzedniej nazwy
        elif not row and _kind(line):
            name = line
    return items


def parse_text(text):
    out = {}
    if m := w.RE_INVOICE_NR.search(text):
        out["invoice_no"] = m.group(1).rstrip(".")
    if m := w.RE_ISSUE.search(text):
        out["issue_date"] = w._iso(m.group(1))
    if m := RE_TOTAL.search(text):
        out["amount_gross"] = w._num(m.group(3))
    elif m := RE_DUE.search(text):
        out["amount_gross"] = w._num(m.group(1))
    periods = [(w._iso(a), w._iso(b)) for a, b in RE_PERIOD.findall(text)]
    if periods:
        out["period_start"] = min(p[0] for p in periods)
        out["period_end"] = max(p[1] for p in periods)
    if m := RE_KWH.search(text):
        out["consumption"], out["unit"] = w._num(m.group(1)), "kWh"
    if m := RE_RESULT.search(text):
        out["account_balance"] = -_signed(m.group(1))        # nadpłata po rozliczeniu = +, do zapłaty = -
    elif m := RE_REFUND.search(text):
        out["account_balance"] = w._num(m.group(1))
    elif m := RE_PAY.search(text):
        out["account_balance"] = -w._num(m.group(1))
    return out


# Awaryjnie (gdy tabela pozycji ma inny układ): sumy sekcji "Ogółem wartość - sprzedaż energii" i "... usługa dystrybucji".
# Używane tylko wtedy, gdy po doliczeniu VAT dokładnie zgadzają się z kwotą faktury.
RE_SALES = re.compile(r"og[óo][łl]em\s+warto[śs][ćc]\s*-\s*sprzeda[żz]\s+energii:?\s*" + A, re.I)
RE_DIST = re.compile(r"og[óo][łl]em\s+warto[śs][ćc]\s*-\s*us[łl]uga\s+dystrybucji(?:\s+energii)?:?\s*" + A, re.I)


def totals_items(text, fields):
    """Dwie pozycje zbiorcze (energia, dystrybucja razem) z sum sekcji, albo [] gdy nie zgadzają się z kwotą faktury."""
    sales = [w._num(x) for x in RE_SALES.findall(text)]
    dist = [w._num(x) for x in RE_DIST.findall(text)]
    amount = fields.get("amount_gross")
    if not sales or not dist or amount is None:
        return []
    s_net, d_net = round(sum(sales), 2), round(sum(dist), 2)
    for vat in (23.0, 8.0, 5.0):
        s_gross, d_gross = round(s_net * (1 + vat / 100), 2), round(d_net * (1 + vat / 100), 2)
        if abs(s_gross + d_gross - amount) <= 0.05:
            kwh = fields.get("consumption")
            common = {"meter": "prad", "vat_rate": vat, "period_start": fields.get("period_start"),
                      "period_end": fields.get("period_end")}
            return [
                {**common, "kind": "energia", "quantity": kwh, "unit": "kWh" if kwh else None,
                 "unit_price": round(s_net / kwh, 5) if kwh else None, "amount_net": s_net, "amount_gross": s_gross},
                {**common, "kind": "dystrybucja", "quantity": None, "unit": None, "unit_price": None,
                 "amount_net": d_net, "amount_gross": d_gross}]
    return []


def parse_items_safe(text):
    """Pozycje, którym można ufać: z tabeli, jeśli ich suma zgadza się z kwotą faktury; w przeciwnym razie awaryjny
    podział z sum sekcji; w ostatecznosci [] (lepiej brak podziału niż błędny)."""
    fields = parse_text(text)
    items = parse_items(text)
    if w.items_consistent(items, fields.get("amount_gross")):
        return items
    return totals_items(text, fields)


def apply(bill_id, text):
    """Dopisuje do istniejącej faktury wynik rozliczenia (stan konta) i pozycje kosztów odczytane z PDF-a."""
    fields, items = parse_text(text), parse_items_safe(text)
    if "account_balance" in fields:
        db.update_columns(bill_id, account_balance=fields["account_balance"])
    db.replace_items(bill_id, items)
    return fields, items
