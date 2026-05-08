"""
CDPcore — FastAPI backend
Exposes REST API on localhost:8000 for CD playback control.
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import List, Optional

import json
import subprocess
import threading
import time

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import socket
import system_info
import network as net_mgr

import auth
import cd_reader
import metadata
import player
from state import CDState, CDPlayerState, TrackInfo, get_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
logger = logging.getLogger("cd_player")

app = FastAPI(title="CDPcore Backend", version="1.0.0")
app.mount("/favicon", StaticFiles(directory="favicon"), name="favicon")


@app.exception_handler(ValueError)
def _value_error_handler(request: Request, exc: ValueError):
    """Map validator ValueErrors (network.py shape checks, etc.) to 400.
    Keeps API handlers free of per-call try/except boilerplate."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
# Defensive headers applied to every response. Kept narrow on purpose:
# the appliance UI uses inline scripts and styles; making CSP stricter would
# require refactoring the HTML files. The headers below close the easy holes
# (clickjacking, MIME sniffing, referrer leakage) without breaking the UI.
# Cover art comes from third-party HTTPS hosts (Cover Art Archive, iTunes),
# so img-src must allow `https:`.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = _CSP
    return response


def _verify_same_origin(request: Request) -> None:
    """Reject cross-origin browser POSTs to the most sensitive admin endpoints.

    Defense in depth alongside SameSite=Strict cookies. The session cookie
    already prevents cookie-bearing cross-origin POSTs from succeeding;
    this check protects the *cookie-less* sensitive endpoints — /admin/unlock
    (sets the cookie) and /admin/setup (one-shot first-boot) — from being
    invoked by a malicious page on the LAN.

    Browsers always send Origin (and usually Referer) on cross-origin POST.
    CLI tools (curl, the appliance's own tooling) typically don't. The
    contract: if Origin or Referer is present and doesn't match the Host
    the request was made to, reject. If neither is present, allow — that
    pattern matches CLI/script use, which is appliance-friendly and the
    primary defense for those callers is local network reachability anyway.
    """
    from urllib.parse import urlparse
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return
    for header in ("origin", "referer"):
        raw = (request.headers.get(header) or "").strip()
        if not raw:
            continue
        try:
            netloc = urlparse(raw).netloc.lower()
        except Exception:
            raise HTTPException(status_code=403, detail=f"invalid_{header}")
        if netloc and netloc != host:
            raise HTTPException(status_code=403, detail="cross_origin_forbidden")
        # Origin is the strongest signal; if present and matches, accept and
        # don't second-guess via Referer (which may be omitted by policy).
        if header == "origin":
            return

_alsa_devices: List[dict] = []
_audio_snapshot: dict = {}  # last-broadcast audio summary; used for change-detection
_audio_lock = threading.Lock()
_event_loop: asyncio.AbstractEventLoop | None = None  # set at startup; used by non-async threads


def _compute_audio_state(devices: list, alsa_device: str) -> str:
    """Classify current audio situation for the UI."""
    if not devices:
        return "device_missing"
    usb = [d for d in devices if d.get("is_usb")]
    if len(usb) == 1:
        return "usb_single"
    if len(usb) > 1:
        return "usb_multiple"
    return "no_usb"


def _refresh_audio_devices(reason: str = "manual") -> dict:
    """Central entry point for all audio device refresh operations.

    Updates _alsa_devices, selects the best available device, updates
    state.audio_state, and triggers a WebSocket broadcast when the
    audio snapshot changes. Safe to call from any thread or async context.
    """
    global _alsa_devices, _audio_snapshot

    should_broadcast = False
    with _audio_lock:
        state = get_state()
        prev_device = state.alsa_device

        new_devices = cd_reader.enumerate_alsa_devices()
        _alsa_devices = new_devices

        usb_devices = [d for d in new_devices if d.get("is_usb")]
        device_ids = {d["id"] for d in new_devices}

        if len(usb_devices) == 1:
            state.alsa_device = usb_devices[0]["id"]
            state.audio_state = "usb_single"
        elif len(usb_devices) == 0:
            if state.alsa_device not in device_ids and new_devices:
                state.alsa_device = new_devices[0]["id"]
            state.audio_state = "no_usb" if new_devices else "device_missing"
        else:
            # Multiple USB DACs require an explicit stable selection. Preserve
            # the current value; never choose an arbitrary USB or internal output.
            state.audio_state = "usb_multiple"

        for d in new_devices:
            d["auto_selected"] = d["id"] == state.alsa_device

        if state.alsa_device != prev_device:
            logger.info(f"[audio] device {prev_device!r} -> {state.alsa_device!r} (reason={reason})")

        snapshot = {
            "alsa_device": state.alsa_device,
            "audio_state": state.audio_state,
            "device_count": len(new_devices),
        }
        if snapshot != _audio_snapshot:
            _audio_snapshot = snapshot
            should_broadcast = True

    if should_broadcast:
        logger.info(f"[audio] snapshot: {snapshot} (reason={reason})")
        _schedule_broadcast()

    return snapshot


async def _audio_reconciliation_loop():
    """Slow background reconciliation: catches missed udev events and
    backend-restarts-with-DAC-already-connected scenarios."""
    while True:
        await asyncio.sleep(30)
        try:
            _refresh_audio_devices("reconciliation")
        except Exception as e:
            logger.warning(f"[audio] reconciliation error: {e}")

# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------

_ws_clients: set = set()


async def _broadcast(data: dict):
    """Send a state snapshot to all connected WebSocket clients."""
    if not _ws_clients:
        return
    msg = json.dumps(data)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)  # in-place — avoids UnboundLocalError from -= reassignment


async def _broadcast_state():
    state = get_state()
    _sync_player_status(state)
    await _broadcast(state.to_status_dict())


def _schedule_broadcast():
    """Schedule a state broadcast from a non-async context (e.g. player thread)."""
    if _event_loop is not None:
        asyncio.run_coroutine_threadsafe(_broadcast_state(), _event_loop)


async def _state_broadcaster():
    """Push state to WS clients every second — handles elapsed-time updates."""
    while True:
        await asyncio.sleep(1)
        if _ws_clients:
            try:
                await _broadcast_state()
            except Exception as e:
                logger.error(f"State broadcaster error: {e}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        # Send current state immediately on connect
        state = get_state()
        _sync_player_status(state)
        await websocket.send_text(json.dumps(state.to_status_dict()))
        async for _ in websocket.iter_text():
            pass  # clients send nothing; loop keeps connection alive
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


@app.on_event("startup")
async def startup():
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    _refresh_audio_devices("startup")

    # Admin auth status (first-boot welcome handles setup; no auto-init)
    if auth.setup_required():
        logger.info("Admin auth: first-boot setup pending (welcome at /setup)")
    else:
        logger.info(f"Admin auth mode: {auth.get_mode()}")

    # Register callbacks
    player.setup_player(_on_track_end, _on_disc_error)

    # Start background tasks
    asyncio.create_task(_disc_monitor())
    asyncio.create_task(_state_broadcaster())
    asyncio.create_task(_audio_reconciliation_loop())


# ---------------------------------------------------------------------------
# Auto-advance callback (called from player monitor thread when track ends)
# ---------------------------------------------------------------------------

async def _disc_monitor():
    """
    Background task: polls the optical drive to detect disc changes.

    Handles three cases without relying on udev:
      - Service started with disc already in drive  (startup detection)
      - Disc ejected while service is running       (reset state)
      - New disc inserted / disc swapped            (load new metadata)
    """
    await asyncio.sleep(2)          # let uvicorn finish binding
    loop = asyncio.get_running_loop()
    last_id: Optional[str] = None   # None = no disc / unknown

    while True:
        try:
            state = get_state()

            if state.loading:
                await asyncio.sleep(3)
                continue

            # When mpv holds the drive, discid.read() would compete with audio
            # reads.  Use CDROM_DRIVE_STATUS ioctl instead — no data read,
            # safe during playback — to detect disc removal while drive is busy.
            if player.drive_busy():
                if last_id is not None:
                    try:
                        present = await asyncio.wait_for(
                            loop.run_in_executor(None, cd_reader.disc_media_present),
                            timeout=2.0,
                        )
                    except asyncio.TimeoutError:
                        present = True  # ioctl blocked; stall watchdog handles it
                    if not present:
                        logger.info("Disc removed during playback (ioctl)")
                        # Reset state and broadcast before stopping mpv — player.stop()
                        # blocks up to 3 s waiting for the process; doing it in an
                        # executor keeps the event loop (and WS broadcast) responsive.
                        get_state().reset()
                        last_id = None
                        await _broadcast_state()
                        await loop.run_in_executor(None, player.stop)
                await asyncio.sleep(3)
                continue

            # Gate: when no disc is currently known, check drive readiness via
            # ioctl before attempting a physical TOC read.  discid.read() competes
            # with the drive's spin-up motor when the disc is still accelerating;
            # waiting for CDS_DISC_OK avoids unnecessary seeks and failed reads.
            if last_id is None:
                try:
                    drv = await asyncio.wait_for(
                        loop.run_in_executor(None, cd_reader.raw_drive_status),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    drv = 0
                if drv == 3:        # CDS_DRIVE_NOT_READY — spinning up, no TOC read yet
                    await asyncio.sleep(2)
                    continue
                elif drv in (1, 2): # CDS_NO_DISC / CDS_TRAY_OPEN — confirmed empty
                    await asyncio.sleep(5)
                    continue
                # drv == 4 (CDS_DISC_OK) or 0 (unknown) → proceed to read_disc_id()

            try:
                current_id: Optional[str] = await loop.run_in_executor(
                    None, cd_reader.read_disc_id
                )
            except Exception as e:
                logger.debug(f"Disc poll error: {e}")
                current_id = None

            if current_id and current_id != last_id:
                # New disc (or first detection at startup).
                # Re-check audio devices: DAC may have been connected after startup.
                _refresh_audio_devices("disc_insert")

                logger.info(f"Disc {'detected at startup' if last_id is None else 'swapped'}: {current_id}")
                speed_ok = await loop.run_in_executor(None, cd_reader.set_drive_speed, 1)
                if speed_ok:
                    logger.info("Drive speed set to 1x (CDROM_SELECT_SPEED accepted)")
                else:
                    logger.warning("Drive does not support CDROM_SELECT_SPEED — running at default speed (more noise expected)")
                if last_id is not None:
                    # Swap: stop any current playback before loading new disc
                    try:
                        player.stop()
                    except Exception as e:
                        logger.warning(f"Stop before disc swap failed: {e}")
                state.state = CDState.LOADED
                state.loading = True
                await _load_disc_metadata()
                state.loading = False
                last_id = current_id
                await _broadcast_state()

            elif not current_id and last_id is not None:
                # Disc removed
                logger.info("Disc removed")
                try:
                    player.stop()
                except Exception as e:
                    logger.warning(f"Stop on disc removal failed: {e}")
                state.reset()
                last_id = None
                await _broadcast_state()

        except Exception as e:
            logger.error(f"Disc monitor unexpected error: {e}")

        # Disc confirmed → slow poll (avoid unnecessary TOC seeks).
        # No disc → 5 s (ioctl gate above handles the NOT_READY fast-path).
        await asyncio.sleep(5 if last_id is None else 15)


def _on_disc_error():
    """Called by player monitor thread when mpv exits due to disc error (ejection, read failure)."""
    state = get_state()
    try:
        player.stop()
    except Exception:
        pass
    # Distinguish read error (disc still present) from ejection (disc gone).
    # If the disc is still in the drive, return to LOADED — metadata is intact
    # and the user can press Play again without re-inserting.
    # If the disc is gone, do a full reset to IDLE.
    try:
        still_present = cd_reader.disc_media_present()
    except Exception:
        still_present = False
    if still_present:
        logger.info("Disc read error — disc still present, returning to LOADED")
        state.state        = CDState.LOADED
        state.elapsed      = 0
        state.buffering    = False
        state.track_number = 0
        state.track_title  = None
    else:
        logger.info("Disc error — disc removed, resetting to IDLE")
        state.reset()
    _schedule_broadcast()


def _on_track_end(finished_track: int):
    """
    Called by player when a track ends.

    mpv advances through tracks automatically, so for mid-disc track changes
    we only update state (no player.play() call — mpv is already playing the
    next track).  For end-of-disc we set state back to LOADED.
    """
    state = get_state()
    if state.state != CDState.PLAYING:
        return
    next_num = finished_track + 1
    if state.tracks and next_num <= len(state.tracks):
        logger.info(f"Track {finished_track} ended → now on track {next_num}")
        state.track_number = next_num
        state.track_title  = state.tracks[next_num - 1].title
        state.elapsed      = 0
        # mpv is already playing next_num — no need to call player.play()
    else:
        logger.info("End of disc reached")
        state.state        = CDState.LOADED
        state.track_number = 0
        state.track_title  = None
        state.elapsed      = 0
    _schedule_broadcast()


# ---------------------------------------------------------------------------
# Status & tracks
# ---------------------------------------------------------------------------

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(Path(__file__).parent / "favicon" / "favicon.ico")


@app.get("/", response_class=HTMLResponse)
def ui():
    return FileResponse(
        Path(__file__).parent / "ui.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/status")
def get_status():
    state = get_state()
    _sync_player_status(state)
    return state.to_status_dict()


@app.get("/tracks")
def get_tracks():
    state = get_state()
    return [
        {"number": t.number, "title": t.title, "duration": t.duration}
        for t in state.tracks
    ]


def _sync_player_status(state: CDPlayerState):
    """Sync elapsed time and state from the subprocess player."""
    if state.state not in (CDState.PLAYING, CDState.PAUSED):
        return

    ps = player.get_player_status()
    state.buffering = ps.get("buffering", False)
    state.elapsed = int(float(ps.get("elapsed", 0)))

    # Sync state in case playback ended between polls
    ps_state = ps.get("state", "stop")
    if ps_state == "play":
        state.state = CDState.PLAYING
    elif ps_state == "pause":
        state.state = CDState.PAUSED
    elif ps_state == "stop" and state.state == CDState.PLAYING:
        state.state = CDState.LOADED

    # Update duration from TOC if available
    if state.track_number and state.tracks:
        idx = state.track_number - 1
        if 0 <= idx < len(state.tracks):
            state.duration = state.tracks[idx].duration


def _playback_device_ready() -> tuple[bool, str]:
    """Verify that the selected audio device is usable before launching mpv."""
    with _audio_lock:
        state = get_state()
        audio_state = state.audio_state
        alsa_device = state.alsa_device
        device_ids = {d["id"] for d in _alsa_devices}

    if audio_state == "device_missing":
        return False, "audio_device_missing"
    if audio_state == "no_usb":
        return False, "audio_no_usb_dac"
    if audio_state == "usb_multiple" and alsa_device not in device_ids:
        return False, "audio_device_ambiguous"
    return True, ""


# ---------------------------------------------------------------------------
# Playback controls
# ---------------------------------------------------------------------------

@app.post("/play")
def play_from_start():
    state = get_state()
    if state.state == CDState.IDLE:
        raise HTTPException(status_code=409, detail="No disc loaded")
    ready, reason = _playback_device_ready()
    if not ready:
        raise HTTPException(status_code=409, detail=reason)
    try:
        # Pre-spin: wake the drive motor before launching mpv so that
        # --cdda-speed=1 transitions from an already-spinning disc (fast)
        # rather than from a cold stop (can take 60+ s on some drives).
        if not player.drive_busy():
            try:
                cd_reader.read_disc_id()
            except Exception:
                pass
        # Set state before calling play() — play() blocks during mpv spin-up
        # so polls during that window must already show the correct track.
        state.state        = CDState.PLAYING
        state.track_number = 1
        state.elapsed      = 0
        if state.tracks:
            state.track_title = state.tracks[0].title
        player.play(1, len(state.tracks), alsa_device=state.alsa_device)
        _schedule_broadcast()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/play/{track_number}")
def play_track(track_number: int):
    state = get_state()
    if state.state == CDState.IDLE:
        raise HTTPException(status_code=409, detail="No disc loaded")
    if state.tracks and not (1 <= track_number <= len(state.tracks)):
        raise HTTPException(status_code=400, detail=f"Track {track_number} out of range")
    ready, reason = _playback_device_ready()
    if not ready:
        raise HTTPException(status_code=409, detail=reason)
    try:
        if not player.drive_busy():
            try:
                cd_reader.read_disc_id()
            except Exception:
                pass
        # Set state before calling play() — play() blocks during mpv spin-up
        # so polls during that window must already show the correct track.
        state.state        = CDState.PLAYING
        state.track_number = track_number
        state.elapsed      = 0
        if state.tracks and 1 <= track_number <= len(state.tracks):
            state.track_title = state.tracks[track_number - 1].title
        player.play(track_number, len(state.tracks), alsa_device=state.alsa_device)
        _schedule_broadcast()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/pause")
def pause():
    state = get_state()
    if state.state not in (CDState.PLAYING, CDState.PAUSED):
        raise HTTPException(status_code=409, detail="Not playing")
    try:
        player.pause_toggle()
        state.state = CDState.PAUSED if state.state == CDState.PLAYING else CDState.PLAYING
        _schedule_broadcast()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/stop")
def stop():
    state = get_state()
    if state.state == CDState.IDLE:
        return {"ok": True}
    try:
        player.stop()
        state.state   = CDState.LOADED if state.disc_id else CDState.IDLE
        state.elapsed = 0
        _schedule_broadcast()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/next")
def next_track():
    state = get_state()
    if state.state not in (CDState.PLAYING, CDState.PAUSED):
        raise HTTPException(status_code=409, detail="Not playing")
    try:
        player.next_track()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/prev")
def prev_track():
    state = get_state()
    if state.state not in (CDState.PLAYING, CDState.PAUSED):
        raise HTTPException(status_code=409, detail="Not playing")
    try:
        player.prev_track(alsa_device=state.alsa_device)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/eject")
def eject():
    state = get_state()
    try:
        player.eject()
        state.reset()
        _schedule_broadcast()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# CD insertion / ejection (udev triggers)
# ---------------------------------------------------------------------------

@app.post("/cd/inserted")
async def cd_inserted(background_tasks: BackgroundTasks):
    state = get_state()
    logger.info("CD insertion event received")
    state.state = CDState.LOADED
    background_tasks.add_task(_load_disc_metadata)
    return {"ok": True, "message": "Metadata lookup started"}


@app.post("/cd/ejected")
async def cd_ejected():
    state = get_state()
    logger.info("CD ejection event received")
    try:
        player.stop()
    except Exception:
        pass
    state.reset()
    return {"ok": True}


async def _load_disc_metadata():
    """Background task: read TOC + MusicBrainz lookup + cache cover art."""
    state = get_state()
    state.loading = True
    loop  = asyncio.get_running_loop()

    # Single disc read — returns disc_id, toc, and cddb_info in one pass
    disc_id, toc, cddb_info = await loop.run_in_executor(None, cd_reader.read_disc_full)

    if not disc_id:
        logger.error("Could not read disc ID; proceeding without metadata")
        state.disc_id = "unknown"
        state.tracks = [TrackInfo(n, f"Track {n}", d) for n, d in toc]
        state.loading = False
        return

    state.disc_id = disc_id

    # MusicBrainz lookup
    mb_data = await loop.run_in_executor(None, metadata.lookup_disc, disc_id)
    if mb_data:
        state.album     = mb_data.get("album", "Unknown Album")
        state.artist    = mb_data.get("artist", "Unknown Artist")
        state.cover_url = mb_data.get("cover_url")

        mb_tracks = mb_data.get("tracks", [])
        if mb_tracks:
            state.tracks = [
                TrackInfo(t["number"], t["title"], t["duration"])
                for t in mb_tracks
            ]
        elif toc:
            state.tracks = [TrackInfo(n, f"Track {n}", d) for n, d in toc]

        if state.cover_url:
            local = await loop.run_in_executor(
                None, metadata.fetch_and_cache_cover, state.cover_url, disc_id
            )
            if local:
                state.cover_local_path = str(local)
    else:
        # MusicBrainz had no match — try GnuDB (CDDB) as fallback
        gnudb_data = None
        if cddb_info:
            gnudb_data = await loop.run_in_executor(
                None, metadata.lookup_disc_gnudb, cddb_info
            )

        if gnudb_data:
            state.album  = gnudb_data.get("album", "Unknown Album")
            state.artist = gnudb_data.get("artist", "Unknown Artist")
            gnudb_tracks = gnudb_data.get("tracks", [])
            if gnudb_tracks:
                # GnuDB provides titles but no durations — fill from TOC.
                toc_dur = {n: d for n, d in toc}
                state.tracks = [
                    TrackInfo(t["number"], t["title"], toc_dur.get(t["number"], 0))
                    for t in gnudb_tracks
                ]
            elif toc:
                state.tracks = [TrackInfo(n, f"Track {n}", d) for n, d in toc]

            # Fetch cover art from iTunes (free, no key needed)
            itunes_url = await loop.run_in_executor(
                None, metadata.fetch_itunes_cover_url, state.artist, state.album
            )
            if itunes_url:
                state.cover_url = itunes_url
                local = await loop.run_in_executor(
                    None, metadata.fetch_and_cache_cover, itunes_url, disc_id
                )
                if local:
                    state.cover_local_path = str(local)
        else:
            state.album  = "Unknown Album"
            state.artist = "Unknown Artist"
            if toc:
                state.tracks = [TrackInfo(n, f"Track {n}", d) for n, d in toc]

    state.loading = False
    logger.info(
        f"Disc loaded: '{state.album}' by '{state.artist}' "
        f"({len(state.tracks)} tracks)"
    )


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------

@app.get("/cover")
def get_cover():
    state = get_state()
    if state.cover_local_path and Path(state.cover_local_path).exists():
        return FileResponse(
            state.cover_local_path,
            media_type="image/jpeg",
            headers={"Cache-Control": "max-age=3600"},
        )
    if state.cover_url:
        return JSONResponse(
            status_code=302,
            content=None,
            headers={"Location": state.cover_url},
        )
    raise HTTPException(status_code=404, detail="No cover art available")


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

@app.get("/devices")
def get_devices():
    with _audio_lock:
        return [dict(d) for d in _alsa_devices]


@app.post("/devices/rescan")
def rescan_devices():
    snapshot = _refresh_audio_devices("api")
    with _audio_lock:
        devices = [dict(d) for d in _alsa_devices]
    return {"ok": True, "devices": devices, "audio_state": snapshot["audio_state"]}


@app.post("/audio/device-change")
async def audio_device_change(request: Request):
    """Called by the udev audio-device-change.sh script when a sound card is
    added or removed.  Triggers an immediate refresh so the UI updates without
    manual intervention."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    action = body.get("action", "unknown")
    name   = body.get("name", "unknown")
    logger.info(f"[audio] udev event: action={action} name={name}")
    snapshot = _refresh_audio_devices(f"udev_{action}")
    if action == "add":
        async def _deferred_refresh(delay: float, reason: str):
            await asyncio.sleep(delay)
            try:
                _refresh_audio_devices(reason)
            except Exception as e:
                logger.warning(f"[audio] deferred refresh error: {e}")

        asyncio.create_task(_deferred_refresh(0.3, "udev_add_deferred_300ms"))
        asyncio.create_task(_deferred_refresh(1.0, "udev_add_deferred_1s"))
    return {"ok": True, "audio_state": snapshot["audio_state"], "alsa_device": snapshot["alsa_device"]}


@app.post("/devices/select")
def select_device(device_id: str):
    global _audio_snapshot
    with _audio_lock:
        valid = device_id in {d["id"] for d in _alsa_devices}
        if valid:
            state = get_state()
            state.alsa_device = device_id
            for d in _alsa_devices:
                d["auto_selected"] = d["id"] == device_id
            state.audio_state = _compute_audio_state(_alsa_devices, device_id)
            _audio_snapshot = {
                "alsa_device": state.alsa_device,
                "audio_state": state.audio_state,
                "device_count": len(_alsa_devices),
            }
    if not valid:
        raise HTTPException(status_code=400, detail=f"Unknown device: {device_id}")
    _schedule_broadcast()
    return {"ok": True, "selected": device_id}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin authentication
# ---------------------------------------------------------------------------
# Route-sensitivity split (docs/agent_reasoning.md D-2):
#   - Playback/status endpoints are open on the LAN.
#   - /system/* is admin-gated, except the HTML shells themselves (they must
#     load unauthenticated so the page JS can render a PIN modal).
_HTML_SHELL_PATHS = {"/system", "/system/network", "/system/roon", "/system/admin"}


def _needs_admin_auth(request: Request) -> bool:
    if auth.get_mode() == auth.MODE_OFF:
        return False
    path = request.url.path
    if not path.startswith("/system"):
        return False
    if request.method == "GET" and path in _HTML_SHELL_PATHS:
        return False
    return True


@app.middleware("http")
async def admin_gate(request: Request, call_next):
    if _needs_admin_auth(request):
        token = request.cookies.get(auth.COOKIE_NAME)
        if auth.verify_token(token) is None:
            return JSONResponse(
                {"error": "admin_auth_required"},
                status_code=401,
                headers={"WWW-Authenticate": "CDPcore-PIN"},
            )
    return await call_next(request)


@app.get("/admin_gate.js", include_in_schema=False)
def admin_gate_script():
    return FileResponse(
        Path(__file__).parent / "admin_gate.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin/status")
def admin_status(request: Request):
    mode = auth.get_mode()
    pin_configured = auth.is_pin_configured()
    if mode == auth.MODE_OFF:
        return {"mode": "off", "unlocked": True, "expires_at": None,
                "pin_configured": pin_configured}
    exp = auth.verify_token(request.cookies.get(auth.COOKIE_NAME))
    return {"mode": "pin", "unlocked": exp is not None, "expires_at": exp,
            "pin_configured": pin_configured}


class _UnlockRateLimit:
    """Per-IP failure tracker with progressive delay.

    Defense against PIN brute-force without account complexity. Each failed
    /admin/unlock attempt from an IP is recorded with a timestamp. The
    imposed delay grows exponentially after a threshold of consecutive
    failures, capped to a ceiling that keeps legitimate retries usable on a
    LAN appliance. A successful unlock or a quiet window of inactivity
    clears the counter for that IP.

    Tunables (chosen for an appliance on a LAN, not a public-internet API):
      BASELINE_DELAY  baseline per-attempt delay (was already 200 ms)
      THRESHOLD       failures before backoff kicks in
      MAX_BACKOFF     ceiling of the added delay (seconds)
      QUIET_RESET     entries older than this are pruned (seconds)

    Rationale for keeping this in-process and unauthenticated:
      - the appliance runs single-host; persistence across restarts is not
        required because the keyspace is small enough that the limiter only
        needs to defeat in-window brute-force, and a process restart is
        itself an admin action.
      - per-IP keying (request.client.host) is the right granularity for a
        local appliance — no account model is needed.
    """
    BASELINE_DELAY = 0.2
    THRESHOLD = 3
    MAX_BACKOFF = 30.0
    QUIET_RESET = 600  # 10 minutes

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, ts_list: list[float], now: float) -> list[float]:
        return [t for t in ts_list if now - t < self.QUIET_RESET]

    def delay_for(self, ip: str) -> float:
        with self._lock:
            now = time.time()
            entries = self._prune(self._fails.get(ip, []), now)
            if not entries:
                self._fails.pop(ip, None)
                return self.BASELINE_DELAY
            self._fails[ip] = entries
            count = len(entries)
            if count <= self.THRESHOLD:
                return self.BASELINE_DELAY
            # 4th fail = +0.5s, 5th = +1s, 6th = +2s, 7th = +4s, ...
            extra = min(2.0 ** (count - self.THRESHOLD - 1) * 0.5, self.MAX_BACKOFF)
            return self.BASELINE_DELAY + extra

    def record_failure(self, ip: str) -> int:
        with self._lock:
            now = time.time()
            entries = self._prune(self._fails.get(ip, []), now)
            entries.append(now)
            self._fails[ip] = entries
            return len(entries)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


_unlock_limiter = _UnlockRateLimit()


@app.post("/admin/unlock")
def admin_unlock(request: Request, body: dict):
    _verify_same_origin(request)
    mode = auth.get_mode()
    if mode == auth.MODE_OFF:
        return {"ok": True, "mode": "off", "unlocked": True, "expires_at": None}

    client_ip = request.client.host if request.client else "unknown"
    time.sleep(_unlock_limiter.delay_for(client_ip))

    pin = str(body.get("pin") or "")
    if not auth.verify_pin(pin):
        fails = _unlock_limiter.record_failure(client_ip)
        if fails > _UnlockRateLimit.THRESHOLD:
            logger.warning(
                f"admin/unlock: {fails} consecutive failures from {client_ip} "
                f"(next attempt will sleep up to {_unlock_limiter.delay_for(client_ip):.1f}s)"
            )
        raise HTTPException(status_code=401, detail="invalid_pin")
    _unlock_limiter.record_success(client_ip)

    token, exp = auth.issue_token()
    resp = JSONResponse({"ok": True, "expires_at": exp})
    resp.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return resp


@app.post("/admin/lock")
def admin_lock():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/admin/setup_required")
def admin_setup_required():
    """Public: whether first-boot setup (PIN choice or LAN-Trust) is still pending."""
    return {"required": auth.setup_required()}


@app.get("/setup", response_class=HTMLResponse)
def setup_page():
    return FileResponse(
        Path(__file__).parent / "welcome.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/setup")
def admin_setup(request: Request, body: dict):
    """One-shot first-boot setup. 409 once an auth choice has been persisted."""
    _verify_same_origin(request)
    if not auth.setup_required():
        raise HTTPException(status_code=409, detail="already_configured")

    mode = str(body.get("mode") or "").strip().lower()
    if mode == auth.MODE_PIN:
        pin = str(body.get("pin") or "")
        try:
            auth.set_pin(pin)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        auth.set_mode(auth.MODE_PIN)
        token, exp = auth.issue_token()
        resp = JSONResponse({"ok": True, "mode": "pin", "expires_at": exp})
        resp.set_cookie(
            key=auth.COOKIE_NAME,
            value=token,
            max_age=auth.SESSION_TTL,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return resp

    if mode == auth.MODE_OFF:
        auth.set_mode(auth.MODE_OFF)
        return {"ok": True, "mode": "off"}

    raise HTTPException(status_code=400, detail="mode must be 'pin' or 'off'")


# ---------------------------------------------------------------------------
# System management
# ---------------------------------------------------------------------------

@app.get("/system", response_class=HTMLResponse)
def system_page():
    return FileResponse(
        Path(__file__).parent / "system.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/system/info")
def system_info_endpoint():
    return system_info.get_system_info()


@app.post("/system/admin/set_pin")
def system_admin_set_pin(body: dict):
    """Change the admin PIN. Requires an already-unlocked session
    (enforced by the admin_gate middleware on /system/*)."""
    new_pin = str(body.get("new_pin") or "")
    try:
        auth.set_pin(new_pin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/system/admin/set_mode")
def system_admin_set_mode(body: dict):
    """Toggle the admin-auth mode. Session-gated by admin_gate middleware.

    off -> pin: if no PIN exists yet (user came from first-boot LAN-Trust),
    a `new_pin` must be supplied and is set atomically with the mode flip.
    If a PIN already exists, a new one is optional (replaces).

    pin -> off: requires re-entering the current PIN as a guard against an
    unattended-browser takeover. Only enforced when a PIN is configured —
    if there is none, there is nothing to take over.
    """
    mode = str(body.get("mode") or "").strip().lower()
    if mode not in (auth.MODE_PIN, auth.MODE_OFF):
        raise HTTPException(status_code=400, detail="mode must be 'pin' or 'off'")

    if mode == auth.MODE_PIN:
        new_pin = str(body.get("new_pin") or "")
        if new_pin:
            try:
                auth.set_pin(new_pin)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        elif not auth.is_pin_configured():
            raise HTTPException(status_code=400, detail="pin_required")

    elif mode == auth.MODE_OFF and auth.is_pin_configured():
        time.sleep(0.2)
        pin = str(body.get("pin") or "")
        if not auth.verify_pin(pin):
            # 403 (not 401) so admin_gate.js doesn't treat this as an expired
            # session and pop its unlock modal — this is a failed re-auth
            # challenge on an otherwise valid session, which the caller
            # surfaces inline in the auth card.
            raise HTTPException(status_code=403, detail="invalid_pin")

    auth.set_mode(mode)
    return {"ok": True, "mode": mode}


_SYSTEM_ACTIONS = {
    "restart_backend":        ["sudo", "systemctl", "restart", "cdpcore-backend"],
    "restart_extension":      ["sudo", "systemctl", "restart", "cdpcore-extension"],
    "reboot":                 ["sudo", "systemctl", "reboot"],
    "poweroff":               ["sudo", "systemctl", "poweroff"],
    "roon_bridge_start":      ["sudo", "systemctl", "start",   "roonbridge"],
    "roon_bridge_stop":       ["sudo", "systemctl", "stop",    "roonbridge"],
    "roon_bridge_restart":    ["sudo", "systemctl", "restart", "roonbridge"],
    "roon_bridge_enable":     ["sudo", "systemctl", "enable",  "roonbridge"],
    "roon_bridge_disable":    ["sudo", "systemctl", "disable", "roonbridge"],
}


def _run_deferred(cmd: list, delay: float = 0.6):
    """Run a shell command after a short delay (lets the HTTP response be sent first)."""
    def _go():
        time.sleep(delay)
        subprocess.run(cmd)
    threading.Thread(target=_go, daemon=True).start()


@app.post("/system/action")
def system_action(body: dict):
    action = body.get("action", "")

    if action == "restart_all":
        # Extension first (sync), then backend (deferred — kills this process)
        subprocess.run(["sudo", "systemctl", "restart", "cdpcore-extension"])
        _run_deferred(["sudo", "systemctl", "restart", "cdpcore-backend"])
        return {"ok": True}

    if action in _SYSTEM_ACTIONS:
        cmd = _SYSTEM_ACTIONS[action]
        # Anything that may kill this process runs deferred
        _run_deferred(cmd)
        return {"ok": True}

    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


@app.get("/system/network", response_class=HTMLResponse)
def network_config_page():
    return FileResponse(
        Path(__file__).parent / "network_config.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/system/network/info")
async def system_network_info():
    """Return current network configuration (hostname, ethernet, wifi)."""
    loop = asyncio.get_running_loop()
    hostname  = socket.gethostname()
    ethernet  = await loop.run_in_executor(None, net_mgr.get_ethernet_connections)
    radio     = await loop.run_in_executor(None, net_mgr.wifi_radio_state)
    wifi_conn = await loop.run_in_executor(None, net_mgr.get_current_wifi_connection)
    wifi_ip   = await loop.run_in_executor(None, net_mgr.get_wifi_current_ip)
    networks  = await loop.run_in_executor(None, net_mgr.wifi_list)
    return {
        "hostname": hostname,
        "ethernet": ethernet,
        "wifi": {
            "radio":        radio,
            "connected_to": wifi_conn,
            "current_ip":   wifi_ip,
            "networks":     networks,
        },
    }


@app.post("/system/network/hostname")
async def system_set_hostname(body: dict):
    name = (body.get("hostname") or "").strip()
    if not name or not all(c.isalnum() or c in "-" for c in name):
        raise HTTPException(status_code=400, detail="Invalid hostname")
    loop = asyncio.get_running_loop()
    ok, err = await loop.run_in_executor(None, net_mgr.set_hostname, name)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True}


@app.post("/system/network/ethernet")
async def system_set_ethernet(body: dict):
    connection = body.get("connection", "")
    method     = body.get("method", "dhcp")
    loop = asyncio.get_running_loop()
    if method == "dhcp":
        ok, err = await loop.run_in_executor(None, net_mgr.set_ethernet_dhcp, connection)
    elif method == "static":
        address = (body.get("address") or "").strip()
        gateway = (body.get("gateway") or "").strip()
        dns     = (body.get("dns") or "").strip()
        if not address:
            raise HTTPException(status_code=400, detail="address required for static")
        ok, err = await loop.run_in_executor(
            None, net_mgr.set_ethernet_static, connection, address, gateway, dns
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown method: {method}")
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True}


@app.post("/system/network/wifi/radio")
async def system_wifi_radio(body: dict):
    on = bool(body.get("on", True))
    loop = asyncio.get_running_loop()
    ok, err = await loop.run_in_executor(None, net_mgr.wifi_set_radio, on)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True}


@app.post("/system/network/wifi/scan")
async def system_wifi_scan():
    loop = asyncio.get_running_loop()
    networks = await loop.run_in_executor(None, net_mgr.wifi_scan)
    return {"ok": True, "networks": networks}


@app.post("/system/network/wifi/connect")
async def system_wifi_connect(body: dict):
    ssid     = (body.get("ssid") or "").strip()
    password = body.get("password") or ""
    hidden   = bool(body.get("hidden", False))
    if not ssid:
        raise HTTPException(status_code=400, detail="ssid required")
    loop = asyncio.get_running_loop()
    ok, err = await loop.run_in_executor(None, net_mgr.wifi_connect, ssid, password, hidden)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True}


@app.post("/system/network/wifi/disconnect")
async def system_wifi_disconnect(body: dict):
    connection = (body.get("connection") or "").strip()
    if not connection:
        raise HTTPException(status_code=400, detail="connection required")
    loop = asyncio.get_running_loop()
    ok, err = await loop.run_in_executor(None, net_mgr.wifi_disconnect, connection)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True}


_EXTENSION_SETTINGS = Path("/home/cdplayer/.config/cdpcore-extension/settings.json")
_EXTENSION_STATE     = Path("/var/cache/cd-player/roon-state.json")


@app.get("/system/roon", response_class=HTMLResponse)
def roon_config_page():
    return FileResponse(
        Path(__file__).parent / "roon_config.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/system/admin", response_class=HTMLResponse)
def admin_config_page():
    return FileResponse(
        Path(__file__).parent / "admin_config.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/system/roon/info")
def system_roon_info():
    rb  = system_info.get_roon_bridge_info()   # None if not installed
    ext = system_info.get_service_status("cdpcore-extension")

    # State written by the extension on pair/unpair
    roon_state: dict = {}
    try:
        roon_state = json.loads(_EXTENSION_STATE.read_text())
    except Exception:
        pass

    # Extension settings managed from this page
    settings: dict = {}
    try:
        settings = json.loads(_EXTENSION_SETTINGS.read_text())
    except Exception:
        pass

    return {
        "roon_bridge": rb,
        "extension": {
            **ext,
            "paired":     roon_state.get("paired", False),
            "core_name":  roon_state.get("core_name"),
            "updated_at": roon_state.get("updated_at"),
        },
        "settings": settings,
    }


@app.post("/system/roon/settings")
def system_roon_save_settings(body: dict):
    """Persist a subset of extension settings; extension reloads via fs.watchFile."""
    allowed = {"zone_name"}
    try:
        current: dict = {}
        try:
            current = json.loads(_EXTENSION_SETTINGS.read_text())
        except Exception:
            pass
        for k in allowed:
            if k in body:
                current[k] = body[k]
        _EXTENSION_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        _EXTENSION_SETTINGS.write_text(json.dumps(current, indent=2))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/system/logs")
def system_logs(n: int = 150):
    """Return the last N lines of the backend service journal."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "cdpcore-backend", "-n", str(n), "--no-pager", "--output=short"],
            capture_output=True,
            timeout=5,
        )
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        return {"lines": lines}
    except Exception as e:
        return {"lines": [f"Error retrieving logs: {e}"]}
