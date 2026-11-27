"""Fixed physical replacement slots; display labels are not firmware identities."""
import ipaddress

SLOTS = tuple(dict(label=f'CAM{i}', shelf_id=f'SHELF_R1_C{(i+1)//2}',
    device_id=f'ESP32_R1_C{(i+1)//2}_CAM{1 if i%2 else 2}',
    angle_index=1 if i%2 else 2, role='Back' if i%2 else 'Front',
    ip_address=f'192.168.0.{200+i}', environment=f'cam{i}_usb') for i in range(1,7))


def slot_for(label):
    return next((dict(s) for s in SLOTS if s['label']==label), None)


def match_registrations(rows):
    """Never pick a winner from duplicate identities or conflicting shelf assignments."""
    if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):raise ValueError('Device registrations unavailable')
    result=[]
    identities={s['device_id'] for s in SLOTS}
    for slot in SLOTS:
        matches=[r for r in rows if r.get('device_id')==slot['device_id']]
        conflicts=[r for r in rows if r.get('shelf_id')==slot['shelf_id'] and
                   str(r.get('angle_index'))==str(slot['angle_index']) and r.get('device_id')!=slot['device_id']]
        errors=[]
        if len(matches)>1 or conflicts:errors.append('Duplicate or conflicting registration')
        if len(matches)==1:
            row=matches[0]
            for key in ('shelf_id','angle_index','ip_address'):
                value=str(row.get(key,''))
                if key=='ip_address':value=value.split('/')[0]
                if value!=str(slot[key]):errors.append(key+' mismatch')
            if row.get('role') and str(row['role']).lower()!=slot['role'].lower():errors.append('role mismatch')
        result.append(dict(slot,registration=matches[0] if len(matches)==1 else None,
            errors=errors,status='Configuration error' if errors else 'Registered' if matches else 'Not registered'))
    return dict(slots=result,ignored=sum(r.get('device_id') not in identities for r in rows))


def discovery_addresses(rows):
    addresses={s['ip_address'] for s in SLOTS}
    for row in rows:
        for key in ('ip_address','secondary_ip_address'):
            try:addresses.add(str(ipaddress.IPv4Interface(str(row.get(key,''))).ip))
            except ValueError:pass
    return sorted(addresses)
