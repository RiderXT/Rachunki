"""Karta miesiąca: wszystkie koszty miesiąca w jednym miejscu z zaznaczaniem, co już zapłacono.

Pozycje są składane na bieżąco z: faktur za media (a gdy faktura jeszcze nie przyszła: szacunku ze średniej),
subskrypcji, kosztów okresowych przypadających w tym miesiącu oraz własnych pozycji. Zaznaczenia "zapłacone"
są zapamiętywane osobno (klucz pozycji + miesiąc), więc kolejna faktura czy zmiana kwoty ich nie kasuje.
"""
import pandas as pd

from app import db, subscriptions as S

UTILITIES = (("prad", "Prąd"), ("gaz", "Gaz"), ("woda", "Woda"))
COLUMNS = ["key", "name", "kind", "amount", "due", "estimated", "settled", "detail", "paid", "paid_date"]


def _money(v):
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


def _balance_text(v):
    if v is None or pd.isna(v):
        return ""
    return f"po rozliczeniu: nadpłata {_money(v)} zł" if v >= 0 else f"po rozliczeniu: do zapłaty {_money(abs(v))} zł"


def load_marks(month):
    with db.conn() as c:
        rows = c.execute("SELECT item_key, paid, paid_date FROM payment_marks WHERE month=?", [str(month)]).fetchall()
    return {r["item_key"]: (bool(r["paid"]), r["paid_date"]) for r in rows}


def set_paid(month, key, paid, today=None):
    day = (pd.Timestamp(today) if today is not None else pd.Timestamp.today()).date().isoformat() if paid else None
    with db.conn() as c:
        c.execute("INSERT INTO payment_marks (month, item_key, paid, paid_date) VALUES (?,?,?,?) "
                  "ON CONFLICT(month, item_key) DO UPDATE SET paid=excluded.paid, paid_date=excluded.paid_date",
                  [str(month), key, int(bool(paid)), day])


def add_manual(month, name, amount, note=None):
    if not str(name).strip():
        raise ValueError("Nazwa nie może być pusta.")
    if amount is None or pd.isna(amount) or float(amount) < 0:
        raise ValueError("Kwota musi być liczbą nieujemną.")
    with db.conn() as c:
        return c.execute("INSERT INTO manual_items (month, name, amount, note) VALUES (?,?,?,?)",
                         [str(month), str(name).strip(), float(amount), (note or "").strip() or None]).lastrowid


def delete_manual(item_id):
    with db.conn() as c:
        c.execute("DELETE FROM manual_items WHERE id=?", [item_id])
        c.execute("DELETE FROM payment_marks WHERE item_key=?", [f"man:{item_id}"])


def build(month, bills, subs, estimates=None):
    """Pozycje karty dla miesiąca (pd.Period): DataFrame o kolumnach COLUMNS."""
    month = pd.Period(month, "M")
    estimates = estimates or {}
    rows = []

    for util, label in UTILITIES:                       # faktury za media wystawione w tym miesiącu
        b = bills[(bills["utility"] == util) & bills["amount_gross"].notna() & bills["issue_date"].notna()]
        b = b[b["issue_date"].dt.to_period("M") == month]
        for r in b.itertuples():
            bal = getattr(r, "account_balance", None)
            rows.append(dict(key=f"bill:{r.id}", name=f"{label}: faktura {r.invoice_no or ''}".strip(), kind="faktura",
                             amount=float(r.amount_gross), due=r.issue_date, estimated=False,
                             settled=bal is not None and not pd.isna(bal) and bal >= 0, detail=_balance_text(bal)))
        if b.empty:                                     # faktura jeszcze nie przyszła: szacunek ze średniej
            rows.append(dict(key=f"est:{util}", name=f"{label} (szacunek, faktury jeszcze nie ma)", kind="szacunek",
                             amount=float(estimates.get(util, 0.0)), due=None, estimated=True, settled=False,
                             detail="zastąpi ją faktura po jej pobraniu"))

    for r in subs[subs["active"]].itertuples():          # subskrypcje i koszty okresowe przypadające w tym miesiącu
        created = pd.Period(str(r.created_at)[:7], "M") if isinstance(r.created_at, str) else None
        if created is not None and month < created:      # nie wstecz: sprzed dodania pozycji nie było jej w rozliczeniach
            continue
        due = S.occurrence_in_month(r.next_date, r.cycle, month)
        if r.cycle == "month" and due is None:
            due = None
        elif due is None:
            continue
        rows.append(dict(key=f"{'sub' if r.category == 'sub' else 'per'}:{r.id}:{month}", name=r.name,
                         kind="subskrypcja" if r.category == "sub" else "koszt okresowy", amount=float(r.amount),
                         due=due, estimated=False, settled=False, detail=(r.note or "") if isinstance(r.note, str) else ""))

    with db.conn() as c:                                 # własne pozycje
        manual = c.execute("SELECT id, name, amount, note FROM manual_items WHERE month=? ORDER BY id", [str(month)]).fetchall()
    for m in manual:
        rows.append(dict(key=f"man:{m['id']}", name=m["name"], kind="własna", amount=float(m["amount"]), due=None,
                         estimated=False, settled=False, detail=m["note"] or ""))

    df = pd.DataFrame(rows, columns=[c for c in COLUMNS if c not in ("paid", "paid_date")])
    marks = load_marks(month)
    df["paid"] = [marks.get(k, (False, None))[0] for k in df["key"]]
    df["paid_date"] = [marks.get(k, (False, None))[1] for k in df["key"]]
    return df


def totals(df):
    total = float(df["amount"].sum())
    paid = float(df.loc[df["paid"], "amount"].sum())
    return {"total": total, "paid": paid, "left": total - paid, "n": len(df), "n_paid": int(df["paid"].sum()),
            "n_left": int((~df["paid"]).sum())}


def tracking_start(today=None):
    """Miesiąc, od którego liczą się zaległości. Ustawiany przy pierwszym użyciu karty (bieżący miesiąc), żeby stare
    faktury sprzed używania karty nie wyglądały na niezapłacone."""
    cur = db.get_setting("checklist_start")
    if cur:
        return pd.Period(cur, "M")
    start = pd.Period(pd.Timestamp(today) if today is not None else pd.Timestamp.today(), "M")
    db.set_setting("checklist_start", str(start))
    return start


def set_tracking_start(month):
    db.set_setting("checklist_start", str(pd.Period(month, "M")))


def unpaid_previous(month, bills, subs, estimates=None, back=3, start=None):
    """Niezapłacone pozycje z poprzednich miesięcy, od miesiąca rozpoczęcia śledzenia. Bez szacunków (to nie są pewne
    rachunki) i bez faktur, po których u dostawcy została nadpłata (nic nie ma do dopłacenia)."""
    month = pd.Period(month, "M")
    first = max(month - back, pd.Period(start, "M")) if start is not None else month - back
    out = []
    for m in pd.period_range(first, month - 1, freq="M") if first <= month - 1 else []:
        df = build(m, bills, subs, estimates)
        df = df[(~df["paid"]) & (~df["estimated"]) & (~df["settled"])].assign(month=str(m))
        if not df.empty:
            out.append(df)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=COLUMNS + ["month"])
