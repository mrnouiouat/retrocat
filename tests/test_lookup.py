"""Tests for lookup.py, all HTTP mocked via an injected fake session.

Covers: source fallback order,
class-level fallback, nothing-resolves path, 429 backoff, ISBN-10 retry,
response caching, LoC-unreachable degradation, and the org/person heuristic.
No network; sleeps injected so the suite runs instantly.
"""

from __future__ import annotations

import json

import pytest
import requests

from retrocat.lookup import (
    GOOGLE_429_DELAYS,
    BookMetadata,
    LookupClient,
    class_fallback,
    is_corporate_author,
    load_class_map,
)

# The shipped class map (a collection-specific worked example) is what the
# client uses by default, so these tests exercise it directly.
CLASS_MAP = load_class_map()
DEFAULT_LC_CLASS = CLASS_MAP.default_class

# Real, checksum-valid ISBNs (public bibliographic facts).
ISBN = "9780199836741"
ISBN_B = "9780316168717"
ISBN_MERGE = "9781565645998"   # ISBN-10 form: 1565645995 (confirmed pair)
ISBN_MERGE_10 = "1565645995"


# --------------------------------------------------------------------------
# Fake HTTP plumbing, LookupClient only calls session.get(url, params=, timeout=)
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeSession:
    """Routes session.get by URL substring to a canned response, a callable
    (called with the request params), or an exception instance (raised)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for substring, handler in self.routes.items():
            if substring in url:
                result = handler(params) if callable(handler) else handler
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected URL in test: {url}")

    def calls_to(self, substring):
        return [(u, p) for (u, p) in self.calls if substring in u]


def google_hit(**volume_info):
    return FakeResponse(200, {"totalItems": 1, "items": [{"volumeInfo": volume_info}]})


def google_miss():
    return FakeResponse(200, {"totalItems": 0})


def ol_hit(isbn, **data):
    return FakeResponse(200, {f"ISBN:{isbn}": data})


def ol_miss():
    return FakeResponse(200, {})


def loc_miss():
    # Non-200 -> _fetch_loc returns None (payload falsy = source missed).
    return FakeResponse(404)


LOC_MARCXML = """<?xml version="1.0" encoding="UTF-8"?>
<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">
  <zs:records><zs:record><zs:recordData>
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>01234cam a2200301 a 4500</leader>
      <datafield tag="010" ind1=" " ind2=" ">
        <subfield code="a">   94012345 </subfield>
      </datafield>
      <datafield tag="050" ind1="0" ind2="0">
        <subfield code="a">E183.8.I7</subfield>
        <subfield code="b">L63 1994</subfield>
      </datafield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">The secret war :</subfield>
      </datafield>
    </record>
  </zs:recordData></zs:record></zs:records>
</zs:searchRetrieveResponse>"""


def make_client(tmp_path, routes, sleeps=None):
    session = FakeSession(routes)
    client = LookupClient(
        cache_path=tmp_path / "lookup_cache.json",
        session=session,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )
    return client, session


# --------------------------------------------------------------------------
# 1-2. Source fallback order
# --------------------------------------------------------------------------

def test_google_wins_title_openlibrary_wins_call_number(tmp_path):
    """First hit wins per field: title/author from google, call number can
    still come from openlibrary (the primary LC call number source)."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(
            title="Reading the Qur'an",
            subtitle="The Contemporary Relevance",
            authors=["Ziauddin Sardar"],
        ),
        "openlibrary": ol_hit(
            ISBN,
            title="OL Title Should Lose",
            authors=[{"name": "OL Author Should Lose"}],
            classifications={"lc_classifications": ["BP130.4 .S376 2011"]},
        ),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    assert meta.title == "Reading the Qur'an"
    assert meta.subtitle == "The Contemporary Relevance"
    assert meta.author == "Ziauddin Sardar"
    assert meta.call_number == "BP130.4 .S376 2011"
    assert meta.call_number_source == "openlibrary"
    assert meta.confidence == "high"
    assert meta.resolved is True


def test_google_misses_openlibrary_supplies_title_and_author(tmp_path):
    client, _ = make_client(tmp_path, {
        "googleapis": google_miss(),
        "openlibrary": ol_hit(
            ISBN,
            title="The Ornament of the World",
            authors=[{"name": "Maria Rosa Menocal"}],
        ),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    assert meta.title == "The Ornament of the World"
    assert meta.author == "Maria Rosa Menocal"
    assert meta.resolved is True


# --------------------------------------------------------------------------
# 3. LoC MARCXML call number + LCCN
# --------------------------------------------------------------------------

def test_loc_call_number_and_lccn(tmp_path):
    """google/openlibrary give no call number -> LoC 050 $a $b joined, plus
    010 $a as the LCCN."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(title="The secret war"),  # no categories
        "openlibrary": ol_miss(),
        "loc.gov": FakeResponse(200, text=LOC_MARCXML),
    })
    meta = client.lookup(ISBN)
    assert meta.call_number == "E183.8.I7 L63 1994"  # $a and $b joined
    assert meta.call_number_source == "loc"
    assert meta.confidence == "high"
    assert meta.lccn == "94012345"
    assert meta.title == "The secret war"  # google still wins the title


def test_openlibrary_skips_leading_blank_lc_classification(tmp_path):
    """OpenLibrary sometimes returns a blank entry ahead of the real call
    number (real data, e.g. isbn 9780618219087), must not take index 0
    blindly and report a blank string as a high-confidence hit."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_miss(),
        "openlibrary": ol_hit(
            ISBN,
            title="Constantine's Sword",
            classifications={"lc_classifications": ["", "BM535 .C37 2001"]},
        ),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    assert meta.call_number == "BM535 .C37 2001"
    assert meta.call_number_source == "openlibrary"
    assert meta.confidence == "high"


def test_openlibrary_all_blank_lc_classifications_is_a_miss(tmp_path):
    """If every entry in the list is blank, it must NOT be reported as an empty
    high-confidence openlibrary hit. With a title present and no mappable
    category/subject, the book falls through to the local default class rather
    than shipping a blank, but crucially the source is 'default', proving the
    blank OL classification was not consumed."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_miss(),
        "openlibrary": ol_hit(
            ISBN,
            title="100 Years of Lynchings",
            classifications={"lc_classifications": [""]},
        ),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    assert meta.call_number_source == "default"  # not "openlibrary"
    assert meta.confidence == "low"
    assert meta.call_number.startswith(DEFAULT_LC_CLASS)


# --------------------------------------------------------------------------
# 4. Class-level fallback
# --------------------------------------------------------------------------

def test_class_fallback_islam(tmp_path):
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(title="Introducing Islam", categories=["Islam"]),
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    # Bare class "BP" is enriched locally with a Cutter from the main entry
    # (here the title, no author) into a shelf-able number. See lc_call.py.
    assert meta.call_number == "BP .I58"  # "Introducing" -> I58
    assert meta.call_number_source == "class_fallback"
    assert meta.confidence == "low"


@pytest.mark.parametrize("categories, expected", [
    # Every real category observed across the 48 cached pilot+shelf books.
    (["History"], "D"),
    (["Religion"], "BL"),
    (["Political Science"], "J"),
    (["Law"], "K"),
    (["Biography & Autobiography"], "CT"),
    (["Social Science"], "H"),
    (["Philosophy"], "B"),
    (["Civilization"], "CB"),
    (["Koran"], "BP130"),
    (["Burqas (Islamic clothing)"], "BP"),  # 'islamic' substring
    # Ordering: narrower key must beat the broader word it contains.
    (["Islamic law"], "KBP"),               # not 'islam' -> BP
    (["Social Science"], "H"),              # not 'science' -> Q
    (["Business & Economics"], "HB"),       # not 'business' -> HF
    (["Performing Arts"], "PN"),            # not 'art' -> N
    (["Political Science"], "J"),           # not 'science' -> Q
    # Deliberately unmapped -> None (blank, flagged for human review).
    (["Fiction"], None),
    (["Juvenile Fiction"], None),
    (["Billionaires"], None),
    ([], None),
])
def test_class_fallback_map_coverage_and_ordering(categories, expected):
    assert class_fallback(CLASS_MAP, {"categories": categories}) == expected


def test_class_fallback_more_specific_keyword_wins(tmp_path):
    """"Qur'an" maps to BP130 and must beat the broader "Islam" -> BP; the
    class is then enriched with a local Cutter + year."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(
            title="Reflections on the Qur'an",
            categories=["Islam", "Qur'an studies"],
            authors=["Gai Eaton"],
            publishedDate="2012",
        ),
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    assert meta.call_number == "BP130 .E28 2012"  # "Eaton" -> E28, year 2012
    assert meta.call_number_source == "class_fallback"
    assert meta.confidence == "low"


def test_class_fallback_uses_openlibrary_subjects_when_google_unmappable(tmp_path):
    """Google mis-tags (e.g. 'Juvenile Fiction' on a lynching memoir) leave
    categories unmappable, OpenLibrary's curated subject headings, already in
    the cached payload, must be tried as a second signal."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(
            title="A Time of Terror",
            authors=["James Cameron"],
            categories=["Juvenile Fiction"],  # deliberately unmapped mis-tag
            publishedDate="1994",
        ),
        "openlibrary": ol_hit(
            ISBN,
            title="A Time of Terror",
            subjects=[{"name": "Lynching", "url": "https://openlibrary.org/x"},
                      {"name": "History", "url": "https://openlibrary.org/y"}],
        ),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    # "Lynching" -> HV6457, enriched with Cutter (Cameron) + year.
    assert meta.call_number == "HV6457 .C36 1994"
    assert meta.call_number_source == "class_fallback"
    assert meta.confidence == "low"


def test_class_fallback_tolerates_plain_string_subjects():
    # Some OL payload variants carry subjects as plain strings.
    assert class_fallback(CLASS_MAP, 
        None, {"subjects": ["Race relations"]}
    ) == "E185.61"


def test_default_class_when_nothing_maps_never_blank(tmp_path):
    """A titled book whose ONLY signal is a deliberately-unmapped category
    ('Juvenile Fiction') and no OL subjects still leaves with a call number ,
    the last-resort default class enriched with a local Cutter + year. This
    is the A Time of Terror case (9780933121447): online lookup found a title
    and author but no subject, and previously it shipped blank."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(
            title="A Time of Terror",
            authors=["James Cameron"],
            categories=["Juvenile Fiction"],  # unmapped mis-tag
            publishedDate="1994",
        ),
        "openlibrary": ol_hit(ISBN, title="A Time of Terror"),  # no subjects
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    assert meta.call_number == f"{DEFAULT_LC_CLASS} .C36 1994"  # Cameron -> C36
    assert meta.call_number_source == "default"
    assert meta.confidence == "low"
    assert meta.resolved is True  # still a real CREATE, not demoted to MANUAL


def test_default_class_only_applies_when_no_category_matches(tmp_path):
    """The default must NOT pre-empt a real category match, a mappable
    category still wins and is tagged class_fallback, not default."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(title="Introducing Islam", categories=["Islam"]),
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    assert meta.call_number_source == "class_fallback"
    assert meta.call_number.startswith("BP")


# --------------------------------------------------------------------------
# 5. Nothing resolves -> unresolved (pipeline demotes to MANUAL)
# --------------------------------------------------------------------------

def test_nothing_resolves_is_unresolved(tmp_path):
    """No title from any source -> resolved is False. The pipeline demotes
    these to MANUAL; a placeholder CREATE is never fabricated."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_miss(),
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN)
    assert meta.resolved is False
    assert meta.title is None
    assert meta.call_number is None


# --------------------------------------------------------------------------
# 6. Google 429 backoff
# --------------------------------------------------------------------------

def test_google_429_backoff_then_success(tmp_path):
    state = {"n": 0}

    def google_handler(params):
        state["n"] += 1
        if state["n"] <= 2:
            return FakeResponse(429)
        return google_hit(title="Backoff Book", authors=["A. Uthor"])

    sleeps: list[float] = []
    client, session = make_client(tmp_path, {
        "googleapis": google_handler,
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    }, sleeps=sleeps)
    meta = client.lookup(ISBN)
    assert meta.title == "Backoff Book"
    # The two 429s back off with the first two configured delays, before any
    # inter-call politeness sleeps.
    assert sleeps[:2] == list(GOOGLE_429_DELAYS[:2])
    assert state["n"] == 3


def test_google_429_forever_exhausts_retries_without_exception(tmp_path):
    sleeps: list[float] = []
    client, session = make_client(tmp_path, {
        "googleapis": FakeResponse(429),
        "openlibrary": ol_hit(ISBN, title="Fallback Title"),
        "loc.gov": loc_miss(),
    }, sleeps=sleeps)
    meta = client.lookup(ISBN)  # must not raise
    # google returned nothing; openlibrary still supplied the title.
    assert meta.title == "Fallback Title"
    # Every configured delay was consumed before giving up.
    assert [s for s in sleeps if s in GOOGLE_429_DELAYS] == list(GOOGLE_429_DELAYS)
    # And the direct fetch reports the miss as None, not an exception.
    assert client._fetch_google(ISBN_B) is None


# --------------------------------------------------------------------------
# 6b. Transient 5xx retry + cache-poison guard
# --------------------------------------------------------------------------

def test_openlibrary_retries_on_503_then_succeeds(tmp_path):
    """A burst of 503s from OpenLibrary (the failure the operator hit) must be
    ridden out with backoff, not turned into a permanent miss."""
    state = {"n": 0}

    def ol_handler(params):
        state["n"] += 1
        if state["n"] <= 2:
            return FakeResponse(503)
        return ol_hit(
            ISBN,
            title="Resilient Book",
            classifications={"lc_classifications": ["BP130 .R47 2011"]},
        )

    sleeps: list[float] = []
    client, _ = make_client(tmp_path, {
        "googleapis": google_miss(),
        "openlibrary": ol_handler,
        "loc.gov": loc_miss(),
    }, sleeps=sleeps)
    meta = client.lookup(ISBN)
    assert meta.call_number == "BP130 .R47 2011"
    assert state["n"] == 3  # two 503s + one success
    # The two 503s backed off with the first two configured delays (ignoring
    # the 0.2s inter-source politeness sleeps).
    backoff = [s for s in sleeps if s in GOOGLE_429_DELAYS]
    assert backoff[:2] == list(GOOGLE_429_DELAYS[:2])
    # A recovered transient is a clean result, it gets cached normally.
    client.flush_cache()
    cached = json.loads((tmp_path / "lookup_cache.json").read_text(encoding="utf-8"))
    assert ISBN in cached


def test_unrecovered_transient_error_is_not_cached(tmp_path):
    """If 503s never clear, the ISBN must NOT be cached, a re-run re-fetches
    it rather than locking in a blank call number."""
    routes = {
        "googleapis": google_hit(title="Flaky Source Book"),
        "openlibrary": FakeResponse(503),  # 503 forever
        "loc.gov": loc_miss(),
    }
    client, session = make_client(tmp_path, routes)
    meta = client.lookup(ISBN)
    assert meta.title == "Flaky Source Book"  # best-effort result still returned
    calls_after_first = len(session.calls)

    # Not cached: a second lookup re-hits the network instead of serving a
    # poisoned entry.
    client.lookup(ISBN)
    assert len(session.calls) > calls_after_first
    client.flush_cache()
    cached = json.loads((tmp_path / "lookup_cache.json").read_text(encoding="utf-8"))
    assert ISBN not in cached


def test_google_retries_on_503(tmp_path):
    """Google's backend 503s sporadically (observed live), retry, don't
    immediately fall through to a miss."""
    state = {"n": 0}

    def google_handler(params):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResponse(503)
        return google_hit(title="Google Recovered", authors=["A. Uthor"])

    sleeps: list[float] = []
    client, _ = make_client(tmp_path, {
        "googleapis": google_handler,
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    }, sleeps=sleeps)
    meta = client.lookup(ISBN)
    assert meta.title == "Google Recovered"
    assert state["n"] == 2


def test_clean_isbn10_retry_is_cached_despite_flaky_isbn13_round(tmp_path):
    """A transient blip in the DISCARDED ISBN-13 round must not block caching
    when the adopted ISBN-10 retry round fetched cleanly."""

    def google_handler(params):
        if params["q"] == f"isbn:{ISBN_MERGE_10}":
            return google_hit(title="Found Under ISBN-10")
        return FakeResponse(503)  # 13-form round: 503 forever (transient)

    client, session = make_client(tmp_path, {
        "googleapis": google_handler,
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN_MERGE)
    assert meta.title == "Found Under ISBN-10"
    # The adopted retry round was clean -> the entry IS cached; a second
    # lookup makes zero new HTTP calls.
    calls = len(session.calls)
    again = client.lookup(ISBN_MERGE)
    assert len(session.calls) == calls
    assert again == meta


# --------------------------------------------------------------------------
# 7. ISBN-10 retry
# --------------------------------------------------------------------------

def test_isbn10_retry_when_all_sources_miss_isbn13(tmp_path):
    """Pre-2007 titles are often indexed only under ISBN-10: all three
    sources miss the ISBN-13 key, google hits the ISBN-10 form."""

    def google_handler(params):
        if params["q"] == f"isbn:{ISBN_MERGE_10}":
            return google_hit(
                title="Abu Zayd al-Balkhi's Sustenance of the Soul",
                authors=["Malik Badri"],
            )
        return google_miss()

    client, session = make_client(tmp_path, {
        "googleapis": google_handler,
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    meta = client.lookup(ISBN_MERGE)
    assert meta.resolved is True
    assert meta.title == "Abu Zayd al-Balkhi's Sustenance of the Soul"
    assert meta.isbn13 == ISBN_MERGE  # keyed by the canonical 13 form
    # The session saw a second round of calls keyed by the ISBN-10 form.
    google_queries = [p["q"] for (_, p) in session.calls_to("googleapis")]
    assert google_queries == [f"isbn:{ISBN_MERGE}", f"isbn:{ISBN_MERGE_10}"]
    ol_bibkeys = [p["bibkeys"] for (_, p) in session.calls_to("openlibrary")]
    assert ol_bibkeys == [f"ISBN:{ISBN_MERGE}", f"ISBN:{ISBN_MERGE_10}"]


# --------------------------------------------------------------------------
# 8. Caching
# --------------------------------------------------------------------------

def test_cache_prevents_repeat_http_and_persists(tmp_path):
    routes = {
        "googleapis": google_hit(title="Cached Book", authors=["C. Acher"]),
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    }
    client, session = make_client(tmp_path, routes)
    first = client.lookup(ISBN)
    calls_after_first = len(session.calls)
    assert calls_after_first == 3  # one call per source

    second = client.lookup(ISBN)
    assert len(session.calls) == calls_after_first  # zero new HTTP calls
    assert second == first

    client.flush_cache()
    cache_file = tmp_path / "lookup_cache.json"
    assert cache_file.exists()
    assert ISBN in json.loads(cache_file.read_text(encoding="utf-8"))

    # A brand-new client reloads the cache and never touches the network.
    client2, session2 = make_client(tmp_path, routes)
    meta2 = client2.lookup(ISBN)
    assert session2.calls == []
    assert meta2 == first


# --------------------------------------------------------------------------
# 9. LoC unreachable -> degrade once, never retry
# --------------------------------------------------------------------------

def test_loc_unreachable_degrades_after_threshold_not_first_blip(tmp_path):
    """Known issue: LoC SRU is unreachable from some networks. The run must
    continue without it, but ONE mid-run blip must not kill the source for
    the rest of a long run. LoC is disabled only after
    LOC_MAX_CONSECUTIVE_FAILURES consecutive connection failures."""
    from retrocat.lookup import LOC_MAX_CONSECUTIVE_FAILURES

    client, session = make_client(tmp_path, {
        "googleapis": google_hit(title="Still Works"),
        "openlibrary": ol_miss(),
        "loc.gov": requests.ConnectionError("simulated: endpoint unreachable"),
    })
    # Distinct checksum-valid ISBNs so each lookup is a fresh fetch round.
    isbns = [ISBN, ISBN_B, ISBN_MERGE, "9781565646988", "9781101981023"]
    for isbn in isbns:
        meta = client.lookup(isbn)
        assert meta.title == "Still Works"  # degraded, never crashed
    # LoC was probed exactly threshold times, then never again.
    assert len(session.calls_to("loc.gov")) == LOC_MAX_CONSECUTIVE_FAILURES


def test_loc_success_resets_failure_counter(tmp_path):
    """Intermittent LoC blips with successes in between never disable it."""
    state = {"n": 0}

    def loc_handler(params):
        state["n"] += 1
        if state["n"] % 2 == 1:  # odd calls fail, even calls succeed
            raise requests.ConnectionError("blip")
        return loc_miss()  # a reachable-but-no-record response

    client, session = make_client(tmp_path, {
        "googleapis": google_hit(title="Still Works"),
        "openlibrary": ol_miss(),
        "loc.gov": loc_handler,
    })
    isbns = [ISBN, ISBN_B, ISBN_MERGE, "9781565646988", "9781101981023",
             "9780882970264"]
    for isbn in isbns:
        client.lookup(isbn)
    # Alternating fail/success never reaches 3 consecutive failures, so LoC
    # was attempted on every fetch round (2 rounds per lookup: 13 + 10 retry
    # can add more, but at minimum one per lookup).
    assert len(session.calls_to("loc.gov")) >= len(isbns)


# --------------------------------------------------------------------------
# 10. Corporate-author heuristic
# --------------------------------------------------------------------------

def test_is_corporate_author():
    assert is_corporate_author("Center for Constitutional Rights (New York, N.Y.)")
    assert not is_corporate_author("Malik Badri")


# --------------------------------------------------------------------------
# Language resolution (MARC 008/35-37)
# --------------------------------------------------------------------------
# Until this landed, every record was stamped 'eng' regardless of source ,
# wrong for any collection with non-English material.

LOC_MARCXML_ARABIC = """<?xml version="1.0" encoding="UTF-8"?>
<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">
  <zs:records><zs:record><zs:recordData>
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>01234cam a2200301 a 4500</leader>
      <controlfield tag="008">940112s1994    xx            000 0 ara d</controlfield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Al-Kitab :</subfield>
      </datafield>
    </record>
  </zs:recordData></zs:record></zs:records>
</zs:searchRetrieveResponse>"""


def test_google_language_is_mapped_to_marc(tmp_path):
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(title="Kitab al-Tawhid", language="ar"),
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    assert client.lookup(ISBN).language == "ara"


def test_google_language_region_subtag_is_dropped(tmp_path):
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(title="A Book", language="en-US"),
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    assert client.lookup(ISBN).language == "eng"


def test_loc_008_language_wins_over_google(tmp_path):
    """LoC's 008 is a cataloged MARC code, more authoritative than Google's
    ISO 639-1 guess, so it is read first."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(title="Al-Kitab", language="en"),
        "openlibrary": ol_miss(),
        "loc.gov": FakeResponse(200, text=LOC_MARCXML_ARABIC),
    })
    assert client.lookup(ISBN).language == "ara"


def test_no_source_reports_a_language(tmp_path):
    """Left None here; marc_build stamps DEFAULT_LANGUAGE rather than blank."""
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(title="A Book"),  # no language key
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    assert client.lookup(ISBN).language is None


def test_unmappable_google_language_is_not_invented(tmp_path):
    client, _ = make_client(tmp_path, {
        "googleapis": google_hit(title="A Book", language="qq"),
        "openlibrary": ol_miss(),
        "loc.gov": loc_miss(),
    })
    assert client.lookup(ISBN).language is None
