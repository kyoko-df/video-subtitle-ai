"use strict";

const captionFields = {
  cjk_chars: "cjk-chars",
  latin_chars: "latin-chars",
  max_lines: "max-lines",
  max_seconds: "max-seconds",
  min_seconds: "min-seconds",
  max_cps: "max-cps",
};
const styleFields = {
  font: "style-font",
  size: "style-size",
  color: "style-color",
  position: "style-position",
  margin: "style-margin",
};
let historyTimer,
  modelTimer,
  batchController,
  batchStopped = false;
let qualityTimer,
  findPosition = -1;
let translationProvider = "openai",
  translationRequest = 0,
  translationLoading = false;
const translationModels = { openai: "gpt-4o-mini", lmstudio: "" };
let translationInventory = [];
const asrFields = [
  "compression_ratio_threshold",
  "log_prob_threshold",
  "no_speech_threshold",
  "vad_threshold",
];
const translationFields = [
  "concurrency",
  "batch_size",
  "max_chars",
  "timeout",
  "retries",
  "max_tokens",
];

function translationOptions() {
  return {
    ...Object.fromEntries(
      translationFields.map((key) => [
        key,
        $("translation-" + key.replaceAll("_", "-")).value === ""
          ? null
          : Number($("translation-" + key.replaceAll("_", "-")).value),
      ]),
    ),
    reasoning: $("translation-reasoning").value,
    resume: $("translation-resume").checked,
  };
}

function restoreProjectTranslation(job) {
  const settings =
    job.operation?.kind === "translate" ? job.operation.settings : null;
  if (
    !settings ||
    !["openai", "lmstudio"].includes(settings.provider) ||
    typeof settings.model !== "string" ||
    typeof settings.target !== "string"
  )
    return;
  translationModels[translationProvider] = $("translation-model").value;
  translationProvider = settings.provider;
  $("translation-provider").value = settings.provider;
  $("translation-model").value = settings.model;
  translationModels[translationProvider] = settings.model;
  $("target-language").value = settings.target;
  for (const key of translationFields)
    if (settings[key] !== undefined)
      $("translation-" + key.replaceAll("_", "-")).value = settings[key] ?? "";
  $("translation-reasoning").value = settings.reasoning || "auto";
  $("translation-resume").checked = settings.resume ?? true;
  ++translationRequest;
  translationLoading = false;
  renderTranslationProvider();
  if (translationProvider === "lmstudio") refreshTranslationModels();
}

function renderTranslationCapabilities() {
  const local = $("translation-provider").value === "lmstudio";
  const model = local
    ? translationInventory.find(
        (m) => m.id === $("translation-model").value.trim(),
      )
    : null;
  for (const option of $("translation-reasoning").options)
    option.disabled =
      option.value !== "auto" &&
      (!local || !(model?.reasoning_options || []).includes(option.value));
  $("translation-reasoning").disabled = state.busy || state.saving || !local;
  const unsupported = $("translation-reasoning").selectedOptions[0]?.disabled;
  $("translate").disabled ||= !!unsupported;
  $("translation-capabilities").textContent = model
    ? [
        model.loaded === true
          ? "已加载"
          : model.loaded === false
            ? "尚未加载"
            : "加载状态未知",
        model.format,
        model.context_length ? `上下文 ${model.context_length}` : "",
        model.parallel ? `服务并发 ${model.parallel}` : "并发能力未报告",
        unsupported ? "当前思考选项不受支持，请选择模型默认" : "",
      ]
        .filter(Boolean)
        .join(" · ")
    : local
      ? "刷新模型后可查看加载状态和思考能力；默认方式可直接填写模型标识。"
      : "使用模型默认思考设置。";
}

function jsonRequest(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function exportSettings() {
  return {
    captions: Object.fromEntries(
      Object.entries(captionFields).map(([key, id]) => [
        key,
        Number($(id).value),
      ]),
    ),
    style: Object.fromEntries(
      Object.entries(styleFields).map(([key, id]) => [
        key,
        ["size", "margin"].includes(key) ? Number($(id).value) : $(id).value,
      ]),
    ),
    export_mode: $("export-mode").value,
    speaker_labels: $("speaker-labels").checked,
    word_highlight: $("word-highlight").checked,
    allow_overlap: $("allow-overlap").checked,
  };
}

function jobSettings() {
  return {
    ...exportSettings(),
    export_mode: "original",
    engine: state.engine,
    model: $("model").value,
    language: $("language").value || null,
    device: $("device").value,
    compute_type: $("compute").value,
    prompt: $("prompt").value,
    condition_on_previous_text: $("previous-text").checked,
    filter_hallucinations: $("filter-hallucinations").checked,
    asr_profile: $("asr-profile").value,
    ...Object.fromEntries(
      asrFields.map((key) => [
        key,
        $(key.replaceAll("_", "-")).value === ""
          ? null
          : Number($(key.replaceAll("_", "-")).value),
      ]),
    ),
    diarize: $("diarize").checked,
    num_speakers: $("num-speakers").value
      ? Number($("num-speakers").value)
      : null,
    formats: selectedFormats(),
  };
}

function applySettings(settings = {}) {
  for (const [group, fields] of [
    ["captions", captionFields],
    ["style", styleFields],
  ]) {
    for (const [key, id] of Object.entries(fields))
      if (settings[group]?.[key] !== undefined)
        $(id).value = settings[group][key];
  }
  for (const key of ["speaker_labels", "word_highlight", "allow_overlap"])
    if (settings[key] !== undefined)
      $(key.replaceAll("_", "-")).checked = settings[key];
  if (settings.export_mode) $("export-mode").value = settings.export_mode;
  for (const key of ["model", "language", "device", "prompt"])
    if (settings[key] !== undefined) $(key).value = settings[key] ?? "";
  if (settings.compute_type) $("compute").value = settings.compute_type;
  if (settings.asr_profile) $("asr-profile").value = settings.asr_profile;
  for (const key of asrFields)
    if (settings[key] !== undefined)
      $(key.replaceAll("_", "-")).value = settings[key] ?? "";
  for (const [key, id] of [
    ["condition_on_previous_text", "previous-text"],
    ["filter_hallucinations", "filter-hallucinations"],
    ["diarize", "diarize"],
  ])
    if (settings[key] !== undefined) $(id).checked = settings[key];
  if (settings.num_speakers !== undefined)
    $("num-speakers").value = settings.num_speakers ?? "";
  if (settings.formats)
    for (const input of document.querySelectorAll("#format-options input"))
      input.checked = settings.formats.includes(input.value);
  if (settings.engine) state.engine = settings.engine;
}

function updateStudioButtons(editing) {
  for (const id of [
    "replace-all",
    "shift-all",
    "reflow",
    "burn-video",
    "soft-video",
    "translate",
    "delete-suspicions",
  ])
    $(id).disabled = !editing;
  $("redo").disabled = !editing || !state.redo.length;
  $("loop-cue").disabled = !editing || !state.previewUrl || !state.cues.length;
  $("translate").disabled ||=
    !$("translation-model").value.trim() ||
    !$("target-language").value.trim() ||
    ($("translation-provider").value === "openai" &&
      (!state.config?.openai_key_configured ||
        !state.config?.openai_engine_installed));
  $("delete-suspicions").disabled ||= !state.cues.some(
    (c) => c.suspicions?.length,
  );
  $("burn-video").disabled ||=
    !state.job?.media_available ||
    state.config?.ffmpeg_capabilities?.burn_supported === false;
  $("soft-video").disabled ||=
    !state.job?.media_available ||
    state.config?.ffmpeg_capabilities?.soft_supported === false;
  for (const input of document.querySelectorAll(
    ".editor-tools input, .editor-tools select, .delivery-tools input, .delivery-tools select",
  ))
    input.disabled = state.saving || state.busy;
  renderTranslationCapabilities();
  $("translation-model-refresh").disabled =
    state.saving || state.busy || translationLoading;
  $("operation-cancel").hidden = !["queued", "running"].includes(
    state.job?.operation?.status,
  );
}

function renderTranslationProvider() {
  const local = $("translation-provider").value === "lmstudio";
  $("translation-model-refresh").hidden = !local;
  $("translation-model").placeholder = local
    ? "选择或填写本地模型标识"
    : "例如 gpt-4o-mini";
  $("translation-note").textContent = local
    ? "字幕文本会发给本机 LM Studio 服务。请先安装 LM Studio，下载并加载文字对话模型，在 Developer 页面启动服务；无需 OpenAI Key。"
    : "翻译会向配置的 OpenAI 服务发送字幕文本并使用 API 额度。";
  $("translation-model-options").replaceChildren();
  if (!local) $("translation-reasoning").value = "auto";
  $("translation-model-status").hidden = !local;
  updateButtons();
}

async function refreshTranslationModels() {
  if ($("translation-provider").value !== "lmstudio") return;
  const request = ++translationRequest;
  translationLoading = true;
  const status = $("translation-model-status");
  status.hidden = false;
  status.textContent = "正在连接本机 LM Studio…";
  updateButtons();
  try {
    const { models, details } = await api("/api/translation/models");
    if (request !== translationRequest) return;
    translationInventory = details || [];
    $("translation-model-options").replaceChildren(
      ...models.map((id) => {
        const option = document.createElement("option");
        option.value = id;
        const model = translationInventory.find((m) => m.id === id);
        option.label = model
          ? `${model.name} · ${model.loaded === true ? "已加载" : model.loaded === false ? "未加载" : "状态未知"}`
          : id;
        return option;
      }),
    );
    if (!$("translation-model").value.trim() && models.length === 1)
      $("translation-model").value = models[0];
    translationModels.lmstudio = $("translation-model").value;
    status.textContent = models.length
      ? `已连接，发现 ${models.length} 个文字模型，已排除已知的嵌入模型。选择模型后可查看能力。`
      : "服务已连接，但没有可用模型。请在 LM Studio 中下载文字对话模型后刷新。";
  } catch (error) {
    if (request !== translationRequest) return;
    status.textContent = error.message;
  } finally {
    if (request === translationRequest) {
      translationLoading = false;
      updateButtons();
    }
  }
}

function localDraftKey(id = state.job?.id) {
  return "shengmu-draft-" + id;
}
function draftPayload() {
  return {
    ...exportSettings(),
    formats: selectedFormats(),
    segments: state.cues,
    revision: state.job.revision,
  };
}
function clearLocalDraft() {
  clearTimeout(state.draftTimer);
  localStorage.removeItem(localDraftKey());
  state.draftPending = false;
}

function scheduleDraft() {
  if (state.job?.status !== "done") return;
  state.editGeneration += 1;
  state.draftPending = true;
  try {
    localStorage.setItem(localDraftKey(), JSON.stringify(draftPayload()));
  } catch (_) {
    $("edit-hint").textContent = "浏览器备份空间不足，正在保存服务端草稿…";
  }
  clearTimeout(state.draftTimer);
  state.draftTimer = setTimeout(
    () =>
      flushDraft().catch((error) =>
        alertMessage("草稿尚未保存：" + error.message),
      ),
    700,
  );
  clearTimeout(qualityTimer);
  qualityTimer = setTimeout(renderQuality, 200);
}

async function flushDraft() {
  clearTimeout(state.draftTimer);
  if (!state.draftPending || state.job?.status !== "done")
    return state.draftPromise;
  const id = state.job.id,
    generation = state.editGeneration,
    body = JSON.parse(JSON.stringify(draftPayload()));
  state.draftPromise = state.draftPromise
    .catch(() => {})
    .then(() => api("/api/jobs/" + id + "/draft", jsonRequest("PUT", body)));
  await state.draftPromise;
  if (state.job?.id === id && state.editGeneration === generation) {
    state.draftPending = false;
    $("edit-hint").textContent =
      "草稿已自动保存。保存并导出后，下载文件会同步更新。";
    localStorage.removeItem(localDraftKey(id));
  }
}

async function loadProjectResult(job, transcript, sequence) {
  const draft = await api("/api/jobs/" + job.id + "/draft");
  if (sequence !== state.resultSequence || state.job?.id !== job.id) return;
  let backup;
  try {
    backup = JSON.parse(localStorage.getItem(localDraftKey(job.id)));
  } catch (_) {
    /* invalid local backup */
  }
  const restored =
    backup?.revision === job.revision
      ? backup
      : draft?.revision === job.revision
        ? draft
        : null;
  applySettings(job.request);
  applySettings(transcript.metadata);
  restoreProjectTranslation(job);
  if (restored) applySettings(restored);
  state.cues = (restored?.segments || transcript.segments).map((c) => ({
    ...c,
    start: c.start ?? NaN,
    end: c.end ?? NaN,
  }));
  state.history = [];
  state.redo = [];
  state.dirty = !!restored;
  state.draftPending = false;
  state.loop = null;
  $("loop-cue").setAttribute("aria-pressed", "false");
  state.transcriptDuration = transcript.duration;
  $("workspace-title").textContent = job.source;
  if (job.media_available) {
    try {
      const media = await api("/api/media/" + job.media_id);
      if (sequence !== state.resultSequence) return;
      showSourceMedia(media);
      if (job.request.track != null) $("track").value = job.request.track;
      loadWaveform(media, job.request.track, sequence);
    } catch (error) {
      alertMessage(error.message);
    }
  } else {
    state.media = null;
    state.previewUrl = null;
    state.waveform = null;
    $("video").removeAttribute("src");
    $("video").hidden = true;
    $("preview-empty").hidden = false;
    $("waveform-status").textContent = job.media_linked
      ? "源视频不可用，请重新连接共享；仍可编辑字幕"
      : "源文件已释放，仍可编辑字幕";
  }
  $("duration").textContent = timeLabel(transcript.duration);
  switchEngine(state.engine);
  renderCues();
  renderTimeline();
  drawWaveform();
  renderQuality();
  updateButtons();
  if (restored)
    $("edit-hint").textContent = "已恢复自动保存的草稿，下载前请保存并导出。";
  if (backup?.revision === job.revision) {
    state.draftPending = true;
    await flushDraft();
  }
  renderOperation(job);
}

async function openProject(id) {
  try {
    await flushDraft();
    clearTimeout(state.pollTimer);
    const sequence = ++state.resultSequence;
    const job = await api("/api/jobs/" + id);
    if (sequence !== state.resultSequence) return;
    state.cues = [];
    state.media = null;
    state.loop = null;
    state.waveform = null;
    state.transcriptDuration = null;
    state.dirty = false;
    renderCues();
    renderTimeline();
    drawWaveform();
    $("video").hidden = true;
    renderJob(job);
    sessionStorage.setItem("shengmu-job", id);
    pollJob(id, sequence);
  } catch (error) {
    alertMessage(error.message);
  }
}

function makeButton(text, action, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.textContent = text;
  button.disabled = disabled;
  button.onclick = async () => {
    try {
      await action();
    } catch (error) {
      alertMessage(error.message);
    }
  };
  return button;
}

async function refreshHistory() {
  clearTimeout(historyTimer);
  try {
    const jobs = await api("/api/jobs");
    $("job-list").replaceChildren();
    if (!jobs.length) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "尚无项目";
      $("job-list").append(p);
    }
    for (const job of jobs) {
      const row = document.createElement("div");
      row.className = "job-row";
      row.classList.toggle("selected", job.id === state.job?.id);
      const status = ["queued", "running"].includes(job.operation?.status)
        ? `${job.operation.kind === "translate" ? "翻译" : "视频导出"} ${Math.round(job.operation.progress * 100)}%`
        : job.operation?.interrupted
          ? "操作中断，可继续"
          : ["error", "cancelled"].includes(job.operation?.status)
            ? `${job.operation.kind === "translate" ? "翻译" : "视频导出"}${job.operation.status === "error" ? "失败" : "已取消"}`
            : {
                done: "已完成",
                error: "失败",
                cancelled: "已取消",
                queued: "排队中",
              }[job.status] || Math.round(job.progress * 100) + "%";
      const title = makeButton(
        job.source +
          " · " +
          status +
          " · " +
          new Date(job.updated * 1000).toLocaleString("zh-CN", {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          }) +
          " · " +
          job.id.slice(0, 6) +
          (job.draft ? " · 草稿" : ""),
        () => openProject(job.id),
      );
      title.classList.add("job-open");
      row.append(title);
      if (["error", "cancelled"].includes(job.status))
        row.append(
          makeButton(
            "重试",
            async () => {
              await api(`/api/jobs/${job.id}/retry`, { method: "POST" });
              await refreshHistory();
            },
            !job.media_available,
          ),
        );
      if (!terminal.has(job.status))
        row.append(
          makeButton("取消", async () => {
            await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
            await refreshHistory();
          }),
        );
      row.append(
        makeButton(
          "删除任务",
          async () => {
            if (job.id === state.job?.id) await flushDraft();
            await api(`/api/jobs/${job.id}`, { method: "DELETE" });
            if (job.id === state.job?.id) {
              clearLocalDraft();
              resetResult();
              updateButtons();
            }
            await refreshHistory();
          },
          !terminal.has(job.status) ||
            ["queued", "running"].includes(job.operation?.status),
        ),
      );
      if (job.media_available && terminal.has(job.status))
        row.append(
          makeButton(job.media_linked ? "移除引用" : "释放源文件", async () => {
            await releaseMedia(job.media_id);
            if (job.id === state.job?.id) await openProject(job.id);
            await refreshHistory();
          }),
        );
      $("job-list").append(row);
    }
    const mediaFiles = await api("/api/media");
    $("media-cache").replaceChildren();
    for (const media of mediaFiles) {
      const row = document.createElement("div");
      row.className = "job-row";
      const label = document.createElement("span");
      label.textContent = `${media.name} · ${(media.size / 1024 ** 2).toFixed(1)} MB${media.linked ? " · 仅引用" : ""}${media.available ? "" : " · 源文件不可用"}`;
      row.append(label);
      row.append(
        makeButton(
          "打开",
          async () => {
            if (state.busy || state.saving || state.uploading) return;
            await flushDraft();
            const current = await api(`/api/media/${media.id}`);
            if (!current.available)
              throw new Error(
                "源文件不可用，请重新连接共享；内容改变后需要重新添加视频。",
              );
            resetResult();
            showSourceMedia(current);
            loadWaveform(current);
            updateButtons();
          },
          !media.available || state.busy || state.saving || state.uploading,
        ),
      );
      row.append(
        makeButton(
          media.linked ? "移除引用" : "释放",
          async () => {
            await releaseMedia(media.id);
            if (state.media?.id === media.id) {
              state.media = null;
              state.previewUrl = null;
              state.waveform = null;
              $("video").removeAttribute("src");
              $("video").hidden = true;
              if (state.job) state.job.media_available = false;
              updateButtons();
              drawWaveform();
            }
            await refreshHistory();
          },
          media.active,
        ),
      );
      $("media-cache").append(row);
    }
    $("batch-download").hidden = !jobs.some((j) => j.status === "done");
    const current = jobs.find((j) => j.id === state.job?.id);
    if (current && ["queued", "running"].includes(current.operation?.status))
      renderOperation(current);
    if (
      current &&
      state.busy &&
      terminal.has(current.status) &&
      terminal.has(current.operation?.status)
    )
      pollJob(current.id);
  } catch (error) {
    $("batch-status").textContent = "任务列表暂不可用：" + error.message;
  }
  historyTimer = setTimeout(refreshHistory, 2500);
}

async function batchTranscribe(files) {
  const settings = jobSettings();
  if (!settings.formats.length) {
    alertMessage("请至少选择一种导出格式。");
    return;
  }
  batchStopped = false;
  $("batch-select").disabled = true;
  $("batch-cancel").hidden = false;
  const errors = [];
  try {
    for (let index = 0; index < files.length && !batchStopped; index++) {
      const file = files[index];
      if (file.size > state.config.max_upload) {
        errors.push(file.name + "：超过上传上限");
        continue;
      }
      while (!batchStopped) {
        const jobs = await api("/api/jobs");
        if (jobs.filter((j) => !terminal.has(j.status)).length < 5) break;
        $("batch-status").textContent = "等待队列空位…";
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      if (batchStopped) break;
      $("batch-status").textContent =
        `正在添加 ${index + 1} / ${files.length}：${file.name}`;
      batchController = new AbortController();
      let media;
      try {
        media = await api("/api/media?name=" + encodeURIComponent(file.name), {
          method: "POST",
          body: file,
          signal: batchController.signal,
        });
        if (batchStopped) {
          await releaseMedia(media.id);
          break;
        }
        await api(
          "/api/jobs",
          jsonRequest("POST", {
            ...settings,
            media_id: media.id,
            track: media.audio_tracks[0].index,
          }),
        );
      } catch (error) {
        if (media) await releaseMedia(media.id).catch(() => {});
        if (error.name !== "AbortError")
          errors.push(file.name + "：" + error.message);
      }
    }
    $("batch-status").textContent =
      (batchStopped
        ? "已停止添加，已排队任务继续处理。"
        : "文件已加入任务列表。") +
      (errors.length ? " " + errors.join("；") : "");
  } catch (error) {
    alertMessage(error.message);
  } finally {
    batchController = null;
    $("batch-select").disabled = false;
    $("batch-cancel").hidden = true;
    await refreshHistory();
  }
}

function renderQuality() {
  if (!state.transcriptDuration) return;
  const issues = CueEditor.quality(
    state.cues,
    exportSettings(),
    state.transcriptDuration,
  );
  $("quality-summary").textContent =
    "字幕质量检查 · " + issues.length + " 条需检查";
  $("quality-list").replaceChildren();
  for (const row of state.rows) row.classList.remove("has-issue");
  for (const issue of issues) {
    state.rows[issue.index]?.classList.add("has-issue");
    $("quality-list").append(
      makeButton(`第 ${issue.index + 1} 条：${issue.messages.join("、")}`, () =>
        focusCue(issue.index),
      ),
    );
  }
}

function focusCue(index) {
  if (!state.cues[index]) return;
  state.selectedCue = index;
  seek(state.cues[index].start);
  state.rows[index]?.scrollIntoView({ block: "nearest" });
}

function wholeEdit(cues) {
  applyEditorEdit({ index: 0, removed: state.cues, inserted: cues });
  updateButtons();
  renderQuality();
}

function bindTimelineDrag(block) {
  block.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || state.saving || state.busy) return;
    const index = Number(block.dataset.index),
      cue = state.cues[index];
    const rect = block.getBoundingClientRect(),
      lane = $("timeline-lane").getBoundingClientRect();
    const mode =
      event.clientX - rect.left < 8
        ? "start"
        : rect.right - event.clientX < 8
          ? "end"
          : "move";
    let delta = 0;
    block.setPointerCapture(event.pointerId);
    const move = (e) => {
      delta =
        ((e.clientX - event.clientX) / lane.width) * state.transcriptDuration;
      const start = mode === "end" ? cue.start : cue.start + delta;
      const end = mode === "start" ? cue.end : cue.end + delta;
      block.style.left = (start / state.transcriptDuration) * 100 + "%";
      block.style.width =
        ((end - start) / state.transcriptDuration) * 100 + "%";
    };
    const finish = (e) => {
      block.removeEventListener("pointermove", move);
      block.removeEventListener("pointerup", finish);
      block.removeEventListener("pointercancel", cancel);
      if (block.hasPointerCapture(e.pointerId))
        block.releasePointerCapture(e.pointerId);
      updateTimelineBlock(block, index);
      if (Math.abs(delta) < 0.01) return;
      block.dataset.dragged = "1";
      setTimeout(() => {
        delete block.dataset.dragged;
      }, 0);
      try {
        const next = {
          ...cue,
          start: mode === "end" ? cue.start : cue.start + delta,
          end: mode === "start" ? cue.end : cue.end + delta,
          words:
            mode === "move"
              ? (cue.words || []).map((w) => ({
                  ...w,
                  start: w.start + delta,
                  end: w.end + delta,
                }))
              : [],
        };
        const cues = [...state.cues];
        cues[index] = next;
        if (
          !CueEditor.validCues(
            cues,
            state.transcriptDuration,
            $("allow-overlap").checked,
          )
        )
          throw new Error(
            "时间调整超出媒体范围、改变字幕顺序或产生不允许的重叠。",
          );
        applyEditorEdit({ index, removed: [cue], inserted: [next] });
      } catch (error) {
        alertMessage(error.message);
      }
    };
    const cancel = (e) => {
      delta = 0;
      finish(e);
    };
    block.addEventListener("pointermove", move);
    block.addEventListener("pointerup", finish);
    block.addEventListener("pointercancel", cancel);
  });
}

async function loadWaveform(media, track, sequence = state.resultSequence) {
  $("waveform-status").textContent = "正在读取音频波形…";
  try {
    const data = await api(
      `/api/media/${media.id}/waveform` +
        (track == null ? "" : "?track=" + track),
    );
    if (sequence !== state.resultSequence || state.media?.id !== media.id)
      return;
    state.waveform = data;
    drawWaveform();
    $("waveform-status").textContent = "音频波形已就绪";
  } catch (error) {
    if (sequence === state.resultSequence)
      $("waveform-status").textContent = "波形不可用：" + error.message;
  }
}

function drawWaveform() {
  const canvas = $("waveform");
  const width = Math.min(
    16000,
    Math.max(
      100,
      $("timeline-scroll").clientWidth * Number($("timeline-zoom").value),
    ),
  );
  $("timeline-content").style.width =
    Number($("timeline-zoom").value) * 100 + "%";
  canvas.width = width;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, width, 70);
  ctx.fillStyle = "#8faaa0";
  const waveform = state.waveform;
  if (!waveform) return;
  const duration =
    state.transcriptDuration || state.media?.duration || waveform.duration;
  waveform.peaks.forEach((peak, index) => {
    const x =
      (((waveform.offset || 0) +
        (index / waveform.peaks.length) * waveform.duration) /
        duration) *
      width;
    const height = Math.max(1, peak * 65);
    ctx.fillRect(
      x,
      (70 - height) / 2,
      Math.max(
        0.5,
        ((waveform.duration / duration) * width) / waveform.peaks.length,
      ),
      height,
    );
  });
}

function renderSubtitlePreview() {
  const time = $("video").currentTime;
  const active = state.cues
    .map((cue, index) => ({ cue, index }))
    .filter(({ cue }) => cue.start <= time && time < cue.end);
  const preview = $("subtitle-preview"),
    settings = exportSettings();
  const signature = JSON.stringify([
    active.map(({ cue, index }) => [
      index,
      cue.text,
      cue.translation,
      cue.speaker,
      cue.words,
    ]),
    settings.export_mode,
    settings.word_highlight,
    settings.speaker_labels,
  ]);
  if (preview.dataset.signature !== signature) {
    preview.dataset.signature = signature;
    preview.replaceChildren();
    for (const { cue } of active) {
      const line = document.createElement("div");
      if (settings.speaker_labels && cue.speaker) {
        const label = document.createElement("span");
        label.textContent = cue.speaker + ": ";
        line.append(label);
      }
      if (settings.export_mode !== "translated") {
        if (settings.word_highlight && cue.words?.length)
          for (const word of cue.words) {
            const span = document.createElement("span");
            span.textContent = word.text;
            span.dataset.start = word.start;
            span.dataset.end = word.end;
            span.className = "preview-word";
            span.title = word.estimated ? "估算时间" : "识别词时间";
            line.append(span);
          }
        else line.append(document.createTextNode(cue.text));
      }
      if (settings.export_mode !== "original") {
        if (settings.export_mode === "bilingual")
          line.append(document.createElement("br"));
        line.append(document.createTextNode(cue.translation || "（尚未翻译）"));
      }
      preview.append(line);
    }
  }
  for (const span of preview.querySelectorAll(".preview-word"))
    span.classList.toggle(
      "spoken",
      Number(span.dataset.start) <= time && time < Number(span.dataset.end),
    );
  preview.hidden = !active.length || !state.previewUrl;
  preview.style.fontFamily = settings.style.font;
  preview.style.fontSize =
    Math.max(9, (settings.style.size * $("video").clientWidth) / 1920) + "px";
  preview.style.color = settings.style.color;
  preview.style.bottom =
    settings.style.position === "bottom"
      ? Math.max(10, (settings.style.margin / 1080) * 100) + "%"
      : "auto";
  preview.style.top =
    settings.style.position === "top"
      ? Math.max(3, (settings.style.margin / 1080) * 100) + "%"
      : settings.style.position === "middle"
        ? "50%"
        : "auto";
  preview.style.transform =
    settings.style.position === "middle" ? "translateY(-50%)" : "none";
}

function renderOperation(job) {
  const op = job.operation;
  const stats = op?.stats;
  $("operation-status").textContent = op?.status
    ? `${op.message} (${Math.round(op.progress * 100)}%)` +
      (stats
        ? ` · 已恢复 ${stats.restored_cues} 条 · 耗时 ${Math.floor(stats.elapsed_seconds)} 秒 · 重试 ${stats.retries} · 拆批 ${stats.splits}${stats.checkpoint_saved ? " · 译文已保存，可继续" : ""}`
        : "")
    : "";
  $("video-downloads").replaceChildren();
  for (const video of job.videos ||
    (op?.url && op.status === "done" ? [{ url: op.url, mode: op.kind }] : [])) {
    const link = document.createElement("a");
    link.className = "secondary";
    link.href = video.url;
    link.textContent =
      video.mode === "burn" ? "下载烧录视频 ↓" : "下载字幕轨视频 ↓";
    $("video-downloads").append(link);
  }
}

async function performOperation(kind, body) {
  if (state.dirty && !(await saveCurrentProject())) return;
  if (!state.job) return;
  const job = await api(
    `/api/jobs/${state.job.id}/${kind}`,
    jsonRequest("POST", body),
  );
  renderJob(job);
  pollJob(job.id);
}

async function refreshModels() {
  clearTimeout(modelTimer);
  try {
    const models = await api("/api/models");
    $("model-manager").replaceChildren();
    for (const model of models) {
      const row = document.createElement("div");
      row.className = "model-row";
      const text = document.createElement("span");
      const active = ["queued", "running"].includes(model.download?.status);
      text.textContent =
        model.name +
        " · " +
        (active
          ? Math.round(model.download.progress * 100) + "%"
          : model.downloaded
            ? (model.size / 1024 ** 2).toFixed(0) + " MB"
            : "未下载");
      text.title = model.download?.message || model.path;
      row.append(
        text,
        makeButton(
          active
            ? "取消"
            : model.downloaded
              ? model.managed
                ? "移除"
                : "已有缓存"
              : "下载",
          async () => {
            await api(
              `/api/models/${model.name}` +
                (active ? "/cancel" : model.downloaded ? "" : "/download"),
              { method: model.downloaded && !active ? "DELETE" : "POST" },
            );
            await refreshModels();
          },
          model.downloaded && !model.managed && !active,
        ),
      );
      if (model.download?.status === "error") {
        const error = document.createElement("small");
        error.textContent = model.download.message;
        row.append(error);
      }
      $("model-manager").append(row);
    }
    if (models.some((m) => ["queued", "running"].includes(m.download?.status)))
      modelTimer = setTimeout(refreshModels, 1500);
  } catch (error) {
    $("model-manager").textContent = error.message;
  }
}

async function initStudio() {
  const preferences = await api("/api/preferences");
  if (preferences.job) applySettings(preferences.job);
  translationProvider =
    preferences.translation_provider === "lmstudio" ? "lmstudio" : "openai";
  $("translation-provider").value = translationProvider;
  $("translation-model").value =
    preferences.translation_model ?? translationModels[translationProvider];
  translationModels[translationProvider] = $("translation-model").value;
  if (preferences.target_language)
    $("target-language").value = preferences.target_language;
  for (const key of translationFields)
    if (preferences.translation_options?.[key] !== undefined)
      $("translation-" + key.replaceAll("_", "-")).value =
        preferences.translation_options[key] ?? "";
  $("translation-reasoning").value =
    preferences.translation_options?.reasoning || "auto";
  $("translation-resume").checked =
    preferences.translation_options?.resume ?? true;
  renderTranslationProvider();
  if (translationProvider === "lmstudio") refreshTranslationModels();
  $("project-storage").textContent = state.config.persistent
    ? "项目、源文件与草稿自动保存在本机，重启后可继续编辑。"
    : "当前是临时会话，关闭服务后会清理。";
  $("diarization-hint").textContent = state.config.diarization_installed
    ? "已安装说话人引擎。首次使用需 HF_TOKEN 和模型访问权限，也支持本地模型。"
    : "自动区分需安装 .[local,diarization] 并配置 HF_TOKEN；字幕行支持手动标注。";
  refreshHistory();
  refreshModels();
}

$("history-refresh").onclick = refreshHistory;
$("delete-suspicions").onclick = () => {
  wholeEdit(CueEditor.removeSuspicions(state.cues));
  renderQuality();
  updateButtons();
};
$("translation-provider").onchange = () => {
  translationModels[translationProvider] = $("translation-model").value;
  translationProvider = $("translation-provider").value;
  ++translationRequest;
  translationLoading = false;
  $("translation-model").value = translationModels[translationProvider];
  renderTranslationProvider();
  if (translationProvider === "lmstudio") refreshTranslationModels();
};
$("translation-model-refresh").onclick = refreshTranslationModels;
$("translation-model").oninput = () => {
  translationModels[translationProvider] = $("translation-model").value;
  updateButtons();
};
$("translation-reasoning").onchange = updateButtons;
$("target-language").oninput = updateButtons;
$("batch-select").onclick = () => $("batch-input").click();
$("batch-input").onchange = (event) => {
  const files = [...event.target.files];
  event.target.value = "";
  if (files.length) batchTranscribe(files);
};
$("batch-cancel").onclick = () => {
  batchStopped = true;
  batchController?.abort();
};
$("redo").onclick = () => {
  const edit = state.redo.pop();
  if (edit) {
    state.history.push(edit);
    applyEditorEdit(edit, false);
    updateButtons();
  }
};
$("find-next").onclick = () => {
  const query = $("find-text").value;
  if (!query) return;
  for (let i = 1; i <= state.cues.length; i++) {
    const index = (findPosition + i) % state.cues.length;
    if (state.cues[index].text.includes(query)) {
      findPosition = index;
      focusCue(index);
      return;
    }
  }
  alertMessage("未找到匹配字幕。");
};
$("replace-all").onclick = () => {
  try {
    wholeEdit(
      CueEditor.replaceText(
        state.cues,
        $("find-text").value,
        $("replace-text").value,
      ),
    );
  } catch (error) {
    alertMessage(error.message);
  }
};
$("shift-all").onclick = () => {
  try {
    wholeEdit(
      CueEditor.shiftCues(
        state.cues,
        Number($("time-offset").value),
        state.transcriptDuration,
        $("allow-overlap").checked,
      ),
    );
  } catch (error) {
    alertMessage(error.message);
  }
};
$("loop-cue").onclick = () => {
  if (state.loop) state.loop = null;
  else {
    const cue =
      state.cues[
        state.activeIndex >= 0 ? state.activeIndex : (state.selectedCue ?? 0)
      ];
    if (cue) {
      state.loop = { start: cue.start, end: cue.end };
      seek(cue.start);
    }
  }
  $("loop-cue").setAttribute("aria-pressed", String(!!state.loop));
};
$("video").addEventListener("timeupdate", () => {
  if (state.loop && $("video").currentTime >= state.loop.end)
    $("video").currentTime = state.loop.start;
});
$("video").addEventListener("ended", () => {
  if (state.loop) {
    $("video").currentTime = state.loop.start;
    $("video")
      .play()
      .catch(() => {});
  }
});
$("playback-speed").onchange = () => {
  $("video").playbackRate = Number($("playback-speed").value);
};
$("timeline-zoom").oninput = drawWaveform;
$("waveform").onclick = (event) => {
  const rect = event.target.getBoundingClientRect();
  seek(
    ((event.clientX - rect.left) / rect.width) *
      (state.transcriptDuration || state.media?.duration || 0),
  );
};
$("translate").onclick = async () => {
  for (const input of document.querySelectorAll(".delivery-tools input"))
    if (!input.reportValidity()) return;
  try {
    await performOperation("translate", {
      provider: $("translation-provider").value,
      target: $("target-language").value.trim(),
      model: $("translation-model").value.trim(),
      ...translationOptions(),
    });
  } catch (error) {
    alertMessage(error.message);
  }
};
$("burn-video").onclick = async () => {
  try {
    await performOperation("video", { mode: "burn" });
  } catch (error) {
    alertMessage(error.message);
  }
};
$("soft-video").onclick = async () => {
  try {
    await performOperation("video", { mode: "soft" });
  } catch (error) {
    alertMessage(error.message);
  }
};
$("operation-cancel").onclick = async () => {
  try {
    await api(`/api/jobs/${state.job.id}/operation/cancel`, { method: "POST" });
  } catch (error) {
    alertMessage(error.message);
  }
};
$("reflow").onclick = async () => {
  try {
    if (state.dirty && !(await saveCurrentProject())) return;
    const job = await api(
      `/api/jobs/${state.job.id}/reflow`,
      jsonRequest("POST", exportSettings().captions),
    );
    renderJob(job);
    await pollJob(job.id);
  } catch (error) {
    alertMessage(error.message);
  }
};
$("save-preferences").onclick = async () => {
  try {
    await api(
      "/api/preferences",
      jsonRequest("PUT", {
        job: {
          ...jobSettings(),
          ...exportSettings(),
          media_id: "preferences",
          track: null,
        },
        translation_provider: $("translation-provider").value,
        translation_model: $("translation-model").value.trim(),
        target_language: $("target-language").value,
        translation_options: translationOptions(),
      }),
    );
    $("preferences-status").textContent = "常用配置已保存";
  } catch (error) {
    alertMessage(error.message);
  }
};
for (const id of [
  ...Object.values(captionFields),
  ...Object.values(styleFields),
  "speaker-labels",
  "word-highlight",
  "allow-overlap",
  "export-mode",
])
  $(id).addEventListener("change", () => {
    if (state.job?.status === "done") markDirty();
    renderQuality();
    renderSubtitlePreview();
  });
$("format-options").addEventListener("change", () => {
  if (state.job?.status === "done") markDirty();
});
window.addEventListener("resize", drawWaveform);
window.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea, select, [contenteditable]"))
    return;
  if (
    (event.ctrlKey || event.metaKey) &&
    (event.key.toLowerCase() === "y" ||
      (event.key.toLowerCase() === "z" && event.shiftKey)) &&
    !$("redo").disabled
  ) {
    event.preventDefault();
    $("redo").click();
  }
  if (event.code === "Space" && state.previewUrl) {
    event.preventDefault();
    if ($("video").paused)
      $("video")
        .play()
        .catch(() => {});
    else $("video").pause();
  }
  if (["ArrowLeft", "ArrowRight"].includes(event.key) && state.cues.length) {
    event.preventDefault();
    const current =
      state.activeIndex >= 0 ? state.activeIndex : (state.selectedCue ?? 0);
    focusCue(
      Math.max(
        0,
        Math.min(
          state.cues.length - 1,
          current + (event.key === "ArrowRight" ? 1 : -1),
        ),
      ),
    );
  }
});
