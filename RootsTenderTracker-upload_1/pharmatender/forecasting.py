"""Forecasting, gated on evidence.

The engine refuses to produce a forecast until it has enough observations.
Below the gate it reports what it has and what it still needs, rather than
extrapolating from one or two data points. Every output carries the
observations it rests on.

Minimums (configurable):
  * cycle/renewal estimate : 3 historical tenders for the same subject
  * quantity estimate      : 3 quantity observations
  * value estimate         : 3 value observations
  * seasonality            : 6 observations spanning >= 2 calendar years
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, timedelta

from .db import utcnow
from .normalize import fold

MIN_CYCLE = 3
MIN_QTY = 3
MIN_VALUE = 3
MIN_SEASON = 6


def _iso(d: str | None) -> date | None:
    try:
        return date.fromisoformat(d) if d else None
    except ValueError:
        return None


def _gap_days(dates: list[date]) -> list[int]:
    dates = sorted(dates)
    return [(b - a).days for a, b in zip(dates, dates[1:])]


def _cycle(dates: list[date]) -> dict | None:
    gaps = _gap_days(dates)
    if len(gaps) < MIN_CYCLE - 1:
        return None
    median = statistics.median(gaps)
    spread = statistics.pstdev(gaps) if len(gaps) > 1 else 0
    last = max(dates)
    confidence = "high" if spread < median * 0.20 else \
                 "medium" if spread < median * 0.45 else "low"
    return {
        "observations": len(dates),
        "gaps_days": gaps,
        "median_cycle_days": round(median),
        "cycle_spread_days": round(spread),
        "last_tender": last.isoformat(),
        "expected_next": (last + timedelta(days=round(median))).isoformat(),
        "confidence": confidence,
    }


def forecast_products(db, config: dict | None = None) -> list[dict]:
    """Group historical tenders by generic name and look for a rhythm."""
    rows = db.query("""
        SELECT i.generic_name, i.product_name, i.quantity, i.unit,
               t.publication_date, t.submission_deadline, t.contract_period,
               t.estimated_value, t.currency, t.issuing_authority,
               t.tender_number, t.is_oncology
        FROM tender_items i JOIN tenders t ON t.id = i.tender_id
        WHERE COALESCE(i.generic_name, i.product_name) IS NOT NULL
    """)

    grouped = defaultdict(list)
    for r in rows:
        key = fold(r["generic_name"] or r["product_name"])
        if key:
            grouped[key].append(r)

    out = []
    for key, records in grouped.items():
        dates = [d for d in (_iso(r["publication_date"]) for r in records) if d]
        label = records[0]["generic_name"] or records[0]["product_name"]
        evidence = {
            "subject": label,
            "tender_numbers": sorted({r["tender_number"] for r in records
                                      if r["tender_number"]}),
            "authorities": sorted({r["issuing_authority"] for r in records
                                   if r["issuing_authority"]}),
            "publication_dates": sorted(d.isoformat() for d in dates),
        }

        result = {"subject_type": "product", "subject": label,
                  "is_oncology": bool(records[0]["is_oncology"]),
                  "evidence": evidence, "forecasts": [], "gaps": []}

        cycle = _cycle(dates) if len(dates) >= MIN_CYCLE else None
        if cycle:
            result["forecasts"].append({
                "metric": "expected_next_tender_date",
                "value": cycle["expected_next"],
                "confidence": cycle["confidence"],
                "basis": (f"{cycle['observations']} historical tenders, median "
                          f"interval {cycle['median_cycle_days']} days "
                          f"(spread +/-{cycle['cycle_spread_days']}), last on "
                          f"{cycle['last_tender']}"),
            })
            result["forecasts"].append({
                "metric": "tender_frequency",
                "value": f"every ~{cycle['median_cycle_days']} days",
                "confidence": cycle["confidence"],
                "basis": f"intervals observed: {cycle['gaps_days']}",
            })
        else:
            result["gaps"].append(
                f"Renewal cycle: {len(dates)} dated tender(s) on file, "
                f"{MIN_CYCLE} required. No forecast produced."
            )

        qty = [r["quantity"] for r in records if r["quantity"]]
        if len(qty) >= MIN_QTY:
            result["forecasts"].append({
                "metric": "typical_quantity",
                "value": f"{statistics.median(qty):,.0f} "
                         f"{records[0]['unit'] or ''}".strip(),
                "confidence": "medium" if len(qty) < 5 else "high",
                "basis": f"median of {len(qty)} observations: "
                         f"min {min(qty):,.0f}, max {max(qty):,.0f}",
            })
        else:
            result["gaps"].append(
                f"Typical quantity: {len(qty)} observation(s), {MIN_QTY} required."
            )

        vals = [r["estimated_value"] for r in records if r["estimated_value"]]
        if len(vals) >= MIN_VALUE:
            result["forecasts"].append({
                "metric": "historical_value",
                "value": f"{statistics.median(vals):,.0f} "
                         f"{records[0]['currency'] or 'KWD'}",
                "confidence": "medium",
                "basis": f"median of {len(vals)} tender values",
            })

        months = [d.month for d in dates]
        years = {d.year for d in dates}
        if len(dates) >= MIN_SEASON and len(years) >= 2:
            common = statistics.multimode(months)
            result["forecasts"].append({
                "metric": "seasonal_pattern",
                "value": "peaks in month(s) " + ", ".join(map(str, common)),
                "confidence": "low" if len(dates) < 10 else "medium",
                "basis": f"{len(dates)} tenders across {len(years)} years; "
                         f"month distribution {sorted(months)}",
            })
        else:
            result["gaps"].append(
                f"Seasonality: needs {MIN_SEASON} tenders across 2+ years; "
                f"have {len(dates)} across {len(years)}."
            )

        out.append(result)
    return out


def forecast_authorities(db) -> list[dict]:
    rows = db.query("""
        SELECT issuing_authority, department, publication_date, tender_number
        FROM tenders WHERE publication_date IS NOT NULL
    """)
    grouped = defaultdict(list)
    for r in rows:
        grouped[r["issuing_authority"] or "Unknown"].append(r)

    out = []
    for authority, records in grouped.items():
        dates = [d for d in (_iso(r["publication_date"]) for r in records) if d]
        res = {"subject_type": "authority", "subject": authority,
               "forecasts": [], "gaps": [],
               "evidence": {"tender_count": len(records),
                            "date_range": [min(dates).isoformat(),
                                           max(dates).isoformat()] if dates else []}}
        cycle = _cycle(dates) if len(dates) >= MIN_CYCLE else None
        if cycle:
            res["forecasts"].append({
                "metric": "publication_rhythm",
                "value": f"~1 tender every {cycle['median_cycle_days']} days",
                "confidence": cycle["confidence"],
                "basis": f"{len(dates)} publications, intervals "
                         f"{cycle['gaps_days'][:8]}",
            })
        else:
            res["gaps"].append(f"Only {len(dates)} publication dates on file.")
        out.append(res)
    return out


def run_forecasting(db, config: dict | None = None, save: bool = True) -> dict:
    products = forecast_products(db, config)
    authorities = forecast_authorities(db)

    if save:
        for group in (products, authorities):
            for res in group:
                for fc in res["forecasts"]:
                    db.conn.execute(
                        "INSERT INTO forecasts (generated_at, subject_type, "
                        "subject, metric, value, confidence, evidence) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (utcnow(), res["subject_type"], res["subject"],
                         fc["metric"], str(fc["value"]), fc["confidence"],
                         json.dumps({"basis": fc["basis"],
                                     "evidence": res.get("evidence", {})},
                                    ensure_ascii=False)),
                    )
        db.conn.commit()

    produced = sum(len(r["forecasts"]) for r in products + authorities)
    return {
        "products": products,
        "authorities": authorities,
        "forecasts_produced": produced,
        "subjects_examined": len(products) + len(authorities),
    }


def print_forecasts(result: dict, only_forecasts: bool = False) -> None:
    print("=" * 70)
    print(f"FORECASTING -- {result['forecasts_produced']} forecast(s) from "
          f"{result['subjects_examined']} subject(s)")
    print("=" * 70)
    if result["forecasts_produced"] == 0:
        print("\nNot enough history yet. Forecasts need at least "
              f"{MIN_CYCLE} tenders for the same product or authority.")
        print("Keep running screening cycles; this fills in over time.\n")
    for res in result["products"] + result["authorities"]:
        if only_forecasts and not res["forecasts"]:
            continue
        print(f"\n{res['subject_type'].upper()}: {res['subject']}")
        for fc in res["forecasts"]:
            print(f"  + {fc['metric']}: {fc['value']}  [{fc['confidence']}]")
            print(f"      evidence: {fc['basis']}")
        for gap in res["gaps"]:
            print(f"  - {gap}")
