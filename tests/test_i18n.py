import pathlib
import re
import unittest

from installer.i18n import T, t

SRC = pathlib.Path(__file__).resolve().parent.parent / "installer"


class I18nTest(unittest.TestCase):
    def test_same_keys(self):
        self.assertEqual(set(T["en"]) ^ set(T["fr"]), set())

    def test_every_used_key_exists(self):
        used = set()
        for path in SRC.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            used |= set(re.findall(r'"((?:err|warn|step)_[a-z_]+)', text))
            used |= set(re.findall(r'\bt\([^,()]+, "([a-z_]+)"\s*[,)]', text))
        self.assertEqual(used - set(T["en"]), set())

    def test_parameterised_keys(self):
        self.assertIn("8096", t("en", "err_port_busy:8096"))
        self.assertIn("radarr", t("fr", "step_app:radarr").lower())

    def test_placeholders_filled(self):
        self.assertEqual(t("fr", "uninstall_media", path="/srv/a", size="4 Go"),
                         "Supprimer aussi le dossier /srv/a (4 Go)")

    def test_unknown_lang_falls_back_to_english(self):
        self.assertEqual(t("de", "step_done"), T["en"]["step_done"])


if __name__ == "__main__":
    unittest.main()


class DynamicKeysTest(unittest.TestCase):
    def test_app_and_vpn_field_keys(self):
        from installer.compose import SERVICES
        from installer.vpn import PROVIDERS
        keys = {f"app_{s}" for s in SERVICES} | {f"vpnf_{f.lower()}" for p in PROVIDERS.values() for f in p.fields}
        self.assertEqual(keys - set(T["en"]), set())
