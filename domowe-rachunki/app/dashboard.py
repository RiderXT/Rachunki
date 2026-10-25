import hmac
import os
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from app import checklist, db, fuel_manual, invoices, stats, subscriptions
from app.version import VERSION

st.set_page_config(page_title="Rachunki domowe", layout="wide")
PDF_DIR = Path(os.environ.get("PDF_DIR", "/pdf")).resolve()

# Opcjonalne hasło do panelu (APP_PASSWORD w .env); faktury zawierają dane osobowe.
_pw = os.environ.get("APP_PASSWORD", "")
if _pw and not st.session_state.get("auth"):
    typed = st.text_input("Hasło", type="password")
    if typed and hmac.compare_digest(typed, _pw):
        st.session_state["auth"] = True
        st.rerun()
    st.stop()

LABEL = stats.UTILITIES
DEFAULT_PROVIDER = {"prad": "Enea", "gaz": "PGNiG", "woda": "Wodociągi", "paliwo": "ORLEN"}
EDIT_COLS = ["provider", "utility", "invoice_no", "issue_date", "period_start",
             "period_end", "amount_gross", "consumption", "unit", "note"]


def fmt(x):
    return f"{x:,.0f} zł".replace(",", " ")


def fmt2(x):
    return "brak kwoty" if pd.isna(x) else f"{x:,.2f} zł".replace(",", " ").replace(".", ",")


def txt(v):
    """Tekst albo None (pandas zamienia puste wartości na NaN)."""
    return v if isinstance(v, str) and v else None


def safe_pdf(path):
    """Zwraca ścieżkę do PDF-a tylko jeśli leży w PDF_DIR i istnieje."""
    if not txt(path):
        return None
    p = Path(path).resolve()
    try:
        p.relative_to(PDF_DIR)
    except ValueError:
        return None
    return p if p.is_file() else None


def clean(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    return v.isoformat() if isinstance(v, date) else v


with db.conn() as c:
    bills = stats.load_bills(c)
    water_daily = stats.water_daily(c, bills)
    water_price = stats.water_price_series(c, bills)
subs_all = subscriptions.load()
subs = subs_all[subs_all["category"] == "sub"].reset_index(drop=True)              # subskrypcje
periodic = subs_all[subs_all["category"] == "periodic"].reset_index(drop=True)     # koszty roczne i okresowe
sub_sum = subscriptions.summary(subs)
per_table = subscriptions.periodic_table(periodic)
per_sum = subscriptions.periodic_summary(per_table)
water_bills = bills[bills["utility"] == "woda"]
water_done = stats.complete_months(water_bills) if not water_bills.empty else []

sav = stats.suggested_savings(bills) if not bills.empty else None
suggested = float(sav[sav["utility"] != "paliwo"]["per_month"].sum()) if sav is not None else 0.0

# okno statystyk paliwa (ustawiane w zakładce Paliwo) wyznacza też średnią w "Statystykach" i "Subskrypcjach"
fuel_cfg = db.get_setting("fuel_period", stats.DEFAULT_FUEL_CFG)
fuel_start, fuel_end = stats.fuel_window(fuel_cfg)
fuel_now = stats.fuel_stats(bills, fuel_start, fuel_end) if not bills.empty else None
if sav is not None and fuel_now and fuel_now["avg_month"] is not None:
    sav["months_used"] = sav["months_used"].astype(float)
    _i = sav.index[sav["utility"] == "paliwo"][0]
    sav.loc[_i, "months_used"] = round(fuel_now["days"] / stats.DAYS_PER_MONTH, 1)
    sav.loc[_i, "last_sum"] = fuel_now["cost"]
    sav.loc[_i, "per_month"] = fuel_now["avg_month"]

st.sidebar.header("Skarbonka")
contribution = st.sidebar.number_input("Miesięczna wpłata (zł)", min_value=0.0, step=10.0,
                                       value=float(round(suggested)))
balance_from = st.sidebar.date_input("Liczone od", value=date(date.today().year, 1, 1))

_built = Path("/srv/build_time.txt")
_sha = os.environ.get("GIT_SHA", "")
st.sidebar.divider()
st.sidebar.caption(f"Wersja {VERSION}" + (f" ({_sha})" if _sha else "") +
                   (f" · zbudowano {_built.read_text().strip()}" if _built.exists() else ""))

st.title("Rachunki domowe")
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))
if not bills.empty:
    n_partial = int((bills["status"] == "partial").sum())
    if n_partial:
        st.warning(f"{n_partial} faktur ma niepełne dane (brak okresu lub kwoty). "
                   "Uzupełnij je w zakładce „Lista” albo wgraj PDF faktury w zakładce „Wgraj PDF”. "
                   "Bez okresu kwota liczona jest do miesiąca wystawienia, bez kwoty faktura jest pomijana.")

(t_stats, t_month, t_water, t_gas, t_power, t_fuel, t_subs, t_periodic,
 t_files, t_upload, t_add, t_csv, t_list) = st.tabs(
    ["Statystyki", "Miesiąc", "Woda", "Gaz", "Prąd", "Paliwo", "Subskrypcje", "Koszty roczne", "Faktury PDF",
     "Wgraj PDF", "Dodaj fakturę", "Import CSV", "Lista"])

with t_stats:
    if bills.empty:
        st.info("Brak danych. Dodaj pierwszą fakturę albo zaimportuj CSV.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Odkładaj miesięcznie (media)", fmt(suggested), help="Prąd, gaz i woda; paliwo jest osobno.")
        m2.metric("Koszt z użytych miesięcy (media)",
                  fmt(float(sav[sav["utility"] != "paliwo"]["last_sum"].sum())))
        _fuel = sav[sav["utility"] == "paliwo"].iloc[0]
        m3.metric("Paliwo średnio / miesiąc", fmt(float(_fuel["per_month"])) if _fuel["months_used"] else "brak danych",
                  help=f"Okres: {stats.fuel_window_label(fuel_cfg)} (zmienisz w zakładce Paliwo).")
        m4.metric("Liczba faktur", len(bills))
        if not subs.empty or not periodic.empty:
            sb1, sb2, sb3 = st.columns(3)
            sb1.metric("Subskrypcje / miesiąc", fmt2(sub_sum["monthly"]))
            sb2.metric("Koszty roczne: odkładaj / miesiąc", fmt2(per_sum["monthly"]),
                       help="OC, przeglądy, ubezpieczenia itp. przeliczone na miesiąc. Szczegóły w zakładce „Koszty roczne”.")
            sb3.metric("Stałe wydatki / miesiąc", fmt2(suggested + sub_sum["monthly"] + per_sum["monthly"]),
                       help="Media (średnia z ostatnich 12 pełnych miesięcy) + subskrypcje + koszty roczne.")
        for _u, _owner in (("gaz", "PGNiG"), ("prad", "Enei")):
            _hist = stats.balance_history(bills, _u)
            if not _hist.empty:
                _last = _hist.iloc[-1]
                st.metric(f"Stan konta u {_owner}" + (" (nadpłata)" if _last["balance"] >= 0 else " (zaległość)"),
                          fmt2(abs(_last["balance"])), help=f"Wg faktury {_last['invoice_no']} z {_last['date']:%Y-%m-%d}.")
        monthly = stats.monthly_costs(bills)
        if not monthly.empty:
            st.subheader("Koszty miesięczne (zł)")
            st.bar_chart(monthly.rename(columns=LABEL))
        yearly = stats.yearly_costs(bills)
        if not yearly.empty:
            st.subheader("Koszty roczne (zł, wg kalendarza; bieżący rok niepełny)")
            st.dataframe(yearly.rename(columns=LABEL).round(2))
        bal = stats.savings_balance(bills, contribution, balance_from)
        if not bal.empty:
            st.subheader("Saldo skarbonki (wpłaty − faktury, narastająco)")
            st.line_chart(bal[["saldo"]])

def usage_tab(daily, done, key, noun, unit, price, price_title, empty_msg):
    """Wspólna zakładka zużycia i kosztów (woda, gaz): okres, podsumowanie, rok do roku, wykresy."""
    if not done:
        st.info(empty_msg)
        return
    years = sorted({m.year for m in done}, reverse=True)
    choice = st.selectbox("Okres", ["Ostatnie 12 miesięcy"] + [f"Rok {y}" for y in years], key=f"period_{key}")
    months = done[-12:] if choice.startswith("Ostatnie") else [m for m in done if m.year == int(choice.split()[1])]
    totals, compare, n_common = stats.compare_table(daily, months, done)
    name = lambda k: stats.METERS.get(k, "Razem" if k == "razem" else k)  # noqa: E731
    dec = 1 if unit == "m³" else 0
    st.caption(f"Pełne miesiące w okresie: {len(months)} ({months[0]} – {months[-1]}). "
               "Zużycie i koszty są rozkładane na dni okresu rozliczeniowego z faktury.")

    if "razem" in totals.index:
        k1, k2, k3 = st.columns(3)
        k1.metric(f"Zużycie {noun}", f"{totals.loc['razem', 'usage']:,.{dec}f} {unit}".replace(",", " "))
        k2.metric("Koszt", fmt(totals.loc["razem", "cost"]))
        k3.metric("Średnio / miesiąc", fmt(totals.loc["razem", "cost"] / len(months)))
        tbl = totals.rename(index=name)
        st.dataframe(pd.DataFrame({
            f"Zużycie ({unit})": tbl["usage"].round(2), "Koszt (zł)": tbl["cost"].round(2),
            f"Koszt na {unit} (zł)": tbl["price"].round(3 if unit == "kWh" else 2)}))
        st.caption(f"Koszt na {unit} = koszt całkowity (razem z opłatami stałymi) podzielony przez zużycie.")

    st.subheader("Rok do roku")
    if compare.empty:
        st.info("Za mało danych do porównania: potrzebne są te same miesiące z poprzedniego roku "
                "(i ze szczegółowymi pozycjami faktur). Porównanie pojawi się samo, gdy dane się uzbierają.")
    else:
        st.caption(f"Porównanie na {n_common} wspólnych miesiącach (te same miesiące rok wcześniej).")
        cmp_tbl = compare.rename(index=name)
        st.dataframe(pd.DataFrame({
            f"Zużycie ({unit})": cmp_tbl["usage"].round(2), f"rok wcześniej ({unit})": cmp_tbl["usage_prev"].round(2),
            "zmiana zużycia (%)": cmp_tbl["usage_chg"].round(1),
            "Koszt (zł)": cmp_tbl["cost"].round(2), "rok wcześniej (zł)": cmp_tbl["cost_prev"].round(2),
            "zmiana kosztu (%)": cmp_tbl["cost_chg"].round(1)}))

    for value, title in (("usage", f"Zużycie miesięczne ({unit})"), ("cost", "Koszt miesięczny (zł)")):
        monthly = stats.monthly_by_meter(daily, months, value)
        if not monthly.empty:
            st.subheader(title)
            st.bar_chart(monthly.rename(columns=name))
    overlay = stats.year_overlay(daily, done, "cost")
    if overlay.shape[1] > 1:
        st.subheader("Koszt miesiąc po miesiącu: porównanie lat (zł)")
        st.line_chart(overlay)
    if not price.empty:
        st.subheader(price_title)
        st.line_chart(price.rename(columns=name))
    return months


with t_water:
    usage_tab(water_daily, water_done, "water", "wody", "m³", water_price,
              "Cena za m³ na kolejnych fakturach (woda + ścieki, bez opłat stałych)",
              "Brak w pełni pokrytych miesięcy z fakturami za wodę. Pobierz faktury (konektor) "
              "albo dodaj je ręcznie.")

def utility_tab(utility, noun, owner, price_title, empty_msg):
    """Zakładka medium z pozycjami faktur (gaz, prąd): stan konta u dostawcy, zużycie i koszty, rok do roku,
    z czego składa się rachunek, ceny jednostkowe w czasie oraz prognoza roczna przy obecnych cenach."""
    ub = bills[bills["utility"] == utility]
    done = stats.complete_months(ub) if not ub.empty else []
    daily = stats.usage_daily(ub, utility)
    price = stats.unit_price_series(ub, utility)
    labels = stats.LABELS[utility]
    hist = stats.balance_history(bills, utility)
    if not hist.empty:
        last = hist.iloc[-1]
        st.subheader(f"Skarbonka u {owner}")
        b1, b2, b3 = st.columns(3)
        b1.metric("Nadpłata na koncie" if last["balance"] >= 0 else "Zaległość na koncie", fmt2(abs(last["balance"])))
        b2.metric("Stan na dzień", f"{last['date']:%Y-%m-%d}")
        monthly = float(sav.loc[sav["utility"] == utility, "per_month"].iloc[0]) if sav is not None else 0.0
        if last["balance"] > 0 and monthly > 0:
            b3.metric("Wystarczy na", f"{last['balance'] / monthly:.1f} mies.",
                      help="Przy średnim miesięcznym koszcie z ostatnich 12 pełnych miesięcy.")
        st.caption(f"Stan konta po rozliczeniu z faktury {last['invoice_no']}.")
        if len(hist) > 1:
            st.line_chart(hist.set_index("date")[["balance"]].rename(columns={"balance": "Stan konta (zł)"}))

    with db.conn() as cc:
        cov = stats.items_coverage(cc, bills, utility)
    if not cov.empty:
        bad = cov[~cov["ok"]]
        with st.expander(f"Kontrola odczytu pozycji z faktur: {len(cov) - len(bad)} z {len(cov)} odczytanych w całości"):
            if bad.empty:
                st.success("Wszystkie faktury mają odczytane pozycje, a ich suma zgadza się z kwotą faktury.")
            else:
                st.warning("Te faktury nie mają pozycji albo ich suma nie zgadza się z kwotą faktury (np. inny układ PDF). "
                           "Spróbuj przycisku „Przelicz faktury z zapisanych PDF-ów” w zakładce „Wgraj PDF”. "
                           "Jeśli to nie pomoże, wgraj tu jeden z tych PDF-ów do rozpoznania.")
                st.dataframe(pd.DataFrame({
                    "Numer": bad["invoice_no"], "Wystawiona": bad["issue_date"].dt.strftime("%Y-%m-%d"),
                    "Kwota (zł)": bad["amount_gross"], "Pozycji": bad["n"], "Suma pozycji (zł)": bad["total"].round(2),
                    "Różnica (zł)": bad["diff"].round(2)}), hide_index=True)

    months = usage_tab(daily, done, utility, noun, "kWh", price, price_title, empty_msg)
    if not months:
        return
    with db.conn() as gc:
        items_daily = stats.item_daily(gc, bills, utility)
        prices_var, prices_fixed = stats.price_series(gc, bills, utility)
        last12 = daily[daily["date"].dt.to_period("M").isin(done[-12:])]
        annual_kwh = float(last12["usage"].sum())
        actual12 = float(last12["cost"].sum())
        fc = stats.forecast(gc, bills, utility, annual_kwh)
    struct = stats.cost_by_kind(items_daily, months)
    if not struct.empty and (struct.index != "razem").any():
        st.subheader("Z czego składa się rachunek")
        show = struct.rename(index=lambda k: labels.get(k, k))
        st.dataframe(pd.DataFrame({"Koszt (zł)": show["cost"].round(2), "Udział (%)": show["share"].round(1)}))
        fixed = float(struct.loc[struct.index.isin(stats.FIXED_KINDS[utility]), "cost"].sum())
        variable = float(struct.loc[struct.index.isin(stats.VARIABLE_KINDS[utility]), "cost"].sum())
        f1, f2 = st.columns(2)
        f1.metric("Opłaty stałe (miesięczne)", fmt(fixed))
        f2.metric("Opłaty zależne od zużycia", fmt(variable))
        st.caption("Kwoty brutto, rozłożone na dni okresów rozliczeniowych poszczególnych pozycji.")
        if "dystrybucja" in struct.index and struct.loc["dystrybucja", "cost"] > 0:
            st.info("Część faktur z tego okresu ma tylko podział zbiorczy (energia i dystrybucja razem). Wiersz "
                    "„Dystrybucja (razem)” nie jest wliczony do opłat stałych ani zależnych od zużycia, więc te dwie sumy "
                    "dotyczą wyłącznie faktur ze szczegółowymi pozycjami.")
    if not prices_var.empty:
        st.subheader("Ceny za kWh, brutto (zł)")
        if len(prices_var) > 1:
            st.line_chart(prices_var)
        st.dataframe(pd.DataFrame({"Aktualnie (zł / kWh)": prices_var.ffill().iloc[-1].round(4)}))
    if not prices_fixed.empty:
        st.subheader("Opłaty stałe za miesiąc, brutto (zł)")
        if len(prices_fixed) > 1:
            st.line_chart(prices_fixed)
        st.dataframe(pd.DataFrame({"Aktualnie (zł / mies.)": prices_fixed.ffill().iloc[-1].round(2)}))
    else:
        st.info("Nie odczytano opłat stałych (abonament, opłata stała sieciowa, mocowa) z żadnej faktury. "
                "Zobacz „Kontrolę odczytu pozycji z faktur” na górze tej zakładki.")
    if fc:
        st.subheader("Prognoza roczna przy obecnych cenach")
        p1, p2, p3 = st.columns(3)
        p1.metric("Opłaty stałe / miesiąc", fmt2(fc["fixed_month"]))
        p2.metric("Cena zmienna za kWh", f"{fc['per_kwh']:.3f} zł".replace(".", ","))
        p3.metric("Roczny koszt", fmt(fc["annual_cost"]),
                  delta=f"{fc['annual_cost'] - actual12:+,.0f} zł vs ostatnie 12 mies.".replace(",", " "),
                  delta_color="inverse")
        st.caption(f"Zakłada zużycie jak w ostatnich 12 miesiącach ({annual_kwh:,.0f} kWh) i ceny z najnowszej "
                   f"faktury, czyli około {fmt(fc['per_month'])} miesięcznie.".replace(",", " "))


with t_gas:
    utility_tab("gaz", "gazu", "PGNiG", "Koszt za kWh na kolejnych fakturach (z opłatami stałymi)",
                "Brak w pełni pokrytych miesięcy z fakturami za gaz. Uruchom import z eBOK "
                "(python -m app.pgnig --sync) albo wgraj faktury w zakładce „Wgraj PDF”.")

with t_power:
    utility_tab("prad", "prądu", "Enei", "Koszt za kWh na kolejnych fakturach (z opłatami stałymi)",
                "Brak w pełni pokrytych miesięcy z fakturami za prąd. Wgraj faktury Enei w zakładce „Wgraj PDF”. "
                "Historię pobierzesz hurtowo skryptem opisanym w README.")

with t_fuel:
    fb = stats.fuel_bills(bills)
    kinds_labels = list(stats.FUEL_KINDS.values())
    kind_by_label = {v: k for k, v in stats.FUEL_KINDS.items()}
    with st.expander("Dodaj tankowanie ręcznie (płatność przy kasie, bez faktury)", expanded=fb.empty):
        with st.form("add_refuel", clear_on_submit=True):
            r1, r2, r3 = st.columns(3)
            f_day = r1.date_input("Data tankowania", value=date.today())
            _dk = stats.FUEL_KINDS.get(fuel_manual.last_kind(), kinds_labels[0])
            f_kind = r2.selectbox("Paliwo", kinds_labels, index=kinds_labels.index(_dk))
            f_station = r3.text_input("Stacja (opcjonalnie)")
            r4, r5, r6 = st.columns(3)
            f_liters = r4.number_input("Litry", min_value=0.0, step=0.5, format="%.2f")
            f_price = r5.number_input("Cena za litr (zł)", min_value=0.0, step=0.01, format="%.2f")
            f_total = r6.number_input("Kwota razem (opcjonalnie)", min_value=0.0, step=0.01, format="%.2f",
                                      help="Zostaw 0, a kwota wyniesie litry × cena za litr. Wpisz, jeśli paragon pokazuje inną.")
            if st.form_submit_button("Dodaj tankowanie"):
                try:
                    _ok, _amount = fuel_manual.add_refuel(f_day, f_liters, f_price, f_total, kind_by_label[f_kind], f_station)
                    st.session_state["flash"] = f"Dodano tankowanie: {fmt2(_amount)} ({f_liters:g} l)."
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    if fb.empty:
        st.info("Brak tankowań. Dodaj pierwsze powyżej, włącz powiadomienia ORLEN Pay (FUEL_SENDER w .env) albo wgraj "
                "faktury w zakładce „Wgraj PDF”.")
    else:
        presets = {"Ostatni miesiąc": {"mode": "months", "n": 1}, "Ostatnie 2 miesiące": {"mode": "months", "n": 2},
                   "Ostatnie 3 miesiące": {"mode": "months", "n": 3}, "Ostatnie 6 miesięcy": {"mode": "months", "n": 6},
                   "Ostatnie 12 miesięcy": {"mode": "months", "n": 12}, "Ostatnie 30 dni": {"mode": "days", "n": 30}}
        years = sorted({int(y) for y in fb["issue_date"].dt.year}, reverse=True)
        options = list(presets) + ["Od wybranej daty"] + [f"Rok {y}" for y in years]

        def _save_fuel_cfg():
            label = st.session_state["fuel_period_sel"]
            if label in presets:
                cfg = presets[label]
            elif label == "Od wybranej daty":
                since = st.session_state.get("fuel_since") or (date.today() - timedelta(days=30))
                cfg = {"mode": "since", "since": str(since)}
            else:
                cfg = {"mode": "year", "year": int(label.split()[1])}
            db.set_setting("fuel_period", cfg)

        cur_label = stats.fuel_window_label(fuel_cfg)
        st.selectbox("Okres statystyk paliwa", options, index=options.index(cur_label) if cur_label in options else 4,
                     key="fuel_period_sel", on_change=_save_fuel_cfg,
                     help="To samo ustawienie określa średnią wydatków na paliwo w „Statystykach” i „Subskrypcjach”. "
                          "Jest zapamiętywane.")
        if fuel_cfg.get("mode") == "since":
            st.date_input("Od dnia", value=pd.Timestamp(fuel_cfg["since"]).date(), key="fuel_since", on_change=_save_fuel_cfg)

        if not fuel_now or fuel_now["avg_month"] is None:
            st.info("W wybranym okresie nie ma jeszcze danych (okno zaczyna się przed pierwszym zakupem lub jest puste).")
        else:
            st.caption(f"Okres: {fuel_now['start']:%Y-%m-%d} – {fuel_now['end']:%Y-%m-%d}; do średniej liczone jest "
                       f"{fuel_now['days']} dni (od {fuel_now['eff_start']:%Y-%m-%d}, bo pierwszy zakup to "
                       f"{fuel_now['first']:%Y-%m-%d}). Średnia miesięczna = koszt ÷ dni × 30,44.")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Koszt paliwa", fmt2(fuel_now["cost"]))
            k2.metric("Tankowań", fuel_now["refuels"])
            k3.metric("Średnio / tankowanie", fmt2(fuel_now["avg_refuel"]))
            k4.metric("Średnio / miesiąc", fmt2(fuel_now["avg_month"]))
            if fuel_now["liters"]:
                l1, l2 = st.columns(2)
                l1.metric("Litry", f"{fuel_now['liters']:,.0f} l".replace(",", " "))
                l2.metric("Średnia cena za litr", f"{fuel_now['price_per_l']:.2f} zł".replace(".", ","))
            with db.conn() as fc_:
                kinds = stats.fuel_by_kind_in(fc_, bills, fuel_now)
            if not kinds.empty:
                st.subheader("Wg rodzaju paliwa")
                kk = kinds.rename(index=lambda k: stats.FUEL_KINDS.get(k, k))
                st.dataframe(pd.DataFrame({"Litry": kk["liters"].round(1), "Koszt (zł)": kk["cost"].round(2)}))

            st.subheader("Rok do roku")
            prev = stats.fuel_stats_prev_year(bills, fuel_start, fuel_end)
            if not prev or not prev["days"]:
                st.info("Za mało danych do porównania: sprzed roku nie ma jeszcze zakupów w tym samym oknie.")
            else:
                chg = lambda a, b: round((a / b - 1) * 100, 1) if b else None  # noqa: E731
                st.dataframe(pd.DataFrame({
                    "Teraz": [round(fuel_now["avg_month"], 2), fuel_now["refuels"], round(fuel_now["liters"], 1)],
                    f"Rok wcześniej ({prev['start']:%Y-%m-%d} – {prev['end']:%Y-%m-%d})": [
                        round(prev["avg_month"], 2), prev["refuels"], round(prev["liters"], 1)],
                    "Zmiana (%)": [chg(fuel_now["avg_month"], prev["avg_month"]), chg(fuel_now["refuels"], prev["refuels"]),
                                   chg(fuel_now["liters"], prev["liters"])]},
                    index=["Średnio / miesiąc (zł)", "Tankowań", "Litry"]))

            st.subheader("Koszt miesięczny (zł)")
            mm = stats.fuel_monthly_in(fuel_now)
            mm.index = mm.index.to_timestamp()
            st.bar_chart(mm[["cost"]].rename(columns={"cost": "Paliwo (zł)"}))
            st.caption("Pierwszy i ostatni miesiąc okna mogą być niepełne.")

            st.subheader("Tankowania w okresie")
            shown = fuel_now["bills"].sort_values("issue_date", ascending=False)
            st.dataframe(pd.DataFrame({
                "Data": shown["issue_date"].dt.strftime("%Y-%m-%d"), "Opis": shown["note"],
                "Kwota (zł)": shown["amount_gross"], "Litry": shown["consumption"].where(shown["consumption"] > 0),
                "Cena za litr (zł)": (shown["amount_gross"] / shown["consumption"]).where(shown["consumption"] > 0).round(3),
                "Faktura": shown["invoice_no"].fillna("ręcznie")}), hide_index=True)

with t_subs:
    st.write("Aktualne subskrypcje i inne stałe opłaty (np. Spotify, HBO, abonament telefoniczny). "
             "Płatności roczne i kwartalne są przeliczane na miesiąc.")
    cyc_labels = [subscriptions.CYCLES[k] for k in subscriptions.SUB_CYCLES]
    cyc_by_label = {v: k for k, v in subscriptions.CYCLES.items()}
    k1, k2, k3 = st.columns(3)
    k1.metric("Subskrypcje / miesiąc", fmt2(sub_sum["monthly"]))
    k2.metric("Subskrypcje / rok", fmt(sub_sum["yearly"]))
    k3.metric("Aktywnych", sub_sum["active"],
              delta=f"{sub_sum['paused']} wstrzymanych" if sub_sum["paused"] else None, delta_color="off")

    with st.form("add_sub", clear_on_submit=True):
        f1, f2 = st.columns(2)
        s_name = f1.text_input("Nazwa (np. Spotify)")
        s_amount = f2.number_input("Kwota subskrypcji (zł)", min_value=0.0, step=1.0, format="%.2f")
        f3, f4 = st.columns(2)
        s_cycle = f3.selectbox("Płatność", cyc_labels)
        s_next = f4.date_input("Data jednej z płatności (opcjonalnie)", value=None,
                               help="Na tej podstawie program liczy najbliższą płatność.")
        s_note = st.text_input("Notatka (opcjonalnie)")
        if st.form_submit_button("Dodaj subskrypcję"):
            try:
                subscriptions.add(s_name, s_amount, cyc_by_label[s_cycle], s_next, s_note)
                st.session_state["flash"] = f"Dodano subskrypcję: {s_name.strip()}."
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    if not subs.empty:
        st.subheader("Lista")
        table = pd.DataFrame({
            "Usuń": False, "Aktywna": subs["active"], "Nazwa": subs["name"], "Kwota (zł)": subs["amount"],
            "Płatność": subs["cycle"].map(subscriptions.CYCLES),
            "Następna płatność": subs["next_date"].apply(lambda d: d.date() if pd.notna(d) else None),
            "Na miesiąc (zł)": [round(subscriptions.monthly_equiv(a, c), 2) for a, c in zip(subs["amount"], subs["cycle"])],
            "Notatka": subs["note"].fillna("")})
        edited = st.data_editor(
            table, hide_index=True, key="subs_editor", disabled=["Na miesiąc (zł)"],
            column_config={
                "Usuń": st.column_config.CheckboxColumn("Usuń"),
                "Aktywna": st.column_config.CheckboxColumn("Aktywna"),
                "Kwota (zł)": st.column_config.NumberColumn("Kwota (zł)", min_value=0.0, step=0.01, format="%.2f"),
                "Płatność": st.column_config.SelectboxColumn("Płatność", options=cyc_labels, required=True),
                "Następna płatność": st.column_config.DateColumn("Następna płatność")})
        st.caption("Odznacz „Aktywna”, żeby wstrzymać subskrypcję bez usuwania (nie wlicza się do sum).")
        if st.button("Zapisz zmiany", key="save_subs"):
            errors, changed = [], 0
            for sid, (_, old), new in zip(subs["id"], table.iterrows(), edited.to_dict("records")):
                if new["Usuń"]:
                    subscriptions.delete(int(sid))
                    changed += 1
                    continue
                cols = ["Aktywna", "Nazwa", "Kwota (zł)", "Płatność", "Następna płatność", "Notatka"]
                if all(clean(old[c]) == clean(new[c]) for c in cols):
                    continue
                try:
                    subscriptions.update(int(sid), name=new["Nazwa"], amount=new["Kwota (zł)"],
                                         cycle=cyc_by_label[new["Płatność"]], next_date=new["Następna płatność"],
                                         active=new["Aktywna"], note=new["Notatka"])
                    changed += 1
                except ValueError as e:
                    errors.append(f"{new['Nazwa'] or '(bez nazwy)'}: {e}")
            if errors:
                st.error("; ".join(errors))
            st.session_state["flash"] = f"Zapisano zmian: {changed}."
            if not errors:
                st.rerun()

    st.subheader("Stałe wydatki razem")
    fuel_avg = float(sav.loc[sav["utility"] == "paliwo", "per_month"].iloc[0]) if sav is not None else 0.0
    with_fuel = st.checkbox("Wlicz średnie miesięczne wydatki na paliwo", value=False, key="subs_with_fuel")
    parts = [("Subskrypcje", sub_sum["monthly"]), ("Koszty roczne i okresowe (rata miesięczna)", per_sum["monthly"]),
             ("Media (średnia z ostatnich 12 pełnych miesięcy)", suggested)]
    if with_fuel:
        parts.append((f"Paliwo (średnia: {stats.fuel_window_label(fuel_cfg).lower()})", fuel_avg))
    grand = sum(v for _, v in parts)
    st.dataframe(pd.DataFrame({
        "Pozycja": [n for n, _ in parts] + ["Razem"],
        "Miesięcznie (zł)": [round(v, 2) for _, v in parts] + [round(grand, 2)],
        "Rocznie (zł)": [round(v * 12, 2) for _, v in parts] + [round(grand * 12, 2)]}), hide_index=True)
    st.metric("Razem stałe wydatki / miesiąc", fmt2(grand))

    up = subscriptions.upcoming(subs, days=30)
    if not up.empty:
        st.subheader("Najbliższe płatności (30 dni)")
        st.dataframe(pd.DataFrame({"Data": up["date"].dt.strftime("%Y-%m-%d"), "Subskrypcja": up["name"],
                                   "Kwota (zł)": up["amount"]}), hide_index=True)
        st.caption(f"Razem w ciągu 30 dni: {fmt2(float(up['amount'].sum()))}")

with t_periodic:
    st.write("Koszty, które wracają rzadziej niż co miesiąc: OC, przegląd, ubezpieczenia, podatki, opony. Wpisujesz "
             "**jedną wspólną kwotę**, którą już odłożyłeś na takie wydatki, a program sprawdza każdy termin płatności "
             "i wylicza, ile odkładać miesięcznie, żeby w dniu każdej płatności wystarczyło pieniędzy.")
    pcyc_labels = [subscriptions.CYCLES[k] for k in subscriptions.PERIODIC_CYCLES]
    pcyc_by_label = {v: k for k, v in subscriptions.CYCLES.items()}
    pool_cfg = db.get_setting("periodic_pool", {"amount": 0.0, "as_of": None})
    buffer_cfg = float(db.get_setting("periodic_buffer", 0.0))
    horizon_cfg = int(db.get_setting("periodic_horizon", 12))
    horizons = [6, 12, 18, 24, 36]

    with st.form("periodic_settings"):
        g1, g2, g3 = st.columns(3)
        in_pool = g1.number_input("Odłożono na koszty roczne (zł)", min_value=0.0, step=50.0, format="%.2f",
                                  value=float(pool_cfg["amount"]),
                                  help="Jedna wspólna kwota: całe odłożone na te wydatki. Zaktualizuj ją od czasu do czasu, "
                                       "np. po zapłaceniu kosztu lub po większej wpłacie.")
        in_buffer = g2.number_input("Minimalny zapas (zł)", min_value=0.0, step=50.0, format="%.2f", value=buffer_cfg,
                                    help="Ile ma zostać w funduszu po każdej płatności (poduszka bezpieczeństwa).")
        in_horizon = g3.selectbox("Planuj na", horizons, index=horizons.index(horizon_cfg) if horizon_cfg in horizons else 1,
                                  format_func=lambda n: f"{n} miesięcy")
        if st.form_submit_button("Zapisz ustawienia planu"):
            db.set_setting("periodic_pool", {"amount": float(in_pool), "as_of": date.today().isoformat()})
            db.set_setting("periodic_buffer", float(in_buffer))
            db.set_setting("periodic_horizon", int(in_horizon))
            st.session_state["flash"] = "Zapisano ustawienia planu."
            st.rerun()

    pool = float(pool_cfg["amount"])
    plan = subscriptions.fund_plan(periodic, pool, buffer_cfg, horizon_months=horizon_cfg)
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Odłożono na koszty roczne", fmt2(pool),
              help=f"Stan wpisany dnia {pool_cfg['as_of']}." if pool_cfg.get("as_of") else "Wpisz kwotę w ustawieniach planu.")
    p2.metric("Powinno być odłożone dziś", fmt(per_sum["target_now"]),
              help="Ile zgromadziłbyś, odkładając równo od poprzedniej płatności każdego kosztu.")
    p3.metric("Różnica", f"{pool - per_sum['target_now']:+,.0f} zł".replace(",", " "),
              help="Plus: masz więcej niż przy równym odkładaniu, minus: brakuje.")
    p4.metric("Rata miesięczna (średnio)", fmt2(per_sum["monthly"]),
              help="Suma rat: kwota podzielona przez długość cyklu (np. OC 1200 zł rocznie = 100 zł miesięcznie). "
                   "To docelowa średnia, gdy fundusz jest już zasilony.")
    q1, q2, q3 = st.columns(3)
    q1.metric(f"Odkładaj miesięcznie (plan na {horizon_cfg} mies.)", fmt2(plan["monthly"]),
              help="Najniższa stała wpłata miesięczna, przy której w dniu każdej płatności z najbliższych miesięcy "
                   "wystarczy środków (odłożone + wpłaty) na pokrycie wszystkich płatności do tego dnia plus zapas.")
    q2.metric(f"Do zapłacenia w {horizon_cfg} mies.", fmt(plan["total_due"]))
    q3.metric("Pokrycie z odłożonych", f"{min(100.0, pool / plan['total_due'] * 100):.0f}%" if plan["total_due"] else "-")
    if plan["binding"] is not None:
        bd = plan["binding"]
        st.caption(f"Najtrudniejszy termin: {bd['date']:%Y-%m-%d} ({bd['name']}): do tego dnia trzeba mieć na płatności "
                   f"łącznie {fmt(bd['cum'])}. Wyliczenie zakłada równe wpłaty co miesiąc od dziś, a najbliższy termin "
                   "liczy najwyżej jako 1 miesiąc. Wpłata jest wyższa niż średnia rata, gdy płatności wypadają blisko "
                   "siebie albo odłożono mało.")
    elif plan["total_due"]:
        st.success("Odłożona kwota wystarcza na wszystkie płatności w wybranym okresie, bez dalszych wpłat.")

    with st.form("add_periodic", clear_on_submit=True):
        q1_, q2_ = st.columns(2)
        pr_name = q1_.text_input("Nazwa (np. OC samochodu)")
        pr_amount = q2_.number_input("Kwota jednej płatności (zł)", min_value=0.0, step=10.0, format="%.2f")
        q3_, q4_ = st.columns(2)
        pr_cycle = q3_.selectbox("Jak często", pcyc_labels, index=pcyc_labels.index("rocznie"))
        pr_next = q4_.date_input("Najbliższy termin płatności", value=None,
                                 help="Bez daty program nie policzy planu. Kolejne terminy liczy sam.")
        pr_note = st.text_input("Notatka (opcjonalnie)")
        if st.form_submit_button("Dodaj koszt"):
            try:
                subscriptions.add(pr_name, pr_amount, pcyc_by_label[pr_cycle], pr_next, pr_note, category="periodic")
                st.session_state["flash"] = f"Dodano koszt okresowy: {pr_name.strip()}."
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    if not periodic.empty:
        st.subheader("Harmonogram płatności")
        sch = plan["schedule"]
        if sch.empty:
            st.info("Brak płatności w wybranym okresie (sprawdź, czy koszty mają datę najbliższego terminu).")
        else:
            st.dataframe(pd.DataFrame({
                "Data": sch["date"].dt.strftime("%Y-%m-%d"), "Koszt": sch["name"], "Kwota (zł)": sch["amount"],
                "Suma płatności do tego dnia (zł)": sch["cum"].round(2),
                "Dostępne środki (zł)": sch["funds"].round(2), "Po płatności zostanie (zł)": sch["after"].round(2)}),
                hide_index=True)
            st.caption("Dostępne środki = odłożone + wpłaty miesięczne do tego dnia. Wiersz z zerem (lub z zapasem) to termin, "
                       "który wyznacza wysokość miesięcznej wpłaty.")

        st.subheader("Rozkład na poszczególne koszty")
        plan_t = per_table.copy()
        st.dataframe(pd.DataFrame({
            "Koszt": plan_t["name"], "Następny termin": plan_t["next_due"].dt.strftime("%Y-%m-%d"),
            "Dni do terminu": plan_t["days_left"], "Kwota (zł)": plan_t["amount"],
            "Rata miesięczna (zł)": plan_t["monthly"].round(2),
            "Miesięcznie do terminu (zł)": plan_t["to_due"].round(2),
            "Powinno być odłożone dziś (zł)": plan_t["target_now"].round(2)}), hide_index=True)
        st.caption("„Rata miesięczna” to kwota podzielona przez długość cyklu. „Miesięcznie do terminu” to kwota podzielona "
                   "przez liczbę miesięcy do terminu, gdyby odkładać osobno na ten koszt od zera. Plan wspólny powyżej "
                   "jest łagodniejszy, bo uwzględnia to, co już odłożono, i wspólny fundusz.")

        st.subheader("Lista")
        full = subscriptions.periodic_table(periodic.assign(active=True)).set_index("id")
        ptable = pd.DataFrame({
            "Usuń": False, "Aktywna": periodic["active"], "Nazwa": periodic["name"], "Kwota (zł)": periodic["amount"],
            "Jak często": periodic["cycle"].map(subscriptions.CYCLES),
            "Następny termin": periodic["next_date"].apply(lambda d: d.date() if pd.notna(d) else None),
            "Notatka": periodic["note"].fillna(""),
            "Na miesiąc (zł)": [round(full.loc[i, "monthly"], 2) for i in periodic["id"]],
            "Dni do terminu": [full.loc[i, "days_left"] for i in periodic["id"]]})
        pedit = st.data_editor(
            ptable, hide_index=True, key="periodic_editor", disabled=["Na miesiąc (zł)", "Dni do terminu"],
            column_config={
                "Usuń": st.column_config.CheckboxColumn("Usuń"), "Aktywna": st.column_config.CheckboxColumn("Aktywna"),
                "Kwota (zł)": st.column_config.NumberColumn("Kwota (zł)", min_value=0.0, step=1.0, format="%.2f"),
                "Jak często": st.column_config.SelectboxColumn("Jak często", options=pcyc_labels, required=True),
                "Następny termin": st.column_config.DateColumn("Następny termin")})
        st.caption("Po płatności nie trzeba zmieniać daty: kolejne terminy program liczy sam od podanej daty co cykl. "
                   "Odznacz „Aktywna”, żeby wstrzymać koszt bez usuwania.")
        if st.button("Zapisz zmiany", key="save_periodic"):
            errors, changed = [], 0
            cols = ["Aktywna", "Nazwa", "Kwota (zł)", "Jak często", "Następny termin", "Notatka"]
            for sid, (_, old), new in zip(periodic["id"], ptable.iterrows(), pedit.to_dict("records")):
                if new["Usuń"]:
                    subscriptions.delete(int(sid))
                    changed += 1
                    continue
                if all(clean(old[c]) == clean(new[c]) for c in cols):
                    continue
                try:
                    subscriptions.update(int(sid), name=new["Nazwa"], amount=new["Kwota (zł)"],
                                         cycle=pcyc_by_label[new["Jak często"]], next_date=new["Następny termin"],
                                         active=new["Aktywna"], note=new["Notatka"])
                    changed += 1
                except ValueError as e:
                    errors.append(f"{new['Nazwa'] or '(bez nazwy)'}: {e}")
            if errors:
                st.error("; ".join(errors))
            st.session_state["flash"] = f"Zapisano zmian: {changed}."
            if not errors:
                st.rerun()

with t_month:
    st.write("Wszystkie koszty miesiąca w jednym miejscu: faktury za media (a gdy faktury jeszcze nie ma, szacunek ze średniej), "
             "subskrypcje, koszty okresowe i własne pozycje. Zaznacz „Zapłacone” po opłaceniu i zapisz.")
    cur_m = pd.Period(pd.Timestamp.today(), "M")
    month_opts = {f"{m}" + (" (bieżący)" if m == cur_m else ""): m for m in pd.period_range(cur_m - 6, cur_m + 2, freq="M")}
    month = month_opts[st.selectbox("Miesiąc", list(month_opts), index=list(month_opts.values()).index(cur_m), key="month_sel")]
    estimates = {u: float(sav.loc[sav["utility"] == u, "per_month"].iloc[0]) for u in ("prad", "gaz", "woda")} \
        if sav is not None else {}
    track_start = checklist.tracking_start()
    mrows = checklist.build(month, bills, subs_all, estimates)
    mt = checklist.totals(mrows)
    c1, c2, c3 = st.columns(3)
    c1.metric("Razem w miesiącu", fmt2(mt["total"]), help=f"{mt['n']} pozycji")
    c2.metric("Zapłacone", fmt2(mt["paid"]), help=f"{mt['n_paid']} pozycji")
    c3.metric("Zostało do zapłacenia", fmt2(mt["left"]), help=f"{mt['n_left']} pozycji")
    st.progress(min(1.0, mt["paid"] / mt["total"]) if mt["total"] else 0.0,
                text=f"Zapłacono {mt['n_paid']} z {mt['n']} pozycji")

    unpaid = checklist.unpaid_previous(month, bills, subs_all, estimates, start=track_start)
    if not unpaid.empty:
        st.warning(f"Niezapłacone z poprzednich miesięcy: {len(unpaid)} pozycji na {fmt2(float(unpaid['amount'].sum()))}. "
                   "Przełącz miesiąc, żeby je zaznaczyć jako zapłacone.")
        with st.expander("Pokaż niezapłacone z poprzednich miesięcy"):
            st.dataframe(pd.DataFrame({"Miesiąc": unpaid["month"], "Pozycja": unpaid["name"],
                                       "Kwota (zł)": unpaid["amount"], "Uwagi": unpaid["detail"]}), hide_index=True)

    if mrows.empty:
        st.info("Brak pozycji w tym miesiącu. Dodaj własną poniżej albo dodaj subskrypcje i koszty okresowe.")
    else:
        mview = pd.DataFrame({
            "Zapłacone": mrows["paid"], "Pozycja": mrows["name"], "Typ": mrows["kind"], "Kwota (zł)": mrows["amount"],
            "Data": mrows["due"].apply(lambda d: d.date() if pd.notna(d) else None), "Uwagi": mrows["detail"],
            "Zapłacono dnia": mrows["paid_date"].fillna(""), "Usuń": False})
        medit = st.data_editor(
            mview, hide_index=True, key=f"month_editor_{month}",
            disabled=["Pozycja", "Typ", "Kwota (zł)", "Data", "Uwagi", "Zapłacono dnia"],
            column_config={"Zapłacone": st.column_config.CheckboxColumn("Zapłacone"),
                           "Kwota (zł)": st.column_config.NumberColumn("Kwota (zł)", format="%.2f"),
                           "Data": st.column_config.DateColumn("Data", format="YYYY-MM-DD"),
                           "Usuń": st.column_config.CheckboxColumn("Usuń", help="Dotyczy tylko własnych pozycji.")})
        st.caption("Szacunek to średni miesięczny koszt z faktur: zastąpi go faktura po pobraniu. Przy fakturze za gaz lub "
                   "prąd „po rozliczeniu: nadpłata” oznacza, że u dostawcy jest nadpłata i nic nie trzeba dopłacać "
                   "(takie faktury nie trafiają do zaległych).")
        if st.button("Zapisz zaznaczenia", key="save_month", type="primary"):
            changed = 0
            for key, was, new in zip(mrows["key"], mrows["paid"], medit.to_dict("records")):
                if new["Usuń"] and key.startswith("man:"):
                    checklist.delete_manual(int(key.split(":")[1]))
                    changed += 1
                elif bool(new["Zapłacone"]) != bool(was):
                    checklist.set_paid(month, key, bool(new["Zapłacone"]))
                    changed += 1
            st.session_state["flash"] = f"Zapisano zmian: {changed}."
            st.rerun()

    with st.expander("Dodaj własną pozycję do tego miesiąca (np. czynsz, rata, telefon)"):
        with st.form(f"add_manual_{month}", clear_on_submit=True):
            mm1, mm2, mm3 = st.columns(3)
            mi_name = mm1.text_input("Nazwa")
            mi_amount = mm2.number_input("Kwota (zł)", min_value=0.0, step=10.0, format="%.2f")
            mi_note = mm3.text_input("Notatka (opcjonalnie)")
            if st.form_submit_button("Dodaj pozycję"):
                try:
                    checklist.add_manual(month, mi_name, mi_amount, mi_note)
                    st.session_state["flash"] = f"Dodano pozycję: {mi_name.strip()}."
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        st.caption("Pozycje stałe co miesiąc dodaj lepiej w zakładce „Subskrypcje”: będą się pojawiać same.")

    with st.expander("Od kiedy liczyć zaległości"):
        st.write(f"Zaległości (niezapłacone z poprzednich miesięcy) liczą się od miesiąca **{track_start}**. "
                 "Starsze faktury nie są wtedy uznawane za niezapłacone.")
        new_start = st.selectbox("Liczyć od miesiąca", list(pd.period_range(cur_m - 12, cur_m, freq="M"))[::-1],
                                 format_func=str, key="track_start_sel")
        if st.button("Zapisz", key="save_track_start"):
            checklist.set_tracking_start(new_start)
            st.session_state["flash"] = f"Zaległości będą liczone od {new_start}."
            st.rerun()

with t_files:
    if bills.empty:
        st.info("Brak faktur.")
    else:
        f1, f2 = st.columns(2)
        util_names = {"Wszystkie": "all", **{v: k for k, v in LABEL.items()}}
        util_f = util_names[f1.selectbox("Medium", list(util_names))]
        year_opts = sorted({int(y) for y in bills["issue_date"].dt.year.dropna()}, reverse=True)
        year_f = f2.selectbox("Rok", ["Wszystkie"] + [str(y) for y in year_opts])
        view = bills.copy()
        if util_f != "all":
            view = view[view["utility"] == util_f]
        if year_f != "Wszystkie":
            view = view[view["issue_date"].dt.year == int(year_f)]
        view = view.sort_values("issue_date", ascending=False, na_position="last")
        if view.empty:
            st.info("Brak faktur dla tego filtra.")
        else:
            def label(r):
                d = "bez daty" if pd.isna(r.issue_date) else f"{r.issue_date:%Y-%m-%d}"
                return (f"{d} · {LABEL[r.utility]} · {txt(r.invoice_no) or 'bez numeru'} · "
                        f"{fmt2(r.amount_gross)} · #{r.id}")
            labels = {label(r): int(r.id) for r in view.itertuples()}
            bid = labels[st.selectbox("Faktura", list(labels))]
            row = view[view["id"] == bid].iloc[0]
            pdf = safe_pdf(row["pdf_path"])
            if pdf:
                st.download_button("Pobierz PDF", data=pdf.read_bytes(), file_name=pdf.name,
                                   mime="application/pdf", type="primary")
            else:
                st.warning("Do tej faktury nie ma pliku PDF. Możesz go dołączyć poniżej.")
                up = st.file_uploader("Dołącz PDF", type="pdf", key=f"attach{bid}")
                if up and st.button("Zapisz PDF", key=f"save{bid}"):
                    data = up.getvalue()
                    if not data.startswith(b"%PDF"):
                        st.error("To nie jest plik PDF.")
                    else:
                        base = re.sub(r"[^A-Za-z0-9_-]+", "_", txt(row["invoice_no"]) or f"id{bid}")
                        target = PDF_DIR / row["utility"] / f"{base}.pdf"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        db.set_pdf(bid, target)
                        st.session_state["flash"] = "PDF dołączony do faktury."
                        st.rerun()

with t_upload:
    st.write("Przeciągnij pobrane z eBOK pliki PDF (Enea, PGNiG, wodociągi). Program rozpozna dostawcę, "
             "odczyta dane i zapisze fakturę razem z plikiem. Czego nie odczyta, uzupełnisz w zakładce „Lista”.")
    waiting = bills[bills["pdf_path"].isna() & bills["source"].fillna("").str.startswith("mail-")]
    if not waiting.empty:
        st.info("Faktury z powiadomień mailowych, które czekają na PDF: " + ", ".join(
            f"{r.invoice_no} ({r.issue_date:%Y-%m-%d})" if pd.notna(r.issue_date) else str(r.invoice_no)
            for r in waiting.itertuples()) + ". Wgraj ich PDF-y poniżej: dopasują się po numerze.")
    hint_names = {v: k for k, v in LABEL.items()}
    hint = hint_names[st.selectbox("Medium, jeśli dostawca nie zostanie rozpoznany", list(hint_names))]
    uploads = st.file_uploader("Pliki PDF", type="pdf", accept_multiple_files=True, key="pdf_batch")
    if uploads and st.button("Przetwórz pliki", type="primary"):
        st.session_state["upload_rows"] = [
            invoices.ingest(f.getvalue(), f.name, hint, PDF_DIR) for f in uploads]
        st.rerun()
    rep_choice = st.selectbox("Które faktury przeliczyć", ["Wszystkie"] + list(hint_names), key="reparse_choice")
    if st.button("Przelicz faktury z zapisanych PDF-ów",
                 help="Ponownie odczytuje zapisane PDF-y: odtwarza pozycje kosztów, stan konta, kwoty i okresy. "
                      "Przydaje się, gdy faktury wgrano starszą wersją programu. Każdy PDF to ok. sekundy, "
                      "więc przy kilkudziesięciu fakturach trwa to chwilę."):
        bar = st.progress(0.0, text="Przeliczanie...")
        st.session_state["upload_rows"] = invoices.reparse_all(
            None if rep_choice == "Wszystkie" else hint_names[rep_choice],
            progress=lambda i, n, row: bar.progress(i / n, text=f"{i} z {n} faktur ({row['Numer']})"))
        st.rerun()
    if "upload_rows" in st.session_state:
        st.subheader("Wynik")
        st.dataframe(pd.DataFrame(st.session_state.pop("upload_rows")), hide_index=True)
        st.caption("Zaktualizowane statystyki zobaczysz po przełączeniu zakładki lub odświeżeniu strony.")

with t_add:
    with st.form("add"):
        u = st.selectbox("Medium", list(LABEL), format_func=LABEL.get)
        provider = st.text_input("Dostawca (puste = domyślny)")
        inv = st.text_input("Numer faktury")
        issue = st.date_input("Data wystawienia", value=date.today())
        c1, c2 = st.columns(2)
        ps = c1.date_input("Okres od", value=None)
        pe = c2.date_input("Okres do", value=None)
        amount = st.number_input("Kwota brutto (zł)", min_value=0.0, step=1.0, format="%.2f")
        cons = st.number_input("Zużycie (opcjonalnie)", min_value=0.0, step=1.0, format="%.2f")
        unit = st.selectbox("Jednostka zużycia", ["", "kWh", "m3", "l"])
        if st.form_submit_button("Zapisz"):
            ps_v = ps or (issue if u == "paliwo" else None)   # paliwo to zakup jednego dnia
            pe_v = pe or (issue if u == "paliwo" else None)
            ok = db.add_bill(
                provider=provider or DEFAULT_PROVIDER[u], utility=u, invoice_no=inv,
                issue_date=issue.isoformat(),
                period_start=ps_v.isoformat() if ps_v else None,
                period_end=pe_v.isoformat() if pe_v else None,
                amount_gross=amount, consumption=cons or None, unit=unit, source="manual")
            st.session_state["flash"] = "Dodano fakturę." if ok else \
                "Faktura o tym numerze i dostawcy już istnieje."
            st.rerun()

with t_csv:
    st.write("Kolumny: `utility` (prad/gaz/woda), `provider`, `invoice_no`, `issue_date`, "
             "`period_start`, `period_end` (daty RRRR-MM-DD), `amount_gross`, `consumption`, `unit`. "
             "Wymagane tylko `utility`.")
    up = st.file_uploader("Plik CSV", type="csv")
    if up and st.button("Importuj"):
        df = pd.read_csv(up, dtype=str)
        added = skipped = 0
        for rec in df.to_dict("records"):
            rec = {k: clean(v) for k, v in rec.items()}
            if rec.get("utility") not in LABEL:
                skipped += 1
                continue
            rec["provider"] = rec.get("provider") or DEFAULT_PROVIDER[rec["utility"]]
            for k in ("amount_gross", "consumption"):
                if rec.get(k) is not None:
                    rec[k] = float(str(rec[k]).replace(",", "."))
            ok = db.add_bill(**{k: v for k, v in rec.items() if k in db.FIELDS}, source="csv")
            added += ok
            skipped += not ok
        st.session_state["flash"] = f"Zaimportowano {added}, pominięto {skipped}."
        st.rerun()

with t_list:
    if bills.empty:
        st.info("Pusto.")
    else:
        with db.conn() as c:
            raw = pd.read_sql_query(
                "SELECT * FROM bills ORDER BY COALESCE(period_end, issue_date) DESC, id DESC", c)
        edited = st.data_editor(
            raw, hide_index=True, key="editor",
            disabled=["id", "status", "source", "pdf_path", "created_at", "account_balance"],
            column_config={"utility": st.column_config.SelectboxColumn(
                "utility", options=list(LABEL), required=True)})
        st.caption("Daty w formacie RRRR-MM-DD. Status przelicza się sam przy zapisie.")
        if st.button("Zapisz zmiany"):
            errors, changed = [], 0
            for old, new in zip(raw.to_dict("records"), edited.to_dict("records")):
                if all(clean(old[k]) == clean(new[k]) for k in EDIT_COLS):
                    continue
                vals = {k: clean(new[k]) for k in EDIT_COLS}
                bad = [k for k in ("issue_date", "period_start", "period_end")
                       if vals[k] and pd.isna(pd.to_datetime(vals[k], errors="coerce"))]
                if bad:
                    errors.append(f"ID {new['id']}: zła data w {', '.join(bad)}")
                    continue
                try:
                    db.update_bill(new["id"], **vals)
                    changed += 1
                except db.sqlite3.IntegrityError:
                    errors.append(f"ID {new['id']}: taki numer faktury już istnieje")
            if errors:
                st.error("; ".join(errors))
            st.session_state["flash"] = f"Zapisano zmian: {changed}."
            if not errors:
                st.rerun()
        d1, d2 = st.columns([1, 3])
        del_id = d1.number_input("Usuń fakturę o ID", min_value=0, step=1)
        if d2.button("Usuń") and del_id:
            db.delete_bill(int(del_id))
            st.session_state["flash"] = f"Usunięto ID {int(del_id)}."
            st.rerun()
