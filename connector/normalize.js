const MEDIA=new Set(['image','video','document','audio','ptt','sticker']);
const TYPES=new Set(['chat','image','video','document','audio','ptt','sticker','location','vcard','poll_creation','call_log','revoked','unsupported']);
const serial=x=>typeof x==='string'?x:x?._serialized||'';
const clean=(x,max=20000)=>typeof x==='string'?x.slice(0,max):'';

function normalizedKind(type){
  if(type==='chat'||type==='text')return 'chat';
  if(MEDIA.has(type))return type;
  if(type==='revoked')return 'revoked';
  return TYPES.has(type)?type:'unsupported';
}

function record(input,source,revoked=false,options={}){
  const kind=revoked||input?.isRevoked===true?'revoked':normalizedKind(input?.type);
  const isMedia=MEDIA.has(kind);
  const rawCaption=source==='wrapper'?(input?.caption??input?._data?.caption):(input?.caption??input?._data?.caption);
  const body=kind==='revoked'?'[Message deleted]':isMedia?clean(rawCaption):kind==='chat'?clean(input?.body):'';
  const type=typeof input?.type==='string'?input.type:'unsupported';
  const hasMedia=kind!=='revoked'&&isMedia&&(input?.hasMedia===true||input?.mediaData!=null||input?._data?.mediaData!=null);
  const mime=input?.mimetype||input?._data?.mimetype;
  return {id:serial(input?.id),ts:Number(input?.timestamp??input?.t),
    sender:options.sender||serial(input?.author)||((input?.fromMe??input?.id?.fromMe)&&options.selfId)||serial(input?.from),body,
    from_me:!!(input?.fromMe??input?.id?.fromMe),kind:kind==='unsupported'&&type!=='unsupported'?type:kind,
    media_available:hasMedia,media_mime:isMedia&&typeof mime==='string'&&mime.length<=100?mime:null,
    media_restricted:!!(input?.isViewOnce||input?._data?.isViewOnce||input?.isEphemeral||input?._data?.isEphemeral),
    media_marker:typeof options.mediaMarker==='string'?options.mediaMarker:(typeof input?.mediaMarker==='string'?input.mediaMarker:null),protocol_version:2,
    ...(quoteRecord(options.quote??input?.quote)?{quote:quoteRecord(options.quote??input?.quote)}:{})};
}

// Bounded metadata about the message a reply quotes; the quote's own quote is never followed.
export function quoteRecord(q){
  if(!q||typeof q!=='object')return null;
  const text=(x,max)=>typeof x==='string'&&x?x.slice(0,max):null;
  const out={id:text(q.id,300),stanza:text(q.stanza,300),sender:text(q.sender,300),excerpt:text(q.excerpt,200),kind:text(q.kind,50)};
  return out.id||out.stanza?out:null;
}

export const normalizeWrapperMessage=(message,revoked=false,options={})=>record(message,'wrapper',revoked,options);
export const normalizeRawMessage=(raw,options={})=>record(raw,'raw',false,options);
export const isMediaKind=kind=>MEDIA.has(kind);
export const supportedKind=kind=>TYPES.has(kind);

// Current WhatsApp Web serializes MsgKey without _serialized, but library
// downloadMedia still requires it. The caller already looked up this exact key.
export function restoreMessageKey(message,key){
  if(message?.id&&typeof message.id==='object'&&!message.id._serialized)message.id._serialized=key;
  return message;
}
