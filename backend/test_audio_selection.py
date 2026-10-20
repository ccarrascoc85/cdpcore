"""Stable USB identity, persisted preference, selection policy and API tests."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import audio_config
import cd_reader


def device(index, card_id, usb_id=None, is_usb=True):
    result = {"id": f"hw:{index},0", "card_id": card_id, "name": card_id + " DAC",
              "is_usb": is_usb, "auto_selected": False}
    if usb_id is not None:
        result["usb_id"] = usb_id
    return result


class AudioConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"CDPCORE_CONFIG_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)

    def test_round_trip_and_clear(self):
        self.assertIsNone(audio_config.get_preferred())
        dac = device(1, "E50", "152a:8750")
        audio_config.set_preferred(dac)
        expected = {"card_id": "E50", "usb_id": "152a:8750", "name": "E50 DAC"}
        self.assertEqual(audio_config.get_preferred(), expected)
        self.assertEqual(json.loads(audio_config.config_file().read_text()), {"preferred": expected})
        self.assertFalse(audio_config.config_file().with_suffix(".conf.tmp").exists())
        audio_config.clear_preferred()
        self.assertIsNone(audio_config.get_preferred())

    def test_optional_usb_id_and_unicode_name(self):
        dac = device(1, "E50")
        dac["name"] = "DAC \u00e9"
        audio_config.set_preferred(dac)
        self.assertEqual(audio_config.get_preferred()["name"], dac["name"])
        self.assertIsNone(audio_config.get_preferred()["usb_id"])

    def test_corrupt_and_invalid_config(self):
        for contents in ("{bad", "[]", "null", '{"preferred":[]}',
                         '{"preferred":{"card_id":""}}',
                         '{"preferred":{"card_id":"E50","name":"DAC","usb_id":1}}'):
            with self.subTest(contents=contents):
                audio_config.config_file().write_text(contents)
                self.assertIsNone(audio_config.get_preferred())
        audio_config.config_file().write_bytes(b"\xff")
        self.assertIsNone(audio_config.get_preferred())

    def test_failed_replace_preserves_previous_preference(self):
        audio_config.set_preferred(device(1, "E50"))
        with patch.object(Path, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                audio_config.set_preferred(device(2, "Other"))
        self.assertEqual(audio_config.get_preferred()["card_id"], "E50")
        self.assertFalse(audio_config.config_file().with_suffix(".conf.tmp").exists())


class AudioIdentityTests(unittest.TestCase):
    def test_proc_card_id_and_usb_identity(self):
        contents = (" 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0\n"
                    " 1 [E50            ]: USB-Audio - Topping E50\n")

        def read(path, *args, **kwargs):
            if str(path).replace("\\", "/").endswith("/cards"):
                return contents
            return "152a:8750\n"

        with patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", read):
            devices = cd_reader.enumerate_alsa_devices()
        self.assertEqual(devices[0]["card_id"], "vc4hdmi0")
        self.assertFalse(devices[0]["is_usb"])
        self.assertNotIn("usb_id", devices[0])
        self.assertEqual(devices[1]["card_id"], "E50")
        self.assertEqual(devices[1]["usb_id"], "152a:8750")
        self.assertTrue(devices[1]["auto_selected"])

    def test_aplay_fallback_card_id_without_usbid(self):
        result = Mock(stdout="card 2: USB [USB DAC], device 0: Audio [Audio]\n")
        with patch.object(cd_reader, "_parse_proc_asound_cards", return_value=[]), \
                patch.object(cd_reader.subprocess, "run", return_value=result), \
                patch.object(Path, "read_text", side_effect=OSError("missing")):
            devices = cd_reader.enumerate_alsa_devices()
        self.assertEqual(devices[0]["card_id"], "USB")
        self.assertEqual(devices[0]["id"], "hw:2,0")
        self.assertTrue(devices[0]["is_usb"])
        self.assertNotIn("usb_id", devices[0])


class AudioSelectionTests(unittest.TestCase):
    def setUp(self):
        import main
        from fastapi.testclient import TestClient
        from state import CDPlayerState, CDState

        self.main = main
        self.state = CDPlayerState()
        self.state.state = CDState.LOADED
        self.state.alsa_device = "hw:99,0"
        self.devices = [device(1, "E50", "152a:8750"), device(2, "Other", "1234:5678"),
                        device(0, "HDMI", is_usb=False)]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patches = [patch.dict(os.environ, {"CDPCORE_CONFIG_DIR": directory.name,
                                           "CDPCORE_ADMIN_AUTH": "off"}),
                   patch.object(main, "get_state", return_value=self.state),
                   patch.object(main, "_alsa_devices", []),
                   patch.object(main, "_audio_snapshot", {}),
                   patch.object(cd_reader, "enumerate_alsa_devices",
                                side_effect=lambda: [dict(d) for d in self.devices]),
                   patch.object(main, "_schedule_broadcast"),
                   patch.object(main.player, "play"), patch.object(main.player, "resume"),
                   patch.object(main.player, "stop")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # No lifespan: tests must not poll or touch the physical drive.
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def refresh(self):
        return self.main._refresh_audio_devices("test")

    def test_single_usb_ignores_and_keeps_other_preference(self):
        audio_config.set_preferred(self.devices[1])
        self.devices = [self.devices[0], self.devices[2]]
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:1,0")
        self.assertEqual(self.state.audio_state, "usb_single")
        self.assertEqual(audio_config.get_preferred()["card_id"], "Other")
        self.assertEqual(self.main._playback_device_ready(), (True, ""))
        payload = self.client.get("/system/audio").json()
        self.assertFalse(payload["selectable"])

    def test_present_preference_passes_play_gate(self):
        audio_config.set_preferred(self.devices[1])
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:2,0")
        self.assertEqual(self.state.audio_state, "usb_multiple")
        self.assertEqual(self.main._playback_device_ready(), (True, ""))
        self.assertEqual(self.client.post("/play").status_code, 200)
        self.main.player.play.assert_called_once_with(1, 0, alsa_device="hw:2,0")

    def test_missing_preference_keeps_present_current_usb(self):
        audio_config.set_preferred(device(8, "Missing"))
        self.state.alsa_device = "hw:1,0"
        self.state.audio_state = "usb_single"
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:1,0")
        self.assertEqual(audio_config.get_preferred()["card_id"], "Missing")

    def test_no_preference_keeps_present_current_usb(self):
        self.state.alsa_device = "hw:2,0"
        self.state.audio_state = "usb_multiple"
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:2,0")
        self.assertEqual(self.main._playback_device_ready(), (True, ""))
        self.assertIsNone(audio_config.get_preferred())

    def test_second_dac_connected_keeps_and_records_the_one_in_use(self):
        everything = list(self.devices)
        self.devices = [everything[1], everything[2]]          # only "Other" (hw:2,0) + HDMI
        self.state.alsa_device = "hw:0,0"
        self.refresh()
        self.assertEqual(self.state.audio_state, "usb_single")
        self.devices = everything                              # E50 connected afterwards
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:2,0")
        self.assertEqual(self.state.audio_state, "usb_multiple")
        self.assertEqual(audio_config.get_preferred()["card_id"], "Other")
        self.devices[1]["id"] = "hw:5,0"                       # after a reboot the index changes
        self.state.alsa_device = "hw:0,0"
        self.state.audio_state = "default"
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:5,0")

    def test_startup_with_several_dacs_and_no_preference_chooses_nothing(self):
        self.state.alsa_device = "hw:0,0"                      # startup placeholder
        self.state.audio_state = "default"
        self.refresh()
        self.assertEqual(self.state.alsa_device, "")
        self.assertIsNone(audio_config.get_preferred())
        self.assertEqual(self.main._playback_device_ready(), (False, "audio_device_ambiguous"))

    def test_startup_where_one_usb_card_was_seen_first_is_still_recorded(self):
        # Documented limit: if the backend starts while USB cards are still
        # appearing, the first one is used alone and then recorded. From then
        # on the choice is stable and visible on the system page.
        self.devices = [self.devices[0], self.devices[2]]
        self.state.alsa_device = "hw:0,0"
        self.refresh()
        self.devices.append(device(2, "Other", "1234:5678"))
        self.refresh()
        self.assertEqual(audio_config.get_preferred()["card_id"], "E50")

    def test_invalid_or_internal_current_refuses_play(self):
        for current in ("hw:99,0", "hw:0,0", "default"):
            with self.subTest(current=current):
                self.state.alsa_device = current
                self.refresh()
                self.assertEqual(self.state.alsa_device, "")
                self.assertEqual(self.main._playback_device_ready(),
                                 (False, "audio_device_ambiguous"))
                for route in ("/play", "/play/1"):
                    response = self.client.post(route)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()["detail"], "audio_device_ambiguous")
        self.main.player.play.assert_not_called()

    def test_play_gate_rejects_present_internal_even_with_stale_selection(self):
        self.refresh()
        self.state.alsa_device = "hw:0,0"
        self.assertEqual(self.main._playback_device_ready(), (False, "audio_device_ambiguous"))

    def test_usb_and_card_identity_survive_index_change_and_restart(self):
        audio_config.set_preferred(self.devices[0])
        self.refresh()
        self.devices[0]["id"] = "hw:7,0"
        self.state.alsa_device = "default"
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:7,0")
        self.assertEqual(audio_config.get_preferred()["usb_id"], "152a:8750")

    def test_exact_identity_is_tried_before_card_only(self):
        audio_config.set_preferred(self.devices[0])
        self.devices.insert(0, device(3, "E50", "9999:9999"))
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:1,0")

    def test_card_only_fallback_when_usb_id_unavailable(self):
        audio_config.set_preferred(self.devices[0])
        del self.devices[0]["usb_id"]
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:1,0")

    def test_duplicate_identity_does_not_pick_arbitrary_dac(self):
        audio_config.set_preferred(self.devices[0])
        self.devices.append(device(3, "E50", "152a:8750"))
        self.refresh()
        self.assertEqual(self.main._playback_device_ready(), (False, "audio_device_ambiguous"))
        response = self.client.post("/system/audio/select", json={"card_id": "E50"})
        self.assertEqual(response.status_code, 400)

    def test_unplug_single_fallback_and_replug_restores_preference(self):
        preferred = self.devices[0]
        audio_config.set_preferred(preferred)
        self.refresh()
        self.devices = self.devices[1:]
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:2,0")
        self.assertEqual(self.state.audio_state, "usb_single")
        self.assertEqual(audio_config.get_preferred()["card_id"], "E50")
        self.devices.append({**preferred, "id": "hw:8,0"})
        self.refresh()
        self.assertEqual(self.state.alsa_device, "hw:8,0")

    def test_no_usb_or_devices_blocks_without_clearing_preference(self):
        audio_config.set_preferred(self.devices[0])
        for devices, expected in (([self.devices[2]], "audio_no_usb_dac"),
                                  ([], "audio_device_missing")):
            with self.subTest(devices=devices):
                self.devices = devices
                self.refresh()
                self.assertEqual(self.main._playback_device_ready(), (False, expected))
                self.assertEqual(audio_config.get_preferred()["card_id"], "E50")

    def test_select_persists_applies_and_broadcasts(self):
        response = self.client.post("/system/audio/select", json={"card_id": "Other"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload, self.client.get("/system/audio").json())
        self.assertTrue(payload["selectable"])
        self.assertEqual(payload["audio_state"], "usb_multiple")
        self.assertEqual(payload["preferred"]["card_id"], "Other")
        selected = [d for d in payload["devices"] if d["selected"]]
        self.assertEqual([d["id"] for d in selected], ["hw:2,0"])
        self.assertEqual(selected[0]["usb_id"], "1234:5678")
        self.assertEqual(audio_config.get_preferred(), payload["preferred"])
        self.main._schedule_broadcast.assert_called_once()
        self.main.player.play.assert_not_called()

    def test_select_rejects_unknown_internal_or_invalid_card(self):
        audio_config.set_preferred(self.devices[0])
        for body in ({"card_id": "missing"}, {"card_id": "HDMI"}, {"card_id": ""},
                     {"card_id": 1}, {}, {"card_id": None}):
            with self.subTest(body=body):
                response = self.client.post("/system/audio/select", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(audio_config.get_preferred()["card_id"], "E50")

    def test_select_validates_fresh_enumeration(self):
        self.refresh()
        self.devices = self.devices[1:]
        self.assertEqual(self.client.post("/system/audio/select",
                                         json={"card_id": "E50"}).status_code, 400)
        self.assertIsNone(audio_config.get_preferred())

    def test_forget_keeps_present_current_usb(self):
        self.client.post("/system/audio/select", json={"card_id": "Other"})
        response = self.client.post("/system/audio/forget")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["preferred"])
        self.assertIsNone(audio_config.get_preferred())
        self.assertEqual(self.state.alsa_device, "hw:2,0")

    def test_pin_mode_gates_all_audio_routes_and_accepts_session(self):
        with patch.dict(os.environ, {"CDPCORE_ADMIN_AUTH": "pin"}):
            for method, path, body in (("GET", "/system/audio", None),
                                       ("POST", "/system/audio/select", {"card_id": "Other"}),
                                       ("POST", "/system/audio/forget", None)):
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401)
            self.assertIsNone(audio_config.get_preferred())
            self.main.auth.set_pin("246802")
            self.assertEqual(self.client.post("/admin/unlock",
                                             json={"pin": "246802"}).status_code, 200)
            self.assertEqual(self.client.post("/system/audio/select",
                                             json={"card_id": "Other"}).status_code, 200)
            self.assertEqual(self.client.get("/system/audio").status_code, 200)
            self.assertEqual(self.client.post("/system/audio/forget").status_code, 200)

    def test_old_selection_route_gone_and_diagnostic_routes_kept(self):
        response = self.client.post("/devices/select?device_id=hw:2,0")
        self.assertIn(response.status_code, (404, 405))
        response = self.client.post("/devices/rescan")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/devices").json(), response.json()["devices"])

    def test_corrupt_config_does_not_crash_policy(self):
        audio_config.config_file().write_text("not-json{")
        self.refresh()
        self.assertIsNone(self.client.get("/system/audio").json()["preferred"])
        self.assertEqual(self.main._playback_device_ready(), (False, "audio_device_ambiguous"))

    def test_running_segment_untouched_selection_applies_to_next_play(self):
        from state import CDState
        self.state.state = CDState.PLAYING
        self.state.track_number = 3
        response = self.client.post("/system/audio/select", json={"card_id": "Other"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state.state, CDState.PLAYING)
        self.assertEqual(self.state.track_number, 3)
        self.main.player.play.assert_not_called()
        self.main.player.stop.assert_not_called()
        self.main.player.resume.assert_not_called()
        self.state.state = CDState.LOADED
        self.assertEqual(self.client.post("/play").status_code, 200)
        self.main.player.play.assert_called_once_with(1, 0, alsa_device="hw:2,0")


class PlaybackCapabilityTests(unittest.TestCase):
    def test_capture_only_card_is_not_an_output(self):
        listings = {"/proc/asound/card1": ["pcm0c", "id", "usbid"],
                    "/proc/asound/card2": ["pcm0p", "pcm0c", "id"],
                    "/proc/asound/card3": ["id"]}

        def listdir(path):
            key = str(path).replace("\\", "/")
            if key in listings:
                return listings[key]
            raise OSError("no such directory")

        with patch.object(cd_reader.os, "listdir", side_effect=listdir):
            self.assertFalse(cd_reader._card_has_playback(1))   # webcam microphone
            self.assertTrue(cd_reader._card_has_playback(2))    # DAC with a mic input
            self.assertTrue(cd_reader._card_has_playback(3))    # no pcm entries listed: unknown
            self.assertTrue(cd_reader._card_has_playback(9))    # unreadable: unknown
