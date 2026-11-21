"""Stable diagnostic codes; no changes to inference or authorization semantics."""
def failure_code(reason):
    text=str(reason).lower()
    if any(term in text for term in ('database','atomic inventory','revision')):return 'DATABASE_FAILURE'
    rules=[('UNKNOWN_B1',('b1 unknown','unknown b1','back_x01')),
           ('M2_CONFLICT',('m2 conflict','m2 disagreement')),
           ('LOW_CONFIDENCE',('low confidence','confidence insufficient','at least 80%')),
           ('OCCUPANCY_CHANGED',('occupancy changed','relocation','occupancy change conflicts')),
           ('STALE_CAPTURE',('stale','predates','reused','unknown, reused','mismatched capture')),
           ('MISSING_CCTV_CONSENSUS',('cctv consensus','consensus bound')),
           ('DATABASE_FAILURE',('database','atomic inventory','persistent inventory','revision')),
           ('MISSING_CAMERA',('camera unavailable','camera unreachable','camera images','preview failed','connection refused')),
           ('VALIDATION_TIMEOUT',('timeout','time limit','deadline')),
           ('LEASE_EXPIRED',('lease expired',)),
           ('MODEL_FAILURE',('model','preprocessing'))]
    for code,terms in rules:
        if any(term in text for term in terms):return code
    return 'VALIDATION_REJECTED'
