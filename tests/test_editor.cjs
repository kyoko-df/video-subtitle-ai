const test = require("node:test");
const assert = require("node:assert/strict");
const editor = require("../src/shengmu/web/editor.js");

const cues = [
  { start: 0, end: 2, text: "hello world" },
  { start: 3, end: 5, text: "next line" },
];

test("active cue uses half-open boundaries and cached position", () => {
  assert.equal(editor.activeIndex(cues, 1, -1), 0);
  assert.equal(editor.activeIndex(cues, 2, 0), -1);
  assert.equal(editor.activeIndex(cues, 3, 0), 1);
  assert.equal(editor.activeIndex(cues, 0.1, 1), 0);
  assert.equal(editor.activeIndex([], 0), -1);
});

test("split respects text cursor and playback time", () => {
  const parts = editor.splitCue(cues[0], 6, 1.2);
  assert.deepEqual(parts, [
    { start: 0, end: 1.2, text: "hello" },
    { start: 1.2, end: 2, text: "world" },
  ]);
  assert.throws(() => editor.splitCue(cues[0], 0, 1));
  assert.throws(() =>
    editor.splitCue({ start: 0, end: 0.001, text: "ab" }, 1, 0),
  );
});

test("merge keeps multiline text and outer timestamps", () => {
  assert.deepEqual(editor.mergeCues(...cues), {
    start: 0,
    end: 5,
    text: "hello world\nnext line",
  });
  assert.throws(() => editor.mergeCues(cues[0], undefined));
});

test("new cues fill a gap and never overlap", () => {
  const edit = editor.insertCue(cues, 6, 1);
  assert.equal(edit.index, 1);
  assert.deepEqual(edit.inserted, [{ start: 2, end: 3, text: "新字幕" }]);
  assert.throws(() => editor.insertCue(cues, 5, 5));
  assert.deepEqual(editor.insertCue([], 2, 0).inserted, [
    { start: 0, end: 2, text: "新字幕" },
  ]);
});

test("patch-based undo preserves unrelated cue objects", () => {
  const edit = {
    index: 0,
    removed: [cues[0]],
    inserted: editor.splitCue(cues[0], 6, 1),
  };
  const changed = editor.applyEdit(cues, edit);
  assert.equal(changed[2], cues[1]);
  assert.deepEqual(editor.applyEdit(changed, editor.inverseEdit(edit)), cues);
  assert.equal(cues.length, 2);
});

test("invalid timings cannot be saved", () => {
  assert.equal(editor.validCues(cues, 5), true);
  assert.equal(editor.validCues([{ start: NaN, end: 2, text: "x" }], 5), false);
  assert.equal(editor.validCues([{ start: 0, end: 2, text: " " }], 5), false);
  assert.equal(editor.validCues([cues[1], cues[0]], 5), false);
  assert.equal(editor.validCues(cues, 4), false);
});

test("binary lookup handles a movie-sized timeline", () => {
  const movie = Array.from({ length: 2000 }, (_, i) => ({
    start: i * 3,
    end: i * 3 + 2,
    text: String(i),
  }));
  assert.equal(editor.activeIndex(movie, 5997), 1999);
  assert.equal(editor.activeIndex(movie, 5999), -1);
});

test("shift updates all word timestamps and refuses to lose out-of-range cues", () => {
  const data = [
    {
      start: 1,
      end: 2,
      text: "word",
      words: [{ start: 1, end: 2, text: "word" }],
      speaker: "A",
    },
  ];
  const shifted = editor.shiftCues(data, 1, 4);
  assert.equal(shifted[0].words[0].start, 2);
  assert.equal(data[0].start, 1);
  assert.throws(() => editor.shiftCues(data, -2, 4));
  assert.throws(() => editor.shiftCues(data, NaN, 4));
});

test("replacement clears stale alignment and translation, supports undo", () => {
  const original = {
    start: 0,
    end: 1,
    text: "Hello Hello",
    translation: "你好",
    words: [{ start: 0, end: 1, text: "Hello Hello" }],
  };
  const unchanged = { start: 2, end: 3, text: "world" };
  const next = editor.replaceText([original, unchanged], "Hello", "Hi");
  assert.equal(next[0].text, "Hi Hi");
  assert.equal(next[0].translation, null);
  assert.deepEqual(next[0].words, []);
  assert.equal(next[1], unchanged);
  assert.throws(() => editor.replaceText([original], "", "Hi"));
  assert.deepEqual(
    editor.applyEdit(
      next,
      editor.inverseEdit({
        index: 0,
        removed: [original],
        inserted: [next[0]],
      }),
    ),
    [original, unchanged],
  );
});

test("speaker labels survive splitting and different speakers cannot be merged", () => {
  const original = { ...cues[0], speaker: "A", translation: "你好", words: [] };
  assert.equal(editor.splitCue(original, 6, 1)[0].speaker, "A");
  assert.equal(editor.splitCue(original, 6, 1)[0].translation, null);
  assert.throws(() => editor.mergeCues(original, { ...cues[1], speaker: "B" }));
});

test("overlap is explicit and quality checks both languages", () => {
  const data = [
    { start: 0, end: 2, text: "a", translation: "x".repeat(80) },
    { start: 1, end: 3, text: "b" },
  ];
  assert.equal(editor.validCues(data, 4), false);
  assert.equal(editor.validCues(data, 4, true), true);
  const options = {
    captions: {
      cjk_chars: 20,
      latin_chars: 42,
      max_lines: 2,
      min_seconds: 1,
      max_seconds: 7,
      max_cps: 20,
    },
    allow_overlap: true,
  };
  const issues = editor.quality(data, options, 4);
  assert.equal(issues.length, 2);
  assert.ok(issues[0].messages.includes("阅读速度过快"));
  assert.ok(issues[1].messages.includes("重叠语音"));
});
