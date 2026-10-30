import pandas as pd

UTILITIES = {"prad": "Prąd", "gaz": "Gaz", "woda": "Woda", "paliwo": "Paliwo"}
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
        if u == "paliwo":
            fm = fuel_months(df)[-window:]
            total = float(fuel_monthly(df, fm)["cost"].sum()) if fm else 0.0
            rows.append(dict(utility=u, months_used=len(fm), last_sum=total, per_month=total / len(fm) if fm else 0.0))
            continue
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
    d = df[df["utility"] != "paliwo"].copy()   # paliwo to wydatek osobny od rachunków za media
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


# ----------------------------------------------------------------------------------
# Gaz: stan konta u dostawcy, struktura kosztów, ceny jednostkowe, prognoza roczna
# ----------------------------------------------------------------------------------
GAS_KINDS = {"abonament": "Opłata abonamentowa", "paliwo": "Paliwo gazowe",
             "dystr_stala": "Dystrybucyjna stała", "dystr_zmienna": "Dystrybucyjna zmienna",
             "inne": "Inne opłaty", "razem": "Bez podziału (brak pozycji)"}
POWER_KINDS = {"energia": "Energia elektryczna czynna", "dystr_stala": "Opłata stała sieciowa",
               "mocowa": "Opłata mocowa", "dystr_zmienna": "Opłata zmienna sieciowa",
               "jakosciowa": "Opłata jakościowa", "oze": "Opłata OZE", "kogeneracyjna": "Opłata kogeneracyjna",
               "abonament": "Opłata abonamentowa", "przejsciowa": "Opłata przejściowa",
               "handlowa": "Opłata handlowa", "dystrybucja": "Dystrybucja (razem)", "inne": "Inne opłaty", "razem": "Bez podziału (brak pozycji)"}
LABELS = {"gaz": GAS_KINDS, "prad": POWER_KINDS}
# opłaty stałe (zł / miesiąc) i zależne od zużycia (zł / kWh) wg medium
FIXED_KINDS = {"gaz": ("abonament", "dystr_stala"), "prad": ("abonament", "dystr_stala", "mocowa", "przejsciowa", "handlowa")}
VARIABLE_KINDS = {"gaz": ("paliwo", "dystr_zmienna"),
                  "prad": ("energia", "dystr_zmienna", "jakosciowa", "oze", "kogeneracyjna")}
GAS_FIXED, GAS_VARIABLE = FIXED_KINDS["gaz"], VARIABLE_KINDS["gaz"]


def balance_history(bills, utility="gaz"):
    """Stan konta u dostawcy po kolejnych fakturach (+ nadpłata, - zaległość)."""
    b = bills[(bills["utility"] == utility) & bills["account_balance"].notna()]
    if b.empty:
        return pd.DataFrame(columns=["date", "invoice_no", "balance"])
    out = b[["issue_date", "invoice_no", "account_balance"]].rename(
        columns={"issue_date": "date", "account_balance": "balance"})
    return out.sort_values("date").reset_index(drop=True)


def item_daily(conn, bills, utility):
    """Dobowy koszt brutto pozycji faktur danego medium, wg rodzaju. Faktury bez pozycji: jedna pozycja 'razem'."""
    items = pd.read_sql_query("SELECT * FROM bill_items", conn)
    for c in ("period_start", "period_end"):
        items[c] = pd.to_datetime(items[c], errors="coerce")
    by_bill = {k: g for k, g in items.groupby("bill_id")}
    parts = []
    for b in bills[bills["utility"] == utility].itertuples():
        bp = _period(b.period_start, b.period_end, b.issue_date)
        rows = []
        if b.id in by_bill:
            for it in by_bill[b.id].itertuples():
                ok = not (pd.isna(it.period_start) or pd.isna(it.period_end)) and it.period_end >= it.period_start
                rows.append((it.kind, it.amount_gross, (it.period_start, it.period_end) if ok else bp))
        elif not pd.isna(b.amount_gross):
            rows.append(("razem", b.amount_gross, bp))
        for kind, amount, p in rows:
            if p is None:
                continue
            days = pd.date_range(p[0], p[1], freq="D")
            parts.append(pd.DataFrame({"date": days, "kind": kind, "cost": amount / len(days)}))
    if not parts:
        return pd.DataFrame(columns=["date", "kind", "cost"])
    return pd.concat(parts, ignore_index=True)


def cost_by_kind(daily_items, months):
    """Koszt wg rodzaju opłaty w podanych miesiącach (zł i udział %)."""
    if daily_items.empty or not months:
        return pd.DataFrame(columns=["cost", "share"])
    d = daily_items[daily_items["date"].dt.to_period("M").isin(months)]
    t = d.groupby("kind")["cost"].sum().to_frame()
    t["share"] = t["cost"] / t["cost"].sum() * 100 if t["cost"].sum() else 0.0
    return t.sort_values("cost", ascending=False)


def _price_rows(conn, bills, utility):
    """Pozycje z ceną jednostkową: data okresu, rodzaj, cena brutto (zł/kWh albo zł/mc)."""
    items = pd.read_sql_query(
        "SELECT bill_id, kind, unit, unit_price, vat_rate, period_start, period_end FROM bill_items "
        "WHERE unit_price IS NOT NULL", conn)
    if items.empty:
        return items
    for c in ("period_start", "period_end"):
        items[c] = pd.to_datetime(items[c], errors="coerce")
    items = items.merge(bills[["id", "utility", "issue_date"]], left_on="bill_id", right_on="id")
    items = items[items["utility"] == utility].copy()
    items["date"] = items["period_end"].fillna(items["issue_date"])
    items["price"] = items["unit_price"] * (1 + items["vat_rate"].fillna(0) / 100)
    return items


def price_series(conn, bills, utility):
    """(ceny zmienne zł/kWh, opłaty stałe zł/mc) w czasie, brutto, per rodzaj opłaty."""
    items = _price_rows(conn, bills, utility)
    if items.empty:
        return pd.DataFrame(), pd.DataFrame()

    def pivot(kinds):
        x = items[items["kind"].isin(kinds)]
        if x.empty:
            return pd.DataFrame()
        return x.pivot_table(index="date", columns="kind", values="price", aggfunc="mean").sort_index().rename(
            columns=LABELS[utility])
    return pivot(VARIABLE_KINDS[utility]), pivot(FIXED_KINDS[utility])


def forecast(conn, bills, utility, annual_usage):
    """Roczny koszt przy aktualnych cenach: opłaty stałe x 12 + zużycie (kWh) x cena zmienna. None, gdy brak cen."""
    items = _price_rows(conn, bills, utility)
    if items.empty or not annual_usage:
        return None
    latest = items.sort_values(["date", "bill_id"]).groupby("kind").tail(1).set_index("kind")["price"]
    fixed = sum(latest.get(k, 0.0) for k in FIXED_KINDS[utility])
    variable = sum(latest.get(k, 0.0) for k in VARIABLE_KINDS[utility])
    if not fixed and not variable:
        return None
    annual = fixed * 12 + variable * annual_usage
    return {"fixed_month": fixed, "per_kwh": variable, "annual_kwh": annual_usage,
            "annual_cost": annual, "per_month": annual / 12}


def gas_price_series(conn, bills):
    return price_series(conn, bills, "gaz")


def gas_forecast(conn, bills, annual_kwh):
    return forecast(conn, bills, "gaz", annual_kwh)


# ----------------------------------------------------------------------------------
# Paliwo: pojedyncze zakupy (faktura = jedno tankowanie), miesiące kalendarzowe
# ----------------------------------------------------------------------------------
FUEL_KINDS = {"lpg": "Gaz LPG", "pb95": "Benzyna 95", "pb98": "Benzyna 98", "diesel": "Diesel (ON)",
              "adblue": "AdBlue", "inne": "Inne (sklep, myjnia)"}


def fuel_bills(bills):
    b = bills[(bills["utility"] == "paliwo") & bills["amount_gross"].notna() & bills["issue_date"].notna()]
    return b.sort_values("issue_date")


def fuel_months(bills, today=None):
    """Miesiące od pierwszego zakupu do ostatniego zakończonego miesiąca (miesiąc bez tankowania to zero)."""
    b = fuel_bills(bills)
    if b.empty:
        return []
    last = (pd.Timestamp(today) if today is not None else pd.Timestamp.today()).to_period("M") - 1
    first = b["issue_date"].min().to_period("M")
    return list(pd.period_range(first, last, freq="M")) if first <= last else []


def fuel_monthly(bills, months):
    """Koszt, liczba tankowań i litry w każdym z podanych miesięcy (brak zakupów = 0)."""
    b = fuel_bills(bills)
    g = b.groupby(b["issue_date"].dt.to_period("M")).agg(
        cost=("amount_gross", "sum"), refuels=("amount_gross", "size"), liters=("consumption", "sum"))
    return g.reindex(months, fill_value=0.0)


def fuel_summary(bills, months):
    """Podsumowanie okresu: koszt, tankowania, litry, średni koszt tankowania, średnio na miesiąc, cena za litr."""
    if not months:
        return None
    m = fuel_monthly(bills, months)
    b = fuel_bills(bills)
    inp = b[b["issue_date"].dt.to_period("M").isin(months)]
    with_l = inp[inp["consumption"] > 0]
    cost, refuels = float(m["cost"].sum()), int(m["refuels"].sum())
    liters = float(with_l["consumption"].sum())
    return {"cost": cost, "refuels": refuels, "liters": liters,
            "avg_refuel": cost / refuels if refuels else 0.0, "avg_month": cost / len(months),
            "price_per_l": float(with_l["amount_gross"].sum()) / liters if liters else None}


def fuel_compare(bills, months, all_months):
    """Okres vs te same miesiące rok wcześniej (tylko miesiące, które istnieją po obu stronach)."""
    done = set(all_months)
    common = [m for m in months if (m - 12) in done]
    if not common:
        return None, 0
    return {"now": fuel_summary(bills, common), "before": fuel_summary(bills, [m - 12 for m in common])}, len(common)


def fuel_by_kind(conn, bills, months):
    """Litry i koszt wg rodzaju paliwa z pozycji faktur w podanych miesiącach."""
    items = pd.read_sql_query("SELECT bill_id, kind, quantity, unit, amount_gross FROM bill_items WHERE meter='paliwo'", conn)
    if items.empty or not months:
        return pd.DataFrame(columns=["liters", "cost"])
    b = fuel_bills(bills)[["id", "issue_date"]]
    x = items.merge(b, left_on="bill_id", right_on="id")
    x = x[x["issue_date"].dt.to_period("M").isin(months)]
    x["liters"] = x["quantity"].where(x["unit"] == "l", 0.0)
    return x.groupby("kind").agg(liters=("liters", "sum"), cost=("amount_gross", "sum")).sort_values("cost", ascending=False)


# ----------------------------------------------------------------------------------
# Kontrola odczytu: ile faktur ma pozycje kosztów i czy ich suma zgadza się z kwotą faktury
# ----------------------------------------------------------------------------------
def items_coverage(conn, bills, utility):
    b = bills[bills["utility"] == utility][["id", "invoice_no", "issue_date", "amount_gross", "status"]].copy()
    it = pd.read_sql_query("SELECT bill_id, COUNT(*) AS n, SUM(amount_gross) AS total FROM bill_items GROUP BY bill_id", conn)
    b = b.merge(it, left_on="id", right_on="bill_id", how="left")
    b["n"] = b["n"].fillna(0).astype(int)
    b["diff"] = b["total"] - b["amount_gross"]
    b["ok"] = (b["n"] > 0) & (b["diff"].abs() <= 0.01 * b["n"] + 0.03)
    return b.sort_values("issue_date", ascending=False).reset_index(drop=True)


# ----------------------------------------------------------------------------------
# Paliwo: ruchome okno czasowe statystyk (np. "ostatnie 2 miesiące", "od 1 września")
# ----------------------------------------------------------------------------------
DEFAULT_FUEL_CFG = {"mode": "months", "n": 12}
DAYS_PER_MONTH = 30.4375


def fuel_window(cfg=None, today=None):
    """(początek, koniec) okna statystyk paliwa wg ustawienia. Tryby: months (n ostatnich miesięcy do dziś),
    days (n ostatnich dni), since (od daty do dziś), year (rok kalendarzowy)."""
    cfg = cfg or DEFAULT_FUEL_CFG
    today = (pd.Timestamp(today) if today is not None else pd.Timestamp.today()).normalize()
    mode = cfg.get("mode", "months")
    if mode == "days":
        return today - pd.Timedelta(days=int(cfg.get("n", 30)) - 1), today
    if mode == "since" and cfg.get("since"):
        return min(pd.Timestamp(cfg["since"]).normalize(), today), today
    if mode == "year" and cfg.get("year"):
        y = int(cfg["year"])
        return pd.Timestamp(y, 1, 1), min(pd.Timestamp(y, 12, 31), today)
    n = max(1, int(cfg.get("n", 12)))          # okno kroczące: n miesięcy wstecz do dziś (z bieżącym miesiącem)
    return (today - pd.DateOffset(months=n) + pd.Timedelta(days=1)).normalize(), today


def fuel_window_label(cfg=None):
    cfg = cfg or DEFAULT_FUEL_CFG
    mode = cfg.get("mode", "months")
    if mode == "days":
        return f"Ostatnie {cfg.get('n', 30)} dni"
    if mode == "since":
        return "Od wybranej daty"
    if mode == "year":
        return f"Rok {cfg.get('year')}"
    n = int(cfg.get("n", 12))
    return "Ostatni miesiąc" if n == 1 else f"Ostatnie {n} miesiące" if n in (2, 3, 4) else f"Ostatnie {n} miesięcy"


def fuel_stats(bills, start, end):
    """Statystyki zakupów paliwa w oknie [start, end]. Średnia miesięczna jest liczona z dni okna (koszt / dni x 30,44),
    a okno jest skracane do daty pierwszego zakupu, żeby brak danych nie zaniżał średniej."""
    allb = fuel_bills(bills)
    if allb.empty:
        return None
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    first = allb["issue_date"].min().normalize()
    eff_start = max(start, first)
    b = allb[(allb["issue_date"] >= eff_start) & (allb["issue_date"] <= end + pd.Timedelta(hours=23, minutes=59))]
    days = (end - eff_start).days + 1 if end >= eff_start else 0
    with_l = b[b["consumption"] > 0]
    cost, refuels = float(b["amount_gross"].sum()), int(len(b))
    liters = float(with_l["consumption"].sum())
    return {"start": start, "end": end, "eff_start": eff_start, "days": days, "first": first,
            "cost": cost, "refuels": refuels, "liters": liters,
            "avg_refuel": cost / refuels if refuels else 0.0,
            "avg_month": cost / days * DAYS_PER_MONTH if days > 0 else None,
            "price_per_l": float(with_l["amount_gross"].sum()) / liters if liters else None, "bills": b}


def fuel_stats_prev_year(bills, start, end):
    """To samo okno rok wcześniej; None, gdy sprzed roku nie ma jeszcze danych (pierwszy zakup później niż początek okna)."""
    allb = fuel_bills(bills)
    if allb.empty:
        return None
    ps, pe = pd.Timestamp(start) - pd.DateOffset(years=1), pd.Timestamp(end) - pd.DateOffset(years=1)
    if allb["issue_date"].min().normalize() > ps:
        return None
    return fuel_stats(bills, ps, pe)


def fuel_monthly_in(stats_dict):
    """Koszt i liczba tankowań wg miesięcy w oknie (miesiące bez zakupów = 0)."""
    b = stats_dict["bills"]
    months = pd.period_range(stats_dict["eff_start"].to_period("M"), stats_dict["end"].to_period("M"), freq="M")
    g = b.groupby(b["issue_date"].dt.to_period("M")).agg(cost=("amount_gross", "sum"), refuels=("amount_gross", "size"))
    return g.reindex(months, fill_value=0.0)


def fuel_by_kind_in(conn, bills, stats_dict):
    items = pd.read_sql_query("SELECT bill_id, kind, quantity, unit, amount_gross FROM bill_items WHERE meter='paliwo'", conn)
    ids = set(stats_dict["bills"]["id"])
    x = items[items["bill_id"].isin(ids)].copy()
    if x.empty:
        return pd.DataFrame(columns=["liters", "cost"])
    x["liters"] = x["quantity"].where(x["unit"] == "l", 0.0)
    return x.groupby("kind").agg(liters=("liters", "sum"), cost=("amount_gross", "sum")).sort_values("cost", ascending=False)
