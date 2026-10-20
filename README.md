# Swiiper

A Reddit media aggregator with an Instagram-style, full-screen feed. Browse
photos and videos, discover communities, and save posts locally without an account.

## Features

- One active post at a time, with wheel, swipe, arrow-key, and button navigation.
- Inline photos and muted autoplay video, including verified Reddit HLS streams.
- Explore communities with instant suggestions from loaded and popular communities.
- Full-size photos, with a fast preview shown while the original loads.
- 18+ communities stay hidden, including from For you, until the viewer confirms their age.
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

### Proxy: Oxylabs

The worker fetches Reddit through an Oxylabs proxy. In `.env`, set the **proxy user** you created in the Oxylabs dashboard (Proxies → Users). The Web API key does not work here.

```env
OXYLABS_PROXY_USERNAME=your-proxy-user      # with or without the customer-/user- prefix
OXYLABS_PROXY_PASSWORD=your-proxy-password
OXYLABS_PROXY_PRODUCT=residential           # residential | mobile | datacenter | isp | dedicated-datacenter | dedicated-isp
OXYLABS_PROXY_COUNTRY=US
```

The worker builds the documented username for the product (residential example: `customer-USER-cc-US`) and connects to the product's entry point:

| Product | Entry point | Username |
| --- | --- | --- |
| residential, mobile | `pr.oxylabs.io:7777` | `customer-USER[-cc-US][-st-us_state][-sessid-…-sesstime-N]` |
| datacenter / isp (shared) | `dc.oxylabs.io:8000` / `isp.oxylabs.io:8000` | `user-USER[-country-US]` |
| dedicated-datacenter / dedicated-isp | `ddc.oxylabs.io:8000` / `disp.oxylabs.io:8000` | `user-USER` |

Optional settings:
- `OXYLABS_PROXY_STATE`: a US state for residential/mobile, e.g. `us_california`.
- `OXYLABS_PROXY_SESSION_MINUTES`: keep one exit IP for 1–1440 minutes. The default `0` gives a new IP per request. The session is chosen once at worker start and is never changed in response to a denial.
- `OXYLABS_PROXY_ENDPOINT`: a host:port override, e.g. a dedicated IP's port such as `ddc.oxylabs.io:8001`.
- `OXYLABS_PROXY_CREDENTIALS_PATH`: keeps the credentials out of the environment. Point it at a Git-ignored `username:password` file; it's mounted as a Docker secret and takes priority over the two variables.

Check the proxy before collecting posts. This runs one request per stage: the Oxylabs IP check over HTTP, then HTTPS, then the first subreddit's feed.

```powershell
docker compose run --rm scraper_worker python -m app.worker --diagnose
```

Then start the worker:

```powershell
docker compose up -d --build scraper_worker
docker compose logs --tail 30 api scraper_worker
```

`SUBREDDITS` selects the initial feeds (`pics,aww,gaming` by default), and
`POLL_SECONDS` controls the normal ingestion interval (300 seconds by default).
Community refresh requests are processed by the worker between ingestion cycles.
The worker uses bounded retries. It stops on proxy authentication failures (407) and on
throttling that survives its retries (429), instead of switching sessions or credentials to
get past a denial.

Real credential files, `.env` files, downloaded media, and local database files are
excluded from Git and from image builds. `oxylabs-proxy.example.txt` holds no credentials.

## Project layout

```text
app/main.py          FastAPI routes, feeds, community status, local media delivery
app/worker.py        RSS ingestion, media verification, queued community loads
app/proxy.py         Oxylabs proxy settings, username format and Proxy-Authorization
app/names.py         Subreddit name rules and the 18+ check, shared by the API and worker
app/db.py            Shared schema initialization and connection pool
app/directory.py     Local subreddit suggestions: loaded plus popular communities
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
| `GET /api/v1/subreddits/suggest?q=gam` | Suggestions from loaded and popular communities |
| `POST /api/v1/subreddits/{name}/refresh` | Queue a community load |
| `GET /api/v1/subreddits/{name}/status` | Community queue progress |
| `GET /healthz` | Database and cache health |

Feed responses include `posts`, the compatibility alias `results`, `after`, and
`has_more`. Pass the returned `after` cursor into the next request. The frontend
deduplicates post IDs across pages. `upvotes` is `null` when unknown, because RSS
feeds carry no scores; with `REDDIT_JSON_ENRICHMENT=1` the worker stores Reddit's score.
Feed, community and suggestion requests leave out 18+ communities unless they pass `nsfw=1`.

Reddit media is loaded from source CDNs. The worker verifies public HLS manifests
and preserves original-post links for unsupported media. The local `/media`
route supports owned MP4 files, byte ranges, and HEAD requests; it is not an
arbitrary URL relay. Suggestions come from communities already loaded plus a built-in
list of popular ones; no third-party directory is called. Any public subreddit can still be
opened by its exact name.

## Media and 18+ communities

**Images.** Reddit's RSS lists a resized preview (often 140–640 px) before the post's
image, and the worker used to keep the first one. It now stores the full-size original
(`i.redd.it`, `i.imgur.com`, or the upload a `preview.redd.it` copy was made from) and keeps
the largest preview up to 1080 px for cards and video posters. The full-screen views show
that preview immediately, swap in the original once it has downloaded, and fall back to the
preview if the original is gone. Posts stored before this change can be upgraded without a
proxy or any network request:

```powershell
docker compose run --rm --no-deps scraper_worker python -m app.worker --upgrade-images
```

**Video.** Direct MP4s are stored as `video_mp4`, separate from verified HLS playlists
(`video_hls`). Both appear as videos in the API and the Videos filter.

**18+ communities.** Reddit's RSS doesn't say which communities are 18+, so `app/names.py`
guesses from the name and leans toward flagging. They are left out of For you, the
community lists and suggestions, and opening one directly asks the viewer to confirm their
age once per browser. The Explore toggle turns this back off. Correct the guess for specific
communities in `.env`:

```env
NSFW_SUBREDDITS=somecommunity,another
SFW_SUBREDDITS=sexyhair
```

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
