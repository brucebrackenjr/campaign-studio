"use strict";

// Campaign fields and Studio settings share the current saved composition.
// Color resets remain explicit creative choices, then save through /authored.
let brandUpdating = false;
function brandColorTargets() { return projectBuffers.form_drafts.brand_color_defaults || []; }
function brandChangesPending() { return false; }
function guardBrandNavigation() { return false; }
async function updateBrandImages() { return saveProject(); }
