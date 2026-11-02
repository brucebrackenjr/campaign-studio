/* E129 isolated presentation shell. No requests, persistence, preview math, or editor state.
   Create the shell once per product visit. Existing controllers mount into its stable slots. */
(function(scope){
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const tools=[['inspect','↗','Inspect','Image details'],['framing','⌗','Framing','Crop, zoom, and safety area'],['logo','◇','Logo','Move, resize, or replace'],['text','T','Text','Wording, font, color, and placement']];
  function shell(v){
    return `<section class="ws-workspace" aria-label="Product Studio">
      <header class="ws-heading"><div><div class="ws-eyebrow">STUDIO / PRODUCT ${esc(v.productNumber)} OF ${esc(v.productCount)}</div><h1>${esc(v.productName)}</h1></div><div class="ws-heading-actions"><button type="button" class="ws-button ws-quiet" data-ws-action="all-products" aria-label="Back to Studio">← Studio</button><label>Market <select data-ws-target="market" aria-label="Market">${v.markets.map(m=>`<option value="${esc(m.id)}" ${v.market===m.id?'selected':''}>${esc(m.label)}</option>`).join('')}</select></label></div></header>
      <div class="ws-layout">
        <nav class="ws-tools" aria-label="Image editing tools">${tools.map(([id,icon,label,detail])=>`<button type="button" data-ws-mode="${id}" aria-pressed="${v.mode===id}"><span class="ws-tool-symbol" aria-hidden="true">${icon}</span><span><strong>${label}</strong><small>${detail}</small></span></button>`).join('')}</nav>
        <section class="ws-center" aria-label="Image preview"><div class="ws-canvas" data-ws-slot="canvas"><div class="ws-canvas-content" data-ws-slot="surface"></div></div><div class="ws-formats" role="group" aria-label="Image format">${v.formats.map(f=>`<button type="button" data-ws-target="format" data-ws-format="${esc(f.id)}" aria-label="${esc(f.name)} · ${esc(f.id)}${f.url?'':' · Image unavailable'}" aria-pressed="${v.format===f.id}" ${f.available?'':'disabled'}><span class="ws-thumb">${f.url?`<img src="${esc(f.url)}" alt="">`:'<span aria-hidden="true">–</span>'}</span><span><strong>${esc(f.name)}</strong><small>${esc(f.id)}</small></span></button>`).join('')}</div></section>
        <aside class="ws-controls" aria-label="Selected tool controls"><header class="ws-controls-heading"><div class="ws-eyebrow" data-ws-slot="target">${esc(v.marketLabel)} · ${esc(v.formatLabel)}</div><h2 data-ws-slot="mode-title">${esc(tools.find(t=>t[0]===v.mode)?.[2]||'Inspect')}</h2></header><div class="ws-control-body" data-ws-slot="controls"></div><footer class="ws-control-footer" data-ws-slot="commit"><p class="ws-status" role="status" data-ws-slot="status"></p><div><button type="button" class="ws-button" data-ws-action="cancel">Cancel</button><button type="button" class="ws-button ws-primary" data-ws-action="apply" disabled>Apply changes</button></div></footer></aside>
      </div>
    </section>`;
  }
  // Call this after the existing controller accepts a mode change. Never rebuild
  // .ws-center on input, preview completion, or mode change.
  function setMode(root,{mode,title,target,controlsHtml,applyLabel,canApply,status,inspect=false}){
    root.querySelectorAll('[data-ws-mode]').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.wsMode===mode)));
    root.querySelector('[data-ws-slot="mode-title"]').textContent=title;
    root.querySelector('[data-ws-slot="target"]').textContent=target;
    root.querySelector('[data-ws-slot="controls"]').innerHTML=controlsHtml;
    const apply=root.querySelector('[data-ws-action="apply"]');apply.textContent=applyLabel;apply.disabled=!canApply;
    root.querySelector('[data-ws-slot="status"]').textContent=status||'';
    root.querySelector('[data-ws-slot="commit"]').hidden=inspect;
  }
  scope.MotifStudioShell={shell,setMode,esc};
})(window);
