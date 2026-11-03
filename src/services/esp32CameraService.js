/**
 * ESP32-CAM Hardware Node CRUD, NVS Reassignment & Wireless OTA Service
 * Smart Storage FYP
 *
 * Provides:
 *  - probeCameraStatus(ip): GET http://${ip}/status (3s timeout)
 *  - pushCameraConfig(ip, payload): POST http://${ip}/config ({ device_id, rack_id, shelf_id, camera_name, ip_octet })
 *  - uploadCameraFirmware(ip, binFile): POST http://${ip}/update (FormData 'file')
 *  - captureLiveSnapshotUrl(ip, mode = 'live', flash = 0): http://${ip}/capture?mode=${mode}&flash=${flash}&t=${Date.now()}
 *  - Supabase CRUD helpers: getCameras(), upsertCamera(cam), deleteCamera(id)
 */

const SUPABASE_URL = (typeof process !== 'undefined' && process.env?.NEXT_PUBLIC_SUPABASE_URL) || 'https://tosvjwxvcuxvgtcmakta.supabase.co';
const SUPABASE_ANON_KEY = (typeof process !== 'undefined' && process.env?.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY) || 'sb_publishable_N7bQW1bkTZq6TPVPYlqVag_C7vXKF9l';

export const DEFAULT_12_ESP32_CAMERAS = [
  { id: 'cam-201', device_id: 'ESP32_R1_C1_CAM1', camera_name: 'R1-C1 Back Angle (CAM 1)',  rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C1', angle_index: 1, ip_address: '192.168.0.201', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-202', device_id: 'ESP32_R1_C1_CAM2', camera_name: 'R1-C1 Front Angle (CAM 2)', rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C1', angle_index: 2, ip_address: '192.168.0.202', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-203', device_id: 'ESP32_R1_C2_CAM1', camera_name: 'R1-C2 Back Angle (CAM 3)',  rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C2', angle_index: 1, ip_address: '192.168.0.203', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-204', device_id: 'ESP32_R1_C2_CAM2', camera_name: 'R1-C2 Front Angle (CAM 4)', rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C2', angle_index: 2, ip_address: '192.168.0.204', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-205', device_id: 'ESP32_R1_C3_CAM1', camera_name: 'R1-C3 Back Angle (CAM 5)',  rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C3', angle_index: 1, ip_address: '192.168.0.205', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-206', device_id: 'ESP32_R1_C3_CAM2', camera_name: 'R1-C3 Front Angle (CAM 6)', rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C3', angle_index: 2, ip_address: '192.168.0.206', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-207', device_id: 'ESP32_R1_C4_CAM1', camera_name: 'R1-C4 Back Angle (CAM 7)',  rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C4', angle_index: 1, ip_address: '192.168.0.207', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-208', device_id: 'ESP32_R1_C4_CAM2', camera_name: 'R1-C4 Front Angle (CAM 8)', rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C4', angle_index: 2, ip_address: '192.168.0.208', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-209', device_id: 'ESP32_R1_C5_CAM1', camera_name: 'R1-C5 Back Angle (CAM 9)',  rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C5', angle_index: 1, ip_address: '192.168.0.209', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-210', device_id: 'ESP32_R1_C5_CAM2', camera_name: 'R1-C5 Front Angle (CAM 10)', rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C5', angle_index: 2, ip_address: '192.168.0.210', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-211', device_id: 'ESP32_R1_C6_CAM1', camera_name: 'R1-C6 Back Angle (CAM 11)', rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C6', angle_index: 1, ip_address: '192.168.0.211', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
  { id: 'cam-212', device_id: 'ESP32_R1_C6_CAM2', camera_name: 'R1-C6 Front Angle (CAM 12)', rack_id: 'RACK_01', shelf_id: 'SHELF_R1_C6', angle_index: 2, ip_address: '192.168.0.212', status: 'OFFLINE', rssi_dbm: null, last_seen: null },
];

function cleanIpAddress(ip) {
  return String(ip || '').trim().replace(/^https?:\/\//i, '').replace(/\/\d+$/, '').replace(/\/.*$/, '').trim();
}

/**
 * Resolves the local CameraService bridge URL (supporting HTTPS / Cloudflare Tunnel / env vars)
 * to avoid Mixed Content blocking when hosted on Vercel HTTPS.
 */
export function resolveBridgeUrl() {
  if (typeof window !== 'undefined') {
    if (typeof window.getBridgeUrl === 'function') {
      const u = window.getBridgeUrl();
      if (u) return String(u).replace(/\/+$/, '');
    }
    try {
      const saved = localStorage.getItem('smart_storage_bridge_url');
      if (saved && saved.trim()) return saved.trim().replace(/\/+$/, '');
    } catch (e) {}
    if (window.NEXT_PUBLIC_BRIDGE_URL) return String(window.NEXT_PUBLIC_BRIDGE_URL).replace(/\/+$/, '');
    if (window.VITE_CAMERA_BRIDGE_URL) return String(window.VITE_CAMERA_BRIDGE_URL).replace(/\/+$/, '');
  }
  try {
    if (typeof import.meta !== 'undefined' && import.meta.env) {
      const viteUrl = import.meta.env.VITE_CAMERA_BRIDGE_URL || import.meta.env.NEXT_PUBLIC_BRIDGE_URL;
      if (viteUrl) return String(viteUrl).replace(/\/+$/, '');
    }
  } catch (e) {}
  if (typeof process !== 'undefined' && process.env) {
    const envUrl = process.env.NEXT_PUBLIC_BRIDGE_URL || process.env.VITE_CAMERA_BRIDGE_URL;
    if (envUrl) return String(envUrl).replace(/\/+$/, '');
  }
  return 'http://localhost:8008';
}

export function resolveBridgeWsUrl() {
  const httpUrl = resolveBridgeUrl();
  if (httpUrl.startsWith('https://')) return httpUrl.replace(/^https:\/\//i, 'wss://');
  if (httpUrl.startsWith('http://')) return httpUrl.replace(/^http:\/\//i, 'ws://');
  return `ws://${httpUrl}`;
}

function shouldProxyLanThroughBridge() {
  if (typeof window === 'undefined') return false;
  return window.location?.protocol === 'https:';
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = (err) => reject(err);
    reader.readAsDataURL(file);
  });
}

async function supabaseRestRequest(table, method = 'GET', query = '', body = null, prefer = 'return=minimal') {
  const url = `${SUPABASE_URL}/rest/v1/${table}${query}`;
  const headers = {
    apikey: SUPABASE_ANON_KEY,
    Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
    'Content-Type': 'application/json',
  };
  if (prefer) headers.Prefer = prefer;

  const options = { method, headers };
  if (body !== null) options.body = JSON.stringify(body);

  const resp = await fetch(url, options);
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`Supabase ${method} ${table} failed (${resp.status}): ${errText}`);
  }
  if (resp.status === 204 || prefer.includes('return=minimal')) return null;
  return await resp.json();
}

/**
 * Probes an ESP32-CAM's onboard HTTP server at GET http://${ip}/status (3s timeout)
 * @param {string} ip
 * @returns {Promise<{device_id: string, shelf_id: string, rack_id?: string, rssi_dbm: number|null, ip_address: string, status: string}>}
 */
export async function probeCameraStatus(ip) {
  const cleanIp = cleanIpAddress(ip);
  if (!cleanIp) throw new Error('IP address is required to probe camera status.');

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 3500);
  const useBridgeProxy = shouldProxyLanThroughBridge();
  const targetUrl = useBridgeProxy
    ? `${resolveBridgeUrl()}/esp32_probe?ip=${encodeURIComponent(cleanIp)}`
    : `http://${cleanIp}/status`;

  try {
    const resp = await fetch(targetUrl, {
      method: 'GET',
      signal: controller.signal,
      cache: 'no-store',
    });
    clearTimeout(timeoutId);

    if (!resp.ok) {
      throw new Error(`Camera at ${cleanIp} returned HTTP ${resp.status}`);
    }

    const data = await resp.json();
    return {
      device_id: data.device_id || data.mqtt_client_id || `ESP32_${cleanIp.split('.').pop()}`,
      shelf_id: data.shelf_id || 'SHELF_R1_C1',
      rack_id: data.rack_id || 'RACK_01',
      camera_name: data.camera_name || null,
      rssi_dbm: data.rssi_dbm != null ? data.rssi_dbm : (data.rssi != null ? data.rssi : null),
      ip_address: data.ip_address || cleanIp,
      free_heap_bytes: data.free_heap_bytes || null,
      status: 'ONLINE',
    };
  } catch (err) {
    clearTimeout(timeoutId);
    throw new Error(`Unreachable camera at ${cleanIp}: ${err.name === 'AbortError' ? 'Timed out (3s)' : err.message}`);
  }
}

/**
 * Pushes updated identity and static IP configuration to the ESP32-CAM's onboard NVS
 * via POST http://${ip}/config (or via bridge /esp32_nvs_config on HTTPS) and triggers an automatic reboot.
 * @param {string} ip
 * @param {{device_id: string, rack_id: string, shelf_id: string, camera_name?: string, ip_octet: number}} payload
 */
export async function pushCameraConfig(ip, payload) {
  const cleanIp = cleanIpAddress(ip);
  if (!cleanIp) throw new Error('Target camera IP is required.');

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);
  const nvsBody = {
    device_id: payload.device_id,
    rack_id: payload.rack_id || 'RACK_01',
    shelf_id: payload.shelf_id,
    camera_name: payload.camera_name || `${payload.shelf_id} (${payload.device_id})`,
    ip_octet: Number(payload.ip_octet),
  };
  const useBridgeProxy = shouldProxyLanThroughBridge();
  const targetUrl = useBridgeProxy ? `${resolveBridgeUrl()}/esp32_nvs_config` : `http://${cleanIp}/config`;

  try {
    const resp = await fetch(targetUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(useBridgeProxy ? { ip: cleanIp, config: nvsBody } : nvsBody),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${txt}`);
    }

    return await resp.json();
  } catch (err) {
    clearTimeout(timeoutId);
    throw new Error(`Failed to push NVS config to ${cleanIp}: ${err.message}`);
  }
}

/**
 * Uploads compiled firmware (.bin) to the ESP32-CAM via POST http://${ip}/update
 * (or via bridge /esp32_ota_upload when running on Vercel HTTPS)
 * @param {string} ip
 * @param {File} binFile
 */
export async function uploadCameraFirmware(ip, binFile) {
  const cleanIp = cleanIpAddress(ip);
  if (!cleanIp) throw new Error('Camera IP address is required for OTA update.');
  if (!binFile) throw new Error('Please select a valid .bin firmware file.');

  if (shouldProxyLanThroughBridge()) {
    const b64 = await fileToBase64(binFile);
    const resp = await fetch(`${resolveBridgeUrl()}/esp32_ota_upload`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ip: cleanIp,
        filename: binFile.name || 'firmware.bin',
        file_base64: b64,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.status === 'failed') {
      throw new Error(data.error || `OTA Firmware upload via bridge failed (${resp.status})`);
    }
    return data;
  }

  const formData = new FormData();
  formData.append('file', binFile, binFile.name || 'firmware.bin');

  const resp = await fetch(`http://${cleanIp}/update`, {
    method: 'POST',
    body: formData,
  });

  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`OTA Firmware upload failed (${resp.status}): ${errText}`);
  }

  const contentType = resp.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    return await resp.json();
  }
  return { status: 'success', message: await resp.text() };
}

/**
 * Builds the direct HTTP snapshot capture URL for an ESP32-CAM node
 * @param {string} ip
 * @param {'live'|'baseline'} mode
 * @returns {string}
 */
export function captureLiveSnapshotUrl(ip, mode = 'live', flash = 0) {
  const cleanIp = cleanIpAddress(ip);
  if (shouldProxyLanThroughBridge()) {
    return `${resolveBridgeUrl()}/esp32_preview?ip=${encodeURIComponent(cleanIp)}&mode=${encodeURIComponent(mode)}&t=${Date.now()}`;
  }
  return `http://${cleanIp}/capture?mode=${encodeURIComponent(mode)}&flash=${flash}&t=${Date.now()}`;
}

/**
 * Fetches all ESP32-CAM records from Supabase `public.esp32_cameras`
 * (with resilient fallback to local storage / 12-camera seed list)
 */
export async function getCameras(forceRefresh = false) {
  if (!forceRefresh) {
    try {
      const lastFetch = Number(localStorage.getItem('smart_storage_esp32_cameras_fetched_at') || 0);
      const cached = localStorage.getItem('smart_storage_esp32_cameras_cache');
      if (cached && (Date.now() - lastFetch < 300000)) {
        const parsed = JSON.parse(cached);
        if (Array.isArray(parsed) && parsed.length > 0) return parsed;
      }
    } catch (e) {}
  }

  try {
    const cols = 'id,device_id,camera_name,rack_id,shelf_id,angle_index,ip_address';
    const rows = await supabaseRestRequest(
      'esp32_cameras',
      'GET',
      `?select=${cols}&order=shelf_id.asc,angle_index.asc`,
      null,
      'return=representation'
    );
    if (Array.isArray(rows) && rows.length > 0) {
      localStorage.setItem('smart_storage_esp32_cameras_cache', JSON.stringify(rows));
      localStorage.setItem('smart_storage_esp32_cameras_fetched_at', String(Date.now()));
      return rows;
    }
  } catch (err) {
    console.warn('[esp32CameraService] Supabase esp32_cameras query fallback:', err.message);
  }

  try {
    const cached = localStorage.getItem('smart_storage_esp32_cameras_cache');
    if (cached) {
      const parsed = JSON.parse(cached);
      if (Array.isArray(parsed) && parsed.length > 0) return parsed;
    }
  } catch (e) {}

  return [...DEFAULT_12_ESP32_CAMERAS];
}

/**
 * Inserts or updates an ESP32-CAM node in Supabase `public.esp32_cameras` (static mapping columns only).
 * status / rssi_dbm / last_seen are runtime telemetry: merged into the returned object, never persisted.
 * @param {Object} cam
 */
export async function upsertCamera(cam) {
  const record = {
    device_id: String(cam.device_id).trim(),
    camera_name: String(cam.camera_name || `${cam.shelf_id} Angle ${cam.angle_index || 1}`).trim(),
    rack_id: String(cam.rack_id || 'RACK_01').trim(),
    shelf_id: String(cam.shelf_id || 'SHELF_R1_C1').trim(),
    angle_index: Number(cam.angle_index) === 2 ? 2 : 1,
    ip_address: cleanIpAddress(cam.ip_address || '192.168.0.201'),
  };
  const runtime = {
    status: cam.status || 'ONLINE',
    rssi_dbm: cam.rssi_dbm != null ? Number(cam.rssi_dbm) : null,
    last_seen: cam.last_seen || new Date().toISOString(),
  };

  let savedRow = null;
  try {
    const res = await supabaseRestRequest(
      'esp32_cameras',
      'POST',
      '?on_conflict=device_id&select=id,device_id,camera_name,rack_id,shelf_id,angle_index,ip_address',
      record,
      'resolution=merge-duplicates,return=representation'
    );
    if (Array.isArray(res) && res.length > 0) {
      savedRow = res[0];
    }
  } catch (err) {
    console.warn('[esp32CameraService] upsertCamera Supabase table fallback:', err.message);
  }

  const result = {
    id: cam.id || `cam-${Date.now()}`,
    ...record,
    ...(savedRow || {}),
    ...runtime,
    ip_address: cleanIpAddress((savedRow && savedRow.ip_address) || record.ip_address),
  };

  // Update local cache (DB columns only)
  try {
    const list = await getCameras();
    const idx = list.findIndex(item => item.device_id === record.device_id || (cam.id && item.id === cam.id));
    const merged = { id: result.id, ...record, ...(savedRow || {}) };
    if (idx >= 0) {
      list[idx] = { ...list[idx], ...merged };
    } else {
      list.push(merged);
    }
    localStorage.setItem('smart_storage_esp32_cameras_cache', JSON.stringify(list));
  } catch (e) {}
  return result;
}

/**
 * Deletes an ESP32-CAM record from Supabase `public.esp32_cameras` by `id` or `device_id`
 * @param {string} idOrDeviceId
 */
export async function deleteCamera(idOrDeviceId) {
  const isUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(idOrDeviceId));
  const query = isUuid
    ? `?id=eq.${encodeURIComponent(idOrDeviceId)}`
    : `?device_id=eq.${encodeURIComponent(idOrDeviceId)}`;

  try {
    await supabaseRestRequest('esp32_cameras', 'DELETE', query, null, null);
  } catch (err) {
    console.warn('[esp32CameraService] deleteCamera Supabase warning:', err.message);
  }

  try {
    const cached = localStorage.getItem('smart_storage_esp32_cameras_cache');
    if (cached) {
      const list = JSON.parse(cached).filter(c => c.id !== idOrDeviceId && c.device_id !== idOrDeviceId);
      localStorage.setItem('smart_storage_esp32_cameras_cache', JSON.stringify(list));
    }
  } catch (e) {}

  return true;
}

/**
 * Triggers automated PlatformIO build & OTA firmware flashing via CameraService bridge
 * @param {string|string[]} ips
 * @param {boolean} [buildFirst=true]
 * @param {string|null} [repoPath=null]
 * @returns {Promise<{status: string, firmware: object, results: Array}>}
 */
export async function triggerOneClickOtaFlash(ips, buildFirst = true, repoPath = null) {
  const targetIps = Array.isArray(ips) ? ips : [ips];
  const cleanIps = targetIps.map(cleanIpAddress).filter(Boolean);
  if (cleanIps.length === 0) {
    throw new Error('At least one valid ESP32-CAM IP address is required for OTA flash.');
  }

  const bridgeUrl = resolveBridgeUrl();
  const resp = await fetch(`${bridgeUrl}/esp32_ota_flash`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ips: cleanIps,
      build: Boolean(buildFirst),
      repo_path: repoPath || undefined,
    }),
  });

  const data = await resp.json().catch(() => ({}));
  if (!resp.ok && resp.status !== 207) {
    throw new Error(data.error || `OTA flash failed with HTTP status ${resp.status}`);
  }
  return data;
}

const esp32CameraService = {
  DEFAULT_12_ESP32_CAMERAS,
  resolveBridgeUrl,
  resolveBridgeWsUrl,
  probeCameraStatus,
  pushCameraConfig,
  uploadCameraFirmware,
  captureLiveSnapshotUrl,
  getCameras,
  upsertCamera,
  deleteCamera,
  triggerOneClickOtaFlash,
};

if (typeof window !== 'undefined') {
  window.esp32CameraService = esp32CameraService;
}

export default esp32CameraService;

