from pathlib import Path

SAMPLES = Path(__file__).resolve().parents[1] / 'sample_data'
POLICY = 'CURB-1.0'
DISPATCH_POLICY = 'DISPATCH-1.0'
FREE_MINUTES = 10
TIER_LIMIT = 15
BASE_CENTS = 20
UPGRADE_CENTS = 40
DEFAULT_GRACE_MINUTES = 10
