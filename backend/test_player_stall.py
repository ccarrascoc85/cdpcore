"""Cold-start deadline and kernel-aborted-read regression tests."""
import threading
import time
import unittest
from contextlib import ExitStack
from unittest import mock
import player
from test_player_raw import fake_player


class ColdStartStallTests(unittest.TestCase):
    def test_stall_raises_and_tears_down(self):
        gate = threading.Event()
        with ExitStack() as stack:
            p, processes, readers, sent = fake_player(stack, pcm_gate=gate)
            stack.enter_context(mock.patch.object(player, "COLD_START_TOC_TIMEOUT", 0.15))
            t0 = time.monotonic()
            with self.assertRaises(player.DriveStallError):
                p.play(3, 12)
            self.assertGreaterEqual(time.monotonic() - t0, 0.15)
            self.assertLess(time.monotonic() - t0, 1)
            self.assertFalse(p.cold_start_pending)
            self.assertFalse(p.drive_busy)
            self.assertTrue(readers[0].stopped.is_set())
            self.assertTrue(p._cleanup_done.wait(2))
            self.assertTrue(processes[0].terminated)
            self.assertEqual(sent, [])

    def test_cold_start_pending_visible_while_waiting(self):
        gate = threading.Event()
        with ExitStack() as stack:
            p, _, _, _ = fake_player(stack, pcm_gate=gate)
            stack.enter_context(mock.patch.object(player, "COLD_START_TOC_TIMEOUT", 0.2))
            t = threading.Thread(target=lambda: self._swallow(p))
            t.start()
            for _ in range(100):
                if p.cold_start_pending:
                    break
                time.sleep(0.005)
            self.assertTrue(p.cold_start_pending)
            t.join(2)
            self.assertFalse(t.is_alive())
            self.assertFalse(p.cold_start_pending)

    def test_two_kernel_aborts_raise_stall(self):
        from cdda_source import DriveTimeout
        gate = threading.Event()
        with ExitStack() as stack:
            p, processes, readers, _ = fake_player(stack, pcm_gate=gate)
            errors = []
            t = threading.Thread(target=lambda: self._capture(p, errors))
            t.start()
            for _ in range(100):
                if readers:
                    break
                time.sleep(0.005)
            readers[-1].error = DriveTimeout("two aborted reads", host_status=3)
            t.join(2)
            self.assertFalse(t.is_alive())
            self.assertIsInstance(errors[0], player.DriveStallError)
            self.assertTrue(p._cleanup_done.wait(2))
            self.assertTrue(processes[0].terminated)

    def test_toc_read_hang_is_bounded_and_late_completion_discarded(self):
        gate = threading.Event()
        with ExitStack() as stack:
            p, processes, _, _ = fake_player(stack, toc_gate=gate)
            stack.enter_context(mock.patch.object(player, "COLD_START_TOC_TIMEOUT", 0.1))
            with self.assertRaises(player.DriveStallError):
                p.play(1, 12)
            gate.set()
            time.sleep(0.05)
            self.assertEqual(processes, [])

    @staticmethod
    def _capture(p, errors):
        try:
            p.play(1, 12)
        except Exception as exc:
            errors.append(exc)

    @staticmethod
    def _swallow(p):
        try:
            p.play(1, 12)
        except player.DriveStallError:
            pass


if __name__ == "__main__":
    unittest.main()
