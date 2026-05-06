"""hifiT Downloader - PyQt6 GUI for hifi-api Tidal music downloader."""

import base64
import json
import logging
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import requests
from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gui_downloader")

API_BASE = "http://127.0.0.1:8000"

QUALITY_OPTIONS = [
    ("Hi-Res FLAC (FLAC_HIRES)", "FLAC_HIRES"),
    ("FLAC (LOSSLESS)", "FLAC"),
    ("AAC 256kbps (AACLC)", "AACLC"),
    ("AAC 96kbps (HEAACV1)", "HEAACV1"),
]


def _is_frozen() -> bool:
    """Return True when running as a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def _runtime_dir() -> Path:
    """Directory containing the executable (or script dir in dev mode).

    In PyInstaller onedir mode, bundled data files land in _internal/
    next to the exe, so we check there first.
    """
    if _is_frozen():
        exe_dir = Path(sys.executable).parent
        internal = exe_dir / "_internal"
        if internal.is_dir():
            return internal
        return exe_dir
    return Path(__file__).parent


def _writable_dir() -> Path:
    """Writable directory for user files (token.json, config, proxies.txt).

    In frozen mode, this is next to the exe (not inside _internal which is
    read-only).  In dev mode, it's the script directory.
    """
    if _is_frozen():
        return Path(sys.executable).parent
    return Path(__file__).parent


def resource_path(relative: str) -> Path:
    """Resolve a path that works in both dev and frozen modes.

    In dev mode the path is relative to the script directory.
    In frozen mode bundled data files land next to the exe, not inside
    the temporary _MEIPASS extraction folder — we keep them writable
    by resolving relative to the executable's parent directory.
    """
    if _is_frozen():
        return _runtime_dir() / relative
    return Path(__file__).parent / relative


# Config file: use platform-specific app data when frozen so it survives
# re-builds and stays writable.  In dev mode keep it next to the script.
def _config_file() -> Path:
    if _is_frozen():
        return _writable_dir() / "gui_config.json"
    return Path(__file__).parent / "gui_config.json"


CONFIG_FILE = _config_file()

# Paths for auto-starting the hifi-api server
_SCRIPT_DIR = _runtime_dir()
if _is_frozen():
    # When frozen the venv python is not available; use the bundled exe.
    _VENV_PYTHON = Path(sys.executable)
    _MAIN_PY = _runtime_dir() / "main.py"
else:
    _VENV_PYTHON = _SCRIPT_DIR / "tidal_auth" / "venv" / "Scripts" / "python.exe"
    _MAIN_PY = _SCRIPT_DIR / "main.py"


# ─── Api ────────────────────────────────────────────────────────────────────


class Api:
    """HTTP wrapper over the local hifi-api FastAPI server."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "hifiT-Downloader/1.0"})

    def ping(self) -> bool:
        try:
            r = self.session.get(f"{API_BASE}/", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def search_tracks(self, query: str, limit: int = 25, offset: int = 0) -> list[dict]:
        r = self.session.get(
            f"{API_BASE}/search/",
            params={"s": query, "limit": limit, "offset": offset},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("data", {}).get("items", [])

    def get_manifest(self, track_id: int, fmt: str = "FLAC") -> dict:
        """Fetch manifest for a track.

        Handles both /trackManifests/ (URI-based) and /track/ (base64 manifest)
        response formats. Returns a dict with 'manifest' (base64 or raw bytes),
        'manifestMimeType', and 'download_urls' keys.
        """
        r = self.session.get(
            f"{API_BASE}/trackManifests/",
            params={"id": str(track_id), "formats": fmt},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        inner = data.get("data", data)
        attrs = inner.get("attributes", inner) if isinstance(inner, dict) else {}

        manifest = ""
        mime_type = attrs.get("manifestMimeType", attrs.get("manifest_mime_type", ""))
        download_urls: list[str] = []

        if "uri" in attrs:
            # /trackManifests/ returns a URI — fetch the manifest content
            uri = attrs["uri"]
            try:
                manifest_resp = self.session.get(uri, timeout=15)
                manifest_resp.raise_for_status()
                manifest = manifest_resp.text
                if not mime_type:
                    mime_type = "application/dash+xml"
            except Exception:
                pass
        elif "manifest" in attrs:
            # /track/ returns base64 manifest directly
            manifest = attrs["manifest"]

        if not manifest and "manifest" in data:
            manifest = data["manifest"]
            if not mime_type:
                mime_type = data.get("manifestMimeType", "")

        return {
            "manifest": manifest,
            "manifestMimeType": mime_type,
            "download_urls": download_urls,
            "encryptionType": attrs.get("encryptionType", "NONE"),
        }

    def get_cover_url(self, track_id: int) -> str | None:
        r = self.session.get(f"{API_BASE}/cover/", params={"id": track_id}, timeout=10)
        r.raise_for_status()
        covers = r.json().get("covers", [])
        if covers:
            entry = covers[0]
            return entry.get("1280") or entry.get("640") or entry.get("80")
        return None

    def fetch_cover_bytes(self, url: str) -> bytes | None:
        try:
            r = self.session.get(url, timeout=10)
            r.raise_for_status()
            return r.content
        except Exception:
            return None


# ─── DownloadTask ─────────────────────────────────────────────────────────────


@dataclass
class DownloadTask:
    track_id: int
    title: str
    artist: str
    album: str
    duration: int
    status: str = "queued"
    progress: float = 0.0
    error: str = ""
    quality: str = "FLAC"
    cover_url: str = ""
    cover_bytes: bytes = b""
    download_urls: list[str] = field(default_factory=list, repr=False)
    filepath: str = ""
    audio_codec: str = ""
    _row_id: int = field(default=0, repr=False)


# ─── Manifest Decoding ────────────────────────────────────────────────────────


def decode_manifest(
    manifest_b64: str, mime_type: str
) -> tuple[list[str], str, str | None, str | None]:
    """Decode manifest (base64 or raw). Returns (urls, encryption_type, init_url, codec)."""
    # URI-fetched manifests are raw XML/JSON, not base64-encoded
    stripped = manifest_b64.strip()
    if stripped.startswith("<?xml") or stripped.startswith("<MPD") or stripped.startswith("{"):
        raw = manifest_b64
    else:
        try:
            raw = base64.b64decode(manifest_b64).decode("utf-8")
        except Exception:
            return ([], "UNKNOWN", None, None)

    if mime_type == "application/vnd.tidal.bts":
        try:
            data = json.loads(raw)
            enc = data.get("encryptionType", "NONE")
            if enc == "WIDEVINE":
                return ([], "WIDEVINE", None, None)
            return (data.get("urls", []), enc, None, None)
        except json.JSONDecodeError:
            return ([], "UNKNOWN", None, None)

    elif mime_type == "application/dash+xml":
        if "cenc:pssh" in raw or "widevine" in raw.lower():
            return ([], "WIDEVINE", None, None)
        init_url, codec, urls = parse_dash_mpd(raw)
        return (urls, "NONE", init_url, codec)

    return ([], "UNKNOWN", None, None)


def parse_dash_mpd(mpd_xml: str) -> tuple[str | None, str | None, list[str]]:
    """Parse DASH MPD XML and return (init_url, codec, media_segment_urls)."""
    NS = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
    try:
        root = ET.fromstring(mpd_xml)
    except ET.ParseError:
        return (None, None, [])

    segment_template = root.find(".//mpd:SegmentTemplate", NS)
    if segment_template is None:
        return (None, None, [])

    init_url = segment_template.get("initialization")
    media_url = segment_template.get("media", "")
    start_num = int(segment_template.get("startNumber", "1"))

    # Extract codec from Representation element
    rep = root.find(".//mpd:Representation", NS)
    codec = rep.get("codecs") if rep is not None else None

    timeline = []
    for s_elem in segment_template.findall(".//mpd:S", NS):
        duration = int(s_elem.get("d"))
        repeats = int(s_elem.get("r", "0"))
        timeline.extend([duration] * (repeats + 1))

    if not timeline:
        # Fallback: try to infer from representation
        if rep is not None and media_url:
            return (init_url, codec, [media_url.replace("$Number$", "1")])
        return (init_url, codec, [])

    urls = []
    for i in range(len(timeline)):
        urls.append(media_url.replace("$Number$", str(start_num + i)))
    return (init_url, codec, urls)


# ─── Download Worker ──────────────────────────────────────────────────────────


class DownloadWorker(QObject):
    """Worker that runs in a QThread to download tracks."""

    progress = pyqtSignal(int, str)  # (percent, message)
    task_done = pyqtSignal(object)  # DownloadTask
    task_failed = pyqtSignal(object)  # DownloadTask (with error set)

    def __init__(self, api: Api, output_dir: Path, download_mgr=None):
        super().__init__()
        self.api = api
        self.output_dir = output_dir
        self.download_mgr = download_mgr

    def run(self, task: DownloadTask):
        """Execute the full download pipeline for a task."""
        self.task = task
        try:
            self._fetch_manifest(task)
            if self.download_mgr and self.download_mgr.stopped:
                raise RuntimeError("Download stopped by user")
            self._download_file(task)
            task.status = "completed"
            task.progress = 100.0
            self.task_done.emit(task)
        except Exception as e:
            if task.status != "stopped":
                task.status = "failed"
                task.error = str(e)
            self.task_failed.emit(task)

    def _fetch_manifest(self, task: DownloadTask):
        """Fetch and decode the track manifest."""
        self.progress.emit(5, "Fetching manifest...")

        quality_order = ["FLAC_HIRES", "FLAC", "AACLC", "HEAACV1"]
        fmt = (
            quality_order[quality_order.index(task.quality)]
            if task.quality in quality_order
            else "FLAC"
        )
        fallbacks = quality_order[quality_order.index(fmt) + 1 :]

        last_error = None
        used_fallback = False
        for attempt_fmt in [fmt] + fallbacks:
            try:
                resp = self.api.get_manifest(task.track_id, attempt_fmt)
            except Exception as e:
                last_error = e
                continue

            manifest = resp.get("manifest", "")
            mime_type = resp.get("manifestMimeType", "")
            enc_type = resp.get("encryptionType", "NONE")

            if not manifest:
                last_error = ValueError("No manifest found in API response")
                continue

            if enc_type == "WIDEVINE":
                task.status = "drm_locked"
                task.error = "DRM-protected track (not supported in V1)"
                self.task_failed.emit(task)
                return

            urls, actual_enc, init_url, codec = decode_manifest(manifest, mime_type)
            if actual_enc != "UNKNOWN":
                enc_type = actual_enc

            if enc_type == "WIDEVINE":
                task.status = "drm_locked"
                task.error = "DRM-protected track (not supported in V1)"
                self.task_failed.emit(task)
                return

            if not urls:
                last_error = ValueError("No download URLs in manifest")
                continue

            if attempt_fmt != fmt and not used_fallback:
                label = {"FLAC": "FLAC", "AACLC": "AAC 256kbps", "HEAACV1": "AAC 96kbps"}.get(
                    attempt_fmt, attempt_fmt
                )
                self.progress.emit(10, f"Downscaling to {label}...")
                used_fallback = True

            task.download_urls = urls  # type: ignore
            task.init_url = init_url  # type: ignore
            task.audio_codec = codec  # type: ignore
            self.progress.emit(20, f"Manifest decoded ({enc_type})")

            # Fetch cover art for embedding
            try:
                cover_url = self.api.get_cover_url(task.track_id)
                if cover_url:
                    task.cover_bytes = self.api.fetch_cover_bytes(cover_url) or b""
            except Exception:
                pass  # Cover art is optional

            return

        if last_error:
            raise last_error
        raise ValueError(f"Track unavailable in any quality ({', '.join(quality_order)})")

    def _download_file(self, task: DownloadTask):
        """Download all DASH segments, byte-concatenate into a single MP4, and remux with ffmpeg.

        Tidal serves DASH tracks as an init segment (ftyp+moov header) followed by
        ~46 media segments (bare moof+mdat fragments). Each fragment is individually
        unplayable. The correct approach is to byte-concatenate all segments into one
        file, then remux once — matching the approach used by tidal-wave and other
        working Tidal downloaders.
        """
        import shutil

        # Set to True to keep temp files for inspection with ffprobe/ffmpeg
        KEEP_TEMP = False

        urls = getattr(task, "download_urls", [])
        init_url = getattr(task, "init_url", None)
        if not urls:
            raise ValueError("No URLs to download")

        # Check ffmpeg availability
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found in PATH. Install it to download DASH streams.")

        ext = self._detect_ext(task)
        filename = self._sanitize(f"{task.artist} - {task.title}") + ext
        filepath = self.output_dir / filename
        filepath = self._avoid_collision(filepath)

        # Create temp directory for segments
        tmp_dir = filepath.parent / f".tmp_{filepath.stem}"
        tmp_dir.mkdir(exist_ok=True)

        try:
            session = self.api.session
            total_segments = len(urls)

            # Single temp file for byte concatenation: init + all media segments
            combined_path = tmp_dir / "combined.mp4"

            # Download init segment if present
            if init_url:
                self.progress.emit(25, "Downloading init segment...")
                r = session.get(init_url, timeout=60)
                r.raise_for_status()
                with open(combined_path, "wb") as f:
                    f.write(r.content)

            # Download all media segments and append to combined file
            self.progress.emit(25, "Downloading segments...")
            with open(combined_path, "ab") as f:
                for i, url in enumerate(urls):
                    r = session.get(url, stream=True, timeout=60)
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=65536):
                        if not chunk:
                            break
                        f.write(chunk)
                    pct = 25 + int(65 * (i + 1) / total_segments)
                    mb = combined_path.stat().st_size / (1024 * 1024)
                    if self.download_mgr and self.download_mgr.stopped:
                        raise RuntimeError("Download stopped by user")
                    self.progress.emit(
                        min(pct, 90),
                        f"Downloading... {i + 1}/{total_segments} ({mb:.1f} MB)",
                    )

            # Single ffmpeg remux of the combined file
            self.progress.emit(91, "Muxing with ffmpeg...")
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(combined_path),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(filepath),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=600,
            )
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg failed: {result.stderr}")

            # Embed cover art if available
            if task.cover_bytes:
                self._embed_cover(filepath, task.cover_bytes)

            task.progress = 100.0
            task.filepath = str(filepath)  # type: ignore
            self.progress.emit(100, "Download complete")

        finally:
            # Clean up temp directory (skip if KEEP_TEMP for debugging)
            if not KEEP_TEMP:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _detect_ext(self, task: DownloadTask) -> str:
        # FLAC audio cannot be stored in .m4a container — use native .flac
        if task.audio_codec == "flac":
            return ".flac"
        return ".m4a"

    def _sanitize(self, text: str) -> str:
        text = re.sub(r'[<>:"/\\|?*]', "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:150]

    def _avoid_collision(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        i = 1
        while (path.parent / f"{stem}_{i}{suffix}").exists():
            i += 1
        return path.parent / f"{stem}_{i}{suffix}"

    def _embed_cover(self, filepath: Path, cover_data: bytes):
        """Embed cover art into the audio file using mutagen."""
        try:
            ext = filepath.suffix.lower()
            if ext == ".flac":
                from mutagen.flac import FLAC, Picture
                from mutagen.id3 import PictureType

                audio = FLAC(str(filepath))
                audio.clear_pictures()
                pic = Picture()
                pic.data = cover_data
                pic.type = PictureType.COVER_FRONT
                pic.mime = "image/jpeg"
                try:
                    pic.width = 1280
                    pic.height = 1280
                except Exception:
                    pass
                audio.add_picture(pic)
                audio.save()
            elif ext == ".m4a":
                from mutagen.mp4 import MP4

                audio = MP4(str(filepath))
                # MP4 uses a special cover art tag
                audio.tags["covr"] = [cover_data]
                audio.save()
        except Exception:
            pass  # Cover art embedding is optional


class DownloadManager(QObject):
    """Manages download queue and worker threads."""

    progress = pyqtSignal(int, str)  # (percent, message)
    task_completed = pyqtSignal(object)  # DownloadTask
    task_failed = pyqtSignal(object)  # DownloadTask
    task_status_changed = pyqtSignal()  # Emitted when any task status changes

    def __init__(self, api: Api, output_dir: Path, max_concurrent: int = 3):
        super().__init__()
        self.api = api
        self.output_dir = output_dir
        self.max_concurrent = max_concurrent
        self.queue: list[DownloadTask] = []
        self.queue_lock = threading.Lock()
        self.active_threads: list[tuple[QThread, DownloadWorker]] = []
        self.stopped = False

    def add(self, task: DownloadTask):
        with self.queue_lock:
            self.queue.append(task)
        self._dispatch_next()

    def _dispatch_next(self):
        """Start next download if slots available."""
        with self.queue_lock:
            if not self.queue:
                return
            # Clean up finished threads
            self.active_threads = [(t, w) for t, w in self.active_threads if t.isRunning()]
            if len(self.active_threads) >= self.max_concurrent:
                return

            task = self.queue.pop(0)
            task.status = "manifest"

        self.task_status_changed.emit()

        thread = QThread()
        worker = DownloadWorker(self.api, self.output_dir, self)
        worker.moveToThread(thread)

        # Connect signals
        thread.started.connect(lambda: worker.run(task))
        worker.progress.connect(self._on_worker_progress)
        worker.task_done.connect(self._on_task_done)
        worker.task_failed.connect(self._on_task_failed)

        # Clean up when thread finishes
        thread.finished.connect(lambda: self.active_threads.remove((thread, worker)))
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(worker.deleteLater)

        self.active_threads.append((thread, worker))
        thread.start()

    def _on_worker_progress(self, pct: int, msg: str):
        self.progress.emit(pct, msg)

    def _on_task_done(self, task: DownloadTask):
        self.task_completed.emit(task)
        self.task_status_changed.emit()
        self._dispatch_next()

    def _on_task_failed(self, task: DownloadTask):
        self.task_failed.emit(task)
        self.task_status_changed.emit()
        self._dispatch_next()

    def stop_all(self):
        """Stop all active downloads, reset tasks to queued."""
        self.stopped = True
        # Mark in-progress tasks as stopped (so they reset to queued, not failed)
        for _thread, worker in self.active_threads:
            if hasattr(worker, "task") and worker.task:
                worker.task.status = "stopped"
        # Terminate all active threads
        for thread, _worker in self.active_threads:
            thread.terminate()
            thread.wait(2000)
        # Clear internal dispatch queue (not-started items)
        with self.queue_lock:
            self.queue.clear()
        self.active_threads.clear()


# ─── MainWindow ───────────────────────────────────────────────────────────────


class SearchWorker(QObject):
    """Worker that runs search in a background thread."""

    results_ready = pyqtSignal(list)
    search_error = pyqtSignal(str)

    def __init__(self, api: Api = None):
        super().__init__()
        self.api = api
        self._query = ""
        self._limit = 50

    def run(self, query: str, limit: int = 50, offset: int = 0):
        try:
            items = self.api.search_tracks(query, limit=limit, offset=offset)
            self.results_ready.emit(items)
        except Exception as e:
            self.search_error.emit(str(e))


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("hifiT Downloader")
        self.resize(1000, 650)

        self.api = Api()
        self.output_dir = self._load_config().get("output_dir", str(Path.home() / "Music"))
        self.quality = self._load_config().get("quality", "FLAC")
        self.task_map: dict[int, DownloadTask] = {}  # track_id -> task
        self.result_rows: list[QTreeWidgetItem] = []  # track items in results tree
        self.queue_rows: list[tuple[QTreeWidgetItem, DownloadTask]] = []  # (item, task) in queue

        # Search pagination
        self._search_query = ""
        self._search_offset = 0
        self._search_limit = 25
        self._current_page = 0
        self._result_pages: list[list[dict]] = []  # cached raw data per page
        self._seen_track_ids: set[int] = set()  # track IDs already displayed

        # Search worker
        self.search_thread = QThread()
        self.search_worker = SearchWorker(self.api)
        self.search_worker.moveToThread(self.search_thread)
        self.search_worker.results_ready.connect(self._populate_results_safe)
        self.search_worker.search_error.connect(self._on_search_error)
        # Thread is started on first search, not at init

        self.download_mgr = DownloadManager(self.api, Path(self.output_dir))
        self.download_mgr.progress.connect(self.on_download_progress)
        self.download_mgr.task_completed.connect(self._on_task_completed)
        self.download_mgr.task_failed.connect(self._on_task_failed)
        self.download_mgr.task_status_changed.connect(self._on_task_status_changed)

        self._original_max_concurrent = self.download_mgr.max_concurrent
        self._sequential_remaining = 0

        self._results_sort_column: int = -1
        self._results_sort_mode: str = "relevance"
        self._queue_sort_column: int = -1
        self._queue_sort_mode: str = "asc"
        self._is_load_more: bool = False

        self._build_ui()
        self._server_process: subprocess.Popen[str] | None = None
        self._start_server()
        self._check_api()

    def _load_config(self) -> dict:
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text())
            except Exception:
                return {}
        return {}

    def _save_config(self):
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "output_dir": self.output_dir,
                    "quality": self.quality,
                },
                indent=2,
            )
        )

    def _build_ui(self):
        """Build the complete UI."""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ── Top bar: search + quality ──
        top_group = QGroupBox("Search")
        top_layout = QHBoxLayout(top_group)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Enter artist, album, or track name...")
        self.search_input.returnPressed.connect(self.on_search)
        top_layout.addWidget(self.search_input)

        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self.on_search)
        top_layout.addWidget(self.search_btn)

        quality_label = QLabel("Quality:")
        top_layout.addWidget(quality_label)

        self.quality_combo = QComboBox()
        for label, value in QUALITY_OPTIONS:
            self.quality_combo.addItem(label, value)
        # Set current quality
        for i in range(self.quality_combo.count()):
            if self.quality_combo.itemData(i) == self.quality:
                self.quality_combo.setCurrentIndex(i)
                break
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        top_layout.addWidget(self.quality_combo)

        # ── Splitter: results | queue ──
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Results panel
        results_widget = QWidget()
        results_layout = QVBoxLayout(results_widget)
        results_layout.setContentsMargins(0, 0, 0, 0)

        results_label = QLabel("Results")
        results_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        results_layout.addWidget(results_label)

        self.results_tree = QTreeWidget()
        self.results_tree.setColumnCount(5)
        self.results_tree.setHeaderLabels(["Track", "Artist", "Album", "Duration", "Quality"])
        self.results_tree.setColumnWidth(0, 250)
        self.results_tree.setColumnWidth(1, 150)
        self.results_tree.setColumnWidth(2, 150)
        self.results_tree.setColumnWidth(3, 70)
        self.results_tree.setColumnWidth(4, 110)
        self.results_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.results_tree.itemClicked.connect(self._on_result_clicked)
        self.results_tree.header().sectionClicked.connect(self._on_results_header_clicked)
        results_layout.addWidget(self.results_tree)

        # Page navigation for pagination
        self._prev_page_btn = QPushButton("<")
        self._prev_page_btn.setEnabled(False)
        self._prev_page_btn.clicked.connect(self._on_prev_page)
        self._prev_page_btn.setVisible(False)

        self._page_label = QLabel("Page 1")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_label.setVisible(False)

        self._next_page_btn = QPushButton(">")
        self._next_page_btn.clicked.connect(self._on_next_page)
        self._next_page_btn.setVisible(False)

        _pagination_layout = QHBoxLayout()
        _pagination_layout.addWidget(self._prev_page_btn)
        _pagination_layout.addWidget(self._page_label)
        _pagination_layout.addWidget(self._next_page_btn)
        results_layout.addLayout(_pagination_layout)

        btn_layout = QHBoxLayout()
        self.add_to_queue_btn = QPushButton("Add to Queue")
        self.add_to_queue_btn.clicked.connect(self._queue_selected)
        btn_layout.addWidget(self.add_to_queue_btn)

        self.download_btn = QPushButton("Download Selected")
        self.download_btn.clicked.connect(self.on_download)
        btn_layout.addWidget(self.download_btn)

        self.cover_label = QLabel("Cover art")
        self.cover_label.setFixedSize(128, 128)
        self.cover_label.setFrameStyle(QFrame.Shape.StyledPanel)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet("background-color: #e0e0e0; border-radius: 4px;")
        btn_layout.addWidget(self.cover_label)
        btn_layout.addStretch()
        results_layout.addLayout(btn_layout)

        splitter.addWidget(results_widget)

        # Queue panel
        queue_widget = QWidget()
        queue_layout = QVBoxLayout(queue_widget)
        queue_layout.setContentsMargins(0, 0, 0, 0)

        queue_label = QLabel("Download Queue")
        queue_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        queue_layout.addWidget(queue_label)

        self.queue_tree = QTreeWidget()
        self.queue_tree.setColumnCount(5)
        self.queue_tree.setHeaderLabels(["Track", "Artist", "Progress", "Status", "File"])
        self.queue_tree.setColumnWidth(0, 200)
        self.queue_tree.setColumnWidth(1, 130)
        self.queue_tree.setColumnWidth(2, 120)
        self.queue_tree.setColumnWidth(3, 130)
        self.queue_tree.setColumnWidth(4, 150)
        self.queue_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_tree.customContextMenuRequested.connect(self._show_queue_context_menu)
        self.queue_tree.header().sectionClicked.connect(self._on_queue_header_clicked)
        queue_layout.addWidget(self.queue_tree)

        queue_btn_layout = QHBoxLayout()
        self.download_all_btn = QPushButton("Download All")
        self.download_all_btn.clicked.connect(self.on_download_all)
        queue_btn_layout.addWidget(self.download_all_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop_downloads)
        queue_btn_layout.addWidget(self.stop_btn)

        self.clear_btn = QPushButton("Clear Completed")
        self.clear_btn.clicked.connect(self.clear_completed)
        queue_btn_layout.addWidget(self.clear_btn)
        queue_btn_layout.addStretch()
        queue_layout.addLayout(queue_btn_layout)

        splitter.addWidget(queue_widget)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(top_group)
        main_layout.addWidget(splitter)

        # ── Bottom bar ──
        bottom_layout = QHBoxLayout()

        self.status_label = QLabel("Checking API...")
        bottom_layout.addWidget(self.status_label)

        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)
        self.download_progress.setFixedWidth(150)
        bottom_layout.addWidget(self.download_progress)

        dir_label = QLabel("Output:")
        bottom_layout.addWidget(dir_label)

        self.dir_display = QLabel(self.output_dir)
        self.dir_display.setWordWrap(False)
        bottom_layout.addWidget(self.dir_display)

        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse_output)
        bottom_layout.addWidget(self.browse_btn)

        main_layout.addLayout(bottom_layout)

        # ── Server log console ──
        log_group = QGroupBox("Server Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(4, 4, 4, 4)

        self.server_log = QTextEdit()
        self.server_log.setReadOnly(True)
        self.server_log.setMaximumHeight(120)
        self.server_log.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px; background: #1e1e1e; color: #d4d4d4;"
        )
        log_layout.addWidget(self.server_log)

        main_layout.addWidget(log_group)

        # ── OLED pure black theme ──
        self.setStyleSheet(
            "QMainWindow { background-color: #000000; }"
            "QWidget { background-color: #000000; color: #e0e0e0; }"
            "QTreeWidget { background-color: #000000; color: #e0e0e0; border: none; }"
            "QTreeWidget::item { background-color: #000000; color: #e0e0e0; padding: 2px; }"
            "QTreeWidget::item:selected { background-color: #1a1a2e; color: #ffffff; }"
            "QTreeWidget::header { background-color: #0a0a0a; color: #aaaaaa; border: none; padding: 4px; }"
            "QPushButton { background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #333333; padding: 6px 16px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #2a2a2a; }"
            "QPushButton:disabled { background-color: #111111; color: #555555; }"
            "QLabel { color: #e0e0e0; background-color: transparent; }"
            "QLineEdit { background-color: #0a0a0a; color: #e0e0e0; border: 1px solid #333333; padding: 4px; }"
            "QComboBox { background-color: #0a0a0a; color: #e0e0e0; border: 1px solid #333333; padding: 4px; }"
            "QComboBox::drop-down { border: none; }"
            "QComboBox QAbstractItemView { background-color: #0a0a0a; color: #e0e0e0; selection-background-color: #1a1a2e; }"
            "QProgressBar { border: none; background-color: #0a0a0a; text-align: center; }"
            "QProgressBar::chunk { background-color: #4a90d9; }"
            "QSplitter::handle { background-color: #222222; }"
            "QScrollBar:vertical { background-color: #000000; width: 12px; }"
            "QScrollBar::handle:vertical { background-color: #333333; min-height: 20px; border-radius: 6px; }"
            "QScrollBar::add-line, QScrollBar::sub-line { background: none; }"
            "QScrollBar:horizontal { background-color: #000000; height: 12px; }"
            "QScrollBar::handle:horizontal { background-color: #333333; min-width: 20px; border-radius: 6px; }"
            'QFrame[frameShape="4"] { background-color: #0a0a0a; border: 1px solid #222222; }'
        )

    def _append_server_log(self, text: str):
        """Append text to the server log console (thread-safe via QTimer)."""
        QTimer.singleShot(0, lambda: self.server_log.append(text.rstrip()))

    def _check_api(self):
        """Check if the API server is running."""
        if self.api.ping():
            log.debug("API server is running")
            self.status_label.setText("API connected")
            self.status_label.setStyleSheet("color: green;")
        else:
            log.debug("API server not reachable")
            self.status_label.setText("API not running - starting server...")
            self.status_label.setStyleSheet("color: red;")
            self.search_btn.setEnabled(False)
            self.download_btn.setEnabled(False)

    def _start_server(self):
        """Start the hifi-api server if not already running."""
        if self.api.ping():
            log.debug("API server already running, skipping start")
            return

        log.debug("Starting hifi-api server from %s", _MAIN_PY)
        if not _MAIN_PY.exists():
            self.show_error_dialog(
                "Server not found",
                f"main.py not found at {_MAIN_PY}. "
                "Place the GUI in the hifi-api project directory.",
            )
            return

        python_exe = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable
        log.debug("Using Python: %s", python_exe)
        try:
            self._server_process = subprocess.Popen(
                [python_exe, str(_MAIN_PY)],
                cwd=str(_SCRIPT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
            )
            log.debug("Server process started with PID %s", self._server_process.pid)
            self._append_server_log(f"[PID {self._server_process.pid}] Server starting...")
            # Start reader thread to pipe server output to log console
            self._server_reader = threading.Thread(
                target=self._read_server_output,
                daemon=True,
            )
            self._server_reader.start()
        except Exception as e:
            self.show_error_dialog(
                "Failed to start server",
                f"Could not start hifi-api server:\n{e}",
            )
            return

        # Wait for server to become ready (poll up to 15 seconds)
        ready = False
        for i in range(30):
            if self.api.ping():
                ready = True
                log.debug("Server ready after %.1fs", i * 0.5)
                self._append_server_log("[OK] Server is ready")
                break
            threading.Event().wait(0.5)

        if not ready:
            log.debug("Server failed to start after %.1fs", 30 * 0.5)
            if self._server_process.poll() is not None:
                stdout, stderr = self._server_process.communicate()
                error_msg = (stderr if stderr else stdout or "").strip()[:500]
                log.debug("Server stderr: %s", error_msg)
                self._append_server_log(f"[ERR] Server exited: {error_msg}")
                self.show_error_dialog(
                    "Server failed to start",
                    f"hifi-api exited prematurely:\n{error_msg}",
                )
            else:
                self._server_process.terminate()
                self._append_server_log("[ERR] Server timeout after 15s")
                self.show_error_dialog(
                    "Server timeout",
                    "hifi-api did not start within 15 seconds.",
                )

    def _read_server_output(self):
        """Read server stdout line-by-line and pipe to log console."""
        try:
            for line in self._server_process.stdout:
                self._append_server_log(
                    f"[server] {line}",
                )
        except Exception:
            pass
        self._append_server_log("[server] Process ended")

    def _on_quality_changed(self, index: int):
        self.quality = self.quality_combo.itemData(index)
        self._save_config()

    def on_search(self):
        query = self.search_input.text().strip()
        if not query:
            return

        log.debug("Search started: %s", query)
        self.search_btn.setEnabled(False)
        self.search_btn.setText("Searching...")
        self.results_tree.clear()
        self.result_rows.clear()

        # Reset pagination state
        self._search_query = query
        self._search_offset = 0
        self._search_limit = 25
        self._current_page = 0
        self._result_pages = []
        self._seen_track_ids.clear()
        self._update_pagination_ui()

        # Reset worker with new query
        self.search_worker.api = self.api
        self.search_worker._query = query
        self.search_worker._limit = self._search_limit

        # Start thread if not running, then trigger search
        if not self.search_thread.isRunning():
            log.debug("Starting search thread")
            self.search_thread.start()
        QTimer.singleShot(0, self._emit_search)

    def _emit_search(self):
        """Emit search in the worker's thread context."""
        log.debug("Emitting search for: %s", self.search_worker._query)
        self.search_worker.run(
            self.search_worker._query,
            self.search_worker._limit,
            self._search_offset,
        )

    def _do_search(self, query: str, limit: int):
        """Called in the search thread."""
        log.debug("Search worker executing: %s (limit=%d)", query, limit)
        try:
            items = self.api.search_tracks(query, limit=limit)
            log.debug("Search worker got %d items", len(items))
            self.results_ready.emit(items)
        except Exception as e:
            log.debug("Search worker error: %s", e)
            self.search_error.emit(str(e))

    def _populate_results_safe(self, items: list[dict]):
        """Populate results tree with search results."""
        log.debug("Populating %d search results", len(items))
        is_load_more = self._is_load_more
        self._is_load_more = False
        if not is_load_more:
            self.results_tree.clear()
            self.result_rows.clear()
        page_data: list[dict] = []
        for item_data in items:
            track_id = item_data.get("id", 0)
            if track_id in self._seen_track_ids:
                continue
            self._seen_track_ids.add(track_id)

            title = item_data.get("title", "Unknown")
            artist = item_data.get("artist", {}).get("name", "Unknown")
            album = item_data.get("album", {}).get("title", "Unknown")
            duration = item_data.get("duration", 0)
            min_duration = item_data.get("minDuration", "")
            audio_quality = item_data.get("audioQuality", "")

            dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else min_duration

            task = DownloadTask(
                track_id=track_id,
                title=title,
                artist=artist,
                album=album,
                duration=duration,
                quality=self.quality,
            )
            self.task_map[track_id] = task

            tree_item = QTreeWidgetItem(
                [
                    title,
                    artist,
                    album,
                    dur_str,
                    audio_quality,
                ]
            )
            tree_item.setData(0, Qt.ItemDataRole.UserRole, track_id)
            self.results_tree.addTopLevelItem(tree_item)
            self.result_rows.append(tree_item)

            page_data.append(
                {
                    "track_id": track_id,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "duration": dur_str,
                    "audio_quality": audio_quality,
                }
            )

        # Cache this page's raw data for page navigation
        while len(self._result_pages) <= self._current_page:
            self._result_pages.append([])
        self._result_pages[self._current_page] = page_data

        # Update pagination UI
        if len(items) >= self._search_limit:
            self._search_offset += self._search_limit
            self.search_worker._limit = self._search_limit
            self._next_page_btn.setVisible(True)
        else:
            self._next_page_btn.setVisible(False)
        self._update_pagination_ui()

        # Swap display to new page after load-more (only if we have new items)
        if is_load_more and page_data:
            self._swap_to_new_page()

        # Re-sort if an active sort is in place
        if self._results_sort_column >= 0 and self._results_sort_mode != "relevance":
            self._sort_results_tree()

        self.search_btn.setEnabled(True)
        self.search_btn.setText("Search")
        log.debug("Search complete, button re-enabled")

    def _on_search_error(self, error_msg: str):
        """Handle search error from worker thread."""
        log.debug("Search error: %s", error_msg)
        self.search_btn.setEnabled(True)
        self.search_btn.setText("Search")
        QMessageBox.warning(self, "Search Error", f"Search failed: {error_msg}")

    def _on_results_header_clicked(self, column: int):
        """Cycle sort mode on search results header click."""
        if self._results_sort_column == column:
            if self._results_sort_mode == "relevance":
                self._results_sort_mode = "asc"
            elif self._results_sort_mode == "asc":
                self._results_sort_mode = "desc"
            else:
                self._results_sort_mode = "relevance"
        else:
            self._results_sort_column = column
            self._results_sort_mode = "asc"
        self._update_sort_indicators()
        self._sort_results_tree()

    def _update_sort_indicators(self):
        """Set visual sort indicators on the results tree header."""
        header = self.results_tree.header()
        if header is None:
            return
        _no_order = Qt.SortOrder(0)
        # Clear the old sort indicator first
        if self._results_sort_column >= 0:
            header.setSortIndicator(self._results_sort_column, _no_order)
        if self._results_sort_mode == "relevance":
            self._results_sort_column = -1
        elif self._results_sort_column >= 0:
            if self._results_sort_mode == "asc":
                header.setSortIndicator(self._results_sort_column, Qt.SortOrder.AscendingOrder)
            else:
                header.setSortIndicator(self._results_sort_column, Qt.SortOrder.DescendingOrder)

    def _sort_results_tree(self):
        """Sort search results by current sort column and mode."""
        all_items = [
            self.results_tree.topLevelItem(i) for i in range(self.results_tree.topLevelItemCount())
        ]
        if self._results_sort_mode == "relevance":
            return
        all_items.sort(
            key=lambda item: item.text(self._results_sort_column),
            reverse=(self._results_sort_mode == "desc"),
        )
        for item in all_items:
            self.results_tree.takeTopLevelItem(self.results_tree.indexOfTopLevelItem(item))
        for item in all_items:
            self.results_tree.addTopLevelItem(item)

    def _on_queue_header_clicked(self, column: int):
        """Toggle sort mode on queue header click."""
        if self._queue_sort_column == column:
            self._queue_sort_mode = "desc" if self._queue_sort_mode == "asc" else "asc"
        else:
            self._queue_sort_column = column
            self._queue_sort_mode = "asc"
        self._sort_queue_tree()

    def _sort_queue_tree(self):
        """Sort queue by current sort column and mode."""
        all_items = [
            self.queue_tree.topLevelItem(i) for i in range(self.queue_tree.topLevelItemCount())
        ]
        all_items.sort(
            key=lambda item: item.text(self._queue_sort_column),
            reverse=(self._queue_sort_mode == "desc"),
        )
        for item in all_items:
            self.queue_tree.takeTopLevelItem(self.queue_tree.indexOfTopLevelItem(item))
        for item in all_items:
            self.queue_tree.addTopLevelItem(item)

    def _on_next_page(self):
        """Load the next page of search results."""
        if not self._search_query:
            return
        self._current_page += 1
        self._search_offset = self._current_page * self._search_limit
        self._load_page()

    def _swap_to_new_page(self):
        """Swap the tree display to the newly loaded page."""
        self.results_tree.clear()
        self.result_rows.clear()
        for item_data in self._result_pages[self._current_page]:
            tree_item = QTreeWidgetItem(
                [
                    item_data["title"],
                    item_data["artist"],
                    item_data["album"],
                    item_data["duration"],
                    item_data["audio_quality"],
                ]
            )
            tree_item.setData(0, Qt.ItemDataRole.UserRole, item_data["track_id"])
            self.results_tree.addTopLevelItem(tree_item)
            self.result_rows.append(tree_item)
        if self._results_sort_column >= 0 and self._results_sort_mode != "relevance":
            self._sort_results_tree()
        self._update_pagination_ui()

    def _on_prev_page(self):
        """Go back to the previous page (re-display cached items)."""
        if self._current_page <= 0:
            return
        self._current_page -= 1
        self._search_offset = self._current_page * self._search_limit
        self._display_page(self._current_page)

    def _display_page(self, page_index: int):
        """Display the cached items for a specific page."""
        self.results_tree.clear()
        self.result_rows.clear()
        for item_data in self._result_pages[page_index]:
            tree_item = QTreeWidgetItem(
                [
                    item_data["title"],
                    item_data["artist"],
                    item_data["album"],
                    item_data["duration"],
                    item_data["audio_quality"],
                ]
            )
            tree_item.setData(0, Qt.ItemDataRole.UserRole, item_data["track_id"])
            self.results_tree.addTopLevelItem(tree_item)
            self.result_rows.append(tree_item)
        if self._results_sort_column >= 0 and self._results_sort_mode != "relevance":
            self._sort_results_tree()
        self._update_pagination_ui()

    def _load_page(self):
        """Fetch the next page from the API."""
        self._is_load_more = True
        self._next_page_btn.setEnabled(False)
        if not self.search_thread.isRunning():
            self.search_thread.start()
        QTimer.singleShot(0, self._emit_load_page)

    def _emit_load_page(self):
        """Emit page load in the worker's thread context."""
        self.search_worker.run(
            self._search_query,
            self._search_limit,
            self._search_offset,
        )

    def _update_pagination_ui(self):
        """Update pagination button states and page label."""
        self._prev_page_btn.setEnabled(self._current_page > 0)
        self._prev_page_btn.setVisible(self._current_page > 0)
        self._page_label.setText(f"Page {self._current_page + 1}")
        self._page_label.setVisible(True)

    def _on_result_clicked(self, item: QTreeWidgetItem, column: int):
        """Show cover art when a result is clicked."""
        track_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not track_id:
            return

        cover_url = self.api.get_cover_url(track_id)
        if not cover_url:
            return

        cover_bytes = self.api.fetch_cover_bytes(cover_url)
        if cover_bytes:
            pixmap = QPixmap()
            if pixmap.loadFromData(cover_bytes):
                pixmap = pixmap.scaled(
                    128,
                    128,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.cover_label.setPixmap(pixmap)

    def on_download(self):
        """Add selected tracks to queue and start downloading immediately."""
        selected = self.results_tree.selectedItems()
        if not selected:
            QMessageBox.information(self, "No Selection", "Select one or more tracks to download.")
            return

        # Queue selected tracks first
        queued_ids = {task.track_id for _, task in self.queue_rows}
        for item in selected:
            track_id = item.data(0, Qt.ItemDataRole.UserRole)
            if track_id in queued_ids:
                continue
            task = self.task_map.get(track_id)
            if not task:
                continue
            task.status = "queued"
            task.quality = self.quality
            self._add_to_queue(task, item)
            queued_ids.add(track_id)

        # Start downloading immediately
        queued_tasks = [task for _, task in self.queue_rows if task.status == "queued"]
        for task in queued_tasks:
            self.download_mgr.add(task)

    def _add_to_queue(self, task: DownloadTask, source_item: QTreeWidgetItem):
        """Add task to the queue tree."""
        tree_item = QTreeWidgetItem(
            [
                task.title,
                task.artist,
                "0%",
                "Queued",
                "",
            ]
        )
        self.queue_tree.addTopLevelItem(tree_item)
        self.queue_rows.append((tree_item, task))
        if self._queue_sort_column >= 0:
            self._sort_queue_tree()

    def _queue_selected(self):
        """Add selected search results to the queue without starting downloads."""
        selected = self.results_tree.selectedItems()
        if not selected:
            QMessageBox.information(self, "No Selection", "Select one or more tracks to queue.")
            return

        queued_ids = {task.track_id for _, task in self.queue_rows}
        added_count = 0

        for item in selected:
            track_id = item.data(0, Qt.ItemDataRole.UserRole)
            if track_id in queued_ids:
                continue
            task = self.task_map.get(track_id)
            if not task:
                continue

            task.status = "queued"
            task.quality = self.quality
            self._add_to_queue(task, item)
            queued_ids.add(track_id)
            added_count += 1

        if added_count == 0:
            QMessageBox.information(
                self, "All Queued", "All selected tracks are already in the queue."
            )

    def _queue_all_results(self):
        """Queue all unqueued search results. Does NOT auto-start downloads."""
        queued_ids = {task.track_id for _, task in self.queue_rows}
        queued_count = 0

        for tree_item in self.result_rows:
            track_id = tree_item.data(0, Qt.ItemDataRole.UserRole)
            if track_id in queued_ids:
                continue
            task = self.task_map.get(track_id)
            if not task:
                continue

            task.status = "queued"
            task.quality = self.quality
            self._add_to_queue(task, tree_item)
            queued_count += 1

        if queued_count == 0:
            QMessageBox.information(
                self, "All Queued", "All search results are already in the queue."
            )

    def on_download_all(self):
        """Download all queued items sequentially (1 thread)."""
        queued_tasks = [task for _, task in self.queue_rows if task.status == "queued"]
        if not queued_tasks:
            QMessageBox.information(self, "Nothing to Download", "No queued items to download.")
            return

        self.download_mgr.stopped = False
        self.download_mgr.max_concurrent = 1
        self._sequential_remaining = len(queued_tasks)

        for task in queued_tasks:
            self.download_mgr.add(task)

    def on_stop_downloads(self):
        """Stop all active downloads and clear the queue."""
        self.download_mgr.stop_all()
        self._update_stop_button()

    def _update_stop_button(self):
        """Enable/disable Stop button based on active downloads."""
        has_active = any(
            task.status in ("manifest", "downloading", "queued") for _, task in self.queue_rows
        )
        self.stop_btn.setEnabled(has_active)

    def _on_task_status_changed(self):
        """React to any task status change (from DownloadManager signal)."""
        self._update_stop_button()

    def _show_queue_context_menu(self, position):
        """Show context menu on queue_tree right-click."""
        item = self.queue_tree.itemAt(position)
        if not item:
            return

        for tree_item, task in self.queue_rows:
            if tree_item == item:
                menu = QMenu(self)
                remove_action = menu.addAction("Remove")
                action = menu.exec(self.queue_tree.viewport().mapToGlobal(position))
                if action == remove_action:
                    self._remove_from_queue(task, tree_item)
                break

    def _remove_from_queue(self, task: DownloadTask, tree_item: QTreeWidgetItem):
        """Remove a task from the queue."""
        idx = self.queue_tree.indexOfTopLevelItem(tree_item)
        if idx >= 0:
            self.queue_tree.takeTopLevelItem(idx)

        if (tree_item, task) in self.queue_rows:
            self.queue_rows.remove((tree_item, task))

        task.status = "removed"

    def on_download_progress(self, pct: int, msg: str):
        """Called from DownloadWorker via signal."""
        self.download_progress.setValue(pct)

        # Update queue rows with current progress
        for tree_item, task in self.queue_rows:
            if task.status in ("manifest", "downloading", "queued"):
                tree_item.setText(2, f"{pct}%")
                tree_item.setText(3, msg)

    def _on_task_completed(self, task: DownloadTask):
        """Handle completed download."""
        for tree_item, t in self.queue_rows:
            if t.track_id == task.track_id:
                t.status = "completed"
                tree_item.setText(2, "100%")
                tree_item.setText(3, "Completed")
                if task.filepath:
                    tree_item.setText(4, Path(task.filepath).name)
                break
        # Restore max_concurrent when sequential download finishes
        if self._sequential_remaining > 0:
            self._sequential_remaining -= 1
            if self._sequential_remaining == 0:
                self.download_mgr.max_concurrent = self._original_max_concurrent
        self._update_stop_button()

    def _on_task_failed(self, task: DownloadTask):
        """Handle failed download."""
        found = False
        for tree_item, t in self.queue_rows:
            if t.track_id == task.track_id:
                found = True
                if task.status == "stopped":
                    t.status = "queued"
                    tree_item.setText(2, "0%")
                    tree_item.setText(3, "Queued")
                else:
                    t.status = task.status
                    tree_item.setText(2, "0%")
                    tree_item.setText(3, task.error or "Failed")
                break
        # Restore max_concurrent when sequential download finishes
        if self._sequential_remaining > 0:
            self._sequential_remaining -= 1
            if self._sequential_remaining == 0:
                self.download_mgr.max_concurrent = self._original_max_concurrent
        # Dispatch next to prevent stall (for stopped tasks or missing tasks)
        if task.status == "stopped" or not found:
            self.download_mgr._dispatch_next()
        self._update_stop_button()

    def clear_completed(self):
        """Remove completed tasks from the queue tree."""
        to_remove = []
        for tree_item, task in self.queue_rows:
            if task.status in ("completed", "failed", "drm_locked"):
                to_remove.append((tree_item, task))

        for tree_item, task in to_remove:
            self.queue_tree.takeTopLevelItem(self.queue_tree.indexOfTopLevelItem(tree_item))
            self.queue_rows.remove((tree_item, task))

    def _browse_output(self):
        """Open directory picker for output folder."""
        dir_path = QFileDialog.getExistingDirectory(self, "Select Output Folder", self.output_dir)
        if dir_path:
            self.output_dir = dir_path
            self.dir_display.setText(dir_path)
            self.download_mgr.output_dir = Path(dir_path)
            self._save_config()

    def show_error_dialog(self, title: str, message: str) -> None:
        """Show an error dialog.

        Override or monkeypatch this method in tests to avoid blocking
        QMessageBox in headless pytest-qt sessions.  Default implementation
        logs to console and shows a QMessageBox for normal use.
        """
        log.error("[ERROR DIALOG] %s: %s", title, message)
        QMessageBox.critical(self, title, message)

    def closeEvent(self, event):
        """Save config, clean up threads, stop the server, and remove temp folders."""
        self._save_config()
        if self.search_thread.isRunning():
            self.search_thread.quit()
            self.search_thread.wait()
        if self._server_process and self._server_process.poll() is None:
            self._server_process.terminate()
            try:
                self._server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._server_process.kill()
        # Clean up temp download directories in the output folder
        output_path = Path(self.output_dir)
        if output_path.is_dir():
            for item in output_path.iterdir():
                if item.is_dir() and item.name.startswith(".tmp_"):
                    shutil.rmtree(item, ignore_errors=True)
        event.accept()


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # Clean, cross-platform style

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
