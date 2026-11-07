import unittest

from installer import guard
from installer.compose import DISK_APPS
from installer.docker import DockerError
from installer.settings import Settings


def G(**kw):
    return dict(dict(id="g1", running=True, started="2026-11-06T10:00:00Z", health="healthy",
                     network="plaid_default"), **kw)


def Q(**kw):
    return dict(dict(id="q1", running=True, started="2026-11-06T10:00:05Z", health="",
                     network="container:g1"), **kw)


class DecideTest(unittest.TestCase):
    def test_disk_ok_starts_disk_apps(self):
        self.assertEqual(guard.decide("ok", False, {}, set()), ([("up",)], "off"))

    def test_blind_app_restarts(self):
        self.assertEqual(guard.decide("ok", False, {}, {"sonarr", "radarr"})[0],
                         [("up",), ("restart", "radarr"), ("restart", "sonarr")])

    def test_missing_disk_stops_running_apps(self):
        self.assertEqual(guard.decide("missing", False, {"radarr": Q()}, set())[0], [("stop",)])
        self.assertEqual(guard.decide("unconfirmed", False, {"radarr": Q()}, set())[0], [("stop",)])
        self.assertEqual(guard.decide("missing", False, {"radarr": Q(running=False)}, set())[0], [])
        self.assertEqual(guard.decide("missing", False, {"seerr": Q()}, set())[0], [])

    def test_unknown_disk_does_nothing(self):
        self.assertEqual(guard.decide("unknown", False, {"radarr": Q()}, set())[0], [])

    def test_recreated_gluetun_recreates_qbittorrent(self):
        acts, vpn = guard.decide("ok", True, {"gluetun": G(id="g2"), "qbittorrent": Q()}, set())
        self.assertEqual((acts[0], vpn), (("recreate_qbittorrent",), "ok"))

    def test_restarted_gluetun_recreates_qbittorrent(self):
        acts, _ = guard.decide("ok", True, {"gluetun": G(started="2026-11-06T11:00:00Z"), "qbittorrent": Q()}, set())
        self.assertEqual(acts[0], ("recreate_qbittorrent",))

    def test_started_compare_ignores_fraction(self):
        acts, _ = guard.decide("ok", True, {"gluetun": G(started="2026-11-06T10:00:00.1Z"),
                                            "qbittorrent": Q(started="2026-11-06T10:00:00.12Z")}, set())
        self.assertEqual(acts, [("up",)])

    def test_healthy_pair_is_left_alone(self):
        self.assertEqual(guard.decide("ok", True, {"gluetun": G(), "qbittorrent": Q()}, set()), ([("up",)], "ok"))

    def test_vpn_starting_is_not_down(self):
        acts, vpn = guard.decide("ok", True, {"gluetun": G(id="g2", health="starting"), "qbittorrent": Q()}, set())
        self.assertEqual((acts, vpn), ([("up",)], "ok"))

    def test_vpn_unhealthy_or_gone_is_down(self):
        self.assertEqual(guard.decide("ok", True, {"gluetun": G(health="unhealthy")}, set())[1], "down")
        self.assertEqual(guard.decide("ok", True, {"gluetun": G(running=False)}, set())[1], "down")
        self.assertEqual(guard.decide("ok", True, {}, set())[1], "down")

    def test_no_vpn_fix_without_disk(self):
        acts, _ = guard.decide("missing", True, {"gluetun": G(id="g2"), "qbittorrent": Q(running=False)}, set())
        self.assertNotIn(("recreate_qbittorrent",), acts)


class FakeDk:
    WORKER = "plaid-uninstall"
    APPLY_WORKER = "plaid-apply"
    applying = False

    def __init__(self, disk="ok", containers=None, blind=(), worker=False, fail_on=None):
        self.disk, self.containers, self.blind = disk, containers or {}, set(blind)
        self.worker, self.fail_on = worker, fail_on
        self.calls, self.probed = [], []
        self.worker_after = None

    def host_path(self, path, platform):
        return path

    def marker_state(self, src):
        return self.disk

    def stack_containers(self):
        return self.containers

    def sees_marker(self, state_dir, svc):
        self.probed.append(svc)
        return svc not in self.blind

    def container_state(self, name):
        if self.worker_after is not None:  # the removal starts while the round runs
            self.worker_after -= 1
            self.worker = self.worker_after < 0
        if name == self.APPLY_WORKER:
            stopped = getattr(self, "apply_stopped", False)
            return (self.applying or stopped, self.applying, 0)
        return (self.worker and name == self.WORKER, self.worker, 0)

    def compose(self, state_dir, *args, timeout=None):
        self.timeouts = getattr(self, "timeouts", []) + [timeout]
        self.calls.append(args)
        if self.fail_on and self.fail_on in args:
            raise DockerError("No such container: g1")


class FakeFlow:
    def __init__(self, complete=True, vpn=None):
        self.complete = complete
        self.settings = Settings(data_dir="/srv/arr", username="a", password="longenough", vpn_provider=vpn,
                                 home="/home/z", platform="linux")
        self.saved = []

    def is_complete(self, state_dir):
        return self.complete

    def load_settings(self, state_dir):
        return self.settings

    def save_guard_status(self, status, state_dir):
        self.saved.append(status)


class CycleTest(unittest.TestCase):
    def test_cycle_skips_while_busy(self):
        dk, fl = FakeDk(), FakeFlow(complete=False)
        self.assertIsNone(guard.cycle(dk, fl, "/state"))
        self.assertEqual((dk.calls, fl.saved), ([], []))

    def test_cycle_skips_during_removal(self):
        dk, fl = FakeDk(worker=True), FakeFlow()
        self.assertIsNone(guard.cycle(dk, fl, "/state"))
        self.assertEqual((dk.calls, fl.saved), ([], []))

    def test_cycle_saves_status(self):
        dk, fl = FakeDk(disk="missing", containers={"radarr": Q()}), FakeFlow()
        self.assertEqual(guard.cycle(dk, fl, "/state"), {"disk": "missing", "vpn": "off"})
        self.assertEqual(dk.calls, [("stop", *DISK_APPS)])
        self.assertEqual(fl.saved, [{"disk": "missing", "vpn": "off"}])

    def test_cycle_restarts_blind_apps(self):
        dk = FakeDk(containers={"radarr": Q(), "sonarr": Q(running=False)}, blind={"radarr", "sonarr"})
        guard.cycle(dk, FakeFlow(), "/state")
        self.assertEqual(dk.probed, ["radarr"])  # only running apps are asked
        self.assertEqual(dk.calls, [("up", "-d", *DISK_APPS), ("restart", "radarr")])

    def test_one_failing_action_does_not_stop_the_others(self):
        dk = FakeDk(containers={"gluetun": G(id="g2"), "qbittorrent": Q()}, fail_on="--force-recreate")
        fl = FakeFlow(vpn="protonvpn")
        self.assertEqual(guard.cycle(dk, fl, "/state"), {"disk": "ok", "vpn": "ok"})
        self.assertEqual(dk.calls, [("up", "-d", "--force-recreate", "--no-deps", "qbittorrent"),
                                    ("up", "-d", *DISK_APPS)])
        self.assertEqual(len(fl.saved), 1)

    def test_blind_is_only_probed_when_disk_ok(self):
        dk = FakeDk(disk="missing", containers={"radarr": Q()})
        guard.cycle(dk, FakeFlow(), "/state")
        self.assertEqual(dk.probed, [])


if __name__ == "__main__":
    unittest.main()


class RemovalRaceTest(unittest.TestCase):
    def test_cycle_stops_when_removal_starts(self):
        dk = FakeDk(containers={"radarr": Q()})
        dk.worker_after = 1  # free at the start of the round, then the worker exists
        guard.cycle(dk, FakeFlow(), "/state")
        self.assertEqual(dk.calls, [])

    def test_removal_waits_for_the_running_round(self):
        import threading
        from installer import flow
        from tests.test_flow import FakeDocker, settings
        dk = FakeDocker()
        flow.OPS_LOCK.acquire()
        t = threading.Thread(target=flow.uninstall, args=(False, lambda p: None),
                             kwargs={"dk": dk, "state_dir": "/nonexistent"})
        t.start()
        t.join(0.3)
        self.assertFalse(any(c[0] == "detached" for c in dk.calls))
        flow.OPS_LOCK.release()
        t.join(2)
        self.assertTrue(any(c[0] == "detached" for c in dk.calls))

    def test_cycle_holds_the_lock(self):
        from installer import flow
        seen = []
        dk = FakeDk()
        dk.marker_state = lambda src: seen.append(flow.OPS_LOCK.locked()) or "ok"
        guard.cycle(dk, FakeFlow(), "/state")
        self.assertEqual(seen, [True])


class HangTest(unittest.TestCase):
    def test_every_action_has_a_timeout(self):
        dk = FakeDk(containers={"radarr": Q()}, blind={"radarr"})
        guard.cycle(dk, FakeFlow(), "/state")
        self.assertEqual(len(dk.calls), 2)
        self.assertTrue(all(t for t in dk.timeouts))


class FractionTest(unittest.TestCase):
    def test_restart_in_the_same_second_is_seen(self):
        acts, _ = guard.decide("ok", True, {"gluetun": G(started="2026-11-06T10:00:00.2Z"),
                                            "qbittorrent": Q(started="2026-11-06T10:00:00.12Z")}, set())
        self.assertEqual(acts[0], ("recreate_qbittorrent",))


class ApplyPauseTest(unittest.TestCase):
    def test_cycle_skips_while_applying(self):
        dk = FakeDk(containers={"radarr": Q()})
        dk.applying = True
        self.assertIsNone(guard.cycle(dk, FakeFlow(), "/state"))
        self.assertEqual(dk.calls, [])


class StoppedWorkerTest(unittest.TestCase):
    def test_a_stopped_apply_worker_does_not_pause_the_guard(self):
        # Applied, then nobody opened the progress page: the container stays, stopped.
        dk = FakeDk(disk="missing", containers={"radarr": Q()})
        dk.apply_stopped = True
        self.assertEqual(guard.cycle(dk, FakeFlow(), "/state"), {"disk": "missing", "vpn": "off"})
