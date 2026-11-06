import unittest

from installer.settings import Settings, is_dangerous_path, validate

HOME = "/Users/zoe"
WIN_HOME = "C:\\Users\\Zoé"


def make(**kw):
    base = dict(data_dir=HOME + "/Arr", username="admin", password="longenough", vpn_provider=None,
                vpn_fields={}, timezone="Europe/Paris", lang="en", puid=1000, pgid=1000, home=HOME,
                platform="mac")
    base.update(kw)
    return Settings(**base)


class DangerousPathTest(unittest.TestCase):
    def test_dangerous_paths(self):
        for path in ["/", "/Users", "/home", "/volume1", "/mnt", "/srv", HOME, HOME + "/", "/Users/zoe/.."]:
            self.assertTrue(is_dangerous_path(path, HOME), path)
        for path in ["C:\\", "C:\\Users", WIN_HOME, "c:/"]:
            self.assertTrue(is_dangerous_path(path, WIN_HOME), path)

    def test_safe_paths(self):
        self.assertFalse(is_dangerous_path(HOME + "/Arr", HOME))
        self.assertFalse(is_dangerous_path("/srv/arr", HOME))
        self.assertFalse(is_dangerous_path("C:\\Users\\Zoé\\Mes vidéos\\Stack", WIN_HOME))


class ValidateTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(validate(make()), [])

    def test_errors(self):
        self.assertEqual(validate(make(password="short")), ["err_password_short"])
        self.assertEqual(validate(make(username=" ")), ["err_username"])
        self.assertEqual(validate(make(lang="de")), ["err_lang"])
        self.assertEqual(validate(make(vpn_provider="acme")), ["err_vpn_provider"])
        self.assertEqual(validate(make(data_dir=HOME)), ["err_dangerous_path"])
        self.assertEqual(validate(make(data_dir="relative/dir")), ["err_path_absolute"])

    def test_missing_vpn_field(self):
        self.assertEqual(validate(make(vpn_provider="protonvpn", vpn_fields={})), ["err_vpn_fields"])


class RoundtripTest(unittest.TestCase):
    def test_roundtrip(self):
        s = make(password='p@ss "$x" é', vpn_provider="protonvpn", vpn_fields={"WIREGUARD_PRIVATE_KEY": "k='1'"})
        self.assertEqual(Settings.from_json(s.to_json()), s)


if __name__ == "__main__":
    unittest.main()
