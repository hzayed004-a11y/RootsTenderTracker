"""The screening cycle.

One cycle: list -> dedupe -> enrich -> download & parse documents ->
classify -> persist -> log. Any single tender failing is caught, logged and
skipped; the cycle continues.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from . import documents as docs
from . import reference as ref
from .classifier import apply_value_boost, classify
from .db import Database
from .dedupe import Matcher, content_hash, fingerprint
from .normalize import clean

log = logging.getLogger("pharmatender")


class RunContext:
    """Thin wrapper so adapters can log errors without importing the db."""

    def __init__(self, db: Database, run_id: int):
        self.db, self.run_id = db, run_id

    def error(self, stage: str, target: str, message: str) -> None:
        log.error("[%s] %s -> %s", stage, target, message)
        self.db.log_error(self.run_id, stage, target, message)


class Screener:
    def __init__(self, db: Database, adapter, config: dict):
        self.db, self.adapter, self.config = db, adapter, config
        self.doc_dir = Path(config.get("document_dir", "documents"))
        self.doc_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.index = ref.load_index(db, config.get("brand_bridge",
                                                       "brand_inn.yaml"))
            if not self.index.moh:
                log.warning("No reference data loaded - the last five tracker "
                            "columns will be blank. Run `load-reference` first.")
        except Exception as exc:
            log.warning("reference index unavailable: %s", exc)
            self.index = None

    def run(self) -> dict:
        depts = ", ".join(
            d.get("label", "") for d in
            self.config.get("sources", {}).get(self.adapter.id, {}).get("departments", [])
        )
        run_id = self.db.start_run(self.adapter.id, self.adapter.base_url, depts)
        ctx = RunContext(self.db, run_id)
        matcher = Matcher(self.db, self.adapter.id)
        stats = {"run_id": run_id}

        try:
            if not self.adapter.login():
                ctx.error("login", self.adapter.base_url, "authentication failed")
                self.db.finish_run(run_id, "failed", "authentication failed")
                return stats
        except Exception as exc:
            ctx.error("login", self.adapter.base_url, str(exc))
            self.db.finish_run(run_id, "failed", str(exc))
            return stats

        for scraped in self.adapter.list_tenders(ctx):
            self.db.bump(run_id, "listings_reviewed")
            try:
                self._process(scraped, matcher, ctx, run_id)
            except Exception as exc:
                ctx.error("process", scraped.tender_number or scraped.tender_title,
                          repr(exc))

        self.db.finish_run(run_id)
        row = self.db.query("SELECT * FROM screening_runs WHERE id = ?", (run_id,))[0]
        return dict(row)

    # ------------------------------------------------------------------

    def _process(self, scraped, matcher: Matcher, ctx: RunContext, run_id: int) -> None:
        action, tender_id, reason = matcher.resolve(scraped.as_db_row())

        if action == "match":
            existing = self.db.query("SELECT * FROM tenders WHERE id = ?",
                                     (tender_id,))[0]
            new_hash = content_hash(scraped.as_db_row())
            if existing["content_hash"] == new_hash:
                self.db.touch(tender_id)
                self.db.bump(run_id, "duplicates_skipped")
                log.info("unchanged: %s", scraped.tender_number or scraped.tender_title)
                return
            log.info("changed: %s (%s)", scraped.tender_number, reason)

        # Enrich + read documents only for tenders we will keep.
        scraped = self.adapter.enrich(scraped, ctx)
        doc_text, parsed_docs = self._read_documents(scraped, ctx, run_id)

        text = scraped.classification_text + "\n" + doc_text
        verdict = classify(text, config=self.config)

        if not verdict.is_pharma and not verdict.needs_review:
            log.info("filtered out (not pharmaceutical): %s", scraped.tender_title)
            return

        row = scraped.as_db_row()
        row.update({
            "source": self.adapter.id,
            "fingerprint": fingerprint(self.adapter.id, scraped.tender_number,
                                       scraped.tender_page_url or scraped.source_url,
                                       scraped.tender_title,
                                       scraped.issuing_authority),
            "content_hash": content_hash(row),
            "is_pharmaceutical": int(verdict.is_pharma),
            "is_oncology": int(verdict.is_oncology),
            "therapeutic_area": verdict.therapeutic_area,
            "priority": verdict.priority,
            "priority_score": apply_value_boost(
                verdict.priority_score, row.get("estimated_value"), self.config),
            "confidence": round(verdict.confidence, 3),
            "needs_review": int(verdict.needs_review or action == "ambiguous"),
            "review_reason": clean(
                "; ".join(filter(None, [verdict.review_reason,
                                        reason if action == "ambiguous" else ""]))
            ) or None,
            "raw_row": scraped.raw_row,
        })

        if action == "match":
            changed = self.db.update_tender(tender_id, row, run_id)
            self.db.bump(run_id, "updated_tenders")
            log.info("updated %s: %s", scraped.tender_number, ", ".join(changed))
        else:
            tender_id = self.db.insert_tender(row)
            self.db.bump(run_id, "new_tenders")
            log.info("new [%s/%s] %s", verdict.therapeutic_area, verdict.priority,
                     scraped.tender_title)

        if verdict.is_pharma:
            self.db.bump(run_id, "pharma_tenders")
        if verdict.is_oncology:
            self.db.bump(run_id, "oncology_tenders")
        if row["needs_review"]:
            self.db.bump(run_id, "needs_review")

        # line items, classified individually so a single oncology product
        # inside a mixed basket is not lost
        items = []
        # The portal regenerates its PDFs on every request (the footer carries
        # a print timestamp), so the same document fetched from two links has
        # two different hashes. De-duplicate on the item itself instead.
        seen_items: set[tuple] = set()
        for pd in parsed_docs:
            for item in docs.extract_items(pd):
                # Compare on the latin core of the description: the Arabic
                # spec text around it survives the PDF font map differently
                # from one download to the next.
                key = (re.sub(r"[^a-z0-9]", "",
                              clean(item.get("product_name")).lower()),
                       clean(item.get("unit")).lower(),
                       item.get("quantity"))
                if key in seen_items:
                    continue
                seen_items.add(key)
                itext = " ".join(str(v) for v in item.values() if v)
                iv = classify(itext, config=self.config)
                item["is_oncology"] = int(iv.is_oncology)
                item["therapeutic_area"] = iv.therapeutic_area
                items.append(item)

        # A tender with no parsable line-item table still needs one tracker
        # row, so the title becomes the item. Marked for review because the
        # quantity and unit are unknown, not zero.
        if not items:
            items = [{
                "product_name": scraped.tender_title or "Not Available",
                "extracted_from": "listing",
                "therapeutic_area": verdict.therapeutic_area,
                "is_oncology": int(verdict.is_oncology),
                "needs_review": 1,
                "review_reason": "No item table found - quantity and unit not "
                                 "extracted; check the tender documents",
            }]

        code = getattr(scraped, "search_code", None) or scraped.raw_row.get(
            "_search_code")
        for item in items:
            item["search_code"] = code
            self._enrich_item(item)
        self.db.replace_items(tender_id, items)

        self._store_document_records(tender_id, parsed_docs)

    # ------------------------------------------------------------------

    def _enrich_item(self, item: dict) -> None:
        """Fill the reference columns for one line item."""
        if self.index is None:
            return
        text = " ".join(str(item.get(k) or "") for k in
                        ("product_name", "generic_name", "brand_name",
                         "active_ingredient"))
        moh = self.index.match_moh(text)
        roots = self.index.match_roots(text)

        item["registered_products"] = "\n".join(moh["products"]) or None
        item["registered_companies"] = "\n".join(moh["companies"]) or None
        item["roots_product"] = "\n".join(roots["products"]) or None
        item["roots_principal"] = "\n".join(roots["principals"]) or None
        item["roots_status"] = "\n".join(roots["statuses"]) or None

        methods = [m for m in (moh["method"], roots["method"]) if m]
        order = {"exact": 0, "skeleton": 1, "brand": 2}
        item["ref_match_method"] = (min(methods, key=lambda m: order[m])
                                    if methods else "none")

        if not methods:
            item["needs_review"] = 1
            item["review_reason"] = "; ".join(filter(None, [
                item.get("review_reason"),
                "No registered product or Roots match for this molecule",
            ]))
        elif item["ref_match_method"] in ("skeleton", "brand"):
            item["needs_review"] = 1
            item["review_reason"] = "; ".join(filter(None, [
                item.get("review_reason"),
                f"Reference match was fuzzy ({item['ref_match_method']}) - verify",
            ]))

    def _read_documents(self, scraped, ctx: RunContext, run_id: int):
        if not self.config.get("download_documents", True):
            return "", []
        max_docs = int(self.config.get("max_documents_per_tender", 12))
        max_bytes = int(self.config.get("max_document_bytes", 80_000_000))
        texts, parsed_all = [], []
        seen_digests: set[str] = set()

        for att in scraped.attachments[:max_docs]:
            try:
                if att.data is not None:
                    # Fetched by the adapter during listing (postback download).
                    data = att.data
                else:
                    resp = self.adapter.fetch(att.url)
                    resp.raise_for_status()
                    data = resp.content
                if len(data) > max_bytes:
                    ctx.error("document", att.url,
                              f"skipped, {len(data)} bytes exceeds limit")
                    continue
                name = att.filename or urlparse(att.url).path.split("/")[-1] or "file"
                digest = docs.sha256(data)
                if digest in seen_digests:
                    # The same PDF is often reachable from several links on a
                    # row; parsing it twice would duplicate every line item.
                    continue
                seen_digests.add(digest)
                local = self.doc_dir / f"{digest[:16]}_{name[:60]}"
                if not local.exists():
                    local.write_bytes(data)

                for pd in docs.parse_bytes(data, name):
                    pd.local_path = str(local)
                    pd.url = att.url
                    pd.sha256 = digest
                    pd.bytes = len(data)
                    parsed_all.append(pd)
                    if pd.error:
                        ctx.error("document-parse", name, pd.error)
                    if pd.text:
                        texts.append(pd.text)
                self.db.bump(run_id, "documents_scanned")
            except Exception as exc:
                ctx.error("document", att.url, repr(exc))

        return "\n".join(texts), parsed_all

    def _store_document_records(self, tender_id: int, parsed_docs) -> None:
        for pd in parsed_docs:
            if "::" in pd.filename:       # archive member, not a downloadable file
                continue
            self.db.record_document(
                tender_id,
                document_url=getattr(pd, "url", None),
                filename=pd.filename,
                sha256=getattr(pd, "sha256", None),
                bytes=getattr(pd, "bytes", None),
                local_path=getattr(pd, "local_path", None),
                parsed=int(not pd.error),
                parse_error=pd.error or None,
                text_chars=len(pd.text or ""),
            )
