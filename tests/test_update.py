import json
import os
import tempfile
import unittest
from dataclasses import asdict

from installer import flow, update
from installer.compose import DISK_APPS
from installer.docker import DockerError
from tests.test_flow import settings


class FakeDk:
    def __init__(self, disk="ok", new=("radarr",), fail_on=None):
        self.disk, self.new, self.fail_on = disk, set(new), fail_on
        self.calls, self.removed, self.pulled = [], [], False

    def host_path(self, path, platform):
        return path

    def marker_state(self, src):
        return self.disk

    def stack_containers(self):
        services = ["jellyfin", "seerr", "radarr", "sonarr", "qbittorrent", "prowlarr", "bazarr"]
        recreated = lambda svc: self.pulled and svc in self.new and (svc not in DISK_APPS or self.disk == "ok")
        return {svc: {"image": f"sha256:{svc}-{'new' if recreated(svc) else 'old'}"} for svc in services}

    def compose(self, state_dir, *args, timeout=None):
        self.calls.append(args)
        if self.fail_on and args[0] == self.fail_on:
            raise DockerError("network unreachable")
        if args[0] == "up":
            self.pulled = True

    def remove(self, kind, name):
        self.removed.append((kind, name))


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "compose.json"), "w") as f:
            json.dump({"services": {s: {} for s in ("jellyfin", "seerr", "radarr", "sonarr", "qbittorrent",
                                                    "prowlarr", "bazarr", "gluetun")}}, f)
        flow.write_json(os.path.join(self.dir, "settings.json"), json.loads(settings().to_json()))
        self.events = []

    def run_update(self, dk):
        return update.run(lambda p: self.events.append(asdict(p)), dk=dk, state_dir=self.dir)

    def test_pulls_recreates_and_drops_old_images(self):
        dk = FakeDk(new=("radarr", "seerr"))
        self.assertEqual(self.run_update(dk), ["seerr", "radarr"])
        self.assertEqual(dk.calls[0], ("pull",))
        self.assertEqual(dk.calls[1][:2], ("up", "-d"))
        self.assertEqual(set(dk.calls[1][2:]), {"jellyfin", "seerr", "radarr", "sonarr", "qbittorrent", "prowlarr",
                                                "bazarr", "gluetun"})
        self.assertEqual(sorted(dk.removed), [("image", "sha256:radarr-old"), ("image", "sha256:seerr-old")])
        self.assertEqual([e["step"] for e in self.events],
                         ["step_pull", "step_recreate", "step_cleanup_images", "step_done"])
        self.assertTrue(self.events[-1]["done"])
        self.assertEqual(self.events[-1]["detail"], "seerr,radarr")
        self.assertEqual(update.last(self.dir)["updated"], ["seerr", "radarr"])

    def test_nothing_new(self):
        dk = FakeDk(new=())
        self.assertEqual(self.run_update(dk), [])
        self.assertEqual(dk.removed, [])
        self.assertEqual(self.events[-1]["detail"], "")

    def test_disk_apps_wait_for_the_disk(self):
        dk = FakeDk(disk="missing", new=("radarr", "seerr"))
        self.assertEqual(self.run_update(dk), ["seerr"])  # the guard brings Radarr up on its new image later
        self.assertFalse(set(dk.calls[1]) & set(DISK_APPS))

    def test_failed_pull_changes_nothing(self):
        dk = FakeDk(fail_on="pull")
        self.assertIsNone(self.run_update(dk))
        self.assertEqual(len(dk.calls), 1)
        self.assertEqual((self.events[-1]["step"], self.events[-1]["error"]), ("step_pull", "err_update_failed"))
        self.assertIn("network unreachable", self.events[-1]["detail"])
        self.assertIsNone(update.last(self.dir))

    def test_worker_runs_an_update_request(self):
        from installer import reconfigure
        flow.write_json(os.path.join(self.dir, flow.APPLY_REQUEST), {"kind": "update"})
        dk = FakeDk()
        dk.connect_network = lambda cid: None
        dk.self_container_id = lambda: "me"
        self.assertEqual(reconfigure.run_worker(dk=dk, state_dir=self.dir), 0)
        with open(os.path.join(self.dir, flow.APPLY_FILE)) as f:
            self.assertEqual(json.load(f)[-1]["step"], "step_done")


if __name__ == "__main__":
    unittest.main()
