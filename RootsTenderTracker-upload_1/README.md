# Pharmaceutical Tender Screening & Forecasting System

Repeatable screening of the Kuwait MOH e-tenders portal for pharmaceutical
opportunities, with oncology prioritisation, a historical database, and
export into your own Excel template.

**Status:** core built and tested end-to-end against a mock portal. The MOH
adapter is **not yet calibrated** — the portal blocks outside inspection, so
the two site-specific steps below have to run from your machine.

---

## Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Quick start

```bash
python run.py load-reference     # ingest price lists + Roots sheet (once)
python run.py probe              # map the portal -> paste result into config
python run.py run                # screening cycle
python run.py export-tracker     # fill Kuwait_Tenders_Tracker.xlsx
```

---

## How the tracker gets filled

The template is **item-grain**: one row per tender line item.

| Col | Header | Source |
|---|---|---|
| A | Tender no. | portal |
| B | Closing Date | portal |
| C | Item Description | portal (tender documents) |
| D | Theraputic Area | search code label, else the classifier |
| E | Unit | portal |
| F | Tender QTY | portal |
| G | Registered products | MOH Drug + Food Supplement price lists |
| H | Company name | MOH price list manufacturer |
| I | Roots product name | Roots registered-products sheet |
| J | Principle name | Roots "Company" (the principal Roots represents) |
| K | registration status | Roots registration status |

Columns A-F come from the portal. **G-K come from molecule matching** against
the reference files — see below.

### Search codes

A cycle searches the tender-title box for each code in turn, with department
set to Medical Store:

    IN   TB   ON   SP   NU

Each code maps to a Therapeutic Area label in `config.yaml` under
`therapeutic_area_codes`. **ON and TB are set; IN, SP and NU are my best guess
from the letters and need your confirmation** — they land straight in column D.

---

## Molecule matching (columns G-K)

Three problems had to be solved, none of them obvious from the files:

**1. The MOH price list has no active-ingredient column.** The molecule is
buried inside the product name. Matching extracts candidate molecules from the
tender item description and searches product names.

**2. Originators are listed under brand names only.** The string "trastuzumab"
appears nowhere in the price list — but *Herceptin* does. Same for *Crestor*
(rosuvastatin) and *Keytruda* (pembrolizumab). `brand_inn.yaml` bridges roughly
190 brands in both directions. Extend it freely; it is plain `brand: inn` text.

**3. The Roots API column has transliteration variants and inverted rows.**
`Karboplatin`, `Fluorourasil`, `Anaztrozole`, `Azythromycin`, `Levocetrizine`,
`Disloratidine` — and the Waymade rows hold the molecule in the *product* column
with the brand in the API column. Exact matching would have missed most of the
Onko oncology portfolio.

The fix is a consonant-skeleton key that absorbs c/k, s/c, z/s, ph/f, y/i and
vowel drift:

    Karboplatin -> crbpltn <- Carboplatin
    Anaztrozole -> nctrcl  <- Anastrozole

Matching runs in three tiers and records which one fired:

| Tier | Meaning | Shown as |
|---|---|---|
| `exact` | normalised strings identical | no shading |
| `skeleton` | consonant skeleton identical | **amber** |
| `brand` | matched through the brand bridge | **amber** |
| none | nothing found in either source | **pink** |

Amber and pink rows are also pushed to the manual review queue with the reason
attached. Brand aliases are matched **exact-only** — short brand names collide
under the skeleton key (*Keytruda* and *Ketotard* both reduce to `ctrd`), so
allowing fuzzy brand matching produced false positives.

A "Not Available" in columns G-K is a real finding, not a failure: Rifampicin,
Isoniazid and Macitentan are genuinely absent from the MOH price list.

---

## Two calibration steps

### 1. Map the portal

```bash
python run.py probe
```

Writes `probe_output/` (landing.html, findings.json, draft_config.yaml) and
prints whether a login is required, what the department dropdown values are,
whether results are server-rendered or fetched by JavaScript, and how
attachments are linked. Paste the generated `sources.moh_kw` block into
`config.yaml`.

If probe reports that results only appear after a POST it cannot replay,
open the page in Chrome, F12 → Network → Preserve log, run the search,
right-click the request → Copy as cURL. That capture pins down the rest.

### 2. Map your Excel template

```bash
python run.py inspect-template "My Tender Tracker.xlsx"
```

Prints every column with its detected field, number format, dropdown and
formula, and writes `mapping.yaml`. **Review the UNMAPPED columns** — those
are ones the system does not know how to fill. Edit `mapping.yaml` to point
them at a field, or leave them alone and they stay untouched.

Export fills a *copy* of your template. Formatting, dropdowns, freeze panes
and formula columns are preserved; formula columns are never written to.

---

## Commands

| Command | What it does |
|---|---|
| `python run.py run` | Run a new screening cycle |
| `python run.py new` | Tenders first seen in the last cycle |
| `python run.py oncology` | Oncology tenders |
| `python run.py high-priority` | High-priority tenders |
| `python run.py review` | Manual review queue, with reasons |
| `python run.py load-reference` | Ingest MOH price lists + Roots sheet |
| `python run.py lookup <molecule>` | Ad-hoc reference lookup, shows match tier |
| `python run.py export-tracker` | Fill the Kuwait tracker template |
| `python run.py export-tracker --code ON` | One search code only |
| `python run.py export-tracker --show-layout` | Print detected column mapping |
| `python run.py export` | Generic export into any other template |
| `python run.py history` | Screening cycle log |
| `python run.py history --errors` | Errors from recent cycles |
| `python run.py tender MOH/2026/0101` | One tender: fields, items, docs, change history |
| `python run.py forecast` | Forecasting analysis |

Scheduling is yours to set: `cron` on Linux/macOS, Task Scheduler on Windows.

```cron
0 7 * * 1-5  cd /path/to/project && .venv/bin/python run.py run
```

---

## How it decides

**Pharmaceutical filter.** Kept if the text shows pharmaceutical vocabulary,
*or* names a molecule in any therapeutic area, *or* scores on oncology
evidence. Construction, IT and office procurement are dropped. Biomedical
devices are recognised separately — set `review_device_tenders: true` in
config to route them to the review queue instead of dropping them.

**Oncology detection** does not rely on the word "oncology". It scores
five independent signals: named antineoplastics, oncology monoclonals,
cancer indications, oncology context terms, and **WHO INN stems** —
`-tinib`, `-parib`, `-ciclib`, `-zomib`, `-platin`, `-rubicin`, `-taxel`,
`-tecan`, `-citabine`, `-vedotin`. The stem rule is what catches molecules
approved after this code was written: *Zanubrutinib* appears in no list here
and is still classified correctly.

Supportive oncology (ondansetron, pegfilgrastim, zoledronic acid) carries a
lower weight, so a plain antiemetic order is not miscalled as oncology, but
the same drugs in a chemotherapy context are.

Arabic is handled throughout: diacritics stripped, alef/ya/ta-marbuta forms
unified, Arabic-Indic digits converted, and the attached definite article
(الأورام) matched correctly.

**Nothing is silently discarded.** Anything scoring between
`oncology_review_floor` and `oncology_threshold`, anything pharmaceutical
with an unclear therapeutic area, and any ambiguous duplicate goes to the
manual review queue with the reason recorded.

**Priority** is configurable in `config.yaml`: oncology, your target
portfolio (edit the `portfolio` list), specialty areas, and value threshold,
with a penalty for commodity generics.

## Duplicates and changes

Matched in order: normalised tender number → detail URL → fuzzy
(authority + title similarity + date agreement). Two tenders that both carry
numbers and whose numbers differ are never fuzzy-merged, so annual repeat
tenders with near-identical titles stay separate.

A matched tender whose content hash is unchanged is counted as a duplicate
and skipped. A changed one is updated in place, with every field-level
change written to `tender_versions` — run `python run.py tender <number>` to
see the full change history.

## Forecasting

Gated on evidence. Minimums: 3 tenders for a renewal cycle, 3 observations
for typical quantity or value, 6 tenders across 2+ calendar years for
seasonality. Below the gate it reports what it has and what it still needs
instead of extrapolating. Every forecast prints the intervals, medians and
tender numbers it rests on.

Expect nothing useful from forecasting for the first several months. That is
the design working, not a fault.

## Data reliability

Missing values are written as blank, `Not Available`, or
`Needs Manual Review`. Nothing is inferred to fill a gap. Every record keeps
its source URL, detail page URL, document URL, filename, SHA-256 and
extraction date.

## Error handling

A failure in one tender, one document or one department is logged and the
cycle continues. Scanned PDFs with no text layer are flagged
`OCR required` rather than silently yielding nothing. `.doc`, `.xls`, `.rar`
and `.7z` are reported as unsupported rather than failing quietly.

## Self-test

```bash
python selftest.py
```

Builds a mock portal and a sample template, runs three cycles (insert →
all-duplicates → one update + one insert), exports, and runs forecasting.
Run it after any change to the classifier or matcher.

## Layout

```
config.yaml              portal settings, thresholds, portfolio, weights
mapping.yaml             generated by inspect-template
run.py                   entry point
pharmatender/
  db.py                  schema + repository (source of truth)
  terminology.py         pharma / oncology lexicons, EN + AR
  classifier.py          pharma filter, oncology scorer, priority
  normalize.py           Arabic folding, dates, amounts, strengths
  documents.py           PDF / DOCX / XLSX / ZIP parsing, line items
  dedupe.py              fingerprints, matching, change detection
  pipeline.py            the screening cycle
  excel_export.py        template inspection + template-preserving export
  forecasting.py         evidence-gated forecasts
  reference.py           price-list parser, Roots loader, INN matcher
  tracker.py             the 11-column Kuwait tracker export
  probe.py               site reconnaissance
  cli.py                 commands
  sources/moh_kw.py      Kuwait MOH adapter  <- needs calibration
brand_inn.yaml           brand <-> INN bridge (extend this)
reference_data/          price lists, Roots sheet, tracker template
data/tenders.db          historical database
documents/               downloaded attachments
exports/                 generated Excel files
logs/screening.log       cycle log
```

## Adding another portal

Subclass `BaseAdapter`, implement `list_tenders()` and `enrich()`, register
it in `cli.ADAPTERS`. Nothing else changes — classification, dedupe,
database and export are portal-agnostic.

## One caution

Check the portal's terms of use before running this on a schedule, and keep
`delay_seconds` polite. This is a government system, and access to it is
tied to your company registration.
