import fs from 'node:fs';
import {guardQueuedPacket} from './protocol.js';

// Durable outbox of ingest packets. Web ingestion is idempotent, so a packet that was stored
// but not acknowledged before a crash is simply delivered again.
export function createQueue(path,{post,onError=()=>{}}){
  let queue=fs.existsSync(path)?JSON.parse(fs.readFileSync(path,'utf8')):[];
  let flushing=false;
  const persist=()=>{fs.writeFileSync(path+'.tmp',JSON.stringify(queue),{mode:0o600});fs.renameSync(path+'.tmp',path);};
  return {
    get length(){return queue.length;},
    push(packet){queue.push(packet);persist();},
    async flush(){
      if(flushing)return;flushing=true;
      try{
        while(queue.length){
          queue[0]=guardQueuedPacket(queue[0]);
          await post(queue[0]);
          queue.shift();persist();
        }
      }catch(e){onError(e);}
      finally{flushing=false;}
    },
    clear(){queue=[];persist();},
  };
}
