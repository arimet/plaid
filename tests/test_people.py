import os
import stat
import tempfile
import unittest
from unittest import mock

from installer import apps, people
from installer.settings import Settings

HOST = "jellyfin:8096"


def owner(**kw):
    base = dict(data_dir="/srv/arr", username="zoe", password="owner-pw", vpn_provider=None, lang="fr",
                home="/home/zoe", platform="linux", audio_lang="fr", subtitle_langs=["en", "fr"])
    base.update(kw)
    return Settings(**base)


class FakeJellyfin:
    """The routes Plaid uses, as Jellyfin 12.4 answers them."""

    def __init__(self, owner_pw="owner-pw", keys=()):
        self.owner_pw = owner_pw
        self.keys = list(keys)
        self.users = {"o": {"Id": "o", "Name": "zoe", "Policy": {"IsAdministrator": True, "IsDisabled": False}},
                      "a2": {"Id": "a2", "Name": "Admin bis", "Policy": {"IsAdministrator": True, "IsDisabled": False}},
                      "m": {"Id": "m", "Name": "Mamie", "LastActivityDate": "2026-11-10T20:00:00Z",
                            "Policy": {"IsAdministrator": False, "IsDisabled": True}}}
        self.writes, self.passwords, self.configs = [], {}, {}

    def __call__(self, method, url, body=None, headers=None, form=False):
        path = url.split("8096", 1)[1]
        if path == "/Users/AuthenticateByName":
            if body["Pw"] != self.owner_pw:
                raise apps.HttpError(401, url, "")
            return {"AccessToken": "session", "User": {"Id": "o"}}
        if path == "/Auth/Keys" and method == "GET":
            return {"Items": [{"AppName": n, "AccessToken": f"key-{n}"} for n in self.keys]}
        if path == "/Auth/Keys?app=plaid":
            self.keys.append("plaid")
            return None
        assert headers == {"Authorization": 'MediaBrowser Token="key-plaid"'}, headers
        if method != "GET":
            self.writes.append((method, path))
        if path == "/System/Info":
            return {}
        if path == "/Users" and method == "GET":
            return list(self.users.values())
        if path == "/Users/New":
            self.users["n"] = {"Id": "n", "Name": body["Name"], "Policy": {"IsAdministrator": False, "IsDisabled": False}}
            self.passwords["n"] = body["Password"]
            return self.users["n"]
        if path.startswith("/Users/") and path.endswith("/Policy"):
            self.users[path.split("/")[2]]["Policy"] = body
            return None
        if path.startswith("/Users/Configuration?userId="):
            self.configs[path.split("=")[1]] = body
            return None
        if path.startswith("/Users/Password?userId="):
            self.passwords[path.split("=")[1]] = body["NewPw"]
            return None
        if path.startswith("/Users/") and method == "GET":
            return self.users[path.split("/")[2]]
        if path.startswith("/Users/") and method == "DELETE":
            del self.users[path.split("/")[2]]
            return None
        raise AssertionError(path)


class PasswordTest(unittest.TestCase):
    def test_sixteen_readable_characters(self):
        for _ in range(50):
            pw = people.make_password()
            self.assertEqual(len(pw), 16)
            self.assertFalse(set(pw) & set("0O1lI"), pw)


class KeyTest(unittest.TestCase):
    def test_saved_key_needs_no_login(self):
        jf = FakeJellyfin(owner_pw="refused")
        with tempfile.TemporaryDirectory() as d, mock.patch("installer.apps.call", jf):
            open(os.path.join(d, people.KEY_FILE), "w").write("key-plaid")
            self.assertEqual(people.api_key(owner(), HOST, d), "key-plaid")

    def test_first_use_creates_and_keeps_the_key(self):
        jf = FakeJellyfin()
        with tempfile.TemporaryDirectory() as d, mock.patch("installer.apps.call", jf):
            self.assertEqual(people.api_key(owner(), HOST, d), "key-plaid")
            path = os.path.join(d, people.KEY_FILE)
            self.assertEqual(open(path).read(), "key-plaid")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(jf.keys, ["plaid"])

    def test_existing_key_is_reused(self):
        jf = FakeJellyfin(keys=["arr", "plaid"])
        with tempfile.TemporaryDirectory() as d, mock.patch("installer.apps.call", jf):
            self.assertEqual(people.api_key(owner(), HOST, d), "key-plaid")
        self.assertEqual(jf.keys, ["arr", "plaid"])

    def test_refused_owner_password(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("installer.apps.call", FakeJellyfin(owner_pw="other")):
            with self.assertRaises(PermissionError):
                people.api_key(owner(), HOST, d)


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.jf = FakeJellyfin(keys=["plaid"])
        patcher = mock.patch("installer.apps.call", self.jf)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_shows_only_people_who_watch(self):
        self.assertEqual(people.list_people(HOST, "key-plaid"),
                         [{"id": "m", "name": "Mamie", "last_seen": "2026-11-10T20:00:00Z", "locked": True}])

    def test_add_sets_watch_only_policy_and_preferences(self):
        pw = people.add_person(owner(), HOST, "key-plaid", "Papi")
        self.assertEqual(self.jf.passwords["n"], pw)
        policy = self.jf.users["n"]["Policy"]
        for key, value in people.POLICY.items():
            self.assertEqual(policy[key], value, key)
        config = self.jf.configs["n"]
        self.assertEqual((config["AudioLanguagePreference"], config["SubtitleLanguagePreference"]), ("fra", "eng"))
        self.assertEqual((config["SubtitleMode"], config["PlayDefaultAudioTrack"]), ("Smart", False))

    def test_new_password_unlocks(self):
        pw = people.new_password(HOST, "key-plaid", "m")
        self.assertEqual(self.jf.passwords["m"], pw)
        self.assertFalse(self.jf.users["m"]["Policy"]["IsDisabled"])

    def test_unlock_and_remove(self):
        people.unlock(HOST, "key-plaid", "m")
        self.assertFalse(self.jf.users["m"]["Policy"]["IsDisabled"])
        people.remove(HOST, "key-plaid", "m")
        self.assertNotIn("m", self.jf.users)

    def test_administrators_are_never_touched(self):
        for action in (lambda: people.remove(HOST, "key-plaid", "a2"),
                       lambda: people.unlock(HOST, "key-plaid", "o"),
                       lambda: people.new_password(HOST, "key-plaid", "o")):
            with self.assertRaises(PermissionError):
                action()
        self.assertEqual(self.jf.writes, [])

    def test_protect_owner_sets_the_lockout(self):
        people.protect_owner(HOST, "key-plaid", "zoe")
        self.assertEqual(self.jf.users["o"]["Policy"]["LoginAttemptsBeforeLockout"], 5)
        self.assertTrue(self.jf.users["o"]["Policy"]["IsAdministrator"])

    def test_owner_lock(self):
        self.assertFalse(people.owner_locked(HOST, "key-plaid", "zoe"))
        self.jf.users["o"]["Policy"]["IsDisabled"] = True
        self.assertTrue(people.owner_locked(HOST, "key-plaid", "zoe"))
        people.unlock_owner(HOST, "key-plaid", "zoe")
        self.assertFalse(self.jf.users["o"]["Policy"]["IsDisabled"])


class InviteTest(unittest.TestCase):
    def test_home_address_account_and_apps(self):
        text = people.invite_text("fr", owner(), "192.168.1.10", "Mamie", "pw-123", None)
        for part in ("Mamie", "pw-123", "http://192.168.1.10:8096", "Jellyfin", "Android TV", "Apple TV", "Samsung", "LG"):
            self.assertIn(part, text)
        self.assertNotIn("plaid:8096", text)
        self.assertNotIn("https://", text)

    def test_tailscale_on_linux(self):
        text = people.invite_text("fr", owner(tailscale_key="tskey-x"), "192.168.1.10", "Mamie", "pw", None)
        self.assertIn("http://plaid:8096", text)

    def test_tailscale_on_a_desktop(self):
        text = people.invite_text("en", owner(platform="mac", lang="en"), "localhost", "Gran", "pw", None)
        self.assertIn("http://COMPUTER-NAME:8096", text)

    def test_internet_address(self):
        text = people.invite_text("en", owner(lang="en"), "192.168.1.10", "Gran", "pw", "https://plaid.tail1.ts.net")
        self.assertIn("https://plaid.tail1.ts.net", text)


class FunnelStatusTest(unittest.TestCase):
    def test_ready(self):
        out = '{"Self": {"DNSName": "plaid.tail1234.ts.net.", "CapMap": {"https": null, "funnel": null}}}'
        self.assertEqual(people.funnel_status(out), (True, "https://plaid.tail1234.ts.net"))

    def test_not_allowed(self):
        out = '{"Self": {"DNSName": "plaid.tail1234.ts.net.", "CapMap": {"https": null}}}'
        self.assertEqual(people.funnel_status(out), (False, "https://plaid.tail1234.ts.net"))

    def test_unreadable(self):
        self.assertEqual(people.funnel_status("Logged out."), (False, None))


class StaleKeyTest(unittest.TestCase):
    def test_a_key_jellyfin_no_longer_knows_is_made_again(self):
        jf = FakeJellyfin(keys=["plaid"])
        real = jf.__call__

        def call(method, url, body=None, headers=None, form=False):
            if headers == {"Authorization": 'MediaBrowser Token="old-key"'}:
                raise apps.HttpError(401, url, "")
            return real(method, url, body, headers, form)
        with tempfile.TemporaryDirectory() as d, mock.patch("installer.apps.call", call):
            open(os.path.join(d, people.KEY_FILE), "w").write("old-key")
            self.assertEqual(people.api_key(owner(), HOST, d), "key-plaid")
            self.assertEqual(open(os.path.join(d, people.KEY_FILE)).read(), "key-plaid")
