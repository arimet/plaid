import json
import os
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime

from installer import backup, flow
from installer.compose import DISK_APPS
from installer.docker import DockerError
from tests.test_flow import settings

SERVICES = ["jellyfin", "seerr", "radarr", "sonarr", "qbittorrent", "prowlarr", "bazarr"]
NIGHT = datetime(2026, 11, 8, 3, 0)


class FakeDk:
    PROJECT = "plaid"

    def __init__(self, disk="ok", fail_on=None, backups=()):
        self.disk, self.fail_on, self.backups = disk, fail_on, list(backups)
        self.calls = []

    def host_path(self, path, platform):
        return path

    def marker_state(self, src):
        return self.disk

    def compose(self, state_dir, *args, timeout=None):
        self.calls.append(("compose",) + args)

    def _maybe_fail(self, what):
        if self.fail_on == what:
            raise DockerError("No space left on device")

    def backup_archive(self, volumes, dest, sub, name):
        self.calls.append(("archive", tuple(volumes), dest, sub, name))
        self._maybe_fail("archive")
        return 4242

    def restore_archive(self, volumes, dest, sub, name):
        self.calls.append(("restore", tuple(volumes), dest, sub, name))
        self._maybe_fail("restore")

    def backup_folder_state(self, dest):
        return "ok"

    def list_backups(self, dest, sub):
        self.calls.append(("list", dest, sub))
        return self.backups


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "compose.json"), "w") as f:
            json.dump({"services": {s: {} for s in SERVICES + ["gluetun", "tailscale"]},
                       "volumes": {**{f"{s}-config": {} for s in SERVICES}, "tailscale-state": {}}}, f)
        flow.write_json(os.path.join(self.dir, "settings.json"), json.loads(settings().to_json()))
        self.events = []

    def report(self, p):
        self.events.append(asdict(p))

    def test_backup_stops_copies_and_starts_again(self):
        dk = FakeDk()
        self.assertEqual(backup.run(self.report, dk=dk, state_dir=self.dir, now=lambda: NIGHT),
                         "plaid-backup-2026-11-08-0300.tar.gz")
        kinds = [c[0] if c[0] != "compose" else c[1] for c in dk.calls]
        self.assertEqual(kinds, ["stop", "archive", "up"])
        self.assertEqual(set(dk.calls[0][2:]), set(SERVICES))  # not gluetun or Tailscale: no database, no cut
        archive = dk.calls[1]
        self.assertEqual((archive[2], archive[3]), ("/srv/arr", "backups"))  # default: <media folder>/backups
        self.assertIn("plaid_radarr-config", archive[1])
        self.assertNotIn("plaid_tailscale-state", archive[1])  # the machine's own Tailscale identity stays as it is
        self.assertEqual([e["step"] for e in self.events],
                         ["step_backup_stop", "step_backup_copy", "step_backup_start", "step_done"])
        st = backup.status(self.dir)
        self.assertEqual((st["name"], st["size"]), ("plaid-backup-2026-11-08-0300.tar.gz", 4242))

    def test_apps_start_again_when_the_copy_fails(self):
        dk = FakeDk(fail_on="archive")
        self.assertIsNone(backup.run(self.report, dk=dk, state_dir=self.dir, now=lambda: NIGHT))
        self.assertEqual(dk.calls[-1][:2], ("compose", "up"))
        self.assertEqual((self.events[-1]["error"], self.events[-1]["step"]), ("err_backup_failed", "step_backup_copy"))
        self.assertIn("No space left", backup.status(self.dir)["error"])

    def test_no_backup_onto_an_absent_disk(self):
        dk = FakeDk(disk="missing")
        self.assertIsNone(backup.run(self.report, dk=dk, state_dir=self.dir, now=lambda: NIGHT))
        self.assertEqual(dk.calls, [])  # nothing stopped, nothing written on the empty mount point
        self.assertEqual(self.events[-1]["error"], "err_backup_disk")

    def test_another_folder_is_used_as_it_is(self):
        flow.write_json(os.path.join(self.dir, "settings.json"),
                        json.loads(settings(backup_dir="/mnt/other").to_json()))
        dk = FakeDk(disk="missing")  # the media disk away does not matter to another disk
        backup.run(self.report, dk=dk, state_dir=self.dir, now=lambda: NIGHT)
        archive = next(c for c in dk.calls if c[0] == "archive")
        self.assertEqual((archive[2], archive[3]), ("/mnt/other", ""))
        up = dk.calls[-1]
        self.assertFalse(set(up) & set(DISK_APPS))  # disk apps wait for the guard

    def test_restore(self):
        dk = FakeDk()
        name = "plaid-backup-2026-11-08-0300.tar.gz"
        self.assertTrue(backup.restore(name, self.report, dk=dk, state_dir=self.dir))
        kinds = [c[0] if c[0] != "compose" else c[1] for c in dk.calls]
        self.assertEqual(kinds, ["stop", "restore", "up"])
        self.assertEqual(dk.calls[1][4], name)
        self.assertEqual([e["step"] for e in self.events],
                         ["step_restore_stop", "step_restore_copy", "step_restore_start", "step_done"])

    def test_restore_keeps_where_things_are_now(self):
        # The archive's settings come from that day: the folder, the backups, the VPN and Tailscale are today's.
        dk = FakeDk()
        now = settings(backup_dir="/mnt/backups", tailscale_key="tskey-now")
        flow.write_json(os.path.join(self.dir, "settings.json"), json.loads(now.to_json()))
        def restore_archive(volumes, dest, sub, name):
            restored = json.loads(settings(data_dir="/old/disk", username="before",
                                           vpn_fields={"WIREGUARD_PRIVATE_KEY": "old"}).to_json())
            restored["a_field_from_a_newer_plaid"] = 1
            flow.write_json(os.path.join(self.dir, "settings.json"), restored)
        dk.restore_archive = restore_archive
        self.assertTrue(backup.restore("plaid-backup-2026-11-08-0300.tar.gz", self.report, dk=dk, state_dir=self.dir))
        s = flow.load_settings(self.dir)
        self.assertEqual((s.data_dir, s.backup_dir, s.tailscale_key, s.vpn_fields, s.username),
                         ("/srv/arr", "/mnt/backups", "tskey-now", now.vpn_fields, "before"))
        with open(os.path.join(self.dir, "compose.json")) as f:
            self.assertIn('"source": "/srv/arr"', f.read())

    def test_failed_restore_starts_the_apps_again(self):
        dk = FakeDk(fail_on="restore")
        self.assertFalse(backup.restore("plaid-backup-2026-11-08-0300.tar.gz", self.report, dk=dk, state_dir=self.dir))
        self.assertEqual(dk.calls[-1][:2], ("compose", "up"))
        self.assertEqual(self.events[-1]["error"], "err_restore_failed")

    def test_worker_runs_backup_and_restore_requests(self):
        from installer import reconfigure
        for request in ({"kind": "backup"}, {"kind": "restore", "name": "plaid-backup-2026-11-08-0300.tar.gz"}):
            flow.write_json(os.path.join(self.dir, flow.APPLY_REQUEST), request)
            dk = FakeDk()
            dk.connect_network = lambda cid: None
            dk.self_container_id = lambda: "me"
            self.assertEqual(reconfigure.run_worker(dk=dk, state_dir=self.dir), 0)
            self.assertIn(request["kind"], [c[0].replace("archive", "backup") for c in dk.calls])


class LocalTimeTest(unittest.TestCase):
    def test_a_backup_is_named_in_the_stack_time_zone(self):
        from datetime import timezone
        paris = backup.local_now(settings(timezone="Europe/Paris"), utc=datetime(2026, 11, 8, 2, 30, tzinfo=timezone.utc))
        self.assertEqual((paris.hour, paris.minute), (3, 30))
        bad = backup.local_now(settings(timezone="Mars/Olympus"), utc=datetime(2026, 11, 8, 2, 30, tzinfo=timezone.utc))
        self.assertEqual(bad.hour, 2)  # an unknown zone: UTC rather than no name at all


if __name__ == "__main__":
    unittest.main()


class CustomFolderGoneTest(BackupTest):
    def test_a_custom_folder_that_lost_its_mark_is_not_written(self):
        # A Linux mount point whose disk is away is an empty folder: backing up there fills the system disk.
        flow.write_json(os.path.join(self.dir, "settings.json"),
                        json.loads(settings(backup_dir="/mnt/usb").to_json()))
        backup.note(self.dir, at=1.0, dir="/mnt/usb")
        dk = FakeDk()
        dk.backup_folder_state = lambda dest: "empty"
        self.assertIsNone(backup.run(self.report, dk=dk, state_dir=self.dir, now=lambda: NIGHT))
        self.assertEqual(self.events[-1]["error"], "err_backup_folder")
        self.assertFalse([c for c in dk.calls if c[0] == "archive"])

    def test_a_new_custom_folder_is_fine(self):
        flow.write_json(os.path.join(self.dir, "settings.json"),
                        json.loads(settings(backup_dir="/mnt/usb").to_json()))
        dk = FakeDk()
        dk.backup_folder_state = lambda dest: "empty"
        self.assertTrue(backup.run(self.report, dk=dk, state_dir=self.dir, now=lambda: NIGHT))
        self.assertEqual(backup.status(self.dir)["dir"], "/mnt/usb")
