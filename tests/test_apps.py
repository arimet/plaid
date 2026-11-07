import json
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
        pw = next(c for c in self.seen if c[1].endswith("/Users/Password?userId=u1"))  # Jellyfin 10.9+ routes
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
        self.assertFalse([c for c in self.seen if c[0] == "POST" and c[1].endswith("/Users?userId=u1")])
        self.run_change(acct(username="bob"))
        rename = next(c for c in self.seen if c[0] == "POST" and c[1].endswith("/Users?userId=u1"))
        self.assertEqual(rename[2]["Name"], "bob")
        self.assertFalse([c for c in self.seen if "/Password" in c[1]])  # same password: left alone


class AccountWaitTest(AccountTest):
    def test_change_account_waits_for_each_app(self):
        # A time zone or folder change has just recreated them: they may still be starting.
        self.run_change(acct(password="new-pass1"))
        for app in ("bazarr", "qbittorrent", "jellyfin"):
            self.assertTrue(any(f"//{app}:" in url for url in self.waited), app)


# ---------------------------------------------------------------- languages and quality

import copy as _copy
import re as _re

from tests.test_media import RADARR_SCHEMA, SONARR_SCHEMA


class MediaArr(FakeArr):
    """Radarr/Sonarr's formats and profiles: IDs that answer 404 (deleted by hand) or refuse deletion (in use)."""
    def __init__(self, name, profiles=(), missing=(), in_use=()):
        super().__init__(name)
        self.next_id = 100
        self.profiles = {p["id"]: dict(p) for p in profiles}
        self.missing, self.in_use = set(missing), set(in_use)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/qualityprofile/schema":
            return _copy.deepcopy(RADARR_SCHEMA if self.name == "radarr" else SONARR_SCHEMA)
        if path == "/qualityprofile" and method == "GET":
            return list(self.profiles.values())
        kind, pid = _re.fullmatch(r"/(customformat|qualityprofile)(?:/(\d+))?", path).groups()
        pid = int(pid) if pid else None
        if method == "POST":
            self.next_id += 1
            if kind == "qualityprofile":
                self.profiles[self.next_id] = {**body, "id": self.next_id}
            return {**body, "id": self.next_id}
        if pid in self.missing:
            raise apps.HttpError(404, path, "")
        if method == "PUT":
            if kind == "qualityprofile":
                self.profiles[pid] = body
            return body
        if method == "DELETE":
            if pid in self.in_use:
                raise apps.HttpError(400, path, "in use")
            self.profiles.pop(pid, None)
        if method == "GET":
            return self.profiles.get(pid, {"id": pid, "name": f"profile {pid}"})


def chosen(**kw):
    base = dict(audio_lang="fr", profiles=["mine-1080p"], default_profile="mine-1080p", subtitle_langs=["fr"],
                metadata_lang="fr", lang="fr")
    base.update(kw)
    return acct(**base)


def posts(app, kind):
    return [c for c in app.calls if c[0] == "POST" and c[1] == f"/{kind}"]


class EnsureMediaTest(unittest.TestCase):
    def test_ensure_media_creates_formats_then_profiles(self):
        app = MediaArr("radarr")
        ids, kept = app.ensure_media(chosen(profiles=["mine-1080p", "orig-4k"]), {})
        self.assertEqual(len(posts(app, "customformat")), 4)
        profiles = posts(app, "qualityprofile")
        self.assertEqual([p[2]["name"] for p in profiles], ["Français · 1080p", "VO sous-titrée · 4K"])
        self.assertLess(app.calls.index(posts(app, "customformat")[-1]), app.calls.index(profiles[0]))
        self.assertEqual({f["format"] for f in profiles[0][2]["formatItems"]}, set(ids["formats"].values()))
        self.assertEqual(sorted(ids["profiles"]), ["mine-1080p", "orig-4k"])
        self.assertEqual(kept, [])

    def test_ensure_media_updates_by_stored_id(self):
        app = MediaArr("sonarr", profiles=[{"id": 7, "name": "Français · 1080p"}])
        stored = {"formats": {"lang": 1, "original": 2, "multi": 3, "subbed": 4}, "profiles": {"mine-1080p": 7}}
        ids, _ = app.ensure_media(chosen(audio_lang="de"), stored)
        self.assertFalse([c for c in app.calls if c[0] == "POST"])
        self.assertIn(("PUT", "/customformat/1"), [(c[0], c[1]) for c in app.calls])
        put = next(c for c in app.calls if c[:2] == ("PUT", "/qualityprofile/7"))
        self.assertEqual((put[2]["name"], put[2]["id"]), ("Allemand · 1080p", 7))
        self.assertEqual(ids, stored)

    def test_missing_stored_id_is_recreated(self):
        app = MediaArr("radarr", missing=[7])
        ids, _ = app.ensure_media(chosen(), {"formats": {}, "profiles": {"mine-1080p": 7}})
        self.assertNotEqual(ids["profiles"]["mine-1080p"], 7)
        self.assertEqual(len(posts(app, "qualityprofile")), 1)

    def test_unticked_profile_deleted(self):
        app = MediaArr("radarr", profiles=[{"id": 8, "name": "VO sous-titrée · 4K"}])
        ids, kept = app.ensure_media(chosen(), {"formats": {}, "profiles": {"orig-4k": 8}})
        self.assertIn(("DELETE", "/qualityprofile/8", None), app.calls)
        self.assertNotIn("orig-4k", ids["profiles"])
        self.assertEqual(kept, [])

    def test_in_use_profile_is_kept(self):
        app = MediaArr("radarr", profiles=[{"id": 8, "name": "VO sous-titrée · 4K"}], in_use=[8])
        ids, kept = app.ensure_media(chosen(), {"formats": {}, "profiles": {"orig-4k": 8}})
        self.assertEqual(kept, ["VO sous-titrée · 4K"])
        self.assertEqual(ids["profiles"]["orig-4k"], 8)  # tried again on the next change

    def test_builtin_profiles_removed_on_install(self):
        app = MediaArr("radarr", profiles=[{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}], in_use=[1])
        ids, _ = app.ensure_media(chosen(), {}, builtin=True)
        deleted = [c[1] for c in app.calls if c[0] == "DELETE"]
        self.assertEqual(deleted, ["/qualityprofile/1", "/qualityprofile/4"])  # failure on 1 ignored
        self.assertNotIn(f"/qualityprofile/{ids['profiles']['mine-1080p']}", deleted)

    def test_existing_install_keeps_builtins(self):
        app = MediaArr("radarr", profiles=[{"id": 4, "name": "HD-1080p"}])
        app.ensure_media(chosen(), {})
        self.assertFalse([c for c in app.calls if c[0] == "DELETE"])


class MediaAppsTest(unittest.TestCase):
    def fake_call(self, servers=None, libraries=()):
        self.seen = []

        def call(method, url, body=None, headers=None, form=False):
            self.seen.append((method, url, body))
            if url.endswith("/Users/AuthenticateByName"):
                return {"AccessToken": "t", "User": {"Id": "u1"}}
            if url.endswith("/System/Configuration") and method == "GET":
                return {"UICulture": "fr-FR", "PreferredMetadataLanguage": "fr"}
            if url.endswith("/Library/VirtualFolders"):
                return [dict(lib) for lib in libraries]
            for app in ("radarr", "sonarr"):
                if url.endswith(f"/settings/{app}") and method == "GET":
                    return [dict(x) for x in (servers or {}).get(app, [])]
        return call

    def patched(self, call):
        return (mock.patch("installer.apps.call", call), mock.patch("installer.apps.wait_for", lambda *a, **k: None),
                mock.patch("installer.apps.read_when", lambda *a, **k: "bkey"))

    def run_patched(self, call, action):
        a, b, c = self.patched(call)
        with a, b, c:
            action()

    def test_seerr_default_profile(self):
        servers = {"radarr": [{"id": 0, "activeProfileId": 4}], "sonarr": [{"id": 0, "activeProfileId": 4}]}
        ids = {"radarr": {"profiles": {"orig-4k": 21, "mine-1080p": 20}},
               "sonarr": {"profiles": {"orig-4k": 31, "mine-1080p": 30}}}
        s = chosen(profiles=["mine-1080p", "orig-4k"], default_profile="orig-4k")
        self.run_patched(self.fake_call(servers), lambda: apps.seerr_default(s, acct(username="old", password="pw"),
                                                                               "seerr:5055", ids))
        login = next(c for c in self.seen if c[1].endswith("/auth/jellyfin"))
        self.assertEqual(login[2], {"username": "old", "password": "pw"})
        radarr = next(c for c in self.seen if c[0] == "PUT" and c[1].endswith("/settings/radarr/0"))
        self.assertEqual((radarr[2]["activeProfileId"], radarr[2]["activeProfileName"]), (21, "VO sous-titrée · 4K"))
        sonarr = next(c for c in self.seen if c[0] == "PUT" and c[1].endswith("/settings/sonarr/0"))
        self.assertEqual((sonarr[2]["activeProfileId"], sonarr[2]["activeAnimeProfileId"]), (31, 31))
        self.assertNotIn("id", radarr[2])  # Seerr refuses its read-only id in the body (HTTP 400, seen live)

    def test_subtitle_languages(self):
        self.run_patched(self.fake_call(), lambda: apps.set_subtitles(chosen(subtitle_langs=["de", "fr"]),
                                                                      "bazarr:6767", lambda *a: ""))
        form = next(c for c in self.seen if c[1].endswith("/api/system/settings"))[2]
        self.assertEqual(form["languages-enabled"], ["de", "fr"])
        profile = json.loads(form["languages-profiles"])[0]
        self.assertEqual([i["language"] for i in profile["items"]], ["de", "fr"])
        self.assertIsNone(profile["cutoff"])
        self.assertNotIn("settings-auth-password", form)  # only the languages change

    def test_metadata_language(self):
        libs = [{"Name": "Films", "ItemId": "m1", "CollectionType": "movies", "LibraryOptions": {"Enabled": True}},
                {"Name": "Séries", "ItemId": "t1", "CollectionType": "tvshows", "LibraryOptions": {}}]
        self.run_patched(self.fake_call(libraries=libs),
                         lambda: apps.set_metadata_lang(chosen(metadata_lang="es"), acct(), "jellyfin:8096"))
        conf = next(c for c in self.seen if c[0] == "POST" and c[1].endswith("/System/Configuration"))
        self.assertEqual((conf[2]["PreferredMetadataLanguage"], conf[2]["MetadataCountryCode"], conf[2]["UICulture"]),
                         ("es", "ES", "fr-FR"))
        options = [c[2] for c in self.seen if c[1].endswith("/Library/VirtualFolders/LibraryOptions")]
        self.assertEqual([(o["Id"], o["LibraryOptions"]["PreferredMetadataLanguage"]) for o in options],
                         [("m1", "es"), ("t1", "es")])
        self.assertTrue(options[0]["LibraryOptions"]["Enabled"])  # the rest of the options is kept
        refresh = [c[1] for c in self.seen if "/Refresh?" in c[1]]
        self.assertEqual(len(refresh), 2)
        self.assertIn("replaceAllMetadata=true", refresh[0])
