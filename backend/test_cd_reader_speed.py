"""Unit tests for cd_reader.measure_read_speed (no drive needed).

Run:  python -m unittest test_cd_reader_speed

The reader is injected: each fake read sleeps for the time a drive at a given
speed would take, so the returned x-factor can be checked without hardware.
"""
import time
import unittest

import cd_reader


def _reader_at(x_factor: float, fail_at_lba=None):
    """Fake READ CD that takes as long as a drive reading at `x_factor`."""
    calls = []

    def _reader(lba, nframes):
        calls.append((lba, nframes))
        if fail_at_lba is not None and lba == fail_at_lba:
            raise OSError("READ CD failed (simulated bad sector)")
        time.sleep(nframes / 75.0 / x_factor)

    _reader.calls = calls
    return _reader


class MeasureReadSpeedTests(unittest.TestCase):
    def setUp(self):
        cd_reader.drive_speed_measured = None

    def test_warmup_is_not_timed_and_factor_is_close(self):
        # Warm-up chunk would be slow on a real drive (spin-up); make it obviously
        # slow here so a timing bug that includes it shows up as a wrong factor.
        rdr = _reader_at(4.0)
        real = rdr

        def with_slow_warmup(lba, nframes):
            if not real.calls:
                real.calls.append((lba, nframes))
                time.sleep(0.3)
                return
            real(lba, nframes)

        factor = cd_reader.measure_read_speed(lba=1500, frames=15, chunks=3, _reader=with_slow_warmup)
        self.assertIsNotNone(factor)
        self.assertGreater(factor, 2.5)
        self.assertLess(factor, 4.5)
        self.assertEqual(cd_reader.drive_speed_measured, factor)
        # warm-up + 3 timed chunks, consecutive LBAs
        self.assertEqual([c[0] for c in real.calls], [1500, 1515, 1530, 1545])

    def test_true_1x_is_reported_as_1x(self):
        factor = cd_reader.measure_read_speed(lba=1500, frames=15, chunks=2, _reader=_reader_at(1.0))
        self.assertIsNotNone(factor)
        self.assertLessEqual(factor, cd_reader.SPEED_1X_CONFIRMED_MAX)

    def test_read_failure_returns_none_without_retry(self):
        rdr = _reader_at(4.0, fail_at_lba=1530)
        factor = cd_reader.measure_read_speed(lba=1500, frames=15, chunks=3, _reader=rdr)
        self.assertIsNone(factor)
        self.assertIsNone(cd_reader.drive_speed_measured)
        # Stopped at the failing chunk: no retries, no further reads.
        self.assertEqual([c[0] for c in rdr.calls], [1500, 1515, 1530])


if __name__ == "__main__":
    unittest.main()
