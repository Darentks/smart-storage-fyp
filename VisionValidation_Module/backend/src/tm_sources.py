"""Bridge-wide choice of which exported TM models the next update installs.

Saving a choice never downloads or activates a model. The installed sources are the
ones recorded in the model manifest, which the updater replaces together with the files.
Keep the URL rules identical to edge_service/tm_sources.cjs.
"""
import json
import re
import urllib.parse
from pathlib import Path

from .local_operations import atomic_json

ROLES=('Front','Back')
_EXPORT_URL=re.compile(r'https://teachablemachine\.withgoogle\.com/models/([A-Za-z0-9_-]{4,64})/?')
_LOCAL_HOSTS={'localhost','127.0.0.1','::1'}


def validate_sources(value):
    ids=[]
    for role in ROLES:
        url=value.get(role) if isinstance(value,dict) else None
        match=_EXPORT_URL.fullmatch(url) if isinstance(url,str) else None
        if not match:
            raise ValueError(f'{role}: enter an exported model link like https://teachablemachine.withgoogle.com/models/MODEL_ID/')
        ids.append(match.group(1))
    if ids[0]==ids[1]:raise ValueError('Front and Back must be two different models')
    return {role:f'https://teachablemachine.withgoogle.com/models/{model}/' for role,model in zip(ROLES,ids)}


def _saved_path(root):return Path(root)/'.runtime/tm-model-sources.json'


def configured_sources(root):
    path=_saved_path(root);origin='saved'
    if not path.exists():path=Path(root)/'VisionValidation_Module/edge_service/tm-sources.default.json';origin='default'
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or value.get('version')!=1:raise ValueError('Unsupported model source file')
    return validate_sources(value),origin


def installed_sources(manifest):
    try:
        value=json.loads(Path(manifest).read_text(encoding='utf-8'))
        return validate_sources({role:value[role]['source'] for role in ROLES})
    except (OSError,ValueError,KeyError,TypeError):return None


def sources_status(root,manifest):
    try:
        configured,origin=configured_sources(root);error=None
    except (OSError,ValueError):
        configured,origin,error=None,None,'The saved model links are unreadable or invalid; save them again'
    installed=installed_sources(manifest)
    return dict(configured=configured,configured_origin=origin,installed=installed,
        pending_install=bool(configured) and configured!=installed,error=error)


def save_sources(root,body):
    if not isinstance(body,dict) or set(body)!=set(ROLES):raise ValueError('Send exactly the Front and Back model links')
    sources=validate_sources(body)
    atomic_json(_saved_path(root),dict(version=1,**sources))
    return sources


def is_local_dashboard(client_ip,headers):
    """True only for the dashboard on this computer. A tunnel or another website also
    reaches the bridge from loopback, so the address alone is not enough."""
    if client_ip not in ('127.0.0.1','::1','::ffff:127.0.0.1') or headers is None:return False
    if any(headers.get(name) for name in ('X-Forwarded-For','CF-Connecting-IP','Forwarded')):return False
    try:
        if urllib.parse.urlsplit('//'+(headers.get('Host') or '')).hostname not in _LOCAL_HOSTS:return False
        origin=headers.get('Origin')
        return origin is None or urllib.parse.urlsplit(origin).hostname in _LOCAL_HOSTS
    except ValueError:return False
