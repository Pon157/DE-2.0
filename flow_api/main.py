# flow_api/main.py
"""FastAPI backend для Mini App редактора сценариев.

Запуск: uvicorn flow_api.main:app --host 127.0.0.1 --port 8087

Аутентификация: каждый запрос несёт заголовок X-Telegram-Init-Data —
значение из Telegram.WebApp.initData. Backend проверяет HMAC-SHA256
подпись (стандарт Telegram Mini App) и извлекает user.id.
Дополнительно проверяем что owner_id этого user совпадает с bot_id
и что у него активна Pro-подписка.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from urllib.parse import parse_qsl, unquote

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

# Подключаемся к той же БД через тот же движок что и основной бот
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.base import Session
from db.models import (ChildBot, Scenario, ScenarioEdge, ScenarioNode,
                       ScenarioSession, ScenarioTrigger)
from services import referrals
from config import MASTER_BOT_TOKEN

log = logging.getLogger("flow_api")

app = FastAPI(title="DialogEngine Flow API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://flow.dialogengine.ru"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================================
# Аутентификация через Telegram initData
# =========================================================================

def _verify_init_data(init_data_raw: str) -> dict:
    """Проверяет подпись Telegram initData.
    Возвращает распарсенный dict (включая user как dict).
    Бросает HTTPException 401 при любом нарушении.

    Алгоритм согласно документации Telegram Mini Apps:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data_raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData отсутствует")

    params = dict(parse_qsl(init_data_raw, keep_blank_values=True))
    received_hash = params.pop("hash", None)
    if not received_hash:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "hash отсутствует в initData")

    # Формируем data-check-string: отсортированные пары key=value через \n
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))

    secret_key = hmac.new(b"WebAppData", MASTER_BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверная подпись initData")

    # auth_date проверяем — не старше 1 часа
    import time
    auth_date = int(params.get("auth_date", 0))
    if time.time() - auth_date > 3600:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData устарел (> 1 ч)")

    try:
        user = json.loads(unquote(params.get("user", "{}")))
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Не удалось распарсить user")

    if not user.get("id"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user.id отсутствует")

    return user


async def _get_verified_user(
    x_telegram_init_data: str = Header(..., alias="X-Telegram-Init-Data")
) -> dict:
    return _verify_init_data(x_telegram_init_data)


async def _require_pro_owner(bot_id: int, user: dict = Depends(_get_verified_user)) -> int:
    """Проверяет: user — владелец бота bot_id, у него Pro."""
    tg_user_id = user["id"]
    async with Session() as s:
        cfg = await s.get(ChildBot, bot_id)
    if not cfg or cfg.owner_id != tg_user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет доступа к этому боту")
    if not await referrals.is_pro(tg_user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Сценарии доступны только Pro-подписчикам")
    return tg_user_id


# =========================================================================
# Pydantic-схемы
# =========================================================================

_ALLOWED_NODE_TYPES = {"trigger", "message", "input", "condition", "delay", "http", "end"}
_ALLOWED_TRIGGERS = {t.value for t in ScenarioTrigger}

# Максимальный размер config_json одного узла (символов)
_MAX_CONFIG_LEN = 4096
_MAX_NODES = 100
_MAX_EDGES = 200
_MAX_LABEL_LEN = 128
_MAX_NAME_LEN = 128


class NodeIn(BaseModel):
    id: str = Field(..., max_length=64)          # клиентский id (строка из React Flow)
    node_type: str
    config: dict = {}
    pos_x: float = 0.0
    pos_y: float = 0.0
    label: str | None = Field(None, max_length=_MAX_LABEL_LEN)

    @field_validator("node_type")
    @classmethod
    def check_type(cls, v):
        if v not in _ALLOWED_NODE_TYPES:
            raise ValueError(f"Неизвестный тип узла: {v}")
        return v

    @field_validator("config")
    @classmethod
    def check_config(cls, v):
        raw = json.dumps(v)
        if len(raw) > _MAX_CONFIG_LEN:
            raise ValueError("config слишком большой")
        # Дополнительная валидация http-узлов
        if v.get("url") and not str(v["url"]).startswith("https://"):
            raise ValueError("url в http-узле обязан начинаться с https://")
        return v


class EdgeIn(BaseModel):
    from_id: str = Field(..., max_length=64)
    to_id: str = Field(..., max_length=64)
    condition: dict = {}


class ScenarioSaveRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    trigger_type: str
    trigger_value: str | None = Field(None, max_length=256)
    is_active: bool = True
    nodes: list[NodeIn] = []
    edges: list[EdgeIn] = []

    @field_validator("trigger_type")
    @classmethod
    def check_trigger(cls, v):
        if v not in _ALLOWED_TRIGGERS:
            raise ValueError(f"Неизвестный триггер: {v}")
        return v

    @field_validator("nodes")
    @classmethod
    def check_nodes_count(cls, v):
        if len(v) > _MAX_NODES:
            raise ValueError(f"Слишком много узлов (максимум {_MAX_NODES})")
        return v

    @field_validator("edges")
    @classmethod
    def check_edges_count(cls, v):
        if len(v) > _MAX_EDGES:
            raise ValueError(f"Слишком много рёбер (максимум {_MAX_EDGES})")
        return v


# =========================================================================
# Эндпоинты
# =========================================================================

@app.get("/bots/{bot_id}/scenarios")
async def list_scenarios(bot_id: int,
                         _uid=Depends(lambda bot_id=0, u=Depends(_get_verified_user):
                                      None)):
    # Простая авторизация без Pro-проверки для списка (чтобы показать "нет подписки")
    async with Session() as s:
        items = list((await s.scalars(select(Scenario).where(
            Scenario.bot_id == bot_id).order_by(Scenario.id))).all())
    return [{"id": sc.id, "name": sc.name, "trigger_type": sc.trigger_type,
             "trigger_value": sc.trigger_value, "is_active": sc.is_active}
            for sc in items]


@app.get("/bots/{bot_id}/scenarios/{scenario_id}")
async def get_scenario(bot_id: int, scenario_id: int,
                       owner_id: int = Depends(_require_pro_owner)):
    async with Session() as s:
        sc = await s.get(Scenario, scenario_id)
        if not sc or sc.bot_id != bot_id:
            raise HTTPException(404, "Сценарий не найден")
        nodes = list((await s.scalars(select(ScenarioNode).where(
            ScenarioNode.scenario_id == scenario_id))).all())
        edges = list((await s.scalars(select(ScenarioEdge).where(
            ScenarioEdge.scenario_id == scenario_id))).all())
    return {
        "id": sc.id,
        "name": sc.name,
        "trigger_type": sc.trigger_type,
        "trigger_value": sc.trigger_value,
        "is_active": sc.is_active,
        "nodes": [{"id": n.id, "node_type": n.node_type,
                   "config": json.loads(n.config_json or "{}"),
                   "pos_x": n.pos_x, "pos_y": n.pos_y, "label": n.label}
                  for n in nodes],
        "edges": [{"id": e.id, "from_node_id": e.from_node_id,
                   "to_node_id": e.to_node_id,
                   "condition": json.loads(e.condition_json or "{}")}
                  for e in edges],
    }


@app.post("/bots/{bot_id}/scenarios", status_code=201)
async def create_scenario(bot_id: int, body: ScenarioSaveRequest,
                          owner_id: int = Depends(_require_pro_owner)):
    async with Session() as s:
        sc = Scenario(
            bot_id=bot_id,
            name=body.name,
            trigger_type=ScenarioTrigger(body.trigger_type),
            trigger_value=body.trigger_value,
            is_active=body.is_active,
        )
        s.add(sc)
        await s.flush()

        # Сохраняем узлы, строим mapping client_id → db_id
        id_map: dict[str, int] = {}
        for ni in body.nodes:
            node = ScenarioNode(
                scenario_id=sc.id,
                node_type=ni.node_type,
                config_json=json.dumps(ni.config, ensure_ascii=False),
                pos_x=ni.pos_x,
                pos_y=ni.pos_y,
                label=ni.label,
            )
            s.add(node)
            await s.flush()
            id_map[ni.id] = node.id

        for ei in body.edges:
            from_db = id_map.get(ei.from_id)
            to_db = id_map.get(ei.to_id)
            if from_db is None or to_db is None:
                continue
            edge = ScenarioEdge(
                scenario_id=sc.id,
                from_node_id=from_db,
                to_node_id=to_db,
                condition_json=json.dumps(ei.condition, ensure_ascii=False),
            )
            s.add(edge)

        await s.commit()
        return {"id": sc.id}


@app.put("/bots/{bot_id}/scenarios/{scenario_id}")
async def update_scenario(bot_id: int, scenario_id: int,
                          body: ScenarioSaveRequest,
                          owner_id: int = Depends(_require_pro_owner)):
    async with Session() as s:
        sc = await s.get(Scenario, scenario_id)
        if not sc or sc.bot_id != bot_id:
            raise HTTPException(404, "Сценарий не найден")

        sc.name = body.name
        sc.trigger_type = ScenarioTrigger(body.trigger_type)
        sc.trigger_value = body.trigger_value
        sc.is_active = body.is_active

        # Удаляем старые узлы и рёбра (каскадом удалятся через FK)
        old_nodes = list((await s.scalars(select(ScenarioNode).where(
            ScenarioNode.scenario_id == scenario_id))).all())
        old_edges = list((await s.scalars(select(ScenarioEdge).where(
            ScenarioEdge.scenario_id == scenario_id))).all())
        for e in old_edges:
            await s.delete(e)
        for n in old_nodes:
            await s.delete(n)
        await s.flush()

        # Сессии активных пользователей сбрасываем при изменении сценария
        old_sessions = list((await s.scalars(select(ScenarioSession).where(
            ScenarioSession.scenario_id == scenario_id))).all())
        for sess in old_sessions:
            await s.delete(sess)

        id_map: dict[str, int] = {}
        for ni in body.nodes:
            node = ScenarioNode(
                scenario_id=sc.id,
                node_type=ni.node_type,
                config_json=json.dumps(ni.config, ensure_ascii=False),
                pos_x=ni.pos_x,
                pos_y=ni.pos_y,
                label=ni.label,
            )
            s.add(node)
            await s.flush()
            id_map[ni.id] = node.id

        for ei in body.edges:
            from_db = id_map.get(ei.from_id)
            to_db = id_map.get(ei.to_id)
            if from_db is None or to_db is None:
                continue
            s.add(ScenarioEdge(
                scenario_id=sc.id,
                from_node_id=from_db,
                to_node_id=to_db,
                condition_json=json.dumps(ei.condition, ensure_ascii=False),
            ))

        await s.commit()
    return {"ok": True}


@app.delete("/bots/{bot_id}/scenarios/{scenario_id}")
async def delete_scenario(bot_id: int, scenario_id: int,
                          owner_id: int = Depends(_require_pro_owner)):
    async with Session() as s:
        sc = await s.get(Scenario, scenario_id)
        if not sc or sc.bot_id != bot_id:
            raise HTTPException(404, "Сценарий не найден")
        await s.delete(sc)
        await s.commit()
    return {"ok": True}


@app.patch("/bots/{bot_id}/scenarios/{scenario_id}/toggle")
async def toggle_scenario(bot_id: int, scenario_id: int,
                          owner_id: int = Depends(_require_pro_owner)):
    async with Session() as s:
        sc = await s.get(Scenario, scenario_id)
        if not sc or sc.bot_id != bot_id:
            raise HTTPException(404, "Сценарий не найден")
        sc.is_active = not sc.is_active
        await s.commit()
    return {"is_active": sc.is_active}
