"use strict";

// History stores navigation context only. All edits still pass the existing save guards.
let navigationIndex = Number.isInteger(history.state?.motifIndex) ? history.state.motifIndex : 0;
let navigationBusy = false, navigationWaiter = null;
function navigationPath() {
  return page === "library" ? "/" : `/campaigns/${draft.campaign_id}/${page === "review" ? "studio" : page}`;
}
function navigationView() {
  return { page, campaign: draft?.campaign_id, tab, lang, reviewTab: reviewTab === "product" ? "products" : reviewTab,
    selected: [...selected], formats: [...formats], imageMarket, pageNum, filter };
}
function retainNavigationView() {
  if (!navigationBusy && history.state?.motifIndex === navigationIndex)
    history.replaceState({ ...history.state, view: navigationView() }, "", location.href);
}
function recordNavigation() {
  if (navigationBusy) return;
  const path = navigationPath(), state = { motifIndex: navigationIndex, view: navigationView() };
  if (location.pathname === path) history.replaceState(state, "", path);
  else history.pushState({ ...state, motifIndex: ++navigationIndex }, "", path);
}
function traverseNavigation(index) {
  return new Promise(resolve => {
    navigationWaiter = { index, resolve };
    history.go(index - history.state.motifIndex);
  });
}
async function restoreNavigation(view) {
  if (!view || !["library", "campaign", "production", "review", "handoff"].includes(view.page)) return false;
  if (draft?.campaign_id !== view.campaign && view.page !== "library") {
    if (studioWorkspace && !await leaveStudioWorkspace()) return false;
    if (draft && !await flushCurrentWork()) return false;
    if (typeof guardBrandNavigation === "function" && guardBrandNavigation({ kind: "history" })) return false;
    await openCampaign(view.campaign, view.page);
  } else await navigate(view.page);
  if (page !== view.page || (page !== "library" && draft?.campaign_id !== view.campaign)) return false;
  tab = view.tab; lang = view.lang;
  reviewTab = view.reviewTab; selected = [...view.selected]; formats = [...view.formats];
  imageMarket = view.imageMarket; pageNum = view.pageNum; filter = view.filter;
  render();
  return true;
}
async function browserNavigation(event) {
  const index = event.state?.motifIndex;
  if (!Number.isInteger(index)) return;
  if (navigationWaiter) {
    if (index !== navigationWaiter.index) { history.go(navigationWaiter.index - index); return; }
    const done = navigationWaiter.resolve; navigationWaiter = null; done(); return;
  }
  if (index === navigationIndex) return;
  const target = event.state;
  // Restore the old URL before awaiting a save or confirmation. A refusal leaves
  // the producer on the original entry with its controls and pending work intact.
  if (navigationBusy) { history.go(navigationIndex - index); return; }
  navigationBusy = true;
  try {
    await traverseNavigation(navigationIndex);
    history.replaceState({ ...history.state, view: navigationView() }, "", location.href);
    if (await restoreNavigation(target.view)) {
      await traverseNavigation(target.motifIndex);
      navigationIndex = target.motifIndex;
    }
  } catch (error) { toast(error.message); }
  finally { navigationBusy = false; }
}
window.addEventListener("popstate", browserNavigation);
