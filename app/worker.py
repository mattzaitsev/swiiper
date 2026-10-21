"""RSS ingestion through Oxylabs (Web API or proxy), plus a one-request-per-stage diagnostic.

No TLS impersonation, disabled certificate checks, or identity changes in response to a denial.
"""
import argparse
import asyncio
import calendar
import json
import logging
import os
import random
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import aiohttp
import feedparser
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .db import open_pool
from .names import normalize_feed
from .proxy import IP_CHECK_URLS, ProxyEntry, load_proxy
from .webapi import WebApi, load_web_api

log = logging.getLogger('swiiper.worker')
TRANSIENT = {429, 500, 502, 503, 504}
# Stop the whole worker on proxy auth failure (407) or throttling that survived retries (429).
# Never switch sessions or credentials to get past a denial or a Retry-After.
STOP_STATUSES = {407, 429}
# Also stop on a rejected Web API key or a spent Web API quota.
STOP_STAGES = {'webapi_auth', 'webapi_quota'}
MAX_BODY = 4 * 1024 * 1024
MAX_API_BODY = 8 * 1024 * 1024
IMAGE_SUFFIXES = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
ORIGINAL_IMAGE_HOSTS = {'i.redd.it', 'i.imgur.com'}
PREVIEW_HOSTS = {'preview.redd.it', 'external-preview.redd.it'}
# preview.redd.it/<id>.<ext> is a resized copy of the upload at i.redd.it/<id>.<ext>. Newer
# preview names put a title slug ending in "-v0-" before the id.
REDDIT_PREVIEW_PATH = re.compile(r'/(?:[^/]*-v0-)?([A-Za-z0-9]+\.(?:jpe?g|png|gif|webp))', re.IGNORECASE)
MIN_CARD_WIDTH, MAX_PREVIEW_WIDTH = 320, 1080


@dataclass
class FetchFailure(Exception):
    stage: str
    status: int | None = None
    retryable: bool = False
    retry_after: float | None = None

    def __str__(self):
        # Never serialize aiohttp exceptions: their request info can contain credentials.
        return f'stage={self.stage} status={self.status}'


def stops_worker(exc: FetchFailure) -> bool:
    return exc.status in STOP_STATUSES or exc.stage in STOP_STAGES


def retry_delay(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not 0 <= seconds < float('inf'):
        return None
    return seconds


def session_factory():
    connector = aiohttp.TCPConnector(
        ssl=ssl.create_default_context(), limit=4, limit_per_host=2,
        ttl_dns_cache=300, force_close=os.getenv('PROXY_FORCE_CLOSE', '0') == '1')
    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=60, connect=20, sock_connect=20, sock_read=30),
        trust_env=False,
        headers={'User-Agent': os.getenv('REDDIT_USER_AGENT', 'swiiper-sandbox/1.0'),
                 'Accept': 'application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.1'})


async def read_body(response, limit):
    chunks, size = [], 0
    async for chunk in response.content.iter_chunked(65536):
        size += len(chunk)
        if size > limit:
            raise FetchFailure('body_too_large')
        chunks.append(chunk)
    return b''.join(chunks)


async def fetch_once(session, url, route: ProxyEntry | WebApi | None):
    """One GET of `url`: through the Web API, through a proxy, or directly when route is None."""
    if isinstance(route, WebApi):
        return await fetch_via_web_api(session, url, route)
    try:
        async with session.get(url, proxy=route.url if route else None,
                               proxy_headers=route.headers if route else None,
                               allow_redirects=False) as response:
            if response.status != 200:
                raise FetchFailure('http_response', response.status,
                                   response.status in TRANSIENT,
                                   retry_delay(response.headers.get('Retry-After')))
            return await read_body(response, MAX_BODY), response.headers.get('Content-Type', '')
    except aiohttp.ClientHttpProxyError as exc:
        raise FetchFailure('proxy_connect', exc.status, exc.status in TRANSIENT,
                           retry_delay((exc.headers or {}).get('Retry-After'))) from None
    except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError):
        raise FetchFailure('tls_verification') from None
    except aiohttp.ClientProxyConnectionError:
        raise FetchFailure('proxy_tcp_or_dns', retryable=True) from None
    except asyncio.TimeoutError:
        raise FetchFailure('timeout', retryable=True) from None
    except aiohttp.ClientConnectionError:
        raise FetchFailure('connection', retryable=True) from None
    except aiohttp.ClientError:
        raise FetchFailure('http_client') from None


async def fetch_via_web_api(session, url, api: WebApi):
    """Have Oxylabs fetch `url` (POST /v1/scrape) and return the body Reddit served."""
    try:
        async with session.post(api.endpoint, json={'url': url, 'output': ['html']},
                                headers={'Authorization': f'Bearer {api.key}'},
                                timeout=aiohttp.ClientTimeout(total=150, connect=20, sock_read=140),
                                allow_redirects=False) as response:
            raw = await read_body(response, MAX_API_BODY)
            status, retry_after = response.status, retry_delay(response.headers.get('Retry-After'))
    except asyncio.TimeoutError:
        raise FetchFailure('timeout', retryable=True) from None
    except aiohttp.ClientConnectionError:
        raise FetchFailure('connection', retryable=True) from None
    except aiohttp.ClientError:
        raise FetchFailure('http_client') from None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    data = data if isinstance(data, dict) else {}
    if status == 401:
        raise FetchFailure('webapi_auth', 401)
    if status == 429:
        if data.get('title') == 'QUOTA_EXCEEDED':
            raise FetchFailure('webapi_quota', 429)
        raise FetchFailure('webapi_rate_limit', 429, True, retry_after)
    if status >= 500:
        # A faulted request is not charged, so retrying it costs nothing.
        raise FetchFailure('webapi_faulted', status, True, retry_after)
    if status != 200:
        raise FetchFailure('webapi_request', status)
    results = data.get('results')
    result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    target = ((result.get('metadata') or {}).get('statuses') or {}).get('http_request') or {}
    code = target.get('code')
    if isinstance(code, int) and code != 200:
        raise FetchFailure('http_response', code, code in TRANSIENT)
    page = result.get('html')
    if not isinstance(page, str) or not page:
        raise FetchFailure('webapi_empty')
    return page.encode('utf-8'), ''


async def fetch_with_retries(session, url, route, attempts=3):
    for attempt in range(attempts):
        try:
            return await fetch_once(session, url, route)
        except FetchFailure as exc:
            log.warning('Fetch failed %s attempt=%d/%d', exc, attempt + 1, attempts)
            if not exc.retryable or attempt == attempts - 1:
                raise
            delay = max(exc.retry_after or 0, min(30, 2 ** attempt + random.random()))
            await asyncio.sleep(delay)


class MediaLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {'img', 'video', 'source', 'a'}:
            candidate = values.get('href' if tag == 'a' else 'src')
            if candidate:
                self.links.append(candidate)


def safe_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme == 'https' and parsed.hostname and not parsed.username else None


def is_image(url):
    return bool(safe_url(url)) and urlparse(url).path.lower().endswith(IMAGE_SUFFIXES)


def preview_width(url):
    """Width a Reddit preview was resized to. None for originals and full-size copies."""
    parsed = urlparse(url)
    if (parsed.hostname or '').endswith('thumbs.redditmedia.com'):
        return 140
    if parsed.hostname not in PREVIEW_HOSTS:
        return None
    width = parse_qs(parsed.query).get('width', [''])[0]
    return int(width) if width.isdigit() else None


def original_image(url):
    """The full-size original behind a Reddit-hosted image URL, or None."""
    parsed = urlparse(url)
    if parsed.hostname in ORIGINAL_IMAGE_HOSTS:
        return url
    if parsed.hostname == 'preview.redd.it':
        match = REDDIT_PREVIEW_PATH.fullmatch(parsed.path)
        if match:
            return f'https://i.redd.it/{match.group(1)}'
    return None


def pick_image(urls):
    """(full-size image, card preview) from a post's image URLs. Either may be None.

    RSS usually puts a 140-640px preview first, so taking the first image made pictures small.
    The preview is the largest resized copy up to 1080px: fast for cards and video posters, and
    the first paint while the original loads. Preview URLs are signed (s=), so they can't be resized.
    """
    images = list(dict.fromkeys(url for url in urls if is_image(url)))
    resized = sorted(((preview_width(url), url) for url in images if preview_width(url)),
                     key=lambda item: -item[0])
    original = (next(filter(None, map(original_image, images)), None)
                or next((url for url in images if not preview_width(url)), None)
                or (resized[0][1] if resized else None))
    preview = next((url for width, url in resized if width <= MAX_PREVIEW_WIDTH), None)
    if (preview and preview_width(original) is None
            and preview_width(preview) < MIN_CARD_WIDTH):
        preview = None  # next to a full-size original, a tiny preview only makes cards blurry
    return original, preview


def parse_posts(body: bytes, subreddit: str):
    feed = feedparser.parse(body)
    # bozo also flags harmless quirks (e.g. a charset override); reject only feeds that didn't parse.
    if not feed.version or (feed.bozo and not feed.entries):
        raise FetchFailure('invalid_feed')
    posts = []
    for entry in feed.entries:
        permalink = safe_url(entry.get('link'))
        post_id = entry.get('id', '')
        if not re.fullmatch(r't3_[a-z0-9]{1,29}', post_id):
            match = re.search(r'/comments/([a-z0-9]+)/', permalink or '')
            post_id = f't3_{match.group(1)}' if match else None
        stamp = entry.get('published_parsed') or entry.get('updated_parsed')
        if not post_id or not permalink or not stamp:
            continue
        parser = MediaLinks()
        parser.feed(entry.get('summary', ''))
        for content in entry.get('content', []):
            parser.feed(content.get('value', ''))
        candidates = [item.get('url') for item in entry.get('media_content', [])]
        candidates += [item.get('href') for item in entry.get('enclosures', [])]
        candidates += parser.links
        thumbnails = [item.get('url') for item in entry.get('media_thumbnail', [])]
        image, preview = pick_image(candidates + thumbnails)
        # Prefer a direct video, then an image; never invent a DASH MP4 URL.
        mp4 = next((url for url in candidates if safe_url(url)
                    and urlparse(url).path.lower().endswith('.mp4')), None)
        if mp4:
            media_type, media_url, thumbnail = 'video_mp4', mp4, preview or image
        elif any(is_image(url) for url in candidates):
            media_type, media_url, thumbnail = 'direct_image', image, preview
        else:  # a thumbnail alone doesn't make a link or text post an image post
            media_type, media_url, thumbnail = 'unknown', permalink, preview or image
        hosted_video = next((url for url in candidates if safe_url(url)
                             and urlparse(url).hostname == 'v.redd.it'
                             and re.fullmatch(r'/[a-zA-Z0-9]+/?', urlparse(url).path)), None)
        if hosted_video and media_type != 'video_mp4':
            media_type, media_url, thumbnail = 'third_party_embed', hosted_video, preview or image
        # RSS has no scores; JSON enrichment fills them in when enabled.
        posts.append((str(post_id), entry.get('title', 'Untitled'), subreddit,
                      media_type, media_url, thumbnail, permalink,
                      datetime.fromtimestamp(calendar.timegm(stamp), timezone.utc), None))
    return posts


UPSERT = '''INSERT INTO reddit_media_posts
    (db_primary_id, title, subreddit, media_category, clean_delivery_url,
     thumbnail_url, source_origin_url, created_utc, upvote_score)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
    ON CONFLICT (db_primary_id) DO UPDATE SET title=EXCLUDED.title,
      clean_delivery_url=EXCLUDED.clean_delivery_url, media_category=EXCLUDED.media_category,
      thumbnail_url=EXCLUDED.thumbnail_url, source_origin_url=EXCLUDED.source_origin_url,
      created_utc=EXCLUDED.created_utc,
      upvote_score=COALESCE(EXCLUDED.upvote_score, reddit_media_posts.upvote_score)'''


def enrich_posts(posts, listing):
    """Use URLs and scores returned by Reddit metadata, including audio-bearing HLS streams."""
    metadata = {item['data']['name']: item['data']
                for item in listing.get('data', {}).get('children', [])
                if isinstance(item.get('data'), dict) and item['data'].get('name')}
    enriched = []
    for post in posts:
        row = list(post)
        data = metadata.get(row[0], {})
        sources = [data] + data.get('crosspost_parent_list', [])
        for source in sources:
            video = ((source.get('secure_media') or {}).get('reddit_video') or
                     (source.get('media') or {}).get('reddit_video') or {})
            url = safe_url(video.get('hls_url')) or safe_url(video.get('fallback_url'))
            if url:
                # fallback_url is a direct (silent) DASH MP4, not an HLS playlist.
                row[3] = 'video_hls' if urlparse(url).path.endswith('.m3u8') else 'video_mp4'
                row[4] = url
                break
        preview = (data.get('preview', {}).get('images') or [{}])[0]
        # The largest resized copy that fits a card, else the full-size source.
        sized = [item for item in preview.get('resolutions', [])
                 if isinstance(item, dict) and type(item.get('width')) is int
                 and item['width'] <= MAX_PREVIEW_WIDTH and safe_url(item.get('url'))]
        thumbnail = (max(sized, key=lambda item: item['width'])['url'] if sized
                     else safe_url(preview.get('source', {}).get('url')) or safe_url(data.get('thumbnail')))
        row[5] = thumbnail or row[5]
        if type(data.get('score')) is int:
            row[8] = data['score']
        enriched.append(tuple(row))
    return enriched


async def ingest(session, route, pool, cache, subreddit):
    body, _ = await fetch_with_retries(session, f'https://www.reddit.com/r/{subreddit}/.rss', route)
    posts = parse_posts(body, subreddit)
    # Optional for deployments with permitted JSON access. This sandbox's JSON
    # endpoint returns 403, so it is disabled rather than repeatedly retried.
    if os.getenv('REDDIT_JSON_ENRICHMENT', '0') == '1':
        try:
            metadata, _ = await fetch_once(session,
                f'https://www.reddit.com/r/{subreddit}/.json?limit=25&raw_json=1', route)
            posts = enrich_posts(posts, json.loads(metadata))
        except (FetchFailure, ValueError, KeyError, TypeError) as exc:
            log.warning('subreddit=%s video_metadata_unavailable type=%s', subreddit, type(exc).__name__)
    previous = await pool.fetch("SELECT db_primary_id, clean_delivery_url FROM reddit_media_posts WHERE subreddit=$1 AND media_category='video_hls'", subreddit)
    known = {row['db_primary_id']: row['clean_delivery_url'] for row in previous}
    # Playlists come from Reddit's video CDN, which the browser loads directly when playing.
    # Through the Web API each check would be a billed request, so those go direct.
    media_route = None if isinstance(route, WebApi) else route
    resolved = []
    for post in posts:
        row = list(post)
        if row[3] == 'third_party_embed' and urlparse(row[4]).hostname == 'v.redd.it':
            manifest = row[4].split('?')[0].rstrip('/') + '/HLSPlaylist.m3u8'
            if known.get(row[0]) == manifest:
                row[3], row[4] = 'video_hls', manifest
            else:
                try:
                    manifest_body, _ = await fetch_once(session, manifest, media_route)
                    if manifest_body.lstrip().startswith(b'#EXTM3U'):
                        row[3], row[4] = 'video_hls', manifest
                except FetchFailure as exc:
                    log.warning('subreddit=%s video_manifest_unavailable %s', subreddit, exc)
                    if stops_worker(exc):
                        raise
        resolved.append(tuple(row))
    posts = resolved
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(UPSERT, posts)
    try:
        for feed in (subreddit, 'all'):
            async for key in cache.scan_iter(match=f'feed:v3:{feed}:*', count=100):
                await cache.delete(key)
    except RedisError:
        pass  # Existing cached pages expire within 15 seconds.
    log.info('subreddit=%s upserted=%d videos=%d', subreddit, len(posts),
             sum(row[3] in ('video_hls', 'video_mp4') for row in posts))
    return len(posts)


async def upgrade_stored_images(pool):
    """Re-pick full-size images for rows stored before originals were preferred. No network."""
    rows = await pool.fetch("""SELECT db_primary_id, clean_delivery_url, thumbnail_url
        FROM reddit_media_posts WHERE media_category = 'direct_image'""")
    changes = []
    for row in rows:
        image, preview = pick_image([row['clean_delivery_url'], row['thumbnail_url']])
        if image and (image, preview) != (row['clean_delivery_url'], row['thumbnail_url']):
            changes.append((row['db_primary_id'], image, preview))
    if changes:
        await pool.executemany(
            'UPDATE reddit_media_posts SET clean_delivery_url=$2, thumbnail_url=$3 WHERE db_primary_id=$1',
            changes)
    return len(changes)


async def diagnose(session, route, subreddit):
    # The exit-IP checks only apply to a proxy; the Web API check is the feed itself (one request).
    checks = [] if isinstance(route, WebApi) else list(IP_CHECK_URLS)
    results = []
    for name, url in checks + [('reddit_feed', f'https://www.reddit.com/r/{subreddit}/.rss')]:
        try:
            body, content_type = await fetch_once(session, url, route)
            result = {'check': name, 'ok': True, 'status': 200,
                      'bytes': len(body), 'content_type': content_type}
            if name == 'reddit_feed':
                result['posts'] = len(parse_posts(body, subreddit))
            else:
                result['exit_country'] = exit_country(body)
        except FetchFailure as exc:
            result = {'check': name, 'ok': False, 'stage': exc.stage, 'status': exc.status}
        results.append(result)
        print(json.dumps(result), flush=True)
    return 0 if all(item['ok'] for item in results) else 1


def exit_country(body: bytes):
    """Country of the proxy exit IP from Oxylabs' location check, if it says."""
    try:
        data = json.loads(body)
        providers = data.get('providers', {}) if isinstance(data, dict) else {}
        for info in providers.values():
            if isinstance(info, dict) and isinstance(info.get('country'), str):
                return info['country']
    except (ValueError, AttributeError):
        pass
    return None


async def run(args):
    if args.upgrade_images:
        pool = await open_pool()
        try:
            log.info('Upgraded %d stored images to full size', await upgrade_stored_images(pool))
        finally:
            await pool.close()
        return 0
    route = load_web_api() or load_proxy()
    log.info('Fetching Reddit through: %s', route.label)
    subreddits = [normalize_feed(value) for value in os.getenv('SUBREDDITS', 'pics').split(',') if value.strip()]
    if not subreddits:
        raise ValueError('Set at least one SUBREDDITS entry')
    async with session_factory() as session:
        if args.diagnose:
            return await diagnose(session, route, subreddits[0])
        pool = await open_pool()
        cache = Redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'),
                               socket_timeout=1, socket_connect_timeout=1)
        try:
            # Recover an interrupted on-demand request on worker restart.
            await pool.execute("UPDATE subreddit_requests SET status='queued' WHERE status='loading'")
            next_poll = 0
            while True:
                failed = False
                job = await pool.fetchrow('''UPDATE subreddit_requests SET status='loading', updated_at=now()
                    WHERE subreddit=(SELECT subreddit FROM subreddit_requests WHERE status='queued'
                    ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING subreddit''')
                if job:
                    targets = [job['subreddit']]
                elif time.monotonic() >= next_poll:
                    targets = subreddits
                    next_poll = time.monotonic() + max(60, int(os.getenv('POLL_SECONDS', '300')))
                else:
                    await asyncio.sleep(2)
                    continue
                for subreddit in targets:
                    try:
                        count = await ingest(session, route, pool, cache, subreddit)
                        if job:
                            await pool.execute("UPDATE subreddit_requests SET status='ready', post_count=$2, message=NULL, updated_at=now() WHERE subreddit=$1", subreddit, count)
                    except FetchFailure as exc:
                        failed = True
                        log.error('subreddit=%s ingestion_failed %s', subreddit, exc)
                        if job:
                            message = ('This community is unavailable or restricted.' if exc.status in {401,403,404}
                                       else 'Could not load this community. Please try again later.')
                            await pool.execute("UPDATE subreddit_requests SET status='error', message=$2, updated_at=now() WHERE subreddit=$1", subreddit, message)
                        # Stop globally on rejected credentials, a spent quota or persistent throttling.
                        if stops_worker(exc):
                            return 1
                    await asyncio.sleep(2)
                if args.once:
                    return int(failed)
        finally:
            await cache.aclose()
            await pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--diagnose', action='store_true', help='Check the Oxylabs connection without opening a database')
    mode.add_argument('--once', action='store_true', help='Ingest a single polling cycle')
    mode.add_argument('--upgrade-images', action='store_true',
                      help='Point stored image posts at full-size originals (no network needed)')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except KeyboardInterrupt:
        pass
    except ValueError as exc:
        # Configuration errors are written without secrets, so their message is safe to show.
        log.error('Worker configuration error: %s', exc)
        raise SystemExit(2) from None
    except Exception as exc:
        log.error('Worker stopped: %s (check configuration and database availability)', type(exc).__name__)
        raise SystemExit(1) from None
