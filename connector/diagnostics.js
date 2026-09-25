import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {AsyncLocalStorage} from 'node:async_hooks';
const context=new AsyncLocalStorage(),buckets=new Map();
const root=process.env.DIAGNOSTICS_DIR||'/diagnostics',file=path.join(root,'events.jsonl');
export const reference=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value)?value:crypto.randomBytes(16).toString('hex');
export const current=()=>context.getStore()||reference();
export function record(event,fields={},error=null){
 const row={schema:1,ts:new Date().toISOString(),service:'connector',event,reference:current()};
 for(const key of ['status','duration_ms','count','exit_code'])if(Number.isFinite(fields[key]))row[key]=fields[key];
 for(const key of ['code','outcome','method'])if(typeof fields[key]==='string'&&/^[A-Za-z0-9_.:-]{1,80}$/.test(fields[key]))row[key]=fields[key];
 if(fields.route&&/^\/[a-z-]{1,40}$/.test(fields.route))row.route=fields.route;
 if(error){row.error_type=['TypeError','RangeError','SyntaxError','Error','TimeoutError','AbortError'].includes(error.name)?error.name:'Error';
  row.frames=String(error.stack||'').split('\n').slice(1,7).map(line=>{const m=line.match(/([^/\\\s():]+\.(?:js|cjs|mjs)):(\d+):(\d+)/);return m?{file:m[1],line:Number(m[2]),column:Number(m[3])}:null;}).filter(Boolean);}
 const key=event+':'+(row.status||'')+':'+(row.code||''),now=Date.now();let bucket=buckets.get(key)||{at:now,n:0};
 if(now-bucket.at>60000)bucket={at:now,n:0};buckets.set(key,bucket);if(bucket.n++>=20)return row.reference;
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
const routes=new Set(['/status','/sync','/connect','/disconnect']);
export function withRequest(req,res,fn){
 return context.run(reference(req.headers['x-request-id']),async()=>{
  const ref=current(),started=Date.now();res.setHeader('X-Request-ID',ref);
  const raw=String(req.url||'').split('?')[0],route=routes.has(raw)?raw:'/other';
  res.on('finish',()=>{if(res.statusCode>=400)record('request_failed',{route,method:req.method,status:res.statusCode,duration_ms:Date.now()-started});});
  try{return await fn();}catch(error){record('request_exception',{route,method:req.method},error);if(!res.headersSent){res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'Connector request failed',reference:ref}));}else res.destroy();}
 });
}
