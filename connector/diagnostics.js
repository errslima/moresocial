// Sanitized connector events, in the schema shared with the Python services (app/diagnostics.py):
// schema, ts, level, service, release, event, reference, request_id and field-validated metadata.
// Never message text, QR payloads, keys, URLs or error messages; stack locations only.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {AsyncLocalStorage} from 'node:async_hooks';
const context=new AsyncLocalStorage(),buckets=new Map();
const root=process.env.DIAGNOSTICS_DIR||'/diagnostics',file=path.join(root,'events.jsonl');
const release=/^[0-9a-f]{7,40}$/.test(process.env.MORESOCIAL_RELEASE||'')?process.env.MORESOCIAL_RELEASE:null;
const LEVELS=['debug','info','warn','error'];
const SECRET=/^(sk[-_]|pa-|ya29\.|1\/\/|eyJ|Bearer)|[A-Za-z0-9+/_-]{40,}/;
export const reference=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value)?value:crypto.randomBytes(16).toString('hex');
// The request (or operation) ID for outgoing calls: the adopted one inside a request, else a new one.
export const current=()=>context.getStore()||reference();
export function record(event,fields={},error=null){
 const level=LEVELS.includes(fields.level)?fields.level:error?'error':/_(failed|refused|disconnected)$/.test(event)?'warn':'info';
 const row={schema:1,ts:new Date().toISOString(),level,service:'connector',event:/^[a-z][a-z0-9_]{0,79}$/.test(event)?event:'invalid_event',
  reference:crypto.randomBytes(8).toString('hex')};
 if(release)row.release=release;
 const request=context.getStore();if(request)row.request_id=request;
 for(const key of ['status','duration_ms','count','exit_code'])if(Number.isFinite(fields[key]))row[key]=fields[key];
 for(const key of ['code','outcome','method'])if(typeof fields[key]==='string'&&/^[A-Za-z0-9_.:-]{1,80}$/.test(fields[key])&&!SECRET.test(fields[key]))row[key]=fields[key];
 if(fields.route&&/^\/[a-z-]{1,40}$/.test(fields.route))row.route=fields.route;
 if(error){row.error_type=['TypeError','RangeError','SyntaxError','Error','TimeoutError','AbortError'].includes(error.name)?error.name:'Error';
  row.frames=String(error.stack||'').split('\n').slice(1,7).map(line=>{const m=line.match(/([^/\\\s():]+\.(?:js|cjs|mjs)):(\d+):(\d+)/);return m?{file:m[1],line:Number(m[2]),column:Number(m[3])}:null;}).filter(Boolean);}
 // At most 20 events per event/status/code per minute; the next admitted one carries the suppressed count.
 const key=event+':'+(row.status||'')+':'+(row.code||''),now=Date.now();let bucket=buckets.get(key);
 if(!bucket||now-bucket.at>60000){if(bucket?.dropped)row.suppressed=bucket.dropped;bucket={at:now,n:0,dropped:0};}
 buckets.set(key,bucket);if(bucket.n++>=20){bucket.dropped++;return row.reference;}
 if(buckets.size>500)buckets.clear();
 const line=JSON.stringify(row)+'\n';
 try{process.stderr.write(line);}catch{}
 try{if(fs.existsSync(root)){
   if(fs.existsSync(file)&&fs.statSync(file).size+Buffer.byteLength(line)>5*1024*1024){
     for(let i=4;i>=1;i--){const src=i===1?file:file+'.'+(i-1),dest=file+'.'+i;
       if(i===4&&fs.existsSync(dest))fs.unlinkSync(dest);if(fs.existsSync(src))fs.renameSync(src,dest);}
   }
   fs.appendFileSync(file,line,{mode:0o600});
 }}catch{}
 return row.reference;
}
// Run fn under a fresh operation ID so its events and ingest calls correlate.
export const withOperation=fn=>context.run(reference(),fn);
const routes=new Set(['/status','/sync','/connect','/disconnect','/send']);
export function withRequest(req,res,fn){
 return context.run(reference(req.headers['x-request-id']),async()=>{
  const ref=current(),started=Date.now();res.setHeader('X-Request-ID',ref);
  const raw=String(req.url||'').split('?')[0],route=routes.has(raw)?raw:'/other';
  res.on('finish',()=>{if(res.statusCode>=400)record('request_failed',{route,method:req.method,status:res.statusCode,duration_ms:Date.now()-started});});
  try{return await fn();}catch(error){record('request_exception',{route,method:req.method},error);if(!res.headersSent){res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'Connector request failed',reference:ref}));}else res.destroy();}
 });
}
