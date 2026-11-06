"""Runs the assistant against a simulated flow, to look at it in a browser: python3 tests/demo.py [port]"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from installer import web  # noqa: E402
from installer.flow import Progress  # noqa: E402


class DemoFlow:
    def __init__(self):
        self.settings = None
        self.complete = False

    def load_settings(self):
        return self.settings

    def is_complete(self):
        return self.complete

    def preflight(self, s):
        return [] if s.vpn_provider else ["warn_no_vpn"]  # the demo has no real folder to look at

    def install(self, s, report, cancel=None):
        self.settings = s
        steps = ["step_prepare", "step_folders", "step_download", "step_start"] + (["step_vpn"] if s.vpn_provider else [])
        for step in steps + ["step_configure"]:
            report(Progress(step))
            time.sleep(1.5)
        for app in ("qbittorrent", "jellyfin", "radarr", "sonarr", "prowlarr", "bazarr", "seerr"):
            report(Progress(f"step_app:{app}"))
            time.sleep(0.6)
        self.complete = True
        report(Progress("step_done", done=True))

    def uninstall(self, remove_media, report):
        self.settings, self.complete = None, False

    def media_size(self, s):
        return 412 * 1024 ** 3

    # Synthetic catalogue: the real one comes from Prowlarr.
    catalogue = [{"id": f"demo{i}", "name": f"Demo tracker {i}", "language": "en-US",
                  "description": "Synthetic entry for the demo.", "added": i == 1} for i in range(1, 41)]

    def tracker_catalog(self):
        return self.catalogue

    def add_tracker(self, definition):
        next(t for t in self.catalogue if t["id"] == definition)["added"] = True

    def uninstall_status(self):
        return "running", ""


if __name__ == "__main__":
    from installer.settings import Settings  # noqa: E402

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7700
    platform = sys.argv[2] if len(sys.argv) > 2 else "mac"
    home = "/home/anthony" if platform == "linux" else "/Users/anthony"
    env = {"HOST_HOME": home, "HOST_PLATFORM": platform, "PUID": "501", "PGID": "20"}
    flow = DemoFlow()
    if "installed" in sys.argv[3:]:  # straight to the panel; sign in with demo / demo-pass
        flow.settings = Settings(data_dir=home + "/Plaid", username="demo", password="demo-pass",
                                 vpn_provider=None, home=home, platform=platform, lang="fr")
        flow.complete = True
    web.make_server(port, flow, env).serve_forever()
