# Swiiper

A Reddit media aggregator with an Instagram-style, full-screen feed. Browse
photos and videos, discover communities, and save posts locally without an account.

## Features

- One active post at a time, with wheel, swipe, arrow-key, and button navigation.
- Inline photos and muted autoplay video, including verified Reddit HLS streams.
- Explore communities with subreddit autocomplete and directory-reported member counts.
- Community grids, media filters, on-demand community refresh, and paginated feeds.
- Saved posts in browser localStorage, with desktop and mobile navigation.
- FastAPI backend, PostgreSQL persistence, Redis caching, and a separate RSS worker.

## Run locally

Requires Docker Desktop with Docker Compose.

```powershell
Copy-Item .env.example .env
docker compose up -d --build db redis api
```

Open **http://localhost:8000/**. Interactive API documentation is at
http://localhost:8000/docs and service health is at http://localhost:8000/healthz.
Only the API publishes a localhost port; PostgreSQL and Redis communicate on the
internal Docker network, avoiding conflicts with other local projects.

A fresh database starts empty. Starting the interface does not start the scraper.
To collect posts, configure one of these proxy options in `.env`:

1. Set `PROXY_LIST_PATH` to an actual exported `host:port:username:password` file.
2. Keep the supplied `proxies.example.txt` placeholder and set `PROXY_USERNAME`
   and `PROXY_PASSWORD` for the configured `PROXY_URL`.

Then start the worker:

```powershell
docker compose up -d --build scraper_worker
docker compose logs --tail 30 api scraper_worker
```

`SUBREDDITS` selects the initial feeds (`pics,aww,gaming` by default), and
`POLL_SECONDS` controls the normal ingestion interval (300 seconds by default).
Community refresh requests are processed by the worker between ingestion cycles.
The worker uses bounded retries and stops on denied requests instead of rotating
credentials in response to a denial.

Real proxy exports, `.env` files, downloaded media, and local database files are
excluded from Git. The example proxy file contains no credentials. Credentials
are mounted into the worker as a Docker secret and excluded from image builds.

## Project layout

```text
app/main.py          FastAPI routes, feeds, community status, local media delivery
app/worker.py        RSS ingestion, media verification, queued community loads
app/db.py            Shared schema initialization and connection pool
app/directory.py     Subreddit autocomplete adapter and cache
index.html          Application shell
web/app.js          Feed, Explore, Saved, and media viewer
web/search.js       Debounced subreddit autocomplete
web/styles.css      Responsive interface
web/vendor/         Bundled hls.js and its license
tests/              Backend and transport regression tests
compose.yaml        API, worker, PostgreSQL, and Redis services
```

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/feed?subreddit=all&kind=all` | Combined paginated feed |
| `GET /api/v1/feed?subreddit=pics&kind=image` | Community and media filtering |
| `GET /api/v1/subreddits` | Loaded communities and content counts |
| `GET /api/v1/subreddits/suggest?q=gam` | Subreddit autocomplete |
| `POST /api/v1/subreddits/{name}/refresh` | Queue a community load |
| `GET /api/v1/subreddits/{name}/status` | Community queue progress |
| `GET /healthz` | Database and cache health |

Feed responses include `posts`, the compatibility alias `results`, `after`, and
`has_more`. Pass the returned `after` cursor into the next request. The frontend
deduplicates post IDs across pages. Listing scores are not fabricated when RSS
does not supply them.

Reddit media is loaded from source CDNs. The worker verifies public HLS manifests
and preserves original-post links for unsupported media. The local `/media`
route supports owned MP4 files, byte ranges, and HEAD requests; it is not an
arbitrary URL relay. Autocomplete uses an external directory with local fallbacks;
it is not a complete local copy of Reddit's community index.

## Development and tests

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --check web/app.js
node --check web/search.js
```

The regression suite covers feed contracts, pagination, cache failures, community
search, RSS parsing, proxy handling, retry limits, and media byte ranges.
[UI-NOTES.md](UI-NOTES.md) describes the current interface and browser validation;
[VALIDATION.md](VALIDATION.md) preserves the earlier backend repair results.

## Existing installations

Keep the same Compose project name and PostgreSQL volume when upgrading an
existing installation. `docker compose down -v` deletes the saved database.
Schema initialization preserves existing media columns and serializes concurrent
initialization. The API and worker can start in either order after the database
is healthy.

The bundled hls.js library retains its upstream license in `web/vendor/LICENSE`.
