\
from __future__ import annotations

from typing import Any
import aiohttp


class AgentUnavailable(RuntimeError):
    pass


class AgentAuthError(RuntimeError):
    pass


class AgentClient:
    def __init__(self, base_url: str, secret: str, timeout_seconds: int = 60):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Agent-Secret": self.secret,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "WB-Real-Stock-Server/3.0",
        }

    async def health(self) -> dict[str, Any]:
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(
                    f"{self.base_url}/health",
                    headers=self.headers,
                ) as resp:
                    text = await resp.text()
                    if resp.status == 401:
                        raise AgentAuthError("Неверный WB_AGENT_SECRET.")
                    if resp.status >= 400:
                        raise AgentUnavailable(
                            f"Агент ответил HTTP {resp.status}: {text[:500]}"
                        )
                    return await resp.json(content_type=None)
        except AgentAuthError:
            raise
        except Exception as exc:
            raise AgentUnavailable(
                f"Windows-агент недоступен: {exc}"
            ) from exc

    async def get_real_fbo(
        self,
        nm_ids: list[int],
        *,
        dest: str,
        currency: str,
        app_type: int,
        spp: int,
    ) -> dict[int, int]:
        payload = {
            "nm_ids": nm_ids,
            "dest": dest,
            "currency": currency,
            "app_type": app_type,
            "spp": spp,
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    f"{self.base_url}/real-fbo",
                    headers=self.headers,
                    json=payload,
                ) as resp:
                    text = await resp.text()
                    if resp.status == 401:
                        raise AgentAuthError("Неверный WB_AGENT_SECRET.")
                    if resp.status >= 400:
                        raise AgentUnavailable(
                            f"Агент ответил HTTP {resp.status}: {text[:800]}"
                        )
                    data = await resp.json(content_type=None)
        except (AgentAuthError, AgentUnavailable):
            raise
        except Exception as exc:
            raise AgentUnavailable(
                f"Не удалось связаться с Windows-агентом: {exc}"
            ) from exc

        raw = data.get("real_fbo", {})
        return {int(k): int(v) for k, v in raw.items()}
