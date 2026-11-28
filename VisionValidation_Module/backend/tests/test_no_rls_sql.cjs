const assert=require('node:assert/strict'),fs=require('node:fs');
if(!process.env.PHASE1_PGLITE_MODULE){console.log('SKIP: no-RLS suite needs test-only PGlite');process.exit(0);}
const {PGlite}=require(process.env.PHASE1_PGLITE_MODULE);
const fresh=fs.readFileSync('VisionValidation_Module/backend/tests/fixtures/device_schema_before_shelf_consolidation.sql','utf8'),migration=fs.readFileSync('migrations/20261124_disable_camera_rls.sql','utf8');
const tables=['camera_shelves','esp32_cameras','cctv_cameras'];
async function snapshot(db){
 const data={};for(const t of tables)data[t]=(await db.query(`SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) AS rows FROM ${t} t`)).rows;
 const structures=(await db.query(`SELECT c.oid,c.relname,c.relacl,a.attname,a.atttypid,a.atttypmod,a.attnotnull,a.attacl FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid WHERE c.relname=ANY($1) AND a.attnum>0 ORDER BY c.oid,a.attnum`,[tables])).rows;
 const constraints=(await db.query(`SELECT oid,conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid=ANY(ARRAY['camera_shelves'::regclass,'esp32_cameras'::regclass,'cctv_cameras'::regclass]) ORDER BY oid`)).rows;
 const indexes=(await db.query(`SELECT indexrelid,pg_get_indexdef(indexrelid) AS definition FROM pg_index WHERE indrelid=ANY(ARRAY['camera_shelves'::regclass,'esp32_cameras'::regclass,'cctv_cameras'::regclass]) ORDER BY indexrelid`)).rows;
 return {data,structures,constraints,indexes};
}
async function state(db){return (await db.query(`SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=ANY($1) ORDER BY relname`,[tables])).rows;}
(async()=>{
 const db=new PGlite();try{
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  await db.exec(fresh); // No Auth schema or auth.jwt() exists.
  assert.ok((await state(db)).every(r=>!r.relrowsecurity));
  for(const t of tables)assert.equal((await db.query("SELECT has_table_privilege('anon',$1,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS allowed",[t])).rows[0].allowed,false);
  await db.exec(`INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('kept','Kept','SHELF_R1_C1',1);
   INSERT INTO cctv_cameras(cctv_id,camera_name,rtsp_url) VALUES('CCTV_01','Kept CCTV','rtsp://fixture.invalid');`);
  for(const t of tables)await db.exec(`ALTER TABLE ${t} ENABLE ROW LEVEL SECURITY; ALTER TABLE ${t} FORCE ROW LEVEL SECURITY;
   CREATE POLICY legacy_policy ON ${t} FOR ALL TO authenticated USING(false) WITH CHECK(false);`);
  const before=await snapshot(db);await db.exec(migration);await db.exec(migration);
  assert.deepEqual(await snapshot(db),before,'rows, ACLs, columns, constraints, indexes and OIDs unchanged');
  assert.ok((await state(db)).every(r=>!r.relrowsecurity&&!r.relforcerowsecurity));
  assert.equal((await db.query(`SELECT count(*)::int AS n FROM pg_policy WHERE polrelid=ANY(ARRAY['camera_shelves'::regclass,'esp32_cameras'::regclass,'cctv_cameras'::regclass])`)).rows[0].n,0);
  await db.exec('SET ROLE authenticated');
  assert.equal((await db.query('SELECT * FROM camera_shelves')).rows.length,6);
  await db.exec(`UPDATE esp32_cameras SET camera_name='Authorized' WHERE device_id='kept';
   INSERT INTO esp32_cameras(device_id,camera_name,shelf_id,angle_index) VALUES('temporary','Temporary','SHELF_R1_C1',2);
   DELETE FROM esp32_cameras WHERE device_id='temporary'; UPDATE cctv_cameras SET camera_name='Authorized';`);
  await db.exec('RESET ROLE; SET ROLE anon;');
  for(const t of tables)await assert.rejects(()=>db.query('SELECT * FROM '+t),/permission denied/);
  await db.exec('RESET ROLE;');
  // Unsafe deployed grants must cause a complete rollback, not silent permission changes.
  await db.exec('ALTER TABLE camera_shelves ENABLE ROW LEVEL SECURITY; CREATE POLICY keep_on_failure ON camera_shelves USING(false); GRANT UPDATE(camera_name) ON cctv_cameras TO anon;');
  const unsafe=await snapshot(db),oldState=await state(db);
  await assert.rejects(()=>db.exec(migration),/Anonymous grants/);await db.exec('ROLLBACK');
  assert.deepEqual(await snapshot(db),unsafe);assert.deepEqual(await state(db),oldState);
  assert.equal((await db.query("SELECT count(*)::int AS n FROM pg_policy WHERE polname='keep_on_failure'")).rows[0].n,1);
  await db.exec('REVOKE UPDATE(camera_name) ON cctv_cameras FROM anon; GRANT SELECT ON esp32_cameras TO PUBLIC;');
  const publicGrants=await snapshot(db);await assert.rejects(()=>db.exec(migration),/Anonymous grants/);await db.exec('ROLLBACK');
  assert.deepEqual(await snapshot(db),publicGrants,'inherited PUBLIC rights are detected without rewriting grants');
  console.log('PASS no RLS: no Auth dependency; drop policies/force flags; idempotence; identical records/structures/grants; authenticated CRUD; anonymous denial and unsafe-column-ACL rollback');
 }finally{await db.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
