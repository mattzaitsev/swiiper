import base64
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import aiohttp
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app import main, worker


def row(number):
    return dict(db_primary_id=f't3_{number}', title='A & B', subreddit='pics',
                upvote_score=0, media_category='direct_image',
                clean_delivery_url='https://i.redd.it/test.jpg', thumbnail_url=None,
                source_origin_url='https://www.reddit.com/r/pics/comments/abc/title/',
                created_utc=datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.fixture
def client():
    main.app.state.pool = AsyncMock()
    main.app.state.cache = AsyncMock()
    main.app.state.cache.get.return_value = None
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')


@pytest.mark.asyncio
async def test_alias_routes_cursor_and_thumbnail(client):
    main.app.state.pool.fetch.return_value = [row(3), row(2), row(1)]
    async with client:
        first = await client.get('/api/v1/feed', params={'subreddit': 'pics', 'limit': 2})
        modern = await client.get('/pics', params={'limit': 2})
        assert first.status_code == modern.status_code == 200
        assert first.json() == modern.json()
        body = first.json()
        assert body['posts'] == body['results']
        assert body['posts'][0]['mediaUrl'].endswith('.jpg')
        assert body['posts'][0]['thumbnail'] is None
        assert main.decode_cursor(body['after']) == 2
        assert body['cursors']['after'] == body['after']
        main.app.state.pool.fetch.return_value = [row(1)]
        for key in ('after', 'cursors'):
            final = await client.get('/pics', params={key: body['after'], 'limit': 2})
            assert final.json()['after'] is None
            assert main.app.state.pool.fetch.call_args.args[3] == 2
        assert 'thumbnail_url' in main.app.state.pool.fetch.call_args.args[0]


@pytest.mark.asyncio
async def test_empty_and_cache_outage(client):
    main.app.state.pool.fetch.return_value = []
    main.app.state.cache.get.side_effect = RedisConnectionError()
    main.app.state.cache.set.side_effect = RedisConnectionError()
    async with client:
        response = await client.get('/pics')
        assert response.status_code == 200
        assert response.json()['posts'] == []
        assert response.json()['after'] is None
        main.app.state.pool.fetch.side_effect = OSError()
        assert (await client.get('/pics')).status_code == 503


@pytest.mark.parametrize('value', [-1, True, 1.5, '50', 1000001])
def test_invalid_offsets(value):
    cursor = base64.b64encode(json.dumps({'offset': value}).encode()).decode()
    with pytest.raises(main.HTTPException):
        main.decode_cursor(cursor)


@pytest.mark.asyncio
async def test_bad_requests(client):
    async with client:
        for params in ({'after': 'bad!!'}, {'limit': 0}, {'limit': 51},
                       {'after': main.encode_cursor(1), 'cursors': main.encode_cursor(2)}):
            assert (await client.get('/pics', params=params)).status_code in (400, 422)
        assert (await client.get('/api/v1/feed', params={'subreddit': "pics';--"})).status_code == 400


@pytest.mark.asyncio
async def test_media_byte_ranges(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'MEDIA_ROOT', tmp_path)
    (tmp_path / 'sample.mp4').write_bytes(b'0123456789')
    async with client:
        full = await client.get('/media/sample.mp4')
        assert full.status_code == 200 and full.content == b'0123456789'
        for value, content, header in [('bytes=2-5', b'2345', 'bytes 2-5/10'),
                                       ('bytes=-3', b'789', 'bytes 7-9/10'),
                                       ('bytes=7-', b'789', 'bytes 7-9/10')]:
            response = await client.get('/media/sample.mp4', headers={'Range': value})
            assert response.status_code == 206 and response.content == content
            assert response.headers['content-range'] == header
        response = await client.get('/media/sample.mp4', headers={'Range': 'bytes=20-'})
        assert response.status_code == 416
        assert response.headers['content-range'] == 'bytes */10'
        assert (await client.head('/media/sample.mp4')).content == b''
        assert (await client.get('/media/missing.mp4')).status_code == 404
        assert (await client.get('/media/..%5Csecret.mp4')).status_code == 404


ATOM = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>pics</title><id>pics</id>
<updated>2026-01-01T00:00:00Z</updated><entry>
<id>t3_abc</id><updated>2026-01-01T00:00:00Z</updated>
<content type="html">&lt;a href="https://i.redd.it/a.jpg"&gt;[link]&lt;/a&gt;</content>
<link href="https://www.reddit.com/r/pics/comments/abc/title/"/>
<title>A &amp; B</title></entry></feed>'''


def test_atom_order_and_schema():
    rows = worker.parse_posts(ATOM, 'pics')
    assert len(rows) == 1 and len(rows[0]) == 8
    assert rows[0][:5] == ('t3_abc', 'A & B', 'pics', 'direct_image', 'https://i.redd.it/a.jpg')
    assert rows[0][-1] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert 'author' not in worker.UPSERT and 'height' not in worker.UPSERT


def test_non_media_is_link_and_html_is_rejected():
    rows = worker.parse_posts(ATOM.replace(b'https://i.redd.it/a.jpg', b'https://v.redd.it/abc'), 'pics')
    assert rows[0][3] == 'third_party_embed'
    with pytest.raises(worker.FetchFailure, match='invalid_feed'):
        worker.parse_posts(b'<html>Blocked</html>', 'pics')


@pytest.mark.asyncio
async def test_real_connect_error_classification():
    # A local TCP proxy rejects CONNECT before any TLS handshake occurs.
    import asyncio
    async def reject(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(reject, '127.0.0.1', 0)
    async with server, worker.session_factory() as session:
        proxy = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        with pytest.raises(worker.FetchFailure) as caught:
            await worker.fetch_once(session, 'https://www.reddit.com/r/pics/.rss', proxy, None)
        assert caught.value.stage == 'proxy_connect'
        assert caught.value.status == 502


@pytest.mark.asyncio
async def test_bounded_retry_and_auth_stop(monkeypatch):
    fetch = AsyncMock(side_effect=worker.FetchFailure('proxy_connect', 502, True))
    sleep = AsyncMock()
    monkeypatch.setattr(worker, 'fetch_once', fetch)
    monkeypatch.setattr(worker.asyncio, 'sleep', sleep)
    with pytest.raises(worker.FetchFailure):
        await worker.fetch_with_retries(None, 'url', None, None)
    assert fetch.await_count == 3 and sleep.await_count == 2
    fetch.reset_mock(); sleep.reset_mock()
    fetch.side_effect = worker.FetchFailure('proxy_connect', 407, False)
    with pytest.raises(worker.FetchFailure):
        await worker.fetch_with_retries(None, 'url', None, None)
    assert fetch.await_count == 1 and sleep.await_count == 0


@pytest.mark.asyncio
async def test_retry_after_honored(monkeypatch):
    fetch = AsyncMock(side_effect=[worker.FetchFailure('http_response', 429, True, 60), (b'ok', 'text/plain')])
    sleep = AsyncMock()
    monkeypatch.setattr(worker, 'fetch_once', fetch)
    monkeypatch.setattr(worker.asyncio, 'sleep', sleep)
    await worker.fetch_with_retries(None, 'url', None, None)
    assert sleep.await_args.args[0] >= 60


def test_proxy_requires_explicit_credentials(monkeypatch):
    monkeypatch.setenv('PROXY_AUTH_MODE', 'credentials')
    monkeypatch.delenv('PROXY_USERNAME', raising=False)
    with pytest.raises(ValueError):
        worker.proxy_config()
    monkeypatch.setenv('PROXY_USERNAME', 'dashboard-user')
    monkeypatch.setenv('PROXY_PASSWORD', 'test-secret')
    url, auth = worker.proxy_config()
    assert url == 'http://p.webshare.io:80' and auth.login == 'dashboard-user'


def test_proxy_export_preserves_exact_credentials(monkeypatch, tmp_path):
    path = tmp_path / 'proxies.txt'
    path.write_text('p.webshare.io:80:exported-user-123:secret\n', encoding='utf-8')
    monkeypatch.setenv('PROXY_LIST_FILE', str(path))
    proxy, auth = worker.ProxyPool().get()
    assert proxy == 'http://p.webshare.io:80'
    assert auth.login == 'exported-user-123' and auth.password == 'secret'


def test_invalid_proxy_export_does_not_leak(monkeypatch, tmp_path):
    path = tmp_path / 'proxies.txt'
    path.write_text('p.webshare.io:bad:username:secret\n', encoding='utf-8')
    monkeypatch.setenv('PROXY_LIST_FILE', str(path))
    with pytest.raises(ValueError, match='line 1') as caught:
        worker.ProxyPool()
    assert 'secret' not in str(caught.value)


@pytest.mark.asyncio
async def test_home_and_video_filter(client):
    main.app.state.pool.fetch.return_value = [dict(row(1), media_category='video_hls',
                                                 clean_delivery_url='https://v.redd.it/abc/HLSPlaylist.m3u8')]
    async with client:
        response = await client.get('/api/v1/feed', params={'subreddit':'all','kind':'video'})
        assert response.status_code == 200
        assert response.json()['posts'][0]['mediaType'] == 'video'
        assert main.app.state.pool.fetch.call_args.args[1:] == ('all', 26, 0, 'video')
        assert (await client.get('/api/v1/feed?kind=invalid')).status_code == 422


@pytest.mark.asyncio
async def test_community_search_and_status(client):
    main.app.state.pool.fetch.return_value = [{'subreddit':'aww','posts':25,'videos':4,'cover':None}]
    main.app.state.pool.fetchrow.return_value = None
    async with client:
        response = await client.get('/api/v1/subreddits?q=aww')
        assert response.json()['subreddits'][0]['videos'] == 4
        assert (await client.get('/api/v1/subreddits/new_sub/status')).json()['status'] == 'idle'
        assert (await client.post('/api/v1/subreddits/all/refresh')).status_code == 400
        assert (await client.post('/api/v1/subreddits/bad!/refresh')).status_code == 400


def test_video_metadata_enrichment():
    listing = {'data':{'children':[{'data':{'name':'t3_abc','secure_media':{'reddit_video':
        {'hls_url':'https://v.redd.it/abc/HLSPlaylist.m3u8'}},'thumbnail':'https://i.redd.it/poster.jpg'}}]}}
    post = worker.enrich_posts(worker.parse_posts(ATOM, 'pics'), listing)[0]
    assert post[3] == 'video_hls' and post[4].endswith('.m3u8')
    assert post[5] == 'https://i.redd.it/poster.jpg'
