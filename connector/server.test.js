import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
process.env.DIAGNOSTICS_DIR=fs.mkdtempSync(path.join(os.tmpdir(),'moresocial-server-test-'));
const {createHandler,listen,sameKey}=await import('./server.js');

function start(){
  const calls=[];
  const handler=createHandler({key:'workspace-a-key',status:()=>({state:'pairing',qr:'data:image/png;base64,AAA'}),
    connect:async()=>calls.push('connect'),sync:async()=>calls.push('sync'),disconnect:async()=>calls.push('disconnect')});
  const server=listen(handler,0);
  return {calls,server,url:()=>`http://127.0.0.1:${server.address().port}`};
}

test('only the workspace key is accepted',async()=>{
  const s=start();await new Promise(r=>s.server.once('listening',r));
  try{
    assert.equal((await fetch(s.url()+'/status')).status,401);
    assert.equal((await fetch(s.url()+'/status',{headers:{Authorization:'Bearer workspace-b-key'}})).status,401);
    const ok=await fetch(s.url()+'/status',{headers:{Authorization:'Bearer workspace-a-key'}});
    assert.equal(ok.status,200);assert.equal(ok.headers.get('cache-control'),'no-store');
    assert.equal((await ok.json()).state,'pairing');
  }finally{s.server.close();}
});

test('there is no send, photo or repair route',async()=>{
  const s=start();await new Promise(r=>s.server.once('listening',r));
  try{
    for(const p of ['/send','/send-preflight','/photo','/repair','/compatibility']){
      const r=await fetch(s.url()+p,{method:'POST',headers:{Authorization:'Bearer workspace-a-key'},body:'{}'});
      assert.equal(r.status,404,p);
    }
    await fetch(s.url()+'/disconnect',{method:'POST',headers:{Authorization:'Bearer workspace-a-key'}});
    assert.deepEqual(s.calls,['disconnect']);
  }finally{s.server.close();}
});

test('key comparison is exact',()=>{
  assert.equal(sameKey('Bearer abc','abc'),true);
  assert.equal(sameKey('Bearer abcd','abc'),false);
  assert.equal(sameKey('Bearer abc',''),false);
});
