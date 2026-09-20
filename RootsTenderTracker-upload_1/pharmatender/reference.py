"""Reference data: MOH Kuwait price lists + the Roots registered-products sheet.

These fill the last five columns of the tracker:

    Registered products  <- MOH price list PRODUCT NAME
    Company name         <- MOH price list MANUFACTURER NAME
    Roots product name   <- Roots "Product name"
    Principle name       <- Roots "Company"  (the principal Roots represents)
    registration status  <- Roots "Registration Status"

Matching is by molecule, and the molecule names in these sources are messy:

  * The MOH price list has no API column at all -- the molecule is buried
    inside the product name ("0.5%w/v Metronidazole Injection USP (100ml Bag)").
  * The Roots API column carries transliteration variants: Karboplatin,
    Fluorourasil, Anaztrozole, Azythromycin, Levocetrizine, Disloratidine.
  * Some Roots rows hold a reference brand instead of an API
    ("Revlimid (lenalidomide) Capsules", "Paraplatin Injection").
  * The Waymade rows are inverted -- the product name holds the molecule
    and the API column holds the brand ("Alprostadil" / "Neupedix").

So matching runs in three tiers, and every match records which tier produced
it, so a reviewer can audit anything below an exact hit.

  1. exact      normalised strings identical
  2. skeleton   consonant skeleton identical (absorbs c/k, s/c, z/s, ph/f,
                y/i and vowel drift)  -> Karboplatin == Carboplatin
  3. contains   molecule appears inside the product name -> MOH price list
"""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from pathlib import Path

from .normalize import clean, fold

# --------------------------------------------------------------------- keys

_NOISE = re.compile(
    r"\b(injection|infusion|solution|suspension|tablets?|capsules?|caps|tabs?|"
    r"vials?|ampoules?|amps?|syrup|cream|ointment|gel|drops|spray|inhaler|"
    r"sachets?|powder|softgels?|film|coated|prefilled|syringe|usp|bp|ph\.?eur|"
    r"sterile|for|and|with|in|the|iv|im|sc|oral|topical|eye|ear|nasal|"
    r"lyophilized|lyophilised|concentrate|bag|bottle|pack|box|mg|mcg|ml|gm|g|"
    r"iu|units?|w/v|w/w|hcl|hydrochloride|sodium|potassium|sulfate|sulphate|"
    r"phosphate|acetate|citrate|maleate|tartrate|besylate|mesylate|succinate|"
    r"fumarate|dihydrate|monohydrate|anhydrous|as|of|plus|forte|retard|sr|xr|"
    r"od|bd|tds)\b",
    re.IGNORECASE,
)
_PAREN = re.compile(r"\(([^)]{3,60})\)")
_NUMS = re.compile(r"\d+(?:[.,]\d+)?\s*%?")


def normalise_molecule(name: str | None) -> str:
    """Lowercase alphabetic core of a molecule name."""
    if not name:
        return ""
    s = fold(name)
    s = _NUMS.sub(" ", s)
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^a-z ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_SKEL_SUBS = [
    ("ph", "f"), ("gh", "g"), ("kh", "c"), ("th", "t"), ("ch", "c"),
    ("qu", "c"), ("q", "c"), ("k", "c"), ("x", "cs"), ("z", "s"),
    ("s", "c"), ("w", "v"), ("j", "i"),
]


def skeleton(name: str | None) -> str:
    """Consonant skeleton -- absorbs the transliteration drift in these sources.

    Karboplatin / Carboplatin   -> crbpltn
    Fluorourasil / Fluorouracil -> flrrcl
    Anaztrozole  / Anastrozole  -> nctrcl
    Disloratidine/ Desloratadine-> dslrtdn
    """
    s = normalise_molecule(name).replace(" ", "")
    if not s:
        return ""
    for a, b in _SKEL_SUBS:
        s = s.replace(a, b)
    s = re.sub(r"[aeiouy]", "", s)
    s = re.sub(r"(.)\1+", r"\1", s)
    return s


def molecule_variants(raw: str | None) -> list[str]:
    """Pull every plausible molecule string out of a messy API cell.

    "Revlimid (lenalidomide) Capsules" -> ["revlimid lenalidomide", "lenalidomide"]
    "Omeprazole & Sodium Bicarbonate"  -> ["omeprazole bicarbonate", "omeprazole",
                                           "bicarbonate"]
    """
    if not raw:
        return []
    out, seen = [], set()

    def add(v):
        v = normalise_molecule(v)
        if len(v) >= 4 and v not in seen:
            seen.add(v)
            out.append(v)

    add(raw)
    for inner in _PAREN.findall(str(raw)):
        add(inner)
    for part in re.split(r"[&,/+]|\band\b", _PAREN.sub(" ", str(raw))):
        add(part)
    return out


# ------------------------------------------------------- MOH price list PDF

_PRICE_ROW = re.compile(
    r"^(?P<body>.*?)\s{2,}"
    r"(?P<pack>\d[\d.,]*\s*[A-Za-z][\w.'/()+-]*(?:\s+[A-Za-z][\w.'/()+-]*)?)?"
    r"\s+(?P<whole>\d+\.\d{3})\s+(?P<retail>\d+\.\d{3})\s*$"
)
_PRICE_ROW_TIGHT = re.compile(
    r"^(?P<body>.*?)\s+(?P<pack>\d[\d.,]*\s*[A-Za-z][\w.'/()+-]*)"
    r"\s+(?P<whole>\d+\.\d{3})\s+(?P<retail>\d+\.\d{3})\s*$"
)
_PACK_SPLIT = re.compile(r"^(\d[\d.,]*)\s*(.*)$")


def _pdf_lines(path: str | Path) -> list[str]:
    """Layout-preserving text for the price-list parser.

    poppler's pdftotext -layout is the reference implementation: the row
    regexes below depend on the column gaps it emits. Where poppler is not
    installed (a plain `pip install` on macOS or Windows), fall back to
    pdfplumber's layout mode, which reproduces the same column spacing
    closely enough for those regexes.
    """
    try:
        res = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return _pdf_lines_pdfplumber(path)
    return [l.rstrip() for l in res.stdout.split("\n") if l.strip()]


# pdftotext -layout lays a page out on a fixed character grid; the row
# regexes above read the wide gaps it leaves between columns. pdfplumber's
# own layout mode collapses those gaps, so rebuild the grid from word
# coordinates instead. 200 columns matches poppler's default density for
# an A4 page closely enough for the gaps to survive.
_LAYOUT_COLS = 200


def _pdf_lines_pdfplumber(path: str | Path) -> list[str]:
    import pdfplumber
    out: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            if not words:
                continue
            char_w = (page.width or 595) / _LAYOUT_COLS
            lines: dict[int, list] = {}
            for w in words:
                # Round the baseline so words sharing a row group together.
                key = int(round(float(w["top"]) / 3.0))
                lines.setdefault(key, []).append(w)
            for key in sorted(lines):
                row = sorted(lines[key], key=lambda w: float(w["x0"]))
                buf = ""
                for w in row:
                    col = int(float(w["x0"]) / char_w)
                    if col < len(buf):
                        col = len(buf) + 1      # never overwrite a placed word
                    buf += " " * (col - len(buf)) + w["text"]
                if buf.strip():
                    out.append(buf.rstrip())
    return out


def parse_price_list(path: str | Path, source: str) -> tuple[list[dict], list[str]]:
    """Parse a MOH price-list PDF into product rows.

    Two passes. The first takes rows where the layout left a wide gap between
    product name and manufacturer, and builds a manufacturer lexicon from
    them. The second uses that lexicon to split the rows where a long product
    name collapsed the gap.
    """
    lines = _pdf_lines(path)
    staged, unparsed = [], []

    for line in lines:
        m = _PRICE_ROW.match(line) or _PRICE_ROW_TIGHT.match(line)
        if not m:
            unparsed.append(line)
            continue
        staged.append((m.group("body"), m.group("pack"),
                       m.group("whole"), m.group("retail")))

    # pass 1 -- manufacturer lexicon from cleanly separated rows
    lexicon: dict[str, str] = {}
    for body, *_ in staged:
        parts = re.split(r"\s{3,}", body)
        if len(parts) >= 2:
            mfr = clean(parts[-1])
            if 3 <= len(mfr) <= 120:
                lexicon.setdefault(fold(mfr), mfr)

    by_length = sorted(lexicon, key=len, reverse=True)

    rows: list[dict] = []
    for body, pack, whole, retail in staged:
        parts = re.split(r"\s{3,}", body)
        if len(parts) >= 2:
            product, manufacturer = clean(" ".join(parts[:-1])), clean(parts[-1])
        else:
            product, manufacturer = clean(body), ""
            folded = fold(body)
            for key in by_length:          # longest suffix match wins
                if folded.endswith(key):
                    manufacturer = lexicon[key]
                    product = clean(body[: len(body) - len(manufacturer)]) or clean(body)
                    break

        qty, unit = None, None
        if pack:
            pm = _PACK_SPLIT.match(pack.strip())
            if pm:
                try:
                    qty = float(pm.group(1).replace(",", ""))
                except ValueError:
                    qty = None
                unit = clean(pm.group(2)) or None

        if not product or len(product) < 3:
            unparsed.append(body)
            continue

        rows.append({
            "source": source,
            "product_name": product,
            "manufacturer": manufacturer or None,
            "pack_qty": qty,
            "form": unit,
            "wholesale_price": float(whole),
            "retail_price": float(retail),
            "molecule_key": normalise_molecule(product),
            "skeleton": skeleton(product),
        })

    # page furniture, not data
    noise = re.compile(r"STATE OF KUWAIT|MINISTRY OF HEALTH|PHARMACEUTICAL|"
                       r"REGISTRATION|PRICING SECTION|PRODUCT NAME|Price--List|"
                       r"Whole|Retail|/Sale|/Price|SUPP--", re.IGNORECASE)
    unparsed = [u for u in unparsed if not noise.search(u)]
    return rows, unparsed


# ----------------------------------------------------------- Roots workbook

ROOTS_COLUMNS = {
    "company": ["company"],
    "product_name": ["product name"],
    "api": ["api", "active"],
    "medication_use": ["medication use", "use"],
    "pack_size": ["pack size", "pack"],
    "manufacturer": ["mfr. name & location", "mfr", "manufacturer"],
    "price": ["whole sale price", "price"],
    "registration_status": ["registration status", "status"],
}


def load_roots(path: str | Path) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    out: list[dict] = []
    for ws in wb.worksheets:
        rows = [r for r in ws.iter_rows(values_only=True)
                if any(v not in (None, "") for v in r)]
        if len(rows) < 2:
            continue
        header = [fold(str(h or "")) for h in rows[0]]
        idx: dict[str, int] = {}
        for field, aliases in ROOTS_COLUMNS.items():
            for i, h in enumerate(header):
                if h and any(fold(a) in h for a in aliases):
                    idx.setdefault(field, i)
                    break
        if "product_name" not in idx:
            continue
        for r in rows[1:]:
            get = lambda f: clean(r[idx[f]]) if f in idx and idx[f] < len(r) else ""  # noqa: E731
            product = get("product_name")
            if not product:
                continue
            api = get("api")
            # The Waymade rows are inverted: the molecule sits in the product
            # column and the brand in the API column. Both are indexed, so a
            # molecule search hits either way.
            out.append({
                "company": get("company") or None,
                "product_name": product,
                "api": api or None,
                "medication_use": get("medication_use") or None,
                "pack_size": get("pack_size") or None,
                "manufacturer": get("manufacturer") or None,
                "registration_status": get("registration_status") or None,
                "sheet": ws.title,
            })
    return out


# ------------------------------------------------------------- the index

# Minimum consonant-skeleton length for a fuzzy match. Below this, short
# names collide: "Keytruda"/"Ketotard" both reduce to "ctrd".
MIN_SKELETON = 5

STOPWORDS = {
    "supply", "supplies", "tender", "annual", "various", "assorted", "item",
    "items", "medicine", "medicines", "drug", "drugs", "product", "products",
    "medical", "store", "hospital", "ministry", "health", "kuwait", "quantity",
    "requirement", "requirements", "contract", "year", "years", "please",
    "note", "specification", "specifications", "brand", "generic", "original",
}


def candidate_molecules(text: str | None, max_terms: int = 8) -> list[str]:
    """Pull likely molecule words out of a tender item description."""
    base = normalise_molecule(text)
    if not base:
        return []
    words = [w for w in base.split() if len(w) >= 5 and w not in STOPWORDS]
    out: list[str] = []
    # bigrams first ("sodium valproate", "amoxicillin clavulanate")
    for a, b in zip(words, words[1:]):
        out.append(f"{a} {b}")
    out.extend(words)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:max_terms]


def load_brand_bridge(path: str | Path = "brand_inn.yaml") -> dict[str, set[str]]:
    """brand <-> INN, both directions, normalised."""
    import yaml
    p = Path(path)
    bridge: dict[str, set[str]] = defaultdict(set)
    if not p.exists():
        return bridge
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    for brand, inn in data.items():
        b, i = normalise_molecule(brand), normalise_molecule(inn)
        if not b or not i:
            continue
        bridge[b].add(i)
        bridge[i].add(b)
        for tok in i.split():
            if len(tok) >= 5:
                bridge[tok].add(b)
    return bridge


class ReferenceIndex:
    """Molecule -> registered products (MOH) and Roots portfolio entries."""

    def __init__(self, moh_rows: list[dict], roots_rows: list[dict],
                 brand_bridge: dict[str, set[str]] | None = None):
        self.moh = moh_rows
        self.roots = roots_rows
        self.bridge = brand_bridge if brand_bridge is not None else load_brand_bridge()

        self.moh_by_skeleton: dict[str, list[int]] = defaultdict(list)
        self.moh_tokens: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(moh_rows):
            if r["skeleton"]:
                self.moh_by_skeleton[r["skeleton"]].append(i)
            for tok in r["molecule_key"].split():
                if len(tok) >= 5:
                    self.moh_tokens[tok].append(i)
                    sk = skeleton(tok)
                    if len(sk) >= MIN_SKELETON:
                        self.moh_tokens[f"~{sk}"].append(i)

        self.roots_exact: dict[str, list[int]] = defaultdict(list)
        self.roots_skel: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(roots_rows):
            # index the API cell and the product-name cell alike
            for raw in (r.get("api"), r.get("product_name")):
                for v in molecule_variants(raw):
                    self.roots_exact[v].append(i)
                    for tok in v.split():
                        if len(tok) >= 5:
                            self.roots_exact[tok].append(i)
                            sk = skeleton(tok)
                            if len(sk) >= MIN_SKELETON:
                                self.roots_skel[sk].append(i)
                    sk = skeleton(v)
                    if len(sk) >= 4:
                        self.roots_skel[sk].append(i)

    # ---------------- MOH registered products ----------------

    def _expand(self, terms: list[str]) -> list[tuple[str, bool]]:
        """Each search term, plus its brand/INN counterparts."""
        out: list[tuple[str, bool]] = [(t, False) for t in terms]
        for t in terms:
            for alias in self.bridge.get(t, ()):
                out.append((alias, True))
            for tok in t.split():
                for alias in self.bridge.get(tok, ()):
                    out.append((alias, True))
        seen, uniq = set(), []
        for term, via_brand in out:
            if term not in seen:
                seen.add(term)
                uniq.append((term, via_brand))
        return uniq

    def match_moh(self, description: str | None, limit: int = 6) -> dict:
        hits: dict[int, str] = {}
        for term, via_brand in self._expand(candidate_molecules(description)):
            before = len(hits)
            for i in self.moh_tokens.get(term, []):
                hits.setdefault(i, "brand" if via_brand else "exact")
            # Skeleton is a fallback for transliteration drift in long INNs
            # only. Brand names are short and collide under it -- "Keytruda"
            # and "Ketotard" share a skeleton -- so brands stay exact-only.
            if len(hits) == before and not via_brand:
                sk = skeleton(term)
                if len(sk) >= MIN_SKELETON:
                    for i in self.moh_tokens.get(f"~{sk}", []):
                        hits.setdefault(i, "skeleton")
            if len(hits) >= limit * 4:
                break

        if not hits:
            return {"products": [], "companies": [], "method": None, "count": 0}

        order = {"exact": 0, "skeleton": 1, "brand": 2}
        ranked = sorted(hits.items(), key=lambda kv: (order[kv[1]],
                                                      len(self.moh[kv[0]]["product_name"])))
        products, companies = [], []
        for i, _ in ranked[:limit]:
            row = self.moh[i]
            if row["product_name"] not in products:
                products.append(row["product_name"])
            if row["manufacturer"] and row["manufacturer"] not in companies:
                companies.append(row["manufacturer"])
        best = min(hits.values(), key=lambda m: order[m])
        return {"products": products, "companies": companies,
                "method": best, "count": len(hits)}

    # ---------------- Roots portfolio ----------------

    def match_roots(self, description: str | None, limit: int = 4) -> dict:
        hits: dict[int, str] = {}
        for term, via_brand in self._expand(candidate_molecules(description)):
            before = len(hits)
            for i in self.roots_exact.get(term, []):
                hits.setdefault(i, "brand" if via_brand else "exact")
            if len(hits) == before and not via_brand:
                sk = skeleton(term)
                if len(sk) >= MIN_SKELETON:
                    for i in self.roots_skel.get(sk, []):
                        hits.setdefault(i, "skeleton")

        if not hits:
            return {"products": [], "principals": [], "statuses": [],
                    "method": None, "count": 0}

        order = {"exact": 0, "skeleton": 1, "brand": 2}
        ranked = sorted(hits.items(), key=lambda kv: order[kv[1]])
        products, principals, statuses = [], [], []
        for i, _ in ranked[:limit]:
            r = self.roots[i]
            if r["product_name"] not in products:
                products.append(r["product_name"])
            if r["company"] and r["company"] not in principals:
                principals.append(r["company"])
            if r["registration_status"] and r["registration_status"] not in statuses:
                statuses.append(r["registration_status"])
        best = min(hits.values(), key=lambda m: order[m])
        return {"products": products, "principals": principals,
                "statuses": statuses, "method": best, "count": len(hits)}


# ---------------------------------------------------------- persistence

REFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ref_moh_products (
    id INTEGER PRIMARY KEY,
    source TEXT, product_name TEXT, manufacturer TEXT,
    pack_qty REAL, form TEXT, wholesale_price REAL, retail_price REAL,
    molecule_key TEXT, skeleton TEXT, loaded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ref_moh_skel ON ref_moh_products(skeleton);

CREATE TABLE IF NOT EXISTS ref_roots_products (
    id INTEGER PRIMARY KEY,
    company TEXT, product_name TEXT, api TEXT, medication_use TEXT,
    pack_size TEXT, manufacturer TEXT, registration_status TEXT,
    sheet TEXT, loaded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ref_roots_api ON ref_roots_products(api);
"""


def install(db) -> None:
    db.conn.executescript(REFERENCE_SCHEMA)
    db.conn.commit()


def store_moh(db, rows: list[dict]) -> int:
    from .db import utcnow
    install(db)
    db.conn.execute("DELETE FROM ref_moh_products")
    db.conn.executemany(
        "INSERT INTO ref_moh_products (source, product_name, manufacturer, "
        "pack_qty, form, wholesale_price, retail_price, molecule_key, skeleton, "
        "loaded_at) VALUES (:source,:product_name,:manufacturer,:pack_qty,:form,"
        ":wholesale_price,:retail_price,:molecule_key,:skeleton,:loaded_at)",
        [{**r, "loaded_at": utcnow()} for r in rows],
    )
    db.conn.commit()
    return len(rows)


def store_roots(db, rows: list[dict]) -> int:
    from .db import utcnow
    install(db)
    db.conn.execute("DELETE FROM ref_roots_products")
    db.conn.executemany(
        "INSERT INTO ref_roots_products (company, product_name, api, "
        "medication_use, pack_size, manufacturer, registration_status, sheet, "
        "loaded_at) VALUES (:company,:product_name,:api,:medication_use,"
        ":pack_size,:manufacturer,:registration_status,:sheet,:loaded_at)",
        [{**r, "loaded_at": utcnow()} for r in rows],
    )
    db.conn.commit()
    return len(rows)


def load_index(db, brand_path: str | Path = "brand_inn.yaml") -> ReferenceIndex:
    install(db)
    moh = [dict(r) for r in db.query("SELECT * FROM ref_moh_products")]
    roots = [dict(r) for r in db.query("SELECT * FROM ref_roots_products")]
    return ReferenceIndex(moh, roots, load_brand_bridge(brand_path))
