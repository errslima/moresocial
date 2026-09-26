import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

test('pinned compatibility patch preserves the exact MsgKey and rejects source drift',()=>{
 const code=fs.readFileSync(new URL('./patch-library.cjs',import.meta.url),'utf8');
 const original='const msg = message.serialize();\nreturn Collections.Msg.get(newMsgKey._serialized);';
 let output;
 const require=name=>({readFileSync:()=>original,writeFileSync:(_,text)=>{output=text;}});
 require.resolve=()=>'/fake/utils.js';
 vm.runInNewContext(code,{require});
 assert.ok(output.includes('.Msg.get(newMsgKey)'));
 const serialize=new Function('message',output.split('\nreturn Collections')[0]+'\nreturn msg;');
 const key={toString:()=> 'true_123@lid_EXACT'};
 const result=serialize({id:key,serialize:()=>({id:{id:'EXACT',fromMe:true}})});
 assert.equal(result.id._serialized,'true_123@lid_EXACT');
 assert.equal(result.id.id,'EXACT');
 const driftRequire=()=>({readFileSync:()=> 'changed source',writeFileSync:()=>assert.fail('must fail closed')});
 driftRequire.resolve=require.resolve;
 assert.throws(()=>vm.runInNewContext(code,{require:driftRequire}),/Review WhatsApp/);
});
