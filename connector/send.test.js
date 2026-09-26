import test from 'node:test';
import assert from 'node:assert/strict';
import {createSender} from './send.js';
const input={account:'31600000009@c.us',chat:'123456789012345@lid',text:'Hello!'};
function setup(overrides={}){
 const calls=[];
 const fn=createSender({state:()=> 'ready',account:()=>input.account,
  chat:async id=>({id,name:'Test',is_group:false}),
  send:async(...args)=>{calls.push(args);return {id:{_serialized:'true_test_123'},fromMe:true,timestamp:1234567890};},
  onSent:async()=>{},...overrides});
 return {fn,calls};
}
test('text to an existing chat with account guard and safe options',async()=>{
 const {fn,calls}=setup();const result=await fn(input);
 assert.equal(result.state,'sent');assert.equal(result.packet.chat.id,input.chat);
 assert.deepEqual(calls,[[input.chat,input.text,{sendSeen:false,waitUntilMsgSent:true,linkPreview:false}]]);
});
test('refuses unavailable accounts, unknown chats and invalid inputs without sending',async()=>{
 for(const overrides of [{state:()=> 'pairing'},{account:()=> 'other@c.us'},{chat:async()=>null}]){
  const {fn,calls}=setup(overrides);assert.equal((await fn(input)).state,'rejected');assert.equal(calls.length,0);
 }
 for(const patch of [{chat:'status@broadcast'},{text:''},{text:'x'.repeat(4001)},{text:123},{text:'a\0b'}]){
  const {fn,calls}=setup();assert.equal((await fn({...input,...patch})).state,'rejected');assert.equal(calls.length,0);
 }
});
test('send failures are ambiguous, storage failure preserves confirmed send',async()=>{
 for(const send of [async()=>{throw Error('timeout');},async()=>null]){
  assert.equal((await setup({send}).fn(input)).state,'unknown');
 }
 assert.equal((await setup({onSent:async()=>{throw Error('storage');}}).fn(input)).state,'sent');
});

test('uses the actual canonical destination while retaining the requested chat',async()=>{
 const {fn}=setup({send:async()=>({id:{_serialized:'true_canonical_123'},fromMe:true,timestamp:1234567890,to:'987654321@lid'})});
 const result=await fn(input);
 assert.equal(result.requested_chat,input.chat);
 assert.equal(result.packet.chat.id,'987654321@lid');
});
