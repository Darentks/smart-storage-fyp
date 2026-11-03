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
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:
        return False

if TYPE_CHECKING:
    from supabase import Client

DEFAULT_SUPABASE_URL = "https://tosvjwxvcuxvgtcmakta.supabase.co"
DEFAULT_SUPABASE_KEY = "sb_publishable_N7bQW1bkTZq6TPVPYlqVag_C7vXKF9l"

# Lightweight non-Base64 column lists to prevent Supabase Egress bloat
# Minimal clean schema: public.compartments only stores inventory state
COMPARTMENTS_LIGHT_COLS = "shelf_id,rack_id,current_quantity,max_capacity,tm_model_url"
# public.esp32_cameras stores the static hardware mapping (IP per angle)
ESP32_CAMERAS_COLS = "id,device_id,camera_name,rack_id,shelf_id,angle_index,ip_address"
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

    if loaded_path and (not os.getenv("NEXT_PUBLIC_SUPABASE_URL") and not os.getenv("VITE_SUPABASE_URL") and not os.getenv("SUPABASE_URL")):
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
    url = os.getenv("NEXT_PUBLIC_SUPABASE_URL") or os.getenv("SUPABASE_URL") or os.getenv("VITE_SUPABASE_URL")
    return (url.strip() if url else DEFAULT_SUPABASE_URL).rstrip("/")


def get_resolved_supabase_key() -> str:
    find_and_load_env()
    key = (
        os.getenv("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_KEY")
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
                    .order("cctv_id", desc=False)
                    .limit(1)
                    .execute()
                )
                if response.data and len(response.data) > 0:
                    return response.data[0]
            except Exception as exc:
                print(f"[ConfigLoader] SDK query on '{table_name}' failed ({exc}), falling back to SupabaseRestFallback.")

        # REST Fallback
        fallback = SupabaseRestFallback()
        records = fallback.select(table_name, "?select=*&is_active=eq.true&order=cctv_id.asc&limit=1")
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

def _clean_host(ip: Any) -> str:
    """Strips scheme, path and PostgreSQL INET CIDR suffixes (e.g. '192.168.0.201/32')."""
    return str(ip or "").strip().split("//")[-1].split("/")[0].strip()


def list_esp32_cameras() -> List[Dict[str, Any]]:
    """
    READ: Fetches shelf compartments (public.compartments) and merges the static camera mapping
    from public.esp32_cameras (angle 1 -> ip_address / mqtt_client_id, angle 2 -> secondary_*).
    Live telemetry (status, rssi, heap, last_seen) is NOT stored in Supabase; it lives in the bridge RAM.
    """
    compartments: List[Dict[str, Any]] = []
    cameras: List[Dict[str, Any]] = []

    client = get_supabase_client()
    if client is not None:
        try:
            compartments = client.table("compartments").select(COMPARTMENTS_LIGHT_COLS).order("shelf_id", desc=False).execute().data or []
            cameras = (
                client.table("esp32_cameras").select(ESP32_CAMERAS_COLS)
                .order("shelf_id", desc=False).order("angle_index", desc=False).execute().data or []
            )
        except Exception as exc:
            print(f"[ConfigLoader] Warning: SDK read failed ({exc}), trying SupabaseRestFallback.")
            compartments, cameras = [], []

    if not compartments:
        fallback = SupabaseRestFallback()
        compartments = fallback.select("compartments", f"?select={COMPARTMENTS_LIGHT_COLS}&order=shelf_id.asc") or []
        cameras = fallback.select(
            "esp32_cameras", f"?select={ESP32_CAMERAS_COLS}&order=shelf_id.asc,angle_index.asc"
        ) or []

    cams_by_shelf: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for cam in cameras:
        try:
            shelf_key = str(cam.get("shelf_id") or "").strip()
            if not shelf_key:
                continue
            cams_by_shelf.setdefault(shelf_key, {})[int(cam.get("angle_index") or 1)] = cam
        except (TypeError, ValueError):
            continue

    merged: List[Dict[str, Any]] = []
    for comp in compartments:
        row = dict(comp)
        shelf_key = str(row.get("shelf_id") or "").strip()
        shelf_cams = cams_by_shelf.get(shelf_key, {})
        cam1, cam2 = shelf_cams.get(1), shelf_cams.get(2)
        if cam1:
            row["ip_address"] = _clean_host(cam1.get("ip_address")) or None
            row["mqtt_client_id"] = str(cam1.get("device_id") or "").replace("_CAM1", "") or None
            row["camera_name"] = cam1.get("camera_name")
        if cam2:
            row["secondary_ip_address"] = _clean_host(cam2.get("ip_address")) or None
            row["secondary_mqtt_client_id"] = cam2.get("device_id")
        merged.append(row)
    return merged


def update_esp32_health_in_supabase(node_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Minimal clean schema: status / rssi_dbm / free_heap_bytes / last_seen_at are runtime telemetry and are
    kept only in the bridge's in-memory edge_nodes. Nothing is written to Supabase; the payload is echoed back.
    """
    return dict(node_payload) if node_payload else None


# =============================================================================
# SUPABASE OPERATIONS: TEACHABLE MACHINE INVENTORY DELTA
# =============================================================================

def update_compartment_baseline_in_supabase(
    shelf_id: str,
    baseline_image_url: str,
    ip_address: Optional[str] = None,
    baseline_image_2_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Minimal clean schema: 30s baselines are RAM-only (reference_validator baseline_cache).
    No database write occurs; returns a lightweight local record.
    """
    return {"shelf_id": shelf_id.strip(), "ip_address": _clean_host(ip_address) or None}


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

    update_data: Dict[str, Any] = {"current_quantity": quantity_after}
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
    # Comparison logging is handled strictly in local in-memory deque (maxlen=5) in camera_service.py
    local_log = dict(db_log_record)
    local_log["before_image_url"] = comparison_result.get("before_image_url") or comparison_result.get("before_image")
    local_log["after_image_url"] = comparison_result.get("after_image_url") or comparison_result.get("after_image")
    return local_log


def list_comparison_logs(limit: int = 5) -> List[Dict[str, Any]]:
    """
    Deprecated database query: comparison logs are now stored strictly in the
    local in-memory deque (maxlen=5) in camera_service.py (GET /api/comparisons/latest).
    """
    return []
