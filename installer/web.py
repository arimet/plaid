"""The assistant: a form until the stack exists, then a password-protected panel with the links and Remove."""
from __future__ import annotations

import hmac
import html
import json
import mimetypes
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from installer import flow as default_flow
from installer import media
from installer.compose import PORTS, SERVICES
from installer.flow import Progress, WorkerBusy
from installer.i18n import APP_NAMES, T, t
from installer.settings import Settings, validate
from installer.vpn import PROVIDERS

STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "plaid_session"
DAILY_APPS = ("jellyfin", "seerr")
APP_COLORS = {"jellyfin": "var(--plum)", "seerr": "var(--navy)", "radarr": "var(--gold)", "sonarr": "var(--teal)",
              "qbittorrent": "var(--green)", "prowlarr": "var(--rust)", "bazarr": "var(--red)"}
PROVIDER_COLORS = {"protonvpn": "#6d4aff", "private internet access": "#4bb543", "mullvad": "#e8b230",
                   "nordvpn": "#4687ff", "surfshark": "#1ec2b0", "windscribe": "#22305e"}
DEFAULT_DIRS = {"windows": "{home}\\Plaid", "synology": "/volume1/Plaid"}
CHECK = ('<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">'
         '<path d="M2 6.3l2.5 2.5L10 3.4" stroke-width="2" fill="none"/></svg>')
E = html.escape


def human_size(n: int, lang: str) -> str:
    unit = "o" if lang == "fr" else "B"
    for prefix in ("", "K", "M", "G", "T"):
        if n < 1024 or prefix == "T":
            value = f"{n:.1f}" if prefix and n < 10 else f"{n:.0f}"
            return f"{value.replace('.', ',') if lang == 'fr' else value} {prefix}{unit}"
        n /= 1024
    return ""


class State:
    """What the server remembers between requests: sessions and the running install."""

    def __init__(self, flow, env):
        self.flow = flow
        self.env = env
        self.sessions: set[str] = set()
        self.events: list[Progress] = []
        self.running = False
        self.lock = threading.Lock()

    def report(self, p: Progress) -> None:
        with self.lock:
            self.events.append(p)
            if p.done or p.error:
                self.running = False

    def start_install(self, s: Settings) -> None:
        with self.lock:
            self.events, self.running = [], True
        threading.Thread(target=self.flow.install, args=(s, self.report), daemon=True).start()

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions.add(token)
        return token


def header_menu(lang: str) -> str:
    """The "…" button in the header: the pages people rarely need stay out of the main page."""
    items = "".join(
        f'<a class="more{" danger" if path == "/remove" else ""}" href="{path}"><b>{E(t(lang, title))}</b>'
        f'<small>{E(t(lang, desc))}</small></a>'
        for path, title, desc in (("/remote", "remote_done_title", "nav_remote_desc"),
                                  ("/trackers", "trackers_title", "nav_trackers_desc"),
                                  ("/settings", "nav_settings", "nav_settings_desc"),
                                  ("/remove", "uninstall_title", "nav_remove_desc")))
    dots = ('<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="4" cy="10" r="2"/>'
            '<circle cx="10" cy="10" r="2"/><circle cx="16" cy="10" r="2"/></svg>')
    return (f'<details class="menu"><summary aria-label="{E(t(lang, "nav_more"))}">{dots}</summary>'
            f'<nav class="mores">{items}</nav></details>')


def page(lang: str, body: str, *, cloth: str = "", p: int = 100, foot: str = "", script: bool = True,
         menu: str = "", head: str = "", wide: bool = False) -> str:
    current = ' aria-current="true"'
    links = "".join(f'<a href="?lang={code}"{current if code == lang else ""}>{code.upper()}</a>' for code in T)
    texts = json.dumps({k: v for k, v in T[lang].items()}, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plaid</title><link rel="icon" href="/static/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/static/fonts.css">
<link rel="stylesheet" href="/static/style.css">{head}</head><body{' class="wide"' if wide else ""}>
<header class="topbar"><a class="brand" href="/"><span class="mark"><img src="/static/favicon.svg" alt="" width="26" height="26"></span><b>Plaid</b></a>
<div class="top">{menu}<nav class="lang" aria-label="Language">{links}</nav></div></header>
{f'<div class="track topline" aria-hidden="true"{"" if p >= 40 else " hidden"}><div class="fill" id="weave" style="--w:{p}%"></div></div>' if cloth == "part" else ""}
<main id="main">{body}</main>{foot}
<script id="texts" type="application/json">{texts}</script>
{'<script src="/static/app.js"></script>' if script else ''}
</body></html>"""


def default_data_dir(env: dict) -> str:
    platform = env.get("HOST_PLATFORM", "linux")
    return DEFAULT_DIRS.get(platform, "{home}/Plaid").format(home=env.get("HOST_HOME", ""))


REMOTE_PLATFORMS = ("linux", "synology")  # Docker Desktop cannot put a container on the host network


def steps_html(lang: str, key: str) -> str:
    return "<ol>" + "".join(f"<li>{E(step)}</li>" for step in t(lang, key).split("|")) + "</ol>"


def _lang_select(lang: str, name: str, value: str) -> str:
    options = "".join(f'<option value="{code}"{" selected" if code == value else ""}>{E(t(lang, "lang_" + code))}</option>'
                      for code in media.LANGS)
    return f'<select class="input" id="{name}" name="{name}">{options}</select>'


def media_fields(lang: str, s: Settings) -> str:
    """Audio language, the 3 x 3 profile grid with its default, subtitles, fiches: the wizard and the settings."""
    e = media.effective(s)
    head = "".join(f"<th scope=\"col\">{'4K' if q == '4k' else q}</th>" for q in media.QUALITIES)
    rows = ""
    for mode in media.MODES:
        cells = ""
        for q in media.QUALITIES:
            key = f"{mode}-{q}"
            name = media.profile_name(key, lang, e.audio_lang)
            cells += (f'<td><label class="tick"><input type="checkbox" name="profile_{key}"'
                      f'{" checked" if key in e.profiles else ""} aria-label="{E(name)}"></label>'
                      f'<label class="dflt"><input type="radio" name="default_profile" value="{key}"'
                      f'{" checked" if key == e.default_profile else ""} '
                      f'aria-label="{E(t(lang, "field_default_profile"))} : {E(name)}">'
                      f'<span>{E(t(lang, "media_default"))}</span></label></td>')
        rows += f'<tr><th scope="row">{E(t(lang, "media_row_" + mode))}</th>{cells}</tr>'
    subs = "".join(f'<label class="pill"><input type="checkbox" name="sub_{code}"'
                   f'{" checked" if code in e.subtitle_langs else ""}><span>{E(t(lang, "lang_" + code))}</span></label>'
                   for code in media.LANGS)
    return f"""<input type="hidden" name="media" value="1">
  <div class="field"><label class="l" for="audio_lang">{E(t(lang, "field_audio_lang"))}</label>
  {_lang_select(lang, "audio_lang", e.audio_lang)}<p class="hint">{E(t(lang, "field_audio_lang_help"))}</p></div>
  <fieldset class="field"><legend class="l">{E(t(lang, "field_profiles"))}</legend>
  <div class="grid-wrap"><table class="pgrid"><thead><tr><td></td>{head}</tr></thead><tbody>{rows}</tbody></table></div>
  <p class="hint">{E(t(lang, "field_profiles_help"))}</p><p class="ferr" data-err="profiles"></p></fieldset>
  <fieldset class="field"><legend class="l">{E(t(lang, "field_subtitles"))}</legend>
  <div class="pills">{subs}</div><p class="hint">{E(t(lang, "field_subtitles_help"))}</p>
  <p class="ferr" data-err="subtitles"></p></fieldset>
  <div class="field"><label class="l" for="metadata_lang">{E(t(lang, "field_metadata_lang"))}</label>
  {_lang_select(lang, "metadata_lang", e.metadata_lang)}<p class="hint">{E(t(lang, "field_metadata_lang_help"))}</p></div>"""


def picker_html(lang: str, create: bool, field: str = "data_dir") -> str:
    """The disks and folders panel under a folder field; app.js fills it. `create`: offer a new Plaid folder."""
    here = (f'<button type="button" class="btn small" data-here>{E(t(lang, "picker_here"))}</button>'
            if create else "")
    flag = (' data-create="1"' if create else "") + (f' data-for="{field}"' if field != "data_dir" else "")
    return (f'<div class="picker" data-picker{flag}>'
            f'<button type="button" class="linkish" data-picker-open aria-expanded="false">{E(t(lang, "picker_open"))}</button>'
            f'<div class="picker-panel" hidden><p class="picker-where" aria-live="polite"></p><ul class="picker-list"></ul>'
            f'<div class="picker-actions" hidden><button type="button" class="btn small" data-use>'
            f'{E(t(lang, "picker_use"))}</button>{here}</div></div></div>')


def form_page(lang: str, env: dict, running: bool = False, with_vpn: bool = True) -> str:
    default_dir = default_data_dir(env)
    remote = env.get("HOST_PLATFORM", "linux") in REMOTE_PLATFORMS
    last = 5 if remote else 4  # the films and series step is the last question
    remote_section = f"""
<section data-step="4" hidden>
  <div class="step-head">
  <h1>{E(t(lang, "field_remote"))}</h1>
  <p class="lead">{E(t(lang, "remote_help"))}</p>
  <div class="tip">{steps_html(lang, "remote_steps")}
  <p><a href="https://login.tailscale.com/admin/settings/keys" target="_blank" rel="noopener">login.tailscale.com</a></p></div></div>
  <div class="step-body">
  <div class="field"><label class="l" for="tailscale_key">{E(t(lang, "field_tailscale_key"))}</label>
  <input class="input" id="tailscale_key" type="password" autocomplete="off" spellcheck="false" placeholder="tskey-auth-…">
  <p class="ferr" data-err="tailscale"></p></div>
  </div>
</section>""" if remote else ""
    chips = "".join(
        f'<button type="button" class="chip" role="radio" aria-checked="{str(pid == "protonvpn").lower()}" '
        f'data-provider="{E(pid)}" data-pf="{int(p.port_forwarding)}"><span class="sw" style="--c:{PROVIDER_COLORS[pid]}">'
        f'</span>{E(p.label)}</button>' for pid, p in PROVIDERS.items())
    chips += (f'<button type="button" class="chip" role="radio" aria-checked="false" data-provider="">'
              f'<span class="sw"></span>{E(t(lang, "vpn_none"))}</button>')
    fields = ""
    for pid, p in PROVIDERS.items():
        inputs = "".join(
            f'<div class="field"><label class="l" for="f-{i}-{n}">{E(t(lang, "vpnf_" + f.lower()))}</label>'
            f'<input class="input" id="f-{i}-{n}" data-vpn-field="{f}" autocomplete="off" '
            f'type="{"password" if "KEY" in f or "PASSWORD" in f else "text"}"></div>'
            for n, f in enumerate(p.fields) for i in [list(PROVIDERS).index(pid)])
        fields += f'<div class="vpn-fields" data-for="{E(pid)}" {"" if pid == "protonvpn" else "hidden"}>{inputs}</div>'
    pf_icon = ('<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="8" '
               'fill="var(--green)"/><path d="M5 9.3l2.6 2.6L13 6.6" stroke="#fff" stroke-width="2" fill="none"/></svg>')
    warn_icon = ('<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><path d="M9 1.5l8 14H1z" '
                 'fill="var(--gold)"/><path d="M9 6.5v4.5M9 12.6v1.4" stroke="#1b1a22" stroke-width="2"/></svg>')
    body = f"""
<form id="wizard" novalidate data-last="{last}">
<div class="stitches" id="stitches" aria-live="polite">{'<span class="st"></span>' * last}
<span id="stepof"></span></div>

<section data-step="1" class="field-group">
  <div class="step-head">
  <h1>{E(t(lang, "field_folder"))}</h1>
  <p class="lead">{E(t(lang, "subtitle"))}</p></div>
  <div class="step-body">
  <div class="field"><label class="l" for="data_dir">{E(t(lang, "field_folder"))}</label>
  <input class="input" id="data_dir" name="data_dir" value="{E(default_dir)}" autocomplete="off" spellcheck="false">
  {picker_html(lang, True)}
  <p class="hint">{E(t(lang, "field_folder_help"))}</p><p class="ferr" data-err="data_dir"></p></div>
  </div>
</section>

<section data-step="2" hidden>
  <div class="step-head">
  <h1>{E(t(lang, "field_username"))} · {E(t(lang, "field_password"))}</h1>
  <p class="lead">{E(t(lang, "field_account_help"))}</p></div>
  <div class="step-body">
  <div class="field"><label class="l" for="username">{E(t(lang, "field_username"))}</label>
  <input class="input" id="username" name="username" autocomplete="username" value="admin"><p class="ferr" data-err="username"></p></div>
  <div class="field"><label class="l" for="password">{E(t(lang, "field_password"))}</label>
  <input class="input" id="password" name="password" type="password" autocomplete="new-password"></div>
  <div class="field"><label class="l" for="password2">{E(t(lang, "field_password2"))}</label>
  <input class="input" id="password2" type="password" autocomplete="new-password"><p class="ferr" data-err="password"></p></div>
  </div>
</section>

<section data-step="3" hidden>
  <div class="step-head">
  <h1>{E(t(lang, "field_vpn"))}</h1>
  <p class="lead">{E(t(lang, "field_vpn_help"))}</p></div>
  <div class="step-body">
  <div class="prov" role="radiogroup" aria-label="{E(t(lang, "field_vpn"))}">{chips}</div>
  {fields}
  <p class="hint" id="vpn-where">{E(t(lang, "vpn_where"))}</p>
  <div class="note" id="pf-yes">{pf_icon}<span>{E(t(lang, "vpn_pf_yes"))}</span></div>
  <div class="note warn" id="pf-no" hidden>{warn_icon}<span>{E(t(lang, "vpn_pf_no"))}</span></div>
  <p class="ferr" data-err="vpn"></p>
  </div>
</section>
{remote_section}
<section data-step="{last}" hidden>
  <div class="step-head">
  <h1>{E(t(lang, "media_title"))}</h1>
  <p class="lead">{E(t(lang, "media_help"))}</p></div>
  <div class="step-body">
  {media_fields(lang, Settings(data_dir="", username="", password="", vpn_provider=None, lang=lang))}
  </div>
</section>
<section data-step="{last + 1}" hidden>
  <div class="step-head">
  <h1>{E(t(lang, "confirm_title"))}</h1></div>
  <div class="step-body">
  <div id="warnings" class="field-group"></div>
  </div>
</section>
<input type="hidden" name="timezone" id="timezone" value="UTC"><input type="hidden" name="lang" value="{lang}">
</form>

<section id="progress" hidden data-running="{int(running)}" data-vpn="{int(with_vpn)}">
  <div class="step-head"><h1 id="progress-title">{E(t(lang, "progress_title"))}</h1>
  <p class="lead" id="progress-lead">{E(t(lang, "progress_help"))}</p></div>
  <div class="step-body"><ul class="steps" id="steps"></ul>
  <pre class="detail" id="detail" hidden></pre>
  <a class="btn" href="/" id="to-panel" hidden>{E(t(lang, "uninstall_title"))}</a></div>
</section>"""
    foot = f"""<div class="foot" id="foot"><div class="foot-btns">
<button class="btn" type="button" id="back" hidden>{E(t(lang, "btn_back"))}</button>
<button class="btn primary" type="button" id="next">{E(t(lang, "btn_next"))}</button></div></div>"""
    return page(lang, body, cloth="part", p=40 if running else 8, foot=foot, wide=True)


def guard_notes(lang: str, s: Settings, status: dict) -> list[tuple[str, str, bool]]:
    """(title, help, show the confirm button) for each alert the guard raised, the disk first."""
    notes = []
    if status.get("disk") in ("missing", "unconfirmed"):
        key = "guard_disk_" + status["disk"]
        notes.append((t(lang, key), t(lang, key + "_help", path=s.data_dir), status["disk"] == "unconfirmed"))
    if status.get("vpn") == "down":
        notes.append((t(lang, "guard_vpn_down"), t(lang, "guard_vpn_down_help"), False))
    return notes


def guard_html(lang: str, s: Settings, status: dict) -> str:
    button = (f'<form method="post" action="/api/confirm-disk"><button class="btn" type="submit">'
              f'{E(t(lang, "guard_disk_confirm"))}</button></form>')
    return "".join(f'<div class="note alert"><div><b>{E(title)}</b><br>{E(text)}{button if confirm else ""}</div></div>'
                   for title, text, confirm in guard_notes(lang, s, status))


FULL_DISK = 20 * 10 ** 9  # under this, downloads soon stop


def backup_where(s: Settings) -> str:
    from installer import backup
    return backup.where(s)


def long_date(lang: str, when: datetime) -> str:
    month = t(lang, "months").split("|")[when.month - 1]
    return f"{when.day} {month} {when.year}" if lang == "fr" else f"{month} {when.day}, {when.year}"


def backup_date(lang: str, name: str) -> str:
    """"8 novembre 2026, 03:00" from plaid-backup-2026-11-08-0300.tar.gz."""
    when = datetime.strptime(name[len("plaid-backup-"):len("plaid-backup-") + 15], "%Y-%m-%d-%H%M")
    return f"{long_date(lang, when)}, {when:%H:%M}"


def backup_html(lang: str, s: Settings, st: dict) -> str:
    """The panel's backup block: the last one, or why the last try failed."""
    from installer import backup
    if st.get("failed_at") and (not st.get("at") or st["failed_at"] > st["at"]):
        reason = st.get("error") or ""
        line = t(lang, "backup_failed", reason=t(lang, reason) if reason.startswith("err_") else reason)
        kind = "note warn"
    elif st.get("at"):
        line = t(lang, "backup_last", date=long_date(lang, backup.local_now(s, datetime.fromtimestamp(st["at"], timezone.utc))),
                 size=human_size(st.get("size") or 0, lang), folder=backup.where(s))
        kind = "hint"
    else:
        line, kind = t(lang, "backup_none"), "hint"
    return (f'<section class="pcard"><h2>{E(t(lang, "backup_title"))}</h2><div class="maint">'
            f'<p class="hint">{E(t(lang, "backup_help"))}</p>'
            f'<p class="{kind}">{E(line)}</p><form method="post" action="/api/backup"><button class="btn" '
            f'type="submit">{E(t(lang, "backup_btn"))}</button></form>'
            f'<a href="/backups">{E(t(lang, "backup_restore_link"))}</a></div></section>')


def backups_page(lang: str, s: Settings, found: list[dict]) -> str:
    from installer import backup
    head = f'<h1>{E(t(lang, "backups_title"))}</h1><p class="lead">{E(t(lang, "backups_help"))}</p>'
    if not found:
        return subpage(lang, f'<p class="hint">{E(t(lang, "backups_none", folder=backup.where(s)))}</p>', side=head)
    rows = "".join(f'<li class="app-row"><span><b>{E(backup_date(lang, b["name"]))}</b><br><small>'
                   f'{E(human_size(b["size"], lang))}</small></span><a class="btn" href="/backups/confirm?name='
                   f'{E(urllib.parse.quote(b["name"]))}">{E(t(lang, "backups_pick"))}</a></li>' for b in found)
    return subpage(lang, f'<ul class="manual">{rows}</ul>', side=head)


def restore_confirm_page(lang: str, name: str) -> str:
    return subpage(lang, f'<form method="post" action="/api/restore">'
                         f'<input type="hidden" name="name" value="{E(name)}"><button class="btn danger" '
                         f'type="submit">{E(t(lang, "backups_confirm_btn"))}</button></form>',
                   side=f'<h1>{E(t(lang, "backups_confirm", date=backup_date(lang, name)))}</h1>'
                        f'<p class="lead">{E(t(lang, "backups_help"))}</p>')


def disk_html(lang: str, s: Settings, status: dict) -> str:
    """The media disk's room, from the guard's last round. None on a Mac external disk: no bar then."""
    free, total = status.get("free"), status.get("total")
    known = free is not None and bool(total)
    hide = lambda shown: "" if shown else " hidden"
    used = round(100 * (total - free) / total) if known else 0
    room = (t(lang, "picker_free", free=human_size(free, lang), total=human_size(total, lang)) + " · ") if known else ""
    # Every part is always there, hidden when unknown: the page's 30 s refresh fills them (app.js).
    return (f'<section class="pcard disk" data-disk data-full="{FULL_DISK}"><h2>{E(t(lang, "panel_disk"))}</h2>'
            f'<span class="meter"{hide(known)}><i style="width:{used}%"></i></span>'
            f'<p class="hint"><span data-room>{E(room)}</span>{E(s.data_dir)}</p>'
            f'<p class="note warn" data-full-note{hide(known and free < FULL_DISK)}>{E(t(lang, "panel_disk_full"))}</p>'
            f'</section>')


def maintenance_html(lang: str, last: dict | None) -> str:
    """Updating the apps, with the date of the last update that went through."""
    when = (t(lang, "update_last", date=time.strftime("%d/%m/%Y" if lang == "fr" else "%Y-%m-%d",
                                                      time.localtime(last["at"])))
            if last else t(lang, "update_never"))
    return (f'<section class="pcard"><h2>{E(t(lang, "maint_title"))}</h2><div class="maint">'
            f'<p class="hint">{E(t(lang, "update_help"))} {E(when)}</p>'
            f'<form method="post" action="/api/update"><button class="btn" type="submit">{E(t(lang, "update_btn"))}'
            f'</button></form><a href="/torrents">{E(t(lang, "torrents_link"))}</a></div></section>')


def panel_page(lang: str, s: Settings, host: str, complete: bool = True, status: dict | None = None,
               last_update: dict | None = None, backup_status: dict | None = None) -> str:
    states = (status or {}).get("apps") or {}

    def app_link(svc: str, daily: bool) -> str:
        state = states.get(svc, "unknown")
        return (f'<a class="app{" daily" if daily else ""}" href="http://{E(host)}:{PORTS[svc]}" target="_blank" '
                f'rel="noopener"><span class="ic" style="--c:{APP_COLORS[svc]}"><img src="/static/icons/{svc}.svg" '
                f'alt="" width="28" height="28"></span><span><b>{E(svc_name(svc))}</b> '
                f'<span class="role">{E(t(lang, "app_" + svc))}</span><small>{E(t(lang, "appd_" + svc))}</small>'
                f'<span class="state" data-app="{svc}" data-state="{state}"><i></i>'
                f'{E(t(lang, "state_" + state))}</span></span>'
                f'<span class="open">{E(t(lang, "done_open"))}</span></a>')

    # Beginners open two apps; the other five work on their own and are listed for when something needs a look.
    daily = [svc for svc in SERVICES if svc in DAILY_APPS]
    behind = [svc for svc in SERVICES if svc not in DAILY_APPS]
    apps = (f'<h2>{E(t(lang, "apps_daily"))}</h2><div class="apps daily-apps">{"".join(app_link(x, True) for x in daily)}</div>'
            f'<p class="lead">{E(t(lang, "apps_flow"))}</p>'
            f'<h2>{E(t(lang, "apps_behind"))}</h2><p class="hint">{E(t(lang, "apps_behind_help"))}</p>'
            f'<div class="apps behind-apps">{"".join(app_link(x, False) for x in behind)}</div>')
    if not complete:  # a failed install: removal is the only thing to do
        body = f"""<h1>{E(t(lang, "progress_failed"))}</h1><p class="lead">{E(t(lang, "err_unexpected"))}</p>
  <a class="btn danger" href="/remove">{E(t(lang, "uninstall_title"))}</a>"""
        return subpage(lang, body, back=False, menu=False)
    autostart = (f'<p class="note warn">{E(t(lang, "autostart_hint"))}</p>'
                 if s.platform in ("mac", "windows") else "")
    # With the disk gone the apps that read it are stopped: neither "ready" nor "running" holds then.
    stopped = (status or {}).get("disk") in ("missing", "unconfirmed")
    title, lead = ("panel_title", "") if stopped else ("done_title", E(t(lang, "done_lead")) + " ")
    body = f"""<h1>{E(t(lang, title))}</h1>
  {guard_html(lang, s, status or {})}
  <p class="lead">{lead}{t(lang, "done_login", username="<b>" + E(s.username) + "</b>")}</p>
  {apps}
  <div class="pgrid-foot">{disk_html(lang, s, status or {})}
  {maintenance_html(lang, last_update)}
  {backup_html(lang, s, backup_status or {})}</div>
  {autostart}"""
    return subpage(lang, body, back=False, wide=True)


def subpage(lang: str, body: str, back: bool = True, menu: bool = True, head: str = "", wide: bool = False,
            side: str = "") -> str:
    """`side`: the page's title and explanation, beside `body` on a computer screen, above it on a phone."""
    link = f'<a class="back" href="/">‹ {E(t(lang, "nav_back"))}</a>' if back else ""
    if side:
        body, wide = f'<div class="split"><div class="step-head">{side}</div><div class="step-body">{body}</div></div>', True
    return page(lang, f'<section id="done">{link}{body}</section>', foot='<div style="height:32px"></div>',
                menu=header_menu(lang) if menu else "", head=head, wide=wide)


# ---------------------------------------------------------------- settings

LANG_NAMES = {"en": "English", "fr": "Français"}
ACCOUNT_PROMPTS = ("jellyfin", "qbittorrent")  # the apps Plaid signs in to with a password, not a key


def settings_data(form: dict) -> dict:
    """The settings form as reconfigure.merge reads it: VPN fields are named vpn_<provider>_<FIELD>."""
    provider = form.get("vpn_provider", "")
    prefix = f"vpn_{provider}_"
    return {**{k: form[k] for k in ("data_dir", "username", "password", "timezone", "lang", "tailscale_key",
                                    "backup_dir")
               if k in form},
            "vpn_provider": provider,
            "vpn_fields": {k[len(prefix):]: v for k, v in form.items() if provider and k.startswith(prefix)},
            "remove_tailscale": bool(form.get("remove_tailscale")),
            **_media_data(form)}


def _media_data(form: dict) -> dict:
    """Unticked boxes are not sent: the hidden "media" field says the group was there, so none ticked means none."""
    if "media" not in form:
        return {}
    return {"audio_lang": form.get("audio_lang"), "metadata_lang": form.get("metadata_lang"),
            "default_profile": form.get("default_profile"),
            "profiles": [k for k in media.KEYS if form.get(f"profile_{k}")],
            "subtitle_langs": [c for c in media.LANGS if form.get(f"sub_{c}")]}


def _field(label: str, name: str, value: str = "", kind: str = "text", hint: str = "") -> str:
    return (f'<div class="field"><label class="l" for="{name}">{E(label)}</label>'
            f'<input class="input" id="{name}" name="{name}" type="{kind}" value="{E(value)}" autocomplete="off" '
            f'spellcheck="false">{f"<p class={chr(34)}hint{chr(34)}>{E(hint)}</p>" if hint else ""}</div>')


def settings_page(lang: str, s: Settings, note: str = "", shown: Settings | None = None) -> str:
    """Every setting, filled in (with `shown`, what the user typed before going back from the summary).
    Secrets are never sent back: an empty field keeps the current one."""
    shown = shown or s
    keep = t(lang, "settings_keep_secret")
    langs = "".join(f'<option value="{code}"{" selected" if code == shown.lang else ""}>{name}</option>'
                    for code, name in LANG_NAMES.items())
    options = "".join(f'<option value="{pid}"{" selected" if pid == shown.vpn_provider else ""}>{E(p.label)}</option>'
                      for pid, p in PROVIDERS.items())
    options += f'<option value=""{"" if shown.vpn_provider else " selected"}>{E(t(lang, "vpn_none"))}</option>'
    vpn_fields = "".join(
        f'<div class="vpnf" data-for="{pid}">' + "".join(
            _field(t(lang, "vpnf_" + f.lower()), f"vpn_{pid}_{f}", kind="password" if "KEY" in f or "PASSWORD" in f
                   else "text", hint=t(lang, "settings_keep_key") if pid == s.vpn_provider else "")
            for f in p.fields) + "</div>"
        for pid, p in PROVIDERS.items())
    # Without JavaScript: only the chosen provider's fields show.
    visible = "".join(f'form:has(#vpn_provider option[value="{pid}"]:checked) .vpnf[data-for="{pid}"]{{display:block}}'
                    for pid in PROVIDERS)
    def group(title: str, desc: str, fields: str) -> str:
        return (f'<section class="sgroup"><header><h2>{E(title)}</h2><p class="hint">{E(desc)}</p></header>'
                f'<div class="sfields">{fields}</div></section>')
    remote = ""
    if s.platform in REMOTE_PLATFORMS:
        drop = (f'<label class="check"><input type="checkbox" name="remove_tailscale"><div>'
                f'<b>{E(t(lang, "settings_remove_tailscale"))}</b>'
                f'<span>{E(t(lang, "settings_remove_tailscale_help"))}</span></div></label>' if s.tailscale_key else "")
        remote = group(t(lang, "settings_remote_title"), t(lang, "nav_remote_desc"),
                       _field(t(lang, "field_tailscale_key"), "tailscale_key", kind="password",
                              hint=t(lang, "settings_keep_key") if s.tailscale_key else t(lang, "settings_optional"))
                       + drop)
    sep = '<hr class="sep">'
    body = f"""<h1>{E(t(lang, "nav_settings"))}</h1>
  <p class="lead">{E(t(lang, "settings_lead"))}</p>
  {f'<p class="note">{E(note)}</p>' if note else ""}
  <form method="post" action="/settings" class="settings">
  {group(t(lang, "settings_group_general"), t(lang, "settings_group_general_desc"),
         f'<div class="field"><label class="l" for="lang">{E(t(lang, "settings_lang"))}</label>'
         f'<select class="input" id="lang" name="lang">{langs}</select></div>'
         + _field(t(lang, "field_timezone"), "timezone", shown.timezone, hint=t(lang, "settings_timezone_help")))}
  {sep}
  {group(t(lang, "media_title"), t(lang, "media_help"), media_fields(lang, shown))}
  {sep}
  {group(t(lang, "field_folder"), t(lang, "settings_folder_desc"),
         _field(t(lang, "settings_folder_label"), "data_dir", shown.data_dir, hint=t(lang, "settings_folder_help"))
         + picker_html(lang, False))}
  {sep}
  {group(t(lang, "backup_title"), t(lang, "backup_help"),
         f'<div class="field"><label class="l" for="backup_dir">{E(t(lang, "field_backup_dir"))}</label>'
         f'<input class="input" id="backup_dir" name="backup_dir" value="{E(shown.backup_dir or "")}" '
         f'placeholder="{E(backup_where(shown))}" autocomplete="off" spellcheck="false">'
         f'<p class="hint">{E(t(lang, "field_backup_dir_help"))}</p></div>' + picker_html(lang, False, "backup_dir"))}
  {sep}
  {group(t(lang, "settings_group_account"), t(lang, "settings_group_account_desc"),
         _field(t(lang, "field_username"), "username", shown.username)
         + _field(t(lang, "settings_new_password"), "password", kind="password", hint=keep)
         + _field(t(lang, "settings_new_password2"), "password2", kind="password"))}
  {sep}
  {group(t(lang, "field_vpn"), t(lang, "settings_vpn_desc"),
         f'<div class="field"><label class="l" for="vpn_provider">{E(t(lang, "settings_vpn_provider"))}</label>'
         f'<select class="input" id="vpn_provider" name="vpn_provider">{options}</select></div>' + vpn_fields)}
  {sep + remote if remote else ""}
  <button class="btn primary" type="submit">{E(t(lang, "settings_review"))}</button>
  </form>"""
    return subpage(lang, body, head=f"<style>.vpnf{{display:none}}{visible}</style>", wide=True)


def change_lines(lang: str, chg: dict[str, str], new: Settings) -> list[str]:
    value = {"lang": LANG_NAMES[new.lang], "timezone": new.timezone, "folder": new.data_dir,
             "backup": backup_where(new),
             "vpn": PROVIDERS[new.vpn_provider].label if new.vpn_provider else ""}
    return [t(lang, f"change_{key}" + (f"_{kind}" if key in ("vpn", "tailscale") else ""), value=value.get(key, ""))
            for key, kind in chg.items()]


ARROW = ('<svg class="arrow" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">'
         '<path d="M3 8h9M8.5 4.5L12 8l-3.5 3.5" stroke-width="1.8" fill="none" stroke-linecap="round" '
         'stroke-linejoin="round"/></svg>')
CLOCK = ('<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="7.2" '
         'stroke-width="1.6" fill="none"/><path d="M9 5v4l2.6 1.6" stroke-width="1.6" fill="none" '
         'stroke-linecap="round"/></svg>')


def change_rows(lang: str, chg: dict[str, str], old: Settings, new: Settings) -> list[tuple[str, str, str]]:
    """(what, before, after) for each change; before is "" when it means nothing to show (a new key)."""
    vpn = lambda x: PROVIDERS[x.vpn_provider].label if x.vpn_provider else t(lang, "vpn_none")
    on_off = lambda key: t(lang, "settings_on") if key else t(lang, "settings_off")
    rows = []
    if "lang" in chg:
        rows.append((t(lang, "settings_lang"), LANG_NAMES[old.lang], LANG_NAMES[new.lang]))
    if "timezone" in chg:
        rows.append((t(lang, "field_timezone"), old.timezone, new.timezone))
    if "folder" in chg:
        rows.append((t(lang, "field_folder"), old.data_dir, new.data_dir))
    if "backup" in chg:
        rows.append((t(lang, "field_backup_dir"), backup_where(old), backup_where(new)))
    if old.username != new.username:
        rows.append((t(lang, "field_username"), old.username, new.username))
    if old.password != new.password:
        rows.append((t(lang, "field_password"), "", t(lang, "settings_password_new")))
    if "vpn" in chg:
        rows.append((t(lang, "field_vpn"), "", t(lang, "settings_new_key")) if chg["vpn"] == "change" and
                    old.vpn_provider == new.vpn_provider else (t(lang, "field_vpn"), vpn(old), vpn(new)))
    if "tailscale" in chg:
        rows.append(("Tailscale", "", t(lang, "settings_new_key")) if chg["tailscale"] == "change" else
                    ("Tailscale", on_off(old.tailscale_key), on_off(new.tailscale_key)))
    rows += _media_rows(lang, chg, old, new)
    return rows


def _media_rows(lang: str, chg: dict[str, str], old: Settings, new: Settings) -> list[tuple[str, str, str]]:
    """Before is "" when the old install never made these choices."""
    a, b = media.effective(old), media.effective(new)
    was = media.configured(old)
    before = lambda value: value if was else ""
    name = lambda e, key: media.profile_name(key, lang, e.audio_lang)
    langs = lambda codes: ", ".join(t(lang, "lang_" + c) for c in codes)
    rows = []
    if "profiles" in chg:
        if a.audio_lang != b.audio_lang:
            rows.append((t(lang, "field_audio_lang"), before(t(lang, "lang_" + a.audio_lang)),
                         t(lang, "lang_" + b.audio_lang)))
        names = lambda e: ", ".join(name(e, k) for k in media.KEYS if k in e.profiles)
        if names(a) != names(b) or not was:
            rows.append((t(lang, "field_profiles"), before(names(a)), names(b)))
        if name(a, a.default_profile) != name(b, b.default_profile) or not was:
            rows.append((t(lang, "field_default_profile"), before(name(a, a.default_profile)),
                         name(b, b.default_profile)))
    if "subtitles" in chg:
        rows.append((t(lang, "field_subtitles"), before(langs(a.subtitle_langs)), langs(b.subtitle_langs)))
    if "metadata" in chg:
        rows.append((t(lang, "field_metadata_lang"), before(t(lang, "lang_" + a.metadata_lang)),
                     t(lang, "lang_" + b.metadata_lang)))
    return rows


def settings_summary(lang: str, form: dict, chg: dict[str, str], old: Settings, new: Settings, found: list[str],
                     ask: list[str]) -> str:
    """What will change (before → after), the outage, the checks. The form carries back only what was typed,
    to Apply or to Edit (which reopens the settings with it)."""
    from installer.reconfigure import outage
    rows = "".join(
        f'<li><span class="k">{E(what)}</span><span class="v">'
        + (f"<del>{E(before)}</del>{ARROW}" if before else "") + f"<b>{E(after)}</b></span></li>"
        for what, before, after in change_rows(lang, chg, old, new))
    downtime = outage(chg)
    notes = "".join(f'<div class="note {"alert" if k.startswith("err_") else "warn"}"><div>'
                    f'{E(t(lang, k, path=new.data_dir))}</div></div>' for k in found)
    blocked = any(k.startswith("err_") for k in found)
    hidden = "".join(f'<input type="hidden" name="{E(k)}" value="{E(v)}">' for k, v in form.items())
    prompts = "" if blocked else "".join(
        _field(t(lang, "settings_current_password", app=APP_NAMES[app]), f"current_{app}", kind="password",
               hint=t(lang, "settings_current_password_help", app=APP_NAMES[app])) for app in ask)
    apply = "" if blocked else f'<button class="btn primary" type="submit">{E(t(lang, "settings_apply"))}</button>'
    side = f'<h1>{E(t(lang, "settings_summary"))}</h1><p class="lead">{E(t(lang, "settings_summary_lead"))}</p>'
    body = f"""<ul class="diff">{rows}</ul>
  <div class="note{"" if downtime == "outage_none" else " warn"} outage">{CLOCK}<span>{E(t(lang, downtime))}</span></div>
  {notes}
  <form method="post" action="/settings/apply" class="settings">{hidden}{prompts}
  <div class="actions">{apply}
  <button class="btn" type="submit" formaction="/settings/edit">{E(t(lang, "settings_edit"))}</button></div>
  </form>"""
    return subpage(lang, body, side=side)


def update_lines(lang: str, last: dict) -> list[tuple[str, str]]:
    """What an update did, as (text, "ok" | "warn"): for the progress page and the terminal."""
    names = lambda key: [APP_NAMES.get(a, a) for a in last.get(key) or []]
    changed = [APP_NAMES.get(a, a) for a in (last.get("detail") or "").split(",") if a]
    waiting, skipped = names("waiting"), names("skipped")
    lines = [(t(lang, "update_changed", apps=", ".join(changed)), "ok")] if changed else []
    if waiting:
        key = "update_waiting_one" if len(waiting) == 1 else "update_waiting_many"
        lines.append((t(lang, key, apps=", ".join(waiting)), "warn"))
    if skipped:
        lines.append((t(lang, "update_skipped", apps=", ".join(skipped)), "warn"))
    return lines or [(t(lang, "update_none"), "ok")]


def settings_progress(lang: str, status: dict, host: str = "localhost") -> str:
    events, state_ = status.get("events", []), status.get("state")
    last = events[-1] if events else {}
    shown = [e for e in events if e.get("step") != "step_done" and not e.get("error")]

    def knot(i: int) -> str:  # same marks as the install: done, current, failed
        if i < len(shown) - 1 or state_ == "done":
            return f'<span class="knot ok">{CHECK}</span>'
        return '<span class="knot bad"></span>' if state_ == "failed" else '<span class="knot now"></span>'
    steps = "".join(f'<li>{knot(i)}<span>{E(t(lang, e["step"]))}</span></li>' for i, e in enumerate(shown))
    kind = {"step_pull": "update", "step_backup_stop": "backup", "step_restore_stop": "restore"}.get(
        events[0].get("step") if events else "")
    updating = kind == "update"
    if state_ == "running" or (state_ == "idle" and not events):
        title = t(lang, f"{kind}_running" if kind else "settings_running")
        extra, head = "", '<meta http-equiv="refresh" content="2">'
    elif state_ == "done" and kind in ("backup", "restore"):
        name = last.get("detail") or ""
        title, head = t(lang, f"{kind}_done"), ""
        text = (t(lang, "backup_saved", name=name) if kind == "backup"
                else t(lang, "restore_back", date=backup_date(lang, name)))
        extra = f'<p class="lead">{E(text)}</p>'
    elif state_ == "done" and updating:
        title, head = t(lang, "update_done"), ""
        extra = "".join(f'<p class="{"lead" if kind == "ok" else "note warn"}">{E(text)}</p>'
                        for text, kind in update_lines(lang, last))
    elif state_ == "done":
        missed = [a for a in (last.get("detail") or "").split(",") if a in PORTS]
        title, head = t(lang, "settings_done"), ""
        # The apps that kept the old password: one row each, with a link to change it there.
        rows = "".join(f'<li class="app-row"><b>{E(APP_NAMES[a])}</b><a class="btn" href="http://{E(host)}:{PORTS[a]}" '
                       f'target="_blank" rel="noopener">{E(t(lang, "done_open"))}</a></li>' for a in missed)
        extra = (f'<div class="note warn"><div><b>{E(t(lang, "settings_manual_title"))}</b><br>'
                 f'{E(t(lang, "settings_manual_help"))}</div></div><ul class="manual">{rows}</ul>' if missed else "")
        kept = last.get("kept") or []
        if kept:
            extra += (f'<div class="note warn"><div><b>{E(t(lang, "settings_kept_title"))}</b><br>'
                      f'{E(", ".join(kept))}<br>{E(t(lang, "settings_kept_help"))}</div></div>')
        if any(e.get("step") == "step_profiles" for e in events):
            extra += f'<p class="hint">{E(t(lang, "settings_other_version"))}</p>'
    else:
        error = last.get("error") or ""
        # A crashed worker gets the settings' generic error: a backup or a restore says its own.
        own = kind in ("backup", "restore") and error.startswith("err_") and error != "err_apply_failed"
        title = t(lang, error if own else f"err_{kind}_failed" if kind else "err_apply_failed")
        head = ""
        extra = f'<pre class="detail">{E(last.get("detail") or "")}</pre>'
    return subpage(lang, f'<ul class="steps">{steps}</ul>{extra}', head=head, side=f'<h1>{E(title)}</h1>')


def remote_page(lang: str, s: Settings) -> str:
    key = "remote_done_steps" if s.tailscale_key else "remote_desktop_steps"
    return subpage(lang, f'<div class="tip">{steps_html(lang, key)}</div>',
                   side=f'<h1>{E(t(lang, "remote_done_title"))}</h1><p class="lead">{E(t(lang, "nav_remote_desc"))}</p>')


def torrents_page(lang: str, found: list[dict] | None, removed: int | None = None) -> str:
    """Finished torrents already in the library. `found` None: an app did not answer."""
    head = f'<h1>{E(t(lang, "torrents_title"))}</h1>'
    side = head + f'<p class="lead">{E(t(lang, "torrents_help"))}</p><p class="hint">{E(t(lang, "torrents_kept"))}</p>'
    if removed is not None:
        return subpage(lang, f'<p class="lead">{E(t(lang, "torrents_done", n=removed))}</p>', side=head)
    if found is None:
        return subpage(lang, f'<p class="note warn">{E(t(lang, "torrents_error"))}</p>', side=side)
    if not found:
        return subpage(lang, f'<p class="lead">{E(t(lang, "torrents_none"))}</p>', side=side)
    rows = "".join(f'<li class="app-row"><span>{E(x["name"])}</span><b>{E(human_size(x["size"], lang))}</b></li>'
                   for x in found)
    return subpage(lang, f'<ul class="manual">{rows}</ul><form method="post" action="/torrents">'
                                f'<input type="hidden" name="hashes" value="{E("|".join(x["hash"] for x in found))}">'
                                f'<button class="btn danger" type="submit">'
                                f'{E(t(lang, "torrents_btn", n=len(found)))}</button></form>', side=side)


def trackers_page(lang: str) -> str:
    steps = steps_html(lang, "prowlarr_steps")
    side = f"""<h1>{E(t(lang, "trackers_title"))}</h1><p class="lead">{E(t(lang, "trackers_help"))}</p>
  <p class="hint">{E(t(lang, "trackers_rights"))}</p>"""
    return subpage(lang, f"""<input class="input" id="tracker-search" type="search" autocomplete="off" placeholder="{E(t(lang, "trackers_search"))}"
   aria-label="{E(t(lang, "trackers_search"))}">
  <ul class="trackers" id="tracker-list" aria-live="polite"><li class="hint">{E(t(lang, "trackers_loading"))}</li></ul>
  <p class="hint" id="tracker-more"></p>
  <div class="tip"><h2>{E(t(lang, "prowlarr_title"))}</h2><p>{E(t(lang, "prowlarr_default"))}</p>{steps}
  <p>{E(t(lang, "prowlarr_cloudflare"))} <a href="https://wiki.servarr.com/prowlarr/indexers" target="_blank" rel="noopener">{E(t(lang, "prowlarr_docs"))}</a></p></div>""", side=side)


def remove_page(lang: str, s: Settings, media_size: int) -> str:
    size = human_size(media_size, lang)
    return subpage(lang, f"""<form id="uninstall" class="field-group">
    <label class="check"><input type="checkbox" name="remove_media"><div><b>{E(t(lang, "uninstall_media", path=s.data_dir, size=size))}</b></div></label>
    <button class="btn danger" type="submit">{E(t(lang, "btn_uninstall"))}</button>
  </form>
  <p class="lead" id="uninstall-status" aria-live="polite" hidden></p>""",
                   side=f'<h1>{E(t(lang, "uninstall_title"))}</h1><p class="lead">{E(t(lang, "uninstall_text"))}</p>')


def login_page(lang: str, error: bool) -> str:
    err = f'<p class="ferr">{E(t(lang, "err_login"))}</p>' if error else ""
    body = f"""<form method="post" action="/login" class="field-group" style="display:flex;flex-direction:column;gap:16px">
<h1>{E(t(lang, "login_title"))}</h1><p class="lead">{E(t(lang, "login_help"))}</p>
<div class="field"><label class="l" for="u">{E(t(lang, "field_username"))}</label><input class="input" id="u" name="username" autocomplete="username"></div>
<div class="field"><label class="l" for="pw">{E(t(lang, "field_password"))}</label><input class="input" id="pw" name="password" type="password" autocomplete="current-password"></div>
{err}<button class="btn primary" type="submit">{E(t(lang, "btn_login"))}</button></form>"""
    return page(lang, body, script=False)


def svc_name(svc: str) -> str:
    from installer.i18n import APP_NAMES
    return APP_NAMES[svc]


def settings_from(data: dict, env: dict) -> Settings:
    provider = data.get("vpn_provider") or None
    return media.effective(Settings(  # a new install always has its choices made
        data_dir=str(data.get("data_dir", "")).strip(), username=str(data.get("username", "")).strip(),
        password=str(data.get("password", "")), vpn_provider=provider,
        vpn_fields={str(k): str(v) for k, v in (data.get("vpn_fields") or {}).items()} if provider else {},
        timezone=str(data.get("timezone") or "UTC"), lang=data.get("lang") if data.get("lang") in T else "en",
        puid=int(env.get("PUID", 1000)), pgid=int(env.get("PGID", 1000)),
        home=env.get("HOST_HOME", ""), platform=env.get("HOST_PLATFORM", "linux"),
        tailscale_key=str(data.get("tailscale_key") or "").strip()
        if env.get("HOST_PLATFORM", "linux") in REMOTE_PLATFORMS else "",
        **{k: data[k] for k in ("audio_lang", "default_profile", "metadata_lang") if data.get(k)},
        **{k: [str(x) for x in data[k]] for k in ("profiles", "subtitle_langs") if isinstance(data.get(k), list)}))


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # -- helpers
        def lang(self, s: Settings | None = None) -> str:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("lang", [""])[0] in T:
                return query["lang"][0]
            if s and s.lang in T:
                return s.lang
            accepted = self.headers.get("Accept-Language", "")[:2].lower()
            return accepted if accepted in T else "en"

        def authed(self) -> bool:
            cookie = self.headers.get("Cookie", "")
            token = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith(COOKIE + "=")), "")
            return token in state.sessions

        def locked(self) -> bool:
            return (state.running or state.flow.load_settings() is not None) and not self.authed()

        def send(self, status: int, body: str | bytes, ctype="text/html; charset=utf-8", headers=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def json(self, status: int, obj, headers=None):
            self.send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", headers)

        def body(self) -> dict:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            try:
                return json.loads(raw or "{}")
            except ValueError:
                return {}

        def session_cookie(self) -> dict:
            return {"Set-Cookie": f"{COOKIE}={state.new_session()}; HttpOnly; SameSite=Strict; Path=/"}

        # -- routes
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/static/"):
                return self.static(path[len("/static/"):])
            s = state.flow.load_settings()
            lang = self.lang(s)
            if path == "/login":
                return self.send(200, login_page(lang, False))
            if path == "/api/progress":
                if self.locked():
                    return self.json(401, {})
                return self.json(200, self.progress(lang))
            if path == "/api/ping":
                return self.json(200, {"ok": True})
            if path == "/api/trackers":
                if self.locked() or s is None:
                    return self.json(401, {})
                try:
                    return self.json(200, {"trackers": state.flow.tracker_catalog()})
                except Exception as e:  # Prowlarr still starting, or stopped
                    return self.json(503, {"error": t(lang, "trackers_error"), "detail": str(e)})
            if path in ("/api/disks", "/api/browse"):
                if self.locked():
                    return self.json(401, {})
                platform, home = state.env.get("HOST_PLATFORM", "linux"), state.env.get("HOST_HOME", "")
                if path == "/api/browse":
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    return self.json(200, state.flow.browse(query.get("path", [""])[0], platform))
                return self.json(200, {"disks": state.flow.disks(platform, home), "default": default_data_dir(state.env),
                                       "desktop": platform in ("mac", "windows")})
            if path == "/api/status":
                if self.locked() or s is None:
                    return self.json(401, {})
                return self.json(200, state.flow.guard_status())
            if path == "/api/uninstall-status":
                if self.locked():
                    return self.json(401, {})
                status, detail = state.flow.uninstall_status()
                return self.json(200, {"state": status, "detail": detail, "text": t(lang, "err_uninstall_failed")})
            if path not in ("/", "/remote", "/trackers", "/torrents", "/backups", "/backups/confirm", "/remove",
                            "/settings", "/settings/progress"):
                return self.send(404, "Not found", "text/plain")
            if self.locked():
                return self.send(303, "", headers={"Location": "/login"})
            if s is None or state.running:
                if path != "/":
                    return self.send(303, "", headers={"Location": "/"})
                return self.send(200, form_page(lang, state.env, state.running, bool(s and s.vpn_provider)))
            if path == "/remote":
                return self.send(200, remote_page(lang, s))
            if path == "/settings":
                return self.send(200, settings_page(lang, s))
            if path == "/settings/progress":
                host = (self.headers.get("Host") or "localhost").rsplit(":", 1)[0]
                return self.send(200, settings_progress(lang, state.flow.apply_status(), host))
            if path == "/trackers":
                return self.send(200, trackers_page(lang))
            if path == "/backups":
                return self.send(200, backups_page(lang, s, state.flow.list_backups(s)))
            if path == "/backups/confirm":
                name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("name", [""])[0]
                if name not in [b["name"] for b in state.flow.list_backups(s)]:
                    return self.send(303, "", headers={"Location": "/backups"})
                return self.send(200, restore_confirm_page(lang, name))
            if path == "/torrents":
                try:
                    found = state.flow.finished_torrents(s)
                except Exception:  # qBittorrent, Radarr or Sonarr down or starting
                    found = None
                return self.send(200, torrents_page(lang, found))
            if path == "/remove":
                return self.send(200, remove_page(lang, s, state.flow.media_size(s)))
            host = (self.headers.get("Host") or "localhost").rsplit(":", 1)[0]
            if host in ("localhost", "127.0.0.1", "[::1]") and state.env.get("HOST_ADDR", "localhost") != "localhost":
                # Opened on the machine itself: the LAN address works here too, and from the TV or the phone.
                host = state.env["HOST_ADDR"]
            return self.send(200, panel_page(lang, s, host, state.flow.is_complete(), state.flow.guard_status(),
                                             state.flow.last_update(), state.flow.backup_status()))

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            # SameSite treats the seven apps (other ports, same host) as same-site: refuse their pages' POSTs.
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
                return self.json(403, {})
            data = self.body()
            s = state.flow.load_settings()
            lang = data.get("lang") if data.get("lang") in T else self.lang(s)
            if path == "/login":
                ok = s is not None and hmac.compare_digest(str(data.get("username", "")).encode(), s.username.encode()) \
                    and hmac.compare_digest(str(data.get("password", "")).encode(), s.password.encode())
                if not ok:
                    return self.send(401, login_page(lang, True))
                return self.send(303, "", headers={"Location": "/", **self.session_cookie()})
            if path == "/api/preflight":
                if self.locked():
                    return self.json(401, {})
                settings = settings_from(data, state.env)
                found = validate(settings) or state.flow.preflight(settings)
                item = lambda k: {"key": k, "text": t(lang, k, path=settings.data_dir)}
                return self.json(200, {"errors": [item(k) for k in found if k.startswith("err_")],
                                       "warnings": [item(k) for k in found if k.startswith("warn_")]})
            if path == "/api/install":
                if self.locked() or state.running or s is not None:
                    return self.json(409, {})
                settings = settings_from(data, state.env)
                if validate(settings):
                    return self.json(400, {"errors": validate(settings)})
                blocking = [k for k in state.flow.preflight(settings) if k.startswith("err_")]
                if blocking:  # the page checks first, but never install on top of anything
                    return self.json(409, {"errors": blocking})
                state.start_install(settings)
                return self.json(202, {}, self.session_cookie())
            if path == "/api/trackers":
                if self.locked() or s is None:
                    return self.json(401, {})
                try:
                    state.flow.add_tracker(str(data.get("id", "")))
                except KeyError:
                    return self.json(404, {})
                except Exception as e:
                    return self.json(503, {"error": t(lang, "trackers_error"), "detail": str(e)})
                return self.json(200, {"text": t(lang, "tracker_added")})
            if path == "/torrents":
                if self.locked() or s is None:
                    return self.json(401, {})
                try:  # the list is made again here: only what is imported now goes
                    hashes = [h for h in str(data.get("hashes", "")).split("|") if h]
                    return self.send(200, torrents_page(lang, [], state.flow.clean_torrents(s, hashes)))
                except Exception:
                    return self.send(200, torrents_page(lang, None))
            if path in ("/api/backup", "/api/restore"):
                if self.locked() or s is None:
                    return self.json(401, {})
                name = str(data.get("name", ""))
                if path == "/api/restore" and name not in [b["name"] for b in state.flow.list_backups(s)]:
                    return self.send(303, "", headers={"Location": "/backups"})  # gone meanwhile: the list again
                try:
                    state.flow.start_backup() if path == "/api/backup" else state.flow.start_restore(name)
                except WorkerBusy:  # one operation at a time: show the running one
                    pass
                return self.send(303, "", headers={"Location": "/settings/progress"})
            if path == "/api/update":
                if self.locked() or s is None:
                    return self.json(401, {})
                try:
                    state.flow.start_update()
                except WorkerBusy:  # one operation at a time: show the running one
                    pass
                return self.send(303, "", headers={"Location": "/settings/progress"})
            if path == "/api/confirm-disk":
                if self.locked() or s is None:
                    return self.json(401, {})
                state.flow.confirm_disk(s)
                return self.send(303, "", headers={"Location": "/"})
            if path in ("/settings", "/settings/apply", "/settings/edit"):
                if self.locked() or s is None:
                    return self.json(401, {})
                from installer import reconfigure
                new = reconfigure.merge(s, settings_data(data))
                if path == "/settings/edit":  # back from the summary, with what was typed
                    return self.send(200, settings_page(lang, s, shown=new))
                found = (["err_passwords_differ"] if data.get("password") and
                         data.get("password") != data.get("password2") else [])
                chg = reconfigure.changes(s, new)
                if not chg:
                    return self.send(200, settings_page(lang, s, t(lang, "settings_nothing")))
                found += state.flow.settings_check(s, new)
                if path == "/settings":
                    ask = [a for a in state.flow.check_logins(s) if a in ACCOUNT_PROMPTS] if "account" in chg else []
                    return self.send(200, settings_summary(lang, data, chg, s, new, found, ask))
                if any(k.startswith("err_") for k in found) or state.flow.apply_status()["state"] == "running":
                    return self.json(409, {})
                current = {a: data[f"current_{a}"] for a in ACCOUNT_PROMPTS if data.get(f"current_{a}")}
                try:
                    state.flow.start_apply(new, current)
                except WorkerBusy:  # an update started meanwhile, from the terminal: its progress
                    pass
                return self.send(303, "", headers={"Location": "/settings/progress"})
            if path == "/api/uninstall":
                if self.locked() or s is None:
                    return self.json(401, {})
                if state.running:
                    return self.json(409, {})
                state.flow.uninstall(bool(data.get("remove_media")), state.report)
                return self.json(202, {"text": t(lang, "uninstall_started")})
            return self.send(404, "Not found", "text/plain")

        def progress(self, lang: str) -> dict:
            with state.lock:
                events = list(state.events)
            last = events[-1] if events else Progress("step_prepare")
            return {**asdict(last), "text": t(lang, last.step),
                    "errorText": t(lang, last.error) if last.error else "",
                    "history": [{"step": e.step, "text": t(lang, e.step)} for e in events]}

        def static(self, name: str):
            target = (STATIC / name).resolve()
            if STATIC not in target.parents or not target.is_file():
                return self.send(404, "Not found", "text/plain")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self.send(200, target.read_bytes(), ctype)

    return Handler


def make_server(port: int, flow=default_flow, env=None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("", port), make_handler(State(flow, dict(env if env is not None else os.environ))))


def serve(port: int = 7575, flow=default_flow) -> None:
    make_server(port, flow).serve_forever()
