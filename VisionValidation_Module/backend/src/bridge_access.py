"""Verify remote application users without an owner/admin claim or token logging."""
import base64
import json
import time
import urllib.error
import urllib.request

from .config_loader import get_resolved_supabase_url, get_resolved_supabase_key


class BridgeAccessError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def require_application_user(authorization):
    """JWT claims prefilter only; Supabase Auth verifies the actual user token."""
    try:
        scheme, token = authorization.split(' ', 1)
        if scheme.lower() != 'bearer' or len(token) > 16000:
            raise ValueError()
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        if (claims.get('role') != 'authenticated' or not claims.get('sub')
                or type(claims.get('exp')) not in (int, float) or claims['exp'] <= time.time()):
            raise ValueError()
    except (AttributeError, ValueError, TypeError, KeyError):
        raise BridgeAccessError(401, 'Sign in to configure the shared bridge') from None
    request = urllib.request.Request(
        get_resolved_supabase_url() + '/auth/v1/user',
        headers={'apikey': get_resolved_supabase_key(), 'Authorization': 'Bearer ' + token},
        method='GET')
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            user = json.loads(response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise BridgeAccessError(401, 'Session expired or invalid; sign in again') from None
        raise BridgeAccessError(503, 'Unable to verify the application session; retry later') from None
    except (OSError, ValueError):
        raise BridgeAccessError(503, 'Unable to verify the application session; retry later') from None
    if not isinstance(user, dict) or user.get('id') != claims['sub'] or user.get('is_anonymous') is True:
        raise BridgeAccessError(403, 'An application user login is required for configuration')
    return user['id']
