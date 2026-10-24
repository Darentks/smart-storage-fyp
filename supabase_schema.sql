-- =============================================================================
-- Smart Storage FYP - Supabase Database Schema
-- 30-Second Baseline Comparison & Teachable Machine AI Inventory Verification
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Clean up deprecated reference samples table (Classification is 100% handled by Teachable Machine)
DROP TABLE IF EXISTS public.esp32_reference_samples CASCADE;

-- -----------------------------------------------------------------------------
-- Table 1: public.compartments
-- Runtime state, dual-camera hardware telemetry, quantities, and 30s baseline tracking
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.compartments (
    shelf_id VARCHAR(50) PRIMARY KEY,                                      -- e.g. 'SHELF_R1_C1'
    rack_id VARCHAR(50) NOT NULL DEFAULT 'RACK_01',                         -- Rack namespace identifier
    mqtt_client_id VARCHAR(100) UNIQUE NOT NULL,                            -- Primary ESP32 node (e.g. 'ESP32_R1_C1')
    secondary_mqtt_client_id TEXT DEFAULT 'ESP32_R1_C1_CAM2',               -- MQTT ID of 2nd ESP32-CAM (Front Angle)
    camera_name VARCHAR(100) NOT NULL DEFAULT 'ESP32 Node R1-C1',           -- Human-readable camera node name
    ip_address VARCHAR(50) NOT NULL DEFAULT '192.168.0.101',                -- Primary ESP32-CAM Back Angle IP
    secondary_ip_address TEXT DEFAULT '192.168.0.102',                      -- Secondary ESP32-CAM Front Angle IP
    current_quantity INTEGER NOT NULL DEFAULT 4 CHECK (current_quantity >= 0 AND current_quantity <= 4),
    max_capacity INTEGER NOT NULL DEFAULT 4,                                -- Max items in compartment (4)
    baseline_image_url TEXT NULL,                                           -- 30s baseline snapshot (Back Angle or Stitched)
    baseline_image_2_url TEXT NULL,                                         -- 30s baseline snapshot (Front Angle)
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
    before_image_url TEXT NULL,
    after_image_url TEXT NULL,
    diff_score NUMERIC(5,2) NULL,
    similarity_score NUMERIC(5,2) NULL,                                     -- Stores Teachable Machine confidence %
    matched_box_count INTEGER NULL CHECK (matched_box_count >= 0 AND matched_box_count <= 4), -- 0..4 box count
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
-- Row Level Security (RLS) Policies
-- -----------------------------------------------------------------------------
ALTER TABLE public.compartments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.comparison_logs ENABLE ROW LEVEL SECURITY;

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

-- -----------------------------------------------------------------------------
-- Seed Initial Compartments
-- -----------------------------------------------------------------------------
INSERT INTO public.compartments (
    shelf_id, rack_id, mqtt_client_id, secondary_mqtt_client_id, camera_name,
    ip_address, secondary_ip_address, current_quantity, max_capacity, status, tm_model_url
) VALUES
    ('SHELF_R1_C1', 'RACK_01', 'ESP32_R1_C1', 'ESP32_R1_C1_CAM2', 'ESP32 Node R1-C1', '192.168.0.101', '192.168.0.102', 4, 4, 'OFFLINE', 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/'),
    ('SHELF_R1_C2', 'RACK_01', 'ESP32_R1_C2', 'ESP32_R1_C2_CAM2', 'ESP32 Node R1-C2', '192.168.0.101', '192.168.0.102', 4, 4, 'OFFLINE', 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/'),
    ('SHELF_R1_C3', 'RACK_01', 'ESP32_R1_C3', 'ESP32_R1_C3_CAM2', 'ESP32 Node R1-C3', '192.168.0.101', '192.168.0.102', 4, 4, 'OFFLINE', 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/')
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

-- ============================================================================
-- EXPLICIT DATA API GRANTS
-- Grants full CRUD to anon (used by sb_publishable_ key), authenticated, and service_role
-- ============================================================================

-- 1. Schema usage
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;

-- 2. Table-level CRUD permissions for the 2 core tables
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.compartments TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.comparison_logs TO anon, authenticated, service_role;

-- 3. Sequence permissions
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

-- 4. Future-proofing default privileges
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT USAGE, SELECT ON SEQUENCES TO anon, authenticated, service_role;

GRANT USAGE, SELECT ON SEQUENCES TO anon, authenticated, service_role;