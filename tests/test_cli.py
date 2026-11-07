import unittest

from installer import cli, media
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
        self.assertEqual(flow.got, media.effective(Settings(data_dir="/home/zoe/Plaid", username="zoe", password="longenough",
                                            vpn_provider="protonvpn", vpn_fields={"WIREGUARD_PRIVATE_KEY": "key"},
                                            timezone="Europe/Paris", lang="en", puid=1000, pgid=1000,
                                            home="/home/zoe", platform="linux")))  # defaults made explicit
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
        # folder: default · user: zoe · passwords · VPN: up wraps to "No VPN" · Tailscale: skip · films: keep ·
        # go: left = yes
        t, out = self.term("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r", "\r", "\x1b[D\r")
        self.assertEqual(cli.run_install_tui(t, flow, ENV, "fr"), 0)
        self.assertEqual((flow.got.username, flow.got.vpn_provider, flow.got.data_dir), ("zoe", None, "/home/zoe/Plaid"))
        self.assertIn("http://localhost:8096", out.getvalue())

    def test_wrong_confirmation_asks_again(self):
        flow = FakeFlow()
        # no warning: "Install now?" already sits on Yes, so Enter starts it
        t, out = self.term("\r", "zoe\r", "longenough\r", "oops\r", "longenough\r", "\x1b[A\r", "\r", "\r", "\r")
        self.assertEqual(cli.run_install_tui(t, flow, ENV, "fr"), 0)
        self.assertIn("Les mots de passe diffèrent.", out.getvalue())

    def test_declining_changes_nothing(self):
        flow = FakeFlow(found=["warn_no_vpn"])
        t, out = self.term("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r", "\r", "\r")
        self.assertEqual(cli.run_install_tui(t, flow, ENV, "fr"), 1)
        self.assertIsNone(flow.got)
        self.assertIn("Rien n’a été modifié", out.getvalue())


class FolderWarningCliTest(TuiCliTest):
    def test_warning_names_the_folder(self):
        flow = FakeFlow(found=["warn_dir_not_empty"])
        t, out = self.term("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r", "\r", "\r")
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
        t = tui.Term(read_key=keys("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r", "\r", "\r"),
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
    flow.status, flow.confirmed = {"disk": "ok", "vpn": "off"}, False
    flow.guard_status = lambda: flow.status

    def confirm_disk(s):
        flow.confirmed, flow.status = True, dict(flow.status, disk="ok")
        return True
    flow.confirm_disk = confirm_disk
    flow.started = None
    flow.settings_check = lambda old, new: []
    flow.check_logins = lambda s: []

    def start_apply(new, current):
        flow.started, flow.current = new, current
    flow.start_apply = start_apply
    flow.apply_status = lambda: {"state": "done", "events": [{"step": "step_compose"}, {"step": "step_save"},
                                                              {"step": "step_done", "done": True, "detail": ""}]}
    return flow


class PanelCliTest(TuiCliTest):
    # Menu order: apps, trackers, remote, remove, quit. Up from the top wraps to "quit".
    def test_apps_then_quit(self):
        flow = installed_flow()
        t, out = self.term("\r", "\r", "\x1b[A\r")  # apps · Enter to come back · quit
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
        t, out = self.term("\x1b[B\x1b[B\x1b[B\x1b[B\r", "\x1b[D\r", "\r")
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings), 0)
        self.assertIs(flow.removed, False)

    def test_failed_install_offers_removal_only(self):
        flow = installed_flow()
        flow.complete = False
        t, out = self.term("\x1b[D\r", "\r")
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings, complete=False), 0)
        self.assertIs(flow.removed, False)
        self.assertIn("L’installation s’est arrêtée", out.getvalue())


class GuardCliTest(TuiCliTest):
    def test_menu_shows_missing_disk(self):
        flow = installed_flow()
        flow.status = {"disk": "missing", "vpn": "down"}
        t, out = self.term("\x1b[A\r")  # quit
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings), 0)
        self.assertIn("Disque des médias introuvable. Plaid l’attend ici : /home/zoe/Plaid.", out.getvalue())
        self.assertIn("VPN déconnecté.", out.getvalue())
        self.assertFalse(flow.confirmed)

    def test_menu_confirms_disk(self):
        flow = installed_flow()
        flow.status = {"disk": "unconfirmed", "vpn": "off"}
        t, out = self.term("\r", "\x1b[A\r")  # the first action confirms · then quit
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings), 0)
        self.assertTrue(flow.confirmed)
        self.assertIn("C’est noté.", out.getvalue())


class SettingsCliTest(TuiCliTest):
    # Panel menu: apps, trackers, remote, settings, remove, quit.
    # Settings menu: language, time zone, folder, account, VPN, Tailscale (Linux), [apply], back.
    def test_change_timezone_from_the_menu(self):
        flow = installed_flow()
        t, out = self.term("\x1b[B\x1b[B\x1b[B\r", "\x1b[B\r", "America/Montreal\r", "\x1b[A\x1b[A\r", "\r")
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings), 0)
        self.assertEqual(flow.started.timezone, "America/Montreal")
        self.assertIn("Vos changements sont appliqués", out.getvalue())

    def test_blank_password_keeps_it(self):
        flow = installed_flow()
        # settings · account · username unchanged · empty password · back · quit
        t, out = self.term("\x1b[B\x1b[B\x1b[B\r", "\x1b[B\x1b[B\x1b[B\r", "\r", "\r", "\x1b[A\r", "\x1b[A\r")
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings), 0)
        self.assertIsNone(flow.started)

    def test_blocking_error_stops_apply(self):
        flow = installed_flow()
        flow.settings_check = lambda old, new: ["err_folder_missing"]
        # settings · folder · new path · apply · (error shown, Enter to go back) · back · quit
        t, out = self.term("\x1b[B\x1b[B\x1b[B\r", "\x1b[B\x1b[B\r", "/media/usb/plaid\r", "\x1b[A\x1b[A\r", "\r",
                           "\x1b[A\r", "\x1b[A\r")
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings), 0)
        self.assertIsNone(flow.started)
        self.assertIn("Dossier introuvable", out.getvalue())


class ManualPasswordCliTest(TuiCliTest):
    def test_apps_to_change_by_hand_are_listed(self):
        flow = installed_flow()
        flow.apply_status = lambda: {"state": "done", "events": [{"step": "step_account"},
                                                                  {"step": "step_done", "done": True, "detail": "jellyfin"}]}
        t, out = self.term("\x1b[B\x1b[B\x1b[B\r", "\x1b[B\r", "America/Montreal\r", "\x1b[A\x1b[A\r", "\r")
        cli.run_panel_tui(t, flow, dict(ENV, HOST_ADDR="192.168.1.20"), flow.settings)
        cli._print_notes(t, [])
        self.assertIn("Jellyfin  http://192.168.1.20:8096", out.getvalue())


DOWN, UP, SPACE = "\x1b[B", "\x1b[A", " "


class MediaCliTest(TuiCliTest):
    def test_install_keeps_default_media(self):
        flow = FakeFlow()
        t, out = self.term("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r", "\r", "\r")
        self.assertEqual(cli.run_install_tui(t, flow, ENV, "fr"), 0)
        self.assertEqual((flow.got.profiles, flow.got.subtitle_langs), (["mine-1080p"], ["fr", "en"]))
        self.assertIn("Français · 1080p", out.getvalue())

    def test_install_asks_media(self):
        flow = FakeFlow()
        # films: change · audio: down = English · profiles: also tick orig-4k (6 rows down) · default: down = orig-4k ·
        # subtitles: untick en (down, space) · fiches: Enter (French)
        t, out = self.term("\r", "zoe\r", "longenough\r", "longenough\r", "\x1b[A\r", "\r",
                           DOWN + "\r", DOWN + "\r", DOWN * 5 + SPACE + "\r", DOWN + "\r", DOWN + SPACE + "\r", "\r",
                           "\r")
        self.assertEqual(cli.run_install_tui(t, flow, ENV, "fr"), 0)
        g = flow.got
        self.assertEqual((g.audio_lang, g.profiles, g.default_profile, g.subtitle_langs, g.metadata_lang),
                         ("en", ["mine-1080p", "orig-4k"], "orig-4k", ["fr"], "fr"))
        self.assertIn("Anglais · 1080p", out.getvalue())

    def test_settings_tui_media_change(self):
        flow = installed_flow()
        # settings · films (up x3 from lang: back, apply? no change yet: back, then films) · keep audio ·
        # tick mine-4k · default stays (Enter) · subtitles Enter · fiches Enter · apply (up x2) · confirm
        t, out = self.term("\x1b[B\x1b[B\x1b[B\r", UP + UP + "\r", "\r", DOWN * 2 + SPACE + "\r", "\r", "\r", "\r",
                           UP + UP + "\r", "\r")
        self.assertEqual(cli.run_panel_tui(t, flow, ENV, flow.settings), 0)
        self.assertEqual(flow.started.profiles, ["mine-1080p", "mine-4k"])

    def test_apply_tui_prints_kept(self):
        flow = installed_flow()
        flow.apply_status = lambda: {"state": "done", "events": [
            {"step": "step_profiles"}, {"step": "step_done", "done": True, "detail": "", "kept": ["VO sous-titrée · 4K"]}]}
        t, out = self.term("\x1b[B\x1b[B\x1b[B\r", "\x1b[B\r", "America/Montreal\r", "\x1b[A\x1b[A\r", "\r")
        cli.run_panel_tui(t, flow, ENV, flow.settings)
        cli._print_notes(t, [])
        self.assertIn("VO sous-titrée · 4K", out.getvalue())


class DiskLinesTest(unittest.TestCase):
    def test_disk_lines(self):
        flow = FakeFlow()
        flow.disks = lambda platform, home: [
            {"path": "/", "name": "", "free": 5 * 10 ** 10, "total": 2 * 10 ** 11, "system": True},
            {"path": "/mnt/films", "name": "films", "free": 3 * 10 ** 12, "total": 4 * 10 ** 12, "system": False}]
        lines = cli._disk_lines(flow, ENV, "fr")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("Cet ordinateur · /"))
        self.assertIn("/mnt/films", lines[1])
        self.assertIn("libres sur", lines[1])

    def test_disk_lines_never_block(self):  # no disk list, or Docker failing: the question still comes
        self.assertEqual(cli._disk_lines(FakeFlow(), ENV, "fr"), [])
        flow = FakeFlow()
        flow.disks = lambda platform, home: 1 / 0
        self.assertEqual(cli._disk_lines(flow, ENV, "fr"), [])
