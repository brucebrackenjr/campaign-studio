"use strict";

// A product visit owns the scene. Tool selection never owns persistence or pixels.
class StudioScene {
  constructor(owner) {
    this.owner = owner;
    owner.scene = this;
    this.sequence = 0;
    this.events = new AbortController();
    this.layers = null;
    this.blocks = {};
    this.selected = "message";
    this.ready = false;
    this.closed = false;
    this.previewTimer = null;
    this.previewError = null;
    this.gesture = null;
    this.scopeSequence = 0;
    this.pending = false;
    this.queued = false;
    this.prepareTarget();
    this.mount();
    this.createSaveQueue();
    const held = owner.scenes?.get(this.sceneKey());
    if (held) {
      this.layers = held.layers;
      this.blocks = copy(held.blocks);
      this.logo = copy(held.logo);
      this.ready = true;
      this.paint();
    }
    this.setMode(owner.mode);
    if (!held) this.refresh();
  }
  sceneKey() {
    return studioThumbnailSignature(
      outputFor(currentProduct, lang, this.format),
      this.format,
    );
  }
  get pendingStyles() {
    return this.state.pendingStyles;
  }
  resource(url, key) {
    this.owner.resources ||= new Map();
    if (!this.owner.resources.has(key)) {
      const promise = decodedImage(url).catch((error) => {
        this.owner.resources.delete(key);
        throw error;
      });
      this.owner.resources.set(key, promise);
      while (this.owner.resources.size > 8)
        this.owner.resources.delete(this.owner.resources.keys().next().value);
    }
    return this.owner.resources.get(key);
  }
  current() {
    return (
      !this.closed &&
      studioWorkspace === this.owner &&
      this.owner.scene === this
    );
  }
  prepareTarget() {
    this.product = this.owner.product;
    this.market = MARKET[lang];
    this.format = currentFormat;
    this.size = FMT[this.format];
    this.target = `${this.product}/${this.market}/${this.size.ratio.replace(":", "x")}`;
    const product = model.output_brief.products.find(
      (p) => p.id === this.product,
    );
    this.prepared = product.source.role === "prepared_product";
    this.sourceSubject = copy(product.source.subject_box || null);
    this.state = {
      framing: {
        focal_x: 0.5,
        focal_y: 0.5,
        zoom: 1,
        copy_placement: "top",
        subject_box: null,
        inherit_subject: true,
        logo_placement: null,
        text_placements: {},
        ...copy(product.framing?.[this.market]?.[this.size.ratio] || {}),
      },
      values: {
        message: String(valueFor(currentProduct, lang, "headline") || ""),
        cta: String(valueFor(currentProduct, lang, "cta") || ""),
      },
      scopes: {},
      pendingStyles: {},
      asset: null,
      description: model.metadata?.creatives?.[this.target]?.description || "",
    };
    this.saved = copy(this.state);
    this.brand = copy(model.output_brief.brand);
  }
  mount() {
    const slot = $('[data-ws-slot="surface"]', this.owner.root);
    slot.innerHTML = `<div class="ws-scene-stage"><div class="ws-scene-output"><canvas class="ws-scene-canvas" width="${this.size.w}" height="${this.size.h}" aria-label="Editable campaign image"></canvas>${MotifEditorControls.selection("Headline")}<div class="ws-scene-missing" hidden><strong>Your logo belongs here.</strong><button type="button" class="btn" data-scene-action="upload">Attach a logo</button></div><div class="ws-scene-safety" tabindex="0" role="group" aria-label="Marked product area. Arrow keys move it; Shift and arrows resize it." hidden><span>PRODUCT AREA</span></div></div></div>`;
    this.stage = $(".ws-scene-stage", slot);
    this.output = $(".ws-scene-output", slot);
    this.canvas = $(".ws-scene-canvas", slot);
    this.selection = $(".mf-direct-selection", slot);
    this.output.tabIndex = 0;
    this.selection.hidden = true;
    this.resizeObserver = new ResizeObserver(() => this.sizeSurface());
    this.resizeObserver.observe(this.stage);
    this.sizeSurface();
    const events = { signal: this.events.signal };
    this.output.addEventListener(
      "pointerdown",
      (event) => this.pointerDown(event),
      events,
    );
    this.output.addEventListener(
      "pointermove",
      (event) => this.pointerMove(event),
      events,
    );
    this.output.addEventListener(
      "pointerup",
      (event) => this.pointerEnd(event),
      events,
    );
    this.output.addEventListener(
      "pointercancel",
      (event) => this.pointerEnd(event, true),
      events,
    );
    this.output.addEventListener(
      "lostpointercapture",
      (event) => {
        if (this.gesture) this.pointerEnd(event, true);
      },
      events,
    );
    this.output.addEventListener(
      "keydown",
      (event) => this.keyDown(event),
      events,
    );
    this.owner.root.addEventListener(
      "click",
      (event) => this.click(event),
      events,
    );
    this.owner.root.addEventListener(
      "input",
      (event) => this.input(event),
      events,
    );
    this.owner.root.addEventListener(
      "change",
      (event) => this.input(event),
      events,
    );
    // Keep the already visible format while the first layer packet is prepared.
    const output = outputFor(currentProduct, lang, this.format);
    if (output?.url && !this.owner.scenes?.has(this.sceneKey()))
      decodedImage(output.gallery_url || output.url)
        .then((image) => {
          if (this.current() && !this.layers)
            this.canvas
              .getContext("2d")
              .drawImage(image, 0, 0, this.size.w, this.size.h);
        })
        .catch(() => {});
  }
  sizeSurface() {
    const width = Math.max(
      1,
      Math.min(
        this.stage.clientWidth,
        (this.stage.clientHeight * this.size.w) / this.size.h,
      ),
    );
    this.output.style.width = `${width}px`;
    this.output.style.height = `${(width * this.size.h) / this.size.w}px`;
    this.paintSelection();
  }
  body(state = this.state, { preview = false } = {}) {
    const body = {
      revision_id: model.composition_version || model.version,
      product_id: this.product,
      market: this.market,
      ratio: this.size.ratio,
      values: {},
      field_scopes: {},
    };
    if (
      preview ||
      JSON.stringify(state.framing) !== JSON.stringify(this.saved.framing)
    )
      body.framing = copy(state.framing);
    for (const field of ["message", "cta"]) {
      if (
        preview ||
        state.values[field] !== this.saved.values[field] ||
        JSON.stringify(state.scopes[field]) !==
          JSON.stringify(this.saved.scopes[field])
      ) {
        body.values[field] =
          field === "cta" ? state.values[field] || null : state.values[field];
        body.field_scopes[field] = state.scopes[field]?.kind || "product";
      }
    }
    if (
      state.asset &&
      (preview || state.asset.reference !== this.saved.asset?.reference)
    ) {
      body.replacement = state.asset.reference;
      body.provenance = state.asset.provenance;
    }
    if (!preview && state.description !== this.saved.description)
      body.description_edit = {
        description: state.description || null,
        expected_version:
          model.metadata?.creatives?.[this.target]?.version || 0,
      };
    return body;
  }
  createSaveQueue() {
    const scene = this;
    this.queue = compositionSaveQueue({
      capture: () => copy(scene.state),
      prepare: async () => {
        if (scene.gesture || scene.uploading || scene.lengthIssue())
          return false;
        if (
          Object.keys(scene.pendingStyles).some((id) =>
            scene.state.values[id].trim(),
          )
        ) {
          await scene.refresh();
          // A style chosen during the first request invalidates that packet.
          // Wait for a current packet before deciding that preparation failed.
          if (
            !scene.previewError &&
            Object.keys(scene.pendingStyles).some((id) =>
              scene.state.values[id].trim(),
            )
          )
            await scene.refresh();
          if (
            Object.keys(scene.pendingStyles).some((id) =>
              scene.state.values[id].trim(),
            )
          ) {
            const error = new Error(
              "The text preview could not be prepared. Retry the preview to keep your style choices.",
            );
            scene.status(error.message, true);
            throw error;
          }
        }
        return true;
      },
      changed: (state) => {
        if (scene.current()) paintStudioSave(state);
      },
      send: async (attempt) => {
        if (!(await saveProject()))
          throw Object.assign(
            new Error(
              getSaveState()?.problem?.phase === "conflict"
                ? MotifSave.conflictMessage
                : "Campaign changes could not be saved.",
            ),
            { status: getSaveState()?.problem?.phase === "conflict" ? 409 : 0 },
          );
        if (!scene.current()) throw new Error("The editing target changed.");
        attempt.request ||= scene.body(attempt.captured);
        return api(
          `/api/campaigns/${scene.owner.campaign}/scene/save`,
          attempt.request,
        );
      },
      accept: (response, submitted) => {
        if (!scene.current()) return;
        const data = mergeStudioAcknowledgement(model, response);
        scene.saved = copy(submitted);
        useData(data, { applied: true });
        scene.brand = copy(data.output_brief.brand);
        // The saved version changes, but the same local scene and any newer input remain.
        scene.publishThumbnail();
        refreshStudioTargets();
        paintWordingAvailability();
      },
    });
    studioSave = this.queue;
  }
  lengthIssue() {
    return (
      MotifTextLimits.issue("mf-word-message", this.state.values.message) ||
      MotifTextLimits.issue("mf-word-cta", this.state.values.cta) ||
      MotifTextLimits.issue("image-description", this.state.description)
    );
  }
  refresh() {
    if (this.pending) return this.previewWork;
    this.previewWork = this.performRefresh();
    return this.previewWork;
  }
  async performRefresh() {
    if (!this.current() || this.gesture) return;
    if (this.pending) {
      this.queued = true;
      return;
    }
    if (this.lengthIssue()) {
      this.status(this.lengthIssue());
      return;
    }
    this.pending = true;
    const sequence = this.sequence;
    const revision = model.composition_version || model.version;
    try {
      const result = await api(
        `/api/campaigns/${this.owner.campaign}/scene/preview`,
        this.body(this.state, { preview: true }),
      );
      const [source, logo, blocks] = await Promise.all([
        this.resource(result.source.url, result.source.key),
        result.logo ? this.resource(result.logo.url, result.logo.key) : null,
        Promise.all(
          Object.entries(result.text_blocks).map(async ([id, block]) => [
            id,
            { ...block, image: await decodedImage(block.url) },
          ]),
        ),
      ]);
      if (!this.current() || sequence !== this.sequence || this.gesture) return;
      // A save may acknowledge the same scene while this read is running. Its pixels
      // are still valid if input did not change; version never authorizes a write here.
      this.layers = {
        source,
        logo,
        blocks: Object.fromEntries(blocks),
        prepared: result.source.prepared,
      };
      this.blocks = copy(result.text_blocks);
      this.logo = result.logo ? copy(result.logo) : null;
      this.ready = true;
      this.previewError = null;
      this.findings = result.findings;
      this.rasterDirty = false;
      let styled = false;
      for (const [id, style] of Object.entries(this.pendingStyles))
        if (this.blocks[id]) {
          this.state.framing.text_placements[id] = {
            ...this.blocks[id].placement,
            ...style,
          };
          delete this.pendingStyles[id];
          styled = true;
        }
      if (styled) this.changed({ raster: true });
      this.paint();
      this.paintControlsState();
      this.publishThumbnail();
      this.status("");
    } catch (error) {
      if (!this.current() || sequence !== this.sequence) return;
      if (
        error.status === 409 &&
        revision !== (model.composition_version || model.version)
      )
        this.queued = true;
      else {
        this.previewError = error;
        this.status(error.message, true);
      }
    } finally {
      this.pending = false;
      if (this.current() && (this.queued || sequence !== this.sequence)) {
        this.queued = false;
        this.schedulePreview();
      }
    }
  }
  schedulePreview() {
    clearTimeout(this.previewTimer);
    this.previewTimer = setTimeout(() => this.refresh(), 180);
  }
  changed({ raster = false, immediate = false, provisional = false } = {}) {
    this.sequence++;
    if (raster) this.rasterDirty = true;
    this.queue.schedule(immediate ? 0 : 1000);
    this.paint();
    this.paintControlsState();
    // A pending raster can be skipped or discarded while a pointer is held.
    // Resume it after the gesture, even when that gesture only moved a layer.
    if (raster || this.rasterDirty) this.schedulePreview();
    if (!this.gesture) this.publishThumbnail({ provisional });
  }
  status(message, retry = false) {
    if (!this.current()) return;
    const foot = $('[data-ws-slot="commit"]', this.owner.root);
    foot.hidden = !message;
    foot.innerHTML = `<p role="status" data-ws-slot="status">${esc(message)}</p>${retry ? '<button type="button" class="btn small" data-scene-action="retry">Retry preview</button>' : ""}`;
  }
  setMode(mode, block) {
    if (this.gesture) return;
    const leaveMarking = this.marking && mode !== "framing";
    if (leaveMarking) this.marking = false;
    this.owner.mode = mode;
    modal = mode === "framing" ? "frame" : mode;
    if (block) this.selected = block;
    for (const button of $$("[data-ws-mode]", this.owner.root))
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.wsMode === mode),
      );
    $('[data-ws-slot="mode-title"]', this.owner.root).textContent = {
      inspect: "Inspect",
      framing: "Framing",
      logo: "Logo",
      text: "Text",
    }[mode];
    $('[data-ws-slot="target"]', this.owner.root).textContent =
      `${LANG[lang]} · ${this.size.name}`;
    this.controls();
    this.paintSelection();
    if (leaveMarking) this.paint();
    else this.paintSafety();
    $(".ws-scene-missing", this.output).hidden =
      mode !== "logo" || !!this.brand.logo_path || !!this.state.asset;
    this.output.dataset.mode = mode;
    if (typeof retainStudioNavigation === "function") retainStudioNavigation();
  }
  controls() {
    const root = $('[data-ws-slot="controls"]', this.owner.root),
      frame = this.state.framing;
    if (this.owner.mode === "text") {
      root.innerHTML = `<div class="mf-editor-tools"><div class="mf-blocks ws-blocks" role="group" aria-label="Text block">${[
        ["message", "Headline"],
        ["cta", "Call to action"],
      ]
        .map(
          ([id, label]) =>
            `<button type="button" data-scene-block="${id}" aria-pressed="${id === this.selected}"><strong>${label}</strong></button>`,
        )
        .join(
          "",
        )}</div><div class="mf-combined-wording">${MotifEditorControls.wordingFields({ values: this.state.values })}</div><div id="scene-text-style"></div><div class="mf-instructions"><strong>Drag to move. Pull a corner to resize.</strong><p>Arrows move; Shift moves ten pixels.</p><p>Tab to a corner; arrows resize.</p></div><button type="button" class="btn" data-scene-action="reset-text">Reset text layout</button></div>`;
      this.textControls();
    } else if (this.owner.mode === "logo") {
      root.innerHTML = `<div class="mf-logo-tools"><p>${this.brand.logo_path || this.state.asset ? "Drag to move. Pull a corner to resize." : "Attach your campaign logo to place it on this image."}</p><p class="small muted">Arrows move; Shift moves ten pixels. Tab to a corner to resize.</p><button type="button" class="btn" data-scene-action="upload">${this.brand.logo_path || this.state.asset ? "Replace logo" : "Attach a logo"}</button><input type="file" data-scene-upload accept="image/png,image/jpeg" hidden><button type="button" class="btn" data-scene-action="reset-logo" ${!this.logo ? "disabled" : ""}>Reset logo placement</button></div>`;
    } else if (this.owner.mode === "framing") {
      root.innerHTML = this.prepared
        ? "<p>This prepared product uses a separate background. Photo framing is unavailable for this source.</p>"
        : `<div class="framing-tools mf-editor-tools">${framingControl("Zoom", "scene-zoom", frame.zoom * 100, 100, 200, `${Math.round(frame.zoom * 100)}%`, "100% fills this format. Zoom in to reframe.")}${framingControl("Horizontal position", "scene-focal", frame.focal_x * 100, 0, 100, framePositionLabel(frame.focal_x * 100, "horizontal"))}${framingControl("Vertical position", "scene-vertical", frame.focal_y * 100, 0, 100, framePositionLabel(frame.focal_y * 100, "vertical"))}<div class="field"><label for="scene-copy-placement">Copy placement</label><select id="scene-copy-placement" ${Object.keys(frame.text_placements).length ? "disabled" : ""}><option value="top" ${frame.copy_placement === "top" ? "selected" : ""}>Top left</option><option value="bottom" ${frame.copy_placement === "bottom" ? "selected" : ""}>Bottom left</option></select></div><section class="framing-protection"><strong>Product area</strong><label><input type="checkbox" id="scene-show-safety" ${this.showSafety ? "checked" : ""}> Show safety area</label><p class="small muted">Mark the product area to protect it when cropping.</p><button type="button" class="btn small" data-scene-action="mark">${this.marking ? "Finish marking" : "Mark area"}</button><button type="button" class="btn small" data-scene-action="clear-area">Clear</button></section><button type="button" class="btn" data-scene-action="reset-frame">Reset framing</button></div>`;
    } else {
      const template = document.createElement("template");
      template.innerHTML = inspectorModal();
      root.innerHTML = template.content.querySelector(
        ".large-inspector > div:last-child",
      ).innerHTML;
      const description = $("#image-description", root);
      if (description) description.value = this.state.description;
    }
    this.paintControlsState();
    if (this.queue?.state().error)
      paintStudioSave({
        phase: this.queue.state().error.status === 409 ? "conflict" : "error",
        error: this.queue.state().error,
      });
  }
  textControls() {
    const root = $('[data-ws-slot="controls"]', this.owner.root);
    const placement = {
      ...(this.state.framing.text_placements[this.selected] ||
        this.blocks[this.selected]?.placement ||
        {}),
      ...this.pendingStyles[this.selected],
    };
    const style = $("#scene-text-style", root);
    if (style)
      style.innerHTML = MotifEditorControls.textStyleControls({
        fontId: placement.font_id || "noto_sans",
        color: placement.color,
        campaignColor: this.brand.text_color,
        palette: this.brand.palette,
      });
    for (const field of $$("[data-mf-field]", root))
      field.hidden = field.dataset.mfField !== this.selected;
    for (const button of $$("[data-scene-block]", root))
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.sceneBlock === this.selected),
      );
  }
  paintControlsState() {
    if (!this.current()) return;
    const framingTools = $(".framing-tools", this.owner.root);
    if (framingTools && this.layers && !this.prepared) {
      const f = this.state.framing,
        source = this.layers.source;
      const crop = frameCrop(
        source.naturalWidth,
        source.naturalHeight,
        this.size,
        { zoom: f.zoom, focal: f.focal_x * 100, vertical: f.focal_y * 100 },
      );
      for (const [id, value, available, axis] of [
        ["scene-zoom", f.zoom * 100, true, null],
        ["scene-focal", crop.focal, crop.rangeX > 0.5, "horizontal"],
        ["scene-vertical", crop.vertical, crop.rangeY > 0.5, "vertical"],
      ]) {
        const input = $(`#${id}`, framingTools);
        input.value = value;
        input.disabled = !available;
        $(`#${id}-value`, framingTools).textContent = axis
          ? available
            ? framePositionLabel(value, axis)
            : "At fit"
          : `${Math.round(value)}%`;
      }
      const box =
        f.subject_box || (f.inherit_subject ? this.sourceSubject : null);
      const safe = frameSubjectFits(
        crop,
        box && {
          x: box.left,
          y: box.top,
          w: box.right - box.left,
          h: box.bottom - box.top,
        },
        source.naturalWidth,
        source.naturalHeight,
      );
      let warning = $("[data-scene-crop-warning]", framingTools);
      if (!warning) {
        warning = document.createElement("p");
        warning.dataset.sceneCropWarning = "";
        warning.className = "mw-field-error";
        warning.setAttribute("role", "status");
        framingTools.append(warning);
      }
      warning.hidden = safe || this.marking;
      warning.textContent = safe
        ? ""
        : "Crop cuts the marked product area. Zoom out or reposition before downloading.";
    }
    const rules = (model.output_brief || draft).markets[this.market]
      ?.phrase_rules;
    for (const field of ["message", "cta"]) {
      const input = $(`#mf-word-${field}`, this.owner.root);
      if (!input) continue;
      const terms = MotifWording.matchingTerms(this.state.values[field], rules);
      let note = $(`#scene-${field}-error`, this.owner.root);
      const message =
        MotifTextLimits.issue(`mf-word-${field}`, this.state.values[field]) ||
        (terms.length
          ? `Remove ${terms.map((t) => `“${t}”`).join(", ")} or edit the prohibited words.`
          : "");
      if (message && !note) {
        note = document.createElement("p");
        note.id = `scene-${field}-error`;
        note.className = "mw-field-error";
        input.after(note);
      }
      if (note) {
        note.textContent = message;
        note.hidden = !message;
      }
      if (message) {
        input.setAttribute("aria-invalid", "true");
        input.setAttribute("aria-describedby", note.id);
      } else {
        input.removeAttribute("aria-invalid");
        input.removeAttribute("aria-describedby");
      }
    }
  }
  paint() {
    if (!this.layers) return;
    const ctx = this.canvas.getContext("2d"),
      { w, h } = this.size,
      f = this.state.framing;
    ctx.clearRect(0, 0, w, h);
    if (this.marking) ctx.drawImage(this.layers.source, 0, 0, w, h);
    else if (this.layers.prepared)
      ctx.drawImage(this.layers.source, 0, 0, w, h);
    else {
      const crop = frameCrop(
        this.layers.source.naturalWidth,
        this.layers.source.naturalHeight,
        this.size,
        { zoom: f.zoom, focal: f.focal_x * 100, vertical: f.focal_y * 100 },
      );
      ctx.drawImage(
        this.layers.source,
        crop.sx,
        crop.sy,
        crop.sw,
        crop.sh,
        0,
        0,
        w,
        h,
      );
    }
    if (!this.marking) {
      const bounds = this.logoBounds();
      if (this.layers.logo && bounds)
        ctx.drawImage(
          this.layers.logo,
          bounds[0],
          bounds[1],
          bounds[2] - bounds[0],
          bounds[3] - bounds[1],
        );
      for (const id of ["message", "cta"]) {
        const layer = this.layers.blocks[id],
          block = this.blocks[id];
        if (!layer || !block) continue;
        const p = this.state.framing.text_placements[id] || block.placement;
        const scale = p.font_size / layer.placement.font_size;
        const color = p.color || this.brand.text_color;
        let image = layer.image;
        if (color !== layer.color) {
          if (layer.tintColor !== color) {
            const tint = document.createElement("canvas");
            tint.width = w;
            tint.height = h;
            const mask = tint.getContext("2d");
            mask.drawImage(image, 0, 0);
            mask.globalCompositeOperation = "source-in";
            mask.fillStyle = color;
            mask.fillRect(0, 0, w, h);
            layer.tint = tint;
            layer.tintColor = color;
          }
          image = layer.tint;
        }
        const x = p.left * w - layer.placement.left * w * scale,
          y = p.top * h - layer.placement.top * h * scale;
        ctx.drawImage(image, x, y, w * scale, h * scale);
      }
    }
    this.paintSelection();
    this.paintSafety();
    if (this.owner.mode === "framing") this.paintControlsState();
  }
  logoBounds() {
    if (!this.logo) return null;
    const p = this.state.framing.logo_placement;
    if (!p) return this.logo.default_bounds;
    const width = p.width * this.size.w,
      height = (width * this.logo.size[1]) / this.logo.size[0];
    return [
      p.left * this.size.w,
      p.top * this.size.h,
      p.left * this.size.w + width,
      p.top * this.size.h + height,
    ];
  }
  textBounds(id) {
    const layer = this.layers?.blocks[id],
      block = this.blocks[id];
    if (!layer || !block) return null;
    const p = this.state.framing.text_placements[id] || block.placement,
      scale = p.font_size / layer.placement.font_size;
    const x = p.left * this.size.w - layer.placement.left * this.size.w * scale,
      y = p.top * this.size.h - layer.placement.top * this.size.h * scale;
    return layer.bounds.map(
      (value, index) => value * scale + (index % 2 ? y : x),
    );
  }
  hit(point) {
    const rect = this.output.getBoundingClientRect();
    const bounds = (id) => {
      const frame = this.selectionFrame(id);
      return (
        frame && [
          frame.left,
          frame.top,
          frame.left + frame.width,
          frame.top + frame.height,
        ]
      );
    };
    return MotifSceneGeometry.hitTest(
      {
        x: (point.x * rect.width) / this.size.w,
        y: (point.y * rect.height) / this.size.h,
      },
      Object.fromEntries(
        ["message", "cta"].map((id) => [id, { bounds: bounds(id) }]),
      ),
      bounds("logo"),
    );
  }
  selectionFrame(id) {
    const bounds = id === "logo" ? this.logoBounds() : this.textBounds(id);
    if (!bounds) return null;
    const rect = this.output.getBoundingClientRect();
    return MotifManipulation.selectionFrame(
      {
        left: (bounds[0] / this.size.w) * rect.width,
        top: (bounds[1] / this.size.h) * rect.height,
        width: ((bounds[2] - bounds[0]) / this.size.w) * rect.width,
        height: ((bounds[3] - bounds[1]) / this.size.h) * rect.height,
      },
      { width: rect.width, height: rect.height },
    );
  }
  paintSelection() {
    if (!this.selection) return;
    const id =
      this.owner.mode === "logo"
        ? "logo"
        : this.owner.mode === "text"
          ? this.selected
          : null;
    const frame = id ? this.selectionFrame(id) : null;
    this.selection.hidden = !this.ready || !frame || this.marking;
    if (this.selection.hidden) return;
    for (const key of ["left", "top", "width", "height"])
      this.selection.style[key] = `${frame[key]}px`;
    this.selection.classList.toggle("is-top-edge", frame.top < 24);
    const label =
      id === "logo" ? "Logo" : id === "message" ? "Headline" : "Call to action";
    $(".mf-direct-label", this.selection).textContent = label;
    this.selection.setAttribute(
      "aria-label",
      `${label}. Arrow keys move; Shift moves ten pixels.`,
    );
    const corners = {
      nw: "top left",
      ne: "top right",
      sw: "bottom left",
      se: "bottom right",
    };
    for (const button of $$("[data-mf-corner]", this.selection))
      button.setAttribute(
        "aria-label",
        `Resize ${label} from ${corners[button.dataset.mfCorner]}. Arrow keys resize. Shift uses ten pixels.`,
      );
  }
  point(event) {
    const r = this.output.getBoundingClientRect();
    return {
      x: ((event.clientX - r.left) * this.size.w) / r.width,
      y: ((event.clientY - r.top) * this.size.h) / r.height,
    };
  }
  readGesture(id) {
    const bounds = id === "logo" ? this.logoBounds() : this.textBounds(id);
    const placement =
      id === "logo"
        ? this.state.framing.logo_placement || {
            left: bounds[0] / this.size.w,
            top: bounds[1] / this.size.h,
            width: (bounds[2] - bounds[0]) / this.size.w,
          }
        : this.state.framing.text_placements[id] || this.blocks[id]?.placement;
    return {
      id,
      bounds: bounds && [...bounds],
      placement: copy(placement),
      framing: copy(this.state.framing),
    };
  }
  pointerDown(event) {
    if (
      event.button !== 0 ||
      event.isPrimary === false ||
      event.target.closest("button:not([data-mf-corner])") ||
      !this.ready ||
      this.uploading
    )
      return;
    const point = this.point(event),
      corner = event.target.closest("[data-mf-corner]")?.dataset.mfCorner;
    const id = this.marking
      ? "area"
      : corner
        ? this.owner.mode === "logo"
          ? "logo"
          : this.selected
        : this.hit(point);
    if (id !== "area")
      this.setMode(
        id === "logo" ? "logo" : id === "framing" ? "framing" : "text",
        ["message", "cta"].includes(id) ? id : null,
      );
    if (id === "framing" && this.prepared) return;
    event.preventDefault();
    event.stopPropagation();
    this.output.setPointerCapture(event.pointerId);
    (corner
      ? event.target
      : this.selection.hidden
        ? this.output
        : this.selection
    ).focus({ preventScroll: true });
    this.gesture = {
      id,
      pointer: event.pointerId,
      point,
      corner,
      start: ["framing", "area"].includes(id)
        ? { framing: copy(this.state.framing) }
        : this.readGesture(id),
    };
  }
  pointerMove(event) {
    const g = this.gesture;
    if (!g || g.pointer !== event.pointerId) return;
    const point = this.point(event),
      dx = point.x - g.point.x,
      dy = point.y - g.point.y;
    if (g.id === "area") {
      const clamp = (v) => Math.max(0, Math.min(1, v));
      this.state.framing.subject_box = {
        left: clamp(Math.min(point.x, g.point.x) / this.size.w),
        top: clamp(Math.min(point.y, g.point.y) / this.size.h),
        right: clamp(Math.max(point.x, g.point.x) / this.size.w),
        bottom: clamp(Math.max(point.y, g.point.y) / this.size.h),
      };
      this.state.framing.inherit_subject = false;
    } else if (g.id === "framing") {
      const f = g.start.framing,
        source = this.layers.source,
        crop = frameCrop(source.naturalWidth, source.naturalHeight, this.size, {
          zoom: f.zoom,
          focal: f.focal_x * 100,
          vertical: f.focal_y * 100,
        });
      this.state.framing.focal_x = crop.rangeX
        ? Math.max(
            0,
            Math.min(1, f.focal_x - (dx * crop.sw) / this.size.w / crop.rangeX),
          )
        : 0.5;
      this.state.framing.focal_y = crop.rangeY
        ? Math.max(
            0,
            Math.min(1, f.focal_y - (dy * crop.sh) / this.size.h / crop.rangeY),
          )
        : 0.5;
    } else
      this.writeGesture(
        g.corner
          ? MotifSceneGeometry.resize(g.start, g.corner, dx, dy, {
              width: this.size.w,
              height: this.size.h,
            })
          : MotifSceneGeometry.move(g.start, dx, dy, {
              width: this.size.w,
              height: this.size.h,
            }),
      );
    this.sequence++;
    this.paint();
  }
  writeGesture(value) {
    if (value.id === "logo")
      this.state.framing.logo_placement = copy(value.placement);
    else this.state.framing.text_placements[value.id] = copy(value.placement);
  }
  pointerEnd(event, cancel = false) {
    const g = this.gesture;
    if (!g || g.pointer !== event.pointerId) return;
    this.gesture = null;
    if (this.output.hasPointerCapture(event.pointerId))
      this.output.releasePointerCapture(event.pointerId);
    const area = this.state.framing.subject_box;
    if (
      cancel ||
      (g.id === "area" &&
        (!area || area.right <= area.left || area.bottom <= area.top))
    )
      this.state.framing = copy(g.start.framing);
    this.changed({
      raster: !!g.corner && g.id !== "logo",
      provisional: !!g.corner,
      immediate: true,
    });
    if (this.owner.mode === "framing") this.controls();
  }
  keyDown(event) {
    if (event.key === "Escape" && this.gesture) {
      this.pointerEnd({ pointerId: this.gesture.pointer }, true);
      event.preventDefault();
      return;
    }
    if (
      !this.ready ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey ||
      !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)
    )
      return;
    if (this.owner.mode === "framing" && !this.prepared) {
      event.preventDefault();
      event.stopPropagation();
      const f = this.state.framing,
        horizontal = ["ArrowLeft", "ArrowRight"].includes(event.key),
        direction = ["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1;
      if (event.target.closest(".ws-scene-safety")) {
        const source =
          f.subject_box || (f.inherit_subject ? this.sourceSubject : null);
        if (!source) return;
        const box = copy(source),
          start = horizontal ? "left" : "top",
          end = horizontal ? "right" : "bottom",
          delta = direction * 0.01;
        if (event.shiftKey)
          box[end] = Math.max(box[start] + 0.01, Math.min(1, box[end] + delta));
        else {
          const shift = Math.max(-box[start], Math.min(1 - box[end], delta));
          box[start] += shift;
          box[end] += shift;
        }
        f.subject_box = box;
        f.inherit_subject = false;
      } else {
        const source = this.layers.source,
          crop = frameCrop(
            source.naturalWidth,
            source.naturalHeight,
            this.size,
            { zoom: f.zoom, focal: f.focal_x * 100, vertical: f.focal_y * 100 },
          );
        const range = horizontal ? crop.rangeX : crop.rangeY,
          span = horizontal ? crop.sw : crop.sh,
          dimension = horizontal ? this.size.w : this.size.h,
          key = horizontal ? "focal_x" : "focal_y";
        f[key] = range
          ? Math.max(
              0,
              Math.min(
                1,
                f[key] -
                  (direction * (event.shiftKey ? 10 : 1) * span) /
                    dimension /
                    range,
              ),
            )
          : 0.5;
      }
      this.changed({ immediate: true });
      return;
    }
    const id =
      this.owner.mode === "logo"
        ? "logo"
        : this.owner.mode === "text"
          ? this.selected
          : null;
    if (!id) return;
    const start = this.readGesture(id);
    if (!start.bounds) return;
    event.preventDefault();
    event.stopPropagation();
    const step = event.shiftKey ? 10 : 1,
      dx =
        event.key === "ArrowLeft"
          ? -step
          : event.key === "ArrowRight"
            ? step
            : 0,
      dy =
        event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0;
    const corner = event.target.closest("[data-mf-corner]")?.dataset.mfCorner;
    this.writeGesture(
      corner
        ? MotifSceneGeometry.resize(
            start,
            corner,
            dx,
            dy,
            { width: this.size.w, height: this.size.h },
            true,
          )
        : MotifSceneGeometry.move(start, dx, dy, {
            width: this.size.w,
            height: this.size.h,
          }),
    );
    this.changed({
      raster: !!corner && id !== "logo",
      provisional: !!corner,
      immediate: true,
    });
  }
  paintSafety() {
    const node = $(".ws-scene-safety", this.output),
      box =
        this.state.framing.subject_box ||
        (this.state.framing.inherit_subject ? this.sourceSubject : null);
    node.hidden =
      !box ||
      (!this.showSafety && !this.marking) ||
      this.owner.mode !== "framing";
    if (node.hidden) return;
    let bounds = [box.left, box.top, box.right, box.bottom];
    if (!this.marking && this.layers) {
      const source = this.layers.source,
        f = this.state.framing,
        crop = frameCrop(source.naturalWidth, source.naturalHeight, this.size, {
          zoom: f.zoom,
          focal: f.focal_x * 100,
          vertical: f.focal_y * 100,
        });
      bounds = [
        (box.left * source.naturalWidth - crop.sx) / crop.sw,
        (box.top * source.naturalHeight - crop.sy) / crop.sh,
        (box.right * source.naturalWidth - crop.sx) / crop.sw,
        (box.bottom * source.naturalHeight - crop.sy) / crop.sh,
      ];
    }
    Object.assign(node.style, {
      left: `${bounds[0] * 100}%`,
      top: `${bounds[1] * 100}%`,
      width: `${(bounds[2] - bounds[0]) * 100}%`,
      height: `${(bounds[3] - bounds[1]) * 100}%`,
    });
  }
  async click(event) {
    const block = event.target.closest("[data-scene-block]"),
      all = event.target.closest("[data-mf-apply-all]"),
      action = event.target.closest("[data-scene-action]"),
      color = event.target.closest("[data-mf-color],[data-mf-color-default]");
    if (!block && !all && !action && !color) return;
    event.preventDefault();
    event.stopPropagation();
    if (block) {
      this.setMode("text", block.dataset.sceneBlock);
      return;
    }
    if (all) {
      const field = all.dataset.mfApplyAll;
      this.state.scopes[field] = {
        kind: "all_products",
        sequence: ++this.scopeSequence,
      };
      this.changed();
      await flushStudioSave();
      return;
    }
    if (color) {
      this.style({
        color: color.hasAttribute("data-mf-color-default")
          ? null
          : color.dataset.mfColor,
      });
      return;
    }
    switch (action.dataset.sceneAction) {
      case "retry":
        this.refresh();
        break;
      case "upload":
        if (this.owner.mode !== "logo") this.setMode("logo");
        $("[data-scene-upload]", this.owner.root)?.click();
        break;
      case "reset-text":
        this.state.framing.text_placements = {};
        this.changed({ raster: true });
        break;
      case "reset-logo":
        this.state.framing.logo_placement = null;
        this.changed();
        break;
      case "reset-frame":
        Object.assign(this.state.framing, {
          zoom: 1,
          focal_x: 0.5,
          focal_y: 0.5,
        });
        this.changed();
        this.controls();
        break;
      case "mark":
        this.marking = !this.marking;
        this.showSafety = true;
        this.paint();
        this.controls();
        break;
      case "clear-area":
        this.state.framing.subject_box = null;
        this.state.framing.inherit_subject = false;
        this.changed();
        break;
    }
  }
  input(event) {
    const el = event.target;
    if (el.matches("[data-scene-upload]")) {
      if (event.type === "change" && el.files[0]) this.upload(el.files[0]);
      return;
    }
    const field = el.dataset.mfValue;
    if (field) {
      event.stopPropagation();
      if (this.state.values[field] === el.value) return;
      this.state.values[field] = el.value;
      this.state.scopes[field] = {
        kind: "product",
        sequence: ++this.scopeSequence,
      };
      this.changed({ raster: true });
      return;
    }
    if (el.matches("[data-mf-font]")) {
      event.stopPropagation();
      this.style({ font_id: el.value });
      return;
    }
    if (el.id === "image-description") {
      event.stopPropagation();
      if (this.state.description === el.value) return;
      this.state.description = el.value;
      this.changed();
      return;
    }
    const key = {
      "scene-zoom": "zoom",
      "scene-focal": "focal_x",
      "scene-vertical": "focal_y",
    }[el.id];
    if (key) {
      event.stopPropagation();
      if (this.state.framing[key] === Number(el.value) / 100) return;
      this.state.framing[key] = Number(el.value) / 100;
      this.changed();
      const output = $(`#${el.id}-value`, this.owner.root);
      if (output)
        output.textContent =
          key === "zoom"
            ? `${Math.round(Number(el.value))}%`
            : framePositionLabel(
                Number(el.value),
                key === "focal_x" ? "horizontal" : "vertical",
              );
      return;
    }
    if (el.id === "scene-copy-placement") {
      this.state.framing.copy_placement = el.value;
      this.changed({ raster: true });
    }
    if (el.id === "scene-show-safety") {
      this.showSafety = el.checked;
      this.paintSafety();
    }
  }
  style(patch) {
    const p =
      this.state.framing.text_placements[this.selected] ||
      this.blocks[this.selected]?.placement;
    const current = { ...p, ...this.pendingStyles[this.selected] };
    if (Object.entries(patch).every(([key, value]) => current[key] === value))
      return;
    if (!p) {
      this.pendingStyles[this.selected] = {
        ...this.pendingStyles[this.selected],
        ...patch,
      };
      this.changed({ raster: true });
      this.textControls();
      return;
    }
    this.state.framing.text_placements[this.selected] = { ...p, ...patch };
    this.changed({ raster: "font_id" in patch });
    this.textControls();
  }
  async upload(file) {
    if (this.uploading) return;
    this.uploading = true;
    this.status("Attaching logo…");
    try {
      const response = await fetch("/api/uploads", {
        method: "POST",
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          "X-Campaign-Token": $('meta[name="campaign-token"]').content,
          "X-Campaign-ID": this.owner.campaign,
          "X-Image-Role": "logo",
          "X-Asset-Name": encodeURIComponent(
            (draft.brand?.name || "Brand") + " logo",
          ),
        },
        body: file,
      });
      const result = await response.json();
      if (!response.ok)
        throw new Error(result.message || "The logo could not be uploaded.");
      if (!this.current()) return;
      this.state.asset = {
        reference: result.reference,
        provenance: result.provenance,
      };
      // The server fits all saved format placements to the replacement dimensions.
      this.state.framing.logo_placement = null;
      this.uploading = false;
      this.changed({ raster: true });
      await this.refresh();
      if (this.current()) {
        this.setMode(this.owner.mode);
        await this.queue.flush();
      }
    } catch (error) {
      if (this.current()) this.status(error.message, true);
    } finally {
      this.uploading = false;
    }
  }
  publishThumbnail({ provisional = false } = {}) {
    if (
      !this.ready ||
      (this.rasterDirty && !provisional) ||
      this.marking ||
      !this.current()
    )
      return;
    const button = $(`[data-ws-format="${this.size.ratio}"]`, this.owner.root),
      slot = $(".ws-thumb", button);
    if (!slot) return;
    let canvas = slot.querySelector("canvas");
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.setAttribute("aria-hidden", "true");
      slot.replaceChildren(canvas);
    }
    canvas.width = 96;
    canvas.height = Math.round((96 * this.size.h) / this.size.w);
    canvas
      .getContext("2d")
      .drawImage(this.canvas, 0, 0, canvas.width, canvas.height);
    this.owner.localThumbnail = {
      format: this.format,
      signature: this.rasterDirty
        ? null
        : studioThumbnailSignature(
            outputFor(currentProduct, lang, this.format),
            this.format,
          ),
      target: studioVisualTarget(),
      canvas,
    };
    const state = this.owner.thumbnails?.get(button);
    if (state) {
      state.sequence++;
      state.pending = null;
      state.signature = this.owner.localThumbnail.signature;
    }
  }
  dispose() {
    if (this.layers && !this.rasterDirty && !this.queue.dirty()) {
      this.owner.scenes ||= new Map();
      this.owner.scenes.set(this.sceneKey(), {
        layers: this.layers,
        blocks: copy(this.blocks),
        logo: copy(this.logo),
      });
      while (this.owner.scenes.size > 6)
        this.owner.scenes.delete(this.owner.scenes.keys().next().value);
    }
    this.closed = true;
    this.sequence++;
    clearTimeout(this.previewTimer);
    this.events.abort();
    this.resizeObserver.disconnect();
    this.queue.stop();
    this.layers = null;
  }
}
