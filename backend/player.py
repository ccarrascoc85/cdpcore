"""
CD playback via mpv with IPC socket (real-time, no ripping).

One mpv process per disc:
  mpv --cdda-device=CDROM --no-video --ao=alsa cdda://

Track navigation via IPC JSON commands:
  set_property chapter N   (0-indexed: chapter 0 = track 1)

Pause/resume via IPC:
  set_property pause true/false

Elapsed time via IPC:
  get_property time-pos     (seconds within current chapter/track)

Auto-advance: mpv advances chapters automatically; a monitor thread watches
for chapter-change events and fires on_track_end for state updates.
When the disc ends (end-file event), on_track_end fires for the last track.
"""
import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

CDROM_DEVICE = os.environ.get("CDROM_DEVICE", "/dev/cdrom")
ALSA_DEVICE  = os.environ.get("ALSA_DEVICE",  "hw:0,0")
_IPC_SOCK    = Path(os.environ.get("CD_CACHE_DIR", "/var/cache/cd-player")) / "mpv.sock"


def _aplay_device(hw_device: str) -> str:
    if hw_device.startswith("hw:"):
        return "plughw:" + hw_device[3:]
    return hw_device


class CDPlayer:
    """mpv-based real-time CD player with IPC control.

    Locking rule: self._lock guards in-memory state fields only. Never hold it
    across process, IPC, or drive I/O; teardown can wedge in kernel D-state.
    """

    def __init__(self):
        self._mpv_proc:      Optional[subprocess.Popen] = None
        self._ipc_sock:      Optional[socket.socket]    = None
        self._ipc_lock       = threading.Lock()
        self._current_track: int   = 0
        self._total_tracks:  int   = 0
        self._paused:        bool  = False
        self._track_elapsed: float = 0.0   # fallback timer
        self._track_start:   float = 0.0
        self._lock           = threading.Lock()
        self._start_lock     = threading.Lock()
        self._on_track_end:  Optional[Callable[[int], None]] = None
        self._on_disc_error: Optional[Callable[[], None]]    = None
        # chapter-list start times (disc-absolute seconds) cached at mpv startup
        self._chapter_starts: list = []
        # True between play() call and first valid IPC time-pos (mpv spinning up)
        self._starting: bool = False
        self._mpv_generation: int = 0
        self._teardown_generations: set[int] = set()

    def set_on_track_end(self, callback: Callable[[int], None]):
        self._on_track_end = callback

    def set_on_disc_error(self, callback: Callable[[], None]):
        self._on_disc_error = callback

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------

    def play(self, track_number: int, total_tracks: int,
             cdrom_device: str = CDROM_DEVICE,
             alsa_device:  str = ALSA_DEVICE,
             seek_to_sec: float = 0.0):
        with self._start_lock:
            with self._lock:
                mpv_running = self._mpv_running()

            if not mpv_running:
                proc = self._spawn_mpv(cdrom_device, alsa_device)
                with self._lock:
                    self._mpv_generation += 1
                    generation = self._mpv_generation
                    self._mpv_proc = proc
                    self._starting = True
                logger.info(f"mpv started (pid={proc.pid})")
                threading.Thread(
                    target=self._monitor_mpv,
                    args=(generation,),
                    daemon=True,
                ).start()

            with self._lock:
                self._current_track = track_number
                self._total_tracks  = total_tracks
                self._paused        = False
                self._track_elapsed = 0.0
                self._track_start   = time.monotonic()

        if not mpv_running:
            # Wait for IPC socket, then wait until mpv has read the CDDA TOC
            # (chapter property becomes non-None), then seek to the target track.
            self._wait_for_ipc(timeout=10)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self._ipc_query("chapter") is not None:
                    break
                if self.is_stopped():
                    return  # stop() was called while waiting — abort
                time.sleep(0.5)
            if track_number > 1:
                self._ipc_send(["set_property", "chapter", track_number - 1])
            threading.Thread(
                target=self._cache_chapter_list_bg,
                args=(generation,),
                daemon=True,
            ).start()
            self._ipc_send(["set_property", "pause", False])
            with self._lock:
                self._starting = False
            logger.info(f"Playing track {track_number}/{total_tracks}")
            return

        # mpv already running — jump to the requested chapter via IPC.
        # set_property chapter N is a no-op when already on chapter N
        # (e.g. restarting the current track), so an explicit seek is needed then.
        current_chapter = self._ipc_query("chapter")
        self._ipc_send(["set_property", "chapter", track_number - 1])
        if current_chapter == track_number - 1:
            # Same-chapter restart: use the cached TOC start time + 150 ms clearance
            # to land clearly inside the chapter boundary.
            idx = track_number - 1
            if idx < len(self._chapter_starts):
                start_time = self._chapter_starts[idx] + 0.15
            else:
                start_time = seek_to_sec + 1.0
            self._ipc_send(["seek", start_time, "absolute"])
        self._ipc_send(["set_property", "pause", False])
        logger.info(f"Playing track {track_number}/{total_tracks}")

    def pause(self):
        with self._lock:
            if self._paused or not self._mpv_running():
                return
            self._track_elapsed = self._track_elapsed + (time.monotonic() - self._track_start)
            self._paused = True
        self._ipc_send(["set_property", "pause", True])
        logger.info("Paused")

    def resume(self):
        with self._lock:
            if not self._paused or not self._mpv_running():
                return
            self._track_start = time.monotonic()
            self._paused = False
        self._ipc_send(["set_property", "pause", False])
        logger.info("Resumed")

    def stop(self):
        with self._start_lock:
            self._stop_mpv()
        logger.info("Stopped")

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    @property
    def drive_busy(self) -> bool:
        """True whenever mpv holds the CD device open (playing, paused, or spinning up)."""
        with self._lock:
            return self._mpv_running()

    def is_playing(self) -> bool:
        with self._lock:
            return self._mpv_running() and not self._paused

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused and self._mpv_running()

    def is_stopped(self) -> bool:
        with self._lock:
            return not self._mpv_running() and not self._paused

    def get_elapsed(self) -> float:
        """Elapsed seconds within the current track.

        mpv CDDA reports time-pos as disc-absolute seconds (from disc start),
        not track-relative.  We subtract the chapter start time to convert.

        Priority:
          1. Cached mpv chapter-list start (most accurate, from actual TOC sectors)
          2. MusicBrainz cumulative durations (< 1s error, available immediately)
          3. Monotonic timer fallback (when IPC is unavailable)
        """
        pos = self._ipc_get_time_pos()
        if pos is not None:
            self._starting = False  # mpv is producing real position data
            idx = self._current_track - 1
            if 0 <= idx < len(self._chapter_starts):
                return max(0.0, pos - self._chapter_starts[idx])
            # IPC works but chapter-list not cached yet — fall back to MusicBrainz
            # cumulative durations (off by < 1 s vs actual sector boundary)
            try:
                from state import get_state
                tracks = get_state().tracks
                if tracks and 0 <= idx < len(tracks):
                    mb_start = float(sum(t.duration for t in tracks[:idx]))
                    return max(0.0, pos - mb_start)
            except Exception:
                pass
            return pos
        if self._starting:
            return 0.0  # freeze elapsed at 0 while mpv is spinning up
        return self._get_timer_elapsed()

    def _get_timer_elapsed(self) -> float:
        """Track-relative elapsed time from monotonic timer (no IPC)."""
        with self._lock:
            if self._paused:
                return self._track_elapsed
            return self._track_elapsed + (time.monotonic() - self._track_start)

    def current_track(self) -> int:
        with self._lock:
            return self._current_track

    def get_status(self) -> dict:
        with self._lock:
            running = self._mpv_proc is not None
            paused  = self._paused
            song    = max(0, self._current_track - 1)
            buffering = self._starting
            if paused:
                elapsed = self._track_elapsed
            elif running:
                elapsed = self._track_elapsed + (time.monotonic() - self._track_start)
            else:
                elapsed = 0.0

        if paused:
            state = "pause"
        elif running:
            state = "play"
        else:
            state = "stop"

        return {
            "state":     state,
            "elapsed":   str(elapsed),
            "song":      str(song),
            "duration":  "0",
            "buffering": buffering,
        }

    # ------------------------------------------------------------------
    # Internal: mpv lifecycle
    # ------------------------------------------------------------------

    def _mpv_running(self) -> bool:
        """True if mpv process is alive. Must hold self._lock."""
        return self._mpv_proc is not None and self._mpv_proc.poll() is None

    @staticmethod
    def _proc_running(proc: Optional[subprocess.Popen]) -> bool:
        return proc is not None and proc.poll() is None

    def _spawn_mpv(self, cdrom_device: str, alsa_device: str) -> subprocess.Popen:
        """Start mpv process without holding self._lock."""
        try:
            _IPC_SOCK.unlink(missing_ok=True)
        except Exception:
            pass

        cmd = [
            "mpv",
            "--no-video",
            "--ao=alsa",
            f"--audio-device=alsa/{_aplay_device(alsa_device)}",
            f"--input-ipc-server={_IPC_SOCK}",
            "--really-quiet",
            "--pause",
            "--demuxer-max-bytes=512KiB",
            f"cdda://{cdrom_device}",
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stop_mpv(self):
        """Detach mpv state quickly and tear the process down in the background."""
        proc, generation, ipc_sock = self._detach_mpv_for_teardown()
        if proc is None:
            return
        threading.Thread(
            target=self._teardown_mpv_bg,
            args=(proc, generation, ipc_sock),
            daemon=True,
        ).start()

    def _detach_mpv_for_teardown(self):
        with self._lock:
            proc = self._mpv_proc
            generation = self._mpv_generation

            self._mpv_proc = None
            self._current_track = 0
            self._paused = False
            self._track_elapsed = 0.0
            self._track_start = 0.0
            self._chapter_starts = []
            self._starting = False

            if proc is None or generation in self._teardown_generations:
                return None, generation, None
            self._teardown_generations.add(generation)

        with self._ipc_lock:
            ipc_sock = self._ipc_sock
            self._ipc_sock = None
        return proc, generation, ipc_sock

    def _teardown_mpv_bg(self, proc: subprocess.Popen, generation: int,
                         ipc_sock: Optional[socket.socket]):
        try:
            self._teardown_mpv_slow(proc, generation, ipc_sock)
        finally:
            with self._lock:
                self._teardown_generations.discard(generation)

    def _send_quit_to_socket(self, ipc_sock: Optional[socket.socket]):
        if ipc_sock is None:
            return
        try:
            msg = json.dumps({"command": ["quit"]}) + "\n"
            ipc_sock.sendall(msg.encode())
        except Exception:
            pass

    def _teardown_mpv_slow(self, proc: subprocess.Popen, generation: int,
                           ipc_sock: Optional[socket.socket]):
        # Use only the captured IPC socket. Reconnecting through the shared
        # socket path could signal a newer mpv if this teardown is stale.
        self._send_quit_to_socket(ipc_sock)
        time.sleep(0.3)
        if ipc_sock:
            try:
                ipc_sock.close()
            except Exception:
                pass

        if self._proc_running(proc):
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # A D-state drive wedge cannot be killed from userspace.
                    # This mitigation can wedge too, so it only runs in a
                    # daemon thread that request paths and monitors never join.
                    try:
                        subprocess.run(
                            ["eject", "-i", "off", CDROM_DEVICE],
                            timeout=3, capture_output=True,
                        )
                    except Exception:
                        pass
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass

        with self._lock:
            current_generation = self._mpv_generation
            current_proc = self._mpv_proc
        if current_generation != generation or current_proc is not None:
            return
        try:
            _IPC_SOCK.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: IPC socket
    # ------------------------------------------------------------------

    def _wait_for_ipc(self, timeout: float = 10):
        """Block until mpv's IPC socket is available, then cache chapter list.

        The chapter-list query is retried with a generous timeout because mpv
        needs to read the full CD TOC before it can respond to IPC queries,
        which can take several seconds on a CDDA drive.
        """
        # Phase 1: wait for the socket file to appear and connect
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _IPC_SOCK.exists():
                try:
                    s = socket.socket(socket.AF_UNIX)
                    s.connect(str(_IPC_SOCK))
                    with self._ipc_lock:
                        self._ipc_sock = s
                    logger.info("IPC socket connected")
                    break
                except Exception:
                    pass
            time.sleep(0.1)
        else:
            logger.warning("IPC socket not available after timeout")
            return

        # chapter-list is fetched asynchronously after mpv starts playing
        # (see _cache_chapter_list_bg).  Nothing to do here.

    def _get_ipc(self) -> Optional[socket.socket]:
        """Return connected IPC socket, creating one if needed."""
        with self._ipc_lock:
            if self._ipc_sock is not None:
                return self._ipc_sock
        # Not connected yet — try to connect
        if not _IPC_SOCK.exists():
            return None
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(_IPC_SOCK))
            s.settimeout(0.5)
            with self._ipc_lock:
                self._ipc_sock = s
            return s
        except Exception:
            return None

    def _close_ipc(self):
        with self._ipc_lock:
            s = self._ipc_sock
            self._ipc_sock = None
        if s:
            try:
                s.close()
            except Exception:
                pass

    def _ipc_send(self, command: list):
        """Send a fire-and-forget IPC command."""
        s = self._get_ipc()
        if s is None:
            return
        try:
            msg = json.dumps({"command": command}) + "\n"
            s.sendall(msg.encode())
        except Exception:
            self._close_ipc()

    def _ipc_query(self, prop: str, timeout: float = 0.5):
        """Send a get_property command and return the parsed data value.

        Uses a fresh socket per call to avoid reading stale responses
        that accumulated from fire-and-forget _ipc_send commands on the
        shared socket.
        """
        if not _IPC_SOCK.exists():
            return None
        s = None
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(_IPC_SOCK))
            s.settimeout(timeout)
            msg = json.dumps({"command": ["get_property", prop]}) + "\n"
            s.sendall(msg.encode())
            raw = b""
            while True:
                chunk = s.recv(65536)
                raw += chunk
                if b"\n" in raw:
                    break
            resp = json.loads(raw.split(b"\n")[0])
            if resp.get("error") == "success":
                return resp.get("data")
        except Exception:
            pass
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        return None

    def _cache_chapter_list_bg(self, generation: int):
        """Background thread: fetch chapter-list once mpv has read the CDDA TOC.

        mpv creates the IPC socket before CDDA is fully demuxed, so
        chapter-list returns 'property unavailable' for several seconds after
        startup.  Retry for up to 10 s, then fall back to MusicBrainz durations.
        """
        for attempt in range(10):
            time.sleep(1.0)
            with self._lock:
                if generation != self._mpv_generation or not self._mpv_running():
                    return
                if self._chapter_starts:
                    return
            chapter_list = self._ipc_query("chapter-list", timeout=2.0)
            if chapter_list and isinstance(chapter_list, list) and len(chapter_list) > 0:
                starts = [float(c.get("time", 0)) for c in chapter_list]
                with self._lock:
                    if generation != self._mpv_generation:
                        return
                    self._chapter_starts = starts
                logger.info(
                    f"Cached {len(self._chapter_starts)} chapter starts "
                    f"(attempt {attempt + 1}): {[round(t, 2) for t in self._chapter_starts[:4]]}…"
                )
                return
        logger.warning("chapter-list unavailable after 10 s — using TOC durations as fallback")

    def _ipc_get_time_pos(self) -> Optional[float]:
        val = self._ipc_query("time-pos")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def _elapsed_now(self) -> float:
        pos = self._ipc_get_time_pos()
        if pos is not None:
            return pos
        return self._track_elapsed + (time.monotonic() - self._track_start)

    # ------------------------------------------------------------------
    # Internal: monitor thread
    # ------------------------------------------------------------------

    def _monitor_mpv(self, generation: int):
        """
        Watch mpv events via IPC:
        - chapter-change → update _current_track, fire on_track_end for prev
        - end-file       → disc ended, fire on_track_end for last track
        """
        # Wait for IPC socket
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _IPC_SOCK.exists():
                break
            time.sleep(0.15)
        else:
            return

        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(_IPC_SOCK))
            s.settimeout(1.0)
            # Subscribe to chapter-change, pause, and cache-stall events
            s.sendall(b'{"command":["observe_property",1,"chapter"]}\n')
            s.sendall(b'{"command":["observe_property",2,"pause"]}\n')
            s.sendall(b'{"command":["observe_property",3,"paused-for-cache"]}\n')
        except Exception as e:
            logger.warning(f"Monitor IPC connect failed: {e}")
            return

        buf = b""
        end_handled       = False
        _stall_pos:   Optional[float] = None
        _stall_since: float = time.monotonic()
        _last_pos_chk: float = time.monotonic()
        _paused_for_cache:  bool  = False
        _cache_stall_since: float = 0.0   # monotonic time when paused-for-cache first became True
        # observe_property emits one immediate notification per property with
        # the current value at subscription time. Those baseline notifications
        # are not transitions — pause=true at startup just means we passed
        # --pause, and paused-for-cache=false is mpv's idle steady state.
        # Suppress them so the journal only records real events.
        _pause_baseline_seen: bool = False
        _cache_baseline_seen: bool = False

        while True:
            with self._lock:
                if generation != self._mpv_generation or not self._mpv_running():
                    break
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                # Got IPC data — reset stall timer
                _stall_since = time.monotonic()
            except socket.timeout:
                # Stall watchdog: check time-pos every 2 s.
                # If it hasn't advanced in 8 s while playing,
                # assume disc was ejected and mpv is hung.
                now = time.monotonic()
                if now - _last_pos_chk >= 2.0 and not self._paused and not self._starting:
                    _last_pos_chk = now
                    if _paused_for_cache:
                        if now - _cache_stall_since < 5.0:
                            # Buffer drained — give the drive 5 s to recover before
                            # the stall watchdog fires.  Beyond that it's genuinely stuck.
                            _stall_since = now
                    pos = self._ipc_query("time-pos")
                    if pos is None:
                        pass  # transient IPC failure — don't touch stall tracking
                    elif pos != _stall_pos:
                        _stall_pos   = pos
                        _stall_since = now
                    elif now - _stall_since > 8.0:
                        logger.warning(
                            f"Playback stalled >{now - _stall_since:.0f}s "
                            f"(time-pos={pos}) — disc error"
                        )
                        end_handled = True  # prevent double-fire from cleanup
                        if self._on_disc_error:
                            self._on_disc_error()
                        break
                continue
            except Exception:
                break

            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue

                event = msg.get("event")

                if event == "property-change" and msg.get("name") == "paused-for-cache":
                    now_pfc = time.monotonic()
                    new_paused = bool(msg.get("data"))
                    if not _cache_baseline_seen:
                        # Initial baseline notification — anchor the timer so any
                        # later true→false transition computes a sensible delta.
                        _cache_baseline_seen = True
                        _cache_stall_since = now_pfc
                        _paused_for_cache = new_paused
                    elif new_paused and not _paused_for_cache:
                        # Real stall: false→true transition.
                        _cache_stall_since = now_pfc
                        _paused_for_cache = True
                        logger.warning("mpv paused waiting for drive data (CDDA read retry)")
                    elif (not new_paused) and _paused_for_cache:
                        # Real recovery: true→false transition we previously logged.
                        _paused_for_cache = False
                        logger.info(
                            f"mpv cache refilled after "
                            f"{now_pfc - _cache_stall_since:.1f}s — resuming"
                        )
                    # Other combinations (false→false redundant notifications) are
                    # mpv's internal bookkeeping; no event from the user's POV.

                elif event == "property-change" and msg.get("name") == "pause":
                    paused = msg.get("data")
                    if not _pause_baseline_seen:
                        # Initial baseline notification — mpv was started with
                        # --pause, so the first observed value is true. Not an
                        # auto-pause event; suppress to avoid misleading logs.
                        _pause_baseline_seen = True
                    elif paused is True:
                        logger.warning("mpv auto-paused (CDDA buffer underrun or drive stall)")
                    elif paused is False:
                        logger.info("mpv auto-resumed")

                elif event == "property-change" and msg.get("name") == "chapter":
                    new_chapter = msg.get("data")
                    if new_chapter is not None:
                        new_track = new_chapter + 1  # 1-indexed
                        with self._lock:
                            prev_track = self._current_track
                            # Only update internal state for sequential auto-advance.
                            # User seeks set _current_track via play() before the IPC
                            # command fires; monitor events from those seeks must not
                            # overwrite _current_track or trigger _on_track_end.
                            sequential = new_track == prev_track + 1 and prev_track > 0
                            if sequential:
                                self._current_track = new_track
                                self._track_elapsed = 0.0
                                self._track_start   = time.monotonic()
                        if sequential:
                            logger.info(f"Track advanced: {prev_track} → {new_track}")
                            if self._on_track_end:
                                self._on_track_end(prev_track)

                elif event == "end-file":
                    end_handled = True
                    reason = msg.get("reason", "")
                    if reason in ("stop", "quit"):
                        break
                    with self._lock:
                        last  = self._current_track
                        total = self._total_tracks
                    if reason == "eof" and last == total and total > 0:
                        # Natural end of disc — last track finished cleanly
                        logger.info(f"Disc ended at track {last}")
                        if self._on_track_end:
                            self._on_track_end(last)
                    else:
                        # Premature end: disc ejected, read error, or eof mid-disc
                        logger.info(f"mpv end-file reason={reason} at track {last}/{total} — disc error")
                        if self._on_disc_error:
                            self._on_disc_error()
                    break

        try:
            s.close()
        except Exception:
            pass

        # Clean up mpv proc reference if it has exited unexpectedly.
        # If end-file was never received (race: mpv exited before the event was
        # processed), fire _on_disc_error so the UI doesn't stay frozen.
        should_error = False
        with self._lock:
            if (
                generation == self._mpv_generation
                and self._mpv_proc
                and self._mpv_proc.poll() is not None
            ):
                self._mpv_proc = None
                self._current_track = 0
                should_error = not end_handled

        if should_error and self._on_disc_error:
            logger.info("mpv exited without end-file event — firing disc error")
            self._on_disc_error()


# ---------------------------------------------------------------------------
# Module-level singleton + simple function API
# ---------------------------------------------------------------------------

_player = CDPlayer()


def setup_player(on_track_end_callback: Callable[[int], None],
                 on_disc_error_callback: Optional[Callable[[], None]] = None):
    _player.set_on_track_end(on_track_end_callback)
    if on_disc_error_callback:
        _player.set_on_disc_error(on_disc_error_callback)


def drive_busy() -> bool:
    return _player.drive_busy


def _track_start_sec(track_number: int) -> float:
    """Cumulative start time in seconds for track_number (1-indexed)."""
    try:
        from state import get_state
        tracks = get_state().tracks
        return float(sum(t.duration for t in tracks[:track_number - 1]))
    except Exception:
        return 0.0


def play(track_number: Optional[int] = None,
         total_tracks: int = 0,
         alsa_device: str = ALSA_DEVICE):
    track_num = track_number or 1
    _player.play(
        track_num,
        total_tracks,
        cdrom_device=CDROM_DEVICE,
        alsa_device=alsa_device,
        seek_to_sec=_track_start_sec(track_num),
    )


def pause_toggle():
    if _player.is_paused():
        _player.resume()
    else:
        _player.pause()


def resume():
    _player.resume()


def stop():
    _player.stop()


def next_track():
    from state import get_state
    state = get_state()
    current = _player.current_track()
    if current <= 0 or not state.tracks:
        return
    next_num = current + 1
    if next_num > len(state.tracks):
        return
    _player.play(next_num, len(state.tracks),
                 cdrom_device=CDROM_DEVICE, alsa_device=state.alsa_device,
                 seek_to_sec=_track_start_sec(next_num))
    # Monitor thread won't fire _on_track_end (current_track already set to next_num by play())
    # so update main state directly
    state.track_number = next_num
    state.track_title  = state.tracks[next_num - 1].title
    state.elapsed      = 0


def prev_track(alsa_device: str = ALSA_DEVICE):
    from state import get_state
    state = get_state()
    current = _player.current_track()
    # Use the monotonic timer for the prev-direction decision.  IPC time-pos
    # reflects actual CDDA sector reads which lag behind wall-clock time
    # (CDDA seek/spin-up latency), so it under-reports elapsed time right
    # after a chapter jump and would always send us to the previous track.
    # The timer is reset on every play() call and accurately measures how
    # long we have been in the current track from the user's perspective.
    if not state.tracks or current < 1:
        logger.warning(f"prev_track: no active track (current={current}), ignoring")
        return
    elapsed = _player._get_timer_elapsed()
    # <5s into track and not on first track → go to previous track
    # otherwise (≥5s, or on track 1) → restart current track
    target = (current - 1) if elapsed < 5.0 and current > 1 else current
    logger.info(f"prev_track: current={current}, elapsed={elapsed:.1f}s → target={target}")
    _player.play(target, len(state.tracks), alsa_device=alsa_device,
                 seek_to_sec=_track_start_sec(target))
    # Monitor thread won't fire (current_track already set to target by play())
    state.track_number = target
    state.track_title  = state.tracks[target - 1].title
    state.elapsed      = 0


def eject():
    _player.stop()
    subprocess.run(["eject", CDROM_DEVICE], check=True, timeout=10)


def get_player_status() -> dict:
    return _player.get_status()
