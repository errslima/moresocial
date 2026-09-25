import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeRawMessage,normalizeWrapperMessage,restoreMessageKey} from './normalize.js';

test('stored message keys repair current Web serialization without replacing existing keys',()=>{
  const m={id:{id:'opaque'}};
  assert.equal(restoreMessageKey(m,'false_peer_opaque').id._serialized,'false_peer_opaque');
  assert.equal(restoreMessageKey(m,'other').id._serialized,'false_peer_opaque');
  assert.equal(restoreMessageKey(null,'missing'),null);
});

test('outgoing direct messages retain the account owner identity',()=>{
  const row=normalizeWrapperMessage({id:'m',timestamp:1,from:'peer@c.us',fromMe:true,body:'hi',type:'chat'},false,{selfId:'owner@c.us'});
  assert.equal(row.sender,'owner@c.us');
  assert.equal(row.from_me,true);
});

test('wrapper text is preserved and image captions are separate from media body',()=>{
  const text=normalizeWrapperMessage({id:{_serialized:'m1'},timestamp:10,from:'peer@c.us',body:'hello',type:'chat'});
  assert.equal(text.body,'hello');assert.equal(text.kind,'chat');
  const image=normalizeWrapperMessage({id:{_serialized:'m2'},timestamp:11,from:'peer@c.us',body:'/9j/encoded-thumbnail',caption:'Look at this',type:'image',hasMedia:true,mimetype:'image/jpeg'});
  assert.equal(image.body,'Look at this');assert.equal(image.media_available,true);assert.equal(image.media_mime,'image/jpeg');
  const noCaption=normalizeWrapperMessage({id:'m3',timestamp:12,from:'peer@c.us',body:'UklGRthumbnail',type:'image'});
  assert.equal(noCaption.body,'');
});

test('raw history media ignores body and text may legitimately resemble base64',()=>{
  const image=normalizeRawMessage({id:'false_chat_m',t:15,from:'peer@c.us',body:'iVBORw0KGgo=',caption:'caption only',type:'image',hasMedia:true});
  assert.equal(image.body,'caption only');assert.equal(image.protocol_version,2);
  const text=normalizeRawMessage({id:'false_chat_t',t:16,from:'peer@c.us',body:'iVBORw0KGgo=',type:'chat'});
  assert.equal(text.body,'iVBORw0KGgo=');
});

test('revocation wins, unknown types do not expose arbitrary body',()=>{
  const revoked=normalizeWrapperMessage({id:'m',timestamp:1,from:'peer',body:'stale text',type:'image',hasMedia:true},true);
  assert.equal(revoked.kind,'revoked');assert.equal(revoked.body,'[Message deleted]');assert.equal(revoked.media_available,false);
  const unknown=normalizeRawMessage({id:'m2',t:2,from:'peer',body:'private bytes',type:'future_payload'});
  assert.equal(unknown.kind,'future_payload');assert.equal(unknown.body,'');
});
