import json
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

from installer import apps, extras
from installer.docker import RESTORED_STATE
from installer.settings import Settings

HOST = "jellyfin:8096"
FT_REPO = "https://www.iamparadox.dev/jellyfin/plugins/manifest.json"


def owner():
    return Settings(data_dir="/srv/arr", username="zoe", password="owner-pw", vpn_provider=None, lang="fr",
                    home="/home/zoe", platform="linux")


class CatalogTest(unittest.TestCase):
    def test_ids(self):
        self.assertEqual([x["id"] for x in extras.CATALOG],
                         ["theme", "enhanced", "mediabar", "introdb", "episodes", "homesections", "genremapper"])

    def test_dependencies_come_along(self):
        self.assertEqual(extras.packages_for(["enhanced"]),
                         {"Jellyfin Enhanced", "Custom Tabs", "Plugin Pages", "File Transformation"})
        self.assertEqual(extras.packages_for(["theme"]), set())

    def test_theme_is_not_a_plugin(self):
        self.assertFalse(extras.has_plugins(["theme"]))
        self.assertTrue(extras.has_plugins(["theme", "introdb"]))


class ThemeTest(unittest.TestCase):
    def test_line_goes_first_and_leaves_alone(self):
        on = extras.with_theme("body{color:red}", True)
        self.assertTrue(on.startswith(extras.THEME_LINE))
        self.assertIn("body{color:red}", on)
        self.assertEqual(extras.with_theme(on, True), on)
        self.assertEqual(extras.with_theme(on, False), "body{color:red}")
        self.assertEqual(extras.with_theme("", False), "")


class StateTest(unittest.TestCase):
    def test_empty_state_and_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(extras.load_state(d), {"chosen": [], "installed": [], "removed_for_update": []})
            extras.save_state({"chosen": ["theme"], "installed": [], "removed_for_update": []}, d)
            self.assertEqual(extras.load_state(d)["chosen"], ["theme"])
        self.assertIn(extras.STATE_FILE, RESTORED_STATE)


class FakeJellyfin:
    """Repositories, packages, plugins and branding as Jellyfin 12.4 answers them."""

    def __init__(self, plugins=(), repos=(), missing=(), css=None, down=False):
        self.repos = [{"Name": "Jellyfin Stable", "Url": "https://repo.jellyfin.org/files/plugin/manifest.json",
                       "Enabled": True}] + [{"Name": "mine", "Url": u, "Enabled": True} for u in repos]
        self.plugins = {n: "Active" for n in plugins}
        self.missing, self.down = set(missing), down
        self.branding = {"SplashscreenEnabled": True, **({"CustomCss": css} if css is not None else {})}
        self.calls = []

    def __call__(self, method, url, body=None, headers=None, form=False):
        path = urllib.parse.unquote(url.split("8096", 1)[1])
        self.calls.append((method, path))
        if self.down:
            raise ConnectionRefusedError("jellyfin down")
        if path == "/System/Info":
            return {}
        if path == "/Repositories":
            if method == "POST":
                self.repos = body
            return [dict(r) for r in self.repos]
        if path.startswith("/Packages/Installed/"):
            name = path.rsplit("/", 1)[1]
            if name in self.missing:
                raise apps.HttpError(404, url, "Package not found")
            self.plugins[name] = "Restart"
            return None
        if path == "/Plugins":
            return [{"Name": n, "Id": n.lower().replace(" ", ""), "Version": "1.0", "Status": st}
                    for n, st in self.plugins.items()]
        if path.startswith("/Plugins/") and method == "DELETE":
            plugin_id = path.split("/")[2]
            self.plugins = {n: st for n, st in self.plugins.items() if n.lower().replace(" ", "") != plugin_id}
            return None
        if path == "/System/Restart":
            self.plugins = {n: "Active" for n in self.plugins}
            return None
        if path == "/System/Configuration/branding":
            if method == "POST":
                self.branding = body
            return dict(self.branding)
        raise AssertionError(path)


class ApplyTest(unittest.TestCase):
    def run_apply(self, jf, chosen, state=None):
        self.events = []
        with tempfile.TemporaryDirectory() as d, mock.patch("installer.apps.call", jf), mock.patch("time.sleep"):
            open(os.path.join(d, "jellyfin-key"), "w").write("key")
            if state:
                extras.save_state(state, d)
            failed = extras.apply(owner(), chosen, self.events.append, d, host=HOST)
            return failed, extras.load_state(d)

    def test_install_adds_missing_repositories_and_restarts(self):
        jf = FakeJellyfin(repos=[FT_REPO])
        failed, state = self.run_apply(jf, ["mediabar", "introdb"])
        self.assertEqual(failed, [])
        urls = [r["Url"] for r in jf.repos]
        self.assertEqual(urls.count(FT_REPO), 1)
        self.assertIn("https://repo.jellyfin.org/files/plugin/manifest.json", urls)  # the user's stay
        self.assertIn(("POST", "/System/Restart"), jf.calls)
        self.assertEqual(set(state["installed"]), {"Media Bar", "TheIntroDB", "File Transformation"})
        self.assertEqual(state["chosen"], ["mediabar", "introdb"])
        self.assertTrue(self.events[-1].done)

    def test_a_plugin_installed_by_hand_is_never_plaids(self):
        jf = FakeJellyfin(plugins=["File Transformation"])
        _, state = self.run_apply(jf, ["mediabar"])
        self.assertNotIn("File Transformation", state["installed"])
        _, state = self.run_apply(jf, [], state)
        self.assertIn("File Transformation", jf.plugins)
        self.assertNotIn("Media Bar", jf.plugins)

    def test_shared_dependency_stays_while_needed(self):
        jf = FakeJellyfin()
        _, state = self.run_apply(jf, ["mediabar", "introdb"])
        _, state = self.run_apply(jf, ["introdb"], state)
        self.assertNotIn("Media Bar", jf.plugins)
        self.assertIn("File Transformation", jf.plugins)
        self.assertNotIn("Media Bar", state["installed"])

    def test_missing_package_fails_only_its_extra(self):
        jf = FakeJellyfin(missing=["TheIntroDB"])
        failed, state = self.run_apply(jf, ["mediabar", "introdb"])
        self.assertEqual(failed, ["introdb"])
        self.assertEqual(state["chosen"], ["mediabar"])
        self.assertEqual(self.events[-1].detail, "introdb")

    def test_theme_only_touches_the_css(self):
        jf = FakeJellyfin(css="body{color:red}")
        self.run_apply(jf, ["theme"])
        self.assertTrue(jf.branding["CustomCss"].startswith(extras.THEME_LINE))
        self.assertTrue(jf.branding["SplashscreenEnabled"])
        self.assertNotIn(("POST", "/System/Restart"), jf.calls)
        self.run_apply(jf, [], {"chosen": ["theme"], "installed": [], "removed_for_update": []})
        self.assertEqual(jf.branding["CustomCss"], "body{color:red}")


class WorkerTest(unittest.TestCase):
    def worker(self, apply_effect):
        from types import SimpleNamespace
        from installer import flow, reconfigure
        with tempfile.TemporaryDirectory() as d:
            flow._write_state(owner(), d, "/srv/arr", False)
            flow.write_json(os.path.join(d, flow.APPLY_REQUEST), {"kind": "extras", "chosen": ["theme"]})
            dk = SimpleNamespace(connect_network=lambda c: None, self_container_id=lambda: "me")
            with mock.patch("installer.extras.apply", side_effect=apply_effect) as run:
                code = reconfigure.run_worker(dk=dk, state_dir=d)
            events = os.path.join(d, flow.APPLY_FILE)
            return code, run, json.load(open(events)) if os.path.exists(events) else []

    def test_runs_the_chosen_extras(self):
        code, run, _ = self.worker(lambda s, chosen, report, d, **kw: [])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args[0][1], ["theme"])

    def test_a_failure_is_reported(self):
        def broken(s, chosen, report, d, **kw):
            raise RuntimeError("Jellyfin did not come back after its restart")
        code, _, events = self.worker(broken)
        self.assertEqual((code, events[-1]["error"]), (1, "err_extras_failed"))

    def test_start_extras_asks_the_worker(self):
        from installer import flow
        with tempfile.TemporaryDirectory() as d, mock.patch("installer.flow._start_worker") as start:
            flow.start_extras(["theme", "introdb"], state_dir=d)
        self.assertEqual(start.call_args[0][0], {"kind": "extras", "chosen": ["theme", "introdb"]})
