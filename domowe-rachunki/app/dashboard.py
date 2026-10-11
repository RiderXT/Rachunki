import hmac
import os
import re
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from app import db, invoices, stats
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
DEFAULT_PROVIDER = {"prad": "Enea", "gaz": "PGNiG", "woda": "Wodociągi"}
EDIT_COLS = ["provider", "utility", "invoice_no", "issue_date", "period_start",
             "period_end", "amount_gross", "consumption", "unit"]


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
water_bills = bills[bills["utility"] == "woda"]
water_done = stats.complete_months(water_bills) if not water_bills.empty else []
gas_bills = bills[bills["utility"] == "gaz"]
gas_done = stats.complete_months(gas_bills) if not gas_bills.empty else []
gas_daily = stats.usage_daily(gas_bills, "gaz")
gas_price = stats.unit_price_series(gas_bills, "gaz")

sav = stats.suggested_savings(bills) if not bills.empty else None
suggested = float(sav["per_month"].sum()) if sav is not None else 0.0

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

t_stats, t_water, t_gas, t_files, t_upload, t_add, t_csv, t_list = st.tabs(
    ["Statystyki", "Woda", "Gaz", "Faktury PDF", "Wgraj PDF", "Dodaj fakturę", "Import CSV", "Lista"])

with t_stats:
    if bills.empty:
        st.info("Brak danych. Dodaj pierwszą fakturę albo zaimportuj CSV.")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("Odkładaj miesięcznie (razem)", fmt(suggested))
        m2.metric("Koszt z użytych miesięcy (razem)", fmt(float(sav["last_sum"].sum())))
        m3.metric("Liczba faktur", len(bills))

        table = pd.DataFrame({
            "Medium": sav["utility"].map(LABEL),
            "Miesięcy w średniej": sav["months_used"],
            "Średnio / miesiąc (zł)": sav["per_month"].round(2),
            "Suma z tych miesięcy (zł)": sav["last_sum"].round(2),
        })
        st.dataframe(table, hide_index=True)
        st.caption("Średnia z maks. 12 ostatnich miesięcy w pełni pokrytych fakturami. "
                   "Faktury są rozkładane na dni swojego okresu rozliczeniowego.")

        _hist = stats.balance_history(bills)
        if not _hist.empty:
            _last = _hist.iloc[-1]
            st.metric("Stan konta u PGNiG" + (" (nadpłata)" if _last["balance"] >= 0 else " (zaległość)"),
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

with t_gas:
    hist = stats.balance_history(bills)
    if not hist.empty:
        last = hist.iloc[-1]
        st.subheader("Skarbonka u PGNiG")
        b1, b2, b3 = st.columns(3)
        b1.metric("Nadpłata na koncie" if last["balance"] >= 0 else "Zaległość na koncie", fmt2(abs(last["balance"])))
        b2.metric("Stan na dzień", f"{last['date']:%Y-%m-%d}")
        gas_monthly = float(sav.loc[sav["utility"] == "gaz", "per_month"].iloc[0]) if sav is not None else 0.0
        if last["balance"] > 0 and gas_monthly > 0:
            b3.metric("Wystarczy na", f"{last['balance'] / gas_monthly:.1f} mies.",
                      help="Przy średnim miesięcznym koszcie gazu z ostatnich 12 pełnych miesięcy.")
        st.caption(f"Stan konta po rozliczeniu z faktury {last['invoice_no']}. "
                   "Kolejne faktury są pokrywane z nadpłaty, jeśli ją masz.")
        if len(hist) > 1:
            st.line_chart(hist.set_index("date")[["balance"]].rename(columns={"balance": "Stan konta (zł)"}))

    gas_months = usage_tab(gas_daily, gas_done, "gas", "gazu", "kWh", gas_price,
                           "Koszt za kWh na kolejnych fakturach (z opłatami stałymi)",
                           "Brak w pełni pokrytych miesięcy z fakturami za gaz. Uruchom import z eBOK "
                           "(python -m app.pgnig --sync) albo wgraj faktury w zakładce „Wgraj PDF”.")
    if gas_months:
        with db.conn() as gc:
            gas_items = stats.item_daily(gc, bills, "gaz")
            prices_var, prices_fixed = stats.gas_price_series(gc, bills)
            last12 = gas_daily[gas_daily["date"].dt.to_period("M").isin(gas_done[-12:])]
            annual_kwh = float(last12["usage"].sum())
            actual12 = float(last12["cost"].sum())
            forecast = stats.gas_forecast(gc, bills, annual_kwh)
        struct = stats.cost_by_kind(gas_items, gas_months)
        if not struct.empty and (struct.index != "razem").any():
            st.subheader("Z czego składa się rachunek")
            show = struct.rename(index=lambda k: stats.GAS_KINDS.get(k, k))
            st.dataframe(pd.DataFrame({"Koszt (zł)": show["cost"].round(2), "Udział (%)": show["share"].round(1)}))
            fixed = float(struct.loc[struct.index.isin(stats.GAS_FIXED), "cost"].sum())
            variable = float(struct.loc[struct.index.isin(stats.GAS_VARIABLE), "cost"].sum())
            f1, f2 = st.columns(2)
            f1.metric("Opłaty stałe (abonament + dystrybucja stała)", fmt(fixed))
            f2.metric("Opłaty zależne od zużycia (paliwo + dystrybucja zmienna)", fmt(variable))
            st.caption("Kwoty brutto, rozłożone na dni okresów rozliczeniowych poszczególnych pozycji.")
        if not prices_var.empty:
            st.subheader("Cena za kWh, brutto (zł)")
            st.line_chart(prices_var)
        if not prices_fixed.empty:
            st.subheader("Opłaty stałe za miesiąc, brutto (zł)")
            st.line_chart(prices_fixed)
        if forecast:
            st.subheader("Prognoza roczna przy obecnych cenach")
            p1, p2, p3 = st.columns(3)
            p1.metric("Opłaty stałe / miesiąc", fmt2(forecast["fixed_month"]))
            p2.metric("Cena zmienna za kWh", f"{forecast['per_kwh']:.3f} zł".replace(".", ","))
            p3.metric("Roczny koszt", fmt(forecast["annual_cost"]),
                      delta=f"{forecast['annual_cost'] - actual12:+,.0f} zł vs ostatnie 12 mies.".replace(",", " "),
                      delta_color="inverse")
            st.caption(f"Zakłada zużycie jak w ostatnich 12 miesiącach ({annual_kwh:,.0f} kWh) i ceny z najnowszej "
                       f"faktury, czyli około {fmt(forecast['per_month'])} miesięcznie.".replace(",", " "))

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
        unit = st.selectbox("Jednostka zużycia", ["", "kWh", "m3"])
        if st.form_submit_button("Zapisz"):
            ok = db.add_bill(
                provider=provider or DEFAULT_PROVIDER[u], utility=u, invoice_no=inv,
                issue_date=issue.isoformat(),
                period_start=ps.isoformat() if ps else None,
                period_end=pe.isoformat() if pe else None,
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
