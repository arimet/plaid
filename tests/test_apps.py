import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from installer import apps


class Slow(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(1)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class CallTest(unittest.TestCase):
    def test_timeout_names_the_url(self):
        server = HTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/indexer"
        old, apps.TIMEOUT = apps.TIMEOUT, 0.2
        self.addCleanup(setattr, apps, "TIMEOUT", old)
        with self.assertRaises(OSError) as ctx:
            apps.call("GET", url)
        self.assertIn("/api/v1/indexer", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class DefaultIndexerTest(unittest.TestCase):
    def test_saved_disabled_then_bulk_enabled(self):
        calls = []

        def prowlarr(method, path, body=None):
            calls.append((method, path, body))
            if path == "/indexer":
                return [] if method == "GET" else {"id": 7}
            if path == "/indexer/schema":
                return [{"definitionName": "internetarchive", "name": "Internet Archive", "privacy": "public",
                         "protocol": "torrent", "fields": []}]
        apps.add_default_indexer(prowlarr)
        # Saving an enabled indexer runs a live search that archive.org can take minutes to answer;
        # a disabled one saves at once, and the bulk edit enables it without testing.
        post = next(c for c in calls if c[0] == "POST")
        self.assertEqual((post[1], post[2]["enable"]), ("/indexer", False))
        self.assertEqual(calls[-1], ("PUT", "/indexer/bulk", {"ids": [7], "enable": True}))


def fake_prowlarr(existing=(), schema=None):
    calls = []
    schema = schema or [
        {"definitionName": "zeta", "name": "Zeta", "privacy": "public", "protocol": "torrent", "language": "fr-FR",
         "description": "z", "fields": []},
        {"definitionName": "alpha", "name": "Alpha", "privacy": "public", "protocol": "torrent", "language": "en-US",
         "description": "a", "fields": []},
        {"definitionName": "club", "name": "Club", "privacy": "private", "protocol": "torrent", "fields": []},
        {"definitionName": "news", "name": "News", "privacy": "public", "protocol": "usenet", "fields": []},
    ]

    def prowlarr(method, path, body=None):
        calls.append((method, path, body))
        if path == "/indexer" and method == "GET":
            return [{"definitionName": d, "name": d} for d in existing]
        if path == "/indexer/schema":
            return [dict(x) for x in schema]
        if path == "/indexer" and method == "POST":
            return {"id": 9}
    return prowlarr, calls


class CatalogTest(unittest.TestCase):
    def test_public_torrent_trackers_sorted_with_added_flag(self):
        prowlarr, _ = fake_prowlarr(existing=["zeta"])
        found = apps.list_public_indexers(prowlarr)
        self.assertEqual([(t["id"], t["added"]) for t in found], [("alpha", False), ("zeta", True)])
        self.assertEqual(found[0]["language"], "en-US")

    def test_add_indexer_is_idempotent(self):
        prowlarr, calls = fake_prowlarr(existing=["alpha"])
        apps.add_indexer(prowlarr, "alpha")
        self.assertFalse([c for c in calls if c[0] == "POST"])

    def test_add_indexer_rejects_private_or_unknown(self):
        prowlarr, _ = fake_prowlarr()
        for definition in ("club", "nope"):
            with self.assertRaises(KeyError):
                apps.add_indexer(prowlarr, definition)

    def test_add_indexer_saves_then_enables(self):
        prowlarr, calls = fake_prowlarr()
        apps.add_indexer(prowlarr, "alpha")
        post = next(c for c in calls if c[0] == "POST")
        self.assertEqual((post[2]["name"], post[2]["enable"]), ("Alpha", False))
        self.assertEqual(calls[-1], ("PUT", "/indexer/bulk", {"ids": [9], "enable": True}))


from unittest import mock

from installer.settings import Settings


def acct(**kw):
    base = dict(data_dir="/srv/arr", username="admin", password="old-pass1", vpn_provider=None, lang="en")
    base.update(kw)
    return Settings(**base)


class FakeArr(apps.Arr):
    def __init__(self, name, clients=()):
        self.name, self.key, self.calls, self.clients = name, "k", [], list(clients)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/downloadclient":
            return self.clients
        if path == "/config/host":
            return {"id": 1}


def qbit_client(host):
    return {"id": 3, "name": "qBittorrent", "fields": [{"name": "host", "value": host}, {"name": "port", "value": 8080}]}


class ArrSettingsTest(unittest.TestCase):
    def test_set_download_client_host(self):
        app = FakeArr("radarr", [qbit_client("gluetun")])
        app.set_download_client_host("qbittorrent")
        put = [c for c in app.calls if c[0] == "PUT"]
        # forceSave: qBittorrent may still be starting; Radarr would refuse a client it cannot reach yet.
        self.assertEqual(put[0][1], "/downloadclient/3?forceSave=true")
        self.assertEqual(put[0][2]["fields"][0]["value"], "qbittorrent")

    def test_set_download_client_host_noop(self):
        app = FakeArr("radarr", [qbit_client("qbittorrent")])
        app.set_download_client_host("qbittorrent")
        self.assertFalse([c for c in app.calls if c[0] == "PUT"])

    def test_rescan_commands(self):
        radarr, sonarr = FakeArr("radarr"), FakeArr("sonarr")
        radarr.rescan()
        sonarr.rescan()
        self.assertEqual(radarr.calls, [("POST", "/command", {"name": "RescanMovie"})])
        self.assertEqual(sonarr.calls, [("POST", "/command", {"name": "RescanSeries"})])


def host_of(svc):
    return f"{svc}:1"


class AccountTest(unittest.TestCase):
    def fake_call(self, refuse=(), down=()):
        self.seen = []

        def call(method, url, body=None, headers=None, form=False):
            self.seen.append((method, url, body))
            app = url.split("//")[1].split(":")[0]
            if app in down:
                raise OSError("unreachable")
            if app in refuse:
                raise apps.HttpError(401, url, "")
            if url.endswith("/Users/AuthenticateByName"):
                return {"AccessToken": "t", "User": {"Id": "u1"}}
            if url.endswith("/Users/u1") and method == "GET":
                return {"Name": "admin", "Id": "u1"}
            return None
        return call

    def test_check_logins_lists_refused_only(self):
        with mock.patch("installer.apps.call", self.fake_call(refuse=("jellyfin",), down=("qbittorrent",))):
            self.assertEqual(apps.check_logins(acct(), host_of), ["jellyfin"])

    def test_check_logins_qbittorrent_refused(self):
        with mock.patch("installer.apps.call", self.fake_call(refuse=("qbittorrent",))):
            self.assertEqual(apps.check_logins(acct(), host_of), ["qbittorrent"])

    def run_change(self, new, current=None, refuse=()):
        arrs = {}

        def make_arr(name, host, version, compose_out):
            arrs[name] = FakeArr(name)
            return arrs[name]
        self.waited = []
        with mock.patch("installer.apps.call", self.fake_call(refuse=refuse)), \
                mock.patch("installer.apps.Arr", make_arr), \
                mock.patch("installer.apps.wait_for", lambda url, **k: self.waited.append(url)), \
                mock.patch("installer.apps.read_when", lambda *a, **k: "bkey"):
            failed = apps.change_account(acct(), new, host_of, lambda *a: "", current or {}, lambda app: None)
        return failed, arrs

    def test_change_account_everywhere(self):
        failed, arrs = self.run_change(acct(password="new-pass1"))
        self.assertEqual(failed, [])
        for name in ("radarr", "sonarr", "prowlarr"):
            put = [c for c in arrs[name].calls if c[0] == "PUT"][0]
            self.assertEqual((put[2]["username"], put[2]["password"]), ("admin", "new-pass1"))
        bazarr = next(c for c in self.seen if c[1].endswith("/api/system/settings"))
        self.assertEqual(bazarr[2]["settings-auth-password"], "new-pass1")
        prefs = next(c for c in self.seen if c[1].endswith("/app/setPreferences"))
        self.assertIn("new-pass1", prefs[2]["json"])
        pw = next(c for c in self.seen if c[1].endswith("/Users/u1/Password"))
        self.assertEqual(pw[2], {"CurrentPw": "old-pass1", "NewPw": "new-pass1"})

    def test_change_account_reports_failures(self):
        failed, _ = self.run_change(acct(password="new-pass1"), refuse=("bazarr",))
        self.assertEqual(failed, ["bazarr"])

    def test_change_account_uses_given_current_password(self):
        self.run_change(acct(password="new-pass1"), current={"jellyfin": "typed"})
        login = next(c for c in self.seen if c[1].endswith("/Users/AuthenticateByName"))
        self.assertEqual(login[2]["Pw"], "typed")

    def test_jellyfin_rename_only_when_username_changes(self):
        self.run_change(acct(password="new-pass1"))
        self.assertFalse([c for c in self.seen if c[0] == "POST" and c[1].endswith("/Users/u1")])
        self.run_change(acct(username="bob"))
        rename = next(c for c in self.seen if c[0] == "POST" and c[1].endswith("/Users/u1"))
        self.assertEqual(rename[2]["Name"], "bob")
        self.assertFalse([c for c in self.seen if c[1].endswith("/Password")])  # same password: left alone


class AccountWaitTest(AccountTest):
    def test_change_account_waits_for_each_app(self):
        # A time zone or folder change has just recreated them: they may still be starting.
        self.run_change(acct(password="new-pass1"))
        for app in ("bazarr", "qbittorrent", "jellyfin"):
            self.assertTrue(any(f"//{app}:" in url for url in self.waited), app)
