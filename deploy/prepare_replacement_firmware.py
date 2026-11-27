"""Apply the narrowly scoped portable build settings to the approved sibling firmware.

Run once after reviewing the diff. Does not build, upload, erase or change identities.
"""
import argparse
from pathlib import Path


def apply(repo):
    main=repo/'src/main.cpp';script=repo/'http_ota_provisioner.py'
    text=main.read_text(encoding='utf8')
    if '#ifndef DEPLOY_WIFI_SSID' not in text:
        import re
        definitions=[]
        for constant,macro in [('WIFI_SSID','DEPLOY_WIFI_SSID'),('WIFI_PASSWORD','DEPLOY_WIFI_PASSWORD'),('MQTT_BROKER_IP','DEPLOY_MQTT_BROKER')]:
            pattern=r'(const char\* '+constant+r'\s*=\s*)("[^"\n]*")(;)'
            match=re.search(pattern,text)
            if not match:raise ValueError('Expected existing firmware network constant: '+constant)
            definitions.append('#ifndef '+macro+'\n#define '+macro+' '+match[2]+'\n#endif')
            text=re.sub(pattern,lambda m:m[1]+macro+m[3],text,count=1)
        text=text.replace('// 2. NETWORK, HTTP SNAPSHOT SERVER & MQTT BROKER CONFIGURATION',
            '// 2. NETWORK, HTTP SNAPSHOT SERVER & MQTT BROKER CONFIGURATION\n'+'\n'.join(definitions))
    if 'doc["camera_role"]' not in text:
        needle='    doc["firmware_ver"]   = FIRMWARE_VER;'
        if needle not in text:raise ValueError('Expected firmware status handler')
        text=text.replace(needle,needle+'''
    doc["angle_index"] = deviceId.endsWith("CAM2") ? 2 : 1;
    doc["camera_role"] = deviceId.endsWith("CAM2") ? "Front" : "Back";
    doc["mqtt_connected"] = mqttClient.connected();
    doc["mac_address"] = WiFi.macAddress();''',1)
    if '#ifndef CUSTOM_REPAIR_IDENTITY' not in text:
        text=text.replace('Preferences prefs;', '#ifndef CUSTOM_REPAIR_IDENTITY\n#define CUSTOM_REPAIR_IDENTITY ""\n#endif\nPreferences prefs;')
        text=text.replace('  if (savedBuildOctet != CUSTOM_IP_OCTET) {',
            '  bool repairIdentity = strlen(CUSTOM_REPAIR_IDENTITY) > 0 && prefs.getString("repair_nonce", "") != CUSTOM_REPAIR_IDENTITY;\n  if (savedBuildOctet != CUSTOM_IP_OCTET || repairIdentity) {\n    if (repairIdentity) prefs.putString("repair_nonce", CUSTOM_REPAIR_IDENTITY);')
    build=script.read_text(encoding='utf8')
    if '# Portable deployment overrides' not in build:
        needle='\n\ndef http_ota_upload('
        extra='''

# Portable deployment overrides. Values stay in a local ignored JSON file.
# Apply only in the pre phase; generating a header avoids command-line secrets.
if not env.get("SMART_STORAGE_DEPLOY_HEADER"):
    import json
    from pathlib import Path
    settings_path = os.getenv("SMART_STORAGE_FIRMWARE_CONFIG")
    settings = {}
    if settings_path:
        settings = json.loads(Path(settings_path).read_text(encoding="utf8"))
        if not isinstance(settings, dict) or set(settings) - {"wifi_ssid", "wifi_password", "mqtt_broker"}:
            raise ValueError("Invalid local firmware deployment configuration")
    header_lines = ["#pragma once"]
    repair_nonce = os.getenv("SMART_STORAGE_REPAIR_IDENTITY", "")
    if repair_nonce:
        if len(repair_nonce) != 32 or any(c not in "0123456789abcdef" for c in repair_nonce):
            raise ValueError("Invalid replacement repair nonce")
        header_lines.append("#define CUSTOM_REPAIR_IDENTITY " + json.dumps(repair_nonce))
    for key, macro in (("wifi_ssid", "DEPLOY_WIFI_SSID"), ("wifi_password", "DEPLOY_WIFI_PASSWORD"), ("mqtt_broker", "DEPLOY_MQTT_BROKER")):
        if key in settings:
            value = settings[key]
            if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c)<32 for c in value):
                raise ValueError("Invalid local firmware setting: " + key)
            header_lines.append("#define " + macro + " " + json.dumps(value, ensure_ascii=True))
    header = Path(env.subst("$BUILD_DIR")) / "smart_storage_deploy.h"
    header.parent.mkdir(parents=True, exist_ok=True)
    content = "\\n".join(header_lines) + "\\n"
    if not header.exists() or header.read_text(encoding="utf8") != content:
        header.write_text(content, encoding="utf8")
    env.Append(CCFLAGS=["-include", str(header)])
    env["SMART_STORAGE_DEPLOY_HEADER"] = True
'''
        if needle not in build:raise ValueError('Expected existing PlatformIO hook')
        build=build.replace(needle,extra+needle,1)
    if 'repair_nonce = os.getenv' not in build:
        build=build.replace('    header_lines = ["#pragma once"]', '\n'.join([
            '    header_lines = ["#pragma once"]',
            '    repair_nonce = os.getenv("SMART_STORAGE_REPAIR_IDENTITY", "")',
            '    if repair_nonce:',
            '        if len(repair_nonce) != 32 or any(c not in "0123456789abcdef" for c in repair_nonce):',
            '            raise ValueError("Invalid replacement repair nonce")',
            '        header_lines.append("#define CUSTOM_REPAIR_IDENTITY " + json.dumps(repair_nonce))']))
    main.write_text(text,encoding='utf8');script.write_text(build,encoding='utf8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--repo',type=Path,required=True)
    apply(parser.parse_args().repo.resolve())
