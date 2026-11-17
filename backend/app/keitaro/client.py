"""Тонкий синхронный клиент Keitaro Admin API v1.

- Авторизация заголовком Api-Key (контракт стенда). Ключ не попадает в сообщения ошибок и repr.
- Повторов нет: решение о повторе/сверке принимает сервисный слой.
- Для записи различаем «точно не применилось» и «результат неизвестен» (ambiguous).
"""

from typing import Any

import httpx

API_PREFIX = "/admin_api/v1"


class KeitaroError(Exception):
    """kind: config | auth | not_found | bad_request | unavailable | server | timeout | network | invalid_response"""

    def __init__(self, kind: str, message: str, status: int | None = None, ambiguous: bool = False):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        # True, если запрос на запись мог дойти и примениться: требуется сверка чтением.
        self.ambiguous = ambiguous

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}" + (f" (HTTP {self.status})" if self.status else "")


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])[:300]
    except ValueError:
        pass
    return (resp.text or resp.reason_phrase or "").strip()[:300]


class KeitaroClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0, transport: httpx.BaseTransport | None = None):
        if not base_url or not api_key:
            raise KeitaroError("config", "Keitaro не настроен: задайте KEITARO_URL и KEITARO_API_KEY в .env")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + API_PREFIX,
            headers={"Api-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"KeitaroClient(base_url={str(self._http.base_url)!r})"

    def close(self) -> None:
        self._http.close()

    # --- транспорт ---

    def _request(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> Any:
        write = method != "GET"
        try:
            resp = self._http.request(method, path, json=json, params=params)
        except httpx.ConnectTimeout as e:
            raise KeitaroError("timeout", "Keitaro не ответил при подключении", ambiguous=False) from e
        except httpx.TimeoutException as e:
            raise KeitaroError("timeout", "Истёк таймаут ожидания ответа Keitaro", ambiguous=write) from e
        except httpx.ConnectError as e:
            raise KeitaroError("network", "Не удалось подключиться к Keitaro", ambiguous=False) from e
        except httpx.TransportError as e:
            raise KeitaroError("network", f"Сетевая ошибка: {type(e).__name__}", ambiguous=write) from e

        status = resp.status_code
        if status in (401, 403):
            raise KeitaroError("auth", "Доступ запрещён: проверьте API-ключ и права", status)
        if status == 402:
            raise KeitaroError("unavailable", "Admin API недоступен в этой редакции Keitaro", status)
        if status == 404:
            raise KeitaroError("not_found", _error_text(resp) or "Сущность не найдена", status)
        if status in (400, 409, 422):
            raise KeitaroError("bad_request", _error_text(resp) or "Keitaro отклонил запрос", status)
        if status >= 500:
            raise KeitaroError("server", _error_text(resp) or "Внутренняя ошибка Keitaro", status, ambiguous=write)
        if not 200 <= status < 300:
            raise KeitaroError("server", f"Неожиданный ответ {status}", status, ambiguous=write)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise KeitaroError("invalid_response", "Ответ Keitaro не является JSON", status, ambiguous=write) from e

    def _expect(self, data: Any, kind: type, what: str) -> Any:
        if not isinstance(data, kind):
            raise KeitaroError("invalid_response", f"Неожиданный формат ответа: {what}")
        return data

    # --- чтение ---

    def list_offers(self) -> list[dict]:
        return self._expect(self._request("GET", "/offers"), list, "offers")

    def get_offer(self, offer_id: int) -> dict:
        return self._expect(self._request("GET", f"/offers/{int(offer_id)}"), dict, "offer")

    def list_domains(self) -> list[dict]:
        return self._expect(self._request("GET", "/domains"), list, "domains")

    def list_campaign_groups(self) -> list[dict]:
        return self._expect(self._request("GET", "/groups", params={"type": "campaigns"}), list, "groups")

    def list_traffic_sources(self) -> list[dict]:
        return self._expect(self._request("GET", "/traffic_sources"), list, "traffic_sources")

    def list_campaigns(self, offset: int = 0, limit: int = 1000) -> list[dict]:
        return self._expect(self._request("GET", "/campaigns", params={"offset": offset, "limit": limit}), list, "campaigns")

    def get_campaign(self, campaign_id: int) -> dict:
        return self._expect(self._request("GET", f"/campaigns/{int(campaign_id)}"), dict, "campaign")

    def get_campaign_streams(self, campaign_id: int) -> list[dict]:
        return self._expect(self._request("GET", f"/campaigns/{int(campaign_id)}/streams"), list, "streams")

    def get_stream(self, stream_id: int) -> dict:
        return self._expect(self._request("GET", f"/streams/{int(stream_id)}"), dict, "stream")

    # --- запись ---

    def create_campaign(self, payload: dict) -> dict:
        return self._expect(self._request("POST", "/campaigns", json=payload), dict, "campaign")

    def create_stream(self, payload: dict) -> dict:
        return self._expect(self._request("POST", "/streams", json=payload), dict, "stream")

    def update_stream_offers(self, stream_id: int, offers: list[dict]) -> dict | None:
        """Частичный PUT: только состав офферов. Остальные поля потока не отправляются."""
        return self._request("PUT", f"/streams/{int(stream_id)}", json={"offers": offers})
