import subprocess
import unittest

from installer import docker


class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls = []
        self.result = (returncode, stdout, stderr)

    def __call__(self, args):
        self.calls.append(args)
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
        self.assertEqual(run.calls[0], ["docker", "compose", "-p", "arrstack", "-f", "/state/compose.json",
                                        "--env-file", "/state/.env", "up", "-d"])

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
        self.assertIn("label=com.docker.compose.project=arrstack", run.calls[0])
        self.assertFalse(docker.stack_exists(run=FakeRun(0, "")))

    def test_port_busy(self):
        run = FakeRun(0, "other\n")
        self.assertTrue(docker.port_busy(8096, run=run))
        self.assertIn("publish=8096", run.calls[0])


if __name__ == "__main__":
    unittest.main()
