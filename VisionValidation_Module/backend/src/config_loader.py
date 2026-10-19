"""
Configuration and Supabase Ingestion & CRUD Loader for Smart Storage Vision Validation Module
Author: Daren Tan Khai Shuen
Handles:
1. Dynamic retrieval of active CCTV camera RTSP streams from Supabase ('cameras' table).
2. Runtime compartment telemetry, 30s baseline tracking & OpenCV inventory delta logs.
3. Tested ESP32-CAM reference sample datasets ('esp32_reference_samples' table).
"""

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
from dotenv import load_dotenv

try:
    from supabase import Client, create_client
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False
    Client = Any  # type: ignore

_client_cache: Optional[Client] = None


def find_and_load_env() -> Optional[str]:
    """
    Locates and loads the project .env file from common search locations.
    Handles potential leading whitespace in .env keys.
    :return: Path to loaded .env file, or None if not found
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    candidate_paths = [
        os.path.join(os.getcwd(), ".env"),
        os.path.abspath(os.path.join(current_dir, "..", ".env")),
        os.path.abspath(os.path.join(current_dir, "..", "..", ".env")),
        os.path.abspath(os.path.join(current_dir, "..", "..", "..", ".env")),
    ]

    loaded_path = None
    for path in candidate_paths:
        if os.path.isfile(path):
            load_dotenv(dotenv_path=path, override=False)
            loaded_path = path
            break

    if loaded_path and (not os.getenv("VITE_SUPABASE_URL") and not os.getenv("SUPABASE_URL")):
        try:
            with open(loaded_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

    return loaded_path


def get_supabase_client() -> Optional[Client]:
    """
    Initializes and returns a cached Supabase Client using environment variables.
    """
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    if not _SUPABASE_AVAILABLE:
        print("[ConfigLoader] Warning: 'supabase' library is not installed. Run 'pip install supabase'.")
        return None

    find_and_load_env()

    supabase_url = os.getenv("SUPABASE_URL") or os.getenv("VITE_SUPABASE_URL")
    supabase_key = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("VITE_SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )

    if not supabase_url or not supabase_key:
        print("[ConfigLoader] Warning: Missing Supabase credentials in environment (.env).")
        return None

    try:
        _client_cache = create_client(supabase_url.strip(), supabase_key.strip())
        return _client_cache
    except Exception as exc:
        print(f"[ConfigLoader] Error initializing Supabase client: {exc}")
        return None


def mask_rtsp_url(url: str) -> str:
    """Masks password inside RTSP URL for safe terminal logging."""
    if not isinstance(url, str):
        return str(url)
    return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:****@", url)


def build_hikvision_rtsp_url(
    ip_address: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    rtsp_port: int = 554,
    stream_channel: int = 102,
) -> str:
    """Constructs standard Hikvision RTSP stream URL."""
    if username and password:
        auth_part = f"{username}:{password}@"
    elif username:
        auth_part = f"{username}@"
    else:
        auth_part = ""

    return f"rtsp://{auth_part}{ip_address}:{rtsp_port}/Streaming/Channels/{stream_channel}"


def get_active_camera_record() -> Optional[Dict[str, Any]]:
    """Queries the first active CCTV camera from Supabase table 'cameras'."""
    client = get_supabase_client()
    if client is None:
        return None

    try:
        response = (
            client.table("cameras")
            .select("*")
            .eq("is_active", True)
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        if response.data and len(response.data) > 0:
            return response.data[0]
        return None
    except Exception as exc:
        print(f"[ConfigLoader] Failed to query 'cameras' table from Supabase: {exc}")
        return None


def get_active_rtsp_url(default_source: Union[int, str] = 0) -> Union[int, str]:
    """Retrieves the RTSP URL of the active CCTV camera from Supabase."""
    print("[ConfigLoader] Ingestion: Resolving active camera feed configuration...")

    camera = get_active_camera_record()
    if camera and camera.get("rtsp_url"):
        rtsp_url = str(camera["rtsp_url"]).strip()
        cam_name = camera.get("name", "Active Camera")
        masked = mask_rtsp_url(rtsp_url)
        print(f"[ConfigLoader] Successfully fetched active camera '{cam_name}' from Supabase: {masked}")
        return rtsp_url

    print(
        f"[ConfigLoader] No active camera stream found in Supabase (or connection unavailable). "
        f"Falling back to default source: {default_source}"
    )
    return default_source


# =============================================================================
# SUPABASE OPERATIONS: public.compartments & TELEMETRY
# =============================================================================

def list_esp32_cameras() -> List[Dict[str, Any]]:
    """
    READ: Fetches all registered shelf compartments & ESP32 runtime state
    from the Supabase 'compartments' table ordered by shelf_id.
    """
    client = get_supabase_client()
    if client is None:
        return []
    try:
        resp = client.table("compartments").select("*").order("shelf_id", desc=False).execute()
        return resp.data or []
    except Exception as exc:
        print(f"[ConfigLoader] Warning: Could not read 'compartments' table ({exc}).")
        return []


def update_esp32_health_in_supabase(telemetry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Updates live health status, ip_address, rssi_dbm, free_heap_bytes, and last_seen_at
    in 'public.compartments' whenever a 30-second MQTT heartbeat arrives on 'iotlab/storage/health'.
    Identifies the compartment by mqtt_client_id (or shelf_id).
    """
    client = get_supabase_client()
    if client is None:
        return None

    mqtt_client_id = telemetry.get("device_id") or telemetry.get("mqtt_client_id")
    shelf_id = telemetry.get("shelf_id")
    if not mqtt_client_id and not shelf_id:
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    raw_status = str(telemetry.get("status", "ONLINE")).upper()
    normalized_status = "ONLINE" if raw_status in ["ALIVE", "ONLINE"] else raw_status

    update_fields: Dict[str, Any] = {
        "status": normalized_status,
        "last_seen_at": now_iso,
    }

    if "ip_address" in telemetry and telemetry["ip_address"]:
        update_fields["ip_address"] = str(telemetry["ip_address"]).strip()
    if "rssi_dbm" in telemetry and telemetry["rssi_dbm"] is not None:
        try:
            update_fields["rssi_dbm"] = int(telemetry["rssi_dbm"])
        except (ValueError, TypeError):
            pass
    if "free_heap_bytes" in telemetry and telemetry["free_heap_bytes"] is not None:
        try:
            update_fields["free_heap_bytes"] = int(telemetry["free_heap_bytes"])
        except (ValueError, TypeError):
            pass

    try:
        query = client.table("compartments").update(update_fields)
        if mqtt_client_id:
            query = query.eq("mqtt_client_id", mqtt_client_id)
        elif shelf_id:
            query = query.eq("shelf_id", shelf_id)

        resp = query.execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]

        # Auto-insert if node row doesn't exist yet
        if shelf_id and mqtt_client_id:
            update_fields["shelf_id"] = shelf_id
            update_fields["mqtt_client_id"] = mqtt_client_id
            update_fields.setdefault("rack_id", telemetry.get("rack_id", "RACK_01"))
            update_fields.setdefault("camera_name", telemetry.get("camera_name", f"ESP32 Node ({shelf_id})"))
            update_fields.setdefault("current_quantity", 4)
            update_fields.setdefault("max_capacity", 4)
            insert_resp = client.table("compartments").insert(update_fields).execute()
            return insert_resp.data[0] if insert_resp.data else None

        return None
    except Exception as exc:
        print(f"[ConfigLoader] Health telemetry Supabase sync skipped: {exc}")
        return None


# =============================================================================
# SUPABASE OPERATIONS: 30s BASELINE & OPENCV INVENTORY DELTA
# =============================================================================

def update_compartment_baseline_in_supabase(
    shelf_id: str,
    baseline_image_url: str,
    ip_address: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Updates baseline_image_url, baseline_updated_at = now(), status = 'ONLINE',
    and optionally ip_address in public.compartments where shelf_id = shelf_id.
    """
    client = get_supabase_client()
    if client is None:
        return None

    shelf_id = shelf_id.strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    update_data: Dict[str, Any] = {
        "baseline_image_url": baseline_image_url,
        "baseline_updated_at": now_iso,
        "status": "ONLINE",
        "last_seen_at": now_iso,
    }
    if ip_address:
        update_data["ip_address"] = str(ip_address).strip()

    try:
        resp = client.table("compartments").update(update_data).eq("shelf_id", shelf_id).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        return None
    except Exception as exc:
        print(f"[ConfigLoader] Failed to update compartment baseline in Supabase: {exc}")
        return None


def apply_inventory_delta_in_supabase(
    shelf_id: str,
    mqtt_client_id: str,
    trigger_action: str,
    comparison_result: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Applies inventory deduction (-1 / +1) in public.compartments and logs
    the comparison details into public.comparison_logs:
    a) Reads current_quantity from public.compartments for shelf_id (default 4).
    b) Applies delta = int(comparison_result.get("delta", 0)) so
       quantity_after = max(0, min(4, quantity_before + delta)).
    c) If delta != 0, updates current_quantity = quantity_after and
       baseline_image_url = comparison_result.get("after_image") in public.compartments.
    d) Inserts a row into public.comparison_logs and returns the log record.
    """
    client = get_supabase_client()
    if client is None:
        return None

    shelf_id = shelf_id.strip()
    mqtt_client_id = mqtt_client_id.strip()
    delta = int(comparison_result.get("delta", 0))
    state_delta = comparison_result.get("state_delta", "NO_CHANGE")
    diff_score = comparison_result.get("diff_score")
    similarity_score = comparison_result.get("similarity_score")
    matched_box_count = comparison_result.get("matched_box_count")
    before_image_url = comparison_result.get("before_image_url") or comparison_result.get("before_image")
    after_image_url = comparison_result.get("after_image_url") or comparison_result.get("after_image")
    action_clean = trigger_action.upper() if trigger_action.upper() in ["BORROW", "RETURN"] else "BORROW"

    # a) Read current_quantity from public.compartments for shelf_id (default 4)
    quantity_before = 4
    try:
        resp = client.table("compartments").select("current_quantity").eq("shelf_id", shelf_id).limit(1).execute()
        if resp.data and len(resp.data) > 0:
            quantity_before = int(resp.data[0].get("current_quantity", 4))
    except Exception as exc:
        print(f"[ConfigLoader] Warning querying compartments quantity: {exc}")

    # b) Compute quantity_after = max(0, min(4, quantity_before + delta))
    quantity_after = max(0, min(4, quantity_before + delta))

    # c) If delta != 0, update current_quantity & baseline_image_url in public.compartments
    now_iso = datetime.now(timezone.utc).isoformat()
    if delta != 0:
        try:
            update_data: Dict[str, Any] = {
                "current_quantity": quantity_after,
                "last_seen_at": now_iso,
                "status": "ONLINE",
            }
            if after_image_url:
                update_data["baseline_image_url"] = after_image_url
                update_data["baseline_updated_at"] = now_iso

            client.table("compartments").update(update_data).eq("shelf_id", shelf_id).execute()
            print(f"[ConfigLoader] Updated {shelf_id} inventory: {quantity_before} -> {quantity_after} ({state_delta}).")
        except Exception as exc:
            print(f"[ConfigLoader] Failed to update compartment inventory in Supabase: {exc}")

    # d) Insert row into public.comparison_logs
    log_record: Dict[str, Any] = {
        "shelf_id": shelf_id,
        "mqtt_client_id": mqtt_client_id,
        "trigger_action": action_clean,
        "before_image_url": before_image_url,
        "after_image_url": after_image_url,
        "diff_score": float(diff_score) if diff_score is not None else None,
        "similarity_score": float(similarity_score) if similarity_score is not None else None,
        "matched_box_count": int(matched_box_count) if matched_box_count is not None else None,
        "state_delta": state_delta,
        "quantity_before": quantity_before,
        "quantity_after": quantity_after,
        "created_at": now_iso,
    }

    try:
        ins = client.table("comparison_logs").insert(log_record).execute()
        return ins.data[0] if ins.data else log_record
    except Exception as exc:
        print(f"[ConfigLoader] Failed to insert comparison_log in Supabase: {exc}")
        return log_record


def list_comparison_logs(limit: int = 15) -> List[Dict[str, Any]]:
    """
    Fetches recent rows from public.comparison_logs ordered by created_at desc.
    """
    client = get_supabase_client()
    if client is None:
        return []
    try:
        resp = (
            client.table("comparison_logs")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        print(f"[ConfigLoader] Warning: Could not list comparison_logs ({exc}).")
        return []


# =============================================================================
# SUPABASE OPERATIONS: public.esp32_reference_samples
# =============================================================================

def insert_esp32_reference_sample(sample_record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Inserts an ESP32 reference capture sample into public.esp32_reference_samples.
    """
    client = get_supabase_client()
    if client is None:
        return None

    shelf_id = sample_record.get("shelf_id", "SHELF_R1_C1")
    mqtt_client_id = sample_record.get("mqtt_client_id") or f"ESP32_{shelf_id.replace('SHELF_', '')}"
    rack_id = sample_record.get("rack_id", "RACK_01")
    box_count = int(sample_record.get("box_count", 4))
    image_url = sample_record.get("image_url", "")
    sample_label = sample_record.get("sample_label") or f"{shelf_id} - {box_count} Boxes Ref"

    payload: Dict[str, Any] = {
        "shelf_id": shelf_id,
        "mqtt_client_id": mqtt_client_id,
        "rack_id": rack_id,
        "sample_label": sample_label,
        "box_count": max(0, min(4, box_count)),
        "image_url": image_url,
        "flash_enabled": bool(sample_record.get("flash_enabled", True)),
        "slot_states": sample_record.get("slot_states", {"front_top": 1, "back_top": 1, "front_bottom": 1, "back_bottom": 1}),
        "feature_signature": sample_record.get("feature_signature"),
        "capture_latency_ms": sample_record.get("capture_latency_ms"),
    }

    try:
        resp = client.table("esp32_reference_samples").upsert(payload, on_conflict="shelf_id,sample_label").execute()
        return resp.data[0] if resp.data else payload
    except Exception as exc:
        print(f"[ConfigLoader] Failed to upsert esp32_reference_sample: {exc}")
        return None


def list_esp32_reference_samples(shelf_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lists reference samples from public.esp32_reference_samples, optionally filtered by shelf_id.
    """
    client = get_supabase_client()
    if client is None:
        return []
    try:
        query = client.table("esp32_reference_samples").select("*").order("created_at", desc=True)
        if shelf_id:
            query = query.eq("shelf_id", shelf_id.strip())
        resp = query.execute()
        return resp.data or []
    except Exception as exc:
        print(f"[ConfigLoader] Warning: Could not list esp32_reference_samples ({exc}).")
        return []


def delete_esp32_reference_sample(sample_id: str) -> bool:
    """
    Deletes an ESP32 reference sample from public.esp32_reference_samples by sample_id.
    """
    client = get_supabase_client()
    if client is None:
        return False
    try:
        client.table("esp32_reference_samples").delete().eq("sample_id", sample_id).execute()
        return True
    except Exception as exc:
        print(f"[ConfigLoader] Failed to delete esp32_reference_sample ({exc}):")
        return False
