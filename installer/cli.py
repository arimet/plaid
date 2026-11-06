"""Command-line mode: the same questions as the web assistant, asked in the terminal."""
from __future__ import annotations

import getpass
from typing import Callable

from installer.compose import PORTS, SERVICES
from installer.flow import Progress
from installer.i18n import APP_NAMES, t
from installer.settings import validate
from installer.vpn import PROVIDERS
from installer.web import REMOTE_PLATFORMS, default_data_dir, settings_from

Ask = Callable[[str], str]
Out = Callable[[str], None]


def _yes(inp: Ask, lang: str, question: str) -> bool:
    return inp(f"{question} {t(lang, 'cli_yes_no')} ").strip().lower() in (t(lang, "cli_yes"), "y", "yes", "oui")


def run_install(inp: Ask, out: Out, flow, env: dict, lang: str, secret: Ask | None = None) -> int:
    secret = secret or inp
    out(t(lang, "subtitle"))
    default_dir = default_data_dir(env)
    data = {"lang": lang}
    data["data_dir"] = inp(f"{t(lang, 'field_folder')} [{default_dir}]: ").strip() or default_dir
    data["username"] = inp(f"{t(lang, 'field_username')} [admin]: ").strip() or "admin"
    data["password"] = secret(f"{t(lang, 'field_password')}: ")
    if secret(f"{t(lang, 'field_password2')}: ") != data["password"]:
        out(t(lang, "err_passwords_differ"))
        return 1
    ids = list(PROVIDERS)
    out(f"0. {t(lang, 'vpn_none')}")
    for n, pid in enumerate(ids, 1):
        out(f"{n}. {PROVIDERS[pid].label}")
    choice = inp(f"{t(lang, 'field_vpn')} [1]: ").strip() or "1"
    provider = ids[int(choice) - 1] if choice.isdigit() and 0 < int(choice) <= len(ids) else None
    data["vpn_provider"] = provider
    data["vpn_fields"] = {f: secret(f"{t(lang, 'vpnf_' + f.lower())}: ") for f in PROVIDERS[provider].fields} \
        if provider else {}
    if env.get("HOST_PLATFORM", "linux") in REMOTE_PLATFORMS:
        out("")
        out(f"{t(lang, 'field_remote')}: {t(lang, 'remote_help')}")
        for n, step in enumerate(t(lang, "remote_steps").split("|"), 1):
            out(f"  {n}. {step}")
        data["tailscale_key"] = secret(f"{t(lang, 'field_tailscale_key')} {t(lang, 'cli_skip')}: ").strip()
    tz = env.get("TZ", "UTC")
    data["timezone"] = inp(f"{t(lang, 'field_timezone')} [{tz}]: ").strip() or tz

    s = settings_from(data, env)
    errors = validate(s)
    if errors:
        for key in errors:
            out(t(lang, key))
        return 1
    found = flow.preflight(s)
    blocking = [k for k in found if k.startswith("err_")]
    for key in found:
        out(t(lang, key))
    if blocking or (found and not _yes(inp, lang, t(lang, "btn_continue_anyway"))):
        return 1

    last: list[Progress] = []

    def report(p: Progress) -> None:
        last.append(p)
        out(("✗ " if p.error else "· ") + t(lang, p.error or p.step))
        if p.detail:
            out(p.detail)
    flow.install(s, report)
    if not last or not last[-1].done:
        return 1
    host = env.get("HOST_ADDR", "localhost")
    out("")
    out(t(lang, "done_title"))
    for svc in SERVICES:
        out(f"  {APP_NAMES[svc]:<12} {t(lang, 'app_' + svc):<16} http://{host}:{PORTS[svc]}")
    out("")
    out(t(lang, "prowlarr_default"))
    for step in t(lang, "prowlarr_steps").split("|"):
        out(f"  - {step}")
    if s.tailscale_key:
        out("")
        out(t(lang, "remote_done_title"))
        for step in t(lang, "remote_done_steps").split("|"):
            out(f"  - {step}")
    return 0


def run_uninstall(inp: Ask, out: Out, flow, lang: str, path: str = "", size: str = "") -> int:
    if not _yes(inp, lang, t(lang, "uninstall_confirm")):
        return 1
    remove_media = bool(path) and _yes(inp, lang, t(lang, "uninstall_media", path=path, size=size))
    flow.uninstall(remove_media, lambda p: out(t(lang, p.step)))
    out(t(lang, "uninstall_started"))
    return 0


def secret_input(prompt: str) -> str:
    return getpass.getpass(prompt)
