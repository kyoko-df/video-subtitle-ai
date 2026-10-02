"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  config: null,
  engine: "local",
  media: null,
  job: null,
  cues: [],
  rows: [],
  blocks: [],
  history: [],
  playback: [],
  playbackPosition: -1,
  activeIndex: -1,
  busy: false,
  uploading: false,
  saving: false,
  dirty: false,
  previewUrl: null,
  xhr: null,
  uploadSequence: 0,
  pollTimer: null,
  transcriptDuration: null,
  resultSequence: 0,
  pendingMedia: new Set(),
};
const terminal = new Set(["done", "error", "cancelled"]);
const selectedFormats = () =>
  [...document.querySelectorAll("#format-options input:checked")].map(
    (input) => input.value,
  );

function alertMessage(message = "") {
  $("alert").textContent = message;
  $("alert").hidden = !message;
}

function timeLabel(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(value / 3600);
  const m = Math.floor(value / 60) % 60;
  const s = value % 60;
  return (
    (h ? String(h).padStart(2, "0") + ":" : "") +
    String(m).padStart(2, "0") +
    ":" +
    String(s).padStart(2, "0")
  );
}

function updateButtons() {
  const engineReady =
    state.config &&
    (state.engine === "local"
      ? state.config.local_engine_installed
      : state.config.openai_engine_installed &&
        state.config.openai_key_configured);
  const editing =
    state.job?.status === "done" &&
    state.transcriptDuration !== null &&
    !state.saving &&
    !state.busy;
  $("start").disabled =
    !state.media ||
    !engineReady ||
    state.busy ||
    state.uploading ||
    state.saving ||
    !selectedFormats().length;
  $("save").disabled = !editing || !selectedFormats().length;
  $("add-cue").disabled = !editing;
  $("undo").disabled = !editing || !state.history.length;
  $("release-source").disabled =
    !state.media || state.busy || state.saving || state.uploading;
  $("dropzone").disabled = state.busy || state.saving;
  $("cancel").hidden = !state.busy;
  for (const input of document.querySelectorAll(
    ".inspector select, .inspector textarea, .inspector input[type=checkbox], .segmented button",
  )) {
    input.disabled =
      state.busy || state.saving || (input.id === "track" && !state.media);
  }
  for (const input of document.querySelectorAll(
    "#cue-list input, #cue-list textarea, #cue-list button",
  )) {
    input.disabled = state.saving;
  }
}

function switchEngine(engine) {
  state.engine = engine;
  for (const mode of ["local", "openai"]) {
    $(mode + "-mode").classList.toggle("selected", mode === engine);
    $(mode + "-mode").setAttribute("aria-pressed", String(mode === engine));
    $(mode + "-settings").hidden = mode !== engine;
  }
  const installed =
    state.config &&
    (engine === "local"
      ? state.config.local_engine_installed
      : state.config.openai_engine_installed);
  $("engine-note").textContent =
    engine === "local"
      ? installed
        ? "音频保留在本机，首次运行会下载模型。"
        : '本地引擎未安装：pip install -e ".[local]"'
      : installed
        ? "音频会发送至配置的 OpenAI 服务，并消耗 API 额度。"
        : 'API 引擎未安装：pip install -e ".[openai]"';
  updateButtons();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-Session-Token": state.config?.token || "",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = "请求失败，请检查本地服务。";
    try {
      const data = await response.json();
      detail =
        typeof data.detail === "string"
          ? data.detail
          : "参数无效，请检查字幕时间和内容。";
    } catch (_) {
      /* keep readable fallback */
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function releaseMedia(id) {
  if (!id) return;
  state.pendingMedia.add(id);
  try {
    await api("/api/media/" + id, { method: "DELETE" });
  } catch (error) {
    if (error.status !== 404) throw error;
  }
  state.pendingMedia.delete(id);
  if (sessionStorage.getItem("shengmu-media") === id)
    sessionStorage.removeItem("shengmu-media");
}

function resetResult() {
  state.resultSequence += 1;
  clearTimeout(state.pollTimer);
  state.job = null;
  state.cues = [];
  state.history = [];
  state.dirty = false;
  state.transcriptDuration = null;
  sessionStorage.removeItem("shengmu-job");
  $("task-progress").hidden = true;
  $("downloads").hidden = true;
  $("subtitle-preview").hidden = true;
  renderCues();
  renderTimeline();
  updateButtons();
}

function uploadRequest(file, sequence) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    state.xhr = xhr;
    xhr.open("POST", "/api/media?name=" + encodeURIComponent(file.name));
    xhr.setRequestHeader("X-Session-Token", state.config.token);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (sequence === state.uploadSequence && event.lengthComputable) {
        $("upload-progress").firstElementChild.style.width =
          (event.loaded / event.total) * 100 + "%";
      }
    };
    xhr.onload = () => {
      try {
        const data = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.detail || "文件读取失败。"));
      } catch (_) {
        reject(new Error("服务返回无效响应。"));
      }
    };
    xhr.onerror = () => reject(new Error("无法连接本地服务。"));
    xhr.onabort = () => reject(new Error("文件读取已取消。"));
    xhr.send(file);
  });
}

async function uploadFile(file) {
  if (!file || state.busy || state.saving || !state.config) return;
  if (file.size > state.config.max_upload) {
    alertMessage("文件超过 GUI 上传上限，请使用 CLI 直接处理。");
    return;
  }
  if (state.dirty && !window.confirm("放弃未保存的字幕修改并更换文件？"))
    return;
  const sequence = ++state.uploadSequence;
  state.xhr?.abort();
  const previousId = state.media?.id || sessionStorage.getItem("shengmu-media");
  if (previousId) state.pendingMedia.add(previousId);
  state.media = null;
  state.uploading = true;
  resetResult();
  alertMessage();
  $("file-label").textContent = file.name;
  $("file-size").textContent = (file.size / 1024 / 1024).toFixed(1) + " MB";
  $("upload-hint").textContent = "正在读取文件…";
  $("upload-progress").hidden = false;
  $("upload-progress").firstElementChild.style.width = "0%";
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = URL.createObjectURL(file);
  $("video").src = state.previewUrl;
  $("video").hidden = false;
  $("preview-empty").hidden = true;
  $("workspace-title").textContent = file.name;
  updateButtons();
  try {
    for (const id of [...state.pendingMedia]) await releaseMedia(id);
    if (sequence !== state.uploadSequence) return;
    const media = await uploadRequest(file, sequence);
    if (sequence !== state.uploadSequence) {
      await releaseMedia(media.id);
      return;
    }
    state.media = media;
    sessionStorage.setItem("shengmu-media", media.id);
    $("track").replaceChildren();
    for (const track of media.audio_tracks) {
      const option = document.createElement("option");
      option.value = track.index;
      option.textContent =
        `音轨 ${track.index} · ${track.language} · ${track.channels} 声道` +
        (track.title ? " · " + track.title : "");
      $("track").append(option);
    }
    $("duration").textContent = timeLabel(media.duration);
    $("timeline-end").textContent = timeLabel(media.duration);
    $("upload-hint").textContent = "已读取 · 点击更换文件";
  } catch (error) {
    if (sequence === state.uploadSequence) {
      alertMessage(error.message);
      $("upload-hint").textContent = "点击重新选择文件";
    }
  } finally {
    if (sequence === state.uploadSequence) {
      state.uploading = false;
      $("upload-progress").hidden = true;
      updateButtons();
    }
  }
}

function renderJob(job) {
  state.job = job;
  state.busy = !terminal.has(job.status);
  $("task-progress").hidden = false;
  $("task-message").textContent = job.message;
  $("task-percent").textContent = Math.round(job.progress * 100) + "%";
  $("progress-bar").style.width = job.progress * 100 + "%";
  const stageIndex =
    {
      queued: -1,
      probe: 0,
      extract: 0,
      transcribe: 1,
      ready: 2,
      export: 2,
      done: 3,
    }[job.status] ?? -1;
  [...document.querySelectorAll(".steps span")].forEach((step, index) => {
    step.classList.toggle("active", index === stageIndex);
    step.classList.toggle("complete", index < stageIndex);
  });
  $("footer-status").textContent = state.busy
    ? "正在处理"
    : job.status === "done"
      ? "字幕已就绪"
      : "本地字幕工作台";
  $("downloads").hidden = job.status !== "done";
  $("download-links").replaceChildren();
  for (const file of job.files) {
    const link = document.createElement("a");
    link.href = file.url;
    link.textContent = file.format.toUpperCase() + " ↓";
    link.download = file.name;
    $("download-links").append(link);
  }
  updateButtons();
}

async function pollJob(jobId, sequence = state.resultSequence) {
  try {
    const job = await api("/api/jobs/" + jobId);
    if (
      sequence !== state.resultSequence ||
      (state.job && state.job.id !== jobId)
    )
      return;
    renderJob(job);
    if (!terminal.has(job.status)) {
      state.pollTimer = setTimeout(() => pollJob(jobId, sequence), 900);
    } else if (job.status === "done") {
      const transcript = await api("/api/jobs/" + jobId + "/transcript");
      if (sequence !== state.resultSequence || state.job?.id !== jobId) return;
      state.cues = transcript.segments;
      state.history = [];
      state.dirty = false;
      state.transcriptDuration = transcript.duration;
      $("workspace-title").textContent = job.source;
      renderCues();
      renderTimeline();
      updateButtons();
      if (!state.cues.length)
        alertMessage(
          "未检测到语音。请确认音轨中包含人声，或尝试指定识别语言。",
        );
    } else if (job.status === "error") {
      alertMessage(job.message);
    }
  } catch (error) {
    if (sequence !== state.resultSequence) return;
    state.busy = false;
    alertMessage(error.message);
    updateButtons();
  }
}

function seek(start) {
  if (!state.previewUrl || !Number.isFinite(start)) return;
  $("video").currentTime = start;
  $("video")
    .play()
    .catch(() => {});
}

function markDirty() {
  state.dirty = true;
  $("edit-hint").textContent = "有未保存修改。保存后下载文件会同步更新。";
  updatePreview();
}

function fitCueTexts(inputs) {
  for (const input of inputs) input.style.height = "auto";
  const heights = inputs.map((input) => Math.max(33, input.scrollHeight + 2));
  inputs.forEach((input, index) => {
    input.style.height = heights[index] + "px";
  });
}

function recordEdit(edit) {
  state.history.push(edit);
  if (state.history.length > 100) state.history.shift();
  $("undo").disabled = state.saving || !state.history.length;
}

function rebuildPlayback() {
  state.playback = state.cues
    .map((cue, index) => ({ ...cue, index }))
    .filter(
      (cue) =>
        Number.isFinite(cue.start) &&
        Number.isFinite(cue.end) &&
        cue.end > cue.start,
    )
    .sort((a, b) => a.start - b.start);
  state.playbackPosition = -1;
}

function updateCue(index, key, value) {
  state.cues[index] = { ...state.cues[index], [key]: value };
  updateTimelineBlock(state.blocks[index], index);
  if (key !== "text") rebuildPlayback();
  markDirty();
}

function createCueRow(index) {
  const row = document.createElement("div");
  row.className = "cue-row";
  row.dataset.index = index;
  const currentIndex = () => Number(row.dataset.index);
  const number = document.createElement("button");
  number.className = "cue-index";
  number.type = "button";
  number.title = "跳转到此字幕";
  number.onclick = () => seek(state.cues[currentIndex()].start);
  const timing = document.createElement("div");
  timing.className = "cue-time";
  for (const key of ["start", "end"]) {
    const input = document.createElement("input");
    input.type = "number";
    input.step = "0.001";
    input.min = "0";
    input.dataset.key = key;
    input.value = state.cues[index][key].toFixed(3);
    bindCueInput(input, currentIndex, key);
    timing.append(input);
    if (key === "start") {
      const arrow = document.createElement("span");
      arrow.textContent = "–";
      timing.append(arrow);
    }
  }
  const text = document.createElement("textarea");
  text.className = "cue-text";
  text.rows = 1;
  text.value = state.cues[index].text;
  bindCueInput(text, currentIndex, "text");
  const actions = document.createElement("div");
  actions.className = "cue-actions";
  const operations = [
    [
      "拆",
      "拆分",
      () =>
        CueEditor.splitCue(
          state.cues[currentIndex()],
          text.selectionStart || Math.floor(text.value.length / 2),
          $("video").currentTime,
        ),
      1,
    ],
    [
      "并",
      "合并下一条",
      () => [
        CueEditor.mergeCues(
          state.cues[currentIndex()],
          state.cues[currentIndex() + 1],
        ),
      ],
      2,
    ],
    ["×", "删除", () => [], 1],
  ];
  for (const [label, title, operation, count] of operations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cue-delete";
    button.textContent = label;
    button.dataset.action = title;
    button.onclick = () => {
      try {
        const index = currentIndex();
        const inserted = operation();
        applyEditorEdit({
          index,
          removed: state.cues.slice(index, index + count),
          inserted,
        });
      } catch (error) {
        alertMessage(error.message);
      }
    };
    actions.append(button);
  }
  row.append(number, timing, text, actions);
  return row;
}

function bindCueInput(input, currentIndex, key) {
  let original;
  input.onfocus = () => {
    original = { ...state.cues[currentIndex()] };
  };
  input.oninput = () => {
    if (!original) original = { ...state.cues[currentIndex()] };
    const value =
      key === "text"
        ? input.value
        : input.value === ""
          ? NaN
          : Number(input.value);
    updateCue(currentIndex(), key, value);
    if (key === "text") requestAnimationFrame(() => fitCueTexts([input]));
  };
  input.onchange = () => {
    const index = currentIndex();
    if (original && !Object.is(original[key], state.cues[index][key])) {
      recordEdit({
        index,
        removed: [original],
        inserted: [{ ...state.cues[index] }],
      });
    }
    original = { ...state.cues[index] };
  };
}

function labelRows(start = 0, end = state.rows.length) {
  for (let index = start; index < end; index += 1) {
    const row = state.rows[index];
    row.dataset.index = index;
    row.querySelector(".cue-index").textContent = String(index + 1).padStart(
      2,
      "0",
    );
    for (const input of row.querySelectorAll("input")) {
      input.setAttribute(
        "aria-label",
        `第 ${index + 1} 条字幕${input.dataset.key === "start" ? "开始" : "结束"}时间（秒）`,
      );
    }
    row
      .querySelector("textarea")
      .setAttribute("aria-label", `第 ${index + 1} 条字幕内容`);
    for (const button of row.querySelectorAll(".cue-actions button")) {
      button.setAttribute(
        "aria-label",
        `${button.dataset.action}第 ${index + 1} 条字幕`,
      );
    }
    state.blocks[index]?.setAttribute(
      "aria-label",
      `预览第 ${index + 1} 条字幕`,
    );
    if (state.blocks[index]) state.blocks[index].dataset.index = index;
  }
  $("cue-count").textContent = state.cues.length;
}

function renderCues() {
  const fragment = document.createDocumentFragment();
  state.rows = state.cues.map((_, index) => createCueRow(index));
  for (const row of state.rows) fragment.append(row);
  $("cue-list").replaceChildren(fragment);
  labelRows();
  showEmptyCues();
  fitCueTexts(state.rows.map((row) => row.querySelector("textarea")));
  $("edit-hint").textContent =
    "可新增、拆分、合并和撤销；拆分使用文字光标和当前播放位置。";
  state.activeIndex = -1;
  rebuildPlayback();
  updatePreview();
}

function showEmptyCues() {
  $("cue-list").querySelector(".cues-empty")?.remove();
  if (!state.cues.length) {
    const empty = document.createElement("div");
    empty.className = "cues-empty";
    empty.textContent = "没有字幕内容";
    $("cue-list").append(empty);
  }
}

function createTimelineBlock(index) {
  const block = document.createElement("button");
  block.className = "timeline-cue";
  block.type = "button";
  block.dataset.index = index;
  block.onclick = () => seek(state.cues[Number(block.dataset.index)].start);
  updateTimelineBlock(block, index);
  return block;
}

function updateTimelineBlock(block, index) {
  if (!block) return;
  const cue = state.cues[index];
  const duration = state.transcriptDuration || state.media?.duration || 1;
  const valid =
    Number.isFinite(cue.start) &&
    Number.isFinite(cue.end) &&
    cue.end > cue.start;
  block.hidden = !valid;
  block.style.left = Math.max(0, (cue.start / duration) * 100) + "%";
  block.style.width =
    Math.max(0.2, ((cue.end - cue.start) / duration) * 100) + "%";
  block.title = cue.text;
  block.setAttribute("aria-label", `预览第 ${index + 1} 条字幕`);
}

function renderTimeline() {
  const fragment = document.createDocumentFragment();
  state.blocks = state.cues.map((_, index) => createTimelineBlock(index));
  for (const block of state.blocks) fragment.append(block);
  $("timeline-lane").replaceChildren(fragment);
  const duration = state.transcriptDuration || state.media?.duration || 0;
  $("timeline-end").textContent = duration ? timeLabel(duration) : "--:--";
  updatePreview();
  if (state.activeIndex >= 0)
    state.blocks[state.activeIndex]?.classList.add("active");
}

function applyEditorEdit(edit, record = true) {
  if (state.saving) return;
  if (record) recordEdit(edit);
  state.rows[state.activeIndex]?.classList.remove("active");
  state.blocks[state.activeIndex]?.classList.remove("active");
  state.activeIndex = -1;
  state.cues = CueEditor.applyEdit(state.cues, edit);
  const newRows = edit.inserted.map((_, i) => createCueRow(edit.index + i));
  const newBlocks = edit.inserted.map((_, i) =>
    createTimelineBlock(edit.index + i),
  );
  const oldRows = state.rows.splice(
    edit.index,
    edit.removed.length,
    ...newRows,
  );
  const oldBlocks = state.blocks.splice(
    edit.index,
    edit.removed.length,
    ...newBlocks,
  );
  for (const node of [...oldRows, ...oldBlocks]) node.remove();
  for (const [parent, nodes, all] of [
    [$("cue-list"), newRows, state.rows],
    [$("timeline-lane"), newBlocks, state.blocks],
  ]) {
    const fragment = document.createDocumentFragment();
    for (const node of nodes) fragment.append(node);
    parent.insertBefore(fragment, all[edit.index + nodes.length] || null);
  }
  labelRows(
    edit.index,
    edit.removed.length === newRows.length
      ? edit.index + newRows.length
      : state.rows.length,
  );
  showEmptyCues();
  fitCueTexts(newRows.map((row) => row.querySelector("textarea")));
  rebuildPlayback();
  markDirty();
  $("undo").disabled = !state.history.length;
}

function updatePreview() {
  const position = CueEditor.activeIndex(
    state.playback,
    $("video").currentTime,
    state.playbackPosition,
  );
  state.playbackPosition = position;
  const index = position >= 0 ? state.playback[position].index : -1;
  const text = index >= 0 ? state.cues[index].text : "";
  if ($("subtitle-preview").textContent !== text)
    $("subtitle-preview").textContent = text;
  $("subtitle-preview").hidden = index < 0 || !state.previewUrl;
  if (index === state.activeIndex) return;
  state.rows[state.activeIndex]?.classList.remove("active");
  state.blocks[state.activeIndex]?.classList.remove("active");
  state.rows[index]?.classList.add("active");
  state.blocks[index]?.classList.add("active");
  state.activeIndex = index;
}

$("start").onclick = async () => {
  if (state.dirty && !window.confirm("放弃未保存修改并重新生成字幕？")) return;
  alertMessage();
  resetResult();
  state.busy = true;
  updateButtons();
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        media_id: state.media.id,
        engine: state.engine,
        model: $("model").value,
        language: $("language").value || null,
        track: Number($("track").value),
        device: $("device").value,
        compute_type: $("compute").value,
        prompt: $("prompt").value,
        condition_on_previous_text: $("previous-text").checked,
        filter_hallucinations: $("filter-hallucinations").checked,
        formats: selectedFormats(),
      }),
    });
    renderJob(job);
    sessionStorage.setItem("shengmu-job", job.id);
    pollJob(job.id);
  } catch (error) {
    state.busy = false;
    alertMessage(error.message);
    updateButtons();
  }
};

$("cancel").onclick = async () => {
  if (!state.job) return;
  try {
    renderJob(
      await api("/api/jobs/" + state.job.id + "/cancel", { method: "POST" }),
    );
  } catch (error) {
    alertMessage(error.message);
  }
};

$("save").onclick = async () => {
  alertMessage();
  if (!CueEditor.validCues(state.cues, state.transcriptDuration)) {
    alertMessage(
      "请检查字幕：内容不能为空，时间必须按顺序且不重叠，结束时间应在媒体时长内。",
    );
    return;
  }
  state.saving = true;
  updateButtons();
  $("save").textContent = "正在保存…";
  try {
    renderJob(
      await api("/api/jobs/" + state.job.id + "/transcript", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          segments: state.cues,
          formats: selectedFormats(),
        }),
      }),
    );
    state.dirty = false;
    $("edit-hint").textContent = "已保存，所有已选格式均已更新。";
  } catch (error) {
    alertMessage(error.message);
  } finally {
    state.saving = false;
    $("save").textContent = "保存并导出";
    updateButtons();
  }
};

$("add-cue").onclick = () => {
  try {
    applyEditorEdit(
      CueEditor.insertCue(
        state.cues,
        state.transcriptDuration,
        $("video").currentTime,
      ),
    );
  } catch (error) {
    alertMessage(error.message);
  }
};

$("undo").onclick = () => {
  const edit = state.history.pop();
  if (edit) applyEditorEdit(CueEditor.inverseEdit(edit), false);
};

$("release-source").onclick = async () => {
  if (!state.media || state.busy || state.saving || state.uploading) return;
  state.uploading = true;
  updateButtons();
  try {
    await releaseMedia(state.media.id);
    state.media = null;
    $("upload-hint").textContent = "源文件已释放，字幕仍可编辑和下载";
  } catch (error) {
    alertMessage(error.message);
  } finally {
    state.uploading = false;
    updateButtons();
  }
};

$("dropzone").onclick = () => $("file-input").click();
$("file-input").onchange = (event) => {
  uploadFile(event.target.files[0]);
  event.target.value = "";
};
for (const eventName of ["dragover", "dragenter"]) {
  $("dropzone").addEventListener(eventName, (event) => {
    event.preventDefault();
    if (!state.busy) $("dropzone").classList.add("dragover");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  $("dropzone").addEventListener(eventName, (event) => {
    event.preventDefault();
    $("dropzone").classList.remove("dragover");
  });
}
$("dropzone").addEventListener("drop", (event) =>
  uploadFile(event.dataTransfer.files[0]),
);
$("local-mode").onclick = () => switchEngine("local");
$("openai-mode").onclick = () => switchEngine("openai");
$("device").onchange = () => {
  $("compute").value = $("device").value === "cuda" ? "float16" : "int8";
};
$("format-options").onchange = () => {
  if (state.job?.status === "done")
    $("edit-hint").textContent = "点击保存并导出，应用新的格式选择。";
  updateButtons();
};
$("video").addEventListener("timeupdate", updatePreview);
$("video").addEventListener("error", () => {
  if (state.previewUrl)
    alertMessage(
      "浏览器无法预览此媒体编码，仍可继续生成字幕。可用支持该编码的播放器打开原文件。",
    );
});
window.addEventListener("beforeunload", (event) => {
  if (state.dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
window.addEventListener("resize", () => {
  requestAnimationFrame(() =>
    fitCueTexts(state.rows.map((row) => row.querySelector("textarea"))),
  );
});
window.addEventListener("keydown", (event) => {
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key.toLowerCase() === "z" &&
    !event.shiftKey &&
    !event.target.matches("input, textarea, [contenteditable]") &&
    !$("undo").disabled
  ) {
    event.preventDefault();
    $("undo").click();
  }
});

(async () => {
  try {
    state.config = await api("/api/config");
    $("connection").lastChild.textContent = "本地服务已连接";
    $("key-status").textContent = state.config.openai_key_configured
      ? "已配置 API Key（服务器环境变量）"
      : "未配置 API Key。请设置 OPENAI_API_KEY 后重启服务。";
    if (!state.config.ffmpeg || !state.config.ffprobe)
      alertMessage("未找到 FFmpeg / ffprobe，请安装后重新启动。");
    switchEngine("local");
    const jobId = sessionStorage.getItem("shengmu-job");
    if (jobId) pollJob(jobId);
  } catch (error) {
    $("connection").lastChild.textContent = "服务未连接";
    alertMessage(error.message);
  }
})();
