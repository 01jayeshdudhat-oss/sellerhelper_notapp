# Offline product keyword dictionary

Product keyword matching uses a local SQLite dictionary built from the English
Kaikki/Wiktionary JSONL dump. Django does not call a translation service.
Dictionary coverage varies by word and language; it does not guarantee a
translation for every product term. When a dictionary translation is missing,
the application keeps the original keyword and adds local script-transliteration
aliases for the supported Indian scripts. Transliteration is not translation.

## Build the dictionary

From the Django project directory, run:

```powershell
python manage.py build_product_dictionary
```

The command downloads the gzip-compressed English JSONL source (about 500 MiB),
streams its English entries directly into a SQLite index, and removes the
compressed source after a successful build. The index is stored outside the project:

- Windows: `%LOCALAPPDATA%\SellerHelper\keyword-dictionary\product_keywords.sqlite3`
- Other platforms: `$XDG_DATA_HOME/SellerHelper/keyword-dictionary/product_keywords.sqlite3`
  or `~/.local/share/SellerHelper/keyword-dictionary/product_keywords.sqlite3`

To use a different database path, set `PRODUCT_KEYWORD_DICTIONARY_PATH` in the
Django process environment before building and running the application.

To index a JSONL file you already have instead of downloading it:

```powershell
python manage.py build_product_dictionary --source "D:\data\kaikki-english.jsonl.gz"
```

Use `--keep-download` if you want the command to retain the downloaded JSONL
after building. The build writes a temporary database first and only replaces
the active database after a successful index. If the download is interrupted,
its partial file is kept and the next run resumes it when the source server
supports byte ranges.

After the index is built, open **Django admin → Products**, select the products
to update, and choose **Regenerate multilingual search keywords**. The build
does not overwrite product keyword fields automatically; this preserves
superuser-maintained keyword edits unless regeneration is explicitly requested.

## Data attribution

The source is the Kaikki machine-readable English dictionary, extracted from
English Wiktionary with Wiktextract:

- [Kaikki English dictionary](https://kaikki.org/dictionary/English/)
- [Kaikki raw-data information](https://kaikki.org/dictionary/rawdata.html)
- [Wiktionary reuse terms](https://en.wiktionary.org/wiki/Wiktionary:Copyrights)
- [Wiktextract paper](https://www.lrec-conf.org/proceedings/lrec2022/pdf/2022.lrec-1.140.pdf)

Wiktionary contributions are available under the applicable Creative Commons
Attribution-ShareAlike and GNU Free Documentation License terms. Review the
source terms before redistributing the generated dictionary or its contents.
The Kaikki data page requests citation of the Wiktextract paper for academic
use and appreciates links to the relevant Kaikki pages.
