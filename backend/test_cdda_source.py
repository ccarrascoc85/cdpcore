"""CDDA policy contracts without optical hardware or Linux-only imports."""
import array
import ctypes
import io
import itertools
import os
import threading
import struct
import types
import time
import unittest
from unittest import mock

import cdda_source as c


def audio(lba, n):
    return b"".join(array.array("h", [1000 + i, -1000 - i] * 588).tobytes()
                    for i in range(lba, lba + n))


class Pipe(io.BytesIO):
    def close(self):
        self.was_closed = True


class FakeDrive:
    c2 = False
    nframes = 45

    def __init__(self, read=audio):
        self.reader = read
        self.calls, self.retries = [], []
        self.closed = False

    def read(self, lba, n):
        self.calls.append((lba, n))
        return self.reader(lba, n)

    def set_retry(self, count):
        self.retries.append(count)
        return True

    def close(self):
        self.closed = True


class BurstDrive(FakeDrive):
    """Five to eight bad frames per 17-frame revolution, optionally timed."""
    def __init__(self, start=0, end=170, failure_s=0, warning=None):
        super().__init__(self._read)
        self.bursts = [(a, min(end, a + 5 + i % 4))
                       for i, a in enumerate(range(start, end, 17))]
        self.failure_s, self.warning = failure_s, warning
        self.failed_calls = []
        self.c2 = warning == "c2"

    def bad(self, frame):
        return any(a <= frame < b for a, b in self.bursts)

    def _read(self, lba, n):
        if any(self.bad(f) for f in range(lba, lba + n)):
            self.failed_calls.append((lba, n))
            time.sleep(self.failure_s)
            raise c.ReadError("burst", "03/11/00")
        if self.warning == "slow" and lba == 45:
            time.sleep(0.31)
        flags = [bytes([128]) + bytes(c.C2 - 1) if self.c2 and f == 45 else 0
                 for f in range(lba, lba + n)]
        return audio(lba, n), flags


class SourceTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CDPCORE_CDDA_INJECT_FAIL", None)

    def render(self, drive, plan=None, **kwargs):
        la = c.Lookahead(2000, 2000)
        rd = c.SalvageReader(drive, plan or [c.Segment(1, 0, 100)], la, **kwargs)
        pipe = Pipe()
        wr = c.PipeWriter(la, pipe)
        rd.start()
        rd.join(5)
        self.assertFalse(rd.is_alive())
        self.assertIsNone(rd.error)
        wr.start()
        wr.join(2)
        self.assertFalse(wr.is_alive())
        self.assertTrue(pipe.was_closed)
        return rd, wr, pipe.getvalue()

    def test_gapless_plan_and_track_boundary(self):
        plan = [c.Segment(1, 0, 12), c.Segment(2, 12, 21)]
        rd, wr, pcm = self.render(FakeDrive(), plan)
        self.assertEqual(wr.frames_written, 21)
        self.assertEqual(pcm, audio(0, 21))
        self.assertEqual(c.Toc.locate(plan, 11), (1, 11 / 75))
        self.assertEqual(c.Toc.locate(plan, 12), (2, 0))
        self.assertTrue(rd.finished.is_set())

    def test_data_tracks_and_offset(self):
        toc = c.Toc([c.Track(1, 0, 20), c.Track(2, 20, 40, True), c.Track(3, 40, 60)])
        self.assertEqual(toc.plan_from(1, 5), [c.Segment(1, 5, 20), c.Segment(3, 40, 60)])
        with self.assertRaises(ValueError):
            toc.plan_from(2)

    def test_continuous_successful_probe_backfills_once_in_order(self):
        def read(lba, n):
            if lba < 167 and lba + n > 164:
                raise c.ReadError("bad", "03/11/00")
            return audio(lba, n)
        d = FakeDrive(read)
        rd, wr, pcm = self.render(d, [c.Segment(1, 0, 230)], reserve_s=0, gap_treatment="silence")
        self.assertEqual(wr.frames_written, 230)
        self.assertEqual(rd.snapshot()["unreadable_frames"], 5)  # 3 bad + two guards
        self.assertEqual(pcm[163 * c.FRAME:168 * c.FRAME], bytes(5 * c.FRAME))
        self.assertEqual(pcm[169 * c.FRAME:170 * c.FRAME], audio(169, 1))
        for f in range(164, 167):
            self.assertEqual(d.calls.count((f, 1)), 1)
        singles = [lba for lba, n in d.calls if n == 1]
        self.assertEqual(singles[singles.index(164):singles.index(164) + 5],
                         [164, 168, 165, 166, 167])
        self.assertEqual(d.calls.count((168, 1)), 1)
        self.assertEqual(rd.snapshot()["muted_unattempted"], 0)

    def test_continuous_periodic_burst_walker_preserves_readable_frames(self):
        # Inspect the frames sent to concealment, before its mandatory guards.
        # Vary burst alignment to cover chunk edges and short trailing probes.
        for phase in range(17):
            with self.subTest(phase=phase):
                drive = BurstDrive(start=phase)
                rd = c.SalvageReader(drive, [c.Segment(1, 0, 170)], c.Lookahead(),
                                    reserve_s=0)
                frames = []
                with mock.patch.object(rd._concealer, "feed", side_effect=frames.append):
                    rd.run()
                self.assertIsNone(rd.error)
                self.assertEqual(len(frames), 170)
                bad = {f for f in range(170) if drive.bad(f)}
                self.assertEqual({f for f, pcm in enumerate(frames) if pcm is None}, bad)
                for f, pcm in enumerate(frames):
                    if f not in bad:
                        self.assertEqual(pcm, audio(f, 1))
                self.assertLess(len(drive.failed_calls), len(bad))
                singles = [lba for lba, n in drive.calls if n == 1]
                self.assertEqual(len(singles), len(set(singles)))
                self.assertEqual(rd.snapshot()["muted_unattempted"], 0)

    def test_continuous_reserve_exhaustion_stops_walker_attempts(self):
        # Run out before the probe, before backfill, during backfill, or after
        # two failed burst endpoints. No further command may spend the reserve.
        for limit, bad, calls in [
            (1, {0}, [0]),
            (2, {0}, [0, 4]),
            (3, {0}, [0, 4, 1]),
            (2, {0, 4}, [0, 4]),
        ]:
            with self.subTest(limit=limit, bad=bad):
                def read(lba, n):
                    if lba in bad:
                        raise c.ReadError("bad", "03/11/00")
                    return audio(lba, n)
                drive = FakeDrive(read)
                rd = c.SalvageReader(drive, [], c.Lookahead())
                rd.drive = drive
                frames = []
                with mock.patch.object(rd, "_reserve_low", side_effect=lambda: len(drive.calls) >= limit), \
                     mock.patch.object(rd._concealer, "feed", side_effect=frames.append):
                    rd._group(0, 12, 0, 12)
                self.assertEqual(drive.calls, [(f, 1) for f in calls])
                self.assertEqual(len(frames), 12)
                first_remaining = 5 if bad == {0, 4} else (2 if limit == 3 else 1)
                self.assertEqual(frames[first_remaining:], [None] * (12 - first_remaining))
                self.assertEqual(rd.snapshot()["muted_unattempted"], 12 - first_remaining)

    def test_reserve_guard_only_remaining_frames(self):
        d = FakeDrive(lambda lba, n: (_ for _ in ()).throw(c.ReadError("bad", "03/11/00")))
        rd, wr, pcm = self.render(d, [c.Segment(1, 0, 45)])
        self.assertEqual(d.calls, [(0, 45)])
        self.assertEqual(rd.snapshot()["muted_unattempted"], 45)
        self.assertEqual(rd.snapshot()["unreadable_frames"], 45)
        self.assertEqual(pcm, bytes(45 * c.FRAME))

    def test_recovery_group_second_attempt_salvages(self):
        seen = {}
        def read(lba, n):
            key = lba, n
            seen[key] = seen.get(key, 0) + 1
            if n > 13 or seen[key] == 1:
                raise c.ReadError("marginal", "03/11/00")
            return audio(lba, n)
        rd, wr, pcm = self.render(FakeDrive(read), mode="recovery")
        self.assertEqual(rd.snapshot()["unreadable_frames"], 0)
        self.assertEqual(pcm, audio(0, 100))

    def test_recovery_single_requires_full_ladder(self):
        def read(lba, n):
            if lba <= 20 < lba + n:
                raise c.ReadError("bad", "03/11/00")
            return audio(lba, n)
        d = FakeDrive(read)
        rd, _, pcm = self.render(d, mode="recovery", gap_treatment="silence")
        self.assertEqual(d.calls.count((20, 1)), 3)
        self.assertEqual(rd.snapshot()["unreadable_frames"], 3)
        self.assertEqual(pcm[19 * c.FRAME:22 * c.FRAME], bytes(3 * c.FRAME))

    def test_span_edges_frame_exact_and_probes_bounded(self):
        for a, b in [(0, 210), (11, 237), (44, 451), (4, 45), (21, 299), (10, 599), (0, 600)]:
            with self.subTest(a=a, b=b):
                def read(lba, n):
                    if lba < b and lba + n > a:
                        raise c.ReadError("band", "03/11/00")
                    return audio(lba, n)
                d = FakeDrive(read)
                rd = c.SalvageReader(d, [], c.Lookahead(1000, 1000), mode="recovery")
                rd.drive = d
                self.assertEqual(rd._resolve_span(0, 600), (a, b))
                self.assertEqual(len(d.calls), len(set(d.calls)))
                self.assertTrue(all(n <= d.nframes for _, n in d.calls))
                self.assertLess(rd.snapshot()["spans"][0][3], 20)

    def test_recovery_every_ladder_stage_salvages_in_order(self):
        for stage in ("group", "overlap", "four", "centred", "single"):
            with self.subTest(stage=stage):
                def read(lba, n):
                    accepted = ((stage == "group" and (lba, n) == (30, 13)) or
                                (stage == "overlap" and (lba, n) == (24, 25)) or
                                (stage == "four" and n <= 4) or
                                (stage == "centred" and ((lba, n) == (26, 13) or n == 1)) or
                                (stage == "single" and n == 1))
                    if not accepted:
                        raise c.ReadError("marginal", "03/11/00")
                    return audio(lba, n)
                drive, la = FakeDrive(read), c.Lookahead(100, 100)
                rd = c.SalvageReader(drive, [], la, mode="recovery")
                rd.drive = drive
                self.assertEqual(rd._group(30, 13, 0, 100), 13)
                rd._concealer.finish()
                la.close()
                pipe = Pipe()
                c.PipeWriter(la, pipe).run()
                self.assertEqual(pipe.getvalue(), audio(30, 13))
                self.assertEqual(rd.snapshot()["unreadable_frames"], 0)

    def test_shared_guards_do_not_duplicate_timeline_frames(self):
        out, muted = [], []
        con = c._Concealer(out.append, lambda: muted.append(1), "silence")
        for pcm in [None, audio(1, 1), None, audio(3, 1), audio(4, 1), audio(5, 1)]:
            con.feed(pcm)
        con.finish()
        self.assertEqual(len(b"".join(out)), 6 * c.FRAME)
        self.assertEqual(len(muted), 4)

    def test_mirror_uses_contiguous_output_after_an_earlier_long_gap(self):
        frames = ([audio(i, 1) for i in range(20)] + [None] * 9 + [audio(29, 1)] +
                  [audio(i, 1) for i in range(30, 33)] + [None] * 3 +
                  [audio(i, 1) for i in range(36, 39)])
        out = []
        con = c._Concealer(out.append, lambda: None, "mirror")
        for frame in frames:
            con.feed(frame)
        con.finish()
        pcm = b"".join(out)
        self.assertEqual(len(pcm), len(frames) * c.FRAME)
        expected = c.fade(c.mirror(pcm[27 * c.FRAME:32 * c.FRAME]), fade_in=False)
        self.assertEqual(pcm[32 * c.FRAME:37 * c.FRAME], expected)

    def test_recovery_dead_band_timeline_and_readable_suffix(self):
        def read(lba, n):
            if lba < 239 and lba + n > 80:
                raise c.ReadError("band", "03/11/00")
            return audio(lba, n)
        rd, wr, pcm = self.render(FakeDrive(read), [c.Segment(1, 0, 400)],
                                  mode="recovery", gap_treatment="silence")
        self.assertEqual(wr.frames_written, 400)
        self.assertEqual(rd.snapshot()["unreadable_frames"], 161)
        self.assertEqual(pcm[79 * c.FRAME:240 * c.FRAME], bytes(161 * c.FRAME))
        self.assertEqual(pcm[241 * c.FRAME:], audio(241, 159))

    def test_no_progressive_guess_on_sparse_failing_probes(self):
        la = c.Lookahead(1000, 1000)
        d = FakeDrive(lambda lba, n: (_ for _ in ()).throw(c.ReadError("bad", "03/11/00")))
        rd = c.SalvageReader(d, [], la, mode="recovery")
        rd.drive = d
        rd._resolve_span(0, 1000)
        self.assertEqual(la.frames, 0)

    def test_retry_modes_and_restore_on_exception(self):
        for mode, retry in [("continuous", 8), ("recovery", 128)]:
            d = FakeDrive()
            self.render(d, mode=mode)
            self.assertEqual(d.retries, [retry, 128])
            self.assertTrue(d.closed)
        d = FakeDrive(lambda *a: (_ for _ in ()).throw(RuntimeError("device lost")))
        rd = c.SalvageReader(d, [c.Segment(1, 0, 5)], c.Lookahead())
        rd.run()
        self.assertIsInstance(rd.error, RuntimeError)
        self.assertEqual(d.retries, [8, 128])

    def test_stop_during_read_and_pipe_closes(self):
        entered, release = threading.Event(), threading.Event()
        def read(lba, n):
            entered.set()
            release.wait(1)
            return audio(lba, n)
        d = FakeDrive(read)
        la, pipe = c.Lookahead(), Pipe()
        rd = c.SalvageReader(d, [c.Segment(1, 0, 100)], la)
        wr = c.PipeWriter(la, pipe)
        rd.start()
        wr.start()
        self.assertTrue(entered.wait(1))
        rd.stop()
        release.set()
        rd.join(2)
        wr.join(2)
        self.assertFalse(rd.is_alive())
        self.assertTrue(pipe.was_closed)
        self.assertEqual(d.retries[-1], 128)
        self.assertEqual(wr.frames_written, 0)

    def test_endian_hint_and_swap(self):
        a = array.array("h", [i // 2 for i in range(2352)])
        le = a.tobytes()
        a.byteswap()
        be = a.tobytes()
        self.assertEqual(c.endian_hint(le), "LE")
        self.assertEqual(c.endian_hint(be), "BE")
        _, _, out = self.render(FakeDrive(lambda lba, n: be[:c.FRAME] * n),
                                [c.Segment(1, 0, 2)])
        self.assertEqual(out, le[:c.FRAME] * 2)

    def test_c2_interpolation_per_channel_and_frame_counter(self):
        a = array.array("h", [10, -10, 10000, -10000, 30, -30] + [30, -30] * 585)
        pointers = bytearray(c.C2)
        pointers[0] = 0x0F  # bytes 4..7: second L/R pair
        pcm, fixed = c.conceal_c2(a.tobytes(), [bytes(pointers)])
        self.assertEqual(fixed, 2)
        self.assertEqual(array.array("h", pcm)[:6], array.array("h", [10, -10, 20, -20, 30, -30]))
        d = FakeDrive(lambda lba, n: (a.tobytes() * n, [bytes(pointers)] * n))
        d.c2 = True
        rd, _, _ = self.render(d, [c.Segment(1, 0, 2)])
        self.assertEqual(rd.snapshot()["c2_frames"], 2)
        self.assertEqual(rd.la.target, 2000)

    def test_mirror_full_level_and_long_gap_fallback_with_guards(self):
        for length in (1, 7):
            out, count = [], []
            con = c._Concealer(out.append, lambda: count.append(1), "mirror")
            for f in range(12):
                con.feed(audio(f, 1))
            for _ in range(length):
                con.feed(None)
            con.feed(audio(20, 1))  # right guard
            con.feed(audio(21, 1))
            con.finish()
            pcm = b"".join(out)
            self.assertEqual(len(pcm) // c.FRAME, 14 + length)
            self.assertEqual(len(count), length + 2)
            gap = pcm[11 * c.FRAME:(13 + length) * c.FRAME]
            if length + 2 <= 8:
                self.assertEqual(gap[100:104], c.mirror(b"".join(out[:11]))[100:104])
            else:
                self.assertEqual(gap, bytes(len(gap)))

    def test_guard_crosses_chunk_and_track_boundaries(self):
        def read(lba, n):
            if lba <= 45 < lba + n:
                raise c.ReadError("bad", "03/11/00")
            return audio(lba, n)
        rd, wr, pcm = self.render(FakeDrive(read), [c.Segment(1, 0, 45), c.Segment(2, 45, 90)],
                                  reserve_s=0, gap_treatment="silence")
        self.assertEqual(pcm[44 * c.FRAME:47 * c.FRAME], bytes(3 * c.FRAME))
        self.assertEqual(wr.frames_written, 90)
        self.assertEqual(rd.snapshot()["unreadable_frames"], 3)

    def test_fault_injection_and_invalid_policy(self):
        with mock.patch.dict(os.environ, {"CDPCORE_CDDA_INJECT_FAIL": "10:12:0"}):
            rd, _, _ = self.render(FakeDrive(), reserve_s=0)
        self.assertGreater(rd.snapshot()["unreadable_frames"], 0)
        self.assertTrue(rd.sense_failure.is_set())
        with self.assertRaises(ValueError):
            c.SalvageReader("fake", [], c.Lookahead(), mode="bad")

    def test_two_kernel_aborts_are_fatal_but_sense_is_not(self):
        d = FakeDrive(lambda *a: (_ for _ in ()).throw(c.ReadError("timeout", host_status=3)))
        rd = c.SalvageReader(d, [c.Segment(1, 0, 90)], c.Lookahead(), reserve_s=0)
        rd.run()
        self.assertIsInstance(rd.error, c.DriveTimeout)
        self.assertFalse(rd.sense_failure.is_set())
        self.assertEqual(d.retries[-1], 128)


class BufferTests(unittest.TestCase):
    def test_periodic_bursts_keep_paced_writer_fed(self):
        # A bounded fake pipe consumes at 75 fps. Damage is preceded by C2 so
        # the reader builds the specified adaptive headroom before 0.3s errors.
        class PacedPipe(Pipe):
            def write(self, pcm):
                time.sleep(len(pcm) / c.FRAME / 75)
                return super().write(pcm)
        for warning in ("c2", "slow"):
            with self.subTest(warning=warning):
                drive = BurstDrive(start=450, end=509, failure_s=0.3, warning=warning)
                la, pipe = c.Lookahead(), PacedPipe()
                rd = c.SalvageReader(drive, [c.Segment(1, 0, 600)], la, gap_treatment="silence")
                # Exclude startup from the steady-state underrun contract.
                rd.start()
                deadline = time.monotonic() + 3
                while la.frames < 150 and time.monotonic() < deadline:
                    time.sleep(0.01)
                wr = c.PipeWriter(la, pipe)
                wr.start()
                rd.join(8)
                wr.join(12)
                self.assertFalse(rd.is_alive())
                self.assertFalse(wr.is_alive())
                self.assertIsNone(rd.error)
                self.assertEqual(wr.frames_written, 600)
                self.assertEqual(wr.underruns, 0)
                self.assertIsNone(wr.error)
                self.assertEqual(rd.snapshot()["muted_unattempted"], 0)
                bad = {f for f in range(600) if drive.bad(f)}
                guarded = bad | {f + delta for f in bad for delta in (-1, 1)}
                self.assertEqual(rd.snapshot()["unreadable_frames"], len(guarded))
                self.assertLess(len(drive.failed_calls), len(bad))
                pcm = pipe.getvalue()
                muted = {f for f in range(600)
                         if pcm[f * c.FRAME:(f + 1) * c.FRAME] == c.SILENCE_FRAME}
                self.assertEqual(muted, guarded)

    def test_close_releases_full_producer_and_empty_consumer(self):
        la = c.Lookahead(1, 1)
        la.put(bytes(c.FRAME), 1)
        producer = threading.Thread(target=lambda: la.put(bytes(c.FRAME), 1))
        producer.start()
        time.sleep(0.03)
        self.assertTrue(producer.is_alive())
        la.close()
        producer.join(1)
        self.assertFalse(producer.is_alive())
        la = c.Lookahead()
        result = []
        consumer = threading.Thread(target=lambda: result.append(la.get()))
        consumer.start()
        time.sleep(0.03)
        self.assertTrue(consumer.is_alive())
        la.close()
        consumer.join(1)
        self.assertEqual(result, [None])
        self.assertEqual(la.underruns, 1)

    def test_target_grows_and_decays_without_exceeding_bound(self):
        la = c.Lookahead(2, 5, calm_s=60)
        with mock.patch.object(c.time, "monotonic", return_value=0):
            la.trouble()
        self.assertEqual(la.target, 5)
        with mock.patch.object(c.time, "monotonic", return_value=61):
            la.put(bytes(c.FRAME), 1)
        self.assertEqual(la.target, 2)

    def test_slow_read_and_c2_grow_buffer(self):
        for result in (audio(0, 1), (audio(0, 1), [bytes([128]) + bytes(c.C2 - 1)])):
            la = c.Lookahead()
            rd = c.SalvageReader("fake", [], la)
            rd.drive = FakeDrive(lambda *a: result)
            with mock.patch.object(c.time, "monotonic", side_effect=itertools.chain([0], itertools.repeat(0.31))):
                rd._try(0, 1)
            self.assertEqual(la.target, 20 * 75)

    def test_partial_pipe_writes_preserve_pcm(self):
        class Partial(Pipe):
            def write(self, pcm):
                return super().write(pcm[:103])
        la, pipe = c.Lookahead(), Partial()
        la.put(audio(0, 3), 3)
        la.close()
        wr = c.PipeWriter(la, pipe)
        wr.run()
        self.assertEqual(pipe.getvalue(), audio(0, 3))
        self.assertEqual(wr.frames_written, 3)


class DriveTests(unittest.TestCase):
    def test_toc_linux_ioctl_contract_and_data_control_bit(self):
        def ioctl(fd, command, value):
            if command == 0x5305:
                value[:] = bytes([1, 3])
            else:
                number = value[0]
                start = {1: 0, 2: 100, 3: 200, 0xAA: 400}[number]
                value[:] = struct.pack("BBBBiB3x", number, 0x40 if number == 2 else 0,
                                       1, 0, start, 0)
        with mock.patch.dict("sys.modules", {"fcntl": types.SimpleNamespace(ioctl=ioctl)}), \
             mock.patch.object(c.os, "open", return_value=8), mock.patch.object(c.os, "close") as closed:
            toc = c.Toc.read()
        self.assertEqual(toc.tracks, [c.Track(1, 0, 100), c.Track(2, 100, 200, True), c.Track(3, 200, 400)])
        closed.assert_called_once_with(8)

    def test_read_cd_cdb_c2_layout_and_single_ioctl(self):
        commands = []
        def ioctl(fd, command, hdr):
            cdb = bytes((ctypes.c_ubyte * hdr.cmd_len).from_address(hdr.cmdp))
            commands.append(cdb)
            pcm = audio(0, 1)
            pointers = bytes([128]) + bytes(c.C2 - 1)
            ctypes.memmove(hdr.dxferp, pcm + pointers, len(pcm) + len(pointers))
        with mock.patch.dict("sys.modules", {"fcntl": types.SimpleNamespace(ioctl=ioctl)}), \
             mock.patch.object(c.os, "open", return_value=8), mock.patch.object(c.os, "close"), \
             mock.patch.object(c.Drive, "set_retry", return_value=True):
            drive = c.Drive(c2_supported=True)
            pcm, flags = drive.read(0x123456, 1)
            drive.close()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0], bytes([0xBE, 4, 0, 0x12, 0x34, 0x56, 0, 0, 1, 0x12, 0, 0]))
        self.assertEqual(pcm, audio(0, 1))
        self.assertEqual(flags, [bytes([128]) + bytes(c.C2 - 1)])

    def test_fixed_and_descriptor_sense_and_host_timeout(self):
        fixed = bytearray(18)
        fixed[0], fixed[2], fixed[12], fixed[13] = 0x70, 3, 0x11, 0
        for raw, host, expected in [(bytes(fixed), 0, "03/11/00"), (bytes([0x72, 4, 0x3E, 2]), 0, "04/3e/02"), (b"", 3, "")]:
            with self.subTest(raw=raw, host=host):
                def ioctl(fd, command, hdr):
                    hdr.status = 2 if raw else 0
                    hdr.host_status, hdr.sb_len_wr = host, len(raw)
                    if raw:
                        ctypes.memmove(hdr.sbp, raw, len(raw))
                with mock.patch.dict("sys.modules", {"fcntl": types.SimpleNamespace(ioctl=ioctl)}):
                    with self.assertRaises(c.ReadError) as caught:
                        c.read_cd(8, 0, 1, 8000, ctypes.create_string_buffer(c.FRAME), ctypes.create_string_buffer(32))
                self.assertEqual(caught.exception.sense, expected)
                self.assertEqual(caught.exception.stalled, host == 3)

    def test_startup_restore_without_audio_read(self):
        with mock.patch.object(c.os, "open", return_value=8), mock.patch.object(c.os, "close") as closed, \
             mock.patch.object(c, "_command") as command:
            self.assertTrue(c.restore_factory_retry())
        self.assertEqual(command.call_args.args[1][0], 0x55)
        self.assertEqual(command.call_args.args[2].raw[11], 128)
        closed.assert_called_once_with(8)

    def test_cached_c2_support_avoids_extra_probe_at_segment_start(self):
        with mock.patch.object(c.os, "open", return_value=8), mock.patch.object(c.os, "close"), \
             mock.patch.object(c, "_command") as command, mock.patch.object(c, "read_cd") as read:
            drive = c.Drive(c2_supported=False, retry_count=8)
            self.assertFalse(drive.c2)
            read.assert_not_called()
            self.assertEqual([call.args[2].raw[11] for call in command.call_args_list], [128, 8])
            drive.close()
    def test_c2_illegal_request_fallback_and_retry_restore(self):
        def command(fd, cdb, buf, sense, size, timeout, direction=-3):
            if cdb[0] == 0xBE and cdb[9] == 0x12:
                raise c.ReadError("unsupported", "05/24/00")
        with mock.patch.object(c.os, "open", return_value=8) as opened, \
             mock.patch.object(c.os, "close") as closed, \
             mock.patch.object(c, "_command", side_effect=command) as commands:
            d = c.Drive()
            self.assertFalse(d.c2)
            self.assertEqual(opened.call_args.args[1], os.O_RDWR | c.NONBLOCK)
            d.set_retry(8)
            d.close()
            self.assertEqual(d.retry, 128)
            closed.assert_called_once_with(8)
            self.assertEqual([x.args[0] for x in commands.call_args_list], [8] * 4)

    def test_c2_media_failure_retains_pointer_mode_for_salvage(self):
        with mock.patch.object(c.os, "open", return_value=8), mock.patch.object(c.os, "close") as closed, \
             mock.patch.object(c, "read_cd", side_effect=c.ReadError("bad", "03/11/00")), \
             mock.patch.object(c, "_command"):
            drive = c.Drive()
            self.assertTrue(drive.c2)
            drive.close()
            closed.assert_called_once_with(8)

    def test_max_chunk_uses_reenumerated_device_and_c2_width(self):
        with mock.patch.object(c.os.path, "realpath", return_value="/dev/sr1"), \
             mock.patch.object(c.Path, "read_text", return_value="120") as read:
            self.assertEqual(c.max_frames_per_read(c2=True), 46)
            self.assertEqual(c.max_frames_per_read(), 52)
        with mock.patch.object(c.Path, "read_text", side_effect=OSError):
            self.assertEqual(c.max_frames_per_read(), 45)


if __name__ == "__main__":
    unittest.main()
