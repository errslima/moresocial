// Pure history bounds, separate from whatsapp-web.js so they are testable.
export const LIMITS={chats:50,messagesPerChat:200,maxAgeDays:90};

export function selectChats(chats,now=Date.now(),limits=LIMITS){
  const cutoff=now/1000-limits.maxAgeDays*86400;
  return chats.filter(c=>c&&typeof c.id==='string'&&!c.id.endsWith('@broadcast')&&Number(c.active)>=cutoff)
    .sort((a,b)=>b.active-a.active).slice(0,limits.chats);
}

export function boundMessages(messages,now=Date.now(),limits=LIMITS){
  const cutoff=now/1000-limits.maxAgeDays*86400;
  return messages.filter(m=>Number.isFinite(m.ts)&&m.ts>=cutoff).slice(-limits.messagesPerChat);
}
