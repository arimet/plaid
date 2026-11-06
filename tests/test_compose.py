import json
import os
import shutil
import subprocess
import tempfile
import unittest

from installer.compose import PORTS, SERVICES, render_compose, render_env
from installer.settings import Settings

SECRET_KEY = "k'ey\"$x é"


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

    def test_all_apps_and_ports(self):
        c = render_compose(make(vpn=False), "/srv/arr", has_dri=False)
        for svc in SERVICES:
            self.assertIn(svc, c["services"])
            self.assertIn(f"{PORTS[svc]}:{PORTS[svc]}", c["services"][svc]["ports"])

    def test_no_secret_in_compose(self):
        text = json.dumps(render_compose(make(), "/srv/arr", has_dri=False), ensure_ascii=False)
        self.assertNotIn(SECRET_KEY, text)
        self.assertNotIn(make().password, text)

    def test_labels_and_volumes(self):
        c = render_compose(make(), "/srv/arr", has_dri=False)
        for name, svc in c["services"].items():
            self.assertEqual(svc["labels"]["io.arr-installer"], "1", name)
            self.assertEqual(svc["restart"], "unless-stopped", name)
        for name in SERVICES:
            self.assertEqual(c["volumes"][f"{name}-config"]["labels"]["io.arr-installer"], "1")
        self.assertEqual(c["networks"]["default"]["labels"]["io.arr-installer"], "1")

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
    def test_env_quoting(self):
        s = make()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "compose.json"), "w") as f:
                json.dump(render_compose(s, "/d", has_dri=False), f)
            with open(os.path.join(d, ".env"), "w") as f:
                f.write(render_env(s))
            out = subprocess.run(["docker", "compose", "-p", "t", "-f", "compose.json", "--env-file", ".env",
                                  "config", "--format", "json"], cwd=d, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            env = json.loads(out.stdout)["services"]["gluetun"]["environment"]
            # `config` prints a compose file, where a literal "$" is written "$$".
            self.assertEqual(env["WIREGUARD_PRIVATE_KEY"].replace("$$", "$"), SECRET_KEY)


if __name__ == "__main__":
    unittest.main()
