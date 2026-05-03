# hifi-api-gui

A PyQt6 desktop application for downloading lossless music from Tidal, built on [hifi-api](https://github.com/binimum/hifi-api) by [binimum](https://github.com/binimum).

![GUI Preview](https://github.com/HyperRamzey/hifi-api-gui/blob/main/.github/image.png)

> [!WARNING]
> Tidal has begun blocking accounts en masse. This affects not only users of this API but also other providers such as lucide.to. There is currently no solution -- this includes homelab users who don't expose their API to the Internet. Use at your own risk.

## Features

- **Search Tidal** by track, artist, album, video, playlist, or ISRC
- **Download in lossless quality** -- Hi-Res FLAC, FLAC (CD quality), AAC 256kbps, AAC 96kbps
- **Automatic quality fallback** -- if your preferred quality isn't available, it downgrades gracefully
- **Queue management** -- add tracks to queue, download individually or all at once
- **Cover art embedding** -- album artwork is automatically embedded into your downloaded files
- **Built-in API server** -- the GUI auto-starts the backend server; no separate setup needed
- **Proxy support** -- rotate through proxies with configurable retry logic
- **OLED pure-black theme** -- dark UI that's easy on the eyes

## Prerequisites

- **Python 3.10+**
- **ffmpeg** -- required for DASH segment concatenation and audio remuxing. [Download here](https://ffmpeg.org/download.html)
- **Tidal HiFi or HiFi Plus subscription**

## Quick Start

### 1. Authenticate

Run the authentication script to obtain your Tidal OAuth tokens:

```bash
cd tidal_auth
pip install -r requirements.txt
python tidal_auth.py
```

Follow the browser instructions. Your credentials will be saved to `token.json`.

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Launch the GUI

```bash
python gui_downloader.py
```

The API server will start automatically. Search for music, queue tracks, and download.

## Configuration

Copy `.env.example` to `.env` and fill in your credentials. Alternatively, tokens loaded from `token.json` are used automatically.

| Variable | Default | Description |
|---|---|---|
| `CLIENT_ID` | | Tidal API client ID |
| `CLIENT_SECRET` | | Tidal API client secret |
| `USER_ID` | | Tidal user ID |
| `REFRESH_TOKEN` | | Tidal OAuth2 refresh token |
| `COUNTRY_CODE` | `US` | Tidal country code |
| `USE_PROXIES` | `False` | Enable proxy support |
| `ROTATE_PROXIES_ON_REFRESH` | `False` | Rotate proxy when refreshing tokens |
| `PROXIES_FILE` | `proxies.txt` | Path to proxy list (one per line) |
| `MAX_RETRIES` | `2` | Retry attempts per request |
| `FALLBACK_TO_DIRECT_CONNECTION` | `False` | **WARNING:** Exposes your IP if all proxies fail |
| `USER_AGENT` | Android 14 (Samsung S24) | Override the device User-Agent |
| `DEV_MODE` | `False` | Enable verbose upstream request logging |

### Proxy Configuration

Create a `proxies.txt` file with one proxy per line:

```
http://user:pass@hostname:port
https://user:pass@hostname:port
```

Set `USE_PROXIES=True` in your `.env` file to enable.

## Download Quality

The app supports four quality tiers, tried in this order:

1. **Hi-Res FLAC** (FLAC_HIRES) -- up to 24-bit/192kHz
2. **FLAC** (LOSSLESS) -- CD quality (16-bit/44.1kHz)
3. **AAC 256kbps** (HIGH)
4. **AAC 96kbps** (LOW)

If a track isn't available in your selected quality, the app automatically downgrades to the next available tier.

> [!NOTE] DRM-protected content
> Decoding DRM-locked tracks (HI_RES_LOSSLESS / Dolby Atmos) is **not yet implemented**. If a track is DRM-protected, the app falls back to the highest available non-DRM quality (FLAC LOSSLESS or lower).

## API Server (Standalone)

The API server can also run standalone without the GUI:

```bash
python main.py
```

It listens on `0.0.0.0:8000` by default. See the original [hifi-api](https://github.com/binimum/hifi-api) repository for the full API endpoint documentation.

## Docker

Build and run the API server with Docker:

```bash
docker-compose up
```

The server will be available at `http://localhost:8000`.

## Security

- **Never commit `token.json`, `.env`, or `proxies.txt`** -- these are in `.gitignore`
- Set `FALLBACK_TO_DIRECT_CONNECTION=False` unless you intentionally want your IP exposed
- When running the API server standalone, bind to `127.0.0.1` instead of `0.0.0.0` if you don't need external access

## Credits

- Original API: [sachinsenal0x64/hifi](https://github.com/sachinsenal0x64/hifi)
- API server: [binimum/hifi-api](https://github.com/binimum/hifi-api)
- GUI inspired by [qobuz-downloader](https://github.com/CarloPozzoni/qobuz-downloader)
