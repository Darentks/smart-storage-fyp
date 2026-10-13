"""
Configuration and Supabase Ingestion Loader for Smart Storage Vision Validation Module
Author: Daren Tan Khai Shuen
Handles dynamic retrieval of active CCTV camera RTSP streams from Supabase.
"""

import os
import re
from typing import Any, Dict, Optional, Union
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
        # Current working directory
        os.path.join(os.getcwd(), ".env"),
        # Backend directory
        os.path.abspath(os.path.join(current_dir, "..", ".env")),
        # Module root
        os.path.abspath(os.path.join(current_dir, "..", "..", ".env")),
        # Workspace / project root
        os.path.abspath(os.path.join(current_dir, "..", "..", "..", ".env")),
    ]

    loaded_path = None
    for path in candidate_paths:
        if os.path.isfile(path):
            load_dotenv(dotenv_path=path, override=False)
            loaded_path = path
            break

    # Robust fallback: If keys are indented in .env, parse them manually into os.environ
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
    Looks for VITE_SUPABASE_URL / SUPABASE_URL and VITE_SUPABASE_ANON_KEY / SUPABASE_KEY.
    :return: Supabase Client instance or None
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
    """
    Constructs standard Hikvision RTSP stream URL.
    Format: rtsp://<username>:<password>@<ip>:<rtsp_port>/Streaming/Channels/<stream_channel>
    :param ip_address: Camera IP (e.g. '192.168.0.103')
    :param username: RTSP username
    :param password: RTSP password
    :param rtsp_port: RTSP port (default 554)
    :param stream_channel: Channel ID (101: Main stream, 102: Sub stream)
    """
    if username and password:
        auth_part = f"{username}:{password}@"
    elif username:
        auth_part = f"{username}@"
    else:
        auth_part = ""

    return f"rtsp://{auth_part}{ip_address}:{rtsp_port}/Streaming/Channels/{stream_channel}"


def get_active_camera_record() -> Optional[Dict[str, Any]]:
    """
    Queries the first active CCTV camera from Supabase table 'cameras'.
    :return: Camera record dictionary or None if none found / query failed.
    """
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
    """
    Retrieves the RTSP URL of the active CCTV camera from Supabase.
    If multiple active cameras exist, selects the first active one.
    Falls back to `default_source` (default: 0 for local webcam) on failure or when no active camera is registered.

    :param default_source: Fallback camera index or URL (default: 0)
    :return: Active RTSP URL string, or default_source fallback
    """
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
