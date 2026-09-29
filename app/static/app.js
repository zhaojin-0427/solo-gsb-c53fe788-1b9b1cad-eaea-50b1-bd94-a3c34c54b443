// 印前色彩软打样台前端（原生 JavaScript，无框架）
"use strict";

const state = {
  profiles: { source: [], press: [], display: [] },
  job: null,
  view: "preview",
  releases: null,
  selectedGateId: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) msg = j.detail;
    } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

function showError(msg) {
  const box = $("errorBox");
  if (!msg) {
    box.hidden = true;
    box.textContent = "";
  } else {
    box.hidden = false;
    box.textContent = msg;
  }
}

function profileLabel(p) {
  const tag = p.builtin ? "内置" : "自定义";
  return `[${tag}] ${p.name} (${p.color_space})`;
}

function fillSelect(select, items, keepId) {
  select.innerHTML = "";
  for (const p of items) {
    const opt = document.createElement("option");
    opt.value = p.profile_id;
    opt.textContent = profileLabel(p);
    select.appendChild(opt);
  }
  if (items.length && !keepId) {
    // Defaults: built-in first
  }
  if (keepId) select.value = keepId;
}

async function loadProfiles() {
  state.profiles = await api("/api/profiles");
  fillSelect($("sourceProfile"), state.profiles.source);
  fillSelect($("pressProfile"), state.profiles.press);
  fillSelect($("displayProfile"), state.profiles.display);
}

$("useEmbedded").addEventListener("change", (e) => {
  $("sourceProfile").disabled = e.target.checked;
});

$("uploadProfileBtn").addEventListener("click", async () => {
  const role = $("uploadRole").value;
  const file = $("profileFile").files[0];
  const msg = $("profileUploadMsg");
  msg.textContent = "";
  if (!file) {
    msg.textContent = "请先选择 .icc/.icm 文件";
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  try {
    const rec = await api(`/api/profiles/${role}`, { method: "POST", body: fd });
    msg.textContent = `已上传：${rec.name}`;
    await loadProfiles();
    const select = $(`${role}Profile`);
    if (select) select.value = rec.profile_id;
  } catch (err) {
    msg.textContent = "上传被拒绝：" + err.message;
  }
});

$("imageFile").addEventListener("change", () => {
  const f = $("imageFile").files[0];
  $("imageInfo").textContent = f ? `${f.name} (${(f.size / 1024 / 1024).toFixed(2)} MB)` : "";
});

$("runBtn").addEventListener("click", async () => {
  showError("");
  const file = $("imageFile").files[0];
  if (!file) return showError("请先选择图像文件");
  const fd = new FormData();
  fd.append("image", file);
  fd.append("source_profile", $("sourceProfile").value);
  fd.append("press_profile", $("pressProfile").value);
  fd.append("display_profile", $("displayProfile").value);
  fd.append("source_intent", $("sourceIntent").value);
  fd.append("proof_intent", $("proofIntent").value);
  fd.append("threshold", $("threshold").value);
  fd.append("use_embedded", $("useEmbedded").checked ? "true" : "false");

  const btn = $("runBtn");
  btn.disabled = true;
  $("busy").hidden = false;
  try {
    const job = await api("/api/jobs", { method: "POST", body: fd });
    state.job = job;
    renderJob();
    loadReleases().catch(() => {});
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    $("busy").hidden = true;
  }
});

function renderJob() {
  const job = state.job;
  $("resultsPanel").hidden = false;
  const s = job.stats;
  const pct = (s.over_ratio * 100).toFixed(3);
  $("statsBar").innerHTML =
    `<span>均值 ΔE00 <b>${s.mean.toFixed(2)}</b></span>` +
    `<span>P95 <b>${s.p95.toFixed(2)}</b></span>` +
    `<span>最大 <b>${s.max.toFixed(2)}</b></span>` +
    `<span>阈值 ${s.threshold} 超阈面积 <b>${pct}%</b></span>` +
    `<span>超阈像素 ${s.over_pixels.toLocaleString()} / ${s.total_pixels.toLocaleString()}</span>` +
    `<span>任务 <code>${job.job_id.slice(0, 12)}…</code></span>`;

  $("reportJson").textContent = JSON.stringify(job.report, null, 2);
  setView("preview");

  const base = `/api/jobs/${job.job_id}`;
  $("expPreview").href = `${base}/images/preview.png`;
  $("expProof").href = `${base}/images/softproof.png`;
  $("expHeat").href = `${base}/images/heatmap.png`;
  $("expMask").href = `${base}/images/mask.png`;
  $("expReport").href = `${base}/report`;
  $("expZip").href = `${base}/export.zip`;
  for (const a of ["expPreview", "expProof", "expHeat", "expMask"]) $(a).download = $(a).href.split("/").pop();
  $("expReport").download = `${job.job_id.slice(0, 12)}-report.json`;
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => setView(tab.dataset.view));
});

function setView(view) {
  state.view = view;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  const job = state.job;
  if (!job) return;
  const map = {
    preview: job.images.preview,
    softproof: job.images.softproof,
    heatmap: job.images.heatmap,
    overlay: job.images.overlay,
    mask: job.images.mask,
  };
  $("viewImage").src = map[view];
}

// ---- ROI selection -------------------------------------------------------
const img = $("viewImage"), canvas = $("roiCanvas");
let drag = null;

function roiRect() {
  const wrap = $("canvasWrap");
  const wRect = wrap.getBoundingClientRect();
  const r = img.getBoundingClientRect();
  canvas.style.position = "absolute";
  canvas.style.left = (r.left - wRect.left + wrap.scrollLeft) + "px";
  canvas.style.top = (r.top - wRect.top + wrap.scrollTop) + "px";
  canvas.style.width = r.width + "px";
  canvas.style.height = r.height + "px";
  canvas.width = Math.round(r.width);
  canvas.height = Math.round(r.height);
}
img.addEventListener("load", roiRect);
window.addEventListener("resize", roiRect);

function imgCoords(ev) {
  const r = img.getBoundingClientRect();
  const sx = img.naturalWidth / r.width;
  const sy = img.naturalHeight / r.height;
  return {
    x: Math.round((ev.clientX - r.left) * sx),
    y: Math.round((ev.clientY - r.top) * sy),
    sx, sy, r,
  };
}

canvas.addEventListener("mousedown", (ev) => {
  if (!state.job) return;
  const c = imgCoords(ev);
  drag = { x0: c.x, y0: c.y, c };
});
window.addEventListener("mousemove", (ev) => {
  if (!drag) return;
  const c = imgCoords(ev);
  drawRect(drag.x0, drag.y0, c.x, c.y);
});
window.addEventListener("mouseup", async (ev) => {
  if (!drag) return;
  const c = imgCoords(ev);
  const x = Math.max(0, Math.min(drag.x0, c.x));
  const y = Math.max(0, Math.min(drag.y0, c.y));
  const w = Math.abs(c.x - drag.x0);
  const h = Math.abs(c.y - drag.y0);
  drag = null;
  if (w < 3 || h < 3) return;
  await queryRegion(x, y, w, h);
});

function drawRect(x0d, y0d, x1d, y1d) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const { sx, sy } = drag.c;
  const r = {
    x: Math.min(x0d, x1d) / sx,
    y: Math.min(y0d, y1d) / sy,
    w: Math.abs(x1d - x0d) / sx,
    h: Math.abs(y1d - y0d) / sy,
  };
  ctx.fillStyle = "rgba(58,110,165,.18)";
  ctx.strokeStyle = "#ffdd33";
  ctx.lineWidth = 1.5;
  ctx.fillRect(r.x, r.y, r.w, r.h);
  ctx.strokeRect(r.x, r.y, r.w, r.h);
}

async function queryRegion(x, y, w, h) {
  try {
    const data = await api(`/api/jobs/${state.job.job_id}/region`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y, width: w, height: h }),
    });
    $("regionPanel").hidden = false;
    $("regionData").textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    $("regionPanel").hidden = false;
    $("regionData").textContent = "区域查询失败：" + err.message;
  }
}


// ---------------------------------------------------------------------------
// Press-profile drafts, regression gates and releases
// ---------------------------------------------------------------------------

async function apiRaw(path, options) {
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const err = new Error(data && data.detail ? data.detail : `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function showReleaseError(msg) {
  const box = $("releaseError");
  box.hidden = !msg;
  box.textContent = msg || "";
}

function shortSha(sha) {
  return sha ? `${sha.slice(0, 12)}…` : "—";
}

function shortId(id) {
  return id ? `${id.slice(0, 10)}…` : "—";
}

function statusLabel(status) {
  return {
    active: "当前",
    superseded: "已替代",
    withdrawn: "已撤回",
    draft: "待门禁",
    passed: "门禁通过",
    failed: "门禁失败",
    published: "已发布",
  }[status] || status;
}

async function loadReleases() {
  state.releases = await apiRaw("/api/releases/overview");
  renderReleases();
}

function currentFamily() {
  const fid = $("releaseFamilySelect").value;
  return state.releases.families.find((f) => f.family_id === fid) || null;
}

function renderReleases() {
  const data = state.releases;
  if (!data) return;

  const familySelect = $("releaseFamilySelect");
  const wantedFamily = familySelect.value;
  familySelect.innerHTML = '<option value="">＋ 新建配置族</option>';
  for (const f of data.families) {
    const opt = document.createElement("option");
    opt.value = f.family_id;
    opt.textContent = `${f.name}${f.current_version ? `（当前 v${f.current_version}）` : "（无当前版本）"}`;
    familySelect.appendChild(opt);
  }
  familySelect.value = wantedFamily;

  const jobs = data.jobs || [];
  const jobSelect = $("baselineJobSelect");
  const wantedJob = jobSelect.value;
  jobSelect.innerHTML = "";
  for (const job of jobs) {
    const opt = document.createElement("option");
    opt.value = job.job_id;
    opt.textContent = `${shortId(job.job_id)} · ${job.width}×${job.height} · ${job.press_profile} · ΔE均值 ${Number(job.de_mean).toFixed(2)}`;
    jobSelect.appendChild(opt);
  }
  if (wantedJob) jobSelect.value = wantedJob;
  if (!jobSelect.value && state.job) jobSelect.value = state.job.job_id;

  const fid = familySelect.value;
  const family = data.families.find((f) => f.family_id === fid);
  const familyVersions = fid ? data.versions.filter((v) => v.family_id === fid) : data.versions;
  renderVersions(family, familyVersions);
  renderDrafts(fid);
  renderAudits(fid);
  renderGate();
}

function renderVersions(family, versions) {
  const summary = $("versionSummary");
  if (!family) {
    summary.textContent = "选择已有配置族，或填写名称后上传第一个草稿。";
  } else if (family.current_version) {
    summary.innerHTML = `<b>${family.name}</b> 当前版本：<b>v${family.current_version}</b>`;
  } else {
    summary.innerHTML = `<b>${family.name}</b>：当前无 active 版本（可恢复撤回版本）`;
  }

  const body = $("versionsBody");
  body.innerHTML = "";
  if (!versions.length) {
    body.innerHTML = '<tr><td colspan="4" class="muted">暂无版本</td></tr>';
    return;
  }
  for (const v of versions.slice(0, 20)) {
    const tr = document.createElement("tr");
    const actions = document.createElement("div");
    actions.className = "mini-actions";

    const reportLink = document.createElement("a");
    reportLink.className = "btn";
    reportLink.textContent = "报告";
    reportLink.href = v.gate_report;
    reportLink.target = "_blank";
    actions.appendChild(reportLink);

    const iccLink = document.createElement("a");
    iccLink.className = "btn";
    iccLink.textContent = "ICC";
    iccLink.href = v.snapshot;
    iccLink.download = "";
    actions.appendChild(iccLink);

    if (v.status === "active") {
      const btn = document.createElement("button");
      btn.textContent = "撤回";
      btn.onclick = () => {
        const f = state.releases.families.find((item) => item.family_id === v.family_id);
        versionAction(v, "withdraw", f ? f.current_version : 0);
      };
      actions.appendChild(btn);
    } else if (v.status === "withdrawn") {
      const btn = document.createElement("button");
      btn.textContent = "恢复";
      btn.className = "primary";
      btn.onclick = () => versionAction(v, "restore", familyVersion(v.family_id));
      actions.appendChild(btn);
    } else if (v.status === "superseded") {
      const btn = document.createElement("button");
      btn.textContent = "回滚";
      btn.className = "primary";
      btn.onclick = () => versionAction(v, "rollback", familyVersion(v.family_id));
      actions.appendChild(btn);
    }

    tr.innerHTML = `<td><code>v${v.version_number}</code><br><span class="muted">${familyName(v.family_id)}</span></td>
      <td>${statusLabel(v.status)}</td><td><code title="${v.profile_sha256}">${shortSha(v.profile_sha256)}</code></td><td></td>`;
    tr.lastElementChild.appendChild(actions);
    body.appendChild(tr);
  }
}

function familyVersion(fid) {
  const f = state.releases.families.find((item) => item.family_id === fid);
  return f ? (f.current_version || 0) : 0;
}

function familyName(fid) {
  const f = state.releases.families.find((x) => x.family_id === fid);
  return f ? f.name : shortId(fid);
}

function renderDrafts(fid) {
  const body = $("draftsBody");
  body.innerHTML = "";
  const drafts = state.releases.drafts.filter((d) => !fid || d.family_id === fid).slice(0, 30);
  if (!drafts.length) {
    body.innerHTML = '<tr><td colspan="5" class="muted">暂无草稿。先运行代表性任务，再上传候选印刷 ICC。</td></tr>';
    return;
  }
  for (const d of drafts) {
    const run = state.releases.gate_runs.find((r) => r.run_id === d.latest_run_id);
    const tr = document.createElement("tr");
    const actions = document.createElement("div");
    actions.className = "mini-actions";

    const gateBtn = document.createElement("button");
    gateBtn.textContent = "重跑门禁";
    gateBtn.onclick = () => runGate(d.draft_id);
    actions.appendChild(gateBtn);

    const viewBtn = document.createElement("button");
    viewBtn.textContent = "查看结果";
    viewBtn.disabled = !run;
    viewBtn.onclick = () => { state.selectedGateId = run.run_id; renderGate(); };
    actions.appendChild(viewBtn);

    if (d.status === "passed" || (run && run.passed)) {
      const pub = document.createElement("button");
      pub.textContent = "发布";
      pub.className = "primary";
      const family = state.releases.families.find((f) => f.family_id === d.family_id);
      pub.onclick = () => publishDraft(d, family.current_version || 0);
      actions.appendChild(pub);
    }

    const gateText = run ? (run.passed ? "通过" : "失败") : "未运行";
    tr.innerHTML = `<td>${familyName(d.family_id)}<br><code title="${d.candidate_sha256}">${shortSha(d.candidate_sha256)}</code></td>
      <td><code>${shortId(d.draft_id)}</code><br><span class="muted">${d.candidate_name}</span></td>
      <td>${statusLabel(d.status)}</td><td>${gateText}</td><td></td>`;
    tr.lastElementChild.appendChild(actions);
    body.appendChild(tr);
  }
}

function renderAudits(fid) {
  const body = $("auditsBody");
  body.innerHTML = "";
  const rows = state.releases.audits.filter((a) => !fid || a.family_id === fid).slice(0, 50);
  for (const a of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${a.created_at}</td><td>${auditAction(a.action)}</td>
      <td>v${a.expected_version ?? "—"} / v${a.observed_version ?? "—"}</td>
      <td class="${a.result === "allowed" ? "pass" : "fail"}">${a.result === "allowed" ? "允许" : "拒绝"}</td>
      <td>${a.reason || ""}</td>`;
    body.appendChild(tr);
  }
}

function auditAction(action) {
  return { publish: "发布", withdraw: "撤回", restore: "恢复", rollback: "回滚" }[action] || action;
}

function renderGate() {
  const runs = state.releases ? state.releases.gate_runs : [];
  let run = runs.find((r) => r.run_id === state.selectedGateId);
  if (!run && runs.length) run = runs[0];
  if (!run) {
    $("gateDetail").hidden = true;
    return;
  }
  panel.hidden = false;
  $("gateTitle").textContent = `门禁结果：${run.passed ? "全部通过" : "存在失败"}（${shortId(run.run_id)}）`;
  $("gateReportLink").href = run.report;
  $("gateChangesImg").src = run.images.changes;
  $("gateCandidateMask").src = run.images.candidate_mask;
  $("gateBaselineMask").src = run.images.baseline_mask;
  $("gateJson").textContent = JSON.stringify(run, null, 2);

  $("gateChecks").innerHTML = run.checks.map((c) => `
    <div class="check ${c.passed ? "pass" : "fail"}">
      <b>${c.passed ? "✓" : "✕"} ${c.name}</b>
      <span>候选 ${c.candidate} / 基线 ${c.baseline} / 上限 ${c.limit}</span>
      <p>${c.reason}</p>
    </div>`).join("");
}

$("releaseFamilySelect").addEventListener("change", () => {
  $("releaseFamilyName").disabled = Boolean($("releaseFamilySelect").value);
  renderReleases();
});

$("createDraftBtn").addEventListener("click", async () => {
  showReleaseError("");
  const file = $("releaseProfileFile").files[0];
  if (!file) return showReleaseError("请选择候选印刷 ICC 配置");
  const jobId = $("baselineJobSelect").value;
  if (!jobId) return showReleaseError("请先在上方生成并保存一个代表性任务作为基线");
  const fd = new FormData();
  fd.append("file", file);
  fd.append("family_id", $("releaseFamilySelect").value);
  fd.append("family_name", $("releaseFamilyName").value);
  fd.append("baseline_job_id", jobId);
  fd.append("max_de_mean", $("limitMean").value);
  fd.append("max_de_p95", $("limitP95").value);
  fd.append("max_over_threshold_ratio", $("limitOverRatio").value);
  fd.append("max_mask_changed_pixels", $("limitChanged").value);
  const btn = $("createDraftBtn");
  btn.disabled = true;
  $("releaseMsg").textContent = "正在上传并固定图像、源/显示配置和参数…";
  try {
    const draft = await apiRaw("/api/releases/drafts", { method: "POST", body: fd });
    $("releaseMsg").textContent = "草稿已保存，开始按 SHA-256 快照重跑…";
    const run = await apiRaw(`/api/releases/drafts/${draft.draft_id}/gate`, { method: "POST" });
    state.selectedGateId = run.run_id;
    await loadReleases();
    $("releaseMsg").textContent = run.passed ? "门禁全部通过，可以发布。" : "门禁失败，草稿已保留。";
  } catch (err) {
    showReleaseError(err.message);
    $("releaseMsg").textContent = "";
  } finally {
    btn.disabled = false;
  }
});

async function runGate(draftId) {
  showReleaseError("");
  try {
    const run = await apiRaw(`/api/releases/drafts/${draftId}/gate`, { method: "POST" });
    state.selectedGateId = run.run_id;
    await loadReleases();
  } catch (err) { showReleaseError(err.message); }
}

function askExpectedVersion(current) {
  const value = window.prompt("请输入期望的当前版本号，用于乐观并发控制：", String(current ?? 0));
  if (value === null) return null;
  const n = Number(value);
  return Number.isInteger(n) && n >= 0 ? n : null;
}

async function publishDraft(draft, current) {
  const expected = askExpectedVersion(current);
  if (expected === null) return;
  try {
    await apiRaw(`/api/releases/drafts/${draft.draft_id}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_version: expected }),
    });
    await loadReleases();
  } catch (err) { alert("发布失败：" + err.message); }
}

async function versionAction(version, action, current) {
  const expected = askExpectedVersion(current);
  if (expected === null) return;
  try {
    await apiRaw(`/api/releases/versions/${version.version_id}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_version: expected }),
    });
    await loadReleases();
  } catch (err) { alert(`${action === "withdraw" ? "撤回" : action === "restore" ? "恢复" : "回滚"}失败：` + err.message); }
}

Promise.all([
  loadProfiles().catch((e) => showError("加载配置列表失败: " + e.message)),
  loadReleases().catch((e) => showReleaseError("加载发布模块失败: " + e.message)),
]);

setInterval(() => loadReleases().catch(() => {}), 15000);
