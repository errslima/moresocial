import test from 'node:test';
import assert from 'node:assert/strict';
import {createHeartbeat,healthReport} from './health.js';

const base={state:'ready',pending:0,pending_age_s:0,syncing:false,chats_total:60,history_chats:50,history_done:50,
  chats_synced:50,sweep:{last_at:'2026-09-27T01:10:00.000Z',outcome:'ok',failed_chats:0,last_complete_at:'2026-09-27T01:10:00.000Z'}};

test('report carries only bounded health fields, never account, QR or error text',()=>{
  const r=healthReport({...base,account:'31600000001@c.us',qr:'data:image/png;base64,AAA',self_chat:'x@lid',
    error:'Message storage unavailable',error_code:'ingest_unavailable',pending:-4,history_chats:1.5});
  const text=JSON.stringify(r);
  for(const secret of ['31600000001','data:image','x@lid','Message storage'])assert.equal(text.includes(secret),false,secret);
  assert.equal(r.pending,0);assert.equal(r.history.history_chats,0);assert.equal(r.error_code,'ingest_unavailable');
  assert.equal(healthReport({...base,state:'surprise'}).state,'error');
  assert.equal(healthReport({...base,sweep:{last_at:'not a date',outcome:'great'}}).sweep.outcome,null);
});

test('a failed heartbeat is recorded, never thrown, and does not stop later beats',async()=>{
  const events=[];let fail=true,posts=0;
  const hb=createHeartbeat({snapshot:()=>base,post:async()=>{posts++;if(fail)throw Error('web down');},
    record:(name)=>events.push(name)});
  assert.equal(await hb.beat(),false);
  assert.deepEqual(events,['heartbeat_failed']);
  fail=false;
  assert.equal(await hb.beat(),true);assert.equal(posts,2);
});

test('a failing snapshot is contained as well',async()=>{
  const events=[];
  const hb=createHeartbeat({snapshot:()=>{throw Error('boom');},post:async()=>{},record:n=>events.push(n)});
  assert.equal(await hb.beat(),false);assert.deepEqual(events,['heartbeat_failed']);
});

test('beats never overlap; a request during a beat runs once afterwards',async()=>{
  let release,running=0,max=0,posts=0;
  const hb=createHeartbeat({snapshot:()=>base,post:async()=>{running++;max=Math.max(max,running);posts++;
    if(posts===1)await new Promise(r=>{release=r;});running--;}});
  const first=hb.beat();
  assert.equal(await hb.beat(),false);assert.equal(await hb.beat(),false);
  release();await first;await new Promise(r=>setTimeout(r,10));
  assert.equal(max,1);assert.equal(posts,2);
});

test('a state change is reported after it settles',async()=>{
  const seen=[];let state='loading';
  const hb=createHeartbeat({snapshot:()=>({...base,state}),post:async r=>seen.push(r.state),settle:5});
  await hb.beat();
  state='ready';hb.changed('ready');hb.changed('ready');
  await new Promise(r=>setTimeout(r,30));
  hb.changed('ready');await new Promise(r=>setTimeout(r,30));
  assert.deepEqual(seen,['loading','ready']);hb.stop();
});

test('nothing is posted while the connector is not wanted',async()=>{
  let posts=0;
  const hb=createHeartbeat({snapshot:()=>null,post:async()=>{posts++;}});
  assert.equal(await hb.beat(),false);assert.equal(posts,0);
});
