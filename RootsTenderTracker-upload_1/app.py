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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pharmatender.normalize import NA as NA_TEXT
from pharmatender import reference as ref
from pharmatender import reporting
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
--accent:#5aa3a0;--good:#3fb950;--warn:#d29922;--bad:#f85149;--brand:#4d8b89}
body{margin:0;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--panel);border-bottom:1px solid var(--line);
padding:14px 22px;display:flex;align-items:center;gap:22px;flex-wrap:wrap;
position:sticky;top:0;z-index:5}
header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.2px;
color:var(--dim)}
header .brand{display:flex;align-items:center;gap:14px}
header .brand img{height:34px;display:block}
header .brand .bar{width:1px;height:26px;background:var(--line)}
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
tr.hit td{background:#0f2418}
.filters{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-top:14px}
.filters > div{min-width:172px}
.filters label{margin:0 0 4px}
.filters .acts{display:flex;gap:8px;align-items:center;min-width:0}
.filters .acts .btn{margin:0}
tr.dim td{color:#7d8d9c}
table td{vertical-align:top}
"""

NAV = [("/", "Dashboard"), ("/tracker", "Tracker"), ("/reports", "Reports"),
       ("/tenders", "Tenders"), ("/review", "Review queue"),
       ("/forecast", "Forecast"), ("/lookup", "Molecule lookup"),
       ("/history", "History"), ("/calibrate", "Portal setup")]


def page(title: str, body: str, active: str = "") -> HTMLResponse:
    nav = "".join(
        f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>'
        for h, t in NAV)
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - Tender Screening</title><style>{CSS}</style></head><body>
<header><div class="brand">
<img src="/static/roots-logo.png" alt="Roots Pharmaceutical">
<span class="bar"></span><h1>Tender Screening &amp; Tracking</h1></div>
<nav>{nav}</nav></header>
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


def esc_attr(v) -> str:
    """Escaping for a value that lands inside an HTML attribute."""
    return (str(v or "").replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


# ------------------------------------------------------------------ auth

@app.middleware("http")
async def gate(request: Request, call_next):
    if (APP_TOKEN and request.url.path != "/healthz"
            and not request.url.path.startswith("/static/")):
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
<a class="btn sec" href="/tracker">Open the tracker</a>
<a class="btn sec" href="/reports">Reports</a>
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

# ------------------------------------------------- shared row filtering

MATCH_STATUSES = [
    ("all", "All line items"),
    ("roots", "Roots SKU match"),
    ("moh", "MOH registered only"),
    ("nomatch", "No reference match"),
    ("review", "Needs review"),
]

CLOSING_PRESETS = [
    ("open", "Open (not yet closed)"),
    ("30", "Closing within 30 days"),
    ("60", "Closing within 60 days"),
    ("closed", "Already closed"),
    ("", "Any closing date"),
]


def _display_area(rec: dict, area_labels: dict) -> str:
    """Column D of the tracker: the code's label, else the classifier's area."""
    return (area_labels.get(rec.get("search_code"))
            or rec.get("item_area") or rec.get("tender_area") or NA_TEXT)


def _area_labels(cfg: dict) -> dict:
    return {("ON" if k is True else "NO" if k is False else str(k)): v
            for k, v in (cfg.get("therapeutic_area_codes") or {}).items()}


def _closing_window(closing: str) -> tuple[str, str]:
    """(from, to) ISO bounds for a closing-date preset. Empty string = open end."""
    today = date.today()
    if closing == "open":
        return today.isoformat(), ""
    if closing == "closed":
        return "", today.isoformat()
    if closing in ("30", "60"):
        return today.isoformat(), (today + timedelta(days=int(closing))).isoformat()
    return "", ""


def tracker_rows(db, cfg: dict, status: str = "all", area: str = "",
                 closing: str = "open", q: str = "",
                 date_from: str = "", date_to: str = "") -> list[dict]:
    """Every tracker row passing the current filters, in display order.

    Filtering happens here rather than in SQL because the therapeutic area
    shown in column D is derived (search code first, classifier second), so
    the screen, the counts and the export all have to agree on one rule.
    """
    labels = _area_labels(cfg)
    rows = trk.fetch_rows(db)
    lo, hi = _closing_window(closing)
    lo, hi = date_from or lo, date_to or hi
    q = (q or "").strip().lower()

    out = []
    for r in rows:
        r = dict(r)
        r["_area"] = _display_area(r, labels)
        roots = (r.get("roots_product") or "").strip()
        moh = (r.get("registered_products") or "").strip()
        if status == "roots" and not roots:
            continue
        if status == "moh" and (roots or not moh):
            continue
        if status == "nomatch" and (roots or moh):
            continue
        if status == "review" and not r.get("needs_review"):
            continue
        if area and r["_area"] != area:
            continue
        deadline = r.get("submission_deadline") or ""
        if lo and (not deadline or deadline < lo):
            continue
        if hi and (not deadline or deadline > hi):
            continue
        if q:
            hay = " ".join(str(r.get(k) or "") for k in
                           ("tender_number", "product_name", "roots_product",
                            "roots_principal", "registered_products",
                            "registered_companies")).lower()
            if q not in hay:
                continue
        out.append(r)
    return out


def _area_options(db, cfg: dict, selected: str) -> str:
    labels = _area_labels(cfg)
    areas = sorted({_display_area(dict(r), labels) for r in trk.fetch_rows(db)})
    opts = '<option value="">All therapeutic areas</option>'
    for a in areas:
        sel = " selected" if a == selected else ""
        opts += f'<option value="{esc_attr(a)}"{sel}>{esc(a)}</option>'
    return opts


def filter_bar(action: str, db, cfg: dict, status: str, area: str,
               closing: str, q: str, with_status: bool = True,
               download: str = "") -> str:
    """The one filter control used across Tracker, Tenders and Review."""
    status_sel = ""
    if with_status:
        opts = "".join(
            f'<option value="{k}"{" selected" if k == status else ""}>{lab}</option>'
            for k, lab in MATCH_STATUSES)
        status_sel = f'<div><label>Match status</label><select name="status">{opts}</select></div>'
    closing_opts = "".join(
        f'<option value="{k}"{" selected" if k == closing else ""}>{lab}</option>'
        for k, lab in CLOSING_PRESETS)
    dl = (f'<a class="btn sec" href="{download}">Download Excel</a>'
          if download else "")
    return f"""<form method="get" action="{action}" class="filters">
{status_sel}
<div><label>Therapeutic area</label>
<select name="area">{_area_options(db, cfg, area)}</select></div>
<div><label>Closing date</label><select name="closing">{closing_opts}</select></div>
<div><label>Search</label>
<input name="q" value="{esc_attr(q)}" placeholder="molecule, tender no. or SKU"></div>
<div class="acts"><button class="btn">Apply</button>
<a class="btn sec" href="{action}">Reset</a>{dl}</div>
</form>"""


@app.get("/tracker", response_class=HTMLResponse)
def tracker_view(status: str = "all", area: str = "", closing: str = "open",
                 q: str = ""):
    """The tracker itself, on screen: one row per tender line item, in the
    same eleven columns as the Excel template."""
    db, cfg = get_db(), load_config()
    rows = tracker_rows(db, cfg, status, area, closing, q)

    qs = urlencode({"status": status, "area": area, "closing": closing, "q": q})
    bar = filter_bar("/tracker", db, cfg, status, area, closing, q,
                     download=f"/tracker/export?{qs}")

    today = date.today().isoformat()
    body_rows = ""
    for r in rows[:800]:
        roots = trk._join(r.get("roots_product"))
        moh = trk._join(r.get("registered_products"))
        method = r.get("ref_match_method")
        if roots != NA_TEXT:
            cls = "hit"
            tag = ('<span class="tag t-ok">SKU</span>' if method == "exact"
                   else '<span class="tag t-rev">SKU?</span>')
        elif moh != NA_TEXT:
            cls, tag = "", '<span class="tag t-na">REG</span>'
        else:
            cls, tag = "dim", ""
        deadline = r.get("submission_deadline") or ""
        closing_cell = esc(deadline)
        if deadline and deadline < today:
            closing_cell = f'<span class="muted">{esc(deadline)} (closed)</span>'
        body_rows += (
            f'<tr class="{cls}"><td>{esc(r.get("tender_number"))} {tag}</td>'
            f'<td>{closing_cell}</td>'
            f'<td>{esc((r.get("product_name") or "")[:150])}</td>'
            f'<td>{esc(r["_area"])}</td><td>{esc(r.get("unit"))}</td>'
            f'<td>{esc(r.get("quantity"))}</td>'
            f'<td>{esc(moh)}</td>'
            f'<td>{esc(trk._join(r.get("registered_companies")))}</td>'
            f'<td><b>{esc(roots)}</b></td>'
            f'<td>{esc(trk._join(r.get("roots_principal")))}</td>'
            f'<td>{esc(trk._join(r.get("roots_status")))}</td></tr>')

    more = ("" if len(rows) <= 800 else
            f'<p class="sub">Showing the first 800 of {len(rows)}.</p>')
    body = f"""<div class="panel"><h2>Tracker</h2>
<p class="sub">One row per tender line item, in the eleven columns of your
template. Columns A-F come from the portal and its tender documents; G-H from
the MOH price lists; I-K from the Roots registered-products sheet. The
download gives you exactly the rows shown below.</p>
{bar}</div>
<div class="panel"><p class="sub">{len(rows)} row(s)</p>{more}
<div style="overflow-x:auto"><table>
<tr><th>Tender no.</th><th>Closing Date</th><th>Item Description</th>
<th>Theraputic Area</th><th>Unit</th><th>Tender QTY</th>
<th>Registered products</th><th>Company name</th>
<th>Roots product name</th><th>Principle name</th><th>Registration status</th></tr>
{body_rows or '<tr><td colspan=11 class=muted>Nothing matches these filters. Widen them, or run a screening cycle from the dashboard.</td></tr>'}
</table></div></div>"""
    return page("Tracker", body, "/tracker")


@app.get("/tracker/export")
def tracker_export(status: str = "all", area: str = "", closing: str = "open",
                   q: str = ""):
    """The rows currently on screen, written into your tracker template."""
    db, cfg = get_db(), load_config()
    rows = tracker_rows(db, cfg, status, area, closing, q)
    label = dict(MATCH_STATUSES).get(status, "tracker")
    data = trk.export_rows_to_bytes(rows, cfg["tracker_template"],
                                    _area_labels(cfg))
    name = (f"Roots_Tender_Tracker_{status}"
            f"{'_' + area.replace(' ', '-') if area else ''}"
            f"_{date.today().isoformat()}.xlsx")
    log.info("tracker export: %d row(s), filter=%s", len(rows), label)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})



def reports_data(db, cfg: dict, area: str = "", closing: str = "open",
                 q: str = "") -> dict:
    """Everything both the Reports page and its workbook are built from.

    One source for both, so a downloaded report always says the same thing as
    the screen it was downloaded from.
    """
    today = date.today().isoformat()
    horizon = (date.today() + timedelta(days=30)).isoformat()
    scoped = tracker_rows(db, cfg, "all", area, closing, q)
    # Totals describe the whole database; the tables below follow the filters.
    everything = tracker_rows(db, cfg, "all", "", "", "")

    def count(rows, pred):
        return sum(1 for r in rows if pred(r))

    has_roots = lambda r: bool((r.get("roots_product") or "").strip())   # noqa: E731
    has_moh = lambda r: bool((r.get("registered_products") or "").strip())  # noqa: E731
    is_open = lambda r: (r.get("submission_deadline") or "") >= today    # noqa: E731

    tender_ids = {r.get("tender_number") for r in scoped}
    open_ids = {r.get("tender_number") for r in scoped if is_open(r)}

    opportunities = [
        [r.get("tender_number"), r.get("submission_deadline"),
         (r.get("product_name") or "")[:300], r.get("unit"), r.get("quantity"),
         trk._join(r.get("roots_product")), trk._join(r.get("roots_principal")),
         trk._join(r.get("roots_status")),
         "exact" if r.get("ref_match_method") == "exact" else "verify"]
        for r in sorted(scoped, key=lambda x: (x.get("submission_deadline") or "",
                                               x.get("tender_number") or ""))
        if has_roots(r) and is_open(r)
    ]

    # One row per tender, and the area shown is the same derived value the
    # tracker prints, so the two sheets never disagree about a tender's area.
    titles = {r["tender_number"]: r for r in db.query(
        "SELECT tender_number, tender_title, priority FROM tenders")}
    closing_soon, seen_soon = [], set()
    for r in sorted(scoped, key=lambda x: (x.get("submission_deadline") or "")):
        num = r.get("tender_number")
        deadline = r.get("submission_deadline") or ""
        if num in seen_soon or not (today <= deadline <= horizon):
            continue
        seen_soon.add(num)
        meta = titles.get(num, {})
        closing_soon.append([num, deadline, r["_area"],
                             (meta["tender_title"] if meta else "" or "")[:200],
                             meta["priority"] if meta else ""])

    principals: dict[str, int] = {}
    areas: dict[str, int] = {}
    seen_tenders: set = set()
    for r in scoped:
        for pr in str(r.get("roots_principal") or "").split("\n"):
            pr = pr.strip()
            if pr:
                principals[pr] = principals.get(pr, 0) + 1
        key = r.get("tender_number")
        if key not in seen_tenders:
            seen_tenders.add(key)
            areas[r["_area"]] = areas.get(r["_area"], 0) + 1

    tracker = [
        [r.get("tender_number"), r.get("submission_deadline"),
         (r.get("product_name") or "")[:300], r["_area"], r.get("unit"),
         r.get("quantity"), trk._join(r.get("registered_products")),
         trk._join(r.get("registered_companies")),
         trk._join(r.get("roots_product")), trk._join(r.get("roots_principal")),
         trk._join(r.get("roots_status"))]
        for r in scoped]

    bits = [dict(CLOSING_PRESETS).get(closing, "Any closing date")]
    if area:
        bits.append(area)
    if q:
        bits.append(f'search "{q}"')
    return {
        "as_of": today,
        "scope": " | ".join(bits),
        "tenders": len(tender_ids),
        "items": len(scoped),
        "open_tenders": len(open_ids),
        "roots_matches": count(scoped, has_roots),
        "moh_matches": count(scoped, has_moh),
        "open_roots": count(scoped, lambda r: has_roots(r) and is_open(r)),
        "needs_review": count(scoped, lambda r: r.get("needs_review")),
        "oncology": count(scoped, lambda r: r.get("is_oncology")),
        "total_items": len(everything),
        "opportunities": opportunities,
        "closing_soon": closing_soon,
        "tracker": tracker,
        "by_principal": sorted(principals.items(), key=lambda kv: -kv[1]),
        "by_area": sorted(areas.items(), key=lambda kv: -kv[1]),
    }


@app.get("/reports", response_class=HTMLResponse)
def reports(area: str = "", closing: str = "open", q: str = ""):
    """Where the screening result turns into something to act on: which open
    tenders Roots can bid, who the principal is, and what closes soonest."""
    db, cfg = get_db(), load_config()
    d = reports_data(db, cfg, area, closing, q)
    qs = urlencode({"area": area, "closing": closing, "q": q})
    bar = filter_bar("/reports", db, cfg, "all", area, closing, q,
                     with_status=False, download=f"/reports/export?{qs}")

    cards = "".join(
        f'<div class="card"><div class="n">{v}</div><div class="k">{k}</div></div>'
        for k, v in [("Tenders", d["tenders"]), ("Line items", d["items"]),
                     ("Open tenders", d["open_tenders"]),
                     ("Roots SKU matches", d["roots_matches"]),
                     ("Open + Roots match", d["open_roots"]),
                     ("MOH registered", d["moh_matches"])])

    opp_rows = "".join(
        f"<tr><td>{esc(o[0])}</td><td>{esc(o[1])}</td>"
        f"<td>{esc(str(o[2])[:90])}</td><td>{esc(o[3])}</td><td>{esc(o[4])}</td>"
        f"<td><b>{esc(o[5])}</b></td><td>{esc(o[6])}</td><td>{esc(o[7])}</td>"
        f"""<td>{'<span class="tag t-ok">exact</span>' if o[8] == 'exact'
                 else '<span class="tag t-rev">verify</span>'}</td></tr>"""
        for o in d["opportunities"])
    soon_rows = "".join(
        f"<tr><td>{esc(c[0])}</td><td>{esc(c[1])}</td><td>{esc(c[2])}</td>"
        f"<td>{esc(str(c[3])[:80])}</td><td>{esc(c[4])}</td></tr>"
        for c in d["closing_soon"])
    prin_rows = "".join(
        f'<tr><td>{esc(k)}</td><td>{v}</td>'
        f'<td><a href="/forecast?q={esc_attr(k)}">forecast</a></td></tr>'
        for k, v in d["by_principal"])
    area_rows = "".join(
        f'<tr><td>{esc(k)}</td><td>{v}</td>'
        f'<td><a href="/tracker?area={esc_attr(k)}&closing=">open</a></td></tr>'
        for k, v in d["by_area"])

    body = f"""<div class="panel"><h2>Reports</h2>
<p class="sub">Screening result as of {d['as_of']}. The download is a
formatted workbook of exactly this scope.</p>{bar}
<div class="cards" style="margin-top:16px">{cards}</div></div>

<div class="panel"><h2>Bidding opportunities</h2>
<p class="sub">Open tenders where the screened molecule matches a Roots
registered product. "verify" marks a fuzzy match - check it before relying
on it.</p>
<div style="overflow-x:auto"><table>
<tr><th>Tender no.</th><th>Closing</th><th>Item</th><th>Unit</th><th>Qty</th>
<th>Roots product</th><th>Principal</th><th>Status</th><th>Match</th></tr>
{opp_rows or '<tr><td colspan=9 class=muted>No open tender in this scope matches a Roots SKU.</td></tr>'}
</table></div></div>

<div class="panel"><h2>Closing within 30 days</h2>
<div style="overflow-x:auto"><table>
<tr><th>Tender no.</th><th>Closing</th><th>Area</th><th>Title</th><th>Priority</th></tr>
{soon_rows or '<tr><td colspan=5 class=muted>Nothing closing in the next 30 days.</td></tr>'}
</table></div></div>

<div class="row">
<div class="panel"><h2>Roots SKU hits by principal</h2><table>
<tr><th>Principal</th><th>Line items</th><th></th></tr>
{prin_rows or '<tr><td colspan=3 class=muted>No Roots matches in this scope.</td></tr>'}</table></div>
<div class="panel"><h2>Tenders by therapeutic area</h2><table>
<tr><th>Area</th><th>Tenders</th><th></th></tr>
{area_rows or '<tr><td colspan=3 class=muted>No tenders in this scope.</td></tr>'}</table></div>
</div>"""
    return page("Reports", body, "/reports")


@app.get("/reports/export")
def reports_export(area: str = "", closing: str = "open", q: str = ""):
    db, cfg = get_db(), load_config()
    d = reports_data(db, cfg, area, closing, q)
    data = reporting.build_report(d, BASE / "static" / "roots-logo.png")
    name = f"Roots_Tender_Report_{date.today().isoformat()}.xlsx"
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})



@app.get("/tenders", response_class=HTMLResponse)
def tenders(area: str = "", closing: str = "open", q: str = ""):
    """Tender-level view: one row per tender, filtered the same way as
    everything else."""
    db, cfg = get_db(), load_config()
    rows = tracker_rows(db, cfg, "all", area, closing, q)
    meta = {r["tender_number"]: r for r in db.query(
        "SELECT tender_number, tender_title, priority, is_oncology, "
        "needs_review FROM tenders")}

    grouped: dict[str, dict] = {}
    for r in rows:
        num = r.get("tender_number")
        g = grouped.setdefault(num, {"area": r["_area"], "items": 0,
                                     "roots": 0,
                                     "closing": r.get("submission_deadline")})
        g["items"] += 1
        if (r.get("roots_product") or "").strip():
            g["roots"] += 1

    bar = filter_bar("/tenders", db, cfg, "all", area, closing, q,
                     with_status=False)
    body_rows = ""
    for num, g in sorted(grouped.items(),
                         key=lambda kv: (kv[1]["closing"] or "", kv[0] or "")):
        m = meta.get(num)
        tags = ""
        if m and m["is_oncology"]:
            tags += '<span class="tag t-onc">ONC</span> '
        if m and m["priority"] == "High":
            tags += '<span class="tag t-high">HIGH</span> '
        if m and m["needs_review"]:
            tags += '<span class="tag t-rev">REVIEW</span> '
        if g["roots"]:
            tags += f'<span class="tag t-ok">{g["roots"]} SKU</span>'
        body_rows += (
            f"<tr><td>{esc(num)}</td><td>{esc(g['closing'])}</td>"
            f"<td>{esc(((m['tender_title'] if m else '') or '')[:95])}</td>"
            f"<td>{esc(g['area'])}</td><td>{g['items']}</td><td>{tags}</td>"
            f'<td><a href="/tracker?q={esc_attr(num)}&closing=">items</a></td></tr>')

    body = f"""<div class="panel"><h2>Tenders</h2>
<p class="sub">{len(grouped)} tender(s) in scope. Open a tender's items to see
the tracker rows behind it.</p>{bar}</div>
<div class="panel"><table><tr><th>Tender no.</th><th>Closing</th>
<th>Title</th><th>Therapeutic area</th><th>Items</th><th></th><th></th></tr>
{body_rows or '<tr><td colspan=7 class=muted>Nothing matches these filters.</td></tr>'}
</table></div>"""
    return page("Tenders", body, "/tenders")


@app.post("/review/approve")
def review_approve(request: Request, tender_ids: list[str] = Form(default=[]),
                   item_ids: list[str] = Form(default=[]),
                   back: str = Form("/review")):
    """Clear the review flag on what the user has approved.

    Approving is the whole point of the queue: the row leaves it and shows up
    in Tenders without the amber mark, so the queue only ever holds what is
    still outstanding.
    """
    db = get_db()
    t_ids = [int(i) for i in tender_ids if str(i).isdigit()]
    i_ids = [int(i) for i in item_ids if str(i).isdigit()]

    # Tenders owning an approved item, so a tender whose last flagged item was
    # just cleared loses its own mark too.
    touched = set(t_ids)
    if i_ids:
        placeholders = ",".join("?" * len(i_ids))
        touched.update(r["tender_id"] for r in db.query(
            f"SELECT DISTINCT tender_id FROM tender_items WHERE id IN ({placeholders})",
            tuple(i_ids)))

    with db.tx() as cur:
        for tid in t_ids:
            cur.execute("UPDATE tenders SET needs_review=0, review_reason=NULL "
                        "WHERE id=?", (tid,))
        for iid in i_ids:
            cur.execute("UPDATE tender_items SET needs_review=0, "
                        "review_reason=NULL WHERE id=?", (iid,))
        for tid in touched:
            cur.execute("""UPDATE tenders SET needs_review=0 WHERE id=? AND NOT
                           EXISTS (SELECT 1 FROM tender_items
                                   WHERE tender_id=? AND needs_review=1)""",
                        (tid, tid))

    log.info("approved %d tender(s) and %d item(s) out of review",
             len(t_ids), len(i_ids))
    return RedirectResponse(back or "/review", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review(area: str = "", closing: str = "", q: str = ""):
    db, cfg = get_db(), load_config()
    labels = _area_labels(cfg)
    lo, hi = _closing_window(closing)
    ql = (q or "").strip().lower()

    def keep(rec: dict) -> bool:
        a = _display_area(rec, labels)
        if area and a != area:
            return False
        d = rec.get("submission_deadline") or ""
        if lo and (not d or d < lo):
            return False
        if hi and (not d or d > hi):
            return False
        if ql:
            hay = " ".join(str(rec.get(k) or "") for k in
                           ("tender_number", "product_name", "tender_title",
                            "review_reason")).lower()
            if ql not in hay:
                return False
        return True

    tenders_q = [dict(r) for r in db.query(
        """SELECT id, tender_number, tender_title, review_reason,
                  submission_deadline, therapeutic_area AS tender_area,
                  search_code FROM tenders WHERE needs_review=1
           ORDER BY submission_deadline IS NULL, submission_deadline LIMIT 300""")]
    items_q = [dict(r) for r in db.query(
        """SELECT i.id, i.product_name, i.review_reason, i.ref_match_method,
                  i.therapeutic_area AS item_area, i.search_code,
                  t.tender_number, t.submission_deadline
           FROM tender_items i JOIN tenders t ON t.id=i.tender_id
           WHERE i.needs_review=1
           ORDER BY t.submission_deadline IS NULL, t.submission_deadline
           LIMIT 400""")]
    tenders_q = [r for r in tenders_q if keep(r)]
    items_q = [r for r in items_q if keep(r)]

    bar = filter_bar("/review", db, cfg, "all", area, closing, q,
                     with_status=False)
    back = esc_attr("/review?" + urlencode({"area": area, "closing": closing,
                                            "q": q}))

    trows = "".join(
        f'<tr><td><input type="checkbox" name="tender_ids" value="{r["id"]}" '
        f'style="width:auto"></td><td>{esc(r["tender_number"])}</td>'
        f'<td>{esc(r.get("submission_deadline"))}</td>'
        f'<td>{esc((r.get("tender_title") or "")[:70])}</td>'
        f'<td>{esc(_display_area(r, labels))}</td>'
        f'<td>{esc(r.get("review_reason"))}</td></tr>' for r in tenders_q)
    irows = "".join(
        f'<tr><td><input type="checkbox" name="item_ids" value="{r["id"]}" '
        f'style="width:auto"></td><td>{esc(r["tender_number"])}</td>'
        f'<td>{esc(r.get("submission_deadline"))}</td>'
        f'<td>{esc((r.get("product_name") or "")[:60])}</td>'
        f'<td>{esc(_display_area(r, labels))}</td>'
        f'<td>{esc(r.get("ref_match_method"))}</td>'
        f'<td>{esc(r.get("review_reason"))}</td></tr>' for r in items_q)

    body = f"""<div class="panel"><h2>Review queue</h2>
<p class="sub">Nothing is discarded silently: anything uncertain lands here
with the reason attached. Approving a row clears its amber mark and returns
it to Tenders.</p>{bar}</div>
<form method="post" action="/review/approve">
<input type="hidden" name="back" value="{back}">
<div class="panel"><h2>Tenders ({len(tenders_q)})</h2>
<div style="overflow-x:auto"><table>
<tr><th><input type="checkbox" onclick="for(const c of
this.closest('table').querySelectorAll('input[name=tender_ids]'))
c.checked=this.checked" style="width:auto"></th><th>Tender no.</th>
<th>Closing</th><th>Title</th><th>Area</th><th>Reason</th></tr>
{trows or '<tr><td colspan=6 class=muted>Nothing outstanding.</td></tr>'}
</table></div></div>
<div class="panel"><h2>Line items ({len(items_q)})</h2>
<div style="overflow-x:auto"><table>
<tr><th><input type="checkbox" onclick="for(const c of
this.closest('table').querySelectorAll('input[name=item_ids]'))
c.checked=this.checked" style="width:auto"></th><th>Tender no.</th>
<th>Closing</th><th>Item</th><th>Area</th><th>Match</th><th>Reason</th></tr>
{irows or '<tr><td colspan=7 class=muted>Nothing outstanding.</td></tr>'}
</table></div></div>
<div class="panel"><button class="btn">Approve selected</button>
<span class="muted" style="margin-left:10px">Approved rows leave this queue
and keep their data - only the review mark is cleared.</span></div>
</form>"""
    return page("Review queue", body, "/review")




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
def forecast(q: str = ""):
    """Planning view. Search a principal (TQ, Jamjoom, Onko) or a molecule and
    get the SKUs behind it, the tender demand seen so far, and what the cycle
    engine can say about the next one."""
    db, cfg = get_db(), load_config()
    q = (q or "").strip()
    labels = _area_labels(cfg)
    today = date.today().isoformat()

    rows = [dict(r) for r in db.query("""
        SELECT i.id, i.product_name, i.quantity, i.unit, i.roots_product,
               i.roots_principal, i.roots_status, i.registered_products,
               i.ref_match_method, i.therapeutic_area AS item_area,
               i.search_code, t.tender_number, t.submission_deadline,
               t.publication_date, t.tender_fee,
               t.therapeutic_area AS tender_area
        FROM tender_items i JOIN tenders t ON t.id = i.tender_id""")]

    principals = sorted({p.strip() for r in rows
                         for p in str(r.get("roots_principal") or "").split("\n")
                         if p.strip()})
    chips = " ".join(
        f'<a class="btn sec" href="/forecast?q={esc_attr(p)}">{esc(p)}</a>'
        for p in principals)

    search = f"""<div class="panel"><h2>Forecast &amp; planning</h2>
<p class="sub">Search a principal to see its portfolio against live tender
demand, or a molecule to see who could supply it. Demand figures come from
the tenders screened so far; a next-tender date appears once a molecule has
been seen at least three times.</p>
<form method="get" action="/forecast" class="filters">
<div style="min-width:320px"><label>Principal, molecule or SKU</label>
<input name="q" value="{esc_attr(q)}"
 placeholder="e.g. Jamjoom, Onko, paracetamol, Feverol"></div>
<div class="acts"><button class="btn">Search</button>
{'<a class="btn sec" href="/forecast">Clear</a>' if q else ''}</div></form>
{'<div style="margin-top:14px"><label>Principals in the portfolio</label>' + chips + '</div>' if principals else ''}
</div>"""

    if not q:
        overview = ""
        if principals:
            prows = ""
            for p in principals:
                sel = [r for r in rows
                       if p in str(r.get("roots_principal") or "")]
                skus = {s.strip() for r in sel
                        for s in str(r.get("roots_product") or "").split("\n")
                        if s.strip()}
                open_n = len({r["tender_number"] for r in sel
                              if (r.get("submission_deadline") or "") >= today})
                prows += (f"<tr><td>{esc(p)}</td><td>{len(skus)}</td>"
                          f"<td>{len(sel)}</td><td>{open_n}</td>"
                          f'<td><a href="/forecast?q={esc_attr(p)}">open</a></td></tr>')
            overview = f"""<div class="panel"><h2>Portfolio at a glance</h2>
<table><tr><th>Principal</th><th>SKUs seen in tenders</th>
<th>Line items</th><th>Open tenders</th><th></th></tr>{prows}</table></div>"""
        return page("Forecast", search + overview, "/forecast")

    ql = q.lower()
    hits = [r for r in rows if ql in " ".join(str(r.get(k) or "") for k in
            ("roots_principal", "roots_product", "product_name",
             "registered_products")).lower()]

    if not hits:
        body = search + f"""<div class="panel"><h2>No match</h2>
<p class="sub">Nothing screened so far mentions "{esc(q)}". Either no tender
has asked for it yet, or the molecule has not been matched to a SKU - the
review queue lists what could not be matched.</p></div>"""
        return page("Forecast", body, "/forecast")

    # ---- SKU-level demand -------------------------------------------------
    skus: dict[str, dict] = {}
    for r in hits:
        names = [s.strip() for s in str(r.get("roots_product") or "").split("\n")
                 if s.strip()] or ["(no Roots SKU matched)"]
        for name in names:
            s = skus.setdefault(name, {
                "principal": trk._join(r.get("roots_principal")),
                "status": trk._join(r.get("roots_status")),
                "tenders": set(), "qty": 0.0, "units": set(),
                "molecules": set(), "next_close": None, "open": 0})
            s["tenders"].add(r["tender_number"])
            s["qty"] += float(r.get("quantity") or 0)
            if r.get("unit"):
                s["units"].add(r["unit"])
            s["molecules"].add((r.get("product_name") or "")[:60])
            d = r.get("submission_deadline") or ""
            if d >= today:
                s["open"] += 1
                if s["next_close"] is None or d < s["next_close"]:
                    s["next_close"] = d

    srows = "".join(
        f"<tr><td><b>{esc(name)}</b></td><td>{esc(s['principal'])}</td>"
        f"<td>{esc(s['status'])}</td><td>{len(s['tenders'])}</td>"
        f"<td>{s['qty']:,.0f}</td><td>{esc('; '.join(sorted(s['units'])))}</td>"
        f"<td>{s['open']}</td><td>{esc(s['next_close'])}</td></tr>"
        for name, s in sorted(skus.items(), key=lambda kv: -len(kv[1]['tenders'])))

    # ---- the tenders behind it -------------------------------------------
    seen = set()
    trows = ""
    for r in sorted(hits, key=lambda x: (x.get("submission_deadline") or "")):
        key = (r["tender_number"], r.get("product_name"))
        if key in seen:
            continue
        seen.add(key)
        d = r.get("submission_deadline") or ""
        state = ('<span class="tag t-ok">open</span>' if d >= today
                 else '<span class="muted">closed</span>')
        trows += (f"<tr><td>{esc(r['tender_number'])}</td><td>{esc(d)} {state}</td>"
                  f"<td>{esc((r.get('product_name') or '')[:70])}</td>"
                  f"<td>{esc(_display_area(r, labels))}</td>"
                  f"<td>{esc(r.get('unit'))}</td><td>{esc(r.get('quantity'))}</td>"
                  f"<td>{esc(trk._join(r.get('roots_product')))}</td></tr>")

    # ---- what the cycle engine can add -----------------------------------
    res = run_forecasting(db, cfg, save=False)
    fblocks = ""
    for r in res["products"] + res["authorities"]:
        if ql not in str(r["subject"]).lower():
            continue
        if not r["forecasts"] and not r["gaps"]:
            continue
        f = "".join(f"<tr><td>{esc(x['metric'])}</td><td>{esc(x['value'])}</td>"
                    f"<td>{esc(x['confidence'])}</td><td>{esc(x['basis'])}</td></tr>"
                    for x in r["forecasts"])
        g = "".join(f'<li class="muted">{esc(x)}</li>' for x in r["gaps"])
        fblocks += f"""<div class="panel"><h2>{esc(r['subject'])}</h2>
<p class="sub">{r['subject_type']}</p>
{'<table><tr><th>Metric</th><th>Value</th><th>Confidence</th><th>Evidence</th></tr>' + f + '</table>' if f else ''}
{'<ul>' + g + '</ul>' if g else ''}</div>"""

    n_tenders = len({r["tender_number"] for r in hits})
    n_open = len({r["tender_number"] for r in hits
                  if (r.get("submission_deadline") or "") >= today})
    cards = "".join(
        f'<div class="card"><div class="n">{v}</div><div class="k">{k}</div></div>'
        for k, v in [("SKUs", len(skus)), ("Tenders", n_tenders),
                     ("Open now", n_open), ("Line items", len(hits))])

    body = search + f"""<div class="panel"><h2>"{esc(q)}"</h2>
<div class="cards">{cards}</div>
<a class="btn sec" href="/tracker?q={esc_attr(q)}&closing=">See these in the tracker</a>
</div>
<div class="panel"><h2>SKU demand</h2>
<p class="sub">Quantity is the total asked for across every screened tender,
in the units the tender used - read it per unit, not as one figure.</p>
<div style="overflow-x:auto"><table>
<tr><th>Roots SKU</th><th>Principal</th><th>Registration</th><th>Tenders</th>
<th>Total qty</th><th>Units</th><th>Open</th><th>Next closing</th></tr>
{srows}</table></div></div>
<div class="panel"><h2>Tenders behind it</h2>
<div style="overflow-x:auto"><table>
<tr><th>Tender no.</th><th>Closing</th><th>Item</th><th>Area</th>
<th>Unit</th><th>Qty</th><th>Roots SKU</th></tr>{trows}</table></div></div>
{fblocks or '<div class="panel"><p class="sub">No cycle forecast yet: a molecule needs at least three past tenders before the engine will project the next one.</p></div>'}"""
    return page("Forecast", body, "/forecast")



STATIC_FILES = {"roots-logo.png": "image/png"}


@app.get("/static/{name}")
def static_file(name: str):
    """Serves the handful of bundled assets. The whitelist keeps this from
    becoming a way to read arbitrary files off the container."""
    media = STATIC_FILES.get(name)
    path = BASE / "static" / name
    if not media or not path.is_file():
        return Response(status_code=404)
    return Response(path.read_bytes(), media_type=media,
                    headers={"Cache-Control": "public, max-age=86400"})


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
