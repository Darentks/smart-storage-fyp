-- FRESH INSTALL ONLY -- NEVER APPLY TO AN EXISTING DB.
-- Desired device-admin schema. No inventory, ROI, intent, receipt or audit storage.
-- Existing databases: use migrations/20261124_consolidate_shelf_identity.sql; do not run this bootstrap.
-- Access is controlled by the existing table grants and authenticated application
-- sessions or a protected server service role. No RLS/claim authorization is used.
-- No camera IPs, credentials, placeholder RTSP sources or public CRUD are seeded.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m'))
    OR to_regclass('public.camera_shelves') IS NOT NULL
    OR to_regclass('public.esp32_cameras') IS NOT NULL
    OR to_regclass('public.cctv_cameras') IS NOT NULL
    OR to_regclass('public.compartments') IS NOT NULL
    OR to_regclass('public.inventory_accepted_events') IS NOT NULL
    OR to_regclass('public.inventory_operators') IS NOT NULL
    OR to_regclass('public.inventory_reconciliations') IS NOT NULL THEN
    RAISE EXCEPTION 'FRESH INSTALL ONLY: existing Smart Storage objects found; use the reviewed existing-database migration';
  END IF;
END $$;

CREATE TABLE public.compartments (
  shelf_id varchar(50) PRIMARY KEY,
  CONSTRAINT compartments_key_valid CHECK (length(shelf_id)>0 AND shelf_id=btrim(shelf_id))
);
COMMENT ON TABLE public.compartments IS 'Smart Storage shelf identity registry; no inventory';
CREATE TABLE public.esp32_cameras (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id varchar(100) NOT NULL UNIQUE CHECK (length(btrim(device_id))>0),
  camera_name varchar(100) NOT NULL CHECK (length(btrim(camera_name))>0),
  rack_id varchar(50) NOT NULL DEFAULT 'RACK_01',
  shelf_id varchar(50) NOT NULL,
  angle_index smallint NOT NULL,
  ip_address inet,
  CONSTRAINT fk_esp32_camera_shelf_identity FOREIGN KEY (shelf_id)
    REFERENCES public.compartments(shelf_id) ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT uq_esp32_camera_shelf_angle UNIQUE (shelf_id,angle_index),
  CONSTRAINT chk_esp32_angle CHECK (angle_index IN (1,2))
);
CREATE TABLE public.cctv_cameras (
  cctv_id varchar(50) PRIMARY KEY CHECK (length(btrim(cctv_id))>0),
  camera_name varchar(100) NOT NULL CHECK (length(btrim(camera_name))>0),
  rack_id varchar(50) NOT NULL DEFAULT 'RACK_01',
  ip_address inet,
  rtsp_url text NOT NULL CHECK (length(btrim(rtsp_url))>0),
  camera_role varchar(30) NOT NULL DEFAULT 'GRID_6_ROI',
  is_active boolean NOT NULL DEFAULT true,
  CONSTRAINT chk_cctv_camera_role CHECK (camera_role IN ('GRID_6_ROI','SIDE_PRESENCE'))
);
-- PK/UNIQUE indexes cover device-id/shelf/angle/CCTV-id queries. No redundant
-- small-table boolean/rack indexes. Shelf identity is configuration, not stock.
INSERT INTO public.compartments(shelf_id) VALUES
  ('SHELF_R1_C1'),('SHELF_R1_C2'),('SHELF_R1_C3'),
  ('SHELF_R1_C4'),('SHELF_R1_C5'),('SHELF_R1_C6');
ALTER TABLE public.compartments DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.esp32_cameras DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.cctv_cameras DISABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.compartments,public.esp32_cameras,public.cctv_cameras FROM PUBLIC,anon,authenticated;
GRANT USAGE ON SCHEMA public TO authenticated,service_role;
GRANT SELECT ON public.compartments TO authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.esp32_cameras,public.cctv_cameras TO authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.compartments,public.esp32_cameras,public.cctv_cameras TO service_role;
COMMIT;
