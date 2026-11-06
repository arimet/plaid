import unittest

from installer import reconfigure
from tests.test_flow import settings


class MergeTest(unittest.TestCase):
    def test_blank_secrets_keep_current(self):
        old = settings(tailscale_key="tskey-auth-old")
        new = reconfigure.merge(old, {"password": "", "vpn_provider": "protonvpn", "vpn_fields": {},
                                      "tailscale_key": ""})
        self.assertEqual((new.password, new.vpn_fields, new.tailscale_key),
                         (old.password, old.vpn_fields, old.tailscale_key))

    def test_vpn_removed(self):
        new = reconfigure.merge(settings(), {"vpn_provider": ""})
        self.assertEqual((new.vpn_provider, new.vpn_fields), (None, {}))
        self.assertEqual(reconfigure.changes(settings(), new), {"vpn": "remove"})

    def test_new_provider_takes_only_its_fields(self):
        new = reconfigure.merge(settings(), {"vpn_provider": "mullvad", "vpn_fields": {"WIREGUARD_PRIVATE_KEY": "m"}})
        self.assertEqual(new.vpn_fields, {"WIREGUARD_PRIVATE_KEY": "m"})

    def test_same_provider_new_key(self):
        new = reconfigure.merge(settings(), {"vpn_provider": "protonvpn", "vpn_fields": {"WIREGUARD_PRIVATE_KEY": "k2"}})
        self.assertEqual(reconfigure.changes(settings(), new), {"vpn": "change"})

    def test_tailscale_remove(self):
        new = reconfigure.merge(settings(tailscale_key="tskey-a"), {"remove_tailscale": True})
        self.assertEqual(new.tailscale_key, "")

    def test_platform_fields_never_change(self):
        new = reconfigure.merge(settings(), {"platform": "windows", "puid": 0, "home": "/"})
        self.assertEqual((new.platform, new.puid, new.home), ("linux", 1000, "/home/z"))

    def test_text_fields_are_trimmed(self):
        new = reconfigure.merge(settings(), {"data_dir": " /srv/new ", "username": " bob "})
        self.assertEqual((new.data_dir, new.username), ("/srv/new", "bob"))


class ChangesTest(unittest.TestCase):
    def test_changes(self):
        old = settings()
        self.assertEqual(reconfigure.changes(old, old), {})
        self.assertEqual(reconfigure.changes(settings(vpn_provider=None, vpn_fields={}), old), {"vpn": "add"})
        self.assertEqual(reconfigure.changes(old, settings(password="anotherone")), {"account": "change"})
        self.assertEqual(reconfigure.changes(old, settings(username="bob")), {"account": "change"})
        self.assertEqual(reconfigure.changes(old, settings(data_dir="/srv/new")), {"folder": "change"})
        self.assertEqual(reconfigure.changes(old, settings(lang="fr")), {"lang": "change"})
        self.assertEqual(reconfigure.changes(old, settings(timezone="Europe/Paris")), {"timezone": "change"})
        self.assertEqual(reconfigure.changes(old, settings(tailscale_key="tskey-a")), {"tailscale": "add"})
        self.assertEqual(reconfigure.changes(settings(tailscale_key="tskey-a"), settings(tailscale_key="tskey-b")),
                         {"tailscale": "change"})

    def test_steps(self):
        S = reconfigure.steps
        self.assertEqual(S({"lang": "change"}, True), ["step_save"])
        self.assertEqual(S({"timezone": "change"}, True), ["step_compose", "step_vpn", "step_save"])
        self.assertEqual(S({"timezone": "change"}, False), ["step_compose", "step_save"])
        self.assertEqual(S({"vpn": "remove"}, False), ["step_compose", "step_download_client", "step_save"])
        self.assertEqual(S({"vpn": "add"}, True), ["step_compose", "step_vpn", "step_download_client", "step_save"])
        self.assertEqual(S({"vpn": "change"}, True), ["step_compose", "step_vpn", "step_save"])
        self.assertEqual(S({"folder": "change", "account": "change"}, False),
                         ["step_folder", "step_compose", "step_rescan", "step_account", "step_save"])
        self.assertEqual(S({"tailscale": "remove"}, False), ["step_compose", "step_save"])
        self.assertEqual(S({"tailscale": "change"}, False), ["step_compose", "step_tailscale", "step_save"])

    def test_outage(self):
        self.assertEqual(reconfigure.outage({"lang": "change", "account": "change"}), "outage_none")
        self.assertEqual(reconfigure.outage({"timezone": "change"}), "outage_apps")
        self.assertEqual(reconfigure.outage({"vpn": "change"}), "outage_downloads")
        self.assertEqual(reconfigure.outage({"vpn": "add", "folder": "change"}), "outage_apps")


if __name__ == "__main__":
    unittest.main()
