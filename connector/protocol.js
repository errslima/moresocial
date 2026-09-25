const MEDIA=new Set(['image','video','document','audio','ptt','sticker']);

export function guardQueuedPacket(packet){
  // Pre-v2 raw history copied m.body for media. Preserve message identity and metadata,
  // but drop the untrusted old body before replay; the resumable repair can refill it.
  for(const m of packet.messages||[])if(!Number.isInteger(m.protocol_version)||m.protocol_version<2){
    if(MEDIA.has(m.kind))m.body='';
    m.protocol_version=Math.max(1,Number(m.protocol_version)||1);
  }
  return packet;
}
