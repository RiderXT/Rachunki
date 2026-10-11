import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/rachunki.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  utility TEXT NOT NULL CHECK (utility IN ('prad','gaz','woda')),
  invoice_no TEXT,
  issue_date TEXT,
  period_start TEXT,
  period_end TEXT,
  amount_gross REAL,
  consumption REAL,
  unit TEXT,
  status TEXT NOT NULL DEFAULT 'ok',      -- ok | partial (brak okresu lub kwoty)
  source TEXT NOT NULL DEFAULT 'manual',  -- manual | csv | mail-woda | ...
  pdf_path TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bills_invoice
  ON bills(provider, invoice_no) WHERE invoice_no IS NOT NULL;
CREATE TABLE IF NOT EXISTS bill_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
  meter TEXT NOT NULL,          -- dom | ogrod | inny
  kind TEXT NOT NULL,           -- woda | scieki | abonament
  quantity REAL,
  unit TEXT,
  amount_gross REAL NOT NULL,
  period_start TEXT,
  period_end TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_bill ON bill_items(bill_id);
CREATE TABLE IF NOT EXISTS processed_mail (
  message_id TEXT PRIMARY KEY,
  processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

ITEM_FIELDS = ["meter", "kind", "quantity", "unit", "amount_gross", "period_start", "period_end",
               "unit_price", "amount_net", "vat_rate"]
FIELDS = ["provider", "utility", "invoice_no", "issue_date", "period_start",
          "period_end", "amount_gross", "consumption", "unit", "status",
          "source", "pdf_path", "account_balance"]


NEW_COLUMNS = {  # kolumny dodane po pierwszym wydaniu: dopisywane do istniejących baz
    "bills": [("account_balance", "REAL")],  # stan konta u dostawcy po rozliczeniu (+ nadpłata, - zaległość)
    "bill_items": [("unit_price", "REAL"), ("amount_net", "REAL"), ("vat_rate", "REAL")],
}


def _migrate(c):
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


def fill_missing(bill_id, fields):
    """Uzupełnia tylko puste pola istniejącej faktury (np. rekordu z maila) i przelicza jej status.
    Zwraca listę uzupełnionych kolumn."""
    skip = {"status", "source", "pdf_path"}
    with conn() as c:
        row = c.execute("SELECT * FROM bills WHERE id=?", [bill_id]).fetchone()
        upd = {k: v for k, v in fields.items()
               if k in FIELDS and k not in skip and v is not None and row[k] is None}
        merged = {k: row[k] for k in FIELDS}
        merged.update(upd)
        upd["status"] = compute_status(merged)
        c.execute(f"UPDATE bills SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), bill_id])
    return [k for k in upd if k != "status"]
