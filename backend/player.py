"""CDDA segments streamed into mpv's raw PCM stdin; control through JSON IPC."""
import atexit
import json
import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from cdda_source import Drive, DriveTimeout, Lookahead, PipeWriter, SalvageReader, Toc, FPS

logger = logging.getLogger(__name__)
CDROM_DEVICE = "/dev/cdrom"
ALSA_DEVICE = os.environ.get("ALSA_DEVICE", "hw:0,0")
_IPC_SOCK = Path(os.environ.get("CD_CACHE_DIR", "/var/cache/cd-player")) / "mpv.sock"
COLD_START_TOC_TIMEOUT = 15.0


def _aplay_device(hw_device):
    return "plughw:" + hw_device[3:] if hw_device.startswith("hw:") else hw_device


class DriveStallError(RuntimeError):
    """The optical drive stopped answering bounded read commands."""


class CDPlayer:
    """Locks cover state only; device I/O and teardown run off request locks."""
    def __init__(self):
        self._lock = threading.RLock()
        self._start_lock = threading.RLock()
        self._ipc_lock = threading.Lock()
        self._ipc_sock = self._mpv_proc = None
        self._reader = self._writer = self._lookahead = None
        self._toc, self._plan = None, []
        self._current_track = self._total_tracks = 0
        self._paused = self._starting = self._cold_start_pending = False
        self._mpv_generation = self._request_revision = 0
        self._track_elapsed = self._track_start = self._segment_started = 0.0
        self._last_pos = self._startup_error = None
        self._on_track_end = self._on_disc_error = None
        self._cleanup_done = threading.Event()
        self._cleanup_done.set()
        self._mode, self._gap_treatment = "continuous", "mirror"
        self._disc_c2 = None

    def set_on_track_end(self, callback):
        self._on_track_end = callback

    def set_on_disc_error(self, callback):
        self._on_disc_error = callback

    def _settings(self):
        try:
            from playback_config import get_settings
            return get_settings()
        except ImportError:
            mode = os.environ.get("CDPCORE_READ_MODE", "continuous")
            return {"read_mode": mode if mode in ("continuous", "recovery") else "continuous",
                    "gap_treatment": "mirror"}

    def prepare_disc(self):
        """Probe C2 at disc load; each segment still reads its authoritative TOC."""
        self._disc_c2 = None
        toc = Toc.read(CDROM_DEVICE)
        tracks = toc.audio_tracks()
        if not tracks:
            return None
        first = tracks[0]
        probe_lba = min(first.start_lba + 150, first.end_lba - 1)
        drive = Drive(CDROM_DEVICE, probe_lba=probe_lba)
        try:
            self._disc_c2 = drive.c2 if drive.probe_error is None or not drive.c2 else None
            return self._disc_c2
        finally:
            drive.close()

    def play(self, track_number, total_tracks, cdrom_device=CDROM_DEVICE,
             alsa_device=ALSA_DEVICE, seek_to_sec=0.0):
        with self._start_lock:
            with self._lock:
                if self._cold_start_pending:
                    self._current_track, self._total_tracks = track_number, total_tracks
                    self._request_revision += 1
                    logger.info("cdda: queued track %s while segment starts", track_number)
                    return
            self._stop_mpv()
            with self._lock:
                self._mpv_generation += 1
                generation = self._mpv_generation
                self._request_revision += 1
                self._current_track, self._total_tracks = track_number, total_tracks
                self._paused, self._starting, self._cold_start_pending = False, True, True
                self._startup_error, self._last_pos = None, None
                self._track_start, self._track_elapsed = time.monotonic(), 0.0
                self._segment_started = self._track_start
            threading.Thread(target=self._start_segment, args=(generation, cdrom_device, alsa_device),
                             daemon=True, name="cdda-start").start()
        deadline = time.monotonic() + COLD_START_TOC_TIMEOUT
        while True:
            with self._lock:
                if generation != self._mpv_generation:
                    return
                reader, pending = self._reader, self._cold_start_pending
                error = self._startup_error or (reader.error if reader else None)
            if error:
                self.stop()
                if isinstance(error, DriveTimeout):
                    raise DriveStallError("drive_stalled") from error
                raise error
            if not pending:
                return
            if reader and reader.sense_failure.is_set():
                return  # recovery work has no playback deadline
            if time.monotonic() >= deadline:
                logger.error("Drive stalled: no PCM or sense failure within %.0fs", COLD_START_TOC_TIMEOUT)
                self.stop()
                raise DriveStallError("drive_stalled")
            time.sleep(0.02)

    def _start_segment(self, generation, device, alsa_device):
        try:
            # Old SG_IO and factory restore must complete before new retry policy.
            while not self._cleanup_done.wait(0.05):
                with self._lock:
                    if generation != self._mpv_generation:
                        return
            toc = Toc.read(device)
            while True:
                with self._lock:
                    if generation != self._mpv_generation:
                        return
                    target, revision = self._current_track, self._request_revision
                plan, settings = toc.plan_from(target), self._settings()
                la = Lookahead()
                extra = {"c2_supported": self._disc_c2} if self._disc_c2 is not None else {}
                reader = SalvageReader(device, plan, la, mode=settings["read_mode"],
                                       gap_treatment=settings["gap_treatment"], **extra)
                proc = self._spawn_mpv(device, alsa_device)
                writer = PipeWriter(la, proc.stdin)
                with self._lock:
                    stale = generation != self._mpv_generation or revision != self._request_revision
                    if not stale:
                        self._mpv_proc, self._reader, self._writer, self._lookahead = proc, reader, writer, la
                        self._toc, self._plan = toc, plan
                        self._mode, self._gap_treatment = settings["read_mode"], settings["gap_treatment"]
                        reader.start()
                if stale:
                    proc.stdin.close()
                    proc.terminate()
                    proc.wait(timeout=3)
                    continue
                while not reader.first_pcm.wait(0.02):
                    with self._lock:
                        stale = generation != self._mpv_generation or revision != self._request_revision
                    if stale or reader.error or reader.finished.is_set():
                        break
                with self._lock:
                    stale = generation != self._mpv_generation or revision != self._request_revision
                    if not stale and reader.first_pcm.is_set() and not reader.error:
                        self._cold_start_pending = False
                if stale:
                    reader.stop()
                    proc.terminate()
                    reader.join()
                    proc.stdin.close()
                    proc.wait(timeout=3)
                    with self._lock:
                        if generation != self._mpv_generation:
                            return
                        self._reader = self._writer = self._mpv_proc = None
                    continue
                if reader.error:
                    raise reader.error
                with self._lock:
                    if generation != self._mpv_generation:
                        return
                    writer.start()
                    threading.Thread(target=self._monitor_mpv, args=(generation,), daemon=True,
                                     name="cdda-monitor").start()
                return
        except Exception as exc:
            with self._lock:
                if generation == self._mpv_generation:
                    self._startup_error = exc

    def pause(self):
        with self._lock:
            if self._paused or not self.drive_busy:
                return
            self._paused = True
        self._ipc_send(["set_property", "pause", True])

    def resume(self):
        with self._lock:
            if not self._paused or not self.drive_busy:
                return
            self._paused = False
            self._track_start = time.monotonic()
        self._ipc_send(["set_property", "pause", False])

    def stop(self):
        with self._start_lock:
            self._stop_mpv()

    @property
    def cold_start_pending(self):
        with self._lock:
            return self._cold_start_pending

    @property
    def drive_busy(self):
        with self._lock:
            return self._cold_start_pending or self._mpv_running()

    def is_playing(self):
        return self.drive_busy and not self.is_paused()

    def is_paused(self):
        with self._lock:
            return self._paused and self._mpv_running()

    def is_stopped(self):
        return not self.drive_busy

    def current_track(self):
        with self._lock:
            return self._current_track

    def _get_timer_elapsed(self):
        with self._lock:
            if self._starting:
                return 0.0
            return self._track_elapsed if self._paused else self._track_elapsed + time.monotonic() - self._track_start

    def _accept_position(self, generation, pos):
        callbacks = []
        with self._lock:
            if generation != self._mpv_generation or not self._plan:
                return
            track, elapsed = Toc.locate(self._plan, max(0, float(pos) * FPS))
            previous = self._current_track
            if track != previous and previous:
                numbers = [s.track for s in self._plan]
                if previous in numbers and track in numbers:
                    callbacks = numbers[numbers.index(previous):numbers.index(track)]
            self._current_track = track
            self._track_elapsed, self._track_start = elapsed, time.monotonic()
            self._last_pos = pos
            if pos > 0:
                if self._starting:
                    logger.info("cdda: first audio after %.2fs", time.monotonic() - self._segment_started)
                self._starting, self._cold_start_pending = False, False
        for previous in callbacks:
            if self._on_track_end:
                self._on_track_end(previous)

    def get_elapsed(self):
        with self._lock:
            return self._track_elapsed if self._last_pos is not None else self._get_timer_elapsed()

    def get_status(self):
        with self._lock:
            running, paused, reader = self.drive_busy, self._paused, self._reader
            stats = reader.snapshot() if reader else {}
            return {"state": "pause" if paused else "play" if running else "stop",
                    "elapsed": str(self.get_elapsed() if running else 0.0),
                    "song": str(max(0, self._current_track - 1)), "duration": "0",
                    "buffering": self._starting and not (reader and reader.sense_failure.is_set()),
                    "read_errors": stats.get("unreadable_frames", 0),
                    "c2_frames": stats.get("c2_frames", 0),
                    "c2_available": reader.c2_available if reader else None,
                    "read_mode": self._mode, "gap_treatment": self._gap_treatment,
                    "error": "drive_stalled" if isinstance(self._startup_error, DriveStallError) else None}

    def _mpv_running(self):
        return self._mpv_proc is not None and self._mpv_proc.poll() is None

    @staticmethod
    def _proc_running(proc):
        return proc is not None and proc.poll() is None

    def _spawn_mpv(self, cdrom_device, alsa_device):
        _IPC_SOCK.parent.mkdir(parents=True, exist_ok=True)
        _IPC_SOCK.unlink(missing_ok=True)
        cmd = ["mpv", "--no-video", "--ao=alsa", f"--audio-device=alsa/{_aplay_device(alsa_device)}",
               "--demuxer=rawaudio", "--demuxer-rawaudio-format=s16le", "--demuxer-rawaudio-rate=44100",
               "--demuxer-rawaudio-channels=2", "--demuxer-max-bytes=352800", "--demuxer-max-back-bytes=0",
               "--cache=yes", "--cache-secs=1", f"--input-ipc-server={_IPC_SOCK}", "--really-quiet", "-"]
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, bufsize=0)

    def _stop_mpv(self):
        with self._lock:
            proc, reader, writer = self._mpv_proc, self._reader, self._writer
            generation = self._mpv_generation
            self._mpv_generation += 1
            self._mpv_proc = self._reader = self._writer = self._lookahead = None
            self._current_track = 0
            self._paused = self._starting = self._cold_start_pending = False
            self._last_pos, self._track_elapsed = None, 0.0
            self._plan = []
        with self._ipc_lock:
            ipc, self._ipc_sock = self._ipc_sock, None
        if reader:
            reader.stop()
        if proc or reader:
            self._cleanup_done.clear()
            threading.Thread(target=self._teardown_mpv_bg, args=(proc, generation, ipc, reader, writer),
                             daemon=True, name="cdda-stop").start()

    def _teardown_mpv_bg(self, proc, generation, ipc_sock, reader=None, writer=None):
        try:
            if ipc_sock:
                try:
                    ipc_sock.sendall(b'{"command":["quit"]}\n')
                    ipc_sock.close()
                except OSError:
                    pass
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if reader:
                reader.join()
            if proc and proc.stdin:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
            if writer and writer.ident is not None:
                writer.join(timeout=2)
            with self._lock:
                if self._mpv_proc is None:
                    _IPC_SOCK.unlink(missing_ok=True)
        finally:
            self._cleanup_done.set()

    def _monitor_mpv(self, generation):
        pause_synced = None
        while True:
            with self._lock:
                if generation != self._mpv_generation:
                    return
                reader, proc, writer, plan = self._reader, self._mpv_proc, self._writer, list(self._plan)
                paused = self._paused
            if reader.error:
                if isinstance(reader.error, DriveTimeout):
                    self._report_stall(generation)
                else:
                    self._finish_segment(generation)
                return
            if pause_synced != paused:
                with self._start_lock:
                    with self._lock:
                        if generation != self._mpv_generation:
                            return
                    if self._ipc_send(["set_property", "pause", paused]):
                        pause_synced = paused
            pos = self._ipc_query("time-pos", timeout=0.2)
            if pos is not None:
                self._accept_position(generation, pos)
            if self._starting and not paused and not reader.sense_failure.is_set() and time.monotonic() - self._segment_started >= COLD_START_TOC_TIMEOUT:
                self._report_stall(generation)
                return
            eof = self._ipc_query("eof-reached", timeout=0.2)
            if eof or proc.poll() is not None:
                if reader.finished.is_set() and writer.frames_written == sum(s.end_lba - s.start_lba for s in plan):
                    self._finish_segment(generation, last=plan[-1].track)
                else:
                    self._finish_segment(generation)
                return
            time.sleep(0.2)

    def _finish_segment(self, generation, last=None):
        # Serialize the final generation check and callback with new starts.
        # Callbacks may stop the player themselves, hence the reentrant lock.
        with self._start_lock:
            with self._lock:
                if generation != self._mpv_generation:
                    return
            if last is not None:
                self._stop_mpv()
                if self._on_track_end:
                    self._on_track_end(last)
            elif self._on_disc_error:
                self._on_disc_error()
            else:
                self._stop_mpv()

    def _report_stall(self, generation):
        with self._start_lock:
            with self._lock:
                if generation != self._mpv_generation:
                    return
            logger.error("Drive stalled: reader stopped answering; tearing segment down")
            self._stop_mpv()
            self._startup_error = DriveStallError("drive_stalled")
            if self._on_disc_error:
                self._on_disc_error()

    def _get_ipc(self) -> Optional[socket.socket]:
        """Return connected IPC socket, creating one if needed."""
        with self._ipc_lock:
            if self._ipc_sock is not None:
                return self._ipc_sock
        # Not connected yet - try to connect
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
            return False
        try:
            msg = json.dumps({"command": command}) + "\n"
            s.sendall(msg.encode())
            return True
        except Exception:
            self._close_ipc()
            return False

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
                if not chunk:
                    return None
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


# ---------------------------------------------------------------------------
# Module-level singleton + simple function API
# ---------------------------------------------------------------------------

_player = CDPlayer()


def prepare_disc():
    return _player.prepare_disc()


def setup_player(on_track_end_callback: Callable[[int], None],
                 on_disc_error_callback: Optional[Callable[[], None]] = None):
    _player.set_on_track_end(on_track_end_callback)
    if on_disc_error_callback:
        _player.set_on_disc_error(on_disc_error_callback)


def drive_busy() -> bool:
    return _player.drive_busy


def cold_start_pending() -> bool:
    return _player.cold_start_pending


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
    numbers = [t.number for t in _player._toc.audio_tracks()] if _player._toc else [t.number for t in state.tracks]
    next_num = next((n for n in numbers if n > current), None)
    if next_num is None:
        return
    _player.play(next_num, len(state.tracks),
                 cdrom_device=CDROM_DEVICE, alsa_device=state.alsa_device,
                 seek_to_sec=_track_start_sec(next_num))
    # Monitor thread won't fire _on_track_end (current_track already set to next_num by play())
    # so update main state directly
    state.track_number = next_num
    state.track_title = next((t.title for t in state.tracks if t.number == next_num), f"Track {next_num}")
    state.elapsed      = 0


def prev_track(alsa_device: str = ALSA_DEVICE):
    from state import get_state
    state = get_state()
    current = _player.current_track()
    if not state.tracks or current < 1:
        logger.warning(f"prev_track: no active track (current={current}), ignoring")
        return
    elapsed = _player.get_elapsed()
    # <5s into track and not on first track -> go to previous track
    # otherwise (>=5s, or on track 1) -> restart current track
    numbers = [t.number for t in _player._toc.audio_tracks()] if _player._toc else [t.number for t in state.tracks]
    previous = [n for n in numbers if n < current]
    target = previous[-1] if elapsed < 5.0 and previous else current
    logger.info(f"prev_track: current={current}, elapsed={elapsed:.1f}s -> target={target}")
    _player.play(target, len(state.tracks), alsa_device=alsa_device,
                 seek_to_sec=_track_start_sec(target))
    # Monitor thread won't fire (current_track already set to target by play())
    state.track_number = target
    state.track_title = next((t.title for t in state.tracks if t.number == target), f"Track {target}")
    state.elapsed      = 0


def eject():
    _player.stop()
    def _go():
        _player._cleanup_done.wait()
        subprocess.run(["eject", CDROM_DEVICE], check=True, timeout=10)
    threading.Thread(target=_go, daemon=True).start()


def get_player_status() -> dict:
    return _player.get_status()


def shutdown():
    _player.stop()
    _player._cleanup_done.wait()


atexit.register(shutdown)
