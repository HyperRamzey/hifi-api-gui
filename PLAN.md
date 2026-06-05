# Fix: 429 Too Many Requests on API server

## Context

The user is experiencing 429 "Too Many Requests" errors when rapidly clicking on tracks in the search results. Each click triggers `_on_result_clicked` which makes two sequential HTTP requests to the local hifi-api server:
1. `GET /cover/?id=<track_id>` — fetch the cover URL
2. `GET <cover_url>` — fetch the cover image bytes

Rapid clicking causes many requests in quick succession, saturating the local API server and triggering its rate limiter. The user wants to ensure only 1 thread/connection is used at a time to prevent this.

## Approach

Add a threading lock to the `Api` class so all HTTP requests go through one at a time. This serializes requests across all API methods (ping, search_tracks, get_manifest, get_cover_url, fetch_cover_bytes), preventing the local server from being saturated by rapid clicks.

## Files to modify

- `gui_downloader.py` — Add a `threading.Lock()` to the `Api` class, wrap all `session.get()` calls with `with self._lock:`

## Steps

- [x] Add `self._lock = threading.Lock()` to `Api.__init__`
- [x] Wrap `ping()` session.get() with `with self._lock:`
- [x] Wrap `search_tracks()` session.get() with `with self._lock:`
- [x] Wrap `get_manifest()` session.get() calls with `with self._lock:`
- [x] Wrap `get_cover_url()` session.get() with `with self._lock:`
- [x] Wrap `fetch_cover_bytes()` session.get() with `with self._lock:`
- [x] Wrap `DownloadWorker._download_file()` session.get() calls with `with self.api._lock:`

## Verification

- Open the app, search for an artist, and rapidly click on many tracks
- No more 429 errors should appear
- Cover art should still load correctly for each clicked track
