"use strict";

// Bounded, decoded presentation reuse. This cache never authorizes a save or export.
const MotifVisuals = (() => {
  const entries = new Map(),
    jobs = [],
    nodeKeys = new WeakMap();
  let running = 0,
    bytes = 0,
    warmTimer = null;
  const limit = 48 * 1024 * 1024;
  function fontKey(product, market, ratio, wording) {
    const placements = product?.framing?.[market]?.[ratio]?.text_placements;
    return ["message", "cta"]
      .filter((id) => wording?.[id])
      .map((id) => {
        const font = placements?.[id]?.font_id || "noto_sans";
        return [font, model.visual_fonts?.[font]];
      });
  }
  function key(url) {
    const match = url?.match(
      /\/api\/campaigns\/([^/]+)\/composition\/[^/]+\/(thumbnails|source-thumbnails)\/([^/?]+)(?:\/([^/?]+)\/([^/?]+))?/,
    );
    if (!match || match[1] !== C.id) return url;
    const [, campaign, kind, productId, market, ratio] = match;
    const product = (model.output_brief || model.value)?.products.find(
      (p) => p.id === productId,
    );
    const source = [product?.source, model.visual_sources?.[productId]];
    if (kind === "source-thumbnails")
      return JSON.stringify([campaign, kind, productId, source]);
    const output = model.manifest?.outputs.find(
      (o) =>
        o.product_id === productId &&
        o.market === market &&
        o.ratio.replace(":", "x") === ratio,
    );
    const brand = (model.output_brief || model.value)?.brand;
    return JSON.stringify([
      campaign,
      kind,
      productId,
      market,
      ratio,
      url.split("?")[1],
      source,
      model.visual_products?.[productId],
      fontKey(product, market, ratio.replace("x", ":"), output?.copy),
      output?.copy,
      product?.framing?.[market]?.[ratio.replace("x", ":")] || {},
      brand?.logo_path,
      model.visual_logo,
      brand?.text_color,
    ]);
  }
  function prune() {
    for (const [id, entry] of entries) {
      if (entries.size <= 96 && bytes <= limit) break;
      if (
        entry.users ||
        entry.nodes?.some((node) => node.isConnected) ||
        !entry.blobURL
      )
        continue;
      URL.revokeObjectURL(entry.blobURL);
      bytes -= entry.bytes;
      entries.delete(id);
    }
  }
  function pump() {
    while (running < 3 && jobs.length) {
      const job = jobs.shift();
      running++;
      job().finally(() => {
        running--;
        pump();
      });
    }
  }
  function load(url, priority = false) {
    const id = key(url);
    if (entries.has(id)) {
      const entry = entries.get(id);
      entries.delete(id);
      entries.set(id, entry);
      return entry;
    }
    const entry = { id, url, nodes: [], blobURL: null, bytes: 0, users: 0 };
    entry.ready = new Promise((resolve, reject) => {
      const job = async () => {
        try {
          const versioned = url.match(
            /\/api\/campaigns\/([^/]+)\/composition\/[^/]+\//,
          );
          if (versioned && (versioned[1] !== C.id || key(url) !== id))
            throw new Error("Preview was superseded");
          const currentVersion = model?.composition_version || model?.version;
          const requestURL =
            versioned && currentVersion
              ? url.replace(
                  /\/composition\/[^/]+\//,
                  `/composition/${currentVersion}/`,
                )
              : url;
          const response = await fetch(requestURL);
          if (!response.ok) throw new Error("Preview unavailable");
          const blob = await response.blob();
          if (!blob.type.startsWith("image/"))
            throw new Error("Preview unavailable");
          const blobURL = URL.createObjectURL(blob),
            image = new Image();
          image.src = blobURL;
          try {
            await image.decode();
          } catch (error) {
            URL.revokeObjectURL(blobURL);
            throw error;
          }
          entry.image = image;
          entry.blobURL = blobURL;
          entry.pixelBytes = image.naturalWidth * image.naturalHeight * 4;
          entry.bytes = blob.size + entry.pixelBytes;
          bytes += entry.bytes;
          resolve(entry);
          prune();
        } catch (error) {
          entries.delete(id);
          reject(error);
        }
      };
      if (priority) jobs.unshift(job);
      else jobs.push(job);
    });
    // Prefetch is best effort; an actual visible image offers its normal retry on revisit.
    entry.ready.catch(() => {});
    entries.set(id, entry);
    pump();
    return entry;
  }
  function image(url, alt = "") {
    if (!url) return "";
    return `<img data-visual-url="${esc(url)}" alt="${esc(alt)}">`;
  }
  function mount(root = document) {
    for (const placeholder of $$("img[data-visual-url]", root)) {
      if (
        placeholder.dataset.visualMounted &&
        nodeKeys.get(placeholder) === key(placeholder.dataset.visualUrl)
      )
        continue;
      const url = placeholder.dataset.visualUrl,
        entry = load(url, true);
      entry.users++;
      const install = () => {
        if (!placeholder.isConnected || placeholder.dataset.visualUrl !== url)
          return;
        const node =
          entry.nodes.find((image) => !image.isConnected) ||
          (entry.nodes.length ? entry.image.cloneNode() : entry.image);
        for (const attribute of [...node.attributes])
          if (attribute.name !== "src") node.removeAttribute(attribute.name);
        for (const attribute of [...placeholder.attributes])
          if (attribute.name !== "src")
            node.setAttribute(attribute.name, attribute.value);
        node.dataset.visualMounted = "true";
        nodeKeys.set(node, entry.id);
        if (!entry.nodes.includes(node)) {
          entry.nodes.push(node);
          if (node !== entry.image) {
            entry.bytes += entry.pixelBytes;
            bytes += entry.pixelBytes;
          }
        }
        placeholder.replaceWith(node);
        prune();
      };
      const release = () => {
        entry.users--;
        prune();
      };
      if (entry.blobURL) {
        install();
        release();
      } else
        entry.ready
          .then(install)
          .catch(() => {
            if (placeholder.isConnected)
              placeholder.title =
                "Preview unavailable. Reopen this view to retry.";
          })
          .finally(release);
    }
    clearTimeout(warmTimer);
    if (
      page === "production" ||
      (page === "review" && reviewTab !== "product")
    ) {
      const campaign = C.id,
        version = model?.composition_version || model?.version;
      warmTimer = setTimeout(() => {
        if (
          C.id !== campaign ||
          (model?.composition_version || model?.version) !== version ||
          studioWorkspace
        )
          return;
        for (const output of model?.manifest?.outputs || [])
          if (output.market === MARKET[lang] && output.gallery_url)
            load(output.gallery_url);
      }, 400);
    }
  }
  return { image, mount, key, load, fontKey };
})();
window.MotifVisuals = MotifVisuals;
