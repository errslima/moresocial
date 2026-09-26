import test from 'node:test';
import assert from 'node:assert/strict';
import {CONTACT_TTL,NEGATIVE_TTL,createContactResolver,contactRecord} from './contacts.js';

test('resolver deduplicates, bounds and caches successful identities for 24 hours',async()=>{
  let now=1000,calls=0;
  const resolver=createContactResolver({now:()=>now,lookup:async id=>{calls++;return {id,saved_name:'Synthetic'};}});
  const first=await resolver.resolve(['lid@lid','lid@lid','phone@c.us']);
  assert.equal(calls,2);assert.equal(first.length,2);assert.equal(first[0].saved_name,'Synthetic');
  now+=CONTACT_TTL-1;await resolver.resolve(['lid@lid']);assert.equal(calls,2);
  now+=2;await resolver.resolve(['lid@lid']);assert.equal(calls,3);
  assert.equal((await resolver.resolve(Array.from({length:300},(_,i)=>'id'+i))).length,200);
});

test('negative cache is short and a lookup error does not emit an empty overwrite',async()=>{
  let now=1000,calls=0;
  const resolver=createContactResolver({now:()=>now,lookup:async()=>{calls++;throw Error('unavailable');}});
  assert.deepEqual(await resolver.resolve(['opaque@lid']),[]);
  now+=NEGATIVE_TTL-1;await resolver.resolve(['opaque@lid']);assert.equal(calls,1);
  now+=2;await resolver.resolve(['opaque@lid']);assert.equal(calls,2);
});

test('only explicit LID to phone mapping creates aliases',()=>{
  const row=contactRecord({id:{_serialized:'opaque@lid'},name:'Saved',pushname:'Profile'},'opaque@lid',{lid:'opaque@lid',pn:'15550001111@c.us'});
  assert.deepEqual(row.aliases,['opaque@lid','15550001111@c.us']);
  assert.equal(row.phone_number,'15550001111');
  assert.equal(row.saved_name,'Saved');
  assert.deepEqual(contactRecord({id:'opaque@lid'},'opaque@lid',null).aliases,['opaque@lid']);
});

test('library mapping arrays resolve only the requested identity',()=>{
  const mapping=[{lid:'other@lid',pn:'15550002222@c.us'},{lid:'opaque@lid',pn:'15550001111@c.us'}];
  const row=contactRecord({id:'opaque@lid'},'opaque@lid',mapping);
  assert.equal(row.phone_number,'15550001111');
  assert.deepEqual(row.aliases,['opaque@lid','15550001111@c.us']);
});

test('mapped address-book name wins over a LID profile name',async()=>{
  const {lookupContact}=await import('./contacts.js');
  const row=await lookupContact({
    getContactById:async id=>id.endsWith('@lid')?{id,pushname:'~'}:{id,name:'Saved Friend'},
    getContactLidAndPhone:async()=>[{lid:'123@lid',pn:'15550001111@c.us'}],
  },'123@lid');
  assert.equal(row.saved_name,'Saved Friend');
  assert.deepEqual(row.aliases,['123@lid','15550001111@c.us']);
});

test('mapped lookup recovers from a failed original lookup; unrelated mappings never join',async()=>{
  const {lookupContact}=await import('./contacts.js');
  const client={getContactById:async id=>{if(id.endsWith('@lid'))throw Error();return {id,name:'Friend'};},
    getContactLidAndPhone:async()=>[{lid:'123@lid',pn:'15550001111@c.us'}]};
  assert.equal((await lookupContact(client,'123@lid')).saved_name,'Friend');
  assert.equal(await lookupContact(client,'999@lid'),null);
});

test('unnamed successful lookups retry after the shorter TTL',async()=>{
  let now=0,calls=0;
  const resolver=createContactResolver({now:()=>now,lookup:async id=>({id,saved_name:++calls>1?'Friend':null})});
  await resolver.resolve(['123@lid']);
  now=NEGATIVE_TTL+1;
  assert.equal((await resolver.resolve(['123@lid']))[0].saved_name,'Friend');
});

test('direct peers refresh without incoming messages, group IDs never become people',async()=>{
  const {chatContactIds}=await import('./contacts.js');
  const direct={id:'15550001111@c.us',is_group:false};
  assert.deepEqual(chatContactIds(direct,[]),[direct.id]);
  assert.deepEqual(chatContactIds(direct,[{from_me:true,sender:'self@c.us'}]),[direct.id]);
  assert.deepEqual(chatContactIds({id:'group@g.us'},[{sender:'123@lid',from_me:false}]),['123@lid']);
});
