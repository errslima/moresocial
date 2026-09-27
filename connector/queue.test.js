import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createQueue} from './queue.js';

test('packets survive a failed delivery and a restart, then drain in order',async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'moresocial-queue-'));
  const file=path.join(dir,'pending.json');
  let fail=true;const sent=[];
  const q=createQueue(file,{post:async p=>{if(fail)throw Error('down');sent.push(p.chat.id);}});
  q.push({chat:{id:'a'},messages:[]});q.push({chat:{id:'b'},messages:[]});
  await q.flush();assert.equal(q.length,2);
  assert.equal(fs.statSync(file).mode&0o777,0o600);
  fail=false;
  const restarted=createQueue(file,{post:async p=>sent.push(p.chat.id)});
  await restarted.flush();
  assert.deepEqual(sent,['a','b']);assert.equal(restarted.length,0);
  fs.rmSync(dir,{recursive:true,force:true});
});

test('reports how long packets wait and whether delivery is failing',async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'moresocial-queue-'));
  let t=1000000,fail=true;
  const q=createQueue(path.join(dir,'pending.json'),{post:async()=>{if(fail)throw Error('down');},now:()=>t});
  assert.equal(q.age(),0);assert.equal(q.failing,false);
  q.push({chat:{id:'a'},messages:[]});t+=90000;
  await q.flush();
  assert.equal(q.failing,true);assert.equal(q.age(),90);assert.equal(q.length,1);
  fail=false;await q.flush();
  assert.equal(q.failing,false);assert.equal(q.age(),0);assert.equal(q.length,0);
  fs.rmSync(dir,{recursive:true,force:true});
});
