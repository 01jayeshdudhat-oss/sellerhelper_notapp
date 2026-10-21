import logging
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import NamedTuple

from django.conf import settings

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)
LANGUAGE_SCRIPTS = {
    "as": "Assamese",
    "bn": "Bengali",
    "brx": "Devanagari",
    "doi": "Dogra",
    "gu": "Gujarati",
    "hi": "Devanagari",
    "kn": "Kannada",
    "ks": "Sharada",
    "kok": "Devanagari",
    "mai": "Tirhuta",
    "ml": "Malayalam",
    "mni": "MeeteiMayek",
    "mr": "Devanagari",
    "ne": "Devanagari",
    "or": "Oriya",
    "pa": "Gurmukhi",
    "sa": "Devanagari",
    "sat": "Santali",
    "sd": "Urdu",
    "ta": "Tamil",
    "te": "Telugu",
    "ur": "Urdu",
}
_SEARCH_KEYWORD_LANGUAGE_LABELS = {
    "ARABIC": "Urdu / Sindhi (Arabic script)",
    "BENGALI": "Bengali / Assamese",
    "DEVANAGARI": "Hindi / Marathi / Nepali / Sanskrit (Devanagari)",
    "GURMUKHI": "Punjabi (Gurmukhi)",
    "GUJARATI": "Gujarati",
    "KANNADA": "Kannada",
    "LATIN": "English (Latin)",
    "MALAYALAM": "Malayalam",
    "MEETEI MAYEK": "Manipuri (Meitei Mayek)",
    "OL CHIKI": "Santali (Ol Chiki)",
    "ORIYA": "Odia",
    "SHARADA": "Kashmiri (Sharada)",
    "TAMIL": "Tamil",
    "TELUGU": "Telugu",
    "TIRHUTA": "Maithili (Tirhuta)",
}


class ProductKeywordMatches(NamedTuple):
    keywords: list[str]
    dictionary_matched: bool


def format_search_keywords_by_language(keywords):
    if isinstance(keywords, str):
        if "\n\n" in keywords and any(
            line.strip() in _SEARCH_KEYWORD_LANGUAGE_LABELS.values()
            or line.strip() in {"Numeric identifiers", "Other search terms"}
            for line in keywords.splitlines()
        ):
            return keywords
        keyword_phrases = keywords.split()
    else:
        keyword_phrases = keywords

    grouped_keywords = {}
    seen_by_language = {}
    for phrase in keyword_phrases:
        phrase = " ".join(str(phrase or "").split())
        if not phrase:
            continue
        first_letter = next(
            (
                character
                for character in phrase
                if unicodedata.category(character)[0] == "L"
            ),
            "",
        )
        if not first_letter and phrase.isdigit():
            language_label = "Numeric identifiers"
        elif not first_letter:
            language_label = "Other search terms"
        else:
            character_name = unicodedata.name(first_letter, "")
            language_label = next(
                (
                    label
                    for script_name, label in _SEARCH_KEYWORD_LANGUAGE_LABELS.items()
                    if script_name in character_name
                ),
                "Other search terms",
            )
        seen = seen_by_language.setdefault(language_label, set())
        if phrase.casefold() in seen:
            continue
        seen.add(phrase.casefold())
        grouped_keywords.setdefault(language_label, []).append(phrase)

    return "\n\n".join(
        f"{language_label}\n\n{' '.join(language_keywords)}"
        for language_label, language_keywords in grouped_keywords.items()
    )


def normalize_search_text(value):
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = "".join(
        unicodedata.lookup(unicodedata.name(character).replace("CANDRABINDU", "ANUSVARA"))
        if "CANDRABINDU" in unicodedata.name(character, "")
        and _has_unicode_name(
            unicodedata.name(character, "").replace("CANDRABINDU", "ANUSVARA")
        )
        else character
        for character in normalized
    )

    latin_folded = []
    last_base_is_latin = False
    for character in unicodedata.normalize("NFD", normalized):
        category = unicodedata.category(character)
        name = unicodedata.name(character, "")
        if category.startswith("M"):
            if last_base_is_latin or (
                ord(character) in range(0x0610, 0x061B)
                or ord(character) in range(0x064B, 0x0660)
                or ord(character) == 0x0670
                or ord(character) in range(0x06D6, 0x06EE)
            ):
                continue
        latin_folded.append(character)
        if category.startswith("L"):
            last_base_is_latin = "LATIN" in unicodedata.name(character, "")
        elif not category.startswith("M"):
            last_base_is_latin = False

    normalized = unicodedata.normalize("NFC", "".join(latin_folded))
    trailing = len(normalized)
    while trailing and normalized[trailing - 1].isspace():
        trailing -= 1
    if trailing and "VIRAMA" in unicodedata.name(normalized[trailing - 1], ""):
        normalized = normalized[:trailing - 1] + normalized[trailing:]
    return normalized


def _has_unicode_name(name):
    try:
        unicodedata.lookup(name)
        return True
    except KeyError:
        return False


def _transliterate_token(source_script, target_script, token):
    import ast
    import importlib

    added_str_compatibility = not hasattr(ast, "Str")
    if added_str_compatibility:
        # Aksharamukha still imports ast.Str, which Python 3.14 removed.
        setattr(ast, "Str", ast.Constant)
    try:
        transliterate = importlib.import_module("aksharamukha.transliterate")
    finally:
        if added_str_compatibility:
            delattr(ast, "Str")
    return transliterate.process(source_script, target_script, token)


def dictionary_path():
    configured_path = getattr(settings, "PRODUCT_KEYWORD_DICTIONARY_PATH", "").strip()
    if configured_path:
        return Path(configured_path).expanduser()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        data_root = Path(local_app_data)
    else:
        data_root = Path(
            os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        )
    return data_root / "SellerHelper" / "keyword-dictionary" / "product_keywords.sqlite3"


def dictionary_is_ready():
    path = dictionary_path()
    if not path.is_file():
        return False

    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'dictionary'"
            ).fetchone()
            if row is None:
                return False
            return connection.execute("SELECT 1 FROM dictionary LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        logger.exception("The local product keyword dictionary could not be opened.")
        return False


def lookup_product_keywords(source_text):
    tokens = list(dict.fromkeys(
        token for token in _TOKEN_RE.findall(source_text) if token.strip()
    ))
    if not tokens:
        return ProductKeywordMatches([], False)

    path = dictionary_path()
    translated = []
    translated_languages_by_token = {}
    if path.is_file():
        try:
            connection = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro",
                uri=True,
            )
            try:
                for token in tokens:
                    normalized = token.casefold()
                    token_languages = set()
                    english_words = {
                        normalized,
                        *(
                            row[0].casefold()
                            for row in connection.execute(
                                "SELECT DISTINCT source_key FROM dictionary "
                                "WHERE target_key = ?",
                                (normalized,),
                            )
                        ),
                    }
                    for english_word in english_words:
                        for target_word, language_code in connection.execute(
                            "SELECT target_word, target_language "
                            "FROM dictionary WHERE source_key = ?",
                            (english_word,),
                        ):
                            translated.append(target_word)
                            token_languages.add(language_code)
                    translated.extend(
                        row[0]
                        for row in connection.execute(
                            "SELECT source_word FROM dictionary WHERE target_key = ?",
                            (normalized,),
                        )
                    )
                    translated_languages_by_token[token] = token_languages
            finally:
                connection.close()
        except sqlite3.Error:
            logger.exception(
                "Offline product keyword lookup failed; using script aliases only."
            )
            translated = []
            translated_languages_by_token = {}
    else:
        logger.warning(
            "Offline product keyword dictionary is not installed; "
            "adding script aliases where possible."
        )

    transliteration_groups = {}
    for token in tokens:
        original_token = token
        if not token.isalpha():
            continue
        source_script = "autodetect"
        if token.isascii() and token.isalpha():
            source_script = "RomanReadable"
            token = token.casefold()
        translated_scripts = {
            LANGUAGE_SCRIPTS[language_code]
            for language_code in translated_languages_by_token.get(
                original_token,
                set(),
            )
            if language_code in LANGUAGE_SCRIPTS
        }
        for target_script in dict.fromkeys(LANGUAGE_SCRIPTS.values()):
            if target_script in translated_scripts:
                continue
            transliteration_groups.setdefault(
                (source_script, target_script),
                [],
            ).append(token)

    for (source_script, target_script), group_tokens in transliteration_groups.items():
        alias = _transliterate_token(
            source_script,
            target_script,
            " ".join(dict.fromkeys(group_tokens)),
        ).strip()
        if alias:
            translated.append(alias)

    return ProductKeywordMatches(
        list(dict.fromkeys(
            value.strip()
            for value in translated
            if value and value.strip()
        )),
        any(translated_languages_by_token.values()),
    )
