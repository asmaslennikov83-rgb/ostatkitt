\
from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


@dataclass(frozen=True)
class Cabinet:
    key: str
    name: str
    token: str


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WB_AGENT_URL = os.getenv("WB_AGENT_URL", "").strip().rstrip("/")
WB_AGENT_SECRET = os.getenv("WB_AGENT_SECRET", "").strip()

CABINETS = {
    "cab1": Cabinet(
        key="cab1",
        name=os.getenv("WB_CABINET_1_NAME", "Кабинет 1").strip(),
        token=os.getenv("WB_CABINET_1_TOKEN", "").strip(),
    ),
    "cab2": Cabinet(
        key="cab2",
        name=os.getenv("WB_CABINET_2_NAME", "Кабинет 2").strip(),
        token=os.getenv("WB_CABINET_2_TOKEN", "").strip(),
    ),
}

WB_DEST = os.getenv("WB_DEST", "-1257786").strip()
WB_CURRENCY = os.getenv("WB_CURRENCY", "rub").strip()
WB_APP_TYPE = env_int("WB_APP_TYPE", 1)
WB_SPP = env_int("WB_SPP", 30)
HTTP_TIMEOUT_SECONDS = env_int("HTTP_TIMEOUT_SECONDS", 60)

ALLOWED_TELEGRAM_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

CONTROL_NM_IDS = [
    int(x.strip())
    for x in os.getenv("CONTROL_NM_IDS", "16358648,219205510,206008365").split(",")
    if x.strip().isdigit()
]

CONTROL_EXPECTED_FBO: dict[int, int] = {}
for pair in os.getenv(
    "CONTROL_EXPECTED_FBO",
    "16358648:0,219205510:0,206008365:4",
).split(","):
    if ":" not in pair:
        continue
    left, right = pair.split(":", 1)
    try:
        CONTROL_EXPECTED_FBO[int(left.strip())] = int(right.strip())
    except ValueError:
        pass


def validate() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not WB_AGENT_URL:
        missing.append("WB_AGENT_URL")
    if not WB_AGENT_SECRET:
        missing.append("WB_AGENT_SECRET")
    for cab in CABINETS.values():
        if not cab.token:
            missing.append(f"WB token: {cab.name}")
    if missing:
        raise RuntimeError("Не заполнены настройки .env: " + ", ".join(missing))
