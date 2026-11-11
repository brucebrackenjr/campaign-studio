const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function context(){
 const c=vm.createContext({C:{id:'autumn'},reviewTab:'compare',selected:[2,4],formats:[1,1],imageMarket:'es-MX',pageNum:2,filter:'missing',lang:'es'});
 vm.runInContext(fs.readFileSync('src/campaign_studio/static/motif/studio-workspace.js','utf8'),c);
 return {c,run:code=>vm.runInContext(code,c)};
}
test('Studio return restores this campaign gallery context without selected product mutation',()=>{
 const h=context();h.run('retainStudioGallery();reviewTab="product";selected[0]=0;formats[0]=0;lang="en";restoreStudioGallery()');
 assert.equal(h.c.reviewTab,'compare');assert.deepEqual([...h.c.selected],[2,4]);assert.deepEqual([...h.c.formats],[1,1]);assert.equal(h.c.lang,'es');assert.equal(h.c.pageNum,2);
});
test('direct product entry in another campaign does not adopt a foreign gallery view',()=>{
 const h=context();h.run('retainStudioGallery();C.id="winter";reviewTab="product";selected=[0,1];lang="en";pageNum=0;filter="all";restoreStudioGallery()');
 assert.equal(h.c.reviewTab,'products');assert.equal(h.c.lang,'en');assert.equal(h.c.pageNum,0);assert.equal(h.c.filter,'all');assert.deepEqual([...h.c.selected],[0,1]);
});
