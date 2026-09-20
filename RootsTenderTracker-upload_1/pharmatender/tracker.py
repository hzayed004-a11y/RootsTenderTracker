"""Export into the Kuwait_Tenders_Tracker template.

The tracker is item-grain: one row per tender line item, not per tender.

    A Tender no.            <- tenders.tender_number
    B Closing Date          <- tenders.submission_deadline
    C Item Description      <- tender_items.product_name
    D Theraputic Area       <- search code label, else classifier area
    E Unit                  <- tender_items.unit
    F Tender QTY            <- tender_items.quantity
    G Registered products   <- MOH price list  (reference.py)
    H Company name          <- MOH price list manufacturer
    I Roots product name    <- Roots sheet
    J Principle name        <- Roots "Company" (the principal)
    K registration status   <- Roots registration status

Written into a COPY of the user's file, so the original template is never
touched and its formatting survives.
"""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .normalize import NA, fold

TRACKER_COLUMNS = [
    ("tender_number", ["tender no", "tender number"]),
    ("submission_deadline", ["closing date", "closing"]),
    ("product_name", ["item description", "description"]),
    ("therapeutic_area", ["theraputic area", "therapeutic area"]),
    ("unit", ["unit"]),
    ("quantity", ["tender qty", "quantity", "qty"]),
    ("registered_products", ["registered products", "registered product"]),
    ("registered_companies", ["company name", "company"]),
    ("roots_product", ["roots product name", "roots product"]),
    ("roots_principal", ["principle name", "principal name", "principle"]),
    ("roots_status", ["registration status", "status"]),
]

REVIEW_FILL = PatternFill("solid", fgColor="FFF2CC")   # amber: matched fuzzily
EMPTY_FILL = PatternFill("solid", fgColor="FCE4E4")    # pink: nothing matched


def detect_layout(path: str | Path) -> dict:
    """Find the tracker's header row and map its columns to our fields."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]

    header_row, header_cells = None, []
    for r, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True), 1):
        cells = [(i + 1, str(v).strip()) for i, v in enumerate(row)
                 if v not in (None, "")]
        if len(cells) >= 4:
            header_row, header_cells = r, cells
            break
    wb.close()
    if header_row is None:
        raise RuntimeError(f"No header row found in {path}")

    mapping, unmapped = {}, []
    for col_idx, text in header_cells:
        folded = fold(text)
        matched = None
        for field, aliases in TRACKER_COLUMNS:
            if field in mapping.values():
                continue
            if any(fold(a) in folded for a in aliases):
                matched = field
                break
        if matched:
            mapping[get_column_letter(col_idx)] = matched
        else:
            unmapped.append((get_column_letter(col_idx), text))

    return {
        "sheet": ws.title,
        "header_row": header_row,
        "data_starts_row": header_row + 1,
        "columns": mapping,
        "unmapped": unmapped,
        "headers": {get_column_letter(i): t for i, t in header_cells},
    }


def _area_for(rec: dict, area_labels: dict) -> str:
    """Column D: the classifier's therapeutic area, with the search code's
    configured label kept only as a fallback. The code is a tender-series
    prefix rather than a disease area, so it cannot lead here."""
    for area in (rec.get("item_area"), rec.get("tender_area")):
        if area and area != "Other Pharmaceutical":
            return area
    return (rec.get("item_area") or rec.get("tender_area")
            or area_labels.get(rec.get("search_code")) or NA)


def _join(value: str | None, limit: int = 3) -> str:
    """Reference matches are stored newline-separated; show the top few."""
    if not value:
        return NA
    parts = [p for p in str(value).split("\n") if p.strip()]
    if not parts:
        return NA
    shown = parts[:limit]
    if len(parts) > limit:
        shown.append(f"(+{len(parts) - limit} more)")
    return "; ".join(shown)


def fetch_rows(db, where: str = "", params: tuple = ()) -> list[dict]:
    clause = f"WHERE {where}" if where else ""
    sql = f"""
        SELECT t.tender_number, t.submission_deadline, t.therapeutic_area
                 AS tender_area, t.priority, t.is_oncology,
               i.product_name, i.unit, i.quantity, i.search_code,
               i.therapeutic_area AS item_area,
               i.registered_products, i.registered_companies,
               i.roots_product, i.roots_principal, i.roots_status,
               i.ref_match_method, i.needs_review, i.review_reason
        FROM tender_items i
        JOIN tenders t ON t.id = i.tender_id
        {clause}
        ORDER BY t.submission_deadline IS NULL, t.submission_deadline,
                 t.tender_number, i.id
    """
    return [dict(r) for r in db.query(sql, params)]


def export_rows_to_bytes(rows: list[dict], template: str | Path,
                         area_labels: dict | None = None) -> bytes:
    """Write an already-selected set of rows into a copy of the template.

    The web UI filters rows in Python so that the screen and the download
    cannot drift apart; this is the same writer as export_tracker, taking the
    rows it is given instead of querying for them.
    """
    import io
    buf = io.BytesIO(Path(template).read_bytes())
    layout = detect_layout(template)
    area_labels = {("ON" if k is True else "NO" if k is False else str(k)): v
                   for k, v in (area_labels or {}).items()}

    wb = openpyxl.load_workbook(buf)
    ws = wb[layout["sheet"]]
    start, cols = layout["data_starts_row"], layout["columns"]
    stats = {"rows": 0, "moh_matched": 0, "roots_matched": 0,
             "unmatched": 0, "fuzzy": 0}

    for offset, rec in enumerate(rows):
        r = start + offset
        area = rec.get("_area") or _area_for(rec, area_labels)
        values = _row_values(rec, area)
        method = rec.get("ref_match_method")
        matched_moh = values["registered_products"] != NA
        matched_roots = values["roots_product"] != NA
        stats["rows"] += 1
        stats["moh_matched"] += int(matched_moh)
        stats["roots_matched"] += int(matched_roots)
        if not matched_moh and not matched_roots:
            stats["unmatched"] += 1
        if method in ("skeleton", "brand"):
            stats["fuzzy"] += 1
        _write_row(ws, cols, r, values, matched_moh, matched_roots, method)

    _write_legend(ws, layout, len(rows), stats)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _row_values(rec: dict, area: str) -> dict:
    return {
        "tender_number": rec.get("tender_number") or NA,
        "submission_deadline": rec.get("submission_deadline") or NA,
        "product_name": rec.get("product_name") or NA,
        "therapeutic_area": area,
        "unit": rec.get("unit") or NA,
        "quantity": rec.get("quantity"),
        "registered_products": _join(rec.get("registered_products")),
        "registered_companies": _join(rec.get("registered_companies")),
        "roots_product": _join(rec.get("roots_product")),
        "roots_principal": _join(rec.get("roots_principal")),
        "roots_status": _join(rec.get("roots_status")),
    }


def _write_row(ws, cols: dict, r: int, values: dict, matched_moh: bool,
               matched_roots: bool, method: str | None) -> None:
    for col, field in cols.items():
        cell = ws[f"{col}{r}"]
        cell.value = values.get(field)
        if field in ("registered_products", "registered_companies",
                     "roots_product", "roots_principal", "roots_status"):
            if not matched_moh and not matched_roots:
                cell.fill = EMPTY_FILL
            elif method in ("skeleton", "brand"):
                cell.fill = REVIEW_FILL
            cell.alignment = Alignment(wrap_text=True, vertical="top")


def export_tracker(db, template: str | Path, out_path: str | Path,
                   area_labels: dict | None = None,
                   where: str = "", params: tuple = (),
                   layout: dict | None = None) -> dict:
    template, out_path = Path(template), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, out_path)

    layout = layout or detect_layout(template)
    # Normalise keys: an unquoted "ON" in YAML arrives as the boolean True.
    area_labels = {("ON" if k is True else "NO" if k is False else str(k)): v
                   for k, v in (area_labels or {}).items()}
    rows = fetch_rows(db, where, params)

    wb = openpyxl.load_workbook(out_path)
    ws = wb[layout["sheet"]]
    start = layout["data_starts_row"]
    cols = layout["columns"]

    stats = {"rows": 0, "moh_matched": 0, "roots_matched": 0,
             "unmatched": 0, "fuzzy": 0}

    for offset, rec in enumerate(rows):
        r = start + offset
        area = _area_for(rec, area_labels)

        values = {
            "tender_number": rec.get("tender_number") or NA,
            "submission_deadline": rec.get("submission_deadline") or NA,
            "product_name": rec.get("product_name") or NA,
            "therapeutic_area": area,
            "unit": rec.get("unit") or NA,
            "quantity": rec.get("quantity"),
            "registered_products": _join(rec.get("registered_products")),
            "registered_companies": _join(rec.get("registered_companies")),
            "roots_product": _join(rec.get("roots_product")),
            "roots_principal": _join(rec.get("roots_principal")),
            "roots_status": _join(rec.get("roots_status")),
        }

        method = rec.get("ref_match_method")
        matched_moh = values["registered_products"] != NA
        matched_roots = values["roots_product"] != NA
        stats["rows"] += 1
        stats["moh_matched"] += int(matched_moh)
        stats["roots_matched"] += int(matched_roots)
        if not matched_moh and not matched_roots:
            stats["unmatched"] += 1
        if method in ("skeleton", "brand"):
            stats["fuzzy"] += 1

        for col, field in cols.items():
            cell = ws[f"{col}{r}"]
            cell.value = values.get(field)
            if field in ("registered_products", "registered_companies",
                         "roots_product", "roots_principal", "roots_status"):
                if not matched_moh and not matched_roots:
                    cell.fill = EMPTY_FILL
                elif method in ("skeleton", "brand"):
                    cell.fill = REVIEW_FILL
                cell.alignment = Alignment(wrap_text=True, vertical="top")

    _write_legend(ws, layout, len(rows), stats)
    wb.save(out_path)
    stats["path"] = str(out_path)
    return stats


def _write_legend(ws, layout: dict, n_rows: int, stats: dict) -> None:
    """A short key below the data, so a reader knows what the shading means."""
    r = layout["data_starts_row"] + n_rows + 2
    first_col = min(layout["columns"], key=lambda c: ws[f"{c}1"].column)
    ws[f"{first_col}{r}"] = "LEGEND"
    ws[f"{first_col}{r}"].font = Font(bold=True)
    notes = [
        ("Amber cells", "matched by fuzzy molecule match (transliteration or "
                        "brand bridge) - verify before relying on it"),
        ("Pink cells", "no match found in the MOH price lists or the Roots "
                       "sheet - may be an unregistered molecule, or a brand "
                       "name not in the bridge"),
        ("Not Available", "the source did not contain this value; nothing was "
                          "inferred"),
        ("Coverage", f"{stats['rows']} item rows | MOH matched "
                     f"{stats['moh_matched']} | Roots matched "
                     f"{stats['roots_matched']} | unmatched {stats['unmatched']} "
                     f"| fuzzy {stats['fuzzy']}"),
        ("Generated", date.today().isoformat()),
    ]
    for i, (label, text) in enumerate(notes, start=1):
        ws[f"{first_col}{r + i}"] = label
        ws[f"{first_col}{r + i}"].font = Font(bold=True)
        nxt = get_column_letter(ws[f"{first_col}{r + i}"].column + 1)
        ws[f"{nxt}{r + i}"] = text


def print_layout(layout: dict) -> None:
    print("=" * 68)
    print(f"TRACKER LAYOUT  sheet '{layout['sheet']}', "
          f"header row {layout['header_row']}, data from row "
          f"{layout['data_starts_row']}")
    print("=" * 68)
    for col, header in layout["headers"].items():
        field = layout["columns"].get(col)
        mark = " " if field else "?"
        print(f" {mark}{col:<3} {header[:34]:<36} -> {field or 'UNMAPPED'}")
    if layout["unmapped"]:
        print("\nUnmapped columns are left untouched by the export.")
