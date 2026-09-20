"""Site reconnaissance.

Run this first, from a machine that can reach the portal. It answers the
questions the adapter needs and cannot guess:

  * is there a login wall?
  * is the results table server-rendered, or fetched by JavaScript?
  * what are the form field names and the department option values?
  * what column headers does the results table use?
  * where do attachment links point?

It writes probe_output/ with the raw HTML plus a findings report, and prints
a draft `sources.moh_kw` config block to paste into config.yaml.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

from .sources.base import DEFAULT_UA

XHR_RX = re.compile(
    r"""(?:fetch|axios\.(?:get|post)|\$\.(?:get|post|ajax)|url\s*[:=])\s*"""
    r"""\(?\s*['"`]([^'"`]{4,200})['"`]""",
    re.IGNORECASE,
)
API_PATH_RX = re.compile(r"""['"`](/[\w\-./]*(?:api|rest|service|data|search|"""
                         r"""tender)[\w\-./]*)['"`]""", re.IGNORECASE)
LOGIN_HINTS = ("login", "signin", "sign in", "username", "password",
               "تسجيل الدخول", "كلمة المرور")


def probe(url: str, out_dir: str = "probe_output", timeout: int = 45) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    })

    findings: dict = {"url": url}
    resp = session.get(url, timeout=timeout)
    findings["status_code"] = resp.status_code
    findings["final_url"] = resp.url
    findings["redirected"] = resp.url.rstrip("/") != url.rstrip("/")
    (out / "landing.html").write_text(resp.text, encoding="utf-8")

    soup = BeautifulSoup(resp.text, "html.parser")
    text_low = resp.text.lower()

    # ---- login wall -------------------------------------------------
    findings["password_field_present"] = bool(soup.find("input", {"type": "password"}))
    findings["login_hints"] = sorted({h for h in LOGIN_HINTS if h in text_low})

    # ---- forms and fields -------------------------------------------
    forms = []
    for f in soup.find_all("form"):
        fields = []
        for inp in f.find_all(("input", "select", "textarea")):
            entry = {
                "tag": inp.name,
                "name": inp.get("name"),
                "id": inp.get("id"),
                "type": inp.get("type"),
            }
            if inp.name == "select":
                entry["options"] = [
                    {"value": o.get("value", ""),
                     "label": " ".join(o.get_text().split())}
                    for o in inp.find_all("option")
                ]
            fields.append(entry)
        forms.append({
            "action": urljoin(resp.url, f.get("action") or ""),
            "method": (f.get("method") or "GET").upper(),
            "fields": fields,
        })
    findings["forms"] = forms

    # department dropdown: any select whose options mention the target names
    dept_select, dept_options = None, []
    wanted = ("medical store", "biomedical", "مخازن", "هندسة")
    for f in forms:
        for fld in f["fields"]:
            for opt in fld.get("options", []):
                if any(w in opt["label"].lower() for w in wanted):
                    dept_select = fld.get("name") or fld.get("id")
                    dept_options = fld["options"]
                    break
    findings["department_field"] = dept_select
    findings["department_options"] = dept_options

    # ---- tables ------------------------------------------------------
    tables = []
    for i, t in enumerate(soup.find_all("table")):
        rows = t.find_all("tr")
        headers = [" ".join(c.get_text().split())
                   for c in (rows[0].find_all(("th", "td")) if rows else [])]
        tables.append({
            "index": i,
            "id": t.get("id"),
            "class": t.get("class"),
            "rows": len(rows),
            "headers": headers,
        })
    findings["tables"] = tables
    findings["server_rendered_results"] = any(
        t["rows"] > 2 and t["headers"] for t in tables
    )

    # ---- JavaScript endpoints ---------------------------------------
    scripts = [urljoin(resp.url, s["src"]) for s in soup.find_all("script", src=True)]
    findings["script_files"] = scripts[:40]

    candidates: set[str] = set()
    inline = "\n".join(s.get_text() for s in soup.find_all("script", src=False))
    for blob in (inline,):
        candidates.update(XHR_RX.findall(blob))
        candidates.update(API_PATH_RX.findall(blob))

    for src in scripts[:12]:
        try:
            js = session.get(src, timeout=timeout).text
        except Exception:
            continue
        candidates.update(XHR_RX.findall(js))
        candidates.update(API_PATH_RX.findall(js))

    endpoints = sorted({
        urljoin(resp.url, c) for c in candidates
        if not c.startswith(("data:", "#", "javascript:"))
        and any(k in c.lower() for k in ("tender", "api", "search", "rest",
                                         "service", "data", "list", "dept"))
    })
    findings["candidate_endpoints"] = endpoints[:40]

    # ---- attachment links -------------------------------------------
    doc_links = sorted({
        urljoin(resp.url, a["href"]) for a in soup.find_all("a", href=True)
        if re.search(r"\.(pdf|docx?|xlsx?|zip|rar)(\?|$)", a["href"], re.I)
    })
    findings["document_links_on_landing"] = doc_links[:25]
    findings["js_download_handlers"] = sorted({
        " ".join(a.get_text().split())[:60] for a in soup.find_all("a", href=True)
        if a["href"].lower().startswith("javascript:")
    })[:25]

    # ---- verdict -----------------------------------------------------
    if findings["server_rendered_results"]:
        mode = "html"
        verdict = "Results are in the page HTML - the html adapter mode will work."
    elif endpoints:
        mode = "json"
        verdict = ("Page appears to be JavaScript-driven. Candidate API endpoints "
                   "found - use mode: json and confirm the right endpoint.")
    else:
        mode = "html"
        verdict = ("No results table and no API endpoint found. The page most "
                   "likely needs a session/login, or renders results only after "
                   "a POST. Capture the Search request in DevTools "
                   "(Network > Copy as cURL) and send it over.")
    findings["mode"] = mode
    findings["verdict"] = verdict

    (out / "findings.json").write_text(
        json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    draft = {
        "sources": {
            "moh_kw": {
                "url": url,
                "mode": mode,
                "department_field": dept_select or "REPLACE_ME",
                "departments": [
                    {"label": o["label"], "value": o["value"]}
                    for o in dept_options
                    if any(w in o["label"].lower() for w in wanted)
                ] or [{"label": "Medical Store", "value": "REPLACE_ME"},
                      {"label": "Biomedical Engineering", "value": "REPLACE_ME"}],
                "table_selector": next(
                    (f"#{t['id']}" for t in tables if t.get("id") and t["rows"] > 2),
                    None,
                ),
                "login": {"required": bool(findings["password_field_present"])},
            }
        }
    }
    (out / "draft_config.yaml").write_text(
        yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return findings


def print_report(f: dict) -> None:
    p = print
    p("=" * 68)
    p("PORTAL PROBE REPORT")
    p("=" * 68)
    p(f"URL            : {f['url']}")
    p(f"HTTP status    : {f['status_code']}")
    if f["redirected"]:
        p(f"REDIRECTED TO  : {f['final_url']}   <- likely a login wall")
    p(f"Password field : {'YES' if f['password_field_present'] else 'no'}")
    if f["login_hints"]:
        p(f"Login hints    : {', '.join(f['login_hints'])}")
    p("")
    p(f"Forms found    : {len(f['forms'])}")
    for i, form in enumerate(f["forms"]):
        named = [x["name"] for x in form["fields"] if x["name"]]
        p(f"  [{i}] {form['method']} {form['action']}")
        p(f"      fields: {', '.join(named[:18]) or '(none named)'}")
    p("")
    p(f"Department field  : {f['department_field'] or 'NOT FOUND'}")
    if f["department_options"]:
        p("Department options:")
        for o in f["department_options"][:40]:
            p(f"    {o['value']!r:>18}  {o['label']}")
    p("")
    p(f"Tables found   : {len(f['tables'])}")
    for t in f["tables"][:8]:
        p(f"  #{t['index']} id={t['id']} rows={t['rows']}")
        if t["headers"]:
            p(f"      headers: {' | '.join(t['headers'][:12])}")
    p("")
    if f["candidate_endpoints"]:
        p("Candidate API endpoints:")
        for e in f["candidate_endpoints"][:20]:
            p(f"    {e}")
        p("")
    if f["document_links_on_landing"]:
        p("Document links on landing page:")
        for d in f["document_links_on_landing"][:10]:
            p(f"    {d}")
    if f["js_download_handlers"]:
        p(f"JS download handlers: {len(f['js_download_handlers'])} "
          "(attachments behind JavaScript, not plain links)")
    p("")
    p("VERDICT: " + f["verdict"])
    p("")
    p("Wrote probe_output/landing.html, findings.json, draft_config.yaml")
    p("=" * 68)
