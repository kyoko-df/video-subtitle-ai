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
