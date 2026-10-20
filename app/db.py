"""Shared initialization matching the supplied PostgreSQL schema."""
import os

import asyncpg

SCHEMA = """
DO $$ BEGIN
    CREATE TYPE media_type AS ENUM ('direct_image', 'video_hls', 'third_party_embed', 'unknown');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE TABLE IF NOT EXISTS reddit_media_posts (
    db_primary_id VARCHAR(32) PRIMARY KEY,
    title TEXT NOT NULL,
    subreddit VARCHAR(64) NOT NULL,
    upvote_score INT DEFAULT 0,
    media_category media_type DEFAULT 'unknown',
    clean_delivery_url TEXT NOT NULL,
    thumbnail_url TEXT,
    source_origin_url TEXT NOT NULL,
    created_utc TIMESTAMPTZ NOT NULL,
    discovered_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS reddit_media_posts_feed_idx
    ON reddit_media_posts (subreddit, created_utc DESC, db_primary_id DESC);
CREATE TABLE IF NOT EXISTS subreddit_requests (
    subreddit VARCHAR(64) PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',
    post_count INT NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def open_pool():
    pool = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=5,
                                     command_timeout=15)
    # API and worker can start at the same time. Serialize their schema setup.
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(73491260)')
                await conn.execute(SCHEMA)
    except Exception:
        await pool.close()
        raise
    return pool
