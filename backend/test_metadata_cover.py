"""Cover art: thumbnails instead of full-size originals, oversized cache refresh.

Run:  python -m unittest test_metadata_cover
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import metadata


class _Resp:
    def __init__(self, status, body=b""):
        self.status_code, self.content = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, routes, seen):
        self.routes, self.seen = routes, seen

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        self.seen.append(url)
        return self.routes.get(url, _Resp(404))


CAA = "https://coverartarchive.org/release/abc-123/front"


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("CACHE_DIR", Path(self.tmp.name)), ("_mb_rate_limit", lambda: None)):
            p = mock.patch.object(metadata, name, value)
            p.start(); self.addCleanup(p.stop)
        self.seen = []

    def _run(self, routes, url=CAA, disc="d1"):
        with mock.patch.object(metadata.httpx, "Client", lambda **kw: _Client(routes, self.seen)):
            return metadata.fetch_and_cache_cover(url, disc)

    def test_built_url_is_the_1200_thumbnail(self):
        self.assertEqual(metadata._build_cover_art_url("abc-123"), CAA + "-1200")

    def test_candidates_for_caa_and_other_hosts(self):
        self.assertEqual(metadata._cover_candidates(CAA), [CAA + "-1200", CAA + "-500", CAA])
        self.assertEqual(metadata._cover_candidates(CAA + "-250"), [CAA + "-1200", CAA + "-500", CAA])
        other = "https://is1-ssl.mzstatic.com/image/x/1000x1000bb.jpg"
        self.assertEqual(metadata._cover_candidates(other), [other])

    def test_uses_thumbnail_first(self):
        path = self._run({CAA + "-1200": _Resp(200, b"small")})
        self.assertEqual(path.read_bytes(), b"small")
        self.assertEqual(self.seen, [CAA + "-1200"])

    def test_falls_back_to_500_then_original(self):
        path = self._run({CAA + "-500": _Resp(200, b"mid")})
        self.assertEqual(path.read_bytes(), b"mid")
        self.assertEqual(self.seen, [CAA + "-1200", CAA + "-500"])
        self.seen.clear()
        path = self._run({CAA: _Resp(200, b"orig")}, disc="d2")
        self.assertEqual(path.read_bytes(), b"orig")
        self.assertEqual(self.seen, [CAA + "-1200", CAA + "-500", CAA])

    def test_small_cache_is_reused_without_network(self):
        (Path(self.tmp.name) / "cover_d1.jpg").write_bytes(b"x" * 1000)
        path = self._run({})
        self.assertEqual(path.read_bytes(), b"x" * 1000)
        self.assertEqual(self.seen, [])

    def test_oversized_cache_is_replaced_by_thumbnail(self):
        big = Path(self.tmp.name) / "cover_d1.jpg"
        big.write_bytes(b"x" * (metadata.MAX_CACHED_COVER_BYTES + 1))
        path = self._run({CAA + "-1200": _Resp(200, b"small")}, url=CAA)
        self.assertEqual(path.read_bytes(), b"small")

    def test_oversized_cache_kept_when_refetch_fails(self):
        big = Path(self.tmp.name) / "cover_d1.jpg"
        big.write_bytes(b"x" * (metadata.MAX_CACHED_COVER_BYTES + 1))
        path = self._run({})
        self.assertEqual(path, big)
        self.assertEqual(path.stat().st_size, metadata.MAX_CACHED_COVER_BYTES + 1)

    def test_non_caa_oversized_cache_is_kept(self):
        url = "https://is1-ssl.mzstatic.com/image/x/1000x1000bb.jpg"
        big = Path(self.tmp.name) / "cover_d1.jpg"
        big.write_bytes(b"x" * (metadata.MAX_CACHED_COVER_BYTES + 1))
        path = self._run({}, url=url)
        self.assertEqual(path, big)
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
