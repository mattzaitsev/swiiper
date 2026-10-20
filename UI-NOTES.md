# Complete UI

Open `http://localhost:8000/`.

- **Home:** a continuous, one-item viewport. Wheel, vertical swipe, arrow keys, and previous/next buttons switch the active post. More metadata loads automatically at page boundaries. Only the current photo or video is mounted; leaving a video destroys its HLS player, clears its source, and removes it. Lightweight post metadata remains in memory so Back can reload earlier media, without a growing scroll document. Videos autoplay muted with native playback/audio controls. There is no Home hero/banner.
- **Search:** both the header and Explore fields have an attached live dropdown from the first character, matching-text highlights, directory-reported member counts, click selection, arrow-key/Enter selection, Escape/outside-click dismissal, and immediate narrowing while new results load.
- **Explore:** subreddit search at the top, quick picks, available community counts, and community cards. The previous large introductory headline is removed. Search matches loaded/suggested communities; an exact public subreddit name can also be opened and loaded on demand.
- **Community pages:** paginated media grids with a refresh action. New community requests run in the background worker with visible queued/loading/ready/error states.
- **Saved:** bookmarks persist in this browser using localStorage. No account is required.
- **Media viewer:** community/saved grid cards open an immersive viewer with native controls, previous/next navigation, arrow-key navigation, Escape to close, and touch gestures for images. Home media plays inline.
- **Responsive navigation:** desktop sidebar and mobile bottom navigation.

## Video sources

The worker recognizes Reddit-hosted video references in RSS. It requests the corresponding public HLS manifest and only marks the post playable when the response is a valid `#EXTM3U` playlist. Verified manifests are reused on later ingestion runs. The self-hosted hls.js **1.7.3** bundle provides HLS playback; native HLS is the fallback when hls.js is not supported. Native MP4 playback and the existing local MP4 range route are retained. The hls.js license is included under `web/vendor/LICENSE`.

Reddit's JSON listing endpoint returned 403 in this environment. JSON enrichment is disabled by default (`REDDIT_JSON_ENRICHMENT=0`) and is not needed for the verified RSS video path. Unavailable or unsupported media retains an original-post link.

## API additions

- `GET /api/v1/feed?subreddit=all&kind=all|image|video`: combined Home feed and server-side media filtering.
- `GET /api/v1/subreddits?q=...`: available communities, post/video counts and covers.
- `POST /api/v1/subreddits/{name}/refresh`: queue a community load. Duplicate/recent requests are coalesced; the queue is bounded.
- `GET /api/v1/subreddits/{name}/status`: progress and useful failure messages.

The additive `subreddit_requests` table stores queue state. Existing media columns and data are preserved. The worker checks pending requests between its normal ingestion cycles. Newly discovered communities are retained in the database; the Refresh action updates them on demand.

## Verification

- **24 automated backend tests passed**, including combined feeds, video filtering, community search/status validation and metadata enrichment, plus the original cursor/range/proxy regressions.
- Browser: an actual Reddit HLS video played with its time counter advancing; the updated Home played it inline.
- Browser: moved through post 22 across the 18-item API page boundary with exactly one rendered card at every step; previous/next and mouse-wheel navigation passed. Video next/back retained exactly one mounted video and restarted playback on returning.
- Browser: search for `landscapephotography`, request loading, and render the resulting **25 posts** succeeded.
- Browser: saving a video, reloading Saved, and removing that saved item worked.
- Browser: Explore's large intro is absent and the search field is at the top.
- JavaScript syntax validation and Docker image builds passed.

Fonts load from Google Fonts with local sans-serif fallbacks. Media comes from the source CDNs; the application does not download or rehost remote video bytes.

## Feed navigation references

Reviewed the public fullscreen-viewer logic on [Reddclips](https://reddclips.com/) and [Scrolller](https://scrolller.com/): both maintain an active post index and use throttled wheel/swipe navigation. Home uses that interaction pattern with explicit media teardown. Community discovery grids remain separate. Browser HTTP caching is managed by the browser; this change releases offscreen media players and DOM elements, not browser cache entries.


## Subreddit autocomplete

`GET /api/v1/subreddits/suggest?q=f` returns up to 12 matching community names and member totals. The search adapter uses the public `https://api.reddclips.com/search` endpoint inspected in the reference site's frontend. Reddit's own autocomplete returned 403 both directly and through the configured proxy during validation. No proxy credentials are passed to the directory. This is an external service dependency, not a complete local subreddit index; counts are directory-reported and may lag Reddit.

The browser debounces input for 180 ms, cancels obsolete requests, ignores stale responses, and keeps a bounded five-minute cache. The backend also caches successful queries for five minutes, caps concurrent connections and response size, and briefly backs off after upstream failures. If the provider is unavailable, loaded local communities remain searchable and missing counts are labelled unavailable rather than replaced with post totals.

Validation: all 24 backend tests passed. Browser checks covered first-letter results, narrowing from `f` to `foo`, rapid typing through `g`/`gam`/`gaming`, real member-count display, mouse and keyboard selection, Escape dismissal, and successful community navigation from both search fields. JavaScript syntax and Docker build passed.
