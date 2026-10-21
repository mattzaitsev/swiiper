# Executed validation

> The September 2026 entries below describe the earlier Webshare proxy setup, kept for history. The worker now uses Oxylabs (see the October 20 changeover at the end).

Validated on the user's Windows/Docker sandbox on September 21, 2026 (September 22 UTC).

- Original malformed destination reproduced **502 at proxy CONNECT**. With the same original proxy credential, HTTP Webshare, HTTPS Webshare and the corrected Reddit Atom URL each returned **200**.
- Supplied proxy export: **20,000** entries; loaded successfully by the rebuilt worker through a Docker secret file mount. Credentials were not printed or included in the package.
- `python -m pytest -q`: **17 passed**. Two expected deprecation warnings concern BasicAuth in pinned aiohttp 3.14.3; functionality is tested and supported in this pin.
- `node --check` on the frontend script: passed.
- Docker Compose configuration validation: passed.
- Both Docker images built successfully; local API and worker containers recreated. PostgreSQL and Redis were left running, and the existing database volume was retained.
- Live ingestion: **25 pics + 25 aww + 25 gaming = 75 database rows** after the first cycle (previous count: 0).
- `/healthz`: `database=ok`, `cache=ok`.
- `/api/v1/feed?subreddit=pics&limit=2` and `/pics?limit=2`: identical payloads, two posts, matching `results`, valid next cursor `eyJvZmZzZXQiOjJ9`.
- `/`: HTTP 200. Browser verification rendered the first 10 cards, loaded the next 10 on scroll, then the final 5 and displayed **End of feed**. No captured browser error/warning logs.
- Range tests use a ten-byte local fixture and validate 200, 206, suffix/open ranges, Content-Range, 416, HEAD and missing/path-invalid files. This verifies HTTP transport semantics; it is not a video encoding/playback quality test.

Scope: the proxy control test and successful feed ingestion validate the tested connections, not all 20,000 export entries or every future request. The worker collects RSS metadata and does not download/transcode video. MP4 byte-range serving is available for local files supplied in `media/`.

Repository publication check, October 20, 2026: all 24 backend tests passed in a disposable container; frontend JavaScript syntax and Docker Compose configuration validation passed. Real credentials and local media were excluded from the initial commit.

## Oxylabs changeover, October 20, 2026

- The Webshare gateway, export-file pool and IP-check endpoints were replaced with an Oxylabs configuration in `app/proxy.py`. It covers residential, mobile, datacenter, ISP and the dedicated products, with the documented entry points and username formats. Country, US state, sticky session and endpoint override are supported.
- Proxy authentication now uses a `Proxy-Authorization` header built with `aiohttp.encode_basic_auth`, replacing the deprecated `BasicAuth`. The suite no longer emits the deprecation warnings.
- `python -m pytest -q`: **42 passed** in a disposable `python:3.12-slim` container. This includes a local proxy that captures the CONNECT request's `Proxy-Authorization` header, plus username formats for every product, settings validation, and credential-file handling.
- Docker Compose configuration validation passed, and `node --check` passed on both frontend scripts.
- Not yet run: a live `--diagnose` against Oxylabs. It needs the account's proxy-user credentials in `.env`.

## Media, 18+ and search fixes, October 20, 2026

- `python -m pytest -q`: **65 passed** in a disposable `python:3.12-slim` container. New tests cover image ranking on Reddit-shaped RSS entries (original over preview, 140 px previews dropped, `-v0-` preview names, external previews, MP4, hosted video, text posts), the stored-image upgrade, score and MP4 enrichment, the 18+ check and its overrides, the age gate on both feed routes, For you exclusion, local suggestions, and `Cache-Control` on UI files.
- Stored data before the fix: 248 image posts pointed at `preview.redd.it` copies (58 at 140 px, 8 at 320 px, 182 at 640 px) and 1 at an `i.redd.it` original. `--upgrade-images` repointed all 248 to `i.redd.it` originals. Of 40 sampled derived originals, 37 returned 200 and 3 returned 404 (removed uploads); the UI falls back to the preview for those.
- Schema upgrade on the existing volume: the `upvote_score` default of 0 was dropped and all 390 placeholder zeros became `NULL`; `media_type` gained `video_mp4`.
- Browser: the Home stream showed a 4000×2667 original; the community viewer showed a 3024×4032 original filling its stage instead of rendering at natural size; grid cards used 640 px previews. For you returned only SFW communities, an 18+ community returned the age gate, and dismissing the dialog kept 18+ communities hidden. Explore suggestions were served locally.
- UI files are now sent with `Cache-Control: no-cache`. Without it, the browser kept using a stale stylesheet after the rebuild.
- `node --check` passed on both frontend scripts; Docker Compose configuration validation passed.

## Oxylabs Web API, October 20, 2026

- The worker now reaches Reddit through the Oxylabs Web API (`POST /v1/scrape`, `output: ["html"]`) whenever `OXYLABS_WEB_API_KEY` is set, and falls back to the proxy configuration otherwise. The account in use has a Web API instance and no proxy user.
- A Web API scrape of `https://www.reddit.com/r/pics/.rss` returned the Atom feed unchanged in `results[0].html`, with the target status in `results[0].metadata.statuses.http_request.code` (200), in about 7 seconds.
- `python -m pytest -q`: **77 passed**. New tests use a local stand-in for the Web API and cover the Bearer header and request body, target status mapping, 401, quota and rate-limit 429s, faulted 500s, 400, empty results, key validation without echoing the value, the one-request diagnose, and video playlist checks going direct in Web API mode but through the proxy in proxy mode.
- Live: `--diagnose` returned `reddit_feed ok, status 200, 25 posts`. Two `--once` cycles upserted a queued community (25) and then pics, aww and gaming (25 each). The newest post was 21 minutes old, and new image posts used `i.redd.it` originals with 640 px card previews.
