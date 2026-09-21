"""Command line interface.

    pharmatender probe                 map the portal, draft its config
    pharmatender inspect-template FILE read the Excel template, draft mapping
    pharmatender run                   run a new screening cycle
    pharmatender new                   tenders first seen in the last cycle
    pharmatender oncology              oncology tenders
    pharmatender high-priority         high-priority tenders
    pharmatender review                manual review queue
    pharmatender export                export to the Excel template
    pharmatender history               screening cycle history
    pharmatender tender NUMBER         full history of one tender
    pharmatender forecast              run forecasting analysis
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from . import reference as ref
from . import tracker as trk
from datetime import date

from .db import Database
from .excel_export import (default_output_name, export, inspect_template,
                           print_inspection)
from .forecasting import print_forecasts, run_forecasting
from .pipeline import Screener
from .sources.moh_kw import MohKuwaitAdapter

ADAPTERS = {"moh_kw": MohKuwaitAdapter}
DEFAULT_CONFIG = "config.yaml"


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"Config not found: {p}. Copy config.yaml from the project root.")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def setup_logging(config: dict, verbose: bool) -> None:
    log_dir = Path(config.get("log_dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(log_dir / "screening.log", encoding="utf-8")]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )


def _table(rows, columns: list[tuple[str, str, int]]) -> None:
    if not rows:
        print("(no matching records)")
        return
    header = "  ".join(f"{title:<{w}}" for _, title, w in columns)
    print(header)
    print("-" * len(header))
    for r in rows:
        line = []
        for key, _, w in columns:
            val = r[key] if key in r.keys() else ""
            val = "" if val is None else str(val)
            line.append(f"{val[:w]:<{w}}")
        print("  ".join(line))
    print(f"\n{len(rows)} record(s)")


LIST_COLUMNS = [
    ("tender_number", "TENDER NO", 18),
    ("submission_deadline", "DEADLINE", 11),
    ("priority", "PRIO", 6),
    ("therapeutic_area", "AREA", 20),
    ("tender_title", "TITLE", 52),
]


# ---------------------------------------------------------------- commands


def cmd_probe(args, config):
    from .probe import print_report, probe
    url = args.url or config.get("sources", {}).get("moh_kw", {}).get(
        "url", MohKuwaitAdapter.base_url)
    print_report(probe(url, out_dir=args.out))


def cmd_inspect_template(args, config):
    report = inspect_template(args.file, mapping_out=args.mapping_out)
    print_inspection(report)
    print(f"\nDraft mapping written to {args.mapping_out}")


def cmd_run(args, config):
    db = Database(config["database"])
    adapter_cls = ADAPTERS[args.source]
    screener = Screener(db, adapter_cls(config, logging.getLogger("adapter")), config)
    stats = screener.run()
    print("\n" + "=" * 62)
    print(f"SCREENING CYCLE #{stats.get('run_id')}  --  {stats.get('status')}")
    print("=" * 62)
    for label, key in [
        ("Listings reviewed", "listings_reviewed"),
        ("Documents scanned", "documents_scanned"),
        ("New tenders", "new_tenders"),
        ("Updated tenders", "updated_tenders"),
        ("Duplicates skipped", "duplicates_skipped"),
        ("Pharmaceutical", "pharma_tenders"),
        ("Oncology", "oncology_tenders"),
        ("Needs manual review", "needs_review"),
        ("Errors", "errors"),
    ]:
        print(f"  {label:<22}{stats.get(key, 0)}")
    if stats.get("errors"):
        print("\n  See logs/screening.log and `pharmatender history --errors`.")


def cmd_new(args, config):
    db = Database(config["database"])
    last = db.query("SELECT id FROM screening_runs ORDER BY id DESC LIMIT 1")
    if not last:
        return print("No screening cycles have been run yet.")
    rows = db.query(
        "SELECT * FROM tenders WHERE first_seen >= "
        "(SELECT started_at FROM screening_runs WHERE id = ?) "
        "ORDER BY priority_score DESC", (last[0]["id"],))
    _table(rows, LIST_COLUMNS)


def cmd_oncology(args, config):
    db = Database(config["database"])
    _table(db.query(
        "SELECT * FROM tenders WHERE is_oncology = 1 "
        "ORDER BY submission_deadline IS NULL, submission_deadline"), LIST_COLUMNS)


def cmd_high(args, config):
    db = Database(config["database"])
    _table(db.query(
        "SELECT * FROM tenders WHERE priority = 'High' "
        "ORDER BY priority_score DESC"), LIST_COLUMNS)


def cmd_review(args, config):
    db = Database(config["database"])
    rows = db.query(
        "SELECT * FROM tenders WHERE needs_review = 1 ORDER BY last_seen DESC")
    _table(rows, [("tender_number", "TENDER NO", 18),
                  ("tender_title", "TITLE", 40),
                  ("review_reason", "REASON FOR REVIEW", 58)])


def cmd_load_reference(args, config):
    """Ingest the MOH price lists and the Roots sheet into the database."""
    db = Database(config["database"])
    srcs = config.get("reference", {})
    total = []

    for label, key in (("drug", "drug_price_list"), ("food", "food_price_list")):
        path = args.drug if key == "drug_price_list" and args.drug else \
               args.food if key == "food_price_list" and args.food else \
               srcs.get(key)
        if not path:
            continue
        if not Path(path).exists():
            print(f"  ! {key}: file not found: {path}")
            continue
        rows, unparsed = ref.parse_price_list(path, label)
        total.extend(rows)
        missing = sum(1 for r in rows if not r["manufacturer"])
        print(f"  {key:<18} {len(rows):>5} products  "
              f"({missing} without manufacturer, {len(unparsed)} lines unparsed)")

    if total:
        ref.store_moh(db, total)

    roots_path = args.roots or srcs.get("roots_products")
    if roots_path and Path(roots_path).exists():
        rows = ref.load_roots(roots_path)
        ref.store_roots(db, rows)
        from collections import Counter
        comp = Counter(r["company"] for r in rows if r["company"])
        stat = Counter(r["registration_status"] for r in rows
                       if r["registration_status"])
        print(f"  {'roots_products':<18} {len(rows):>5} products")
        print(f"       principals: {', '.join(f'{k} ({v})' for k, v in comp.most_common())}")
        print(f"       statuses  : {', '.join(f'{k} ({v})' for k, v in stat.items())}")
    elif roots_path:
        print(f"  ! roots_products: file not found: {roots_path}")

    index = ref.load_index(db, config.get("brand_bridge", "brand_inn.yaml"))
    print(f"\n  reference index ready: {len(index.moh)} registered products, "
          f"{len(index.roots)} Roots products, "
          f"{len(index.bridge)} brand/INN aliases")


def cmd_snapshot(args, config):
    """Write the screening tables to JSON for upload to the hosted app.

    The portal blocks datacentre addresses, so the instance the team reads
    cannot screen for itself; it is handed the result of a cycle run here.
    """
    from . import sync
    db = Database(config["database"])
    snap = sync.write_snapshot(db, args.out)
    print(f"\n  wrote {args.out}")
    for table, n in snap["counts"].items():
        if n:
            print(f"  {table:<18} {n}")
    print("\n  Upload it on the hosted app under 'Publish cycle'.\n")
    return 0


def cmd_lookup(args, config):
    """Ad-hoc molecule lookup against the reference data."""
    db = Database(config["database"])
    index = ref.load_index(db, config.get("brand_bridge", "brand_inn.yaml"))
    if not index.moh and not index.roots:
        return print("No reference data loaded. Run `load-reference` first.")
    text = " ".join(args.text)
    moh, roots = index.match_moh(text), index.match_roots(text)
    print(f"\n  query     : {text}")
    print(f"  molecules : {', '.join(ref.candidate_molecules(text)) or '(none found)'}")
    print(f"\n  MOH registered products [{moh['method'] or 'no match'}, "
          f"{moh['count']} hits]")
    for p_ in moh["products"]:
        print(f"      {p_}")
    for c in moh["companies"]:
        print(f"      company: {c}")
    print(f"\n  Roots portfolio [{roots['method'] or 'no match'}, "
          f"{roots['count']} hits]")
    for p_, in zip(roots["products"]):
        print(f"      {p_}")
    if roots["principals"]:
        print(f"      principal(s): {', '.join(roots['principals'])}")
        print(f"      status(es)  : {', '.join(roots['statuses'])}")


def cmd_export_tracker(args, config):
    db = Database(config["database"])
    template = args.template or config.get("tracker_template")
    if not template or not Path(template).exists():
        sys.exit(f"Tracker template not found: {template}. Set "
                 "`tracker_template` in config.yaml.")

    layout = trk.detect_layout(template)
    if args.show_layout:
        return trk.print_layout(layout)

    where, params = "", ()
    if args.oncology_only:
        where = "t.is_oncology = 1"
    elif args.high_only:
        where = "t.priority = 'High'"
    elif args.code:
        where, params = "i.search_code = ?", (args.code,)

    out = Path(args.out or config.get("export_dir", "exports")) / \
        f"Kuwait_Tenders_Tracker_{date.today().isoformat()}.xlsx"
    labels = {k: v for k, v in (config.get("therapeutic_area_codes") or {}).items()}
    stats = trk.export_tracker(db, template, out, area_labels=labels,
                               where=where, params=params, layout=layout)

    print(f"\n  wrote {stats['path']}")
    print(f"  {stats['rows']} item row(s)")
    print(f"  MOH registered product matched : {stats['moh_matched']}")
    print(f"  Roots portfolio matched        : {stats['roots_matched']}")
    print(f"  no reference match (pink)      : {stats['unmatched']}")
    print(f"  fuzzy match (amber, verify)    : {stats['fuzzy']}")


def cmd_export(args, config):
    db = Database(config["database"])
    mapping_path = Path(args.mapping or config.get("excel_mapping", "mapping.yaml"))
    if not mapping_path.exists():
        sys.exit(f"No Excel mapping at {mapping_path}. Run "
                 "`pharmatender inspect-template <your_template.xlsx>` first.")
    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))

    where, params = "", ()
    if args.oncology_only:
        where = "t.is_oncology = 1" if args.grain == "item" else "is_oncology = 1"
    elif args.high_only:
        where = "t.priority = 'High'" if args.grain == "item" else "priority = 'High'"

    out = Path(args.out or config.get("export_dir", "exports")) / default_output_name()
    path = export(db, mapping, out, where=where, params=params, grain=args.grain)
    print(f"Exported to {path}")


def cmd_history(args, config):
    db = Database(config["database"])
    if args.errors:
        rows = db.query(
            "SELECT run_id, occurred_at, stage, target, message FROM run_errors "
            "ORDER BY id DESC LIMIT 60")
        return _table(rows, [("run_id", "RUN", 5), ("stage", "STAGE", 16),
                             ("target", "TARGET", 40), ("message", "MESSAGE", 50)])
    rows = db.query("SELECT * FROM screening_runs ORDER BY id DESC LIMIT 40")
    _table(rows, [("id", "RUN", 5), ("started_at", "STARTED", 20),
                  ("listings_reviewed", "SEEN", 6), ("new_tenders", "NEW", 5),
                  ("updated_tenders", "UPD", 5), ("duplicates_skipped", "DUP", 5),
                  ("oncology_tenders", "ONC", 5), ("needs_review", "REV", 5),
                  ("errors", "ERR", 5), ("status", "STATUS", 10)])


def cmd_tender(args, config):
    db = Database(config["database"])
    rows = db.query("SELECT * FROM tenders WHERE tender_number = ?", (args.number,))
    if not rows:
        return print(f"No tender with number {args.number}")
    t = rows[0]
    print(f"\n{t['tender_number']}  --  {t['tender_title']}")
    for f in ("issuing_authority", "department", "therapeutic_area", "priority",
              "publication_date", "submission_deadline", "tender_status",
              "estimated_value", "currency", "confidence", "review_reason",
              "source_url", "tender_page_url", "first_seen", "last_seen"):
        if t[f] not in (None, ""):
            print(f"  {f:<20}{t[f]}")
    items = db.query("SELECT * FROM tender_items WHERE tender_id = ?", (t["id"],))
    if items:
        print(f"\n  {len(items)} line item(s):")
        for i in items[:40]:
            print(f"    - {i['product_name']}  qty={i['quantity']} "
                  f"{i['unit'] or ''} [{i['therapeutic_area']}]")
    ver = db.query(
        "SELECT * FROM tender_versions WHERE tender_id = ? ORDER BY id", (t["id"],))
    if ver:
        print("\n  change history:")
        for v in ver:
            print(f"    {v['changed_at'][:16]}  {v['field']}: "
                  f"{v['old_value']} -> {v['new_value']}")
    d = db.query("SELECT * FROM documents WHERE tender_id = ?", (t["id"],))
    if d:
        print("\n  documents:")
        for x in d:
            status = "parsed" if x["parsed"] else f"FAILED: {x['parse_error']}"
            print(f"    {x['filename']}  ({status})")


def cmd_forecast(args, config):
    db = Database(config["database"])
    print_forecasts(run_forecasting(db, config, save=not args.dry_run),
                    only_forecasts=args.only_forecasts)


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pharmatender", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("probe", help="map the portal and draft its config")
    s.add_argument("--url"); s.add_argument("--out", default="probe_output")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("inspect-template", help="read the Excel template")
    s.add_argument("file")
    s.add_argument("--mapping-out", default="mapping.yaml")
    s.set_defaults(func=cmd_inspect_template)

    s = sub.add_parser("run", help="run a new screening cycle")
    s.add_argument("--source", default="moh_kw", choices=list(ADAPTERS))
    s.set_defaults(func=cmd_run)

    sub.add_parser("new", help="tenders found in the last cycle").set_defaults(
        func=cmd_new)
    sub.add_parser("oncology", help="oncology tenders").set_defaults(func=cmd_oncology)
    sub.add_parser("high-priority", help="high-priority tenders").set_defaults(
        func=cmd_high)
    sub.add_parser("review", help="manual review queue").set_defaults(func=cmd_review)

    s = sub.add_parser("load-reference",
                       help="ingest the MOH price lists + Roots sheet")
    s.add_argument("--drug"); s.add_argument("--food"); s.add_argument("--roots")
    s.set_defaults(func=cmd_load_reference)

    s = sub.add_parser("snapshot",
                       help="write this cycle's result for the hosted instance")
    s.add_argument("-o", "--out", default="screening_snapshot.json")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("lookup", help="molecule lookup against reference data")
    s.add_argument("text", nargs="+")
    s.set_defaults(func=cmd_lookup)

    s = sub.add_parser("export-tracker",
                       help="fill the Kuwait_Tenders_Tracker template")
    s.add_argument("--template"); s.add_argument("--out")
    s.add_argument("--code", help="only rows found by this search code (ON, TB...)")
    s.add_argument("--oncology-only", action="store_true")
    s.add_argument("--high-only", action="store_true")
    s.add_argument("--show-layout", action="store_true",
                   help="print the detected column mapping and exit")
    s.set_defaults(func=cmd_export_tracker)

    s = sub.add_parser("export", help="export to a generic Excel template")
    s.add_argument("--mapping"); s.add_argument("--out")
    s.add_argument("--grain", choices=["tender", "item"], default=None)
    s.add_argument("--oncology-only", action="store_true")
    s.add_argument("--high-only", action="store_true")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("history", help="screening cycle history")
    s.add_argument("--errors", action="store_true")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("tender", help="full history of one tender")
    s.add_argument("number"); s.set_defaults(func=cmd_tender)

    s = sub.add_parser("forecast", help="run forecasting analysis")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--only-forecasts", action="store_true")
    s.set_defaults(func=cmd_forecast)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config) if args.command != "inspect-template" else {}
    setup_logging(config, args.verbose)
    args.func(args, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
