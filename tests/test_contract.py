import base64
import json
from html import escape
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app import main, names, proxy, webapi, worker


def row(number):
    return dict(db_primary_id=f't3_{number}', title='A & B', subreddit='pics',
                upvote_score=None, media_category='direct_image',
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
    assert len(rows) == 1 and len(rows[0]) == 9
    assert rows[0][:5] == ('t3_abc', 'A & B', 'pics', 'direct_image', 'https://i.redd.it/a.jpg')
    assert rows[0][7] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert rows[0][8] is None  # RSS carries no score
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
        proxy_url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        with pytest.raises(worker.FetchFailure) as caught:
            await worker.fetch_once(session, 'https://www.reddit.com/r/pics/.rss', proxy.ProxyEntry(url=proxy_url))
        assert caught.value.stage == 'proxy_connect'
        assert caught.value.status == 502


@pytest.mark.asyncio
async def test_bounded_retry_and_auth_stop(monkeypatch):
    fetch = AsyncMock(side_effect=worker.FetchFailure('proxy_connect', 502, True))
    sleep = AsyncMock()
    monkeypatch.setattr(worker, 'fetch_once', fetch)
    monkeypatch.setattr(worker.asyncio, 'sleep', sleep)
    with pytest.raises(worker.FetchFailure):
        await worker.fetch_with_retries(None, 'url', None)
    assert fetch.await_count == 3 and sleep.await_count == 2
    fetch.reset_mock(); sleep.reset_mock()
    fetch.side_effect = worker.FetchFailure('proxy_connect', 407, False)
    with pytest.raises(worker.FetchFailure):
        await worker.fetch_with_retries(None, 'url', None)
    assert fetch.await_count == 1 and sleep.await_count == 0


@pytest.mark.asyncio
async def test_retry_after_honored(monkeypatch):
    fetch = AsyncMock(side_effect=[worker.FetchFailure('http_response', 429, True, 60), (b'ok', 'text/plain')])
    sleep = AsyncMock()
    monkeypatch.setattr(worker, 'fetch_once', fetch)
    monkeypatch.setattr(worker.asyncio, 'sleep', sleep)
    await worker.fetch_with_retries(None, 'url', None)
    assert sleep.await_args.args[0] >= 60


OXY_ENV = ('OXYLABS_PROXY_PRODUCT', 'OXYLABS_PROXY_USERNAME', 'OXYLABS_PROXY_PASSWORD',
           'OXYLABS_PROXY_COUNTRY', 'OXYLABS_PROXY_STATE', 'OXYLABS_PROXY_SESSION_MINUTES',
           'OXYLABS_PROXY_ENDPOINT', 'OXYLABS_PROXY_CREDENTIALS_FILE')


@pytest.fixture
def oxy_env(monkeypatch):
    for name in OXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OXYLABS_PROXY_USERNAME', 'dashuser')
    monkeypatch.setenv('OXYLABS_PROXY_PASSWORD', 'test-secret')
    return monkeypatch


def sent_login(entry):
    encoded = entry.headers['Proxy-Authorization'].removeprefix('Basic ')
    return base64.b64decode(encoded).decode().split(':', 1)


def test_missing_credentials_names_both_options(oxy_env):
    oxy_env.delenv('OXYLABS_PROXY_PASSWORD')
    with pytest.raises(ValueError, match='OXYLABS_WEB_API_KEY.*OXYLABS_PROXY_USERNAME'):
        proxy.load_proxy()


def test_oxylabs_residential_defaults(oxy_env):
    entry = proxy.load_proxy()
    assert entry.url == 'http://pr.oxylabs.io:7777'
    assert sent_login(entry) == ['customer-dashuser', 'test-secret']
    assert 'test-secret' not in entry.label and 'test-secret' not in repr(entry)
    assert 'dashuser' not in entry.label


@pytest.mark.parametrize('product,country,state,minutes,login_pattern,endpoint', [
    ('residential', 'us', 'us_california', '30',
     r'customer-dashuser-cc-US-st-us_california-sessid-[0-9a-f]{12}-sesstime-30', 'pr.oxylabs.io:7777'),
    ('mobile', 'DE', '', '0', r'customer-dashuser-cc-DE', 'pr.oxylabs.io:7777'),
    ('datacenter', 'US', '', '0', r'user-dashuser-country-US', 'dc.oxylabs.io:8000'),
    ('isp', 'GB', '', '0', r'user-dashuser-country-GB', 'isp.oxylabs.io:8000'),
    ('dedicated-datacenter', 'US', '', '0', r'user-dashuser', 'ddc.oxylabs.io:8000'),
    ('dedicated-isp', '', '', '0', r'user-dashuser', 'disp.oxylabs.io:8000'),
])
def test_oxylabs_username_formats(oxy_env, product, country, state, minutes, login_pattern, endpoint):
    import re
    oxy_env.setenv('OXYLABS_PROXY_PRODUCT', product)
    oxy_env.setenv('OXYLABS_PROXY_COUNTRY', country)
    oxy_env.setenv('OXYLABS_PROXY_STATE', state)
    oxy_env.setenv('OXYLABS_PROXY_SESSION_MINUTES', minutes)
    entry = proxy.load_proxy()
    assert entry.url == f'http://{endpoint}'
    assert re.fullmatch(login_pattern, sent_login(entry)[0])


def test_oxylabs_prefix_and_endpoint_override(oxy_env):
    oxy_env.setenv('OXYLABS_PROXY_USERNAME', 'customer-dashuser')
    entry = proxy.load_proxy()
    assert sent_login(entry)[0] == 'customer-dashuser'
    oxy_env.setenv('OXYLABS_PROXY_PRODUCT', 'dedicated-datacenter')
    oxy_env.setenv('OXYLABS_PROXY_ENDPOINT', 'ddc.oxylabs.io:8001')
    entry = proxy.load_proxy()
    assert entry.url == 'http://ddc.oxylabs.io:8001' and sent_login(entry)[0] == 'user-dashuser'


@pytest.mark.parametrize('name,value', [
    ('OXYLABS_PROXY_PRODUCT', 'webshare'),
    ('OXYLABS_PROXY_ENDPOINT', 'http://pr.oxylabs.io:7777'),
    ('OXYLABS_PROXY_COUNTRY', 'USA'),
    ('OXYLABS_PROXY_STATE', 'california'),
    ('OXYLABS_PROXY_SESSION_MINUTES', '1441'),
    ('OXYLABS_PROXY_USERNAME', 'bad user'),
])
def test_oxylabs_rejects_bad_settings(oxy_env, name, value):
    oxy_env.setenv(name, value)
    with pytest.raises(ValueError):
        proxy.load_proxy()


def test_oxylabs_state_needs_us_residential(oxy_env):
    oxy_env.setenv('OXYLABS_PROXY_STATE', 'us_texas')
    oxy_env.setenv('OXYLABS_PROXY_COUNTRY', 'CA')
    with pytest.raises(ValueError, match='COUNTRY=US'):
        proxy.load_proxy()


def test_oxylabs_credentials_file_wins_and_placeholder_falls_back(oxy_env, tmp_path):
    secret = tmp_path / 'oxylabs-proxy.txt'
    secret.write_text('# comment\nfileuser:pa:ss\n', encoding='utf-8')
    oxy_env.setenv('OXYLABS_PROXY_CREDENTIALS_FILE', str(secret))
    assert sent_login(proxy.load_proxy()) == ['customer-fileuser', 'pa:ss']
    placeholder = tmp_path / 'oxylabs-proxy.example.txt'
    placeholder.write_text('# placeholder only\n', encoding='utf-8')
    oxy_env.setenv('OXYLABS_PROXY_CREDENTIALS_FILE', str(placeholder))
    assert sent_login(proxy.load_proxy()) == ['customer-dashuser', 'test-secret']


def test_invalid_credentials_file_does_not_leak(oxy_env, tmp_path):
    secret = tmp_path / 'oxylabs-proxy.txt'
    secret.write_text('no-separator-secret\n', encoding='utf-8')
    oxy_env.setenv('OXYLABS_PROXY_CREDENTIALS_FILE', str(secret))
    with pytest.raises(ValueError, match='line 1') as caught:
        proxy.load_proxy()
    assert 'secret' not in str(caught.value).replace('secret file', '')


@pytest.mark.asyncio
async def test_proxy_authorization_reaches_the_proxy():
    import asyncio
    seen = {}

    async def capture(reader, writer):
        head = (await reader.readuntil(b'\r\n\r\n')).decode()
        seen['request_line'] = head.split('\r\n', 1)[0]
        seen['auth'] = next((line.split(':', 1)[1].strip() for line in head.split('\r\n')
                             if line.lower().startswith('proxy-authorization:')), None)
        writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(capture, '127.0.0.1', 0)
    entry = proxy.ProxyEntry(url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}',
                             headers={'Proxy-Authorization': aiohttp.encode_basic_auth('customer-u-cc-US', 'p')})
    async with server, worker.session_factory() as session:
        with pytest.raises(worker.FetchFailure) as caught:
            await worker.fetch_once(session, 'https://www.reddit.com/r/pics/.rss', entry)
    assert seen['request_line'].startswith('CONNECT www.reddit.com:443')
    assert seen['auth'] == aiohttp.encode_basic_auth('customer-u-cc-US', 'p')
    assert caught.value.status == 407 and 407 in worker.STOP_STATUSES


def test_feed_with_harmless_parser_warning_is_kept():
    quirky = ATOM.replace(b'encoding="UTF-8"', b'encoding="ISO-8859-1"')
    rows = worker.parse_posts(quirky, 'pics')
    assert len(rows) == 1


def test_exit_country_from_location_check():
    body = json.dumps({'ip': '203.0.113.7', 'providers': {'dbip': {'country': 'US', 'city': 'Austin'}}}).encode()
    assert worker.exit_country(body) == 'US'
    assert worker.exit_country(b'not json') is None


@pytest.mark.asyncio
async def test_home_and_video_filter(client):
    main.app.state.pool.fetch.return_value = [dict(row(1), media_category='video_hls',
                                                 clean_delivery_url='https://v.redd.it/abc/HLSPlaylist.m3u8')]
    async with client:
        response = await client.get('/api/v1/feed', params={'subreddit':'all','kind':'video'})
        assert response.status_code == 200
        assert response.json()['posts'][0]['mediaType'] == 'video'
        assert main.app.state.pool.fetch.call_args.args[1:] == (
            'all', 26, 0, 'video', [], ['video_hls', 'video_mp4'])
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


def reddit_feed(*entries):
    return ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:media="http://search.yahoo.com/mrss/"><title>pics</title><id>pics</id>'
            '<updated>2026-01-01T00:00:00Z</updated>' + ''.join(entries) + '</feed>').encode()


def reddit_entry(post_id, img=None, link=None, thumbnail=None):
    """An entry shaped like Reddit's RSS: a preview <img>, then the post's [link]."""
    permalink = f'https://www.reddit.com/r/pics/comments/{post_id}/t/'
    picture = f'<img src="{escape(img)}" alt="t" />' if img else ''
    html = (f'<table><tr><td><a href="{permalink}">{picture}</a></td><td>submitted by '
            f'<a href="https://www.reddit.com/user/u">/u/u</a><br/>'
            f'<span><a href="{escape(link or permalink)}">[link]</a></span> '
            f'<span><a href="{permalink}">[comments]</a></span></td></tr></table>')
    thumb = f'<media:thumbnail url="{escape(thumbnail)}"/>' if thumbnail else ''
    return (f'<entry><id>t3_{post_id}</id><updated>2026-01-01T00:00:00Z</updated>'
            f'<content type="html">{escape(html, quote=False)}</content>'
            f'<link href="{permalink}"/>{thumb}<title>t</title></entry>')


PREVIEW_640 = 'https://preview.redd.it/abc123.jpeg?width=640&crop=smart&auto=webp&s=sig1'
PREVIEW_140 = 'https://preview.redd.it/abc123.jpeg?width=140&height=105&crop=140:105,smart&s=sig2'


def test_full_size_images_and_card_previews():
    rows = worker.parse_posts(reddit_feed(
        reddit_entry('img', PREVIEW_640, 'https://i.redd.it/abc123.jpeg', PREVIEW_140),
        reddit_entry('gal', 'https://preview.redd.it/gal111.jpg?width=140&height=140&s=x',
                     'https://www.reddit.com/gallery/gal'),
        reddit_entry('slug', 'https://preview.redd.it/my-cat-v0-xyz789.png?width=320&s=y',
                     'https://www.reddit.com/gallery/slug'),
        reddit_entry('ext', 'https://external-preview.redd.it/AbC-dE.jpg?width=640&s=z',
                     'https://example.com/story', 'https://external-preview.redd.it/AbC-dE.jpg?width=320&s=w'),
    ), 'pics')
    found = {row[0]: row[3:6] for row in rows}
    # The full-size original, not the first preview the feed lists; the 640px preview for cards.
    assert found['t3_img'] == ('direct_image', 'https://i.redd.it/abc123.jpeg', PREVIEW_640)
    # Only a 140px preview: show the original it was resized from, never the tiny copy.
    assert found['t3_gal'] == ('direct_image', 'https://i.redd.it/gal111.jpg', None)
    assert found['t3_slug'][1] == 'https://i.redd.it/xyz789.png'
    # Non-Reddit images have no derivable original; the largest preview wins.
    assert found['t3_ext'][1:] == ('https://external-preview.redd.it/AbC-dE.jpg?width=640&s=z',) * 2


def test_mp4_hosted_video_and_text_posts():
    poster = 'https://external-preview.redd.it/Vid.png?width=640&format=pjpg&s=v'
    rows = worker.parse_posts(reddit_feed(
        reddit_entry('mp4', 'https://external-preview.redd.it/C.jpg?width=320&s=c', 'https://i.imgur.com/clip.mp4'),
        reddit_entry('vid', poster, 'https://v.redd.it/vid123'),
        reddit_entry('txt'),
    ), 'pics')
    found = {row[0]: row[3:6] for row in rows}
    assert found['t3_mp4'] == ('video_mp4', 'https://i.imgur.com/clip.mp4',
                               'https://external-preview.redd.it/C.jpg?width=320&s=c')
    assert found['t3_vid'] == ('third_party_embed', 'https://v.redd.it/vid123', poster)
    assert found['t3_txt'] == ('unknown', 'https://www.reddit.com/r/pics/comments/txt/t/', None)


def test_pick_image_edge_cases():
    assert worker.pick_image([]) == (None, None)
    assert worker.pick_image([None, 'http://i.redd.it/a.jpg', 'https://example.com/page']) == (None, None)
    assert worker.pick_image(['https://b.thumbs.redditmedia.com/t.jpg']) == ('https://b.thumbs.redditmedia.com/t.jpg',) * 2
    big = 'https://preview.redd.it/q.jpg?width=2160&s=a'
    assert worker.pick_image([big]) == ('https://i.redd.it/q.jpg', None)


def test_enrichment_scores_mp4_fallback_and_card_preview():
    sized = [{'url': f'https://preview.redd.it/p.jpg?width={w}&s={w}', 'width': w} for w in (640, 1080, 2160)]
    listing = {'data': {'children': [{'data': {
        'name': 't3_abc', 'score': 1234,
        'media': {'reddit_video': {'fallback_url': 'https://v.redd.it/abc/DASH_720.mp4?source=fallback'}},
        'preview': {'images': [{'source': {'url': 'https://preview.redd.it/p.jpg?s=src'}, 'resolutions': sized}]},
    }}]}}
    post = worker.enrich_posts(worker.parse_posts(ATOM, 'pics'), listing)[0]
    assert post[3] == 'video_mp4' and post[4].startswith('https://v.redd.it/abc/DASH_720.mp4')
    assert post[5] == 'https://preview.redd.it/p.jpg?width=1080&s=1080'
    assert post[8] == 1234
    assert 'COALESCE(EXCLUDED.upvote_score' in worker.UPSERT


@pytest.mark.asyncio
async def test_upgrade_stored_images():
    pool = AsyncMock()
    pool.fetch.return_value = [
        {'db_primary_id': 't3_a', 'clean_delivery_url': PREVIEW_640, 'thumbnail_url': PREVIEW_640},
        {'db_primary_id': 't3_b', 'clean_delivery_url': PREVIEW_140, 'thumbnail_url': PREVIEW_140},
        {'db_primary_id': 't3_c', 'clean_delivery_url': 'https://i.redd.it/c.png', 'thumbnail_url': None},
    ]
    assert await worker.upgrade_stored_images(pool) == 2
    assert pool.executemany.await_args.args[1] == [
        ('t3_a', 'https://i.redd.it/abc123.jpeg', PREVIEW_640),
        ('t3_b', 'https://i.redd.it/abc123.jpeg', None)]


@pytest.mark.parametrize('name,expected', [
    ('gonewild', True), ('sex', True), ('gaybbc', True), ('incest_captions', True), ('femboys', True),
    ('sexyhair', True), ('EarthPorn', False), ('unixporn', False), ('cocktails', False),
    ('asexuality', False), ('sussex', False), ('landscapephotography', False), ('pics', False)])
def test_nsfw_names(monkeypatch, name, expected):
    monkeypatch.delenv('NSFW_SUBREDDITS', raising=False)
    monkeypatch.delenv('SFW_SUBREDDITS', raising=False)
    assert names.is_nsfw(name) is expected


def test_nsfw_overrides(monkeypatch):
    monkeypatch.setenv('NSFW_SUBREDDITS', 'r/Pics, art')
    monkeypatch.setenv('SFW_SUBREDDITS', 'sexyhair')
    assert names.is_nsfw('pics') and names.is_nsfw('art')
    assert not names.is_nsfw('sexyhair')


@pytest.mark.asyncio
async def test_18_plus_communities_need_age_confirmation(client, monkeypatch):
    monkeypatch.delenv('NSFW_SUBREDDITS', raising=False)
    main.app.state.pool.fetch.return_value = [dict(row(1), subreddit='gonewild')]
    async with client:
        for path in ('/api/v1/feed?subreddit=gonewild', '/gonewild'):
            gated = await client.get(path)
            assert gated.status_code == 403 and gated.json()['detail']['code'] == 'age_gate'
        allowed = await client.get('/api/v1/feed', params={'subreddit': 'gonewild', 'nsfw': 1})
        assert allowed.status_code == 200 and allowed.json()['posts'][0]['nsfw'] is True
        # "For you" leaves 18+ communities out unless the viewer opted in.
        await client.get('/api/v1/feed', params={'subreddit': 'all'})
        assert main.app.state.pool.fetch.call_args.args[5] == ['gonewild']
        await client.get('/api/v1/feed', params={'subreddit': 'all', 'nsfw': 'true'})
        assert main.app.state.pool.fetch.call_args.args[5] == []
        cache_keys = [call.args[0] for call in main.app.state.cache.set.call_args_list]
        assert 'feed:v3:all:all:0:0:25' in cache_keys and 'feed:v3:all:all:1:0:25' in cache_keys


@pytest.mark.asyncio
async def test_community_list_hides_18_plus_by_default(client):
    main.app.state.pool.fetch.return_value = [{'subreddit': 'aww', 'posts': 25, 'videos': 4, 'cover': None},
                                              {'subreddit': 'sex', 'posts': 25, 'videos': 0, 'cover': None}]
    async with client:
        shown = (await client.get('/api/v1/subreddits')).json()['subreddits']
        assert [c['subreddit'] for c in shown] == ['aww'] and shown[0]['nsfw'] is False
        everything = (await client.get('/api/v1/subreddits?nsfw=1')).json()['subreddits']
        assert [c['nsfw'] for c in everything] == [False, True]
        assert "'video_mp4'" in main.app.state.pool.fetch.call_args.args[0]


@pytest.mark.asyncio
async def test_mp4_is_video_and_unknown_score_is_null(client):
    main.app.state.pool.fetch.return_value = [dict(row(1), media_category='video_mp4',
                                                 clean_delivery_url='https://i.imgur.com/c.mp4')]
    async with client:
        post = (await client.get('/pics')).json()['posts'][0]
    assert post['mediaType'] == 'video' and post['upvotes'] is None and post['nsfw'] is False


@pytest.mark.asyncio
async def test_ui_files_are_revalidated(client):
    async with client:
        for path in ('/', '/assets/app.js'):
            response = await client.get(path)
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-cache'
        assert 'cache-control' not in (await client.get('/healthz')).headers


WEB_API_REPLY = {'state': 'done', 'results': [{
    'html': ATOM.decode(), 'metadata': {'statuses': {'http_request': {'code': 200, 'tag': 'HTTP_OK'}}}}]}


async def fake_web_api(status, payload):
    """A local stand-in for webapi.oxylabs.io that records the request it receives."""
    import asyncio
    seen = {}

    async def reply(reader, writer):
        lines = (await reader.readuntil(b'\r\n\r\n')).decode().split('\r\n')
        header = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip() for line in lines[1:] if ':' in line}
        seen['auth'] = header.get('authorization')
        seen['body'] = json.loads(await reader.readexactly(int(header['content-length'])))
        body = json.dumps(payload).encode()
        writer.write(f'HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n'
                     f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(reply, '127.0.0.1', 0)
    api = webapi.WebApi('k' * 20, endpoint=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/scrape')
    return server, api, seen


@pytest.mark.asyncio
async def test_web_api_fetches_the_feed_with_the_key():
    server, api, seen = await fake_web_api(200, WEB_API_REPLY)
    async with server, worker.session_factory() as session:
        body, _ = await worker.fetch_once(session, 'https://www.reddit.com/r/pics/.rss', api)
    assert seen['auth'] == 'Bearer ' + 'k' * 20
    assert seen['body'] == {'url': 'https://www.reddit.com/r/pics/.rss', 'output': ['html']}
    assert len(worker.parse_posts(body, 'pics')) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status,payload,expected', [
    (200, {'results': [{'html': 'x', 'metadata': {'statuses': {'http_request': {'code': 404}}}}]},
     ('http_response', 404, False, False)),
    (200, {'results': [{'html': '', 'metadata': {}}]}, ('webapi_empty', None, False, False)),
    (401, {'title': 'Unauthorized'}, ('webapi_auth', 401, False, True)),
    (429, {'title': 'QUOTA_EXCEEDED'}, ('webapi_quota', 429, False, True)),
    (429, {'title': 'TOO_MANY_REQUESTS'}, ('webapi_rate_limit', 429, True, True)),
    (500, {'state': 'faulted', 'title': 'REQUEST_FAILED_AFTER_TOO_MANY_RETRIES'},
     ('webapi_faulted', 500, True, False)),
    (400, {'errors': []}, ('webapi_request', 400, False, False)),
])
async def test_web_api_failures(status, payload, expected):
    server, api, _ = await fake_web_api(status, payload)
    async with server, worker.session_factory() as session:
        with pytest.raises(worker.FetchFailure) as caught:
            await worker.fetch_once(session, 'https://www.reddit.com/r/pics/.rss', api)
    failure = caught.value
    assert (failure.stage, failure.status, failure.retryable, worker.stops_worker(failure)) == expected
    assert 'k' * 20 not in str(failure)


def test_web_api_key_from_environment(monkeypatch):
    monkeypatch.delenv('OXYLABS_WEB_API_KEY', raising=False)
    assert webapi.load_web_api() is None
    monkeypatch.setenv('OXYLABS_WEB_API_KEY', ' abcDEF123456789xyz0 ')
    api = webapi.load_web_api()
    assert api.key == 'abcDEF123456789xyz0' and 'abcDEF' not in repr(api)
    monkeypatch.setenv('OXYLABS_WEB_API_KEY', 'not a key, has spaces')
    with pytest.raises(ValueError) as caught:
        webapi.load_web_api()
    assert 'spaces' not in str(caught.value)


@pytest.mark.asyncio
async def test_web_api_diagnose_is_one_request(monkeypatch, capsys):
    fetch = AsyncMock(return_value=(ATOM, ''))
    monkeypatch.setattr(worker, 'fetch_once', fetch)
    assert await worker.diagnose(None, webapi.WebApi('k' * 20), 'pics') == 0
    assert [call.args[1] for call in fetch.await_args_list] == ['https://www.reddit.com/r/pics/.rss']
    assert json.loads(capsys.readouterr().out)['posts'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('route,media_route', [
    (webapi.WebApi('k' * 20), None),  # billed per request, so playlists go direct
    (proxy.ProxyEntry(url='http://127.0.0.1:9'), proxy.ProxyEntry(url='http://127.0.0.1:9')),
])
async def test_video_playlist_checks_and_route(monkeypatch, route, media_route):
    feed = reddit_feed(reddit_entry('vid', 'https://external-preview.redd.it/V.png?width=640&s=v',
                                    'https://v.redd.it/vid123'))
    monkeypatch.setattr(worker, 'fetch_with_retries', AsyncMock(return_value=(feed, '')))
    fetch = AsyncMock(return_value=(b'#EXTM3U\n', ''))
    monkeypatch.setattr(worker, 'fetch_once', fetch)
    conn = MagicMock(executemany=AsyncMock())
    pool = MagicMock(fetch=AsyncMock(return_value=[]))
    pool.acquire.return_value.__aenter__.return_value = conn

    async def no_keys(**_):
        for key in ():
            yield key

    assert await worker.ingest(None, route, pool, MagicMock(scan_iter=no_keys), 'pics') == 1
    assert fetch.await_args.args[1:] == ('https://v.redd.it/vid123/HLSPlaylist.m3u8', media_route)
    assert conn.executemany.await_args.args[1][0][3:5] == ('video_hls', 'https://v.redd.it/vid123/HLSPlaylist.m3u8')
