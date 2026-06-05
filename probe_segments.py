"""Download one track's DASH segments and probe them with ffprobe."""
import json
import subprocess
import sys
from pathlib import Path

import requests

API_BASE = "http://127.0.0.1:8000"
TRACK_ID = 58623485  # ZippO - Куришь часто (from the logs)
OUTPUT_DIR = Path(r"E:\xlam")
KEEP_TEMP = True


def main():
    # 1. Get manifest
    print("[1/5] Fetching manifest...")
    r = requests.get(f"{API_BASE}/trackManifests/", params={"id": TRACK_ID, "formats": "FLAC"})
    r.raise_for_status()
    data = r.json()
    inner = data.get("data", {}).get("data", data.get("data", {}))
    attrs = inner.get("attributes", inner)

    if "uri" in attrs:
        print(f"  URI-based manifest: {attrs['uri'][:80]}...")
        manifest_resp = requests.get(attrs["uri"], timeout=30)
        manifest_resp.raise_for_status()
        mpd_xml = manifest_resp.text
    elif "manifest" in attrs:
        print(f"  Base64 manifest: {len(attrs['manifest'])} bytes")
        import base64
        manifest_bytes = base64.b64decode(attrs["manifest"])
        mpd_xml = manifest_bytes.decode("utf-8")
    else:
        print("ERROR: No manifest data found")
        sys.exit(1)

    # 2. Parse MPD
    print("[2/5] Parsing MPD...")
    import xml.etree.ElementTree as ET
    NS = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
    root = ET.fromstring(mpd_xml)
    period = root.find(".//mpd:Period", NS)
    seg_template = period.find(".//mpd:SegmentTemplate", NS)
    init_url = seg_template.get("initialization")
    media_url = seg_template.get("media", "")
    start_num = int(seg_template.get("startNumber", "1"))

    # Parse SegmentTimeline
    timeline = seg_template.find("mpd:SegmentTimeline", NS)
    segments = []
    if timeline is not None:
        S = timeline.findall("mpd:S", NS)
        time_offset = 0
        for s_elem in S:
            d = int(s_elem.get("d"))
            r = int(s_elem.get("r", "0"))
            segments.append((d, r))

    print(f"  Init URL: {init_url[:80]}...")
    print(f"  Media URL pattern: {media_url[:80]}...")
    print(f"  Start number: {start_num}")
    print(f"  Timeline entries: {len(segments)}")

    # Calculate total segments
    total = 0
    for d, r in segments:
        total += 1 + r
    print(f"  Total media segments: {total}")

    # 3. Download segments
    print("[3/5] Downloading segments...")
    tmp_dir = OUTPUT_DIR / f".tmp_probe_{TRACK_ID}"
    tmp_dir.mkdir(exist_ok=True)

    session = requests.Session()

    # Download init segment
    r = session.get(init_url, timeout=60)
    r.raise_for_status()
    init_path = tmp_dir / "init.mp4"
    init_path.write_bytes(r.content)
    print(f"  Init: {init_path.name} = {init_path.stat().st_size:,} bytes")

    # Download media segments
    seg_paths = []
    for i in range(total):
        url = media_url.replace("$Number$", str(start_num + i))
        seg_path = tmp_dir / f"seg_{i + 1:04d}.mp4"
        r = session.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(seg_path, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
        seg_paths.append(seg_path)
        if (i + 1) % 10 == 0 or i == total - 1:
            print(f"  Downloaded {i + 1}/{total} segments")

    print(f"  All {total} segments downloaded to {tmp_dir}")

    # 4. Probe with ffprobe
    print("[4/5] Probing segments with ffprobe...")
    for idx, seg_path in enumerate([init_path] + seg_paths[:3]):  # probe init + first 3 media
        print(f"\n  === {seg_path.name} ({seg_path.stat().st_size:,} bytes) ===")
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_format", "-show_streams",
                "-print_format", "json",
                str(seg_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            probe = json.loads(result.stdout)
            stream = probe.get("streams", [{}])[0]
            fmt = probe.get("format", {})
            print(f"    Codec: {stream.get('codec_name', '?')}")
            print(f"    Codec type: {stream.get('codec_type', '?')}")
            print(f"    Sample rate: {stream.get('sample_rate', '?')}")
            print(f"    Channels: {stream.get('channels', '?')}")
            print(f"    Bits per raw sample: {stream.get('bits_per_raw_sample', '?')}")
            print(f"    Bits per channel: {stream.get('bits_per_channel', '?')}")
            print(f"    Format: {fmt.get('format_name', '?')}")
            print(f"    Duration: {fmt.get('duration', '?')}")
            print(f"    Bitrate: {fmt.get('bit_rate', '?')}")

            # Check MP4 boxes
            result2 = subprocess.run(
                ["ffprobe", "-v", "info", str(seg_path)],
                capture_output=True, text=True, timeout=10,
            )
            print(f"    ffprobe info: {result2.stdout.strip()[:300]}")
        else:
            print(f"    ffprobe error: {result2.stderr[:200]}")

    # 5. Try ffmpeg concat (current buggy approach)
    print("\n[5/5] Testing current ffmpeg concat approach...")
    concat_list = tmp_dir / "concat.txt"
    with open(concat_list, "w") as f:
        f.write(f"file '{init_path.name}'\n")
        for sp in seg_paths:
            f.write(f"file '{sp.name}'\n")

    output = tmp_dir / "output_buggy.m4a"
    result = subprocess.run(
        ["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c", "copy", "-y", str(output)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    print(f"    Return code: {result.returncode}")
    print(f"    Stderr (first 500 chars): {result.stderr[:500]}")

    print(f"\n[DONE] Temp files kept at: {tmp_dir}")
    print(f"  Run: ffprobe -v info {tmp_dir}/seg_0001.mp4")
    print(f"  KEEP_TEMP = True in gui_downloader.py to keep GUI downloads too")


if __name__ == "__main__":
    main()
