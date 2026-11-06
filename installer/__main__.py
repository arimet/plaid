"""python -m installer [--cli | --uninstall | --uninstall-worker [--remove-media]] [--lang fr]"""
from __future__ import annotations

import argparse
import os
import sys

from installer import cli, flow, web


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="plaid", description="Installs and removes the Plaid media stack.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cli", action="store_true", help="ask the questions in the terminal")
    mode.add_argument("--uninstall", action="store_true", help="remove the stack from the terminal")
    mode.add_argument("--uninstall-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--remove-media", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--lang", choices=["en", "fr"], default="fr" if os.environ.get("LANG", "").startswith("fr") else "en")
    parser.add_argument("--port", type=int, default=7000)
    args = parser.parse_args(argv)
    env = dict(os.environ)

    if args.uninstall_worker:
        flow.uninstall_worker(env["ARR_STATE"], args.remove_media)
        return 0
    if args.cli:
        if flow.load_settings() is not None:
            print(web.t(args.lang, "err_stack_exists"))
            return 1
        return cli.run_install(input, print, flow, env, args.lang, cli.secret_input)
    if args.uninstall:
        s = flow.load_settings()
        size = web.human_size(flow.media_size(s), args.lang) if s else ""
        return cli.run_uninstall(input, print, flow, args.lang, s.data_dir if s else "", size)
    web.serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
