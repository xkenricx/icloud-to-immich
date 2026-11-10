(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // ---------- small storage helpers (never let storage errors break the page) ----------
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode etc. */ } },
    del(k) { try { localStorage.removeItem(k); } catch (_) { /* ignore */ } },
  };

  // ---------- access key: from the link, else remembered from last time ----------
  let TOKEN = new URLSearchParams(location.hash.slice(1)).get("t") || store.get("i2i-key") || "";

  let S = null;              // latest server snapshot
  let editingImmich = false;
  let lastActive = null;
  let deletePreview = null;  // {count, bytes}
  let scopeTimer = null, scopePending = true, scopeReady = false;
  let autoScanned = false, stopped = false, failCount = 0;

  // ---------- helpers ----------
  async function api(path, body, token = TOKEN) {
    const r = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "X-Token": token, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 403) { const e = new Error("bad-key"); e.badKey = true; throw e; }
    if (!r.ok) throw new Error(data.error || `Something went wrong (${r.status}).`);
    return data;
  }
  const size = (n) => {
    if (!n) return "0 MB";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i >= 3 ? 1 : 0)} ${u[i]}`;
  };
  const num = (n) => (n ?? 0).toLocaleString();
  const dur = (s) => {
    if (!isFinite(s) || s <= 0) return "";
    if (s < 90) return `${Math.round(s)} seconds`;
    if (s < 5400) return `${Math.round(s / 60)} minutes`;
    return `${(s / 3600).toFixed(1)} hours`;
  };
  const pct = (a, b) => (b ? Math.min(100, (100 * a) / b) : 0);
  const show = (el, on) => { el.hidden = !on; };
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function bindForm(form, fn) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = form.querySelector("button[type=submit]");
      const err = form.querySelector("[data-err]");
      err.textContent = ""; btn.disabled = true;
      const old = btn.textContent; btn.textContent = "Working…";
      try { await fn(Object.fromEntries(new FormData(form))); await poll(); }
      catch (x) { err.textContent = x.badKey ? "That access key didn't work." : x.message; }
      finally { btn.disabled = false; btn.textContent = old; }
    });
  }
  async function act(btn, errEl, fn) {
    if (errEl) errEl.textContent = "";
    btn.disabled = true;
    try { await fn(); await poll(); }
    catch (x) { if (errEl) errEl.textContent = x.message; else alert(x.message); }
    finally { btn.disabled = false; }
  }

  // ---------- access-key gate ----------
  function showGate(message) {
    show($("gate"), true); show($("wizard"), false);
    const err = $("f-gate").querySelector("[data-err]");
    err.textContent = message || "";
    $("foot").textContent = "";
  }
  bindForm($("f-gate"), async (v) => {
    let key = (v.key || "").trim();
    const m = key.match(/#t=([^&\s]+)/);           // they pasted the whole link: fine
    if (m) key = m[1];
    if (!key) throw new Error("Paste the access key first.");
    await api("/api/state", undefined, key);       // throws bad-key if wrong
    TOKEN = key;
    store.set("i2i-key", key);
    history.replaceState(null, "", `#t=${key}`);   // makes the address bookmarkable
    show($("gate"), false); show($("wizard"), true);
  });

  // ---------- 1 & 2: Immich + iCloud ----------
  bindForm($("f-immich"), async (v) => { await api("/api/immich", v); editingImmich = false; });
  $("immich-change").onclick = () => { editingImmich = true; render(); };
  bindForm($("f-signin"), async (v) => { await api("/api/icloud/signin", v); $("f-signin").reset(); });
  bindForm($("f-code"), async (v) => { await api("/api/icloud/code", v); $("f-code").reset(); });

  // ---------- 3: what to move ----------
  function fillYears(first, last) {
    const y0 = Number((first || iso(new Date())).slice(0, 4));
    const y1 = Math.max(Number((last || iso(new Date())).slice(0, 4)), new Date().getFullYear());
    for (const id of ["from-y", "to-y"]) {
      const sel = $(id); if (sel.options.length) continue;
      for (let y = y0; y <= y1; y++) sel.add(new Option(String(y), String(y)));
    }
    for (const id of ["from-m", "to-m"]) {
      const sel = $(id); if (sel.options.length) continue;
      MONTHS.forEach((m, i) => sel.add(new Option(m, String(i + 1).padStart(2, "0"))));
    }
    $("from-y").value = String(y0); $("from-m").value = "01";
    $("to-y").value = String(y1); $("to-m").value = "12";
  }

  function chosen() { return (document.querySelector("input[name=mode]:checked") || {}).value || "recent"; }

  function readScope() {
    const mode = chosen();
    if (mode === "all") return { mode: "all" };
    if (mode === "older_than") return { mode: "older_than", days: Number($("age").value) };
    if (mode === "period") {
      const start = `${$("from-y").value}-${$("from-m").value}-01`;
      const ty = Number($("to-y").value), tm = Number($("to-m").value);
      const end = iso(new Date(ty, tm, 0));            // last day of the "to" month
      if (start > end) return { error: "The start month is after the end month." };
      return { mode: "range", start, end };
    }
    const d = new Date(); d.setDate(d.getDate() - 30);
    return { mode: "range", start: iso(d), end: "" };   // recent: last 30 days
  }

  function remember() {
    store.set("i2i-choice", JSON.stringify({
      mode: chosen(), age: $("age").value,
      fy: $("from-y").value, fm: $("from-m").value, ty: $("to-y").value, tm: $("to-m").value,
    }));
  }

  function restoreChoice(sum) {
    fillYears(sum.library_first, sum.library_last);
    let c = null;
    try { c = JSON.parse(store.get("i2i-choice") || "null"); } catch (_) { c = null; }
    // First time ever: default to the small "try it first" selection.
    let mode = c?.mode || (sum.verified || sum.deleted ? "all" : "recent");
    if (c) {
      if (c.age) $("age").value = c.age;
      for (const [k, id] of [["fy", "from-y"], ["fm", "from-m"], ["ty", "to-y"], ["tm", "to-m"]]) {
        if (c[k] && [...$(id).options].some((o) => o.value === c[k])) $(id).value = c[k];
      }
    }
    const radio = document.querySelector(`input[name=mode][value=${mode}]`);
    (radio || document.querySelector("input[name=mode][value=recent]")).checked = true;
  }

  function scopeChanged() {
    clearTimeout(scopeTimer);
    scopePending = true;
    deletePreview = null;
    render();
    scopeTimer = setTimeout(async () => {
      const sc = readScope();
      $("scope-err").textContent = sc.error || "";
      if (sc.error) return;                 // stays pending: copying stays blocked
      remember();
      try { await api("/api/scope", sc); scopePending = false; await poll(); }
      catch (x) { $("scope-err").textContent = x.message; }
    }, 250);
  }
  document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", scopeChanged));
  // Touching a menu also selects the option it belongs to.
  const owner = { age: "older_than", "from-m": "period", "from-y": "period", "to-m": "period", "to-y": "period" };
  Object.keys(owner).forEach((id) => $(id).addEventListener("change", () => {
    document.querySelector(`input[name=mode][value=${owner[id]}]`).checked = true;
    scopeChanged();
  }));
  $("rescan").onclick = () => act($("rescan"), $("scope-err"), () => api("/api/scan", {}));

  // ---------- 4: copy ----------
  $("copy-start").onclick = () => act($("copy-start"), $("copy-err"), () => api("/api/transfer", {}));
  $("copy-stop").onclick = () => act($("copy-stop"), null, () => api("/api/stop", {}));

  // ---------- 5: delete ----------
  $("del-preview").onclick = () => act($("del-preview"), $("del-err"), async () => {
    deletePreview = await api("/api/delete/preview", {});
    $("del-word").value = "";
    $("del-go").disabled = true;
  });
  $("del-word").addEventListener("input", () => {
    $("del-go").disabled = $("del-word").value.trim().toUpperCase() !== "DELETE";
  });
  $("del-go").onclick = () => act($("del-go"), $("del-err"), async () => {
    await api("/api/delete", { confirm: $("del-word").value, count: deletePreview.count });
    deletePreview = null;
  });

  // ---------- 6: finish ----------
  $("quit").onclick = async () => {
    if (S && S.busy && !confirm("Something is still running. Stop it and sign out?")) return;
    try { await api("/api/quit", {}); } catch (_) { /* the app is restarting or closing */ }
    stopped = true;
    document.querySelectorAll("#wizard .step").forEach((s) => s.classList.add("locked"));
    $("s-finish").classList.remove("locked");
    $("quit").hidden = true; $("bye").hidden = false;
  };

  // ---------- render ----------
  function setStep(id, st) {
    const el = $(id);
    el.classList.toggle("locked", st === "locked");
    el.classList.toggle("active", st === "active");
    el.classList.toggle("done", st === "done");
    el.querySelector("h2").dataset.n = el.dataset.n;
  }

  function render() {
    if (!S) return;
    const server = S.mode === "server";
    const p = S.progress || {};
    const sum = S.summary;
    const job = p.job, running = S.busy;

    const immichDone = S.immich.connected && !editingImmich;
    const icloudDone = S.icloud.status === "signed_in";
    const scanned = S.scanned;
    const copyRunning = running && job === "transferring";
    const deleteRunning = running && job === "deleting";
    const anyVerified = sum && sum.verified > 0;
    const copyComplete = sum && sum.in_scope > 0 && sum.pending === 0 && sum.failed === 0;

    // which step is highlighted
    const ready = immichDone && icloudDone;
    const states = {
      "s-immich": immichDone ? "done" : "active",
      "s-icloud": !immichDone ? "locked" : icloudDone ? "done" : "active",
      "s-library": !ready ? "locked" : !scanned ? "active" : anyVerified ? "done" : "active",
      "s-copy": !(ready && scanned) ? "locked" : copyComplete && !copyRunning ? "done" : "active",
      "s-delete": !(ready && anyVerified) || copyRunning ? "locked" : "active",
      "s-finish": S.icloud.status === "signed_out" && !S.immich.connected ? "locked" : "",
    };
    const order = ["s-immich", "s-icloud", "s-library", "s-copy", "s-delete"];
    const active = order.filter((id) => states[id] === "active").pop() || "s-finish";
    order.forEach((id) => { if (states[id] === "active" && id !== active) states[id] = ""; });
    Object.entries(states).forEach(([id, st]) => setStep(id, st));
    if (active !== lastActive) {
      lastActive = active;
      $(active).scrollIntoView({ behavior: "smooth", block: "nearest" });
      const first = $(active).querySelector("input[type=email], input[name=url], input[name=code], input[name=api_key]");
      if (first && !first.closest("[hidden]")) setTimeout(() => first.focus(), 300);
    }

    // 1 Immich
    show($("f-immich"), !immichDone);
    show($("immich-ok"), immichDone);
    if (S.immich.connected) {
      $("immich-desc").textContent = `Connected to Immich at ${S.immich.url} as ${S.immich.user}`;
    }

    // 2 iCloud
    show($("f-signin"), S.icloud.status === "signed_out");
    show($("f-code"), S.icloud.status === "needs_2fa");
    show($("icloud-ok"), icloudDone);
    $("code-hint").innerHTML = S.icloud.delivery === "sms"
      ? "Apple sent a 6-digit code to your phone by text message. Type it below."
      : "Apple is asking you to confirm it's you. On your iPhone, iPad or Mac, tap <b>Allow</b>, then type the 6-digit code it shows.";
    if (icloudDone) {
      $("icloud-desc").textContent = `Signed in as ${S.icloud.apple_id}. ${num(S.icloud.library)} photos and videos in iCloud.`;
    }

    // 3 Library: read it automatically the first time we get here
    const scanning = running && job === "scanning";
    if (ready && !scanned && !running && !autoScanned) {
      autoScanned = true;
      api("/api/scan", {}).then(poll).catch((x) => ($("scope-err").textContent = x.message));
    }
    show($("scan-run"), scanning || (!scanned && icloudDone));
    if (scanning) {
      $("scan-count").textContent = p.total ? `${num(p.done)} of ${num(p.total)}` : "";
      $("scan-bar").style.width = `${pct(p.done, p.total)}%`;
    }
    if (!running && job === "scanning" && p.phase === "error") $("scope-err").textContent = p.message;
    show($("scope"), scanned && !!sum);
    if (sum && !scopeReady) {        // first summary: set up the menus, then send the choice
      scopeReady = true;
      restoreChoice(sum);
      scopeChanged();
    }
    if (sum) {
      const st = [
        [num(sum.in_scope), "photos & videos"], [size(sum.bytes), "to copy"],
        [num(sum.live), "Live Photos"], [num(sum.edited), "edited"],
      ];
      if (sum.verified) st.push([num(sum.verified), "already in Immich"]);
      if (sum.deleted) st.push([num(sum.deleted), "freed from iCloud"]);
      $("stats").innerHTML = st.map(([a, b]) => `<div class="stat"><b>${a}</b><span>${b}</span></div>`).join("");
      $("stats").style.opacity = scopePending ? 0.45 : 1;
      const range = sum.in_scope && sum.first ? `From ${sum.first} to ${sum.last}. ` : "";
      $("albums-line").textContent = !sum.in_scope ? "Nothing in iCloud matches this choice. Try another one."
        : range + (sum.albums.length ? `These albums will be recreated in Immich: ${sum.albums.join(", ")}.`
          : "No albums in this selection.");
    }

    // 4 Copy
    $("copy-note").textContent = server
      ? "You can close this page while it copies: it keeps going on your server. Come back to this address any time to see how it's doing."
      : "Keep the iCloud to Immich window open, and your computer awake, until it finishes. If it stops, just start it again and it carries on where it left off.";
    const remaining = sum ? sum.pending + sum.failed : 0;
    show($("copy-start-row"), !copyRunning && scanned);
    $("copy-start").textContent = !sum ? "Start copying"
      : scopePending ? "Updating…"
      : sum.in_scope && !remaining ? "All copied"
      : sum.verified && sum.pending ? `Continue: copy the remaining ${num(remaining)}`
      : sum.failed && !sum.pending ? `Try the ${num(sum.failed)} that failed again`
      : `Copy ${num(sum.in_scope)} photos & videos`;
    $("copy-start").disabled = !sum || remaining === 0 || scopePending;
    show($("copy-run"), copyRunning);
    if (copyRunning) {
      const albums = p.phase === "albums";
      $("copy-bar").style.width = `${albums ? 100 : pct(p.bytes_done, p.bytes_total)}%`;
      $("copy-count").textContent = albums ? "Recreating your albums" : `${num(p.done)} of ${num(p.total)}`;
      $("copy-bytes").textContent = `${size(p.bytes_done)} of ${size(p.bytes_total)}`;
      const left = p.rate ? (p.bytes_total - p.bytes_done) / p.rate : 0;
      $("copy-eta").textContent = p.rate && left ? `about ${dur(left)} left` : "starting…";
      $("copy-current").textContent = p.current ? `Now copying: ${p.current}` : "";
    }
    const copyFinished = !running && job === "transferring";
    show($("copy-result"), !!sum && !copyRunning && (copyFinished || sum.verified > 0));
    if (sum && !copyRunning) {
      let msg = `<span class="tick">✓</span> <b>${num(sum.verified + sum.deleted)}</b> of ${num(sum.in_scope)} are safely in Immich.`;
      if (sum.failed) msg += ` <span class="err">${num(sum.failed)} didn't copy.</span> They're still in iCloud, and you can try them again.`;
      if (copyFinished && p.phase === "stopped") msg = `Paused. ${msg} Click the button above to carry on.`;
      if (copyFinished && p.phase === "error") msg = `<span class="err">${escapeHtml(p.message)}</span><br>${msg}`;
      $("copy-result").innerHTML = msg;
    }
    show($("copy-next"), !!copyComplete && !copyRunning);
    const fails = S.failures || [];
    show($("fail-box"), fails.length > 0 && !copyRunning);
    $("fail-sum").textContent = `Show the ones that didn't copy (${fails.length}${fails.length === 50 ? "+" : ""})`;
    $("fail-list").innerHTML = fails.map((f) =>
      `<li>${escapeHtml(f.filename)} (${(f.taken || "").slice(0, 10)}): ${escapeHtml(f.error || "")}</li>`).join("");

    // 5 Delete
    show($("del-preview"), !deleteRunning);
    $("del-preview").disabled = !anyVerified || copyRunning || scopePending;
    show($("del-confirm"), !!deletePreview && !deleteRunning && deletePreview.count > 0);
    if (deletePreview) {
      $("del-summary").textContent = deletePreview.count
        ? `${num(deletePreview.count)} photos and videos (${size(deletePreview.bytes)}) are confirmed in Immich and would be deleted from iCloud and all your Apple devices.`
        : "";
      $("del-err").textContent = deletePreview.count ? "" : "Nothing in your current choice has been copied yet, so there's nothing to delete.";
    }
    show($("del-run"), deleteRunning);
    if (deleteRunning) {
      $("del-bar").style.width = `${pct(p.done, p.total)}%`;
      $("del-count").textContent = `${num(p.done)} of ${num(p.total)}${p.current ? ` · ${p.current}` : ""}`;
    }
    const delFinished = !running && job === "deleting";
    show($("del-done"), delFinished);
    if (delFinished) {
      const c = p.counts || {};
      let m = `<span class="tick">✓</span> Deleted <b>${num(c.deleted)}</b> from iCloud.`;
      if (c.skipped) m += ` ${num(c.skipped)} were kept because they're no longer confirmed in Immich.`;
      if (c.failed) m += ` <span class="err">${num(c.failed)} couldn't be deleted.</span>`;
      if (p.phase === "error") m = `<span class="err">${escapeHtml(p.message)}</span><br>` + m;
      $("del-result").innerHTML = m;
    }

    // 6 Finish
    $("finish-text").textContent = server
      ? "Signs you out of iCloud. The app stays on your server, ready for next time at this same address. It also signs you out by itself after 20 minutes without use."
      : "Signs you out of iCloud, deletes the temporary sign-in data and closes the app. Your progress is saved: open the app again any time and it carries on where it left off.";
    if (!$("quit").hidden) $("quit").textContent = server ? "Sign out" : "Sign out and close";
    $("bye").innerHTML = server
      ? "<b>Signed out.</b> To start again, reload this page in a few seconds."
      : "<b>All done.</b> You can close this tab.";

    // log + footer
    $("log").textContent = (p.log || []).join("\n");
    $("foot").textContent = `iCloud to Immich ${S.version}` +
      (server ? " · running on your server · tip: bookmark this page" : " · running on this computer");
  }

  // ---------- polling ----------
  async function poll() {
    if (stopped) return;
    try {
      S = await api("/api/state");
      failCount = 0;
      if (!$("gate").hidden || $("wizard").hidden) {
        show($("gate"), false); show($("wizard"), true);
        store.set("i2i-key", TOKEN);              // works: remember it for next time
      }
      render();
    } catch (x) {
      if (x.badKey) {
        store.del("i2i-key");
        return showGate(TOKEN ? "The saved access key didn't work. It may have changed. Paste the current one." : "");
      }
      if (++failCount > 3) {
        $("foot").textContent = S && S.mode === "server"
          ? "Can't reach the app right now. If you just signed out, it's restarting: reload in a few seconds."
          : "Lost contact with the app. Is its window still open?";
      }
    }
  }

  if (!TOKEN) showGate("");
  else poll();
  setInterval(() => { if (!document.hidden && TOKEN && $("gate").hidden) poll(); }, 1000);
})();
