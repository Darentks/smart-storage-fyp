"""
Configuration and Supabase Ingestion & CRUD Loader for Smart Storage Vision Validation Module
Author: Daren Tan Khai Shuen
Handles:
1. Dynamic retrieval of active CCTV camera RTSP streams from Supabase ('cameras' table).
2. Runtime compartment telemetry, 30s baseline tracking & OpenCV inventory delta logs.
3. Tested ESP32-CAM reference sample datasets ('esp32_reference_samples' table).
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union
from dotenv import load_dotenv

if TYPE_CHECKING:
    from supabase import Client

DEFAULT_SUPABASE_URL = "https://hrufeowqpnyjfwxydocm.supabase.co"
DEFAULT_SUPABASE_KEY = "sb_publishable_zGEkwPhOLtDiNzSJXhB0wA_WDrgr0Gt"

# Lightweight non-Base64 column lists to prevent Supabase Egress bloat
COMPARTMENTS_LIGHT_COLS = (
    "shelf_id,rack_id,mqtt_client_id,camera_name,ip_address,secondary_ip_address,"
    "secondary_mqtt_client_id,current_quantity,max_capacity,baseline_updated_at,"
    "tm_model_url,status,rssi_dbm,free_heap_bytes,last_seen_at,created_at"
)
COMPARISON_LOGS_LIGHT_COLS = (
    "log_id,shelf_id,mqtt_client_id,trigger_action,diff_score,similarity_score,"
    "matched_box_count,scenario_label,state_delta,quantity_before,quantity_after,created_at"
)

try:
    from supabase import create_client
    _SUPABASE_AVAILABLE = True
except ImportError:
    create_client = None  # type: ignore
    _SUPABASE_AVAILABLE = False

_client_cache: Optional[Any] = None


def _supabase_rest(
    method: str = "GET",
    table: str = "",
    query: str = "",
    payload: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
    prefer: Optional[str] = "return=representation",
    timeout: float = 8.0,
) -> Any:
    """
    Native Python urllib.request PostgREST helper that calls:
    {supabase_url}/rest/v1/{table}{query}
    with headers 'apikey', 'Authorization: Bearer', 'Content-Type: application/json', and 'Prefer'.
    Returns parsed JSON data (or True/False for DELETE, or [] on error).
    """
    base_url = get_resolved_supabase_url()
    api_key = get_resolved_supabase_key()

    if query and not query.startswith("?"):
        query = "?" + query

    clean_table = table.lstrip("/")
    url = f"{base_url}/rest/v1/{clean_table}{query}"

    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer

    data_bytes = None
    if payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 204:
                return True
            raw_text = resp.read().decode("utf-8")
            return json.loads(raw_text) if raw_text else []
    except Exception as exc:
        print(f"[_supabase_rest] {method.upper()} on '{table}' failed: {exc}")
        return False if method.upper() == "DELETE" else []


# Alias for backward compatibility
_supabase_rest_request = _supabase_rest


class SupabaseRestFallback:
    """
    Lightweight REST fallback using urllib.request and json to directly communicate
    with Supabase PostgREST API ({SUPABASE_URL}/rest/v1/{table}).
    Ensures database reads and writes succeed even if supabase-py is not installed or
    throws auth format exceptions (e.g. sb_publishable_ keys).
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, timeout: float = 8.0):
        self.base_url = (base_url or get_resolved_supabase_url()).rstrip("/")
        self.api_key = api_key or get_resolved_supabase_key()
        self.timeout = timeout

    def select(self, table: str, query_params: str = "?select=*") -> List[Dict[str, Any]]:
        res = _supabase_rest(
            method="GET",
            table=table,
            query=query_params,
            timeout=self.timeout,
        )
        return res if isinstance(res, list) else []

    def insert(self, table: str, payload: Union[Dict[str, Any], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        res = _supabase_rest(
            method="POST",
            table=table,
            payload=payload,
            prefer="return=representation",
            timeout=self.timeout,
        )
        return res if isinstance(res, list) else []

    def upsert(
        self,
        table: str,
        payload: Union[Dict[str, Any], List[Dict[str, Any]]],
        on_conflict: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = f"?on_conflict={urllib.parse.quote(on_conflict)}" if on_conflict else ""
        res = _supabase_rest(
            method="POST",
            table=table,
            query=query,
            payload=payload,
            prefer="resolution=merge-duplicates,return=representation",
            timeout=self.timeout,
        )
        return res if isinstance(res, list) else []

    def update(self, table: str, query_params: str, payload: Dict[str, Any], prefer: str = "return=minimal") -> List[Dict[str, Any]]:
        res = _supabase_rest(
            method="PATCH",
            table=table,
            query=query_params,
            payload=payload,
            prefer=prefer,
            timeout=self.timeout,
        )
        return res if isinstance(res, list) else []

    def delete(self, table: str, query_params: str) -> bool:
        res = _supabase_rest(
            method="DELETE",
            table=table,
            query=query_params,
            timeout=self.timeout,
        )
        return bool(res)


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


def get_resolved_supabase_url() -> str:
    find_and_load_env()
    url = os.getenv("SUPABASE_URL") or os.getenv("VITE_SUPABASE_URL")
    return (url.strip() if url else DEFAULT_SUPABASE_URL).rstrip("/")


def get_resolved_supabase_key() -> str:
    find_and_load_env()
    key = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("VITE_SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )
    return key.strip() if key else DEFAULT_SUPABASE_KEY


def get_supabase_client() -> Optional[Any]:
    """
    Initializes and returns a cached Supabase Client using environment variables
    or default fallback credentials.
    """
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    if not _SUPABASE_AVAILABLE or create_client is None:
        print("[ConfigLoader] Warning: 'supabase' library is not installed. Operations will use SupabaseRestFallback.")
        return None

    supabase_url = get_resolved_supabase_url()
    supabase_key = get_resolved_supabase_key()

    try:
        _client_cache = create_client(supabase_url, supabase_key)
        return _client_cache
    except Exception as exc:
        print(f"[ConfigLoader] Error initializing Supabase client ({exc}). Will fall back to SupabaseRestFallback.")
        return None


def mask_rtsp_url(url: Union[int, str]) -> str:
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
    """Queries the first active CCTV camera from Supabase table 'cctv_cameras' (or legacy 'cameras')."""
    client = get_supabase_client()
    for table_name in ("cctv_cameras", "cameras"):
        if client is not None:
            try:
                response = (
                    client.table(table_name)
                    .select("*")
                    .eq("is_active", True)
                    .order("created_at", desc=False)
                    .limit(1)
                    .execute()
                )
                if response.data and len(response.data) > 0:
                    return response.data[0]
            except Exception as exc:
                print(f"[ConfigLoader] SDK query on '{table_name}' failed ({exc}), falling back to SupabaseRestFallback.")

        # REST Fallback
        fallback = SupabaseRestFallback()
        records = fallback.select(table_name, "?select=*&is_active=eq.true&order=created_at.asc&limit=1")
        if records:
            return records[0]

    return None


def get_active_rtsp_url(default_source: Union[int, str] = 0) -> Union[int, str]:
    """Retrieves the RTSP URL of the active CCTV camera from Supabase."""
    print("[ConfigLoader] Ingestion: Resolving active camera feed configuration...")

    camera = get_active_camera_record()
    if camera and camera.get("rtsp_url"):
        rtsp_url = str(camera["rtsp_url"]).strip()
        cam_name = camera.get("camera_name") or camera.get("name") or "Active Camera"
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
    from the Supabase 'compartments' table ordered by shelf_id (excluding Base64 columns).
    """
    client = get_supabase_client()
    if client is not None:
        try:
            resp = client.table("compartments").select(COMPARTMENTS_LIGHT_COLS).order("shelf_id", desc=False).execute()
            return resp.data or []
        except Exception as exc:
            print(f"[ConfigLoader] Warning: SDK read 'compartments' failed ({exc}), trying SupabaseRestFallback.")

    fallback = SupabaseRestFallback()
    return fallback.select("compartments", f"?select={COMPARTMENTS_LIGHT_COLS}&order=shelf_id.asc")


def update_esp32_health_in_supabase(node_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Updates live health status, ip_address / secondary_ip_address, rssi_dbm, free_heap_bytes, and last_seen_at
    in 'public.compartments' whenever a 30-second MQTT heartbeat arrives on 'iotlab/storage/health'.
    Identifies the compartment by shelf_id or mqtt_client_id.
    """
    device_id = str(node_payload.get("device_id") or node_payload.get("mqtt_client_id") or "").strip()
    shelf_id = str(node_payload.get("shelf_id") or "").strip()

    # If shelf_id is missing, attempt to derive it from device_id (e.g. ESP32_R1_C1 or ESP32_R1_C1_CAM2)
    if not shelf_id and device_id:
        clean_dev = device_id.upper().replace("_CAM1", "").replace("_CAM2", "")
        if clean_dev.startswith("ESP32_"):
            shelf_id = f"SHELF_{clean_dev.replace('ESP32_', '')}"

    if not device_id and not shelf_id:
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    raw_status = str(node_payload.get("status", "ONLINE")).upper()
    normalized_status = "ONLINE" if raw_status in ["ALIVE", "ONLINE"] else raw_status

    update_fields: Dict[str, Any] = {
        "status": normalized_status,
        "last_seen_at": now_iso,
    }

    raw_ip = str(node_payload.get("ip_address") or node_payload.get("ip") or "").strip()

    # Solution 3: Separate CAM1 vs. CAM2 IP columns on MQTT heartbeat
    is_cam2 = device_id.upper().endswith("_CAM2")
    if is_cam2:
        if raw_ip:
            update_fields["secondary_ip_address"] = raw_ip
        if device_id:
            update_fields["secondary_mqtt_client_id"] = device_id
    else:
        if raw_ip:
            update_fields["ip_address"] = raw_ip
        if device_id:
            update_fields["mqtt_client_id"] = device_id

    if "rssi_dbm" in node_payload and node_payload["rssi_dbm"] is not None:
        try:
            update_fields["rssi_dbm"] = int(node_payload["rssi_dbm"])
        except (ValueError, TypeError):
            pass
    if "free_heap_bytes" in node_payload and node_payload["free_heap_bytes"] is not None:
        try:
            update_fields["free_heap_bytes"] = int(node_payload["free_heap_bytes"])
        except (ValueError, TypeError):
            pass

    client = get_supabase_client()
    if client is not None:
        try:
            query = client.table("compartments").update(update_fields)
            if shelf_id:
                query = query.eq("shelf_id", shelf_id)
            elif is_cam2:
                query = query.eq("secondary_mqtt_client_id", device_id)
            elif device_id:
                query = query.eq("mqtt_client_id", device_id)

            resp = query.execute()
            if resp.data and len(resp.data) > 0:
                return resp.data[0]

            # Auto-insert if node row doesn't exist yet (primary node only)
            if shelf_id and not is_cam2:
                insert_fields = dict(update_fields)
                insert_fields["shelf_id"] = shelf_id
                insert_fields["mqtt_client_id"] = device_id or f"ESP32_{shelf_id.replace('SHELF_', '')}"
                insert_fields.setdefault("rack_id", node_payload.get("rack_id", "RACK_01"))
                insert_fields.setdefault("camera_name", node_payload.get("camera_name", f"ESP32 Node ({shelf_id})"))
                insert_fields.setdefault("current_quantity", 4)
                insert_fields.setdefault("max_capacity", 4)
                insert_resp = client.table("compartments").insert(insert_fields).execute()
                return insert_resp.data[0] if insert_resp.data else None

            return None
        except Exception as exc:
            print(f"[ConfigLoader] Health telemetry SDK sync skipped ({exc}), trying SupabaseRestFallback.")

    # REST Fallback
    fallback = SupabaseRestFallback()
    if shelf_id:
        query_param = f"?shelf_id=eq.{urllib.parse.quote(str(shelf_id))}"
    elif is_cam2:
        query_param = f"?secondary_mqtt_client_id=eq.{urllib.parse.quote(str(device_id))}"
    else:
        query_param = f"?mqtt_client_id=eq.{urllib.parse.quote(str(device_id))}"

    updated = fallback.update("compartments", query_param, update_fields, prefer="return=minimal")
    if updated:
        return updated[0]

    if shelf_id and not is_cam2:
        insert_fields = dict(update_fields)
        insert_fields["shelf_id"] = shelf_id
        insert_fields["mqtt_client_id"] = device_id or f"ESP32_{shelf_id.replace('SHELF_', '')}"
        insert_fields.setdefault("rack_id", node_payload.get("rack_id", "RACK_01"))
        insert_fields.setdefault("camera_name", node_payload.get("camera_name", f"ESP32 Node ({shelf_id})"))
        insert_fields.setdefault("current_quantity", 4)
        insert_fields.setdefault("max_capacity", 4)
        inserted = fallback.insert("compartments", insert_fields)
        return inserted[0] if inserted else None

    return None


# =============================================================================
# SUPABASE OPERATIONS: 30s BASELINE & TEACHABLE MACHINE INVENTORY DELTA
# =============================================================================

def update_compartment_baseline_in_supabase(
    shelf_id: str,
    baseline_image_url: str,
    ip_address: Optional[str] = None,
    baseline_image_2_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Updates baseline_updated_at = now(), status = 'ONLINE', and optionally ip_address
    in public.compartments using Prefer: return=minimal (never uploads Base64 strings).
    """
    shelf_id = shelf_id.strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    update_data: Dict[str, Any] = {
        "baseline_updated_at": now_iso,
        "status": "ONLINE",
        "last_seen_at": now_iso,
    }
    if ip_address:
        update_data["ip_address"] = str(ip_address).strip()

    _supabase_rest(
        method="PATCH",
        table="compartments",
        query=f"?shelf_id=eq.{urllib.parse.quote(shelf_id)}",
        payload=update_data,
        prefer="return=minimal",
    )
    return update_data


def apply_inventory_delta_in_supabase(
    shelf_id: str,
    mqtt_client_id: str,
    trigger_action: str,
    comparison_result: Dict[str, Any],
    current_quantity_hint: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Applies inventory deduction (-1 / +1) in public.compartments ONLY when delta != 0,
    and inserts a lightweight row (without Base64 images) into public.comparison_logs.
    """
    shelf_id = shelf_id.strip()
    mqtt_client_id = mqtt_client_id.strip()
    delta = int(comparison_result.get("delta", 0))
    state_delta = str(comparison_result.get("state_delta", "NO_CHANGE"))
    diff_score = comparison_result.get("diff_score")
    similarity_score = comparison_result.get("similarity_score")
    matched_box_count = comparison_result.get("matched_box_count")

    if matched_box_count is None:
        return None

    # Skip writing NO_CHANGE / intention-only checks to Supabase
    if delta == 0 or state_delta in ("NO_CHANGE", "SKIPPED_NO_TM"):
        return None

    fallback = SupabaseRestFallback()
    if current_quantity_hint is not None:
        quantity_before = int(current_quantity_hint)
    else:
        quantity_before = 4
        records = fallback.select(
            "compartments",
            f"?select=current_quantity&shelf_id=eq.{urllib.parse.quote(shelf_id)}&limit=1",
        )
        if records:
            quantity_before = int(records[0].get("current_quantity", 4))

    quantity_after = max(0, min(4, int(matched_box_count)))
    if quantity_after == quantity_before:
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    derived_action = "BORROW" if quantity_after < quantity_before else "RETURN"

    update_data: Dict[str, Any] = {
        "current_quantity": quantity_after,
        "baseline_updated_at": now_iso,
        "last_seen_at": now_iso,
        "status": "ONLINE",
    }
    _supabase_rest(
        method="PATCH",
        table="compartments",
        query=f"?shelf_id=eq.{urllib.parse.quote(shelf_id)}",
        payload=update_data,
        prefer="return=minimal",
    )
    print(f"[ConfigLoader] Updated {shelf_id} inventory: {quantity_before} -> {quantity_after} ({state_delta}).")

    scenario_label = comparison_result.get("matched_scenario_label") or comparison_result.get("scenario_label")
    db_log_record: Dict[str, Any] = {
        "shelf_id": shelf_id,
        "mqtt_client_id": mqtt_client_id,
        "trigger_action": derived_action,
        "before_image_url": None,
        "after_image_url": None,
        "diff_score": float(diff_score) if diff_score is not None else None,
        "similarity_score": float(similarity_score) if similarity_score is not None else None,
        "matched_box_count": int(matched_box_count),
        "scenario_label": scenario_label,
        "state_delta": state_delta,
        "quantity_before": quantity_before,
        "quantity_after": quantity_after,
        "created_at": now_iso,
    }
    _supabase_rest(
        method="POST",
        table="comparison_logs",
        payload=db_log_record,
        prefer="return=minimal",
    )

    local_log = dict(db_log_record)
    local_log["before_image_url"] = comparison_result.get("before_image_url") or comparison_result.get("before_image")
    local_log["after_image_url"] = comparison_result.get("after_image_url") or comparison_result.get("after_image")
    return local_log


def list_comparison_logs(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Fetches recent rows from public.comparison_logs ordered by created_at desc (excluding Base64 columns).
    """
    safe_limit = min(max(1, int(limit)), 10)
    client = get_supabase_client()
    if client is not None:
        try:
            resp = (
                client.table("comparison_logs")
                .select(COMPARISON_LOGS_LIGHT_COLS)
                .neq("state_delta", "NO_CHANGE")
                .order("created_at", desc=True)
                .limit(safe_limit)
                .execute()
            )
            return resp.data or []
        except Exception as exc:
            print(f"[ConfigLoader] Warning: SDK read 'comparison_logs' failed ({exc}), trying SupabaseRestFallback.")

    fallback = SupabaseRestFallback()
    return fallback.select(
        "comparison_logs",
        f"?select={COMPARISON_LOGS_LIGHT_COLS}&state_delta=neq.NO_CHANGE&order=created_at.desc&limit={safe_limit}",
    )
