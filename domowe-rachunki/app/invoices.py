"""Wgrywanie faktur z PDF-ów: rozpoznanie dostawcy, odczyt danych, zapis pliku i faktury w bazie.

Wodociągi mają dopasowany parser (w tym pozycje dom/ogród). Dla gazu i prądu działa na razie
odczyt ogólny (numer, kwota, daty, okres); braki uzupełnia się w zakładce "Lista".
"""
import hashlib
import re
from pathlib import Path

from app import db, enea_pdf, fuel_pdf, gas_pdf
from app import water_mail as water

DEFAULT_PROVIDER = {"prad": "Enea", "gaz": "PGNiG", "woda": "Wodociągi", "paliwo": "ORLEN"}
PROVIDERS = [  # (medium, dostawca, wzorzec rozpoznawczy w tekście faktury)
    ("woda", "Wodociągi", re.compile(r"Gospodarki Komunalnej|zgkszamotuly", re.I)),
    ("gaz", "PGNiG", re.compile(r"PGNiG|Polskie G[óo]rnictwo Naftowe|ebok\.pgnig|myORLEN", re.I)),
    ("prad", "Enea", re.compile(r"\bEnea\b", re.I)),
    ("paliwo", "ORLEN", re.compile(r"ORLEN\s+Pay|STACJA\s+PALIW", re.I)),
]
RE_PERIOD_GENERIC = re.compile(
    r"okres[^\d]{0,60}" + water.DATE_ANY + r"\s*(?:-|–|—|do|\.{2,3})\s*" + water.DATE_ANY, re.I)


def detect(text):
    for utility, provider, rx in PROVIDERS:
        if rx.search(text):
            return utility, provider
    return None


def generic_fields(text):
    out = {}
    if m := water.RE_AMOUNT_ZL_GR.search(text):
        out["amount_gross"] = float(f"{water._num(m.group(1)):.0f}.{m.group(2)}")
    else:
        for rx in water.RE_AMOUNTS:
            if m := rx.search(text):
                out["amount_gross"] = water._num(m.group(1))
                break
    if m := RE_PERIOD_GENERIC.search(text):
        out["period_start"], out["period_end"] = water._iso(m.group(1)), water._iso(m.group(2))
    if m := water.RE_ISSUE.search(text):
        out["issue_date"] = water._iso(m.group(1))
    if m := water.RE_INVOICE_NR.search(text):
        out["invoice_no"] = m.group(1).rstrip(".")
    return out


def same_file_exists(provider, data):
    """Czy ten sam plik (po zawartości) jest już zapisany przy jakiejś fakturze tego dostawcy."""
    digest = hashlib.sha1(data).hexdigest()
    with db.conn() as c:
        paths = [r["pdf_path"] for r in c.execute(
            "SELECT pdf_path FROM bills WHERE provider=? AND pdf_path IS NOT NULL", [provider])]
    for p in paths:
        try:
            if Path(p).is_file() and hashlib.sha1(Path(p).read_bytes()).hexdigest() == digest:
                return True
        except OSError:
            pass
    return False


def find_existing(provider, invoice_no, text):
    """Istniejąca faktura tego dostawcy: po numerze, a jeśli PDF zapisuje numer inaczej niż mail powiadomienia,
    to rekord z maila czekający na PDF, którego numer występuje w tekście (przy kilku: ten, który jest pierwszy)."""
    with db.conn() as c:
        if invoice_no:  # PDF podaje własny numer: albo to znana faktura, albo nowa (nie zgadujemy po tekście)
            return c.execute("SELECT * FROM bills WHERE provider=? AND invoice_no=?", [provider, invoice_no]).fetchone()
        waiting = c.execute("SELECT * FROM bills WHERE provider=? AND invoice_no IS NOT NULL "
                            "AND pdf_path IS NULL AND source LIKE 'mail-%'", [provider]).fetchall()
    hits = [(text.find(r["invoice_no"]), r) for r in waiting if r["invoice_no"] in text]
    return min(hits, key=lambda h: h[0])[1] if hits else None


def ingest(data, filename, hint, pdf_dir):
    """Przetwarza jeden PDF. `hint` = medium (prad/gaz/woda), gdy dostawcy nie uda się rozpoznać."""
    row = {"Plik": filename, "Dostawca": "", "Numer": "", "Kwota": "", "Okres": "", "Wynik": ""}
    if not data.startswith(b"%PDF"):
        row["Wynik"] = "to nie jest PDF"
        return row
    try:
        text = water.pdf_text(data)
    except Exception as e:  # noqa: BLE001
        row["Wynik"] = f"nie udało się odczytać PDF: {e}"
        return row
    utility, provider = detect(text) or (hint, DEFAULT_PROVIDER[hint])
    if utility == "woda":
        fields, items = water.parse_text(text), water.parse_items(text)
    elif utility == "gaz":
        fields, items = gas_pdf.parse_text(text), gas_pdf.parse_items(text)
    elif utility == "prad" and re.search(r"\bEnea\b", text, re.I):
        fields, items = enea_pdf.parse_text(text), enea_pdf.parse_items(text)
    elif utility == "paliwo":
        fields, items = fuel_pdf.parse_text(text), fuel_pdf.parse_items(text)
    else:
        fields, items = generic_fields(text), []

    if same_file_exists(provider, data):
        row.update(Dostawca=provider, Numer=fields.get("invoice_no") or "", Wynik="duplikat (ten sam plik już wgrano)")
        return row
    existing = find_existing(provider, fields.get("invoice_no"), text)
    if existing is not None:
        number = existing["invoice_no"]
        path = Path(pdf_dir) / utility / water._pdf_name(number, data)
    else:
        path = Path(pdf_dir) / utility / water._pdf_name(fields.get("invoice_no"), data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    if existing is not None:  # faktura już jest (np. z maila lub z poprzedniego importu): uzupełnij, nie dubluj
        done = []
        if not existing["pdf_path"]:
            db.set_pdf(existing["id"], path)
            done.append("PDF")
        done += db.fill_missing(existing["id"], fields)
        with db.conn() as c:
            has_items = c.execute("SELECT COUNT(*) FROM bill_items WHERE bill_id=?", [existing["id"]]).fetchone()[0]
        if items and not has_items:
            db.replace_items(existing["id"], items)
            done.append("pozycje")
        if utility == "gaz":
            gas_pdf.apply(existing["id"], text)
        elif utility == "prad" and re.search(r"\bEnea\b", text, re.I):
            enea_pdf.apply(existing["id"], text)
        fields = {**fields, "invoice_no": number}
        row["Wynik"] = ("uzupełniono istniejącą fakturę: " + ", ".join(done)) if done else "duplikat (już w bazie)"
        created = False
    else:
        created = db.add_bill(provider=provider, utility=utility, source="upload",
                              pdf_path=str(path), items=items, **fields)
    row.update(Dostawca=provider, Numer=fields.get("invoice_no") or "",
               Kwota=f"{fields['amount_gross']:.2f}" if "amount_gross" in fields else "",
               Okres=f"{fields['period_start']} – {fields['period_end']}" if "period_start" in fields else "")
    if not row["Wynik"]:
        if not created:
            row["Wynik"] = "duplikat (już w bazie)"
        elif db.compute_status({**fields}) == "ok":
            row["Wynik"] = "dodano"
        else:
            row["Wynik"] = "dodano, ale dane niepełne: uzupełnij w zakładce Lista"
    return row


PARSERS = {"woda": (water.parse_text, water.parse_items), "gaz": (gas_pdf.parse_text, gas_pdf.parse_items),
           "prad": (enea_pdf.parse_text, enea_pdf.parse_items), "paliwo": (fuel_pdf.parse_text, fuel_pdf.parse_items)}


def reparse_all():
    """Ponownie odczytuje wszystkie zapisane PDF-y faktur (po zmianie parsera lub gdy faktury wgrano starszą wersją):
    nadpisuje kwotę, okres, zużycie i datę wystawienia danymi z PDF-a, odtwarza pozycje kosztów i stan konta.
    Numer faktury i notatka (np. stacja) zostają. Zwraca wiersze do pokazania w tabeli."""
    rows = []
    with db.conn() as c:
        bills = c.execute("SELECT id, provider, utility, invoice_no, pdf_path FROM bills "
                          "WHERE pdf_path IS NOT NULL ORDER BY issue_date, id").fetchall()
    for b in bills:
        path = Path(b["pdf_path"])
        row = {"Plik": path.name, "Dostawca": b["provider"], "Numer": b["invoice_no"] or "", "Kwota": "", "Okres": "", "Wynik": ""}
        rows.append(row)
        if not path.is_file():
            row["Wynik"] = "brak pliku PDF"
            continue
        parser = PARSERS.get(b["utility"])
        try:
            text = water.pdf_text(path.read_bytes())
            fields, items = parser[0](text), parser[1](text)
        except Exception as e:  # noqa: BLE001
            row["Wynik"] = f"nie udało się odczytać: {e}"
            continue
        fields.pop("invoice_no", None)
        fields.pop("note", None)
        db.fill_missing(b["id"], fields, overwrite=True)
        if items:
            db.replace_items(b["id"], items)
        row["Kwota"] = f"{fields['amount_gross']:.2f}" if "amount_gross" in fields else ""
        row["Okres"] = f"{fields['period_start']} – {fields['period_end']}" if "period_start" in fields else ""
        notes = [f"pozycji: {len(items)}"]
        if "account_balance" in fields:
            notes.append(f"stan konta: {fields['account_balance']:.2f} zł")
        total, amount = sum(i["amount_gross"] for i in items), fields.get("amount_gross")
        if not items and b["utility"] != "paliwo":
            notes.append("BRAK POZYCJI (inny układ PDF?)")
        elif items and amount is not None and abs(total - amount) > 0.01 * len(items) + 0.03:
            notes.append(f"UWAGA: suma pozycji {total:.2f} ≠ kwota {amount:.2f}")
        if "amount_gross" not in fields:
            notes.append("BRAK KWOTY")
        row["Wynik"] = ", ".join(notes)
    return rows
