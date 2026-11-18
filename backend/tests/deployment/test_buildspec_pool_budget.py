"""Pin the ECS DB pool budget behind the 2026-11-18 prod 500s.

A backend task's QueuePool (DB_POOL_SIZE + DB_MAX_OVERFLOW) is the only bound on
concurrent DB sessions per API task. The read fan-out after the 2026-11-17 sync
exhausted size 10 + overflow 5; these tests keep the backend ceiling at the
reviewed value and the cross-service rolling-deploy peak inside the RDS budget.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILDSPEC_PATH = REPO_ROOT / "buildspec.yml"

_BACKEND_BLOCK_START = 'BACKEND_ENVIRONMENT="$(jq -c'
_WORKER_BLOCK_START = 'WORKER_ENVIRONMENT="$(jq -c'
_BEAT_BLOCK_START = 'BEAT_ENVIRONMENT="$(jq -c'
_BLOCK_ENDS = (
    _WORKER_BLOCK_START,
    _BEAT_BLOCK_START,
    'FRONTEND_ENVIRONMENT="$(jq -c',
)
_POOL_VALUE_RE = re.compile(r'\{"name":"(DB_POOL_SIZE|DB_MAX_OVERFLOW)","value":"(\d+)"\}')

# One backend task, one worker task, one beat task; the API rolls old+new
# (maximumPercent=200) while worker and beat stay at desired count 1.
_MAX_ROLLING_DEPLOY_PEAK = 75


def _environment_block(text: str, start: str) -> str:
    start_index = text.index(start)
    end_index = len(text)
    for marker in _BLOCK_ENDS:
        marker_index = text.find(marker, start_index + len(start))
        if marker_index != -1:
            end_index = min(end_index, marker_index)
    return text[start_index:end_index]


def _pool_bounds(block: str) -> dict[str, int]:
    return {name: int(value) for name, value in _POOL_VALUE_RE.findall(block)}


def test_backend_pool_peak_covers_read_fanout() -> None:
    text = BUILDSPEC_PATH.read_text(encoding="utf-8")
    bounds = _pool_bounds(_environment_block(text, _BACKEND_BLOCK_START))

    assert bounds["DB_POOL_SIZE"] == 10
    assert bounds["DB_MAX_OVERFLOW"] == 20
    assert bounds["DB_POOL_SIZE"] + bounds["DB_MAX_OVERFLOW"] == 30


def test_cross_service_peak_stays_inside_rds_budget() -> None:
    text = BUILDSPEC_PATH.read_text(encoding="utf-8")
    backend = _pool_bounds(_environment_block(text, _BACKEND_BLOCK_START))
    worker = _pool_bounds(_environment_block(text, _WORKER_BLOCK_START))
    beat = _pool_bounds(_environment_block(text, _BEAT_BLOCK_START))

    backend_peak = backend["DB_POOL_SIZE"] + backend["DB_MAX_OVERFLOW"]
    worker_peak = worker["DB_POOL_SIZE"] + worker["DB_MAX_OVERFLOW"]
    beat_peak = beat["DB_POOL_SIZE"] + beat["DB_MAX_OVERFLOW"]

    assert 2 * backend_peak + worker_peak + beat_peak <= _MAX_ROLLING_DEPLOY_PEAK
