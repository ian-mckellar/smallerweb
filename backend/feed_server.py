"""
SmallerWeb Feed Backend

Polls RSS/Atom feeds from the .txt feed lists, accumulates entries in SQLite
(so history grows beyond each feed's ~15-item window), and serves combined
Atom feeds at the endpoints the frontend expects:

    GET /              — all blog entries (Atom)
    GET /?nso          — same
    GET /?yt           — YouTube entries
    GET /?gh           — (empty)
    GET /?comic        — comic entries
    GET /embeddings    — (stub)
"""

import concurrent.futures
import logging
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import fastfeedparser
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, jsonify, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "feeds.db")

# --- Feed-level category mapping ---
FEED_CATEGORIES = {
    "quantamagazine.org": ["science"],
    "scientificamerican.com": ["science"],
    "nature.com": ["science"],
    "nautil.us": ["science", "essays"],
    "biographic.com": ["science"],
    "media.mit.edu": ["science", "tech"],
    "thepointmag.com": ["essays", "humanities"],
    "bostonreview.net": ["essays", "politics"],
    "nplusonemag.com": ["essays", "humanities"],
    "pioneerworks.org": ["art", "essays"],
    "4columns.org": ["art", "culture"],
    "aeon.co": ["essays", "humanities"],
}


# --- SQLite persistence ---

def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            link TEXT PRIMARY KEY,
            title TEXT,
            author TEXT,
            description TEXT,
            updated TEXT,
            categories TEXT,
            feed_url TEXT,
            feed_type TEXT
        )
    """)
    conn.commit()
    conn.close()


def _upsert_entries(entries, feed_type):
    """Insert or update entries in the database."""
    conn = sqlite3.connect(DB_PATH)
    for e in entries:
        conn.execute("""
            INSERT INTO entries (link, title, author, description, updated, categories, feed_url, feed_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(link) DO UPDATE SET
                title=excluded.title,
                author=excluded.author,
                description=excluded.description,
                updated=excluded.updated,
                categories=excluded.categories,
                feed_url=excluded.feed_url
        """, (
            e["link"], e["title"], e["author"], e["description"],
            e["updated"], ",".join(e["categories"]), e["feed_url"], feed_type,
        ))
    conn.commit()
    conn.close()


def _load_entries(feed_type):
    """Load all entries of a given type from the database."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT link, title, author, description, updated, categories, feed_url "
        "FROM entries WHERE feed_type = ? ORDER BY updated DESC",
        (feed_type,),
    ).fetchall()
    conn.close()
    return [
        {
            "link": r[0], "title": r[1], "author": r[2], "description": r[3],
            "updated": r[4], "categories": r[5].split(",") if r[5] else [],
            "feed_url": r[6],
        }
        for r in rows
    ]


# --- Feed fetching ---

def _find_feed_file(name):
    for path in (os.path.join(os.path.dirname(__file__), "..", name), name):
        if os.path.isfile(path):
            return path
    return None


def _load_feed_urls(filename):
    path = _find_feed_file(filename)
    if not path:
        return []
    urls = []
    with open(path) as f:
        for line in f:
            url = line.split("#")[0].strip()
            if url:
                urls.append(url)
    return urls


def _categories_for_feed(feed_url):
    domain = (urlparse(feed_url).hostname or "").removeprefix("www.")
    for key, cats in FEED_CATEGORIES.items():
        if key in domain:
            return cats
    return []


def _fetch_feed(feed_url):
    """Fetch a single RSS/Atom feed and return a list of entry dicts."""
    try:
        resp = requests.get(
            feed_url.strip(), timeout=15,
            headers={"User-Agent": "SmallerWeb/1.0 (personal feed reader)"},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", feed_url, e)
        return []

    try:
        feed = fastfeedparser.parse(resp.content)
    except Exception as e:
        logger.warning("Failed to parse %s: %s", feed_url, e)
        return []

    if not feed.entries:
        return []

    feed_author = ""
    feed_meta = getattr(feed, "feed", {}) if hasattr(feed, "feed") else {}
    if isinstance(feed_meta, dict):
        feed_author = feed_meta.get("author", "") or feed_meta.get("title", "")

    default_cats = _categories_for_feed(feed_url)
    results = []

    # Derive the base URL from the feed URL for fixing broken relative/localhost links
    parsed_feed = urlparse(feed_url)
    feed_origin = f"{parsed_feed.scheme}://{parsed_feed.hostname}"

    for entry in feed.entries:
        link = entry.get("link", "")
        if not link:
            continue
        # Fix feeds that use localhost URLs (e.g. Pioneer Works)
        if "localhost" in link:
            path = urlparse(link).path
            link = feed_origin + path
        # Upgrade http to https where possible
        if link.startswith("http://"):
            link = link.replace("http://", "https://", 1)

        updated = entry.get("updated") or entry.get("published") or ""
        author = entry.get("author", "") or feed_author
        title = entry.get("title", "")
        description = entry.get("description", "")
        if not description:
            content = entry.get("content")
            if isinstance(content, list) and content:
                description = content[0].get("value", "")

        categories = []
        for tag in entry.get("tags", []):
            term = tag.get("term", "")
            if term:
                categories.append(term)
        if not categories:
            categories = list(default_cats)

        results.append({
            "link": link,
            "title": title,
            "author": author,
            "description": description,
            "updated": updated,
            "categories": categories,
            "feed_url": feed_url.strip(),
        })

    return results


def _paginated_urls(feed_url, max_pages):
    """Generate paginated feed URLs for feeds that support ?paged= or ?page=."""
    urls = [feed_url]
    if max_pages <= 1:
        return urls
    parsed = urlparse(feed_url)
    # WordPress-style feeds use ?paged=N, Aeon uses ?page=N
    if "aeon.co" in (parsed.hostname or ""):
        param = "page"
    else:
        param = "paged"
    sep = "&" if "?" in feed_url else "?"
    for p in range(2, max_pages + 1):
        urls.append(f"{feed_url}{sep}{param}={p}")
    return urls


# Sites that support paginated RSS feeds
PAGINATED_FEEDS = {
    "nplusonemag.com",
    "thepointmag.com",
    "quantamagazine.org",
    "nautil.us",
    "aeon.co",
    "biographic.com",
    "scientificamerican.com",
}


def _expand_feed_urls(feed_urls, max_pages=1):
    """Expand feed URLs with pagination for sites that support it."""
    expanded = []
    for url in feed_urls:
        domain = (urlparse(url).hostname or "").removeprefix("www.")
        if any(d in domain for d in PAGINATED_FEEDS) and max_pages > 1:
            expanded.extend(_paginated_urls(url, max_pages))
        else:
            expanded.append(url)
    return expanded


def _fetch_feeds_concurrent(feed_urls):
    """Fetch multiple feeds concurrently."""
    all_entries = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_feed, url): url for url in feed_urls}
        for future in concurrent.futures.as_completed(futures):
            try:
                all_entries.extend(future.result())
            except Exception as e:
                logger.warning("Feed worker error: %s", e)
    return all_entries


# --- Atom output ---

def _build_atom_feed(entries, title="SmallerWeb Feed"):
    root = ET.Element("feed", xmlns="http://www.w3.org/2005/Atom")
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "updated").text = datetime.now(timezone.utc).isoformat()

    for e in entries:
        entry_el = ET.SubElement(root, "entry")
        ET.SubElement(entry_el, "title").text = e["title"]
        ET.SubElement(entry_el, "link", href=e["link"])
        ET.SubElement(entry_el, "id").text = e["link"]

        author_el = ET.SubElement(entry_el, "author")
        ET.SubElement(author_el, "name").text = e["author"]

        if e["updated"]:
            ET.SubElement(entry_el, "updated").text = e["updated"]
        if e["description"]:
            desc_el = ET.SubElement(entry_el, "summary", type="html")
            desc_el.text = e["description"]
        for cat in e["categories"]:
            if cat:
                ET.SubElement(entry_el, "category", term=cat)
        if e["feed_url"]:
            ET.SubElement(entry_el, "link", rel="via", href=e["feed_url"])

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# --- Refresh ---

def refresh_feeds():
    """Poll all feed lists (page 1 only), store new entries in SQLite."""
    logger.info("Refreshing feeds...")

    for filename, feed_type in [
        ("smallweb.txt", "web"),
        ("smallyt.txt", "yt"),
        ("smallcomic.txt", "comic"),
    ]:
        urls = _load_feed_urls(filename)
        if not urls:
            continue
        new_entries = _fetch_feeds_concurrent(urls)
        if new_entries:
            _upsert_entries(new_entries, feed_type)
        logger.info("%s: fetched %d new entries from %d feeds", feed_type, len(new_entries), len(urls))


def backfill_feeds(max_pages=10):
    """One-time deep fetch of paginated feeds to build up history."""
    logger.info("Backfilling feeds (up to %d pages per feed)...", max_pages)

    for filename, feed_type in [
        ("smallweb.txt", "web"),
        ("smallyt.txt", "yt"),
        ("smallcomic.txt", "comic"),
    ]:
        urls = _load_feed_urls(filename)
        if not urls:
            continue
        expanded = _expand_feed_urls(urls, max_pages=max_pages)
        logger.info("%s: fetching %d URLs (%d feeds x up to %d pages)", feed_type, len(expanded), len(urls), max_pages)
        new_entries = _fetch_feeds_concurrent(expanded)
        if new_entries:
            _upsert_entries(new_entries, feed_type)
        logger.info("%s: backfilled %d entries", feed_type, len(new_entries))

    logger.info("Feed refresh complete.")


# --- Routes ---

@app.route("/")
def feed_root():
    if "yt" in request.args:
        return Response(
            _build_atom_feed(_load_entries("yt"), "SmallerWeb - YouTube"),
            mimetype="application/atom+xml",
        )
    if "gh" in request.args:
        return Response(
            _build_atom_feed([], "SmallerWeb - GitHub"),
            mimetype="application/atom+xml",
        )
    if "comic" in request.args:
        return Response(
            _build_atom_feed(_load_entries("comic"), "SmallerWeb - Comics"),
            mimetype="application/atom+xml",
        )
    # Default and ?nso
    return Response(
        _build_atom_feed(_load_entries("web"), "SmallerWeb"),
        mimetype="application/atom+xml",
    )


@app.route("/embeddings")
def embeddings():
    return jsonify({"embeddings": {}})


# --- Startup ---
_init_db()

# Backfill on first run (empty DB), otherwise just refresh page 1
conn = sqlite3.connect(DB_PATH)
count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
conn.close()
if count == 0:
    backfill_feeds(max_pages=10)
else:
    refresh_feeds()
    logger.info("DB has %d existing entries, skipping backfill", count)

scheduler = BackgroundScheduler()
scheduler.add_job(refresh_feeds, "interval", minutes=5)
scheduler.start()

if __name__ == "__main__":
    app.run(port=5555, debug=True)
