import json
import os
import shutil
import subprocess
import tempfile
import unittest

from installer.compose import DISK_APPS, PORTS, SERVICES, render_compose
from installer.settings import Settings

SECRET_KEY = "k'ey\"$x é\\"  # ends with a backslash: broke the old .env quoting


def make(vpn=True, password="p@ss \"$x\" é"):
    return Settings(data_dir="/srv/arr", username="admin", password=password,
                    vpn_provider="protonvpn" if vpn else None,
                    vpn_fields={"WIREGUARD_PRIVATE_KEY": SECRET_KEY} if vpn else {},
                    timezone="Europe/Paris", lang="fr", puid=1001, pgid=1002, home="/home/z", platform="linux")


class ComposeTest(unittest.TestCase):
    def test_vpn_layout(self):
        c = render_compose(make(), "/srv/arr", has_dri=False)
        self.assertIn("gluetun", c["services"])
        self.assertEqual(c["services"]["qbittorrent"]["network_mode"], "service:gluetun")
        self.assertIn("8080:8080", c["services"]["gluetun"]["ports"])
        self.assertNotIn("ports", c["services"]["qbittorrent"])

    def test_no_vpn_layout(self):
        c = render_compose(make(vpn=False), "/srv/arr", has_dri=False)
        self.assertNotIn("gluetun", c["services"])
        self.assertIn("8080:8080", c["services"]["qbittorrent"]["ports"])
        self.assertNotIn("network_mode", c["services"]["qbittorrent"])

    def test_every_service_rotates_logs(self):
        from dataclasses import replace
        c = render_compose(replace(make(), tailscale_key="tskey-x"), "/srv/arr", has_dri=False)
        self.assertEqual({"gluetun", "tailscale"} - set(c["services"]), set())
        for svc, conf in c["services"].items():
            self.assertEqual(conf.get("logging"), {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}, svc)

    def test_all_apps_and_ports(self):
        c = render_compose(make(vpn=False), "/srv/arr", has_dri=False)
        for svc in SERVICES:
            self.assertIn(svc, c["services"])
            self.assertIn(f"{PORTS[svc]}:{PORTS[svc]}", c["services"][svc]["ports"])

    def test_secrets_escaped_for_compose(self):
        env = render_compose(make(), "/srv/arr", has_dri=False)["services"]["gluetun"]["environment"]
        self.assertEqual(env["WIREGUARD_PRIVATE_KEY"], SECRET_KEY.replace("$", "$$"))

    def test_pasted_key_is_stripped(self):
        s = make()
        s.vpn_fields = {"WIREGUARD_PRIVATE_KEY": "  abc= \n"}
        env = render_compose(s, "/d", has_dri=False)["services"]["gluetun"]["environment"]
        self.assertEqual(env["WIREGUARD_PRIVATE_KEY"], "abc=")

    def test_labels_and_volumes(self):
        c = render_compose(make(), "/srv/arr", has_dri=False)
        for name, svc in c["services"].items():
            self.assertEqual(svc["labels"]["io.plaid"], "1", name)
        for name in SERVICES:
            self.assertEqual(c["volumes"][f"{name}-config"]["labels"]["io.plaid"], "1")
        self.assertEqual(c["networks"]["default"]["labels"]["io.plaid"], "1")

    def test_data_bind_long_syntax(self):
        c = render_compose(make(), "/srv/my arr", has_dri=False)
        bind = {"type": "bind", "source": "/srv/my arr", "target": "/data"}
        for svc in ("radarr", "sonarr", "qbittorrent", "jellyfin", "bazarr"):
            self.assertIn(bind, c["services"][svc]["volumes"], svc)

    def test_dri_only_when_present(self):
        self.assertNotIn("devices", render_compose(make(), "/d", has_dri=False)["services"]["jellyfin"])
        self.assertEqual(render_compose(make(), "/d", has_dri=True)["services"]["jellyfin"]["devices"],
                         ["/dev/dri:/dev/dri"])

    def test_prowlarr_dns_and_ids(self):
        c = render_compose(make(), "/d", has_dri=False)
        self.assertEqual(c["services"]["prowlarr"]["dns"], ["1.1.1.1", "9.9.9.9"])
        self.assertEqual(c["services"]["radarr"]["environment"]["PUID"], "1001")

    @unittest.skipUnless(shutil.which("docker"), "docker missing")
    def test_compose_reads_secrets_intact(self):
        s = make()
        s.tailscale_key = "tskey-auth-$1"
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "compose.json"), "w") as f:
                json.dump(render_compose(s, "/d", has_dri=False), f)
            out = subprocess.run(["docker", "compose", "-p", "t", "-f", "compose.json", "config", "--format", "json"],
                                 cwd=d, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            services = json.loads(out.stdout)["services"]
            # `config` prints a compose file, where a literal "$" is written "$$".
            self.assertEqual(services["gluetun"]["environment"]["WIREGUARD_PRIVATE_KEY"].replace("$$", "$"), SECRET_KEY)
            self.assertEqual(services["tailscale"]["environment"]["TS_AUTHKEY"].replace("$$", "$"), "tskey-auth-$1")


class TailscaleComposeTest(unittest.TestCase):
    KEY = "tskey-auth-fake"

    def test_service_joins_host_network(self):
        s = make()
        s.tailscale_key = self.KEY
        c = render_compose(s, "/d", has_dri=False)
        ts = c["services"]["tailscale"]
        self.assertEqual(ts["network_mode"], "host")
        self.assertEqual(ts["environment"]["TS_AUTHKEY"], self.KEY)
        self.assertEqual(ts["environment"]["TS_HOSTNAME"], "plaid")
        self.assertIn("tailscale-state:/var/lib/tailscale", ts["volumes"])
        self.assertEqual(c["volumes"]["tailscale-state"]["labels"]["io.plaid"], "1")

    def test_absent_without_key(self):
        c = render_compose(make(), "/d", has_dri=False)
        self.assertNotIn("tailscale", c["services"])
        self.assertNotIn("tailscale-state", c["volumes"])


class GuardComposeTest(unittest.TestCase):
    def test_disk_apps_are_started_by_the_guard(self):
        s = make(vpn=True)
        s.tailscale_key = "tskey-auth-fake"
        c = render_compose(s, "/srv/arr", False)
        for name, svc in c["services"].items():
            self.assertEqual(svc["restart"], "no" if name in DISK_APPS else "unless-stopped", name)


class InternetAccessTest(unittest.TestCase):
    def render(self, **kw):
        from dataclasses import replace
        return render_compose(replace(make(), **kw), "/srv/arr", has_dri=False)

    def test_off_changes_nothing(self):
        self.assertEqual(self.render(tailscale_key="tskey-x"), self.render(tailscale_key="tskey-x", internet_access=False))
        self.assertNotIn("plaid-state", json.dumps(self.render(tailscale_key="tskey-x")))

    def test_on_serves_jellyfin_through_funnel(self):
        c = self.render(tailscale_key="tskey-x", internet_access=True)
        ts = c["services"]["tailscale"]
        self.assertIn("plaid-state:/plaid-state:ro", ts["volumes"])
        self.assertEqual(ts["environment"]["TS_SERVE_CONFIG"], "/plaid-state/tailscale-serve.json")
        self.assertEqual(c["volumes"]["plaid-state"], {"external": True})

    def test_on_without_tailscale_does_nothing(self):
        self.assertEqual(self.render(internet_access=True), self.render())


class ServeConfigTest(unittest.TestCase):
    def test_only_jellyfin_is_published(self):
        from installer.compose import SERVE_CONFIG
        text = json.dumps(SERVE_CONFIG)
        self.assertIn("http://127.0.0.1:8096", text)
        for port in (5055, 7878, 8989, 8080, 9696, 6767, 7575):
            self.assertNotIn(str(port), text)
        self.assertEqual(SERVE_CONFIG["AllowFunnel"], {"${TS_CERT_DOMAIN}:443": True})
