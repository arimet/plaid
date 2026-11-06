"""python -m installer [--cli | --uninstall | --uninstall-worker [--remove-media] | --apply-worker] [--lang fr]"""
from __future__ import annotations

import argparse
import os
import sys

from installer import cli, flow, guard, tui, web


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="plaid", description="Installs and removes the Plaid media stack.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cli", action="store_true", help="ask the questions in the terminal")
    mode.add_argument("--uninstall", action="store_true", help="remove the stack from the terminal")
    mode.add_argument("--uninstall-worker", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--apply-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--remove-media", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--lang", choices=["en", "fr"], default="fr" if os.environ.get("LANG", "").startswith("fr") else "en")
    parser.add_argument("--port", type=int, default=7575)
    args = parser.parse_args(argv)
    env = dict(os.environ)

    if args.apply_worker:
        from installer import reconfigure
        return reconfigure.run_worker()
    if args.uninstall_worker:
        flow.uninstall_worker(env["ARR_STATE"], args.remove_media)
        return 0
    # A real terminal gets menus, colours and live progress; a pipe or a log gets plain questions.
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if args.cli:
        s = flow.load_settings()
        if s is not None:  # already installed: the terminal version of the panel
            if not interactive:
                print(web.t(s.lang, "err_stack_exists"))
                return 1
            flow.reattach()  # the tracker catalogue needs Prowlarr, on the stack network
            return cli.run_panel_tui(tui.Term(), flow, env, s, complete=flow.is_complete())
        if interactive:
            return cli.run_install_tui(tui.Term(), flow, env, args.lang)
        return cli.run_install(input, print, flow, env, args.lang, cli.secret_input)
    if args.uninstall:
        s = flow.load_settings()
        lang = s.lang if s else args.lang  # the language chosen at install, not the terminal's
        size = web.human_size(flow.media_size(s), lang) if s else ""
        path = s.data_dir if s else ""
        if interactive:
            return cli.run_uninstall_tui(tui.Term(), flow, lang, path, size)
        return cli.run_uninstall(input, print, flow, lang, path, size)
    flow.reattach()
    guard.start()  # the assistant container is the one Docker restarts at boot: it watches the disk and the VPN
    web.serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
