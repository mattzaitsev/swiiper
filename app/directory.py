"""Small, cached adapter for the public subreddit directory used by Reddclips."""
import asyncio
from collections import OrderedDict
import re
import time

import aiohttp

DIRECTORY_URL = 'https://api.reddclips.com/search'


def normalize_results(data, query):
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise ValueError('Invalid directory response')
    found = {}
    for item in data['results']:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,50}', name):
            continue
        name = name.lower()
        if query not in name or name in found:
            continue
        count = item.get('subscribers')
        display = item.get('displayName')
        found[name] = {'subreddit': name,
                       'displayName': display if isinstance(display, str) and display.lower() == name else name,
                       'members': count if type(count) is int and count >= 0 else None}
    return sorted(found.values(), key=lambda r: (
        r['subreddit'] != query, not r['subreddit'].startswith(query),
        -(r['members'] or 0), r['subreddit']))[:12]


class SubredditDirectory:
    def __init__(self, session):
        self.session = session
        self.cache = OrderedDict()
        self.slots = asyncio.Semaphore(4)
        self.retry_at = 0

    async def search(self, query):
        now = time.monotonic()
        cached = self.cache.get(query)
        if cached and cached[0] > now:
            self.cache.move_to_end(query)
            return cached[1]
        if now < self.retry_at:
            raise ValueError('Directory temporarily unavailable')
        try:
            async with asyncio.timeout(7):
                async with self.slots:
                    async with self.session.get(DIRECTORY_URL,
                            params={'q': query, 'limit': 50}, allow_redirects=False) as response:
                        if response.status != 200:
                            raise ValueError('Directory temporarily unavailable')
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(16384):
                            body.extend(chunk)
                            if len(body) > 512_000:
                                raise ValueError('Directory response too large')
                        import json
                        rows = normalize_results(json.loads(body), query)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            self.retry_at = time.monotonic() + 15
            raise
        self.cache[query] = (time.monotonic() + 300, rows)
        self.cache.move_to_end(query)
        while len(self.cache) > 256:
            self.cache.popitem(last=False)
        return rows
