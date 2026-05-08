"""런타임 오버라이드 사이드카.

대시보드/텔레그램에서 토글되는 일부 설정(kis_env, agent.dry_run 등)은
.env / config.yaml 같은 소스 레이어가 부팅 시 읽기-전용이라 재기동 시
회귀한다. 이 모듈은 data 디렉터리 아래 `.runtime_overrides.json` 파일에
변경값을 영속화하고, build_runtime이 부팅 시 이를 Settings/AppConfig 위에
덮어쓴다.

비밀값(KIS 키 등)은 여기에 저장하지 않는다 — 그건 .env / Secrets Manager.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FILENAME = ".runtime_overrides.json"


def overrides_path(data_dir: Path) -> Path:
    return Path(data_dir) / _FILENAME


def load_overrides(data_dir: Path) -> dict[str, Any]:
    p = overrides_path(data_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def update_overrides(data_dir: Path, **kv: Any) -> dict[str, Any]:
    """주어진 키-값 쌍만 병합 저장. None은 무시(삭제 의도면 별도 API)."""
    cur = load_overrides(data_dir)
    cur.update({k: v for k, v in kv.items() if v is not None})
    p = overrides_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur
