"use strict";
// All website-provided text is inserted with textContent (never innerHTML): sites are untrusted.
const TOKEN = document.body.dataset.token;
const $ = (id) => document.getElementById(id);
let runId = null;
let reports = [];

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.add("hidden"), 6000);
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-VSA-Token": TOKEN },
  };
  const response = await fetch("/api/" + path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

async function busy(button, work) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Working…";
  try { return await work(); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = label; }
}

function show(id) { $(id).classList.remove("hidden"); }
function badge(status) { return el("span", { class: "badge " + status }, status); }
function seconds(value) {
  if (value === null || value === undefined) return "unknown";
  const s = Math.round(value);
  return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
function bytes(value) {
  if (!value) return "unknown";
  const units = ["B", "KB", "MB", "GB"];
  let n = value, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + units[i];
}

// --- 1. analyse -------------------------------------------------------------------------------
$("analyse-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = await api("analyse", { url: $("url").value });
    runId = data.run;
    const p = data.profile, page = p.page;
    const box = $("profile");
    box.replaceChildren(
      el("dl", {},
        el("dt", {}, "Site"), el("dd", {}, p.site),
        el("dt", {}, "Plugin"), el("dd", {}, p.plugin),
        el("dt", {}, "Page title"), el("dd", {}, page.title || "(none)"),
        el("dt", {}, "HTTP status"), el("dd", {}, String(page.status)),
        el("dt", {}, "Rendered with browser"), el("dd", {}, page.rendered ? "yes" : "no"),
        el("dt", {}, "Structured data"), el("dd", {}, page.structured_data.map((d) => d["@type"]).join(", ") || "none"),
        el("dt", {}, "Navigation links"), el("dd", {}, String(page.navigation.length)),
        el("dt", {}, "Media on page"), el("dd", {}, String(page.media_urls.length)),
        el("dt", {}, "Notes"), el("dd", {}, page.notes.join("; ") || "none")),
      el("h3", {}, "Search interfaces"),
      p.search_interfaces.length
        ? el("table", {}, el("tr", {}, el("th", {}, "Kind"), el("th", {}, "Template"), el("th", {}, "Evidence")),
            p.search_interfaces.map((s) => el("tr", {}, el("td", {}, s.kind), el("td", {}, el("span", { class: "mono" }, s.url_template)), el("td", {}, s.evidence))))
        : el("p", { class: "errors" }, "No search interface found."));
    show("profile");
    show("search-section");
    toast("Analysed " + p.site);
  });
});

// --- 2. search ---------------------------------------------------------------------------------
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = await api("search", { run: runId, query: $("query").value, year: $("year").value });
    const box = $("results");
    if (!data.results.length) {
      box.replaceChildren(el("p", {}, "No exactly matching results. ", el("button", { class: "secondary", onclick: diagnose }, "Diagnose")));
      show("results");
      return;
    }
    const rows = data.results.map((r, i) => el("tr", {},
      el("td", {}, el("input", { type: "checkbox", checked: true, value: String(i), "aria-label": "select " + r.title })),
      el("td", {}, r.title), el("td", {}, r.year || ""),
      el("td", {}, el("a", { href: r.url, target: "_blank", rel: "noopener noreferrer" }, r.item_id))));
    const button = el("button", { type: "button" }, "Discover and verify media");
    button.addEventListener("click", () => busy(button, discoverAndVerify));
    box.replaceChildren(el("table", {}, el("tr", {}, el("th", {}, ""), el("th", {}, "Title"), el("th", {}, "Year"), el("th", {}, "Item")), rows),
      el("div", { class: "row" }, button));
    show("results");
  });
});

async function discoverAndVerify() {
  const indices = [...$("results").querySelectorAll("input[type=checkbox]:checked")].map((c) => Number(c.value));
  if (!indices.length) throw new Error("Select at least one result");
  const found = await api("discover", { run: runId, indices });
  toast(`Found ${found.candidates.length} media sources; verifying…`);
  const data = await api("verify", { run: runId });
  reports = data.reports;
  renderMedia(data.reports, data.groups);
  show("media-section");
  show("generate-section");
}

// --- 3. media -------------------------------------------------------------------------------------
function preview(candidate) {
  const video = el("video", { controls: true, preload: "metadata", muted: true, playsinline: true });
  if (candidate.kind === "progressive") {
    video.src = candidate.url;
  } else if (candidate.kind === "hls") {
    if (video.canPlayType("application/vnd.apple.mpegurl")) video.src = candidate.url;
    else loadScript("https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js").then(() => {
      if (window.Hls && window.Hls.isSupported()) { const hls = new window.Hls(); hls.loadSource(candidate.url); hls.attachMedia(video); }
    }).catch(() => {});
  } else if (candidate.kind === "dash") {
    loadScript("https://cdnjs.cloudflare.com/ajax/libs/dashjs/4.7.4/dash.all.min.js").then(() => {
      window.dashjs.MediaPlayer().create().initialize(video, candidate.url, false);
    }).catch(() => {});
  }
  return video;
}

const scripts = {};
function loadScript(src) {
  scripts[src] = scripts[src] || new Promise((resolve, reject) => {
    document.head.append(el("script", { src, crossorigin: "anonymous", onload: resolve, onerror: reject }));
  });
  return scripts[src];
}

function renderMedia(items, groups) {
  const cards = items.map((r, index) => {
    const c = r.candidate;
    const variants = r.variants.length ? r.variants.map((v) => `${v.width}×${v.height} ${Math.round(v.bandwidth / 1000)} kb/s`).join(", ") : "";
    return el("article", { class: "card" },
      el("div", { class: "row" }, badge(r.status), el("strong", {}, c.label || c.kind), el("span", { class: "hint" }, c.site + " · " + (c.server || ""))),
      preview(c),
      el("dl", {},
        el("dt", {}, "Format"), el("dd", {}, [r.container, c.kind].filter(Boolean).join(" / ")),
        el("dt", {}, "Video"), el("dd", {}, r.video_codec ? `${r.video_codec} ${r.width}×${r.height}` : "none"),
        el("dt", {}, "Audio"), el("dd", {}, r.audio_codec || "none"),
        el("dt", {}, "Duration"), el("dd", {}, seconds(r.duration)),
        el("dt", {}, "Size"), el("dd", {}, bytes(r.size)),
        el("dt", {}, "Decoded"), el("dd", {}, `${r.decoded_frames} frames`),
        variants ? el("dt", {}, "Variants") : null, variants ? el("dd", {}, variants) : null,
        el("dt", {}, "Site claims"), el("dd", {}, Object.entries(c.declared).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"),
        el("dt", {}, "Fingerprint"), el("dd", {}, el("span", { class: "mono" }, (r.fingerprint.head_sha256 || "").slice(0, 16)))),
      r.errors.length ? el("div", { class: "errors" }, r.errors.join("; ")) : null,
      el("details", {}, el("summary", {}, "Technical evidence"), el("pre", { class: "mono" }, JSON.stringify(r.evidence, null, 1))),
      el("div", { class: "row" },
        el("button", { class: "good", onclick: (e) => review(e.target, "working", c.url) }, "Working"),
        el("button", { class: "bad", onclick: (e) => review(e.target, "wrong", c.url) }, "Something's wrong")));
  });
  $("media").replaceChildren(el("div", { class: "cards" }, cards));
  $("groups").replaceChildren(el("h3", {}, `Distinct media: ${groups.length}`),
    el("table", {}, el("tr", {}, el("th", {}, "Identity"), el("th", {}, "Sources"), el("th", {}, "Sites"), el("th", {}, "Same duration as")),
      groups.map((g) => el("tr", {}, el("td", {}, el("span", { class: "mono" }, g.media_id.slice(0, 40))),
        el("td", {}, String(g.members.length)), el("td", {}, g.sites.join(", ") + (g.cross_site ? " (cross-site)" : "")),
        el("td", {}, String(g.related.length))))));
  show("groups");
}

async function review(button, verdict, target) {
  return busy(button, async () => {
    const note = verdict === "wrong" ? (prompt("What is wrong? (optional)") || "") : "";
    const data = await api("review", { run: runId, verdict, target, note });
    toast(verdict === "working" ? "Recorded as working" : "Recorded; diagnosing…");
    if (verdict === "wrong") renderFindings(data.findings);
  });
}

document.querySelectorAll(".review-run button").forEach((button) =>
  button.addEventListener("click", () => review(button, button.dataset.verdict, "run")));

async function diagnose() {
  const data = await api("diagnose", { run: runId });
  renderFindings(data.findings);
  show("generate-section");
}

function renderFindings(findings) {
  const box = $("diagnosis");
  if (!findings.length) {
    box.replaceChildren(el("p", {}, "No failure found in the logs. Describe the problem in the review note so it is kept with the run."));
  } else {
    box.replaceChildren(el("h3", {}, "Diagnosis"), findings.map((f, i) => el("div", { class: "finding " + f.severity },
      el("strong", {}, f.code), " — ", f.cause,
      f.evidence.length ? el("div", { class: "mono" }, f.evidence.join("\n")) : null,
      f.repair ? el("div", { class: "row" }, el("span", {}, "Proposed repair: " + f.repair.description),
        el("button", { onclick: (e) => busy(e.target, () => applyRepair(i)) }, "Apply and rerun")) : el("div", { class: "hint" }, "No automatic repair."))));
  }
  show("diagnosis");
}

async function applyRepair(index) {
  const data = await api("repair", { run: runId, finding: index });
  const history = data.history.map((h) => `v${h.version} ${h.status}`).join(", ");
  toast(data.promoted ? `Repair worked (version ${data.version}). History: ${history}`
                      : `Repair did not improve the result; the previous working version is kept. History: ${history}`);
}

// --- 4. generate -----------------------------------------------------------------------------------
$("generate-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = await api("generate", { run: runId, adapter_id: $("adapter-id").value, moviehub: $("moviehub").value });
    const rows = data.steps.filter((s) => !["analyse", "search", "select", "discover", "verify"].includes(s.step)).map((s) => {
      const { step, status, time, suites, ...rest } = s;
      return el("tr", {}, el("td", {}, step), el("td", {}, badge(status)),
        el("td", {}, suites ? suites.map((x) => `${x.name}: ${x.summary}`).join("; ") : JSON.stringify(rest)));
    });
    $("generate").replaceChildren(
      el("p", {}, data.ok ? "The adapter passed MovieHub's own query and test suite." : "Generation stopped; see the failed step."),
      el("table", {}, el("tr", {}, el("th", {}, "Step"), el("th", {}, "Status"), el("th", {}, "Details")), rows),
      el("p", { class: "hint" }, "Evidence folder: ", el("span", { class: "mono" }, data.dir)),
      el("div", { class: "row" }, ["summary.md", "evidence.jsonl", "suites.txt", "moviehub_result.json"].map((name) =>
        el("a", { href: `/api/run/${runId}?file=${name}`, target: "_blank" }, name))));
    show("generate");
  });
});

// --- batch and permissions --------------------------------------------------------------------------
$("batch-add").addEventListener("click", (e) => busy(e.target, async () => {
  const data = await api("batch/add", { lines: $("batch-lines").value });
  toast(`Queued ${data.added}`);
  $("batch-lines").value = "";
  refresh();
}));
$("batch-start").addEventListener("click", (e) => busy(e.target, async () => {
  const data = await api("batch/start", { concurrency: Number($("batch-concurrency").value) });
  toast(data.started ? "Batch started" : data.reason);
  refresh();
}));
$("permit-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    await api("permit", { domain: $("permit-domain").value, reason: $("permit-reason").value });
    $("permit-domain").value = ""; $("permit-reason").value = "";
    refresh();
  });
});

async function refresh() {
  try {
    const state = await api("state");
    const batch = await api("batch");
    const p = batch.progress;
    $("batch").replaceChildren(
      el("div", {}, `${p.done} done · ${p.failed} failed · ${p.running} running · ${p.pending} pending${batch.running ? " · running" : ""}`),
      el("progress", { max: String(Math.max(1, p.total)), value: String(p.done + p.failed) }),
      batch.items.length ? el("table", {}, el("tr", {}, el("th", {}, "#"), el("th", {}, "URL"), el("th", {}, "Title"), el("th", {}, "Status"), el("th", {}, "Attempts"), el("th", {}, "Last error")),
        batch.items.map((i) => el("tr", {}, el("td", {}, String(i.id)), el("td", {}, i.url), el("td", {}, i.query), el("td", {}, badge(i.status === "done" ? "ok" : i.status)),
          el("td", {}, String(i.attempts)), el("td", { class: "errors" }, (i.last_error || "").slice(0, 160))))) : "");
    $("permissions").replaceChildren(el("table", {}, el("tr", {}, el("th", {}, "Domain"), el("th", {}, "Reason"), el("th", {}, "Rate/s"), el("th", {}, "robots.txt")),
      state.permissions.map((s) => el("tr", {}, el("td", {}, s.domain), el("td", {}, s.reason), el("td", {}, String(s.rate_per_second)), el("td", {}, s.respect_robots ? "honoured" : "ignored")))));
    if (!$("moviehub").value && state.moviehub) $("moviehub").placeholder = "MovieHub checkout: " + state.moviehub;
    if (batch.running) setTimeout(refresh, 3000);
  } catch (error) { toast(error.message); }
}
refresh();
