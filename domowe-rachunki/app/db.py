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
CREATE TABLE IF NOT EXISTS processed_mail (
  message_id TEXT PRIMARY KEY,
  processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

FIELDS = ["provider", "utility", "invoice_no", "issue_date", "period_start",
          "period_end", "amount_gross", "consumption", "unit", "status",
          "source", "pdf_path"]


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def compute_status(d):
    ok = d.get("period_start") and d.get("period_end") and d.get("amount_gross") is not None
    return "ok" if ok else "partial"


def add_bill(**f):
    """Dodaje fakturę. Zwraca False, jeśli taka (dostawca + numer) już istnieje."""
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
            c.execute(f"INSERT INTO bills ({cols}) VALUES ({marks})", [data[k] for k in FIELDS])
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
