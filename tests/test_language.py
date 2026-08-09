"""Tests for language.py — MARC 008/35-37 language code normalization.

Context: an early version of this pipeline stamped every record 'eng' because
the 008 template hardcoded it — wrong for any collection with non-English
material, and silently wrong. These tests pin the normalization that fixes
that; the corpus-wide default now lives in config, not here.
"""

from retrocat.language import marc_language


class TestISO639_1:
    def test_english(self):
        assert marc_language("en") == "eng"

    def test_arabic(self):
        # The whole reason this module exists.
        assert marc_language("ar") == "ara"

    def test_urdu_and_persian(self):
        assert marc_language("ur") == "urd"
        assert marc_language("fa") == "per"

    def test_region_subtag_is_dropped(self):
        # Google Books returns 'en-US' / 'pt-BR' style values; MARC has three
        # characters and no room for a region.
        assert marc_language("en-US") == "eng"
        assert marc_language("pt-BR") == "por"

    def test_script_subtag_is_dropped(self):
        assert marc_language("zh-Hant") == "chi"

    def test_underscore_separator_tolerated(self):
        assert marc_language("en_GB") == "eng"

    def test_case_and_whitespace_insensitive(self):
        assert marc_language("  AR  ") == "ara"


class TestBibliographicVariants:
    """MARC uses ISO 639-2/B, not /T. Getting this wrong is silent: the record
    still imports, just under a code that is not in the MARC list."""

    def test_persian_is_per_not_fas(self):
        assert marc_language("fas") == "per"

    def test_german_is_ger_not_deu(self):
        assert marc_language("deu") == "ger"

    def test_french_is_fre_not_fra(self):
        assert marc_language("fra") == "fre"

    def test_chinese_is_chi_not_zho(self):
        assert marc_language("zho") == "chi"

    def test_already_bibliographic_passes_through(self):
        assert marc_language("ara") == "ara"
        assert marc_language("eng") == "eng"

    def test_unknown_three_letter_code_passes_through(self):
        # The full MARC list is far longer than our map; a well-formed 3-letter
        # code we don't recognize is far likelier to be real than noise.
        assert marc_language("nob") == "nob"


class TestNoSignal:
    def test_none_and_empty(self):
        assert marc_language(None) is None
        assert marc_language("") is None
        assert marc_language("   ") is None

    def test_undetermined_and_no_content_are_not_signals(self):
        # 'und'/'zxx' are valid MARC codes but tell us nothing, so the caller
        # should fall through to the next source rather than lock them in.
        assert marc_language("und") is None
        assert marc_language("zxx") is None

    def test_unrecognized_two_letter_code(self):
        assert marc_language("qq") is None

    def test_non_alphabetic_rejected(self):
        assert marc_language("12") is None
        assert marc_language("e1") is None

    def test_unknown_long_word_rejected(self):
        assert marc_language("klingon") is None


class TestOperatorEnteredNames:
    """The manual worklist is filled by a human at a shelf, who writes
    'Arabic', not 'ara'."""

    def test_plain_names(self):
        assert marc_language("Arabic") == "ara"
        assert marc_language("English") == "eng"
        assert marc_language("urdu") == "urd"

    def test_farsi_alias_maps_to_persian(self):
        assert marc_language("Farsi") == "per"
        assert marc_language("Persian") == "per"

    def test_name_with_surrounding_whitespace(self):
        assert marc_language("  Turkish ") == "tur"
