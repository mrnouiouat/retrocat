"""ISBN -> BookMetadata via Google Books, OpenLibrary, and LoC SRU.

Sources are tried in order; the first hit wins per field (title/author can
come from a different source than the call number). Every raw source payload
is cached in .cache/lookup_cache.json keyed by canonical ISBN-13, so re-runs
never re-hit the APIs and field-extraction bug fixes don't require re-fetching.
"""

from __future__ import annotations

import json
import logging
import os
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import requests

from .isbn import isbn13_to_10
from .language import marc_language
from .lc_call import build_call_number, extract_year

logger = logging.getLogger(__name__)

GOOGLE_URL = "https://www.googleapis.com/books/v1/volumes"
OPENLIBRARY_URL = "https://openlibrary.org/api/books"
# Known issue: this endpoint is unreachable from some networks (observed as
# connection timeouts from sandboxed environments). Connectivity degrades
# gracefully on first use; unreachable -> warn and continue without LoC
# rather than hanging the run.
LOC_SRU_URL = "http://lx2.loc.gov:210/lcdb"

REQUEST_TIMEOUT = 15  # seconds
INTER_CALL_SLEEP = 0.2  # politeness delay between calls per source
GOOGLE_429_DELAYS = (1.0, 2.0, 4.0, 8.0)  # bounded exponential backoff
# LoC is only disabled for the run after this many CONSECUTIVE connection
# failures — one mid-run blip must not kill the best call-number source for
# the rest of a multi-thousand-book run.
LOC_MAX_CONSECUTIVE_FAILURES = 3
# Statuses worth retrying: rate-limit (429) and transient server-side faults
# (Google's backend returns sporadic 503s; OpenLibrary 503s in bursts). A 404
# is a legitimate "not found" and is never retried.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_MARCXML_NS = {"m": "http://www.loc.gov/MARC21/slim"}


def _marcxml_record_tags(xml_text: str) -> set[str]:
    """Tags present in an SRU response — used to detect zero-record responses."""
    try:
        return {elem.tag for elem in ET.fromstring(xml_text).iter()}
    except ET.ParseError:
        return set()

# Corporate-author heuristic (MARC 110 vs 100), validated against real data
# during the original deployment. Extend as more corporate bodies appear —
# "foundation" was added after a real "El-Falah Foundation" was mis-tagged as
# a person and the Cutter landed on the shared word "Foundation" instead of
# the distinctive name.
_ORG_KEYWORDS = (
    "center", "commission", "organization", "association",
    "press", "council", "institute", "society", "foundation",
)


@dataclass(frozen=True)
class ClassMap:
    """Ordered keyword -> LC class mapping plus the last-resort default class.

    Shipped as data/lc_class_map.toml (a collection-specific worked example);
    override with [lookup].class_map_file in config.toml. Order is meaningful
    — the first keyword found in the subject text wins — and tomllib preserves
    document order, so the file is written narrowest-first.
    """

    default_class: str
    keywords: tuple[tuple[str, str], ...]


def load_class_map(path: str | Path | None = None) -> ClassMap:
    """Load the shipped class map, or an override file when given."""
    if path is None:
        raw = tomllib.loads(
            resources.files("retrocat").joinpath("data/lc_class_map.toml")
            .read_text(encoding="utf-8")
        )
    else:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    default_class = raw.get("default_class", "AC")
    keywords = tuple(
        (str(k).lower(), str(v)) for k, v in (raw.get("classes") or {}).items()
    )
    return ClassMap(default_class=default_class, keywords=keywords)


def class_fallback(
    class_map: ClassMap,
    google: dict[str, Any] | None,
    ol: dict[str, Any] | None = None,
) -> str | None:
    """Class-level LC fallback from Google Books categories, backed by
    OpenLibrary subject headings.

    OL subjects are a second signal already present in the cached payload
    (no extra HTTP): when Google's categories are missing or unmappable
    (e.g. a mis-tag like "Juvenile Fiction"), OL's human-curated headings
    ("Lynching", "Race relations", ...) often still place the book.
    """
    parts: list[str] = []
    if google:
        parts.extend(google.get("categories") or [])
    if ol:
        for subject in ol.get("subjects") or []:
            # jscmd=data returns [{"name": ..., "url": ...}, ...]; be
            # tolerant of plain strings too.
            name = subject.get("name") if isinstance(subject, dict) else subject
            if name:
                parts.append(str(name))
    haystack = " | ".join(parts).lower()
    for keyword, lc_class in class_map.keywords:
        if keyword in haystack:
            return lc_class
    return None


def is_corporate_author(author: str) -> bool:
    """Org-vs-person heuristic (MARC 110 vs 100)."""
    lowered = author.lower()
    return any(keyword in lowered for keyword in _ORG_KEYWORDS)


@dataclass
class BookMetadata:
    isbn13: str
    title: str | None = None
    subtitle: str | None = None
    author: str | None = None
    call_number: str | None = None
    # google|openlibrary|loc|class_fallback|default|manual
    call_number_source: str | None = None
    confidence: str | None = None          # high | low
    lccn: str | None = None
    # MARC 008/35-37 code (see language.py). None = no source reported one;
    # marc_build falls back to the configured default rather than leaving it
    # blank.
    language: str | None = None

    @property
    def resolved(self) -> bool:
        """A record is usable only if a title resolved — never fabricate one."""
        return bool(self.title)


class LookupClient:
    """Lookup with per-ISBN response caching and graceful source degradation."""

    def __init__(
        self,
        cache_path: str | Path = ".cache/lookup_cache.json",
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        cache_flush_every: int = 25,
        class_map: ClassMap | None = None,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.session = session or requests.Session()
        self.sleep = sleep
        self.cache_flush_every = cache_flush_every
        self.class_map = class_map or load_class_map()
        self._dirty = 0
        self._loc_available: bool | None = None  # None = not yet probed
        self._loc_consecutive_failures = 0
        # Set when a retryable failure (5xx/429/network) is NOT overcome within
        # a lookup, so the result is not cached — a re-run re-fetches it rather
        # than the transient blip poisoning a book's call number permanently.
        self._transient = False
        self._cache: dict[str, dict[str, Any]] = {}
        if self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
                logger.info("lookup cache loaded: %d ISBNs", len(self._cache))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("could not read lookup cache (%s) — starting fresh", exc)

    # ---------------------------------------------------------------- cache

    def flush_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=0), encoding="utf-8"
        )
        tmp.replace(self.cache_path)
        self._dirty = 0

    def _mark_dirty(self) -> None:
        self._dirty += 1
        if self._dirty >= self.cache_flush_every:
            self.flush_cache()

    # -------------------------------------------------------------- sources

    def _get_with_retry(
        self, url: str, params: dict[str, Any], source: str, isbn: str
    ):
        """GET with bounded exponential backoff on retryable statuses
        (5xx/429) and transient network errors. Returns the final Response, or
        None if the request could not be completed. Sets ``self._transient``
        when a retryable failure is not overcome, so the caller can decline to
        cache a result that a re-run might resolve.
        """
        delays = iter(GOOGLE_429_DELAYS)
        while True:
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as exc:
                delay = next(delays, None)
                if delay is None:
                    logger.warning(
                        "%s network error for %s: %s — retries exhausted",
                        source, isbn, exc,
                    )
                    self._transient = True
                    return None
                logger.info(
                    "%s network error for %s — backing off %.1fs", source, isbn, delay
                )
                self.sleep(delay)
                continue
            except requests.RequestException as exc:
                logger.warning("%s request failed for %s: %s", source, isbn, exc)
                return None
            if resp.status_code in RETRYABLE_STATUS:
                delay = next(delays, None)
                if delay is None:
                    logger.warning(
                        "%s HTTP %d for %s: retries exhausted",
                        source, resp.status_code, isbn,
                    )
                    self._transient = True
                    return None
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                logger.info(
                    "%s HTTP %d for %s — backing off %.1fs",
                    source, resp.status_code, isbn, delay,
                )
                self.sleep(delay)
                continue
            return resp

    def _fetch_google(self, isbn: str) -> dict[str, Any] | None:
        """Google Books volume lookup with 429/5xx backoff-and-retry."""
        params = {"q": f"isbn:{isbn}"}
        api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
        if api_key:
            params["key"] = api_key
        resp = self._get_with_retry(GOOGLE_URL, params, "google books", isbn)
        if resp is None:
            return None
        if resp.status_code != 200:
            logger.warning("google books HTTP %d for %s", resp.status_code, isbn)
            return None
        data = resp.json()
        items = data.get("items") or []
        return items[0].get("volumeInfo") if items else None

    def _fetch_openlibrary(self, isbn: str) -> dict[str, Any] | None:
        resp = self._get_with_retry(
            OPENLIBRARY_URL,
            {"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
            "openlibrary",
            isbn,
        )
        if resp is None:
            return None
        if resp.status_code != 200:
            logger.warning("openlibrary HTTP %d for %s", resp.status_code, isbn)
            return None
        return resp.json().get(f"ISBN:{isbn}")

    def _fetch_loc(self, isbn: str) -> str | None:
        """LoC SRU MARCXML fetch; degrades to disabled on connectivity failure."""
        if self._loc_available is False:
            return None
        try:
            resp = self.session.get(
                LOC_SRU_URL,
                params={
                    "version": "1.1",
                    "operation": "searchRetrieve",
                    "query": f"bath.isbn={isbn}",
                    "maximumRecords": "1",
                    "recordSchema": "marcxml",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            # Deliberately NOT marked transient: LoC misses most ISBNs anyway,
            # and treating an LoC-down run as uncacheable would stop the whole
            # cache from filling. Google/OpenLibrary carry the transient guard.
            self._loc_consecutive_failures += 1
            if self._loc_consecutive_failures >= LOC_MAX_CONSECUTIVE_FAILURES:
                if self._loc_available is not False:
                    logger.warning(
                        "LoC SRU unreachable %d times in a row (%s) — disabling "
                        "it for the rest of this run (known issue: the endpoint "
                        "is blocked from some networks)",
                        self._loc_consecutive_failures, exc,
                    )
                self._loc_available = False
            else:
                logger.info(
                    "LoC SRU connection failure %d/%d for %s — will try again",
                    self._loc_consecutive_failures,
                    LOC_MAX_CONSECUTIVE_FAILURES, isbn,
                )
            return None
        except requests.RequestException as exc:
            logger.warning("LoC SRU request failed for %s: %s", isbn, exc)
            return None
        self._loc_available = True
        self._loc_consecutive_failures = 0
        if resp.status_code != 200:
            logger.warning("LoC SRU HTTP %d for %s", resp.status_code, isbn)
            return None
        # A zero-record SRU response is still a 200 with a body — treat it as
        # a miss (otherwise the ISBN-10 retry never fires).
        if f"{{{_MARCXML_NS['m']}}}record" not in _marcxml_record_tags(resp.text):
            return None
        return resp.text

    def _fetch_all(self, isbn: str) -> dict[str, Any]:
        payloads: dict[str, Any] = {}
        payloads["google"] = self._fetch_google(isbn)
        self.sleep(INTER_CALL_SLEEP)
        payloads["openlibrary"] = self._fetch_openlibrary(isbn)
        self.sleep(INTER_CALL_SLEEP)
        payloads["loc"] = self._fetch_loc(isbn)
        self.sleep(INTER_CALL_SLEEP)
        return payloads

    # -------------------------------------------------------------- parsing

    @staticmethod
    def _parse_loc_marcxml(xml_text: str) -> dict[str, str | None]:
        """Extract 050 (call number), 010 (LCCN), 245$a (title fallback), and
        the language code from the record's own 008/35-37 (041$a as backup).

        LoC's 008 already carries a MARC language code, so it needs no mapping
        — it is the most authoritative language signal available and is read
        ahead of Google's ISO 639-1 guess.
        """
        out: dict[str, str | None] = {
            "call_number": None, "lccn": None, "title": None, "language": None,
        }
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return out
        for controlfield in root.iter(f"{{{_MARCXML_NS['m']}}}controlfield"):
            if controlfield.get("tag") != "008":
                continue
            data = controlfield.text or ""
            if len(data) >= 38:
                out["language"] = marc_language(data[35:38])
            break
        for datafield in root.iter(f"{{{_MARCXML_NS['m']}}}datafield"):
            tag = datafield.get("tag")
            subs = {
                sf.get("code"): (sf.text or "").strip()
                for sf in datafield.findall(f"{{{_MARCXML_NS['m']}}}subfield")
            }
            if tag == "050" and out["call_number"] is None:
                parts = [subs.get("a", ""), subs.get("b", "")]
                joined = " ".join(p for p in parts if p).strip()
                out["call_number"] = joined or None
            elif tag == "010" and out["lccn"] is None:
                out["lccn"] = subs.get("a", "").strip() or None
            elif tag == "041" and out["language"] is None:
                out["language"] = marc_language(subs.get("a", ""))
            elif tag == "245" and out["title"] is None:
                out["title"] = subs.get("a", "").strip(" /:") or None
        return out

    def _resolve(self, isbn13: str, payloads: dict[str, Any]) -> BookMetadata:
        google = payloads.get("google")
        ol = payloads.get("openlibrary")
        loc = (
            self._parse_loc_marcxml(payloads["loc"])
            if payloads.get("loc")
            else {"call_number": None, "lccn": None, "title": None,
                  "language": None}
        )

        meta = BookMetadata(isbn13=isbn13)

        # Title/subtitle: google -> openlibrary -> loc 245 fallback.
        if google and google.get("title"):
            meta.title = google["title"].strip()
            meta.subtitle = (google.get("subtitle") or "").strip() or None
        elif ol and ol.get("title"):
            meta.title = ol["title"].strip()
            meta.subtitle = (ol.get("subtitle") or "").strip() or None
        elif loc["title"]:
            meta.title = loc["title"]

        # Author: google -> openlibrary.
        if google and google.get("authors"):
            meta.author = ", ".join(a.strip() for a in google["authors"] if a.strip())
        elif ol and ol.get("authors"):
            names = [a.get("name", "").strip() for a in ol["authors"]]
            meta.author = ", ".join(n for n in names if n) or None

        # Call number: openlibrary (primary) -> loc -> class-level fallback.
        # OpenLibrary's lc_classifications list sometimes carries a blank
        # entry ahead of the real one (e.g. ["", "BM535 .C37 2001"]) — skip
        # blanks rather than trusting index 0.
        ol_lc = None
        if ol:
            raw_lc = (ol.get("classifications") or {}).get("lc_classifications") or []
            ol_lc = next((c.strip() for c in raw_lc if c and c.strip()), None)
        if ol_lc:
            meta.call_number = ol_lc
            meta.call_number_source = "openlibrary"
            meta.confidence = "high"
        elif loc["call_number"]:
            meta.call_number = loc["call_number"]
            meta.call_number_source = "loc"
            meta.confidence = "high"
        elif meta.title:
            # Only titled books ship — an untitled book is demoted to MANUAL and
            # never reaches MARC, so only titled books need a generated number.
            # Inferred class from categories/subjects, else the last-resort
            # default so a shippable book never leaves blank (see the class
            # map's default_class).
            lc_class = class_fallback(self.class_map, google, ol)
            source = "class_fallback"
            if lc_class is None:
                lc_class = self.class_map.default_class
                source = "default"
            # Turn the inferred (or default) class into a shelf-able LC-shaped
            # number by adding a local Cutter (from the main entry) and the
            # year. No network — see lc_call.py. Still confidence=low: the
            # subject class is a guess even though the Cutter/year are exact.
            year = extract_year(
                (google or {}).get("publishedDate"),
                (ol or {}).get("publish_date"),
            )
            meta.call_number = build_call_number(
                lc_class,
                author=meta.author,
                title=meta.title,
                year=year,
                corporate=is_corporate_author(meta.author or ""),
            )
            meta.call_number_source = source
            meta.confidence = "low"

        # Language: loc (already a MARC code, cataloged) -> google (ISO 639-1,
        # mapped). Left None when neither reports one; marc_build then stamps
        # the configured default. Small-library collections are often not
        # monolingual, so this is not a formality — see language.py.
        meta.language = loc["language"] or marc_language(
            (google or {}).get("language")
        )

        # LCCN: openlibrary identifiers -> loc 010.
        if ol:
            lccns = (ol.get("identifiers") or {}).get("lccn")
            if lccns:
                meta.lccn = lccns[0].strip() or None
        if meta.lccn is None and loc["lccn"]:
            meta.lccn = loc["lccn"]

        return meta

    # ---------------------------------------------------------------- public

    @staticmethod
    def _all_missed(payloads: dict[str, Any]) -> bool:
        return not any(payloads.get(k) for k in ("google", "openlibrary", "loc"))

    def lookup(self, isbn13: str) -> BookMetadata:
        """Resolve metadata for a canonical ISBN-13 (cache-first)."""
        entry = self._cache.get(isbn13)
        if entry is None:
            self._transient = False
            entry = self._fetch_all(isbn13)
            unrecovered = self._transient
            # ISBN-10 retry: pre-2007 titles are often indexed only under
            # their ISBN-10 form.
            if self._all_missed(entry):
                isbn10 = isbn13_to_10(isbn13)
                if isbn10:
                    logger.info(
                        "all sources missed %s — retrying as ISBN-10 %s",
                        isbn13, isbn10,
                    )
                    self._transient = False
                    retry = self._fetch_all(isbn10)
                    retry["retried_as_isbn10"] = isbn10
                    if not self._all_missed(retry):
                        # Adopting the retry round wholesale: its own transient
                        # status is what decides cacheability, not the
                        # discarded 13-form round's.
                        entry = retry
                        unrecovered = self._transient
                    else:
                        unrecovered = unrecovered or self._transient
            if unrecovered:
                # A retryable source error was not overcome this run. Return
                # best-effort metadata but do NOT cache it, so a re-run
                # re-fetches instead of locking in a transient miss.
                logger.info(
                    "not caching %s — unrecovered transient source error", isbn13
                )
                return self._resolve(isbn13, entry)
            self._cache[isbn13] = entry
            self._mark_dirty()
        return self._resolve(isbn13, entry)
