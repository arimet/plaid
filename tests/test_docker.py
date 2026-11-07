import json
import os
import subprocess
import tempfile
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
        self.assertIn("type=bind,src=/srv/arr,dst=/m", run.calls[0])  # --mount: a missing folder is an error, not created
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


class ComposeTimeoutTest(unittest.TestCase):
    def test_compose_passes_its_timeout(self):
        run = FakeRun()
        docker.compose("/state", "up", "-d", run=run, timeout=5)
        self.assertEqual(run.timeouts, [5])


class MinorFixesDockerTest(unittest.TestCase):
    def test_only_a_clean_absent_answer_is_blind(self):
        self.assertFalse(docker.sees_marker("/state", "radarr", run=FakeRun(1, "", "")))
        # Not an answer about the file: the app is restarting, or a slow NAS made the call time out.
        self.assertTrue(docker.sees_marker("/state", "radarr", run=FakeRun(1, "", 'service "radarr" is not running')))
        self.assertTrue(docker.sees_marker("/state", "radarr", run=FakeRun(124, "", "timed out")))

    def test_stack_containers_skips_one_offs_and_keeps_the_running_one(self):
        def c(cid, running, oneoff="False"):
            return {"Id": cid, "State": {"Running": running, "StartedAt": "2026-11-06T10:00:00Z"},
                    "HostConfig": {"NetworkMode": "plaid_default"},
                    "Config": {"Labels": {"com.docker.compose.service": "gluetun", "com.docker.compose.oneoff": oneoff}}}
        for order in ([c("real", True), c("old", False), c("run", True, "True")],
                      [c("run", True, "True"), c("old", False), c("real", True)]):
            got = docker.stack_containers(run=FakeRun(0, json.dumps(order)))
            self.assertEqual(got["gluetun"]["id"], "real")


class SettingsProbeTest(unittest.TestCase):
    def test_folder_state(self):
        for out, code, err, want in (("library\n", 0, "", "library"), ("empty\n", 0, "", "empty"),
                                     ("other\n", 0, "", "other"),
                                     ("", 125, "bind source path does not exist: /media/usb", "missing"),
                                     ("", 125, "boom", "unknown")):
            run = FakeRun(code, out, err)
            self.assertEqual(docker.folder_state("/srv/new", run=run), want)
        self.assertIn("type=bind,src=/srv/new,dst=/m,readonly", run.calls[0])

    def test_folder_state_timeout(self):
        run = FakeRun(124, "", "timed out")
        self.assertEqual(docker.folder_state("/srv/new", run=run), "unknown")
        self.assertEqual(run.calls[1][:3], ["docker", "rm", "-f"])

    def test_ensure_data_dirs_never_creates_the_folder(self):
        run = FakeRun()
        docker.ensure_data_dirs("/srv/arr", 1000, 1000, run=run, create=False)
        self.assertIn("type=bind,src=/srv/arr,dst=/m", run.calls[0])
        self.assertNotIn("-v", run.calls[0])
        docker.ensure_data_dirs("/srv/arr", 1000, 1000, run=run)  # the install makes the folder it was given
        self.assertIn("/srv/arr:/m", run.calls[1])

    def test_run_detached_volumes(self):
        run = FakeRun()
        docker.run_detached("img", ["--apply-worker"], {}, docker.APPLY_WORKER, run=run, volumes=("plaid-state:/state",))
        self.assertIn("plaid-state:/state", run.calls[0])
        self.assertIn("plaid-apply", run.calls[0])


class ScriptRun:
    """One result per call, in order: (returncode, stdout, stderr)."""
    def __init__(self, *results):
        self.calls, self.results = [], list(results)

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        code, out, err = self.results.pop(0)
        return subprocess.CompletedProcess(args, code, out, err)


LINUX_DF = """Filesystem           Type       1024-blocks    Used Available Capacity Mounted on
overlay              overlay    474095688  38086160 411853320   8% /
overlay              overlay       812676       432    812244   0% /host
/dev/sda2            ext4       240000000 100000000 140000000  42% /host
/dev/sda1            vfat          523248      6220    517028   1% /host/boot/efi
tmpfs                tmpfs         812676       796    811880   0% /host/run
/dev/sdb1            exfat     1953000000 953000000 1000000000  49% /host/media/zoe/My Films
/dev/sdc1            ext4      3906000000 906000000 3000000000  23% /host/mnt/films
/dev/sdc1            ext4      3906000000 906000000 3000000000  23% /host/srv/films-bind
/dev/sda2            ext4       240000000 100000000 140000000  42% /host/var/lib/docker
//nas/share          cifs      7812000000 812000000 7000000000  10% /host/mnt/nas
"""


class DisksTest(unittest.TestCase):
    def test_parse_df_keeps_real_disks(self):
        got = docker.parse_df(LINUX_DF)
        self.assertEqual([d["path"] for d in got], ["/", "/media/zoe/My Films", "/mnt/films", "/mnt/nas"])
        root, films = got[0], got[1]
        self.assertTrue(root["system"])
        self.assertEqual((films["name"], films["free"], films["total"], films["system"]),
                         ("My Films", 1000000000 * 1024, 1953000000 * 1024, False))

    def test_linux_disks_read_the_host_mounts(self):
        run = ScriptRun((0, LINUX_DF, ""))
        self.assertEqual(len(docker.disks("linux", "/home/zoe", run=run)), 4)
        self.assertIn("/:/host:ro", run.calls[0])

    def test_mac_disks_never_list_the_home(self):
        out = ("\t/run/host_mark/Users 482797652 374210316 108587336  78% /u\n"
               "Films\t/run/host_mark/Volumes 1953000000 953000000 1000000000  49% /v/Films\n")
        run = ScriptRun((0, out, ""))
        got = docker.disks("mac", "/Users/zoe", run=run)
        self.assertEqual([(d["path"], d["name"], d["system"]) for d in got],
                         [("/Users/zoe", "", True), ("/Volumes/Films", "Films", False)])
        self.assertEqual(got[0]["free"], 108587336 * 1024)
        # Docker Desktop's /Volumes share reports the Mac's own disk: an external disk's room is unknown.
        self.assertEqual((got[1]["free"], got[1]["total"]), (None, None))
        self.assertNotIn("/Users/zoe", " ".join(run.calls[0]))  # only the /Users share, for its free space

    def test_windows_disks_are_drive_letters(self):
        out = "c\tC: 1000 500 500 50% /h/c\nd\tD: 4000 1000 3000 25% /h/d\nwsl\tx 1 1 0 99% /h/wsl\n"
        got = docker.disks("windows", "C:\\Users\\Zoé", run=ScriptRun((0, out, "")))
        self.assertEqual([(d["path"], d["system"]) for d in got], [("C:\\Users\\Zoé", True), ("D:\\", False)])

    def test_disks_probe_failure_is_empty(self):
        self.assertEqual(docker.disks("linux", "/home/zoe", run=ScriptRun((125, "", "boom"))), [])


class BrowseTest(unittest.TestCase):
    def test_browsable(self):
        for path, platform in (("/Volumes/Films", "mac"), ("/Volumes/Films/Movies", "mac"), ("D:\\", "windows"),
                               ("D:\\Films", "windows"), ("/mnt/films", "linux"), ("/", "linux"),
                               ("/volume1", "synology")):
            self.assertTrue(docker.browsable(path, platform), path)
        for path, platform in (("/Users/zoe", "mac"), ("/Volumes", "mac"), ("/Volumes/../Users", "mac"),
                               ("C:\\Users", "windows"), ("/proc/1", "linux"), ("/sys", "linux"),
                               ("relative", "linux"), ("/mnt/a,b", "linux"), ("//proc", "linux"),
                               ("//sys/x", "linux"), ("/var/run/docker", "linux")):
            self.assertFalse(docker.browsable(path, platform), path)

    def test_browse_lists_folders_and_space(self):
        run = ScriptRun((0, "/dev/sdc1 3906000000 906000000 3000000000 23% /m\nMovies\0TV shows\0x,y\0", ""))
        got = docker.browse("/mnt/films", "linux", run=run)
        self.assertEqual(got, {"path": "/mnt/films", "parent": "/mnt", "dirs": ["Movies", "TV shows"],
                               "free": 3000000000 * 1024, "total": 3906000000 * 1024})
        self.assertIn("type=bind,src=/mnt/films,dst=/m,readonly", run.calls[0])

    def test_browse_mac_reads_through_volumes(self):
        run = ScriptRun((0, "v 100 50 50 50% /v\nSeries\0", ""))
        got = docker.browse("/Volumes/Films/Movies", "mac", run=run)
        self.assertEqual((got["parent"], got["dirs"]), ("/Volumes/Films", ["Series"]))
        self.assertIsNone(got["free"])
        self.assertIn("type=bind,src=/Volumes,dst=/v,readonly", run.calls[0])
        self.assertEqual(run.calls[0][-1], "Films/Movies")  # path inside /v, given as an argument
        self.assertIsNone(docker.browse("/Volumes/Films", "mac", run=ScriptRun((0, "v 1 1 0 9% /v\n", "")))["parent"])

    def test_browse_errors(self):
        self.assertEqual(docker.browse("/Users/zoe", "mac", run=ScriptRun()), {"error": "err_folder_unreadable"})
        missing = ScriptRun((125, "", "bind source path does not exist: /mnt/x"))
        self.assertEqual(docker.browse("/mnt/x", "linux", run=missing), {"error": "err_folder_missing"})
        self.assertEqual(docker.browse("/mnt/x", "linux", run=ScriptRun((3, "", "boom"))),
                         {"error": "err_folder_unreadable"})


class DiskCheckTest(unittest.TestCase):
    def test_existing_folder(self):
        run = ScriptRun((0, "/dev/sdc1 3906000000 906000000 3000000000 23% /m\n2\n", ""))
        self.assertEqual(docker.disk_check("/mnt/films/Plaid", "linux", "/home/zoe", run=run),
                         {"free": 3000000000 * 1024, "hardlinks": True})
        self.assertIn("type=bind,src=/mnt/films/Plaid,dst=/m", run.calls[0])
        self.assertNotIn("readonly", " ".join(run.calls[0]))

    def test_no_hardlinks_and_read_only(self):
        no_link = ScriptRun((0, "d 10 5 5 50% /m\n1\n", ""))
        self.assertFalse(docker.disk_check("/media/usb", "linux", "/home/z", run=no_link)["hardlinks"])
        read_only = ScriptRun((0, "d 10 5 5 50% /m\n-\n", ""))
        self.assertIsNone(docker.disk_check("/media/usb", "linux", "/home/z", run=read_only)["hardlinks"])

    def test_new_folder_checks_its_parent(self):
        run = ScriptRun((125, "", "bind source path does not exist"), (0, "d 10 5 5 50% /m\n2\n", ""))
        self.assertEqual(docker.disk_check("/mnt/films/Plaid", "linux", "/home/z", run=run),
                         {"free": 5 * 1024, "hardlinks": True})
        self.assertIn("type=bind,src=/mnt/films,dst=/m", run.calls[1])

    def test_desktop_never_opens_the_home(self):
        # Docker Desktop hung on the Mac home folder: only the /Users share's free space, no link test there.
        run = ScriptRun((125, "", "bind source path does not exist"), (0, "u 10 5 5 50% /m\n", ""))
        self.assertEqual(docker.disk_check("/Users/zoe/Plaid", "mac", "/Users/zoe", run=run),
                         {"free": 5 * 1024, "hardlinks": None})
        self.assertIn("type=bind,src=/Users,dst=/m,readonly", run.calls[1])

    def test_mac_volume_room_is_unknown(self):
        run = ScriptRun((0, "v 10 5 5 50% /m\n1\n", ""))
        self.assertEqual(docker.disk_check("/Volumes/Films/Plaid", "mac", "/Users/zoe", run=run),
                         {"free": None, "hardlinks": False})

    def test_mac_home_by_another_name_is_never_opened(self):
        for path in ("/Volumes/Macintosh HD/Users/zoe/Plaid", "/System/Volumes/Data/Users/zoe/Plaid",
                     "/volumes/macintosh hd/Users/zoe/Plaid"):
            run = ScriptRun((125, "", "bind source path does not exist"), (0, "u 10 5 5 50% /m\n", ""))
            self.assertEqual(docker.disk_check(path, "mac", "/Users/zoe", run=run), {"free": 5 * 1024, "hardlinks": None})
            self.assertIn("type=bind,src=/Users,dst=/m,readonly", run.calls[1], path)

    def test_mac_volume_room_ignores_case(self):
        run = ScriptRun((0, "v 10 5 5 50% /m\n2\n", ""))
        self.assertIsNone(docker.disk_check("/volumes/Films/Plaid", "mac", "/Users/zoe", run=run)["free"])

    def test_unknown(self):
        self.assertEqual(docker.disk_check("/mnt/x", "linux", "/home/z", run=ScriptRun((3, "", "boom"))),
                         {"free": None, "hardlinks": None})
        gone = ScriptRun((125, "", "does not exist"), (125, "", "does not exist"))
        self.assertEqual(docker.disk_check("/mnt/x/y", "linux", "/home/z", run=gone), {"free": None, "hardlinks": None})



class LocalSh:
    """Runs a helper's `sh -c` script on this machine, its /h and /u folders moved under `root`, with a fake df that
    prints nothing for a folder named Silent (a volume that does not answer)."""
    def __init__(self, root):
        self.root, self.calls = root, []
        bin_dir = os.path.join(root, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "df"), "w") as f:
            f.write('#!/bin/sh\ncase "$2" in *Silent*) exit 1;; esac\n'
                    'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\necho "fs 100 40 60 40% $2"\n')
        os.chmod(os.path.join(bin_dir, "df"), 0o755)

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        script = args[args.index("-c") + 1].replace("/h/", self.root + "/h/").replace("/u ", self.root + "/u ")
        env = dict(os.environ, PATH=os.path.join(self.root, "bin") + ":" + os.environ["PATH"])
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env)


class MinorDisksTest(unittest.TestCase):
    def test_mergerfs_pool_and_escaped_mount_point(self):
        out = LINUX_DF + ("pool:media       fuse.mergerfs 9000000000 1000000000 8000000000  12% /host/mnt/pool\n"
                          "/dev/sdd1        ext4       100 50 50  50% /host/media/zoe/My\\040Disk\n")
        paths = [d["path"] for d in docker.parse_df(out)]
        self.assertIn("/mnt/pool", paths)
        self.assertIn("/media/zoe/My Disk", paths)

    def test_a_silent_volume_does_not_shift_the_names(self):
        with tempfile.TemporaryDirectory() as root:
            for d in ("u", "h/Alpha", "h/Silent", "h/Zulu"):
                os.makedirs(os.path.join(root, d))
            got = docker.disks("mac", "/Users/zoe", run=LocalSh(root))
        self.assertEqual([(d["name"], d["system"]) for d in got], [("", True), ("Alpha", False), ("Zulu", False)])

    def test_browse_names_with_a_newline(self):
        run = ScriptRun((0, "d 10 5 5 50% /m\nMovies\0TV\nshows\0", ""))
        self.assertEqual(docker.browse("/mnt/films", "linux", run=run)["dirs"], ["Movies", "TV\nshows"])

    def test_link_test_file_has_its_own_name_and_old_ones_go(self):
        run = ScriptRun((0, "d 10 5 5 50% /m\n2\n", ""), (0, "d 10 5 5 50% /m\n2\n", ""))
        docker.disk_check("/mnt/films", "linux", "/home/z", run=run)
        docker.disk_check("/mnt/films", "linux", "/home/z", run=run)
        names = [c[-1] for c in run.calls]
        self.assertRegex(names[0], r"^\.plaid-link-[0-9a-f]{8}$")
        self.assertNotEqual(names[0], names[1])
        self.assertIn("rm -f /m/.plaid-link-*", run.calls[0][run.calls[0].index("-c") + 1])
