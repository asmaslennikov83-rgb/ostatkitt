from __future__ import annotations

import os
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


@dataclass(frozen=True)
class CabinetConfig:
    key: str
    name: str
    token: str


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_ids: set[int]
    cabinets: tuple[CabinetConfig, ...]
    lookback_days: int
    retention_days: int
    no_sales_target: int
    distribute_all_threshold: int
    base_dir: Path = BASE_DIR


def load_settings() -> Settings:
    allowed = _ids(_required("TELEGRAM_ALLOWED_IDS"))
    if not allowed:
        raise RuntimeError("TELEGRAM_ALLOWED_IDS пуст")

    cabinets = (
        CabinetConfig("cabinet_1", _required("WB_CABINET_1_NAME"), _required("WB_TOKEN_1")),
        CabinetConfig("cabinet_2", _required("WB_CABINET_2_NAME"), _required("WB_TOKEN_2")),
    )
    return Settings(
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        allowed_ids=allowed,
        cabinets=cabinets,
        lookback_days=int(os.getenv("WB_LOOKBACK_DAYS", "14")),
        retention_days=int(os.getenv("HISTORY_RETENTION_DAYS", "4")),
        no_sales_target=int(os.getenv("NO_SALES_TARGET_PER_WAREHOUSE", "2")),
        distribute_all_threshold=int(os.getenv("DISTRIBUTE_ALL_THRESHOLD", "20")),
    )
