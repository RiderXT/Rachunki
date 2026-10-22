"""Subskrypcje i inne stałe opłaty (Spotify, HBO, abonamenty): zapis, przeliczenie na miesiąc, najbliższe płatności."""
import pandas as pd

from app import db

CYCLES = {"month": "miesięcznie", "quarter": "kwartalnie", "halfyear": "co pół roku", "year": "rocznie",
          "2years": "co 2 lata", "3years": "co 3 lata"}
MONTHS_PER_CYCLE = {"month": 1, "quarter": 3, "halfyear": 6, "year": 12, "2years": 24, "3years": 36}
SUB_CYCLES = ("month", "quarter", "year")                                   # subskrypcje
PERIODIC_CYCLES = ("quarter", "halfyear", "year", "2years", "3years")       # koszty roczne i okresowe (OC, przeglądy)
CATEGORIES = ("sub", "periodic")
EDITABLE = {"name", "amount", "cycle", "next_date", "active", "note", "saved", "category"}


def load(category=None):
    with db.conn() as c:
        df = pd.read_sql_query("SELECT * FROM subscriptions WHERE (? IS NULL OR category=?) "
                               "ORDER BY active DESC, name COLLATE NOCASE, id", c, params=[category, category])
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


def add(name, amount, cycle="month", next_date=None, note=None, active=True, category="sub", saved=0.0):
    _check(name, amount, cycle)
    if category not in CATEGORIES:
        raise ValueError(f"Nieznana kategoria: {category}")
    if saved is None or pd.isna(saved) or float(saved) < 0:
        raise ValueError("Odłożona kwota musi być liczbą nieujemną.")
    with db.conn() as c:
        cur = c.execute("INSERT INTO subscriptions (name, amount, cycle, next_date, active, note, category, saved) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        [str(name).strip(), float(amount), cycle, str(next_date)[:10] if next_date else None,
                         int(bool(active)), (note or "").strip() or None, category, float(saved)])
    return cur.lastrowid


def update(sub_id, **fields):
    fields = {k: v for k, v in fields.items() if k in EDITABLE}
    _check(fields.get("name"), fields.get("amount"), fields.get("cycle"))
    if "name" in fields:
        fields["name"] = str(fields["name"]).strip()
    if "saved" in fields:
        if pd.isna(fields["saved"]) or float(fields["saved"]) < 0:
            raise ValueError("Odłożona kwota musi być liczbą nieujemną.")
        fields["saved"] = float(fields["saved"])
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


def occurrence_in_month(base, cycle, month):
    """Data płatności przypadająca w podanym miesiącu (pd.Period), liczona od daty wzorcowej co cykl; None, gdy brak."""
    if base is None or pd.isna(base):
        return None
    base = pd.Timestamp(base).normalize()
    diff = (month.year - base.year) * 12 + (month.month - base.month)
    if diff % MONTHS_PER_CYCLE[cycle]:
        return None
    return base + pd.DateOffset(months=diff)     # od daty wzorcowej: dzień jest przycinany do długości miesiąca


DAYS_PER_MONTH = 30.4375


def periodic_table(df, today=None):
    """Koszty okresowe: rata miesięczna, najbliższy termin, miesiące do terminu, kwota na miesiąc do tego terminu
    (gdyby odkładać osobno na ten koszt od zera) oraz ile powinno być już odłożone przy równym odkładaniu."""
    today = (pd.Timestamp(today) if today is not None else pd.Timestamp.today()).normalize()
    rows = []
    for r in df[df["active"]].itertuples():
        step = MONTHS_PER_CYCLE[r.cycle]
        due = next_due(r.next_date, r.cycle, today)
        row = {"id": r.id, "name": r.name, "amount": r.amount, "cycle": r.cycle, "monthly": r.amount / step,
               "next_due": due, "days_left": None, "months_left": None, "to_due": None, "target_now": None}
        if due is not None:
            days_left = (due - today).days
            months_left = max(1, -(-days_left // DAYS_PER_MONTH)) if days_left > 0 else 1
            elapsed = min(1.0, max(0.0, 1 - days_left / (step * DAYS_PER_MONTH)))
            row.update(days_left=days_left, months_left=months_left, to_due=r.amount / months_left,
                       target_now=r.amount * elapsed)
        rows.append(row)
    cols = ["id", "name", "amount", "cycle", "monthly", "next_due", "days_left", "months_left", "to_due", "target_now"]
    return pd.DataFrame(rows, columns=cols).sort_values("next_due", na_position="last").reset_index(drop=True)


def periodic_summary(table):
    """Sumy: rata miesięczna (średnio) i ile powinno być odłożone dziś przy równym odkładaniu."""
    return {"monthly": float(table["monthly"].sum()), "target_now": float(table["target_now"].fillna(0).sum()),
            "count": len(table)}


def occurrences(df, today=None, horizon_months=12):
    """Wszystkie płatności aktywnych kosztów okresowych w oknie [dziś, dziś + horyzont]: (date, name, amount)."""
    today = (pd.Timestamp(today) if today is not None else pd.Timestamp.today()).normalize()
    end = today + pd.DateOffset(months=int(horizon_months))
    rows = []
    for r in df[df["active"]].itertuples():
        if r.next_date is None or pd.isna(r.next_date):
            continue
        base, step = pd.Timestamp(r.next_date).normalize(), MONTHS_PER_CYCLE[r.cycle]
        k = 0
        while base + pd.DateOffset(months=k) < today:           # pierwsza płatność dziś lub później
            k += step
        while base + pd.DateOffset(months=k) <= end:
            rows.append({"date": base + pd.DateOffset(months=k), "name": r.name, "amount": float(r.amount)})
            k += step
    return pd.DataFrame(rows, columns=["date", "name", "amount"]).sort_values(["date", "name"]).reset_index(drop=True)


def fund_plan(df, pool=0.0, buffer=0.0, today=None, horizon_months=12):
    """Plan wspólnego funduszu na koszty okresowe. Zakłada równe wpłaty co miesiąc od dziś i sprawdza każdy termin:
    środki dostępne w dniu płatności (odłożone + wpłaty) muszą pokryć sumę wszystkich płatności do tego dnia plus zapas.
    Zwraca najniższą taką wpłatę miesięczną i harmonogram płatności (najbliższy termin liczony min. 1 miesiąc)."""
    today = (pd.Timestamp(today) if today is not None else pd.Timestamp.today()).normalize()
    occ = occurrences(df, today, horizon_months)
    pool, buffer = float(pool or 0.0), float(buffer or 0.0)
    if occ.empty:
        return {"monthly": 0.0, "total_due": 0.0, "schedule": occ.assign(t=[], cum=[], funds=[], after=[]), "binding": None}
    occ["t"] = ((occ["date"] - today).dt.days / DAYS_PER_MONTH).clip(lower=1.0)      # miesiące od dziś (min. 1)
    occ["cum"] = occ["amount"].cumsum()
    needs = (occ["cum"] + buffer - pool) / occ["t"]
    monthly = max(0.0, float(needs.max()))
    occ["funds"] = pool + monthly * occ["t"]
    occ["after"] = occ["funds"] - occ["cum"]
    return {"monthly": monthly, "total_due": float(occ["amount"].sum()), "schedule": occ,
            "binding": occ.loc[needs.idxmax()] if monthly > 0 else None}
