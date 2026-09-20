"""Web interface for the Pharmaceutical Tender Screening System.

Runs the whole pipeline behind a browser UI so there is nothing to install
locally. Deployed on Railway it can also reach the MOH portal directly, which
is what makes the calibration step possible at all.

Environment variables
---------------------
APP_TOKEN    access token; if unset the UI is open to anyone with the URL
DATA_DIR     where the database, documents and exports live (default /data)
PORT         listening port (Railway sets this)
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pharmatender import reference as ref
from pharmatender import tracker as trk
from pharmatender.db import Database
from pharmatender.forecasting import run_forecasting
from pharmatender.pipeline import Screener
from pharmatender.probe import probe
from pharmatender.sources.moh_kw import MohKuwaitAdapter

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
APP_TOKEN = os.environ.get("APP_TOKEN", "").strip()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("web")

app = FastAPI(title="Pharma Tender Screening")


# --------------------------------------------------------------- config

def load_config() -> dict:
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg["database"] = str(DATA_DIR / "tenders.db")
    cfg["document_dir"] = str(DATA_DIR / "documents")
    cfg["export_dir"] = str(DATA_DIR / "exports")
    cfg["brand_bridge"] = str(BASE / "brand_inn.yaml")
    cfg["tracker_template"] = str(BASE / "reference_data" /
                                  "Kuwait_Tenders_Tracker.xlsx")
    cfg["reference"] = {
        "drug_price_list": str(BASE / "reference_data" / "Drug_Price_List_May-2026.pdf"),
        "food_price_list": str(BASE / "reference_data" / "FoodSupp_Price_List_May-2026.pdf"),
        "roots_products": str(BASE / "reference_data" / "Roots_Registered_Products__2026_.xlsx"),
    }
    # live overrides written by the calibration page
    override = DATA_DIR / "portal_config.yaml"
    if override.exists():
        patch = yaml.safe_load(override.read_text(encoding="utf-8")) or {}
        cfg.setdefault("sources", {}).setdefault("moh_kw", {}).update(
            patch.get("sources", {}).get("moh_kw", {}))
        for key in ("therapeutic_area_codes",):
            if key in patch:
                cfg[key] = patch[key]
    return cfg


def save_portal_config(patch: dict) -> None:
    path = DATA_DIR / "portal_config.yaml"
    current = {}
    if path.exists():
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    src = current.setdefault("sources", {}).setdefault("moh_kw", {})
    src.update(patch.get("sources", {}).get("moh_kw", {}))
    if "therapeutic_area_codes" in patch:
        current["therapeutic_area_codes"] = patch["therapeutic_area_codes"]
    path.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def get_db() -> Database:
    return Database(load_config()["database"])


# ------------------------------------------------------------ job runner

class Job:
    """One background task at a time, with a live log the UI can poll."""

    def __init__(self):
        self.name = None
        self.state = "idle"        # idle | running | done | failed
        self.lines: list[str] = []
        self.started = None
        self.finished = None
        self.result: dict = {}
        self._lock = threading.Lock()

    def start(self, name: str, fn) -> bool:
        with self._lock:
            if self.state == "running":
                return False
            self.name, self.state = name, "running"
            self.lines = []
            self.started = datetime.now(timezone.utc)
            self.finished, self.result = None, {}

        handler = _ListHandler(self)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                               "%H:%M:%S"))
        root = logging.getLogger()
        root.addHandler(handler)

        def runner():
            try:
                self.result = fn(self) or {}
                self.state = "done"
            except Exception as exc:
                self.log(f"FAILED: {exc}")
                self.log(traceback.format_exc()[-1500:])
                self.state = "failed"
            finally:
                self.finished = datetime.now(timezone.utc)
                root.removeHandler(handler)

        threading.Thread(target=runner, daemon=True).start()
        return True

    def log(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > 800:
            del self.lines[:200]


class _ListHandler(logging.Handler):
    def __init__(self, job: Job):
        super().__init__()
        self.job = job

    def emit(self, record):
        try:
            self.job.log(self.format(record))
        except Exception:
            pass


JOB = Job()


# ------------------------------------------------------------- rendering

CSS = """
*{box-sizing:border-box}
:root{--bg:#0f1720;--panel:#17212b;--line:#22303d;--ink:#e6edf3;--dim:#8ba1b4;
--accent:#4da3ff;--good:#3fb950;--warn:#d29922;--bad:#f85149}
body{margin:0;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--panel);border-bottom:1px solid var(--line);
padding:14px 22px;display:flex;align-items:center;gap:22px;flex-wrap:wrap;
position:sticky;top:0;z-index:5}
header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.2px}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{color:var(--dim);text-decoration:none;padding:6px 11px;border-radius:6px;
font-size:14px}
nav a:hover{background:#1f2c38;color:var(--ink)}
nav a.on{background:#1f2c38;color:var(--accent)}
main{padding:22px;max-width:1500px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;
padding:15px 17px}
.card .n{font-size:27px;font-weight:600;line-height:1.1}
.card .l{color:var(--dim);font-size:12.5px;margin-top:3px;
text-transform:uppercase;letter-spacing:.5px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:9px;
padding:18px 20px;margin-bottom:18px}
.panel h2{margin:0 0 4px;font-size:15px;font-weight:600}
.panel p.sub{margin:0 0 14px;color:var(--dim);font-size:13.5px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:11.5px;
text-transform:uppercase;letter-spacing:.5px;padding:7px 9px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 9px;border-bottom:1px solid #1c2731;vertical-align:top}
tr:hover td{background:#1b2732}
a{color:var(--accent)}
.btn{display:inline-block;background:var(--accent);color:#06121f;
border:0;border-radius:7px;padding:9px 15px;font-size:14px;font-weight:600;
cursor:pointer;text-decoration:none;margin:3px 5px 3px 0}
.btn:hover{filter:brightness(1.1)}
.btn.sec{background:#243140;color:var(--ink)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;
font-weight:600}
.t-onc{background:#3a1d2a;color:#ff9db4}
.t-high{background:#3a2a12;color:#ffc972}
.t-ok{background:#12301c;color:#7ee39a}
.t-rev{background:#382a12;color:#ffd07a}
.t-na{background:#2a2f36;color:#9fb0bf}
pre.log{background:#0b1219;border:1px solid var(--line);border-radius:7px;
padding:13px;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
max-height:430px;overflow:auto;white-space:pre-wrap;color:#b9cadb}
input,select{background:#0e1721;border:1px solid var(--line);color:var(--ink);
border-radius:6px;padding:8px 10px;font-size:14px;width:100%}
label{display:block;font-size:12.5px;color:var(--dim);margin:11px 0 4px;
text-transform:uppercase;letter-spacing:.4px}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));
gap:12px}
.note{border-left:3px solid var(--warn);background:#221c10;padding:11px 14px;
border-radius:0 7px 7px 0;font-size:13.5px;margin:12px 0}
.note.bad{border-color:var(--bad);background:#241315}
.note.ok{border-color:var(--good);background:#10231a}
.muted{color:var(--dim)}
"""

NAV = [("/", "Dashboard"), ("/tenders", "Tenders"), ("/review", "Review queue"),
       ("/export", "Export tracker"), ("/lookup", "Molecule lookup"),
       ("/calibrate", "Portal setup"), ("/history", "History"),
       ("/forecast", "Forecast")]


def page(title: str, body: str, active: str = "") -> HTMLResponse:
    nav = "".join(
        f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>'
        for h, t in NAV)
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - Tender Screening</title><style>{CSS}</style></head><body>
<header><h1>Pharmaceutical Tender Screening</h1><nav>{nav}</nav></header>
<main>{body}</main></body></html>"""
    return HTMLResponse(html)


def _code(term) -> str:
    """Config codes may arrive as booleans: YAML 1.1 reads ON as true."""
    raw = term.get("code", "") if isinstance(term, dict) else term
    return "ON" if raw is True else "NO" if raw is False else str(raw or "")


def esc(v) -> str:
    if v is None:
        return '<span class="muted">-</span>'
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\n", "<br>"))


# ------------------------------------------------------------------ auth

@app.middleware("http")
async def gate(request: Request, call_next):
    if APP_TOKEN:
        token = (request.query_params.get("token")
                 or request.cookies.get("app_token"))
        if token != APP_TOKEN:
            if request.url.path == "/login" and request.method == "POST":
                return await call_next(request)
            return HTMLResponse(f"""<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><style>{CSS}</style></head>
<body><main style="max-width:380px;margin-top:14vh">
<div class="panel"><h2>Access token</h2>
<p class="sub">This deployment is token-protected.</p>
<form method="post" action="/login">
<input name="token" type="password" placeholder="token" autofocus>
<button class="btn" style="margin-top:12px;width:100%">Enter</button>
</form></div></main></body></html>""", status_code=401)
    resp = await call_next(request)
    if APP_TOKEN and request.query_params.get("token") == APP_TOKEN:
        resp.set_cookie("app_token", APP_TOKEN, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax")
    return resp


@app.post("/login")
def login(token: str = Form("")):
    resp = RedirectResponse("/", status_code=303)
    if token == APP_TOKEN:
        resp.set_cookie("app_token", token, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax")
    return resp


# ------------------------------------------------------------- dashboard

def reference_status(db) -> dict:
    ref.install(db)
    moh = db.query("SELECT COUNT(*) c FROM ref_moh_products")[0]["c"]
    roots = db.query("SELECT COUNT(*) c FROM ref_roots_products")[0]["c"]
    return {"moh": moh, "roots": roots}


def portal_ready(cfg: dict) -> tuple[bool, str]:
    src = cfg.get("sources", {}).get("moh_kw", {})
    depts = src.get("departments") or []
    if not src.get("department_field"):
        return False, "department form field not set"
    if not depts or any(str(d.get("value", "")).startswith("REPLACE")
                        for d in depts):
        return False, "department option value not set"
    return True, "calibrated"


@app.get("/", response_class=HTMLResponse)
def dashboard():
    cfg = load_config()
    db = get_db()
    rs = reference_status(db)
    ready, why = portal_ready(cfg)

    t = db.query("SELECT COUNT(*) c FROM tenders")[0]["c"]
    onc = db.query("SELECT COUNT(*) c FROM tenders WHERE is_oncology=1")[0]["c"]
    hi = db.query("SELECT COUNT(*) c FROM tenders WHERE priority='High'")[0]["c"]
    rev = db.query("SELECT COUNT(*) c FROM tenders WHERE needs_review=1")[0]["c"]
    items = db.query("SELECT COUNT(*) c FROM tender_items")[0]["c"]
    runs = db.query("SELECT * FROM screening_runs ORDER BY id DESC LIMIT 1")

    cards = "".join(
        f'<div class="card"><div class="n">{n}</div><div class="l">{l}</div></div>'
        for n, l in [(t, "Tenders"), (items, "Line items"), (onc, "Oncology"),
                     (hi, "High priority"), (rev, "Need review"),
                     (rs["moh"], "Registered products"),
                     (rs["roots"], "Roots products")])

    if rs["moh"] == 0:
        ref_note = ('<div class="note bad"><b>Reference data not loaded.</b> '
                    'The last five tracker columns cannot be filled. '
                    '<form method="post" action="/job/load-reference" '
                    'style="display:inline"><button class="btn" '
                    'style="margin-left:8px">Load reference data</button>'
                    '</form></div>')
    else:
        ref_note = (f'<div class="note ok">Reference data loaded: '
                    f'{rs["moh"]:,} MOH registered products and {rs["roots"]} '
                    f'Roots products.</div>')

    if ready:
        portal_note = ('<div class="note ok">Portal calibrated. Screening '
                       'cycles can run.</div>')
        run_btn = ('<form method="post" action="/job/run">'
                   '<button class="btn">Run new screening cycle</button></form>')
    else:
        portal_note = (f'<div class="note"><b>Portal not calibrated yet</b> - '
                       f'{why}. Open <a href="/calibrate">Portal setup</a> and '
                       f'press Probe: this server can reach the MOH site and '
                       f'will read the form fields for you.</div>')
        run_btn = ('<a class="btn" href="/calibrate">Go to portal setup</a>')

    last = ""
    if runs:
        r = runs[0]
        last = f"""<div class="panel"><h2>Last screening cycle</h2>
<p class="sub">#{r['id']} started {r['started_at'][:16].replace('T',' ')} UTC
- {r['status']}</p><table><tr>
<th>Listings</th><th>New</th><th>Updated</th><th>Duplicates</th>
<th>Pharma</th><th>Oncology</th><th>Review</th><th>Errors</th></tr><tr>
<td>{r['listings_reviewed']}</td><td>{r['new_tenders']}</td>
<td>{r['updated_tenders']}</td><td>{r['duplicates_skipped']}</td>
<td>{r['pharma_tenders']}</td><td>{r['oncology_tenders']}</td>
<td>{r['needs_review']}</td><td>{r['errors']}</td></tr></table></div>"""

    job = ""
    if JOB.state != "idle":
        job = ('<div class="panel"><h2>Task</h2>'
               '<div id="jobbox"></div></div>'
               '<script>setInterval(async()=>{'
               'const r=await fetch("/job/status");const j=await r.json();'
               'document.getElementById("jobbox").innerHTML='
               '"<p class=sub>"+j.name+" - "+j.state+"</p><pre class=log>"'
               '+j.lines.join("\\n")+"</pre>";'
               'if(j.state=="done"||j.state=="failed"){}},1500);</script>')

    body = f"""<div class="grid">{cards}</div>{ref_note}{portal_note}
<div class="panel"><h2>Actions</h2><p class="sub">A screening cycle searches
the portal for each code (IN, TB, ON, SP, NU) against the Medical Store
department, reads the tender documents, matches molecules against the price
lists and updates the database.</p>{run_btn}
<a class="btn sec" href="/export">Export tracker</a>
<a class="btn sec" href="/review">Review queue ({rev})</a></div>
{job}{last}"""
    return page("Dashboard", body, "/")


# ------------------------------------------------------------------ jobs

@app.get("/job/status")
def job_status():
    return {"name": JOB.name, "state": JOB.state, "lines": JOB.lines[-200:],
            "result": JOB.result}


@app.post("/job/load-reference")
def job_load_reference():
    def work(job: Job):
        cfg = load_config()
        db = get_db()
        rows = []
        for label, key in (("drug", "drug_price_list"),
                           ("food", "food_price_list")):
            path = cfg["reference"][key]
            job.log(f"parsing {Path(path).name} ...")
            r, unparsed = ref.parse_price_list(path, label)
            job.log(f"  {len(r)} products ({len(unparsed)} lines unparsed)")
            rows.extend(r)
        ref.store_moh(db, rows)
        roots = ref.load_roots(cfg["reference"]["roots_products"])
        ref.store_roots(db, roots)
        job.log(f"Roots sheet: {len(roots)} products")
        idx = ref.load_index(db, cfg["brand_bridge"])
        job.log(f"index ready: {len(idx.moh)} registered products, "
                f"{len(idx.roots)} Roots products, {len(idx.bridge)} aliases")
        return {"moh": len(rows), "roots": len(roots)}

    JOB.start("Load reference data", work)
    return RedirectResponse("/", status_code=303)


@app.post("/job/run")
def job_run():
    def work(job: Job):
        cfg = load_config()
        db = get_db()
        adapter = MohKuwaitAdapter(cfg, logging.getLogger("adapter"))
        job.log(f"portal: {adapter.base_url}")
        stats = Screener(db, adapter, cfg).run()
        job.log(f"cycle finished: new={stats.get('new_tenders')} "
                f"updated={stats.get('updated_tenders')} "
                f"duplicates={stats.get('duplicates_skipped')} "
                f"oncology={stats.get('oncology_tenders')} "
                f"errors={stats.get('errors')}")
        return stats

    if not JOB.start("Screening cycle", work):
        return RedirectResponse("/?busy=1", status_code=303)
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------- calibration

@app.get("/calibrate", response_class=HTMLResponse)
def calibrate_page():
    cfg = load_config()
    src = cfg.get("sources", {}).get("moh_kw", {})
    findings_path = DATA_DIR / "probe_findings.json"
    findings = {}
    if findings_path.exists():
        findings = json.loads(findings_path.read_text(encoding="utf-8"))

    result = ""
    if findings:
        opts = findings.get("department_options") or []
        rows = "".join(
            f"<tr><td><code>{esc(o['value'])}</code></td><td>{esc(o['label'])}</td></tr>"
            for o in opts[:60])
        forms = "".join(
            f"<tr><td>{f['method']}</td><td>{esc(f['action'])[:70]}</td>"
            f"<td>{esc(', '.join(x['name'] for x in f['fields'] if x['name'])[:160])}</td></tr>"
            for f in findings.get("forms", []))
        tables = "".join(
            f"<tr><td>{t['index']}</td><td>{esc(t['id'])}</td><td>{t['rows']}</td>"
            f"<td>{esc(' | '.join(t['headers'][:9]))}</td></tr>"
            for t in findings.get("tables", [])[:8])
        result = f"""<div class="panel"><h2>Probe result</h2>
<p class="sub">HTTP {findings.get('status_code')} -
{esc(findings.get('verdict'))}</p>
<p>Password field present: <b>{findings.get('password_field_present')}</b>
&nbsp;|&nbsp; results in page HTML:
<b>{findings.get('server_rendered_results')}</b></p>
<h3 style="font-size:13px;color:#8ba1b4;margin-top:16px">Department options
found</h3><table><tr><th>value</th><th>label</th></tr>{rows or
'<tr><td colspan=2 class=muted>none detected</td></tr>'}</table>
<h3 style="font-size:13px;color:#8ba1b4;margin-top:16px">Forms</h3>
<table><tr><th>method</th><th>action</th><th>fields</th></tr>{forms}</table>
<h3 style="font-size:13px;color:#8ba1b4;margin-top:16px">Tables</h3>
<table><tr><th>#</th><th>id</th><th>rows</th><th>headers</th></tr>{tables or
'<tr><td colspan=4 class=muted>none</td></tr>'}</table></div>"""

    depts = src.get("departments") or [{}]
    d0 = depts[0] if depts else {}
    codes = {("ON" if k is True else "NO" if k is False else str(k)): v
             for k, v in (cfg.get("therapeutic_area_codes") or {}).items()}

    body = f"""<div class="panel"><h2>Portal setup</h2>
<p class="sub">This server can reach etenders.moh.gov.kw directly. Probe reads
the page and reports the form field names and department values the scraper
needs. Nothing is changed on the portal - it is a read-only page fetch.</p>
<form method="post" action="/calibrate/probe">
<button class="btn">Probe the portal now</button></form></div>
{result}
<div class="panel"><h2>Scraper settings</h2>
<p class="sub">Fill these from the probe result, then save. The scraper types
each code into the tender-title box with department set to Medical Store.</p>
<form method="post" action="/calibrate/save"><div class="row">
<div><label>Portal URL</label><input name="url"
value="{esc(src.get('url',''))}"></div>
<div><label>Mode</label><select name="mode">
<option value="html" {"selected" if src.get('mode')=='html' else ''}>html - table in page</option>
<option value="json" {"selected" if src.get('mode')=='json' else ''}>json - page calls an API</option>
</select></div>
<div><label>Department field name</label><input name="department_field"
value="{esc(src.get('department_field') or '')}"></div>
<div><label>Tender title field name</label><input name="title_field"
value="{esc(src.get('title_field') or '')}"></div>
<div><label>Department label</label><input name="dept_label"
value="{esc(d0.get('label','Medical Store'))}"></div>
<div><label>Department option value</label><input name="dept_value"
value="{esc(d0.get('value',''))}"></div>
<div><label>Results table selector (optional)</label><input
name="table_selector" value="{esc(src.get('table_selector') or '')}"></div>
<div><label>Search codes (comma separated)</label><input name="codes"
value="{esc(','.join(_code(t) for t in src.get('title_search_terms',[])))}"></div>
</div>
<h3 style="font-size:13px;color:#8ba1b4;margin-top:20px">Therapeutic area per
code</h3><p class="sub">These write straight into column D of your tracker.</p>
<div class="row">
{''.join(f'<div><label>{c}</label><input name="area_{c}" value="{esc(codes.get(c,""))}"></div>' for c in ["IN","TB","ON","SP","NU"])}
</div>
<button class="btn" style="margin-top:16px">Save settings</button>
</form></div>"""
    return page("Portal setup", body, "/calibrate")


@app.post("/calibrate/probe")
def calibrate_probe():
    def work(job: Job):
        cfg = load_config()
        url = cfg["sources"]["moh_kw"]["url"]
        job.log(f"fetching {url}")
        findings = probe(url, out_dir=str(DATA_DIR / "probe_output"))
        (DATA_DIR / "probe_findings.json").write_text(
            json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
        job.log(f"HTTP {findings['status_code']} - {findings['verdict']}")
        job.log(f"forms={len(findings['forms'])} "
                f"tables={len(findings['tables'])} "
                f"department options={len(findings['department_options'])}")
        return {"ok": True}

    JOB.start("Probe portal", work)
    return RedirectResponse("/calibrate", status_code=303)


@app.post("/calibrate/save")
def calibrate_save(url: str = Form(""), mode: str = Form("html"),
                   department_field: str = Form(""), title_field: str = Form(""),
                   dept_label: str = Form(""), dept_value: str = Form(""),
                   table_selector: str = Form(""), codes: str = Form(""),
                   area_IN: str = Form(""), area_TB: str = Form(""),
                   area_ON: str = Form(""), area_SP: str = Form(""),
                   area_NU: str = Form("")):
    terms = [{"code": c.strip()} for c in codes.split(",") if c.strip()]
    patch = {
        "sources": {"moh_kw": {
            "url": url.strip(),
            "mode": mode,
            "department_field": department_field.strip() or None,
            "title_field": title_field.strip() or None,
            "departments": [{"label": dept_label.strip() or "Medical Store",
                             "value": dept_value.strip()}],
            "table_selector": table_selector.strip() or None,
            "title_search_terms": terms,
        }},
        "therapeutic_area_codes": {k: v.strip() for k, v in
                                   [("IN", area_IN), ("TB", area_TB),
                                    ("ON", area_ON), ("SP", area_SP),
                                    ("NU", area_NU)] if v.strip()},
    }
    save_portal_config(patch)
    return RedirectResponse("/calibrate", status_code=303)


# ---------------------------------------------------------------- tenders

@app.get("/tenders", response_class=HTMLResponse)
def tenders(filter: str = "all"):
    db = get_db()
    where = {"oncology": "WHERE is_oncology=1",
             "high": "WHERE priority='High'",
             "new": "WHERE first_seen >= (SELECT started_at FROM screening_runs "
                    "ORDER BY id DESC LIMIT 1)"}.get(filter, "")
    rows = db.query(f"SELECT * FROM tenders {where} ORDER BY "
                    "submission_deadline IS NULL, submission_deadline LIMIT 400")

    tabs = "".join(
        f'<a class="btn {"" if filter==k else "sec"}" href="/tenders?filter={k}">{t}</a>'
        for k, t in [("all", "All"), ("new", "New this cycle"),
                     ("oncology", "Oncology"), ("high", "High priority")])

    body_rows = ""
    for r in rows:
        tags = ""
        if r["is_oncology"]:
            tags += '<span class="tag t-onc">ONC</span> '
        if r["priority"] == "High":
            tags += '<span class="tag t-high">HIGH</span> '
        if r["needs_review"]:
            tags += '<span class="tag t-rev">REVIEW</span>'
        n = db.query("SELECT COUNT(*) c FROM tender_items WHERE tender_id=?",
                     (r["id"],))[0]["c"]
        body_rows += (f"<tr><td>{esc(r['tender_number'])}</td>"
                      f"<td>{esc(r['submission_deadline'])}</td>"
                      f"<td>{esc(r['search_code'])}</td>"
                      f"<td>{esc((r['tender_title'] or '')[:95])}</td>"
                      f"<td>{esc(r['therapeutic_area'])}</td>"
                      f"<td>{n}</td><td>{tags}</td></tr>")

    body = f"""<div class="panel"><h2>Tenders</h2>
<p class="sub">{len(rows)} record(s)</p>{tabs}</div>
<div class="panel"><table><tr><th>Tender no.</th><th>Closing</th><th>Code</th>
<th>Title</th><th>Area</th><th>Items</th><th></th></tr>
{body_rows or '<tr><td colspan=7 class=muted>No tenders yet - run a screening cycle.</td></tr>'}
</table></div>"""
    return page("Tenders", body, "/tenders")


@app.get("/review", response_class=HTMLResponse)
def review():
    db = get_db()
    t = db.query("SELECT * FROM tenders WHERE needs_review=1 "
                 "ORDER BY last_seen DESC LIMIT 200")
    i = db.query("""SELECT i.product_name, i.review_reason, i.ref_match_method,
                    t.tender_number FROM tender_items i
                    JOIN tenders t ON t.id=i.tender_id
                    WHERE i.needs_review=1 LIMIT 300""")
    trows = "".join(f"<tr><td>{esc(r['tender_number'])}</td>"
                    f"<td>{esc((r['tender_title'] or '')[:70])}</td>"
                    f"<td>{esc(r['review_reason'])}</td></tr>" for r in t)
    irows = "".join(f"<tr><td>{esc(r['tender_number'])}</td>"
                    f"<td>{esc(r['product_name'][:60])}</td>"
                    f"<td>{esc(r['ref_match_method'])}</td>"
                    f"<td>{esc(r['review_reason'])}</td></tr>" for r in i)
    body = f"""<div class="panel"><h2>Manual review queue</h2>
<p class="sub">Nothing is discarded silently. Anything uncertain lands here
with the reason attached.</p></div>
<div class="panel"><h2>Tenders ({len(t)})</h2><table>
<tr><th>Tender no.</th><th>Title</th><th>Reason</th></tr>
{trows or '<tr><td colspan=3 class=muted>Empty</td></tr>'}</table></div>
<div class="panel"><h2>Line items ({len(i)})</h2><table>
<tr><th>Tender no.</th><th>Item</th><th>Match</th><th>Reason</th></tr>
{irows or '<tr><td colspan=4 class=muted>Empty</td></tr>'}</table></div>"""
    return page("Review queue", body, "/review")


# ----------------------------------------------------------------- export

@app.get("/export", response_class=HTMLResponse)
def export_page():
    cfg = load_config()
    db = get_db()
    n = db.query("SELECT COUNT(*) c FROM tender_items")[0]["c"]
    layout = trk.detect_layout(cfg["tracker_template"])
    cols = "".join(f"<tr><td><code>{c}</code></td><td>{esc(layout['headers'][c])}</td>"
                   f"<td>{esc(f)}</td></tr>"
                   for c, f in layout["columns"].items())
    body = f"""<div class="panel"><h2>Export tracker</h2>
<p class="sub">Fills a copy of your Kuwait_Tenders_Tracker template, one row
per tender line item. {n} item row(s) available.</p>
<a class="btn" href="/export/file">Download all</a>
<a class="btn sec" href="/export/file?scope=oncology">Oncology only</a>
<a class="btn sec" href="/export/file?scope=high">High priority only</a>
</div>
<div class="panel"><h2>Detected column mapping</h2>
<p class="sub">Read from your template's header row - nothing is hard-coded.</p>
<table><tr><th>Col</th><th>Header</th><th>Filled from</th></tr>{cols}</table>
</div>
<div class="panel"><h2>Cell shading in the output</h2><table>
<tr><th>Colour</th><th>Meaning</th></tr>
<tr><td>none</td><td>exact molecule match</td></tr>
<tr><td>amber</td><td>fuzzy match - transliteration or brand bridge; verify</td></tr>
<tr><td>pink</td><td>no match in the price lists or the Roots sheet</td></tr>
</table></div>"""
    return page("Export", body, "/export")


@app.get("/export/file")
def export_file(scope: str = "all"):
    cfg = load_config()
    db = get_db()
    where = {"oncology": "t.is_oncology=1", "high": "t.priority='High'"}.get(scope, "")
    out = Path(cfg["export_dir"]) / f"Kuwait_Tenders_Tracker_{date.today()}.xlsx"
    trk.export_tracker(db, cfg["tracker_template"], out,
                       area_labels=cfg.get("therapeutic_area_codes", {}),
                       where=where)
    data = out.read_bytes()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{out.name}"'})


# ----------------------------------------------------------------- lookup

@app.get("/lookup", response_class=HTMLResponse)
def lookup(q: str = ""):
    cfg = load_config()
    db = get_db()
    out = ""
    if q.strip():
        idx = ref.load_index(db, cfg["brand_bridge"])
        moh, roots = idx.match_moh(q), idx.match_roots(q)
        mols = ", ".join(ref.candidate_molecules(q)) or "none detected"
        mrows = "".join(f"<tr><td>{esc(p)}</td></tr>" for p in moh["products"])
        crows = "".join(f"<tr><td>{esc(c)}</td></tr>" for c in moh["companies"])
        rrows = "".join(f"<tr><td>{esc(p)}</td></tr>" for p in roots["products"])
        out = f"""<div class="panel"><h2>Result</h2>
<p class="sub">molecules extracted: {esc(mols)}</p>
<div class="row">
<div><h3 style="font-size:13px;color:#8ba1b4">Registered products
[{moh['method'] or 'no match'}, {moh['count']} hits]</h3>
<table>{mrows or '<tr><td class=muted>none</td></tr>'}</table></div>
<div><h3 style="font-size:13px;color:#8ba1b4">Company</h3>
<table>{crows or '<tr><td class=muted>none</td></tr>'}</table></div>
<div><h3 style="font-size:13px;color:#8ba1b4">Roots
[{roots['method'] or 'no match'}, {roots['count']} hits]</h3>
<table>{rrows or '<tr><td class=muted>none</td></tr>'}</table>
<p class="sub" style="margin-top:8px">principal:
{esc(', '.join(roots['principals']) or '-')}<br>status:
{esc(', '.join(roots['statuses']) or '-')}</p></div></div></div>"""

    body = f"""<div class="panel"><h2>Molecule lookup</h2>
<p class="sub">Search the MOH price lists and the Roots sheet the same way the
screening pipeline does - useful for checking a tender line by hand.</p>
<form method="get"><input name="q" value="{esc(q) if q else ''}"
placeholder="e.g. Carboplatin 450mg vial"><button class="btn"
style="margin-top:12px">Search</button></form></div>{out}"""
    return page("Lookup", body, "/lookup")


# ---------------------------------------------------------------- history

@app.get("/history", response_class=HTMLResponse)
def history():
    db = get_db()
    runs = db.query("SELECT * FROM screening_runs ORDER BY id DESC LIMIT 50")
    errs = db.query("SELECT * FROM run_errors ORDER BY id DESC LIMIT 60")
    rrows = "".join(
        f"<tr><td>{r['id']}</td><td>{r['started_at'][:16].replace('T',' ')}</td>"
        f"<td>{r['listings_reviewed']}</td><td>{r['new_tenders']}</td>"
        f"<td>{r['updated_tenders']}</td><td>{r['duplicates_skipped']}</td>"
        f"<td>{r['oncology_tenders']}</td><td>{r['needs_review']}</td>"
        f"<td>{r['errors']}</td><td>{esc(r['status'])}</td></tr>" for r in runs)
    erows = "".join(
        f"<tr><td>{e['run_id']}</td><td>{esc(e['stage'])}</td>"
        f"<td>{esc((e['target'] or '')[:50])}</td>"
        f"<td>{esc((e['message'] or '')[:110])}</td></tr>" for e in errs)
    body = f"""<div class="panel"><h2>Screening history</h2><table>
<tr><th>Run</th><th>Started (UTC)</th><th>Seen</th><th>New</th><th>Upd</th>
<th>Dup</th><th>Onc</th><th>Review</th><th>Err</th><th>Status</th></tr>
{rrows or '<tr><td colspan=10 class=muted>No cycles yet</td></tr>'}</table></div>
<div class="panel"><h2>Errors</h2><p class="sub">A failure in one tender or one
document never stops the cycle.</p><table>
<tr><th>Run</th><th>Stage</th><th>Target</th><th>Message</th></tr>
{erows or '<tr><td colspan=4 class=muted>None</td></tr>'}</table></div>"""
    return page("History", body, "/history")


@app.get("/forecast", response_class=HTMLResponse)
def forecast():
    db = get_db()
    res = run_forecasting(db, load_config(), save=False)
    blocks = ""
    for r in res["products"] + res["authorities"]:
        if not r["forecasts"] and not r["gaps"]:
            continue
        f = "".join(f"<tr><td>{esc(x['metric'])}</td><td>{esc(x['value'])}</td>"
                    f"<td>{esc(x['confidence'])}</td><td>{esc(x['basis'])}</td></tr>"
                    for x in r["forecasts"])
        g = "".join(f'<li class="muted">{esc(x)}</li>' for x in r["gaps"])
        blocks += f"""<div class="panel"><h2>{esc(r['subject'])}</h2>
<p class="sub">{r['subject_type']}</p>
{'<table><tr><th>Metric</th><th>Value</th><th>Confidence</th><th>Evidence</th></tr>' + f + '</table>' if f else ''}
{'<ul>' + g + '</ul>' if g else ''}</div>"""
    head = f"""<div class="panel"><h2>Forecasting</h2>
<p class="sub">{res['forecasts_produced']} forecast(s) from
{res['subjects_examined']} subject(s). Forecasts need at least 3 tenders for
the same product or authority; below that the engine reports what it still
needs instead of extrapolating.</p></div>"""
    return page("Forecast", head + blocks, "/forecast")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------- startup

@app.on_event("startup")
def startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    db = Database(cfg["database"])
    rs = reference_status(db)
    if rs["moh"] == 0:
        log.info("no reference data - loading price lists in background")
        job_load_reference()
    log.info("ready. data dir=%s token=%s", DATA_DIR,
             "set" if APP_TOKEN else "NOT SET (open access)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
