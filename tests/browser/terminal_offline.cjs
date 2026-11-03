const fs = require('fs');
const http = require('http');
const path = require('path');
const assert = require('assert/strict');
const os = require('os');
const {execFileSync} = require('child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lupine-terminal-test-'));
const fixture = JSON.parse(execFileSync(process.env.PYTHON_EXE || 'python', ['-c', `
import os,json
os.environ['DJANGO_SETTINGS_MODULE']='config.settings'
import django
django.setup()
from inventory.views import _terminal_config
from django.template.loader import render_to_string
config=_terminal_config()
print(json.dumps({'html':render_to_string('inventory/terminal_app.html',{'terminal_data':config}),'worker':render_to_string('inventory/terminal_worker.js',config)}))
`], {cwd:root,encoding:'utf8'}));
const app = '/inventory/terminal/';
const html = fixture.html;
const worker = fixture.worker;
const client = fs.readFileSync(path.join(root,'inventory/terminal_client.js'));
const profile = path.join(output,'browser-profile');
let auth = 1, closed = false, dropReply = false, dropped = false;
const received = new Map();
let nextId = 0, postCalls = 0, largestBatch = 0;
const server = http.createServer((req,res) => {
    const parsed = new URL(req.url, 'http://localhost');
    const send = (data,status=200) => {res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
    if (parsed.pathname === '/seed') {res.end('<html>seed</html>'); return;}
    if (parsed.pathname === app+'sw.js') {res.writeHead(200,{'Content-Type':'application/javascript','Service-Worker-Allowed':app,'Cache-Control':'no-cache'});res.end(worker);return;}
    if (parsed.pathname === app+'client.js') {res.writeHead(200,{'Content-Type':'application/javascript'});res.end(client);return;}
    if (parsed.pathname === app+'offline/' || parsed.pathname === app && auth) {res.writeHead(200,{'Content-Type':'text/html'});res.end(html);return;}
    if (parsed.pathname === app+'login/') {res.writeHead(200,{'Content-Type':'text/html'});res.end('<h1>LOGIN</h1>');return;}
    if (!auth) {res.writeHead(302,{Location:app+'login/'});res.end();return;}
    if (parsed.pathname.endsWith('/context/')) {send({ok:true,user:{id:auth,username:'operator-'+auth},csrf_token:'csrf-test'});return;}
    if (parsed.pathname.endsWith('/sessions/')) {send({ok:true,sessions:[1,2].map(id=>({id,number:'INV-'+id,root_locations:'Warszawa'}))});return;}
    const match = parsed.pathname.match(/sessions\/(\d+)\/(bootstrap|terminals)/);
    if (match && match[2] === 'bootstrap') {send({ok:true,session:{id:+match[1],number:'INV-'+match[1]},locations:[{code:'LOC-000001',name:'Pokój 101',path:'Warszawa / Pokój 101'}],terminals:[]});return;}
    if (match && req.method === 'POST') {
        let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
            if (req.headers['x-csrftoken']!=='csrf-test') return send({error:'csrf'},403);
            const data=JSON.parse(body);
            if (!parsed.pathname.endsWith('/sync/')) return send({ok:true,terminal:{id:+match[1]*10,name:data.name}});
            postCalls++; largestBatch=Math.max(largestBatch,data.scans.length);
            if (closed && +match[1]===1) return send({error:'Sesja jest zamknięta. Skany pozostają w pamięci.'},409);
            const accepted=data.scans.map(scan=>{
                const key=scan.terminal_id+':'+scan.client_scan_id;
                const existed=received.has(key);
                if (!existed) received.set(key,{...scan,raw_scan_id:++nextId});
                return {client_scan_id:scan.client_scan_id,raw_scan_id:received.get(key).raw_scan_id,created:!existed};
            });
            if (dropReply) {dropReply=false;dropped=true;res.destroy();return;}
            send({ok:true,accepted,errors:[]});
        });return;
    }
    res.writeHead(404);res.end();
});
const launch = () => chromium.launchPersistentContext(profile,{headless:true,acceptDownloads:true,viewport:{width:390,height:844},...(process.env.CHROMIUM_EXECUTABLE_PATH ? {executablePath:process.env.CHROMIUM_EXECUTABLE_PATH} : {})});
async function rows(page) {return page.evaluate(()=>new Promise((resolve,reject)=>{const r=indexedDB.open('lupine-terminal-v1',3);r.onsuccess=()=>{const db=r.result;const q=db.transaction('scans').objectStore('scans').getAll();q.onsuccess=()=>{db.close();resolve(q.result);};q.onerror=()=>reject(q.error);};}));}
async function scan(page,code) {await page.locator('[data-scan-input]').fill(code);await page.locator('[data-scan-input]').press('Enter');}
async function count(page,n) {await page.waitForFunction(n=>document.querySelector('[data-local-count]').textContent===String(n),n);}
async function ready(page) {await page.waitForFunction(()=>document.querySelector('[data-memory-status]').textContent.includes('offline: gotowe'));}
(async()=>{
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const base='http://127.0.0.1:'+server.address().port;
    let context=await launch();
    context.on('page',p=>p.on('pageerror',e=>console.error('PAGEERROR',e.message)));
    let page=await context.newPage();
    await page.goto(base+'/seed');
    await page.evaluate(()=>new Promise(resolve=>{
        const r=indexedDB.open('lupine-terminal-v1',1);
        r.onupgradeneeded=()=>r.result.createObjectStore('scans',{keyPath:'client_scan_id'});
        r.onsuccess=()=>{const d=r.result,t=d.transaction('scans','readwrite');t.objectStore('scans').put({client_scan_id:'legacy',session_id:99,terminal_id:99,code:'OLD',synced:false});t.oncomplete=()=>{d.close();resolve();};};
    }));
    await page.goto(base+app);
    await page.waitForSelector('[data-session-select] option',{state:'attached'});
    await page.locator('[data-terminal-name]').fill('T1');
    await page.locator('[data-start-button]').click();
    await page.waitForSelector('[data-scan-panel]:not(.terminal-hidden)');await ready(page);
    console.log('PASS startup, v1 database upgrade preserves legacy scan, offline shell prepared');
    await context.setOffline(true);
    await scan(page,'LOC-000001');await scan(page,'00001');await scan(page,'00002');await count(page,3);
    assert.equal((await rows(page)).length,4);
    assert.equal((await rows(page)).filter(r=>r.code==='00002')[0].current_location_code,'LOC-000001');
    await page.reload();await count(page,3);await ready(page);
    await page.close();page=await context.newPage();await page.goto(base+app);await count(page,3);
    console.log('PASS offline scans, correct location, offline reload and tab reopening');
    await page.evaluate(()=>{const input=document.querySelector('[data-scan-input]');for(let i=0;i<100;i++){input.value='FAST-'+i;input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));}});
    await count(page,103);assert.equal((await rows(page)).length,104);
    console.log('PASS 100 rapid scans serialized without lost writes');
    await page.evaluate(()=>{const original=IDBObjectStore.prototype.add;IDBObjectStore.prototype.add=function(...args){const request=original.apply(this,args);const transaction=this.transaction;IDBObjectStore.prototype.add=original;request.addEventListener('success',()=>transaction.abort());return request;};});
    await scan(page,'ABORTED');await page.waitForSelector('[data-retry-save]:not([hidden])');
    assert.equal(await page.locator('[data-local-count]').textContent(),'103');
    assert.equal((await rows(page)).some(r=>r.code==='ABORTED'),false);
    await page.locator('[data-retry-save]').click();await count(page,104);
    assert.equal((await rows(page)).filter(r=>r.code==='ABORTED').length,1);
    console.log('PASS aborted transaction never reports saved, retry commits exactly once');
    await page.screenshot({path:path.join(output,'terminal-offline-preview.png'),fullPage:true});
    auth=0;await context.setOffline(false);await page.locator('[data-sync-button]').click();
    await page.waitForSelector('[data-login-link]:not([hidden])');
    assert.equal(received.size,0);assert.equal((await rows(page)).filter(r=>!r.synced).length,105);
    console.log('PASS expired login retains all scans');
    auth=2;await page.locator('[data-sync-button]').click();
    await page.waitForFunction(()=>document.querySelector('[data-scan-message]').textContent.includes('należy do'));
    assert.equal(received.size,0);assert.equal(await page.locator('[data-scan-input]').getAttribute('readonly'),'');
    console.log('PASS different account cannot send or relabel the original queue');
    auth=1;closed=true;
    await page.locator('[data-change-session-button]').click();
    await page.waitForSelector('[data-session-select] option',{state:'attached'});
    await page.locator('[data-session-select]').selectOption('2');await page.locator('[data-terminal-name]').fill('T2');
    await page.locator('[data-start-button]').click();await count(page,0);
    await scan(page,'SECOND-SESSION');await count(page,1);
    await page.waitForFunction(()=>new Promise(resolve=>{const r=indexedDB.open('lupine-terminal-v1',3);r.onsuccess=()=>{const d=r.result,q=d.transaction('scans').objectStore('scans').getAll();q.onsuccess=()=>{d.close();resolve(q.result.some(s=>s.code==='SECOND-SESSION' && s.synced));};};}));
    assert.equal(received.size,1);
    console.log('PASS blocked old session preserves queue but does not block current session sync');
    closed=false;dropReply=true;await page.locator('[data-sync-button]').click();
    await new Promise((resolve,reject)=>{const timeout=setTimeout(()=>{clearInterval(t);reject(new Error('No dropped reply'));},10000);const t=setInterval(()=>{if(dropped){clearInterval(t);clearTimeout(timeout);resolve();}},50);});
    await page.waitForFunction(()=>new Promise(resolve=>{const r=indexedDB.open('lupine-terminal-v1',3);r.onsuccess=()=>{const d=r.result,q=d.transaction('scans').objectStore('scans').getAll();q.onsuccess=()=>{d.close();resolve(q.result.filter(s=>s.owner_id===1 && !s.synced).length===0);};};}),null,{timeout:30000});
    assert.equal(received.size,105);assert.equal((await rows(page)).length,106);
    assert.equal((await rows(page)).filter(r=>!r.synced).length,1);
    console.log('PASS lost server reply retried idempotently; synced receipts and legacy scan retained');
    const downloadPromise=page.waitForEvent('download');await page.locator('[data-backup-button]').click();
    const download=await downloadPromise;const backup=JSON.parse(fs.readFileSync(await download.path(),'utf8'));
    assert.equal(backup.scans.length,106);assert.equal(backup.format,'lupine-terminal-backup-v2');
    const keys=await page.evaluate(async()=>{const c=await caches.open('lupine-terminal-shell-v1');return(await c.keys()).map(r=>new URL(r.url).pathname).sort();});
    assert.deepEqual(keys,[app+'client.js',app+'offline/']);
    console.log('PASS export includes full journal; cache excludes authentication and API data');
    const other=await context.newPage();await other.goto(base+app);
    await other.waitForFunction(()=>document.querySelector('[data-message]').textContent.includes('innej karcie'));
    assert.equal(await other.locator('[data-start-button]').isDisabled(),true);await other.close();
    console.log('PASS parallel tab blocked');
    await context.close();context=await launch();await context.setOffline(true);page=await context.newPage();
    await page.goto(base+app);await count(page,1);assert.equal((await rows(page)).length,106);
    console.log('PASS complete browser restart offline restores state and journal');
    // A large confirmed journal must not be read by counters, scanning or sync.
    await page.evaluate(()=>new Promise((resolve,reject)=>{
        const r=indexedDB.open('lupine-terminal-v1',3);
        r.onsuccess=()=>{
            const d=r.result,t=d.transaction(['scans','meta'],'readwrite'),store=t.objectStore('scans');
            for(let i=0;i<100000;i++)store.put({client_scan_id:'history-'+i,owner_id:1,session_id:2,terminal_id:20,code:'HISTORY',synced:true,pending:0});
            for(let i=0;i<10000;i++)store.put({client_scan_id:'backlog-'+i,owner_id:1,session_id:2,terminal_id:20,code:'BACKLOG',synced:false,pending:1});
            const locations={};for(let i=0;i<5000;i++)locations['LOC-'+i]={name:'Pokój '+i,path:'Budynek A / Piętro 1 / Pokój '+i};
            t.objectStore('meta').put({key:'locations',value:locations});
            t.oncomplete=()=>{d.close();resolve();};t.onabort=()=>reject(t.error);
        };
    }));
    await page.reload();await count(page,1);
    await page.evaluate(()=>{
        window.fullJournalReads=0;window.stateWrites=0;window.maxStateBytes=0;
        const getAll=IDBObjectStore.prototype.getAll,put=IDBObjectStore.prototype.put;
        IDBObjectStore.prototype.getAll=function(...args){
            if(this.name==='scans' && (!args[1] || args[1]>500)) {
                window.fullJournalReads++;throw Error('Unbounded journal read during normal operation');
            }
            return getAll.apply(this,args);
        };
        IDBObjectStore.prototype.put=function(value,...args){
            if(this.name==='meta' && value.key==='state'){
                window.stateWrites++;window.maxStateBytes=Math.max(window.maxStateBytes,JSON.stringify(value).length);
                if(value.value.locations)throw Error('Location dictionary rewritten with each scan');
            }
            return put.call(this,value,...args);
        };
    });
    const scanStarted=performance.now();
    await page.evaluate(()=>{const input=document.querySelector('[data-scan-input]');for(let i=0;i<200;i++){input.value='LOAD-'+i;input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));}});
    await count(page,201);
    const scanMs=Math.round(performance.now()-scanStarted);
    assert.equal(await page.evaluate(()=>window.fullJournalReads),0);
    assert.equal(await page.evaluate(()=>window.stateWrites),200);
    assert.ok(await page.evaluate(()=>window.maxStateBytes<1024));
    console.log('PASS 100,000 confirmed + 10,000 pending + 5,000 locations: 200 committed scans in '+scanMs+' ms; no journal reads; compact state writes');
    const syncStarted=performance.now();
    await context.setOffline(false);await page.locator('[data-sync-button]').click();
    await page.waitForFunction(()=>document.querySelector('[data-pending-count]').textContent==='1',null,{timeout:60000});
    const syncMs=Math.round(performance.now()-syncStarted);
    assert.equal(received.size,10305);
    assert.equal(await page.evaluate(()=>window.fullJournalReads),0);
    assert.equal(largestBatch,100);
    console.log('PASS indexed sync of 10,200 pending in '+syncMs+' ms; maximum batch 100, confirmed history untouched');
    const largeDownloadPromise=page.waitForEvent('download');await page.locator('[data-backup-button]').click();
    const largeDownload=await largeDownloadPromise;
    const largeBackup=JSON.parse(fs.readFileSync(await largeDownload.path(),'utf8'));
    assert.equal(largeBackup.scans.length,110306);
    assert.equal(largeBackup.state.locations['LOC-4999'].name,'Pokój 4999');
    assert.equal(await page.evaluate(()=>window.fullJournalReads),0);
    assert.equal(await page.locator('[data-scan-input]').getAttribute('readonly'),null);
    console.log('PASS chunked export of 110,306 records (max 500 per read), complete JSON and scan input restored');

    await page.screenshot({path:path.join(output,'terminal-large-journal.png'),fullPage:true});

    // Upgrade a previously prepared v2 device, including its saved room dictionary.
    const v2=await context.browser().newContext();const oldPage=await v2.newPage();
    await oldPage.goto(base+'/seed');
    await oldPage.evaluate(()=>new Promise(resolve=>{
        const r=indexedDB.open('lupine-terminal-v1',2);
        r.onupgradeneeded=()=>{
            const d=r.result,store=d.createObjectStore('scans',{keyPath:'client_scan_id'}),meta=d.createObjectStore('meta',{keyPath:'key'});
            store.put({client_scan_id:'v2-pending',owner_id:1,session_id:1,terminal_id:10,code:'V2-PENDING',synced:false});
            store.put({client_scan_id:'v2-confirmed',owner_id:1,session_id:1,terminal_id:10,code:'V2-CONFIRMED',synced:true,raw_scan_id:999});
            meta.put({key:'state',value:{owner_id:1,owner_name:'operator-1',session_id:1,terminal_id:10,session_number:'INV-1',terminal_name:'T1',local_count:2,current_location_code:'LOC-1',current_location_label:'Pokój 1',locations:{'LOC-1':{name:'Pokój 1'}}}});
        };
        r.onsuccess=()=>{r.result.close();resolve();};
    }));
    closed=true;await oldPage.goto(base+app);await count(oldPage,2);
    const upgraded=await rows(oldPage);
    assert.equal(upgraded.length,2);assert.equal(upgraded.find(s=>s.client_scan_id==='v2-pending').pending,1);
    assert.equal(upgraded.find(s=>s.client_scan_id==='v2-confirmed').pending,0);
    assert.equal(upgraded.find(s=>s.client_scan_id==='v2-confirmed').raw_scan_id,999);
    const metadata=await oldPage.evaluate(()=>new Promise(resolve=>{
        const r=indexedDB.open('lupine-terminal-v1',3);r.onsuccess=()=>{const d=r.result,q=d.transaction('meta').objectStore('meta').getAll();q.onsuccess=()=>{d.close();resolve(q.result);};};
    }));
    assert.equal(metadata.find(m=>m.key==='state').value.locations,undefined);
    assert.equal(metadata.find(m=>m.key==='locations').value['LOC-1'].name,'Pokój 1');
    console.log('PASS v2-to-v3 migration preserves pending scans, receipts, working state and location dictionary');
    await v2.close();

    await context.close();await new Promise(resolve=>server.close(resolve));
    console.log('ALL OFFLINE BROWSER TESTS PASSED; sync requests='+postCalls); console.log('Test artifacts: '+output);
})().catch(error=>{console.error(error);process.exitCode=1;server.close();setTimeout(()=>process.exit(1),500);});
