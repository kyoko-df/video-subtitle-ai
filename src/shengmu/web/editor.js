"use strict";

const CueEditor = (() => {
  function activeIndex(cues, time, previous = -1) {
    if (!Number.isFinite(time)) return -1;
    const current = cues[previous];
    if (current && current.start <= time && time < current.end) return previous;
    const next = cues[previous + 1];
    if (next && next.start <= time && time < next.end) return previous + 1;
    let low = 0;
    let high = cues.length - 1;
    while (low <= high) {
      const middle = (low + high) >>> 1;
      if (cues[middle].start <= time) low = middle + 1;
      else high = middle - 1;
    }
    return high >= 0 && time < cues[high].end ? high : -1;
  }

  function splitCue(cue, cursor, time) {
    const left = cue.text.slice(0, cursor).trim();
    const right = cue.text.slice(cursor).trim();
    if (!left || !right || cue.end - cue.start < 0.002) {
      throw new Error(
        "请将文字光标放在字幕中间，并确保两侧均有内容和有效时长。",
      );
    }
    const ratio = cursor / cue.text.length;
    const midpoint =
      time > cue.start && time < cue.end
        ? time
        : cue.start + (cue.end - cue.start) * ratio;
    const cut = Math.max(
      cue.start + 0.001,
      Math.min(cue.end - 0.001, midpoint),
    );
    return [
      {
        ...cue,
        start: cue.start,
        end: cut,
        text: left,
        ...(cue.words ? { words: [] } : {}),
        ...(cue.translation !== undefined ? { translation: null } : {}),
      },
      {
        ...cue,
        start: cut,
        end: cue.end,
        text: right,
        ...(cue.words ? { words: [] } : {}),
        ...(cue.translation !== undefined ? { translation: null } : {}),
      },
    ];
  }

  function mergeCues(first, second) {
    if (!second) throw new Error("没有下一条字幕可以合并。");
    if (first.speaker && second.speaker && first.speaker !== second.speaker)
      throw new Error("不同说话人的字幕请先统一标注再合并。");
    return {
      ...first,
      ...(first.words || second.words
        ? {
            words:
              first.words?.length && second.words?.length
                ? [...first.words, ...second.words]
                : [],
          }
        : {}),
      ...(first.translation !== undefined || second.translation !== undefined
        ? {
            translation:
              first.translation && second.translation
                ? first.translation + "\n" + second.translation
                : null,
          }
        : {}),
      start: first.start,
      end: second.end,
      text: first.text + "\n" + second.text,
    };
  }

  function insertCue(cues, duration, time) {
    let start = Math.max(0, time || 0);
    let index = 0;
    while (index < cues.length && cues[index].start <= start) {
      start = Math.max(start, cues[index].end);
      index += 1;
    }
    const end = Math.min(start + 2, cues[index]?.start ?? duration, duration);
    if (!Number.isFinite(end) || end - start < 0.05) {
      throw new Error(
        "当前位置没有空闲时间，请先拆分字幕或将预览移到空白区间。",
      );
    }
    return { index, removed: [], inserted: [{ start, end, text: "新字幕" }] };
  }

  function applyEdit(cues, edit) {
    return [
      ...cues.slice(0, edit.index),
      ...edit.inserted,
      ...cues.slice(edit.index + edit.removed.length),
    ];
  }

  function inverseEdit(edit) {
    return {
      index: edit.index,
      removed: edit.inserted,
      inserted: edit.removed,
    };
  }

  function validCues(cues, duration, allowOverlap = false) {
    let lastEnd = 0,
      lastStart = 0;
    for (const cue of cues) {
      if (
        !Number.isFinite(cue.start) ||
        !Number.isFinite(cue.end) ||
        cue.start < 0 ||
        cue.start < lastStart ||
        (!allowOverlap && cue.start < lastEnd - 0.001) ||
        cue.end <= cue.start ||
        cue.end > duration + 0.05 ||
        !cue.text.trim()
      )
        return false;
      lastEnd = cue.end;
      lastStart = cue.start;
    }
    return true;
  }

  function shiftCues(cues, offset, duration, allowOverlap = false) {
    if (!Number.isFinite(offset)) throw new Error("偏移必须是有效秒数。");
    const shifted = cues.map((c) => ({
      ...c,
      start: c.start + offset,
      end: c.end + offset,
      words: (c.words || []).map((w) => ({
        ...w,
        start: w.start + offset,
        end: w.end + offset,
      })),
    }));
    if (!validCues(shifted, duration, allowOverlap))
      throw new Error("偏移后字幕超出媒体范围或时间无效。");
    return shifted;
  }

  function replaceText(cues, search, replacement) {
    if (!search) throw new Error("请输入查找文字。");
    return cues.map((c) => {
      const text = c.text.split(search).join(replacement);
      return text === c.text ? c : { ...c, text, words: [], translation: null };
    });
  }

  function quality(cues, settings, duration) {
    const options = settings.captions;
    let end = 0;
    return cues.flatMap((c, index) => {
      const messages = [];
      if (
        !Number.isFinite(c.start) ||
        !Number.isFinite(c.end) ||
        c.start < 0 ||
        c.end <= c.start ||
        c.end > duration + 0.05 ||
        !c.text.trim()
      )
        messages.push("内容或时间无效");
      if (c.start < end - 0.001)
        messages.push(settings.allow_overlap ? "重叠语音" : "时间冲突");
      end = Math.max(end, c.end || 0);
      const seconds = c.end - c.start;
      if (seconds < options.min_seconds) messages.push("显示时间过短");
      if (seconds > options.max_seconds) messages.push("显示时间过长");
      for (const text of [c.text, c.translation].filter(Boolean)) {
        const limit = /[\u3000-\u9fff\uac00-\ud7af]/.test(text)
          ? options.cjk_chars
          : options.latin_chars;
        if (
          text.split("\n").length > options.max_lines ||
          text.split("\n").some((l) => [...l].length > limit)
        )
          messages.push("行数或行长超限");
        if (text.replace(/\s/g, "").length / seconds > options.max_cps)
          messages.push("阅读速度过快");
      }
      return messages.length
        ? [{ index, messages: [...new Set(messages)] }]
        : [];
    });
  }

  return {
    shiftCues,
    replaceText,
    quality,
    activeIndex,
    splitCue,
    mergeCues,
    insertCue,
    applyEdit,
    inverseEdit,
    validCues,
  };
})();

if (typeof module !== "undefined") module.exports = CueEditor;
