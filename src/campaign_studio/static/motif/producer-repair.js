/* Reusable presentation only. The host owns data, validation, upload, persistence,
   accepted-output ownership, plan admission, dispatch, recovery, and editor guards. */
(function (scope) {
  'use strict';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const formats = [{id:'1:1',name:'Square',size:'1080 × 1080'}, {id:'9:16',name:'Story',size:'1080 × 1920'}, {id:'16:9',name:'Landscape',size:'1920 × 1080'}];
  const button = (label,action,style='',extra='') => `<button type="button" class="pr-button ${style}" data-pr-action="${esc(action)}" ${extra}>${label}</button>`;
  const byId = id => `data-pr-id="${esc(id)}"`;
  // Availability is supplied per accepted output and per existing host action.
  // Missing flags stay disabled; no shared edit lock can overrule logo admission.
  function imageAction(v,p,market,format,action,label,style) {
    const enabled=v.actionAvailability?.[p.id]?.[market+'|'+format]?.[action]===true;
    return button(label,action,style,`${byId(p.id)} data-pr-market="${esc(market)}" data-pr-format="${esc(format)}" ${enabled?'':'disabled'}`);
  }
  function marketSelect(markets,selected,{all=false,action='market',label='Market'}={}) {
    return `<label class="pr-select-label"><span>${esc(label)}</span><select aria-label="${esc(label)}" data-pr-select="${esc(action)}">${all?`<option value="all" ${selected==='all'?'selected':''}>All markets</option>`:''}${markets.map(m=>`<option value="${esc(m.id)}" ${selected===m.id?'selected':''}>${esc(m.label)}</option>`).join('')}</select></label>`;
  }
  function heading(title,eyebrow,detail,actions='') {
    return `<header class="pr-page-head wf-stage-header"><div><div class="pr-eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1>${detail?`<p>${esc(detail)}</p>`:''}</div><div class="pr-head-actions">${actions}</div></header>`;
  }
  function capDialog() {
    return `<div class="pr-overlay"><section class="pr-cap-dialog" role="dialog" aria-modal="true" aria-labelledby="pr-cap-title"><div class="pr-cap-symbol" aria-hidden="true">5</div><h2 id="pr-cap-title">You’ve reached the five-product limit.</h2><p>Campaigns can have up to five products. This campaign already has five. Remove a product to add another, or edit one to change its image and description.</p>${button('Back to products','close-cap','pr-primary')}</section></div>`;
  }
  function formatSet() {
    return `<div class="pr-format-set">${formats.map(f=>`<div><i style="aspect-ratio:${f.id.replace(':','/')}" aria-hidden="true"></i><span><strong>${f.name}</strong><small>${f.id} · ${f.size}</small></span></div>`).join('')}</div>`;
  }
  function missingImageContent() {
    return '<span class="pr-add-image-plus" aria-hidden="true">+</span><strong>Add image</strong>';
  }
  function productArtwork(p, active) {
    if(p.image) return `<img src="${esc(p.image)}" alt="${esc(p.name)} – product photo">`;
    if(p.mode==='concept' || active) return `<div class="pr-concept-art"><span class="${active&&p.sourceLabel==='Generating image…'?'pr-source-wait':''}" aria-hidden="true">✳</span><strong>${esc(p.sourceLabel || 'Image will be generated.')}</strong></div>`;
    return `<div class="pr-missing-art"><strong>Add product image</strong><div class="pr-missing-actions">${button('Select existing image','choose','pr-small',byId(p.id))}${button('Generate new image','choose-concept','pr-small',byId(p.id))}</div></div>`;
  }
  function productCards(v) {
    return `<div class="pr-products" style="--pr-count:${Math.min(v.products.length,5)}">${v.products.map(p=>`<article class="pr-product-card">
      <div class="pr-product-photo">${productArtwork(p,v.mutationsBlocked)}</div>
      <div class="pr-product-info"><div class="pr-card-facts">${!p.image&&!v.mutationsBlocked&&p.mode!=='concept'?`<span class="pr-source-label">${esc(p.sourceLabel||'Photo needed')}</span>`:''}${p.outputSummary?`<span class="pr-output-summary">${esc(p.outputSummary)}</span>`:''}</div><h2>${esc(p.name)}</h2><p>${esc(p.description)}</p><div class="pr-card-actions">${button('Edit product','edit','pr-small',byId(p.id))}${p.image?button('View photo ↗','source','pr-small pr-quiet',byId(p.id)):''}</div></div>
    </article>`).join('')}</div>`;
  }
  function production(v) {
    const count=v.products.length,total=count*v.markets.length*3;
    const top=heading('Choose your campaign imagery.','02 / PRODUCTION','Use existing photos, or generate missing source images. Finished formats are created when you download.',`<div class="wf-sequence-actions">${button('← Copy & Rules','previous-copy','pr-small wf-previous')}${button('Next: Studio →','studio','pr-primary',v.savedCount?'':'disabled aria-describedby="wf-studio-availability"')}</div>`);
    if(!count) return top+`<section class="pr-empty"><div class="pr-eyebrow">START WITH A PRODUCT</div><h2>Bring the photo. Or bring the idea.</h2><p>Choose an existing image, or describe a new image to generate.</p>${button('Add your first product','add','pr-primary')}${formatSet()}<p id="wf-studio-availability" class="wf-studio-availability">Add a source image to open Studio.</p></section>`;
    return top+`<div class="pr-workspace"><section class="pr-product-board" aria-label="Campaign products">${v.error?`<div class="pr-inline-error" role="alert"><div><strong>${esc(v.error.title)}</strong><p>${esc(v.error.detail)}</p></div>${button(v.error.actionLabel||'Edit product','edit','pr-small',byId(v.error.productId))}</div>`:''}${productCards(v)}</section>${productionSidebar(v)}</div>`;
  }
  function productionSidebar(v) {
    const panel=actionPanel(v),keyAlreadyVisible=/data-pr-action="(?:key|update-key)"/.test(panel);
    return `<div class="wf-production-sidebar"><div class="wf-product-management"><span class="pr-cap">${v.products.length} of ${v.productLimit} products</span>${button('＋ Add product','add','pr-primary',v.mutationsBlocked?'disabled':'')}</div>${panel}${keyAlreadyVisible?'':`<div class="pr-key-access">${button('OpenAI key','key','pr-small',v.mutationsBlocked?'disabled':'')}</div>`}<details class="wf-output-coverage"><summary>${v.products.length} products · ${v.markets.length} markets · 3 formats</summary><div class="wf-formats">${formats.map(f=>`<span><strong>${f.name}</strong><small>${f.id}</small></span>`).join('')}</div><div class="wf-markets">${v.markets.map(m=>`<span>${esc(m.label)}</span>`).join('')}</div></details>${!v.savedCount?'<p class="wf-studio-availability" id="wf-studio-availability" style="padding:0 18px 16px">Add a source image to open Studio.</p>':''}</div>`;
  }
  function requestDetails(v) {
    const req=v.plan?.requests||0;
    return req ? `<details class="pr-request-details"><summary>Request details</summary><p>Sent to OpenAI: audience, scene direction, and product description. Supplied photos, logo, and wording stay local.</p>${button('Exact prompt & settings','settings','pr-small pr-wide')}<p>Dispatched requests use allowance even if they fail or the result is uncertain. This is not a spending cap. No automatic paid retries.</p></details>` : '';
  }
  function credentialPanel(v) {
    if (v.keyReloaded && v.resumeGeneration)
      return `<h2>Resume generation.</h2><p>Try again with the key loaded at startup.</p>${button('Resume generation',v.resumeAction||'resumeGeneration','pr-primary pr-wide')}`;
    if (v.keyUpdated && v.resumeGeneration)
      return `<h2>Resume generation.</h2><p>Key updated. Resume to try again.</p>${button('Resume generation',v.resumeAction||'resumeGeneration','pr-primary pr-wide')}`;
    if (!['rejected_key','access_denied'].includes(v.credentialIssue)) return '';
    return `<h2>Update your API key.</h2><p>${v.credentialIssue==='access_denied'?'This key does not have access to the image model.':'OpenAI did not accept this key.'}</p>${button('Update API key','update-key','pr-primary pr-wide')}<p class="pr-operation-detail">Your images and edits are unchanged.</p>`;
  }
  function actionPanel(v) {
    const req=v.plan?.requests||0,total=v.products.length*v.markets.length*3;
    if(v.operationHtml) return `<aside class="pr-action-panel">${v.operationHtml}</aside>`;
    const missing=v.products.filter(p=>!p.image&&p.mode!=='concept'&&p.sourceLabel!=='Replace the unavailable photo');
    let content=credentialPanel(v);
    if(content) {} // A current credential problem owns the next action.
    else if(missing.length) content=`<h2>Add product images.</h2><p>Select a photo, or choose to generate one.</p><ul class="pr-needed-products">${missing.map(p=>`<li><strong>${esc(p.name)}</strong>${button('Choose image','choose','pr-small',byId(p.id))}</li>`).join('')}</ul>`;
    else if(v.planState==='complete') content=`<h2>Your source images are available.</h2><p>${v.savedCount} campaign previews.</p>${button('Open Studio →','studio','pr-primary pr-wide')}<p class="pr-operation-detail">The finished images are created when you download.</p>`;
    else if(v.planState==='error'&&v.planError?.proposal) content='';
    else if(v.planState==='error') content=`<h2>${v.planError?.proposal||v.planError?.fields?.length?'Check the campaign details.':'Couldn’t prepare the estimate.'}</h2>${!v.planError?.proposal?`<p>${esc(v.planError?.message||'Finish saving your changes before preparing this estimate.')}</p>`:''}${v.planError?.fields?.length?button('Edit these details','plan-input','pr-primary pr-wide'):v.planError?.proposal?'':button('Try again','review-plan','pr-primary pr-wide')}`;
    else if(v.planState==='pending') content=`<h2>Waiting for changes to save.</h2><p class="pr-source-summary">${esc(v.sourceSummary)}</p>${button(req?'Generate missing images':'Prepare source images','start','pr-primary pr-wide','disabled')}`;
    else if(v.planState==='preparing') content=`<h2>Preparing estimate…</h2><p class="pr-source-summary">${esc(v.sourceSummary)}</p>${button(req?'Generate missing images':'Prepare source images','start','pr-primary pr-wide','disabled')}`;
    else content=`<h2>${req?'Ready to generate.':'Prepare your source images.'}</h2><p class="pr-source-summary">${esc(v.sourceSummary)}</p>${req?`<div class="pr-inline-estimate"><strong>${esc(v.plan.estimate)}</strong><span>Estimated AI cost<small>${req} new AI ${req===1?'request':'requests'} · actual charges may differ.</small></span></div>`:'<p>Uses your existing imagery. No AI requests.</p>'}${v.plan.keyMissing?`<p class="pr-key-needed">Add an API key to generate a new image.</p>${button('Add OpenAI API key','key','pr-primary pr-wide')}`:button(req?'Generate missing images':'Prepare source images','start','pr-primary pr-wide',v.plan.busy?'disabled':'')}<p class="pr-export-count">${total} images at export</p>${requestDetails(v)}`;
    return `<aside class="pr-action-panel">${content}</aside>`;
  }
  function productEditor(v) {
    const p=v.product,existing=v.existing,photo=p.mode!=='concept',needsImage=photo&&!p.image;
    const errors=v.invalidFields||[];
    const invalid=key=>errors.includes(key)?'aria-invalid="true" aria-describedby="pr-product-error"':'';
    return `<div class="pr-overlay"><section class="pr-editor pr-editor-stable modal" role="dialog" aria-modal="true" aria-labelledby="pr-editor-title"><header class="modal-head"><div><div class="pr-eyebrow">${esc(v.origin==='campaign'?'CAMPAIGN / PRODUCTS':'PRODUCTION / PRODUCTS')}</div><h2 id="pr-editor-title">${existing?'Edit product.':'Add a product.'}</h2></div>${button('×','dismiss','pr-close','aria-label="Close product editor"')}</header>
    <div class="pr-editor-body modal-body" ${v.adding?'inert':''}>${v.error?`<div id="pr-product-error" class="pr-product-error" role="alert" tabindex="-1"><span aria-hidden="true">!</span><div><strong>Check the product details.</strong><p>${esc(v.error)}</p></div></div>`:''}<section class="pr-product-fields"><label>Product name<input id="product-name" data-pr-input="name" value="${esc(p.name)}" ${invalid('name')}></label><label><span id="pr-description-label">Description and distinguishing details</span><textarea aria-labelledby="pr-description-label" id="product-description" data-pr-input="description" rows="4" ${invalid('description')} ${!photo&&!errors.includes('description')?'aria-describedby="pr-description-hint"':''}>${esc(p.description)}</textarea>${!photo?'<small id="pr-description-hint" class="pr-field-hint">Used with your campaign direction to generate the image.</small>':''}</label><label>Asset origin<input id="product-origin" data-pr-input="origin" value="${esc(p.origin)}"></label><label>Rights assertion<textarea id="product-rights" data-pr-input="rights" rows="2">${esc(p.rights)}</textarea></label></section>
    <section class="pr-image-choice"><h3>Product image</h3>${photo&&p.treatment!=='prepared'?'<p class="pr-image-requirement">Use an image with a background and no transparent areas.</p>':''}<div class="pr-mode-choice" role="group" aria-label="Image source"><button type="button" data-pr-action="mode-photo" aria-pressed="${photo}">Select existing image</button><button type="button" data-pr-action="mode-concept" aria-pressed="${!photo}">Generate new image</button></div>
    ${photo?`<div class="pr-chosen-image ${needsImage?'pr-no-image':''}">${p.image?`<img src="${esc(p.image)}" alt="Selected photo for ${esc(p.name||'this product')}">`:`<strong>Choose the photo you want to use.</strong><p>Browse anywhere on your computer.</p>${button('Choose image…','browse','pr-primary')}`}</div>
    <div class="pr-image-file">${v.uploading?'<span role="status">Checking photo…</span>':''}<span>${p.image?`<strong>${esc(p.filename||'Selected product photo')}</strong><small>PNG or JPEG · original preserved</small>`:'PNG or JPEG · up to 20 MiB / 40 MP'}</span>${p.image?button('Choose different image…','browse','pr-small'):''}</div>
    ${!p.image&&p.previousImage?`<div class="pr-previous-image"><img src="${esc(p.previousImage)}" alt="Previous campaign photo"><div><strong>Your previous campaign image</strong><small>${esc(p.previousFilename||p.name)}</small></div>${button('Use this image','restore','pr-small')}</div>`:''}`:
    `<div class="pr-concept-panel"><span aria-hidden="true">✳</span><h3>Describe the image you want.</h3><p>Include its appearance, setting, and details that matter. Review the exact prompt and cost in Production before creating it.</p><small>Generated product appearance is illustrative.</small></div>${p.previousImage?`<div class="pr-previous-image"><img src="${esc(p.previousImage)}" alt="Previous campaign photo"><div><strong>Your image is kept.</strong><small>Use it again whenever you need it.</small></div>${button('Use this image','restore','pr-small')}</div>`:''}`}
    <input type="file" accept="image/png,image/jpeg" data-pr-file hidden></section></div>
    <footer class="modal-foot"><div>${existing?button('Remove product','remove','pr-danger pr-small'):''}</div><div>${existing?'':button('Cancel','cancel')}${button(v.adding?'Adding product…':v.uploading?'Uploading photo…':existing?'Done':'Add product','save','pr-primary',v.busy?'disabled':'')}</div></footer></section></div>`;
  }
  function removeProductDialog(v) {
    return `<div class="pr-overlay"><section class="pr-cap-dialog pr-remove-dialog" role="dialog" aria-modal="true" aria-labelledby="pr-remove-title"><div class="pr-eyebrow">REMOVE PRODUCT</div><h2 id="pr-remove-title">Remove ${esc(v.name)}?</h2><p>This removes the product and its image settings from this campaign.</p><div class="pr-remove-actions">${button('Cancel','cancel-remove')}${button('Remove product','confirm-remove','pr-primary')}</div></section></div>`;
  }
  function studio(v) {
    const all=v.studioView==='images';
    const nav=`<div class="pr-studio-toolbar"><div class="pr-tabs" role="tablist" aria-label="Studio views">${[['products','All products'],['images','All images'],['compare','Compare']].map(([id,label])=>`<button role="tab" aria-selected="${v.studioView===id}" data-pr-action="studio-${id}">${label}</button>`).join('')}</div><div class="pr-toolbar-right">${marketSelect(v.markets,all?v.imageMarket:v.market,{all,action:all?'image-market':'market'})}${button('Production record','record','pr-small pr-quiet')}</div></div>`;
    return heading('Shape the final image.','03 / STUDIO','Edit wording, fonts, and colors. Move and resize text and logos, or adjust the crop and zoom.',`<div class="wf-sequence-actions">${button('← Production','previous-production','pr-small wf-previous')}${button('Next: Handoff →','handoff','pr-primary')}</div>`)+nav+(all?allImages(v):v.studioView==='compare'?compare(v):studioProducts(v));
  }
  function studioProducts(v) {
    return `<div class="pr-studio-products">${v.products.map(p=>`<article class="pr-studio-card"><button class="pr-studio-hero ${p.sourceAvailable?'':'pr-add-image'}" data-pr-action="${p.sourceAvailable?'open-product':'add-image'}" ${byId(p.id)} aria-label="${p.sourceAvailable?'Edit images':'Add image'} for ${esc(p.name)}">${p.outputs?.[v.market+'|1:1']?`<img src="${esc(p.outputs[v.market+'|1:1'])}" alt="${esc(p.name)} Square, ${esc(v.markets.find(m=>m.id===v.market)?.label)}">`:p.sourceAvailable?'<span>Adjust this layout in Studio</span>':missingImageContent()}</button><div class="pr-studio-info"><h2>${esc(p.name)}</h2><div class="pr-mini-formats">${formats.map(f=>`<button data-pr-action="${p.sourceAvailable?'open-format':'add-image'}" ${byId(p.id)} data-pr-format="${f.id}" aria-label="${p.sourceAvailable?'Open':'Add image for'} ${esc(p.name)} ${f.name}"><span>${p.outputs?.[v.market+'|'+f.id]?`<img src="${esc(p.outputs[v.market+'|'+f.id])}" alt="">`:p.sourceAvailable?'Adjust layout':'+'}</span><small>${f.name}</small></button>`).join('')}</div>${button(p.sourceAvailable?'Edit images →':'Add image →',p.sourceAvailable?'open-product':'add-image','pr-small pr-wide',byId(p.id))}</div></article>`).join('')}</div>`;
  }
  function allImages(v) {
    const entries=v.products.flatMap(p=>v.markets.filter(m=>v.imageMarket==='all'||v.imageMarket===m.id).flatMap(m=>formats.map(f=>({p,m,f,url:p.outputs?.[m.id+'|'+f.id]}))));
    const filtered=v.onlyUnavailable?entries.filter(e=>!e.url):entries,per=12,pages=Math.max(1,Math.ceil(filtered.length/per)),page=Math.min(Math.max(v.imagePage||0,0),pages-1),first=page*per;
    return `<div class="pr-image-pagination"><strong>${filtered.length?`${first+1}–${Math.min(first+per,filtered.length)} of ${filtered.length} images`:'No matching images'}</strong><label class="pr-filter"><input type="checkbox" data-pr-input="only-unavailable" ${v.onlyUnavailable?'checked':''}> Unavailable only</label><div><span>Page ${page+1} of ${pages}</span>${button('← Previous','image-prev','pr-small',page===0?'disabled':'')}${button('Next →','image-next','pr-small',page===pages-1?'disabled':'')}</div></div>
    <div class="pr-contact-sheet">${filtered.slice(first,first+per).map(e=>`<button class="pr-image-tile" data-pr-action="${e.p.sourceAvailable?'open-output':'add-image'}" ${byId(e.p.id)} data-pr-market="${e.m.id}" data-pr-format="${e.f.id}"><span class="pr-image-stage">${e.url?`<img src="${esc(e.url)}" alt="${esc(e.p.name)}, ${e.f.name}, ${esc(e.m.label)}">`:e.p.sourceAvailable?'<strong>Adjust this layout in Studio</strong>':missingImageContent()}</span><span class="pr-image-caption"><strong>${esc(e.p.name)}</strong><small>${e.f.name} · ${esc(e.m.label)}</small></span></button>`).join('')}</div>`;
  }
  function compare(v) {
    const selected=(v.compareIds||v.products.slice(0,2).map(p=>p.id)).map(id=>v.products.find(p=>p.id===id)).filter(Boolean);
    return `<div class="pr-compare-head"><p>Compare two products in the same market and format.</p><label class="pr-select-label"><span>Format</span><select data-pr-select="compare-format">${formats.map(f=>`<option value="${f.id}" ${v.format===f.id?'selected':''}>${f.name} ${f.id}</option>`).join('')}</select></label></div><div class="pr-compare-grid">${selected.map((p,i)=>`<section><select data-pr-select="compare-${i}" aria-label="${i?'Right':'Left'} comparison product">${v.products.map(q=>`<option value="${esc(q.id)}" ${p.id===q.id?'selected':''}>${esc(q.name)}</option>`).join('')}</select><div class="pr-compare-stage">${p.outputs?.[v.market+'|'+v.format]?`<img src="${esc(p.outputs[v.market+'|'+v.format])}" alt="${esc(p.name)}">`:p.sourceAvailable?'Adjust this layout in Studio':button(missingImageContent(),'add-image','pr-add-image',byId(p.id))}</div><div class="pr-compare-actions" role="group" aria-label="Actions for ${esc(p.name)}">${[['text','Edit text'],['framing','Adjust framing'],['inspect','Inspect']].map(([action,label])=>imageAction(v,p,v.market,v.format,action,label,'pr-small')).join('')}</div></section>`).join('')}</div>`;
  }
  scope.MotifProducerRepair={esc,button,formats,heading,marketSelect,capDialog,formatSet,productCards,production,actionPanel,credentialPanel,requestDetails,productEditor,removeProductDialog,studio};
})(window);
