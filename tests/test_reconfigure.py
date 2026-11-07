import unittest

from installer import reconfigure
from tests.test_flow import settings


class MergeTest(unittest.TestCase):
    def test_blank_secrets_keep_current(self):
        old = settings(tailscale_key="tskey-auth-old")
        new = reconfigure.merge(old, {"password": "", "vpn_provider": "protonvpn", "vpn_fields": {},
                                      "tailscale_key": ""})
        self.assertEqual((new.password, new.vpn_fields, new.tailscale_key),
                         (old.password, old.vpn_fields, old.tailscale_key))

    def test_vpn_removed(self):
        new = reconfigure.merge(settings(), {"vpn_provider": ""})
        self.assertEqual((new.vpn_provider, new.vpn_fields), (None, {}))
        self.assertEqual(reconfigure.changes(settings(), new), {"vpn": "remove"})

    def test_new_provider_takes_only_its_fields(self):
        new = reconfigure.merge(settings(), {"vpn_provider": "mullvad", "vpn_fields": {"WIREGUARD_PRIVATE_KEY": "m"}})
        self.assertEqual(new.vpn_fields, {"WIREGUARD_PRIVATE_KEY": "m"})

    def test_same_provider_new_key(self):
        new = reconfigure.merge(settings(), {"vpn_provider": "protonvpn", "vpn_fields": {"WIREGUARD_PRIVATE_KEY": "k2"}})
        self.assertEqual(reconfigure.changes(settings(), new), {"vpn": "change"})

    def test_tailscale_remove(self):
        new = reconfigure.merge(settings(tailscale_key="tskey-a"), {"remove_tailscale": True})
        self.assertEqual(new.tailscale_key, "")

    def test_platform_fields_never_change(self):
        new = reconfigure.merge(settings(), {"platform": "windows", "puid": 0, "home": "/"})
        self.assertEqual((new.platform, new.puid, new.home), ("linux", 1000, "/home/z"))

    def test_text_fields_are_trimmed(self):
        new = reconfigure.merge(settings(), {"data_dir": " /srv/new ", "username": " bob "})
        self.assertEqual((new.data_dir, new.username), ("/srv/new", "bob"))


class ChangesTest(unittest.TestCase):
    def test_changes(self):
        old = settings()
        self.assertEqual(reconfigure.changes(old, old), {})
        self.assertEqual(reconfigure.changes(settings(vpn_provider=None, vpn_fields={}), old), {"vpn": "add"})
        self.assertEqual(reconfigure.changes(old, settings(password="anotherone")), {"account": "change"})
        self.assertEqual(reconfigure.changes(old, settings(username="bob")), {"account": "change"})
        self.assertEqual(reconfigure.changes(old, settings(data_dir="/srv/new")), {"folder": "change"})
        self.assertEqual(reconfigure.changes(old, settings(lang="fr")), {"lang": "change"})
        self.assertEqual(reconfigure.changes(old, settings(timezone="Europe/Paris")), {"timezone": "change"})
        self.assertEqual(reconfigure.changes(old, settings(tailscale_key="tskey-a")), {"tailscale": "add"})
        self.assertEqual(reconfigure.changes(settings(tailscale_key="tskey-a"), settings(tailscale_key="tskey-b")),
                         {"tailscale": "change"})

    def test_steps(self):
        S = reconfigure.steps
        self.assertEqual(S({"lang": "change"}, True), ["step_save"])
        self.assertEqual(S({"timezone": "change"}, True), ["step_compose", "step_vpn", "step_save"])
        self.assertEqual(S({"timezone": "change"}, False), ["step_compose", "step_save"])
        self.assertEqual(S({"vpn": "remove"}, False), ["step_compose", "step_download_client", "step_save"])
        self.assertEqual(S({"vpn": "add"}, True), ["step_compose", "step_vpn", "step_download_client", "step_save"])
        self.assertEqual(S({"vpn": "change"}, True), ["step_compose", "step_vpn", "step_save"])
        self.assertEqual(S({"folder": "change", "account": "change"}, False),
                         ["step_folder", "step_compose", "step_rescan", "step_account", "step_save"])
        self.assertEqual(S({"tailscale": "remove"}, False), ["step_compose", "step_save"])
        self.assertEqual(S({"tailscale": "change"}, False), ["step_compose", "step_tailscale", "step_save"])

    def test_outage(self):
        self.assertEqual(reconfigure.outage({"lang": "change", "account": "change"}), "outage_none")
        self.assertEqual(reconfigure.outage({"timezone": "change"}), "outage_apps")
        self.assertEqual(reconfigure.outage({"vpn": "change"}), "outage_downloads")
        self.assertEqual(reconfigure.outage({"vpn": "add", "folder": "change"}), "outage_apps")


if __name__ == "__main__":
    unittest.main()


import json
import os
import tempfile
from dataclasses import replace
from types import SimpleNamespace

from installer import flow
from installer.docker import DockerError
from installer.settings import Settings
from tests.test_flow import FakeDocker


class Ops:
    def __init__(self, fail_set_host=0, not_changed=()):
        self.calls, self.fail_set_host, self.not_changed = [], fail_set_host, list(not_changed)

    def set_host(self, app, host):
        self.calls.append(("set_host", app, host))
        if self.fail_set_host:
            self.fail_set_host -= 1
            raise RuntimeError("Radarr refused")

    def change_account(self, old, new, current):
        self.calls.append(("account", new.password, tuple(sorted(current.items()))))
        if getattr(self, "peek", None):
            self.peek()
        return list(self.not_changed)

    def rescan(self, s):
        self.calls.append(("rescan",))
        if getattr(self, "fail_rescan", False):
            raise RuntimeError("Jellyfin 401")

    def profiles(self, login, new):
        self.calls.append(("profiles", login.password, tuple(new.profiles or ())))
        return {"radarr": {"profiles": {k: 9 for k in new.profiles or ()}}}, list(getattr(self, "kept", []))

    def subtitles(self, new):
        self.calls.append(("subtitles", tuple(new.subtitle_langs)))

    def metadata(self, login, new):
        self.calls.append(("metadata", login.password, new.metadata_lang))
        if getattr(self, "fail_metadata", False):
            raise RuntimeError("Jellyfin down")


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.events = []

    def run_apply(self, old, new, ops=None, dk=None, current=None):
        flow._write_state(old, self.dir, old.data_dir, False)
        self.dk, self.ops = dk or FakeDocker(), ops or Ops()
        return reconfigure.apply(old, new, current or {}, self.events.append, dk=self.dk, state_dir=self.dir,
                                 vpn_wait=1, ops=self.ops)

    def saved(self):
        with open(os.path.join(self.dir, "settings.json")) as f:
            return Settings.from_json(f.read())

    def compose_json(self):
        with open(os.path.join(self.dir, "compose.json")) as f:
            return json.load(f)

    def composes(self):
        return [c[1:] for c in self.dk.calls if c[0] == "compose"]

    def test_lang_only_writes_settings(self):
        self.assertEqual(self.run_apply(settings(), settings(lang="fr")), [])
        self.assertEqual(self.saved().lang, "fr")
        self.assertEqual(self.composes(), [])
        self.assertTrue(self.events[-1].done)

    def test_timezone_recreates_and_waits_vpn(self):
        self.run_apply(settings(), settings(timezone="Europe/Paris"))
        c = self.composes()
        self.assertEqual(c[0], ("up", "-d", "--remove-orphans"))
        self.assertEqual(c[1][0], "ps")
        self.assertIn(("up", "-d", "--force-recreate", "--no-deps", "qbittorrent"), c)
        self.assertEqual(self.saved().timezone, "Europe/Paris")

    def test_vpn_removed_steps_and_orphans(self):
        new = settings(vpn_provider=None, vpn_fields={})
        self.run_apply(settings(), new)
        self.assertIn(("up", "-d", "--remove-orphans"), self.composes())
        self.assertNotIn("gluetun", self.compose_json()["services"])
        self.assertEqual([c for c in self.ops.calls if c[0] == "set_host"],
                         [("set_host", "radarr", "qbittorrent"), ("set_host", "sonarr", "qbittorrent")])

    def test_failure_rolls_back(self):
        old = settings()
        self.assertIsNone(self.run_apply(old, settings(vpn_provider=None, vpn_fields={}), ops=Ops(fail_set_host=1)))
        self.assertEqual(self.saved(), old)
        self.assertIn("gluetun", self.compose_json()["services"])
        self.assertEqual(self.composes().count(("up", "-d", "--remove-orphans")), 2)
        self.assertIn(("set_host", "radarr", "gluetun"), self.ops.calls)
        self.assertEqual((self.events[-1].error, self.events[-1].step), ("err_apply_failed", "step_download_client"))

    def test_account_partial_keeps_new_password(self):
        result = self.run_apply(settings(), settings(password="new-pass1"), ops=Ops(not_changed=["jellyfin"]))
        self.assertEqual(result, ["jellyfin"])
        self.assertEqual(self.saved().password, "new-pass1")
        self.assertEqual(self.events[-1].detail, "jellyfin")

    def test_account_all_failed_keeps_old_password(self):
        everyone = ["radarr", "sonarr", "prowlarr", "bazarr", "qbittorrent", "jellyfin"]
        self.run_apply(settings(), settings(password="new-pass1"), ops=Ops(not_changed=everyone))
        self.assertEqual(self.saved().password, "longenough")

    def test_account_passes_current_passwords(self):
        self.run_apply(settings(), settings(password="new-pass1"), current={"jellyfin": "typed"})
        self.assertIn(("account", "new-pass1", (("jellyfin", "typed"),)), self.ops.calls)

    def test_tailscale_removed_logs_out_first(self):
        self.run_apply(settings(tailscale_key="tskey-a"), settings())
        c = self.composes()
        self.assertEqual(c[0], ("exec", "-T", "tailscale", "tailscale", "logout"))
        self.assertEqual(c[1], ("up", "-d", "--remove-orphans"))
        self.assertIn(("rm", "volume", "plaid_tailscale-state"), self.dk.calls)

    def test_tailscale_key_changed_starts_over(self):
        self.run_apply(settings(tailscale_key="tskey-a"), settings(tailscale_key="tskey-b"))
        c = self.composes()
        i = c.index(("rm", "-sf", "tailscale"))
        self.assertIn(("exec", "-T", "tailscale", "tailscale", "logout"), c[:i])
        self.assertEqual(c[i + 1], ("up", "-d", "tailscale"))

    def test_folder_change(self):
        self.run_apply(settings(), settings(data_dir="/srv/new"))
        self.assertIn(("ensure-existing", "/srv/new"), self.dk.calls)
        self.assertNotIn(("ensure", "/srv/new"), self.dk.calls)
        self.assertIn(("rescan",), self.ops.calls)
        data = self.compose_json()["services"]["radarr"]["volumes"][-1]
        self.assertEqual(data["source"], "/srv/new")


class CheckTest(unittest.TestCase):
    def test_folder_missing_is_refused(self):
        dk = FakeDocker(folder="missing")
        self.assertEqual(reconfigure.check(settings(), settings(data_dir="/srv/new"), dk), ["err_folder_missing"])

    def test_folder_without_library_warns(self):
        found = reconfigure.check(settings(), settings(data_dir="/srv/new"), FakeDocker(folder="other"))
        self.assertEqual(found, ["warn_folder_no_library"])
        self.assertEqual(reconfigure.check(settings(), settings(data_dir="/srv/new"), FakeDocker(folder="unknown")),
                         ["err_folder_unreadable"])

    def test_vpn_needs_tun(self):
        old = settings(vpn_provider=None, vpn_fields={})
        self.assertEqual(reconfigure.check(old, settings(), FakeDocker(devices=set())), ["err_no_tun"])

    def test_invalid_values(self):
        self.assertIn("err_password_short", reconfigure.check(settings(), settings(password="short"), FakeDocker()))


class WorkerTest(unittest.TestCase):
    def test_start_apply_mounts_state(self):
        dk = FakeDocker()
        with tempfile.TemporaryDirectory() as d:
            flow.start_apply(settings(lang="fr"), {"jellyfin": "x"}, dk=dk, state_dir=d)
            with open(os.path.join(d, "apply-request.json")) as f:
                req = json.load(f)
            self.assertEqual((req["settings"]["lang"], req["current"]), ("fr", {"jellyfin": "x"}))
            self.assertEqual(oct(os.stat(os.path.join(d, "apply-request.json")).st_mode & 0o777), "0o600")
        self.assertEqual(dk.calls[-1], ("detached", "img", ("--apply-worker",), (), "plaid-apply", "plaid-state:/state"))

    def test_apply_status_cleans_finished_worker(self):
        dk = FakeDocker(states={"plaid-apply": (True, False, 0)})
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "apply.json"), "w") as f:
                json.dump([{"step": "step_save"}, {"step": "step_done", "done": True, "detail": ""}], f)
            st = flow.apply_status(dk=dk, state_dir=d)
        self.assertEqual(st["state"], "done")
        self.assertIn(("rm", "container", "plaid-apply"), dk.calls)

    def test_apply_status_crashed_worker_fails_with_logs(self):
        dk = FakeDocker(states={"plaid-apply": (True, False, 1)}, log_text="Traceback")
        with tempfile.TemporaryDirectory() as d:
            st = flow.apply_status(dk=dk, state_dir=d)
        self.assertEqual((st["state"], st["events"][-1]["detail"]), ("failed", "Traceback"))

    def test_apply_status_running_and_idle(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(flow.apply_status(dk=FakeDocker(states={"plaid-apply": (True, True, 0)}),
                                               state_dir=d)["state"], "running")
            self.assertEqual(flow.apply_status(dk=FakeDocker(), state_dir=d)["state"], "idle")


class ReviewFixesApplyTest(ApplyTest):
    def test_failed_rescan_keeps_the_new_folder(self):
        ops = Ops()
        ops.fail_rescan = True
        self.assertEqual(self.run_apply(settings(), settings(data_dir="/srv/new"), ops=ops), [])
        self.assertEqual(self.saved().data_dir, "/srv/new")

    def test_new_password_saved_only_once_an_app_has_it(self):
        ops = Ops()
        seen = []
        ops.peek = lambda: seen.append(self.saved().password)
        self.run_apply(settings(), settings(timezone="Europe/Paris", password="new-pass1"), ops=ops)
        self.assertEqual(seen, ["longenough"])  # while the apps change, Plaid still signs in with the old one
        self.assertEqual(self.saved().password, "new-pass1")


class EmptyFolderTest(unittest.TestCase):
    def test_empty_folder_asks_if_the_disk_is_plugged(self):
        found = reconfigure.check(settings(), settings(data_dir="/srv/new"), FakeDocker(folder="empty"))
        self.assertEqual(found, ["warn_folder_empty"])


class StepsReportedOnceTest(ApplyTest):
    def test_each_step_reported_once(self):
        self.run_apply(settings(), settings(timezone="Europe/Paris"))  # no account step: the case seen for real
        self.assertEqual([e.step for e in self.events], ["step_compose", "step_vpn", "step_save", "step_done"])
        self.events.clear()
        self.run_apply(settings(), settings(timezone="Europe/Paris", password="new-pass1"))
        self.assertEqual([e.step for e in self.events],
                         ["step_compose", "step_vpn", "step_account", "step_save", "step_done"])


MEDIA = dict(audio_lang="fr", profiles=["mine-1080p"], default_profile="mine-1080p", subtitle_langs=["fr", "en"],
             metadata_lang="fr")


def media_settings(**kw):
    return settings(**{**MEDIA, **kw})


class MediaChangesTest(unittest.TestCase):
    def test_changes_media_kinds(self):
        C, old = reconfigure.changes, media_settings()
        self.assertEqual(C(old, media_settings(profiles=["mine-1080p", "orig-4k"])), {"profiles": "change"})
        self.assertEqual(C(old, media_settings(default_profile="orig-4k", profiles=["mine-1080p", "orig-4k"])),
                         {"profiles": "change"})
        self.assertEqual(C(old, media_settings(audio_lang="de")), {"profiles": "change"})
        self.assertEqual(C(old, media_settings(subtitle_langs=["fr"])), {"subtitles": "change"})
        self.assertEqual(C(old, media_settings(metadata_lang="en")), {"metadata": "change"})
        self.assertEqual(C(settings(), media_settings()),  # never configured: everything is new
                         {"profiles": "change", "subtitles": "change", "metadata": "change"})
        self.assertEqual(C(old, media_settings(lang="fr")), {"lang": "change", "profiles": "change"})  # renamed
        self.assertEqual(C(settings(), settings(lang="fr")), {"lang": "change"})

    def test_media_steps_order_and_outage(self):
        chg = {"profiles": "change", "subtitles": "change", "metadata": "change", "account": "change"}
        self.assertEqual(reconfigure.steps(chg, False),
                         ["step_profiles", "step_subtitles", "step_metadata", "step_account", "step_save"])
        self.assertEqual(reconfigure.outage(chg), "outage_none")

    def test_merge_media_fields(self):
        data = {"audio_lang": "de", "profiles": ["orig-4k"], "default_profile": "orig-4k", "subtitle_langs": ["it"],
                "metadata_lang": "es"}
        new = reconfigure.merge(settings(), data)
        self.assertEqual((new.audio_lang, new.profiles, new.default_profile, new.subtitle_langs, new.metadata_lang),
                         ("de", ["orig-4k"], "orig-4k", ["it"], "es"))
        self.assertIsNone(reconfigure.merge(settings(), {}).profiles)  # absent: kept as it was
        self.assertEqual(reconfigure.merge(media_settings(), {}).profiles, ["mine-1080p"])


class MediaApplyTest(ApplyTest):
    def test_apply_saves_arr_ids_and_reports_kept(self):
        ops = Ops()
        ops.kept = ["Français · 4K"]
        self.assertEqual(self.run_apply(media_settings(), media_settings(profiles=["mine-1080p", "orig-4k"]), ops),
                         [])
        self.assertEqual(self.saved().arr_ids, {"radarr": {"profiles": {"mine-1080p": 9, "orig-4k": 9}}})
        self.assertEqual(self.saved().profiles, ["mine-1080p", "orig-4k"])
        self.assertEqual(self.events[-1].kept, ["Français · 4K"])

    def test_media_steps_sign_in_with_old_account(self):
        old = media_settings()
        new = media_settings(password="brandnew1", profiles=["orig-4k"], default_profile="orig-4k",
                             metadata_lang="en", subtitle_langs=["en"])
        self.run_apply(old, new)
        names = [c[0] for c in self.ops.calls]
        self.assertEqual(names, ["profiles", "subtitles", "metadata", "account"])
        self.assertEqual(self.ops.calls[0][1], old.password)
        self.assertEqual(self.ops.calls[2][1], old.password)

    def test_failure_after_profiles_restores_them(self):
        ops = Ops()
        ops.fail_metadata = True
        old = media_settings()
        self.assertIsNone(self.run_apply(old, media_settings(profiles=["orig-4k"], default_profile="orig-4k",
                                                             metadata_lang="en"), ops))
        self.assertEqual(ops.calls[-1], ("profiles", old.password, ("mine-1080p",)))  # back to the old grid
        self.assertEqual(self.events[-1].error, "err_apply_failed")

    def test_failure_on_unconfigured_install_restores_nothing(self):
        ops = Ops()
        ops.fail_metadata = True
        self.assertIsNone(self.run_apply(settings(), media_settings(), ops))
        self.assertEqual([c[0] for c in ops.calls], ["profiles", "subtitles", "metadata"])
        # The profiles made stay in Radarr: their IDs are kept, so the next try updates them, never duplicates.
        self.assertEqual(self.saved().arr_ids, {"radarr": {"profiles": {"mine-1080p": 9}}})
        self.assertIsNone(self.saved().profiles)
