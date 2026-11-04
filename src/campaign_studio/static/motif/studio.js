"use strict";

// Studio selections are navigation state. Saved pixels and descriptions come
// from the server’s current accepted output.
function workbenchEmptyImage(campaign) {
  return campaign.saved_expected_images === 0 && campaign.output_expected === false && !campaign.damaged_images
    ? "Images not produced yet"
    : "Image unavailable";
}

function workbenchCampaigns() {
  return saved
    .map((campaign, index) => ({ campaign, index }))
    .sort((a, b) => {
      const time = (value) =>
        Number.isFinite(Date.parse(value)) ? Date.parse(value) : -Infinity;
      return (
        time(b.campaign.updated) - time(a.campaign.updated) || a.index - b.index
      );
    })
    .map((item) => item.campaign);
}
function activityLabel(value) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime())
    ? date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
    : "Saved on this device";
}
function workbenchStatus(campaign) {
  if (campaign.status === "damaged" || campaign.damaged_images) return "Needs recovery";
  if (campaign.outcome_unknown) return "Result not confirmed";
  return (
    {
      complete: "Ready to review",
      partial: "Partial",
      paused: "Paused",
      stopped: "Stopped",
      running: "In progress",
      planned: "Plan ready",
      confirmed: "Ready to start",
      draft: "Draft",
    }[campaign.status] || "Needs attention"
  );
}
function workbenchCounts(campaign) {
  return `${campaign.available_images ?? 0} available · ${campaign.planned_images ?? campaign.products * campaign.markets * 3} planned`;
}
function syncWorkbench() {
  const track = $("#workbench-track");
  if (!track) return;
  const max = track.scrollWidth - track.clientWidth;
  $('[data-action="workbench:previous"]').disabled = track.scrollLeft < 2;
  $('[data-action="workbench:next"]').disabled =
    max < 2 || track.scrollLeft >= max - 2;
}
function moveWorkbench(direction, oneCard = false) {
  const track = $("#workbench-track");
  const card = track?.querySelector(".campaign-card");
  if (!card) return;
  const step =
    card.getBoundingClientRect().width +
    parseFloat(getComputedStyle(track).columnGap);
  const distance =
    step * (oneCard ? 1 : Math.max(1, Math.floor(track.clientWidth / step)));
  track.scrollBy({
    left: direction * distance,
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
      ? "instant"
      : "smooth",
  });
}
function bindWorkbench() {
  const track = $("#workbench-track");
  if (!track) return;
  track.addEventListener("scroll", syncWorkbench, { passive: true });
  track.addEventListener("focusin", (event) => {
    const card = event.target.closest(".workbench-item, .campaign-card[data-action=new]");
    if (!card || !track.contains(card)) return;
    const viewport = track.getBoundingClientRect();
    const bounds = card.getBoundingClientRect();
    const inset = 8;
    const distance =
      bounds.left < viewport.left + inset
        ? bounds.left - viewport.left - inset
        : bounds.right > viewport.right - inset
          ? bounds.right - viewport.right + inset
          : 0;
    if (distance)
      track.scrollBy({
        left: distance,
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "instant"
          : "smooth",
      });
  });
  track.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (["Home", "End"].includes(event.key))
      track.scrollTo({
        left: event.key === "Home" ? 0 : track.scrollWidth,
        behavior: "instant",
      });
    else moveWorkbench(event.key === "ArrowRight" ? 1 : -1, true);
  });
  syncWorkbench();
}
window.addEventListener("resize", syncWorkbench);

// A file may disappear after the catalog response. Keep the campaign usable and
// show an intentional empty state without borrowing another product’s artwork.
document.addEventListener(
  "error",
  (event) => {
    if (event.target.matches?.("img[data-workbench-thumbnail]"))
      event.target.outerHTML =
        '<span class="awaiting-art">Image unavailable</span>';
  },
  true,
);
