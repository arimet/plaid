"""The assistant: a form until the stack exists, then a password-protected panel with the links and Remove."""
from __future__ import annotations

import hmac
import html
import json
import mimetypes
import os
import secrets
import threading
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from installer import flow as default_flow
from installer.compose import PORTS, SERVICES
from installer.flow import Progress
from installer.i18n import T, t
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
                                  ("/remove", "uninstall_title", "nav_remove_desc")))
    dots = ('<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="4" cy="10" r="2"/>'
            '<circle cx="10" cy="10" r="2"/><circle cx="16" cy="10" r="2"/></svg>')
    return (f'<details class="menu"><summary aria-label="{E(t(lang, "nav_more"))}">{dots}</summary>'
            f'<nav class="mores">{items}</nav></details>')


def page(lang: str, body: str, *, cloth: str = "", p: int = 100, foot: str = "", script: bool = True,
         menu: str = "") -> str:
    current = ' aria-current="true"'
    links = "".join(f'<a href="?lang={code}"{current if code == lang else ""}>{code.upper()}</a>' for code in T)
    texts = json.dumps({k: v for k, v in T[lang].items()}, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plaid</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/static/fonts.css">
<link rel="stylesheet" href="/static/style.css"></head><body>
<div class="cloth {cloth}" id="cloth" style="--p:{p}%"><div class="woven"></div>
<a class="brand" href="/"><b>Plaid</b></a><div class="top">{menu}<nav class="lang" aria-label="Language">{links}</nav></div></div>
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


def form_page(lang: str, env: dict, running: bool = False, with_vpn: bool = True) -> str:
    default_dir = default_data_dir(env)
    remote = env.get("HOST_PLATFORM", "linux") in REMOTE_PLATFORMS
    last = 4 if remote else 3
    remote_section = f"""
<section data-step="4" hidden>
  <h1>{E(t(lang, "field_remote"))}</h1>
  <p class="lead">{E(t(lang, "remote_help"))}</p>
  <div class="tip">{steps_html(lang, "remote_steps")}
  <p><a href="https://login.tailscale.com/admin/settings/keys" target="_blank" rel="noopener">login.tailscale.com</a></p></div>
  <div class="field"><label class="l" for="tailscale_key">{E(t(lang, "field_tailscale_key"))}</label>
  <input class="input" id="tailscale_key" type="password" autocomplete="off" spellcheck="false" placeholder="tskey-auth-…">
  <p class="ferr" data-err="tailscale"></p></div>
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
  <h1>{E(t(lang, "field_folder"))}</h1>
  <p class="lead">{E(t(lang, "subtitle"))}</p>
  <div class="field"><label class="l" for="data_dir">{E(t(lang, "field_folder"))}</label>
  <input class="input" id="data_dir" name="data_dir" value="{E(default_dir)}" autocomplete="off" spellcheck="false">
  <p class="hint">{E(t(lang, "field_folder_help"))}</p><p class="ferr" data-err="data_dir"></p></div>
</section>

<section data-step="2" hidden>
  <h1>{E(t(lang, "field_username"))} · {E(t(lang, "field_password"))}</h1>
  <p class="lead">{E(t(lang, "field_account_help"))}</p>
  <div class="field"><label class="l" for="username">{E(t(lang, "field_username"))}</label>
  <input class="input" id="username" name="username" autocomplete="username" value="admin"><p class="ferr" data-err="username"></p></div>
  <div class="field"><label class="l" for="password">{E(t(lang, "field_password"))}</label>
  <input class="input" id="password" name="password" type="password" autocomplete="new-password"></div>
  <div class="field"><label class="l" for="password2">{E(t(lang, "field_password2"))}</label>
  <input class="input" id="password2" type="password" autocomplete="new-password"><p class="ferr" data-err="password"></p></div>
</section>

<section data-step="3" hidden>
  <h1>{E(t(lang, "field_vpn"))}</h1>
  <p class="lead">{E(t(lang, "field_vpn_help"))}</p>
  <div class="prov" role="radiogroup" aria-label="{E(t(lang, "field_vpn"))}">{chips}</div>
  {fields}
  <p class="hint" id="vpn-where">{E(t(lang, "vpn_where"))}</p>
  <div class="note" id="pf-yes">{pf_icon}<span>{E(t(lang, "vpn_pf_yes"))}</span></div>
  <div class="note warn" id="pf-no" hidden>{warn_icon}<span>{E(t(lang, "vpn_pf_no"))}</span></div>
  <p class="ferr" data-err="vpn"></p>
</section>
{remote_section}
<section data-step="{last + 1}" hidden>
  <h1>{E(t(lang, "confirm_title"))}</h1>
  <div id="warnings" class="field-group"></div>
</section>
<input type="hidden" name="timezone" id="timezone" value="UTC"><input type="hidden" name="lang" value="{lang}">
</form>

<section id="progress" hidden data-running="{int(running)}" data-vpn="{int(with_vpn)}">
  <h1 id="progress-title">{E(t(lang, "progress_title"))}</h1>
  <p class="lead" id="progress-lead">{E(t(lang, "progress_help"))}</p>
  <ul class="steps" id="steps"></ul>
  <pre class="detail" id="detail" hidden></pre>
  <a class="btn" href="/" id="to-panel" hidden>{E(t(lang, "uninstall_title"))}</a>
</section>"""
    foot = f"""<div class="foot" id="foot">
<button class="btn" type="button" id="back" hidden>{E(t(lang, "btn_back"))}</button>
<button class="btn primary" type="button" id="next">{E(t(lang, "btn_next"))}</button></div>"""
    return page(lang, body, cloth="part", p=40 if running else 8, foot=foot)


def panel_page(lang: str, s: Settings, host: str, complete: bool = True) -> str:
    def app_link(svc: str, daily: bool) -> str:
        return (f'<a class="app{" daily" if daily else ""}" href="http://{E(host)}:{PORTS[svc]}" target="_blank" '
                f'rel="noopener"><span class="ic" style="--c:{APP_COLORS[svc]}"><img src="/static/icons/{svc}.svg" '
                f'alt="" width="28" height="28"></span><span><b>{E(svc_name(svc))}</b> '
                f'<span class="role">{E(t(lang, "app_" + svc))}</span><small>{E(t(lang, "appd_" + svc))}</small></span>'
                f'<span class="open">{E(t(lang, "done_open"))}</span></a>')

    # Beginners open two apps; the other five work on their own and are listed for when something needs a look.
    daily = [svc for svc in SERVICES if svc in DAILY_APPS]
    behind = [svc for svc in SERVICES if svc not in DAILY_APPS]
    apps = (f'<h2>{E(t(lang, "apps_daily"))}</h2><div class="apps">{"".join(app_link(x, True) for x in daily)}</div>'
            f'<p class="lead">{E(t(lang, "apps_flow"))}</p>'
            f'<h2>{E(t(lang, "apps_behind"))}</h2><p class="hint">{E(t(lang, "apps_behind_help"))}</p>'
            f'<div class="apps">{"".join(app_link(x, False) for x in behind)}</div>')
    if not complete:  # a failed install: removal is the only thing to do
        body = f"""<h1>{E(t(lang, "progress_failed"))}</h1><p class="lead">{E(t(lang, "err_unexpected"))}</p>
  <a class="btn danger" href="/remove">{E(t(lang, "uninstall_title"))}</a>"""
        return subpage(lang, body, back=False, menu=False)
    autostart = (f'<p class="note warn">{E(t(lang, "autostart_hint"))}</p>'
                 if s.platform in ("mac", "windows") else "")
    body = f"""<h1>{E(t(lang, "done_title"))}</h1>
  <p class="lead">{E(t(lang, "done_lead"))} {t(lang, "done_login", username="<b>" + E(s.username) + "</b>")}</p>
  {apps}
  {autostart}"""
    return subpage(lang, body, back=False)


def subpage(lang: str, body: str, back: bool = True, menu: bool = True) -> str:
    link = f'<a class="back" href="/">‹ {E(t(lang, "nav_back"))}</a>' if back else ""
    return page(lang, f'<section id="done">{link}{body}</section>', foot='<div style="height:32px"></div>',
                menu=header_menu(lang) if menu else "")


def remote_page(lang: str, s: Settings) -> str:
    key = "remote_done_steps" if s.tailscale_key else "remote_desktop_steps"
    return subpage(lang, f'<h1>{E(t(lang, "remote_done_title"))}</h1><p class="lead">{E(t(lang, "nav_remote_desc"))}</p>'
                         f'<div class="tip">{steps_html(lang, key)}</div>')


def trackers_page(lang: str) -> str:
    steps = steps_html(lang, "prowlarr_steps")
    return subpage(lang, f"""<h1>{E(t(lang, "trackers_title"))}</h1><p class="lead">{E(t(lang, "trackers_help"))}</p>
  <input class="input" id="tracker-search" type="search" autocomplete="off" placeholder="{E(t(lang, "trackers_search"))}"
   aria-label="{E(t(lang, "trackers_search"))}">
  <ul class="trackers" id="tracker-list" aria-live="polite"><li class="hint">{E(t(lang, "trackers_loading"))}</li></ul>
  <p class="hint" id="tracker-more"></p><p class="hint">{E(t(lang, "trackers_rights"))}</p>
  <div class="tip"><h2>{E(t(lang, "prowlarr_title"))}</h2><p>{E(t(lang, "prowlarr_default"))}</p>{steps}
  <p>{E(t(lang, "prowlarr_cloudflare"))} <a href="https://wiki.servarr.com/prowlarr/indexers" target="_blank" rel="noopener">{E(t(lang, "prowlarr_docs"))}</a></p></div>""")


def remove_page(lang: str, s: Settings, media_size: int) -> str:
    size = human_size(media_size, lang)
    return subpage(lang, f"""<form id="uninstall" class="field-group">
    <h1>{E(t(lang, "uninstall_title"))}</h1>
    <p class="lead">{E(t(lang, "uninstall_text"))}</p>
    <label class="check"><input type="checkbox" name="remove_media"><div><b>{E(t(lang, "uninstall_media", path=s.data_dir, size=size))}</b></div></label>
    <button class="btn danger" type="submit">{E(t(lang, "btn_uninstall"))}</button>
  </form>
  <p class="lead" id="uninstall-status" aria-live="polite" hidden></p>""")


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
    return Settings(
        data_dir=str(data.get("data_dir", "")).strip(), username=str(data.get("username", "")).strip(),
        password=str(data.get("password", "")), vpn_provider=provider,
        vpn_fields={str(k): str(v) for k, v in (data.get("vpn_fields") or {}).items()} if provider else {},
        timezone=str(data.get("timezone") or "UTC"), lang=data.get("lang") if data.get("lang") in T else "en",
        puid=int(env.get("PUID", 1000)), pgid=int(env.get("PGID", 1000)),
        home=env.get("HOST_HOME", ""), platform=env.get("HOST_PLATFORM", "linux"),
        tailscale_key=str(data.get("tailscale_key") or "").strip()
        if env.get("HOST_PLATFORM", "linux") in REMOTE_PLATFORMS else "")


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
            if path == "/api/uninstall-status":
                if self.locked():
                    return self.json(401, {})
                status, detail = state.flow.uninstall_status()
                return self.json(200, {"state": status, "detail": detail, "text": t(lang, "err_uninstall_failed")})
            if path not in ("/", "/remote", "/trackers", "/remove"):
                return self.send(404, "Not found", "text/plain")
            if self.locked():
                return self.send(303, "", headers={"Location": "/login"})
            if s is None or state.running:
                if path != "/":
                    return self.send(303, "", headers={"Location": "/"})
                return self.send(200, form_page(lang, state.env, state.running, bool(s and s.vpn_provider)))
            if path == "/remote":
                return self.send(200, remote_page(lang, s))
            if path == "/trackers":
                return self.send(200, trackers_page(lang))
            if path == "/remove":
                return self.send(200, remove_page(lang, s, state.flow.media_size(s)))
            host = (self.headers.get("Host") or "localhost").rsplit(":", 1)[0]
            return self.send(200, panel_page(lang, s, host, state.flow.is_complete()))

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
                item = lambda k: {"key": k, "text": t(lang, k)}
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
