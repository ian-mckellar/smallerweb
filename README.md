# SmallerWeb

A personal feed reader forked from [Kagi Small Web](https://github.com/kagisearch/smallweb). Surfaces articles, videos, and comics from curated feeds with a discovery-oriented UI.

## How it works

Two services run locally:

- **Backend** (`backend/feed_server.py`) — polls RSS/Atom feeds, stores entries in SQLite, serves combined Atom feeds
- **Frontend** (`app/sw.py`) — the Small Web UI consuming those feeds, with random discovery, seen tracking, category filtering, likes, and notes

Sites that block iframing (Nature, Quanta, Scientific American, etc.) are proxied through the backend so they render inline.

## Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt -r app/requirements.txt

# Terminal 1 — backend (port 5555)
python backend/feed_server.py

# Terminal 2 — frontend (port 8000)
cd app && gunicorn --workers 1 --threads 4 sw:app -b 0.0.0.0:8000
```

Then open http://localhost:8000.

## Feed lists

- `smallweb.txt` — publication/essay RSS feeds (Quanta, Aeon, n+1, Nature, etc.)
- `smallyt.txt` — YouTube channel feeds
- `smallcomic.txt` — webcomic feeds (xkcd, Octopus Pie)

Edit these files to add or remove sources. The backend picks up changes on its next 5-minute refresh cycle.

## Category mapping

The backend maps feeds to topic categories (science, essays, humanities, etc.) defined in `FEED_CATEGORIES` in `backend/feed_server.py`. The frontend uses these for topic filtering and the category dropdown.

## Credits

Based on [Kagi Small Web](https://github.com/kagisearch/smallweb) by [Kagi](https://kagi.com).
