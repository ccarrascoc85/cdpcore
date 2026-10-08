"""Queued-start regression: latest track wins before PCM reaches the pipe."""
import threading
import time
import unittest
from contextlib import ExitStack
from test_player_raw import fake_player


class QueuedTrackDuringColdStartTests(unittest.TestCase):
    def _run(self, requests):
        gate = threading.Event()
        with ExitStack() as stack:
            p, processes, readers, sent = fake_player(stack, toc_gate=gate)
            errors = []
            def start():
                try:
                    p.play(1, 12)
                except Exception as exc:
                    errors.append(exc)
            first = threading.Thread(target=start)
            first.start()
            for _ in range(200):
                if p.cold_start_pending:
                    break
                time.sleep(0.005)
            self.assertTrue(p.cold_start_pending)
            for n in requests:
                t0 = time.monotonic()
                p.play(n, 12)
                self.assertLess(time.monotonic() - t0, 0.5)
                self.assertEqual(sent, [])
                self.assertEqual(processes, [])
            gate.set()
            first.join(3)
            self.assertFalse(first.is_alive())
            self.assertEqual(errors, [])
            target = requests[-1] if requests else 1
            self.assertEqual(readers[-1].plan[0].track, target)
            self.assertEqual(p.current_track(), target)
            self.assertFalse(p.cold_start_pending)
            self.assertEqual(len(processes), 1)
            p.play(3, 12)
            self.assertEqual(readers[-1].plan[0].track, 3)
            self.assertEqual(len(processes), 2)

    def test_single_request_during_start_is_honoured(self):
        self._run([9])

    def test_last_request_during_start_wins(self):
        self._run([9, 8])

    def test_no_request_during_start_keeps_track_one(self):
        self._run([])

    def test_retarget_after_reader_started_before_first_pcm(self):
        gate = threading.Event()
        with ExitStack() as stack:
            p, processes, readers, sent = fake_player(stack, pcm_gate=gate)
            first = threading.Thread(target=lambda: p.play(1, 12))
            first.start()
            for _ in range(200):
                if readers:
                    break
                time.sleep(0.005)
            self.assertTrue(readers)
            old = readers[0]
            p.play(9, 12)
            for _ in range(200):
                if len(readers) > 1:
                    break
                time.sleep(0.005)
            self.assertTrue(old.stopped.is_set())
            self.assertEqual(readers[-1].plan[0].track, 9)
            self.assertEqual(sent, [])
            gate.set()
            first.join(3)
            self.assertFalse(first.is_alive())
            self.assertEqual(p.current_track(), 9)

    def test_stop_during_start_clears_pending_flag(self):
        gate = threading.Event()
        with ExitStack() as stack:
            p, processes, _, sent = fake_player(stack, toc_gate=gate)
            first = threading.Thread(target=lambda: p.play(1, 12))
            first.start()
            for _ in range(200):
                if p.cold_start_pending:
                    break
                time.sleep(0.005)
            p.stop()
            gate.set()
            first.join(3)
            self.assertFalse(first.is_alive())
            self.assertFalse(p.cold_start_pending)
            self.assertEqual(sent, [])
            self.assertEqual(processes, [])


if __name__ == "__main__":
    unittest.main()
