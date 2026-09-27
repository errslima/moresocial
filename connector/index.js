// Moresocial WhatsApp connector: one container per workspace, QR-linked WhatsApp Web
// (whatsapp-web.js LocalAuth). Adapted from EnzoSocial's connector (GPL-3.0);
// photo download and repair routes were removed. It holds only its own session
// and a key scoped to its workspace; it has no database or model credentials.
import {record,current,withOperation} from './diagnostics.js';
import fs from 'node:fs';
import crypto from 'node:crypto';
import wweb from 'whatsapp-web.js';
import QRCode from 'qrcode';
import {createContactResolver,lookupContact,chatContactIds} from './contacts.js';
import {normalizeRawMessage,normalizeWrapperMessage} from './normalize.js';
import {LIMITS,selectChats,boundMessages} from './history.js';
import {createQueue} from './queue.js';
import {createHandler,listen} from './server.js';
import {createSender} from './send.js';
import {createHeartbeat} from './health.js';
const {Client,LocalAuth}=wweb;

const SESSION=process.env.SESSION_DIR||'/session';
const WEB=process.env.WEB_INTERNAL_URL||'http://web:8001';
const key=fs.readFileSync(process.env.CONNECTOR_KEY_FILE||'/run/secrets/connector_key','utf8').trim();
let client=null, desired=true, starting=false, syncing=false;
let selfChat=null;
let status={state:'starting',qr:null,last_sync:null,chats_synced:0,error:null,error_code:null,
  sweep:{last_at:null,outcome:null,failed_chats:0,last_complete_at:null}};
const historyPath=SESSION+'/history.json';
let history=fs.existsSync(historyPath)?JSON.parse(fs.readFileSync(historyPath,'utf8')):{};
const persistHistory=()=>{fs.writeFileSync(historyPath+'.tmp',JSON.stringify(history),{mode:0o600});fs.renameSync(historyPath+'.tmp',historyPath);};

async function post(packet){
  const r=await fetch(WEB+'/internal/connector/ingest',{method:'POST',headers:{'Content-Type':'application/json',
    Authorization:'Bearer '+key,'X-Request-ID':current()},body:JSON.stringify(packet),signal:AbortSignal.timeout(20000)});
  if(!r.ok){record('ingest_refused',{status:r.status});throw Error('ingest HTTP '+r.status);}
}
const queue=createQueue(SESSION+'/pending.json',{post,onError:e=>{record('ingest_failed',{},e);
  status.error='Message storage unavailable; pending messages will retry';status.error_code='ingest_unavailable';}});

const contactResolver=createContactResolver({lookup:async id=>{
  if(!client||status.state!=='ready')return null;
  return lookupContact(client,id);
}});

const me=()=>client?.info?.wid?._serialized||null;
function mediaMarker(m){
  const value=m?.mediaKey||m?._data?.mediaKey;
  return typeof value==='string'&&value.length<2048?crypto.createHash('sha256').update(value).digest('hex'):null;
}

async function event(m,revoked=false){
  // Live messages use the same idempotent ingest path as history.
  try{
    if(!client||m.isStatus||m.from==='status@broadcast')return;
    const chatId=m.fromMe?m.to:m.from;
    if(!chatId||chatId==='status@broadcast')return;
    const {chat,key:canonical}=await client.pupPage.evaluate((id,msgId)=>{
      const c=window.require('WAWebCollections').Chat.getModelsArray().find(x=>x.id?._serialized===id);
      const found=msgId&&c?.msgs?.getModelsArray?.()?.find(x=>x.id?.id===msgId);
      return {key:found?String(found.id):null,
        chat:c?{id,name:String(c.formattedTitle||c.name||c.id.user||id),is_group:!!c.groupMetadata||c.id.server==='g.us'}:{id,name:id,is_group:id.endsWith('@g.us')}};
    },chatId,m.id?.id||null);
    const row=normalizeWrapperMessage(m,revoked,{mediaMarker:mediaMarker(m),selfId:me()});
    if(!row.id)row.id=canonical;
    if(!row.id||!Number.isFinite(row.ts))return;
    const contacts=await contactResolver.resolve(chatContactIds(chat,[row]));
    queue.push({account:me(),chat,messages:boundMessages([row]),contacts});await queue.flush();
  }catch(e){record('message_event_failed',{},e);}
}

async function sync(){
  if(syncing||status.state!=='ready')return;
  syncing=true;status.chats_synced=0;let errors=0;
  try{
    // Read only already-loaded page models (EnzoSocial compatibility note: the library's
    // getChatModel() breaks on some WhatsApp Web versions). Nothing here can send.
    const all=await client.pupPage.evaluate(()=>window.require('WAWebCollections').Chat.getModelsArray().map(c=>({
      id:c.id?._serialized,name:String(c.formattedTitle||c.name||c.id?.user||''),
      is_group:!!c.groupMetadata||c.id?.server==='g.us',active:Number(c.t)||0})).filter(c=>c.id));
    const chats=selectChats(all);
    status.chats_total=all.length;status.history_chats=chats.length;
    for(const c of chats){
      if(!desired||status.state!=='ready')break;
      try{
        if(!history[c.id]?.done)await client.pupPage.evaluate(async(id,target)=>{
          // loadEarlierMsgs only reads history into the page.
          const chat=window.require('WAWebCollections').Chat.getModelsArray().find(c=>c.id?._serialized===id);
          const loader=window.require('WAWebChatLoadMessages');
          let rounds=0;
          while(chat&&chat.msgs.getModelsArray().length<target&&!chat.msgs.msgLoadState?.noEarlierMsgs&&rounds<20){
            const got=await loader.loadEarlierMsgs({chat});rounds++;if(!got?.length)break;
          }
        },c.id,LIMITS.messagesPerChat);
        const since=(history[c.id]?.sent_ts??0)-60;
        const raw=await client.pupPage.evaluate((id,me,since)=>{
          const chat=window.require('WAWebCollections').Chat.getModelsArray().find(c=>c.id?._serialized===id);
          const ser=x=>x?(typeof x==='string'?x:x._serialized||String(x)):null;
          const keyRe=/^(true|false)_.+_.+/;
          return (chat?.msgs?.getModelsArray?.()||[]).filter(m=>(Number(m.t)||0)>=since&&keyRe.test(ser(m.id)||'')).map(m=>({
            id:ser(m.id),t:m.t,author:ser(m.author),from:ser(m.from),fromMe:!!m.id.fromMe,
            sender:ser(m.author)||(m.id.fromMe?me:ser(m.from))||id,body:typeof m.body==='string'?m.body:'',
            caption:typeof m.caption==='string'?m.caption:null,type:m.type||'chat',isRevoked:!!m.isRevoked,
            hasMedia:!!(m.hasMedia||m.mediaData||m._data?.mediaData),mimetype:typeof m.mimetype==='string'?m.mimetype:null}));
        },c.id,me(),since);
        const messages=boundMessages(raw.map(m=>normalizeRawMessage(m,{sender:m.sender})));
        const contacts=await contactResolver.resolve(chatContactIds(c,messages));
        const {active,...chat}=c;
        queue.push({account:me(),chat,messages,contacts});await queue.flush();
        status.chats_synced++;
        const entry=history[c.id]||{};
        if(messages.length)entry.sent_ts=Math.max(entry.sent_ts||0,...messages.map(m=>m.ts));
        entry.done=true;history[c.id]=entry;
      }catch(e){record('chat_sync_failed',{},e);errors++;}
    }
    persistHistory();
    status.history_done=chats.filter(c=>history[c.id]?.done).length;
    // last_sync keeps its old meaning (sweep finished, possibly partially); sweep.* says how it went.
    const at=new Date().toISOString();
    status.last_sync=at;
    status.sweep={last_at:at,outcome:errors?'partial':'ok',failed_chats:errors,
      last_complete_at:errors?status.sweep.last_complete_at:at};
    status.error=errors?`${errors} chats could not be synced; will retry`:queue.length?'Messages pending storage':null;
    status.error_code=errors?'sync_partial':null;
  }catch(e){record('history_sync_failed',{},e);status.error='History sync failed; will retry';status.error_code='sync_failed';
    status.sweep={...status.sweep,last_at:new Date().toISOString(),outcome:'failed'};}
  finally{syncing=false;record('history_sync_completed',{count:status.chats_synced,outcome:errors?'partial':'ok'});}
}

async function connect(){
  if(starting||client)return;
  desired=true;starting=true;status.state='starting';status.error=null;status.error_code=null;
  // After a container restart Chromium's Singleton* symlinks point at a dead container.
  for(const name of ['SingletonLock','SingletonCookie','SingletonSocket']){
    const p=SESSION+'/auth/session/'+name;
    try{if(fs.lstatSync(p).isSymbolicLink())fs.unlinkSync(p);}catch(e){if(e.code!=='ENOENT')throw e;}
  }
  const instance=new Client({authStrategy:new LocalAuth({dataPath:SESSION+'/auth'}),
    webVersionCache:{type:'local',path:SESSION+'/web-cache'},
    puppeteer:{headless:true,executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox','--disable-dev-shm-usage']}});
  client=instance;
  instance.on('qr',async qr=>{status.qr=await QRCode.toDataURL(qr);status.state='pairing';});
  instance.on('authenticated',()=>{status.state='loading';status.qr=null;});
  instance.on('ready',async()=>{
    selfChat=await instance.pupPage.evaluate(()=>{
      const me=window.require('WAWebUserPrefsMeUser');
      return me.getMaybeMeLidUser?.()?._serialized||me.getMaybeMePnUser?.()?._serialized||null;
    }).catch(()=>null);
    status.state='ready';status.qr=null;status.error=null;status.error_code=null;void withOperation(sync);
  });
  instance.on('auth_failure',()=>{record('whatsapp_auth_failed');status.state='auth_failure';status.qr=null;});
  instance.on('disconnected',()=>{record('whatsapp_disconnected');status.state='disconnected';status.qr=null;});
  instance.on('message_create',m=>void withOperation(()=>event(m)));
  instance.on('message_revoke_everyone',(after,before)=>void withOperation(()=>event(before||after,true)));
  instance.on('message_edit',m=>void withOperation(()=>event(m)));
  try{await instance.initialize();}
  catch(e){record('browser_connection_failed',{},e);status.state='error';status.error='Browser connection failed';
    status.error_code='browser_failed';
    try{await instance.destroy();}catch{}client=null;}
  finally{starting=false;}
}

async function disconnect(){
  // Stop reconnect loops first, then log out so the linked device disappears from the phone.
  desired=false;
  try{if(client){await client.logout();await client.destroy();}}catch{status.error='Could not confirm logout; unlink this device on your phone';}
  client=null;status.state='stopped';status.qr=null;queue.clear();
}

const send=createSender({state:()=>desired?status.state:'stopped',account:me,
  chat:id=>client.pupPage.evaluate(id=>{
    const c=window.require('WAWebCollections').Chat.getModelsArray().find(c=>c.id?._serialized===id);
    return c?{id,name:String(c.formattedTitle||c.name||id),is_group:c.id.server==='g.us'}:null;
  },id).then(c=>c||(id===me()||id===selfChat?{id,name:'You',is_group:false}:null)),
  send:(...args)=>client.sendMessage(...args),
  onSent:async packet=>{queue.push(packet);void queue.flush();}});

listen(createHandler({key,
  status:()=>({...status,syncing,pending:queue.length,account:status.state==='ready'?me():null,
    self_chat:status.state==='ready'?(selfChat||me()):null}),
  connect:async()=>{desired=true;if(client){try{await client.destroy();}catch{}client=null;}await connect();},
  sync,disconnect,send}));
setInterval(()=>void withOperation(()=>queue.flush()),10000);
// Health reports use their own request, so a slow or failing web app never blocks ingestion.
const heartbeat=createHeartbeat({record,
  snapshot:()=>desired?{...status,syncing,pending:queue.length,pending_age_s:queue.age(),ingest_failing:queue.failing}:null,
  post:async report=>{
    const r=await fetch(WEB+'/internal/connector/health',{method:'POST',headers:{'Content-Type':'application/json',
      Authorization:'Bearer '+key,'X-Request-ID':current()},body:JSON.stringify(report),signal:AbortSignal.timeout(10000)});
    if(!r.ok)throw Error('health HTTP '+r.status);
  }});
heartbeat.start();
setInterval(()=>heartbeat.changed(desired?status.state:'stopped'),1000);
setInterval(()=>void withOperation(sync),15*60*1000);
setInterval(()=>{if(desired&&!starting&&['disconnected','error'].includes(status.state)){client=null;void connect();}},60000);
void connect();
process.on('unhandledRejection',error=>{record('unhandled_rejection',{},error);process.exit(1);});
process.on('uncaughtException',error=>{record('uncaught_exception',{},error);process.exit(1);});
record('service_started');
