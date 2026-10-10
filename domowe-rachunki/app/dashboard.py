import hmac
import os
import re
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from app import db, stats
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
                   "Uzupełnij je w zakładce „Lista”. Bez okresu kwota liczona jest "
                   "do miesiąca wystawienia, bez kwoty faktura jest pomijana.")

t_stats, t_water, t_files, t_add, t_csv, t_list = st.tabs(
    ["Statystyki", "Woda", "Faktury PDF", "Dodaj fakturę", "Import CSV", "Lista"])

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

with t_water:
    if water_bills.empty or not water_done:
        st.info("Brak w pełni pokrytych miesięcy z fakturami za wodę. Pobierz faktury (konektor) "
                "albo dodaj je ręcznie.")
    else:
        years = sorted({m.year for m in water_done}, reverse=True)
        choice = st.selectbox("Okres", ["Ostatnie 12 miesięcy"] + [f"Rok {y}" for y in years])
        months = water_done[-12:] if choice.startswith("Ostatnie") else \
            [m for m in water_done if m.year == int(choice.split()[1])]
        totals, compare, n_common = stats.water_table(water_daily, months, water_done)
        name = lambda k: stats.METERS.get(k, "Razem" if k == "razem" else k)  # noqa: E731
        st.caption(f"Pełne miesiące w okresie: {len(months)} ({months[0]} – {months[-1]}). "
                   "Zużycie i koszty są rozkładane na dni okresu rozliczeniowego z faktury.")

        if "razem" in totals.index:
            k1, k2, k3 = st.columns(3)
            k1.metric("Zużycie wody", f"{totals.loc['razem', 'm3']:.1f} m³")
            k2.metric("Koszt", fmt(totals.loc["razem", "cost"]))
            k3.metric("Średnio / miesiąc", fmt(totals.loc["razem", "cost"] / len(months)))
            tbl = totals.rename(index=name)
            st.dataframe(pd.DataFrame({
                "Zużycie (m³)": tbl["m3"].round(2), "Koszt (zł)": tbl["cost"].round(2),
                "Koszt na m³ (zł)": tbl["price"].round(2)}))
            st.caption("Koszt na m³ = koszt całkowity (razem z opłatami stałymi) podzielony przez zużycie.")

        st.subheader("Rok do roku")
        if compare.empty:
            st.info("Za mało danych do porównania: potrzebne są te same miesiące z poprzedniego roku "
                    "(i ze szczegółowymi pozycjami faktur). Porównanie pojawi się samo, gdy dane się uzbierają.")
        else:
            st.caption(f"Porównanie na {n_common} wspólnych miesiącach (te same miesiące rok wcześniej).")
            cmp_tbl = compare.rename(index=name)
            st.dataframe(pd.DataFrame({
                "Zużycie (m³)": cmp_tbl["m3"].round(2), "rok wcześniej (m³)": cmp_tbl["m3_prev"].round(2),
                "zmiana zużycia (%)": cmp_tbl["m3_chg"].round(1),
                "Koszt (zł)": cmp_tbl["cost"].round(2), "rok wcześniej (zł)": cmp_tbl["cost_prev"].round(2),
                "zmiana kosztu (%)": cmp_tbl["cost_chg"].round(1)}))

        for value, title in (("m3", "Zużycie miesięczne (m³)"), ("cost", "Koszt miesięczny (zł)")):
            monthly = stats.water_monthly(water_daily, months, value)
            if not monthly.empty:
                st.subheader(title)
                st.bar_chart(monthly.rename(columns=name))
        if not water_price.empty:
            st.subheader("Cena za m³ na kolejnych fakturach (woda + ścieki, bez opłat stałych)")
            st.line_chart(water_price.rename(columns=name))

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
            disabled=["id", "status", "source", "pdf_path", "created_at"],
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
