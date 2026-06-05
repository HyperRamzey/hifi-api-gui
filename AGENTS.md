# hifiT Agent Guidelines

## What this is
PyQt6 GUI music downloader wrapping a FastAPI reverse proxy for Tidal's API. Two components:
- **main.py** — FastAPI server (`0.0.0.0:8000`) that proxies Tidal API calls, manages OAuth2 tokens, handles playback manifests
- **gui_downloader.py** — Single-file PyQt6 desktop client that searches, downloads, and queues Tidal tracks

## Key paths
- `main.py` — FastAPI server entry point (uvicorn, port 8000)
- `gui_downloader.py` — Single-file PyQt6 application (~1900 lines)
- `pyproject.toml` — ruff + pylint config
- `requirements.txt` — dependencies (pigar-generated)
- `tidal_auth/venv/` — virtual environment (Python 3.13)
- `.env` / `.env.example` — Tidal OAuth tokens + config
- `gui_config.json` — user config (output_dir, quality)
- `tests/test_gui_downloader.py` — 70 tests across 9 classes

## Running
```bash
# Start API server
tidal_auth/venv/Scripts/python.exe main.py

# Or start via GUI (auto-starts server)
tidal_auth/venv/Scripts/python.exe gui_downloader.py

# Run tests
tidal_auth/venv/Scripts/python.exe -m pytest tests/ -v

# Lint
tidal_auth/venv/Scripts/python.exe -m ruff check .
tidal_auth/venv/Scripts/python.exe -m pylint .
```

## Architecture

### API endpoints (localhost:8000)
| Endpoint | Params | Returns |
|---|---|---|
| `GET /` | — | `{"version": "2.10"}` |
| `GET /search/` | `s`, `limit`, `offset`, `a`, `al` | `{"items": [...]}` |
| `GET /track/` | `id`, `quality` | playback manifest |
| `GET /trackManifests/` | `id`, `formats` | multi-format manifest |
| `GET /cover/` | `id` | `{"covers": [{"1280": url, ...}]}` |
| `GET /info/` | `id` | track metadata |

### Manifest decoding
`decode_manifest(manifest_b64, mime_type)` always expects **base64-encoded** input:
- `application/vnd.tidal.bts` → JSON parse → `encryptionType` field. `NONE` = downloadable, `WIDEVINE` = DRM-locked
- `application/dash+xml` → XML parse → check for `cenc:pssh` / `widevine` markers → parse `SegmentTemplate` for URLs
- Any other mime type → `([], "UNKNOWN")`

### DASH download strategy
Tidal DASH tracks are served as init segment (ftyp+moov header) + ~46 media segments (bare moof+mdat fragments).
Each fragment is individually unplayable. The correct approach in `DownloadWorker._download_file()`:
1. Download init segment → write to temp file
2. Download all media segments → byte-append to same temp file
3. Single `ffmpeg -i combined.mp4 -c copy -movflags +faststart output.m4a` remux
4. Detect extension from codec: `flac` → `.flac`, otherwise `.m4a`

### GUI thread model
- **QThread worker pattern**: Worker inherits `QObject` (NOT `QThread`), moved via `moveToThread()`, communicates via `pyqtSignal`
- Worker `run()` must call `self.thread().quit()` in a `finally` block so the QThread exits its event loop after completion
- Thread-finished cleanup uses `cleanup_and_dispatch()` helper that removes the thread from `active_threads` AND calls `_dispatch_next()` to pull the next queued item
- `self.search_thread` / `self.search_worker` are instance variables — **never create threads as local variables** (GC will collect them while running)
- `QTimer.singleShot(0, callback)` for thread-safe UI updates from background threads
- `closeEvent` cleans up: search thread quit+wait, server subprocess terminate+wait

### Search results pagination
- **Page-by-page navigation**: `< Page N >` buttons; each page shows only its own items (not cumulative)
- **Dedup**: `_seen_track_ids: set[int]` tracks all displayed IDs; duplicates skipped on load-more
- **Load-more flow**: `_load_page()` sets `_is_load_more=True` → worker fetches → `_populate_results_safe()` appends → `_swap_to_new_page()` rebuilds tree from cached page data only
- **Sort**: Header click cycles relevance → asc → desc → relevance; `_update_sort_indicators()` sets `header.setSortIndicator()` arrows; `_results_sort_column` tracks active column

### Download queue workflow
- **Search results** → 5 columns: Track, Artist, Album, Duration, Quality
- **Search buttons**: "Add to Queue" (queues selected, no download) + "Download Selected" (queues + starts download immediately)
- **Queue buttons**: "Download All" (sequential, 1 thread) + "Stop" (halts active downloads) + "Clear Completed"
- **Remove** → right-click context menu on queue items
- `audioQuality` from search API response displayed in Quality column
- "Download All" temporarily sets `download_mgr.max_concurrent = 1` during download
- **Cover art embedding**: cover fetched during manifest decode, embedded into output file via mutagen (FLAC: `mutagen.flac.Picture`, M4A: `mutagen.mp4.MP4["covr"]`)
- **DRM handling**: `decode_manifest()` detects Widevine via `encryptionType` field or `cenc:pssh`/`widevine` XML markers. DRM-locked tracks raise `RuntimeError` — status is set to `drm_locked` before the raise, caught by `run()`'s except block which emits `task_failed`. Status `drm_locked` is excluded from the `except` block's status check so it doesn't get overwritten to `failed`.
- **Stop button**: enabled when tasks have status "manifest"/"downloading", calls `download_mgr.stop_all()` which sets `stopped=True`, terminates threads; tasks reset to "Queued" status

### Server lifecycle (auto-start)
- GUI checks `api.ping()` on init — if API already running, skip start
- Otherwise starts `tidal_auth/venv/Scripts/python.exe main.py` via `subprocess.Popen`
- Polls `/` endpoint up to 15s for readiness
- Server stdout piped to `self.server_log` (QTextEdit) via background reader thread
- `closeEvent` terminates subprocess (5s grace, then kill)

### API request serialization
- **Thread-safe**: All `Api` class `session.get()` calls are wrapped with `with self._lock:` to serialize requests one at a time
- This prevents 429 rate-limit errors when the user rapidly clicks on tracks (cover art fetches)
- The lock is also used by `DownloadWorker._download_file()` for DASH segment downloads via `with self.api._lock:`

## Linting conventions
- **ruff**: line-length=100, target=py313, selects E/F/UP/B/SIM/I
- **pylint**: py-version=3.13, many warnings disabled (see pyproject.toml)
- Pre-existing issues from main.py/tests are in ignore lists — don't fix them unless touching that code
- `ruff check --fix` + `ruff format` before committing

## Testing
- `pytest` + `pytest-qt` (PyQt6 testing) + `pytest-httpserver` (mock API)
- Tests in `tests/test_gui_downloader.py` — 70 tests across 9 classes
- `pyproject.toml` has `addopts = "-v --tb=short --color=yes"` — verbose by default
- **Never monkeypatch PyQt6 classes in `pytest_configure` or `pytest_runtest_setup`** — causes Qt event loop hangs
- `show_error_dialog()` on MainWindow wraps QMessageBox — override/monkeypatch in tests instead of calling QMessageBox.critical directly
- `_kill_server_on_port_8000` autouse fixture cleans up server processes after each test
- `http_server` fixture on port 18932 for API mocking
- Use `_make_api(http_server)` helper to override `API_BASE` for Api tests
- For GUI tests that create `MainWindow()`, call `window.show()` before `qtbot.addWidget(window)`

## Code patterns to follow
- Use `logging` module with `log = logging.getLogger(__name__)` pattern
- `subprocess.Popen` with `text=True` for string output (not bytes)
- Daemon threads for background readers (`_read_server_output`)
- Dataclass for `DownloadTask` with `field(default_factory=...)` for mutable defaults
- Type annotations: `X | None` not `Optional[X]`, `dict` not `Dict`
- Always wrap API calls with `with self._lock:` for thread safety

## Gotchas
- **main.py must `import sys`** — `_resolve_file()` uses `sys.frozen` / `sys.executable`. Missing import crashes server on startup.
- **Config file location** — `gui_config.json` saved next to script (dev) or exe (frozen). Never in AppData or `_internal/`.
- **Quality fallback chain** — `FLAC_HIRES → FLAC → AACLC → HEAACV1`. GUI auto-downgrades if preferred quality unavailable.
- **QProgressBar in PyQt6 6.11** starts in indeterminate mode — always call `setValue(0)` after construction
- **mutagen** required for cover art embedding (FLAC: `mutagen.flac.Picture`, M4A: `mutagen.mp4.MP4["covr"]`) — already in `requirements.txt`
- **QThread lambda trap**: `thread.started.connect(lambda: worker.run(task))` executes the lambda in the **main thread**, blocking the GUI. Fix: set `worker.task = task` as an attribute, then `thread.started.connect(worker.run)` — PyQt dispatches direct method references to the worker's thread context
- **Tree header sorting**: `sectionClicked` signal requires `setSectionsClickable(True)` to fire; sort indicator arrows require `setSortIndicatorShown(True)` to display — both must be called explicitly
- **SearchWorker must be triggered via signals**: never call `search_worker.run()` directly from the main thread even via `QTimer.singleShot(0, ...)`. Define a `pyqtSignal` on MainWindow, connect it to `search_worker.run`, and `.emit()` from the singleShot callback
- **Git dubious ownership**: On Windows, if git complains about "dubious ownership", run `git config --global --add safe.directory <repo_path>`
- **GUI error handling**: Always wrap synchronous API calls in `_on_result_clicked` with try/except for `HTTPError` and generic `Exception` — show QMessageBox warning instead of crashing the GUI

## Security
- **Never commit tokens**: `token.json`, `.env`, `.env.*`, `gui_config.json` are all in `.gitignore`
- Remote: `https://HyperRamzey:ghp_***@github.com/HyperRamzey/hifi-api-gui.git` (GitHub token in remote URL — use with caution)
