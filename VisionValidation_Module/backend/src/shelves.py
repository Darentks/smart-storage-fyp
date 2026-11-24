"""Single source of truth for the active rack layout (RACK_01, columns C1-C3)."""
SHELF_IDS = tuple(f'SHELF_R1_C{i}' for i in range(1, 4))
SHELF_SET = frozenset(SHELF_IDS)
