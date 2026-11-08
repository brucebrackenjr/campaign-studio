/* Motif Wording. Presentation only: no matching, storage, fetch, or export admission. */
(function(scope){
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const button=(label,action,primary=false,extra='')=>`<button type="button" class="btn small ${primary?'primary':''}" data-mw="${esc(action)}" ${extra}>${esc(label)}</button>`;
  // Consume the writer's existing wording_issue projection. This does not match text
  // or decide whether export is safe; the server owns those decisions.
  function fromIssue(issue){
    if(!issue)return {phase:'clear',cases:[]};
    const groups=new Map(),fields={message:'Headline',cta:'Call to action'},languages={'en-US':'English (United States)','es-MX':'Spanish (Mexico)'};
    for(const finding of issue.matches||[]){
      const context=finding.context||{},phrase=context.phrase;
      if(!phrase)continue;
      const productId=context.product_id||finding.item?.split('/')[0],market=context.market||phrase.market;
      for(const match of phrase.matches||[]){
        const id=[productId,market,match.field].map(encodeURIComponent).join(':');
        if(!groups.has(id))groups.set(id,{id,product:context.product_name||'Product',language:languages[market]||market,field:fields[match.field]||'Printed wording',terms:[],value:match.value,target:{productId,market,field:match.field,origin:match.origin,path:match.path},ruleIds:[]});
        const row=groups.get(id);
        if(row.value == null && match.value != null)row.value=match.value;
        if(!row.terms.includes(phrase.configured_phrase))row.terms.push(phrase.configured_phrase);
        if(!row.ruleIds.includes(finding.rule))row.ruleIds.push(finding.rule);
      }
    }
    return {phase:issue.pending_rules||issue.code==='pending_rules'?'rules-pending':groups.size?'blocked':'failed',cases:[...groups.values()]};
  }
  function notice({phase='clear',cases=[],index=0,surface='handoff',hasImages=true}={}){
    if(phase==='clear')return '';
    if(phase==='rules-pending')return `<section class="mw-notice mw-neutral" aria-label="Export availability"><div><h3>Save your prohibited words before export.</h3><p>Your latest rule changes are waiting to save.</p></div>${button('Try saving again','apply-rules',true)}</section>`;
    if(phase==='rules-invalid')return `<section class="mw-notice mw-neutral" aria-label="Export availability"><div><h3>Finish your prohibited word before export.</h3></div>${button('Edit prohibited words','edit-invalid-rule',true)}</section>`;
    if(phase==='pending')return `<section class="mw-notice mw-neutral" aria-label="Export availability" aria-busy="true"><div><h3>Checking current wording…</h3><p>Export is paused while the check runs.</p></div></section>`;
    if(phase==='stale'||phase==='failed')return `<section class="mw-notice mw-neutral" aria-label="Export availability"><div><h3>${phase==='stale'?'This campaign changed.':'The wording check could not finish.'}</h3><p>${phase==='stale'?'Refresh to check the current wording before export.':'Your work is still here. Try the check again before exporting.'}</p></div>${button(phase==='stale'?'Refresh current images':'Try again','refresh',true)}</section>`;
    const c=cases[index];if(!c)return '';
    const terms=c.terms.map(t=>`“${esc(t)}”`).join(', ');
    const field=c.field==='Call to action'?'call to action':'headline';
    const destination=!hasImages?'Campaign':'Studio';
    const subject=c.terms.length===1?`${terms} is`:`${terms} are`;
    const title=`Update the ${field}${surface==='handoff'?' before exporting':''}`;
    const body=`${subject} on this campaign’s prohibited-word list. Change ${c.terms.length===1?'it':'them'} in the ${esc(c.language)} ${field} for ${esc(c.product)}.`;
    const actions=`${button(`Edit ${field} in ${destination}`,'edit:'+c.id,true)}<button type="button" class="mw-link" data-mw="rules:${esc(c.id)}">Edit prohibited words</button>`;
    return `<section class="mw-notice" aria-labelledby="mw-title"><div class="mw-case"><h3 id="mw-title">${title}</h3><p>${body}</p></div>${actions?`<div class="mw-actions">${actions}</div>`:''}${cases.length>1?`<div class="mw-paging"><span aria-live="polite">${index+1} of ${cases.length} wording changes</span><div>${button('←','previous',false,`aria-label="Previous wording change" ${index===0?'disabled':''}`)}${button('→','next',false,`aria-label="Next wording change" ${index===cases.length-1?'disabled':''}`)}</div></div>`:''}</section>`;
  }
  function fieldMessage(terms=[]){return terms.length?`<p class="mw-field-error" id="mw-field-error">Remove ${terms.map(t=>`“${esc(t)}”`).join(', ')} or edit the prohibited words.</p>`:'';}
  function rules({language='English (United States)',words=[]}={}){
    return `<section class="mw-rules" aria-labelledby="mw-rules-title"><div class="row between"><h3 id="mw-rules-title">Prohibited words</h3>${button('＋ Add word or phrase','add-rule')}</div><p class="small muted">Keep these words and phrases out of ${esc(language)} images.</p>${words.length?`<div class="mw-rule-list">${words.map((w,i)=>`<button class="mw-rule" type="button" data-mw="rule:${i}" aria-label="Edit prohibited phrase ${esc(w)}">${esc(w)} <span aria-hidden="true">↗</span></button>`).join('')}</div>`:''}</section>`;
  }
  function ruleForm({phrase='',language='English (United States)',existing=true,returnLabel='Handoff',hasImages=true}={}){
    return `<div class="overlay"><section class="modal mw-rule-modal" role="dialog" aria-modal="true" aria-labelledby="mw-rule-title"><header class="modal-head"><div><div class="eyebrow">${esc(language)} · RETURN TO ${esc(returnLabel)}</div><h2 id="mw-rule-title">${existing?'Edit prohibited wording.':'Add prohibited wording.'}</h2></div><button class="close" type="button" data-mw="close-rule" aria-label="Close dialog">×</button></header><div class="modal-body"><div class="field"><label for="mw-rule-phrase">Word or phrase</label><input id="mw-rule-phrase" value="${esc(phrase)}" autocomplete="off"></div><p class="small muted">A match in printed wording prevents export.</p><p class="mw-field-error" id="mw-rule-error" hidden>Enter a word or phrase.</p></div><footer class="modal-foot">${existing?`<button type="button" class="btn small danger" data-mw="remove-rule">Remove word or phrase</button>`:''}<div class="row">${button('Cancel','cancel-rule')}${button('Save prohibited word','save-rule',true)}</div></footer></section></div>`;
  }
  scope.MotifWording={fromIssue,notice,fieldMessage,rules,ruleForm};
})(window);
