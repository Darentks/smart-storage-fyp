// Optional test-only PostgreSQL engine: set PHASE1_PGLITE_MODULE to its module path.
const assert=require('node:assert/strict'),fs=require('node:fs');
if(!process.env.PHASE1_PGLITE_MODULE){console.log('SKIP: Phase 1 SQL tests require PHASE1_PGLITE_MODULE (test-only PGlite)');process.exit(0);}
const {PGlite}=require(process.env.PHASE1_PGLITE_MODULE);
(async()=>{
 const db=new PGlite();
 await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
 CREATE TABLE public.compartments(shelf_id varchar(50) PRIMARY KEY,rack_id text DEFAULT 'r',current_quantity integer NOT NULL DEFAULT 5,max_capacity integer NOT NULL DEFAULT 5,tm_model_url text);
 INSERT INTO public.compartments(shelf_id,current_quantity,max_capacity) VALUES('s',5,5),('legacy-six',6,6);`);
 const migration=fs.readFileSync('migrations/20261121_persistent_inventory.sql','utf8');
 await db.exec(migration);await db.exec(migration);
 const row=async(shelf='s')=>(await db.query('SELECT * FROM public.compartments WHERE shelf_id=$1',[shelf])).rows[0];
 const initial=await row();assert.equal(initial.confirmed_occupancy,null);assert.equal(initial.current_quantity,5);assert.equal(initial.revision,0);
 assert.equal((await row('legacy-six')).current_quantity,6);
 const args=(id,revision,qty,previous,next,action='BORROW')=>['s',id,revision,qty,previous===null?null:JSON.stringify(previous),JSON.stringify(next),action,'five-slot-stacked-v1','stacked-visibility-v4-x01'];
 const rpc=async values=>(await db.query('SELECT public.commit_inventory_observation($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9) AS result',values)).rows[0].result;
 async function rejectsUnchanged(values,pattern){const before=await row();await assert.rejects(()=>rpc(values),pattern);assert.deepEqual(await row(),before);}
 await rejectsUnchanged(args('guess',0,5,null,[0,1,1,1,1]),/matching quantity/);
 await rejectsUnchanged(args('unknown',0,5,null,[1,1,1,null,0]),/known/);
 const baselineArgs=args('baseline',0,5,null,[1,1,1,1,1]);
 const baseline=await rpc(baselineArgs);assert.equal(baseline.state.revision,1);assert.equal(baseline.state.current_quantity,5);
 const duplicate=await rpc(baselineArgs);assert.equal(duplicate.status,'duplicate');assert.equal((await row()).revision,1);
 await rejectsUnchanged(args('baseline',0,5,null,[1,1,1,1,1],'RETURN'),/Event ID reused/);
 await rejectsUnchanged(args('stale',0,5,[1,1,1,1,1],[0,1,1,1,1]),/Stale inventory/);
 // Competing queued RPCs with the same revision: only one can commit.
 const competing=await Promise.allSettled([
  rpc(args('writer-a',1,5,[1,1,1,1,1],[0,1,1,1,1])),
  rpc(args('writer-b',1,5,[1,1,1,1,1],[1,1,0,1,1]))]);
 assert.equal(competing.filter(r=>r.status==='fulfilled').length,1);
 const current=await row();assert.equal(current.revision,2);assert.equal(current.current_quantity,4);
 const removalId=competing[0].status==='fulfilled'?'writer-a':'writer-b';
 const beforeSlots=current.confirmed_occupancy;
 await rejectsUnchanged(args('wrong-action',2,4,beforeSlots,[1,1,1,1,1]),/BORROW/);
 const relocated=beforeSlots[0]===0?[1,1,0,1,1]:[0,1,1,1,1];
 await rejectsUnchanged(args('relocation',2,4,beforeSlots,relocated,'RETURN'),/RETURN/);
 await rejectsUnchanged(args('periodic-change',2,4,beforeSlots,[1,1,1,1,1],'RECHECK'),/RECHECK/);
 // Simulate a failure AFTER the quantity/slot UPDATE, when its receipt is inserted.
 await db.exec(`CREATE FUNCTION public.test_receipt_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.event_id='receipt-fails' THEN RAISE EXCEPTION 'simulated receipt failure'; END IF; RETURN NEW; END $$;
 CREATE TRIGGER receipt_failure BEFORE INSERT ON public.inventory_accepted_events FOR EACH ROW EXECUTE FUNCTION public.test_receipt_failure();`);
 await rejectsUnchanged(args('receipt-fails',2,4,beforeSlots,[1,1,1,1,1],'RETURN'),/simulated receipt failure/);
 assert.equal((await db.query("SELECT count(*)::int AS n FROM public.inventory_accepted_events WHERE event_id='receipt-fails'")).rows[0].n,0);
 const returned=await rpc(args('return',2,4,beforeSlots,[1,1,1,1,1],'RETURN'));
 assert.equal(returned.state.current_quantity,5);assert.equal(returned.state.revision,3);
 await assert.rejects(()=>db.exec("UPDATE public.compartments SET current_quantity=4 WHERE shelf_id='s'"),/inventory_confirmed_state/);
 await db.exec('SET ROLE anon');
 await assert.rejects(()=>db.exec("UPDATE public.compartments SET current_quantity=4 WHERE shelf_id='s'"),/permission denied/);
 await assert.rejects(()=>db.exec("INSERT INTO public.compartments(shelf_id,confirmed_occupancy) VALUES('forged','[1,1,1,1,1]'::jsonb)"),/permission denied/);
 await db.exec("INSERT INTO public.compartments(shelf_id) VALUES('new-legacy')");
 await assert.rejects(()=>rpc(args('browser',3,5,[1,1,1,1,1],[0,1,1,1,1])),/permission denied/);await db.exec('RESET ROLE');
 const events=(await db.query('SELECT event_id FROM public.inventory_accepted_events ORDER BY event_id')).rows.map(r=>r.event_id);
 assert.deepEqual(events.sort(),['baseline',removalId,'return'].sort());
 await db.close();
 console.log('PASS: real PostgreSQL WASM migration/idempotency, legacy preservation, atomic commissioning/removal/return, duplicate receipt, competing revisions, rollback, unknown/relocation/action holds and RPC permissions');
 console.log('LIMIT: PGlite serializes connection requests; native multi-session lock contention still needs staging verification.');
})().catch(e=>{console.error(e);process.exit(1);});
