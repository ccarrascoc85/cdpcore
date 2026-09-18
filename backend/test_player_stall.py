"""Regression tests for a cold start whose TOC read never completes.

Run:  python -m unittest test_player_stall

Scenario (seen on real hardware, 2026-09-18): the optical drive stops
answering SCSI commands while mpv opens it. mpv's opener thread blocks in
uninterruptible I/O and the "chapter" property never becomes available.
Before this fix the player waited out its deadline, logged "Playing track N"
and unpaused an mpv that had no file loaded: the UI showed PLAYING with a
frozen 0:00 counter and no way to recover. Now the player tears mpv down and
raises DriveStallError so the backend can go back to LOADED with a visible
"drive_stalled" reason.
"""
import threading
import time
import unittest
from unittest import mock

import player

_real_sleep = time.sleep


class _FakeProc:
    pid = 4343

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _make_stalled_player():
    """A CDPlayer whose fake mpv never reports a chapter (TOC never read)."""
    p = player.CDPlayer()
    sent = []
    stopped = threading.Event()
    p._spawn_mpv = lambda cdrom_device, alsa_device: _FakeProc()
    p._monitor_mpv = lambda generation: None
    p._cache_chapter_list_bg = lambda generation: None
    p._wait_for_ipc = lambda timeout=10: None
    p._ipc_send = lambda command: sent.append(list(command))
    p._ipc_query = lambda prop, timeout=0.5: None

    real_stop_mpv = p._stop_mpv

    def _stop_mpv():
        stopped.set()
        real_stop_mpv()

    p._stop_mpv = _stop_mpv
    return p, sent, stopped


class ColdStartStallTests(unittest.TestCase):
    def test_stall_raises_and_tears_down(self):
        p, sent, stopped = _make_stalled_player()
        with mock.patch.object(player, "COLD_START_TOC_TIMEOUT", 0.4), \
             mock.patch.object(player.time, "sleep", lambda s: _real_sleep(0.01)):
            t0 = time.monotonic()
            with self.assertRaises(player.DriveStallError):
                p.play(3, 12)
            elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 3.0)
        self.assertTrue(stopped.is_set(), "mpv must be torn down on a stall")
        self.assertFalse(p.cold_start_pending)
        # Never unpaused, never seeked: no audio command reached mpv.
        self.assertNotIn(["set_property", "pause", False], sent)
        self.assertEqual([c for c in sent if c[:2] == ["set_property", "chapter"]], [])
        self.assertFalse(p.drive_busy, "player must not report the drive as busy after teardown")

    def test_cold_start_pending_visible_while_waiting(self):
        p, sent, stopped = _make_stalled_player()
        with mock.patch.object(player, "COLD_START_TOC_TIMEOUT", 0.6), \
             mock.patch.object(player.time, "sleep", lambda s: _real_sleep(0.01)):
            t = threading.Thread(target=lambda: self._swallow(p), daemon=True)
            t.start()
            for _ in range(100):
                if p.cold_start_pending:
                    break
                _real_sleep(0.005)
            self.assertTrue(p.cold_start_pending)
            t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertFalse(p.cold_start_pending)

    @staticmethod
    def _swallow(p):
        try:
            p.play(1, 12)
        except player.DriveStallError:
            pass


if __name__ == "__main__":
    unittest.main()
