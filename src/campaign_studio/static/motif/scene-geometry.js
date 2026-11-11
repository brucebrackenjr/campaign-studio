/* Shared scene geometry. Bounds, rather than alpha sampling, own selection. */
(function (scope) {
  "use strict";
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  function hitTest(point, blocks, logo) {
    for (const id of ["cta", "message", "logo"]) {
      const bounds = id === "logo" ? logo : blocks[id]?.bounds;
      if (
        bounds &&
        point.x >= bounds[0] &&
        point.y >= bounds[1] &&
        point.x <= bounds[2] &&
        point.y <= bounds[3]
      )
        return id;
    }
    return "framing";
  }
  function move(start, dx, dy, size) {
    const [left, top, right, bottom] = start.bounds;
    dx = clamp(dx, -left, size.width - right);
    dy = clamp(dy, -top, size.height - bottom);
    return {
      ...start,
      bounds: [left + dx, top + dy, right + dx, bottom + dy],
      placement: {
        ...start.placement,
        left: clamp(start.placement.left + dx / size.width, 0, 1),
        top: clamp(start.placement.top + dy / size.height, 0, 1),
      },
    };
  }
  function resize(start, corner, dx, dy, size, keyboard = false) {
    const [left, top, right, bottom] = start.bounds,
      width = right - left,
      height = bottom - top;
    const sx = corner.endsWith("e") ? 1 : -1,
      sy = corner.startsWith("s") ? 1 : -1;
    const x = sx > 0 ? left : right,
      y = sy > 0 ? top : bottom;
    const max = Math.min(
      (sx > 0 ? size.width - x : x) / Math.max(1, width),
      (sy > 0 ? size.height - y : y) / Math.max(1, height),
    );
    const requested =
      1 +
      (sx * dx * width + sy * dy * height) /
        Math.max(1, width * width + height * height);
    let scale = clamp(
      requested,
      Math.min(1, 24 / Math.max(width, height)),
      max,
    );
    const placement = { ...start.placement };
    if (start.id !== "logo") {
      const font = placement.font_size;
      const next = Math.max(
        1,
        Math.min(
          65535,
          Math.floor(font * max),
          Math.round(keyboard ? font + sx * dx + sy * dy : font * scale),
        ),
      );
      scale = next / font;
      placement.font_size = next;
    }
    placement.left = clamp(
      (x + (placement.left * size.width - x) * scale) / size.width,
      0,
      1,
    );
    placement.top = clamp(
      (y + (placement.top * size.height - y) * scale) / size.height,
      0,
      1,
    );
    placement.width = clamp(placement.width * scale, 0.0001, 1);
    return {
      ...start,
      placement,
      bounds: [
        x + (left - x) * scale,
        y + (top - y) * scale,
        x + (right - x) * scale,
        y + (bottom - y) * scale,
      ],
    };
  }
  scope.MotifSceneGeometry = { hitTest, move, resize };
  if (typeof module !== "undefined") module.exports = scope.MotifSceneGeometry;
})(typeof window === "undefined" ? globalThis : window);
