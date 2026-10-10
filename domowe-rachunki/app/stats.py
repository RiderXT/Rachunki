import pandas as pd

UTILITIES = {"prad": "Prąd", "gaz": "Gaz", "woda": "Woda"}
DATE_COLS = ["issue_date", "period_start", "period_end"]


def load_bills(conn):
    df = pd.read_sql_query(
        "SELECT * FROM bills ORDER BY COALESCE(period_end, issue_date), id", conn)
    for c in DATE_COLS:
        df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def daily_costs(df):
    """Rozkłada każdą fakturę równo na dni jej okresu rozliczeniowego.
    Brak okresu -> cały miesiąc wystawienia. Brak kwoty -> pomijana."""
    parts = []
    for r in df.itertuples():
        if pd.isna(r.amount_gross):
            continue
        s, e = r.period_start, r.period_end
        if pd.isna(s) or pd.isna(e):
            if pd.isna(r.issue_date):
                continue
            s = r.issue_date.replace(day=1)
            e = s + pd.offsets.MonthEnd(0)
        if e < s:
            continue
        days = pd.date_range(s, e, freq="D")
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
