import base64
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from installer import flow
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

    def ensure_data_dirs(self, src, puid, pgid):
        self.calls.append(("ensure", src))

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

    def run_detached(self, image, args, env):
        self.calls.append(("detached", image, tuple(args), tuple(sorted(env))))

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
        self.assertEqual(flow.load_settings(self.state), settings())
        self.assertTrue(flow.is_complete(self.state))
        for name in ("compose.json", "settings.json"):
            self.assertTrue(os.path.exists(os.path.join(self.state, name)), name)

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
        kind, image, args, env = dk.calls[-1]
        self.assertEqual((kind, image, args), ("detached", "img", ("--uninstall-worker",)))
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
                        dk.calls.index(("rm", "network", "arrstack_default")))

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
