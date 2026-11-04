/* Fit complete campaign identities using the font actually rendered here. */
(function (scope) {
  "use strict";
  function fit(heading = document.querySelector(".campaign-heading")) {
    if (heading) {
      const rows = [...heading.querySelectorAll(".campaign-heading-brand,.campaign-heading-name")];
      for (const row of rows) row.style.fontSize = "";
      const style = getComputedStyle(heading), caret = heading.querySelector(".campaign-heading-caret");
      const caretStyle = getComputedStyle(caret);
      const available = heading.parentElement.getBoundingClientRect().width
        - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - parseFloat(style.columnGap)
        - caret.getBoundingClientRect().width - parseFloat(caretStyle.marginLeft) - parseFloat(caretStyle.marginRight) - 2;
      if (available > 0) for (const row of rows) {
        if (row.hidden) continue;
        const range = document.createRange();
        range.selectNodeContents(row);
        if (range.getBoundingClientRect().width <= available) continue;
        let low = 1, high = parseFloat(getComputedStyle(row).fontSize);
        // Font fallback and emoji widths differ across systems. Measure each
        // candidate rather than assuming that character count predicts width.
        for (let step = 0; step < 10; step++) {
          const size = (low + high) / 2;
          row.style.fontSize = `${size}px`;
          if (range.getBoundingClientRect().width <= available) low = size;
          else high = size;
        }
        row.style.fontSize = `${Math.floor(low * 100) / 100}px`;
      }
    }
    const topbar = document.querySelector(".topbar");
    if (topbar) document.documentElement.style.setProperty("--motif-header-height", `${topbar.getBoundingClientRect().height}px`);
  }
  let frame = 0;
  function schedule() {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; fit(); });
  }
  scope.addEventListener("resize", schedule);
  document.fonts?.ready.then(schedule);
  document.fonts?.addEventListener("loadingdone", schedule);
  scope.MotifIdentityLayout = {fit};
})(window);
