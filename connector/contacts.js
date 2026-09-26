export const CONTACT_TTL=24*60*60*1000;
export const NEGATIVE_TTL=15*60*1000;

// Resolver is independent of whatsapp-web.js so TTLs, batching and failure behavior can be tested.
export function createContactResolver({lookup, now=()=>Date.now(), concurrency=5}={}){
  const cache=new Map();
  async function resolve(ids){
    const unique=[...new Set((ids||[]).filter(x=>typeof x==='string'&&x.length&&x.length<=300))].slice(0,200);
    const output=[];let next=0;
    async function worker(){
      while(next<unique.length){
        const id=unique[next++], hit=cache.get(id), time=now();
        if(hit&&time-hit.time<(hit.value?.saved_name||hit.value?.profile_name?CONTACT_TTL:NEGATIVE_TTL)){if(hit.value)output.push(hit.value);continue;}
        try{
          const value=await lookup(id);
          if(value){
            const record={...value,id:value.id||id,aliases:[...new Set([id,...(Array.isArray(value.aliases)?value.aliases:[])])].slice(0,20),refreshed:Math.floor(time/1000),source:'whatsapp'};
            cache.set(id,{time,value:record});output.push(record);
          }else cache.set(id,{time,value:null});
        }catch{cache.set(id,{time,value:null});}
      }
    }
    await Promise.all(Array.from({length:Math.min(concurrency,unique.length)},worker));
    return output;
  }
  return {resolve,cache};
}

export function contactRecord(contact,requestedId,mapping=null){
  if(!contact)return null;
  const id=contact.id?._serialized||contact.id||requestedId;
  const aliases=[requestedId,id].filter(x=>typeof x==='string'&&x);
  if(Array.isArray(mapping))mapping=mapping.find(m=>m&&(m.lid===requestedId||m.pn===requestedId||m.lid===id||m.pn===id))||null;
  let phoneIdentity=null,phoneNumber=null;
  if(mapping&&typeof mapping==='object'){
    // Only explicit WA mapping fields are allowed to join identities.
    const lid=typeof mapping.lid==='string'?mapping.lid:null;
    const pn=typeof mapping.pn==='string'?mapping.pn:(typeof mapping.phone==='string'?mapping.phone:null);
    if(lid&&pn){aliases.push(lid,pn);phoneIdentity=pn;phoneNumber=pn.split('@')[0]||null;}
  }
  const text=x=>typeof x==='string'&&x.trim()?x.trim().slice(0,200):null;
  return {id,aliases:[...new Set(aliases)].slice(0,20),saved_name:text(contact.name),
    profile_name:text(contact.pushname)||text(contact.shortName),phone_identity:phoneIdentity,phone_number:phoneNumber};
}

// A LID contact may have only a profile name while the explicitly mapped phone
// contact holds the address-book name. Keep lookup failures independent.
export async function lookupContact(client,id){
  const get=async value=>{try{return await client.getContactById(value);}catch{return null;}};
  const contact=await get(id);
  let mapping=null;
  try{mapping=await client.getContactLidAndPhone([id]);}catch{}
  const row=contactRecord(contact||{id},id,mapping);
  for(const alias of row.aliases){
    if(alias===id)continue;
    const other=contactRecord(await get(alias),alias);
    if(other){row.saved_name ||= other.saved_name;row.profile_name ||= other.profile_name;}
  }
  return contact||row.saved_name||row.profile_name||row.phone_identity?row:null;
}

export function chatContactIds(chat,messages){
  // Refresh the peer even when all messages are outgoing or the history cursor
  // yields no new messages. Group titles must never become participant names.
  return [...new Set([...(chat.is_group||chat.id.endsWith('@g.us')?[]:[chat.id]),
    ...messages.filter(m=>!m.from_me).map(m=>m.sender)].filter(Boolean))];
}
