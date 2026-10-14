import hmac
import os
import re
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from app import db, invoices, stats, subscriptions
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
subs = subscriptions.load()
sub_sum = subscriptions.summary(subs)
water_bills = bills[bills["utility"] == "woda"]
water_done = stats.complete_months(water_bills) if not water_bills.empty else []

sav = stats.suggested_savings(bills) if not bills.empty else None
suggested = float(sav[sav["utility"] != "paliwo"]["per_month"].sum()) if sav is not None else 0.0

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

t_stats, t_water, t_gas, t_power, t_fuel, t_subs, t_files, t_upload, t_add, t_csv, t_list = st.tabs(
    ["Statystyki", "Woda", "Gaz", "Prąd", "Paliwo", "Subskrypcje", "Faktury PDF", "Wgraj PDF", "Dodaj fakturę",
     "Import CSV", "Lista"])

with t_stats:
    if bills.empty:
        st.info("Brak danych. Dodaj pierwszą fakturę albo zaimportuj CSV.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Odkładaj miesięcznie (media)", fmt(suggested), help="Prąd, gaz i woda; paliwo jest osobno.")
        m2.metric("Koszt z użytych miesięcy (media)",
                  fmt(float(sav[sav["utility"] != "paliwo"]["last_sum"].sum())))
        _fuel = sav[sav["utility"] == "paliwo"].iloc[0]
        m3.metric("Paliwo średnio / miesiąc", fmt(float(_fuel["per_month"])) if _fuel["months_used"] else "brak danych")
        m4.metric("Liczba faktur", len(bills))
        if not subs.empty:
            sb1, sb2 = st.columns(2)
            sb1.metric("Subskrypcje / miesiąc", fmt2(sub_sum["monthly"]))
            sb2.metric("Stałe wydatki (media + subskrypcje) / miesiąc", fmt2(suggested + sub_sum["monthly"]),
                       help="Media: średnia z ostatnich 12 pełnych miesięcy. Szczegóły w zakładce „Subskrypcje”.")

        table = pd.DataFrame({
            "Medium": sav["utility"].map(LABEL),
            "Miesięcy w średniej": sav["months_used"],
            "Średnio / miesiąc (zł)": sav["per_month"].round(2),
            "Suma z tych miesięcy (zł)": sav["last_sum"].round(2),
        })
        st.dataframe(table, hide_index=True)
        st.caption("Średnia z maks. 12 ostatnich miesięcy w pełni pokrytych fakturami. "
                   "Faktury są rozkładane na dni swojego okresu rozliczeniowego.")

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
    if fb.empty:
        st.info("Brak faktur za paliwo. Włącz powiadomienia ORLEN Pay (FUEL_SENDER w .env) albo wgraj faktury "
                "w zakładce „Wgraj PDF”.")
    else:
        fmonths = stats.fuel_months(bills)
        months = []
        if not fmonths:
            st.info("Pierwsze zakupy są z bieżącego miesiąca: statystyki okresowe pojawią się po jego zakończeniu.")
        else:
            years = sorted({m.year for m in fmonths}, reverse=True)
            choice = st.selectbox("Okres", ["Ostatnie 12 miesięcy"] + [f"Rok {y}" for y in years], key="period_fuel")
            months = fmonths[-12:] if choice.startswith("Ostatnie") else \
                [m for m in fmonths if m.year == int(choice.split()[1])]
            summ = stats.fuel_summary(bills, months)
            st.caption(f"Pełne miesiące w okresie: {len(months)} ({months[0]} – {months[-1]}). Miesiąc bez tankowania "
                       f"liczy się jako zero. Dane są od pierwszej faktury ({fb['issue_date'].min():%Y-%m-%d}).")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Koszt paliwa", fmt(summ["cost"]))
            k2.metric("Tankowań", summ["refuels"])
            k3.metric("Średnio / tankowanie", fmt(summ["avg_refuel"]))
            k4.metric("Średnio / miesiąc", fmt(summ["avg_month"]))
            if summ["liters"]:
                l1, l2 = st.columns(2)
                l1.metric("Litry", f"{summ['liters']:,.0f} l".replace(",", " "))
                l2.metric("Średnia cena za litr", f"{summ['price_per_l']:.2f} zł".replace(".", ","))
            with db.conn() as fc_:
                kinds = stats.fuel_by_kind(fc_, bills, months)
            if not kinds.empty:
                st.subheader("Wg rodzaju paliwa")
                kk = kinds.rename(index=lambda k: stats.FUEL_KINDS.get(k, k))
                st.dataframe(pd.DataFrame({"Litry": kk["liters"].round(1), "Koszt (zł)": kk["cost"].round(2)}))

            st.subheader("Rok do roku")
            cmp_, n_common = stats.fuel_compare(bills, months, fmonths)
            if not cmp_:
                st.info("Za mało danych do porównania: potrzebne są te same miesiące z poprzedniego roku.")
            else:
                st.caption(f"Porównanie na {n_common} wspólnych miesiącach (te same miesiące rok wcześniej).")
                now, before = cmp_["now"], cmp_["before"]
                chg = lambda a, b: round((a / b - 1) * 100, 1) if b else None  # noqa: E731
                st.dataframe(pd.DataFrame({
                    "Teraz": [round(now["cost"], 2), now["refuels"], round(now["liters"], 1)],
                    "Rok wcześniej": [round(before["cost"], 2), before["refuels"], round(before["liters"], 1)],
                    "Zmiana (%)": [chg(now["cost"], before["cost"]), chg(now["refuels"], before["refuels"]),
                                   chg(now["liters"], before["liters"])]},
                    index=["Koszt (zł)", "Tankowań", "Litry"]))

            st.subheader("Koszt miesięczny (zł)")
            mm = stats.fuel_monthly(bills, months)
            mm.index = mm.index.to_timestamp()
            st.bar_chart(mm[["cost"]].rename(columns={"cost": "Paliwo (zł)"}))

        st.subheader("Tankowania")
        shown = fb[fb["issue_date"].dt.to_period("M").isin(months)] if months else fb
        shown = shown.sort_values("issue_date", ascending=False)
        st.dataframe(pd.DataFrame({
            "Data": shown["issue_date"].dt.strftime("%Y-%m-%d"), "Stacja": shown["note"],
            "Kwota (zł)": shown["amount_gross"], "Litry": shown["consumption"].where(shown["consumption"] > 0),
            "Cena za litr (zł)": (shown["amount_gross"] / shown["consumption"]).where(shown["consumption"] > 0).round(3),
            "Numer faktury": shown["invoice_no"]}), hide_index=True)

with t_subs:
    st.write("Aktualne subskrypcje i inne stałe opłaty (np. Spotify, HBO, abonament telefoniczny). "
             "Płatności roczne i kwartalne są przeliczane na miesiąc.")
    cyc_labels = list(subscriptions.CYCLES.values())
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
    parts = [("Subskrypcje", sub_sum["monthly"]), ("Media (średnia z ostatnich 12 pełnych miesięcy)", suggested)]
    if with_fuel:
        parts.append(("Paliwo (średnia miesięczna)", fuel_avg))
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
    if st.button("Przelicz faktury z zapisanych PDF-ów",
                 help="Ponownie odczytuje wszystkie zapisane PDF-y: odtwarza pozycje kosztów, stan konta, kwoty i okresy. "
                      "Przydaje się, gdy faktury wgrano starszą wersją programu."):
        st.session_state["upload_rows"] = invoices.reparse_all()
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
