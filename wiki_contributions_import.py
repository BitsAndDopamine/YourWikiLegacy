#!/usr/bin/env python3
"""
wiki_contributions_import.py
=============================
Imports every article a Wikimedia user has ever edited, across all
languages/wikis, into a SQLite database.

Uses the Wikimedia Single-User-Login (SUL) merge list to automatically
discover every wiki the account exists on, then queries the contribution
list (usercontribs) on each one.

Usage:
    # All Wikipedia language editions, test run with 5 articles per wiki
    python wiki_contributions_import.py --user "Example Name" --db my_contributions.db --limit 5

    # Full import across all Wikipedia language editions
    python wiki_contributions_import.py --user "Example Name" --db my_contributions.db

    # Only specific wikis (e.g. only de + en + eo)
    python wiki_contributions_import.py --user "Example Name" --db my_contributions.db --wikis de,en,eo

    # Also include other projects (Wiktionary, Commons, ...), not just Wikipedia
    python wiki_contributions_import.py --user "Example Name" --db my_contributions.db --all-projects
"""

import argparse
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

USER_AGENT = "wiki-contributions-import/1.0 (personal archival script)"
META_API = "https://meta.wikimedia.org/w/api.php"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


# ─── Database ──────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,              -- article title (in the respective language)
    wiki_lang        TEXT NOT NULL,               -- language code, e.g. 'de', 'en', 'eo'
    wiki_project     TEXT NOT NULL DEFAULT 'wikipedia',  -- 'wikipedia', 'wiktionary', ...
    text             TEXT,                        -- original text (intro paragraphs)
    text_edited      TEXT,                        -- edited text, never truncated automatically
    extra_latex      TEXT,                        -- extra LaTeX code (images, infoboxes)
    page_id          TEXT,
    revision_id      TEXT,                        -- most recently seen revision at fetch time
    categories       TEXT,                        -- JSON array
    comment          TEXT,                        -- internal note, e.g. 'duplicate' for duplicates
    status           TEXT DEFAULT 'pending',
    sort_key         TEXT,                        -- used for sorting in the LaTeX export
    short_title      TEXT,
    edit_count       INTEGER DEFAULT 1,            -- number of the user's own edits to this article
    first_edited_at  TEXT,                         -- timestamp of the user's first edit
    last_edited_at   TEXT,                         -- timestamp of the user's last edit
    fetched_at       TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(wiki_lang, wiki_project, title)
);
"""


def get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def is_in_db(conn: sqlite3.Connection, wiki_lang: str, wiki_project: str, title: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM articles WHERE wiki_lang = ? AND wiki_project = ? AND lower(title) = lower(?)",
        (wiki_lang, wiki_project, title),
    ).fetchone()
    return row is not None


# ─── Sorting (language-independent, simplified) ───────────────────────────
#
# There is no single "correct" sort order across all languages. As a
# practical, language-neutral default, this script:
#   - for Latin script: sorts by the diacritic-stripped letter
#     (é, ê, ë -> e), the way most library catalogs do
#   - for non-Latin scripts (Cyrillic, Greek, CJK, ...): uses the
#     original character as its own section
# This is not a linguistically correct collation per language, but it is
# consistent and usable without per-language configuration.

def strip_diacritics(ch: str) -> str:
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def compute_sort_key(title: str) -> str:
    if not title:
        return ""
    out = []
    for ch in title:
        base = strip_diacritics(ch)
        out.append(base if base else ch)
    return "".join(out).upper()


# ─── Wikimedia API ─────────────────────────────────────────────────────────

def api_get(url: str, params: dict, sleep: float = 0.3) -> dict:
    params = {**params, "format": "json"}
    resp = SESSION.get(url, params=params, timeout=30)
    resp.raise_for_status()
    time.sleep(sleep)
    return resp.json()


def discover_wikis(username: str, project_filter: str | None) -> list[dict]:
    """Finds every wiki the account exists on (via SUL).

    project_filter: e.g. 'wikipedia.org' to only include Wikipedias,
                     or None for all projects.
    """
    data = api_get(META_API, {
        "action": "query",
        "meta": "globaluserinfo",
        "guiuser": username,
        "guiprop": "merged",
    })

    gui = data.get("query", {}).get("globaluserinfo", {})
    if "missing" in gui:
        print(f"ERROR: Global user '{username}' not found.", file=sys.stderr)
        sys.exit(1)

    merged = gui.get("merged", [])
    wikis = []
    for entry in merged:
        url = entry.get("url", "")
        wiki_db = entry.get("wiki", "")
        if entry.get("editcount", 0) == 0:
            continue
        if project_filter and project_filter not in url:
            continue
        # Derive language code from the URL, e.g. https://de.wikipedia.org -> 'de'
        m = re.match(r"https?://([a-z0-9-]+)\.(\w+)\.org", url)
        if not m:
            continue
        lang_code, project = m.group(1), m.group(2)
        if lang_code in ("www", "commons", "meta", "species", "wikidata"):
            # Projects without a language code or without articles - skip,
            # unless the user explicitly wants --all-projects and they are relevant
            if project_filter is None and lang_code in ("commons", "meta", "species", "wikidata"):
                continue
        wikis.append({
            "lang": lang_code,
            "project": project,     # 'wikipedia', 'wiktionary', 'wikisource', ...
            "api_url": url.rstrip("/") + "/w/api.php",
            "editcount": entry.get("editcount", 0),
        })
    return wikis


def get_user_contribs(api_url: str, username: str, namespace: int = 0, limit: int | None = None) -> dict:
    """Returns dict: title -> {revid, first_ts, last_ts, count}"""
    titles: dict[str, dict] = {}
    uccontinue = None
    fetched = 0

    while True:
        params = {
            "action": "query",
            "list": "usercontribs",
            "ucuser": username,
            "ucnamespace": namespace,
            "uclimit": "max",
            "ucprop": "title|ids|timestamp",
            "ucdir": "newer",  # oldest edit first -> lets us record first_ts correctly
        }
        if uccontinue:
            params["uccontinue"] = uccontinue

        data = api_get(api_url, params, sleep=0.3)
        contribs = data.get("query", {}).get("usercontribs", [])

        for c in contribs:
            title = c["title"]
            ts = c["timestamp"]
            revid = c["revid"]
            if title not in titles:
                titles[title] = {"revid": revid, "first_ts": ts, "last_ts": ts, "count": 1}
            else:
                entry = titles[title]
                entry["count"] += 1
                entry["last_ts"] = ts
                entry["revid"] = revid  # ucdir=newer -> last value seen is the newest

        fetched += len(contribs)
        if limit and len(titles) >= limit:
            break

        cont = data.get("continue", {})
        if "uccontinue" not in cont:
            break
        uccontinue = cont["uccontinue"]

    if limit:
        titles = dict(list(titles.items())[:limit])

    return titles


def fetch_article_data(api_url: str, titles_batch: list[str]) -> dict:
    """Fetches intro text, categories, and page_id for up to 20 titles at once.
    Resolves redirects. Returns: normalized_title -> {text, categories, page_id, redirected_from}
    """
    params = {
        "action": "query",
        "titles": "|".join(titles_batch),
        "prop": "extracts|categories",
        "exintro": 1,
        "explaintext": 1,
        "cllimit": "max",
        "clshow": "!hidden",
        "redirects": 1,
        "formatversion": 2,
    }
    data = api_get(api_url, params, sleep=0.5)
    query = data.get("query", {})

    redirect_map = {r["from"]: r["to"] for r in query.get("redirects", [])}
    normalized_map = {n["from"]: n["to"] for n in query.get("normalized", [])}

    result = {}
    for page in query.get("pages", []):
        if page.get("missing"):
            continue
        title = page.get("title", "")
        cats = [c["title"].split(":", 1)[-1] for c in page.get("categories", [])]
        result[title] = {
            "text": page.get("extract", ""),
            "categories": cats,
            "page_id": str(page.get("pageid", "")),
        }

    # Map original title -> final (redirect-resolved) title
    resolved = {}
    for orig in titles_batch:
        cur = normalized_map.get(orig, orig)
        cur = redirect_map.get(cur, cur)
        resolved[orig] = cur

    return result, resolved


# ─── Main logic ────────────────────────────────────────────────────────────

def batched(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def import_wiki(conn: sqlite3.Connection, username: str, wiki: dict,
                 resume: bool, limit: int | None) -> int:
    lang, project, api_url = wiki["lang"], wiki["project"], wiki["api_url"]
    print(f"\n=== {project}.{lang} ===")

    contribs = get_user_contribs(api_url, username, namespace=0, limit=limit)
    print(f"  {len(contribs)} edited articles (main namespace) found.")

    if not contribs:
        return 0

    titles = list(contribs.keys())
    imported = 0

    for batch in batched(titles, 20):
        # Skip already-imported articles (before the API call, saves requests)
        if resume:
            batch = [t for t in batch if not is_in_db(conn, lang, project, t)]
        if not batch:
            continue

        try:
            article_data, resolved = fetch_article_data(api_url, batch)
        except requests.RequestException as e:
            print(f"  Warning: error fetching {batch}: {e}", file=sys.stderr)
            continue

        for orig_title in batch:
            final_title = resolved.get(orig_title, orig_title)
            data = article_data.get(final_title)
            if not data:
                # Page no longer exists / was deleted
                continue

            info = contribs[orig_title]
            sort_key = compute_sort_key(final_title)
            categories_json = None
            if data["categories"]:
                import json
                categories_json = json.dumps(data["categories"], ensure_ascii=False)

            comment = None
            if resume and is_in_db(conn, lang, project, final_title) and final_title != orig_title:
                comment = "duplicate"  # redirect led to an article already present

            conn.execute(
                """
                INSERT INTO articles
                    (title, wiki_lang, wiki_project, text, page_id, revision_id,
                     categories, comment, sort_key, short_title,
                     edit_count, first_edited_at, last_edited_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wiki_lang, wiki_project, title) DO UPDATE SET
                    text=excluded.text,
                    page_id=excluded.page_id,
                    revision_id=excluded.revision_id,
                    categories=excluded.categories,
                    edit_count=excluded.edit_count,
                    first_edited_at=excluded.first_edited_at,
                    last_edited_at=excluded.last_edited_at,
                    fetched_at=excluded.fetched_at
                """,
                (
                    final_title, lang, project, data["text"], data["page_id"], str(info["revid"]),
                    categories_json, comment, sort_key, final_title[:80],
                    info["count"], info["first_ts"], info["last_ts"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            imported += 1

        conn.commit()

    print(f"  {imported} articles imported/updated.")
    return imported


def run(username: str, db_path: str, wikis_filter: list[str] | None,
        all_projects: bool, resume: bool, limit: int | None) -> None:
    conn = get_db(db_path)

    project_filter = None if all_projects else "wikipedia.org"
    print(f"Looking up wikis for user '{username}' …")
    wikis = discover_wikis(username, project_filter)

    if wikis_filter:
        wikis = [w for w in wikis if w["lang"] in wikis_filter]

    if not wikis:
        print("No matching wikis with edits found.", file=sys.stderr)
        return

    print(f"Found wikis ({len(wikis)}):")
    for w in wikis:
        print(f"  - {w['project']}.{w['lang']}  ({w['editcount']} total edits)")

    total = 0
    for w in wikis:
        total += import_wiki(conn, username, w, resume, limit)

    conn.close()
    print(f"\nDone. {total} articles imported/updated in total into '{db_path}'.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Imports every article edited by a user (all languages) into a SQLite database"
    )
    parser.add_argument("--user", required=True, help="Wikimedia username (global, SUL)")
    parser.add_argument("--db", default="wiki_contributions.db", help="Path to the SQLite database")
    parser.add_argument("--wikis", default=None,
                         help="Comma-separated list of language codes, e.g. 'de,en,eo' (default: all found)")
    parser.add_argument("--all-projects", action="store_true",
                         help="Also include Wiktionary, Wikisource etc., not just Wikipedia")
    parser.add_argument("--resume", action="store_true", default=True, help="Skip already-imported articles (default)")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Re-import all articles")
    parser.add_argument("--limit", type=int, default=None, help="Only the first N articles per wiki (for testing)")
    args = parser.parse_args()

    wikis_filter = args.wikis.split(",") if args.wikis else None

    run(
        username=args.user,
        db_path=args.db,
        wikis_filter=wikis_filter,
        all_projects=args.all_projects,
        resume=args.resume,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
