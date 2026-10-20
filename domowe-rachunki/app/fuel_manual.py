"""Ręczne dodawanie tankowania (płatność przy kasie, bez faktury): data, litry i cena za litr (albo kwota razem)."""
import pandas as pd

from app import db

PROVIDER = "Paliwo (ręcznie)"
VAT = 23.0


def add_refuel(day, liters, price=None, total=None, kind="lpg", station=None):
    """Dodaje tankowanie. Kwota = litry x cena za litr, chyba że podano kwotę razem (np. z paragonu).
    Zwraca (True, kwota) albo zgłasza ValueError."""
    if day is None:
        raise ValueError("Podaj datę tankowania.")
    liters = float(liters or 0)
    price, total = float(price or 0), float(total or 0)
    if liters <= 0:
        raise ValueError("Podaj liczbę litrów większą od zera.")
    amount = round(total, 2) if total > 0 else round(liters * price, 2)
    if amount <= 0:
        raise ValueError("Podaj cenę za litr albo kwotę razem.")
    from app.stats import FUEL_KINDS
    iso = pd.Timestamp(day).date().isoformat()
    label = FUEL_KINDS.get(kind, kind)
    note = ", ".join(x for x in (label, (station or "").strip()) if x)
    item = {"meter": "paliwo", "kind": kind, "quantity": liters, "unit": "l", "vat_rate": VAT,
            "unit_price": round(amount / liters / (1 + VAT / 100), 5), "amount_net": round(amount / (1 + VAT / 100), 2),
            "amount_gross": amount, "period_start": iso, "period_end": iso}
    ok = db.add_bill(provider=PROVIDER, utility="paliwo", issue_date=iso, period_start=iso, period_end=iso,
                     amount_gross=amount, consumption=liters, unit="l", note=note, source="manual", items=[item])
    return ok, amount


def last_kind(default="lpg"):
    """Rodzaj paliwa z ostatniego zakupu (domyślna wartość formularza)."""
    with db.conn() as c:
        row = c.execute("SELECT i.kind FROM bill_items i JOIN bills b ON b.id=i.bill_id "
                        "WHERE i.meter='paliwo' ORDER BY b.issue_date DESC, b.id DESC LIMIT 1").fetchone()
    return row["kind"] if row else default
