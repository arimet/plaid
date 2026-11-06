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

    def install(self, s, report):
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
