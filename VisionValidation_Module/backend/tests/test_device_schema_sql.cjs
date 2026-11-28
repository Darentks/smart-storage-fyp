// Isolated PostgreSQL WASM only. Never connects to Supabase or uses real keys.
const assert=require('node:assert/strict'),fs=require('node:fs');
if(!process.env.PHASE1_PGLITE_MODULE){console.log('SKIP: device schema requires test-only PHASE1_PGLITE_MODULE');process.exit(0);}
const {PGlite}=require(process.env.PHASE1_PGLITE_MODULE);
const read=path=>fs.readFileSync(path,'utf8');
const fresh=read('supabase_schema.sql'),prepare=read('migrations/20261123_prepare_camera_shelves.sql');
const cutover=read('migrations/manual/20261123_device_admin_cutover.sql'),audit=read('migrations/audit/20261123_device_dependency_audit.sql');
const legacy=read('VisionValidation_Module/backend/tests/fixtures/legacy_supabase_schema.sql').replace(/CREATE EXTENSION IF NOT EXISTS .pgcrypto.;/,'');
async function setup(){
 const db=new PGlite();
 await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
 CREATE SCHEMA auth; GRANT USAGE ON SCHEMA auth TO anon,authenticated,service_role;
 CREATE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS $$ SELECT coalesce(nullif(current_setting('request.jwt.claims',true),''),'{}')::jsonb $$;
 CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;`);
 return db;
}
async function rejects(db,sql,pattern){await assert.rejects(()=>db.exec(sql),pattern);await db.exec('ROLLBACK; RESET ROLE;');}
async function role(db,name,claims={}){
 await db.exec('RESET ROLE;');await db.query("SELECT set_config('request.jwt.claims',$1,false)",[JSON.stringify(claims)]);
 await db.exec('SET ROLE '+name);
}
const admin={app_metadata:{camera_admin:true}},ordinary={user_metadata:{camera_admin:true}};
async function snapshots(db){
 await db.exec('RESET ROLE;');const result={};
 for(const table of ['compartments','esp32_cameras','cctv_cameras','inventory_accepted_events','inventory_operators','inventory_reconciliations'])
  result[table]=(await db.query(`SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) AS value FROM public.${table} t`)).rows[0].value;
 return result;
}
async function freshCase(){
 const db=await setup();try{
  await db.exec(fresh);
  assert.deepEqual((await db.query("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).rows.map(r=>r.tablename),['cctv_cameras','compartments','esp32_cameras']);
  assert.equal((await db.query('SELECT count(*)::int AS n FROM compartments')).rows[0].n,6);
  assert.equal((await db.query('SELECT count(*)::int AS n FROM esp32_cameras')).rows[0].n,0);
  assert.equal((await db.query("SELECT has_table_privilege('authenticated','esp32_cameras','TRUNCATE') AS allowed")).rows[0].allowed,false,'authenticated API users cannot bypass RLS using TRUNCATE');
  await rejects(db,fresh,/FRESH INSTALL ONLY/);
  await role(db,'anon');await rejects(db,'SELECT * FROM esp32_cameras',/permission denied/);
  await role(db,'authenticated',ordinary);
  assert.equal((await db.query('SELECT * FROM esp32_cameras')).rows.length,0);
  await db.exec("INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('ordinary','Ordinary','SHELF_R1_C1',1); UPDATE esp32_cameras SET camera_name='Updated' WHERE device_id='ordinary'; DELETE FROM esp32_cameras WHERE device_id='ordinary';");
  await role(db,'authenticated',admin);
  await db.exec("INSERT INTO esp32_cameras(device_id,camera_name,rack_id,shelf_id,angle_index,ip_address) VALUES('back','Back','RACK_01','SHELF_R1_C1',1,'192.0.2.1'); INSERT INTO cctv_cameras(cctv_id,camera_name,rtsp_url,camera_role) VALUES('CCTV_03','Side','rtsp://fixture.invalid/stream','SIDE_PRESENCE');");
  await db.exec("UPDATE esp32_cameras SET ip_address='192.0.2.2' WHERE device_id='back'");
  assert.equal((await db.query("SELECT host(ip_address) AS ip FROM esp32_cameras WHERE device_id='back'")).rows[0].ip,'192.0.2.2');
  await rejects(db,"INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('orphan','Orphan','missing',2)",/foreign key/);
  await role(db,'authenticated',admin);
  await rejects(db,"INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('bad','Bad','SHELF_R1_C2',3)",/chk_esp32_angle/);
  await role(db,'authenticated',admin);
  await rejects(db,"INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('duplicate','Duplicate','SHELF_R1_C1',1)",/uq_esp32_camera_shelf_angle/);
  await role(db,'authenticated',admin);await db.exec("DELETE FROM esp32_cameras WHERE device_id='back'; DELETE FROM cctv_cameras;");
  assert.equal((await db.query('SELECT * FROM esp32_cameras')).rows.length,0);
  await role(db,'service_role');assert.equal((await db.query('SELECT * FROM compartments')).rows.length,6);
 }finally{await db.close();}
 console.log('PASS fresh: only three device tables, stable keys, no invented cameras, authenticated CRUD without admin claim, checks/FK and anonymous denial');
}
async function legacyCase(){
 const db=await setup();try{
  await db.exec(legacy);
  await db.exec(`INSERT INTO compartments(shelf_id,rack_id,current_quantity,max_capacity) VALUES('EXTERNAL_SHELF','EXTERNAL_RACK',6,6);
   INSERT INTO esp32_cameras(device_id,camera_name,rack_id,shelf_id,angle_index) VALUES('external','External','EXTERNAL_RACK','EXTERNAL_SHELF',1);
   ALTER TABLE esp32_cameras ADD COLUMN vendor_note text; UPDATE esp32_cameras SET vendor_note='preserved';
   INSERT INTO inventory_accepted_events(shelf_id,event_id,request,result) VALUES('EXTERNAL_SHELF','old-event','{}','{}');
   INSERT INTO inventory_operators(user_id) VALUES('00000000-0000-4000-8000-000000000001');
   INSERT INTO inventory_reconciliations(reconciliation_id,shelf_id,operator_id,reason,before_state,after_state,request)
    VALUES('00000000-0000-4000-8000-000000000002','EXTERNAL_SHELF','00000000-0000-4000-8000-000000000001','Historical fixture','{}','{}','{}');
   CREATE SCHEMA reporting; CREATE VIEW reporting.stock AS SELECT shelf_id,current_quantity FROM compartments;
   CREATE TABLE reporting.external_use(shelf_id varchar(50) REFERENCES compartments(shelf_id));
   GRANT UPDATE(camera_name) ON esp32_cameras TO anon;
   CREATE ROLE external_reader; GRANT USAGE ON SCHEMA public,auth TO external_reader; GRANT SELECT ON esp32_cameras TO external_reader;
   CREATE POLICY extra_shared_policy ON esp32_cameras FOR ALL TO authenticated USING(true) WITH CHECK(true);`);
  const before=await snapshots(db);
  const grants=await db.query("SELECT table_name,grantee,privilege_type FROM information_schema.table_privileges WHERE table_schema='public' AND table_name IN ('esp32_cameras','cctv_cameras') ORDER BY 1,2,3");
  const policies=await db.query("SELECT tablename,policyname,qual,with_check FROM pg_policies WHERE tablename IN ('esp32_cameras','cctv_cameras') ORDER BY 1,2");
  await rejects(db,cutover,/explicit reviewed operator approval/);
  await db.exec(prepare);await db.exec(prepare);
  assert.deepEqual(await snapshots(db),before);
  assert.deepEqual((await db.query("SELECT table_name,grantee,privilege_type FROM information_schema.table_privileges WHERE table_schema='public' AND table_name IN ('esp32_cameras','cctv_cameras') ORDER BY 1,2,3")).rows,grants.rows);
  assert.deepEqual((await db.query("SELECT tablename,policyname,qual,with_check FROM pg_policies WHERE tablename IN ('esp32_cameras','cctv_cameras') ORDER BY 1,2")).rows,policies.rows);
  const fks=(await db.query("SELECT conname,convalidated FROM pg_constraint WHERE conrelid='esp32_cameras'::regclass AND contype='f'")).rows;
  assert.equal(fks.length,2);assert.ok(fks.every(row=>row.convalidated));
  const catalog=await db.exec(audit);assert.ok(catalog.some(result=>result.rows?.some(row=>String(row.dependent).includes('reporting.stock'))));
  await role(db,'anon');await db.exec("INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('temporary','Temporary','EXTERNAL_SHELF',2); DELETE FROM esp32_cameras WHERE device_id='temporary';");
  assert.deepEqual(await snapshots(db),before,'old app CRUD still works after preparation');
  await db.exec("SET smart_storage.approve_device_admin_cutover='approved-after-staging';");
  await db.exec(cutover);await db.exec(cutover);
  assert.deepEqual(await snapshots(db),before,'manual fixture cutover deletes no records/history');
  assert.equal((await db.query("SELECT count(*)::int AS n FROM pg_constraint WHERE conrelid='esp32_cameras'::regclass AND contype='f'")).rows[0].n,1);
  assert.equal((await db.query("SELECT has_column_privilege('anon','esp32_cameras','camera_name','UPDATE') AS allowed")).rows[0].allowed,false);
  await role(db,'anon');await rejects(db,'SELECT * FROM cctv_cameras',/permission denied/);
  await role(db,'authenticated',ordinary);assert.equal((await db.query('SELECT * FROM esp32_cameras')).rows.length,0,'unknown permissive policy cannot bypass restrictive admin guard');
  await rejects(db,"INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('unauthorized','Denied','EXTERNAL_SHELF',2)",/row-level security/);
  await role(db,'external_reader');assert.equal((await db.query('SELECT * FROM esp32_cameras')).rows.length,0,'custom role cannot bypass admin restriction through an old PUBLIC policy');
  await role(db,'authenticated',admin);await db.exec("UPDATE esp32_cameras SET camera_name='Admin update' WHERE device_id='external'");
  assert.equal((await db.query("SELECT camera_name FROM esp32_cameras WHERE device_id='external'")).rows[0].camera_name,'Admin update');
  await rejects(db,"DELETE FROM camera_shelves WHERE shelf_id='EXTERNAL_SHELF'",/permission denied/);
  await role(db,'service_role');await rejects(db,"DELETE FROM camera_shelves WHERE shelf_id='EXTERNAL_SHELF'",/foreign key/);
  await db.exec('RESET ROLE;');assert.equal((await db.query('SELECT * FROM reporting.stock')).rows.length,7);
 }finally{await db.close();}
 console.log('PASS legacy: rows/history/extra columns and external view/FK preserved; prep rerun; dual validated FK; unchanged grants; guarded cutover; restrictive RLS/column-ACL denial');
}
async function failureCases(){
 for(const kind of ['orphan','blank','collision','wrong_constraint']){
  const db=await setup();try{
   await db.exec(legacy);
   if(kind==='orphan')await db.exec("ALTER TABLE esp32_cameras DROP CONSTRAINT fk_esp32_camera_compartment; UPDATE esp32_cameras SET shelf_id='missing' WHERE device_id='ESP32_R1_C1_CAM1';");
   if(kind==='blank')await db.exec("INSERT INTO compartments(shelf_id) VALUES(' ')");
   if(kind==='collision')await db.exec('CREATE TABLE camera_shelves(secret text)');
   if(kind==='wrong_constraint')await db.exec('ALTER TABLE esp32_cameras ADD CONSTRAINT fk_esp32_camera_shelf_identity CHECK(angle_index=1 OR angle_index=2)');
   const before=await snapshots(db);
   await rejects(db,prepare,kind==='orphan'?/Orphan or missing/:kind==='blank'?/Invalid legacy/:kind==='collision'?/name collision/:/Conflicting fk_/);
   assert.deepEqual(await snapshots(db),before);
   if(kind!=='collision')assert.equal((await db.query("SELECT to_regclass('camera_shelves') AS object")).rows[0].object,null,'transaction rolls back partial preparation');
  }finally{await db.close();}
 }
 const db=await setup();try{await db.exec('CREATE TABLE unrelated_application(id int)');await rejects(db,fresh,/FRESH INSTALL ONLY/);}finally{await db.close();}
 const missing=await setup();try{
  await missing.exec("CREATE TABLE esp32_cameras(id int,shelf_id text); INSERT INTO esp32_cameras VALUES(1,'kept'); CREATE TABLE cctv_cameras(id int)");
  await rejects(missing,prepare,/Preparation requires existing/);
  assert.equal((await missing.query('SELECT shelf_id FROM esp32_cameras')).rows[0].shelf_id,'kept');
 }finally{await missing.close();}
 const extra=await setup();try{
  await extra.exec(legacy);await extra.exec(prepare);
  await extra.exec("ALTER TABLE esp32_cameras ADD CONSTRAINT unexpected_old_parent_fk FOREIGN KEY(shelf_id) REFERENCES compartments(shelf_id); SET smart_storage.approve_device_admin_cutover='approved-after-staging'");
  const before=await snapshots(extra);await rejects(extra,cutover,/Additional or unexpected legacy device FK/);
  assert.deepEqual(await snapshots(extra),before);
  assert.equal((await extra.query("SELECT count(*)::int AS n FROM pg_constraint WHERE conrelid='esp32_cameras'::regclass AND contype='f'")).rows[0].n,3);
 }finally{await extra.close();}
 console.log('PASS failures: orphan/blank/name collision/constraint conflict rollback; fresh install refuses existing shared-app tables');
}
(async()=>{await freshCase();await legacyCase();await failureCases();})().catch(error=>{console.error(error);process.exitCode=1;});
