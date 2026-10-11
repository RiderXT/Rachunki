"""Wgrywanie faktur z PDF-ów: rozpoznanie dostawcy, odczyt danych, zapis pliku i faktury w bazie.

Wodociągi mają dopasowany parser (w tym pozycje dom/ogród). Dla gazu i prądu działa na razie
odczyt ogólny (numer, kwota, daty, okres); braki uzupełnia się w zakładce "Lista".
"""
import hashlib
import re
from pathlib import Path

from app import db, gas_pdf
from app import water_mail as water

DEFAULT_PROVIDER = {"prad": "Enea", "gaz": "PGNiG", "woda": "Wodociągi"}
PROVIDERS = [  # (medium, dostawca, wzorzec rozpoznawczy w tekście faktury)
    ("woda", "Wodociągi", re.compile(r"Gospodarki Komunalnej|zgkszamotuly", re.I)),
    ("gaz", "PGNiG", re.compile(r"PGNiG|Polskie G[óo]rnictwo Naftowe|ebok\.pgnig|myORLEN", re.I)),
    ("prad", "Enea", re.compile(r"\bEnea\b", re.I)),
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
    else:
        fields, items = generic_fields(text), []

    if not fields.get("invoice_no"):  # bez numeru nie ma klucza unikalności: sprawdź ten sam plik po skrócie treści
        digest = hashlib.sha1(data).hexdigest()[:10]
        with db.conn() as c:
            if c.execute("SELECT 1 FROM bills WHERE pdf_path LIKE ?", [f"%{digest}.pdf"]).fetchone():
                row["Wynik"] = "duplikat (ten sam plik już wgrano)"
                return row
    path = Path(pdf_dir) / utility / water._pdf_name(fields.get("invoice_no"), data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    created = db.add_bill(provider=provider, utility=utility, source="upload",
                          pdf_path=str(path), items=items, **fields)
    if not created and fields.get("invoice_no"):  # duplikat: dołącz PDF, jeśli faktura go nie ma
        with db.conn() as c:
            r = c.execute("SELECT id, pdf_path FROM bills WHERE provider=? AND invoice_no=?",
                          [provider, fields["invoice_no"]]).fetchone()
            if r and not r["pdf_path"]:
                c.execute("UPDATE bills SET pdf_path=? WHERE id=?", [str(path), r["id"]])
                row["Wynik"] = "faktura już była, dołączono PDF"
        if utility == "gaz" and r:
            gas_pdf.apply(r["id"], text)  # uzupełnij pozycje kosztów i stan konta
            row["Wynik"] = row["Wynik"] or "faktura już była, uzupełniono pozycje i stan konta"
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
