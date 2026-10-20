"""RSS ingestion plus a one-request-per-stage proxy diagnostic.

No generated credential suffixes, TLS impersonation or disabled certificate checks.
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
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import feedparser
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .db import open_pool
from .main import normalize_feed

log = logging.getLogger('swiiper.worker')
TRANSIENT = {429, 500, 502, 503, 504}
MAX_BODY = 4 * 1024 * 1024


@dataclass
class FetchFailure(Exception):
    stage: str
    status: int | None = None
    retryable: bool = False
    retry_after: float | None = None

    def __str__(self):
        # Never serialize aiohttp exceptions: their request info can contain credentials.
        return f'stage={self.stage} status={self.status}'


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


def proxy_config():
    endpoint = os.getenv('PROXY_URL', 'http://p.webshare.io:80')
    url = urlparse(endpoint)
    if url.scheme != 'http' or not url.hostname or url.username or url.password:
        raise ValueError('PROXY_URL must be an HTTP gateway URL without embedded credentials')
    mode = os.getenv('PROXY_AUTH_MODE', 'credentials')
    if mode == 'ip':
        return endpoint, None
    if mode != 'credentials':
        raise ValueError('PROXY_AUTH_MODE must be credentials or ip')
    username, password = os.getenv('PROXY_USERNAME', ''), os.getenv('PROXY_PASSWORD', '')
    if not username or not password:
        raise ValueError('Set PROXY_USERNAME and PROXY_PASSWORD from the dashboard Endpoint Generator')
    return endpoint, aiohttp.BasicAuth(username, password)


class ProxyPool:
    """Use actual exported host:port:username:password entries, never guessed IDs."""
    def __init__(self):
        self.entries = []
        filename = os.getenv('PROXY_LIST_FILE')
        if filename:
            for number, line in enumerate(Path(filename).read_text(encoding='utf-8-sig').splitlines(), 1):
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                fields = line.strip().split(':', 3)
                if (len(fields) != 4 or not re.fullmatch(r'[A-Za-z0-9.-]+', fields[0])
                        or not fields[1].isdigit() or not 1 <= int(fields[1]) <= 65535
                        or not fields[2] or not fields[3]):
                    raise ValueError(f'Invalid proxy export format on line {number}')
                host, port, username, password = fields
                self.entries.append((f'http://{host}:{port}', aiohttp.BasicAuth(username, password)))
        if not self.entries:
            self.entries.append(proxy_config())

    def get(self):
        return random.choice(self.entries)


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


async def fetch_once(session, url, proxy, auth):
    try:
        async with session.get(url, proxy=proxy, proxy_auth=auth, allow_redirects=False) as response:
            if response.status != 200:
                raise FetchFailure('http_response', response.status,
                                   response.status in TRANSIENT,
                                   retry_delay(response.headers.get('Retry-After')))
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_BODY:
                    raise FetchFailure('body_too_large')
                chunks.append(chunk)
            return b''.join(chunks), response.headers.get('Content-Type', '')
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


async def fetch_with_retries(session, url, proxy, auth, attempts=3):
    for attempt in range(attempts):
        try:
            return await fetch_once(session, url, proxy, auth)
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


def parse_posts(body: bytes, subreddit: str):
    feed = feedparser.parse(body)
    if not feed.version or feed.bozo:
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
        media_url, media_type = permalink, 'unknown'
        # Prefer a direct video/image over a thumbnail; never invent a DASH MP4 URL.
        for suffixes, kind in [(('.mp4',), 'video_hls'), (('.jpg', '.jpeg', '.png', '.webp', '.gif'), 'direct_image')]:
            for candidate in candidates:
                if safe_url(candidate) and urlparse(candidate).path.lower().endswith(suffixes):
                    media_url, media_type = candidate, kind
                    break
            if media_type != 'unknown':
                break
        thumbnails = entry.get('media_thumbnail', [])
        thumbnail = safe_url(thumbnails[0].get('url')) if thumbnails else None
        hosted_video = next((url for url in candidates if safe_url(url)
                             and urlparse(url).hostname == 'v.redd.it'
                             and re.fullmatch(r'/[a-zA-Z0-9]+/?', urlparse(url).path)), None)
        if hosted_video and media_type != 'video_hls':
            if media_type == 'direct_image':
                thumbnail = thumbnail or media_url
            media_type, media_url = 'third_party_embed', hosted_video
        posts.append((str(post_id), entry.get('title', 'Untitled'), subreddit,
                      media_type, media_url, thumbnail, permalink,
                      datetime.fromtimestamp(calendar.timegm(stamp), timezone.utc)))
    return posts


UPSERT = '''INSERT INTO reddit_media_posts
    (db_primary_id, title, subreddit, media_category, clean_delivery_url,
     thumbnail_url, source_origin_url, created_utc)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
    ON CONFLICT (db_primary_id) DO UPDATE SET title=EXCLUDED.title,
      clean_delivery_url=EXCLUDED.clean_delivery_url, media_category=EXCLUDED.media_category,
      thumbnail_url=EXCLUDED.thumbnail_url, source_origin_url=EXCLUDED.source_origin_url,
      created_utc=EXCLUDED.created_utc'''


def enrich_posts(posts, listing):
    """Use URLs returned by Reddit metadata, including audio-bearing HLS streams."""
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
                row[3], row[4] = 'video_hls', url
                break
        preview = data.get('preview', {}).get('images', [])
        thumbnail = safe_url(data.get('thumbnail'))
        if preview:
            thumbnail = safe_url(preview[0].get('source', {}).get('url')) or thumbnail
        row[5] = thumbnail or row[5]
        enriched.append(tuple(row))
    return enriched


async def ingest(session, proxies, pool, subreddit):
    proxy, auth = proxies.get()
    body, _ = await fetch_with_retries(session, f'https://www.reddit.com/r/{subreddit}/.rss', proxy, auth)
    posts = parse_posts(body, subreddit)
    # Optional for deployments with permitted JSON access. This sandbox's JSON
    # endpoint returns 403, so it is disabled rather than repeatedly retried.
    if os.getenv('REDDIT_JSON_ENRICHMENT', '0') == '1':
        try:
            metadata, _ = await fetch_once(session,
                f'https://www.reddit.com/r/{subreddit}/.json?limit=25&raw_json=1', proxy, auth)
            posts = enrich_posts(posts, json.loads(metadata))
        except (FetchFailure, ValueError, KeyError, TypeError) as exc:
            log.warning('subreddit=%s video_metadata_unavailable type=%s', subreddit, type(exc).__name__)
    previous = await pool.fetch("SELECT db_primary_id, clean_delivery_url FROM reddit_media_posts WHERE subreddit=$1 AND media_category='video_hls'", subreddit)
    known = {row['db_primary_id']: row['clean_delivery_url'] for row in previous}
    resolved = []
    for post in posts:
        row = list(post)
        if row[3] == 'third_party_embed' and urlparse(row[4]).hostname == 'v.redd.it':
            manifest = row[4].split('?')[0].rstrip('/') + '/HLSPlaylist.m3u8'
            if known.get(row[0]) == manifest:
                row[3], row[4] = 'video_hls', manifest
            else:
                try:
                    manifest_body, _ = await fetch_once(session, manifest, proxy, auth)
                    if manifest_body.lstrip().startswith(b'#EXTM3U'):
                        row[3], row[4] = 'video_hls', manifest
                except FetchFailure as exc:
                    log.warning('subreddit=%s video_manifest_unavailable %s', subreddit, exc)
                    if exc.status in {407, 429, 506}:
                        raise
        resolved.append(tuple(row))
    posts = resolved
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(UPSERT, posts)
    cache = Redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'),
                           socket_timeout=1, socket_connect_timeout=1)
    try:
        for feed in (subreddit, 'all'):
            async for key in cache.scan_iter(match=f'feed:v2:{feed}:*', count=100):
                await cache.delete(key)
    except RedisError:
        pass  # Existing cached pages expire within 15 seconds.
    finally:
        await cache.aclose()
    log.info('subreddit=%s upserted=%d videos=%d', subreddit, len(posts),
             sum(row[3] == 'video_hls' for row in posts))
    return len(posts)


async def diagnose(session, proxy, auth, subreddit):
    results = []
    for name, url in [('http_gateway', 'http://ipv4.webshare.io/'),
                      ('https_connect', 'https://ipv4.webshare.io/'),
                      ('reddit_feed', f'https://www.reddit.com/r/{subreddit}/.rss')]:
        try:
            body, content_type = await fetch_once(session, url, proxy, auth)
            result = {'check': name, 'ok': True, 'status': 200,
                      'bytes': len(body), 'content_type': content_type}
            if name == 'reddit_feed':
                result['posts'] = len(parse_posts(body, subreddit))
        except FetchFailure as exc:
            result = {'check': name, 'ok': False, 'stage': exc.stage, 'status': exc.status}
        results.append(result)
        print(json.dumps(result), flush=True)
    return 0 if all(item['ok'] for item in results) else 1


async def run(args):
    proxies = ProxyPool()
    log.info('Loaded %d configured proxy entries', len(proxies.entries))
    subreddits = [normalize_feed(value) for value in os.getenv('SUBREDDITS', 'pics').split(',') if value.strip()]
    if not subreddits:
        raise ValueError('Set at least one SUBREDDITS entry')
    async with session_factory() as session:
        if args.diagnose:
            proxy, auth = proxies.entries[0]
            return await diagnose(session, proxy, auth, subreddits[0])
        pool = await open_pool()
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
                        count = await ingest(session, proxies, pool, subreddit)
                        if job:
                            await pool.execute("UPDATE subreddit_requests SET status='ready', post_count=$2, message=NULL, updated_at=now() WHERE subreddit=$1", subreddit, count)
                    except FetchFailure as exc:
                        failed = True
                        log.error('subreddit=%s ingestion_failed %s', subreddit, exc)
                        if job:
                            message = ('This community is unavailable or restricted.' if exc.status in {401,403,404}
                                       else 'Could not load this community. Please try again later.')
                            await pool.execute("UPDATE subreddit_requests SET status='error', message=$2, updated_at=now() WHERE subreddit=$1", subreddit, message)
                        # Stop globally for auth/denial, throttling or unknown 506 semantics.
                        # Do not cycle usernames/IPs to defeat a denial or Retry-After.
                        if exc.status in {407, 429, 506}:
                            return 1
                    await asyncio.sleep(2)
                if args.once:
                    return int(failed)
        finally:
            await pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--diagnose', action='store_true', help='Probe proxy stages without opening a database')
    mode.add_argument('--once', action='store_true', help='Ingest a single polling cycle')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.error('Worker stopped: %s (check configuration and database availability)', type(exc).__name__)
        raise SystemExit(1) from None
