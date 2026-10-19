-- =============================================================================
-- Smart Storage FYP - Supabase Database Schema
-- 30-Second Baseline Comparison & ESP32 Inventory Verification Workflow
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- -----------------------------------------------------------------------------
-- Table 1: public.compartments
-- Runtime state, hardware telemetry, quantities, and 30-second baseline tracking
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.compartments (
    shelf_id VARCHAR(50) PRIMARY KEY,                                      -- e.g. 'SHELF_R1_C1'
    rack_id VARCHAR(50) NOT NULL DEFAULT 'RACK_01',                         -- Rack namespace identifier
    mqtt_client_id VARCHAR(100) UNIQUE NOT NULL,                            -- e.g. 'ESP32_R1_C1'
    camera_name VARCHAR(100) NOT NULL DEFAULT 'ESP32 Node R1-C1',           -- Human-readable camera node name
    ip_address VARCHAR(50) NULL,                                            -- Local IP address of ESP32 node
    current_quantity INTEGER NOT NULL DEFAULT 4 CHECK (current_quantity >= 0 AND current_quantity <= 4),
    max_capacity INTEGER NOT NULL DEFAULT 4,                                -- Max items in compartment (4)
    baseline_image_url TEXT NULL,                                           -- Latest 30-second baseline snapshot
    baseline_updated_at TIMESTAMPTZ NULL,                                   -- Timestamp of last baseline update
    status VARCHAR(20) NOT NULL DEFAULT 'OFFLINE',                          -- 'ONLINE', 'OFFLINE', 'ERROR'
    rssi_dbm INTEGER NULL,                                                  -- Wi-Fi signal strength (dBm)
    free_heap_bytes INTEGER NULL,                                           -- ESP32 free SRAM heap (bytes)
    last_seen_at TIMESTAMPTZ NULL,                                          -- Last MQTT heartbeat timestamp
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_compartments_rack_id ON public.compartments(rack_id);
CREATE INDEX IF NOT EXISTS idx_compartments_mqtt_client_id ON public.compartments(mqtt_client_id);

-- -----------------------------------------------------------------------------
-- Table 2: public.esp32_reference_samples
-- Tested ESP32-CAM angle & scenario reference photos for 4, 3, 2, 1, 0 boxes
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.esp32_reference_samples (
    sample_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mqtt_client_id VARCHAR(100) NOT NULL,
    rack_id VARCHAR(50) NOT NULL DEFAULT 'RACK_01',
    shelf_id VARCHAR(50) NOT NULL REFERENCES public.compartments(shelf_id) ON DELETE CASCADE,
    sample_label VARCHAR(120) NOT NULL,
    box_count INTEGER NOT NULL DEFAULT 4 CHECK (box_count >= 0 AND box_count <= 4),
    slot_states JSONB NOT NULL DEFAULT '{"front_top": 1, "back_top": 1, "front_bottom": 1, "back_bottom": 1}'::jsonb,
    flash_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    feature_signature JSONB NULL,
    image_url TEXT NOT NULL,
    capture_latency_ms INTEGER NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_esp32_ref_shelf_label UNIQUE (shelf_id, sample_label)
);

CREATE INDEX IF NOT EXISTS idx_esp32_ref_samples_shelf_id ON public.esp32_reference_samples(shelf_id);
CREATE INDEX IF NOT EXISTS idx_esp32_ref_samples_box_count ON public.esp32_reference_samples(box_count);

-- -----------------------------------------------------------------------------
-- Table 3: public.comparison_logs
-- OpenCV 30s-baseline vs. post-trigger comparison & quantity delta updates
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.comparison_logs (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shelf_id VARCHAR(50) NOT NULL REFERENCES public.compartments(shelf_id) ON DELETE CASCADE,
    mqtt_client_id VARCHAR(100) NOT NULL,
    trigger_action VARCHAR(20) NOT NULL DEFAULT 'BORROW',
    before_image_url TEXT NULL,
    after_image_url TEXT NULL,
    diff_score NUMERIC(5,2) NULL,
    similarity_score NUMERIC(5,2) NULL,
    matched_box_count INTEGER NULL,
    state_delta VARCHAR(30) NOT NULL CHECK (state_delta IN ('ITEM_REMOVED', 'ITEM_RETURNED', 'NO_CHANGE')),
    quantity_before INTEGER NOT NULL,
    quantity_after INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comparison_logs_shelf_id ON public.comparison_logs(shelf_id);
CREATE INDEX IF NOT EXISTS idx_comparison_logs_created_at ON public.comparison_logs(created_at DESC);

-- -----------------------------------------------------------------------------
-- Row Level Security (RLS) Policies
-- -----------------------------------------------------------------------------
ALTER TABLE public.compartments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.esp32_reference_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.comparison_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow public access on compartments" ON public.compartments;
CREATE POLICY "Allow public access on compartments"
    ON public.compartments
    FOR ALL
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS "Allow public access on esp32_reference_samples" ON public.esp32_reference_samples;
CREATE POLICY "Allow public access on esp32_reference_samples"
    ON public.esp32_reference_samples
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
    shelf_id, rack_id, mqtt_client_id, camera_name, current_quantity, max_capacity, status
) VALUES
    ('SHELF_R1_C1', 'RACK_01', 'ESP32_R1_C1', 'ESP32 Node R1-C1', 4, 4, 'OFFLINE'),
    ('SHELF_R1_C2', 'RACK_01', 'ESP32_R1_C2', 'ESP32 Node R1-C2', 4, 4, 'OFFLINE'),
    ('SHELF_R1_C3', 'RACK_01', 'ESP32_R1_C3', 'ESP32 Node R1-C3', 4, 4, 'OFFLINE')
ON CONFLICT (shelf_id) DO UPDATE SET
    rack_id = EXCLUDED.rack_id,
    mqtt_client_id = EXCLUDED.mqtt_client_id,
    camera_name = EXCLUDED.camera_name,
    current_quantity = EXCLUDED.current_quantity,
    max_capacity = EXCLUDED.max_capacity,
    status = EXCLUDED.status;