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
# Each step is one page on a screen of its own (Term.fullscreen): questions leave nothing in the terminal's
# history. What matters afterwards (the links, an error, "nothing was changed") is printed once it closes.

Notes = list  # [(text, kind)] printed on the normal screen once the full-screen part is over


def _print_notes(term, notes: Notes) -> None:
    term.line()
    for text, kind in notes:
        if kind == "detail":
            term.line("    " + term.style(text, dim=True))
        else:
            term.note(text, kind)
    term.line()


def _steps_labels(lang: str, with_vpn: bool) -> list[tuple[str, str]]:
    keys = ["step_prepare", "step_folders", "step_download", "step_start"] + (["step_vpn"] if with_vpn else []) + \
        ["step_configure"]
    return [(k, t(lang, k)) for k in keys]


def _ask_media(term, lang: str, e) -> dict:
    """Audio language, profiles and their default, subtitles, fiches; `e` holds the current choices."""
    from installer import media
    hint = t(lang, "cli_select_hint")
    langs = [(code, t(lang, "lang_" + code)) for code in media.LANGS]
    audio = term.select(t(lang, "field_audio_lang"), langs, default=e.audio_lang, hint=hint)
    names = [(key, media.profile_name(key, lang, audio)) for key in media.KEYS]
    profiles = term.checklist(t(lang, "field_profiles"), names, default=e.profiles, hint=t(lang, "cli_check_hint"),
                              validate=lambda v: None if v else t(lang, "err_profiles_none"))
    default = profiles[0] if len(profiles) == 1 else term.select(
        t(lang, "field_default_profile"), [n for n in names if n[0] in profiles],
        default=e.default_profile if e.default_profile in profiles else profiles[0], hint=hint)
    subtitles = term.checklist(t(lang, "field_subtitles"), langs, default=e.subtitle_langs,
                               hint=t(lang, "cli_check_hint"),
                               validate=lambda v: None if v else t(lang, "err_subtitles_none"))
    metadata = term.select(t(lang, "field_metadata_lang"), langs, default=e.metadata_lang, hint=hint)
    return {"audio_lang": audio, "profiles": profiles, "default_profile": default, "subtitle_langs": subtitles,
            "metadata_lang": metadata}


def _media_summary(lang: str, e) -> str:
    from installer import media
    return ", ".join([media.profile_name(e.default_profile, lang, e.audio_lang)] +
                     [t(lang, "lang_" + c) for c in e.subtitle_langs])


def _disk_lines(flow, env: dict, lang: str) -> list[str]:
    """One line per disk, shown above the folder question. Never in the way: no list when Docker cannot tell."""
    from installer.web import human_size
    try:
        disks = flow.disks(env.get("HOST_PLATFORM", "linux"), env.get("HOST_HOME", ""))
    except Exception:
        return []
    room = lambda d: ([t(lang, "picker_free", free=human_size(d["free"], lang), total=human_size(d["total"], lang))]
                      if d["free"] is not None and d["total"] is not None else [])  # unknown on a Mac external disk
    return [" · ".join([t(lang, "picker_system") if d["system"] else d["name"], d["path"], *room(d)]) for d in disks]


def _ask(term, flow, env: dict, lang: str):
    """The questions, one page each. Returns (settings, notes); settings is None when nothing will be installed."""
    from installer.settings import is_absolute, is_dangerous_path
    from installer.web import PROVIDER_COLORS

    remote = env.get("HOST_PLATFORM", "linux") in REMOTE_PLATFORMS
    total = 5 if remote else 4  # the films and series page is the last question
    home = env.get("HOST_HOME", "")
    recap: list[tuple[str, str]] = []
    data = {"lang": lang}

    term.page(t(lang, "field_folder"), t(lang, "field_folder_help"), step=(1, total), intro=t(lang, "subtitle"))
    for line in _disk_lines(flow, env, lang):
        term.note(line)
    data["data_dir"] = term.text(
        t(lang, "field_folder"), default=default_data_dir(env),
        validate=lambda v: t(lang, "err_path_absolute") if not is_absolute(v)
        else t(lang, "err_dangerous_path") if is_dangerous_path(v, home) else None)
    recap.append((t(lang, "field_folder"), data["data_dir"]))

    term.page(f"{t(lang, 'field_username')} · {t(lang, 'field_password')}", t(lang, "field_account_help"),
              step=(2, total), recap=recap)
    data["username"] = term.text(t(lang, "field_username"), default="admin",
                                 validate=lambda v: None if v.strip() else t(lang, "err_username"))
    data["password"] = term.text(t(lang, "field_password"), secret=True,
                                 validate=lambda v: None if len(v) >= 8 else t(lang, "err_password_short"))
    term.text(t(lang, "field_password2"), secret=True,
              validate=lambda v: None if v == data["password"] else t(lang, "err_passwords_differ"))
    recap.append((t(lang, "field_username"), data["username"]))

    term.page(t(lang, "field_vpn"), t(lang, "field_vpn_help"), step=(3, total), recap=recap)
    options = [(pid, p.label, _swatch(PROVIDER_COLORS.get(pid)), t(lang, "cli_vpn_pf" if p.port_forwarding
                                                                       else "cli_vpn_nopf"))
               for pid, p in PROVIDERS.items()] + [("", t(lang, "vpn_none"), "grey", t(lang, "cli_vpn_none"))]
    provider = term.select(t(lang, "field_vpn"), options, default="protonvpn", hint=t(lang, "cli_select_hint"))
    data["vpn_provider"] = provider or None
    data["vpn_fields"] = {}
    if provider:
        term.note(t(lang, "vpn_where"))
        for f in PROVIDERS[provider].fields:
            data["vpn_fields"][f] = term.text(t(lang, "vpnf_" + f.lower()), secret="KEY" in f or "PASSWORD" in f,
                                              validate=lambda v: None if v.strip() else t(lang, "err_vpn_fields"))
    recap.append((t(lang, "field_vpn"), PROVIDERS[provider].label if provider else t(lang, "vpn_none")))

    if remote:
        term.page(t(lang, "field_remote"), t(lang, "remote_help"), step=(4, total), recap=recap)
        for n, line in enumerate(t(lang, "remote_steps").split("|"), 1):
            term.note(f"{n}. {line}")
        term.line()
        data["tailscale_key"] = term.text(
            t(lang, "field_tailscale_key"), secret=True, hint=t(lang, "cli_skip"),
            validate=lambda v: None if not v or v.strip().startswith("tskey-") else t(lang, "err_tailscale_key"))
        recap.append((t(lang, "field_remote"), "Tailscale" if data["tailscale_key"] else "—"))

    from installer import media
    term.page(t(lang, "media_title"), t(lang, "media_help"), step=(total, total), recap=recap)
    e = media.effective(settings_from(data, env))
    if term.select(t(lang, "media_title"), [("keep", t(lang, "cli_media_keep", summary=_media_summary(lang, e))),
                                            ("change", t(lang, "cli_media_change"))],
                   hint=t(lang, "cli_select_hint")) == "change":
        data.update(_ask_media(term, lang, e))
        e = media.effective(settings_from(data, env))
    recap.append((t(lang, "media_title"), _media_summary(lang, e)))

    data["timezone"] = env.get("TZ", "UTC")
    s = settings_from(data, env)
    term.page(t(lang, "confirm_title"), recap=recap)
    term.note(t(lang, "cli_tz", tz=data["timezone"]))
    found = validate(s) or flow.preflight(s)
    notes = [(t(lang, key, path=s.data_dir), "error" if key.startswith("err_") else "warn") for key in found]
    for text, kind in notes:
        term.note(text, kind)
    term.line()
    if any(k.startswith("err_") for k in found):
        return None, notes
    question = t(lang, "cli_continue") if found else t(lang, "cli_install_start")
    if not term.confirm(question, t(lang, "cli_yes_word"), t(lang, "cli_no_word"), default=not found):
        return None, [(t(lang, "cli_cancelled"), "info")]
    return s, []


def run_install_tui(term, flow, env: dict, lang: str) -> int:
    """Same questions as the web assistant, with arrow-key menus, masked secrets and live progress."""
    import queue
    import threading

    from installer.tui import Steps

    done, notes = False, [(t(lang, "cli_cancelled"), "info")]
    with term.fullscreen():
        try:
            s, notes = _ask(term, flow, env, lang)
        except KeyboardInterrupt:
            s = None
        if s is not None:
            term.page(t(lang, "progress_title"), t(lang, "progress_help"))
            steps = Steps(term, _steps_labels(lang, bool(s.vpn_provider)))
            events: queue.Queue = queue.Queue()
            cancel = threading.Event()
            worker = threading.Thread(target=flow.install, args=(s, events.put), kwargs={"cancel": cancel},
                                      daemon=True)
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
                    notes = _undo_steps(term, flow, lang, cancel, worker)
                    break
                if p.step.startswith("step_app:"):
                    steps.subs.append(t(lang, p.step))
                else:
                    steps.advance(p.step)
                if p.error:
                    notes = [(t(lang, "progress_failed"), "error"), (t(lang, p.error), "info")] + \
                        ([(p.detail, "detail")] if p.detail else []) + [(t(lang, "cli_manage"), "info")]
                    break
                if p.done:
                    steps.finish()
                    done = True
                    break
                steps.render()
    if not done:
        _print_notes(term, notes)
        return 1
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


def print_apps(term, lang: str, host: str, states: dict | None = None) -> None:
    from installer.web import DAILY_APPS
    for title, group, daily in ((t(lang, "apps_daily"), [x for x in SERVICES if x in DAILY_APPS], True),
                                (t(lang, "apps_behind"), [x for x in SERVICES if x not in DAILY_APPS], False)):
        term.heading(title)
        for svc in group:
            url = term.style(f"http://{host}:{PORTS[svc]}", fg="accent2", underline=True)
            name = term.pad(term.style(APP_NAMES[svc], bold=True), 13)  # padded on what shows, not on the codes
            state = (states or {}).get(svc)
            word = f"  {term.style(t(lang, 'state_' + state), dim=state == 'running')}" if state else ""
            term.line(f"  {term.style('■', fg=_APP_SWATCH[svc])} {name}{t(lang, 'app_' + svc).ljust(17)}{url}{word}")
            if daily:
                for chunk in term.wrap(t(lang, "appd_" + svc), 4):
                    term.line("    " + term.style(chunk, dim=True))


def run_panel_tui(term, flow, env: dict, s, complete: bool = True) -> int:
    """What the web panel does, in the terminal: for machines without a screen or a browser."""
    from installer.web import guard_notes, human_size
    lang, host = s.lang, env.get("HOST_ADDR", "localhost")
    size = lambda: human_size(flow.media_size(s), lang)
    code, notes = 0, []
    actions = [("apps", t(lang, "menu_apps")), ("trackers", t(lang, "menu_trackers")),
               ("remote", t(lang, "menu_remote")), ("settings", t(lang, "menu_settings")),
               ("update", t(lang, "update_btn")), ("torrents", t(lang, "torrents_link")),
               ("remove", t(lang, "menu_remove")), ("quit", t(lang, "menu_quit"))]
    with term.fullscreen():
        try:
            if not complete:  # a failed install: removal is the only thing to do
                term.page(t(lang, "progress_failed"), t(lang, "err_unexpected"))
                code, notes = _remove_steps(term, flow, lang, s.data_dir, size())
            flash = ""
            while complete:
                term.page(t(lang, "panel_title"), intro=t(lang, "subtitle"))
                if flash:
                    term.note(flash, "ok")
                    flash = ""
                status = flow.guard_status()
                for title, text, _ in guard_notes(lang, s, status):
                    term.note(f"{title}{' ' if title.endswith('?') else '. '}{text}", "warn")
                confirm = [("confirm_disk", t(lang, "guard_disk_confirm"))] if status.get("disk") == "unconfirmed" else []
                choice = term.select(t(lang, "menu_title"), confirm + actions, hint=t(lang, "cli_select_hint"))
                if choice == "quit":
                    break
                if choice == "confirm_disk" and flow.confirm_disk(s):  # False: the menu shows the fresh state again
                    flash = t(lang, "guard_disk_confirmed")
                if choice == "apps":
                    term.page(t(lang, "menu_apps"), t(lang, "apps_open_anywhere"))
                    print_apps(term, lang, host, status.get("apps"))
                    term.line()
                    if status.get("free") is not None and status.get("total"):
                        colon = " : " if lang == "fr" else ": "
                        term.note(t(lang, "panel_disk") + colon + t(lang, "picker_free", free=human_size(
                            status["free"], lang), total=human_size(status["total"], lang)))
                    term.note(t(lang, "done_login", username=s.username))
                    term.pause(t(lang, "cli_back"))
                elif choice == "trackers":
                    term.page(t(lang, "trackers_title"), t(lang, "trackers_help"))
                    _pick_trackers(term, flow, lang)
                elif choice == "remote":
                    key = "remote_done_steps" if s.tailscale_key else "remote_desktop_steps"
                    term.page(t(lang, "remote_done_title"), t(lang, "nav_remote_desc"))
                    for n, line in enumerate(t(lang, key).split("|"), 1):
                        term.note(f"{n}. {line}")
                    term.pause(t(lang, "cli_back"))
                elif choice == "settings":
                    result = _settings_tui(term, flow, env, s)
                    if result is not None:  # applied (or failed): the panel closes on its outcome
                        code, notes = result
                        break
                elif choice == "update":
                    flash = _update_tui(term, flow, lang)
                elif choice == "torrents":
                    flash = _torrents_tui(term, flow, lang, s)
                elif choice == "remove":
                    term.page(t(lang, "uninstall_title"), t(lang, "uninstall_text"))
                    code, notes = _remove_steps(term, flow, lang, s.data_dir, size())
                    if code == 0:
                        break
        except KeyboardInterrupt:
            pass
    if notes:
        _print_notes(term, notes)
    return code


def _update_tui(term, flow, lang: str) -> str:
    """Runs the apps' update in the worker and follows it. Returns the line the menu shows next."""
    import time
    from installer.tui import Steps
    term.page(t(lang, "update_running"), t(lang, "update_help"))
    if flow.apply_status()["state"] != "running":  # one operation at a time: follow the running one
        flow.start_update()
    steps = Steps(term, [(k, t(lang, k)) for k in ("step_pull", "step_recreate", "step_cleanup_images")])
    while True:
        status = flow.apply_status()
        events = status["events"]
        if events and status["state"] in ("running", "done", "failed"):
            steps.advance(events[-1]["step"])
        if status["state"] == "done":
            steps.finish()
            changed = [APP_NAMES.get(a, a) for a in (events[-1].get("detail") or "").split(",") if a]
            return t(lang, "update_changed", apps=", ".join(changed)) if changed else t(lang, "update_none")
        if status["state"] == "failed":
            steps.failed = True
            steps.render()
            term.note(t(lang, "err_update_failed"), "error")
            term.note(events[-1].get("detail", "") if events else "")
            term.pause(t(lang, "cli_back"))
            return ""
        steps.render()
        time.sleep(0.3)


def _torrents_tui(term, flow, lang: str, s) -> str:
    """Lists the finished torrents already in the library, deletes them on a yes."""
    from installer.web import human_size
    term.page(t(lang, "torrents_title"))
    try:
        found = flow.finished_torrents(s)
    except Exception:  # qBittorrent, Radarr or Sonarr down or starting
        term.note(t(lang, "torrents_error"), "warn")
        term.pause(t(lang, "cli_back"))
        return ""
    if not found:
        term.note(t(lang, "torrents_none"))
        term.note(t(lang, "torrents_kept"))
        term.pause(t(lang, "cli_back"))
        return ""
    term.note(t(lang, "torrents_help"))
    for x in found:
        term.note(f"{x['name']} · {human_size(x['size'], lang)}")
    term.note(t(lang, "torrents_kept"))
    if not term.confirm(t(lang, "torrents_btn", n=len(found)) + " ?", t(lang, "cli_yes_word"), t(lang, "cli_no_word"),
                        default=False):
        return ""
    try:
        return t(lang, "torrents_done", n=flow.clean_torrents(s))
    except Exception:
        return t(lang, "torrents_error")


def _settings_tui(term, flow, env: dict, s) -> tuple[int, Notes] | None:
    """Pick settings one by one, then apply them all. None when the user went back without applying."""
    from installer import reconfigure
    from installer.settings import is_absolute, is_dangerous_path
    from installer import media
    from installer.web import LANG_NAMES
    lang, data = s.lang, {}
    remote = s.platform in REMOTE_PLATFORMS
    keep = t(lang, "settings_keep_secret")
    while True:
        new = reconfigure.merge(s, data)
        chg = reconfigure.changes(s, new)
        term.page(t(lang, "nav_settings"), t(lang, "nav_settings_desc"))
        options = [("lang", t(lang, "settings_lang"), None, LANG_NAMES[new.lang]),
                   ("timezone", t(lang, "field_timezone"), None, new.timezone),
                   ("folder", t(lang, "field_folder"), None, new.data_dir),
                   ("account", f"{t(lang, 'field_username')} · {t(lang, 'field_password')}", None, new.username),
                   ("vpn", t(lang, "field_vpn"), None,
                    PROVIDERS[new.vpn_provider].label if new.vpn_provider else t(lang, "vpn_none"))]
        if remote:
            options.append(("tailscale", t(lang, "field_remote"), None, "Tailscale" if new.tailscale_key else "—"))
        options.append(("media", t(lang, "media_title"), None, _media_summary(lang, media.effective(new))))
        if chg:
            options.append(("apply", t(lang, "settings_apply"), None, ", ".join(_change_names(lang, chg, new))))
        options.append(("back", t(lang, "btn_back")))
        pick = term.select(t(lang, "cli_settings_pick"), options, hint=t(lang, "cli_select_hint"))
        if pick == "back":
            return None
        if pick == "apply":
            result = _apply_tui(term, flow, lang, s, new, chg, env.get("HOST_ADDR", "localhost"))
            if result is not None:
                return result
        elif pick == "media":
            data.update(_ask_media(term, lang, media.effective(new)))
        elif pick == "lang":
            data["lang"] = term.select(t(lang, "settings_lang"), list(LANG_NAMES.items()), default=new.lang)
        elif pick == "timezone":
            data["timezone"] = term.text(t(lang, "field_timezone"), default=new.timezone,
                                         hint=t(lang, "settings_timezone_help"))
        elif pick == "folder":
            term.note(t(lang, "settings_folder_help"))
            for line in _disk_lines(flow, env, lang):
                term.note(line)
            data["data_dir"] = term.text(
                t(lang, "field_folder"), default=new.data_dir,
                validate=lambda v: t(lang, "err_path_absolute") if not is_absolute(v)
                else t(lang, "err_dangerous_path") if is_dangerous_path(v, s.home) else None)
        elif pick == "account":
            data["username"] = term.text(t(lang, "field_username"), default=new.username,
                                         validate=lambda v: None if v.strip() else t(lang, "err_username"))
            password = term.text(t(lang, "field_password"), secret=True, hint=keep,
                                 validate=lambda v: None if not v or len(v) >= 8 else t(lang, "err_password_short"))
            if password:
                term.text(t(lang, "field_password2"), secret=True,
                          validate=lambda v: None if v == password else t(lang, "err_passwords_differ"))
            data["password"] = password
        elif pick == "vpn":
            options = [(pid, p.label) for pid, p in PROVIDERS.items()] + [("", t(lang, "vpn_none"))]
            provider = term.select(t(lang, "field_vpn"), options, default=new.vpn_provider or "")
            fields = {}
            if provider:
                same = provider == s.vpn_provider
                for f in PROVIDERS[provider].fields:
                    fields[f] = term.text(t(lang, "vpnf_" + f.lower()), secret="KEY" in f or "PASSWORD" in f,
                                          hint=keep if same else "",
                                          validate=lambda v: None if same or v.strip() else t(lang, "err_vpn_fields"))
            data.update(vpn_provider=provider, vpn_fields=fields)
        elif pick == "tailscale":
            choice = "new"
            if s.tailscale_key:
                choice = term.select(t(lang, "field_remote"), [("new", t(lang, "field_tailscale_key")),
                                                               ("remove", t(lang, "settings_remove_tailscale"))])
            if choice == "remove":
                data.update(remove_tailscale=True, tailscale_key="")
            else:
                data["remove_tailscale"] = False
                data["tailscale_key"] = term.text(
                    t(lang, "field_tailscale_key"), secret=True, hint=keep if s.tailscale_key else t(lang, "cli_skip"),
                    validate=lambda v: None if not v or v.strip().startswith("tskey-") else t(lang, "err_tailscale_key"))


def _change_names(lang: str, chg: dict, new) -> list[str]:
    from installer.web import change_lines
    return change_lines(lang, chg, new)


def _apply_tui(term, flow, lang: str, s, new, chg, host: str = "localhost") -> tuple[int, Notes] | None:
    """Summary, checks, current passwords if needed, then the worker's progress. None: back to the settings."""
    import time
    from installer import reconfigure
    from installer.tui import Steps
    term.page(t(lang, "settings_summary"))
    for line in _change_names(lang, chg, new):
        term.note(line)
    term.note(t(lang, reconfigure.outage(chg)))
    found = flow.settings_check(s, new)
    for key in found:
        term.note(t(lang, key, path=new.data_dir), "error" if key.startswith("err_") else "warn")
    term.line()
    if any(k.startswith("err_") for k in found):
        term.pause(t(lang, "cli_back"))
        return None
    current = {}
    if "account" in chg:
        for app in flow.check_logins(s):
            term.note(t(lang, "settings_current_password_help", app=APP_NAMES[app]), "warn")
            current[app] = term.text(t(lang, "settings_current_password", app=APP_NAMES[app]), secret=True)
    if not term.confirm(t(lang, "settings_apply") + " ?", t(lang, "cli_yes_word"), t(lang, "cli_no_word"),
                        default=not found):
        return None
    flow.start_apply(new, current)
    todo = reconfigure.steps(chg, bool(new.vpn_provider))
    steps = Steps(term, [(key, t(lang, key)) for key in todo])
    while True:
        status = flow.apply_status()
        events = status["events"]
        if events:
            steps.advance(events[-1]["step"])
        if status["state"] == "done":
            steps.finish()
            missed = [a for a in (events[-1].get("detail") or "").split(",") if a in PORTS]
            notes = [(t(lang, "settings_done"), "ok")]
            if missed:  # one line per app, with where to change it by hand
                colon = " : " if lang == "fr" else ": "
                notes.append((t(lang, "settings_manual_title") + colon + t(lang, "settings_manual_help"), "warn"))
                notes += [(f"{APP_NAMES[a]}  http://{host}:{PORTS[a]}", "info") for a in missed]
            kept = events[-1].get("kept") or []
            if kept:
                colon = " : " if lang == "fr" else ": "
                notes.append((t(lang, "settings_kept_title") + colon + ", ".join(kept), "warn"))
                notes.append((t(lang, "settings_kept_help"), "info"))
            if "step_profiles" in todo:
                notes.append((t(lang, "settings_other_version"), "info"))
            return 0, notes
        if status["state"] == "failed":
            steps.failed = True
            steps.render()
            term._settle()
            return 1, [(t(lang, "err_apply_failed"), "error"), (events[-1].get("detail", "") if events else "", "detail")]
        steps.render()
        time.sleep(0.3)


def _pick_trackers(term, flow, lang: str) -> None:
    term.note(t(lang, "trackers_rights"))
    try:
        catalogue = flow.tracker_catalog()
    except Exception:
        term.note(t(lang, "trackers_error"), "error")
        term.pause(t(lang, "cli_back"))
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


def _wait_removal(term, flow, lang: str, steps, done_text: str) -> tuple[int, Notes]:
    import time
    while True:
        state, detail = flow.uninstall_status()
        if state == "failed":
            steps.failed = True
            steps.render()
            term._settle()
            return 1, [(t(lang, "err_uninstall_failed"), "error"), (detail, "detail")]
        if state in ("done", "idle"):
            steps.finish()
            return 0, [(done_text, "ok")]
        steps.render()
        time.sleep(0.12)


def _remove_steps(term, flow, lang: str, path: str, size: str) -> tuple[int, Notes]:
    """Confirmation, then removal with live progress, on the current page."""
    from installer.tui import Steps
    yes, no = t(lang, "cli_yes_word"), t(lang, "cli_no_word")
    if not term.confirm(t(lang, "uninstall_confirm"), yes, no):
        return 1, [(t(lang, "cli_cancelled"), "info")]
    remove_media = bool(path) and term.confirm(t(lang, "uninstall_media", path=path, size=size), yes, no)
    term.line()
    flow.uninstall(remove_media, lambda p: None)
    return _wait_removal(term, flow, lang, Steps(term, [("rm", t(lang, "cli_uninstall_wait"))]),
                         t(lang, "uninstall_done"))


def run_uninstall_tui(term, flow, lang: str, path: str = "", size: str = "") -> int:
    code, notes = 1, [(t(lang, "cli_cancelled"), "info")]
    with term.fullscreen():
        try:
            term.page(t(lang, "uninstall_title"), t(lang, "uninstall_text"))
            code, notes = _remove_steps(term, flow, lang, path, size)
        except KeyboardInterrupt:
            pass
    _print_notes(term, notes)
    return code


def _undo_steps(term, flow, lang: str, cancel, worker) -> Notes:
    """Stops the install at its next step, then removes everything it created (the media folder only
    if the install made it)."""
    from installer.tui import Steps
    cancel.set()
    term.line()
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
    return _wait_removal(term, flow, lang, steps, t(lang, "cli_rollback_done"))[1]


def undo_install(term, flow, lang: str, cancel, worker) -> int:
    """Rollback outside a full-screen page. Returns 1: the install did not happen."""
    _print_notes(term, _undo_steps(term, flow, lang, cancel, worker))
    return 1
