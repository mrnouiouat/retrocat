"""Language codes for the MARC 008/35-37 language field.

Sources report language in different vocabularies: Google Books returns
BCP-47 / ISO 639-1 (``en``, ``ar``, sometimes ``en-US``), while a LoC MARCXML
record already carries a MARC code in its own 008. MARC uses the ISO 639-2/B
("bibliographic") variants, which differ from the 639-2/T ("terminological")
ones for ~20 languages — ``per`` not ``fas``, ``ger`` not ``deu``, ``fre`` not
``fra``. Getting that wrong is silent: the record still imports, it is just
filed under a language that does not exist in the MARC code list.

Small-library collections are frequently not monolingual (the deployment this
tool grew out of held substantial Arabic, Urdu and Persian material), so the
008 language is resolved per book; the corpus-wide default lives in config
(``[library].marc_language``), not here.
"""

from __future__ import annotations

# Codes that carry no usable signal: 'und' (undetermined) and 'zxx' (no
# linguistic content) are real MARC codes but tell us nothing, so a source
# reporting them is treated as silent and the next source gets a turn.
_NO_SIGNAL = frozenset({"und", "zxx"})

# ISO 639-1 (Google Books' vocabulary) -> MARC. Weighted toward languages
# common in small religious/academic collections (Arabic, Urdu, Persian,
# Turkish, Malay/Indonesian, Bengali, Somali, Bosnian/Albanian) plus the
# common European languages.
ISO639_1_TO_MARC: dict[str, str] = {
    "en": "eng", "ar": "ara", "ur": "urd", "fa": "per", "tr": "tur",
    "ps": "pus", "ku": "kur", "he": "heb", "sy": "syr", "am": "amh",
    "fr": "fre", "de": "ger", "es": "spa", "it": "ita", "pt": "por",
    "nl": "dut", "sv": "swe", "no": "nor", "da": "dan", "fi": "fin",
    "is": "ice", "el": "gre", "la": "lat", "ga": "gle", "cy": "wel",
    "ru": "rus", "uk": "ukr", "pl": "pol", "cs": "cze", "sk": "slo",
    "hu": "hun", "ro": "rum", "bg": "bul", "sr": "srp", "hr": "hrv",
    "bs": "bos", "sq": "alb", "sl": "slv", "mk": "mac", "et": "est",
    "lv": "lav", "lt": "lit", "hy": "arm", "ka": "geo", "az": "aze",
    "uz": "uzb", "kk": "kaz", "ky": "kir", "tg": "tgk", "tk": "tuk",
    "hi": "hin", "bn": "ben", "pa": "pan", "gu": "guj", "mr": "mar",
    "ne": "nep", "si": "sin", "sa": "san", "ta": "tam", "te": "tel",
    "kn": "kan", "ml": "mal", "or": "ori", "as": "asm",
    "id": "ind", "ms": "may", "tl": "tgl", "th": "tha", "vi": "vie",
    "my": "bur", "km": "khm", "lo": "lao", "bo": "tib",
    "zh": "chi", "ja": "jpn", "ko": "kor", "mn": "mon",
    "sw": "swa", "so": "som", "ha": "hau", "yo": "yor", "ig": "ibo",
    "af": "afr", "zu": "zul", "xh": "xho", "st": "sot", "rw": "kin",
}

# ISO 639-2/T -> 639-2/B, the ~20 codes where the two standards disagree.
# A source handing us 'fas' means Persian; MARC spells that 'per'.
ISO639_2T_TO_MARC_B: dict[str, str] = {
    "sqi": "alb", "hye": "arm", "eus": "baq", "mya": "bur", "zho": "chi",
    "ces": "cze", "nld": "dut", "fra": "fre", "kat": "geo", "deu": "ger",
    "ell": "gre", "isl": "ice", "mkd": "mac", "mri": "mao", "msa": "may",
    "fas": "per", "ron": "rum", "slk": "slo", "bod": "tib", "cym": "wel",
    "hrv": "hrv", "srp": "srp",
}


# Plain English names -> MARC, for the manual worklist only. An operator
# filling in a row at the shelf writes "Arabic", not "ara"; requiring a code
# would just produce blank cells. Machine sources never hit this map.
LANGUAGE_NAME_TO_MARC: dict[str, str] = {
    "english": "eng", "arabic": "ara", "urdu": "urd", "persian": "per",
    "farsi": "per", "turkish": "tur", "pashto": "pus", "kurdish": "kur",
    "hebrew": "heb", "amharic": "amh", "somali": "som", "swahili": "swa",
    "french": "fre", "german": "ger", "spanish": "spa", "italian": "ita",
    "portuguese": "por", "dutch": "dut", "russian": "rus", "greek": "gre",
    "latin": "lat", "bosnian": "bos", "albanian": "alb", "serbian": "srp",
    "croatian": "hrv", "malay": "may", "indonesian": "ind", "bengali": "ben",
    "hindi": "hin", "punjabi": "pan", "gujarati": "guj", "tamil": "tam",
    "sanskrit": "san", "chinese": "chi", "japanese": "jpn", "korean": "kor",
    "thai": "tha", "vietnamese": "vie", "tagalog": "tgl", "filipino": "tgl",
    "hausa": "hau", "yoruba": "yor", "afrikaans": "afr", "polish": "pol",
    "bahasa": "ind",
}


def marc_language(code: str | None) -> str | None:
    """Normalize a source-reported language value to a MARC 008/35-37 code.

    Accepts ISO 639-1 (``ar``), BCP-47 with a region or script subtag
    (``en-US``, ``zh-Hant`` — the subtag is dropped, MARC has no room for it),
    3-letter codes already in MARC or ISO 639-2/T form, and plain English
    language names (for hand-filled worklist rows). Returns None for anything
    unrecognized or signal-free, so the caller falls through to the next source
    rather than stamping a made-up code.
    """
    if not code:
        return None
    primary = code.strip().lower().replace("_", "-").split("-")[0]
    if not primary.isascii() or not primary.isalpha():
        return None
    if len(primary) == 2:
        return ISO639_1_TO_MARC.get(primary)
    if len(primary) == 3:
        if primary in _NO_SIGNAL:
            return None
        # Unrecognized 3-letter codes pass through: the MARC list is far longer
        # than the map above, and a valid-shaped code we don't know is far more
        # likely to be a real MARC code than noise.
        return ISO639_2T_TO_MARC_B.get(primary, primary)
    return LANGUAGE_NAME_TO_MARC.get(primary)
