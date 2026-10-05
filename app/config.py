from __future__ import annotations

import os
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не заполнена переменная окружения {name}")
    return value


def _ids(value: str) -> set[int]:
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if part:
            result.add(int(part))
    return result


def _seller_id_from_token(token: str) -> str | None:
    """Best-effort extraction of WB seller ID (sid) from a JWT token."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        value = data.get("sid")
        return str(value).strip() if value not in (None, "") else None
    except Exception:
        return None


@dataclass(frozen=True)
class CabinetConfig:
    key: str
    name: str
    token: str
    seller_id: str | None = None


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_ids: set[int]
    cabinets: tuple[CabinetConfig, ...]
    lookback_days: int
    retention_days: int
    no_sales_target: int
    distribute_all_threshold: int
    safety_stock_per_warehouse: int
    base_dir: Path = BASE_DIR


def load_settings() -> Settings:
    allowed = _ids(_required("TELEGRAM_ALLOWED_IDS"))
    if not allowed:
        raise RuntimeError("TELEGRAM_ALLOWED_IDS пуст")

    token_1 = _required("WB_TOKEN_1")
    token_2 = _required("WB_TOKEN_2")
    cabinets = (
        CabinetConfig(
            "cabinet_1", _required("WB_CABINET_1_NAME"), token_1,
            os.getenv("WB_CABINET_1_ID", "").strip() or _seller_id_from_token(token_1),
        ),
        CabinetConfig(
            "cabinet_2", _required("WB_CABINET_2_NAME"), token_2,
            os.getenv("WB_CABINET_2_ID", "").strip() or _seller_id_from_token(token_2),
        ),
    )
    return Settings(
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        allowed_ids=allowed,
        cabinets=cabinets,
        lookback_days=int(os.getenv("WB_LOOKBACK_DAYS", "14")),
        retention_days=int(os.getenv("HISTORY_RETENTION_DAYS", "4")),
        no_sales_target=int(os.getenv("NO_SALES_TARGET_PER_WAREHOUSE", "2")),
        distribute_all_threshold=int(os.getenv("DISTRIBUTE_ALL_THRESHOLD", "20")),
        safety_stock_per_warehouse=max(1, int(os.getenv("SAFETY_STOCK_PER_WAREHOUSE", "4"))),
    )
