"""Shared initialization matching the supplied PostgreSQL schema."""
import os

import asyncpg

SCHEMA = """
DO $$ BEGIN
    CREATE TYPE media_type AS ENUM ('direct_image', 'video_hls', 'video_mp4', 'third_party_embed', 'unknown');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
-- Databases created before direct MP4s had their own category.
ALTER TYPE media_type ADD VALUE IF NOT EXISTS 'video_mp4';
CREATE TABLE IF NOT EXISTS reddit_media_posts (
    db_primary_id VARCHAR(32) PRIMARY KEY,
    title TEXT NOT NULL,
    subreddit VARCHAR(64) NOT NULL,
    upvote_score INT,  -- NULL until a score is known; RSS feeds don't carry one
    media_category media_type DEFAULT 'unknown',
    clean_delivery_url TEXT NOT NULL,
    thumbnail_url TEXT,
    source_origin_url TEXT NOT NULL,
    created_utc TIMESTAMPTZ NOT NULL,
    discovered_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS reddit_media_posts_feed_idx
    ON reddit_media_posts (subreddit, created_utc DESC, db_primary_id DESC);
-- The combined "all" feed sorts across every subreddit; without this it scans and sorts the whole table.
CREATE INDEX IF NOT EXISTS reddit_media_posts_recent_idx
    ON reddit_media_posts (created_utc DESC, db_primary_id DESC);
CREATE TABLE IF NOT EXISTS subreddit_requests (
    subreddit VARCHAR(64) PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',
    post_count INT NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Earlier versions defaulted scores to 0, which read as a real score. Runs once.
DO $$ BEGIN
    IF (SELECT column_default FROM information_schema.columns WHERE table_schema = current_schema()
        AND table_name = 'reddit_media_posts' AND column_name = 'upvote_score') = '0' THEN
        ALTER TABLE reddit_media_posts ALTER COLUMN upvote_score DROP DEFAULT;
        UPDATE reddit_media_posts SET upvote_score = NULL WHERE upvote_score = 0;
    END IF;
END $$;
"""
# A new enum value can only be used once the transaction that added it has committed.
AFTER_SCHEMA = r"""
UPDATE reddit_media_posts SET media_category = 'video_mp4'
WHERE media_category = 'video_hls' AND clean_delivery_url ~* '\.mp4($|\?)';
"""


async def open_pool():
    pool = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=5,
                                     command_timeout=15)
    # API and worker can start at the same time. Serialize their schema setup.
    try:
        async with pool.acquire() as conn:
            for statements in (SCHEMA, AFTER_SCHEMA):
                async with conn.transaction():
                    await conn.execute('SELECT pg_advisory_xact_lock(73491260)')
                    await conn.execute(statements)
    except Exception:
        await pool.close()
        raise
    return pool
