"""Excel layer.

Two jobs:

  inspect_template()  reads the user's existing workbook and reports its
                      sheets, header row, columns, data validations, number
                      formats and formulas -- then drafts a column->field
                      mapping by fuzzy-matching headers to known fields.

  export()            fills a *copy* of that template. It never rebuilds the
                      workbook: existing formatting, validations and formulas
                      are preserved, and formula columns are skipped so the
                      template's own calculations keep working.
"""
from __future__ import annotations

import shutil
from datetime import date
from difflib import get_close_matches
from pathlib import Path

import openpyxl
import yaml
from openpyxl.utils import get_column_letter

from .normalize import NA, fold

# Canonical field names the pipeline can supply, with the header phrasings
# most likely to appear in a tender tracker.
FIELD_SYNONYMS: dict[str, list[str]] = {
    "tender_number": ["tender number", "tender no", "tender ref", "reference",
                      "ref", "tender id", "رقم المناقصة"],
    "tender_title": ["tender title", "title", "subject", "tender subject",
                     "description", "موضوع المناقصة"],
    "issuing_authority": ["issuing authority", "authority", "entity", "buyer",
                          "customer", "ministry", "الجهة"],
    "department": ["department", "dept", "الإدارة"],
    "country": ["country", "market", "الدولة"],
    "publication_date": ["publication date", "published", "issue date",
                         "announcement date", "تاريخ النشر"],
    "submission_deadline": ["submission deadline", "closing date", "deadline",
                            "due date", "last date", "تاريخ الإقفال"],
    "closing_time": ["closing time", "time"],
    "product_name": ["product name", "product", "item", "commodity", "الصنف"],
    "generic_name": ["generic name", "generic", "inn", "molecule",
                     "scientific name", "الاسم العلمي"],
    "brand_name": ["brand name", "brand", "trade name", "الاسم التجاري"],
    "active_ingredient": ["active ingredient", "active substance", "api",
                          "المادة الفعالة"],
    "dosage_form": ["dosage form", "form", "presentation", "الشكل الصيدلاني"],
    "strength": ["strength", "concentration", "التركيز"],
    "pack_size": ["pack size", "pack", "packing", "العبوة"],
    "quantity": ["quantity", "qty", "required quantity", "الكمية"],
    "unit": ["unit", "uom", "الوحدة"],
    "therapeutic_area": ["therapeutic area", "category", "class",
                         "therapy area", "specialty", "الفئة العلاجية"],
    "indication": ["indication", "use", "دواعي الاستعمال"],
    "tender_status": ["tender status", "status", "stage", "الحالة"],
    "contract_period": ["contract period", "duration", "period", "المدة"],
    "estimated_value": ["estimated value", "value", "budget", "amount",
                        "القيمة التقديرية"],
    "currency": ["currency", "العملة"],
    "manufacturer_req": ["manufacturer requirements", "manufacturer",
                         "manufacturing requirements"],
    "registration_req": ["registration requirements", "registration",
                         "regulatory requirements", "التسجيل"],
    "tech_spec": ["technical specifications", "specifications", "specs",
                  "المواصفات"],
    "delivery_req": ["delivery requirements", "delivery", "lead time",
                     "التسليم"],
    "priority": ["priority", "relevance", "الأولوية"],
    "is_oncology": ["oncology", "oncology flag", "is oncology"],
    "needs_review": ["needs manual review", "manual review", "review"],
    "review_reason": ["review reason", "reason", "remarks"],
    "source_url": ["source url", "source", "website", "link"],
    "tender_document_url": ["tender document url", "document url",
                            "attachment", "document link"],
    "document_filename": ["document filename", "filename", "file"],
    "extraction_date": ["extraction date", "extracted on", "date added",
                        "screening date"],
    "notes": ["notes", "note", "comments", "ملاحظات"],
}

_LOOKUP = {fold(alias): field
           for field, aliases in FIELD_SYNONYMS.items()
           for alias in aliases + [field.replace("_", " ")]}


def guess_field(header: str) -> str | None:
    f = fold(header)
    if not f:
        return None
    if f in _LOOKUP:
        return _LOOKUP[f]
    close = get_close_matches(f, _LOOKUP.keys(), n=1, cutoff=0.86)
    return _LOOKUP[close[0]] if close else None


# ---------------------------------------------------------------- inspect


def inspect_template(path: str | Path, mapping_out: str | Path | None = None) -> dict:
    path = Path(path)
    wb_v = openpyxl.load_workbook(path, data_only=True)
    wb_f = openpyxl.load_workbook(path, data_only=False)

    report: dict = {"file": str(path), "sheets": []}
    draft: dict = {"template": str(path), "sheets": {}}

    for ws_f in wb_f.worksheets:
        ws_v = wb_v[ws_f.title]
        header_row, headers = _find_header_row(ws_v)
        cols = []
        for idx, name in headers:
            letter = get_column_letter(idx)
            sample, fmt, formula = None, None, None
            for r in range(header_row + 1, min(header_row + 12, ws_v.max_row + 1)):
                cv = ws_v.cell(row=r, column=idx).value
                cf = ws_f.cell(row=r, column=idx).value
                if isinstance(cf, str) and cf.startswith("="):
                    formula = cf
                if sample is None and cv not in (None, ""):
                    sample = cv
                    fmt = ws_v.cell(row=r, column=idx).number_format
            cols.append({
                "column": letter,
                "header": name,
                "guessed_field": guess_field(name),
                "sample": str(sample)[:60] if sample is not None else None,
                "number_format": fmt,
                "formula": formula,
                "python_type": type(sample).__name__ if sample is not None else None,
            })

        validations = []
        for dv in ws_f.data_validations.dataValidation:
            validations.append({
                "type": dv.type,
                "formula": dv.formula1,
                "ranges": str(dv.sqref or dv.ranges),
            })

        sheet_report = {
            "name": ws_f.title,
            "dimensions": ws_f.dimensions,
            "header_row": header_row,
            "data_starts_row": header_row + 1,
            "last_data_row": ws_v.max_row,
            "columns": cols,
            "data_validations": validations,
            "freeze_panes": ws_f.freeze_panes,
            "has_table_objects": [t for t in getattr(ws_f, "tables", {})],
        }
        report["sheets"].append(sheet_report)

        draft["sheets"][ws_f.title] = {
            "header_row": header_row,
            "data_starts_row": header_row + 1,
            "grain": "item" if any(c["guessed_field"] in
                                   ("product_name", "generic_name", "quantity")
                                   for c in cols) else "tender",
            "columns": {c["column"]: (c["guessed_field"] or "UNMAPPED")
                        for c in cols},
            "skip_columns": [c["column"] for c in cols if c["formula"]],
        }

    if mapping_out:
        Path(mapping_out).write_text(
            yaml.safe_dump(draft, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    report["draft_mapping"] = draft
    return report


def _find_header_row(ws, scan: int = 15) -> tuple[int, list[tuple[int, str]]]:
    """The header row is the one with the most non-empty short text cells."""
    best_row, best_score, best_headers = 1, -1, []
    for r in range(1, min(scan, ws.max_row) + 1):
        headers, score = [], 0
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and 0 < len(v.strip()) <= 60:
                headers.append((c, v.strip()))
                score += 2 if guess_field(v) else 1
        if score > best_score:
            best_row, best_score, best_headers = r, score, headers
    return best_row, best_headers


def print_inspection(report: dict) -> None:
    print("=" * 70)
    print(f"TEMPLATE: {report['file']}")
    print("=" * 70)
    for s in report["sheets"]:
        print(f"\nSheet '{s['name']}'  ({s['dimensions']})")
        print(f"  header row {s['header_row']}, data from row {s['data_starts_row']}, "
              f"last row {s['last_data_row']}")
        print(f"  {'col':<5}{'header':<34}{'-> field':<24}{'format'}")
        for c in s["columns"]:
            mark = " " if c["guessed_field"] else "?"
            print(f"  {mark}{c['column']:<4}{c['header'][:33]:<34}"
                  f"{(c['guessed_field'] or 'UNMAPPED'):<24}{c['number_format'] or ''}")
            if c["formula"]:
                print(f"       formula (will be preserved): {c['formula'][:60]}")
        if s["data_validations"]:
            print("  data validations / dropdowns:")
            for dv in s["data_validations"]:
                print(f"    {dv['ranges']}  {dv['type']}  {str(dv['formula'])[:60]}")
    print("\nA draft mapping was written. Review every UNMAPPED column before "
          "exporting.")


# ---------------------------------------------------------------- export


def export(db, mapping: dict, out_path: str | Path,
           where: str = "", params: tuple = (), grain: str | None = None) -> Path:
    """Fill a copy of the template. Returns the output path."""
    template = Path(mapping["template"])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, out_path)

    wb = openpyxl.load_workbook(out_path)
    for sheet_name, sheet_cfg in mapping["sheets"].items():
        if sheet_name not in wb.sheetnames:
            continue
        if grain and sheet_cfg.get("grain") != grain:
            continue
        ws = wb[sheet_name]
        rows = _fetch_rows(db, sheet_cfg.get("grain", "tender"), where, params)
        _write_sheet(ws, sheet_cfg, rows)

    wb.save(out_path)
    return out_path


def _fetch_rows(db, grain: str, where: str, params: tuple) -> list[dict]:
    clause = f"WHERE {where}" if where else ""
    if grain == "item":
        sql = f"""
            SELECT t.*, i.item_no, i.product_name, i.generic_name, i.brand_name,
                   i.active_ingredient, i.dosage_form, i.strength, i.pack_size,
                   i.quantity, i.unit, i.indication, i.manufacturer_req,
                   i.registration_req, i.tech_spec, i.delivery_req,
                   i.extracted_from, i.notes AS item_notes,
                   i.therapeutic_area AS item_area, i.is_oncology AS item_onc
            FROM tenders t LEFT JOIN tender_items i ON i.tender_id = t.id
            {clause}
            ORDER BY t.priority_score DESC, t.submission_deadline
        """
    else:
        sql = f"SELECT * FROM tenders t {clause} " \
              f"ORDER BY priority_score DESC, submission_deadline"
    out = []
    for r in db.query(sql, params):
        d = dict(r)
        doc = db.query(
            "SELECT document_url, filename FROM documents WHERE tender_id = ? LIMIT 1",
            (d["id"],),
        )
        d["tender_document_url"] = doc[0]["document_url"] if doc else None
        d["document_filename"] = doc[0]["filename"] if doc else None
        d["extraction_date"] = (d.get("last_seen") or "")[:10]
        d["is_oncology"] = "Yes" if d.get("is_oncology") else "No"
        d["needs_review"] = "Yes" if d.get("needs_review") else "No"
        out.append(d)
    return out


def _write_sheet(ws, cfg: dict, rows: list[dict]) -> None:
    start = int(cfg.get("data_starts_row", cfg.get("header_row", 1) + 1))
    skip = set(cfg.get("skip_columns", []))
    columns = {col: field for col, field in cfg["columns"].items()
               if field and field != "UNMAPPED" and col not in skip}

    # clear previous exported data without touching formatting
    for r in range(start, ws.max_row + 1):
        for col in columns:
            ws[f"{col}{r}"] = None

    for offset, row in enumerate(rows):
        r = start + offset
        for col, field in columns.items():
            value = row.get(field)
            if field == "product_name" and not value:
                value = row.get("tender_title")
            if field == "therapeutic_area":
                value = row.get("item_area") or row.get("therapeutic_area")
            if value in (None, ""):
                value = NA if field in _REQUIRED else None
            if isinstance(value, str) and len(value) > 32000:
                value = value[:32000]
            ws[f"{col}{r}"] = value


_REQUIRED = {
    "tender_number", "tender_title", "issuing_authority",
    "submission_deadline", "product_name", "generic_name", "quantity",
    "therapeutic_area", "source_url",
}


def default_output_name(prefix: str = "tender_screening") -> str:
    return f"{prefix}_{date.today().isoformat()}.xlsx"
