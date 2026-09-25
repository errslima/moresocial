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
        if(hit&&time-hit.time<(hit.value?CONTACT_TTL:NEGATIVE_TTL)){if(hit.value)output.push(hit.value);continue;}
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
