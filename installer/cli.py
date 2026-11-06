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
        out(t(lang, key, path=s.data_dir))
    if blocking or (found and not _yes(inp, lang, t(lang, "btn_continue_anyway"))):
        return 1

    last: list[Progress] = []

    def report(p: Progress) -> None:
        last.append(p)
        out(("✗ " if p.error else "· ") + t(lang, p.error or p.step))
        if p.detail:
            out(p.detail)
    try:
        flow.install(s, report)
    except KeyboardInterrupt:  # Ctrl-C mid-install: leave the machine as it was
        out(t(lang, "cli_rollback"))
        flow.rollback(lambda p: None)
        out(t(lang, "cli_rollback_done"))
        return 1
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


# ---------------------------------------------------------------- interactive terminal (TTY)

def _steps_labels(lang: str, with_vpn: bool) -> list[tuple[str, str]]:
    keys = ["step_prepare", "step_folders", "step_download", "step_start"] + (["step_vpn"] if with_vpn else []) + \
        ["step_configure"]
    return [(k, t(lang, k)) for k in keys]


def run_install_tui(term, flow, env: dict, lang: str) -> int:
    """Same questions as the web assistant, with arrow-key menus, masked secrets and live progress."""
    import queue
    import threading

    from installer.settings import is_absolute, is_dangerous_path
    from installer.tui import Steps
    from installer.web import DAILY_APPS, PROVIDER_COLORS

    remote = env.get("HOST_PLATFORM", "linux") in REMOTE_PLATFORMS
    total = 4 if remote else 3
    home = env.get("HOST_HOME", "")
    term.banner("Plaid", t(lang, "subtitle"))
    try:
        term.step(1, total, t(lang, "field_folder"), t(lang, "field_folder_help"))
        data = {"lang": lang}
        data["data_dir"] = term.text(
            t(lang, "field_folder"), default=default_data_dir(env),
            validate=lambda v: t(lang, "err_path_absolute") if not is_absolute(v)
            else t(lang, "err_dangerous_path") if is_dangerous_path(v, home) else None)

        term.step(2, total, f"{t(lang, 'field_username')} · {t(lang, 'field_password')}", t(lang, "field_account_help"))
        data["username"] = term.text(t(lang, "field_username"), default="admin",
                                     validate=lambda v: None if v.strip() else t(lang, "err_username"))
        data["password"] = term.text(t(lang, "field_password"), secret=True,
                                     validate=lambda v: None if len(v) >= 8 else t(lang, "err_password_short"))
        term.text(t(lang, "field_password2"), secret=True,
                  validate=lambda v: None if v == data["password"] else t(lang, "err_passwords_differ"))

        term.step(3, total, t(lang, "field_vpn"), t(lang, "field_vpn_help"))
        options = [(pid, p.label, "", t(lang, "cli_vpn_pf" if p.port_forwarding else "cli_vpn_nopf"))
                   for pid, p in PROVIDERS.items()] + [("", t(lang, "vpn_none"), "", t(lang, "cli_vpn_none"))]
        options = [(o[0], o[1], _swatch(PROVIDER_COLORS.get(o[0])), o[3]) for o in options]
        provider = term.select(t(lang, "field_vpn"), options, default="protonvpn", hint=t(lang, "cli_select_hint"))
        data["vpn_provider"] = provider or None
        data["vpn_fields"] = {}
        if provider:
            term.note(t(lang, "vpn_where"))
            for f in PROVIDERS[provider].fields:
                data["vpn_fields"][f] = term.text(t(lang, "vpnf_" + f.lower()), secret="KEY" in f or "PASSWORD" in f,
                                                  validate=lambda v: None if v.strip() else t(lang, "err_vpn_fields"))

        if remote:
            term.step(4, total, t(lang, "field_remote"), t(lang, "remote_help"))
            for n, line in enumerate(t(lang, "remote_steps").split("|"), 1):
                term.note(f"{n}. {line}")
            data["tailscale_key"] = term.text(
                t(lang, "field_tailscale_key"), secret=True, hint=t(lang, "cli_skip"),
                validate=lambda v: None if not v or v.strip().startswith("tskey-") else t(lang, "err_tailscale_key"))

        data["timezone"] = env.get("TZ", "UTC")
        term.line()
        term.note(t(lang, "cli_tz", tz=data["timezone"]))
        s = settings_from(data, env)
        found = validate(s) or flow.preflight(s)
        for key in found:
            term.note(t(lang, key, path=s.data_dir), "error" if key.startswith("err_") else "warn")
        if any(k.startswith("err_") for k in found):
            return 1
        question = t(lang, "cli_continue") if found else t(lang, "cli_install_start")
        if not term.confirm(question, t(lang, "cli_yes_word"), t(lang, "cli_no_word"), default=not found):
            term.note(t(lang, "cli_cancelled"))
            return 1
    except KeyboardInterrupt:
        term.line()
        term.note(t(lang, "cli_cancelled"))
        return 1

    term.heading(t(lang, "progress_title"))
    term.note(t(lang, "progress_help"))
    steps = Steps(term, _steps_labels(lang, bool(s.vpn_provider)))
    events: queue.Queue = queue.Queue()
    cancel = threading.Event()
    worker = threading.Thread(target=flow.install, args=(s, events.put), kwargs={"cancel": cancel}, daemon=True)
    worker.start()
    while True:
        try:
            p = events.get(timeout=0.12)
        except queue.Empty:
            steps.render()
            continue
        except KeyboardInterrupt:  # Ctrl-C mid-install: leave the machine as it was
            steps.failed = True  # the interrupted step shows ✗, not a frozen spinner
            steps.render()
            term._settle()
            term.line()
            return undo_install(term, flow, lang, cancel, worker)
        if p.step.startswith("step_app:"):
            steps.subs.append(t(lang, p.step))
        else:
            steps.advance(p.step)
        if p.error:
            steps.failed = True
            steps.render()
            term._settle()
            term.note(t(lang, p.error), "error")
            if p.detail:
                term.line("    " + term.style(p.detail, dim=True))
            return 1
        if p.done:
            steps.finish()
            break
        steps.render()

    host = env.get("HOST_ADDR", "localhost")
    term.heading(t(lang, "done_title"))
    term.note(t(lang, "done_login", username=s.username), "ok")
    print_apps(term, lang, host)
    term.line()
    term.note(t(lang, "trackers_help"))
    if s.tailscale_key:
        for line in t(lang, "remote_done_steps").split("|"):
            term.note(line)
    term.note(t(lang, "cli_manage"), "ok")
    term.note(t(lang, "cli_panel_other", url=f"http://{host}:7575"))
    term.line()
    return 0


def print_apps(term, lang: str, host: str) -> None:
    from installer.web import DAILY_APPS
    for title, group, daily in ((t(lang, "apps_daily"), [x for x in SERVICES if x in DAILY_APPS], True),
                                (t(lang, "apps_behind"), [x for x in SERVICES if x not in DAILY_APPS], False)):
        term.heading(title)
        for svc in group:
            url = term.style(f"http://{host}:{PORTS[svc]}", underline=True)
            name = term.pad(term.style(APP_NAMES[svc], bold=True), 13)  # padded on what shows, not on the codes
            term.line(f"  {term.style('■', fg=_APP_SWATCH[svc])} {name}{t(lang, 'app_' + svc).ljust(17)}{url}")
            if daily:
                for chunk in term.wrap(t(lang, "appd_" + svc), 4):
                    term.line("    " + term.style(chunk, dim=True))


def run_panel_tui(term, flow, env: dict, s, complete: bool = True) -> int:
    """What the web panel does, in the terminal: for machines without a screen or a browser."""
    from installer.web import human_size
    lang, host = s.lang, env.get("HOST_ADDR", "localhost")
    size = lambda: human_size(flow.media_size(s), lang)
    if not complete:  # a failed install: removal is the only thing to do
        term.banner("Plaid", t(lang, "subtitle"))
        term.line()
        term.note(t(lang, "progress_failed"), "error")
        term.note(t(lang, "err_unexpected"))
        return run_uninstall_tui(term, flow, lang, s.data_dir, size())
    term.banner("Plaid", t(lang, "subtitle"))
    actions = [("apps", t(lang, "menu_apps")), ("trackers", t(lang, "menu_trackers")),
               ("remote", t(lang, "menu_remote")), ("remove", t(lang, "menu_remove")), ("quit", t(lang, "menu_quit"))]
    try:
        while True:
            term.line()
            choice = term.select(t(lang, "menu_title"), actions, hint=t(lang, "cli_select_hint"))
            if choice == "quit":
                return 0
            if choice == "apps":
                print_apps(term, lang, host)
                term.line()
                term.note(t(lang, "apps_open_anywhere"))
                term.note(t(lang, "done_login", username=s.username))
            elif choice == "trackers":
                _pick_trackers(term, flow, lang)
            elif choice == "remote":
                key = "remote_done_steps" if s.tailscale_key else "remote_desktop_steps"
                term.heading(t(lang, "remote_done_title"))
                for n, line in enumerate(t(lang, key).split("|"), 1):
                    term.note(f"{n}. {line}")
            elif choice == "remove":
                return run_uninstall_tui(term, flow, lang, s.data_dir, size())
    except KeyboardInterrupt:
        term.line()
        return 0


def _pick_trackers(term, flow, lang: str) -> None:
    term.heading(t(lang, "trackers_title"))
    term.note(t(lang, "trackers_help"))
    term.note(t(lang, "trackers_rights"))
    try:
        catalogue = flow.tracker_catalog()
    except Exception:
        term.note(t(lang, "trackers_error"), "error")
        return
    while True:
        term.line()
        query = term.text(t(lang, "trackers_prompt"), hint=t(lang, "trackers_prompt_hint")).strip().lower()
        if not query:
            return
        found = [x for x in catalogue if query in x["name"].lower() or query in x["description"].lower()]
        if not found:
            term.note(t(lang, "trackers_none"))
            continue
        options = [(x["id"], x["name"] + (f"  ✓ {t(lang, 'tracker_added')}" if x["added"] else ""), None,
                    x["language"]) for x in found[:10]]
        options.append(("", t(lang, "trackers_back"), None, ""))
        if len(found) > 10:
            term.note(t(lang, "trackers_more", n=len(found) - 10))
        picked = term.select(t(lang, "trackers_title"), options, hint=t(lang, "cli_select_hint"))
        tracker = next((x for x in found if x["id"] == picked), None)
        if tracker is None or tracker["added"]:
            continue
        try:
            flow.add_tracker(tracker["id"])
        except Exception:
            term.note(t(lang, "trackers_error"), "error")
            continue
        tracker["added"] = True
        term.note(t(lang, "tracker_added_named", name=tracker["name"]), "ok")


_APP_SWATCH = {"jellyfin": "plum", "seerr": "navy", "radarr": "gold", "sonarr": "teal", "qbittorrent": "green",
               "prowlarr": "rust", "bazarr": "red"}


def _swatch(hex_color: str | None) -> str:
    """Closest tartan colour for a provider's brand swatch."""
    return {"#6d4aff": "plum", "#4bb543": "green", "#e8b230": "gold", "#4687ff": "navy", "#1ec2b0": "teal",
            "#22305e": "navy"}.get(hex_color or "", "grey")


def run_uninstall_tui(term, flow, lang: str, path: str = "", size: str = "") -> int:
    import time

    from installer.tui import Steps
    term.banner("Plaid", t(lang, "uninstall_title"))
    term.line()
    term.note(t(lang, "uninstall_text"))
    yes, no = t(lang, "cli_yes_word"), t(lang, "cli_no_word")
    try:
        if not term.confirm(t(lang, "uninstall_confirm"), yes, no):
            term.note(t(lang, "cli_cancelled"))
            return 1
        remove_media = bool(path) and term.confirm(t(lang, "uninstall_media", path=path, size=size), yes, no)
    except KeyboardInterrupt:
        term.note(t(lang, "cli_cancelled"))
        return 1
    flow.uninstall(remove_media, lambda p: None)
    steps = Steps(term, [("rm", t(lang, "cli_uninstall_wait"))])
    while True:
        state, detail = flow.uninstall_status()
        if state == "failed":
            steps.failed = True
            steps.render()
            term._settle()
            term.note(t(lang, "err_uninstall_failed"), "error")
            term.line("    " + term.style(detail, dim=True))
            return 1
        if state in ("done", "idle"):
            steps.finish()
            term.note(t(lang, "uninstall_done"), "ok")
            return 0
        steps.render()
        time.sleep(0.12)


def undo_install(term, flow, lang: str, cancel, worker) -> int:
    """Stops the install at its next step, then removes everything it created (the media folder only
    if the install made it). Returns 1: the install did not happen."""
    import time

    from installer.tui import Steps
    cancel.set()
    term.note(t(lang, "cli_rollback"), "warn")
    steps = Steps(term, [("undo", t(lang, "cli_uninstall_wait"))])
    while worker.is_alive():  # the current step ends (Ctrl-C also stopped its docker command)
        steps.render()
        worker.join(0.12)
    while True:
        try:
            flow.rollback(lambda p: None)
            break
        except KeyboardInterrupt:
            continue  # a second Ctrl-C must not leave half an install behind
    while True:
        state, detail = flow.uninstall_status()
        if state == "failed":
            steps.failed = True
            steps.render()
            term._settle()
            term.note(t(lang, "err_uninstall_failed"), "error")
            term.line("    " + term.style(detail, dim=True))
            return 1
        if state in ("done", "idle"):
            steps.finish()
            term.note(t(lang, "cli_rollback_done"), "ok")
            return 1
        steps.render()
        time.sleep(0.12)
