"""Kuwait Ministry of Health e-tenders adapter.

    https://etenders.moh.gov.kw/pdApps/CompanyRegistration/v1/ViewTenders

STATUS: UNCALIBRATED.
-----------------------------------------------------------------------
The portal blocks automated inspection from outside, so the exact request
shape could not be confirmed while writing this. Everything site-specific
therefore lives in `config.yaml` under `sources.moh_kw`, not in this file.

Run `pharmatender probe` first. It prints the forms, fields, department
option values, tables and any XHR endpoints it finds, and writes a draft
config block. Paste that block into config.yaml and this adapter works.

The adapter supports both shapes a portal of this kind usually takes:
  * "html"  - a server-rendered results table (ASP.NET postback or GET query)
  * "json"  - a REST endpoint the page calls, which is far more reliable
`mode` in config picks between them; probe tells you which one applies.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..normalize import clean, parse_amount, parse_date, parse_time
from .base import Attachment, BaseAdapter, ScrapedTender

# Header text -> our field name. Bilingual because the portal may render
# either language depending on session locale.
COLUMN_ALIASES = {
    "tender_number": ["tender no", "tender number", "tender ref", "reference",
                      "ref no", "practice no", "رقم المناقصة", "رقم", "المرجع"],
    "tender_title": ["subject", "title", "description", "tender subject",
                     "موضوع المناقصة", "الموضوع", "البيان", "وصف"],
    "department": ["department", "requesting department", "الجهة", "الادارة",
                   "القسم"],
    "issuing_authority": ["authority", "entity", "issued by", "الجهة المالكة"],
    "publication_date": ["publish date", "published", "announcement date",
                         "issue date", "تاريخ النشر", "تاريخ الاعلان"],
    "submission_deadline": ["closing date", "last date", "deadline",
                            "submission date", "expiry", "تاريخ الاقفال",
                            "اخر موعد", "تاريخ الإغلاق"],
    "tender_status": ["status", "state", "الحالة"],
    "estimated_value": ["value", "estimated value", "amount", "القيمة",
                        "القيمة التقديرية"],
    "tender_fee": ["fee", "document fee", "price of documents", "قيمة الوثائق",
                   "ثمن الوثائق"],
    "bid_bond": ["bid bond", "initial guarantee", "التأمين الأولي",
                 "التامين الاولي"],
    "contract_period": ["duration", "period", "contract period", "المدة"],
}

DOC_EXT_RX = re.compile(r"\.(pdf|docx?|xlsx?|zip|rar|7z)(\?|$)", re.IGNORECASE)

# The results grid uses headers that the generic aliases above read wrongly:
# its "Title" column holds the practice number (6ON086) and "Description"
# holds the actual subject. Exact header text wins over the fuzzy aliases.
GRID_COLUMN_OVERRIDES = {
    "title": "tender_number",
    "description": "tender_title",
    "posting date": "publication_date",
    "closing date": "submission_deadline",
    "department": "department",
    "amount": "tender_fee",          # price of the tender documents, not the value
}

# Documents hang off JavaScript postbacks, not hrefs:
#   javascript:__doPostBack('gv1$ctl02$lnkDownload','')
POSTBACK_RX = re.compile(r"__doPostBack\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)")


def _map_header(text: str) -> str | None:
    t = clean(text).lower()
    if not t:
        return None
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in t:
                return field
    return None


class MohKuwaitAdapter(BaseAdapter):
    id = "moh_kw"
    name = "Kuwait Ministry of Health e-Tenders"
    base_url = "https://etenders.moh.gov.kw/pdApps/CompanyRegistration/v1/ViewTenders"

    def __init__(self, config: dict, logger=None):
        super().__init__(config, logger)
        self.cfg = config.get("sources", {}).get("moh_kw", {})
        self.base_url = self.cfg.get("url", self.base_url)
        self.mode = self.cfg.get("mode", "html")
        self.departments = self.cfg.get("departments", [])
        # The portal's front page is searched by tender title. Each code is a
        # disease-area prefix used by the Medical Store; a cycle runs one
        # search per code per department.
        self.title_terms = self.cfg.get("title_search_terms", [])

    # ------------------------------------------------------------ login

    def login(self) -> bool:
        login_cfg = self.cfg.get("login")
        if not login_cfg or not login_cfg.get("required"):
            return True
        import os
        user = os.environ.get(login_cfg.get("username_env", "MOH_KW_USER"))
        pwd = os.environ.get(login_cfg.get("password_env", "MOH_KW_PASS"))
        if not user or not pwd:
            raise RuntimeError(
                "Login is required but credentials are not set. Export "
                f"{login_cfg.get('username_env')} and "
                f"{login_cfg.get('password_env')} before running."
            )
        resp = self.session.post(
            login_cfg["url"],
            data={login_cfg.get("username_field", "username"): user,
                  login_cfg.get("password_field", "password"): pwd},
            timeout=self.config.get("timeout", 45),
        )
        ok = resp.ok and login_cfg.get("success_marker", "") in resp.text
        if self.log:
            self.log.info("login %s", "succeeded" if ok else "FAILED")
        return ok

    # ------------------------------------------------------- listing

    def list_tenders(self, run=None):
        terms = self.title_terms or [{"code": "", "label": None}]
        for dept in (self.departments or [{"label": "ALL", "value": ""}]):
            dept_label = dept.get("label", dept.get("value", ""))
            for term in terms:
                # str() is deliberate: a config written without quotes can
                # hand us True for the code "ON" (YAML 1.1 boolean).
                raw = term.get("code", "") if isinstance(term, dict) else term
                code = "ON" if raw is True else ("NO" if raw is False else str(raw or ""))
                if self.log:
                    self.log.info("search: department=%s title=%r",
                                  dept_label, code)
                try:
                    if self.mode == "json":
                        found = self._list_json(dept, code)
                    else:
                        found = self._list_html(dept, code)
                    n = 0
                    for tender in found:
                        tender.search_code = code or None
                        tender.raw_row["_search_code"] = code
                        n += 1
                        yield tender
                    if self.log:
                        self.log.info("  -> %d listing(s)", n)
                except Exception as exc:
                    target = f"{dept_label}/{code}"
                    if self.log:
                        self.log.error("search %s failed: %s", target, exc)
                    if run:
                        run.error("listing", target, str(exc))

    def _list_json(self, dept: dict, title_term: str = ""):
        """Preferred path: the page's own XHR endpoint."""
        endpoint = self.cfg["api"]["url"]
        method = self.cfg["api"].get("method", "GET").upper()
        params = dict(self.cfg["api"].get("params", {}))
        dept_param = self.cfg["api"].get("department_param", "departmentId")
        if dept.get("value"):
            params[dept_param] = dept["value"]
        title_param = self.cfg["api"].get("title_param", "tenderTitle")
        if title_term:
            params[title_param] = title_term

        page, page_param = 1, self.cfg["api"].get("page_param")
        while True:
            if page_param:
                params[page_param] = page
            self.polite_sleep()
            if method == "POST":
                resp = self.session.post(endpoint, json=params,
                                         timeout=self.config.get("timeout", 45))
            else:
                resp = self.session.get(endpoint, params=params,
                                        timeout=self.config.get("timeout", 45))
            resp.raise_for_status()
            payload = resp.json()
            rows = payload
            for key in self.cfg["api"].get("rows_path", []):
                rows = rows.get(key, []) if isinstance(rows, dict) else rows
            if not rows:
                break
            for row in rows:
                yield self._from_json_row(row, dept)
            if not page_param or len(rows) < self.cfg["api"].get("page_size", 50):
                break
            page += 1

    def _from_json_row(self, row: dict, dept: dict) -> ScrapedTender:
        fieldmap = self.cfg["api"].get("field_map", {})
        get = lambda f, default=None: row.get(fieldmap.get(f, f), default)  # noqa: E731
        value, currency = parse_amount(get("estimated_value"))
        detail_tpl = self.cfg.get("detail_url_template")
        detail = None
        if detail_tpl:
            detail = detail_tpl.format(**{k: row.get(k, "") for k in row})
        return ScrapedTender(
            tender_number=clean(get("tender_number")) or None,
            tender_title=clean(get("tender_title")) or None,
            tender_title_ar=clean(get("tender_title_ar")) or None,
            department=dept.get("label") or clean(get("department")) or None,
            issuing_authority=clean(get("issuing_authority"))
                              or "Ministry of Health - Kuwait",
            publication_date=parse_date(get("publication_date")),
            submission_deadline=parse_date(get("submission_deadline")),
            closing_time=parse_time(get("submission_deadline")),
            tender_status=clean(get("tender_status")) or None,
            estimated_value=value,
            currency=currency or "KWD",
            source_url=self.base_url,
            tender_page_url=detail,
            raw_row=row,
        )

    def _list_html(self, dept: dict, title_term: str = ""):
        """Server-rendered table. Handles ASP.NET postbacks if configured."""
        resp = self.fetch(self.base_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        if dept.get("value") or title_term:
            soup = self._post_search(soup, dept, title_term)

        table = self._find_results_table(soup)
        if table is None:
            raise RuntimeError(
                "No results table found. Run `pharmatender probe` and update "
                "sources.moh_kw.table_selector in config.yaml."
            )
        # The hidden state of the *results* page. Each document postback has to
        # be replayed against this exact state, so capture it once here.
        state = self._form_payload(soup)
        state.pop("btnGo", None)
        yield from self._parse_table(table, dept, resp.url, state)

    def _form_payload(self, soup: BeautifulSoup) -> dict:
        """Every named field of the page's form, ASP.NET hidden state included."""
        form = soup.find("form")
        if form is None:
            return {}
        payload = {}
        for inp in form.find_all(("input", "select", "textarea")):
            name = inp.get("name")
            if not name:
                continue
            if inp.name == "select":
                opt = inp.find("option", selected=True) or inp.find("option")
                payload[name] = opt.get("value", "") if opt else ""
            elif inp.get("type") in ("checkbox", "radio"):
                if inp.has_attr("checked"):
                    payload[name] = inp.get("value", "on")
            else:
                payload[name] = inp.get("value", "")
        # Never submit Clear: it wipes the search we are about to run.
        payload.pop("btnClear", None)
        return payload

    def _post_search(self, soup: BeautifulSoup, dept: dict, title_term: str = ""):
        """Replay the Search button. ASP.NET needs the hidden state fields."""
        form = soup.find("form")
        if form is None:
            return soup
        payload = self._form_payload(soup)

        dept_field = self.cfg.get("department_field")
        if dept_field and dept.get("value"):
            payload[dept_field] = dept["value"]
        title_field = self.cfg.get("title_field")
        if title_field and title_term:
            payload[title_field] = title_term
        payload.update(self.cfg.get("extra_form_fields", {}))

        action = urljoin(self.base_url, form.get("action") or "")
        self.polite_sleep()
        r = self.session.post(action, data=payload,
                              timeout=self.config.get("timeout", 45))
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")

    def _find_results_table(self, soup: BeautifulSoup):
        selector = self.cfg.get("table_selector")
        if selector:
            return soup.select_one(selector)
        # Heuristic: the widest table with a recognisable header row.
        best, best_score = None, 0
        for table in soup.find_all("table"):
            headers = [clean(th.get_text()) for th in table.find_all(("th", "td"),
                                                                     limit=15)]
            score = sum(1 for h in headers if _map_header(h))
            if score > best_score:
                best, best_score = table, score
        return best if best_score >= 2 else None

    def _download_postback(self, state: dict, target: str):
        """Replay one document postback and return (bytes, filename).

        The portal answers a download postback with the file itself, leaving
        the page state untouched, so the same state drives every row.
        """
        payload = dict(state)
        payload["__EVENTTARGET"] = target
        payload["__EVENTARGUMENT"] = ""
        self.polite_sleep()
        resp = self.session.post(self.base_url, data=payload,
                                 timeout=self.config.get("timeout", 45))
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" in ctype:
            return None, ""      # no document behind this link
        name = ""
        disp = resp.headers.get("Content-Disposition") or ""
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp)
        if m:
            name = m.group(1).strip()
        return resp.content, name

    def _row_documents(self, tr, state: dict, tender_no: str) -> list[Attachment]:
        """Collect the PDFs behind a row's __doPostBack download links."""
        out: list[Attachment] = []
        if not state:
            return out
        seen = set()
        for a in tr.find_all("a", href=True):
            m = POSTBACK_RX.search(a["href"])
            if not m:
                continue
            target = m.group(1)
            # Payment links open a billing flow, not a document.
            if target in seen or "download" not in target.lower():
                continue
            seen.add(target)
            try:
                data, name = self._download_postback(state, target)
            except Exception as exc:
                if self.log:
                    self.log.warning("document postback %s failed: %s", target, exc)
                continue
            if not data:
                continue
            if not name:
                name = f"{tender_no or 'tender'}_{target.rsplit('$', 1)[-1]}.pdf"
            out.append(Attachment(url=f"{self.base_url}#{target}",
                                  filename=name, data=data))
        return out

    def _parse_table(self, table, dept: dict, page_url: str, state: dict | None = None):
        rows = table.find_all("tr")
        if not rows:
            return
        header_cells = [clean(c.get_text()) for c in rows[0].find_all(("th", "td"))]
        mapping = {}
        for i, h in enumerate(header_cells):
            field = GRID_COLUMN_OVERRIDES.get(clean(h).lower()) or _map_header(h)
            if field:
                mapping[i] = field
        if not mapping:
            raise RuntimeError("Results table header not recognised - see probe output")

        for tr in rows[1:]:
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            raw = {header_cells[i] if i < len(header_cells) else f"col{i}":
                   clean(c.get_text()) for i, c in enumerate(cells)}
            rec: dict = {}
            for i, fieldname in mapping.items():
                if i < len(cells):
                    rec[fieldname] = clean(cells[i].get_text())

            detail = None
            attachments = []
            for a in tr.find_all("a", href=True):
                href = urljoin(page_url, a["href"])
                if DOC_EXT_RX.search(href):
                    attachments.append(Attachment(
                        url=href,
                        filename=clean(a.get_text()) or urlparse(href).path.split("/")[-1],
                    ))
                elif detail is None and "javascript:" not in a["href"].lower():
                    detail = href
            # The MOH grid exposes its PDFs only through postbacks; fetch them
            # now, while this results page's state is still the live one.
            attachments.extend(
                self._row_documents(tr, state or {}, rec.get("tender_number") or ""))

            value, currency = parse_amount(rec.get("estimated_value"))
            fee, _ = parse_amount(rec.get("tender_fee"))
            bond, _ = parse_amount(rec.get("bid_bond"))

            yield ScrapedTender(
                tender_number=rec.get("tender_number") or None,
                tender_title=rec.get("tender_title") or None,
                department=dept.get("label") or rec.get("department"),
                issuing_authority=rec.get("issuing_authority")
                                  or "Ministry of Health - Kuwait",
                publication_date=parse_date(rec.get("publication_date")),
                submission_deadline=parse_date(rec.get("submission_deadline")),
                closing_time=parse_time(rec.get("submission_deadline")),
                tender_status=rec.get("tender_status"),
                contract_period=rec.get("contract_period"),
                estimated_value=value,
                currency=currency or "KWD",
                tender_fee=fee,
                bid_bond=bond,
                source_url=page_url,
                tender_page_url=detail,
                attachments=attachments,
                raw_row=raw,
            )

    # -------------------------------------------------------- detail page

    def enrich(self, tender: ScrapedTender, run=None) -> ScrapedTender:
        if not tender.tender_page_url:
            return tender
        try:
            resp = self.fetch(tender.tender_page_url)
            resp.raise_for_status()
        except Exception as exc:
            if run:
                run.error("detail", tender.tender_page_url, str(exc))
            return tender

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        tender.description = clean(soup.get_text(" "))[:20000]

        known = {a.url for a in tender.attachments}
        for a in soup.find_all("a", href=True):
            href = urljoin(resp.url, a["href"])
            if DOC_EXT_RX.search(href) and href not in known:
                tender.attachments.append(Attachment(
                    url=href,
                    filename=clean(a.get_text()) or urlparse(href).path.split("/")[-1],
                ))
                known.add(href)

        # Detail pages often carry label/value pairs the listing omits.
        for row in soup.find_all("tr"):
            cells = [clean(c.get_text()) for c in row.find_all(("th", "td"))]
            if len(cells) == 2:
                field = _map_header(cells[0])
                if field and not getattr(tender, field, None):
                    val = cells[1]
                    if field in ("publication_date", "submission_deadline"):
                        val = parse_date(val)
                    elif field in ("estimated_value", "tender_fee", "bid_bond"):
                        val, cur = parse_amount(val)
                        if cur and not tender.currency:
                            tender.currency = cur
                    if val:
                        setattr(tender, field, val)
        return tender
