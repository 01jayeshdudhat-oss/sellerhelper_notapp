import gzip
import json
import os
import sqlite3
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from seller.offline_keyword_dictionary import dictionary_path


DICTIONARY_URL = (
    "https://kaikki.org/dictionary/English/"
    "kaikki.org-dictionary-English.jsonl"
)
LANGUAGE_CODES = {
    "as": {"as", "asm"},
    "bn": {"bn", "ben"},
    "brx": {"brx"},
    "doi": {"doi"},
    "gu": {"gu", "guj"},
    "hi": {"hi", "hin"},
    "kn": {"kn", "kan"},
    "ks": {"ks", "kas"},
    "kok": {"kok", "gom"},
    "mai": {"mai"},
    "ml": {"ml", "mal"},
    "mni": {"mni"},
    "mr": {"mr", "mar"},
    "ne": {"ne", "nep"},
    "or": {"or", "ori", "ory"},
    "pa": {"pa", "pan"},
    "sa": {"sa", "san"},
    "sat": {"sat"},
    "sd": {"sd", "snd"},
    "ta": {"ta", "tam"},
    "te": {"te", "tel"},
    "ur": {"ur", "urd"},
}


def _default_download_path():
    return dictionary_path().parent / "kaikki-english-dictionary.jsonl.gz"


def _download_dictionary(path, stdout):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".part")
    downloaded = temporary_path.stat().st_size if temporary_path.exists() else 0
    requested_offset = downloaded
    try:
        headers = {
            "User-Agent": "SellerHelper offline keyword dictionary builder",
            "Accept-Encoding": "gzip",
        }
        if downloaded:
            headers["Range"] = f"bytes={downloaded}-"
        request = urllib.request.Request(
            DICTIONARY_URL,
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            status = getattr(response, "status", response.getcode())
            content_range = response.headers.get("Content-Range", "")
            if downloaded and status == 206:
                if not content_range.startswith(f"bytes {downloaded}-"):
                    raise CommandError(
                        "The dictionary server resumed from an unexpected byte "
                        f"offset ({content_range!r}); partial file kept at "
                        f"{temporary_path}."
                    )
                mode = "ab"
            else:
                downloaded = 0
                mode = "wb"
            content_length = int(response.headers.get("Content-Length", "0") or "0")
            total = downloaded + content_length
            received = 0
            with temporary_path.open(mode) as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    received += len(chunk)
                    if total and downloaded % (100 * 1024 * 1024) < len(chunk):
                        stdout.write(
                            f"Downloaded {downloaded / 1024**3:.2f} / "
                            f"{total / 1024**3:.2f} GiB"
                        )
            if content_length and received != content_length:
                raise CommandError(
                    "The dictionary download ended before the server-declared "
                    f"length ({received} of {content_length} bytes)."
                )
            if status == 206 and content_range:
                byte_range, separator, total_size = content_range.removeprefix(
                    "bytes "
                ).partition("/")
                range_start, range_separator, range_end = byte_range.partition("-")
                if separator and range_separator and range_start.isdigit():
                    if int(range_start) != requested_offset:
                        raise CommandError(
                            "The dictionary server resumed from an unexpected "
                            f"byte offset ({content_range!r})."
                        )
                    if range_end.isdigit() and total_size.isdigit():
                        expected_chunk_size = int(range_end) - int(range_start) + 1
                        if received != expected_chunk_size:
                            raise CommandError(
                                "The resumed response length does not match its "
                                f"Content-Range header ({content_range!r})."
                            )
                        if int(range_end) + 1 != int(total_size):
                            raise CommandError(
                                "The dictionary server returned an incomplete range "
                                f"({content_range!r}); retry to resume."
                            )
                        if downloaded != int(total_size):
                            raise CommandError(
                                "The resumed download is incomplete: received "
                                f"{downloaded} of {total_size} bytes."
                            )
        temporary_path.replace(path)
    except Exception as exc:
        raise CommandError(
            f"Could not download the Wiktionary dictionary: {exc}. "
            f"Partial data was kept at {temporary_path} and will resume on retry."
        ) from exc


def _iter_target_translations(translations):
    if not isinstance(translations, list):
        return
    for translation in translations:
        if not isinstance(translation, dict):
            continue
        language_code = translation.get("lang_code", "")
        if not isinstance(language_code, str):
            continue
        language_code = language_code.casefold()
        for target_code, aliases in LANGUAGE_CODES.items():
            if language_code in aliases:
                yield target_code, translation.get("word", "")
                break


def _build_database(source_path, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, staging_name = tempfile.mkstemp(
        prefix="product-keywords-",
        suffix=".sqlite3",
        dir=output_path.parent,
    )
    os.close(file_descriptor)
    staging_path = Path(staging_name)
    counts = Counter()
    connection = None
    try:
        connection = sqlite3.connect(staging_path)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(
            """
            CREATE TABLE dictionary (
                source_key TEXT NOT NULL,
                source_word TEXT NOT NULL,
                target_language TEXT NOT NULL,
                target_key TEXT NOT NULL,
                target_word TEXT NOT NULL,
                PRIMARY KEY (source_key, target_language, target_key)
            )
            """
        )
        rows = []
        open_source = gzip.open if source_path.suffix == ".gz" else open
        with open_source(source_path, "rt", encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CommandError(
                        f"Invalid JSON on dictionary line {line_number}."
                    ) from exc
                if not isinstance(entry, dict) or entry.get("lang_code") != "en":
                    continue
                source_word = entry.get("word")
                source_key = (
                    source_word.casefold().strip()
                    if isinstance(source_word, str)
                    else ""
                )
                if not source_key:
                    continue
                for language_code, target_word in _iter_target_translations(
                    entry.get("translations", ())
                ):
                    target_word = (
                        target_word.strip()
                        if isinstance(target_word, str)
                        else ""
                    )
                    target_key = target_word.casefold()
                    if not target_key:
                        continue
                    rows.append(
                        (
                            source_key,
                            source_word.strip(),
                            language_code,
                            target_key,
                            target_word,
                        )
                    )
                    counts[language_code] += 1
                if len(rows) >= 10000:
                    connection.executemany(
                        "INSERT OR IGNORE INTO dictionary VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                    connection.commit()
                    rows.clear()

        if rows:
            connection.executemany(
                "INSERT OR IGNORE INTO dictionary VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        connection.commit()
        if connection.execute("SELECT 1 FROM dictionary LIMIT 1").fetchone() is None:
            raise CommandError(
                "No English-to-Indic translations were found in the dictionary."
            )
        connection.execute(
            "CREATE INDEX dictionary_target_key_idx ON dictionary (target_key)"
        )
        connection.commit()
        connection.close()
        connection = None
        staging_path.replace(output_path)
    except Exception:
        if connection is not None:
            connection.close()
        staging_path.unlink(missing_ok=True)
        raise

    return counts


class Command(BaseCommand):
    help = (
        "Download the Wiktionary-derived word dictionary and build an offline "
        "English-to-Indic lookup database."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            type=Path,
            help=(
                "Use an existing Kaikki English JSONL or gzip-compressed JSONL "
                "file instead of downloading it."
            ),
        )
        parser.add_argument(
            "--keep-download",
            action="store_true",
            help="Keep the downloaded JSONL dictionary after building the SQLite index.",
        )

    def handle(self, *args, **options):
        output_path = dictionary_path()
        download_path = options["source"] or _default_download_path()
        downloaded_here = options["source"] is None
        if downloaded_here and not download_path.is_file():
            self.stdout.write(
                "Downloading the compressed Kaikki English Wiktionary dataset "
                "(about 500 MiB; some languages have incomplete coverage)."
            )
            _download_dictionary(download_path, self.stdout)
        if not download_path.is_file():
            raise CommandError(
                f"Dictionary source file does not exist: {download_path}"
            )

        self.stdout.write("Building the offline SQLite translation index...")
        counts = _build_database(download_path, output_path)
        self.stdout.write(self.style.SUCCESS(
            f"Built {output_path} with {sum(counts.values())} translation entries "
            f"across {len(counts)} Indian languages."
        ))
        missing_languages = sorted(set(LANGUAGE_CODES) - set(counts))
        if missing_languages:
            self.stdout.write(self.style.WARNING(
                "This Wiktionary snapshot contains no translations for: "
                + ", ".join(missing_languages)
            ))

        if downloaded_here and not options["keep_download"]:
            download_path.unlink(missing_ok=True)
            self.stdout.write("Removed the temporary raw JSONL download.")
