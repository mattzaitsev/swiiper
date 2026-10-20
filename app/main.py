import base64
import binascii
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import aiohttp
import asyncpg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .db import open_pool
from .directory import SubredditDirectory

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
MEDIA_ROOT = Path(os.getenv('MEDIA_DIR', str(ROOT / 'media'))).resolve()
MAX_OFFSET = 1_000_000


class MediaFileResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        async def send_with_range_unit(message):
            # Starlette 0.52.1 emits '*/size' for 416; HTTP requires 'bytes */size'.
            if message['type'] == 'http.response.start' and message['status'] == 416:
                message['headers'] = [
                    (key, b'bytes ' + value if key == b'content-range' and value.startswith(b'*/') else value)
                    for key, value in message['headers']]
            await send(message)
        await super().__call__(scope, receive, send_with_range_unit)


def normalize_feed(value: str) -> str:
    value = value.strip().removeprefix('/r/').removeprefix('r/').rstrip('/').lower()
    if not re.fullmatch(r'[a-z0-9_]{1,50}', value):
        raise ValueError('Use one subreddit name containing letters, numbers or underscores')
    return value


def encode_cursor(offset: int) -> str:
    return base64.b64encode(json.dumps({'offset': offset}, separators=(',', ':')).encode()).decode()


def decode_cursor(token: str | None) -> int:
    if token is None:
        return 0
    try:
        if not token or len(token) > 256:
            raise ValueError()
        data = json.loads(base64.b64decode(token + '=' * (-len(token) % 4),
                                          altchars=b'-_', validate=True))
        if not isinstance(data, dict) or set(data) != {'offset'}:
            raise ValueError()
        offset = data['offset']
        if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
            raise ValueError()
        return offset
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise HTTPException(400, 'Invalid pagination cursor') from None


@asynccontextmanager
async def lifespan(app):
    app.state.pool = await open_pool()
    app.state.cache = Redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'),
                                    decode_responses=True, socket_timeout=1,
                                    socket_connect_timeout=1)
    app.state.directory_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
        timeout=aiohttp.ClientTimeout(total=6, connect=3),
        headers={'User-Agent': 'swiiper-sandbox/1.0', 'Accept': 'application/json'})
    app.state.directory = SubredditDirectory(app.state.directory_session)
    try:
        yield
    finally:
        await app.state.directory_session.close()
        await app.state.cache.aclose()
        await app.state.pool.close()


app = FastAPI(title='Swiiper sandbox', lifespan=lifespan)
app.mount('/assets', StaticFiles(directory=ROOT / 'web', check_dir=False), name='assets')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=False,
                   allow_methods=['GET', 'HEAD'], allow_headers=['Range'],
                   expose_headers=['Content-Range', 'Accept-Ranges', 'Content-Length'])


@app.get('/', include_in_schema=False)
async def index():
    return FileResponse(ROOT / 'index.html')


@app.get('/healthz')
async def health(request: Request):
    try:
        await request.app.state.pool.fetchval('SELECT 1')
    except (asyncpg.PostgresError, OSError, TimeoutError):
        raise HTTPException(503, 'Database unavailable') from None
    try:
        await request.app.state.cache.ping()
        cache = 'ok'
    except RedisError:
        cache = 'unavailable'
    return {'database': 'ok', 'cache': cache}


async def feed_response(request: Request, feed: str, after: str | None, limit: int, kind: str = 'all'):
    try:
        feed = normalize_feed(feed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    offset = decode_cursor(after)
    key = f'feed:v2:{feed}:{kind}:{offset}:{limit}'
    cache = request.app.state.cache
    try:
        cached = await cache.get(key)
        if cached:
            return json.loads(cached)
    except (RedisError, ValueError):
        log.warning('Feed cache unavailable or invalid; querying database')
    try:
        rows = await request.app.state.pool.fetch(
            '''SELECT db_primary_id, title, subreddit, upvote_score, media_category,
                      clean_delivery_url, thumbnail_url, source_origin_url, created_utc
               FROM reddit_media_posts WHERE ($1 = 'all' OR subreddit = $1)
                 AND ($4 = 'all' OR ($4 = 'video' AND media_category = 'video_hls')
                      OR ($4 = 'image' AND media_category = 'direct_image'))
               ORDER BY created_utc DESC, db_primary_id DESC LIMIT $2 OFFSET $3''',
            feed, limit + 1, offset, kind)
    except (asyncpg.PostgresError, OSError, TimeoutError):
        raise HTTPException(503, 'Feed database unavailable') from None
    posts = []
    for row in rows[:limit]:
        posts.append({
            'id': row['db_primary_id'].removeprefix('t3_'),
            'title': row['title'], 'subreddit': row['subreddit'],
            'upvotes': row['upvote_score'],
            'mediaType': {'direct_image': 'image', 'video_hls': 'video'}.get(row['media_category'], 'link'),
            'mediaUrl': row['clean_delivery_url'],
            'thumbnail': row['thumbnail_url'],
            'sourceUrl': row['source_origin_url'],
            'timestamp': row['created_utc'].isoformat(),
        })
    next_offset = offset + len(posts)
    has_more = len(rows) > limit and next_offset <= MAX_OFFSET
    # `posts` is canonical. `results` is a temporary legacy alias on BOTH routes.
    payload = {'posts': posts, 'results': posts, 'subreddit': feed,
               'after': encode_cursor(next_offset) if has_more else None,
               'cursors': {'after': encode_cursor(next_offset) if has_more else None},
               'has_more': has_more}
    try:
        # An empty feed is cached briefly so successful ingestion appears quickly.
        await cache.set(key, json.dumps(payload), ex=15 if posts else 2)
    except RedisError:
        log.warning('Could not cache feed')
    return payload


@app.get('/api/v1/feed')
async def legacy_feed(request: Request, subreddit: str = 'pics', after: str | None = None,
                      cursors: str | None = None, limit: int = Query(25, ge=1, le=50),
                      kind: Literal['all', 'image', 'video'] = 'all'):
    return await feed_response(request, subreddit, select_cursor(after, cursors), limit, kind)


@app.get('/api/v1/subreddits')
async def communities(request: Request, q: str = Query('', max_length=50)):
    # strpos treats user input literally (including underscores), unlike LIKE.
    try:
        rows = await request.app.state.pool.fetch('''
            SELECT subreddit, count(*) AS posts,
                   count(*) FILTER (WHERE media_category='video_hls') AS videos,
                   (array_agg(COALESCE(thumbnail_url,
                       CASE WHEN media_category='direct_image' THEN clean_delivery_url END)
                       ORDER BY created_utc DESC)
                       FILTER (WHERE thumbnail_url IS NOT NULL OR media_category='direct_image'))[1] AS cover
            FROM reddit_media_posts WHERE strpos(subreddit, $1) > 0
            GROUP BY subreddit ORDER BY count(*) DESC, subreddit LIMIT 60
        ''', q.strip().lower().removeprefix('r/'))
    except (asyncpg.PostgresError, OSError, TimeoutError):
        raise HTTPException(503, 'Communities unavailable') from None
    return {'subreddits': [dict(row) for row in rows]}


@app.get('/api/v1/subreddits/suggest')
async def suggest_communities(request: Request, q: str = Query('', max_length=54)):
    query = q.strip().lower().removeprefix('/r/').removeprefix('r/').rstrip('/')
    if not query:
        return {'subreddits': [], 'source': 'directory', 'available': True}
    if not re.fullmatch(r'[a-z0-9_]{1,50}', query):
        raise HTTPException(400, 'Use letters, numbers, and underscores')
    try:
        rows = await request.app.state.directory.search(query)
        return {'subreddits': rows, 'source': 'reddclips', 'available': True}
    except (aiohttp.ClientError, TimeoutError, ValueError):
        # A directory outage must not stop browsing communities already loaded.
        try:
            rows = await request.app.state.pool.fetch(
                "SELECT DISTINCT subreddit FROM reddit_media_posts WHERE strpos(subreddit, $1)>0 ORDER BY subreddit LIMIT 12", query)
        except (asyncpg.PostgresError, OSError, TimeoutError):
            rows = []
        return {'subreddits': [{'subreddit': row['subreddit'],
                               'displayName': row['subreddit'], 'members': None} for row in rows],
                'source': 'local', 'available': False}


@app.post('/api/v1/subreddits/{name}/refresh', status_code=202)
async def refresh_community(request: Request, name: str):
    try:
        name = normalize_feed(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if name == 'all':
        raise HTTPException(400, 'Choose a specific subreddit')
    try:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(73491261)')
                current = await conn.fetchrow('SELECT * FROM subreddit_requests WHERE subreddit=$1', name)
                if current and (current['status'] in ('queued', 'loading') or
                                (datetime.now(timezone.utc) - current['updated_at']).total_seconds() < 60):
                    return dict(current)
                pending = await conn.fetchval("SELECT count(*) FROM subreddit_requests WHERE status IN ('queued','loading')")
                if pending >= 20:
                    raise HTTPException(429, 'Several communities are loading. Please try again shortly.')
                result = await conn.fetchrow('''INSERT INTO subreddit_requests (subreddit) VALUES ($1)
                    ON CONFLICT (subreddit) DO UPDATE SET status='queued', message=NULL, updated_at=now()
                    RETURNING *''', name)
                return dict(result)
    except (asyncpg.PostgresError, OSError, TimeoutError):
        raise HTTPException(503, 'Cannot load this community right now') from None


@app.get('/api/v1/subreddits/{name}/status')
async def community_status(request: Request, name: str):
    try:
        name = normalize_feed(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    try:
        row = await request.app.state.pool.fetchrow('SELECT * FROM subreddit_requests WHERE subreddit=$1', name)
    except (asyncpg.PostgresError, OSError, TimeoutError):
        raise HTTPException(503, 'Community status unavailable') from None
    return dict(row) if row else {'subreddit': name, 'status': 'idle'}


def select_cursor(after, cursors):
    legacy = None if cursors in (None, '{}') else cursors
    if after is not None and legacy is not None and decode_cursor(after) != decode_cursor(legacy):
        raise HTTPException(400, 'Conflicting after and cursors parameters')
    return after if after is not None else legacy


@app.api_route('/media/{filename}', methods=['GET', 'HEAD'])
async def media(filename: str):
    # Serve only sandbox-owned local MP4s, never an arbitrary URL or filesystem path.
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.mp4', filename):
        raise HTTPException(404, 'Media not found')
    path = (MEDIA_ROOT / filename).resolve()
    if path.parent != MEDIA_ROOT or not path.is_file():
        raise HTTPException(404, 'Media not found')
    return MediaFileResponse(path, media_type='video/mp4')


# Keep this catch-all AFTER the named routes.
@app.get('/{feed_name}')
async def modern_feed(request: Request, feed_name: str, after: str | None = None,
                      cursors: str | None = None, limit: int = Query(25, ge=1, le=50),
                      kind: Literal['all', 'image', 'video'] = 'all'):
    return await feed_response(request, feed_name, select_cursor(after, cursors), limit, kind)
