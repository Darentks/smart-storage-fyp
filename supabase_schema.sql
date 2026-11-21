-- Fresh installation / deliberate reset ONLY: this script deletes existing data.
-- Existing databases: apply migrations/20261121_persistent_inventory.sql instead.
-- Includes Phase 1 durable inventory and server-only atomic commit RPC.
BEGIN;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

DROP TABLE IF EXISTS public.inventory_reconciliations CASCADE;
DROP TABLE IF EXISTS public.inventory_operators CASCADE;
DROP TABLE IF EXISTS public.inventory_accepted_events CASCADE;
DROP TABLE IF EXISTS public.esp32_reference_samples CASCADE;
DROP TABLE IF EXISTS public.comparison_logs CASCADE;
DROP TABLE IF EXISTS public.esp32_cameras CASCADE;
DROP TABLE IF EXISTS public.cctv_cameras CASCADE;
DROP TABLE IF EXISTS public.compartments CASCADE;

DROP FUNCTION IF EXISTS public.strip_base64_compartments() CASCADE;
DROP FUNCTION IF EXISTS public.strip_base64_comparison_logs() CASCADE;
DROP FUNCTION IF EXISTS public.set_updated_at() CASCADE;

CREATE TABLE public.compartments (
    shelf_id         VARCHAR(50) PRIMARY KEY,
    rack_id          VARCHAR(50) NOT NULL DEFAULT 'RACK_01',
    current_quantity INTEGER NOT NULL DEFAULT 5,
    max_capacity     INTEGER NOT NULL DEFAULT 5,
    tm_model_url     TEXT DEFAULT 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/',
    CONSTRAINT chk_compartment_quantity_nonnegative CHECK (current_quantity >= 0),
    CONSTRAINT chk_compartment_capacity_positive CHECK (max_capacity > 0),
    CONSTRAINT chk_compartment_quantity_within_capacity CHECK (current_quantity <= max_capacity)
);

CREATE INDEX idx_compartments_rack_id ON public.compartments (rack_id);

CREATE TABLE public.esp32_cameras (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id   VARCHAR(100) NOT NULL UNIQUE,
    camera_name VARCHAR(100) NOT NULL,
    rack_id     VARCHAR(50) NOT NULL DEFAULT 'RACK_01',
    shelf_id    VARCHAR(50) NOT NULL,
    angle_index SMALLINT NOT NULL,
    ip_address  INET,
    CONSTRAINT fk_esp32_camera_compartment
        FOREIGN KEY (shelf_id) REFERENCES public.compartments (shelf_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT uq_esp32_camera_shelf_angle UNIQUE (shelf_id, angle_index),
    CONSTRAINT chk_esp32_angle CHECK (angle_index IN (1, 2))
);

CREATE INDEX idx_esp32_cameras_shelf_id ON public.esp32_cameras (shelf_id);
CREATE INDEX idx_esp32_cameras_rack_id ON public.esp32_cameras (rack_id);

CREATE TABLE public.cctv_cameras (
    cctv_id     VARCHAR(50) PRIMARY KEY,
    camera_name VARCHAR(100) NOT NULL,
    rack_id     VARCHAR(50) NOT NULL DEFAULT 'RACK_01',
    ip_address  INET,
    rtsp_url    TEXT NOT NULL DEFAULT 'rtsp://admin:Password@192.168.0.101:554/Streaming/Channels/102',
    camera_role VARCHAR(30) NOT NULL DEFAULT 'GRID_6_ROI',
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT chk_cctv_camera_role CHECK (camera_role IN ('GRID_6_ROI', 'SIDE_PRESENCE'))
);

CREATE INDEX idx_cctv_cameras_rack_id ON public.cctv_cameras (rack_id);
CREATE INDEX idx_cctv_cameras_is_active ON public.cctv_cameras (is_active);

ALTER TABLE public.compartments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.esp32_cameras ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cctv_cameras ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow public access on compartments" ON public.compartments FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow public access on esp32_cameras" ON public.esp32_cameras FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow public access on cctv_cameras" ON public.cctv_cameras FOR ALL USING (true) WITH CHECK (true);

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.compartments, public.esp32_cameras, public.cctv_cameras TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

INSERT INTO public.compartments (shelf_id, rack_id, current_quantity, max_capacity, tm_model_url) VALUES
    ('SHELF_R1_C1', 'RACK_01', 5, 5, 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/'),
    ('SHELF_R1_C2', 'RACK_01', 5, 5, 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/'),
    ('SHELF_R1_C3', 'RACK_01', 5, 5, 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/'),
    ('SHELF_R1_C4', 'RACK_01', 5, 5, 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/'),
    ('SHELF_R1_C5', 'RACK_01', 5, 5, 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/'),
    ('SHELF_R1_C6', 'RACK_01', 5, 5, 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/')
ON CONFLICT (shelf_id) DO UPDATE SET
    max_capacity = EXCLUDED.max_capacity,
    current_quantity = EXCLUDED.current_quantity,
    tm_model_url = EXCLUDED.tm_model_url;

INSERT INTO public.esp32_cameras (device_id, camera_name, rack_id, shelf_id, angle_index, ip_address) VALUES
    ('ESP32_R1_C1_CAM1', 'R1-C1 Back Angle (CAM 1)',  'RACK_01', 'SHELF_R1_C1', 1, '192.168.0.201'),
    ('ESP32_R1_C1_CAM2', 'R1-C1 Front Angle (CAM 2)', 'RACK_01', 'SHELF_R1_C1', 2, '192.168.0.202'),
    ('ESP32_R1_C2_CAM1', 'R1-C2 Back Angle (CAM 3)',  'RACK_01', 'SHELF_R1_C2', 1, '192.168.0.203'),
    ('ESP32_R1_C2_CAM2', 'R1-C2 Front Angle (CAM 4)', 'RACK_01', 'SHELF_R1_C2', 2, '192.168.0.204'),
    ('ESP32_R1_C3_CAM1', 'R1-C3 Back Angle (CAM 5)',  'RACK_01', 'SHELF_R1_C3', 1, '192.168.0.205'),
    ('ESP32_R1_C3_CAM2', 'R1-C3 Front Angle (CAM 6)', 'RACK_01', 'SHELF_R1_C3', 2, '192.168.0.206'),
    ('ESP32_R1_C4_CAM1', 'R1-C4 Back Angle (CAM 7)',  'RACK_01', 'SHELF_R1_C4', 1, '192.168.0.207'),
    ('ESP32_R1_C4_CAM2', 'R1-C4 Front Angle (CAM 8)', 'RACK_01', 'SHELF_R1_C4', 2, '192.168.0.208'),
    ('ESP32_R1_C5_CAM1', 'R1-C5 Back Angle (CAM 9)',  'RACK_01', 'SHELF_R1_C5', 1, '192.168.0.209'),
    ('ESP32_R1_C5_CAM2', 'R1-C5 Front Angle (CAM 10)', 'RACK_01', 'SHELF_R1_C5', 2, '192.168.0.210'),
    ('ESP32_R1_C6_CAM1', 'R1-C6 Back Angle (CAM 11)', 'RACK_01', 'SHELF_R1_C6', 1, '192.168.0.211'),
    ('ESP32_R1_C6_CAM2', 'R1-C6 Front Angle (CAM 12)', 'RACK_01', 'SHELF_R1_C6', 2, '192.168.0.212')
ON CONFLICT (device_id) DO NOTHING;

INSERT INTO public.cctv_cameras (cctv_id, camera_name, rack_id, ip_address, rtsp_url, camera_role, is_active) VALUES
    ('CCTV_01', 'Hikvision CCTV 1 (Left-Oblique 6-ROI Angle)', 'RACK_01', '192.168.0.101', 'rtsp://admin:Password@192.168.0.101:554/Streaming/Channels/102', 'GRID_6_ROI', TRUE),
    ('CCTV_02', 'Hikvision CCTV 2 (Center-Top 6-ROI Angle)', 'RACK_01', '192.168.0.102', 'rtsp://admin:Password@192.168.0.102:554/Streaming/Channels/102', 'GRID_6_ROI', TRUE),
    ('CCTV_03', 'Hikvision CCTV 3 (Side Presence Angle)', 'RACK_01', '192.168.0.103', 'rtsp://admin:Password@192.168.0.103:554/Streaming/Channels/102', 'SIDE_PRESENCE', TRUE)
ON CONFLICT (cctv_id) DO UPDATE SET
    camera_name = EXCLUDED.camera_name,
    camera_role = EXCLUDED.camera_role;


-- Phase 1 persistent inventory (kept in sync with the standalone migration).
ALTER TABLE public.compartments
  ADD COLUMN IF NOT EXISTS confirmed_occupancy jsonb,
  ADD COLUMN IF NOT EXISTS last_verified_at timestamptz,
  ADD COLUMN IF NOT EXISTS layout_version text,
  ADD COLUMN IF NOT EXISTS evidence_version text,
  ADD COLUMN IF NOT EXISTS revision bigint NOT NULL DEFAULT 0;

CREATE OR REPLACE FUNCTION public.inventory_slots_total(slots jsonb)
RETURNS integer LANGUAGE plpgsql IMMUTABLE SET search_path = '' AS $$
DECLARE n integer := 0; bit jsonb;
BEGIN
  IF slots IS NULL OR jsonb_typeof(slots) <> 'array' THEN RETURN NULL; END IF;
  IF jsonb_array_length(slots) <> 5 THEN RETURN NULL; END IF;
  FOR bit IN SELECT value FROM jsonb_array_elements(slots) LOOP
    IF bit NOT IN ('0'::jsonb, '1'::jsonb) THEN RETURN NULL; END IF;
    n := n + (bit::text)::integer;
  END LOOP;
  IF (slots->>2)::integer > (slots->>1)::integer
     OR (slots->>4)::integer > (slots->>3)::integer THEN RETURN NULL; END IF;
  RETURN n;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.compartments'::regclass AND conname='inventory_confirmed_state') THEN
    ALTER TABLE public.compartments ADD CONSTRAINT inventory_confirmed_state CHECK (
      revision >= 0 AND (
        (confirmed_occupancy IS NULL AND last_verified_at IS NULL AND layout_version IS NULL AND evidence_version IS NULL)
        OR (confirmed_occupancy IS NOT NULL AND public.inventory_slots_total(confirmed_occupancy) IS NOT NULL
          AND public.inventory_slots_total(confirmed_occupancy) = current_quantity
          AND last_verified_at IS NOT NULL AND layout_version IS NOT NULL AND evidence_version IS NOT NULL)
      )
    );
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.inventory_accepted_events (
  shelf_id text NOT NULL REFERENCES public.compartments(shelf_id),
  event_id text NOT NULL,
  request jsonb NOT NULL,
  result jsonb NOT NULL,
  accepted_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY (shelf_id, event_id)
);
ALTER TABLE public.inventory_accepted_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.inventory_accepted_events FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.inventory_accepted_events TO service_role;

CREATE OR REPLACE FUNCTION public.commit_inventory_observation(
  p_shelf_id text, p_event_id text, p_expected_revision bigint,
  p_expected_quantity integer, p_expected_occupancy jsonb, p_occupancy jsonb,
  p_action text, p_layout_version text, p_evidence_version text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  current_row public.compartments%ROWTYPE;
  prior_event public.inventory_accepted_events%ROWTYPE;
  request_data jsonb;
  receipt jsonb;
  next_quantity integer;
  i integer;
BEGIN
  IF p_event_id IS NULL OR length(btrim(p_event_id)) NOT BETWEEN 1 AND 200 THEN
    RAISE EXCEPTION 'Invalid inventory event ID';
  END IF;
  IF p_layout_version IS DISTINCT FROM 'five-slot-stacked-v1'
    OR p_evidence_version IS DISTINCT FROM 'stacked-visibility-v4-x01' THEN
    RAISE EXCEPTION 'Unsupported layout/evidence version';
  END IF;
  next_quantity := public.inventory_slots_total(p_occupancy);
  IF next_quantity IS NULL THEN RAISE EXCEPTION 'All five slots must be known and supported'; END IF;
  IF p_action IS NULL OR p_action NOT IN ('BORROW','RETURN','RECHECK') THEN
    RAISE EXCEPTION 'Invalid expected action';
  END IF;
  request_data := jsonb_build_object('revision',p_expected_revision,'quantity',p_expected_quantity,
    'previous',p_expected_occupancy,'occupancy',p_occupancy,'action',p_action,
    'layout',p_layout_version,'evidence',p_evidence_version);

  -- All operations on a shelf serialize here, including duplicate requests.
  SELECT * INTO current_row FROM public.compartments WHERE shelf_id=p_shelf_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unknown compartment'; END IF;
  SELECT * INTO prior_event FROM public.inventory_accepted_events
    WHERE shelf_id=p_shelf_id AND event_id=p_event_id;
  IF FOUND THEN
    IF prior_event.request IS DISTINCT FROM request_data THEN RAISE EXCEPTION 'Event ID reused with different request'; END IF;
    RETURN jsonb_build_object('status','duplicate','state',to_jsonb(current_row),'receipt',prior_event.result);
  END IF;
  IF p_expected_revision IS NULL OR current_row.revision IS DISTINCT FROM p_expected_revision
    OR current_row.current_quantity IS DISTINCT FROM p_expected_quantity
    OR current_row.confirmed_occupancy IS DISTINCT FROM p_expected_occupancy THEN
    RAISE EXCEPTION 'Stale inventory revision, quantity or occupancy';
  END IF;
  IF current_row.confirmed_occupancy IS NULL THEN
    -- A legacy/default quantity alone never establishes slots. Fresh validated
    -- evidence may commission a matching count without inventing a removal.
    IF next_quantity <> current_row.current_quantity THEN RAISE EXCEPTION 'Unverified baseline requires matching quantity'; END IF;
  ELSE
    IF current_row.layout_version IS DISTINCT FROM p_layout_version
      OR current_row.evidence_version IS DISTINCT FROM p_evidence_version THEN
      RAISE EXCEPTION 'Stored state requires explicit version reconciliation';
    END IF;
    FOR i IN 0..4 LOOP
      IF p_action='BORROW' AND (p_occupancy->>i)::integer > (current_row.confirmed_occupancy->>i)::integer THEN
        RAISE EXCEPTION 'BORROW cannot add or relocate boxes';
      ELSIF p_action='RETURN' AND (p_occupancy->>i)::integer < (current_row.confirmed_occupancy->>i)::integer THEN
        RAISE EXCEPTION 'RETURN cannot remove or relocate boxes';
      ELSIF p_action='RECHECK' AND p_occupancy IS DISTINCT FROM current_row.confirmed_occupancy THEN
        RAISE EXCEPTION 'RECHECK cannot authorize inventory changes';
      END IF;
    END LOOP;
  END IF;

  UPDATE public.compartments SET current_quantity=next_quantity, confirmed_occupancy=p_occupancy,
    last_verified_at=transaction_timestamp(), layout_version=p_layout_version,
    evidence_version=p_evidence_version, revision=revision+1
    WHERE shelf_id=p_shelf_id RETURNING * INTO current_row;
  receipt := to_jsonb(current_row);
  INSERT INTO public.inventory_accepted_events(shelf_id,event_id,request,result)
    VALUES(p_shelf_id,p_event_id,request_data,receipt);
  -- Any error, including receipt insertion, rolls back the complete statement.
  RETURN jsonb_build_object('status','accepted','state',receipt,'receipt',receipt);
END $$;

REVOKE ALL ON FUNCTION public.commit_inventory_observation(text,text,bigint,integer,jsonb,jsonb,text,text,text)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.commit_inventory_observation(text,text,bigint,integer,jsonb,jsonb,text,text,text)
  TO service_role;
-- Existing browser camera CRUD remains; inventory writes are server-only.
REVOKE UPDATE, INSERT ON public.compartments FROM PUBLIC, anon, authenticated;
GRANT UPDATE (rack_id,max_capacity,tm_model_url) ON public.compartments TO anon, authenticated;
GRANT INSERT (shelf_id,rack_id,current_quantity,max_capacity,tm_model_url)
  ON public.compartments TO anon, authenticated;
NOTIFY pgrst, 'reload schema';


-- Phase 4: only allowlisted authenticated operators can physically reconcile.
CREATE TABLE IF NOT EXISTS public.inventory_operators(user_id uuid PRIMARY KEY, enabled boolean NOT NULL DEFAULT true);
CREATE TABLE IF NOT EXISTS public.inventory_reconciliations(
 reconciliation_id uuid PRIMARY KEY, shelf_id text NOT NULL REFERENCES public.compartments(shelf_id),
 operator_id uuid NOT NULL, accepted_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 event_type text NOT NULL DEFAULT 'RECONCILIATION' CHECK(event_type='RECONCILIATION'),
 reason text NOT NULL, before_state jsonb NOT NULL, after_state jsonb NOT NULL, request jsonb NOT NULL);
ALTER TABLE public.inventory_operators ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inventory_reconciliations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.inventory_operators,public.inventory_reconciliations FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.inventory_operators,public.inventory_reconciliations TO service_role;
CREATE OR REPLACE FUNCTION public.operator_reconciliation_context(p_shelf_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE result jsonb;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM public.inventory_operators WHERE user_id=auth.uid() AND enabled) THEN RAISE EXCEPTION 'Operator access denied'; END IF;
 SELECT to_jsonb(c) || jsonb_build_object('last_accepted_action',
   (SELECT jsonb_build_object('event_id',e.event_id,'action',e.request->>'action','accepted_at',e.accepted_at)
    FROM public.inventory_accepted_events e WHERE e.shelf_id=p_shelf_id AND (e.result->>'revision')::bigint=c.revision ORDER BY e.accepted_at DESC LIMIT 1))
 INTO result FROM public.compartments c WHERE shelf_id=p_shelf_id;
 IF result IS NULL THEN RAISE EXCEPTION 'Unknown shelf'; END IF;
 RETURN result;
END $$;
DROP FUNCTION IF EXISTS public.reconcile_inventory(text,uuid,bigint,integer,jsonb,text);
CREATE OR REPLACE FUNCTION public.reconcile_inventory(
 p_shelf_id text,p_reconciliation_id uuid,p_expected_revision bigint,p_quantity integer,p_slots jsonb,p_reason text,p_context jsonb DEFAULT '{}'::jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE old public.compartments%ROWTYPE; updated public.compartments%ROWTYPE;
 receipt public.inventory_reconciliations%ROWTYPE; requested jsonb; operator uuid := auth.uid();
BEGIN
 IF operator IS NULL OR NOT EXISTS(SELECT 1 FROM public.inventory_operators WHERE user_id=operator AND enabled) THEN RAISE EXCEPTION 'Operator access denied'; END IF;
 IF p_reconciliation_id IS NULL OR p_reason IS NULL OR length(btrim(p_reason)) NOT BETWEEN 5 AND 500 THEN RAISE EXCEPTION 'Recount ID and meaningful reason required'; END IF;
 IF public.inventory_slots_total(p_slots) IS NULL OR p_quantity IS DISTINCT FROM public.inventory_slots_total(p_slots) THEN RAISE EXCEPTION 'Exact supported physical recount and matching quantity required'; END IF;
 requested=jsonb_build_object('shelf',p_shelf_id,'revision',p_expected_revision,'quantity',p_quantity,'slots',p_slots,'reason',p_reason,'operator',operator,'context',jsonb_build_object('run_id',p_context->>'run_id','trigger_id',p_context->>'trigger_id','observed_slots',p_context->'observed_slots','observation_unverified',true));
 SELECT * INTO old FROM public.compartments WHERE shelf_id=p_shelf_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Unknown shelf'; END IF;
 SELECT * INTO receipt FROM public.inventory_reconciliations WHERE reconciliation_id=p_reconciliation_id;
 IF FOUND THEN
  IF receipt.request IS DISTINCT FROM requested THEN RAISE EXCEPTION 'Reconciliation ID reused'; END IF;
  RETURN jsonb_build_object('status','duplicate','state',to_jsonb(old),'audit',to_jsonb(receipt));
 END IF;
 IF p_expected_revision IS NULL OR old.revision IS DISTINCT FROM p_expected_revision THEN RAISE EXCEPTION 'Stale recount revision'; END IF;
 UPDATE public.compartments SET current_quantity=p_quantity,confirmed_occupancy=p_slots,
 last_verified_at=transaction_timestamp(),layout_version='five-slot-stacked-v1',evidence_version='stacked-visibility-v4-x01',revision=revision+1
 WHERE shelf_id=p_shelf_id RETURNING * INTO updated;
 INSERT INTO public.inventory_reconciliations(reconciliation_id,shelf_id,operator_id,reason,before_state,after_state,request)
 VALUES(p_reconciliation_id,p_shelf_id,operator,p_reason,to_jsonb(old),to_jsonb(updated),requested) RETURNING * INTO receipt;
 RETURN jsonb_build_object('status','accepted','state',to_jsonb(updated),'audit',to_jsonb(receipt));
END $$;
REVOKE ALL ON FUNCTION public.operator_reconciliation_context(text) FROM PUBLIC,anon;
REVOKE ALL ON FUNCTION public.reconcile_inventory(text,uuid,bigint,integer,jsonb,text,jsonb) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.operator_reconciliation_context(text),public.reconcile_inventory(text,uuid,bigint,integer,jsonb,text,jsonb) TO authenticated;

NOTIFY pgrst, 'reload schema';
COMMIT;
