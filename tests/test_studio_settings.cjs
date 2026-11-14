const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

test("partial result and draft settings preserve the job's transcription controls", () => {
  const controls = new Map();
  const $ = (id) => {
    if (!controls.has(id))
      controls.set(id, { value: "", checked: false, addEventListener() {} });
    return controls.get(id);
  };
  const studio = {
    $,
    state: {},
    window: { addEventListener() {} },
    document: { querySelectorAll: () => [] },
    updateButtons() {},
  };
  vm.runInNewContext(
    fs.readFileSync(
      path.join(__dirname, "../src/shengmu/web/studio.js"),
      "utf8",
    ),
    studio,
  );

  studio.applySettings({ asr_chunk_seconds: 30, resume: false });
  studio.applySettings({ asr_backend: "mlx", captions: { max_lines: 2 } });
  studio.applySettings({ export_mode: "original", segments: [] });
  studio.applySettings();
  assert.equal($("asr-chunk-seconds").value, 30);
  assert.equal($("asr-resume").checked, false);

  studio.applySettings({ asr_chunk_seconds: 120, resume: true });
  assert.equal($("asr-chunk-seconds").value, 120);
  assert.equal($("asr-resume").checked, true);
});
