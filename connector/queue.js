import fs from 'node:fs';
import {guardQueuedPacket} from './protocol.js';

// Durable outbox of ingest packets. Web ingestion is idempotent, so a packet that was stored
// but not acknowledged before a crash is simply delivered again.
export function createQueue(path,{post,onError=()=>{},now=Date.now}){
  let queue=fs.existsSync(path)?JSON.parse(fs.readFileSync(path,'utf8')):[];
  let flushing=false,failing=false;
  // When the queue last became non-empty (in memory; a restart counts from startup).
  let since=queue.length?now():null;
  const persist=()=>{fs.writeFileSync(path+'.tmp',JSON.stringify(queue),{mode:0o600});fs.renameSync(path+'.tmp',path);};
  return {
    get length(){return queue.length;},
    get failing(){return failing;},
    // Seconds the oldest undelivered packet has been waiting, for health reports.
    age(){return since===null?0:Math.max(0,Math.floor((now()-since)/1000));},
    push(packet){if(!queue.length)since=now();queue.push(packet);persist();},
    async flush(){
      if(flushing)return;flushing=true;
      try{
        while(queue.length){
          queue[0]=guardQueuedPacket(queue[0]);
          await post(queue[0]);
          queue.shift();persist();
        }
        failing=false;since=null;
      }catch(e){failing=true;onError(e);}
      finally{flushing=false;}
    },
    clear(){queue=[];since=null;failing=false;persist();},
  };
}
