#!/usr/bin/env python3
"""End-to-end test of the tracker pipeline against a mock MOH portal.

Proves the whole chain before the live portal is calibrated:
  search codes -> tenders -> line items -> molecule matching against the MOH
  price lists and the Roots sheet -> the 11-column tracker.

Run:  python selftest_tracker.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

import openpyxl
import yaml

from pharmatender import reference as ref
from pharmatender import tracker as trk
from pharmatender.db import Database
from pharmatender.pipeline import Screener
from pharmatender.sources.base import BaseAdapter, ScrapedTender

WORK = Path("selftest_tracker_workspace")
CONFIG = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
CONFIG["database"] = str(WORK / "test.db")
CONFIG["download_documents"] = False
CONFIG["delay_seconds"] = 0

# Realistic Medical Store line items, one per search code.
TENDERS = [
    ("ON", "MOH/MS/2026/ON-0142", "2026-03-15", [
        ("Carboplatin 450mg/45ml solution for infusion", "Vial", 2400),
        ("Lenalidomide 25mg hard capsules", "Capsule", 18000),
        ("Trastuzumab 440mg powder for concentrate", "Vial", 650),
        ("Azacitidine 100mg powder for suspension for injection", "Vial", 1200),
    ]),
    ("ON", "MOH/MS/2026/ON-0151", "2026-04-02", [
        ("Anastrozole 1mg film coated tablets", "Tablet", 42000),
        ("Letrozole 2.5mg film coated tablets", "Tablet", 36000),
        ("Ondansetron 8mg/4ml ampoules for oncology day care", "Ampoule", 55000),
    ]),
    ("TB", "MOH/MS/2026/TB-0067", "2026-03-28", [
        ("Rifampicin 300mg capsules", "Capsule", 90000),
        ("Isoniazid 300mg tablets", "Tablet", 120000),
    ]),
    ("IN", "MOH/MS/2026/IN-0233", "2026-03-20", [
        ("Meropenem 1g powder for solution for injection", "Vial", 26000),
        ("Azithromycin 250mg capsules", "Capsule", 48000),
        ("Ciprofloxacin 500mg film coated tablets", "Tablet", 75000),
    ]),
    ("SP", "MOH/MS/2026/SP-0088", "2026-04-10", [
        ("Rosuvastatin 20mg film coated tablets", "Tablet", 64000),
        ("Pantoprazole 40mg powder for solution for injection", "Vial", 31000),
        ("Macitentan 10mg film coated tablets", "Tablet", 3600),
    ]),
    ("NU", "MOH/MS/2026/NU-0019", "2026-04-18", [
        ("Omega-3 fish oil 1000mg softgel capsules", "Capsule", 40000),
        ("Folic Acid 400mcg tablets", "Tablet", 150000),
    ]),
    # excluded by the pharmaceutical filter
    ("SP", "MOH/BME/2026/SP-0004", "2026-03-30", [
        ("Supply and installation of hospital ward furniture", "Unit", 300),
    ]),
]


class MockMOH(BaseAdapter):
    id = "moh_kw"
    name = "Mock Kuwait MOH portal"
    base_url = "https://etenders.moh.gov.kw/mock"

    def list_tenders(self, run=None):
        for code, number, deadline, items in TENDERS:
            title = "; ".join(n for n, _, _ in items)
            t = ScrapedTender(
                tender_number=number,
                tender_title=f"Supply of {title}",
                issuing_authority="Ministry of Health - Kuwait",
                department="Medical Store",
                publication_date="2026-02-01",
                submission_deadline=deadline,
                currency="KWD",
                source_url=self.base_url,
                tender_page_url=f"{self.base_url}/{number}",
                search_code=code,
            )
            t.raw_row = {"_search_code": code, "_items": items}
            yield t

    def enrich(self, tender, run=None):
        return tender


def patched_items(screener):
    """Stand in for document parsing: the mock supplies the item table."""
    original = screener._process

    def _process(scraped, matcher, ctx, run_id):
        import pharmatender.documents as docs
        rows = scraped.raw_row.get("_items", [])
        docs_extract = docs.extract_items
        docs.extract_items = lambda pd: []
        try:
            original(scraped, matcher, ctx, run_id)
        finally:
            docs.extract_items = docs_extract
        # replace the title-derived placeholder with the real line items
        t = screener.db.query(
            "SELECT id FROM tenders WHERE tender_number = ?",
            (scraped.tender_number,))
        if t and rows:
            items = []
            for name, unit, qty in rows:
                item = {"product_name": name, "unit": unit, "quantity": qty,
                        "search_code": scraped.search_code,
                        "extracted_from": "mock item table"}
                screener._enrich_item(item)
                items.append(item)
            screener.db.replace_items(t[0]["id"], items)

    screener._process = _process
    return screener


def main() -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    db = Database(CONFIG["database"])

    print("### 1. LOAD REFERENCE DATA\n")
    src = CONFIG["reference"]
    rows = []
    for label, key in (("drug", "drug_price_list"), ("food", "food_price_list")):
        r, _ = ref.parse_price_list(src[key], label)
        rows.extend(r)
    ref.store_moh(db, rows)
    roots = ref.load_roots(src["roots_products"])
    ref.store_roots(db, roots)
    print(f"  {len(rows)} MOH registered products, {len(roots)} Roots products")

    print("\n### 2. SCREENING CYCLE (search codes IN/TB/ON/SP/NU)\n")
    screener = patched_items(Screener(db, MockMOH(CONFIG), CONFIG))
    stats = screener.run()
    print(f"  listings={stats['listings_reviewed']} new={stats['new_tenders']} "
          f"pharma={stats['pharma_tenders']} oncology={stats['oncology_tenders']} "
          f"review={stats['needs_review']}")
    excluded = db.query("SELECT 1 FROM tenders WHERE tender_number LIKE '%BME%'")
    print(f"  furniture tender excluded: {not excluded}")

    print("\n### 3. TRACKER EXPORT\n")
    out = WORK / "Kuwait_Tenders_Tracker_filled.xlsx"
    st = trk.export_tracker(db, CONFIG["tracker_template"], out,
                            area_labels=CONFIG["therapeutic_area_codes"])
    print(f"  rows={st['rows']} MOH matched={st['moh_matched']} "
          f"Roots matched={st['roots_matched']} unmatched={st['unmatched']} "
          f"fuzzy={st['fuzzy']}")

    wb = openpyxl.load_workbook(out)
    ws = wb["RFQs"]
    print(f"\n  header intact: {ws['A1'].value!r} ... {ws['K1'].value!r}\n")
    hdr = f"  {'TENDER NO':<21}{'CLOSING':<12}{'ITEM':<34}{'AREA':<19}{'UNIT':<9}{'QTY':>8}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in range(2, 2 + st["rows"]):
        print(f"  {str(ws[f'A{r}'].value)[:20]:<21}{str(ws[f'B{r}'].value)[:11]:<12}"
              f"{str(ws[f'C{r}'].value)[:33]:<34}{str(ws[f'D{r}'].value)[:18]:<19}"
              f"{str(ws[f'E{r}'].value)[:8]:<9}{str(ws[f'F{r}'].value):>8}")

    print(f"\n  {'ITEM':<34}{'REGISTERED PRODUCT':<36}{'ROOTS':<16}{'PRINCIPAL':<11}{'STATUS'}")
    print("  " + "-" * 105)
    for r in range(2, 2 + st["rows"]):
        print(f"  {str(ws[f'C{r}'].value)[:33]:<34}{str(ws[f'G{r}'].value)[:35]:<36}"
              f"{str(ws[f'I{r}'].value)[:15]:<16}{str(ws[f'J{r}'].value)[:10]:<11}"
              f"{str(ws[f'K{r}'].value)[:18]}")


if __name__ == "__main__":
    main()
