const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');

function control(){
 const contract={limits:{campaign_name:25,brand_name:20,product_name:80,headline:120,cta:60,audience:500,direction:1000,description:1000,origin_label:200,rights:1000,phrase:256,file_reference:4096,replay_model:200,replay_history:2000},fields:{'campaign-name':'campaign_name','new-name':'campaign_name','rename-name':'campaign_name','new-brand':'brand_name','new-audience':'audience','product-name':'product_name','product-description':'description','product-origin':'origin_label','product-rights':'rights','mf-word-message':'headline','mf-word-cta':'cta','mw-rule-phrase':'phrase','image-description':'description'},labels:{campaign_name:'Campaign name',brand_name:'Brand name',product_name:'Product name',headline:'Headline',cta:'Call to action',audience:'Audience',direction:'Creative direction',description:'Description',origin_label:'Origin label',rights:'Usage rights',phrase:'Prohibited phrase',file_reference:'File reference',replay_model:'Replay model',replay_history:'Replay history'}};
 const elements=new Map([['motif-text-limits',{textContent:JSON.stringify(contract)}]]),live=[];let listener;
 const make=(id,value='')=>{
  const attributes=new Map(),node={id,value,dataset:{},attributes,validityMessage:'',focused:false,reported:false,
   getAttribute:k=>attributes.get(k)??null,setAttribute:(k,v)=>attributes.set(k,v),removeAttribute:k=>attributes.delete(k),
   toggleAttribute:(k,on)=>on?attributes.set(k,''):attributes.delete(k),before(holder){this.holder=holder;},
   setCustomValidity(message){this.validityMessage=message},focus(){this.focused=true},setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end},reportValidity(){this.reported=true;return!this.validityMessage}};
  elements.set(id,node);return node;
 };
 const input=make('campaign-name');
 const document={body:{append:node=>live.push(node)},getElementById:id=>elements.get(id),querySelectorAll:()=>[...elements.values()].filter(n=>n.id&&n.attributes),addEventListener:(event,fn)=>{listener=fn;},createElement:()=>({textContent:'',style:{setProperty(){}},setAttribute(){},getBoundingClientRect(){return{width:this.textContent.length*7}},append(...nodes){this.children=nodes;for(const node of nodes){node.parentElement=this;elements.set(node.id,node)}}})};
 const context=vm.createContext({document,window:{},MutationObserver:class{observe(){}}});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/text-limits.js','utf8'),context);
 return{api:context.window.MotifTextLimits,document,input,elements,attributes:input.attributes,make,live,type:(value,field=input)=>{field.value=value;listener({target:field});}};
}

test('counter mounts once around the same control and preserves an existing focus selection',()=>{
 const h=control(),field=h.make('new-name','Campaign draft');
 h.document.activeElement=field;field.selectionStart=3;field.selectionEnd=7;
 h.api.update(field);
 const holder=field.holder;
 assert.equal(holder.children[0],field);assert.equal(field.focused,true);
 assert.deepEqual([field.selectionStart,field.selectionEnd],[3,7]);
 h.type('W'.repeat(26),field);h.type('Corrected',field);
 assert.equal(field.holder,holder);assert.equal(holder.children.length,2);
 assert.equal(field.value,'Corrected');
});

test('only an over-limit count appears, code points and all pasted text are preserved, and correction clears it',()=>{
 const h=control(),note=h.elements.get('campaign-name-length');
 for(const value of ['🦊'.repeat(25),'é'.repeat(25),'e\u0301'.repeat(12)+'x']){
  h.type(value);assert.equal(h.api.count(value),25);assert.equal(h.input.value,value);
  assert.equal(note.hidden,true);assert.equal(note.textContent,'');assert.equal(h.attributes.has('data-length-invalid'),false);
 }
 h.type('🦊'.repeat(26));assert.equal(h.input.value,'🦊'.repeat(26));
 assert.equal(note.hidden,false);assert.equal(note.textContent,'26 / 25');
 assert.equal(h.attributes.has('data-length-invalid'),true);assert.equal(h.attributes.has('maxlength'),false);
 assert.equal(h.attributes.get('aria-invalid'),'true');assert.match(h.live[0].textContent,/Campaign name: 26 of 25/);
 assert.equal(h.api.guardFields(['campaign-name']),false);assert.equal(h.input.reported,true);assert.match(h.input.validityMessage,/Campaign name.*25/);
 h.type('Corrected');assert.equal(note.hidden,true);assert.equal(note.textContent,'');assert.equal(h.attributes.has('data-length-invalid'),false);
 assert.equal(h.attributes.has('aria-invalid'),false);assert.equal(h.input.validityMessage,'');assert.equal(h.api.guardFields(['campaign-name']),true);
 assert.equal(h.attributes.get('aria-describedby'),'campaign-name-length');assert.equal(note.title,undefined);
 assert.equal(h.live[0].textContent,'Campaign name is within the limit.');
 h.type('W'.repeat(25));assert.equal(note.hidden,true);h.type('W'.repeat(26));assert.equal(note.hidden,false);
});

test('a separate field validity error survives correction of the length',()=>{
 const h=control();h.input.setAttribute('aria-invalid','true');h.type('W'.repeat(26));h.type('ok');
 assert.equal(h.attributes.get('aria-invalid'),'true');
});

test('a raw field and its pending buffer produce one readable save error',async()=>{
 const issues=[{path:['name'],label:'Campaign name',message:'Use no more than 60 characters.'},{path:[],label:'Campaign name',message:'Use no more than 60 characters.'}];
 const context=vm.createContext({$:()=>({content:'csrf'}),fetch:async()=>({ok:false,status:422,json:async()=>({code:'text_too_long',field_errors:issues})})});
 const source=readFileSync('src/campaign_studio/static/motif/api.js','utf8');
 vm.runInContext(source.slice(source.indexOf('async function api('),source.indexOf('function assetURL(')),context);
 await assert.rejects(context.api('/authored',{}),error=>{
  assert.equal(error.message,'Campaign name: Use no more than 60 characters.');
  assert.equal(error.fields.length,2,'both field addresses remain available');return true;
 });
});

for (const [field,limit] of [['name',80],['description',1000],['origin',200],['rights',1000]]) test(`product ${field} refuses over-limit Update before changing or closing its draft`,()=>{
 const h=control();
 const inputs=Object.fromEntries(['name','description','origin','rights'].map(key=>['#product-'+key,h.make('product-'+key,'Allowed')]));
 const invalid=inputs['#product-'+field];h.type('🦊'.repeat(limit+1),invalid);
 const draft={products:[{id:'item',name:'Original'}]},before=structuredClone(draft);
 const context=vm.createContext({draft,modalProduct:{id:'item',treatment:'concept',source:{}},MotifTextLimits:h.api,$:id=>inputs[id],openModal(){throw Error('Invalid Update reopened editor')},closeModal(){throw Error('Invalid Update closed editor')}});
 const source=readFileSync('src/campaign_studio/static/motif/actions.js','utf8');
 vm.runInContext(source.slice(source.indexOf('function saveProductDraft('),source.indexOf('function openInputField(')),context);
 context.saveProductDraft();assert.equal(invalid.focused,true);assert.equal(invalid.reported,true);
 assert.deepEqual(draft,before);assert.equal(invalid.value,'🦊'.repeat(limit+1));
 assert.match(invalid.validityMessage,new RegExp(limit.toLocaleString()));h.type('🦊'.repeat(limit),invalid);
 assert.equal(h.api.guardFields(['product-'+field]),true);
});

for(const [purpose,setup] of [
 ['campaign_name',(a,v)=>a.brief.name=v],['brand_name',(a,v)=>a.brief.brand={name:v}],
 ['audience',(a,v)=>a.brief.audience=v],['direction',(a,v)=>a.brief.creative_direction=v],
 ['headline',(a,v)=>a.brief.markets={'en-US':{copy:{message:v}}}],['cta',(a,v)=>a.brief.products=[{copy_overrides:{'es-MX':{cta:v}}}]],
 ['phrase',(a,v)=>a.brief.markets={'en-US':{phrase_rules:[{phrase:v}]}}],
 ['product_name',(a,v)=>a.brief.products=[{name:v}]],['description',(a,v)=>a.brief.products=[{source:{description:v}}]],
 ['origin_label',(a,v)=>a.brief.brand={logo_provenance:{label:v}}],['rights',(a,v)=>a.brief.copy_provenance={rights:v}],
 ['file_reference',(a,v)=>a.brief.products=[{source:{path:v}}]],['replay_model',(a,v)=>a.brief.replay={sources:{item:{model:v}}}],
 ['replay_history',(a,v)=>a.brief.replay={history:v}],
 ['campaign_name',(a,v)=>a.form_drafts={fields:{'brief/overview/en':{'campaign-name':v}}}],
 ['description',(a,v)=>a.review_drafts={target:{values:{'image-description':v}}}],
]) test(`captured ${purpose} length is validated without requiring a complete brief`,()=>{
 const {api}=control(),limit=api.contract.limits[purpose],authored={brief:{}};
 setup(authored,'🦊'.repeat(limit));assert.equal(api.authoredIssue(authored),null);
 setup(authored,'🦊'.repeat(limit+1));assert.match(api.authoredIssue(authored),new RegExp(limit.toLocaleString()));
 assert.equal(api.authoredIssue({brief:{},form_drafts:{fields:{ignored:{'not-authored':'W'.repeat(5000)}}}}),null);
});

for(const [action,field,purpose] of [['create','new-name','campaign_name'],['create','new-brand','brand_name'],['create','new-audience','audience'],['saveName','rename-name','campaign_name'],['saveRule','mw-rule-phrase','phrase'],['saveDescription','image-description','description'],['applyText','mf-word-message','headline'],['applyText','mf-word-cta','cta'],['applyDraftWording','draft-wording-value','cta']]) test(`${action} stops an invalid ${field} before dispatch`,async()=>{
 const h=control(),input=h.make(field,'🦊'.repeat(h.api.contract.limits[purpose]+1));
 if(field==='draft-wording-value')input.dataset.lengthPurpose=purpose;
 const context=vm.createContext({MotifTextLimits:h.api,localDialog:false,draft:null,busy:false,guardImageCleanup(){throw Error('Invalid field escaped its action guard');}});
 const source=readFileSync('src/campaign_studio/static/motif/actions.js','utf8');
 vm.runInContext(source.slice(source.indexOf('async function dispatchAction('),source.indexOf('document.addEventListener("click", event => {',source.indexOf('async function dispatchAction('))),context);
 await context.dispatchAction({action});assert.equal(input.focused,true);assert.equal(input.reported,true);
 h.type('Corrected',input);assert.equal(h.api.guardFields([field]),true);
});

test('Text over-limit wording never queues preview or applies an obsolete preview, and correction resumes preview',async()=>{
 const h=control(),timers=[],editor={campaign:'campaign',values:{message:'W'.repeat(121),cta:'Explore'},sequence:0,saving:false,recovery:null,input:{active:()=>false},preview:{displayed:true,result:{impact:{render_outputs:[1]}}}};
 const context=vm.createContext({document:{addEventListener(){}},MotifTextLimits:h.api,clearTimeout(){},setTimeout:fn=>{timers.push(fn)},MotifEditorControls:{copy:{previewing:'Preparing preview'}},C:{id:'campaign'},modal:'text',api(){throw Error('Invalid wording sent to server')},editor});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/text.js','utf8'),context);
 vm.runInContext('textEditor=editor;paintText=()=>{};scheduleTextPreview()',context);
 assert.equal(timers.length,0);assert.equal(editor.preview,null);assert.match(editor.message,/Headline.*120/);
 await context.previewText();assert.equal(await context.applyText(),false);
 editor.values.message='Corrected';context.scheduleTextPreview();assert.equal(timers.length,1);assert.equal(editor.message,'Preparing preview');
});
