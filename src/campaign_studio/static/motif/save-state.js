/* Motif Save presentation. The host owns the one saveProject function. */
(function(scope){
 'use strict';
 const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const phase={dirty:'waiting',saving:'saving',saved:'saved',error:'failure',conflict:'conflict'};
 const labels={dirty:'Unsaved changes',saving:'Saving…',saved:'Saved',error:'Not saved',conflict:'Save paused'};
 const details={
  dirty:['Your latest edits are waiting to save.','Saving starts after a brief pause. Use Save whenever you want. Incomplete fields and pending edits can be saved.'],
  saving:['Saving your current work.','You can keep editing. Saved appears when the current work is confirmed in this workspace.'],
  saved:['Your current work is saved.','Unfinished fields and pending edits are included. Images change when you apply an edit or create outputs.'],
  error:['Your latest edits are not confirmed saved.','Your work is still here. Use Save to try again. Keep this tab open until saving succeeds.'],
  conflict:['Newer changes were saved elsewhere.','Your edits are still in this tab. Automatic saving is paused. Review the saved work before choosing what to keep.']
 };
 function mount(root,host,options={}){
  let state=null,open=false,live='',closed=false;
  root.classList.add('mf-save-host');
  root.innerHTML=`<div class="mf-save-cluster"><button type="button" class="mf-save-status" data-hs="details" aria-expanded="false"><span class="mf-save-symbol" aria-hidden="true"></span><span data-hs-label></span></button><button type="button" class="btn small mf-save-button" data-hs="save">Save</button><button type="button" class="btn small mf-save-review" data-hs="review" hidden>Review update</button></div><span class="mf-save-live" role="status" aria-live="polite" aria-atomic="true"></span><section class="mf-save-details" role="region" aria-label="Campaign save details" hidden><header><strong></strong><button type="button" data-hs="close-details" aria-label="Close save details">×</button></header><p data-hs-body></p><p class="mf-save-meta" hidden></p></section>`;
  const q=s=>root.querySelector(s);
  function render(next){
   if(closed)return;state=next;root.hidden=!state;
   if(!state){open=false;live='';return;}
   if(!phase[state.phase])throw Error('Unknown Motif Save phase');
   root.dataset.saveState=phase[state.phase];q('[data-hs-label]').textContent=labels[state.phase];
   q('[data-hs="details"]').setAttribute('aria-label',labels[state.phase]+'. Campaign save details.');q('[data-hs="details"]').setAttribute('aria-expanded',String(open));
   // Keep keyboard focus on Save while its current request is unavailable.
   q('[data-hs="save"]').setAttribute('aria-disabled',String(['saving','conflict'].includes(state.phase)||state.busy===true||!!state.validationMessage));
   q('[data-hs="save"]').setAttribute('aria-label','Save '+(options.scopeLabel||'campaign'));
   q('[data-hs="save"]').title=state.validationMessage||'Save current work, including unfinished fields and pending edits';
   q('[data-hs="review"]').hidden=state.phase!=='conflict';q('[data-hs="review"]').disabled=state.busy===true;
   if(live!==labels[state.phase]){live=labels[state.phase];q('.mf-save-live').textContent=live;}
   const d=details[state.phase];q('.mf-save-details').hidden=!open;q('.mf-save-details strong').textContent=d[0];q('[data-hs-body]').textContent=state.validationMessage||state.message||d[1];
   const meta=q('.mf-save-meta');meta.hidden=state.phase!=='saved';meta.textContent='Saved to this workspace'+(state.savedAt?' · '+new Intl.DateTimeFormat(undefined,{hour:'numeric',minute:'2-digit'}).format(new Date(state.savedAt)):'');
  }
  const close=()=>{open=false;render(state);};
  const click=event=>{const b=event.target.closest('[data-hs]');if(!b||!root.contains(b))return;const a=b.dataset.hs;
   if(a==='details'){open=!open;render(state);}
   if(a==='close-details'){close();q('[data-hs="details"]').focus();}
   // The host publishes success/failure/conflict. A resolved promise alone never means Saved.
   if(a==='save'&&state&&!['saving','conflict'].includes(state.phase)&&state.busy!==true&&!state.validationMessage)Promise.resolve().then(()=>host.onSave()).catch(()=>{});
   if(a==='review')Promise.resolve().then(()=>host.onReviewConflict()).catch(()=>{});
  };
  const key=e=>{if(e.key==='Escape'&&open){e.stopPropagation();e.preventDefault();close();q('[data-hs="details"]').focus();}};
  const outside=e=>{if(open&&!root.contains(e.target))close();};
  root.addEventListener('click',click);root.addEventListener('keydown',key);document.addEventListener('pointerdown',outside);
  render(host.getSaveState());const unsubscribe=host.subscribeSaveState(render);
  return{refresh:()=>render(host.getSaveState()),closeDetails:close,destroy(){closed=true;unsubscribe?.();root.removeEventListener('click',click);root.removeEventListener('keydown',key);document.removeEventListener('pointerdown',outside);root.innerHTML='';}};
 }
 function mountRecovery(root,host){
  let state=null,closed=false,ownedFocus=false;
  const click=event=>{
   const button=event.target.closest('[data-save-recover]');
   if(!button||!root.contains(button)||button.disabled||!state?.problem)return;
   ownedFocus=true;
   Promise.resolve().then(()=>state.problem.phase==='conflict'?host.onReviewConflict():host.onSave()).catch(()=>{});
  };
  const focus=event=>{if(!root.contains(event.target))ownedFocus=false;};
  function render(next){
   if(closed)return;state=next;
   if(!state?.problem){
    const restore=root.contains(document.activeElement)||(ownedFocus&&document.activeElement===document.body);
    root.innerHTML='';ownedFocus=false;if(restore)host.onResolved?.();return;
   }
   if(!root.querySelector('[data-save-recover]'))root.innerHTML='<section class="mf-save-recovery" role="alert"><div><strong></strong><p></p></div><button type="button" class="pr-button" data-save-recover></button></section>';
   const conflict=state.problem.phase==='conflict',trying=state.phase==='saving'||state.busy;
   root.querySelector('strong').textContent=conflict?'Newer changes were saved elsewhere.':'Your latest changes couldn’t be saved.';
   root.querySelector('p').textContent=state.problem.message||(conflict?details.conflict[1]:'Keep this window open and try again.');
   const button=root.querySelector('[data-save-recover]');button.disabled=trying;
   button.textContent=trying?'Trying again…':conflict?'Review update':'Try again';
  }
  root.addEventListener('click',click);document.addEventListener('focusin',focus);
  render(host.getSaveState());const unsubscribe=host.subscribeSaveState(render);
  return{destroy(){closed=true;unsubscribe?.();root.removeEventListener('click',click);document.removeEventListener('focusin',focus);root.innerHTML='';}};
 }
 scope.MotifSave={mount,mountRecovery};
})(window);
