"""Tests for gui_downloader.py — unit tests and GUI integration tests."""

import base64
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
)
from pytest_httpserver import HTTPServer

from gui_downloader import (
    Api,
    DownloadTask,
    DownloadWorker,
    decode_manifest,
    parse_dash_mpd,
)

# NOTE: show_error_dialog is patched per-test via subclass or monkeypatch.
# We avoid global patching because modifying PyQt6 classes during pytest
# collection interferes with Qt's internal state and causes hangs.


@pytest.fixture(autouse=True)
def _kill_server_on_port_8000():
    """Kill any process listening on port 8000 after each test."""
    yield
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.splitlines():
            if ":8000" in line and "LISTENING" in line:
                parts = line.strip().split()
                pid = parts[-1]
                if pid.isdigit():
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True,
                        check=False,
                    )
    except Exception:
        pass


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def http_server():
    """Start a local test HTTP server."""
    server = HTTPServer(host="127.0.0.1", port=18932)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def api(http_server: HTTPServer):
    """Api instance pointed at the test server."""
    a = Api()
    a.session.headers["User-Agent"] = "test"
    return a


@pytest.fixture
def bts_manifest_json() -> str:
    """Non-DRM BTS manifest JSON."""
    return json.dumps(
        {
            "mimeType": "audio/flac",
            "codecs": "flac",
            "encryptionType": "NONE",
            "urls": [
                "https://stream.tidal.com/track/12345.flac?token=abc",
            ],
        }
    )


@pytest.fixture
def bts_manifest_b64(bts_manifest_json: str) -> str:
    """Base64-encoded BTS manifest."""
    return base64.b64encode(bts_manifest_json.encode()).decode()


@pytest.fixture
def drm_manifest_json() -> str:
    """Widevine DRM manifest JSON."""
    return json.dumps(
        {
            "mimeType": "audio/flac",
            "codecs": "flac",
            "encryptionType": "WIDEVINE",
            "urls": [],
        }
    )


@pytest.fixture
def drm_manifest_b64(drm_manifest_json: str) -> str:
    """Base64-encoded DRM manifest."""
    return base64.b64encode(drm_manifest_json.encode()).decode()


@pytest.fixture
def valid_mpd_xml() -> str:
    """A minimal valid DASH MPD XML."""
    return """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
         minBufferTime="PT1.5S" type="static"
         profiles="urn:mpeg:dash:profile:isoff-live:2011">
      <Period id="1" start="PT0S">
        <AdaptationSet contentType="audio" mimeType="audio/mp4"
                       segAlignment="true" bitRate="256000">
          <Representation id="1" codecs="mp4a.40.2"
                          audioSamplingRate="44100"
                          bandwidth="256000">
            <SegmentTemplate timescale="44100"
                             initialization="init.mp4"
                             media="seg-$Number$.m4s"
                             startNumber="1"/>
            <SegmentTimeline>
              <S d="44100" r="3"/>
              <S d="22050"/>
            </SegmentTimeline>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""


@pytest.fixture
def valid_mpd_b64(valid_mpd_xml: str) -> str:
    """Base64-encoded valid DASH MPD."""
    return base64.b64encode(valid_mpd_xml.encode()).decode()


@pytest.fixture
def drm_mpd_xml() -> str:
    """DASH MPD with Widevine DRM markers."""
    return """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
      <Period>
        <AdaptationSet>
          <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
            <cenc:pssh>BIY0ZGF0YQ==</cenc:pssh>
          </ContentProtection>
          <Representation>
            <SegmentTemplate media="seg-$Number$.m4s" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""


@pytest.fixture
def drm_mpd_b64(drm_mpd_xml: str) -> str:
    """Base64-encoded DRM DASH MPD."""
    return base64.b64encode(drm_mpd_xml.encode()).decode()


@pytest.fixture
def malformed_xml() -> str:
    """Invalid XML that should be handled gracefully."""
    return "<MPD><Period><AdaptationSet not closed"


@pytest.fixture
def sample_track_data() -> dict:
    """Sample track data matching the Tidal search API response shape."""
    return {
        "id": 62397812,
        "title": "One More Time",
        "artist": {"id": 234567, "name": "Daft Punk"},
        "album": {"id": 8394857, "title": "Discovery", "cover": "abc123"},
        "duration": 243,
        "trackNumber": 4,
        "streamingStatus": "STREAMABLE",
    }


@pytest.fixture
def sample_search_response(sample_track_data: dict) -> dict:
    """Sample search API response — items nested under data."""
    return {"data": {"items": [sample_track_data]}}


@pytest.fixture
def sample_manifest_response(bts_manifest_b64: str) -> dict:
    """Sample /trackManifests/ API response — URI-based manifest."""
    return {
        "data": {
            "data": {
                "attributes": {
                    "uri": "https://example.tidal.com/manifest/123.mpd",
                    "hash": "abc123==",
                    "formats": ["FLAC"],
                    "manifestMimeType": "application/vnd.tidal.bts",
                }
            }
        }
    }


@pytest.fixture
def sample_cover_response() -> dict:
    """Sample cover API response."""
    return {
        "covers": [
            {
                "id": 8394857,
                "name": "Discovery",
                "1280": "https://resources.tidal.com/images/abc123/1280x1280.jpg",
                "640": "https://resources.tidal.com/images/abc123/640x640.jpg",
                "80": "https://resources.tidal.com/images/abc123/80x80.jpg",
            }
        ]
    }


# ─── decode_manifest tests ──────────────────────────────────────────────────


class TestDecodeManifest:
    """Tests for decode_manifest function."""

    def test_bts_non_drm_returns_urls(self, bts_manifest_b64: str):
        """Non-DRM BTS manifest returns URLs and NONE encryption."""
        urls, enc, init_url, codec = decode_manifest(bts_manifest_b64, "application/vnd.tidal.bts")
        assert urls == ["https://stream.tidal.com/track/12345.flac?token=abc"]
        assert enc == "NONE"
        assert init_url is None
        assert codec is None

    def test_bts_drm_returns_empty(self, drm_manifest_b64: str):
        """DRM BTS manifest returns empty URLs and WIDEVINE encryption."""
        urls, enc, init_url, codec = decode_manifest(drm_manifest_b64, "application/vnd.tidal.bts")
        assert urls == []
        assert enc == "WIDEVINE"
        assert init_url is None
        assert codec is None

    def test_bts_default_encryption_is_none(self):
        """BTS manifest without encryptionType defaults to NONE."""
        payload = json.dumps({"urls": ["http://example.com/audio.flac"]})
        manifest_b64 = base64.b64encode(payload.encode()).decode()
        urls, enc, init_url, codec = decode_manifest(manifest_b64, "application/vnd.tidal.bts")
        assert urls == ["http://example.com/audio.flac"]
        assert enc == "NONE"
        assert init_url is None
        assert codec is None

    def test_dash_non_drm_parses_segments(self, valid_mpd_b64: str):
        """Non-DRM DASH MPD returns segment URLs and init URL."""
        urls, enc, init_url, codec = decode_manifest(valid_mpd_b64, "application/dash+xml")
        assert enc == "NONE"
        assert len(urls) >= 1
        assert all("seg-" in url for url in urls)
        assert init_url is not None
        assert codec is not None

    def test_dash_drm_detected_via_pssh(self, drm_mpd_b64: str):
        """DASH MPD with cenc:pssh is detected as DRM."""
        urls, enc, init_url, codec = decode_manifest(drm_mpd_b64, "application/dash+xml")
        assert urls == []
        assert enc == "WIDEVINE"
        assert init_url is None
        assert codec is None

    def test_dash_drm_detected_via_widevine_text(self):
        """DASH MPD containing 'widevine' text is detected as DRM."""
        mpd = "<MPD><Period><AdaptationSet><widevine>test</widevine></AdaptationSet></MPD>"
        manifest_b64 = base64.b64encode(mpd.encode()).decode()
        urls, enc, init_url, codec = decode_manifest(manifest_b64, "application/dash+xml")
        assert urls == []
        assert enc == "WIDEVINE"
        assert init_url is None
        assert codec is None

    def test_invalid_base64_returns_unknown(self):
        """Invalid base64 input returns empty URLs and UNKNOWN encryption."""
        urls, enc, init_url, codec = decode_manifest(
            "not-valid-base64!!!", "application/vnd.tidal.bts"
        )
        assert urls == []
        assert enc == "UNKNOWN"
        assert init_url is None
        assert codec is None

    def test_bts_invalid_json_returns_unknown(self):
        """BTS manifest with invalid JSON returns UNKNOWN."""
        manifest_b64 = base64.b64encode(b"not json").decode()
        urls, enc, init_url, codec = decode_manifest(manifest_b64, "application/vnd.tidal.bts")
        assert urls == []
        assert enc == "UNKNOWN"
        assert init_url is None
        assert codec is None

    def test_unknown_mime_type_returns_unknown(self, bts_manifest_b64: str):
        """Unknown MIME type returns empty URLs and UNKNOWN encryption."""
        urls, enc, init_url, codec = decode_manifest(bts_manifest_b64, "application/unknown")
        assert urls == []
        assert enc == "UNKNOWN"
        assert init_url is None
        assert codec is None


# ─── parse_dash_mpd tests ───────────────────────────────────────────────────


class TestParseDashMpd:
    """Tests for parse_dash_mpd function."""

    def test_valid_mpd_returns_segment_urls(self, valid_mpd_xml: str):
        """Valid MPD with SegmentTimeline returns init URL, codec, and segment URLs."""
        init_url, codec, urls = parse_dash_mpd(valid_mpd_xml)
        assert init_url is not None
        assert codec is not None
        assert len(urls) >= 1
        assert all("seg-" in url for url in urls)
        assert "1.m4s" in urls[0]

    def test_mpd_without_timeline_uses_fallback(self):
        """MPD without SegmentTimeline but with Representation falls back."""
        mpd = """<?xml version="1.0"?>
        <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
          <Period>
            <AdaptationSet>
              <Representation>
                <SegmentTemplate media="seg-$Number$.m4s" startNumber="1"/>
              </Representation>
            </AdaptationSet>
          </Period>
        </MPD>"""
        init_url, codec, urls = parse_dash_mpd(mpd)
        assert init_url is None
        assert codec is None
        assert len(urls) >= 1
        assert "seg-1.m4s" in urls[0]

    def test_mpd_missing_segment_template_returns_empty(self):
        """MPD without SegmentTemplate returns empty list."""
        mpd = """<?xml version="1.0"?>
        <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
          <Period>
            <AdaptationSet>
              <Representation id="1"/>
            </AdaptationSet>
          </Period>
        </MPD>"""
        init_url, codec, urls = parse_dash_mpd(mpd)
        assert init_url is None
        assert codec is None
        assert urls == []

    def test_malformed_xml_returns_empty(self, malformed_xml: str):
        """Malformed XML returns empty list, does not crash."""
        init_url, codec, urls = parse_dash_mpd(malformed_xml)
        assert init_url is None
        assert codec is None
        assert urls == []

    def test_empty_string_returns_empty(self):
        """Empty string returns empty list."""
        init_url, codec, urls = parse_dash_mpd("")
        assert init_url is None
        assert codec is None
        assert urls == []

    def test_mpd_with_flac_codec_returns_codec(self):
        """MPD with codecs attribute returns codec string."""
        mpd = """<?xml version="1.0"?>
        <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
          <Period>
            <AdaptationSet mimeType="audio/mp4">
              <Representation id="1" codecs="flac">
                <SegmentTemplate initialization="init.mp4" media="seg-$Number$.m4s" startNumber="1"/>
              </Representation>
            </AdaptationSet>
          </Period>
        </MPD>"""
        init_url, codec, urls = parse_dash_mpd(mpd)
        assert init_url == "init.mp4"
        assert codec == "flac"
        assert urls == ["seg-1.m4s"]


# ─── DownloadTask tests ─────────────────────────────────────────────────────


class TestDownloadTask:
    """Tests for DownloadTask dataclass."""

    def test_defaults(self):
        """DownloadTask has sensible defaults."""
        task = DownloadTask(
            track_id=123,
            title="Test Track",
            artist="Test Artist",
            album="Test Album",
            duration=180,
        )
        assert task.status == "queued"
        assert task.progress == 0.0
        assert task.error == ""
        assert task.quality == "FLAC"
        assert task.cover_url == ""
        assert task.download_urls == []
        assert task.filepath == ""

    def test_update_status(self):
        """Task status can be updated."""
        task = DownloadTask(
            track_id=1,
            title="T",
            artist="A",
            album="Al",
            duration=60,
        )
        task.status = "downloading"
        task.progress = 50.0
        assert task.status == "downloading"
        assert task.progress == 50.0

    def test_download_urls_list(self):
        """download_urls starts as empty list."""
        task = DownloadTask(
            track_id=1,
            title="T",
            artist="A",
            album="Al",
            duration=60,
        )
        assert isinstance(task.download_urls, list)
        assert len(task.download_urls) == 0


# ─── Api tests ───────────────────────────────────────────────────────────────


class TestApi:
    """Tests for Api class using pytest-httpserver."""

    def _make_api(self, http_server: HTTPServer) -> Api:
        """Create Api instance pointing at test server."""
        a = Api()
        a.session.headers["User-Agent"] = "test"
        # Override the base URL to point at test server
        import gui_downloader

        original = gui_downloader.API_BASE
        gui_downloader.API_BASE = f"http://127.0.0.1:{http_server.port}"
        return a, original

    def _restore_base(self, original: str):
        import gui_downloader

        gui_downloader.API_BASE = original

    def test_ping_success(self, http_server: HTTPServer):
        """ping returns True when server responds 200."""
        http_server.expect_request("/").respond_with_json({"version": "1.0"})
        api, orig = self._make_api(http_server)
        try:
            assert api.ping() is True
        finally:
            self._restore_base(orig)

    def test_ping_failure(self):
        """ping returns False when server is unreachable."""
        bad_api = Api()
        bad_api.session.headers["User-Agent"] = "test"
        result = bad_api.ping()
        assert result is False

    def test_search_tracks(self, http_server: HTTPServer, sample_search_response: dict):
        """search_tracks returns items from API response."""
        http_server.expect_request("/search/").respond_with_json(sample_search_response)
        api, orig = self._make_api(http_server)
        try:
            items = api.search_tracks("daft punk", limit=10)
            assert len(items) == 1
            assert items[0]["title"] == "One More Time"
        finally:
            self._restore_base(orig)

    def test_search_tracks_empty(self, http_server: HTTPServer):
        """search_tracks returns empty list when no items."""
        http_server.expect_request("/search/").respond_with_json({"data": {"items": []}})
        api, orig = self._make_api(http_server)
        try:
            items = api.search_tracks("nonexistent")
            assert items == []
        finally:
            self._restore_base(orig)

    def test_get_manifest(self, http_server: HTTPServer, bts_manifest_b64: str):
        """get_manifest returns manifest data from /trackManifests/ endpoint."""
        manifest_resp = {
            "data": {
                "data": {
                    "attributes": {
                        "uri": "https://example.tidal.com/manifest/123.mpd",
                        "hash": "abc123==",
                        "formats": ["FLAC"],
                    }
                }
            }
        }
        http_server.expect_request("/trackManifests/").respond_with_json(manifest_resp)
        http_server.expect_request("/manifest/123.mpd").respond_with_json(
            {"manifest": bts_manifest_b64, "manifestMimeType": "application/vnd.tidal.bts"},
        )
        api, orig = self._make_api(http_server)
        try:
            result = api.get_manifest(62397812, "FLAC")
            assert "manifest" in result
            assert "manifestMimeType" in result
            assert "encryptionType" in result
        finally:
            self._restore_base(orig)

    def test_get_cover_url_returns_largest(
        self, http_server: HTTPServer, sample_cover_response: dict
    ):
        """get_cover_url returns the 1280px URL when available."""
        http_server.expect_request("/cover/").respond_with_json(sample_cover_response)
        api, orig = self._make_api(http_server)
        try:
            url = api.get_cover_url(8394857)
            assert "1280x1280" in url
        finally:
            self._restore_base(orig)

    def test_get_cover_url_fallback_to_640(self, http_server: HTTPServer):
        """get_cover_url falls back to 640px when 1280px not available."""
        response = {
            "covers": [
                {
                    "id": 1,
                    "name": "Album",
                    "640": "http://example.com/640.jpg",
                    "80": "http://example.com/80.jpg",
                },
            ]
        }
        http_server.expect_request("/cover/").respond_with_json(response)
        api, orig = self._make_api(http_server)
        try:
            url = api.get_cover_url(1)
            assert "640.jpg" in url
        finally:
            self._restore_base(orig)

    def test_get_cover_url_returns_none_on_empty(self, http_server: HTTPServer):
        """get_cover_url returns None when no covers."""
        http_server.expect_request("/cover/").respond_with_json({"covers": []})
        api, orig = self._make_api(http_server)
        try:
            url = api.get_cover_url(999)
            assert url is None
        finally:
            self._restore_base(orig)

    def test_fetch_cover_bytes(self, http_server: HTTPServer):
        """fetch_cover_bytes returns image bytes."""
        test_image = b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
        http_server.expect_request("/image.png").respond_with_data(
            test_image,
            content_type="image/png",
        )
        api, orig = self._make_api(http_server)
        try:
            content = api.fetch_cover_bytes(
                f"http://127.0.0.1:{http_server.port}/image.png",
            )
            assert content == test_image
        finally:
            self._restore_base(orig)

    def test_fetch_cover_bytes_returns_none_on_error(self, http_server: HTTPServer):
        """fetch_cover_bytes returns None on HTTP error."""
        http_server.expect_request("/missing.png").respond_with_json(
            {"error": "not found"},
            status=404,
        )
        api, orig = self._make_api(http_server)
        try:
            content = api.fetch_cover_bytes(
                f"http://127.0.0.1:{http_server.port}/missing.png",
            )
            assert content is None
        finally:
            self._restore_base(orig)


# ─── DownloadWorker utility tests ────────────────────────────────────────────


class TestDownloadWorkerUtilities:
    """Tests for DownloadWorker utility methods."""

    def _worker(self):
        """Create a minimal DownloadWorker for utility testing."""
        worker = DownloadWorker.__new__(DownloadWorker)
        # Call QObject.__init__ (required for PyQt6 objects)
        from PyQt6.QtCore import QObject

        QObject.__init__(worker)
        worker.api = MagicMock()
        worker.output_dir = Path("/tmp")
        return worker

    def test_detect_ext_flac(self):
        """FLAC codec returns .flac extension (not .m4a — FLAC not supported in MP4 container)."""
        worker = self._worker()
        flac_task = DownloadTask(
            track_id=1, title="T", artist="A", album="Al", duration=180, audio_codec="flac"
        )
        assert worker._detect_ext(flac_task) == ".flac"
        hires_task = DownloadTask(
            track_id=1, title="T", artist="A", album="Al", duration=180, audio_codec="flac"
        )
        assert worker._detect_ext(hires_task) == ".flac"

    def test_detect_ext_aac(self):
        """AAC codec returns .m4a extension."""
        worker = self._worker()
        aac_task = DownloadTask(
            track_id=1, title="T", artist="A", album="Al", duration=180, audio_codec="mp4a.40.2"
        )
        assert worker._detect_ext(aac_task) == ".m4a"
        heaac_task = DownloadTask(
            track_id=1, title="T", artist="A", album="Al", duration=180, audio_codec="mp4a.40.5"
        )
        assert worker._detect_ext(heaac_task) == ".m4a"

    def test_sanitize_removes_special_chars(self):
        """_sanitize removes filesystem-unsafe characters."""
        worker = self._worker()
        result = worker._sanitize('Track <1> / "quoted" | file?*')
        assert "<" not in result
        assert ">" not in result
        assert '"' not in result
        assert "\\" not in result
        assert "|" not in result
        assert "?" not in result
        assert "*" not in result

    def test_sanitize_strips_excess_whitespace(self):
        """_sanitize collapses multiple spaces."""
        worker = self._worker()
        result = worker._sanitize("Too   many    spaces")
        assert "  " not in result
        assert result == "Too many spaces"

    def test_sanitize_truncates_long_names(self):
        """_sanitize truncates names longer than 150 chars."""
        worker = self._worker()
        long_name = "A" * 200
        result = worker._sanitize(long_name)
        assert len(result) <= 150

    def test_avoid_collision_renames(self, tmp_path: Path):
        """_avoid_collision appends _1, _2 for existing files."""
        base = tmp_path / "artist - track.flac"
        base.touch()

        worker = self._worker()
        result = worker._avoid_collision(base)
        assert result == tmp_path / "artist - track_1.flac"

        (tmp_path / "artist - track_1.flac").touch()
        result2 = worker._avoid_collision(base)
        assert result2 == tmp_path / "artist - track_2.flac"

    def test_avoid_collision_no_conflict(self, tmp_path: Path):
        """_avoid_collision returns original path if file doesn't exist."""
        path = tmp_path / "new-file.flac"
        worker = self._worker()
        result = worker._avoid_collision(path)
        assert result == path


# ─── pytest-qt fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def qt_message_handler(capsys: "CaptureFixture"):
    """Capture Qt warning/error messages and print them to console.

    Installs a Qt message handler that prints all Qt messages to stdout.
    """
    from PyQt6.QtCore import Qt, qInstallMessageHandler

    messages: list[str] = []

    def handler(msg_type: Qt.MessageType, _context, message: str) -> None:
        messages.append(f"[QT {msg_type.name}] {message}")
        print(f"[QT {msg_type.name}] {message}")

    qInstallMessageHandler(handler)
    yield messages
    # No need to uninstall — test is over


# ─── GUI tests (pytest-qt) ──────────────────────────────────────────────────


class TestMainWindow:
    """GUI integration tests using pytest-qt."""

    def _make_window(self, qtbot, api_running: bool = False):
        """Create MainWindow, optionally mocking API ping."""
        from gui_downloader import MainWindow

        if api_running:
            with patch.object(
                MainWindow, "api", new_callable=lambda: MagicMock(ping=MagicMock(return_value=True))
            ):
                pass
        window = MainWindow()
        qtbot.addWidget(window)
        return window

    def test_show_error_dialog_logs_and_does_not_block(self, qtbot: "QtBot"):
        """show_error_dialog logs to console and does not block on QMessageBox."""
        from gui_downloader import MainWindow

        # Subclass that skips server startup (avoids subprocess hang in tests)
        class _TestMainWindow(MainWindow):
            def _start_server(self):
                pass

        window = _TestMainWindow()
        window.show()
        qtbot.addWidget(window)

        window.show_error_dialog("Test Title", "Test message body")
        # If we reach here without hanging, the test passes.

    def test_main_window_created(self, qtbot: "QtBot"):
        """MainWindow can be instantiated."""
        from gui_downloader import MainWindow

        window = MainWindow()
        window.show()
        qtbot.addWidget(window)
        assert window.isVisible()
        assert window.windowTitle() == "hifiT Downloader"

    def test_search_input_exists(self, qtbot: "QtBot"):
        """MainWindow has a search input field."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert isinstance(window.search_input, QLineEdit)
        assert window.search_input.placeholderText() != ""

    def test_search_button_exists(self, qtbot: "QtBot"):
        """MainWindow has a search button."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert isinstance(window.search_btn, QPushButton)
        assert window.search_btn.text() == "Search"

    def test_quality_combo_has_options(self, qtbot: "QtBot"):
        """Quality dropdown has all quality options."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert isinstance(window.quality_combo, QComboBox)
        assert window.quality_combo.count() == 4
        assert window.quality_combo.itemText(0) == "Hi-Res FLAC (FLAC_HIRES)"
        assert window.quality_combo.itemText(1) == "FLAC (LOSSLESS)"
        assert window.quality_combo.itemText(2) == "AAC 256kbps (AACLC)"
        assert window.quality_combo.itemText(3) == "AAC 96kbps (HEAACV1)"

    def test_results_tree_has_columns(self, qtbot: "QtBot"):
        """Results tree has correct column headers."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert isinstance(window.results_tree, QTreeWidget)
        assert window.results_tree.columnCount() == 5
        headers = [
            window.results_tree.headerItem().text(i)
            for i in range(window.results_tree.columnCount())
        ]
        assert headers == ["Track", "Artist", "Album", "Duration", "Quality"]

    def test_queue_tree_has_columns(self, qtbot: "QtBot"):
        """Queue tree has correct column headers."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert window.queue_tree.columnCount() == 5
        headers = [
            window.queue_tree.headerItem().text(i) for i in range(window.queue_tree.columnCount())
        ]
        assert headers == ["Track", "Artist", "Progress", "Status", "File"]

    def test_download_button_exists(self, qtbot: "QtBot"):
        """MainWindow has a download button."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert isinstance(window.download_btn, QPushButton)
        assert window.download_btn.text() == "Download Selected"

    def test_clear_button_exists(self, qtbot: "QtBot"):
        """MainWindow has a clear completed button."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert isinstance(window.clear_btn, QPushButton)
        assert window.clear_btn.text() == "Clear Completed"

    def test_progress_bar_exists(self, qtbot: "QtBot"):
        """MainWindow has a progress bar."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert window.download_progress.value() == 0
        assert window.download_progress.minimum() == 0
        assert window.download_progress.maximum() == 100

    def test_cover_label_exists(self, qtbot: "QtBot"):
        """MainWindow has a cover art label."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert isinstance(window.cover_label, QLabel)

    def test_browse_button_exists(self, qtbot: "QtBot"):
        """MainWindow has a browse button for output dir."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert isinstance(window.browse_btn, QPushButton)
        assert window.browse_btn.text() == "Browse..."

    def test_status_label_exists(self, qtbot: "QtBot"):
        """MainWindow has a status label."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        assert isinstance(window.status_label, QLabel)

    def test_api_check_shows_connected(self, qtbot: "QtBot", http_server: HTTPServer):
        """Status shows connected when API is reachable."""
        from gui_downloader import MainWindow

        http_server.expect_request("/").respond_with_json({"version": "1.0"})

        # Temporarily point API at test server
        import gui_downloader

        original = gui_downloader.API_BASE
        gui_downloader.API_BASE = f"http://127.0.0.1:{http_server.port}"
        try:
            window = MainWindow()
            qtbot.addWidget(window)
            assert "API connected" in window.status_label.text()
        finally:
            gui_downloader.API_BASE = original

    def test_api_check_shows_not_running(self, qtbot: "QtBot", monkeypatch: "MonkeyPatch"):
        """Status shows not running when API is unreachable."""
        from gui_downloader import MainWindow

        # Prevent _start_server from trying to launch main.py
        monkeypatch.setattr("gui_downloader.MainWindow._start_server", lambda self: None)

        window = MainWindow()
        qtbot.addWidget(window)
        assert "API not running" in window.status_label.text()

    def test_populate_results_adds_items(self, qtbot: "QtBot", sample_track_data: dict):
        """_populate_results adds QTreeWidgetItems for each track."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)

        window._populate_results_safe([sample_track_data])
        assert window.results_tree.topLevelItemCount() == 1

        item = window.results_tree.topLevelItem(0)
        assert item is not None
        assert item.text(0) == "One More Time"
        assert item.text(1) == "Daft Punk"
        assert item.text(2) == "Discovery"
        assert item.text(3) == "4:03"

    def test_deduplication_skips_duplicate_track_ids(self, qtbot: "QtBot", sample_track_data: dict):
        """_populate_results_safe skips tracks whose IDs were already seen."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)

        # First page: two unique tracks (fixture id is 62397812)
        page1 = [sample_track_data, {**sample_track_data, "id": 99, "title": "Second Track"}]
        window._populate_results_safe(page1)
        assert window.results_tree.topLevelItemCount() == 2

        # Second page: one duplicate of first track (same id=62397812) + one new
        page2 = [
            {**sample_track_data, "title": "Duplicate"},
            {**sample_track_data, "id": 100, "title": "Third Track"},
        ]
        window._is_load_more = True
        window._populate_results_safe(page2)
        # Page-by-page: tree shows only page 2's non-duplicate items
        assert window.results_tree.topLevelItemCount() == 1
        assert window.results_tree.topLevelItem(0).text(0) == "Third Track"

    def test_page_navigation_displays_cached_page(self, qtbot: "QtBot", sample_track_data: dict):
        """Going back to a previous page restores its cached items."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)

        # Simulate page 1
        page1 = [
            {**sample_track_data, "id": 1, "title": "Track A"},
            {**sample_track_data, "id": 2, "title": "Track B"},
        ]
        window._current_page = 0
        window._search_limit = 25
        window._search_query = "test"
        window._populate_results_safe(page1)
        assert window.results_tree.topLevelItemCount() == 2
        assert window.results_tree.topLevelItem(0).text(0) == "Track A"

        # Simulate page 2
        page2 = [
            {**sample_track_data, "id": 3, "title": "Track C"},
        ]
        window._current_page = 1
        window._is_load_more = True
        window._populate_results_safe(page2)
        # Page-by-page: tree shows only page 2's items
        assert window.results_tree.topLevelItemCount() == 1
        assert window.results_tree.topLevelItem(0).text(0) == "Track C"

        # Go back to page 1 (from cache)
        window._on_prev_page()
        assert window._current_page == 0
        assert window.results_tree.topLevelItemCount() == 2
        assert window.results_tree.topLevelItem(0).text(0) == "Track A"
        assert window.results_tree.topLevelItem(1).text(0) == "Track B"

    def test_sort_indicator_updates_on_header_click(self, qtbot: "QtBot"):
        """Clicking a result header column sets the sort indicator."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)

        header = window.results_tree.header()

        window._on_results_header_clicked(0)  # click Track column
        assert window._results_sort_column == 0
        assert window._results_sort_mode == "asc"
        assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder

        window._on_results_header_clicked(0)  # cycle to desc
        assert window._results_sort_column == 0
        assert window._results_sort_mode == "desc"
        assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder

        window._on_results_header_clicked(0)  # cycle to relevance
        assert window._results_sort_column == -1
        assert window._results_sort_mode == "relevance"

        window._on_results_header_clicked(1)  # click Artist column
        assert window._results_sort_column == 1
        assert window._results_sort_mode == "asc"
        assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder

    def test_add_to_queue_adds_item(self, qtbot: "QtBot"):
        """_add_to_queue adds a row to the queue tree."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)

        task = DownloadTask(
            track_id=1,
            title="Test",
            artist="Artist",
            album="Album",
            duration=120,
        )
        source = QTreeWidgetItem(["Test", "Artist", "Album", "2:00", "", "Ready"])
        window._add_to_queue(task, source)

        assert window.queue_tree.topLevelItemCount() == 1
        queue_item = window.queue_tree.topLevelItem(0)
        assert queue_item.text(0) == "Test"
        assert queue_item.text(2) == "0%"
        assert queue_item.text(3) == "Queued"

    def test_on_download_progress_updates_bar(self, qtbot: "QtBot"):
        """on_download_progress updates the progress bar."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)

        window.on_download_progress(75, "Downloading... 75%")
        assert window.download_progress.value() == 75

    def test_quality_combo_saves_config(
        self, qtbot: "QtBot", tmp_path: Path, monkeypatch: "MonkeyPatch"
    ):
        """Changing quality saves config file."""
        from gui_downloader import MainWindow

        config_path = tmp_path / "gui_config.json"
        monkeypatch.setattr("gui_downloader.CONFIG_FILE", config_path)

        window = MainWindow()
        qtbot.addWidget(window)

        window.quality_combo.setCurrentIndex(2)  # AAC 256kbps
        window._on_quality_changed(2)

        config = json.loads(config_path.read_text())
        assert config["quality"] == "AACLC"

    def test_browse_output_updates_dir(
        self, qtbot: "QtBot", tmp_path: Path, monkeypatch: "MonkeyPatch"
    ):
        """_browse_output updates the output directory."""
        from gui_downloader import MainWindow

        config_path = tmp_path / "gui_config.json"
        monkeypatch.setattr("gui_downloader.CONFIG_FILE", config_path)

        mock_dir = str(tmp_path / "music")
        tmp_path.joinpath("music").mkdir()

        window = MainWindow()
        qtbot.addWidget(window)

        with patch.object(QFileDialog, "getExistingDirectory", return_value=mock_dir):
            window._browse_output()

        assert window.output_dir == mock_dir
        assert window.dir_display.text() == mock_dir

    def test_clear_completed_removes_done(self, qtbot: "QtBot"):
        """clear_completed removes completed and failed tasks from queue."""
        from gui_downloader import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)

        # Add some tasks with different statuses
        for title, status in [
            ("Done 1", "completed"),
            ("Done 2", "failed"),
            ("Active", "downloading"),
        ]:
            task = DownloadTask(
                track_id=hash(title) % 10000,
                title=title,
                artist="A",
                album="Al",
                duration=60,
                status=status,
            )
            item = QTreeWidgetItem([title, "A", "100%", status, ""])
            window.queue_tree.addTopLevelItem(item)
            window.queue_rows.append((item, task))

        window.clear_completed()

        assert window.queue_tree.topLevelItemCount() == 1
        remaining = window.queue_tree.topLevelItem(0)
        assert remaining.text(0) == "Active"

    def test_config_persistence(self, qtbot: "QtBot", tmp_path: Path, monkeypatch: "MonkeyPatch"):
        """Config is loaded from and saved to file."""
        from gui_downloader import MainWindow

        config_path = tmp_path / "gui_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "output_dir": str(tmp_path / "music"),
                    "quality": "AACLC",
                }
            )
        )
        monkeypatch.setattr("gui_downloader.CONFIG_FILE", config_path)

        window = MainWindow()
        qtbot.addWidget(window)

        assert window.output_dir == str(tmp_path / "music")
        assert window.quality == "AACLC"

        # Verify quality combo reflects loaded value
        current_idx = window.quality_combo.currentIndex()
        assert window.quality_combo.itemData(current_idx) == "AACLC"


# ─── DownloadWorker _fetch_manifest tests ────────────────────────────────────


class TestDownloadWorkerFetchManifest:
    """Tests for DownloadWorker._fetch_manifest logic."""

    def _make_worker(self):
        """Create a worker with a mock API."""
        worker = DownloadWorker.__new__(DownloadWorker)
        from PyQt6.QtCore import QObject

        QObject.__init__(worker)
        worker.api = MagicMock()
        worker.output_dir = Path("/tmp")
        return worker

    def test_fetch_manifest_non_drm(self, bts_manifest_b64: str):
        """Non-DRM manifest sets download_urls on task."""
        worker = self._make_worker()
        worker.api.get_manifest.return_value = {
            "manifest": bts_manifest_b64,
            "manifestMimeType": "application/vnd.tidal.bts",
            "download_urls": [],
            "encryptionType": "NONE",
        }

        task = DownloadTask(
            track_id=1,
            title="T",
            artist="A",
            album="Al",
            duration=60,
        )
        worker._fetch_manifest(task)

        assert task.download_urls == ["https://stream.tidal.com/track/12345.flac?token=abc"]

    def test_fetch_manifest_drm_emits_failed(self, drm_manifest_b64: str):
        """DRM manifest sets drm_locked status and emits task_failed."""
        worker = self._make_worker()
        worker.api.get_manifest.return_value = {
            "manifest": drm_manifest_b64,
            "manifestMimeType": "application/vnd.tidal.bts",
            "download_urls": [],
            "encryptionType": "WIDEVINE",
        }

        task = DownloadTask(
            track_id=1,
            title="T",
            artist="A",
            album="Al",
            duration=60,
        )
        failed_emitted = False

        def on_failed(t):
            nonlocal failed_emitted
            failed_emitted = True

        worker.task_failed.connect(on_failed)
        worker._fetch_manifest(task)

        assert task.status == "drm_locked"
        assert "DRM" in task.error
        assert failed_emitted is True

    def test_fetch_manifest_no_manifest_raises(self):
        """Empty manifest raises ValueError."""
        worker = self._make_worker()
        worker.api.get_manifest.return_value = {
            "manifest": "",
            "manifestMimeType": "",
            "download_urls": [],
            "encryptionType": "NONE",
        }

        task = DownloadTask(
            track_id=1,
            title="T",
            artist="A",
            album="Al",
            duration=60,
        )
        with pytest.raises(ValueError, match="No manifest found"):
            worker._fetch_manifest(task)

    def test_fetch_manifest_with_encryption_type(self, bts_manifest_b64: str):
        """Worker reads manifest from new get_manifest() return format."""
        worker = self._make_worker()
        worker.api.get_manifest.return_value = {
            "manifest": bts_manifest_b64,
            "manifestMimeType": "application/vnd.tidal.bts",
            "download_urls": [],
            "encryptionType": "NONE",
        }

        task = DownloadTask(
            track_id=1,
            title="T",
            artist="A",
            album="Al",
            duration=60,
        )
        worker._fetch_manifest(task)

        assert task.download_urls == ["https://stream.tidal.com/track/12345.flac?token=abc"]
