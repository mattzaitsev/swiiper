from unittest.mock import AsyncMock

import httpx
import pytest

from app import main
from app.directory import suggest


def search_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')


def test_suggestions_rank_exact_prefix_loaded_then_popular():
    rows = suggest('foo', {'seafood': 40, 'foodies': 3, 'food': 9})
    assert [row['subreddit'] for row in rows] == ['food', 'foodies', 'foodporn', 'seafood']
    assert rows[0] == {'subreddit': 'food', 'displayName': 'food', 'posts': 9, 'popular': True, 'nsfw': False}
    assert rows[2]['displayName'] == 'FoodPorn' and rows[2]['posts'] is None
    assert len(suggest('a', {})) == 12


def test_suggestions_hide_18_plus_unless_asked():
    loaded = {'sex': 5, 'sexyhair': 1}
    assert suggest('sex', loaded) == []
    assert {row['subreddit'] for row in suggest('sex', loaded, include_nsfw=True)} == {'sex', 'sexyhair'}
    assert all(row['nsfw'] for row in suggest('sex', loaded, include_nsfw=True))


@pytest.mark.asyncio
async def test_search_route_and_invalid_queries():
    main.app.state.pool = AsyncMock()
    main.app.state.pool.fetch.return_value = [{'subreddit': 'awwducational', 'posts': 7}]
    async with search_client() as client:
        result = await client.get('/api/v1/subreddits/suggest', params={'q': '/r/Aww'})
        assert result.status_code == 200
        body = result.json()
        assert body['source'] == 'local' and body['available'] is True
        assert [row['subreddit'] for row in body['subreddits']][:2] == ['aww', 'awwducational']
        assert body['subreddits'][1]['posts'] == 7
        assert main.app.state.pool.fetch.call_args.args[1] == 'aww'
        assert (await client.get('/api/v1/subreddits/suggest?q=')).json()['subreddits'] == []
        for query in ('<script>', 'foo bar', 'x' * 51):
            assert (await client.get('/api/v1/subreddits/suggest', params={'q': query})).status_code == 400


@pytest.mark.asyncio
async def test_search_without_database_still_suggests_popular_names():
    main.app.state.pool = AsyncMock()
    main.app.state.pool.fetch.side_effect = OSError()
    async with search_client() as client:
        response = await client.get('/api/v1/subreddits/suggest?q=pic')
        assert response.status_code == 200
        assert response.json()['available'] is False
        assert response.json()['subreddits'][0]['subreddit'] == 'pics'
        assert response.json()['subreddits'][0]['posts'] is None
