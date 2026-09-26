// Text only, to an existing conversation, from the exact linked account requested.
// The web app durably claims each request before calling this endpoint and never retries it.
export function createSender({state,account,chat,send,onSent}){
  return async input=>{
    if(!input||typeof input.account!=='string'||typeof input.chat!=='string'||
      !/^[0-9a-z._-]{1,80}@(c\.us|g\.us|lid|s\.whatsapp\.net)$/.test(input.chat)||
      typeof input.text!=='string'||!input.text.trim()||Array.from(input.text).length>4000||input.text.includes('\0')){
      return {state:'rejected'};
    }
    if(state()!=='ready'||account()!==input.account)return {state:'rejected'};
    let target;
    try{target=await chat(input.chat);}catch{return {state:'rejected'};}
    if(!target||target.id!==input.chat||state()!=='ready'||account()!==input.account)return {state:'rejected'};
    // Once send() starts, errors are ambiguous: they must never be described as unsent.
    try{
      const msg=await send(input.chat,input.text.trim(),{sendSeen:false,waitUntilMsgSent:true,linkPreview:false});
      if(!msg?.id?._serialized||!msg.fromMe)return {state:'unknown'};
      const packet={account:input.account,chat:target,messages:[{id:msg.id._serialized,
        ts:msg.timestamp,from_me:true,sender:input.account,kind:'chat',body:input.text.trim()}]};
      // Queue the same packet as the event handler. Ingestion deduplicates its message ID.
      try{await onSent(packet);}catch{/* The response also carries the packet for ingestion. */}
      return {state:'sent',packet};
    }catch{return {state:'unknown'};}
  };
}
