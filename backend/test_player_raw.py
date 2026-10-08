"""Raw player contracts with fake processes, readers, pipes and IPC."""
import io
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from contextlib import ExitStack

import cdda_source as c
import player


class FakeProc:
    pid = 4242
    def __init__(self):
        self.stdin = io.BytesIO()
        self.terminated = False
    def poll(self):
        return 0 if self.terminated else None
    def terminate(self):
        self.terminated = True
    def kill(self):
        self.terminated = True
    def wait(self, timeout=None):
        return 0


class FakeReader(threading.Thread):
    gate = None
    def __init__(self, device, plan, la, **kwargs):
        super().__init__(daemon=True)
        self.plan, self.la, self.policy = plan, la, kwargs
        self.first_pcm = threading.Event()
        self.sense_failure = threading.Event()
        self.finished = threading.Event()
        self.stopped = threading.Event()
        self.error, self.c2_available, self.nframes = None, False, 45
        self.counters = {"unreadable_frames": 0, "c2_frames": 0}
    def run(self):
        while not self.stopped.is_set():
            if self.gate is None or self.gate.wait(0.01):
                self.first_pcm.set()
                return
    def stop(self):
        self.stopped.set()
        self.la.close()
    def snapshot(self):
        return self.counters.copy()


class FakeWriter:
    ident = None
    def __init__(self, la, pipe):
        self.frames_written = 0
        self.started = False
    def start(self):
        self.started = True


def fake_player(stack, toc_gate=None, pcm_gate=None, monitor=False):
    p = player.CDPlayer()
    processes, readers, sent = [], [], []
    toc = c.Toc([c.Track(i, (i - 1) * 750, i * 750) for i in range(1, 13)])
    def read_toc(device):
        if toc_gate:
            toc_gate.wait(2)
        return toc
    def spawn(*args):
        proc = FakeProc()
        processes.append(proc)
        return proc
    def reader(*args, **kwargs):
        rd = FakeReader(*args, **kwargs)
        rd.gate = pcm_gate
        readers.append(rd)
        return rd
    stack.enter_context(mock.patch.object(player.Toc, "read", side_effect=read_toc))
    stack.enter_context(mock.patch.object(player, "SalvageReader", side_effect=reader))
    stack.enter_context(mock.patch.object(player, "PipeWriter", FakeWriter))
    p._spawn_mpv = spawn
    if not monitor:
        p._monitor_mpv = lambda generation: None
    p._ipc_send = lambda command: sent.append(command)
    p._ipc_query = lambda prop, timeout=0.5: None
    stack.callback(lambda: (p.stop(), p._cleanup_done.wait(2)))
    return p, processes, readers, sent


class RawPlayerTests(unittest.TestCase):
    def test_play_plan_and_time_pos_natural_boundary_once(self):
        with ExitStack() as stack:
            p, processes, readers, sent = fake_player(stack)
            ended = []
            p.set_on_track_end(ended.append)
            p.play(3, 12)
            self.assertEqual(readers[-1].plan[0], c.Segment(3, 1500, 2250))
            gen = p._mpv_generation
            p._accept_position(gen, 9.99)
            self.assertEqual(p.current_track(), 3)
            p._accept_position(gen, 10.0)
            self.assertEqual(p.current_track(), 4)
            self.assertEqual(p.get_elapsed(), 0)
            p._accept_position(gen, 10.0)
            self.assertEqual(ended, [3])
            p._accept_position(gen - 1, 25)
            self.assertEqual(p.current_track(), 4)

    def test_main_boundary_uses_physical_audio_track_number(self):
        import main
        from state import get_state, CDState, TrackInfo
        state = get_state()
        saved = state.__dict__.copy()
        self.addCleanup(lambda: state.__dict__.update(saved))
        state.state = CDState.PLAYING
        state.tracks = [TrackInfo(1, "First", 10), TrackInfo(3, "Third", 10)]
        with mock.patch.object(player, "get_player_status", return_value={"state": "play", "song": "2"}), \
             mock.patch.object(main, "_schedule_broadcast"):
            main._on_track_end(1)
        self.assertEqual(state.track_number, 3)
        self.assertEqual(state.track_title, "Third")

    def test_navigation_restarts_segments_and_prev_rule(self):
        from state import get_state, TrackInfo
        state = get_state()
        saved = state.tracks
        state.tracks = [TrackInfo(i, str(i), 10) for i in range(1, 13)]
        try:
            with ExitStack() as stack:
                p, processes, readers, _ = fake_player(stack)
                stack.enter_context(mock.patch.object(player, "_player", p))
                p.play(3, 12)
                player.next_track()
                self.assertEqual(readers[-1].plan[0].track, 4)
                self.assertTrue(processes[0].terminated)
                p._accept_position(p._mpv_generation, 4)
                player.prev_track()
                self.assertEqual(readers[-1].plan[0].track, 3)
                p._accept_position(p._mpv_generation, 6)
                player.prev_track()
                self.assertEqual(readers[-1].plan[0].track, 3)
                self.assertEqual(len(processes), 4)
        finally:
            state.tracks = saved

    def test_pause_resume_use_properties_and_time_pos_is_truth(self):
        with ExitStack() as stack:
            p, _, _, sent = fake_player(stack)
            p.play(1, 12)
            p._accept_position(p._mpv_generation, 2)
            p.pause()
            self.assertEqual(p.get_status()["state"], "pause")
            self.assertEqual(p.get_elapsed(), 2)
            p.resume()
            self.assertEqual(sent, [["set_property", "pause", True], ["set_property", "pause", False]])
            self.assertEqual(p.get_elapsed(), 2)

    def test_pause_request_before_process_is_published_is_preserved(self):
        gate = threading.Event()
        with ExitStack() as stack:
            p, _, _, _ = fake_player(stack, toc_gate=gate)
            first = threading.Thread(target=lambda: p.play(1, 12))
            first.start()
            for _ in range(100):
                if p.cold_start_pending:
                    break
                time.sleep(0.005)
            p.pause()
            gate.set()
            first.join(2)
            self.assertFalse(first.is_alive())
            self.assertTrue(p.is_paused())

    def test_ipc_closed_socket_returns_without_spinning(self):
        p = player.CDPlayer()
        sock = mock.MagicMock()
        sock.recv.return_value = b""
        with mock.patch.object(player._IPC_SOCK.__class__, "exists", return_value=True), \
             mock.patch.object(player.socket, "AF_UNIX", 1, create=True), \
             mock.patch.object(player.socket, "socket", return_value=sock):
            self.assertIsNone(p._ipc_query("time-pos"))
        sock.recv.assert_called_once()
        sock.close.assert_called_once()

    def test_read_counters_reset_new_segment(self):
        with ExitStack() as stack:
            p, _, readers, _ = fake_player(stack)
            p.play(1, 12)
            readers[-1].counters = {"unreadable_frames": 5, "c2_frames": 9}
            self.assertEqual(p.get_status()["read_errors"], 5)
            self.assertEqual(p.get_status()["c2_frames"], 9)
            p.play(3, 12)
            self.assertEqual(p.get_status()["read_errors"], 0)

    def test_stdin_command_contract(self):
        p = player.CDPlayer()
        with mock.patch.object(player.Path, "mkdir"), mock.patch.object(player.Path, "unlink"), \
             mock.patch.object(player.subprocess, "Popen", return_value=FakeProc()) as spawn:
            p._spawn_mpv("/dev/cdrom", "hw:2,0")
        cmd = spawn.call_args.args[0]
        for arg in ["--demuxer=rawaudio", "--demuxer-rawaudio-format=s16le",
                    "--demuxer-rawaudio-rate=44100", "--demuxer-rawaudio-channels=2",
                    "--demuxer-max-bytes=352800", "--demuxer-max-back-bytes=0",
                    "--cache-secs=1", "--audio-device=alsa/plughw:2,0"]:
            self.assertIn(arg, cmd)
        self.assertEqual(cmd[-1], "-")
        self.assertFalse(any("cdda://" in x for x in cmd))
        self.assertEqual(spawn.call_args.kwargs["stdin"], player.subprocess.PIPE)

    def test_sense_resolution_does_not_expire(self):
        gate = threading.Event()
        with ExitStack() as stack:
            p, _, readers, _ = fake_player(stack, pcm_gate=gate)
            errors = []
            t = threading.Thread(target=lambda: self._play_capture(p, errors))
            t.start()
            deadline = time.monotonic() + 1
            while not readers and time.monotonic() < deadline:
                time.sleep(0.005)
            readers[-1].sense_failure.set()
            t.join(1)
            self.assertFalse(t.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(p.get_status()["buffering"])
            gate.set()

    @staticmethod
    def _play_capture(p, errors):
        try:
            p.play(1, 12)
        except Exception as exc:
            errors.append(exc)

    def test_stop_is_fast_with_reader_still_inside_read(self):
        with ExitStack() as stack:
            p, proc, readers, _ = fake_player(stack)
            p.play(1, 12)
            release = threading.Event()
            readers[-1].join = lambda timeout=None: release.wait(1)
            t0 = time.monotonic()
            p.stop()
            self.assertLess(time.monotonic() - t0, 0.2)
            self.assertTrue(readers[-1].stopped.is_set())
            release.set()
            self.assertTrue(p._cleanup_done.wait(2))

    def test_eject_waits_for_retry_restoration_off_request_path(self):
        with ExitStack() as stack:
            p, _, _, _ = fake_player(stack)
            stack.enter_context(mock.patch.object(player, "_player", p))
            p.play(1, 12)
            called = threading.Event()
            stack.enter_context(mock.patch.object(player.subprocess, "run", side_effect=lambda *a, **k: called.set()))
            player.eject()
            self.assertTrue(called.wait(2))

    def test_shutdown_waits_for_factory_restore(self):
        with ExitStack() as stack:
            p, _, readers, _ = fake_player(stack)
            p.play(1, 12)
            stack.enter_context(mock.patch.object(player, "_player", p))
            restored = threading.Event()
            readers[-1].join = lambda timeout=None: restored.wait(2)
            done = threading.Event()
            t = threading.Thread(target=lambda: (player.shutdown(), done.set()))
            t.start()
            self.assertFalse(done.wait(0.03))
            restored.set()
            t.join(2)
            self.assertTrue(done.is_set())

    def test_eof_fires_last_track_once(self):
        with ExitStack() as stack:
            p, _, readers, _ = fake_player(stack)
            p.play(12, 12)
            readers[-1].finished.set()
            p._writer.frames_written = 750
            ended = []
            p.set_on_track_end(ended.append)
            p._ipc_query = lambda prop, timeout=0.5: prop == "eof-reached"
            player.CDPlayer._monitor_mpv(p, p._mpv_generation)
            self.assertEqual(ended, [12])
            self.assertFalse(p.drive_busy)

    def test_runtime_no_position_deadline_reports_drive_stall(self):
        with ExitStack() as stack:
            p, procs, readers, _ = fake_player(stack)
            p.play(1, 12)
            errors = []
            p.set_on_disc_error(lambda: errors.append(p.get_status()["error"]))
            p._segment_started = time.monotonic() - 16
            player.CDPlayer._monitor_mpv(p, p._mpv_generation)
            self.assertEqual(errors, ["drive_stalled"])
            self.assertTrue(readers[0].stopped.is_set())
            self.assertTrue(p._cleanup_done.wait(2))
            self.assertTrue(procs[0].terminated)

    def test_old_monitor_completion_cannot_stop_a_new_segment(self):
        for finished in (False, True):
            with self.subTest(finished=finished), ExitStack() as stack:
                p, _, readers, _ = fake_player(stack)
                p.play(1, 12)
                generation = p._mpv_generation
                if finished:
                    readers[-1].finished.set()
                    p._writer.frames_written = 9000
                callbacks = []
                p.set_on_track_end(lambda n: callbacks.append(n))
                p.set_on_disc_error(lambda: (callbacks.append("error"), p.stop()))
                def query(prop, timeout=0.5):
                    if prop == "eof-reached":
                        p.play(3, 12)
                        return True
                    return 1
                p._ipc_query = query
                player.CDPlayer._monitor_mpv(p, generation)
                self.assertEqual(p.current_track(), 3)
                self.assertTrue(p.drive_busy)
                self.assertEqual(callbacks, [])


class PlaybackConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patch = mock.patch.dict(os.environ, {"CDPCORE_CONFIG_DIR": self.directory.name})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CDPCORE_READ_MODE", None)

    def test_defaults_persistence_and_environment_override(self):
        import playback_config as cfg
        self.assertEqual(cfg.get_settings(), {"read_mode": "continuous", "gap_treatment": "mirror"})
        cfg.save_settings({"read_mode": "recovery", "gap_treatment": "silence"})
        self.assertEqual(cfg.get_settings(), {"read_mode": "recovery", "gap_treatment": "silence"})
        self.assertEqual(cfg.config_file().name, "playback.conf")
        with mock.patch.dict(os.environ, {"CDPCORE_READ_MODE": "continuous"}):
            self.assertEqual(cfg.get_settings()["read_mode"], "continuous")
            self.assertEqual(cfg.get_persisted()["read_mode"], "recovery")
        with mock.patch.dict(os.environ, {"CDPCORE_READ_MODE": "invalid"}):
            self.assertEqual(cfg.get_settings()["read_mode"], "recovery")

    def test_invalid_settings_do_not_change_persisted_file(self):
        import playback_config as cfg
        cfg.save_settings({"read_mode": "recovery"})
        original = cfg.config_file().read_bytes()
        for body in ({}, {"read_mode": "bad"}, {"gap_treatment": None}, {"unknown": "x"}, []):
            with self.assertRaises(ValueError):
                cfg.save_settings(body)
            self.assertEqual(cfg.config_file().read_bytes(), original)

    def test_corrupt_file_and_invalid_fields_fall_back(self):
        import playback_config as cfg
        cfg.save_settings({"gap_treatment": "silence"})
        cfg.config_file().write_text("bad json", encoding="ascii")
        self.assertEqual(cfg.get_settings(), cfg.DEFAULTS)
        cfg.config_file().write_text('{"read_mode": "bad", "gap_treatment": "silence"}', encoding="ascii")
        self.assertEqual(cfg.get_settings(), {"read_mode": "continuous", "gap_treatment": "silence"})

    def test_next_segment_applies_settings_current_segment_keeps_policy(self):
        import playback_config as cfg
        with ExitStack() as stack:
            p, _, readers, _ = fake_player(stack)
            p.play(1, 12)
            self.assertEqual(readers[-1].policy["mode"], "continuous")
            cfg.save_settings({"read_mode": "recovery", "gap_treatment": "silence"})
            self.assertEqual(p.get_status()["read_mode"], "continuous")
            p.play(2, 12)
            self.assertEqual(readers[-1].policy, {"mode": "recovery", "gap_treatment": "silence"})

    def test_api_settings_admin_gate_and_validation(self):
        from fastapi.testclient import TestClient
        import auth
        import main
        auth.set_mode("pin")
        client = TestClient(main.app)
        self.addCleanup(client.close)
        for route in ("/system/playback/config", "/system/playback/status"):
            self.assertEqual(client.get(route).status_code, 401)
        self.assertEqual(client.post("/system/playback/config", json={"read_mode": "recovery"}).status_code, 401)
        token, _ = auth.issue_token()
        client.cookies.set(auth.COOKIE_NAME, token)
        response = client.get("/system/playback/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["gap_treatment"], "mirror")
        response = client.post("/system/playback/config", json={"read_mode": "recovery", "gap_treatment": "silence"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/system/playback/config").json()["read_mode"], "recovery")
        self.assertEqual(client.post("/system/playback/config", json={"read_mode": "invalid"}).status_code, 400)

    def test_api_status_counters_and_runtime_stall_reason(self):
        from fastapi.testclient import TestClient
        import main
        from state import get_state, CDState
        state = get_state()
        saved = state.__dict__.copy()
        self.addCleanup(lambda: state.__dict__.update(saved))
        state.state = CDState.PLAYING
        ps = {"state": "play", "elapsed": "1.5", "buffering": False,
              "read_errors": 7, "c2_frames": 4, "c2_available": False, "read_mode": "recovery"}
        with mock.patch.object(player, "get_player_status", return_value=ps):
            client = TestClient(main.app)
            self.addCleanup(client.close)
            status = client.get("/status").json()
            self.assertEqual(status["read_errors"], 7)
            self.assertEqual(status["c2_frames"], 4)
            self.assertFalse(status["c2_available"])
            self.assertEqual(status["read_mode"], "recovery")
        with mock.patch.object(player, "get_player_status", return_value={"error": "drive_stalled"}), \
             mock.patch.object(main, "_schedule_broadcast"):
            main._on_disc_error()
        self.assertEqual(state.state, CDState.LOADED)
        self.assertEqual(state.error, "drive_stalled")


if __name__ == "__main__":
    unittest.main()
