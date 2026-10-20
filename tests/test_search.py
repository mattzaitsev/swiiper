from unittest.mock import AsyncMock

import httpx
import pytest

from app import main
from app.directory import normalize_results, SubredditDirectory


def test_directory_names_counts_and_ranking():
    rows = normalize_results({'results': [
        {'name': 'Food', 'displayName': 'Food', 'subscribers': 300},
        {'name': 'football', 'subscribers': 100},
        {'name': 'Seafood', 'subscribers': 1000},
        {'name': 'food', 'subscribers': 999},
        {'name': 'cats', 'subscribers': 1000},
        {'name': '<script>', 'subscribers': 1000},
        {'name': 'foodies', 'subscribers': True},
    ]}, 'foo')
    assert [row['subreddit'] for row in rows] == ['food', 'football', 'foodies', 'seafood']
    assert rows[0]['members'] == 300
    assert rows[2]['members'] is None
    assert normalize_results({'results': [{'name': 'f', 'subscribers': 0}]}, 'f')[0]['members'] == 0


@pytest.mark.asyncio
async def test_search_route_and_invalid_queries():
    main.app.state.directory = AsyncMock()
    main.app.state.directory.search.return_value = [{'subreddit': 'food', 'members': 300}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        result = await client.get('/api/v1/subreddits/suggest', params={'q': '/r/Foo'})
        assert result.status_code == 200
        assert result.json()['source'] == 'reddclips'
        assert result.json()['subreddits'][0]['members'] == 300
        main.app.state.directory.search.assert_awaited_once_with('foo')
        assert (await client.get('/api/v1/subreddits/suggest?q=')).json()['subreddits'] == []
        for query in ('<script>', 'foo bar', 'x' * 51):
            assert (await client.get('/api/v1/subreddits/suggest', params={'q': query})).status_code == 400


@pytest.mark.asyncio
async def test_search_outage_uses_loaded_names_without_inventing_counts():
    main.app.state.directory = AsyncMock()
    main.app.state.directory.search.side_effect = TimeoutError()
    main.app.state.pool = AsyncMock()
    main.app.state.pool.fetch.return_value = [{'subreddit': 'food'}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        response = await client.get('/api/v1/subreddits/suggest?q=f')
        assert response.status_code == 200
        assert response.json()['available'] is False
        assert response.json()['subreddits'] == [{'subreddit': 'food', 'displayName': 'food', 'members': None}]


@pytest.mark.asyncio
async def test_cached_query_avoids_network_and_expired_entry_does_not(monkeypatch):
    session = AsyncMock()
    service = SubredditDirectory(session)
    monkeypatch.setattr('app.directory.time.monotonic', lambda: 10)
    service.cache['f'] = (20, [{'subreddit': 'food', 'members': 300}])
    assert (await service.search('f'))[0]['members'] == 300
    session.get.assert_not_called()
    service.retry_at = 40
    monkeypatch.setattr('app.directory.time.monotonic', lambda: 30)
    with pytest.raises(ValueError):
        await service.search('f')
