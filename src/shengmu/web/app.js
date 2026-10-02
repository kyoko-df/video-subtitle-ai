"use strict";
const $ = (id) => document.getElementById(id);
const state = { config: null, engine: "local", media: null, job: null, cues: [], busy: false,
  uploading: false, saving: false, dirty: false, previewUrl: null, xhr: null, uploadSequence: 0, pollTimer: null };
const terminal = new Set(["done", "error", "cancelled"]);
const selectedFormats = () => [...document.querySelectorAll("#format-options input:checked")].map(input => input.value);

function alertMessage(message = "") { $("alert").textContent = message; $("alert").hidden = !message; }
function timeLabel(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(value / 3600), m = Math.floor(value / 60) % 60, s = value % 60;
  return (h ? String(h).padStart(2, "0") + ":" : "") + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}
function updateButtons() {
  const engineReady = state.config && (state.engine === "local" ? state.config.local_engine_installed : state.config.openai_engine_installed && state.config.openai_key_configured);
  $("start").disabled = !state.media || !engineReady || state.busy || state.uploading || state.saving || !selectedFormats().length;
  $("save").disabled = !state.job || state.job.status !== "done" || state.saving || !selectedFormats().length;
  $("dropzone").disabled = state.busy || state.saving;
  $("cancel").hidden = !state.busy;
  for (const input of document.querySelectorAll(".inspector select, .inspector textarea, .inspector input[type=checkbox], .segmented button")) input.disabled = state.busy || state.saving || (input.id === "track" && !state.media);
  for (const input of document.querySelectorAll("#cue-list input, #cue-list textarea, #cue-list button")) input.disabled = state.saving;
}
function switchEngine(engine) {
  state.engine = engine;
  for (const mode of ["local", "openai"]) {
    $(mode + "-mode").classList.toggle("selected", mode === engine);
    $(mode + "-mode").setAttribute("aria-pressed", String(mode === engine));
    $(mode + "-settings").hidden = mode !== engine;
  }
  const installed = state.config && (engine === "local" ? state.config.local_engine_installed : state.config.openai_engine_installed);
  $("engine-note").textContent = engine === "local" ? (installed ? "音频保留在本机，首次运行会下载模型。" : '本地引擎未安装：pip install -e ".[local]"') : (installed ? "音频会发送至配置的 OpenAI 服务，并消耗 API 额度。" : 'API 引擎未安装：pip install -e ".[openai]"');
  updateButtons();
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "X-Session-Token": state.config?.token || "", ...(options.headers || {}) } });
  if (!response.ok) {
    let detail = "请求失败，请检查本地服务。";
    try { const data = await response.json(); detail = typeof data.detail === "string" ? data.detail : "参数无效，请检查字幕时间和内容。"; } catch (_) { /* keep readable fallback */ }
    throw new Error(detail);
  }
  return response.json();
}
function resetResult() {
  clearTimeout(state.pollTimer); state.job = null; state.cues = []; state.dirty = false; state.transcriptDuration = null;
  sessionStorage.removeItem("shengmu-job"); $("cue-list").replaceChildren(); $("cue-count").textContent = "0";
  $("task-progress").hidden = true; $("downloads").hidden = true; $("subtitle-preview").hidden = true;
  renderTimeline(); updateButtons();
}
async function uploadFile(file) {
  if (!file || state.busy || state.saving || !state.config) return;
  if (file.size > state.config.max_upload) { alertMessage("文件超过 GUI 的 2 GB 上限，请使用 CLI 直接处理。"); return; }
  const sequence = ++state.uploadSequence;
  state.xhr?.abort(); state.media = null; state.uploading = true; resetResult(); alertMessage();
  $("file-label").textContent = file.name; $("file-size").textContent = (file.size / 1024 / 1024).toFixed(1) + " MB";
  $("upload-hint").textContent = "正在读取文件…"; $("upload-progress").hidden = false;
  $("upload-progress").firstElementChild.style.width = "0%";
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = URL.createObjectURL(file); $("video").src = state.previewUrl;
  $("video").hidden = false; $("preview-empty").hidden = true; $("workspace-title").textContent = file.name;
  updateButtons();
  try {
    const media = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); state.xhr = xhr;
      xhr.open("POST", "/api/media?name=" + encodeURIComponent(file.name));
      xhr.setRequestHeader("X-Session-Token", state.config.token);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = event => { if (sequence === state.uploadSequence && event.lengthComputable) $("upload-progress").firstElementChild.style.width = (event.loaded / event.total * 100) + "%"; };
      xhr.onload = () => { try { const data = JSON.parse(xhr.responseText); if (xhr.status >= 200 && xhr.status < 300) resolve(data); else reject(new Error(data.detail || "文件读取失败。")); } catch (_) { reject(new Error("服务返回无效响应。")); } };
      xhr.onerror = () => reject(new Error("无法连接本地服务。"));
      xhr.onabort = () => reject(new Error("文件读取已取消。")); xhr.send(file);
    });
    if (sequence !== state.uploadSequence) return;
    state.media = media; $("track").replaceChildren();
    for (const track of media.audio_tracks) {
      const option = document.createElement("option"); option.value = track.index;
      option.textContent = `音轨 ${track.index} · ${track.language} · ${track.channels} 声道${track.title ? " · " + track.title : ""}`;
      $("track").append(option);
    }
    $("duration").textContent = timeLabel(media.duration); $("timeline-end").textContent = timeLabel(media.duration);
    $("upload-hint").textContent = "已读取 · 点击更换文件";
  } catch (error) { if (sequence === state.uploadSequence) { alertMessage(error.message); $("upload-hint").textContent = "点击重新选择文件"; } }
  finally { if (sequence === state.uploadSequence) { state.uploading = false; $("upload-progress").hidden = true; updateButtons(); } }
}
function renderJob(job) {
  state.job = job; state.busy = !terminal.has(job.status);
  $("task-progress").hidden = false; $("task-message").textContent = job.message;
  $("task-percent").textContent = Math.round(job.progress * 100) + "%";
  $("progress-bar").style.width = job.progress * 100 + "%";
  const stageIndex = { queued: -1, probe: 0, extract: 0, transcribe: 1, ready: 2, export: 2, done: 3 }[job.status] ?? -1;
  [...document.querySelectorAll(".steps span")].forEach((step, index) => { step.classList.toggle("active", index === stageIndex); step.classList.toggle("complete", index < stageIndex); });
  $("footer-status").textContent = state.busy ? "正在处理" : job.status === "done" ? "字幕已就绪" : "本地字幕工作台";
  $("downloads").hidden = job.status !== "done";
  $("download-links").replaceChildren();
  for (const file of job.files) {
    const link = document.createElement("a"); link.href = file.url; link.textContent = file.format.toUpperCase() + " ↓"; link.download = file.name; $("download-links").append(link);
  }
  updateButtons();
}
async function pollJob(jobId) {
  try {
    const job = await api("/api/jobs/" + jobId); renderJob(job);
    if (!terminal.has(job.status)) state.pollTimer = setTimeout(() => pollJob(jobId), 900);
    else if (job.status === "done") {
      const transcript = await api("/api/jobs/" + jobId + "/transcript");
      state.cues = transcript.segments; state.dirty = false; state.transcriptDuration = transcript.duration;
      $("workspace-title").textContent = job.source; renderCues(); renderTimeline();
      if (!state.cues.length) alertMessage("未检测到语音。请确认音轨中包含人声，或尝试指定识别语言。");
    } else if (job.status === "error") alertMessage(job.message);
  } catch (error) { state.busy = false; alertMessage(error.message); updateButtons(); }
}
function seek(start) { if (state.previewUrl) { $("video").currentTime = start; $("video").play().catch(() => {}); } }
function markDirty() { state.dirty = true; $("edit-hint").textContent = "有未保存修改。保存后下载文件会同步更新。"; updatePreview(); }
function fitCueText(input) { input.style.height = "auto"; input.style.height = Math.max(33, input.scrollHeight + 2) + "px"; }
function renderCues() {
  $("cue-list").replaceChildren(); $("cue-count").textContent = state.cues.length;
  $("edit-hint").textContent = "生成后可修改文本和时间，点击序号跳转预览。";
  state.cues.forEach((cue, index) => {
    const row = document.createElement("div"); row.className = "cue-row";
    const number = document.createElement("button"); number.className = "cue-index"; number.type = "button";
    number.textContent = String(index + 1).padStart(2, "0"); number.title = "跳转到此字幕"; number.onclick = () => seek(cue.start);
    const timing = document.createElement("div"); timing.className = "cue-time";
    for (const key of ["start", "end"]) {
      const input = document.createElement("input"); input.type = "number"; input.step = "0.001"; input.min = "0";
      input.value = cue[key].toFixed(3); input.setAttribute("aria-label", `第 ${index + 1} 条字幕${key === "start" ? "开始" : "结束"}时间（秒）`);
      input.oninput = () => { cue[key] = input.value === "" ? NaN : Number(input.value); markDirty(); };
      timing.append(input); if (key === "start") { const arrow = document.createElement("span"); arrow.textContent = "–"; timing.append(arrow); }
    }
    const text = document.createElement("textarea"); text.className = "cue-text"; text.rows = 1; text.value = cue.text;
    text.setAttribute("aria-label", `第 ${index + 1} 条字幕内容`); text.oninput = () => { cue.text = text.value; fitCueText(text); markDirty(); };
    const remove = document.createElement("button"); remove.className = "cue-delete"; remove.type = "button"; remove.textContent = "×";
    remove.setAttribute("aria-label", `删除第 ${index + 1} 条字幕`); remove.onclick = () => { state.cues.splice(index, 1); renderCues(); renderTimeline(); markDirty(); };
    row.append(number, timing, text, remove); $("cue-list").append(row); fitCueText(text);
  });
  if (!state.cues.length) { const empty = document.createElement("div"); empty.className = "cues-empty"; empty.textContent = "没有字幕内容"; $("cue-list").append(empty); }
  updatePreview();
}
function renderTimeline() {
  $("timeline-lane").replaceChildren();
  const duration = state.transcriptDuration || state.media?.duration || 0;
  $("timeline-end").textContent = duration ? timeLabel(duration) : "--:--";
  state.cues.forEach((cue, index) => {
    const block = document.createElement("button"); block.className = "timeline-cue"; block.type = "button";
    block.style.left = Math.max(0, cue.start / duration * 100) + "%"; block.style.width = Math.max(.2, (cue.end - cue.start) / duration * 100) + "%";
    block.title = cue.text; block.setAttribute("aria-label", `预览第 ${index + 1} 条字幕`); block.onclick = () => seek(cue.start); $("timeline-lane").append(block);
  });
  updatePreview();
}
function updatePreview() {
  const now = $("video").currentTime;
  const index = state.cues.findIndex(cue => cue.start <= now && cue.end > now);
  $("subtitle-preview").textContent = index >= 0 ? state.cues[index].text : "";
  $("subtitle-preview").hidden = index < 0 || !state.previewUrl;
  [...$("cue-list").children].forEach((row, i) => row.classList.toggle("active", i === index));
  [...$("timeline-lane").children].forEach((block, i) => block.classList.toggle("active", i === index));
}
$("start").onclick = async () => {
  alertMessage(); resetResult(); state.busy = true; updateButtons();
  try {
    const job = await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      media_id: state.media.id, engine: state.engine, model: $("model").value, language: $("language").value || null,
      track: Number($("track").value), device: $("device").value, compute_type: $("compute").value,
      prompt: $("prompt").value, formats: selectedFormats() }) });
    renderJob(job); sessionStorage.setItem("shengmu-job", job.id); pollJob(job.id);
  } catch (error) { state.busy = false; alertMessage(error.message); updateButtons(); }
};
$("cancel").onclick = async () => { if (state.job) { try { renderJob(await api("/api/jobs/" + state.job.id + "/cancel", { method: "POST" })); } catch (error) { alertMessage(error.message); } } };
$("save").onclick = async () => {
  alertMessage();
  let lastEnd = 0;
  for (const cue of state.cues) {
    if (!Number.isFinite(cue.start) || !Number.isFinite(cue.end) || cue.start < lastEnd - .001 || cue.end <= cue.start || cue.end > state.transcriptDuration + .05 || !cue.text.trim()) { alertMessage("请检查字幕：内容不能为空，时间必须按顺序且不重叠，结束时间应在媒体时长内。"); return; }
    lastEnd = cue.end;
  }
  state.saving = true; updateButtons(); $("save").textContent = "正在保存…";
  try {
    renderJob(await api("/api/jobs/" + state.job.id + "/transcript", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ segments: state.cues, formats: selectedFormats() }) }));
    state.dirty = false; $("edit-hint").textContent = "已保存，所有已选格式均已更新。"; renderTimeline();
  } catch (error) { alertMessage(error.message); }
  finally { state.saving = false; $("save").textContent = "保存并导出"; updateButtons(); }
};
$("dropzone").onclick = () => $("file-input").click();
$("file-input").onchange = event => { uploadFile(event.target.files[0]); event.target.value = ""; };
for (const eventName of ["dragover", "dragenter"]) $("dropzone").addEventListener(eventName, event => { event.preventDefault(); if (!state.busy) $("dropzone").classList.add("dragover"); });
for (const eventName of ["dragleave", "drop"]) $("dropzone").addEventListener(eventName, event => { event.preventDefault(); $("dropzone").classList.remove("dragover"); });
$("dropzone").addEventListener("drop", event => uploadFile(event.dataTransfer.files[0]));
$("local-mode").onclick = () => switchEngine("local"); $("openai-mode").onclick = () => switchEngine("openai");
$("device").onchange = () => { $("compute").value = $("device").value === "cuda" ? "float16" : "int8"; };
$("format-options").onchange = () => { if (state.job?.status === "done") $("edit-hint").textContent = "点击保存并导出，应用新的格式选择。"; updateButtons(); };
$("video").addEventListener("timeupdate", updatePreview);
$("video").addEventListener("error", () => { if (state.previewUrl) alertMessage("浏览器无法预览此媒体编码，仍可继续生成字幕。可用支持该编码的播放器打开原文件。"); });
window.addEventListener("beforeunload", event => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
window.addEventListener("resize", () => { for (const input of document.querySelectorAll(".cue-text")) fitCueText(input); });
(async () => {
  try {
    state.config = await api("/api/config");
    $("connection").lastChild.textContent = "本地服务已连接";
    $("key-status").textContent = state.config.openai_key_configured ? "已配置 API Key（服务器环境变量）" : "未配置 API Key。请设置 OPENAI_API_KEY 后重启服务。";
    if (!state.config.ffmpeg || !state.config.ffprobe) alertMessage("未找到 FFmpeg / ffprobe，请安装后重新启动。");
    switchEngine("local");
    const jobId = sessionStorage.getItem("shengmu-job"); if (jobId) pollJob(jobId);
  } catch (error) { $("connection").lastChild.textContent = "服务未连接"; alertMessage(error.message); }
})();
