"""Move a screening result from the machine that ran it to the one that serves it.

The MOH portal refuses connections from datacentre addresses, so a cycle can
only run from inside the company network, while the app people read is hosted.
This carries the screening tables between the two.

Reference data is deliberately not included: each instance loads the price
lists itself, and the hosted one parses them with poppler, so its copy is the
better of the two and must not be overwritten by an import.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

# Order matters on import: parents before the rows that point at them.
SYNC_TABLES = (
    "screening_runs",
    "tenders",
    "tender_items",
    "documents",
    "run_errors",
    "tender_versions",
    "forecasts",
)

FORMAT = 1


def _columns(db, table: str) -> list[str]:
    return [r["name"] for r in db.query(f"PRAGMA table_info({table})")]


def _table_exists(db, table: str) -> bool:
    return bool(db.query(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def export_snapshot(db) -> dict:
    """Every screened row, as plain JSON-safe values."""
    tables: dict[str, list[dict]] = {}
    for table in SYNC_TABLES:
        if not _table_exists(db, table):
            continue
        tables[table] = [dict(r) for r in db.query(f"SELECT * FROM {table}")]
    return {
        "format": FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {t: len(rows) for t, rows in tables.items()},
        "tables": tables,
    }


def import_snapshot(db, payload: dict) -> dict:
    """Replace the screening tables with the snapshot's rows.

    A replace rather than a merge: the snapshot is one complete cycle's view
    of the portal, and merging two partial views would leave rows behind that
    the source no longer has. Row ids are preserved so tender_items still
    point at the right tender.
    """
    if not isinstance(payload, dict) or "tables" not in payload:
        raise ValueError("Not a screening snapshot: no 'tables' key")
    if int(payload.get("format", 0)) != FORMAT:
        raise ValueError(f"Snapshot format {payload.get('format')} is not "
                         f"supported (expected {FORMAT})")

    tables = payload["tables"]
    written: dict[str, int] = {}

    with db.tx() as cur:
        # Children first, so nothing is orphaned mid-way.
        for table in reversed(SYNC_TABLES):
            if _table_exists(db, table):
                cur.execute(f"DELETE FROM {table}")

        for table in SYNC_TABLES:
            rows = tables.get(table) or []
            if not rows or not _table_exists(db, table):
                written[table] = 0
                continue
            # Only columns this database actually has: an older or newer
            # instance should still take what it understands.
            known = set(_columns(db, table))
            cols = [c for c in rows[0] if c in known]
            if not cols:
                written[table] = 0
                continue
            sql = (f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
                   f"VALUES ({','.join('?' * len(cols))})")
            cur.executemany(sql, [[r.get(c) for c in cols] for r in rows])
            written[table] = len(rows)

    return {"imported": written,
            "exported_at": payload.get("exported_at")}


def write_snapshot(db, path) -> dict:
    snap = export_snapshot(db)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False)
    return snap
