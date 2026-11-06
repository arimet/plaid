"""Try the terminal interface with a simulated install: python3 tests/demo_cli.py [linux|mac] [--uninstall] [--lang en]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from installer import cli, tui  # noqa: E402
from tests.demo import DemoFlow  # noqa: E402


class Flow(DemoFlow):
    def uninstall_status(self):
        return "done", ""


if __name__ == "__main__":
    platform = "linux" if "linux" in sys.argv else "mac"
    lang = "en" if "en" in sys.argv else "fr"
    home = "/home/zoe" if platform == "linux" else "/Users/zoe"
    env = {"HOST_HOME": home, "HOST_PLATFORM": platform, "PUID": "501", "PGID": "20", "TZ": "Europe/Paris",
           "HOST_ADDR": "localhost"}
    if "--uninstall" in sys.argv:
        sys.exit(cli.run_uninstall_tui(tui.Term(), Flow(), lang, home + "/Plaid", "412 Go"))
    sys.exit(cli.run_install_tui(tui.Term(), Flow(), env, lang))
