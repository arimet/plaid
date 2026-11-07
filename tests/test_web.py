import http.client
import json
import threading
import time
import unittest
from types import SimpleNamespace

from installer import web
from installer.flow import Progress
from installer.settings import Settings

from tests.test_reconfigure import MEDIA
from installer import media
from installer.i18n import t

ENV = {"HOST_HOME": "/home/zoe", "HOST_PLATFORM": "linux", "PUID": "1000", "PGID": "1000"}
FORM = {"data_dir": "/home/zoe/Plaid", "username": "zoe", "password": "secret-pass 'é\"", "vpn_provider": None,
        "vpn_fields": {}, "timezone": "Europe/Paris", "lang": "fr"}


class FakeFlow:
    def __init__(self, installed=None, complete=True):
        self.installed = installed
        self.complete = complete
        self.uninstalled = None
        self.status = {"disk": "ok", "vpn": "off"}
        self.confirmed = False

    def guard_status(self):
        return self.status

    found = []
    refused = []
    started = None
    apply_state = {"state": "idle", "events": []}

    def settings_check(self, old, new):
        return list(self.found)

    def check_logins(self, s):
        return list(self.refused)

    def start_apply(self, new, current):
        self.started, self.current = new, current

    def apply_status(self):
        return self.apply_state

    def confirm_disk(self, s):
        self.confirmed = True

    def is_complete(self):
        return self.installed is not None and self.complete

    def load_settings(self):
        return self.installed

    def preflight(self, s):
        return ["warn_no_vpn"]

    def install(self, s, report, cancel=None):
        report(Progress("step_prepare"))
        self.installed = s
        report(Progress("step_done", done=True))
        return {"jellyfin": 8096, "seerr": 5055}

    def uninstall(self, remove_media, report):
        self.uninstalled = remove_media

    def media_size(self, s):
        return 412 * 1024 ** 3

    updated = None
    update_started = False

    def last_update(self):
        return self.updated

    def start_update(self):
        self.update_started = True


def settings():
    return Settings(home="/home/zoe", platform="linux", **dict(FORM, lang="fr"))


class WebTest(unittest.TestCase):
    def start(self, flow):
        self.server = web.make_server(0, flow, ENV)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body) if isinstance(body, dict) else body
        c.request(method, path, body=data, headers=headers or {})
        r = c.getresponse()
        result = r.status, dict(r.getheaders()), r.read().decode()
        c.close()
        return result

    def test_form_fr(self):
        self.start(FakeFlow())
        status, _, html = self.req("GET", "/?lang=fr")
        self.assertEqual(status, 200)
        self.assertIn("Dossier des médias", html)
        self.assertNotIn("Media folder", html)
        self.assertIn("/home/zoe/Plaid", html)

    def test_form_language_from_header(self):
        self.start(FakeFlow())
        _, _, html = self.req("GET", "/", headers={"Accept-Language": "en-GB,en;q=0.9"})
        self.assertIn("Media folder", html)

    def test_auth_after_install(self):
        self.start(FakeFlow(installed=settings()))
        status, headers, _ = self.req("GET", "/")
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        status, _, _ = self.req("POST", "/login", "username=zoe&password=wrong",
                                {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 401)
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        status, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, html = self.req("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(":8096", html)

    def test_preflight_texts(self):
        self.start(FakeFlow())
        status, _, body = self.req("POST", "/api/preflight", dict(FORM, password="short"))
        data = json.loads(body)
        self.assertEqual(data["errors"][0]["key"], "err_password_short")
        self.assertIn("8 caractères", data["errors"][0]["text"])
        status, _, body = self.req("POST", "/api/preflight", FORM)
        self.assertEqual([w["key"] for w in json.loads(body)["warnings"]], ["warn_no_vpn"])

    def test_install_flow(self):
        flow = FakeFlow()
        self.start(flow)
        status, headers, _ = self.req("POST", "/api/install", FORM)
        self.assertEqual(status, 202)
        cookie = headers["Set-Cookie"].split(";")[0]
        for _ in range(50):
            _, _, body = self.req("GET", "/api/progress", headers={"Cookie": cookie})
            if json.loads(body)["done"]:
                break
            time.sleep(0.05)
        self.assertTrue(json.loads(body)["done"])
        status, _, html = self.req("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(":8096", html)
        self.assertIn(":5055", html)
        self.assertIn("Internet Archive", html)
        self.assertNotIn("secret-pass", html)

    def test_install_rejects_invalid(self):
        self.start(FakeFlow())
        status, _, _ = self.req("POST", "/api/install", dict(FORM, password="x"))
        self.assertEqual(status, 400)

    def test_uninstall_checkbox_default(self):
        flow = FakeFlow(installed=settings())
        self.start(flow)
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        cookie = headers["Set-Cookie"].split(";")[0]
        _, _, html = self.req("GET", "/remove", headers={"Cookie": cookie})
        self.assertIn('name="remove_media"', html)
        self.assertNotIn("checked", html)
        self.assertIn("/home/zoe/Plaid", html)
        self.assertIn("412", html)
        status, _, _ = self.req("POST", "/api/uninstall", {"remove_media": False}, {"Cookie": cookie})
        self.assertEqual(status, 202)
        self.assertIs(flow.uninstalled, False)

    def test_failed_install_panel_offers_removal_only(self):
        self.start(FakeFlow(installed=settings(), complete=False))
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        _, _, html = self.req("GET", "/", headers={"Cookie": headers["Set-Cookie"].split(";")[0]})
        self.assertIn("L’installation s’est arrêtée", html)
        self.assertNotIn('class="app', html.split('<script id="texts"')[0])  # no app links
        self.assertIn('href="/remove"', html)

    def test_reload_during_install_shows_progress(self):
        flow = FakeFlow()
        flow.install = lambda s, report: time.sleep(1)
        self.start(flow)
        _, headers, _ = self.req("POST", "/api/install", FORM)
        _, _, html = self.req("GET", "/", headers={"Cookie": headers["Set-Cookie"].split(";")[0]})
        self.assertIn('data-running="1"', html)

    def test_uninstall_requires_login(self):
        self.start(FakeFlow(installed=settings()))
        status, _, _ = self.req("POST", "/api/uninstall", {"remove_media": True})
        self.assertEqual(status, 401)

    def test_static(self):
        self.start(FakeFlow())
        status, headers, _ = self.req("GET", "/static/style.css")
        self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "text/css"))
        self.assertEqual(self.req("GET", "/static/../web.py")[0], 404)


if __name__ == "__main__":
    unittest.main()


class TailscaleWebTest(WebTest):
    def test_field_on_linux_only(self):
        self.start(FakeFlow())
        _, _, html = self.req("GET", "/?lang=fr")
        self.assertIn('id="tailscale_key"', html)
        self.assertIn("login.tailscale.com", html)

    def test_no_field_on_mac(self):
        self.server = web.make_server(0, FakeFlow(), dict(ENV, HOST_PLATFORM="mac"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        _, _, html = self.req("GET", "/?lang=fr")
        self.assertNotIn('id="tailscale_key"', html)

    def test_panel_shows_remote_addresses(self):
        s = settings()
        s.tailscale_key = "tskey-auth-x"
        self.start(FakeFlow(installed=s))
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        _, _, html = self.req("GET", "/remote", headers={"Cookie": headers["Set-Cookie"].split(";")[0]})
        self.assertIn("http://plaid:8096", html)
        self.assertNotIn("tskey-auth-x", html)


class ReviewFixesWebTest(WebTest):
    def login(self):
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        return headers["Set-Cookie"].split(";")[0]

    def test_install_refused_over_existing_stack(self):
        self.start(FakeFlow(installed=settings()))
        status, _, _ = self.req("POST", "/api/install", FORM, {"Cookie": self.login()})
        self.assertEqual(status, 409)

    def test_install_runs_blocking_preflight(self):
        flow = FakeFlow()
        flow.preflight = lambda s: ["err_stack_exists"]
        self.start(flow)
        status, _, _ = self.req("POST", "/api/install", FORM)
        self.assertEqual(status, 409)
        self.assertIsNone(flow.installed)

    def test_cross_origin_post_refused(self):
        self.start(FakeFlow(installed=settings()))
        cookie = self.login()
        status, _, _ = self.req("POST", "/api/uninstall", {"remove_media": True},
                                {"Cookie": cookie, "Origin": "http://localhost:8096"})
        self.assertEqual(status, 403)

    def test_uninstall_status_route(self):
        flow = FakeFlow(installed=settings())
        flow.uninstall_status = lambda: ("failed", "boom")
        self.start(flow)
        _, _, body = self.req("GET", "/api/uninstall-status", headers={"Cookie": self.login()})
        self.assertEqual(json.loads(body)["state"], "failed")
        self.assertIn("boom", json.loads(body)["detail"])


class TrackersWebTest(WebTest):
    def login(self):
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        return headers["Set-Cookie"].split(";")[0]

    def flow(self):
        flow = FakeFlow(installed=settings())
        flow.added = []
        flow.tracker_catalog = lambda: [{"id": "alpha", "name": "Alpha", "language": "en-US", "description": "a",
                                         "added": False}]

        def add(definition):
            if definition != "alpha":
                raise KeyError(definition)
            flow.added.append(definition)
        flow.add_tracker = add
        return flow

    def test_catalog_needs_login(self):
        self.start(self.flow())
        self.assertEqual(self.req("GET", "/api/trackers")[0], 401)
        _, _, body = self.req("GET", "/api/trackers", headers={"Cookie": self.login()})
        self.assertEqual(json.loads(body)["trackers"][0]["id"], "alpha")

    def test_add_tracker(self):
        flow = self.flow()
        self.start(flow)
        cookie = self.login()
        self.assertEqual(self.req("POST", "/api/trackers", {"id": "alpha"}, {"Cookie": cookie})[0], 200)
        self.assertEqual(flow.added, ["alpha"])
        self.assertEqual(self.req("POST", "/api/trackers", {"id": "club"}, {"Cookie": cookie})[0], 404)
        self.assertEqual(self.req("POST", "/api/trackers", {"id": "alpha"})[0], 401)

    def test_panel_has_tracker_picker(self):
        self.start(self.flow())
        _, _, html = self.req("GET", "/trackers?lang=fr", headers={"Cookie": self.login()})
        self.assertIn('id="tracker-search"', html)


class BeginnerPanelTest(WebTest):
    def test_daily_apps_first_with_explanations(self):
        self.start(FakeFlow(installed=settings()))
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        _, _, html = self.req("GET", "/?lang=fr", headers={"Cookie": headers["Set-Cookie"].split(";")[0]})
        main = html.split('<script id="texts"')[0]
        link = lambda svc: main.index(f"/static/icons/{svc}.svg")
        self.assertLess(main.index("Au quotidien"), link("jellyfin"))
        self.assertLess(link("jellyfin"), link("seerr"))
        self.assertLess(link("seerr"), main.index("En coulisses"))
        self.assertLess(main.index("En coulisses"), link("radarr"))
        self.assertIn("Netflix", main)  # Jellyfin explained in plain words


class PanelPagesTest(WebTest):
    def login(self):
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        return {"Cookie": headers["Set-Cookie"].split(";")[0]}

    def test_main_page_keeps_only_the_apps(self):
        self.start(FakeFlow(installed=settings()))
        _, _, html = self.req("GET", "/?lang=fr", headers=self.login())
        main = html.split('<script id="texts"')[0]
        for absent in ('id="tracker-search"', 'name="remove_media"', "Disable key expiry", "tailscale.com/download"):
            self.assertNotIn(absent, main)
        for link in ('href="/remote"', 'href="/trackers"', 'href="/remove"'):
            self.assertIn(link, main)

    def test_pages_need_login_and_link_back(self):
        self.start(FakeFlow(installed=settings()))
        for path in ("/remote", "/trackers", "/remove"):
            self.assertEqual(self.req("GET", path)[0], 303, path)
            status, _, html = self.req("GET", path + "?lang=fr", headers=self.login())
            self.assertEqual(status, 200, path)
            self.assertIn('class="back" href="/"', html)


class HeaderMenuTest(PanelPagesTest):
    def test_links_live_in_a_header_menu(self):
        self.start(FakeFlow(installed=settings()))
        _, _, html = self.req("GET", "/?lang=fr", headers=self.login())
        header = html.split('<main id="main">')[0]
        for link in ('href="/remote"', 'href="/trackers"', 'href="/remove"'):
            self.assertIn(link, header)
        self.assertNotIn("<h2>Pour aller plus loin", html)  # no section: only the button's accessible name

    def test_no_menu_before_install(self):
        self.start(FakeFlow())
        _, _, html = self.req("GET", "/?lang=fr")
        self.assertNotIn('class="menu"', html)


class FolderWarningTest(WebTest):
    def test_warning_names_the_folder(self):
        flow = FakeFlow()
        flow.preflight = lambda s: ["warn_dir_not_empty"]
        self.start(flow)
        _, _, body = self.req("POST", "/api/preflight", FORM)
        self.assertIn("/home/zoe/Plaid", json.loads(body)["warnings"][0]["text"])


class FaviconTest(WebTest):
    def test_tartan_favicon(self):
        self.start(FakeFlow())
        _, _, html = self.req("GET", "/")
        self.assertIn('<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">', html)
        status, headers, body = self.req("GET", "/static/favicon.svg")
        self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "image/svg+xml"))
        self.assertIn("<svg", body)


class LanAddressTest(WebTest):
    def panel(self, env, host_header):
        self.server = web.make_server(0, FakeFlow(installed=settings()), env)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        cookie = headers["Set-Cookie"].split(";")[0]
        return self.req("GET", "/", headers={"Cookie": cookie, "Host": host_header})[2]

    def test_localhost_shows_the_machine_address(self):
        # opened on the machine itself: links must still work from the TV or the phone
        html = self.panel(dict(ENV, HOST_ADDR="192.168.1.20"), "localhost:7575")
        self.assertIn('href="http://192.168.1.20:8096"', html)

    def test_address_typed_in_the_browser_wins(self):
        # from another computer or through Tailscale, the address that reached us works for sure
        html = self.panel(dict(ENV, HOST_ADDR="192.168.1.20"), "plaid:7575")
        self.assertIn('href="http://plaid:8096"', html)

    def test_localhost_without_known_address(self):
        html = self.panel(dict(ENV, HOST_ADDR="localhost"), "localhost:7575")
        self.assertIn('href="http://localhost:8096"', html)


class GuardWebTest(WebTest):
    def open_panel(self, **status):
        self.flow = FakeFlow(installed=settings())
        self.flow.status = dict(self.flow.status, **status)
        self.start(self.flow)
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        self.cookie = headers["Set-Cookie"].split(";")[0]
        return self.req("GET", "/", headers={"Cookie": self.cookie})[2]

    def test_panel_warns_when_disk_missing(self):
        html = self.open_panel(disk="missing")
        self.assertIn("<b>Disque des médias introuvable</b>", html)
        self.assertIn("Plaid l’attend ici : /home/zoe/Plaid.", html)
        self.assertNotIn("/api/confirm-disk", html)

    def test_panel_asks_to_confirm_unconfirmed_disk(self):
        html = self.open_panel(disk="unconfirmed")
        self.assertIn("<b>Ce dossier contient-il vos médias ?</b>", html)
        self.assertIn('action="/api/confirm-disk"', html)

    def test_alerts_use_the_alert_style(self):
        self.assertIn('<div class="note alert"><div><b>Disque des médias introuvable</b>', self.open_panel(disk="missing"))

    def test_stopped_apps_are_not_called_ready(self):
        html = self.open_panel(disk="missing")
        self.assertIn("<h1>Votre stack</h1>", html)
        self.assertNotIn('<p class="lead">Vos sept applis tournent', html)
        self.server.shutdown()
        html = self.open_panel(vpn="down")  # downloads wait, but every app runs
        self.assertIn("<h1>Tout est prêt</h1>", html)

    def test_panel_warns_when_vpn_down(self):
        self.assertIn("<b>VPN déconnecté</b>", self.open_panel(vpn="down"))

    def test_panel_quiet_when_all_ok(self):
        for status in ({"disk": "ok", "vpn": "off"}, {"disk": "unknown", "vpn": "ok"}):
            html = self.open_panel(**status)
            self.assertNotIn('<div class="note alert"><div><b>', html)  # texts also sit in the page's JSON
            self.server.shutdown()

    def test_confirm_disk_route(self):
        self.open_panel(disk="unconfirmed")
        status, headers, _ = self.req("POST", "/api/confirm-disk", "",
                                      {"Cookie": self.cookie, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((status, headers["Location"]), (303, "/"))
        self.assertTrue(self.flow.confirmed)

    def test_confirm_disk_needs_login(self):
        self.open_panel(disk="unconfirmed")
        status, _, _ = self.req("POST", "/api/confirm-disk", "")
        self.assertEqual(status, 401)
        self.assertFalse(self.flow.confirmed)


import urllib.parse
from dataclasses import replace


class SettingsWebTest(WebTest):
    FORM_CT = {"Content-Type": "application/x-www-form-urlencoded"}

    def open(self, **changes):
        self.flow = FakeFlow(installed=replace(settings(), **changes))
        self.start(self.flow)
        form = "username=zoe&password=" + "secret-pass%20%27%C3%A9%22"
        _, headers, _ = self.req("POST", "/login", form, self.FORM_CT)
        self.cookie = headers["Set-Cookie"].split(";")[0]

    def post(self, path, data):
        return self.req("POST", path, urllib.parse.urlencode(data), {**self.FORM_CT, "Cookie": self.cookie})

    def get(self, path):
        return self.req("GET", path, headers={"Cookie": self.cookie})

    def test_settings_needs_login(self):
        self.start(FakeFlow(installed=settings()))
        status, headers, _ = self.req("GET", "/settings")
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        self.assertEqual(self.req("POST", "/settings/apply", "lang=en", self.FORM_CT)[0], 401)

    def test_settings_form_hides_secrets(self):
        self.open(vpn_provider="protonvpn", vpn_fields={"WIREGUARD_PRIVATE_KEY": "k-secret-123"},
                  tailscale_key="tskey-auth-secret9")
        status, _, html = self.get("/settings")
        self.assertEqual(status, 200)
        for secret in ("k-secret-123", "tskey-auth-secret9", "secret-pass"):
            self.assertNotIn(secret, html)
        self.assertIn('value="/home/zoe/Plaid"', html)
        self.assertIn('name="remove_tailscale"', html)

    def test_settings_nothing_changed(self):
        self.open()
        _, _, html = self.post("/settings", {"lang": "fr", "data_dir": "/home/zoe/Plaid", "username": "zoe",
                                             "timezone": "Europe/Paris", "vpn_provider": ""})
        self.assertIn("Rien n’a changé", html)

    def test_settings_summary_lists_changes(self):
        self.open()
        _, _, html = self.post("/settings", {"timezone": "America/Montreal", "vpn_provider": ""})
        self.assertIn('<span class="k">Fuseau horaire</span>', html)
        self.assertIn('<del>Europe/Paris</del>', html)  # before → after
        self.assertIn('<b>America/Montreal</b>', html)
        self.assertIn("Les applis sont indisponibles quelques dizaines de secondes.", html)
        self.assertIn('action="/settings/apply"', html)
        self.assertIn('name="timezone" value="America/Montreal"', html)

    def test_settings_blocking_error_has_no_apply(self):
        self.open()
        self.flow.found = ["err_folder_missing"]
        _, _, html = self.post("/settings", {"data_dir": "/media/usb", "vpn_provider": ""})
        self.assertIn("Dossier introuvable", html)
        self.assertNotIn("Appliquer les changements</button>", html)
        self.assertIn('formaction="/settings/edit"', html)  # going back to fix it stays possible

    def test_settings_passwords_must_match(self):
        self.open()
        _, _, html = self.post("/settings", {"password": "new-pass1", "password2": "other-pass", "vpn_provider": ""})
        self.assertIn("Les mots de passe diffèrent.", html)
        self.assertNotIn("Appliquer les changements</button>", html)

    def test_settings_asks_current_password(self):
        self.open()
        self.flow.refused = ["jellyfin"]
        _, _, html = self.post("/settings", {"password": "new-pass1", "password2": "new-pass1", "vpn_provider": ""})
        self.assertIn('name="current_jellyfin"', html)
        self.flow.refused = ["jellyfin"]
        _, _, html = self.post("/settings", {"timezone": "UTC", "vpn_provider": ""})  # account unchanged: no question
        self.assertNotIn('name="current_jellyfin"', html)

    def test_settings_apply_starts_worker(self):
        self.open()
        status, headers, _ = self.post("/settings/apply", {"timezone": "America/Montreal", "vpn_provider": "",
                                                           "current_jellyfin": "typed"})
        self.assertEqual((status, headers["Location"]), (303, "/settings/progress"))
        self.assertEqual(self.flow.started.timezone, "America/Montreal")
        self.assertEqual(self.flow.current, {"jellyfin": "typed"})

    def test_settings_apply_refuses_blocking_errors(self):
        self.open()
        self.flow.found = ["err_folder_missing"]
        status, _, _ = self.post("/settings/apply", {"data_dir": "/media/usb", "vpn_provider": ""})
        self.assertEqual(status, 409)
        self.assertIsNone(self.flow.started)

    def test_settings_progress_refreshes_while_running(self):
        self.open()
        self.flow.apply_state = {"state": "running", "events": [{"step": "step_compose"}]}
        _, _, html = self.get("/settings/progress")
        self.assertIn('http-equiv="refresh"', html)
        self.assertIn("Redémarrage des applis", html)
        self.flow.apply_state = {"state": "done", "events": [{"step": "step_done", "done": True, "detail": "jellyfin"}]}
        _, _, html = self.get("/settings/progress")
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertIn("Jellyfin", html)

    def test_menu_has_settings(self):
        self.open()
        self.assertIn('href="/settings"', self.get("/")[2])


class SettingsSummaryTest(SettingsWebTest):
    def test_password_row_never_shows_it(self):
        self.open()
        _, _, html = self.post("/settings", {"password": "new-pass1", "password2": "new-pass1", "vpn_provider": ""})
        self.assertIn('<span class="k">Mot de passe</span>', html)
        self.assertEqual(html.count("new-pass1"), 2)  # only the two hidden fields that carry it to Apply

    def test_edit_keeps_what_was_typed(self):
        self.open()
        _, _, html = self.post("/settings/edit", {"timezone": "America/Montreal", "data_dir": "/srv/new",
                                                  "vpn_provider": ""})
        self.assertIn('value="America/Montreal"', html)
        self.assertIn('value="/srv/new"', html)
        self.assertIn('action="/settings"', html)

    def test_summary_offers_edit(self):
        self.open()
        _, _, html = self.post("/settings", {"timezone": "America/Montreal", "vpn_provider": ""})
        self.assertIn('formaction="/settings/edit"', html)


class ManualPasswordTest(SettingsWebTest):
    def test_done_lists_apps_to_change_by_hand_with_links(self):
        self.open()
        self.flow.apply_state = {"state": "done", "events": [
            {"step": "step_account"}, {"step": "step_done", "done": True, "detail": "jellyfin,qbittorrent"}]}
        _, _, html = self.get("/settings/progress")
        self.assertIn('<ul class="manual">', html)
        self.assertIn(':8096"', html)  # Jellyfin's address
        self.assertIn(':8080"', html)  # qBittorrent's
        self.assertEqual(html.count('<li class="app-row">'), 2)


class MediaWebTest(SettingsWebTest):
    def test_settings_page_has_media_group(self):
        self.open()  # installed before these choices existed: the defaults show
        _, _, html = self.get("/settings")
        self.assertEqual(html.count('name="profile_'), 9)
        self.assertIn('name="profile_mine-1080p" checked', html)
        self.assertIn('name="default_profile" value="mine-1080p" checked', html)
        self.assertNotIn('name="profile_orig-4k" checked', html)
        self.assertIn('name="sub_fr" checked', html)
        self.assertIn('name="sub_en" checked', html)
        self.assertIn('<option value="fr" selected>Français</option>', html)

    def test_settings_data_collects_lists(self):
        data = web.settings_data({"media": "1", "profile_orig-4k": "on", "profile_mine-1080p": "on",
                                  "default_profile": "orig-4k", "sub_fr": "on", "audio_lang": "de",
                                  "metadata_lang": "es"})
        self.assertEqual((data["profiles"], data["default_profile"], data["subtitle_langs"], data["audio_lang"],
                          data["metadata_lang"]), (["mine-1080p", "orig-4k"], "orig-4k", ["fr"], "de", "es"))
        self.assertNotIn("profiles", web.settings_data({"timezone": "UTC"}))  # no media group: nothing to change
        self.assertEqual(web.settings_data({"media": "1"})["profiles"], [])  # all unticked: refused later

    def test_summary_refuses_default_not_ticked(self):
        self.open()
        self.flow.settings_check = lambda old, new: media.validate(new)
        _, _, html = self.post("/settings", {"media": "1", "profile_orig-4k": "on", "default_profile": "mine-1080p",
                                             "sub_fr": "on", "audio_lang": "fr", "metadata_lang": "fr"})
        self.assertIn(t("fr", "err_default_profile"), html)
        self.assertNotIn('<button class="btn primary" type="submit">', html)  # no Apply (its text is in the JSON)

    def test_summary_rows_for_profiles(self):
        self.open(**MEDIA)
        _, _, html = self.post("/settings", {"media": "1", "profile_mine-1080p": "on", "profile_multi-4k": "on",
                                             "default_profile": "multi-4k", "sub_fr": "on", "sub_en": "on",
                                             "audio_lang": "fr", "metadata_lang": "en"})
        self.assertIn("<del>Français · 1080p</del>", html)
        self.assertIn("<b>Français · 1080p, MULTi · 4K</b>", html)
        self.assertIn("<b>MULTi · 4K</b>", html)  # the default
        self.assertIn("<del>Français</del>", html)  # fiches before → after
        self.assertIn("<b>Anglais</b>", html)
        self.assertIn(t("fr", "outage_none"), html)

    def test_done_page_lists_kept_profiles(self):
        self.open()
        self.flow.apply_state = {"state": "done", "events": [
            {"step": "step_profiles"}, {"step": "step_done", "done": True, "detail": "", "kept": ["VO sous-titrée · 4K"]}]}
        _, _, html = self.get("/settings/progress")
        self.assertIn("VO sous-titrée · 4K", html)
        self.assertIn(t("fr", "settings_kept_help"), html)
        self.assertIn(t("fr", "settings_other_version"), html)

    def test_install_body_media_fields(self):
        flow = FakeFlow()
        seen = {}
        flow.install = lambda s, report, cancel=None: seen.update(s=s) or report(Progress("step_done", done=True))
        self.start(flow)
        body = dict(FORM, audio_lang="it", profiles=["orig-720p", "multi-1080p"], default_profile="multi-1080p",
                    subtitle_langs=["it", "en"], metadata_lang="it")
        self.assertEqual(self.req("POST", "/api/install", body)[0], 202)
        for _ in range(50):
            if "s" in seen:
                break
            time.sleep(0.05)
        s = seen["s"]
        self.assertEqual((s.audio_lang, s.profiles, s.default_profile, s.subtitle_langs, s.metadata_lang),
                         ("it", ["orig-720p", "multi-1080p"], "multi-1080p", ["it", "en"], "it"))

    def test_install_without_media_fields_gets_defaults(self):
        s = web.settings_from(dict(FORM), ENV)
        self.assertEqual((s.profiles, s.default_profile), (["mine-1080p"], "mine-1080p"))

    def test_wizard_has_media_step(self):
        self.start(FakeFlow())
        _, _, html = self.req("GET", "/?lang=fr")
        self.assertEqual(html.count('name="profile_'), 9)
        self.assertIn('data-last="5"', html)  # folder, account, VPN, Tailscale (Linux), films et séries


class PickerTest(unittest.TestCase):
    start, req = WebTest.start, WebTest.req
    DISKS = [{"path": "/", "name": "", "free": 5 * 10 ** 10, "total": 2 * 10 ** 11, "system": True},
             {"path": "/mnt/films", "name": "films", "free": 3 * 10 ** 12, "total": 4 * 10 ** 12, "system": False}]

    def flow(self, installed=None):
        flow = FakeFlow(installed=installed)
        flow.disks = lambda platform, home: self.seen.append((platform, home)) or self.DISKS
        flow.browse = lambda path, platform: self.seen.append((path, platform)) or {"path": path, "dirs": ["Movies"]}
        self.seen = []
        return flow

    def test_disks_api(self):
        self.start(self.flow())
        status, _, body = self.req("GET", "/api/disks")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"disks": self.DISKS, "default": "/home/zoe/Plaid", "desktop": False})
        self.assertEqual(self.seen, [("linux", "/home/zoe")])

    def test_browse_api(self):
        self.start(self.flow())
        status, _, body = self.req("GET", "/api/browse?path=%2Fmnt%2Fmy%20films")
        self.assertEqual((status, json.loads(body)["dirs"]), (200, ["Movies"]))
        self.assertEqual(self.seen, [("/mnt/my films", "linux")])

    def test_picker_is_locked_after_install(self):
        self.start(self.flow(installed=settings()))
        self.assertEqual(self.req("GET", "/api/disks")[0], 401)
        self.assertEqual(self.req("GET", "/api/browse?path=%2F")[0], 401)
        self.assertEqual(self.seen, [])

    def test_picker_markup(self):
        wizard = web.form_page("fr", ENV)
        self.assertIn('data-picker data-create="1"', wizard)
        self.assertIn("Choisir sur un disque", wizard)
        page = web.settings_page("fr", settings())
        self.assertIn("data-picker", page)
        self.assertNotIn('data-create="1"', page)  # settings take a folder as it is: they never make one


class StatusWebTest(unittest.TestCase):
    start, req = WebTest.start, WebTest.req
    open_panel = GuardWebTest.open_panel

    def test_panel_shows_each_app_state(self):
        html = self.open_panel(apps={"radarr": "running", "sonarr": "failing"})
        self.assertIn('data-app="radarr" data-state="running"', html)
        self.assertIn('data-app="sonarr" data-state="failing"', html)
        self.assertIn('data-app="bazarr" data-state="unknown"', html)  # the guard has not seen it yet
        self.assertIn("En panne", html)

    def test_panel_disk_room(self):
        html = self.open_panel(free=5 * 1024 ** 3, total=500 * 1024 ** 3)
        self.assertIn("5,0 Go libres sur 500 Go", html)
        self.assertIn("presque plein", html)
        self.server.shutdown()
        html = self.open_panel(free=None, total=None)  # a Mac external disk: room unknown, no bar
        self.assertNotIn('class="meter"', html)

    def test_status_api(self):
        self.open_panel(apps={"radarr": "running"})
        status, _, body = self.req("GET", "/api/status", headers={"Cookie": self.cookie})
        self.assertEqual((status, json.loads(body)["apps"]), (200, {"radarr": "running"}))
        self.assertEqual(self.req("GET", "/api/status")[0], 401)


class UpdateWebTest(unittest.TestCase):
    start, req = WebTest.start, WebTest.req
    open_panel = GuardWebTest.open_panel

    def test_panel_offers_the_update(self):
        html = self.open_panel()
        self.assertIn('action="/api/update"', html)
        self.assertIn("Pas encore mis à jour", html)
        self.server.shutdown()
        FakeFlow.updated = {"at": 1794050000, "updated": ["radarr"]}
        self.addCleanup(lambda: setattr(FakeFlow, "updated", None))
        self.assertIn("Dernière mise à jour le ", self.open_panel())

    def test_update_route(self):
        self.open_panel()
        status, headers, _ = self.req("POST", "/api/update", "", {"Cookie": self.cookie,
                                                                  "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((status, headers["Location"]), (303, "/settings/progress"))
        self.assertTrue(self.flow.update_started)

    def test_update_route_needs_login_and_waits_its_turn(self):
        self.open_panel()
        self.assertEqual(self.req("POST", "/api/update", "")[0], 401)
        self.flow.apply_state = {"state": "running", "events": []}
        self.req("POST", "/api/update", "", {"Cookie": self.cookie, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertFalse(self.flow.update_started)  # a settings change runs: its progress page, nothing new

    def test_update_progress_page(self):
        done = {"state": "done", "events": [{"step": "step_pull"}, {"step": "step_recreate"},
                                            {"step": "step_done", "done": True, "detail": "seerr,radarr"}]}
        html = web.settings_progress("fr", done)
        self.assertIn("Applis à jour", html)
        self.assertIn("Mises à jour : Seerr, Radarr.", html)
        none = {"state": "done", "events": [{"step": "step_pull"}, {"step": "step_done", "done": True, "detail": ""}]}
        self.assertIn("Tout était déjà à jour.", web.settings_progress("fr", none))
        failed = {"state": "failed", "events": [{"step": "step_pull", "error": "err_update_failed", "detail": "boom"}]}
        self.assertIn("La mise à jour n’a pas abouti", web.settings_progress("fr", failed))


class TorrentsWebTest(unittest.TestCase):
    start, req = WebTest.start, WebTest.req
    open_panel = GuardWebTest.open_panel
    FOUND = [{"hash": "aaa", "name": "Dune.<b>2024</b>.1080p", "size": 9 * 1024 ** 3}]

    def setUp(self):
        FakeFlow.finished_torrents = lambda flow, s: list(self.FOUND)
        FakeFlow.clean_torrents = lambda flow, s: self.cleaned.append(s) or 1
        self.cleaned = []
        self.addCleanup(lambda: (delattr(FakeFlow, "finished_torrents"), delattr(FakeFlow, "clean_torrents")))

    def test_panel_links_to_the_cleanup(self):
        self.assertIn('href="/torrents"', self.open_panel())

    def test_torrents_page_lists_them(self):
        self.open_panel()
        status, _, html = self.req("GET", "/torrents", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn("Dune.&lt;b&gt;2024&lt;/b&gt;.1080p", html)
        self.assertIn("9,0 Go", html)
        self.assertIn("Supprimer ces torrents (1)", html)

    def test_clean(self):
        self.open_panel()
        status, _, html = self.req("POST", "/torrents", "", {"Cookie": self.cookie,
                                                            "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((status, len(self.cleaned)), (200, 1))
        self.assertIn("Torrents supprimés : 1.", html)

    def test_needs_login(self):
        self.open_panel()
        self.assertEqual(self.req("GET", "/torrents")[0], 303)
        self.assertEqual(self.req("POST", "/torrents", "")[0], 401)
        self.assertEqual(self.cleaned, [])

    def test_apps_down(self):
        FakeFlow.finished_torrents = lambda flow, s: 1 / 0
        self.open_panel()
        _, _, html = self.req("GET", "/torrents", headers={"Cookie": self.cookie})
        self.assertIn("ne répond pas", html)
