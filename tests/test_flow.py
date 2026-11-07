import base64
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from installer import media, flow
from installer.docker import DockerError
from installer.settings import Settings


def settings(**kw):
    base = dict(data_dir="/srv/arr", username="admin", password="longenough", vpn_provider="protonvpn",
                vpn_fields={"WIREGUARD_PRIVATE_KEY": "k"}, timezone="UTC", lang="en", puid=1000, pgid=1000,
                home="/home/z", platform="linux")
    base.update(kw)
    return Settings(**base)


class FakeDocker:
    def __init__(self, **overrides):
        self.calls = []
        self.stack = False
        self.busy = set()
        self.dir = (True, 0)
        self.devices = {"/dev/net/tun", "/dev/dri"}
        self.up_error = None
        self.health = "healthy"
        for k, v in overrides.items():
            setattr(self, k, v)

    def host_path(self, path, platform):
        return path

    def stack_exists(self):
        return self.stack

    def port_busy(self, port):
        return port in self.busy

    def dir_state(self, src):
        return self.dir

    def has_device(self, path):
        return path in self.devices

    def host_has_tailscale(self):
        return False

    APPLY_WORKER = "plaid-apply"

    def ensure_data_dirs(self, src, puid, pgid, create=True):
        self.calls.append(("ensure", src) if create else ("ensure-existing", src))

    def folder_state(self, src):
        return getattr(self, "folder", "library")

    def write_marker(self, src):
        self.calls.append(("marker", src))

    def marker_state(self, src):
        return getattr(self, "disk", "unconfirmed")

    def compose(self, state_dir, *args):
        self.calls.append(("compose",) + args)
        if args[:1] == ("up",) and self.up_error:
            raise DockerError(self.up_error)
        if args[:1] == ("ps",):
            return SimpleNamespace(stdout=json.dumps({"Service": "gluetun", "Health": self.health}))
        return SimpleNamespace(stdout="")

    def self_container_id(self):
        return "me"

    def connect_network(self, container):
        self.calls.append(("connect", container))

    def wipe_dir(self, path, home, platform):
        self.calls.append(("wipe", path))

    def run_detached(self, image, args, env, name, volumes=()):
        self.calls.append(("detached", image, tuple(args), tuple(sorted(env)), name) + tuple(volumes))

    def container_state(self, name):
        return getattr(self, "states", {}).get(name, getattr(self, "state", (False, False, 0)))

    def logs(self, name):
        return getattr(self, "log_text", "")

    def own_image(self):
        return "img"

    def remove(self, kind, name):
        self.calls.append(("rm", kind, name))


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.events = []

    def install(self, s, dk, configure=None):
        configure = configure or (lambda *a, **k: {})
        return flow.install(s, self.events.append, dk=dk, configure=configure, state_dir=self.state, vpn_wait=0,
                            arch="x86_64")

    def test_preflight_existing_stack(self):
        self.assertIn("err_stack_exists", flow.preflight(settings(), dk=FakeDocker(stack=True)))

    def test_preflight_dangerous(self):
        self.assertEqual(flow.preflight(settings(data_dir="/"), dk=FakeDocker()), ["err_dangerous_path"])

    def test_preflight_warnings(self):
        dk = FakeDocker(dir=(False, 10), busy={8096})
        found = flow.preflight(settings(vpn_provider=None, vpn_fields={}), dk=dk)
        self.assertEqual(found, ["err_port_busy:8096", "warn_dir_not_empty", "warn_no_vpn"])

    def test_preflight_no_tun(self):
        dk = FakeDocker(devices=set())
        self.assertEqual(flow.preflight(settings(platform="synology"), dk=dk), ["err_no_tun"])

    def test_install_writes_state_and_configures(self):
        seen = {}
        links = self.install(settings(), FakeDocker(),
                             configure=lambda s, host_of, log, compose_out, has_dri: seen.update(
                                 qbit=host_of("qbittorrent"), radarr=host_of("radarr"), dri=has_dri) or {})
        self.assertEqual(seen, {"qbit": "gluetun:8080", "radarr": "radarr:7878", "dri": True})
        self.assertEqual(links["jellyfin"], 8096)
        self.assertTrue(self.events[-1].done)
        self.assertEqual(flow.load_settings(self.state), media.effective(settings()))  # choices made explicit
        self.assertTrue(flow.is_complete(self.state))
        for name in ("compose.json", "settings.json"):
            self.assertTrue(os.path.exists(os.path.join(self.state, name)), name)

    def test_install_saves_media_choices(self):
        ids = {"radarr": {"formats": {"lang": 1}, "profiles": {"mine-1080p": 9}}}
        self.install(settings(), FakeDocker(), configure=lambda *a: {"arr_ids": ids})
        saved = flow.load_settings(self.state)
        self.assertEqual((saved.arr_ids, saved.profiles), (ids, ["mine-1080p"]))

    def test_install_vpn_timeout(self):
        self.assertIsNone(self.install(settings(), FakeDocker(health="unhealthy")))
        self.assertEqual(self.events[-1].error, "err_vpn_failed")
        self.assertFalse(flow.is_complete(self.state))
        self.assertIsNotNone(flow.load_settings(self.state))  # uninstall can still clean up

    def test_install_port_busy(self):
        dk = FakeDocker(up_error="Bind for 0.0.0.0:8096 failed: port is already allocated")
        self.assertIsNone(self.install(settings(), dk))
        self.assertEqual(self.events[-1].error, "err_port_busy:8096")

    def test_install_unexpected_error_keeps_detail(self):
        def boom(*a, **k):
            raise RuntimeError("qBittorrent refused the login")
        self.assertIsNone(self.install(settings(), FakeDocker(), configure=boom))
        self.assertEqual(self.events[-1].error, "err_unexpected")
        self.assertIn("qBittorrent", self.events[-1].detail)

    def test_uninstall_spawns_worker_with_state(self):
        self.install(settings(), FakeDocker())
        dk = FakeDocker()
        flow.uninstall(False, self.events.append, dk=dk, state_dir=self.state)
        kind, image, args, env, name = dk.calls[-1]
        self.assertEqual((kind, image, args, name), ("detached", "img", ("--uninstall-worker",), flow.WORKER_NAME))
        self.assertIn("ARR_STATE", env)

    def test_worker_keeps_media(self):
        self.install(settings(), FakeDocker())
        payload = flow.pack_state(self.state)
        dk = FakeDocker()
        flow.uninstall_worker(payload, remove_media=False, dk=dk, work_dir=tempfile.mkdtemp())
        self.assertIn(("compose", "down", "-v", "--rmi", "all", "--remove-orphans"), dk.calls)
        self.assertFalse([c for c in dk.calls if c[0] == "wipe"])
        self.assertIn(("rm", "container", flow.INSTALLER_NAME), dk.calls)
        self.assertIn(("rm", "volume", flow.STATE_VOLUME), dk.calls)
        # the installer was attached to the stack network, so compose could not remove it
        self.assertLess(dk.calls.index(("rm", "container", flow.INSTALLER_NAME)),
                        dk.calls.index(("rm", "network", "plaid_default")))

    def test_worker_removes_media_when_asked(self):
        self.install(settings(), FakeDocker())
        dk = FakeDocker()
        flow.uninstall_worker(flow.pack_state(self.state), remove_media=True, dk=dk, work_dir=tempfile.mkdtemp())
        self.assertIn(("wipe", "/srv/arr"), dk.calls)

    def test_worker_after_failed_install(self):
        self.install(settings(), FakeDocker(up_error="boom"))
        dk = FakeDocker()
        flow.uninstall_worker(flow.pack_state(self.state), remove_media=False, dk=dk, work_dir=tempfile.mkdtemp())
        self.assertIn(("compose", "down", "-v", "--rmi", "all", "--remove-orphans"), dk.calls)

    def test_pack_state_roundtrip(self):
        self.install(settings(), FakeDocker())
        files = json.loads(base64.b64decode(flow.pack_state(self.state)))
        self.assertEqual(set(files), {"compose.json", "settings.json"})


if __name__ == "__main__":
    unittest.main()


class ArchTest(unittest.TestCase):
    def test_no_hardware_transcoding_on_arm(self):
        # A Raspberry Pi exposes /dev/dri, but Jellyfin's VAAPI path only works on Intel/AMD.
        state, seen = tempfile.mkdtemp(), {}
        flow.install(settings(), lambda p: None, dk=FakeDocker(), state_dir=state, vpn_wait=0, arch="aarch64",
                     configure=lambda s, host_of, log, compose_out, has_dri: seen.update(dri=has_dri) or {})
        self.assertEqual(seen, {"dri": False})
        with open(os.path.join(state, "compose.json")) as f:
            self.assertNotIn("devices", json.load(f)["services"]["jellyfin"])


class TailscaleFlowTest(unittest.TestCase):
    def test_refused_when_host_already_runs_tailscale(self):
        dk = FakeDocker()
        dk.host_tailscale = True
        dk.host_has_tailscale = lambda: True
        found = flow.preflight(settings(tailscale_key="tskey-auth-x"), dk=dk)
        self.assertEqual(found, ["err_tailscale_exists"])

    def test_not_checked_without_key(self):
        dk = FakeDocker()
        dk.host_has_tailscale = lambda: True
        self.assertNotIn("err_tailscale_exists", flow.preflight(settings(), dk=dk))

    def test_worker_logs_out_before_removal(self):
        state = tempfile.mkdtemp()
        flow.install(settings(tailscale_key="tskey-auth-x"), lambda p: None, dk=FakeDocker(), state_dir=state,
                     vpn_wait=0, arch="x86_64", configure=lambda *a, **k: {})
        dk = FakeDocker()
        flow.uninstall_worker(flow.pack_state(state), remove_media=False, dk=dk, work_dir=tempfile.mkdtemp())
        logout = ("compose", "exec", "-T", "tailscale", "tailscale", "logout")
        self.assertLess(dk.calls.index(logout), dk.calls.index(("compose", "down", "-v", "--rmi", "all", "--remove-orphans")))


class ReviewFixesFlowTest(unittest.TestCase):
    def test_port_from_engine_bind_error(self):
        # Docker Engine on Linux, port held by a host program: the old regex reported "port 0".
        err = ("Error response from daemon: driver failed programming external connectivity on endpoint "
               "plaid-qbittorrent-1: failed to bind port 0.0.0.0:8080/tcp: Error starting userland proxy: "
               "listen tcp4 0.0.0.0:8080: bind: address already in use")
        events = []
        flow.install(settings(vpn_provider=None, vpn_fields={}), events.append, dk=FakeDocker(up_error=err),
                     state_dir=tempfile.mkdtemp(), vpn_wait=0, arch="x86_64", configure=lambda *a, **k: {})
        self.assertEqual(events[-1].error, "err_port_busy:8080")

    def test_uninstall_status(self):
        dk = FakeDocker()
        dk.state = (True, True, 0)
        self.assertEqual(flow.uninstall_status(dk=dk), ("running", ""))
        # the worker finished cleanly: the terminal can say so (the reaper deletes it once nothing runs the image)
        dk.state = (True, False, 0)
        self.assertEqual(flow.uninstall_status(dk=dk), ("done", ""))
        dk.state = (True, False, 1)
        dk.log_text = "compose down failed"
        self.assertEqual(flow.uninstall_status(dk=dk), ("failed", "compose down failed"))
        self.assertIn(("rm", "container", flow.WORKER_NAME), dk.calls)
        dk.state = (False, False, 0)
        self.assertEqual(flow.uninstall_status(dk=dk), ("idle", ""))


class ReattachTest(unittest.TestCase):
    def test_reconnects_to_the_stack_network_when_installed(self):
        # A recreated assistant (new image) starts outside the stack network: the trackers page needs Prowlarr.
        state = tempfile.mkdtemp()
        flow.install(settings(), lambda p: None, dk=FakeDocker(), state_dir=state, vpn_wait=0, arch="x86_64",
                     configure=lambda *a, **k: {})
        dk = FakeDocker()
        flow.reattach(dk=dk, state_dir=state)
        self.assertIn(("connect", "me"), dk.calls)

    def test_nothing_to_do_before_install(self):
        dk = FakeDocker()
        flow.reattach(dk=dk, state_dir=tempfile.mkdtemp())
        self.assertEqual(dk.calls, [])


class CancelTest(unittest.TestCase):
    # Ctrl-C during an install must leave the machine as it was.
    def setUp(self):
        import threading
        self.state, self.events, self.cancel = tempfile.mkdtemp(), [], threading.Event()

    def run_install(self, dk):
        return flow.install(settings(), self.events.append, dk=dk, state_dir=self.state, vpn_wait=0, arch="x86_64",
                            configure=lambda *a, **k: {}, cancel=self.cancel)

    def test_stops_at_the_next_step(self):
        dk = FakeDocker()
        dk.ensure_data_dirs = lambda *a: self.cancel.set()  # Ctrl-C while the folders are being made
        self.assertIsNone(self.run_install(dk))
        self.assertEqual(self.events[-1].error, "err_cancelled")
        self.assertFalse([c for c in dk.calls if c[0] == "compose"])

    def test_remembers_a_folder_it_created(self):
        self.run_install(FakeDocker(dir=(True, 0)))
        self.assertTrue(flow.created_folder(self.state))

    def test_keeps_a_folder_that_held_files(self):
        self.run_install(FakeDocker(dir=(False, 10)))
        self.assertFalse(flow.created_folder(self.state))

    def test_rollback_deletes_only_a_folder_it_created(self):
        self.run_install(FakeDocker(dir=(True, 0)))
        seen = {}
        dk = FakeDocker()
        flow.rollback(lambda p: None, dk=dk, state_dir=self.state)
        kind, image, args, env, name = dk.calls[-1]
        self.assertIn("--remove-media", args)
        other = tempfile.mkdtemp()
        flow.install(settings(), lambda p: None, dk=FakeDocker(dir=(False, 10)), state_dir=other, vpn_wait=0,
                     arch="x86_64", configure=lambda *a, **k: {})
        dk = FakeDocker()
        flow.rollback(lambda p: None, dk=dk, state_dir=other)
        self.assertNotIn("--remove-media", dk.calls[-1][2])


class GuardStatusTest(unittest.TestCase):
    def test_guard_status_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(flow.guard_status(d), {"disk": "unknown", "vpn": "off"})
            with open(os.path.join(d, "guard.json"), "w") as f:
                f.write("{broken")
            self.assertEqual(flow.guard_status(d), {"disk": "unknown", "vpn": "off"})

    def test_guard_status_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            flow.save_guard_status({"disk": "missing", "vpn": "down"}, d)
            self.assertEqual(flow.guard_status(d), {"disk": "missing", "vpn": "down"})

    def test_confirm_disk_writes_marker_and_status(self):
        dk = FakeDocker()
        with tempfile.TemporaryDirectory() as d:
            flow.save_guard_status({"disk": "unconfirmed", "vpn": "ok"}, d)
            self.assertTrue(flow.confirm_disk(settings(), dk=dk, state_dir=d))
            self.assertIn(("marker", "/srv/arr"), dk.calls)
            self.assertEqual(flow.guard_status(d), {"disk": "ok", "vpn": "ok"})


class ConfirmDiskSafetyTest(unittest.TestCase):
    def test_confirm_refuses_when_disk_is_gone(self):
        # The question was on screen, then the disk was unplugged: marking the empty mount point would
        # start the apps on the system disk.
        for disk in ("missing", "unknown", "ok"):
            dk = FakeDocker(disk=disk)
            with tempfile.TemporaryDirectory() as d:
                flow.save_guard_status({"disk": "unconfirmed", "vpn": "off"}, d)
                self.assertFalse(flow.confirm_disk(settings(), dk=dk, state_dir=d))
                self.assertNotIn(("marker", "/srv/arr"), dk.calls)
                self.assertEqual(flow.guard_status(d)["disk"], "unconfirmed")

    def test_confirm_reports_a_failed_write(self):
        dk = FakeDocker()
        def fail(src):
            raise DockerError("Read-only file system")
        dk.write_marker = fail
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(flow.confirm_disk(settings(), dk=dk, state_dir=d))


class StaleGuardStatusTest(unittest.TestCase):
    def test_old_status_is_not_shown(self):
        # The guard is stuck or its container is gone: an old "ok" must not stand for the present.
        import time
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "guard.json"), "w") as f:
                json.dump({"disk": "missing", "vpn": "down", "at": time.time() - 600}, f)
            self.assertEqual(flow.guard_status(d), {"disk": "unknown", "vpn": "off"})
            with open(os.path.join(d, "guard.json"), "w") as f:
                json.dump({"disk": "missing", "vpn": "down"}, f)  # no time: unknown age
            self.assertEqual(flow.guard_status(d), {"disk": "unknown", "vpn": "off"})


class RemovalTakesApplyWorkerTest(unittest.TestCase):
    def test_uninstall_worker_removes_a_leftover_apply_worker(self):
        dk = FakeDocker()
        payload = base64.b64encode(json.dumps({"settings.json": settings().to_json()}).encode()).decode()
        with tempfile.TemporaryDirectory() as d:
            flow.uninstall_worker(payload, False, dk=dk, work_dir=d)
        self.assertLess(dk.calls.index(("rm", "container", "plaid-apply")), dk.calls.index(("rm", "volume", "plaid-state")))
