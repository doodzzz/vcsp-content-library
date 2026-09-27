/* VCSP upload portal client: resumable chunked uploads, publish jobs, library view. No dependencies.
 *
 * Sign-in happens in the page, not through the browser's Basic-auth dialog: the
 * portal is also shown inside VMware Cloud Director, where browsers suppress
 * authentication dialogs for framed content from another origin. The same local
 * credentials are sent explicitly on every request and checked by nginx; they
 * live only in memory and are gone when the page is closed or reloaded. */
"use strict";
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const FILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
  const TYPE_LABEL = { "vcsp.ovf": "OVF template", "vcsp.iso": "ISO media", "vcsp.other": "Other files" };
  const STATUS_LABEL = {
    published: "Published", deferred: "Waiting", incomplete: "Incomplete",
    stale: "Serving previous version", pending: "Indexing",
  };

  const S = {
    config: null, items: [], pending: { uploads: [], ready: [] }, queue: [], busy: false, nameRe: null, auth: null,
    source: "upload", s3: { objects: [], next: null, selected: null, conn: null },
  };

  function basicAuth(user, password) {
    const bytes = new TextEncoder().encode(`${user}:${password}`);
    let bin = "";
    bytes.forEach((b) => { bin += String.fromCharCode(b); });
    return "Basic " + btoa(bin);
  }

  function el(tag, props = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids) if (kid != null) node.append(kid);
    return node;
  }

  function fmtBytes(n) {
    if (n == null) return "";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    const digits = i === 0 || Number.isInteger(n) ? 0 : n < 10 ? 2 : 1;
    return n.toFixed(digits) + " " + units[i];
  }
  const extOf = (name) => (name.includes(".") ? name.split(".").pop().toLowerCase() : "");

  let toastTimer;
  function toast(message, isError = false) {
    const t = $("#toast");
    t.textContent = message;
    t.classList.toggle("is-error", isError);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 9000 : 5000);
  }

  // ------------------------------------------------------------------ API
  async function api(path, { method = "GET", body } = {}) {
    const init = { method, credentials: "omit", headers: { "X-VCSP-Request": "1" } };
    if (S.auth) init.headers.Authorization = S.auth;
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers["Content-Type"] = "application/json";
    }
    let res;
    try {
      res = await fetch("api/" + path, init);
    } catch (e) {
      const err = new Error("The server is not reachable. Check the network connection and try again.");
      err.network = true;
      throw err;
    }
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error page from nginx */ }
    if (!res.ok) {
      const err = new Error(data.error || `The server answered ${res.status} ${res.statusText}.`);
      err.status = res.status;
      err.data = data;
      if (res.status === 401 && S.auth && path !== "config") signOut("Your sign-in is no longer valid. Sign in again.");
      throw err;
    }
    return data;
  }

  function putChunk(id, blob, start, total, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", "api/uploads/" + id);
      xhr.timeout = 15 * 60 * 1000;
      xhr.setRequestHeader("X-VCSP-Request", "1");
      if (S.auth) xhr.setRequestHeader("Authorization", S.auth);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("Content-Range", `bytes ${start}-${start + blob.size - 1}/${total}`);
      xhr.upload.onprogress = (e) => onProgress(e.loaded);
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
        if (xhr.status === 200) return resolve(data);
        const err = new Error(data.error || `The server answered ${xhr.status}.`);
        err.status = xhr.status;
        err.data = data;
        reject(err);
      };
      const netFail = () => { const e = new Error("The connection dropped."); e.network = true; reject(e); };
      xhr.onerror = netFail;
      xhr.ontimeout = netFail;
      xhr.send(blob);
    });
  }

  // ------------------------------------------------------------------ loading
  async function loadConfig() {
    const c = await api("config");
    S.config = c;
    S.nameRe = new RegExp(c.name_pattern);
    document.title = c.lib_name + " - content library";
    $("#lib-name").textContent = c.lib_name;
    $("#sub-url").value = c.subscription_url;
    $("#lib-auth").textContent = c.lib_auth === "basic"
      ? `Subscribers sign in as vcsp with ${c.tenant ? "this tenant's" : "the"} library password`
      : "Subscribers connect without a password";
    const exts = c.allowed_extensions.map((e) => "." + e);
    $("#drop-types").textContent =
      `${exts.join(", ")}. Up to ${fmtBytes(c.max_file_size)} per file. ` +
      (c.ova_extract ? "OVA files are unpacked into OVF templates." : "");
    $("#file-input").setAttribute("accept", exts.join(","));
    setVersion(c.lib_version);
    setQuota(c);
    setupS3(c.s3 || {});
  }

  // ------------------------------------------------------------------ S3 import
  function setupS3(s3) {
    $("#source-switch").hidden = !s3.enabled;
    if (!s3.enabled) { setSource("upload"); return; }
    const select = $("#s3-endpoint");
    const current = select.value;
    select.replaceChildren(...s3.endpoints.map((e) => el("option", { value: e, text: e })));
    if (s3.endpoints.includes(current)) select.value = current;
    if (!$("#s3-region").value) $("#s3-region").value = s3.region || "us-east-1";
    if (!document.querySelector('input[name="s3-addressing"]:checked')) {
      const radio = document.querySelector(`input[name="s3-addressing"][value="${s3.addressing === "virtual" ? "virtual" : "path"}"]`);
      if (radio) radio.checked = true;
    }
  }

  function setSource(source) {
    S.source = source;
    const radio = document.querySelector(`input[name="source"][value="${source}"]`);
    if (radio) radio.checked = true;
    $("#src-upload").hidden = source !== "upload";
    $("#src-s3").hidden = source !== "s3";
    updateButtons();
  }

  function s3Connection() {
    const addressing = document.querySelector('input[name="s3-addressing"]:checked');
    return {
      endpoint: $("#s3-endpoint").value,
      region: $("#s3-region").value.trim(),
      bucket: $("#s3-bucket").value.trim(),
      access_key: $("#s3-access").value.trim(),
      secret_key: $("#s3-secret").value,
      session_token: $("#s3-token").value.trim(),
      addressing: addressing ? addressing.value : "path",
    };
  }

  function s3Status(text, isError = false) {
    const st = $("#s3-status");
    st.textContent = text;
    st.classList.toggle("is-error", isError);
  }

  async function listS3(more) {
    if (S.busy) return;
    const conn = more ? S.s3.conn : { ...s3Connection(), prefix: $("#s3-prefix").value.trim() };
    if (!conn.bucket || !conn.access_key || !conn.secret_key) {
      return s3Status("Enter the bucket, access key ID and secret access key.", true);
    }
    const btn = $("#s3-list");
    btn.disabled = true;
    s3Status(more ? "Loading more objects" : "Connecting and listing the bucket");
    try {
      const page = await api("s3/list", { method: "POST", body: { ...conn, continuation: more ? S.s3.next : null } });
      if (!more) {
        S.s3.objects = [];
        S.s3.selected = null;
      }
      S.s3.conn = conn;
      S.s3.objects = S.s3.objects.concat(page.objects);
      S.s3.next = page.next;
      renderS3();
      const count = S.s3.objects.length;
      s3Status(count
        ? `${count} OVA or ISO file${count === 1 ? "" : "s"} found${page.next ? " so far" : ""}. Choose one to import.`
        : `No OVA or ISO files found${conn.prefix ? " under " + conn.prefix : ""}${page.next ? " yet; load more to keep looking" : ""}.`);
    } catch (e) {
      s3Status(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  function renderS3() {
    const list = $("#s3-objects");
    list.replaceChildren(...S.s3.objects.map((o, i) => {
      const input = el("input", { type: "radio", name: "s3-object", value: String(i) });
      if (S.s3.selected === o) input.checked = true;
      input.addEventListener("change", () => selectS3(o));
      const modified = o.modified ? new Date(o.modified).toLocaleDateString() : "";
      return el("li", {}, el("label", {},
        input,
        el("span", { class: "s3-key", text: o.key }),
        el("span", { class: "s3-meta", text: `${fmtBytes(o.size)}${modified ? "  " + modified : ""}` })));
    }));
    $("#s3-more-row").hidden = !S.s3.next;
    updateButtons();
  }

  function selectS3(obj) {
    S.s3.selected = obj;
    const nameField = $("#item-name");
    if (!nameField.value.trim()) {
      const stem = obj.key.split("/").pop().replace(/\.[^.]+$/, "");
      nameField.value = stem.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 80);
      onItemName();
    }
    updateButtons();
  }

  function clearS3(all) {
    S.s3 = { objects: [], next: null, selected: null, conn: null };
    $("#s3-objects").replaceChildren();
    $("#s3-more-row").hidden = true;
    s3Status("");
    if (all) for (const id of ["#s3-bucket", "#s3-prefix", "#s3-access", "#s3-secret", "#s3-token"]) $(id).value = "";
  }

  async function importS3(item, mode) {
    const obj = S.s3.selected;
    S.busy = true;
    $("#job").hidden = true;
    updateButtons();
    const btn = $("#upload-btn");
    btn.textContent = "Importing";
    try {
      const body = { ...S.s3.conn, key: obj.key, item, mode, description: $("#item-desc").value };
      const job = await runJob(await api("s3/import", { method: "POST", body }));
      if (job.state === "done") {
        const v = job.result.item_versions.length ? ` as version ${job.result.item_versions.join(", ")}` : "";
        toast(`Imported ${obj.key.split("/").pop()} and published ${item}${v}. Library version is now ${job.result.lib_version}.`);
        S.s3.selected = null;
        renderS3();
        $("#item-desc").value = "";
        $("#item-name").value = "";
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      S.busy = false;
      updateButtons();
      refreshAll();
    }
  }

  function setQuota(c) {
    $("#lib-quota").textContent = c.quota_bytes
      ? `Storage: ${fmtBytes(c.used_bytes)} of ${fmtBytes(c.quota_bytes)} used`
      : "";
  }

  function setVersion(v) {
    $("#lib-version").textContent = v ? `Library version ${v}` : "Library version not published yet";
  }

  async function loadItems() {
    const data = await api("items");
    S.items = data.items;
    setVersion(data.lib_version);
    renderItems();
    const list = $("#item-names");
    list.replaceChildren(...S.items.map((i) => el("option", { value: i.name })));
    onItemName();
  }

  async function loadPending() {
    S.pending = await api("uploads");
    renderPending();
    updateButtons();
  }

  async function refreshAll() {
    try {
      await Promise.all([
        loadItems(),
        loadPending(),
        S.config && S.config.quota_bytes ? api("config").then(setQuota) : null,
      ]);
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ------------------------------------------------------------------ library table
  function renderItems() {
    const filter = $("#filter").value.trim().toLowerCase();
    const rows = S.items.filter((i) => !filter || i.name.toLowerCase().includes(filter));
    $("#item-count").textContent = S.items.length ? `(${S.items.length})` : "";
    $("#empty").hidden = S.items.length > 0;
    const body = $("#items tbody");
    body.replaceChildren(...rows.map(itemRow));
  }

  function itemRow(item) {
    const nameCell = el("td", {},
      el("div", { class: "i-name", text: item.name }),
      item.description ? el("div", { class: "i-desc", text: item.description }) : null,
      el("div", { class: "i-files", text: item.files.map((f) => f.name).join(", ") }));
    const version = item.children.length
      ? item.children.map((c) => `${c.name} v${c.version ?? "-"}`).join(", ")
      : (item.version ? `v${item.version}` : "");
    const status = el("td", {},
      el("span", { class: `status ${item.status}`, text: STATUS_LABEL[item.status] || item.status }),
      item.reason ? el("div", { class: "reason", text: item.reason }) : null);
    const actions = el("td", { class: "i-actions" },
      el("button", { class: "btn link", type: "button", text: "Update", onclick: () => startUpdate(item) }),
      S.config && S.config.allow_delete
        ? el("button", { class: "btn danger", type: "button", text: "Delete", onclick: () => deleteItem(item) })
        : null);
    return el("tr", {},
      nameCell,
      el("td", { text: TYPE_LABEL[item.type] || "" }),
      el("td", { class: "num", text: fmtBytes(item.size) }),
      el("td", { class: "num", text: version }),
      status,
      actions);
  }

  function startUpdate(item) {
    $("#item-name").value = item.name;
    onItemName();
    $("#drop").focus();
    $("#drop").scrollIntoView({ block: "center", behavior: "smooth" });
  }

  async function deleteItem(item) {
    if (S.busy) return toast("Wait for the current upload to finish.", true);
    const ok = await confirmDialog(`Delete ${item.name} from the library?`,
      "Subscribed catalogs remove it on their next sync. VMs already deployed from it are not affected.",
      "Delete", true);
    if (!ok) return;
    try {
      const job = await runJob(await api(`items/${encodeURIComponent(item.name)}`, { method: "DELETE" }));
      if (job.state === "done") toast(`Deleted ${item.name}. Library version is now ${job.result.lib_version}.`);
      else toast(job.error, true);
    } catch (e) {
      toast(e.message, true);
    }
    refreshAll();
  }

  // ------------------------------------------------------------------ pending uploads
  function renderPending() {
    const list = $("#pending-list");
    const rows = [];
    for (const u of S.pending.uploads) {
      const pct = Math.floor((u.offset / u.size) * 100);
      rows.push(el("li", {},
        el("span", { class: "p-text" },
          el("code", { text: `${u.item} / ${u.filename}` }),
          el("span", { class: "p-sub", text: `${pct}% uploaded. Choose the same file for this item to resume.` })),
        el("button", { class: "btn link", type: "button", text: "Resume", onclick: () => startUpdate({ name: u.item }) }),
        el("button", { class: "btn danger", type: "button", text: "Discard", onclick: () => discard(`uploads/${u.id}`) })));
    }
    for (const r of S.pending.ready) {
      rows.push(el("li", {},
        el("span", { class: "p-text" },
          el("code", { text: r.item }),
          el("span", { class: "p-sub", text: `Uploaded, not published: ${r.files.map((f) => f.name).join(", ")}` })),
        el("button", { class: "btn link", type: "button", text: "Continue", onclick: () => startUpdate({ name: r.item }) }),
        el("button", { class: "btn danger", type: "button", text: "Discard", onclick: () => discard(`staged/${encodeURIComponent(r.item)}`) })));
    }
    list.replaceChildren(...rows);
    $("#pending").hidden = rows.length === 0;
  }

  async function discard(path) {
    try {
      await api(path, { method: "DELETE" });
    } catch (e) {
      toast(e.message, true);
    }
    loadPending();
  }

  // ------------------------------------------------------------------ compose form
  function readyFilesFor(name) {
    const r = S.pending.ready.find((x) => x.item === name);
    return r ? r.files : [];
  }

  function onItemName() {
    const name = $("#item-name").value.trim();
    const help = $("#item-help");
    const existing = S.items.find((i) => i.name === name);
    $("#mode-field").hidden = !existing;
    if (name && S.nameRe && !S.nameRe.test(name)) {
      help.textContent = "Use 1 to 80 letters, digits, dots, hyphens or underscores, starting with a letter or digit.";
      help.classList.add("is-error");
    } else {
      const staged = readyFilesFor(name);
      help.classList.remove("is-error");
      help.textContent = staged.length
        ? `Already uploaded for this item and included when you publish: ${staged.map((f) => f.name).join(", ")}.`
        : existing
          ? `Updating ${name} (version ${existing.version ?? "pending"}). Subscribers pick up the new version on their next sync.`
          : "Becomes the catalog item name. Letters, digits, dots, hyphens and underscores.";
      const desc = $("#item-desc");
      if (existing && !desc.value && existing.description) desc.value = existing.description;
    }
    updateButtons();
  }

  function addFiles(fileList) {
    if (S.busy) return;
    const allowed = S.config.allowed_extensions;
    const problems = [];
    for (const file of fileList) {
      const ext = extOf(file.name);
      if (!allowed.includes(ext)) {
        problems.push(`${file.name} is not allowed. Allowed file types: ${allowed.map((e) => "." + e).join(", ")}.`);
      } else if (!FILE_RE.test(file.name)) {
        problems.push(`Rename ${file.name}: use letters, digits, dots, hyphens or underscores only.`);
      } else if (file.size === 0) {
        problems.push(`${file.name} is empty.`);
      } else if (file.size > S.config.max_file_size) {
        problems.push(`${file.name} is ${fmtBytes(file.size)}; the limit is ${fmtBytes(S.config.max_file_size)}.`);
      } else {
        S.queue = S.queue.filter((q) => q.file.name !== file.name);
        S.queue.push({ file, sent: 0, state: "ready", message: "" });
      }
    }
    if (problems.length) toast(problems.join(" "), true);
    const ovf = S.queue.find((q) => ["ovf", "ova", "iso"].includes(extOf(q.file.name)));
    const nameField = $("#item-name");
    if (ovf && !nameField.value.trim()) {
      nameField.value = ovf.file.name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 80);
      onItemName();
    }
    renderQueue();
  }

  function renderQueue() {
    const list = $("#queue");
    list.replaceChildren(...S.queue.map((q, idx) => {
      const pct = q.file.size ? Math.min(100, (q.sent / q.file.size) * 100) : 0;
      const bar = el("span");
      bar.style.width = pct.toFixed(1) + "%";
      return el("li", {},
        el("div", { class: "q-row" },
          el("span", { class: "q-name", text: q.file.name }),
          el("span", { class: "q-size", text: fmtBytes(q.file.size) }),
          S.busy ? null : el("button", {
            class: "q-remove", type: "button", "aria-label": `Remove ${q.file.name}`, text: "Remove",
            onclick: () => { S.queue.splice(idx, 1); renderQueue(); },
          })),
        q.message ? el("div", { class: "q-status" + (q.state === "error" ? " is-error" : ""), text: q.message }) : null,
        el("div", { class: "q-bar" }, bar));
    }));
    updateButtons();
  }

  function updateButtons() {
    const name = $("#item-name").value.trim();
    const validName = name && S.nameRe && S.nameRe.test(name);
    const staged = readyFilesFor(name).length > 0;
    const btn = $("#upload-btn");
    if (S.source === "s3") {
      btn.textContent = "Import and publish";
      btn.disabled = S.busy || !validName || !S.s3.selected;
      $("#clear-btn").disabled = true;
      return;
    }
    btn.textContent = S.queue.length ? "Upload and publish" : "Publish";
    btn.disabled = S.busy || !validName || (!S.queue.length && !staged);
    $("#clear-btn").disabled = S.busy || !S.queue.length;
  }

  function setProgress(q, sent, startedAt) {
    q.sent = sent;
    const secs = (performance.now() - startedAt) / 1000;
    const rate = secs > 1 ? (sent - q.startOffset) / secs : 0;
    const eta = rate > 0 ? Math.round((q.file.size - sent) / rate) : null;
    q.message = `${fmtBytes(sent)} of ${fmtBytes(q.file.size)}` +
      (rate > 0 ? `, ${fmtBytes(rate)}/s` : "") +
      (eta != null && eta > 5 ? `, about ${eta > 90 ? Math.round(eta / 60) + " min" : eta + " s"} left` : "");
    const li = $("#queue").children[S.queue.indexOf(q)];
    if (li) {
      li.querySelector(".q-bar > span").style.width = ((sent / q.file.size) * 100).toFixed(1) + "%";
      let status = li.querySelector(".q-status");
      if (!status) {
        status = el("div", { class: "q-status" });
        li.insertBefore(status, li.querySelector(".q-bar"));
      }
      status.textContent = q.message;
    }
  }

  async function uploadOne(q, item) {
    const file = q.file;
    const start = () => api("uploads", { method: "POST", body: { item, filename: file.name, size: file.size } });
    let init = await start();
    let offset = init.offset;
    q.startOffset = offset;
    const startedAt = performance.now();
    if (init.complete) {
      q.sent = file.size;
      q.state = "done";
      q.message = "Already uploaded";
      renderQueue();
      return;
    }
    q.state = "uploading";
    let attempt = 0;
    while (offset < file.size) {
      const end = Math.min(offset + init.chunk_size, file.size);
      try {
        const res = await putChunk(init.id, file.slice(offset, end), offset, file.size,
          (loaded) => setProgress(q, offset + loaded, startedAt));
        offset = res.offset;
        attempt = 0;
        setProgress(q, offset, startedAt);
      } catch (err) {
        if (err.status === 409 && err.data && typeof err.data.offset === "number") {
          offset = err.data.offset;          // server tells us where to continue
          continue;
        }
        const retryable = err.network || err.status === 502 || err.status === 503 || err.status === 504 ||
          (err.status === 400 && err.data && typeof err.data.offset === "number");
        if (!retryable || attempt >= 6) throw err;
        attempt += 1;
        q.message = `Connection problem, retrying (${attempt} of 6)`;
        renderQueue();
        await sleep(Math.min(30000, 1000 * 2 ** attempt));
        init = await start();                // re-sync with the bytes the server actually has
        offset = init.offset;
        if (init.complete) break;
      }
    }
    q.sent = file.size;
    q.state = "done";
    q.message = "Uploaded and verified";
    renderQueue();
  }

  async function runJob(job) {
    const panel = $("#job");
    const render = (j) => {
      panel.hidden = false;
      panel.classList.toggle("is-error", j.state === "failed");
      panel.classList.toggle("is-done", j.state === "done");
      const kids = [el("ol", {}, ...j.steps.map((s) => el("li", { text: s })))];
      for (const n of j.notes) kids.push(el("div", { class: "job-note", text: n }));
      if (j.progress && j.state === "running") {
        const bar = el("span");
        bar.style.width = (j.progress.total ? (j.progress.done / j.progress.total) * 100 : 0).toFixed(1) + "%";
        kids.push(el("div", { class: "job-progress" }, bar));
        kids.push(el("div", { class: "job-progress-text", text: `${fmtBytes(j.progress.done)} of ${fmtBytes(j.progress.total)} downloaded` }));
      }
      if (j.cancellable) {
        kids.push(el("div", { class: "actions" }, el("button", {
          class: "btn quiet", type: "button", text: "Cancel import",
          onclick: async (ev) => {
            ev.target.disabled = true;
            try { await api("jobs/" + j.id, { method: "DELETE" }); } catch (e) { toast(e.message, true); }
          },
        })));
      }
      if (j.state === "failed") kids.push(el("div", { class: "job-result", text: j.error }));
      if (j.state === "done" && j.result) {
        const r = j.result;
        const text = j.kind === "delete"
          ? `Deleted ${r.item}. Library version ${r.lib_version}.`
          : `Published ${r.item}${r.item_versions && r.item_versions.length ? " as version " + r.item_versions.join(", ") : ""}. ` +
            `Library version ${r.lib_version}; subscribers pick it up on their next sync.`;
        kids.push(el("div", { class: "job-result", text }));
      }
      panel.replaceChildren(...kids);
    };
    render(job);
    while (job.state === "running") {
      await sleep(800);
      job = await api("jobs/" + job.id);
      render(job);
    }
    return job;
  }

  async function submit(ev) {
    ev.preventDefault();
    const item = $("#item-name").value.trim();
    if (!S.nameRe.test(item) || S.busy) return;
    const existing = S.items.find((i) => i.name === item);
    const mode = existing ? document.querySelector('input[name="mode"]:checked').value : "replace";
    if (S.source === "s3") {
      if (!S.s3.selected) return;
      if (existing && mode === "replace") {
        const ok = await confirmDialog(`Replace every file of ${item}?`,
          `The item's current files are replaced by ${S.s3.selected.key.split("/").pop()} from S3.`, "Replace", false);
        if (!ok) return;
      }
      return importS3(item, mode);
    }
    if (existing && mode === "replace" && S.queue.length) {
      const ok = await confirmDialog(`Replace every file of ${item}?`,
        `The item's current files are replaced by the ${S.queue.length} file(s) you selected.`, "Replace", false);
      if (!ok) return;
    }
    S.busy = true;
    $("#job").hidden = true;
    renderQueue();
    const btn = $("#upload-btn");
    try {
      for (let i = 0; i < S.queue.length; i += 1) {
        btn.textContent = `Uploading ${i + 1} of ${S.queue.length}`;
        await uploadOne(S.queue[i], item);
      }
      btn.textContent = "Publishing";
      const body = { mode, description: $("#item-desc").value };
      const job = await runJob(await api(`items/${encodeURIComponent(item)}/publish`, { method: "POST", body }));
      if (job.state === "done") {
        const v = job.result.item_versions.length ? ` as version ${job.result.item_versions.join(", ")}` : "";
        toast(`Published ${item}${v}. Library version is now ${job.result.lib_version}.`);
        S.queue = [];
        $("#item-desc").value = "";
        $("#item-name").value = "";
      }
    } catch (err) {
      const q = S.queue.find((x) => x.state === "uploading" || x.state === "ready");
      if (q) { q.state = "error"; q.message = err.message; }
      toast(err.message, true);
    } finally {
      S.busy = false;
      renderQueue();
      refreshAll();
    }
  }

  // ------------------------------------------------------------------ wiring
  function wire() {
    const drop = $("#drop");
    const input = $("#file-input");
    drop.addEventListener("click", () => input.click());
    drop.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
    for (const type of ["dragenter", "dragover"]) {
      drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("is-over"); });
    }
    for (const type of ["dragleave", "drop"]) {
      drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.remove("is-over"); });
    }
    drop.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => e.preventDefault());

    $("#item-name").addEventListener("input", onItemName);
    $("#compose").addEventListener("submit", submit);
    $("#clear-btn").addEventListener("click", () => { S.queue = []; renderQueue(); });
    $("#filter").addEventListener("input", renderItems);
    $("#refresh").addEventListener("click", refreshAll);
    $("#copy-url").addEventListener("click", async () => {
      const url = $("#sub-url");
      try {
        await navigator.clipboard.writeText(url.value);
      } catch (e) {
        url.select();
        document.execCommand("copy");
      }
      $("#copy-url").textContent = "Copied";
      setTimeout(() => { $("#copy-url").textContent = "Copy URL"; }, 2000);
    });
    window.addEventListener("beforeunload", (e) => {
      if (S.busy) { e.preventDefault(); e.returnValue = ""; }
    });
    setInterval(() => { if (S.auth && !S.busy && document.visibilityState === "visible") refreshAll(); }, 30000);
    $("#signin-form").addEventListener("submit", signIn);
    document.querySelectorAll('input[name="source"]').forEach((r) =>
      r.addEventListener("change", () => { if (S.busy) { setSource(S.source); return; } setSource(r.value); }));
    $("#s3-list").addEventListener("click", () => listS3(false));
    $("#s3-more").addEventListener("click", () => listS3(true));
    for (const id of ["#s3-endpoint", "#s3-bucket", "#s3-region", "#s3-prefix"]) {
      $(id).addEventListener("change", () => clearS3(false));
    }
    $("#signin").addEventListener("cancel", (e) => e.preventDefault());   // sign-in cannot be dismissed
    $("#signout").addEventListener("click", () => {
      if (S.busy) return toast("Wait for the current upload to finish.", true);
      signOut("");
    });
  }

  // ------------------------------------------------------------------ sign-in and dialogs
  function showSignIn(message) {
    const dlg = $("#signin");
    $("#signin-error").textContent = message || "";
    $("#signin-error").hidden = !message;
    $("#signin-password").value = "";
    if (!dlg.open) dlg.showModal();
    ($("#signin-user").value ? $("#signin-password") : $("#signin-user")).focus();
  }

  async function signIn(ev) {
    ev.preventDefault();
    const user = $("#signin-user").value.trim();
    const password = $("#signin-password").value;
    if (!user || !password) return;
    const btn = $("#signin-submit");
    btn.disabled = true;
    S.auth = basicAuth(user, password);
    try {
      await loadConfig();
      $("#signin").close();
      $("#lib-user").textContent = `Signed in as ${S.config.user}`;
      $("#signout").hidden = false;
      await refreshAll();
    } catch (e) {
      S.auth = null;
      showSignIn(e.status === 401 ? "The user name or password is incorrect." : e.message);
    } finally {
      btn.disabled = false;
    }
  }

  function signOut(message) {
    S.auth = null;
    S.config = null;
    S.items = [];
    S.queue = [];
    S.pending = { uploads: [], ready: [] };
    $("#items tbody").replaceChildren();
    $("#queue").replaceChildren();
    $("#pending").hidden = true;
    $("#job").hidden = true;
    $("#lib-user").textContent = "";
    $("#signout").hidden = true;
    clearS3(true);
    setSource("upload");
    showSignIn(message);
  }

  function confirmDialog(title, text, okLabel, danger) {
    const dlg = $("#confirm");
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    const ok = $("#confirm-ok");
    ok.textContent = okLabel;
    ok.className = danger ? "btn danger-solid" : "btn primary";
    return new Promise((resolve) => {
      const done = (value) => {
        dlg.removeEventListener("close", onClose);
        ok.onclick = null;
        $("#confirm-cancel").onclick = null;
        if (dlg.open) dlg.close();
        resolve(value);
      };
      const onClose = () => done(false);
      dlg.addEventListener("close", onClose);
      ok.onclick = () => done(true);
      $("#confirm-cancel").onclick = () => done(false);
      dlg.showModal();
      $("#confirm-cancel").focus();
    });
  }

  function start() {
    wire();
    showSignIn("");
  }

  start();
})();
