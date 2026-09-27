// Health heartbeat to the web app's internal API: every minute and soon after a state change.
// It reports only connection state, queue counts and history-sweep outcomes: never message
// text, QR data, account IDs or error text. A failed beat is recorded and retried by the next
// timer; it never throws and never touches the ingest queue.
const STATES=new Set(['starting','pairing','loading','ready','disconnected','auth_failure','error']);
const OUTCOMES=new Set(['ok','partial','failed']);
const CODES=new Set(['ingest_unavailable','sync_partial','sync_failed','browser_failed']);
const count=v=>Number.isInteger(v)&&v>=0?Math.min(v,1000000):0;
const iso=v=>typeof v==='string'&&!Number.isNaN(Date.parse(v))?v:null;

export function healthReport(s){
  return {version:1,state:STATES.has(s.state)?s.state:'error',pending:count(s.pending),
    pending_age_s:count(s.pending_age_s),ingest_failing:s.ingest_failing===true,syncing:s.syncing===true,
    sweep:{last_at:iso(s.sweep?.last_at),outcome:OUTCOMES.has(s.sweep?.outcome)?s.sweep.outcome:null,
      failed_chats:count(s.sweep?.failed_chats),last_complete_at:iso(s.sweep?.last_complete_at)},
    history:{chats_total:count(s.chats_total),history_chats:count(s.history_chats),
      history_done:count(s.history_done),chats_synced:count(s.chats_synced)},
    error_code:CODES.has(s.error_code)?s.error_code:null};
}

export function createHeartbeat({snapshot,post,record=()=>{},interval=60000,settle=2000}){
  let inFlight=false,again=false,timer=null,pending=null,last=null;
  async function beat(){
    if(inFlight){again=true;return false;}
    inFlight=true;
    try{
      const current=snapshot();
      if(current===null)return false;  // not wanted (e.g. after unlinking): nothing to report
      const report=healthReport(current);
      last=report.state;
      await post(report);
      return true;
    }catch(e){record('heartbeat_failed',{},e);return false;}
    finally{
      inFlight=false;
      if(again){again=false;void beat();}
    }
  }
  return {
    beat,
    // A state change is reported shortly after it settles, not on every intermediate event.
    changed(state){
      if(state===last||pending)return;
      pending=setTimeout(()=>{pending=null;void beat();},settle);
    },
    start(){timer=setInterval(()=>void beat(),interval);void beat();return timer;},
    stop(){clearInterval(timer);clearTimeout(pending);pending=null;},
  };
}
