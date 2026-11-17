"""In-memory имитация нужной части Keitaro Admin API v1.

Используется в тестах (через httpx.MockTransport) и для локального прогона UI без стенда
(`python -m app.devtools.fake_keitaro`). Форма данных повторяет наблюдаемую на стенде.
Допущение, проверяемое только live: частичный PUT /streams/{id} с полем offers заменяет состав
офферов и не меняет остальные поля. put_side_effects имитирует обратное, чтобы проверить защиту.
"""

import copy
import itertools
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

API_KEY = "fake-key"
PREFIX = "/admin_api/v1"


@dataclass
class Fault:
    method: str
    path: str  # regex по пути без префикса
    # timeout_before | timeout_after | error_500_before | error_500_after | auth | invalid_json | drop_after
    # | malformed_before (не применено, 200 без id) | malformed_after (применено, 200 без id) | error_400
    mode: str


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class FakeKeitaro:
    def __init__(self, api_key: str = API_KEY):
        self.api_key = api_key
        self.ids = itertools.count(5000)
        self.link_ids = itertools.count(900000)
        self.offers: dict[int, dict] = {}
        self.domains = [{"id": 1813, "name": "https://kylovoa.com/", "state": "active"}]
        self.groups = [{"id": 9799, "name": "VZ adrobot test", "type": "campaigns"}, {"id": 10332, "name": "FORTESTS", "type": "campaigns"}]
        self.sources = [{"id": 836, "name": "FORTESTS", "state": "active"}]
        self.campaigns: dict[int, dict] = {}
        self.streams: dict[int, dict] = {}
        self.faults: list[Fault] = []
        self.calls: list[tuple[str, str]] = []
        # {поле: значение}, которое PUT /streams/{id} дополнительно запишет в поток (имитация побочного эффекта).
        self.put_side_effects: dict = {}
        # Хуки (method, path_regex) -> callable(); выполняются при обработке запроса до ответа.
        self.hooks: list = []

    # --- наполнение ---

    def add_offer(self, offer_id: int, name: str, state: str = "active", country=None) -> dict:
        offer = {"id": offer_id, "name": name, "state": state, "group_id": 1, "country": country if country is not None else ""}
        self.offers[offer_id] = offer
        return offer

    def seed_demo(self) -> "FakeKeitaro":
        for oid, name in [(3749, "Mialflow [BEAUTY-RO-BE_0009] spin ro"), (3717, "Mialflow [BEAUTY-RO-BE_0008] prize ro"),
                          (11111, "FitoMishki [Weight Loss-CO-DT_0309] es"), (11112, "Oxys [BEAUTY-CL-BE_0155] es"),
                          (10184, "Cardio NRJ [HEALTH-PE] es"), (24800, "Flexacil [JOINT-PE] land es")]:
            self.add_offer(oid, f"{oid} {name}")
        self.add_offer(999, "999 Archived offer", state="deleted")
        camp = self._create_campaign({"alias": "democampaign", "name": "test_simonov_demo", "type": "position",
                                      "group_id": 9799, "domain_id": 1813, "traffic_source_id": 836})
        self._create_stream({"campaign_id": camp["id"], "type": "regular", "name": "Flow 1", "position": 1, "schema": "redirect",
                             "action_type": "http", "action_payload": "https://google.com",
                             "filters": [{"name": "country", "mode": "accept", "payload": ["AU"]}]})
        self._create_stream({"campaign_id": camp["id"], "type": "regular", "name": "Flow 2", "position": 2, "schema": "landings",
                             "action_type": "http", "action_payload": "",
                             "offers": [{"offer_id": 3749, "share": 33}, {"offer_id": 3717, "share": 33}, {"offer_id": 11111, "share": 34}]})
        return self

    # --- внешние правки «в самом Keitaro» ---

    def external_remove_offer(self, stream_id: int, offer_id: int) -> None:
        s = self.streams[stream_id]
        s["offers"] = [o for o in s["offers"] if o["offer_id"] != offer_id]

    def external_set_offers(self, stream_id: int, offers: list[tuple[int, int]]) -> None:
        self.streams[stream_id]["offers"] = [self._link(stream_id, o, sh, "active") for o, sh in offers]

    def stream_offers(self, stream_id: int) -> list[tuple[int, int, str]]:
        return [(o["offer_id"], o["share"], o["state"]) for o in self.streams[stream_id]["offers"]]

    # --- логика API ---

    def _link(self, stream_id: int, offer_id: int, share: int, state: str) -> dict:
        return {"id": next(self.link_ids), "stream_id": stream_id, "offer_id": offer_id, "state": state, "share": share,
                "created_at": _now(), "updated_at": _now()}

    def _validate_offers(self, offers) -> tuple[int, dict] | None:
        if not isinstance(offers, list):
            return 400, {"error": "offers must be array"}
        seen = set()
        for o in offers:
            oid = o.get("offer_id")
            if oid not in self.offers or self.offers[oid]["state"] == "deleted":
                return 400, {"error": f"Offer {oid} not found"}
            if oid in seen:
                return 400, {"error": f"Duplicate offer {oid}"}
            if not isinstance(o.get("share"), int) or o["share"] < 0:
                return 400, {"error": "share must be integer"}
            if o.get("state", "active") not in ("active", "disabled"):
                return 400, {"error": "invalid offer state"}
            seen.add(oid)
        return None

    def _create_campaign(self, body: dict) -> dict:
        cid = next(self.ids)
        camp = {"id": cid, "alias": body["alias"], "name": body["name"], "type": body.get("type", "position"), "state": "active",
                "group_id": int(body["group_id"]) if body.get("group_id") is not None else None, "domain_id": body.get("domain_id"),
                "traffic_source_id": body.get("traffic_source_id"), "created_at": _now(), "updated_at": _now()}
        self.campaigns[cid] = camp
        return camp

    def _create_stream(self, body: dict) -> dict:
        sid = next(self.ids)
        stream = {"id": sid, "type": body["type"], "name": body["name"], "campaign_id": body["campaign_id"],
                  "position": body.get("position", len([s for s in self.streams.values() if s["campaign_id"] == body["campaign_id"]]) + 1),
                  "weight": 0, "action_options": body.get("action_options"), "comments": None, "state": body.get("state", "active"),
                  "updated_at": _now(), "action_type": body["action_type"], "action_payload": body.get("action_payload", ""),
                  "schema": body["schema"], "collect_clicks": True, "filter_or": bool(body.get("filter_or", False)),
                  "filters": [{"id": next(self.link_ids), "stream_id": sid, "name": f["name"], "mode": f["mode"], "payload": list(f.get("payload") or [])}
                              for f in body.get("filters") or []],
                  "triggers": [], "landings": [], "offer_selection": "before_click", "offers": []}
        stream["offers"] = [self._link(sid, o["offer_id"], o["share"], o.get("state", "active")) for o in body.get("offers") or []]
        self.streams[sid] = stream
        return stream

    def handle(self, method: str, path: str, params: dict, body) -> tuple[int, object]:
        for hook_method, hook_path, fn in list(self.hooks):
            if hook_method == method and re.fullmatch(hook_path, path):
                fn()
        m = re.fullmatch
        if method == "GET":
            if path == "/offers":
                return 200, list(self.offers.values())
            if g := m(r"/offers/(\d+)", path):
                o = self.offers.get(int(g[1]))
                return (200, o) if o else (404, {"error": "Offer not found"})
            if path == "/domains":
                return 200, self.domains
            if path == "/groups":
                return 200, [g for g in self.groups if g["type"] == params.get("type", "campaigns")]
            if path == "/traffic_sources":
                return 200, self.sources
            if path == "/campaigns":
                offset, limit = int(params.get("offset", 0)), int(params.get("limit", 1000))
                return 200, list(self.campaigns.values())[offset:offset + limit]
            if g := m(r"/campaigns/(\d+)", path):
                c = self.campaigns.get(int(g[1]))
                return (200, c) if c else (404, {"error": "Campaign not found"})
            if g := m(r"/campaigns/(\d+)/streams", path):
                cid = int(g[1])
                if cid not in self.campaigns:
                    return 404, {"error": "Campaign not found"}
                return 200, sorted((s for s in self.streams.values() if s["campaign_id"] == cid), key=lambda s: s["position"])
            if g := m(r"/streams/(\d+)", path):
                s = self.streams.get(int(g[1]))
                return (200, s) if s else (404, {"error": "Stream not found"})
        if method == "POST" and path == "/campaigns":
            if not isinstance(body, dict) or not body.get("alias") or not body.get("name"):
                return 400, {"error": "alias and name are required"}
            if any(c["alias"] == body["alias"] for c in self.campaigns.values()):
                return 400, {"error": "Alias already exists"}
            return 200, self._create_campaign(body)
        if method == "POST" and path == "/streams":
            for f in ("campaign_id", "schema", "type", "name", "action_type"):
                if not isinstance(body, dict) or body.get(f) in (None, ""):
                    return 400, {"error": f"{f} is required"}
            if body["campaign_id"] not in self.campaigns:
                return 400, {"error": "Campaign not found"}
            if body.get("offers") and (err := self._validate_offers(body["offers"])):
                return err
            return 200, self._create_stream(body)
        if method == "PUT" and (g := m(r"/streams/(\d+)", path)):
            s = self.streams.get(int(g[1]))
            if not s:
                return 404, {"error": "Stream not found"}
            if "offers" in body:
                if err := self._validate_offers(body["offers"]):
                    return err
                s["offers"] = [self._link(s["id"], o["offer_id"], o["share"], o.get("state", "active")) for o in body["offers"]]
            for key, value in self.put_side_effects.items():
                s[key] = copy.deepcopy(value)
            s["updated_at"] = _now()
            return 200, s
        return 404, {"error": f"Not found: {method} {path}"}

    # --- транспорт для тестов ---

    def _take_fault(self, method: str, path: str) -> Fault | None:
        for i, f in enumerate(self.faults):
            if f.method == method and re.fullmatch(f.path, path):
                return self.faults.pop(i)
        return None

    def mock_handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(PREFIX)
        self.calls.append((request.method, path))  # каждый запрос, включая отклонённые до обработки
        if request.headers.get("Api-Key") != self.api_key:
            return httpx.Response(401, json={"error": "Access denied"})
        fault = self._take_fault(request.method, path)
        if fault and fault.mode == "timeout_before":
            raise httpx.ConnectTimeout("fake connect timeout", request=request)
        if fault and fault.mode == "auth":
            return httpx.Response(403, json={"error": "Forbidden"})
        if fault and fault.mode == "error_500_before":
            return httpx.Response(500, json={"error": "Internal error"})
        if fault and fault.mode == "invalid_json":
            return httpx.Response(200, content=b"<html>oops</html>")
        if fault and fault.mode == "malformed_before":
            return httpx.Response(200, json={"ok": True})
        if fault and fault.mode == "error_400":
            return httpx.Response(400, json={"error": "Validation failed"})
        body = json.loads(request.content) if request.content else None
        status, data = self.handle(request.method, path, dict(request.url.params), copy.deepcopy(body))
        if fault and fault.mode == "timeout_after":
            raise httpx.ReadTimeout("fake read timeout after apply", request=request)
        if fault and fault.mode == "drop_after":
            raise httpx.RemoteProtocolError("connection dropped after apply", request=request)
        if fault and fault.mode == "error_500_after":
            return httpx.Response(502, json={"error": "Bad gateway"})
        if fault and fault.mode == "malformed_after":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(status, json=data)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.mock_handler)


def create_app(fake: FakeKeitaro | None = None):
    """ASGI-приложение фейкового Keitaro для локального прогона UI."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    fake = fake or FakeKeitaro().seed_demo()
    app = FastAPI(title="Fake Keitaro (local dev only)")

    @app.api_route(PREFIX + "/{path:path}", methods=["GET", "POST", "PUT"])
    async def api(path: str, request: Request):
        if request.headers.get("Api-Key") != fake.api_key:
            return JSONResponse({"error": "Access denied"}, status_code=401)
        raw = await request.body()
        fake.calls.append((request.method, "/" + path))
        status, data = fake.handle(request.method, "/" + path, dict(request.query_params), json.loads(raw) if raw else None)
        return JSONResponse(data, status_code=status)

    @app.post("/__fake/streams/{stream_id}/remove_offer/{offer_id}")
    def external_remove(stream_id: int, offer_id: int):
        fake.external_remove_offer(stream_id, offer_id)
        return {"offers": fake.stream_offers(stream_id)}

    @app.post("/__fake/put_side_effects")
    async def set_side_effects(request: Request):
        fake.put_side_effects = await request.json()
        return fake.put_side_effects

    @app.get("/__fake/streams/{stream_id}")
    def show(stream_id: int):
        return fake.streams[stream_id]

    app.state.fake = fake
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8090)
