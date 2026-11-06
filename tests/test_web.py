import http.client
import json
import threading
import time
import unittest
from types import SimpleNamespace

from installer import web
from installer.flow import Progress
from installer.settings import Settings

ENV = {"HOST_HOME": "/home/zoe", "HOST_PLATFORM": "linux", "PUID": "1000", "PGID": "1000"}
FORM = {"data_dir": "/home/zoe/Plaid", "username": "zoe", "password": "secret-pass 'é\"", "vpn_provider": None,
        "vpn_fields": {}, "timezone": "Europe/Paris", "lang": "fr"}


class FakeFlow:
    def __init__(self, installed=None, complete=True):
        self.installed = installed
        self.complete = complete
        self.uninstalled = None

    def is_complete(self):
        return self.installed is not None and self.complete

    def load_settings(self):
        return self.installed

    def preflight(self, s):
        return ["warn_no_vpn"]

    def install(self, s, report):
        report(Progress("step_prepare"))
        self.installed = s
        report(Progress("step_done", done=True))
        return {"jellyfin": 8096, "seerr": 5055}

    def uninstall(self, remove_media, report):
        self.uninstalled = remove_media

    def media_size(self, s):
        return 412 * 1024 ** 3


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
        _, _, html = self.req("GET", "/", headers={"Cookie": cookie})
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
        self.assertNotIn('class="app"', html)  # no app links (texts JSON may mention ports)
        self.assertIn('name="remove_media"', html)

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
        _, _, html = self.req("GET", "/", headers={"Cookie": headers["Set-Cookie"].split(";")[0]})
        self.assertIn("http://plaid:8096", html)
        self.assertNotIn("tskey-auth-x", html)
