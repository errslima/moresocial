import test from 'node:test';
import assert from 'node:assert/strict';
import {selectChats,boundMessages,LIMITS} from './history.js';

const now=Date.UTC(2026,8,25)/1000;
test('at most 50 most recent chats within 90 days, broadcasts skipped',()=>{
  const chats=Array.from({length:80},(_,i)=>({id:`${i}@c.us`,active:now-i*3600}));
  chats.push({id:'old@c.us',active:now-91*86400},{id:'status@broadcast',active:now});
  const picked=selectChats(chats,now*1000);
  assert.equal(picked.length,LIMITS.chats);
  assert.equal(picked[0].id,'0@c.us');
  assert.ok(!picked.some(c=>c.id==='old@c.us'||c.id.endsWith('@broadcast')));
});
test('at most 200 messages per chat, none older than 90 days',()=>{
  const msgs=Array.from({length:300},(_,i)=>({id:'m'+i,ts:now-300+i}));
  msgs.unshift({id:'ancient',ts:now-100*86400});
  const kept=boundMessages(msgs,now*1000);
  assert.equal(kept.length,200);assert.equal(kept.at(-1).id,'m299');
  assert.ok(!kept.some(m=>m.id==='ancient'));
});
