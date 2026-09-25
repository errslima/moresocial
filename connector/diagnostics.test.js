import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
const root=fs.mkdtempSync(path.join(os.tmpdir(),'moresocial-logs-test-'));
process.env.DIAGNOSTICS_DIR=root;
const {record,reference,withRequest}=await import('./diagnostics.js');
test('safe structured metadata and bounded references',()=>{
 record('synthetic_error',{status:502,body:'PRIVATE_BODY',url:'https://secret/?token=PRIVATE_TOKEN'},new Error('PRIVATE_MESSAGE'));
 const text=fs.readFileSync(path.join(root,'events.jsonl'),'utf8');assert.ok(!text.includes('PRIVATE'));
 const row=JSON.parse(text.trim());assert.equal(row.status,502);assert.equal(row.error_type,'Error');assert.ok(row.frames.length);
 assert.match(reference('bad\ninjection'),/^[a-f0-9]{32}$/);assert.equal(reference('a'.repeat(32)),'a'.repeat(32));
 assert.equal(fs.statSync(path.join(root,'events.jsonl')).mode&0o777,0o600);
});
test('request handler contains async exceptions without leaking error text',async()=>{
 const headers={};let body;const res={headersSent:false,setHeader:(k,v)=>headers[k]=v,on:()=>{},writeHead:()=>{},end:v=>body=v};
 await withRequest({url:'/send?token=PRIVATE_TOKEN',method:'POST',headers:{'x-request-id':'b'.repeat(32)}},res,async()=>{throw new Error('PRIVATE_BODY');});
 assert.equal(headers['X-Request-ID'],'b'.repeat(32));assert.equal(JSON.parse(body).reference,'b'.repeat(32));assert.ok(!body.includes('PRIVATE'));
 assert.ok(!fs.readFileSync(path.join(root,'events.jsonl'),'utf8').includes('PRIVATE'));
});
test.after(()=>fs.rmSync(root,{recursive:true,force:true}));
