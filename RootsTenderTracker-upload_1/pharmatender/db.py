"""SQLite historical database: the source of truth.

Excel is an export layer on top of this. Nothing is ever hard-deleted; tenders
that change get a new row in tender_versions so history is preserved.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- One row per distinct tender, identified by (source, tender_number).
CREATE TABLE IF NOT EXISTS tenders (
    id                  INTEGER PRIMARY KEY,
    fingerprint         TEXT NOT NULL UNIQUE,   -- stable dedupe key
    source              TEXT NOT NULL,          -- adapter id, e.g. 'moh_kw'
    tender_number       TEXT,
    tender_title        TEXT,
    tender_title_ar     TEXT,
    issuing_authority   TEXT,
    department          TEXT,
    country             TEXT DEFAULT 'Kuwait',
    publication_date    TEXT,                   -- ISO yyyy-mm-dd
    submission_deadline TEXT,
    closing_time        TEXT,
    tender_status       TEXT,
    contract_period     TEXT,
    estimated_value     REAL,
    currency            TEXT,
    tender_fee          REAL,
    bid_bond            REAL,
    therapeutic_area    TEXT,
    is_pharmaceutical   INTEGER DEFAULT 0,
    is_oncology         INTEGER DEFAULT 0,
    priority            TEXT,                   -- High | Medium | Low
    priority_score      REAL,
    confidence          REAL,
    needs_review        INTEGER DEFAULT 0,
    review_reason       TEXT,
    source_url          TEXT,
    tender_page_url     TEXT,
    search_code         TEXT,
    raw_row             TEXT,                   -- JSON of the scraped listing row
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    last_changed        TEXT,
    content_hash        TEXT                    -- detects substantive changes
);

CREATE INDEX IF NOT EXISTS ix_tenders_number   ON tenders(tender_number);
CREATE INDEX IF NOT EXISTS ix_tenders_deadline ON tenders(submission_deadline);
CREATE INDEX IF NOT EXISTS ix_tenders_onc      ON tenders(is_oncology);
CREATE INDEX IF NOT EXISTS ix_tenders_prio     ON tenders(priority);

-- Line items: a tender usually covers many products.
CREATE TABLE IF NOT EXISTS tender_items (
    id                INTEGER PRIMARY KEY,
    tender_id         INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    item_no           TEXT,
    product_name      TEXT,
    generic_name      TEXT,
    brand_name        TEXT,
    active_ingredient TEXT,
    atc_code          TEXT,
    dosage_form       TEXT,
    strength          TEXT,
    pack_size         TEXT,
    quantity          REAL,
    unit              TEXT,
    unit_price        REAL,
    line_value        REAL,
    indication        TEXT,
    therapeutic_area  TEXT,
    is_oncology       INTEGER DEFAULT 0,
    manufacturer_req  TEXT,
    registration_req  TEXT,
    tech_spec         TEXT,
    delivery_req      TEXT,
    needs_review      INTEGER DEFAULT 0,
    review_reason     TEXT,
    extracted_from    TEXT,   -- 'listing' | 'detail_page' | document filename
    extraction_date   TEXT,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_tender ON tender_items(tender_id);
CREATE INDEX IF NOT EXISTS ix_items_gen    ON tender_items(generic_name);

-- Attachments, with traceability back to the exact file parsed.
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    tender_id      INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    document_url   TEXT,
    filename       TEXT,
    content_type   TEXT,
    sha256         TEXT,
    bytes          INTEGER,
    local_path     TEXT,
    parsed         INTEGER DEFAULT 0,
    parse_error    TEXT,
    text_chars     INTEGER,
    downloaded_at  TEXT,
    UNIQUE(tender_id, sha256)
);

-- Every field-level change, so nothing is silently overwritten.
CREATE TABLE IF NOT EXISTS tender_versions (
    id          INTEGER PRIMARY KEY,
    tender_id   INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    run_id      INTEGER,
    changed_at  TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT
);

CREATE TABLE IF NOT EXISTS screening_runs (
    id                INTEGER PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    source            TEXT,
    website           TEXT,
    departments       TEXT,
    pages_scanned     INTEGER DEFAULT 0,
    listings_reviewed INTEGER DEFAULT 0,
    documents_scanned INTEGER DEFAULT 0,
    new_tenders       INTEGER DEFAULT 0,
    updated_tenders   INTEGER DEFAULT 0,
    duplicates_skipped INTEGER DEFAULT 0,
    pharma_tenders    INTEGER DEFAULT 0,
    oncology_tenders  INTEGER DEFAULT 0,
    needs_review      INTEGER DEFAULT 0,
    errors            INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'running',
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS run_errors (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER REFERENCES screening_runs(id),
    occurred_at TEXT,
    stage      TEXT,
    target     TEXT,
    message    TEXT
);

CREATE TABLE IF NOT EXISTS forecasts (
    id            INTEGER PRIMARY KEY,
    generated_at  TEXT,
    subject_type  TEXT,   -- 'product' | 'authority' | 'category'
    subject       TEXT,
    metric        TEXT,
    value         TEXT,
    confidence    TEXT,
    evidence      TEXT    -- JSON: the observations the forecast rests on
);
"""

MUTABLE_FIELDS = [
    "tender_title", "issuing_authority", "department", "publication_date",
    "submission_deadline", "closing_time", "tender_status", "contract_period",
    "estimated_value", "currency", "tender_fee", "bid_bond",
    "therapeutic_area", "priority", "tender_page_url",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        additions = {
            "tender_items": ("search_code", "registered_products",
                             "registered_companies", "roots_product",
                             "roots_principal", "roots_status",
                             "ref_match_method"),
            "tenders": ("search_code",),
        }
        for table, cols in additions.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col not in have:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} TEXT")

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------------- runs ----------------

    def start_run(self, source: str, website: str, departments: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO screening_runs (started_at, source, website, departments) "
            "VALUES (?,?,?,?)",
            (utcnow(), source, website, departments),
        )
        self.conn.commit()
        return cur.lastrowid

    def bump(self, run_id: int, field: str, n: int = 1) -> None:
        self.conn.execute(
            f"UPDATE screening_runs SET {field} = COALESCE({field},0) + ? WHERE id = ?",
            (n, run_id),
        )

    def finish_run(self, run_id: int, status: str = "completed", notes: str = "") -> None:
        self.conn.execute(
            "UPDATE screening_runs SET finished_at=?, status=?, notes=? WHERE id=?",
            (utcnow(), status, notes, run_id),
        )
        self.conn.commit()

    def log_error(self, run_id: int, stage: str, target: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO run_errors (run_id, occurred_at, stage, target, message) "
            "VALUES (?,?,?,?,?)",
            (run_id, utcnow(), stage, str(target)[:500], str(message)[:2000]),
        )
        self.bump(run_id, "errors")
        self.conn.commit()

    # ---------------- tenders ----------------

    def find_by_fingerprint(self, fp: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tenders WHERE fingerprint = ?", (fp,)
        ).fetchone()

    def find_by_number(self, source: str, number: str) -> sqlite3.Row | None:
        if not number:
            return None
        return self.conn.execute(
            "SELECT * FROM tenders WHERE source = ? AND tender_number = ?",
            (source, number),
        ).fetchone()

    def insert_tender(self, data: dict[str, Any]) -> int:
        data = dict(data)
        data.setdefault("first_seen", utcnow())
        data["last_seen"] = utcnow()
        if isinstance(data.get("raw_row"), (dict, list)):
            data["raw_row"] = json.dumps(data["raw_row"], ensure_ascii=False)
        cols = ", ".join(data)
        marks = ", ".join("?" * len(data))
        cur = self.conn.execute(
            f"INSERT INTO tenders ({cols}) VALUES ({marks})", tuple(data.values())
        )
        self.conn.commit()
        return cur.lastrowid

    def update_tender(self, tender_id: int, data: dict[str, Any], run_id: int | None = None) -> list[str]:
        """Update in place, recording every field-level change. Returns changed fields."""
        existing = self.conn.execute(
            "SELECT * FROM tenders WHERE id = ?", (tender_id,)
        ).fetchone()
        changed: list[str] = []
        for field in MUTABLE_FIELDS:
            if field not in data:
                continue
            new = data[field]
            old = existing[field]
            if new in (None, "", "Not Available"):
                continue
            if str(old or "") != str(new):
                changed.append(field)
                self.conn.execute(
                    "INSERT INTO tender_versions (tender_id, run_id, changed_at, field, "
                    "old_value, new_value) VALUES (?,?,?,?,?,?)",
                    (tender_id, run_id, utcnow(), field, str(old), str(new)),
                )
        payload = {k: v for k, v in data.items()
                   if k in MUTABLE_FIELDS and v not in (None, "", "Not Available")}
        payload["last_seen"] = utcnow()
        if changed:
            payload["last_changed"] = utcnow()
        if "content_hash" in data:
            payload["content_hash"] = data["content_hash"]
        sets = ", ".join(f"{k} = ?" for k in payload)
        self.conn.execute(
            f"UPDATE tenders SET {sets} WHERE id = ?", (*payload.values(), tender_id)
        )
        self.conn.commit()
        return changed

    def touch(self, tender_id: int) -> None:
        self.conn.execute(
            "UPDATE tenders SET last_seen = ? WHERE id = ?", (utcnow(), tender_id)
        )
        self.conn.commit()

    # ---------------- items & docs ----------------

    def replace_items(self, tender_id: int, items: Iterable[dict]) -> int:
        """Items come from documents that may be re-parsed; replace wholesale."""
        items = list(items)
        if not items:
            return 0
        self.conn.execute("DELETE FROM tender_items WHERE tender_id = ?", (tender_id,))
        for it in items:
            it = {k: v for k, v in it.items() if v is not None}
            it["tender_id"] = tender_id
            it.setdefault("extraction_date", utcnow())
            cols = ", ".join(it)
            marks = ", ".join("?" * len(it))
            self.conn.execute(
                f"INSERT INTO tender_items ({cols}) VALUES ({marks})", tuple(it.values())
            )
        self.conn.commit()
        return len(items)

    def record_document(self, tender_id: int, **kw) -> int | None:
        kw["tender_id"] = tender_id
        kw.setdefault("downloaded_at", utcnow())
        cols = ", ".join(kw)
        marks = ", ".join("?" * len(kw))
        try:
            cur = self.conn.execute(
                f"INSERT INTO documents ({cols}) VALUES ({marks})", tuple(kw.values())
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # same file already stored for this tender

    def document_seen(self, tender_id: int, sha256: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM documents WHERE tender_id = ? AND sha256 = ?",
            (tender_id, sha256),
        ).fetchone() is not None

    # ---------------- queries ----------------

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()
