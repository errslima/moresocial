import crypto from 'node:crypto';
import http from 'node:http';
import {withRequest} from './diagnostics.js';

export function sameKey(header,secret){
  if(!secret)return false;
  const a=Buffer.from(header||''),b=Buffer.from('Bearer '+secret);
  return a.length===b.length&&crypto.timingSafeEqual(a,b);
}

// Private, workspace-key authenticated control surface.
export function createHandler({key,status,connect,sync,disconnect,send}){
  return (req,res)=>withRequest(req,res,async()=>{
    if(!sameKey(req.headers.authorization,key)){res.writeHead(401);res.end();return;}
    res.setHeader('Content-Type','application/json');res.setHeader('Cache-Control','no-store');
    const path=String(req.url||'').split('?')[0];
    if(req.method==='POST'&&path==='/send'&&send){
      let size=0;const chunks=[];
      try{
        for await(const chunk of req){size+=chunk.length;if(size>32768){res.writeHead(413);res.end('{"state":"rejected"}');return;}chunks.push(chunk);}
        let input;
        try{input=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{res.writeHead(400);res.end('{"state":"rejected"}');return;}
        res.end(JSON.stringify(await send(input)));
      }catch{if(!res.writableEnded){res.writeHead(503);res.end('{"state":"unknown"}');}}
      return;
    }
    if(req.method==='GET'&&path==='/status'){res.end(JSON.stringify(status()));return;}
    if(req.method==='POST'&&path==='/connect'){void connect();res.end('{"ok":true}');return;}
    if(req.method==='POST'&&path==='/sync'){void sync();res.end('{"ok":true}');return;}
    if(req.method==='POST'&&path==='/disconnect'){await disconnect();res.end('{"ok":true}');return;}
    res.writeHead(404);res.end('{"error":"Not found"}');
  });
}

export function listen(handler,port=8790){
  return http.createServer(handler).listen(port,'0.0.0.0');
}
