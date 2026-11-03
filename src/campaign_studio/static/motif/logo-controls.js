/* Motif Logo Controls. Presentation and direct manipulation only.
   The host owns upload validation, exact preview digests, persistence, and revisions. */
(function (scope) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clone = value => JSON.parse(JSON.stringify(value));
  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
  const dimensions = (p,t,a) => ({...p,height:p.width*t.width*a.height/a.width/t.height});
  function constrain(p,t,a) {
    const aspect=a.width/a.height;
    const width=clamp(p.width,Math.max(1,aspect)/t.width,Math.min(1,t.height*aspect/t.width));
    const height=width*t.width/aspect/t.height;
    return {x:clamp(p.x,0,1-width),y:clamp(p.y,0,1-height),width};
  }
  function move(p,dx,dy,t,a) { return constrain({...p,x:p.x+dx/t.width,y:p.y+dy/t.height},t,a); }
  function resize(p,corner,dx,dy,t,a) {
    const aspect=a.width/a.height,sx=corner.includes('w')?-1:1,sy=corner.includes('n')?-1:1;
    const w=p.width*t.width,h=w/aspect,x=p.x*t.width,y=p.y*t.height;
    const ax=sx<0?x+w:x,ay=sy<0?y+h:y;
    const maxWidth=Math.min(sx<0?ax:t.width-ax,(sy<0?ay:t.height-ay)*aspect);
    const delta=(sx*dx+sy*dy/aspect)/(1+1/(aspect*aspect));
    if(Math.abs(delta)<1e-9)return {...p}; // Selecting a corner alone does not resize old small artwork.
    const minWidth=Math.max(1,aspect,.05*Math.min(t.width,t.height)*Math.min(1,aspect));
    const width=clamp(w+delta,minWidth,maxWidth);
    return {x:(sx<0?ax-width:ax)/t.width,y:(sy<0?ay-width/aspect:ay)/t.height,width:width/t.width};
  }
  function fitReplacement(p,t,oldAsset,newAsset) {
    const aspect=newAsset.width/newAsset.height;
    // Preserve the existing top-left; reduce only when the new proportions need room.
    return {x:p.x,y:p.y,width:Math.min(p.width,1-p.x,(1-p.y)*t.height*aspect/t.width)};
  }
  function defaultPlacement(t,a) {
    const scale=Math.min(1,t.width*.15/a.width,t.height*.09/a.height);
    const w=Math.round(a.width*scale),h=Math.round(a.height*scale),margin=Math.round(Math.min(t.width,t.height)*.06);
    return {x:(t.width-margin-w)/t.width,y:(t.height-margin-h)/t.height,width:w/t.width};
  }
  // Shared screen-space chrome never becomes artwork placement.
  const selectionFrame = (rect, canvas) => scope.MotifManipulation.selectionFrame(rect, canvas);
  const geometry={dimensions,constrain,move,resize,fitReplacement,defaultPlacement,selectionFrame};
  const button=(text,action,style='',extra='')=>`<button type="button" class="btn ${style}" data-lg="${action}" ${extra}>${text}</button>`;
  class Editor {
    constructor(options) {
      this.o=options;this.root=options.root;this.target=clone(options.target);this.asset=options.asset?clone(options.asset):null;
      this.savedAsset=this.asset?clone(this.asset):null;this.p=clone(options.placement||options.defaultPlacement||{x:.78,y:.89,width:.15});
      this.initial=clone(this.p);this.default=clone(options.defaultPlacement||this.p);this.pendingAsset=false;this.replacementFit=null;this.valid=options.savedDisplay;this.reviewRequired=false;this.reviewing=false;this.reviewMessage='';this.busy=false;this.uploading=false;this.uploadSeq=0;this.open=true;this.seq=0;this.raf=0;this.returnFocus=document.activeElement;
      this.message=this.asset?'':'Attach a PNG or JPEG to begin.';
      if(this.o.onMount)this.o.onMount(this.markup());else this.root.innerHTML=this.markup();this.bind();this.fit();this.paint();
      this.observer=new ResizeObserver(()=>this.fit());this.observer.observe(this.root.querySelector('.mf-logo-stage'));
      if (!this.returnFocus?.matches('[data-ws-target]')) queueMicrotask(() => {
        if (this.open) this.root.querySelector(this.asset ? '[data-lg-box]' : '[data-lg="replace"]').focus();
      });
    }
    dirty() { return this.pendingAsset||JSON.stringify(this.p)!==JSON.stringify(this.initial); }
    signature() { return JSON.stringify({target:this.target,placement:this.p,replacementFit:this.replacementFit,asset:this.asset?.reference,mode:this.pendingAsset?'replacement':'placement'}); }
    markup() {
      const t=this.target;
      return `<div class="overlay mf-logo-overlay"><section class="modal wide mf-logo-modal mf-editor is-direct" role="dialog" aria-modal="true" aria-labelledby="mf-logo-title"><header class="modal-head"><div><div class="eyebrow">${esc(t.campaignName)}</div><h2 id="mf-logo-title">Put your mark on it.</h2></div><button type="button" class="close" data-lg="dismiss" aria-label="Close dialog">×</button></header><div class="modal-body mf-logo-body mf-editor-grid"><aside class="mf-logo-tools mf-editor-tools"><section class="mf-logo-target"><span class="eyebrow">THIS IMAGE</span><h3>${esc(t.productName)}</h3><p>${esc(t.marketLabel)} · ${esc(t.formatName)} ${esc(t.ratio)}</p><p class="small muted">Position and size apply here only.</p></section><section class="mf-logo-asset"><div class="mf-logo-asset-art" data-lg-asset-preview></div><strong data-lg-filename></strong><span class="small muted" data-lg-profile>Original artwork · proportions kept</span>${button('Replace logo','replace','small')}<p class="mf-logo-scope-note">New artwork applies campaign-wide.</p></section><section class="mf-logo-instructions"><h3>Drag to move.<br>Pull a corner to resize.</h3><div class="mf-logo-key-row"><kbd>↑ ↓ ← →</kbd><span>Move the selected logo</span></div><div class="mf-logo-key-row"><kbd>Tab</kbd><span>Focus a corner; arrows resize</span></div><p class="small muted">1 output pixel per step. Hold Shift for 10.</p></section><div class="mf-logo-reset">${button('Reset placement','reset','small quiet')}<p class="small muted">Default placement for this image.</p></div></aside><section class="mf-logo-preview mf-editor-preview"><div class="mf-logo-preview-head mf-preview-head"><span class="eyebrow" data-lg-preview-label>LIVE LOGO PREVIEW</span></div><div class="mf-logo-stage"><div class="mf-logo-canvas"><img class="mf-logo-photo" src="${esc(this.o.previewUrl)}" alt="Composition preview for ${esc(t.productName)}"><div class="mf-logo-art" data-lg-art><img alt="Campaign logo" draggable="false"></div><img class="mf-logo-copy" src="${esc(this.o.copyOverlayUrl)}" alt="${esc(this.o.copyDescription||'Supplied campaign wording, above the logo')}" draggable="false"><img class="mf-logo-composite" alt="Exact local composition preview" hidden><div class="mf-logo-selection" data-lg-box tabindex="0" role="group" aria-label="Logo selection. Drag inside to move; corners resize. Arrow keys move the logo; Shift moves ten output pixels. Tab to a corner to resize." aria-describedby="mf-logo-help"><span class="mf-logo-selection-label" aria-hidden="true">LOGO</span>${['nw','ne','sw','se'].map(c=>`<button type="button" class="mf-logo-handle ${c}" data-lg-corner="${c}" data-mf-corner="${c}" aria-label="Resize from ${({nw:'top left',ne:'top right',sw:'bottom left',se:'bottom right'})[c]}. Arrow keys resize proportionally. Hold Shift for ten pixels."></button>`).join('')}</div><div class="mf-logo-empty"><span class="empty-symbol">＋</span><h3>Your logo belongs here.</h3><p>Add the campaign’s PNG or JPEG to position it on this image.</p>${button('Attach logo','replace','primary small')}</div></div><span class="mf-logo-gesture" hidden></span></div><div class="mf-logo-preview-note"><p id="mf-logo-help" data-lg-status role="status" aria-live="polite"></p><span class="mf-logo-dirty" data-lg-dirty>Not saved</span></div></section></div><footer class="modal-foot"><div class="mf-impact" data-lg-context>${esc(t.marketLabel)} · ${esc(t.ratio)}</div><div class="row">${button('Cancel','cancel')}${button('Review latest','review-latest','primary','hidden data-lg-review-persistent')}${button('Apply logo','save','primary','disabled')}</div></footer></section></div>`;
    }
    bind() {
      this.click=e=>{const b=e.target.closest('[data-lg],[data-sc]');if(!b||!this.root.contains(b))return;const action=b.dataset.lg||b.dataset.sc;
        if(action==='cancel')this.close();if(action==='dismiss')this.dismiss();if(action==='reset')this.reset();if(action==='replace'||action==='choose-image'){this.closeSub(false);this.pick();}if(action==='save')this.pendingAsset?this.confirmReplacement():this.save();if(action==='confirm-replacement')this.save();if(action==='return-product'||action==='back-editor')this.closeSub();if(action==='review-latest')this.reviewLatest();};
      this.root.addEventListener('click',this.click);
      this.key = event => {
        if (event.key === 'Tab') { this.trap(event); return; }
        if (event.key !== 'Escape') return;
        event.preventDefault(); event.stopPropagation();
        if (this.busy || this.reviewing) return;
        if (this.input.cancel()) return;
        if (this.sub) this.closeSub(); else this.close();
      };
      this.root.addEventListener('keydown', this.key);
      this.input = scope.MotifManipulation.bind({
        element: this.root.querySelector('[data-lg-box]'),
        canvas: this.root.querySelector('.mf-logo-canvas'),
        outputSize: () => this.target,
        enabled: () => this.open && !this.tearingDown && !this.busy && !this.uploading && !this.reviewing && !this.sub && !!this.asset,
        read: () => ({ placement: clone(this.p), fit: this.replacementFit ? clone(this.replacementFit) : null }),
        write: value => {
          if (this.tearingDown) return;
          this.p = clone(value.placement); this.replacementFit = value.fit ? clone(value.fit) : null;
          this.changed();
        },
        move: (start, dx, dy) => ({ ...start, placement: move(start.placement, dx, dy, this.target, this.asset) }),
        resize: (start, corner, dx, dy) => ({ ...start, placement: resize(start.placement, corner, dx, dy, this.target, this.asset) }),
        resizeKey: (start, corner, { dx, dy }) => {
          const aspect = this.asset.width / this.asset.height;
          const sx = corner.includes('w') ? -1 : 1, sy = corner.includes('n') ? -1 : 1;
          const dw = dx ? sx * dx : sy * dy * aspect;
          return { ...start, placement: resize(start.placement, corner, sx * dw, sy * dw / aspect, this.target, this.asset) };
        },
        onStart: () => { this.gesture = true; this.valid = null; clearTimeout(this.keyTimer); this.paint(); },
        onFinish: ({ cancelled, keyboard }) => {
          this.gesture = false;
          if (this.tearingDown) return;
          this.message = cancelled ? 'Move cancelled. Earlier edits are still here.' : 'Placement updated. Apply when it feels right.';
          this.paint(); clearTimeout(this.keyTimer);
          if (keyboard) this.keyTimer = setTimeout(() => this.validate(), 150);
          else this.validate();
        },
      });
    }
    fit() {
      if (!this.open) return;
      this.input?.cancel();
      const stage = this.root.querySelector('.mf-logo-stage'), canvas = this.root.querySelector('.mf-logo-canvas');
      const aspect = this.target.width / this.target.height;
      const inset = this.root.closest(".ws-workspace") ? 0 : 36;
      const width = Math.max(1, Math.min(stage.clientWidth - inset, (stage.clientHeight - inset) * aspect));
      canvas.style.width = width + 'px'; canvas.style.height = width / aspect + 'px'; this.paint();
    }
    changed() {
      // Fit disclosure describes this replacement's automatic reduction, not later arrangement.
      if(this.replacementFit){const fitted=fitReplacement(this.replacementFit,this.target,this.asset,this.asset);if(['x','y','width'].some(k=>Math.abs(fitted[k]-this.p[k])>1e-8))this.replacementFit=null;}
      this.valid=null;this.seq++;if(!this.raf)this.raf=requestAnimationFrame(()=>{this.raf=0;this.paint();}); }
    paint() {
      if(!this.open)return;const q=s=>this.root.matches(s)?this.root:this.root.querySelector(s),a=this.asset;
      q('[data-lg-art]').hidden=!a;q('[data-lg-box]').hidden=!a;q('.mf-logo-empty').hidden=!!a;
      if(a){const rect=this.valid?.resolvedRect;const d=rect?{x:rect.left/this.target.width,y:rect.top/this.target.height,width:rect.width/this.target.width,height:rect.height/this.target.height}:dimensions(this.p,this.target,a);const art=q('[data-lg-art]');art.style.left=d.x*100+'%';art.style.top=d.y*100+'%';art.style.width=d.width*100+'%';art.style.height=d.height*100+'%';
        const canvas=q('.mf-logo-canvas').getBoundingClientRect(),frame=selectionFrame({left:d.x*canvas.width,top:d.y*canvas.height,width:d.width*canvas.width,height:d.height*canvas.height},{width:canvas.width,height:canvas.height}),box=q('[data-lg-box]');
        box.style.left=frame.left+'px';box.style.top=frame.top+'px';box.style.width=frame.width+'px';box.style.height=frame.height+'px';box.classList.toggle('is-expanded',frame.expanded);
        box.classList.toggle('is-top-edge',frame.expanded&&frame.top<24);const img=q('[data-lg-art] img');if(img.getAttribute('src')!==a.url)img.src=a.url;}
      const composite=q('.mf-logo-composite');composite.hidden=!this.valid?.compositeUrl||!!this.gesture;if(!composite.hidden)composite.src=this.valid.compositeUrl;
      if(q('[data-lg-asset-preview]').dataset.asset!==(a?.url||'')){q('[data-lg-asset-preview]').dataset.asset=a?.url||'';q('[data-lg-asset-preview]').innerHTML=a?`<img src="${esc(a.url)}" alt="${esc(a.filename)}">`:'<span>No logo attached</span>';}
      q('[data-lg-profile]').textContent=`${a?.type||'Original artwork'} · proportions kept`;
      q('[data-lg-filename]').textContent=a?.filename||'Add your campaign logo';q('[data-lg-filename]').title=a?.filename||'';
      q('[data-lg="replace"]').textContent=a?'Replace logo':'Attach logo';
      q('.mf-logo-modal').setAttribute('aria-busy',String(this.busy||this.reviewing));q('[data-lg-status]').textContent=this.reviewing?'Reviewing the latest image…':this.reviewRequired?this.reviewMessage:this.message;
      q('[data-lg-preview-label]').textContent=this.reviewRequired?'LOCAL DRAFT PREVIEW':'LIVE LOGO PREVIEW';
      const review=q('[data-lg-review-persistent]');review.hidden=!this.reviewRequired&&!this.reviewing;review.disabled=this.busy||this.uploading||this.reviewing;
      q('[data-lg-dirty]').textContent=this.busy?'Applying…':!a?'No logo attached':this.pendingAsset?'New campaign artwork':this.dirty()?'Pending placement':'Applied placement';
      q('[data-lg-dirty]').classList.toggle('is-dirty',this.dirty());q('[data-lg-dirty]').hidden=!this.dirty()&&!this.busy;
      q('[data-lg="save"]').textContent=this.pendingAsset?'Review campaign change':this.busy?'Applying…':'Apply logo';
      q('[data-lg="save"]').disabled=this.busy||this.reviewing||this.reviewRequired||this.uploading||!a||!this.dirty()||!this.valid||this.valid.blocked;
      q('[data-lg="reset"]').disabled=this.busy||this.reviewing||!a||JSON.stringify(this.p)===JSON.stringify(this.default);
      q('[data-lg="replace"]').disabled=this.busy||this.reviewing;for(const el of this.root.querySelectorAll('[data-lg="cancel"],[data-lg="dismiss"]'))el.disabled=this.busy||this.reviewing;
      q('.mf-logo-gesture').hidden=!this.gesture;if(this.gesture)q('.mf-logo-gesture').textContent='Arranging logo';
      q('[data-lg-context]').textContent=`${this.target.marketLabel} · ${this.target.ratio}`;
    }
    requireReview(reason='conflict') {
      this.reviewRequired=true;this.valid=null;this.seq++;clearTimeout(this.keyTimer);
      this.reviewMessage=reason==='uncertain'?'Image update is unconfirmed. Keep arranging, then review the latest image before applying again.':'Your edits are kept. Keep arranging, then review the latest image before applying.';
      this.message=this.reviewMessage;this.paint();
    }
    async validate() {
      if(!this.open||this.tearingDown||!this.asset)return;
      if(this.reviewRequired){this.valid=null;this.paint();return;}
      const seq=++this.seq,signature=this.signature();this.valid=null;this.paint();
      try {const result=await this.o.onPreview({target:clone(this.target),asset:clone(this.asset),placement:clone(this.p),...(this.replacementFit?{replacementFit:clone(this.replacementFit)}:{}),mode:this.pendingAsset?'replacement':'placement'});
        if(!this.open||seq!==this.seq||signature!==this.signature())return;
        if(result.reviewRequired===true||result.code==='conflict'){this.requireReview();return;}
        this.valid={...result,signature};if(result.defaultPlacement)this.default=clone(result.defaultPlacement);if(result.blocked)this.message=result.message||'Preview unavailable. Your logo edits are still here.';this.paint();
      }catch(error){if(this.open&&seq===this.seq){if(error?.code==='conflict'||error?.reviewRequired===true)this.requireReview();else {this.message='Preview unavailable. Your logo edits are still here.';this.paint();}}}
    }
    reset() {if(!this.asset||this.busy||this.reviewing)return;this.p=constrain(clone(this.default),this.target,this.asset);this.message='Default placement restored in this preview. Apply to update the image.';this.changed();this.validate();}
    pick() {if(this.busy||this.reviewing)return;const input=document.createElement('input');input.type='file';input.accept='image/png,image/jpeg';input.addEventListener('change',()=>{if(input.files[0])this.selectFile(input.files[0]);},{once:true});input.click();}
    async selectFile(file) {
      const owner=this.target.campaignId,uploadSeq=++this.uploadSeq;this.uploading=true;this.message='Checking the selected image…';this.paint();try{const asset=await this.o.onSelectFile(file);if(!this.open||owner!==this.target.campaignId||uploadSeq!==this.uploadSeq)return;this.uploading=false;
        const old=this.asset,before=clone(this.p);this.replacementFit=null;this.asset=clone(asset);this.pendingAsset=true;this.p=old?fitReplacement(this.p,this.target,old,this.asset):constrain(defaultPlacement(this.target,this.asset),this.target,this.asset);if(old&&this.p.width<before.width-1e-10)this.replacementFit=before;this.default=clone(asset.defaultPlacement||defaultPlacement(this.target,this.asset));this.message='New artwork is staged. Review the campaign change before applying.';this.paint();await this.validate();
        // A dismissed editor must not focus a later editor sharing this host.
        if(!this.open||owner!==this.target.campaignId||uploadSeq!==this.uploadSeq)return;
        this.root.querySelector('[data-lg-box]')?.focus();
      }catch(error){if(this.open&&uploadSeq===this.uploadSeq){this.uploading=false;this.message='The selected image was not attached. Your earlier edits are still here.';this.paint();const imageFailure=['unsupported_type','decode_failure','no_visible_pixels','color_profile','size','pixels'].includes(error?.kind);if(error?.handled===true&&error.commitState==='not_started'&&!imageFailure){this.root.querySelector('[data-lg="replace"]')?.focus();return;}this.uploadError(error||{},file.name);}}
    }
    uploadError(error,filename) {
      const state=({no_visible_pixels:'image-empty',color_profile:'image-color-profile',unsupported_type:'image-unsupported',size:'image-bytes',pixels:'image-pixels'})[error.kind]||'image-corrupt';
      const view=scope.MotifStorage?.getView(state,{campaignName:this.target.campaignName,inputRole:'logo',filename,previousAttachment:this.asset?.filename||'',detectedType:error.detectedType||'Unsupported format',encodedBytes:error.encodedBytes||0,width:error.width||0,height:error.height||0});
      if(!view)throw error;let body=view.body.replace('Your typed fields and previous attachment are unchanged.','Your pending placement and previous attachment are unchanged.');if(state==='image-color-profile')body=body.replace('has a missing or unsupported color profile.','has an unsupported or invalid embedded color profile.');this.showSub(view.title,body,view.footer,'[data-sc="return-product"]');
    }
    confirmReplacement() {
      if(this.reviewRequired||this.reviewing||!this.valid||!this.dirty())return;const d=this.valid,t=this.target,missing=d.missingOutputs||0;
      this.showSub('Change the campaign logo?',`<p class="mf-logo-confirm-intro">Use <strong>${esc(this.asset.filename)}</strong> for <strong>${esc(t.campaignName)}</strong>.</p><div class="mf-logo-confirm-art"><div class="mf-logo-asset-art"><img src="${esc(this.asset.url)}" alt="New campaign logo"></div><div><span class="eyebrow">NEW ARTWORK</span><strong>${esc(this.asset.filename)}</strong><p class="small muted">Original proportions and colors retained.</p></div></div><div class="data-row"><span>Current images updated</span><b>${d.availableOutputs} of ${d.expectedOutputs}</b></div>${missing?`<div class="mf-logo-replacement-note"><strong>${missing} unavailable images stay unavailable.</strong><p>This change will not generate missing imagery.</p></div>`:''}${d.fittedOutputCount?`<div class="mf-logo-fit-note"><strong>Resized to fit · ${d.fittedOutputCount} ${d.fittedOutputCount===1?'image':'images'}</strong><p>Artwork was reduced where needed to stay inside the image. Positions are unchanged.</p></div>`:""}<div class="data-row"><span>Placement changed</span><b>This image only</b></div><p class="small muted">${esc(t.productName)} · ${esc(t.marketLabel)} · ${esc(t.formatName)} ${esc(t.ratio)}</p><div class="rule"></div><p>Other positions are retained, with only the fitting shown above. Other brief edits are preserved and are not applied to these images.</p><p class="small muted" style="margin-top:12px">This updates the current available images. Your supplied source files and existing downloads are unchanged.</p>`,button('Keep editing','back-editor')+button('Replace campaign logo','confirm-replacement','primary'));
    }
    async save() {
      if(this.busy||this.reviewing||this.reviewRequired||this.uploading||!this.valid||this.valid.blocked||!this.dirty()||this.valid.signature!==this.signature())return;
      const signature=this.signature(),body={target:clone(this.target),asset:clone(this.asset),placement:clone(this.p),...(this.replacementFit?{replacementFit:clone(this.replacementFit)}:{}),mode:this.pendingAsset?'replacement':'placement',previewDigest:this.valid.digest};
      this.closeSub(false);this.busy=true;this.message=body.mode==='replacement'?'Updating the current available images locally…':'Applying this logo locally…';this.paint();
      try{const result=await this.o.onSave(body);if(!this.open||signature!==this.signature())return;this.busy=false;this.initial=clone(this.p);this.savedAsset=clone(this.asset);this.pendingAsset=false;this.replacementFit=null;this.target={...this.target,...result.target};
        // Keep the accepted composition and controls in place, but retire the
        // submitted preview authorization. The next edit must preview anew.
        this.valid={...this.valid,compositeUrl:result.imageUrl,signature:null,digest:null};this.message='';this.paint();
        this.o.onSaved?.({...result,mode:body.mode});this.settleSwitch(true);return true;
      }catch(error){this.settleSwitch(false);if(!this.open||signature!==this.signature())return;this.busy=false;if(error?.status===409&&error.code==='apply_not_accepted'){this.message=scope.MotifEditorControls.copy.applyFailed;this.paint();this.root.querySelector('[data-lg-box]')?.focus();return;}if(error?.status===409&&error.code==='no_changes'){this.message='This logo already matches the saved image. No changes were applied. Your pending edits are kept.';this.paint();this.root.querySelector('[data-lg-box]')?.focus();return;}this.valid=null;this.paint();if(error?.handled===true&&error.commitState==='not_started'){this.message='Image update did not start. Your logo edits are still here.';this.paint();this.root.querySelector('[data-lg-box]')?.focus();return;}if(error?.code==='conflict')this.conflict(error);else {this.requireReview('uncertain');this.showSub('Your logo edits are still here.',`<p>The image update could not be confirmed. Review the current image before trying again.</p><div class="rule"></div><p class="small muted">Your placement and selected artwork are preserved. No new image request was made.</p>`,button('Keep my work','back-editor')+button('Review latest','review-latest','primary'));}}
    }
    conflict(error={}) {this.requireReview();this.showSub('The campaign has changed.',`<div class="notice block"><span class="icon">↗</span><div><strong>Your logo edits are still here.</strong><p>Review the latest image before applying them.</p></div></div><div class="data-row"><span>You opened</span><b>Revision ${esc(this.target.revisionNumber)}</b></div><div class="data-row"><span>Latest saved version</span><b>Revision ${esc(error.latestRevisionNumber||Number(this.target.revisionNumber)+1)}</b></div><p class="small muted" style="margin-top:18px">No newer campaign changes have been overwritten. Keep your draft here, then review the latest artwork and affected images.</p>`,button('Keep my work','back-editor')+button('Review latest','review-latest','primary'));}
    async reviewLatest() {
      if(!this.o.onReviewLatest||this.busy||this.uploading||this.reviewing)return;
      this.reviewing=true;this.paint();
      try {
        // Host must show the newer composition and obtain explicit adoption before reviewed:true.
        const result=await this.o.onReviewLatest({target:clone(this.target),asset:clone(this.asset),placement:clone(this.p),...(this.replacementFit?{replacementFit:clone(this.replacementFit)}:{}),mode:this.pendingAsset?'replacement':'placement'});
        if(!this.open)return;
        this.reviewing=false;
        if(!result?.reviewed){this.closeSub(false);this.paint();this.root.querySelector('[data-lg-review-persistent]')?.focus();return;}
        this.target={...this.target,...result.target};
        if(result.previewUrl){this.o.previewUrl=result.previewUrl;this.root.querySelector('.mf-logo-photo').src=result.previewUrl;}
        if(result.copyOverlayUrl){this.o.copyOverlayUrl=result.copyOverlayUrl;this.root.querySelector('.mf-logo-copy').src=result.copyOverlayUrl;}
        if(result.asset&&!this.pendingAsset)this.asset=clone(result.asset);
        this.reviewRequired=false;this.reviewMessage='';
        this.closeSub(false);this.message='Latest image reviewed. Your pending logo edits are preserved; preview and apply them explicitly.';await this.validate();this.paint();
        this.root.querySelector(this.reviewRequired?'[data-lg-review-persistent]':this.asset?'[data-lg-box]':'[data-lg="replace"]')?.focus();
      }catch(error){if(this.open){this.reviewing=false;if(!this.reviewRequired)this.requireReview();this.reviewMessage='Latest image unavailable. Your draft is kept. Review latest to try again.';this.closeSub(false);this.paint();this.root.querySelector('[data-lg-review-persistent]')?.focus();}}
      finally{this.reviewing=false;}
    }
    showSub(title,body,footer,focus='[data-lg="back-editor"]') {
      this.closeSub(false);
      this.subSiblings=(this.o.onMount?[...this.root.children]:[this.root.querySelector('.mf-logo-modal')]).map(el=>({el,inert:el.inert,hidden:el.getAttribute('aria-hidden')}));
      for(const {el} of this.subSiblings){el.inert=true;el.setAttribute('aria-hidden','true');}
      const el=document.createElement('div');el.className='overlay mf-logo-sub';el.innerHTML=`<section class="modal" role="dialog" aria-modal="true" aria-labelledby="mf-logo-subtitle"><header class="modal-head"><div><div class="eyebrow">${esc(this.target.campaignName)}</div><h2 id="mf-logo-subtitle">${esc(title)}</h2></div><button type="button" class="close" data-lg="back-editor" aria-label="Close dialog">×</button></header><div class="modal-body">${body}</div><footer class="modal-foot">${footer}</footer></section>`;this.root.append(el);this.sub=el;queueMicrotask(()=>el.querySelector(focus)?.focus());
    }
    closeSub(focus=true) {if(focus)this.settleSwitch(false);if(this.sub){this.sub.remove();this.sub=null;for(const {el,inert,hidden} of this.subSiblings){el.inert=inert;if(hidden===null)el.removeAttribute('aria-hidden');else el.setAttribute('aria-hidden',hidden);}this.subSiblings=null;if(focus)this.root.querySelector(this.reviewRequired?'[data-lg-review-persistent]':'[data-lg="replace"]').focus();}}
    trap(e) {const area=this.sub||(this.o.onMount?this.root:this.root.querySelector('.mf-logo-modal'));const els=[...area.querySelectorAll('button:not(:disabled),[tabindex="0"]')].filter(el=>el.getClientRects().length&&!el.closest('[hidden]'));if(!els.length)return;const first=els[0],last=els.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}
    settleSwitch(applied) {
      const resolve = this.switchResolve; this.switchResolve = null; resolve?.(applied);
    }
    applyForSwitch() {
      if (this.busy || this.reviewRequired || this.uploading || !this.valid || this.valid.blocked) return Promise.resolve(false);
      if (!this.pendingAsset) return this.save();
      return new Promise(resolve => { this.switchResolve = resolve; this.confirmReplacement(); });
    }
    dismiss() { this.close(); }
    close(cancelled=this.dirty()) {
      if(this.busy||this.reviewing||!this.open)return;
      // Teardown owns only this transient session. It cannot persist a draft or
      // let a late preview/upload reopen the closed dialog.
      this.tearingDown=true;this.closeSub(false);this.input?.destroy();this.open=false;this.settleSwitch(false);
      this.seq++;clearTimeout(this.keyTimer);cancelAnimationFrame(this.raf);this.observer?.disconnect();
      this.root.removeEventListener('click',this.click);this.root.removeEventListener('keydown',this.key);if(this.o.onUnmount)this.o.onUnmount();else this.root.innerHTML='';
      this.o.onClose?.({cancelled});
      if(this.returnFocus?.isConnected&&this.returnFocus!==document.body)this.returnFocus.focus({preventScroll:true});else this.o.onReturnFocus?.();
    }
  }
  const api={mount:options=>new Editor(options),geometry};scope.MotifLogo=api;if(typeof module!=='undefined')module.exports=api;
})(typeof window!=='undefined'?window:globalThis);
