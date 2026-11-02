/* Motif E108: input and selection chrome only. The host supplies geometry.
   No persistence, preview transport, fonts, logo math, or modal controller. */
(function (scope) {
  "use strict";
  const copy = (value) => structuredClone(value);
  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
  function selectionFrame(rect, canvas) {
    const width = Math.min(canvas.width, Math.max(64, rect.width));
    const height = Math.min(canvas.height, Math.max(64, rect.height));
    return {
      left: clamp(
        rect.left + (rect.width - width) / 2,
        0,
        canvas.width - width,
      ),
      top: clamp(
        rect.top + (rect.height - height) / 2,
        0,
        canvas.height - height,
      ),
      width,
      height,
      expanded: width > rect.width + 0.01 || height > rect.height + 0.01,
    };
  }
  function bind(options) {
    const {
      element,
      canvas,
      read,
      write,
      move,
      resize,
      resizeKey,
      outputSize,
    } = options;
    let gesture = null,
      pending = null,
      raf = 0,
      destroyed = false;
    const controller = new AbortController(),
      events = { signal: controller.signal };
    const enabled = (kind) => !destroyed && (options.enabled?.(kind) ?? true);
    const flush = () => {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      if (pending !== null) {
        const next = pending;
        pending = null;
        write(next);
      }
    };
    const queue = (next) => {
      pending = next;
      if (!raf) raf = requestAnimationFrame(flush);
    };
    const finish = (cancelled = false) => {
      if (!gesture) return false;
      const g = gesture;
      gesture = null;
      if (cancelled) {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
        pending = null;
        write(copy(g.start));
      } else flush();
      if (element.hasPointerCapture?.(g.id))
        element.releasePointerCapture(g.id);
      element.classList.remove("is-dragging");
      options.onFinish?.({
        cancelled,
        start: copy(g.start),
        current: copy(read()),
      });
      return true;
    };
    element.addEventListener(
      "pointerdown",
      (event) => {
        if (
          !enabled() ||
          gesture ||
          event.button !== 0 ||
          event.isPrimary === false
        )
          return;
        const handle = event.target.closest("[data-mf-corner]");
        event.preventDefault();
        (handle || element).focus({ preventScroll: true });
        gesture = {
          start: copy(read()),
          corner: handle?.dataset.mfCorner || null,
          x: event.clientX,
          y: event.clientY,
          id: event.pointerId,
        };
        element.setPointerCapture(event.pointerId);
        element.classList.add("is-dragging");
        options.onStart?.();
      },
      events,
    );
    element.addEventListener(
      "pointermove",
      (event) => {
        const g = gesture;
        if (!g || event.pointerId !== g.id || !enabled()) return;
        const r = canvas.getBoundingClientRect(),
          size = outputSize();
        if (!r.width || !r.height) return;
        const dx = ((event.clientX - g.x) * size.width) / r.width,
          dy = ((event.clientY - g.y) * size.height) / r.height;
        queue(
          g.corner
            ? resize(copy(g.start), g.corner, dx, dy)
            : move(copy(g.start), dx, dy),
        );
      },
      events,
    );
    element.addEventListener(
      "pointerup",
      (event) => {
        if (gesture?.id === event.pointerId) finish();
      },
      events,
    );
    element.addEventListener(
      "pointercancel",
      (event) => {
        if (gesture?.id === event.pointerId) finish(true);
      },
      events,
    );
    element.addEventListener(
      "lostpointercapture",
      () => {
        if (gesture) finish(true);
      },
      events,
    );
    element.addEventListener(
      "keydown",
      (event) => {
        if (event.key === "Escape" && gesture) {
          event.preventDefault();
          event.stopPropagation();
          finish(true);
          return;
        }
        const corner = event.target.closest("[data-mf-corner]")?.dataset.mfCorner;
        if (
          !enabled(corner ? "resize-key" : "move-key") ||
          event.ctrlKey ||
          event.metaKey ||
          event.altKey ||
          !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(
            event.key,
          )
        )
          return;
        event.preventDefault();
        event.stopPropagation();
        flush();
        const step = event.shiftKey ? 10 : 1,
          dx =
            event.key === "ArrowLeft"
              ? -step
              : event.key === "ArrowRight"
                ? step
                : 0,
          dy =
            event.key === "ArrowUp"
              ? -step
              : event.key === "ArrowDown"
                ? step
                : 0;
        const start = copy(read());
        options.onStart?.();
        write(
          corner
            ? resizeKey(start, corner, { dx, dy, step, key: event.key })
            : move(start, dx, dy),
        );
        options.onFinish?.({
          cancelled: false,
          keyboard: true,
          start,
          current: copy(read()),
        });
      },
      events,
    );
    return {
      cancel: () => finish(true),
      active: () => !!gesture,
      destroy() {
        if (destroyed) return;
        finish(true);
        destroyed = true;
        controller.abort();
        if (raf) cancelAnimationFrame(raf);
        pending = null;
      },
    };
  }
  const api = { selectionFrame, bind };
  scope.MotifManipulation = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
