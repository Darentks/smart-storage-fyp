// Isolated PostgreSQL only; never connects to Supabase.
const fs=require('node:fs'),assert=require('node:assert/strict');
if(!process.env.PHASE1_PGLITE_MODULE){console.log('SKIP: shelf consolidation needs test-only PGlite');process.exit(0);}
const {PGlite}=require(process.env.PHASE1_PGLITE_MODULE),read=p=>fs.readFileSync(p,'utf8');
const migration=read('migrations/20261124_consolidate_shelf_identity.sql');
async function setup(){const db=new PGlite();await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');return db;}
async function state(db){return {
 rows:(await db.query(`SELECT 'compartments' AS t,to_jsonb(c) AS row FROM compartments c UNION ALL SELECT 'esp32',to_jsonb(e) FROM esp32_cameras e UNION ALL SELECT 'cctv',to_jsonb(v) FROM cctv_cameras v ORDER BY 1,2`)).rows,
 columns:(await db.query(`SELECT attrelid,attname,atttypid,atttypmod,attnotnull FROM pg_attribute WHERE attrelid=ANY(ARRAY['compartments'::regclass,'esp32_cameras'::regclass,'cctv_cameras'::regclass]) AND attnum>0 AND NOT attisdropped ORDER BY 1,attnum`)).rows,
 access:(await db.query(`SELECT oid,relacl,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=ANY(ARRAY['compartments'::regclass,'esp32_cameras'::regclass,'cctv_cameras'::regclass]) ORDER BY oid`)).rows,
 indexes:(await db.query(`SELECT indexrelid,pg_get_indexdef(indexrelid) FROM pg_index WHERE indrelid=ANY(ARRAY['compartments'::regclass,'esp32_cameras'::regclass,'cctv_cameras'::regclass]) ORDER BY indexrelid`)).rows
};}
async function deployed(){const db=await setup();await db.exec(read('VisionValidation_Module/backend/tests/fixtures/device_schema_before_shelf_consolidation.sql'));
 await db.exec(`CREATE TABLE compartments(shelf_id varchar(50) PRIMARY KEY, current_quantity int DEFAULT 4, vendor_note text);
 INSERT INTO compartments SELECT shelf_id,4,'preserved' FROM camera_shelves;
 ALTER TABLE compartments ENABLE ROW LEVEL SECURITY;
 INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('back','Back','SHELF_R1_C1',1);
 INSERT INTO cctv_cameras(cctv_id,camera_name,rtsp_url) VALUES('CCTV_01','CCTV','rtsp://fixture.invalid');`);return db;}
(async()=>{
 let db=await setup();try{await db.exec(read('supabase_schema.sql'));assert.equal((await db.query('SELECT count(*)::int n FROM compartments')).rows[0].n,6);assert.equal((await db.query("SELECT to_regclass('camera_shelves') t")).rows[0].t,null);
 assert.equal((await db.query("SELECT has_table_privilege('anon','compartments','SELECT') p")).rows[0].p,false);
 await db.exec(migration); // Fresh bootstrap and migration converge.
 }finally{await db.close();}
 db=await deployed();try{const before=await state(db);await db.exec(migration);await db.exec(migration);assert.deepEqual(await state(db),before);
 assert.equal((await db.query("SELECT to_regclass('camera_shelves') t")).rows[0].t,null);
 assert.equal((await db.query("SELECT confrelid::regclass::text parent FROM pg_constraint WHERE conrelid='esp32_cameras'::regclass AND contype='f'")).rows[0].parent,'compartments');
 await db.exec('SET ROLE authenticated');await db.exec("UPDATE esp32_cameras SET camera_name='Updated'; INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('front','Front','SHELF_R1_C1',2); DELETE FROM esp32_cameras WHERE device_id='front';");
 await assert.rejects(()=>db.exec("UPDATE esp32_cameras SET shelf_id='missing'"),/foreign key/);await db.exec('RESET ROLE');
 await assert.rejects(()=>db.exec("DELETE FROM compartments WHERE shelf_id='SHELF_R1_C1'"),/foreign key/);
 }finally{await db.close();}
 for(const failure of ['missing','dependency','unique','extra_column','orphan']){
  db=await deployed();try{
   if(failure==='missing')await db.exec("DELETE FROM compartments WHERE shelf_id='SHELF_R1_C6'");
   if(failure==='dependency')await db.exec('CREATE VIEW external_registry_view AS SELECT * FROM camera_shelves');
   if(failure==='unique')await db.exec('ALTER TABLE compartments DROP CONSTRAINT compartments_pkey');
   if(failure==='extra_column')await db.exec('ALTER TABLE camera_shelves ADD COLUMN unexpected text');
   if(failure==='orphan')await db.exec('ALTER TABLE esp32_cameras DROP CONSTRAINT fk_esp32_camera_shelf_identity; UPDATE esp32_cameras SET shelf_id=\'missing\'');
   const before=await state(db);await assert.rejects(()=>db.exec(migration));await db.exec('ROLLBACK');assert.deepEqual(await state(db),before);
   assert.equal((await db.query("SELECT count(*)::int n FROM camera_shelves")).rows[0].n,6);
  }finally{await db.close();}
 }
 console.log('PASS shelf consolidation: fresh schema, existing data/columns/ACL/RLS/index preservation, repeated migration, authenticated CRUD/FK, rollback on missing IDs/dependencies/unique key/extra data/orphans');
})().catch(e=>{console.error(e);process.exitCode=1;});
