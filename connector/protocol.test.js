import test from 'node:test';
import assert from 'node:assert/strict';
import {guardQueuedPacket} from './protocol.js';

test('legacy queued media loses raw body but keeps identity for later repair',()=>{
  const packet={messages:[{id:'m',ts:1,sender:'synthetic@lid',kind:'image',body:'/9j/old payload'}]};
  assert.equal(guardQueuedPacket(packet),packet);
  assert.deepEqual(packet.messages[0],{id:'m',ts:1,sender:'synthetic@lid',kind:'image',body:'',protocol_version:1});
});

test('current protocol packets and ordinary text are unchanged',()=>{
  const packet={messages:[{id:'m1',kind:'image',body:'Caption',protocol_version:2},{id:'m2',kind:'chat',body:'text'}]};
  guardQueuedPacket(packet);
  assert.equal(packet.messages[0].body,'Caption');assert.equal(packet.messages[1].body,'text');
});
