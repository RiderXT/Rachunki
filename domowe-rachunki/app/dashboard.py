from datetime import date

import pandas as pd
import streamlit as st

from app import db, stats

st.set_page_config(page_title="Rachunki domowe", layout="wide")
LABEL = stats.UTILITIES
DEFAULT_PROVIDER = {"prad": "Enea", "gaz": "PGNiG", "woda": "Wodociągi"}
EDIT_COLS = ["provider", "utility", "invoice_no", "issue_date", "period_start",
             "period_end", "amount_gross", "consumption", "unit"]


def fmt(x):
    return f"{x:,.0f} zł".replace(",", " ")


def clean(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    return v.isoformat() if isinstance(v, date) else v


with db.conn() as c:
    bills = stats.load_bills(c)

sav = stats.suggested_savings(bills) if not bills.empty else None
suggested = float(sav["per_month"].sum()) if sav is not None else 0.0

st.sidebar.header("Skarbonka")
contribution = st.sidebar.number_input("Miesięczna wpłata (zł)", min_value=0.0, step=10.0,
                                       value=float(round(suggested)))
balance_from = st.sidebar.date_input("Liczone od", value=date(date.today().year, 1, 1))

st.title("Rachunki domowe")
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))
if not bills.empty:
    n_partial = int((bills["status"] == "partial").sum())
    if n_partial:
        st.warning(f"{n_partial} faktur ma niepełne dane (brak okresu lub kwoty). "
                   "Uzupełnij je w zakładce „Lista”. Bez okresu kwota liczona jest "
                   "do miesiąca wystawienia, bez kwoty faktura jest pomijana.")

t_stats, t_add, t_csv, t_list = st.tabs(["Statystyki", "Dodaj fakturę", "Import CSV", "Lista"])

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
