"""Duplicate and change detection across screening cycles.

Matching is layered, strongest identifier first:

  1. tender number (normalised)      -- authoritative when present
  2. detail-page URL                 -- stable on most portals
  3. fuzzy: authority + title similarity + overlapping dates

A match means "same tender": we update in place and record what changed.
No match means "new tender": we insert. An ambiguous fuzzy match (similarity
in the grey band) is inserted but flagged for manual review rather than
guessed at either way.
"""
from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from .normalize import fold

STRONG = 0.88   # >= this similarity with matching authority == same tender
GREY = 0.72     # between GREY and STRONG == flag for review


def normalise_number(number: str | None) -> str:
    """'MOH/2026/0142-A' -> 'moh20260142a'. Portals punctuate inconsistently."""
    if not number:
        return ""
    return re.sub(r"[^\w\u0600-\u06FF]", "", fold(number))


def normalise_url(url: str | None) -> str:
    if not url:
        return ""
    url = url.strip().lower()
    url = re.sub(r"[?&](sessionid|jsessionid|token|_|timestamp|rnd)=[^&]*", "", url)
    return url.rstrip("/?&")


def fingerprint(source: str, number: str | None, url: str | None,
                title: str | None, authority: str | None) -> str:
    """Stable primary key. Prefers the tender number; falls back to url, then
    to a title+authority digest so records without numbers still dedupe."""
    num = normalise_number(number)
    if num:
        basis = f"{source}|num|{num}"
    elif url:
        basis = f"{source}|url|{normalise_url(url)}"
    else:
        basis = f"{source}|txt|{fold(authority)}|{fold(title)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def content_hash(fields: dict) -> str:
    """Hash of the substantive fields; a change here means the tender moved."""
    watch = ("tender_title", "issuing_authority", "publication_date",
             "submission_deadline", "closing_time", "tender_status",
             "estimated_value", "contract_period")
    basis = "|".join(fold(str(fields.get(k, ""))) for k in watch)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def similarity(a: str | None, b: str | None) -> float:
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return 0.0
    return SequenceMatcher(None, fa, fb).ratio()


class Matcher:
    """Resolves a freshly scraped tender against the historical database."""

    def __init__(self, db, source: str):
        self.db = db
        self.source = source

    def resolve(self, scraped: dict) -> tuple[str, int | None, str]:
        """Return (action, tender_id, reason).

        action is one of: 'new', 'match', 'ambiguous'
        """
        num = scraped.get("tender_number")
        if num:
            row = self.db.query(
                "SELECT id, tender_number FROM tenders WHERE source = ?", (self.source,)
            )
            target = normalise_number(num)
            for r in row:
                if normalise_number(r["tender_number"]) == target and target:
                    return "match", r["id"], f"tender number {num}"

        url = scraped.get("tender_page_url") or scraped.get("source_url")
        if url:
            nurl = normalise_url(url)
            scraped_num = normalise_number(num)
            for r in self.db.query(
                "SELECT id, tender_number, tender_page_url, source_url "
                "FROM tenders WHERE source = ?",
                (self.source,),
            ):
                # The number is the stronger identifier: two rows carrying
                # different numbers are different tenders however their URLs
                # compare. Portals that list every tender on one search page
                # (MOH Kuwait) share a single source_url across all of them,
                # so without this guard the whole result set collapses into
                # one record.
                other_num = normalise_number(r["tender_number"])
                if scraped_num and other_num and scraped_num != other_num:
                    continue
                if nurl and normalise_url(r["tender_page_url"]) == nurl:
                    return "match", r["id"], "identical detail URL"
                if nurl and normalise_url(r["source_url"]) == nurl:
                    return "match", r["id"], "identical source URL"

        # fuzzy fallback -- same authority, near-identical title, dates agree
        title = scraped.get("tender_title")
        authority = scraped.get("issuing_authority")
        scraped_num = normalise_number(num)
        best, best_score = None, 0.0
        for r in self.db.query(
            "SELECT id, tender_number, tender_title, issuing_authority, "
            "publication_date, submission_deadline FROM tenders WHERE source = ?",
            (self.source,)
        ):
            # Two tenders that both carry a number, and the numbers differ,
            # are different tenders -- however similar their titles read.
            # Annual repeat tenders look nearly identical by title.
            other_num = normalise_number(r["tender_number"])
            if scraped_num and other_num and scraped_num != other_num:
                continue
            if authority and r["issuing_authority"]:
                if similarity(authority, r["issuing_authority"]) < 0.6:
                    continue
            s = similarity(title, r["tender_title"])
            date_agrees = (
                scraped.get("submission_deadline")
                and scraped["submission_deadline"] == r["submission_deadline"]
            )
            if date_agrees:
                s += 0.06
            if s > best_score:
                best, best_score = r["id"], s

        if best_score >= STRONG:
            return "match", best, f"title similarity {best_score:.2f}"
        if best_score >= GREY:
            return ("ambiguous", best,
                    f"title similarity {best_score:.2f} - possible duplicate of "
                    f"tender id {best}, needs manual confirmation")
        return "new", None, ""
