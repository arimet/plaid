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
        return ["warn_dir_not_empty"] + ([] if s.vpn_provider else ["warn_no_vpn"])

    def install(self, s, report):
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


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7700
    env = {"HOST_HOME": "/Users/zoe", "HOST_PLATFORM": "mac", "PUID": "501", "PGID": "20"}
    web.make_server(port, DemoFlow(), env).serve_forever()
