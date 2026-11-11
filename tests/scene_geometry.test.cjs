const test = require("node:test");
const assert = require("node:assert/strict");
const geometry = require("../src/campaign_studio/static/motif/scene-geometry.js");

test("bounds hit testing selects CTA above headline above logo, including transparent space", () => {
  const blocks = {
    message: { bounds: [100, 100, 500, 200] },
    cta: { bounds: [200, 150, 600, 240] },
  };
  assert.equal(
    geometry.hitTest({ x: 250, y: 175 }, blocks, [0, 0, 400, 400]),
    "cta",
  );
  assert.equal(
    geometry.hitTest({ x: 150, y: 175 }, blocks, [0, 0, 400, 400]),
    "message",
  );
  assert.equal(
    geometry.hitTest({ x: 50, y: 50 }, blocks, [0, 0, 400, 400]),
    "logo",
  );
  assert.equal(
    geometry.hitTest({ x: 700, y: 50 }, blocks, [0, 0, 400, 400]),
    "framing",
  );
});
test("moving a layer clamps its bounds, preserves width, and does not mutate another layer", () => {
  const start = {
    id: "logo",
    bounds: [100, 100, 300, 200],
    placement: { left: 0.1, top: 0.1, width: 0.2 },
  };
  const moved = geometry.move(start, -1000, 2000, {
    width: 1000,
    height: 1000,
  });
  assert.deepEqual(moved.bounds, [0, 900, 200, 1000]);
  assert.deepEqual(moved.placement, { left: 0, top: 0.9, width: 0.2 });
  assert.deepEqual(start.bounds, [100, 100, 300, 200]);
});
test("all four text resize corners keep the opposite corner anchored", () => {
  for (const corner of ["nw", "ne", "sw", "se"]) {
    const start = {
      id: "message",
      bounds: [100, 100, 300, 200],
      placement: { left: 0.1, top: 0.1, width: 0.2, font_size: 40 },
    };
    const result = geometry.resize(
      start,
      corner,
      corner.endsWith("e") ? 20 : -20,
      corner.startsWith("s") ? 10 : -10,
      { width: 1000, height: 1000 },
    );
    assert.equal(
      result.bounds[corner.endsWith("e") ? 0 : 2],
      start.bounds[corner.endsWith("e") ? 0 : 2],
    );
    assert.equal(
      result.bounds[corner.startsWith("s") ? 1 : 3],
      start.bounds[corner.startsWith("s") ? 1 : 3],
    );
    assert.ok(result.placement.font_size > 40);
  }
});
