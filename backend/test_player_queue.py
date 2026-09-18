"""Regression tests for track requests issued while mpv is still starting.

Run:  python -m unittest test_player_queue

mpv is never launched: the spawn / IPC / monitor internals of CDPlayer are
replaced with fakes so the cold-start sequencing can be exercised on any host.

Scenario (v1.3.1): the user presses play, then taps track 9 while the VFD shows
STARTING. Before the fix the second play() took the "mpv already running" path
and sent a chapter seek to a socket that had not read the TOC yet; the command
was lost, the first call unpaused on track 1, and the UI kept showing track 9.
"""
import threading
import time
import unittest
from unittest import mock

import player

_real_sleep = time.sleep  # player.time is this same module; avoid patching recursively


class _FakeProc:
    pid = 4242

    def poll(self):
        return None


def _make_player(toc_ready: threading.Event):
    p = player.CDPlayer()
    sent = []
    p._spawn_mpv = lambda cdrom_device, alsa_device: _FakeProc()
    p._monitor_mpv = lambda generation: None
    p._cache_chapter_list_bg = lambda generation: None
    p._wait_for_ipc = lambda timeout=10: None
    p._ipc_send = lambda command: sent.append(list(command))

    def _query(prop, timeout=0.5):
        if prop == "chapter":
            return 0 if toc_ready.is_set() else None
        return None

    p._ipc_query = _query
    return p, sent


class QueuedTrackDuringColdStartTests(unittest.TestCase):
    def _run_cold_start(self, *, requests_during_start, expected_chapter_cmds):
        toc_ready = threading.Event()
        p, sent = _make_player(toc_ready)
        with mock.patch.object(player.time, "sleep", lambda s: _real_sleep(0.01)):
            first = threading.Thread(target=p.play, args=(1, 12), daemon=True)
            first.start()
            # Let the first call spawn "mpv" and enter its TOC wait loop.
            for _ in range(200):
                if p._cold_start_pending:
                    break
                _real_sleep(0.005)
            self.assertTrue(p._cold_start_pending)

            for n in requests_during_start:
                t0 = time.monotonic()
                p.play(n, 12)
                # Queued requests must return immediately, not block on the TOC.
                self.assertLess(time.monotonic() - t0, 0.5)
                self.assertEqual(sent, [], "queued play() must not touch IPC")

            toc_ready.set()
            first.join(timeout=5)
            self.assertFalse(first.is_alive())

        self.assertFalse(p._cold_start_pending)
        chapter_cmds = [c for c in sent if c[:2] == ["set_property", "chapter"]]
        self.assertEqual(chapter_cmds, expected_chapter_cmds)
        self.assertIn(["set_property", "pause", False], sent)
        return p, sent

    def test_single_request_during_start_is_honoured(self):
        p, _ = self._run_cold_start(
            requests_during_start=[9],
            expected_chapter_cmds=[["set_property", "chapter", 8]],
        )
        self.assertEqual(p.current_track(), 9)

    def test_last_request_during_start_wins(self):
        p, _ = self._run_cold_start(
            requests_during_start=[9, 8],
            expected_chapter_cmds=[["set_property", "chapter", 7]],
        )
        self.assertEqual(p.current_track(), 8)

    def test_no_request_during_start_keeps_track_one(self):
        p, _ = self._run_cold_start(
            requests_during_start=[],
            expected_chapter_cmds=[],
        )
        self.assertEqual(p.current_track(), 1)

    def test_after_cold_start_play_uses_running_path(self):
        p, sent = self._run_cold_start(
            requests_during_start=[],
            expected_chapter_cmds=[],
        )
        del sent[:]
        p.play(3, 12)
        self.assertIn(["set_property", "chapter", 2], sent)
        self.assertEqual(p.current_track(), 3)

    def test_stop_during_start_clears_pending_flag(self):
        toc_ready = threading.Event()
        p, sent = _make_player(toc_ready)
        with mock.patch.object(player.time, "sleep", lambda s: _real_sleep(0.01)):
            first = threading.Thread(target=p.play, args=(1, 12), daemon=True)
            first.start()
            for _ in range(200):
                if p._cold_start_pending:
                    break
                _real_sleep(0.005)
            self.assertTrue(p._cold_start_pending)
            with p._lock:
                p._mpv_proc = None  # what _detach_mpv_for_teardown does on stop()
            first.join(timeout=5)
            self.assertFalse(first.is_alive())
        self.assertFalse(p._cold_start_pending)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
