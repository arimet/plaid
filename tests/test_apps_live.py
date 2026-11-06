"""End-to-end: starts a real stack (no VPN) and checks every link configure_all must create.
Run with ARR_LIVE=1. Set ARR_KEEP=1 to leave the stack running afterwards."""
import json
import os
import tempfile
import unittest
import urllib.request

from installer import apps, docker
from installer.compose import PORTS, render_compose, render_env
from installer.settings import Settings

LIVE = os.environ.get("ARR_LIVE") == "1"
PASSWORD = 'p@ss "$x" é'


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


@unittest.skipUnless(LIVE, "set ARR_LIVE=1")
class LinksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = os.path.expanduser("~")
        cls.s = Settings(data_dir=os.path.join(home, "Arr Test é", "live"), username="admin", password=PASSWORD,
                         vpn_provider=None, timezone="Europe/Paris", lang="fr", puid=os.getuid(), pgid=os.getgid(),
                         home=home, platform="mac")
        cls.state = tempfile.mkdtemp()
        with open(f"{cls.state}/compose.json", "w") as f:
            json.dump(render_compose(cls.s, cls.s.data_dir, has_dri=False), f)
        with open(f"{cls.state}/.env", "w") as f:
            f.write(render_env(cls.s))
        docker.ensure_data_dirs(cls.s.data_dir, cls.s.puid, cls.s.pgid)
        docker.compose(cls.state, "up", "-d")

        def compose_out(*args):
            return docker.compose(cls.state, *args).stdout

        def run():
            return apps.configure_all(cls.s, lambda svc: f"localhost:{PORTS[svc]}", print, compose_out, False)
        cls.keys = run()
        cls.counts = cls.snapshot()
        run()  # second run must be a no-op

    @classmethod
    def tearDownClass(cls):
        if os.environ.get("ARR_KEEP") != "1":
            docker.compose(cls.state, "down", "-v")

    @classmethod
    def snapshot(cls):
        k = cls.keys
        r = lambda path: get(f"http://localhost:7878/api/v3{path}", {"X-Api-Key": k["radarr"]})
        s = lambda path: get(f"http://localhost:8989/api/v3{path}", {"X-Api-Key": k["sonarr"]})
        return {
            "radarr_clients": [c["implementation"] for c in r("/downloadclient")],
            "sonarr_clients": [c["implementation"] for c in s("/downloadclient")],
            "radarr_notif": [(n["implementation"], n["onDownload"]) for n in r("/notification")],
            "sonarr_notif": [(n["implementation"], n["onDownload"]) for n in s("/notification")],
            "radarr_roots": [f["path"].rstrip("/") for f in r("/rootfolder")],
            "prowlarr_apps": len(get("http://localhost:9696/api/v1/applications", {"X-Api-Key": k["prowlarr"]})),
            "prowlarr_indexers": [(i["name"], i["enable"])
                                  for i in get("http://localhost:9696/api/v1/indexer", {"X-Api-Key": k["prowlarr"]})],
            "bazarr": get("http://localhost:6767/api/system/settings", {"X-API-KEY": k["bazarr"]})["general"],
            "seerr_public": get("http://localhost:5055/api/v1/settings/public")["initialized"],
        }

    def test_links(self):
        c = self.counts
        self.assertEqual(c["radarr_clients"], ["QBittorrent"])
        self.assertEqual(c["sonarr_clients"], ["QBittorrent"])
        self.assertEqual(c["radarr_notif"], [("MediaBrowser", True)])
        self.assertEqual(c["sonarr_notif"], [("MediaBrowser", True)])
        self.assertEqual(c["radarr_roots"], ["/data/media/movies"])
        self.assertEqual(c["prowlarr_apps"], 2)
        self.assertEqual(c["prowlarr_indexers"], [("Internet Archive", True)])
        self.assertTrue(c["bazarr"]["use_radarr"] and c["bazarr"]["use_sonarr"])
        self.assertTrue(c["seerr_public"])

    def test_jellyfin_libraries(self):
        names = sorted(self.keys["jellyfin_libraries"])
        self.assertEqual(names, ["Films", "Séries"])

    def test_seerr_servers(self):
        self.assertEqual(self.keys["seerr_servers"], {"radarr": 1, "sonarr": 1})

    def test_second_run_is_noop(self):
        self.assertEqual(self.snapshot(), self.counts)


if __name__ == "__main__":
    unittest.main()
