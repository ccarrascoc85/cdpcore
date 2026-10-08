"""Bounded CDDA reads and loss concealment; the output is s16le 44100 Hz stereo.

Linux device access is lazy so the policy can be exercised with fake drives on
other hosts. Only damaged samples pass through the concealment routines.
"""
import array
import collections
import ctypes
import logging
import math
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

FRAME = 2352
C2 = 294
FPS = 75
SILENCE_FRAME = bytes(FRAME)
SG_IO = 0x2285
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Track:
    number: int
    start_lba: int
    end_lba: int
    is_data: bool = False


@dataclass(frozen=True)
class Segment:
    track: int
    start_lba: int
    end_lba: int


class Toc:
    def __init__(self, tracks):
        self.tracks = list(tracks)

    @classmethod
    def read(cls, device="/dev/cdrom"):
        import fcntl
        fd = os.open(device, os.O_RDONLY | NONBLOCK)
        try:
            hdr = bytearray(2)
            fcntl.ioctl(fd, 0x5305, hdr)
            first, last = hdr
            starts = {}
            for t in list(range(first, last + 1)) + [0xAA]:
                entry = bytearray(struct.pack("BBBBiB3x", t, 0, 1, 0, 0, 0))
                fcntl.ioctl(fd, 0x5306, entry)
                _, ctrl, _, _, lba, _ = struct.unpack("BBBBiB3x", entry)
                starts[t] = (lba, bool(ctrl & 0x40))
            return cls(Track(t, starts[t][0], starts[t + 1 if t < last else 0xAA][0],
                             starts[t][1]) for t in range(first, last + 1))
        finally:
            os.close(fd)

    def audio_tracks(self):
        return [t for t in self.tracks if not t.is_data]

    def plan_from(self, track, offset_frames=0):
        if offset_frames < 0:
            raise ValueError("offset_frames must be nonnegative")
        tracks = self.audio_tracks()
        if not any(t.number == track for t in tracks):
            raise ValueError("No audio track at requested number")
        return [Segment(t.number, min(t.end_lba, t.start_lba +
                        (offset_frames if t.number == track else 0)), t.end_lba)
                for t in tracks if t.number >= track]

    @staticmethod
    def locate(plan, frames_consumed):
        pos = max(0, frames_consumed)
        for seg in plan:
            size = seg.end_lba - seg.start_lba
            if pos < size:
                return seg.track, pos / FPS
            pos -= size
        if not plan:
            return 0, 0.0
        last = plan[-1]
        return last.track, (last.end_lba - last.start_lba) / FPS


def max_frames_per_read(device="/dev/cdrom", c2=False):
    try:
        name = os.path.basename(os.path.realpath(device))
        kb = int(Path(f"/sys/block/{name}/queue/max_hw_sectors_kb").read_text())
        return max(1, min(75, kb * 1024 // (FRAME + (C2 if c2 else 0))))
    except (OSError, ValueError):
        return max(1, 45 * FRAME // (FRAME + (C2 if c2 else 0)))


class ReadError(Exception):
    def __init__(self, message, sense="", host_status=0):
        super().__init__(message)
        self.sense = sense
        self.host_status = host_status

    @property
    def stalled(self):
        return self.host_status == 3  # DID_TIME_OUT


class DriveTimeout(ReadError):
    """Two successive kernel-aborted commands, rather than media errors."""


class SgIoHdr(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p), ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint), ("info", ctypes.c_uint),
    ]


def _command(fd, cdb, buf, sense, size, timeout_ms, direction=-3):
    import fcntl
    cmd = (ctypes.c_ubyte * len(cdb))(*cdb)
    hdr = SgIoHdr()
    hdr.interface_id = ord("S")
    hdr.dxfer_direction = direction
    hdr.cmd_len, hdr.mx_sb_len, hdr.dxfer_len = len(cdb), len(sense), size
    hdr.dxferp, hdr.cmdp, hdr.sbp = (ctypes.cast(x, ctypes.c_void_p)
                                     for x in (buf, cmd, sense))
    hdr.timeout = timeout_ms
    fcntl.ioctl(fd, SG_IO, hdr)
    if hdr.status or hdr.host_status or hdr.driver_status or hdr.resid:
        sb = sense.raw[:hdr.sb_len_wr]
        if len(sb) > 13 and sb[0] & 0x7F in (0x70, 0x71):
            code = f"{sb[2] & 15:02x}/{sb[12]:02x}/{sb[13]:02x}"
        elif len(sb) >= 4 and sb[0] & 0x7F in (0x72, 0x73):
            code = f"{sb[1] & 15:02x}/{sb[2]:02x}/{sb[3]:02x}"
        else:
            code = ""
        raise ReadError(f"status={hdr.status} host={hdr.host_status} sense={code or '-'}",
                        code, hdr.host_status)


def read_cd(fd, lba, nframes, timeout_ms, buf, sense, c2=False):
    size = nframes * (FRAME + (C2 if c2 else 0))
    if nframes <= 0 or size > len(buf):
        raise ValueError("READ CD exceeds allocated buffer")
    cdb = [0xBE, 4, (lba >> 24) & 255, (lba >> 16) & 255,
           (lba >> 8) & 255, lba & 255, (nframes >> 16) & 255,
           (nframes >> 8) & 255, nframes & 255, 0x12 if c2 else 0x10, 0, 0]
    start = time.monotonic()
    _command(fd, cdb, buf, sense, size, timeout_ms)
    return time.monotonic() - start


class Drive:
    """Own one writable device descriptor; restore factory retries on every exit."""
    def __init__(self, device="/dev/cdrom", timeout_ms=8000, probe_lba=0, retry_count=128,
                 c2_supported=None):
        self.fd = os.open(device, os.O_RDWR | NONBLOCK)
        self.lock = threading.Lock()
        self.buf = ctypes.create_string_buffer(75 * (FRAME + C2))
        self.sense = ctypes.create_string_buffer(32)
        self.timeout_ms, self.retry, self.c2 = timeout_ms, None, c2_supported is not False
        self.probe_error = None
        self.nframes = max_frames_per_read(device, True)
        try:
            if not self.set_retry(128):
                raise OSError("Cannot restore factory CDDA retry count")
            if retry_count != 128 and not self.set_retry(retry_count):
                raise OSError("Cannot apply CDDA drive retry policy")
            if c2_supported is None:
                try:
                    self.read(probe_lba, 1)
                except ReadError as exc:
                    self.probe_error = exc
                    if exc.sense.startswith("05/"):
                        self.c2 = False
                        logger.info("cdda: C2 pointers not available; reading without them")
                    elif exc.sense or exc.stalled:
                        logger.info("cdda: C2 probe failed; reader will classify subsequent commands")
                    else:
                        raise
            self.nframes = max_frames_per_read(device, self.c2)
        except BaseException:
            self.close()
            raise

    def set_retry(self, count):
        plist = bytes(8) + bytes([1, 10, 0x80, count]) + bytes(8)
        buf = ctypes.create_string_buffer(plist, len(plist))
        with self.lock:
            try:
                _command(self.fd, [0x55, 0x10, 0, 0, 0, 0, 0, 0, len(plist), 0],
                         buf, self.sense, len(plist), 5000, -2)
                self.retry = count
                return True
            except (ReadError, OSError):
                logger.warning("cdda: could not set drive retry count to %s", count)
                return False

    def read(self, lba, n):
        if n > self.nframes:
            raise ValueError("READ CD exceeds device maximum")
        with self.lock:
            read_cd(self.fd, lba, n, self.timeout_ms, self.buf, self.sense, self.c2)
            raw = self.buf.raw[:n * (FRAME + (C2 if self.c2 else 0))]
        if not self.c2:
            return raw, [0] * n
        width = FRAME + C2
        return (b"".join(raw[i * width:i * width + FRAME] for i in range(n)),
                [raw[i * width + FRAME:(i + 1) * width] for i in range(n)])

    def close(self):
        if self.fd is not None:
            try:
                self.set_retry(128)
            finally:
                os.close(self.fd)
                self.fd = None


def restore_factory_retry(device="/dev/cdrom"):
    """Restore page 01h without reading audio or requiring a disc."""
    try:
        fd = os.open(device, os.O_RDWR | NONBLOCK)
    except OSError as exc:
        logger.info("cdda: factory retry restore unavailable: %s", exc)
        return False
    try:
        drive = Drive.__new__(Drive)
        drive.fd, drive.lock = fd, threading.Lock()
        drive.sense = ctypes.create_string_buffer(32)
        drive.retry = None
        return drive.set_retry(128)
    finally:
        os.close(fd)


def _samples(pcm):
    a = array.array("h")
    a.frombytes(pcm)
    if sys.byteorder != "little":
        a.byteswap()
    return a


def _pcm(a):
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def endian_hint(pcm):
    a = _samples(pcm[:min(len(pcm), FRAME * 5)])
    b = a[:]
    b.byteswap()
    def delta(x):
        return sum(abs(x[i] - x[i - 2]) for i in range(2, len(x))) / max(1, len(x) - 2)
    return "BE" if delta(b) < delta(a) else "LE"


def conceal_c2(pcm, flags):
    a = _samples(pcm)
    bad = bytearray(len(a))
    for fi, pointers in enumerate(flags):
        if not pointers:
            continue
        for k in range(FRAME // 2):
            byte = 2 * k
            if any(pointers[j // 8] & (128 >> (j % 8)) for j in (byte, byte + 1)):
                bad[fi * (FRAME // 2) + k] = 1
    fixed = 0
    for ch in (0, 1):
        i = ch
        while i < len(a):
            if not bad[i]:
                i += 2
                continue
            j = i
            while j < len(a) and bad[j]:
                j += 2
            left = a[i - 2] if i >= 2 else (a[j] if j < len(a) else 0)
            right = a[j] if j < len(a) else left
            run = (j - i) // 2
            for m in range(run):
                a[i + 2 * m] = int(left + (right - left) * (m + 1) / (run + 1))
            fixed += run
            i = j
    return _pcm(a), fixed


def fade(pcm, ms=8.0, fade_in=True):
    a = _samples(pcm)
    pairs = len(a) // 2
    n = min(pairs, int(44100 * ms / 1000))
    for k in range(n):
        gain = 0.5 - 0.5 * math.cos(math.pi * (k + 1) / (n + 1))
        idx = k if fade_in else pairs - 1 - k
        a[2 * idx] = int(a[2 * idx] * gain)
        a[2 * idx + 1] = int(a[2 * idx + 1] * gain)
    return _pcm(a)


def mirror(pcm):
    return b"".join(pcm[i:i + 4] for i in range(len(pcm) - 4, -1, -4))


class Lookahead:
    """Bounded FIFO, normally two seconds, at most twenty under damage."""
    def __init__(self, min_frames=2 * FPS, max_frames=20 * FPS, calm_s=60.0):
        self.min, self.max, self.calm_s = min_frames, max_frames, calm_s
        self.target = self.peak_target = min_frames
        self.frames = self.underruns = 0
        self.underrun_time = 0.0
        self.last_trouble = None
        self.q = collections.deque()
        self.cv = threading.Condition()
        self.closed = False

    def trouble(self, seconds=20.0):
        with self.cv:
            self.target = min(self.max, max(self.target, int(seconds * FPS)))
            self.peak_target = max(self.peak_target, self.target)
            self.last_trouble = time.monotonic()
            self.cv.notify_all()

    def put(self, pcm, n=None):
        n = len(pcm) // FRAME if n is None else n
        if len(pcm) != n * FRAME:
            raise ValueError("PCM length does not match frame count")
        offset = 0
        with self.cv:
            while offset < n and not self.closed:
                if self.last_trouble is not None and time.monotonic() - self.last_trouble >= self.calm_s:
                    self.target, self.last_trouble = self.min, None
                room = self.target - self.frames
                if room <= 0:
                    self.cv.wait(0.2)
                    continue
                k = min(room, n - offset)
                self.q.append((pcm[offset * FRAME:(offset + k) * FRAME], k))
                self.frames += k
                offset += k
                self.cv.notify_all()
        return offset == n

    def get(self):
        with self.cv:
            t0 = None
            while not self.q and not self.closed:
                if t0 is None:
                    t0 = time.monotonic()
                    self.underruns += 1
                self.cv.wait()
            if t0 is not None:
                self.underrun_time += time.monotonic() - t0
            if not self.q:
                return None
            pcm, n = self.q.popleft()
            self.frames -= n
            self.cv.notify_all()
            return pcm, n

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()


class _Concealer:
    """Keep two good frames back so a left guard and its fade remain editable.

    Hold at most eight gap frames to choose a full-level mirror or silence.
    Longer runs stream silence immediately, with no unbounded PCM storage.
    """
    def __init__(self, emit, muted, treatment):
        self.emit, self.muted, self.treatment = emit, muted, treatment
        self.good = collections.deque()
        self.history = collections.deque(maxlen=8)
        self.gap = 0
        self.long_gap = False
        self.right_guard = False
        self.fading_in = False
        self.before_gap = None

    def _good_out(self, pcm):
        # Mirror material is the actual preceding output, including earlier
        # silence/fills, rather than old good audio across a previous hole.
        for offset in range(0, len(pcm), FRAME):
            self.history.append(pcm[offset:offset + FRAME])
        self.emit(pcm)

    def _gap_add(self):
        self.muted()
        self.gap += 1
        if self.gap > 8 or self.long_gap:
            self._before_out(False)
            self._good_out(SILENCE_FRAME * self.gap)
            self.gap = 0
            self.long_gap = True

    def _before_out(self, mirroring):
        if self.before_gap is not None:
            pcm = self.before_gap if mirroring else fade(self.before_gap, fade_in=False)
            self._good_out(pcm)
            self.before_gap = None

    def _gap_end(self):
        pre = b"".join(self.history) + (self.before_gap or b"")
        mirroring = not self.long_gap and self.treatment == "mirror" and len(pre) >= self.gap * FRAME
        self._before_out(mirroring)
        if mirroring:
            fill = fade(mirror(pre[-self.gap * FRAME:]), fade_in=False)
        else:
            fill = SILENCE_FRAME * self.gap
        if fill:
            self._good_out(fill)
        self.gap, self.long_gap = 0, False
        self.fading_in = True

    def feed(self, pcm):
        if pcm is None:
            if not self.gap and not self.long_gap:
                if self.good:
                    self.good.pop()  # one left guard, including across chunk/track boundaries
                    self._gap_add()
                while self.good:
                    self.before_gap = self.good.popleft()
            self._gap_add()
            self.right_guard = True
            return
        if self.right_guard:
            self._gap_add()
            self.right_guard = False
            return
        if self.gap or self.long_gap:
            self._gap_end()
        if self.fading_in:
            pcm = fade(pcm, fade_in=True)
            self.fading_in = False
        self.good.append(pcm)
        if len(self.good) > 2:
            self._good_out(self.good.popleft())

    def finish(self):
        if self.gap or self.long_gap:
            self._gap_end()
        while self.good:
            self._good_out(self.good.popleft())


class _InjectedDrive:
    def __init__(self, read, nframes):
        self.read, self.nframes, self.c2 = read, nframes, False

    def set_retry(self, count):
        return True

    def close(self):
        pass


class SalvageReader(threading.Thread):
    GROUP = 13

    def __init__(self, device, plan, lookahead, *, nframes=None, timeout_ms=8000,
                 probe_steps_s=(2, 8, 32, 128, 512), byteswap=False,
                 on_event=None, _read=None, mode="continuous", gap_treatment="mirror",
                 reserve_s=1.5, c2_supported=None):
        super().__init__(daemon=True, name="cdda-reader")
        if mode not in ("continuous", "recovery") or gap_treatment not in ("mirror", "silence"):
            raise ValueError("Invalid playback policy")
        if nframes is not None and nframes <= 0:
            raise ValueError("nframes must be positive")
        self.device, self.plan, self.la = device, list(plan), lookahead
        self.nframes, self.timeout_ms = nframes, timeout_ms
        self.probe_steps_s, self.byteswap = probe_steps_s, byteswap
        self.on_event, self._read = on_event, _read
        self.mode, self.gap_treatment, self.reserve_s = mode, gap_treatment, reserve_s
        self.stop_flag = threading.Event()
        self.first_pcm = threading.Event()
        self.sense_failure = threading.Event()
        self.finished = threading.Event()
        self.stats_lock = threading.Lock()
        self.stats = dict(reads=0, failures=0, read_time=0.0, unreadable_frames=0,
                          c2_frames=0, samples_interpolated=0, muted_unattempted=0,
                          spans=[], kernel_aborts=0)
        self.c2_available = None
        self.c2_supported = c2_supported
        self.error = None
        self.drive = None
        self._aborts = 0
        self._consecutive_errors = 0
        self._endian_checked = False
        self._inject = None
        spec = os.environ.get("CDPCORE_CDDA_INJECT_FAIL")
        if spec:
            parts = spec.split(":")
            if len(parts) not in (2, 3):
                raise ValueError("Invalid CDPCORE_CDDA_INJECT_FAIL")
            a, b = map(int, parts[:2])
            cost = float(parts[2]) if len(parts) == 3 else 0.3
            if a >= b or cost < 0:
                raise ValueError("Invalid CDPCORE_CDDA_INJECT_FAIL range or cost")
            self._inject = (a, b, cost)
        self._concealer = _Concealer(self._output, self._muted, gap_treatment)

    def snapshot(self):
        with self.stats_lock:
            return {**self.stats, "spans": list(self.stats["spans"])}

    def _count(self, key, n=1):
        with self.stats_lock:
            self.stats[key] += n

    def _event(self, kind, **info):
        if self.on_event:
            self.on_event(kind, **info)

    def stop(self):
        self.stop_flag.set()
        self.la.close()

    def _output(self, pcm):
        if self.stop_flag.is_set():
            return
        if self.la.put(pcm, len(pcm) // FRAME) and not self.first_pcm.is_set():
            self.first_pcm.set()
            self._event("first_pcm")

    def _muted(self):
        self._count("unreadable_frames")

    def _try(self, lba, n):
        if self.stop_flag.is_set():
            return None
        t0 = time.monotonic()
        self._count("reads")
        try:
            if self._inject and lba < self._inject[1] and lba + n > self._inject[0]:
                self.stop_flag.wait(self._inject[2])
                raise ReadError("injected unreadable sector", "03/11/00")
            result = self.drive.read(lba, n)
            pcm, flags = result if isinstance(result, tuple) else (result, [0] * n)
            if len(pcm) != FRAME * n or len(flags) != n:
                raise ReadError("Short READ CD response")
            self._aborts = 0
            self._consecutive_errors = 0
            flags = [f if f and any(f) else 0 for f in flags]
            if any(flags):
                self.la.trouble()
            return pcm, flags
        except (ReadError, OSError) as exc:
            self._count("failures")
            self._consecutive_errors += 1
            self.la.trouble()
            if isinstance(exc, ReadError) and exc.stalled:
                self._aborts += 1
                self._count("kernel_aborts")
                if self._aborts >= 2:
                    raise DriveTimeout("Two consecutive kernel-aborted READ CD commands", host_status=3)
            else:
                self._aborts = 0
                if getattr(exc, "sense", ""):
                    self.sense_failure.set()
            dt = time.monotonic() - t0
            logger.warning("cdda: read error at LBA %s (%s fr) after %.1fs: %s; silence",
                           lba, n, dt, getattr(exc, "sense", "") or str(exc))
            self._event("read_error", lba=lba, n=n, sense=getattr(exc, "sense", ""))
            return None
        finally:
            dt = time.monotonic() - t0
            self._count("read_time", dt)
            if dt >= 0.3 and dt >= 0.5 * n / FPS:
                self.la.trouble()

    def _emit(self, got):
        pcm, flags = got
        if not self._endian_checked:
            self.byteswap = self.byteswap or endian_hint(pcm) == "BE"
            self._endian_checked = True
            if self.byteswap:
                logger.info("cdda: big-endian drive PCM; swapping sample bytes")
        if self.byteswap:
            a = array.array("h")
            a.frombytes(pcm)
            a.byteswap()
            pcm = a.tobytes()
        if any(flags):
            self._count("c2_frames", sum(bool(f and any(f)) for f in flags))
            pcm, fixed = conceal_c2(pcm, flags)
            self._count("samples_interpolated", fixed)
        for i in range(len(flags)):
            self._concealer.feed(pcm[i * FRAME:(i + 1) * FRAME])

    def _mute(self, n, unattempted=False):
        if unattempted:
            self._count("muted_unattempted", n)
            logger.warning("cdda: %s consecutive errors, skipping %s fr",
                           self._consecutive_errors, n)
        for _ in range(n):
            if self.stop_flag.is_set():
                return
            self._concealer.feed(None)

    @staticmethod
    def _slice(got, offset, n):
        return got[0][offset * FRAME:(offset + n) * FRAME], got[1][offset:offset + n]

    def _reserve_low(self):
        return self.mode == "continuous" and self.la.frames < self.reserve_s * FPS

    def _group(self, g, k, start, end):
        if self.mode == "continuous":
            # Radial scratches fail in bursts: two failures four frames apart
            # cover the intervening frames without another costly attempt each.
            rec, f, stop = 0, g, g + k
            while f < stop and not self.stop_flag.is_set():
                if self._reserve_low():
                    self._mute(stop - f, True)
                    return rec
                got = self._try(f, 1)
                if got:
                    self._emit(got)
                    rec += 1
                    f += 1
                    continue
                self._mute(1)
                pr = f + 4
                if pr >= stop:
                    f += 1
                    continue
                if self._reserve_low():
                    self._mute(stop - f - 1, True)
                    return rec
                ahead = self._try(pr, 1)
                if ahead is None:
                    self._mute(pr - f)
                else:
                    for m in range(f + 1, pr):
                        if self._reserve_low():
                            self._mute(stop - m, True)
                            return rec
                        got = self._try(m, 1)
                        if got:
                            self._emit(got)
                            rec += 1
                        else:
                            self._mute(1)
                    self._emit(ahead)
                    rec += 1
                f = pr + 1
            return rec
        if self.mode == "recovery":
            for _ in range(2):
                got = self._try(g, k)
                if got:
                    self._emit(got)
                    return k
            s, e = max(start, g - 6), min(end, g + k + 6)
            if e - s <= self.drive.nframes:
                got = self._try(s, e - s)
                if got:
                    self._emit(self._slice(got, g - s, k))
                    return k
        rec = 0
        for q in range(g, g + k, 4):
            m = min(4, g + k - q)
            if self._reserve_low():
                self._mute(g + k - q, True)
                return rec
            got = self._try(q, m)
            if got:
                self._emit(got)
                rec += m
                continue
            if self.mode == "recovery":
                size = min(13, end - start, self.drive.nframes)
                cs = max(start, min(q - 4, end - size))
                if cs + size >= q + m:
                    got = self._try(cs, size)
                    if got:
                        self._emit(self._slice(got, q - cs, m))
                        rec += m
                        continue
            for f in range(q, q + m):
                if self._reserve_low():
                    self._mute(g + k - f, True)
                    return rec
                got = None
                for _ in range(3 if self.mode == "recovery" else 1):
                    got = self._try(f, 1)
                    if got or self.stop_flag.is_set():
                        break
                if got:
                    self._emit(got)
                    rec += 1
                else:
                    self._mute(1)
        return rec

    def _resolve_span(self, fail_lba, end):
        """Dead-band resolver: cache identical probes, refine both edges.

        This is used only after two groups fail the entire recovery ladder.
        Distant failing probes alone cannot prove a band on arbitrary media;
        progressive output is therefore deferred until edge resolution finishes.
        """
        t0 = time.monotonic()
        cache = {}
        pf = po = 0
        def readable(lba, n):
            nonlocal pf, po
            while n > 0 and not self.stop_flag.is_set():
                k = min(self.drive.nframes, n)
                key = (lba, k)
                if key not in cache:
                    cache[key] = self._try(lba, k)
                    if cache[key] is None:
                        pf += 1
                    else:
                        po += 1
                if cache[key] is None:
                    return False
                lba, n = lba + k, n - k
            return n == 0
        n = min(self.drive.nframes, end - fail_lba)
        cache[(fail_lba, n)] = None
        lo, hi, last_bad = fail_lba + n, end, fail_lba
        for step in self.probe_steps_s:
            p = fail_lba + int(step * FPS)
            if p >= end:
                break
            if readable(p, min(n, end - p)):
                hi = p
                break
            lo, last_bad = p + n, p
        while hi - lo > n:
            mid = lo + ((hi - lo) // (2 * n)) * n
            if mid <= lo:
                break
            if readable(mid, min(n, end - mid)):
                hi = mid
            else:
                lo, last_bad = mid + n, mid
        def edge(base, limit, suffix=False):
            low, k = 0, 1
            while k <= limit:
                if not readable(base - k if suffix else base, k):
                    break
                low, k = k, k * 2
            high = min(k, limit + 1)
            while high - low > 1:
                mid = (low + high) // 2
                if readable(base - mid if suffix else base, mid):
                    low = mid
                else:
                    high = mid
            return base - low if suffix else base + low
        first_bad = edge(fail_lba, n - 1)
        # Segment end is a virtual good boundary: even when no full probe
        # fits after a band, salvage its short readable suffix before EOF.
        first_good = edge(hi, hi - max(first_bad, last_bad), True)
        span = (first_bad, first_good, time.monotonic() - t0, pf, po)
        with self.stats_lock:
            self.stats["spans"].append(span)
        self._event("span_resolved", start=first_bad, end=first_good)
        return first_bad, first_good

    def _salvage(self, lba, n, start, end):
        g, stop, dead = lba, lba + n, 0
        group = min(13, self.drive.nframes) if self.mode == "recovery" else n
        while g < stop and not self.stop_flag.is_set():
            k = min(group, stop - g)
            rec = self._group(g, k, start, end)
            dead = dead + 1 if rec == 0 else 0
            g += k
            if self.mode == "recovery" and dead >= 2 and g < end:
                # Confirm the next failed chunk before invoking the band resolver.
                got = self._try(g, min(self.drive.nframes, end - g))
                if got:
                    self._emit(got)
                    return g + len(got[1])
                bad, good = self._resolve_span(g, end)
                if bad > g:
                    for p in range(g, bad, self.drive.nframes):
                        m = min(self.drive.nframes, bad - p)
                        prefix = self._try(p, m)
                        self._emit(prefix) if prefix else self._mute(m)
                self._mute(good - bad)
                return good
        return g

    def run(self):
        try:
            if self._read:
                self.drive = _InjectedDrive(self._read, self.nframes or 45)
            elif hasattr(self.device, "read"):
                self.drive = self.device
            else:
                self.drive = Drive(self.device, self.timeout_ms, self.plan[0].start_lba,
                                   retry_count=8 if self.mode == "continuous" else 128,
                                   c2_supported=self.c2_supported)
                if self.drive.probe_error and self.drive.probe_error.stalled:
                    self._aborts = 1
                elif self.drive.probe_error and self.drive.probe_error.sense and self.drive.c2:
                    self.sense_failure.set()
            self.c2_available = self.drive.c2
            if self.nframes is not None:
                self.drive.nframes = min(self.drive.nframes, self.nframes)
            if self.plan:
                logger.info("cdda: segment from T%s (+0 fr), chunk=%s fr",
                            self.plan[0].track, self.drive.nframes)
            if not self.drive.set_retry(8 if self.mode == "continuous" else 128):
                raise OSError("Cannot apply CDDA drive retry policy")
            for seg in self.plan:
                lba = seg.start_lba
                while lba < seg.end_lba and not self.stop_flag.is_set():
                    n = min(self.drive.nframes, seg.end_lba - lba)
                    got = self._try(lba, n)
                    if self.stop_flag.is_set():
                        break
                    if got:
                        self._emit(got)
                        lba += n
                    else:
                        lba = self._salvage(lba, n, seg.start_lba, seg.end_lba)
                if not self.stop_flag.is_set():
                    self._event("segment_done", track=seg.track)
            if not self.stop_flag.is_set():
                self._concealer.finish()
                self.finished.set()
                self._event("finished")
        except BaseException as exc:
            self.error = exc
            logger.exception("cdda: reader failed")
            self._event("error", error=exc)
        finally:
            try:
                if self.drive:
                    if not isinstance(self.drive, Drive):
                        self.drive.set_retry(128)
                    self.drive.close()
            finally:
                self.la.close()
                st = self.snapshot()
                logger.info("cdda: reader stopped after %s reads, %s errors", st["reads"], st["failures"])


CddaReader = SalvageReader


class PipeWriter(threading.Thread):
    def __init__(self, lookahead, pipe):
        super().__init__(daemon=True, name="cdda-pipe")
        self.la, self.pipe = lookahead, pipe
        self.frames_written = 0
        self.error = None

    @property
    def underruns(self):
        return self.la.underruns

    def run(self):
        try:
            while True:
                item = self.la.get()
                if item is None:
                    break
                pcm, n = item
                view = memoryview(pcm)
                while view:
                    count = self.pipe.write(view)
                    if count is None:
                        count = len(view)
                    if count <= 0:
                        raise BrokenPipeError("PCM pipe made no progress")
                    view = view[count:]
                self.frames_written += n
        except (OSError, ValueError) as exc:
            self.error = exc
            self.la.close()
        finally:
            try:
                self.pipe.close()
            except (OSError, ValueError):
                pass
