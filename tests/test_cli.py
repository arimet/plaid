import unittest

from installer import cli
from installer.flow import Progress
from installer.settings import Settings

ENV = {"HOST_HOME": "/home/zoe", "HOST_PLATFORM": "linux", "PUID": "1000", "PGID": "1000", "TZ": "Europe/Paris"}


class FakeFlow:
    def __init__(self, found=()):
        self.found = list(found)
        self.got = None
        self.removed = None

    def preflight(self, s):
        return self.found

    def install(self, s, report, cancel=None):
        self.got = s
        report(Progress("step_prepare"))
        report(Progress("step_done", done=True))
        return {"jellyfin": 8096}

    def uninstall(self, remove_media, report):
        self.removed = remove_media


def scripted(*answers):
    it = iter(answers)
    return lambda prompt: next(it)


class CliTest(unittest.TestCase):
    def test_cli_install(self):
        flow, out = FakeFlow(), []
        # folder (default), username, password x2, provider 1 = protonvpn, its key, no Tailscale, time zone (default)
        code = cli.run_install(scripted("", "zoe", "longenough", "longenough", "1", "key", "", ""), out.append, flow, ENV, "en")
        self.assertEqual(code, 0)
        self.assertEqual(flow.got, Settings(data_dir="/home/zoe/Plaid", username="zoe", password="longenough",
                                            vpn_provider="protonvpn", vpn_fields={"WIREGUARD_PRIVATE_KEY": "key"},
                                            timezone="Europe/Paris", lang="en", puid=1000, pgid=1000,
                                            home="/home/zoe", platform="linux"))
        self.assertTrue(any(":8096" in line for line in out))

    def test_cli_warning_declined(self):
        flow, out = FakeFlow(found=["warn_no_vpn"]), []
        code = cli.run_install(scripted("", "zoe", "longenough", "longenough", "0", "", "", ""), out.append, flow, ENV, "en")
        self.assertEqual(code, 1)
        self.assertIsNone(flow.got)

    def test_cli_password_mismatch(self):
        flow, out = FakeFlow(), []
        code = cli.run_install(scripted("", "zoe", "longenough", "different", "0", "", "y"), out.append, flow, ENV, "fr")
        self.assertEqual(code, 1)
        self.assertIn("Les mots de passe diffèrent.", out)

    def test_cli_uninstall_default_keeps_media(self):
        flow = FakeFlow()
        code = cli.run_uninstall(scripted("y", ""), [].append, flow, "en", "/srv/Plaid", "4 GB")
        self.assertEqual((code, flow.removed), (0, False))

    def test_cli_uninstall_cancel(self):
        flow = FakeFlow()
        self.assertEqual(cli.run_uninstall(scripted(""), [].append, flow, "en"), 1)
        self.assertIsNone(flow.removed)


if __name__ == "__main__":
    unittest.main()


class TailscaleCliTest(unittest.TestCase):
    def test_asks_key_on_linux(self):
        flow = FakeFlow()
        code = cli.run_install(scripted("", "zoe", "longenough", "longenough", "0", "tskey-auth-x", "", "y"),
                               [].append, flow, ENV, "en")
        self.assertEqual(code, 0)
        self.assertEqual(flow.got.tailscale_key, "tskey-auth-x")


class TuiCliTest(unittest.TestCase):
    def term(self, *seq):
        import io
        from installer import tui
        from tests.test_tui import keys
        out = io.StringIO()
        return tui.Term(read_key=keys(*seq), out=out, color=False), out

    def test_install_with_arrow_keys(self):
        flow = FakeFlow(found=["warn_no_vpn"])
        # folder: default · user: zoe · passwords · VPN: up wraps to "No VPN" · Tailscale: skip · go: left = yes
        t, out = self.term("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r", "\x1b[D\r")
        self.assertEqual(cli.run_install_tui(t, flow, ENV, "fr"), 0)
        self.assertEqual((flow.got.username, flow.got.vpn_provider, flow.got.data_dir), ("zoe", None, "/home/zoe/Plaid"))
        self.assertIn("http://localhost:8096", out.getvalue())

    def test_wrong_confirmation_asks_again(self):
        flow = FakeFlow()
        # no warning: "Install now?" already sits on Yes, so Enter starts it
        t, out = self.term("\r", "zoe\r", "longenough\r", "oops\r", "longenough\r", "\x1b[A\r", "\r", "\r")
        self.assertEqual(cli.run_install_tui(t, flow, ENV, "fr"), 0)
        self.assertIn("Les mots de passe diffèrent.", out.getvalue())

    def test_declining_changes_nothing(self):
        flow = FakeFlow(found=["warn_no_vpn"])
        t, out = self.term("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r", "\r")
        self.assertEqual(cli.run_install_tui(t, flow, ENV, "fr"), 1)
        self.assertIsNone(flow.got)
        self.assertIn("Rien n’a été modifié", out.getvalue())


class FolderWarningCliTest(TuiCliTest):
    def test_warning_names_the_folder(self):
        flow = FakeFlow(found=["warn_dir_not_empty"])
        t, out = self.term("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r", "\r")
        cli.run_install_tui(t, flow, ENV, "fr")
        self.assertIn("Le dossier /home/zoe/Plaid contient déjà des fichiers", out.getvalue())

    def test_plain_mode_too(self):
        flow, out = FakeFlow(found=["warn_dir_not_empty"]), []
        cli.run_install(scripted("", "zoe", "longenough", "longenough", "0", "", "", ""), out.append, flow, ENV, "fr")
        self.assertTrue(any("/home/zoe/Plaid contient" in line for line in out))


class RollbackCliTest(TuiCliTest):
    def test_undo_stops_the_install_then_removes_everything(self):
        import threading
        flow = FakeFlow()
        flow.rolled_back = False
        flow.rollback = lambda report: setattr(flow, "rolled_back", True)
        flow.uninstall_status = lambda: ("done", "")
        cancel = threading.Event()
        worker = threading.Thread(target=cancel.wait, daemon=True)  # an install that stops once cancelled
        worker.start()
        t, out = self.term()
        self.assertEqual(cli.undo_install(t, flow, "fr", cancel, worker), 1)
        self.assertTrue(cancel.is_set())
        self.assertTrue(flow.rolled_back)
        self.assertIn("la machine est comme avant", out.getvalue())


class PlainRollbackTest(unittest.TestCase):
    def test_ctrl_c_in_plain_mode_rolls_back(self):
        flow, out = FakeFlow(), []
        flow.rolled_back = False

        def interrupted(s, report, cancel=None):
            raise KeyboardInterrupt
        flow.install = interrupted
        flow.rollback = lambda report: setattr(flow, "rolled_back", True)
        code = cli.run_install(scripted("", "zoe", "longenough", "longenough", "0", "", "", "o"), out.append, flow,
                               ENV, "fr")
        self.assertEqual(code, 1)
        self.assertTrue(flow.rolled_back)


class SummaryAlignmentTest(unittest.TestCase):
    def test_links_line_up_with_colours_on(self):
        import io
        from installer import tui
        from tests.test_tui import keys
        out = io.StringIO()
        t = tui.Term(read_key=keys("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r", "\r"),
                     out=out, color=True)
        cli.run_install_tui(t, FakeFlow(), ENV, "fr")
        rows = [tui._ANSI.sub("", line) for line in out.getvalue().splitlines() if "http://localhost:" in line
                and "■" in tui._ANSI.sub("", line)]
        self.assertEqual(len({row.index("http://") for row in rows}), 1, rows)


def installed_flow():
    from installer.settings import Settings
    flow = FakeFlow()
    flow.settings = Settings(data_dir="/home/zoe/Plaid", username="zoe", password="longenough", vpn_provider=None,
                             home="/home/zoe", platform="linux", lang="fr")
    flow.added, flow.removed = [], None
    flow.tracker_catalog = lambda: [
        {"id": "alpha", "name": "Alpha", "language": "en-US", "description": "a", "added": False},
        {"id": "beta", "name": "Beta", "language": "fr-FR", "description": "b", "added": True}]
    flow.add_tracker = flow.added.append
    flow.uninstall_status = lambda: ("done", "")
    flow.media_size = lambda s: 0
    return flow


class PanelCliTest(TuiCliTest):
    # Menu order: apps, trackers, remote, remove, quit. Up from the top wraps to "quit".
    def test_apps_then_quit(self):
        flow = installed_flow()
        t, out = self.term("\r", "\x1b[A\r")
        self.assertEqual(cli.run_panel_tui(t, flow, dict(ENV, HOST_ADDR="192.168.1.20"), flow.settings), 0)
        self.assertIn("http://192.168.1.20:8096", out.getvalue())

    def test_add_a_tracker(self):
        flow = installed_flow()
        # trackers · search "al" · pick the first match · empty search goes back · quit
        t, out = self.term("\x1b[B\r", "al\r", "\r", "\r", "\x1b[A\r")
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings), 0)
        self.assertEqual(flow.added, ["alpha"])
        self.assertIn("Alpha", out.getvalue())

    def test_remove_from_the_menu(self):
        flow = installed_flow()
        # remove · confirm (left = yes) · keep the media folder (Enter = no)
        t, out = self.term("\x1b[B\x1b[B\x1b[B\r", "\x1b[D\r", "\r")
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings), 0)
        self.assertIs(flow.removed, False)

    def test_failed_install_offers_removal_only(self):
        flow = installed_flow()
        flow.complete = False
        t, out = self.term("\x1b[D\r", "\r")
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings, complete=False), 0)
        self.assertIs(flow.removed, False)
        self.assertIn("L’installation s’est arrêtée", out.getvalue())
