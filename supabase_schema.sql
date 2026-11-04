BEGIN;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
DROP TABLE IF EXISTS public.esp32_reference_samples, public.comparison_logs, public.esp32_cameras, public.cctv_cameras, public.compartments CASCADE;
DROP FUNCTION IF EXISTS public.strip_base64_compartments(), public.strip_base64_comparison_logs(), public.set_updated_at() CASCADE;

CREATE TABLE public.compartments (
    shelf_id VARCHAR(50) PRIMARY KEY,
    rack_id VARCHAR(50) NOT NULL DEFAULT 'RACK_01',
    current_quantity INTEGER NOT NULL DEFAULT 4 CHECK (current_quantity >= 0),
    max_capacity INTEGER NOT NULL DEFAULT 4 CHECK (max_capacity > 0),
    tm_model_url TEXT DEFAULT 'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/',
    CONSTRAINT chk_compartment_quantity_within_capacity CHECK (current_quantity <= max_capacity)
);
CREATE INDEX idx_compartments_rack_id ON public.compartments (rack_id);

CREATE TABLE public.esp32_cameras (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id VARCHAR(100) NOT NULL UNIQUE,
    camera_name VARCHAR(100) NOT NULL,
    rack_id VARCHAR(50) NOT NULL DEFAULT 'RACK_01',
    shelf_id VARCHAR(50) NOT NULL REFERENCES public.compartments(shelf_id) ON UPDATE CASCADE ON DELETE CASCADE,
    angle_index SMALLINT NOT NULL CHECK (angle_index IN (1, 2)),
    ip_address INET,
    CONSTRAINT uq_esp32_camera_shelf_angle UNIQUE (shelf_id, angle_index)
);
CREATE INDEX idx_esp32_cameras_shelf_id ON public.esp32_cameras (shelf_id);
CREATE INDEX idx_esp32_cameras_rack_id ON public.esp32_cameras (rack_id);

CREATE TABLE public.cctv_cameras (
    cctv_id VARCHAR(50) PRIMARY KEY,
    camera_name VARCHAR(100) NOT NULL,
    rack_id VARCHAR(50) NOT NULL DEFAULT 'RACK_01',
    ip_address INET,
    rtsp_url TEXT NOT NULL DEFAULT 'rtsp://admin:Password@192.168.0.101:554/Streaming/Channels/102',
    is_active BOOLEAN NOT NULL DEFAULT TRUE
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
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO anon, authenticated, service_role;

INSERT INTO public.compartments (shelf_id) VALUES ('SHELF_R1_C1'),('SHELF_R1_C2'),('SHELF_R1_C3'),('SHELF_R1_C4'),('SHELF_R1_C5'),('SHELF_R1_C6') ON CONFLICT DO NOTHING;

INSERT INTO public.esp32_cameras (device_id, camera_name, shelf_id, angle_index, ip_address) VALUES
('ESP32_R1_C1_CAM1','R1-C1 Back Angle (CAM 1)','SHELF_R1_C1',1,'192.168.0.201'),
('ESP32_R1_C1_CAM2','R1-C1 Front Angle (CAM 2)','SHELF_R1_C1',2,'192.168.0.202'),
('ESP32_R1_C2_CAM1','R1-C2 Back Angle (CAM 3)','SHELF_R1_C2',1,'192.168.0.203'),
('ESP32_R1_C2_CAM2','R1-C2 Front Angle (CAM 4)','SHELF_R1_C2',2,'192.168.0.204'),
('ESP32_R1_C3_CAM1','R1-C3 Back Angle (CAM 5)','SHELF_R1_C3',1,'192.168.0.205'),
('ESP32_R1_C3_CAM2','R1-C3 Front Angle (CAM 6)','SHELF_R1_C3',2,'192.168.0.206'),
('ESP32_R1_C4_CAM1','R1-C4 Back Angle (CAM 7)','SHELF_R1_C4',1,'192.168.0.207'),
('ESP32_R1_C4_CAM2','R1-C4 Front Angle (CAM 8)','SHELF_R1_C4',2,'192.168.0.208'),
('ESP32_R1_C5_CAM1','R1-C5 Back Angle (CAM 9)','SHELF_R1_C5',1,'192.168.0.209'),
('ESP32_R1_C5_CAM2','R1-C5 Front Angle (CAM 10)','SHELF_R1_C5',2,'192.168.0.210'),
('ESP32_R1_C6_CAM1','R1-C6 Back Angle (CAM 11)','SHELF_R1_C6',1,'192.168.0.211'),
('ESP32_R1_C6_CAM2','R1-C6 Front Angle (CAM 12)','SHELF_R1_C6',2,'192.168.0.212')
ON CONFLICT DO NOTHING;

INSERT INTO public.cctv_cameras (cctv_id, camera_name, ip_address, rtsp_url, is_active) VALUES
('CCTV_01','Hikvision CCTV 1 (Left Angle)','192.168.0.101','rtsp://admin:Password@192.168.0.101:554/Streaming/Channels/102',TRUE),
('CCTV_02','Hikvision CCTV 2 (Center Angle)','192.168.0.102','rtsp://admin:Password@192.168.0.102:554/Streaming/Channels/102',FALSE),
('CCTV_03','Hikvision CCTV 3 (Right Angle)','192.168.0.103','rtsp://admin:Password@192.168.0.103:554/Streaming/Channels/102',FALSE)
ON CONFLICT DO NOTHING;
COMMIT;
