"""Subskrypcje i inne stałe opłaty (Spotify, HBO, abonamenty): zapis, przeliczenie na miesiąc, najbliższe płatności."""
import pandas as pd

from app import db

CYCLES = {"month": "miesięcznie", "quarter": "kwartalnie", "year": "rocznie"}
MONTHS_PER_CYCLE = {"month": 1, "quarter": 3, "year": 12}
EDITABLE = {"name", "amount", "cycle", "next_date", "active", "note"}


def load():
    with db.conn() as c:
        df = pd.read_sql_query("SELECT * FROM subscriptions ORDER BY active DESC, name COLLATE NOCASE, id", c)
    df["next_date"] = pd.to_datetime(df["next_date"], errors="coerce")
    df["active"] = df["active"].astype(bool)
    return df


def _check(name=None, amount=None, cycle=None):
    if name is not None and not str(name).strip():
        raise ValueError("Nazwa nie może być pusta.")
    if amount is not None and (pd.isna(amount) or float(amount) < 0):
        raise ValueError("Kwota musi być liczbą nieujemną.")
    if cycle is not None and cycle not in CYCLES:
        raise ValueError(f"Nieznany cykl płatności: {cycle}")


def add(name, amount, cycle="month", next_date=None, note=None, active=True):
    _check(name, amount, cycle)
    with db.conn() as c:
        cur = c.execute("INSERT INTO subscriptions (name, amount, cycle, next_date, active, note) VALUES (?,?,?,?,?,?)",
                        [str(name).strip(), float(amount), cycle, str(next_date)[:10] if next_date else None,
                         int(bool(active)), (note or "").strip() or None])
    return cur.lastrowid


def update(sub_id, **fields):
    fields = {k: v for k, v in fields.items() if k in EDITABLE}
    _check(fields.get("name"), fields.get("amount"), fields.get("cycle"))
    if "name" in fields:
        fields["name"] = str(fields["name"]).strip()
    if "active" in fields:
        fields["active"] = int(bool(fields["active"]))
    if "next_date" in fields:
        fields["next_date"] = str(fields["next_date"])[:10] if fields["next_date"] is not None and not pd.isna(fields["next_date"]) else None
    if fields.get("note") is not None:
        fields["note"] = str(fields["note"]).strip() or None
    if not fields:
        return
    with db.conn() as c:
        c.execute(f"UPDATE subscriptions SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), sub_id])


def delete(sub_id):
    with db.conn() as c:
        c.execute("DELETE FROM subscriptions WHERE id=?", [sub_id])


def monthly_equiv(amount, cycle):
    """Kwota przeliczona na miesiąc (rocznie / 12, kwartalnie / 3)."""
    return float(amount) / MONTHS_PER_CYCLE[cycle]


def next_due(base, cycle, today=None):
    """Najbliższa płatność (dziś lub później) liczona od podanej daty wzorcowej co cykl. None bez daty."""
    if base is None or pd.isna(base):
        return None
    base = pd.Timestamp(base).normalize()
    today = (pd.Timestamp(today) if today is not None else pd.Timestamp.today()).normalize()
    step, k, cur = MONTHS_PER_CYCLE[cycle], 0, base
    while cur < today:
        k += step
        cur = base + pd.DateOffset(months=k)   # od daty wzorcowej, żeby 31. nie "dryfowało" po krótszych miesiącach
    return cur


def summary(df):
    """Suma aktywnych subskrypcji przeliczona na miesiąc i rok oraz liczby pozycji."""
    act = df[df["active"]]
    monthly = float(sum(monthly_equiv(r.amount, r.cycle) for r in act.itertuples()))
    return {"monthly": monthly, "yearly": monthly * 12, "active": len(act), "paused": int((~df["active"]).sum())}


def upcoming(df, today=None, days=30):
    """Płatności aktywnych subskrypcji z podaną datą, przypadające w ciągu `days` dni."""
    today = (pd.Timestamp(today) if today is not None else pd.Timestamp.today()).normalize()
    rows = []
    for r in df[df["active"]].itertuples():
        due = next_due(r.next_date, r.cycle, today)
        if due is not None and due <= today + pd.Timedelta(days=days):
            rows.append({"date": due, "name": r.name, "amount": r.amount})
    return pd.DataFrame(rows, columns=["date", "name", "amount"]).sort_values("date").reset_index(drop=True)
