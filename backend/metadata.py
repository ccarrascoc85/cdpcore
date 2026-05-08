"""
MusicBrainz metadata lookup + cover art caching.
Rate limit: 1 request/second (MB policy).
"""
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional, List, Tuple

import httpx
import musicbrainzngs

logger = logging.getLogger(__name__)

CACHE_DIR = Path(os.environ.get("CD_CACHE_DIR", "/tmp/cd-cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MB_APP = "cdpcore"
MB_VERSION = "1.0"
MB_CONTACT = "https://github.com/ccarrascoc85/cdpcore"

musicbrainzngs.set_useragent(MB_APP, MB_VERSION, MB_CONTACT)

_last_mb_call: float = 0.0
_MB_RATE_LIMIT = 1.1  # seconds between calls


def _mb_rate_limit():
    global _last_mb_call
    elapsed = time.monotonic() - _last_mb_call
    if elapsed < _MB_RATE_LIMIT:
        time.sleep(_MB_RATE_LIMIT - elapsed)
    _last_mb_call = time.monotonic()


# ---------------------------------------------------------------------------
# MusicBrainz queries
# ---------------------------------------------------------------------------

def lookup_disc(disc_id: str) -> Optional[dict]:
    """
    Query MusicBrainz for a disc ID.
    Returns dict with album, artist, tracks, cover_url or None on failure.
    """
    cache_key = _cache_path(f"mb_{disc_id}.json")
    cached = _load_json_cache(cache_key)
    if cached:
        logger.info(f"MB metadata from cache for disc {disc_id}")
        return cached

    _mb_rate_limit()
    try:
        result = musicbrainzngs.get_releases_by_discid(
            disc_id,
            includes=["artists", "recordings", "release-groups"],
        )
    except musicbrainzngs.ResponseError as e:
        if "404" in str(e):
            logger.warning(f"Disc {disc_id} not found in MusicBrainz")
        else:
            logger.error(f"MusicBrainz error: {e}")
        return None
    except Exception as e:
        logger.error(f"MusicBrainz request failed: {e}")
        return None

    releases = result.get("disc", {}).get("release-list", [])
    if not releases:
        releases = result.get("release-list", [])
    if not releases:
        logger.warning(f"No releases for disc {disc_id}")
        return None

    release = releases[0]
    metadata = _parse_release(release, disc_id)
    _save_json_cache(cache_key, metadata)
    return metadata


def _parse_release(release: dict, disc_id: str) -> dict:
    """Extract relevant fields from a MB release dict."""
    title = release.get("title", "Unknown Album")

    artist_credits = release.get("artist-credit", [])
    artist = _flatten_artist_credit(artist_credits) or "Unknown Artist"

    tracks = _extract_tracks(release, disc_id)
    cover_url = _build_cover_art_url(release.get("id", ""))

    return {
        "album": title,
        "artist": artist,
        "tracks": tracks,
        "cover_url": cover_url,
        "mb_release_id": release.get("id"),
    }


def _flatten_artist_credit(credits: list) -> str:
    parts = []
    for item in credits:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            name = item.get("artist", {}).get("name", "")
            join = item.get("joinphrase", "")
            parts.append(name + join)
    return "".join(parts).strip()


def _extract_tracks(release: dict, disc_id: str) -> List[dict]:
    """Extract track list from a release, matching the correct disc."""
    medium_list = release.get("medium-list", [])
    for medium in medium_list:
        for disc in medium.get("disc-list", []):
            if disc.get("id") == disc_id:
                return _parse_track_list(medium.get("track-list", []))

    # Fall back to first medium
    if medium_list:
        return _parse_track_list(medium_list[0].get("track-list", []))

    return []


def _parse_track_list(track_list: list) -> List[dict]:
    tracks = []
    for t in track_list:
        recording = t.get("recording", {})
        title = recording.get("title") or t.get("title", f"Track {t.get('number', '?')}")
        length_ms = recording.get("length") or t.get("length")
        duration = int(length_ms) // 1000 if length_ms else 0
        number = int(t.get("number", 0))
        tracks.append({"number": number, "title": title, "duration": duration})
    return sorted(tracks, key=lambda x: x["number"])


def _build_cover_art_url(release_id: str) -> Optional[str]:
    if not release_id:
        return None
    return f"https://coverartarchive.org/release/{release_id}/front"


# ---------------------------------------------------------------------------
# GnuDB (CDDB) fallback
# ---------------------------------------------------------------------------

_GNUDB_BASE  = "http://gnudb.gnudb.org/~cddb/cddb.cgi"
_GNUDB_HELLO = "user+cdpcore+cdpcore+1.0"


def lookup_disc_gnudb(cddb_info: dict) -> Optional[dict]:
    """
    Query GnuDB (CDDB protocol) as a fallback when MusicBrainz has no match.
    Returns same format dict as lookup_disc() but without cover_url.
    """
    if not cddb_info:
        return None

    freedb_id = cddb_info["freedb_id"]
    cache_key = _cache_path(f"gnudb_{freedb_id}.json")
    cached = _load_json_cache(cache_key)
    if cached:
        logger.info(f"GnuDB metadata from cache for disc {freedb_id}")
        return cached

    offsets = "+".join(str(o) for o in cddb_info["offsets"])
    query_url = (
        f"{_GNUDB_BASE}?cmd=cddb+query+{freedb_id}"
        f"+{cddb_info['track_count']}+{offsets}+{cddb_info['seconds']}"
        f"&hello={_GNUDB_HELLO}&proto=6"
    )

    try:
        with httpx.Client(timeout=10) as client:
            lines = client.get(query_url).text.splitlines()
    except Exception as e:
        logger.error(f"GnuDB query failed: {e}")
        return None

    if not lines:
        return None

    code = lines[0].split()[0] if lines[0].split() else ""
    if code == "202":
        logger.info(f"GnuDB: no match for disc {freedb_id}")
        return None
    if code not in ("200", "210", "211"):
        logger.warning(f"GnuDB unexpected response: {lines[0]}")
        return None

    # Pick category + disc ID from response
    # code 200: "200 category discid title..."
    # code 210/211: lines[1] = "category discid title..."
    if code == "200":
        parts = lines[0].split()
        if len(parts) < 3:
            return None
        category, match_id = parts[1], parts[2]
    else:
        if len(lines) < 2:
            return None
        parts = lines[1].split()
        if len(parts) < 2:
            return None
        category, match_id = parts[0], parts[1]

    read_url = (
        f"{_GNUDB_BASE}?cmd=cddb+read+{category}+{match_id}"
        f"&hello={_GNUDB_HELLO}&proto=6"
    )
    try:
        with httpx.Client(timeout=10) as client:
            content = client.get(read_url).text
    except Exception as e:
        logger.error(f"GnuDB read failed: {e}")
        return None

    result = _parse_cddb_entry(content, cddb_info["track_count"])
    if result:
        logger.info(f"GnuDB match: '{result['album']}' by '{result['artist']}'")
        _save_json_cache(cache_key, result)
    return result


def _parse_cddb_entry(content: str, track_count: int) -> Optional[dict]:
    """Parse a CDDB database entry into a metadata dict."""
    fields: dict = {}
    ttitles: dict = {}

    for line in content.splitlines():
        if line.startswith("#") or line.strip() == ".":
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in ("DTITLE", "DYEAR", "DGENRE"):
            fields[key] = fields.get(key, "") + value
        elif key.startswith("TTITLE"):
            try:
                n = int(key[6:])
                ttitles[n] = ttitles.get(n, "") + value
            except ValueError:
                pass

    if "DTITLE" not in fields:
        return None

    dtitle = fields["DTITLE"]
    if " / " in dtitle:
        artist, album = dtitle.split(" / ", 1)
    else:
        artist, album = "Unknown Artist", dtitle

    tracks = [
        {"number": i + 1, "title": ttitles.get(i, f"Track {i + 1}"), "duration": 0}
        for i in range(track_count)
    ]

    return {
        "album":         album.strip(),
        "artist":        artist.strip(),
        "tracks":        tracks,
        "cover_url":     None,
        "mb_release_id": None,
    }


# ---------------------------------------------------------------------------
# iTunes cover art search (fallback when no MusicBrainz cover URL)
# ---------------------------------------------------------------------------

def fetch_itunes_cover_url(artist: str, album: str) -> Optional[str]:
    """
    Search the iTunes Search API for a cover art URL.
    Free, no API key required. Returns a 1000x1000 JPEG URL or None.
    """
    import urllib.parse
    query = urllib.parse.quote(f"{artist} {album}")
    url = f"https://itunes.apple.com/search?term={query}&entity=album&limit=5"
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            data = client.get(url).json()
        for result in data.get("results", []):
            artwork = result.get("artworkUrl100", "")
            if artwork:
                # Scale up from 100x100 to 1000x1000
                return artwork.replace("100x100bb", "1000x1000bb")
    except Exception as e:
        logger.warning(f"iTunes cover search failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Cover art caching
# ---------------------------------------------------------------------------

def fetch_and_cache_cover(cover_url: str, disc_id: str) -> Optional[Path]:
    """
    Download cover art from cover_url and store locally.
    Returns local path or None on failure.
    """
    if not cover_url:
        return None

    local_path = CACHE_DIR / f"cover_{disc_id}.jpg"
    if local_path.exists():
        return local_path

    try:
        _mb_rate_limit()
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            resp = client.get(cover_url)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
            logger.info(f"Cover art cached at {local_path}")
            return local_path
    except Exception as e:
        logger.error(f"Cover art download failed: {e}")
        return None


# ---------------------------------------------------------------------------
# JSON cache helpers
# ---------------------------------------------------------------------------

def _cache_path(filename: str) -> Path:
    return CACHE_DIR / filename


def _load_json_cache(path: Path) -> Optional[dict]:
    if path.exists():
        import json
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None


def _save_json_cache(path: Path, data: dict):
    import json
    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning(f"Cache write failed: {e}")
