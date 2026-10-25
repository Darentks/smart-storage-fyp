-- =============================================================================
-- Smart Storage FYP - Supabase Database Schema
-- 30-Second Baseline Comparison & Teachable Machine AI Inventory Verification
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 1. Drop the old reference sample table so the database stays clean
DROP TABLE IF EXISTS public.esp32_reference_samples CASCADE;

-- -----------------------------------------------------------------------------
-- Table 1: public.compartments
-- Dual-Camera hardware telemetry, stock quantities, and 30s baseline tracking
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.compartments (
    shelf_id VARCHAR(50) PRIMARY KEY,                                      -- e.g. 'SHELF_R1_C1'
    rack_id VARCHAR(50) NOT NULL DEFAULT 'RACK_01',                         -- Rack namespace identifier
    mqtt_client_id VARCHAR(100) UNIQUE NOT NULL,                            -- Primary ESP32 node (e.g. 'ESP32_R1_C1')
    secondary_mqtt_client_id TEXT DEFAULT 'ESP32_R1_C1_CAM2',               -- MQTT ID of 2nd ESP32-CAM (Front Angle)
    camera_name VARCHAR(100) NOT NULL DEFAULT 'ESP32 Dual Node R1-C1',      -- Human-readable camera node name
    ip_address VARCHAR(50) DEFAULT '192.168.0.101',                         -- CAM 1 Back Angle IP
    secondary_ip_address TEXT DEFAULT '192.168.0.102',                      -- CAM 2 Front Angle IP
    current_quantity INTEGER NOT NULL DEFAULT 4 CHECK (current_quantity >= 0 AND current_quantity <= 4),
    max_capacity INTEGER NOT NULL DEFAULT 4,                                -- Max items in compartment (4)
    baseline_image_url TEXT NULL,                                           -- Latest 30s stitched [CAM1 | CAM2] or CAM1 baseline image
    baseline_image_2_url TEXT NULL,                                         -- Latest 30s CAM2 Front baseline image
    baseline_updated_at TIMESTAMPTZ NULL,                                   -- Timestamp of last baseline update
    tm_model_url TEXT DEFAULT 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/', -- Teachable Machine model URL
    status VARCHAR(20) NOT NULL DEFAULT 'OFFLINE',                          -- 'ONLINE', 'OFFLINE', 'ERROR'
    rssi_dbm INTEGER NULL,                                                  -- Wi-Fi signal strength (dBm)
    free_heap_bytes INTEGER NULL,                                           -- ESP32 free SRAM heap (bytes)
    last_seen_at TIMESTAMPTZ NULL,                                          -- Last MQTT heartbeat timestamp
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Migration Statements for Existing Table 1
ALTER TABLE public.compartments ALTER COLUMN ip_address SET DEFAULT '192.168.0.101';
ALTER TABLE public.compartments ALTER COLUMN camera_name SET DEFAULT 'ESP32 Dual Node R1-C1';
ALTER TABLE public.compartments ADD COLUMN IF NOT EXISTS secondary_ip_address TEXT DEFAULT '192.168.0.102';
ALTER TABLE public.compartments ADD COLUMN IF NOT EXISTS secondary_mqtt_client_id TEXT DEFAULT 'ESP32_R1_C1_CAM2';
ALTER TABLE public.compartments ADD COLUMN IF NOT EXISTS baseline_image_2_url TEXT;
ALTER TABLE public.compartments ADD COLUMN IF NOT EXISTS tm_model_url TEXT DEFAULT 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/';

CREATE INDEX IF NOT EXISTS idx_compartments_rack_id ON public.compartments(rack_id);
CREATE INDEX IF NOT EXISTS idx_compartments_mqtt_client_id ON public.compartments(mqtt_client_id);

-- -----------------------------------------------------------------------------
-- Table 2: public.comparison_logs
-- OpenCV 30s-baseline diff vs. post-trigger Teachable Machine Dual-Angle AI verification
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.comparison_logs (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shelf_id VARCHAR(50) NOT NULL REFERENCES public.compartments(shelf_id) ON DELETE CASCADE,
    mqtt_client_id VARCHAR(100) NOT NULL,
    trigger_action VARCHAR(20) NOT NULL DEFAULT 'BORROW',
    before_image_url TEXT NULL,                                             -- Latest 30-second picture
    after_image_url TEXT NULL,                                              -- Post-trigger independent picture
    diff_score NUMERIC(5,2) NULL,                                           -- OpenCV visual pixel diff %
    similarity_score NUMERIC(5,2) NULL,                                     -- Teachable Machine confidence %
    matched_box_count INTEGER NULL,                                         -- 0..4 boxes from CAM1 + CAM2
    scenario_label TEXT NULL,                                               -- Exact matched scenario (Scenario 1..5)
    state_delta VARCHAR(30) NOT NULL CHECK (state_delta IN ('ITEM_REMOVED', 'ITEM_RETURNED', 'NO_CHANGE')),
    quantity_before INTEGER NOT NULL,
    quantity_after INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Migration Statement for Existing Table 2
ALTER TABLE public.comparison_logs ADD COLUMN IF NOT EXISTS scenario_label TEXT;

CREATE INDEX IF NOT EXISTS idx_comparison_logs_shelf_id ON public.comparison_logs(shelf_id);
CREATE INDEX IF NOT EXISTS idx_comparison_logs_created_at ON public.comparison_logs(created_at DESC);

-- -----------------------------------------------------------------------------
-- Table 3: public.cctv_cameras
-- 3 Hikvision CCTV cameras facing the rack
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.cctv_cameras (
    cctv_id VARCHAR(50) PRIMARY KEY,
    camera_name VARCHAR(100) NOT NULL,
    rack_id VARCHAR(50) NOT NULL DEFAULT 'RACK_01',
    ip_address VARCHAR(50) NOT NULL DEFAULT '192.168.100.1',
    rtsp_url TEXT NOT NULL DEFAULT 'rtsp://admin:Password@192.168.100.1:554/Streaming/Channels/102',
    assigned_shelves JSONB NOT NULL DEFAULT '["SHELF_R1_C1", "SHELF_R1_C2", "SHELF_R1_C3"]'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(20) NOT NULL DEFAULT 'ONLINE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cctv_cameras_rack_id ON public.cctv_cameras(rack_id);
CREATE INDEX IF NOT EXISTS idx_cctv_cameras_is_active ON public.cctv_cameras(is_active);

-- -----------------------------------------------------------------------------
-- Row Level Security (RLS) Policies
-- -----------------------------------------------------------------------------
ALTER TABLE public.compartments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.comparison_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cctv_cameras ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow public access on compartments" ON public.compartments;
CREATE POLICY "Allow public access on compartments"
    ON public.compartments
    FOR ALL
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS "Allow public access on comparison_logs" ON public.comparison_logs;
CREATE POLICY "Allow public access on comparison_logs"
    ON public.comparison_logs
    FOR ALL
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS "Allow public access on cctv_cameras" ON public.cctv_cameras;
CREATE POLICY "Allow public access on cctv_cameras"
    ON public.cctv_cameras
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- -----------------------------------------------------------------------------
-- Seed Initial Compartments
-- -----------------------------------------------------------------------------
INSERT INTO public.compartments (
    shelf_id, rack_id, mqtt_client_id, secondary_mqtt_client_id, camera_name,
    ip_address, secondary_ip_address, current_quantity, max_capacity, status, tm_model_url
) VALUES
    ('SHELF_R1_C1', 'RACK_01', 'ESP32_R1_C1', 'ESP32_R1_C1_CAM2', 'ESP32 Dual Node R1-C1', '192.168.0.101', '192.168.0.102', 4, 4, 'OFFLINE', 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/'),
    ('SHELF_R1_C2', 'RACK_01', 'ESP32_R1_C2', 'ESP32_R1_C2_CAM2', 'ESP32 Dual Node R1-C2', '192.168.0.101', '192.168.0.102', 4, 4, 'OFFLINE', 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/'),
    ('SHELF_R1_C3', 'RACK_01', 'ESP32_R1_C3', 'ESP32_R1_C3_CAM2', 'ESP32 Dual Node R1-C3', '192.168.0.101', '192.168.0.102', 4, 4, 'OFFLINE', 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/')
ON CONFLICT (shelf_id) DO UPDATE SET
    rack_id = EXCLUDED.rack_id,
    mqtt_client_id = EXCLUDED.mqtt_client_id,
    secondary_mqtt_client_id = EXCLUDED.secondary_mqtt_client_id,
    camera_name = EXCLUDED.camera_name,
    ip_address = EXCLUDED.ip_address,
    secondary_ip_address = EXCLUDED.secondary_ip_address,
    current_quantity = EXCLUDED.current_quantity,
    max_capacity = EXCLUDED.max_capacity,
    tm_model_url = EXCLUDED.tm_model_url,
    status = EXCLUDED.status;

-- -----------------------------------------------------------------------------
-- Seed 3 Hikvision CCTVs facing the rack
-- -----------------------------------------------------------------------------
INSERT INTO public.cctv_cameras (
    cctv_id, camera_name, rack_id, ip_address, rtsp_url, assigned_shelves, is_active, status
) VALUES
    ('CCTV_01', 'Hikvision CCTV 1 (Left Angle)', 'RACK_01', '192.168.100.1', 'rtsp://admin:Password@192.168.100.1:554/Streaming/Channels/102', '["SHELF_R1_C1", "SHELF_R1_C2", "SHELF_R1_C3"]'::jsonb, TRUE, 'ONLINE'),
    ('CCTV_02', 'Hikvision CCTV 2 (Center Angle)', 'RACK_01', '192.168.100.2', 'rtsp://admin:Password@192.168.100.2:554/Streaming/Channels/102', '["SHELF_R1_C1", "SHELF_R1_C2", "SHELF_R1_C3"]'::jsonb, FALSE, 'OFFLINE'),
    ('CCTV_03', 'Hikvision CCTV 3 (Right Angle)', 'RACK_01', '192.168.100.3', 'rtsp://admin:Password@192.168.100.3:554/Streaming/Channels/102', '["SHELF_R1_C1", "SHELF_R1_C2", "SHELF_R1_C3"]'::jsonb, FALSE, 'OFFLINE')
ON CONFLICT (cctv_id) DO UPDATE SET
    camera_name = EXCLUDED.camera_name,
    rack_id = EXCLUDED.rack_id,
    ip_address = EXCLUDED.ip_address,
    rtsp_url = EXCLUDED.rtsp_url,
    assigned_shelves = EXCLUDED.assigned_shelves,
    is_active = EXCLUDED.is_active,
    status = EXCLUDED.status;

-- ============================================================================
-- EXPLICIT DATA API GRANTS
-- Grants full CRUD to anon (used by sb_publishable_ key), authenticated, and service_role
-- ============================================================================

-- 1. Schema usage
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;

-- 2. Table-level CRUD permissions for all 3 tables
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.compartments TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.comparison_logs TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.cctv_cameras TO anon, authenticated, service_role;

-- 3. Sequence permissions
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

-- 4. Future-proofing default privileges
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT USAGE, SELECT ON SEQUENCES TO anon, authenticated, service_role;
