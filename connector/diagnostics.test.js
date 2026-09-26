import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
const root=fs.mkdtempSync(path.join(os.tmpdir(),'moresocial-logs-test-'));
process.env.DIAGNOSTICS_DIR=root;
process.env.MORESOCIAL_RELEASE='e373a49';
const {record,reference,withRequest,withOperation,current}=await import('./diagnostics.js');
const rows=()=>fs.readFileSync(path.join(root,'events.jsonl'),'utf8').trim().split('\n').map(l=>JSON.parse(l));
test('safe structured metadata in the shared schema',()=>{
 record('synthetic_error',{status:502,body:'PRIVATE_BODY',url:'https://secret/?token=PRIVATE_TOKEN'},new Error('PRIVATE_MESSAGE'));
 const text=fs.readFileSync(path.join(root,'events.jsonl'),'utf8');assert.ok(!text.includes('PRIVATE'));
 const row=JSON.parse(text.trim());assert.equal(row.status,502);assert.equal(row.error_type,'Error');assert.ok(row.frames.length);
 assert.equal(row.schema,1);assert.equal(row.level,'error');assert.equal(row.service,'connector');assert.equal(row.release,'e373a49');
 assert.match(row.reference,/^[a-f0-9]{16}$/);assert.match(row.ts,/Z$/);assert.equal(row.request_id,undefined);
 assert.match(reference('bad\ninjection'),/^[a-f0-9]{32}$/);assert.equal(reference('a'.repeat(32)),'a'.repeat(32));
 assert.equal(fs.statSync(path.join(root,'events.jsonl')).mode&0o777,0o600);
});
test('secret-shaped values are refused even in token fields',()=>{
 record('canary_check',{code:'sk-or-v1-PRIVATECANARY',outcome:'Bearer PRIVATE',method:'eyJhbGciOiJIUzI1NiJ9PRIVATE'});
 const row=rows().at(-1);assert.equal(row.event,'canary_check');assert.equal(row.code,undefined);assert.equal(row.method,undefined);
 assert.ok(!JSON.stringify(row).includes('PRIVATE'));
});
test('request handler contains async exceptions without leaking error text',async()=>{
 const headers={};let body;const res={headersSent:false,setHeader:(k,v)=>headers[k]=v,on:()=>{},writeHead:()=>{},end:v=>body=v};
 await withRequest({url:'/send?token=PRIVATE_TOKEN',method:'POST',headers:{'x-request-id':'b'.repeat(32)}},res,async()=>{throw new Error('PRIVATE_BODY');});
 assert.equal(headers['X-Request-ID'],'b'.repeat(32));assert.equal(JSON.parse(body).reference,'b'.repeat(32));assert.ok(!body.includes('PRIVATE'));
 assert.ok(!fs.readFileSync(path.join(root,'events.jsonl'),'utf8').includes('PRIVATE'));
 const row=rows().at(-1);assert.equal(row.event,'request_exception');assert.equal(row.request_id,'b'.repeat(32));assert.equal(row.route,'/send');
});
test('concurrent operations keep their own correlation IDs',async()=>{
 const seen=await Promise.all([1,2,3].map(n=>withOperation(async()=>{const id=current();await new Promise(r=>setTimeout(r,5*(4-n)));
  record('operation_probe',{count:n});return [id,current()];})));
 for(const [before,after] of seen)assert.equal(before,after);
 assert.equal(new Set(seen.map(s=>s[0])).size,3);
 const probes=rows().filter(r=>r.event==='operation_probe');
 for(const row of probes)assert.equal(row.request_id,seen[row.count-1][0]);
});
test('repeated events are suppressed with a count',()=>{
 for(let i=0;i<25;i++)record('flood_probe',{code:'same'});
 assert.equal(rows().filter(r=>r.event==='flood_probe').length,20);
});
test('an unwritable store never throws',()=>{
 fs.chmodSync(root,0o500);
 try{assert.match(record('store_down_probe'),/^[a-f0-9]{16}$/);}finally{fs.chmodSync(root,0o700);}
});
test.after(()=>fs.rmSync(root,{recursive:true,force:true}));
