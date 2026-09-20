"""Base class for portal adapters.

Each portal gets one adapter. Adapters return plain dicts so the rest of the
pipeline never knows which site a tender came from -- adding a second portal
later (Central Agency for Public Tenders, MOH Saudi, etc.) means writing one
adapter, not touching the pipeline.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


@dataclass
class Attachment:
    url: str
    filename: str = ""
    content_type: str = ""
    # Some portals hand documents out only through a stateful postback, which
    # cannot be replayed later from a bare URL. Those adapters fetch the bytes
    # while the page state is still valid and park them here; the pipeline
    # uses them instead of issuing its own GET.
    data: bytes | None = None


@dataclass
class ScrapedTender:
    tender_number: str | None = None
    tender_title: str | None = None
    tender_title_ar: str | None = None
    issuing_authority: str | None = None
    department: str | None = None
    country: str = "Kuwait"
    publication_date: str | None = None
    submission_deadline: str | None = None
    closing_time: str | None = None
    tender_status: str | None = None
    contract_period: str | None = None
    estimated_value: float | None = None
    currency: str | None = None
    tender_fee: float | None = None
    bid_bond: float | None = None
    source_url: str | None = None
    tender_page_url: str | None = None
    search_code: str | None = None   # the IN/TB/ON/SP/NU term that found it
    description: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    raw_row: dict = field(default_factory=dict)

    def as_db_row(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()
             if k not in ("attachments", "description")}
        return d

    @property
    def classification_text(self) -> str:
        return " \n".join(filter(None, [
            self.tender_title, self.tender_title_ar, self.description,
            self.department, self.issuing_authority,
            " ".join(str(v) for v in self.raw_row.values()),
        ]))


class BaseAdapter:
    id = "base"
    name = "Base adapter"
    base_url = ""

    def __init__(self, config: dict, logger=None):
        self.config = config
        self.log = logger
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        retry = Retry(
            total=self.config.get("retries", 3),
            backoff_factor=self.config.get("backoff", 1.5),
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update({
            "User-Agent": self.config.get("user_agent", DEFAULT_UA),
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        })
        s.verify = self.config.get("verify_tls", True)
        return s

    def polite_sleep(self) -> None:
        time.sleep(float(self.config.get("delay_seconds", 1.5)))

    # ---- to implement in subclasses ----

    def login(self) -> bool:
        """Return True if authenticated (or if no login is needed)."""
        return True

    def list_tenders(self, run) -> Iterator[ScrapedTender]:
        raise NotImplementedError

    def enrich(self, tender: ScrapedTender, run) -> ScrapedTender:
        """Open the detail page and fill in whatever the listing lacked."""
        return tender

    def fetch(self, url: str, **kw) -> requests.Response:
        self.polite_sleep()
        timeout = kw.pop("timeout", self.config.get("timeout", 45))
        return self.session.get(url, timeout=timeout, **kw)
