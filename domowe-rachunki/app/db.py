import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/rachunki.sqlite")

BILLS_COLUMNS = """
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  utility TEXT NOT NULL,                  -- prad | gaz | woda | paliwo (walidowane w aplikacji)
  invoice_no TEXT,
  issue_date TEXT,
  period_start TEXT,
  period_end TEXT,
  amount_gross REAL,
  consumption REAL,
  unit TEXT,
  status TEXT NOT NULL DEFAULT 'ok',      -- ok | partial (brak okresu lub kwoty)
  source TEXT NOT NULL DEFAULT 'manual',  -- manual | csv | upload | mail-... | pgnig-api
  pdf_path TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  account_balance REAL,                   -- stan konta u dostawcy po rozliczeniu (+ nadpłata, - zaległość)
  note TEXT                               -- np. stacja paliw
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS bills ({BILLS_COLUMNS});
CREATE UNIQUE INDEX IF NOT EXISTS ux_bills_invoice
  ON bills(provider, invoice_no) WHERE invoice_no IS NOT NULL;
CREATE TABLE IF NOT EXISTS bill_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
  meter TEXT NOT NULL,          -- dom | ogrod | inny | gaz | prad | paliwo
  kind TEXT NOT NULL,           -- woda | scieki | abonament | paliwo | ...
  quantity REAL,
  unit TEXT,
  amount_gross REAL NOT NULL,
  period_start TEXT,
  period_end TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_bill ON bill_items(bill_id);
CREATE TABLE IF NOT EXISTS subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  amount REAL NOT NULL,                       -- kwota jednej płatności (zł)
  cycle TEXT NOT NULL DEFAULT 'month',        -- month | quarter | year
  next_date TEXT,                             -- data jednej z płatności (program sam liczy najbliższą)
  active INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  category TEXT NOT NULL DEFAULT 'sub',       -- sub (subskrypcje) | periodic (koszty roczne/okresowe)
  saved REAL NOT NULL DEFAULT 0               -- ile już odłożono (koszty okresowe)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_marks (    -- zaznaczenia "zapłacone" w karcie miesiąca
  month TEXT NOT NULL,
  item_key TEXT NOT NULL,
  paid INTEGER NOT NULL DEFAULT 0,
  paid_date TEXT,
  PRIMARY KEY (month, item_key)
);
CREATE TABLE IF NOT EXISTS manual_items (     -- własne pozycje miesiąca (np. czynsz, rata)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  month TEXT NOT NULL,
  name TEXT NOT NULL,
  amount REAL NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS processed_mail (
  message_id TEXT PRIMARY KEY,
  processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

ITEM_FIELDS = ["meter", "kind", "quantity", "unit", "amount_gross", "period_start", "period_end",
               "unit_price", "amount_net", "vat_rate"]
FIELDS = ["provider", "utility", "invoice_no", "issue_date", "period_start",
          "period_end", "amount_gross", "consumption", "unit", "status",
          "source", "pdf_path", "account_balance", "note"]


NEW_COLUMNS = {  # kolumny dodane po pierwszym wydaniu: dopisywane do istniejących baz
    "bills": [("account_balance", "REAL"), ("note", "TEXT")],
    "bill_items": [("unit_price", "REAL"), ("amount_net", "REAL"), ("vat_rate", "REAL")],
    "subscriptions": [("category", "TEXT NOT NULL DEFAULT 'sub'"), ("saved", "REAL NOT NULL DEFAULT 0")],
}


def _drop_utility_check(c):
    """Stare bazy mają CHECK (utility IN ('prad','gaz','woda')), który blokuje nowe media (paliwo).
    SQLite nie umie zmienić CHECK, więc tabela jest przebudowywana (z kopią danych)."""
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='bills'").fetchone()
    if not row or "CHECK" not in row[0].upper():
        return
    cols = ", ".join(r[1] for r in c.execute("PRAGMA table_info(bills)"))
    c.commit()
    c.execute("PRAGMA foreign_keys=OFF")
    c.executescript(f"""
BEGIN;
CREATE TABLE bills_new ({BILLS_COLUMNS});
INSERT INTO bills_new ({cols}) SELECT {cols} FROM bills;
DROP TABLE bills;
ALTER TABLE bills_new RENAME TO bills;
CREATE UNIQUE INDEX IF NOT EXISTS ux_bills_invoice ON bills(provider, invoice_no) WHERE invoice_no IS NOT NULL;
COMMIT;
""")
    c.execute("PRAGMA foreign_keys=ON")


def _migrate(c):
    _drop_utility_check(c)
    for table, cols in NEW_COLUMNS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    _migrate(c)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def compute_status(d):
    ok = d.get("period_start") and d.get("period_end") and d.get("amount_gross") is not None
    return "ok" if ok else "partial"


def _insert_items(c, bill_id, items):
    for it in items or []:
        vals = [it.get(k) for k in ITEM_FIELDS]
        c.execute(f"INSERT INTO bill_items (bill_id, {', '.join(ITEM_FIELDS)}) "
                  f"VALUES (?, {', '.join('?' for _ in ITEM_FIELDS)})", [bill_id, *vals])


def replace_items(bill_id, items):
    with conn() as c:
        c.execute("DELETE FROM bill_items WHERE bill_id=?", [bill_id])
        _insert_items(c, bill_id, items)


def add_bill(**f):
    """Dodaje fakturę (opcjonalnie z pozycjami: items=[{meter, kind, ...}]).
    Zwraca False, jeśli taka (dostawca + numer) już istnieje."""
    items = f.pop("items", None)
    data = {k: f.get(k) for k in FIELDS}
    for k in FIELDS:
        if data[k] == "":
            data[k] = None
    data["status"] = compute_status(data)
    data["source"] = f.get("source") or "manual"
    cols = ", ".join(FIELDS)
    marks = ", ".join("?" for _ in FIELDS)
    try:
        with conn() as c:
            cur = c.execute(f"INSERT INTO bills ({cols}) VALUES ({marks})", [data[k] for k in FIELDS])
            _insert_items(c, cur.lastrowid, items)
        return True
    except sqlite3.IntegrityError:
        return False


def update_bill(bill_id, **f):
    f["status"] = compute_status(f)
    sets = ", ".join(f"{k}=?" for k in f)
    with conn() as c:
        c.execute(f"UPDATE bills SET {sets} WHERE id=?", [*f.values(), bill_id])


def delete_bill(bill_id):
    with conn() as c:
        c.execute("DELETE FROM bills WHERE id=?", [bill_id])


def set_pdf(bill_id, path):
    with conn() as c:
        c.execute("UPDATE bills SET pdf_path=? WHERE id=?", [str(path), bill_id])


def update_columns(bill_id, **cols):
    """Aktualizuje wybrane kolumny faktury bez przeliczania statusu (np. stan konta, ścieżka PDF)."""
    allowed = {"account_balance", "pdf_path"}
    cols = {k: v for k, v in cols.items() if k in allowed}
    if not cols:
        return
    with conn() as c:
        c.execute(f"UPDATE bills SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", [*cols.values(), bill_id])


def fill_missing(bill_id, fields, overwrite=False):
    """Uzupełnia puste pola istniejącej faktury (np. rekordu z maila) i przelicza jej status; z overwrite=True
    nadpisuje też wypełnione (ponowny odczyt z PDF-a). Zwraca listę zmienionych kolumn."""
    skip = {"status", "source", "pdf_path"}
    with conn() as c:
        row = c.execute("SELECT * FROM bills WHERE id=?", [bill_id]).fetchone()
        upd = {k: v for k, v in fields.items()
               if k in FIELDS and k not in skip and v is not None and (overwrite or row[k] is None)
               and row[k] != v}
        merged = {k: row[k] for k in FIELDS}
        merged.update(upd)
        upd["status"] = compute_status(merged)
        c.execute(f"UPDATE bills SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), bill_id])
    return [k for k in upd if k != "status"]


def get_setting(key, default=None):
    import json
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", [key]).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key, value):
    import json
    with conn() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  [key, json.dumps(value)])
