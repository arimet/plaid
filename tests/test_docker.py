import json
import subprocess
import unittest

from installer import docker


class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls = []
        self.result = (returncode, stdout, stderr)

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        self.timeouts = getattr(self, "timeouts", []) + [timeout]
        code, out, err = self.result
        return subprocess.CompletedProcess(args, code, out, err)


class DockerTest(unittest.TestCase):
    def test_wipe_refuses_dangerous(self):
        run = FakeRun()
        with self.assertRaises(ValueError):
            docker.wipe_dir("/", "/root", "linux", run=run)
        with self.assertRaises(ValueError):
            docker.wipe_dir("C:\\Users\\Zoé", "C:\\Users\\Zoé", "windows", run=run)
        self.assertEqual(run.calls, [])

    def test_wipe_mounts_parent(self):
        run = FakeRun()
        docker.wipe_dir("/srv/my arr", "/home/z", "linux", run=run)
        args = run.calls[0]
        self.assertIn("/srv:/p", args)
        self.assertEqual(args[-3:], ["rm", "-rf", "/p/my arr"])

    def test_host_path(self):
        self.assertEqual(docker.host_path("C:\\Users\\Zoé\\Mes vidéos", "windows"),
                         "/run/desktop/mnt/host/c/Users/Zoé/Mes vidéos")
        self.assertEqual(docker.host_path("/Users/z/Arr", "mac"), "/Users/z/Arr")

    def test_compose_args(self):
        run = FakeRun()
        docker.compose("/state", "up", "-d", run=run)
        self.assertEqual(run.calls[0], ["docker", "compose", "-p", "plaid", "-f", "/state/compose.json", "up", "-d"])

    def test_error_carries_stderr(self):
        with self.assertRaises(docker.DockerError) as ctx:
            docker.compose("/state", "up", run=FakeRun(1, "", "port is already allocated"))
        self.assertIn("port is already allocated", str(ctx.exception))

    def test_dir_state(self):
        self.assertEqual(docker.dir_state("/srv/a", run=FakeRun(0, "3 2048\n")), (False, 2048 * 1024))
        self.assertEqual(docker.dir_state("/srv/a", run=FakeRun(0, "0 4\n")), (True, 4096))

    def test_dir_state_missing_folder_is_empty(self):
        run = FakeRun(125, "", "invalid mount config: bind source path does not exist: /srv/a")
        self.assertEqual(docker.dir_state("/srv/a", run=run), (True, 0))

    def test_stack_exists_ignores_installer(self):
        run = FakeRun(0, "abc\n")
        self.assertTrue(docker.stack_exists(run=run))
        self.assertIn("label=com.docker.compose.project=plaid", run.calls[0])
        self.assertFalse(docker.stack_exists(run=FakeRun(0, "")))

    def test_port_busy(self):
        run = FakeRun(0, "other\n")
        self.assertTrue(docker.port_busy(8096, run=run))
        self.assertIn("publish=8096", run.calls[0])


if __name__ == "__main__":
    unittest.main()


class UninstallHelpersTest(unittest.TestCase):
    def test_run_detached(self):
        run = FakeRun()
        docker.run_detached("img", ["--uninstall-worker"], {"ARR_STATE": "abc"}, "worker", run=run)
        args = run.calls[0]
        # no --rm: a failed worker must stay, so the panel can show its logs
        self.assertEqual(args[:5], ["docker", "run", "-d", "--name", "worker"])
        self.assertIn("ARR_STATE=abc", args)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", args)
        self.assertEqual(args[-2:], ["img", "--uninstall-worker"])

    def test_remove_ignores_missing(self):
        run = FakeRun(1, "", "No such container")
        docker.remove("container", "plaid", run=run)
        self.assertEqual(run.calls[0], ["docker", "rm", "-f", "plaid"])

    def test_remove_network(self):
        run = FakeRun()
        docker.remove("network", "plaid_default", run=run)
        self.assertEqual(run.calls[0], ["docker", "network", "rm", "plaid_default"])

    def test_remove_self_image_waits_for_containers(self):
        run = FakeRun(0, "ghcr.io/x/plaid:latest\n")
        docker.remove("image", "self", run=run)
        args = run.calls[-1]
        self.assertIn(docker.HELPER, args)  # the reaper is the small helper image, not the installer
        script = args[-1]
        self.assertIn("containers/json", script)
        self.assertIn("ancestor", script)
        self.assertIn("-X DELETE http://d/images/ghcr.io/x/plaid:latest", script)
        self.assertIn("-X DELETE 'http://d/containers/plaid-uninstall?force=true'", script)
        self.assertIn("seq 150", script)  # bounded: offline, curl is missing and the wait would never end


class TimeoutTest(unittest.TestCase):
    def test_helper_run_times_out(self):
        import sys
        result = docker.default_run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stderr)


class HostTailscaleTest(unittest.TestCase):
    def test_detects_tailscale_interface_on_host_network(self):
        run = FakeRun(0)
        self.assertTrue(docker.host_has_tailscale(run=run))
        self.assertIn("--network", run.calls[0])
        self.assertIn("/sys/class/net/tailscale0", run.calls[0])
        self.assertFalse(docker.host_has_tailscale(run=FakeRun(1)))


class HelperTimeoutTest(unittest.TestCase):
    def test_probe_is_short_and_cleaned_up(self):
        # Docker Desktop can hang on a bind of a just-deleted folder: give up fast, remove the stuck probe.
        run = FakeRun(124, "", "timed out")
        with self.assertRaises(docker.DockerError):
            docker.dir_state("/srv/a", run=run)
        self.assertEqual(run.timeouts[0], docker.PROBE_TIMEOUT)
        name = run.calls[0][run.calls[0].index("--name") + 1]
        self.assertEqual(run.calls[-1], ["docker", "rm", "-f", name])

    def test_wipe_has_no_timeout(self):
        # deleting hundreds of GB can take minutes
        run = FakeRun()
        docker.wipe_dir("/srv/my arr", "/home/z", "linux", run=run)
        self.assertEqual(run.timeouts, [None])


class GuardProbeTest(unittest.TestCase):
    def test_marker_state(self):
        for out, code, err, want in (("ok\n", 0, "", "ok"), ("unconfirmed\n", 0, "", "unconfirmed"),
                                     ("missing\n", 0, "", "missing"),
                                     ("", 125, "bind source path does not exist: /media/usb", "missing"),
                                     ("", 125, "daemon hiccup", "unknown")):
            run = FakeRun(code, out, err)
            self.assertEqual(docker.marker_state("/srv/arr", run=run), want)
        self.assertIn("type=bind,src=/srv/arr,dst=/m,readonly", run.calls[0])

    def test_marker_state_timeout_is_unknown_and_cleans_up(self):
        run = FakeRun(124, "", "timed out")
        self.assertEqual(docker.marker_state("/srv/arr", run=run), "unknown")
        self.assertEqual(run.calls[1][:3], ["docker", "rm", "-f"])

    def test_write_marker(self):
        run = FakeRun()
        docker.write_marker("/srv/arr", run=run)
        self.assertIn("/srv/arr:/m", run.calls[0])
        self.assertEqual(run.calls[0][-2:], ["touch", "/m/.plaid"])

    def test_ensure_data_dirs_writes_marker(self):
        run = FakeRun()
        docker.ensure_data_dirs("/srv/arr", 1000, 1000, run=run)
        self.assertIn("touch /m/.plaid", run.calls[0][-1])

    def test_stack_containers(self):
        inspect = json.dumps([
            {"Id": "g1", "State": {"Running": True, "StartedAt": "2026-11-06T10:00:00.1Z",
                                   "Health": {"Status": "healthy"}},
             "HostConfig": {"NetworkMode": "plaid_default"},
             "Config": {"Labels": {"com.docker.compose.service": "gluetun"}}},
            {"Id": "q1", "State": {"Running": False, "StartedAt": "2026-11-06T09:00:00Z"},
             "HostConfig": {"NetworkMode": "container:g0"},
             "Config": {"Labels": {"com.docker.compose.service": "qbittorrent"}}}])
        run = FakeRun(0, inspect)  # the ps call gets this stdout too: its words become ids, harmless here
        got = docker.stack_containers(run=run)
        self.assertEqual(got["gluetun"], {"id": "g1", "running": True, "started": "2026-11-06T10:00:00.1Z",
                                          "health": "healthy", "network": "plaid_default"})
        self.assertEqual(got["qbittorrent"]["health"], "")
        self.assertEqual(got["qbittorrent"]["network"], "container:g0")
        self.assertIn("label=com.docker.compose.project=plaid", run.calls[0])

    def test_stack_containers_empty(self):
        self.assertEqual(docker.stack_containers(run=FakeRun(0, "")), {})

    def test_sees_marker(self):
        run = FakeRun(0)
        self.assertTrue(docker.sees_marker("/state", "radarr", run=run))
        self.assertEqual(run.calls[0][-5:], ["-T", "radarr", "test", "-e", "/data/.plaid"])
        self.assertFalse(docker.sees_marker("/state", "radarr", run=FakeRun(1)))
