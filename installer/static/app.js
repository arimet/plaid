// Plaid assistant: wizard steps, install progress, removal. No framework, no network beyond this server.
(() => {
  const T = JSON.parse(document.getElementById("texts").textContent);
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const bar = $("#weave");  // the inset progress track under the header
  const weave = (pct) => bar && bar.style.setProperty("--w", pct + "%");
  const post = (url, body) => fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify(body), credentials: "same-origin"});
  const fmt = (s, params) => s.replace(/\{(\w+)\}/g, (_, k) => params[k] ?? "");

  // ---------------------------------------------------------------- the look, kept by this browser (settings)
  const style = $("#style");
  if (style) {
    style.value = document.documentElement.dataset.style;
    style.addEventListener("change", () => {
      document.documentElement.dataset.style = style.value;
      try { localStorage.setItem("plaid-style", style.value); } catch (e) { /* private window: this page only */ }
    });
  }

  // ---------------------------------------------------------------- folder picker (wizard and settings)
  const bytes = (n) => {
    const nf = new Intl.NumberFormat(document.documentElement.lang, {maximumFractionDigits: 1});
    const tb = 1024 ** 4, gb = 1024 ** 3;  // as the removal page counts (web.human_size)
    const [value, unit] = n >= tb ? [n / tb, T.unit_tb] : [n / gb, T.unit_gb];
    return `${nf.format(value < 10 ? value : Math.round(value))}\u00a0${unit}`;
  };
  $$("[data-picker]").forEach((picker) => {
    const field = $("#" + (picker.dataset.for || "data_dir")), panel = $(".picker-panel", picker), list = $(".picker-list", picker);
    const where = $(".picker-where", picker), actions = $(".picker-actions", picker);
    const toggle = $("[data-picker-open]", picker), here = $("[data-here]", picker);
    let current = null, ticket = 0;
    const never = new Promise(() => {});
    // Only the last click's answer is drawn; a session that expired sends the page back to sign-in.
    const get = (url) => {
      const mine = ++ticket;
      return fetch(url, {credentials: "same-origin"}).then((r) => {
        if (r.status === 401) {
          location.reload();
          return never;
        }
        return r.ok ? r.json() : Promise.reject();
      }).then((d) => (mine === ticket ? d : never), (e) => (mine === ticket ? Promise.reject(e) : never));
    };
    const space = (d) => (d.total ? fmt(T.picker_free, {free: bytes(d.free), total: bytes(d.total)}) : "");
    const join = (dir, name) => {
      const sep = /^[A-Za-z]:\\/.test(dir) ? "\\" : "/";  // a Linux folder name may hold a backslash
      return dir.endsWith(sep) ? dir + name : dir + sep + name;
    };
    const item = (title, sub, onClick, used) => {
      const li = document.createElement("li"), b = document.createElement("button"), strong = document.createElement("b");
      b.type = "button";
      strong.textContent = title;
      b.append(strong);
      if (used != null) {
        const meter = document.createElement("span"), fill = document.createElement("i");
        meter.className = "meter";
        fill.style.width = used + "%";
        meter.append(fill);
        b.append(meter);
      }
      if (sub) {
        const small = document.createElement("small");
        small.textContent = sub;
        b.append(small);
      }
      b.addEventListener("click", onClick);
      li.append(b);
      list.append(li);
    };
    const close = () => {
      panel.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
    };
    const choose = (path) => {
      field.value = path;
      field.dispatchEvent(new Event("input", {bubbles: true}));
      close();
      field.focus();
    };
    const loading = () => {
      list.textContent = "";
      actions.hidden = true;
      where.textContent = T.picker_loading;
    };
    const showDisks = async () => {
      current = null;
      loading();
      const data = await get("/api/disks").catch(() => ({disks: []}));
      where.textContent = data.disks.length ? T.picker_disks : T.picker_no_disks;
      data.disks.forEach((d) => {
        const used = d.total ? Math.round((100 * (d.total - d.free)) / d.total) : null;
        if (d.system && data.desktop) {  // Docker Desktop cannot list this disk: only the suggested folder
          item(T.picker_system, [T.picker_system_hint, space(d)].filter(Boolean).join(" · "), () => choose(data.default), used);
        } else {
          item(d.system ? T.picker_system : d.name, [d.path, space(d)].filter(Boolean).join(" · "), () => open(d.path), used);
        }
      });
    };
    const open = async (path) => {
      loading();
      const d = await get("/api/browse?path=" + encodeURIComponent(path)).catch(() => ({error: "err_folder_unreadable"}));
      if (d.error) {
        where.textContent = T[d.error];
        item("↑ " + T.picker_disks, "", showDisks);
        return;
      }
      current = d.path;
      where.textContent = [d.path, space(d)].filter(Boolean).join(" · ");
      item("↑ " + (d.parent ? T.picker_up : T.picker_disks), d.parent || "", () => (d.parent ? open(d.parent) : showDisks()));
      d.dirs.forEach((name) => item(name, "", () => open(join(d.path, name))));
      if (!d.dirs.length) {
        const li = document.createElement("li");
        li.className = "hint";
        li.textContent = T.picker_none;
        list.append(li);
      }
      actions.hidden = false;
    };
    toggle.addEventListener("click", () => {
      const opening = panel.hidden;
      panel.hidden = !opening;
      toggle.setAttribute("aria-expanded", String(opening));
      if (opening) showDisks();
    });
    $("[data-use]", picker).addEventListener("click", () => current && choose(current));
    if (here) here.addEventListener("click", () => current && choose(join(current, "Plaid")));
  });

  // ---------------------------------------------------------------- panel: app states and disk room, every 30 s
  if ($("[data-disk]")) {
    setInterval(() => {
      fetch("/api/status", {credentials: "same-origin"}).then((r) => (r.ok ? r.json() : Promise.reject())).then((st) => {
        $$(".state[data-app]").forEach((el) => {
          const state = (st.apps || {})[el.dataset.app] || "unknown";
          el.dataset.state = state;
          el.lastChild.textContent = T["state_" + state];
        });
        const vpn = $("[data-vpn]");
        if (vpn && vpn.dataset.provider) {  // with a VPN: on, down, or not looked at yet
          const state = st.vpn === "ok" || st.vpn === "down" ? st.vpn : "checking";
          const title = {ok: T.vpn_on, down: T.guard_vpn_down, checking: T.vpn_checking}[state];
          const help = {ok: fmt(T.vpn_on_help, {provider: vpn.dataset.provider}), down: T.guard_vpn_down_help,
                        checking: T.vpn_checking_help}[state];
          vpn.dataset.vpn = state;
          $("b", vpn).textContent = title;
          $(".vpn-help", vpn).textContent = help;
        }
        const disk = $("[data-disk]"), known = st.free != null && st.total > 0;
        $(".meter", disk).hidden = !known;
        if (known) $(".meter i", disk).style.width = Math.round((100 * (st.total - st.free)) / st.total) + "%";
        $("[data-room]", disk).textContent = known ? fmt(T.picker_free, {free: bytes(st.free), total: bytes(st.total)}) + " · " : "";
        $("[data-full-note]", disk).hidden = !(known && st.free < Number(disk.dataset.full));
      }).catch(() => {});  // the next round tries again
    }, 30000);
  }

  // ---------------------------------------------------------------- wizard
  const form = $("#wizard");
  if (form) {
    const sections = $$("section[data-step]", form);
    const next = $("#next"), back = $("#back");
    const LAST = Number(form.dataset.last);  // question steps; the confirmation comes after
    let step = 1, provider = "protonvpn", warningsShown = false;
    $("#timezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

    const show = (n) => {
      step = n;
      sections.forEach((s) => (s.hidden = Number(s.dataset.step) !== n));
      $$(".st", $("#stitches")).forEach((st, i) => (st.className = "st" + (i + 1 < n ? " on" : i + 1 === n ? " now" : "")));
      $("#stepof").textContent = fmt(T.step_of, {n: Math.min(n, LAST), total: LAST});
      back.hidden = n === 1;
      next.textContent = n > LAST ? T.btn_continue_anyway : n === LAST ? T.btn_install : T.btn_next;
      const first = $("input:not([type=hidden]), button.chip[aria-checked=true]", sections[n - 1]);
      if (first) first.focus({preventScroll: true});
    };

    const clearErrors = () => $$("[data-err]").forEach((p) => (p.textContent = ""));
    const fail = (where, text) => {
      const slot = $(`[data-err="${where}"]`);
      if (slot) slot.textContent = text;
    };
    const media = () => ({
      audio_lang: $("#audio_lang").value, metadata_lang: $("#metadata_lang").value,
      profiles: $$('input[name^="profile_"]:checked', form).map((i) => i.name.slice("profile_".length)),
      default_profile: ($('input[name="default_profile"]:checked', form) || {}).value || null,
      subtitle_langs: $$('input[name^="sub_"]:checked', form).map((i) => i.name.slice("sub_".length)),
    });
    const values = () => {
      const fields = {};
      $$(`.vpn-fields[data-for="${CSS.escape(provider)}"] [data-vpn-field]`).forEach((i) => (fields[i.dataset.vpnField] = i.value));
      return {
        data_dir: $("#data_dir").value, username: $("#username").value, password: $("#password").value,
        vpn_provider: provider || null, vpn_fields: provider ? fields : {}, timezone: $("#timezone").value,
        lang: document.documentElement.lang, tailscale_key: $("#tailscale_key") ? $("#tailscale_key").value : "",
        ...media(),
      };
    };

    // VPN provider radio group (click and arrow keys)
    const chips = $$(".chip");
    const pick = (chip) => {
      chips.forEach((c) => { c.setAttribute("aria-checked", String(c === chip)); c.tabIndex = c === chip ? 0 : -1; });
      provider = chip.dataset.provider;
      $$(".vpn-fields").forEach((f) => (f.hidden = f.dataset.for !== provider));
      $("#pf-yes").hidden = chip.dataset.pf !== "1";
      $("#pf-no").hidden = !provider || chip.dataset.pf === "1";
      $("#vpn-where").hidden = !provider;
    };
    chips.forEach((chip, i) => {
      chip.addEventListener("click", () => pick(chip));
      chip.addEventListener("keydown", (e) => {
        const d = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[e.key];
        if (!d) return;
        e.preventDefault();
        const target = chips[(i + d + chips.length) % chips.length];
        pick(target);
        target.focus();
      });
    });
    pick(chips.find((c) => c.getAttribute("aria-checked") === "true"));

    const where = {err_dangerous_path: "data_dir", err_path_absolute: "data_dir", err_username: "username",
                   err_password_short: "password", err_vpn_fields: "vpn", err_vpn_provider: "vpn",
                   err_tailscale_key: "tailscale", err_tailscale_exists: "tailscale",
                   err_profiles_none: "profiles", err_default_profile: "profiles", err_media_lang: "profiles",
                   err_subtitles_none: "subtitles"};
    const stepOf = {data_dir: 1, username: 2, password: 2, vpn: 3, tailscale: 4, profiles: LAST, subtitles: LAST};

    const check = async () => {
      const res = await post("/api/preflight", values());
      const data = await res.json();
      if (data.errors.length) {
        const first = data.errors[0];
        const slot = where[first.key];
        if (slot) { show(stepOf[slot]); data.errors.forEach((e) => fail(where[e.key] || "vpn", e.text)); }
        else { renderNotes(data.errors, true); show(LAST + 1); next.hidden = true; }
        return false;
      }
      if (data.warnings.length && !warningsShown) {
        renderNotes(data.warnings, false);
        warningsShown = true;
        show(LAST + 1);
        return false;
      }
      return true;
    };

    const renderNotes = (items, blocking) => {
      $("#warnings").innerHTML = "";
      items.forEach((w) => {
        const p = document.createElement("p");
        p.className = "note" + (blocking ? " warn" : " warn");
        p.textContent = w.text;
        $("#warnings").append(p);
      });
    };

    next.addEventListener("click", async () => {
      clearErrors();
      if (step === 1 && !$("#data_dir").value.trim()) return fail("data_dir", T.err_path_absolute);
      if (step === 2) {
        if (!$("#username").value.trim()) return fail("username", T.err_username);
        if ($("#password").value.length < 8) return fail("password", T.err_password_short);
        if ($("#password").value !== $("#password2").value) return fail("password", T.err_passwords_differ);
      }
      if (step === 3 && provider && $$(`.vpn-fields[data-for="${CSS.escape(provider)}"] input`).some((i) => !i.value.trim()))
        return fail("vpn", T.err_vpn_fields);
      if (step === LAST) {
        const m = media();
        if (!m.profiles.length) return fail("profiles", T.err_profiles_none);
        if (!m.profiles.includes(m.default_profile)) return fail("profiles", T.err_default_profile);
        if (!m.subtitle_langs.length) return fail("subtitles", T.err_subtitles_none);
      }
      if (step < LAST) return show(step + 1);
      next.disabled = true;
      try {
        if (step === LAST && !(await check())) return;
        await install();
      } finally {
        next.disabled = false;
      }
    });
    back.addEventListener("click", () => { clearErrors(); next.hidden = false; show(step - 1); });
    form.addEventListener("submit", (e) => { e.preventDefault(); next.click(); });
    form.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); next.click(); } });

    const install = async () => {
      const res = await post("/api/install", values());
      if (res.status !== 202) { fail("vpn", T.err_unexpected); return; }
      form.hidden = true;
      $("#foot").hidden = true;
      $("#progress").hidden = false;
      if (bar) bar.parentElement.hidden = false;  // the progress line only shows while installing
      track(Boolean(provider));
    };
    if ($("#progress").dataset.running !== "1") show(1);
  }

  // ---------------------------------------------------------------- progress
  const ORDER = ["step_prepare", "step_folders", "step_download", "step_start", "step_vpn", "step_configure"];
  const OK = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 6.3l2.5 2.5L10 3.4" stroke-width="2" fill="none"/></svg>';
  const track = (withVpn) => {
    const order = ORDER.filter((s) => withVpn || s !== "step_vpn");
    const tick = async () => {
      let data;
      try { data = await (await fetch("/api/progress", {credentials: "same-origin"})).json(); }
      catch { return setTimeout(tick, 2000); }
      const seen = data.history.map((h) => h.step);
      const apps = data.history.filter((h) => h.step.startsWith("step_app:")).map((h) => h.text);
      const current = [...seen].reverse().find((s) => order.includes(s)) || "step_prepare";
      const at = order.indexOf(current);
      $("#steps").innerHTML = "";
      order.forEach((s, i) => {
        const li = document.createElement("li");
        const failed = data.error && i === at;
        const state = data.done || i < at ? "ok" : failed ? "bad" : i === at ? "now" : "wait";
        li.innerHTML = `<span class="knot ${state}">${state === "ok" ? OK : ""}</span><span></span>`;
        li.lastChild.textContent = T[s];
        if (s === "step_configure" && apps.length) {
          const sub = document.createElement("span");
          sub.className = "sub";
          sub.textContent = apps.join(" · ");
          li.lastChild.append(sub);
        }
        $("#steps").append(li);
      });
      weave(data.done ? 100 : 40 + Math.round((60 * Math.max(at, 0)) / order.length));
      if (data.error) {
        $("#progress-title").textContent = T.progress_failed;
        $("#progress-lead").textContent = data.errorText;
        if (data.detail) { $("#detail").hidden = false; $("#detail").textContent = data.detail; }
        $("#failed-actions").hidden = false;
        return;
      }
      if (data.done) return setTimeout(() => location.reload(), 900);
      setTimeout(tick, 1500);
    };
    tick();
  };
  const prog = $("#progress");
  if (prog && prog.dataset.running === "1") {
    form.hidden = true;
    $("#foot").hidden = true;
    prog.hidden = false;
    if (bar) bar.parentElement.hidden = false;
    track(prog.dataset.vpn === "1");
  }

  // ---------------------------------------------------------------- removal
  const removal = $("#uninstall");
  if (removal) {
    removal.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!confirm(T.uninstall_confirm)) return;
      const res = await post("/api/uninstall", {remove_media: removal.remove_media.checked});
      if (res.status !== 202) return;
      removal.hidden = true;
      const status = $("#uninstall-status");
      status.hidden = false;
      status.textContent = T.uninstall_started;
      const ping = async () => {
        let data;
        try { data = await (await fetch("/api/uninstall-status", {cache: "no-store", credentials: "same-origin"})).json(); }
        catch {  // this server is gone: the removal deleted it, so it went through
          const done = document.createElement("h1");
          done.textContent = T.uninstall_done;
          $("#done").replaceChildren(done);
          return;
        }
        if (data.state === "failed") {
          status.textContent = data.text;
          const pre = document.createElement("pre");
          pre.className = "detail";
          pre.textContent = data.detail;
          status.after(pre);
          removal.hidden = false;
          return;
        }
        setTimeout(ping, 2000);
      };
      setTimeout(ping, 3000);
    });
  }
})();

// ---------------------------------------------------------------- tracker catalogue (panel)
(() => {
  const list = document.getElementById("tracker-list");
  if (!list) return;
  const T = JSON.parse(document.getElementById("texts").textContent);
  const search = document.getElementById("tracker-search");
  const more = document.getElementById("tracker-more");
  const SHOWN = 30;
  let all = [];

  const add = async (tracker, button) => {
    button.disabled = true;
    const res = await fetch("/api/trackers", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: tracker.id})});
    if (res.ok) { tracker.added = true; button.textContent = T.tracker_added; }
    else { button.disabled = false; more.textContent = T.trackers_error; }
  };

  const render = () => {
    const q = search.value.trim().toLowerCase();
    const found = all.filter((t) => !q || t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q));
    list.innerHTML = "";
    found.slice(0, SHOWN).forEach((t) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      const b = document.createElement("b");
      b.textContent = t.name;
      const lang = document.createElement("span");
      lang.className = "lang-tag";
      lang.textContent = t.language;
      name.append(b, lang);
      const desc = document.createElement("small");
      desc.textContent = t.description;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = t.added ? T.tracker_added : T.tracker_add;
      button.disabled = t.added;
      button.addEventListener("click", () => add(t, button));
      li.append(name, button, desc);
      list.append(li);
    });
    if (!found.length) list.innerHTML = `<li class="hint">${T.trackers_none}</li>`;
    more.textContent = found.length > SHOWN ? T.trackers_more.replace("{n}", found.length - SHOWN) : "";
  };

  fetch("/api/trackers", {credentials: "same-origin"})
    .then((r) => (r.ok ? r.json() : Promise.reject()))
    .then((data) => { all = data.trackers; render(); })
    .catch(() => { list.innerHTML = `<li class="hint">${T.trackers_error}</li>`; });
  search.addEventListener("input", render);
})();
