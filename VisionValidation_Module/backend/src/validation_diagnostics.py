"""Stable diagnostic codes; no changes to inference or authorization semantics."""
RULES = (('COUNT_CHECK_SKIPPED',('count check skipped','cctv check pending')),
       ('CCTV_UNAVAILABLE',('fresh cctv feeds','cctv health lost')),
       ('UNKNOWN_B1',('b1 unknown','unknown b1','back_x01')),
       ('MQTT_UNAVAILABLE',('mqtt unavailable',)),
       ('M2_CONFLICT',('m2 conflict','m2 disagreement')),
       ('LOW_CONFIDENCE',('low confidence','confidence insufficient','at least 80%')),
       ('OCCUPANCY_CHANGED',('occupancy changed','relocation','occupancy change conflicts')),
       ('STALE_CAPTURE',('stale','predates','reused','unknown, reused','mismatched capture')),
       ('MISSING_CCTV_CONSENSUS',('cctv consensus','consensus bound')),
       ('DATABASE_FAILURE',('database','atomic inventory','persistent inventory','revision')),
       ('MISSING_CAMERA',('camera unavailable','camera unreachable','camera images','preview failed','connection refused')),
       ('VALIDATION_TIMEOUT',('timeout','time limit','deadline')),
       ('LEASE_EXPIRED',('lease expired',)),
       ('MODEL_FAILURE',('model','preprocessing')))
FAILURE_CODES=frozenset(code for code,_ in RULES) | {'VISIBLE_CHANGE_NET_UNPROVEN','VALIDATION_REJECTED'}

def failure_code(reason):
    text=str(reason).lower().replace('_',' ')
    if 'visible change net unproven' in text:return 'VISIBLE_CHANGE_NET_UNPROVEN'
    if any(term in text for term in ('database','atomic inventory','revision')):return 'DATABASE_FAILURE'
    for code,terms in RULES:
        if any(term in text for term in terms):return code
    return 'VALIDATION_REJECTED'
