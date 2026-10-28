import React, { useState, useEffect, useMemo } from 'react';
import {
  probeCameraStatus,
  pushCameraConfig,
  uploadCameraFirmware,
  captureLiveSnapshotUrl,
  getCameras,
  upsertCamera,
  deleteCamera,
} from '../services/esp32CameraService';

const SHELF_OPTIONS = [
  'SHELF_R1_C1',
  'SHELF_R1_C2',
  'SHELF_R1_C3',
  'SHELF_R1_C4',
  'SHELF_R1_C5',
  'SHELF_R1_C6',
];

const SCENARIO_LABELS = {
  4: 'Scenario 1: 4/4 Boxes Present',
  3: 'Scenario 2: 3/4 Boxes Present',
  2: 'Scenario 3: 2/4 Boxes Present',
  1: 'Scenario 4: 1/4 Box Present',
  0: 'Scenario 5: 0/4 No Box Present',
};

/**
 * Strict 6-Class Dual-ESP32 Teachable Machine classifier with Degraded Camera Guardrail.
 * Requires BOTH image_back (CAM1) and image_front (CAM2) to load (backOk && frontOk).
 * Filters backPreds strictly with /^back_(\d+)/i and frontPreds strictly with /^front_(\d+)/i.
 */
export async function predictDualCamerasWithTM(backImgEl, frontImgEl, tmModel = typeof window !== 'undefined' ? window.tmModel : null) {
  if (!tmModel) return null;

  const isUsable = (img) => {
    if (!img) return false;
    if (typeof HTMLImageElement !== 'undefined' && img instanceof HTMLImageElement) {
      return img.complete && img.naturalWidth > 0 && Boolean(img.src) && !img.src.endsWith('/null') && !img.src.endsWith('/undefined');
    }
    return Boolean(img);
  };

  const backOk = isUsable(backImgEl);
  const frontOk = isUsable(frontImgEl);

  if (!backOk || !frontOk) {
    return {
      backClass: backOk ? 'Back_Loaded' : 'Back_Offline',
      backCount: 0,
      backConfidence: 0,
      frontClass: frontOk ? 'Front_Loaded' : 'Front_Offline',
      frontCount: 0,
      frontConfidence: 0,
      totalBoxes: null,
      scenarioLabel: 'Validation aborted: Dual-angle required (1 camera unreachable)',
      confidence: 0,
      degraded: true,
    };
  }

  let bestBack = null;
  let backCount = 0;
  try {
    const backPreds = await tmModel.predict(backImgEl);
    const backOnly = backPreds.filter((p) => /^back_(\d+)/i.test(String(p.className || '').trim()));
    if (backOnly.length > 0) {
      bestBack = backOnly.reduce((a, b) => (a.probability > b.probability ? a : b));
      const m = String(bestBack.className).trim().match(/^back_(\d+)/i);
      backCount = m ? Math.min(2, Math.max(0, parseInt(m[1], 10))) : 0;
    }
  } catch (e) {
    console.warn('[TM] Back camera predict failed:', e);
  }

  let bestFront = null;
  let frontCount = 0;
  try {
    const frontPreds = await tmModel.predict(frontImgEl);
    const frontOnly = frontPreds.filter((p) => /^front_(\d+)/i.test(String(p.className || '').trim()));
    if (frontOnly.length > 0) {
      bestFront = frontOnly.reduce((a, b) => (a.probability > b.probability ? a : b));
      const m = String(bestFront.className).trim().match(/^front_(\d+)/i);
      frontCount = m ? Math.min(2, Math.max(0, parseInt(m[1], 10))) : 0;
    }
  } catch (e) {
    console.warn('[TM] Front camera predict failed:', e);
  }

  if (!bestBack || !bestFront) {
    return {
      backClass: bestBack ? bestBack.className : 'Back_Unmatched',
      backCount: 0,
      backConfidence: bestBack ? Number((bestBack.probability * 100).toFixed(1)) : 0,
      frontClass: bestFront ? bestFront.className : 'Front_Unmatched',
      frontCount: 0,
      frontConfidence: bestFront ? Number((bestFront.probability * 100).toFixed(1)) : 0,
      totalBoxes: null,
      scenarioLabel: 'Validation aborted: Dual-angle required (1 camera unreachable)',
      confidence: 0,
      degraded: true,
    };
  }

  const totalBoxes = Math.min(4, Math.max(0, backCount + frontCount));
  const avgConfidence = Number((((bestBack.probability + bestFront.probability) / 2) * 100).toFixed(1));

  return {
    backClass: bestBack.className,
    backCount,
    backConfidence: Number((bestBack.probability * 100).toFixed(1)),
    frontClass: bestFront.className,
    frontCount,
    frontConfidence: Number((bestFront.probability * 100).toFixed(1)),
    totalBoxes,
    scenarioLabel: SCENARIO_LABELS[totalBoxes] || `${totalBoxes}/4 Boxes`,
    confidence: avgConfidence,
    degraded: false,
  };
}

export default function Esp32CameraManager({ bridgeUrl = 'http://localhost:8008', onNotify }) {
  const [cameras, setCameras] = useState([]);
  const [loading, setLoading] = useState(true);
  const [probingAll, setProbingAll] = useState(false);
  const [snapshotModes, setSnapshotModes] = useState({}); // { [device_id]: 'live' | 'baseline' }
  const [snapshotUrls, setSnapshotUrls] = useState({});   // { [device_id]: url }
  const [otaUploadingId, setOtaUploadingId] = useState(null);

  // Add / Provision / Edit NVS Modal State
  const [modalOpen, setModalOpen] = useState(false);
  const [probingCurrentIp, setProbingCurrentIp] = useState(false);
  const [savingProvision, setSavingProvision] = useState(false);
  const [formState, setFormState] = useState({
    id: null,
    current_ip: '192.168.0.101',
    device_id: 'ESP32_R1_C1_CAM1',
    camera_name: 'R1-C1 Back Angle (CAM 1)',
    rack_id: 'RACK_01',
    shelf_id: 'SHELF_R1_C1',
    angle_index: 1,
    ip_octet: 101,
    rssi_dbm: null,
  });

  const notify = (msg, type = 'info') => {
    if (onNotify) onNotify(msg, type);
    else console.log(`[Esp32CameraManager] [${type}] ${msg}`);
  };

  const loadCameraList = async () => {
    setLoading(true);
    try {
      const list = await getCameras();
      setCameras(list);
    } catch (err) {
      notify(`Failed to load ESP32 cameras: ${err.message}`, 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadCameraList();
  }, []);

  // Group cameras by shelf_id (Angle 1 & Angle 2 side-by-side per compartment)
  const groupedByShelf = useMemo(() => {
    const groups = {};
    cameras.forEach((cam) => {
      const sId = cam.shelf_id || 'UNASSIGNED';
      if (!groups[sId]) groups[sId] = [];
      groups[sId].push(cam);
    });
    Object.keys(groups).forEach((sId) => {
      groups[sId].sort((a, b) => (a.angle_index || 1) - (b.angle_index || 1));
    });
    return groups;
  }, [cameras]);

  // Probe all configured cameras in parallel
  const handleProbeAllCameras = async () => {
    setProbingAll(true);
    notify('Probing all ESP32-CAM nodes over HTTP...', 'info');
    try {
      const updated = await Promise.all(
        cameras.map(async (cam) => {
          try {
            const live = await probeCameraStatus(cam.ip_address);
            const merged = {
              ...cam,
              status: 'ONLINE',
              rssi_dbm: live.rssi_dbm,
              last_seen: new Date().toISOString(),
            };
            await upsertCamera(merged);
            return merged;
          } catch {
            return { ...cam, status: 'OFFLINE' };
          }
        })
      );
      setCameras(updated);
      const onlineCount = updated.filter((c) => c.status === 'ONLINE').length;
      notify(`Probe complete: ${onlineCount}/${updated.length} ESP32-CAM nodes online.`, onlineCount > 0 ? 'success' : 'info');
    } finally {
      setProbingAll(false);
    }
  };

  // Toggle Live vs Baseline snapshot mode and refresh image
  const handleToggleSnapshotMode = (cam, mode) => {
    setSnapshotModes((prev) => ({ ...prev, [cam.device_id]: mode }));
    const url = captureLiveSnapshotUrl(cam.ip_address, mode);
    setSnapshotUrls((prev) => ({ ...prev, [cam.device_id]: url }));
  };

  // Test Trigger button
  const handleTestTrigger = async (cam) => {
    const mode = snapshotModes[cam.device_id] || 'live';
    const snapUrl = captureLiveSnapshotUrl(cam.ip_address, mode);
    setSnapshotUrls((prev) => ({ ...prev, [cam.device_id]: snapUrl }));

    // Require BOTH Angle 1 (Back) and Angle 2 (Front) cameras to be configured & online for dual-angle validation
    const shelfCams = groupedByShelf[cam.shelf_id] || [];
    const cam1 = shelfCams.find((c) => Number(c.angle_index) === 1);
    const cam2 = shelfCams.find((c) => Number(c.angle_index) === 2);
    if (!cam1 || !cam2 || cam1.status === 'OFFLINE' || cam2.status === 'OFFLINE') {
      notify('Validation aborted: Dual-angle required (1 camera unreachable)', 'error');
      return;
    }

    try {
      await fetch(`${bridgeUrl.replace(/\/$/, '')}/esp32_trigger`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          shelf_id: cam.shelf_id,
          command: 'CAPTURE_STATE',
          expected_action: 'BORROW',
          source: 'NODE_MANAGER_TEST',
        }),
      });
      notify(`Triggered capture & MQTT state check for ${cam.device_id} (${cam.ip_address})`, 'success');
    } catch {
      notify(`Refreshed direct HTTP snapshot from ${cam.ip_address}`, 'info');
    }
  };

  // Open Modal for Add or Edit/Reassign NVS
  const openProvisionModal = (cam = null) => {
    if (cam) {
      const lastOctet = parseInt(String(cam.ip_address || '192.168.0.101').split('.').pop(), 10) || 101;
      setFormState({
        id: cam.id || null,
        current_ip: cam.ip_address || '192.168.0.101',
        device_id: cam.device_id || 'ESP32_R1_C1_CAM1',
        camera_name: cam.camera_name || `${cam.shelf_id} Angle ${cam.angle_index || 1}`,
        rack_id: cam.rack_id || 'RACK_01',
        shelf_id: cam.shelf_id || 'SHELF_R1_C1',
        angle_index: cam.angle_index || 1,
        ip_octet: lastOctet,
        rssi_dbm: cam.rssi_dbm,
      });
    } else {
      setFormState({
        id: null,
        current_ip: '192.168.0.101',
        device_id: 'ESP32_R1_C1_CAM1',
        camera_name: 'R1-C1 Back Angle (CAM 1)',
        rack_id: 'RACK_01',
        shelf_id: 'SHELF_R1_C1',
        angle_index: 1,
        ip_octet: 101,
        rssi_dbm: null,
      });
    }
    setModalOpen(true);
  };

  // Auto-generate device_id and ip_octet when shelf_id or angle_index changes in modal
  const handleShelfOrAngleChange = (newShelf, newAngle) => {
    const shelfIdx = SHELF_OPTIONS.indexOf(newShelf);
    const computedOctet = shelfIdx >= 0 ? 101 + shelfIdx * 2 + (Number(newAngle) === 2 ? 1 : 0) : formState.ip_octet;
    const shortShelf = newShelf.replace('SHELF_', '');
    const computedDeviceId = `ESP32_${shortShelf}_CAM${newAngle}`;
    const angleLabel = Number(newAngle) === 2 ? 'Front Angle' : 'Back Angle';
    const camNum = computedOctet - 100;

    setFormState((prev) => ({
      ...prev,
      shelf_id: newShelf,
      angle_index: Number(newAngle),
      device_id: computedDeviceId,
      camera_name: `${shortShelf.replace('_', '-')} ${angleLabel} (CAM ${camNum})`,
      ip_octet: computedOctet,
    }));
  };

  // Probe Current IP inside Modal
  const handleProbeInModal = async () => {
    setProbingCurrentIp(true);
    try {
      const status = await probeCameraStatus(formState.current_ip);
      setFormState((prev) => ({
        ...prev,
        rssi_dbm: status.rssi_dbm,
      }));
      notify(`Connected to ${status.ip_address}: ${status.device_id} (${status.rssi_dbm} dBm)`, 'success');
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setProbingCurrentIp(false);
    }
  };

  // Provision & Save (probe -> push NVS -> upsert Supabase)
  const handleProvisionAndSave = async (e) => {
    e.preventDefault();
    setSavingProvision(true);

    const subnetParts = String(formState.current_ip || '192.168.0.101').trim().split('.');
    const prefix = subnetParts.length === 4 ? subnetParts.slice(0, 3).join('.') : '192.168.0';
    const targetIp = `${prefix}.${formState.ip_octet}`;

    let liveRssi = formState.rssi_dbm;
    let onlineStatus = 'OFFLINE';

    // 1. Probe Current IP
    try {
      const probeRes = await probeCameraStatus(formState.current_ip);
      liveRssi = probeRes.rssi_dbm;
      onlineStatus = 'ONLINE';
    } catch (probeErr) {
      notify(`Note: ${probeErr.message}. Saving configuration to Supabase anyway.`, 'warning');
    }

    // 2. Push NVS Config to ESP32-CAM if reachable
    if (onlineStatus === 'ONLINE') {
      try {
        await pushCameraConfig(formState.current_ip, {
          device_id: formState.device_id,
          rack_id: formState.rack_id,
          shelf_id: formState.shelf_id,
          camera_name: formState.camera_name,
          ip_octet: Number(formState.ip_octet),
        });
        notify(`Pushed NVS identity (${formState.device_id} -> ${targetIp}) & rebooting ESP32-CAM!`, 'success');
      } catch (nvsErr) {
        notify(`NVS push skipped (${nvsErr.message}); saving record to database.`, 'warning');
      }
    }

    // 3. Save to Supabase esp32_cameras
    try {
      const saved = await upsertCamera({
        id: formState.id,
        device_id: formState.device_id,
        camera_name: formState.camera_name,
        rack_id: formState.rack_id,
        shelf_id: formState.shelf_id,
        angle_index: Number(formState.angle_index),
        ip_address: targetIp,
        status: onlineStatus,
        rssi_dbm: liveRssi,
        last_seen: new Date().toISOString(),
      });

      setCameras((prev) => {
        const exists = prev.some((c) => c.device_id === saved.device_id || (saved.id && c.id === saved.id));
        if (exists) {
          return prev.map((c) => (c.device_id === saved.device_id || (saved.id && c.id === saved.id) ? saved : c));
        }
        return [...prev, saved];
      });

      // Notify local bridge so Panel 1 & Panel 2 stay in sync
      try {
        const shelfCams = cameras.filter((c) => c.shelf_id === formState.shelf_id);
        const cam1Node = shelfCams.find((c) => Number(c.angle_index) === 1);
        const cam2Node = shelfCams.find((c) => Number(c.angle_index) === 2);
        await fetch(`${bridgeUrl.replace(/\/$/, '')}/esp32_assign_ips`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            shelf_id: formState.shelf_id,
            ip_address: Number(formState.angle_index) === 1 ? targetIp : (cam1Node ? cam1Node.ip_address : undefined),
            secondary_ip_address: Number(formState.angle_index) === 2 ? targetIp : (cam2Node ? cam2Node.ip_address : undefined),
          }),
        });
      } catch {}

      setModalOpen(false);
      notify(`Provisioned & saved ${saved.device_id} at ${saved.ip_address}`, 'success');
    } catch (saveErr) {
      notify(`Failed to save camera: ${saveErr.message}`, 'error');
    } finally {
      setSavingProvision(false);
    }
  };

  // Wireless OTA (.bin) upload handler
  const handleOtaFileSelect = async (cam, event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;

    setOtaUploadingId(cam.device_id);
    notify(`Uploading OTA firmware (${file.name}) to ${cam.device_id} (${cam.ip_address})...`, 'info');
    try {
      await uploadCameraFirmware(cam.ip_address, file);
      notify(`Wireless OTA complete for ${cam.device_id}! Node is rebooting.`, 'success');
    } catch (err) {
      notify(`OTA failed for ${cam.device_id}: ${err.message}`, 'error');
    } finally {
      setOtaUploadingId(null);
      event.target.value = '';
    }
  };

  // Delete Camera Node
  const handleDeleteCamera = async (cam) => {
    await deleteCamera(cam.id || cam.device_id);
    setCameras((prev) => prev.filter((c) => c.device_id !== cam.device_id));
    notify(`Deleted camera node ${cam.device_id}`, 'info');
  };

  return (
    <section className="panel" style={{ marginTop: 24 }}>
      <div className="panel-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 className="panel-title">📡 ESP32-CAM Hardware Node CRUD, NVS Provisioning &amp; Wireless OTA Manager (12 Nodes)</h2>
          <p className="panel-desc">
            Manage up to 12 ESP32-CAM compartment nodes (Angle 1 Back &amp; Angle 2 Front per shelf), push NVS identity over Wi-Fi, and upload <code>.bin</code> firmware via Wireless OTA.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <button type="button" className="btn-action" onClick={handleProbeAllCameras} disabled={probingAll}>
            {probingAll ? '🔍 Probing All...' : '🔍 Probe All 12 Nodes'}
          </button>
          <button type="button" className="btn-primary" onClick={() => openProvisionModal(null)}>
            ➕ Add / Provision Camera Node
          </button>
        </div>
      </div>

      {loading ? (
        <div style={{ textAlign: 'center', padding: 32, color: 'var(--text-muted)' }}>Loading ESP32-CAM hardware nodes...</div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(460px, 1fr))', gap: 18 }}>
          {Object.entries(groupedByShelf).map(([shelfId, shelfCams]) => (
            <div
              key={shelfId}
              style={{
                background: 'var(--surface-card)',
                border: '1px solid var(--border)',
                borderRadius: 10,
                padding: 16,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>
                <span style={{ fontWeight: 700, fontSize: 15, color: 'var(--primary)' }}>📦 Compartment: {shelfId}</span>
                <span style={{ fontSize: 12, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                  {shelfCams.length} / 2 Angles Configured
                </span>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                {shelfCams.map((cam) => {
                  const isOnline = cam.status === 'ONLINE';
                  const currentMode = snapshotModes[cam.device_id] || 'live';
                  const snapUrl = snapshotUrls[cam.device_id];

                  return (
                    <div
                      key={cam.device_id}
                      style={{
                        background: '#0b1324',
                        border: `1px solid ${isOnline ? 'rgba(16, 185, 129, 0.45)' : 'var(--border)'}`,
                        borderRadius: 8,
                        padding: 12,
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 8,
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 6 }}>
                        <div>
                          <div style={{ fontWeight: 700, fontSize: 13 }}>
                            Angle {cam.angle_index}: {cam.angle_index === 2 ? 'Front' : 'Back'}
                          </div>
                          <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                            {cam.device_id} • {cam.ip_address}
                          </div>
                        </div>
                        <span className={`badge ${isOnline ? 'badge-online' : 'badge-offline'}`} style={{ fontSize: 10 }}>
                          {isOnline ? `ONLINE ${cam.rssi_dbm != null ? `(${cam.rssi_dbm}dBm)` : ''}` : 'OFFLINE'}
                        </span>
                      </div>

                      {/* Live / Baseline Snapshot Toggle */}
                      <div style={{ display: 'flex', gap: 6 }}>
                        <button
                          type="button"
                          className="btn-action"
                          style={{
                            flex: 1,
                            fontSize: 10,
                            padding: '4px 6px',
                            justifyContent: 'center',
                            borderColor: currentMode === 'live' ? 'var(--primary)' : 'var(--border)',
                          }}
                          onClick={() => handleToggleSnapshotMode(cam, 'live')}
                        >
                          📷 Live Feed
                        </button>
                        <button
                          type="button"
                          className="btn-action"
                          style={{
                            flex: 1,
                            fontSize: 10,
                            padding: '4px 6px',
                            justifyContent: 'center',
                            borderColor: currentMode === 'baseline' ? 'var(--primary)' : 'var(--border)',
                          }}
                          onClick={() => handleToggleSnapshotMode(cam, 'baseline')}
                        >
                          ⏱ 30s Baseline
                        </button>
                      </div>

                      {/* Snapshot Window */}
                      <div
                        style={{
                          height: 125,
                          background: '#050811',
                          borderRadius: 6,
                          border: '1px solid var(--border)',
                          overflow: 'hidden',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                        }}
                      >
                        {snapUrl ? (
                          <img src={snapUrl} alt={cam.device_id} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                        ) : (
                          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Click Live/Baseline or Test Trigger</span>
                        )}
                      </div>

                      {/* Node Action Buttons */}
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginTop: 4 }}>
                        <button type="button" className="btn-action" style={{ fontSize: 11, padding: '5px 8px', justifyContent: 'center' }} onClick={() => handleTestTrigger(cam)}>
                          ⚡ Test Trigger
                        </button>
                        <button type="button" className="btn-action" style={{ fontSize: 11, padding: '5px 8px', justifyContent: 'center' }} onClick={() => openProvisionModal(cam)}>
                          ⚙ Edit / NVS
                        </button>
                        <label
                          className="btn-action"
                          style={{
                            fontSize: 11,
                            padding: '5px 8px',
                            justifyContent: 'center',
                            cursor: 'pointer',
                            textAlign: 'center',
                            margin: 0,
                          }}
                        >
                          {otaUploadingId === cam.device_id ? '⏳ Uploading...' : '📦 OTA (.bin)'}
                          <input
                            type="file"
                            accept=".bin"
                            style={{ display: 'none' }}
                            disabled={otaUploadingId === cam.device_id}
                            onChange={(e) => handleOtaFileSelect(cam, e)}
                          />
                        </label>
                        <button
                          type="button"
                          className="btn-action"
                          style={{ fontSize: 11, padding: '5px 8px', justifyContent: 'center', color: 'var(--danger)', borderColor: 'rgba(244,63,94,0.35)' }}
                          onClick={() => handleDeleteCamera(cam)}
                        >
                          🗑 Delete
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Add / Provision Camera Modal */}
      {modalOpen && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(5, 8, 17, 0.82)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 9999,
            padding: 16,
          }}
        >
          <form
            onSubmit={handleProvisionAndSave}
            style={{
              background: 'var(--surface)',
              border: '1px solid var(--primary)',
              borderRadius: 12,
              padding: 24,
              width: '100%',
              maxWidth: 480,
              display: 'flex',
              flexDirection: 'column',
              gap: 12,
            }}
          >
            <h3 style={{ margin: 0, fontSize: 16 }}>📡 Add / Provision ESP32-CAM Node (Wireless NVS)</h3>

            <div className="form-group">
              <label>Current Camera IP Address (to Probe &amp; Push Config)</label>
              <div style={{ display: 'flex', gap: 8 }}>
                <input
                  type="text"
                  className="form-control"
                  value={formState.current_ip}
                  onChange={(e) => setFormState({ ...formState, current_ip: e.target.value })}
                  required
                />
                <button type="button" className="btn-action" onClick={handleProbeInModal} disabled={probingCurrentIp}>
                  {probingCurrentIp ? 'Probing...' : '🔍 Probe'}
                </button>
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <div className="form-group">
                <label>Target Shelf ID</label>
                <select
                  className="form-control"
                  value={formState.shelf_id}
                  onChange={(e) => handleShelfOrAngleChange(e.target.value, formState.angle_index)}
                >
                  {SHELF_OPTIONS.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>

              <div className="form-group">
                <label>Angle Index (1=Back, 2=Front)</label>
                <select
                  className="form-control"
                  value={formState.angle_index}
                  onChange={(e) => handleShelfOrAngleChange(formState.shelf_id, Number(e.target.value))}
                >
                  <option value={1}>Angle 1 — Back Camera</option>
                  <option value={2}>Angle 2 — Front Camera</option>
                </select>
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <div className="form-group">
                <label>Target Device ID</label>
                <input
                  type="text"
                  className="form-control"
                  value={formState.device_id}
                  onChange={(e) => setFormState({ ...formState, device_id: e.target.value })}
                  required
                />
              </div>

              <div className="form-group">
                <label>Target Static IP Octet (101–112)</label>
                <input
                  type="number"
                  min={101}
                  max={250}
                  className="form-control"
                  value={formState.ip_octet}
                  onChange={(e) => setFormState({ ...formState, ip_octet: Number(e.target.value) })}
                  required
                />
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <div className="form-group">
                <label>Rack ID</label>
                <input
                  type="text"
                  className="form-control"
                  value={formState.rack_id}
                  onChange={(e) => setFormState({ ...formState, rack_id: e.target.value })}
                  required
                />
              </div>
              <div className="form-group">
                <label>Camera Label</label>
                <input
                  type="text"
                  className="form-control"
                  value={formState.camera_name}
                  onChange={(e) => setFormState({ ...formState, camera_name: e.target.value })}
                  required
                />
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 8 }}>
              <button type="button" className="btn-action" onClick={() => setModalOpen(false)}>
                Cancel
              </button>
              <button type="submit" className="btn-primary" disabled={savingProvision}>
                {savingProvision ? 'Provisioning...' : '⚡ Provision NVS & Save to Supabase'}
              </button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}

