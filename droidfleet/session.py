"""拟人会话持久化：跨命令保持同一"人"的随机状态与疲劳度。"""
from __future__ import annotations

import pickle
from pathlib import Path
from typing import Optional

from droidfleet.human import HumanProfile

STATE_DIR = Path.home() / ".droidfleet" / "state"
STATE = STATE_DIR / "human_session.pkl"
LEGACY = Path("/tmp/soda_human_state.pkl")


def _read(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            return pickle.load(handle)
    except Exception:
        return None


def load(app: str | None = None) -> HumanProfile:
    blob = _read(STATE) or _read(LEGACY)
    if blob is None:
        return HumanProfile.new(app=app)
    profile = HumanProfile.new(seed=blob["seed"], app=app)
    profile.started = blob["started"]
    profile.rng.setstate(blob["rng"])
    profile._liked_last = blob["liked_last"]
    return profile


def save(profile: HumanProfile) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    blob = {
        "seed": profile.rng.random(),
        "started": profile.started,
        "rng": profile.rng.getstate(),
        "liked_last": profile._liked_last,
    }
    with STATE.open("wb") as handle:
        pickle.dump(blob, handle)


def reset() -> None:
    STATE.unlink(missing_ok=True)
    LEGACY.unlink(missing_ok=True)
