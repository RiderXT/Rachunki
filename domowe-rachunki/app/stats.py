import pandas as pd

UTILITIES = {"prad": "Prąd", "gaz": "Gaz", "woda": "Woda"}
DATE_COLS = ["issue_date", "period_start", "period_end"]


def load_bills(conn):
    df = pd.read_sql_query(
        "SELECT * FROM bills ORDER BY COALESCE(period_end, issue_date), id", conn)
    for c in DATE_COLS:
        df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def _period(start, end, issue):
    """Okres faktury; bez okresu -> miesiąc wystawienia; None, gdy nie da się ustalić."""
    if pd.isna(start) or pd.isna(end):
        if pd.isna(issue):
            return None
        s = issue.replace(day=1)
        return s, s + pd.offsets.MonthEnd(0)
    return (start, end) if end >= start else None


def daily_costs(df):
    """Rozkłada każdą fakturę równo na dni jej okresu rozliczeniowego.
    Brak okresu -> cały miesiąc wystawienia. Brak kwoty -> pomijana."""
    parts = []
    for r in df.itertuples():
        if pd.isna(r.amount_gross):
            continue
        p = _period(r.period_start, r.period_end, r.issue_date)
        if p is None:
            continue
        days = pd.date_range(p[0], p[1], freq="D")
        parts.append(pd.DataFrame(
            {"date": days, "utility": r.utility, "cost": r.amount_gross / len(days)}))
    if not parts:
        return pd.DataFrame(columns=["date", "utility", "cost"])
    return pd.concat(parts, ignore_index=True)


def monthly_costs(df):
    d = daily_costs(df)
    if d.empty:
        return pd.DataFrame()
    d["month"] = d["date"].dt.to_period("M").dt.to_timestamp()
    return d.pivot_table(index="month", columns="utility", values="cost",
                         aggfunc="sum", fill_value=0).sort_index()


def yearly_costs(df):
    d = daily_costs(df)
    if d.empty:
        return pd.DataFrame()
    d["year"] = d["date"].dt.year
    return d.pivot_table(index="year", columns="utility", values="cost",
                         aggfunc="sum", fill_value=0).sort_index()


def suggested_savings(df, window=12):
    """Średnia miesięczna z ostatnich `window` miesięcy w pełni pokrytych fakturami
    (osobno dla każdego medium). Miesiące z dziurą w fakturach są pomijane."""
    d = daily_costs(df)
    rows = []
    for u in UTILITIES:
        x = d[d["utility"] == u]
        if x.empty:
            rows.append(dict(utility=u, months_used=0, last_sum=0.0, per_month=0.0))
            continue
        g = x.groupby(x["date"].dt.to_period("M")).agg(
            cost=("cost", "sum"), days=("date", "nunique"))
        g["dim"] = g.index.days_in_month
        full = g[g["days"] == g["dim"]].tail(window)
        n = len(full)
        total = float(full["cost"].sum())
        rows.append(dict(utility=u, months_used=n, last_sum=total,
                         per_month=total / n if n else 0.0))
    return pd.DataFrame(rows)


def savings_balance(df, contribution, start_month):
    """Saldo 'skarbonki': stałe wpłaty miesięczne minus faktury (wg daty wystawienia)."""
    d = df.copy()
    d["pay_date"] = d["issue_date"].fillna(d["period_end"])
    d = d.dropna(subset=["pay_date", "amount_gross"])
    if d.empty:
        return pd.DataFrame()
    start = pd.Timestamp(start_month).to_period("M")
    end = max(pd.Timestamp.today().to_period("M"), d["pay_date"].max().to_period("M"))
    months = pd.period_range(start, end, freq="M")
    paid = d.groupby(d["pay_date"].dt.to_period("M"))["amount_gross"].sum().reindex(
        months, fill_value=0)
    out = pd.DataFrame({"wplaty": float(contribution), "faktury": paid.values},
                       index=months.to_timestamp())
    out["saldo"] = (out["wplaty"] - out["faktury"]).cumsum()
    return out


# ----------------------------------------------------------------------------------
# Woda: dom vs ogród, zużycie i koszty, porównanie rok do roku
# ----------------------------------------------------------------------------------
METERS = {"dom": "Dom", "ogrod": "Ogród", "inny": "Inne", "bez_podzialu": "Bez podziału (brak pozycji)",
          "gaz": "Gaz", "prad": "Prąd"}


def complete_months(df):
    """Miesiące w pełni pokryte okresami faktur (bez dziur)."""
    d = daily_costs(df)
    if d.empty:
        return []
    g = d.groupby(d["date"].dt.to_period("M"))["date"].nunique()
    return sorted(m for m, n in g.items() if n == m.days_in_month)


def water_daily(conn, bills):
    """Dobowe koszty i zużycie wody per licznik. Faktury bez pozycji trafiają jako 'bez podziału'."""
    bills = bills[bills["utility"] == "woda"]
    items = pd.read_sql_query("SELECT * FROM bill_items", conn)
    for c in ("period_start", "period_end"):
        items[c] = pd.to_datetime(items[c], errors="coerce")
    by_bill = {k: g for k, g in items.groupby("bill_id")}
    parts = []
    for b in bills.itertuples():
        bp = _period(b.period_start, b.period_end, b.issue_date)
        rows = []  # (meter, kind, amount, m3, period)
        if b.id in by_bill:
            for it in by_bill[b.id].itertuples():
                p = (it.period_start, it.period_end) if not (
                    pd.isna(it.period_start) or pd.isna(it.period_end)) and it.period_end >= it.period_start else bp
                m3 = it.quantity if it.kind == "woda" and it.unit == "m3" and not pd.isna(it.quantity) else 0.0
                rows.append((it.meter, it.kind, it.amount_gross, m3, p))
        elif not pd.isna(b.amount_gross):
            m3 = 0.0 if pd.isna(b.consumption) else b.consumption
            rows.append(("bez_podzialu", "razem", b.amount_gross, m3, bp))
        for meter, kind, amount, m3, p in rows:
            if p is None:
                continue
            days = pd.date_range(p[0], p[1], freq="D")
            parts.append(pd.DataFrame({"date": days, "meter": meter, "kind": kind,
                                       "cost": amount / len(days), "usage": m3 / len(days)}))
    if not parts:
        return pd.DataFrame(columns=["date", "meter", "kind", "cost", "usage"])
    return pd.concat(parts, ignore_index=True)


def water_by_meter(daily, months):
    if daily.empty or not months:
        return pd.DataFrame(columns=["cost", "usage"])
    d = daily[daily["date"].dt.to_period("M").isin(months)]
    return d.groupby("meter")[["cost", "usage"]].sum()


def _with_total(t):
    if t.empty:
        return t
    t = t.copy()
    t.loc["razem"] = t.sum()
    return t


def water_table(daily, months, complete):
    """(podsumowanie okresu, porównanie z rokiem wcześniej na wspólnych miesiącach, liczba wspólnych miesięcy)."""
    totals = _with_total(water_by_meter(daily, months))
    if not totals.empty:
        totals["price"] = totals["cost"] / totals["usage"].where(totals["usage"] > 0)
    done = set(complete)
    # miesiące ze starych faktur bez podziału na liczniki porównujemy tylko z takimi samymi
    nosplit = set(daily.loc[daily["meter"] == "bez_podzialu", "date"].dt.to_period("M"))
    common = [m for m in months if (m - 12) in done and ((m in nosplit) == ((m - 12) in nosplit))]
    cur = _with_total(water_by_meter(daily, common))
    prev = _with_total(water_by_meter(daily, [m - 12 for m in common]))
    if cur.empty or prev.empty:
        return totals, pd.DataFrame(), 0
    idx = cur.index.union(prev.index, sort=False)
    cur, prev = cur.reindex(idx).fillna(0.0), prev.reindex(idx).fillna(0.0)
    cmp_ = pd.DataFrame({
        "usage": cur["usage"], "usage_prev": prev["usage"],
        "usage_chg": (cur["usage"] / prev["usage"].where(prev["usage"] > 0) - 1) * 100,
        "cost": cur["cost"], "cost_prev": prev["cost"],
        "cost_chg": (cur["cost"] / prev["cost"].where(prev["cost"] > 0) - 1) * 100,
    })
    return totals, cmp_, len(common)


def water_monthly(daily, months, value):
    """Tabela miesiąc x licznik (value: 'cost' lub 'usage') dla podanych miesięcy."""
    if daily.empty or not months:
        return pd.DataFrame()
    d = daily[daily["date"].dt.to_period("M").isin(months)].copy()
    d["month"] = d["date"].dt.to_period("M").dt.to_timestamp()
    return d.pivot_table(index="month", columns="meter", values=value, aggfunc="sum",
                         fill_value=0).sort_index()


def water_price_series(conn, bills):
    """Zmienna część ceny za m3 (woda + ścieki, jeśli są) z każdej faktury, osobno dla liczników."""
    items = pd.read_sql_query(
        "SELECT bill_id, meter, kind, quantity, unit, amount_gross FROM bill_items", conn)
    if items.empty:
        return pd.DataFrame()
    b = bills[["id", "issue_date", "period_end"]]
    items = items.merge(b, left_on="bill_id", right_on="id")
    items["date"] = items["period_end"].fillna(items["issue_date"])
    rows = []
    for (_, meter), g in items.groupby(["bill_id", "meter"]):
        q = g.loc[(g["kind"] == "woda") & (g["unit"] == "m3"), "quantity"].sum()
        cost = g.loc[g["kind"].isin(["woda", "scieki"]), "amount_gross"].sum()
        if q > 0:
            rows.append({"date": g["date"].iloc[0], "meter": meter, "price": cost / q})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).pivot_table(index="date", columns="meter", values="price").sort_index()


# ----------------------------------------------------------------------------------
# Media z jednym licznikiem (gaz, prąd): to samo co woda, ale zużycie z pola `consumption` faktury
# ----------------------------------------------------------------------------------
compare_table = water_table      # (podsumowanie okresu, porównanie rok do roku, liczba wspólnych miesięcy)
monthly_by_meter = water_monthly


def usage_daily(bills, utility):
    """Dobowy koszt i zużycie faktur danego medium (okres faktury rozkładany równo na dni)."""
    parts = []
    for b in bills[bills["utility"] == utility].itertuples():
        if pd.isna(b.amount_gross):
            continue
        p = _period(b.period_start, b.period_end, b.issue_date)
        if p is None:
            continue
        days = pd.date_range(p[0], p[1], freq="D")
        use = 0.0 if pd.isna(b.consumption) else b.consumption
        parts.append(pd.DataFrame({"date": days, "meter": utility, "kind": "razem",
                                   "cost": b.amount_gross / len(days), "usage": use / len(days)}))
    if not parts:
        return pd.DataFrame(columns=["date", "meter", "kind", "cost", "usage"])
    return pd.concat(parts, ignore_index=True)


def unit_price_series(bills, utility):
    """Koszt całkowity faktury / zużycie (zł za jednostkę) na kolejnych fakturach."""
    b = bills[(bills["utility"] == utility) & bills["amount_gross"].notna() & (bills["consumption"] > 0)].copy()
    if b.empty:
        return pd.DataFrame()
    b["date"] = b["period_end"].fillna(b["issue_date"])
    b[utility] = b["amount_gross"] / b["consumption"]
    return b.set_index("date")[[utility]].sort_index()


def year_overlay(daily, months, value):
    """Tabela miesiąc (1-12) x rok, do porównania sezonowości rok do roku."""
    if daily.empty or not months:
        return pd.DataFrame()
    d = daily[daily["date"].dt.to_period("M").isin(months)]
    m = d.groupby(d["date"].dt.to_period("M"))[value].sum()
    df = pd.DataFrame({"year": [p.year for p in m.index], "month": [p.month for p in m.index], "v": m.values})
    return df.pivot(index="month", columns="year", values="v").sort_index().rename(columns=str)
