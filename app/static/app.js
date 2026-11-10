(() => {
  "use strict";
  const TOKEN = new URLSearchParams(location.hash.slice(1)).get("t") || "";
  const $ = (id) => document.getElementById(id);
  let S = null;              // latest server snapshot
  let editingImmich = false;
  let lastActive = null;
  let deletePreview = null;  // {count, bytes}
  let scopeTimer = null;

  // ---------- helpers ----------
  async function api(path, body) {
    const r = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
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
    if (s < 90) return `${Math.round(s)} s`;
    if (s < 5400) return `${Math.round(s / 60)} min`;
    return `${(s / 3600).toFixed(1)} h`;
  };
  const pct = (a, b) => (b ? Math.min(100, (100 * a) / b) : 0);
  const show = (el, on) => { el.hidden = !on; };

  function bindForm(form, fn) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = form.querySelector("button[type=submit]");
      const err = form.querySelector("[data-err]");
      err.textContent = ""; btn.disabled = true;
      const old = btn.textContent; btn.textContent = "Working…";
      try { await fn(Object.fromEntries(new FormData(form))); await poll(); }
      catch (x) { err.textContent = x.message; }
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

  // ---------- actions ----------
  bindForm($("f-immich"), async (v) => { await api("/api/immich", v); editingImmich = false; });
  $("immich-change").onclick = () => { editingImmich = true; render(); };
  bindForm($("f-signin"), async (v) => {
    await api("/api/icloud/signin", v);
    $("f-signin").reset();
  });
  bindForm($("f-code"), async (v) => { await api("/api/icloud/code", v); $("f-code").reset(); });

  function readScope() {
    const mode = document.querySelector("input[name=mode]:checked").value;
    return { mode, days: $("days").value, start: $("start").value.trim(), end: $("end").value.trim() };
  }
  // While the selection is being edited or is invalid, copying is blocked so the
  // numbers on screen always match what would actually be copied.
  let scopePending = false;
  function scopeChanged() {
    clearTimeout(scopeTimer);
    scopePending = true;
    deletePreview = null;
    render();
    scopeTimer = setTimeout(async () => {
      const sc = readScope();
      const badDate = sc.mode === "range" &&
        [sc.start, sc.end].some((d) => d && !/^\d{4}-\d{2}-\d{2}$/.test(d));
      const badDays = sc.mode === "older_than" && !(Number(sc.days) >= 1);
      $("scope-err").textContent = badDate ? "Dates look like 2021-06-30."
        : badDays ? "Enter a number of days." : "";
      if (badDate || badDays) return;   // stays pending: Start stays disabled
      try { await api("/api/scope", sc); scopePending = false; await poll(); }
      catch (x) { $("scope-err").textContent = x.message; }
    }, 300);
  }
  document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", scopeChanged));
  // Typing in a field selects the option it belongs to, so the summary always matches what you see.
  const owner = { days: "older_than", start: "range", end: "range" };
  ["days", "start", "end"].forEach((id) => $(id).addEventListener("input", () => {
    document.querySelector(`input[name=mode][value=${owner[id]}]`).checked = true;
    scopeChanged();
  }));
  $("rescan").onclick = () => act($("rescan"), $("scope-err"), () => api("/api/scan", {}));

  $("copy-start").onclick = () => act($("copy-start"), $("copy-err"), () => api("/api/transfer", {}));
  $("copy-stop").onclick = () => act($("copy-stop"), null, () => api("/api/stop", {}));

  $("del-preview").onclick = () => act($("del-preview"), $("del-err"), async () => {
    deletePreview = await api("/api/delete/preview", {});
    $("del-word").value = "";
  });
  $("del-word").addEventListener("input", () => {
    $("del-go").disabled = $("del-word").value.trim().toUpperCase() !== "DELETE";
  });
  $("del-go").onclick = () => act($("del-go"), $("del-err"), async () => {
    await api("/api/delete", { confirm: $("del-word").value, count: deletePreview.count });
    deletePreview = null;
  });

  $("quit").onclick = async () => {
    if (S && S.busy && !confirm("Something is still running. Stop it and close?")) return;
    try { await api("/api/quit", {}); } catch (_) { /* server is going away */ }
    stopped = true;
    document.querySelectorAll(".step").forEach((s) => s.classList.add("locked"));
    $("s-finish").classList.remove("locked");
    $("quit").hidden = true; $("bye").hidden = false;
  };

  // ---------- render ----------
  function setStep(id, state) {
    const el = $(id);
    el.classList.toggle("locked", state === "locked");
    el.classList.toggle("active", state === "active");
    el.classList.toggle("done", state === "done");
    el.querySelector("h2").dataset.n = el.dataset.n;
  }

  function render() {
    if (!S) return;
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

    // Each step: "locked" (greyed, body hidden), "active" (highlighted), "done" (✓), or "" (open, neutral).
    const ready = immichDone && icloudDone;
    const states = {
      "s-immich": immichDone ? "done" : "active",
      "s-icloud": !immichDone ? "locked" : icloudDone ? "done" : "active",
      "s-library": !ready ? "locked" : !scanned ? "active" : anyVerified ? "done" : "active",
      "s-copy": !(ready && scanned) ? "locked" : copyComplete && !copyRunning ? "done" : "active",
      "s-delete": !(ready && anyVerified) || copyRunning ? "locked" : "active",
      "s-finish": S.icloud.status === "signed_out" && !S.immich.connected ? "locked" : "",
    };
    // Only one step is highlighted: the last unlocked step that still needs something.
    const order = ["s-immich", "s-icloud", "s-library", "s-copy", "s-delete"];
    let active = order.filter((id) => states[id] === "active").pop() || "s-finish";
    order.forEach((id) => { if (states[id] === "active" && id !== active) states[id] = ""; });
    Object.entries(states).forEach(([id, st]) => setStep(id, st));
    if (active !== lastActive) {
      lastActive = active;
      $(active).scrollIntoView({ behavior: "smooth", block: "nearest" });
      const first = $(active).querySelector("input:not([type=radio]):not([hidden])");
      if (first && !first.closest("[hidden]")) setTimeout(() => first.focus(), 300);
    }

    // 1 Immich
    show($("f-immich"), !immichDone);
    show($("immich-ok"), immichDone);
    if (S.immich.connected) {
      $("immich-desc").textContent = `Connected to ${S.immich.url} as ${S.immich.user} (Immich ${S.immich.version})`;
    }

    // 2 iCloud
    show($("f-signin"), S.icloud.status === "signed_out");
    show($("f-code"), S.icloud.status === "needs_2fa");
    show($("icloud-ok"), icloudDone);
    $("code-hint").innerHTML = S.icloud.delivery === "sms"
      ? "Apple sent a 6-digit code by text message. Type it here."
      : "Apple sent a 6-digit code to your iPhone or Mac. Tap <b>Allow</b>, then type the code here.";
    if (icloudDone) {
      $("icloud-desc").textContent = `Signed in as ${S.icloud.apple_id}: ${num(S.icloud.library)} items in iCloud Photos`;
    }

    // 3 Library: start a scan automatically the first time we get here
    const scanning = running && job === "scanning";
    if (immichDone && icloudDone && !scanned && !running && !autoScanned) {
      autoScanned = true;
      api("/api/scan", {}).then(poll).catch((x) => ($("scope-err").textContent = x.message));
    }
    show($("scan-run"), scanning || (!scanned && icloudDone));
    if (scanning) {
      $("scan-count").textContent = p.total ? `${num(p.done)} of ${num(p.total)}` : "";
      $("scan-bar").style.width = `${pct(p.done, p.total)}%`;
    }
    if (!running && job === "scanning" && p.phase === "error") $("scope-err").textContent = p.message;
    show($("scope"), scanned);
    if (sum) {
      const st = [
        [num(sum.in_scope), "items"], [size(sum.bytes), "to copy"],
        [num(sum.photos), "photos"], [num(sum.videos), "videos"],
        [num(sum.live), "Live Photos"], [num(sum.edited), "edited"],
      ];
      if (sum.verified) st.push([num(sum.verified), "already copied"]);
      if (sum.deleted) st.push([num(sum.deleted), "freed from iCloud"]);
      $("stats").innerHTML = st.map(([a, b]) => `<div class="stat"><b>${a}</b><span>${b}</span></div>`).join("");
      const range = sum.first ? `From ${sum.first} to ${sum.last}. ` : "";
      $("albums-line").textContent = range + (sum.albums.length
        ? `Albums that will be recreated in Immich: ${sum.albums.join(", ")}.`
        : "No albums in this selection.");
    }
    if (!scopeSynced) {   // restore the server's scope once (e.g. after a page reload)
      scopeSynced = true;
      const sc = S.scope;
      const radio = document.querySelector(`input[name=mode][value=${sc.mode}]`);
      if (radio) radio.checked = true;
      $("days").value = sc.days || 90;
      $("start").value = sc.start || "";
      $("end").value = sc.end || "";
    }

    // 4 Copy
    const remaining = sum ? sum.pending + sum.failed : 0;
    show($("copy-start-row"), !copyRunning && scanned);
    $("copy-start").textContent = sum && sum.in_scope && !remaining ? "All copied"
      : sum && sum.verified && remaining
      ? `Continue: copy the remaining ${num(remaining)}`
      : sum && sum.failed && !sum.pending ? `Retry ${num(sum.failed)} that failed`
      : `Start copying${sum ? ` ${num(sum.in_scope)} items` : ""}`;
    $("copy-start").disabled = !sum || remaining === 0 || scopePending;
    $("stats").style.opacity = scopePending ? 0.45 : 1;
    show($("copy-run"), copyRunning);
    if (copyRunning) {
      const pc = p.phase === "albums" ? 100 : pct(p.bytes_done, p.bytes_total);
      $("copy-bar").style.width = `${pc}%`;
      $("copy-count").textContent = p.phase === "albums" ? "Recreating albums" : `${num(p.done)} of ${num(p.total)} items`;
      $("copy-bytes").textContent = `${size(p.bytes_done)} of ${size(p.bytes_total)}`;
      const left = p.rate ? (p.bytes_total - p.bytes_done) / p.rate : 0;
      $("copy-eta").textContent = p.rate ? `${size(p.rate)}/s${left ? `, about ${dur(left)} left` : ""}` : "starting…";
      $("copy-current").textContent = p.current || "";
    }
    const copyFinished = !running && job === "transferring";
    show($("copy-result"), copyFinished || (sum && sum.verified > 0 && !copyRunning));
    if (sum && !copyRunning) {
      let msg = `<span class="tick">✓</span> <b>${num(sum.verified + sum.deleted)}</b> of ${num(sum.in_scope)} items are safely in Immich.`;
      if (sum.failed) msg += ` <span class="err">${num(sum.failed)} failed.</span> They stay in iCloud and you can retry them.`;
      if (copyFinished && p.phase === "stopped") msg = `Stopped. ${msg}`;
      if (copyFinished && p.phase === "error") msg = `<span class="err">${p.message}</span><br>${msg}`;
      $("copy-result").innerHTML = msg;
    }
    const fails = S.failures || [];
    show($("fail-box"), fails.length > 0 && !copyRunning);
    $("fail-sum").textContent = `Show items that failed (${fails.length}${fails.length === 50 ? "+" : ""})`;
    $("fail-list").innerHTML = fails.map((f) =>
      `<li>${escapeHtml(f.filename)} (${(f.taken || "").slice(0, 10)}): ${escapeHtml(f.error || "")}</li>`).join("");

    // 5 Delete
    show($("del-preview"), !deleteRunning);
    $("del-preview").disabled = !anyVerified || copyRunning || scopePending;
    show($("del-confirm"), !!deletePreview && !deleteRunning && deletePreview.count > 0);
    if (deletePreview) {
      $("del-summary").textContent = deletePreview.count
        ? `${num(deletePreview.count)} items (${size(deletePreview.bytes)}) are confirmed in Immich and can be deleted from iCloud and all your Apple devices.`
        : "Nothing in the current selection is ready to delete yet.";
      if (!deletePreview.count) $("del-err").textContent = "Nothing in the current selection is ready to delete yet.";
    }
    show($("del-run"), deleteRunning);
    if (deleteRunning) {
      $("del-bar").style.width = `${pct(p.done, p.total)}%`;
      $("del-count").textContent = `${num(p.done)} of ${num(p.total)} · ${p.current || ""}`;
    }
    const delFinished = !running && job === "deleting";
    show($("del-done"), delFinished);
    if (delFinished) {
      const c = p.counts || {};
      let m = `<span class="tick">✓</span> Deleted <b>${num(c.deleted)}</b> items from iCloud.`;
      if (c.skipped) m += ` ${num(c.skipped)} were kept because they're no longer confirmed in Immich.`;
      if (c.failed) m += ` <span class="err">${num(c.failed)} couldn't be deleted.</span>`;
      if (p.phase === "error") m = `<span class="err">${escapeHtml(p.message)}</span><br>` + m;
      $("del-result").innerHTML = m;
    }

    // log + footer
    $("log").textContent = (p.log || []).join("\n");
    $("foot").textContent = `iCloud to Immich ${S.version} · runs only on this computer`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---------- polling ----------
  let autoScanned = false, stopped = false, failCount = 0, scopeSynced = false;
  async function poll() {
    if (stopped) return;
    try {
      S = await api("/api/state");
      failCount = 0;
      render();
    } catch (x) {
      if (++failCount > 3) $("foot").textContent = "Lost contact with the app. Is its window still open?";
    }
  }
  if (!TOKEN) {
    document.querySelector("main").innerHTML =
      "<p class='warn'>Open this page from the address shown in the iCloud to Immich window.</p>";
  } else {
    poll();
    setInterval(() => { if (!document.hidden) poll(); }, 1000);
  }
})();
