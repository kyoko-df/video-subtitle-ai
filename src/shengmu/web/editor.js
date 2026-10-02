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
      { start: cue.start, end: cut, text: left },
      { start: cut, end: cue.end, text: right },
    ];
  }

  function mergeCues(first, second) {
    if (!second) throw new Error("没有下一条字幕可以合并。");
    return {
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

  function validCues(cues, duration) {
    let lastEnd = 0;
    for (const cue of cues) {
      if (
        !Number.isFinite(cue.start) ||
        !Number.isFinite(cue.end) ||
        cue.start < 0 ||
        cue.start < lastEnd - 0.001 ||
        cue.end <= cue.start ||
        cue.end > duration + 0.05 ||
        !cue.text.trim()
      )
        return false;
      lastEnd = cue.end;
    }
    return true;
  }

  return {
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
