# services/scenario_runner.py
"""Исполнитель сценариев.

Точка входа — run_step(m, bot, bot_db_id):
  Если у пользователя есть активная ScenarioSession (waiting_input=True) —
  берём его ввод и продвигаем граф. Вызывать из user_message РАНЬШЕ
  остальной логики (relay, анкеты и т.п.).

  Если сценария нет — возвращает False, обработка продолжается обычным путём.

  Возвращает True если сообщение было «поглощено» сценарием (дальше
  обычный relay не нужен).

trigger_scenario(bot_id, user_id, scenario_id, bot, m) —
  запустить сценарий с начала (из хендлера кнопки / команды / /start).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from datetime import datetime
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import select

from db.base import Session
from db.models import (ScenarioSession, ScenarioNode, ScenarioEdge,
                       Scenario, ChildBot)

log = logging.getLogger("services.scenario_runner")

# ---------- безопасность HTTP-узла ----------

_SSRF_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata
    ipaddress.ip_network("100.64.0.0/10"),     # Carrier-grade NAT
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),         # link-local IPv6
]
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5, connect=3)
_MAX_RESPONSE_BYTES = 4096
_ALLOWED_HTTP_METHODS = {"GET", "POST"}
_MAX_STEPS_PER_CALL = 30
_MAX_VARIABLES = 32
_MAX_VARIABLE_LEN = 1024
# Максимальная длина regex-паттерна — защита от ReDoS
_MAX_REGEX_LEN = 128


def _is_safe_url(url: str) -> tuple[bool, str]:
    """SSRF-защита: проверяет схему, резолвит DNS и убеждается что
    ни один из полученных IP не попадает в закрытые сети.

    ВАЖНО: проверка выполняется до запроса, но aiohttp по умолчанию следует
    редиректам (до 10 штук) — сервер может отдать 302 → внутренний адрес.
    Поэтому _execute_http передаёт allow_redirects=False и вручную обрабатывает
    ответы 3xx: резолвим Location-header и повторно проверяем через эту функцию.
    """
    if not url or len(url) > 512:
        return False, "URL слишком длинный или пустой"
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Некорректный URL"
    if parsed.scheme != "https":
        return False, "Разрешён только https://"
    host = parsed.hostname
    if not host:
        return False, "Не удалось извлечь хост из URL"
    # Запрещаем IP-адреса напрямую (только доменные имена)
    try:
        ip_direct = ipaddress.ip_address(host)
        for net in _SSRF_NETWORKS:
            if ip_direct in net:
                return False, f"Прямой IP {host} в закрытой сети"
        return False, "Прямые IP-адреса запрещены — используйте доменное имя"
    except ValueError:
        pass  # это доменное имя — ок, резолвим дальше
    # Резолвим DNS синхронно (кратко, в пределах одного asyncio-шага)
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, f"Не удалось разрешить хост: {host}"
    if not infos:
        return False, f"DNS не вернул адресов для: {host}"
    for info in infos:
        addr_str = info[4][0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        for net in _SSRF_NETWORKS:
            if ip in net:
                return False, f"Адрес {addr_str} ({host}) попадает в закрытую сеть"
    return True, ""


def _render_template(template: str, variables: dict) -> str:
    r"""Подставляет {{variable_name}} из variables в строку шаблона.

    Безопасность:
    - Имена переменных ограничены \w+ (только буквы/цифры/underscore) —
      нельзя написать {{__class__}} или {{config.SECRET}}.
    - str(value) гарантирует что любой тип становится строкой, никакого eval.
    - Шаблон не может «вырваться» наружу и получить доступ к globals/locals.
    """
    if not template:
        return ""
    def replace(m):
        key = m.group(1).strip()
        # Только простые идентификаторы — никаких точек, скобок, dunder
        if not re.match(r'^\w{1,64}$', key):
            return m.group(0)   # неизвестный паттерн — оставляем как есть
        val = variables.get(key, "")
        return str(val)[:_MAX_VARIABLE_LEN]
    return re.sub(r"\{\{(\w{1,64})\}\}", replace, template)


# ---------- ядро исполнителя ----------

async def _get_session(bot_db_id: int, user_id: int) -> ScenarioSession | None:
    async with Session() as s:
        return await s.scalar(select(ScenarioSession).where(
            ScenarioSession.bot_id == bot_db_id,
            ScenarioSession.user_id == user_id))


async def _get_node(node_id: int) -> ScenarioNode | None:
    async with Session() as s:
        return await s.get(ScenarioNode, node_id)


async def _next_edges(node_id: int, scenario_id: int) -> list[ScenarioEdge]:
    async with Session() as s:
        return list((await s.scalars(select(ScenarioEdge).where(
            ScenarioEdge.from_node_id == node_id,
            ScenarioEdge.scenario_id == scenario_id))).all())


async def _send_node_message(bot: Bot, user_id: int, cfg: dict,
                             variables: dict) -> None:
    """Отправить сообщение из узла типа message."""
    text = _render_template(cfg.get("text", ""), variables)
    photo = cfg.get("photo_file_id")
    if not text and not photo:
        return
    try:
        if photo:
            if len(text) <= 1024:
                await bot.send_message(user_id, text or "\u200b")  # zero-width space
                # Telegram не разрешает пустой caption — не передаём
                await bot.send_photo(user_id, photo, caption=text if text else None)
            else:
                await bot.send_photo(user_id, photo)
                # Разбиваем длинный текст на чанки
                for i in range(0, len(text), 4096):
                    await bot.send_message(user_id, text[i:i + 4096])
        else:
            for i in range(0, len(text), 4096):
                await bot.send_message(user_id, text[i:i + 4096])
    except Exception as e:
        log.warning("scenario_runner._send_node_message: %s", e)


async def _execute_http(cfg: dict, variables: dict) -> dict:
    """Выполнить HTTP-запрос из узла типа http.

    Безопасность:
    - allow_redirects=False — сами обрабатываем 3xx и проверяем Location
      через _is_safe_url (иначе сервер мог отдать redirect → 169.254.x.x).
    - Заголовки строго фильтруем — блокируем служебные и длинные значения.
    - Тело запроса ограничено по длине.
    - Читаем не более _MAX_RESPONSE_BYTES из ответа.
    """
    url_template = cfg.get("url", "")
    url = _render_template(url_template, variables)
    method = (cfg.get("method") or "POST").upper()
    if method not in _ALLOWED_HTTP_METHODS:
        return {"status": -1, "body": f"Метод {method} не разрешён"}

    ok, reason = _is_safe_url(url)
    if not ok:
        log.warning("scenario_runner: небезопасный URL '%s': %s", url, reason)
        return {"status": -1, "body": f"Небезопасный URL: {reason}"}

    body_template = cfg.get("body_template", "")
    raw_body = _render_template(body_template, variables) if body_template else ""
    # Ограничиваем тело запроса
    body = raw_body[:8192] if raw_body else None

    # Строгая фильтрация заголовков
    BLOCKED_HEADERS = {
        "host", "content-length", "transfer-encoding", "connection",
        "x-forwarded-for", "x-real-ip", "x-forwarded-host",
        "x-forwarded-proto", "x-original-url", "x-rewrite-url",
        "proxy-authorization", "proxy-connection",
    }
    raw_headers = cfg.get("headers") or {}
    headers: dict[str, str] = {}
    for k, v in raw_headers.items():
        k_lower = str(k).lower().strip()
        if k_lower in BLOCKED_HEADERS:
            continue
        if len(k) > 64 or len(str(v)) > 512:
            continue   # аномально длинные — игнорируем
        headers[str(k)[:64]] = str(v)[:512]
    headers["User-Agent"] = "DialogEngine-Scenario/1.0"

    # Коннектор без DNS-кеширования — защита от DNS rebinding
    # (resolver резолвит каждый запрос заново, но мы уже проверили выше)
    connector = aiohttp.TCPConnector(use_dns_cache=False)

    try:
        async with aiohttp.ClientSession(
            timeout=_HTTP_TIMEOUT,
            connector=connector,
            # Редиректы отключены — обрабатываем вручную
            trust_env=False,
        ) as sess:
            req_kwargs: dict = {"headers": headers, "allow_redirects": False}
            if method == "POST":
                req_kwargs["data"] = body or ""
            async with sess.request(method, url, **req_kwargs) as resp:
                # Обрабатываем редирект вручную — проверяем Location
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    ok2, reason2 = _is_safe_url(location)
                    if not ok2:
                        log.warning("scenario_runner: редирект на небезопасный URL '%s': %s",
                                    location, reason2)
                        return {"status": -1, "body": f"Редирект заблокирован: {reason2}"}
                    # Один уровень редиректа — следуем вручную
                    async with sess.request(method, location,
                                            headers=headers,
                                            allow_redirects=False,
                                            data=(body or "") if method == "POST" else None
                                            ) as resp2:
                        raw = await resp2.content.read(_MAX_RESPONSE_BYTES)
                        body_str = raw.decode("utf-8", errors="replace")
                        return {"status": resp2.status, "body": body_str[:_MAX_VARIABLE_LEN]}
                raw = await resp.content.read(_MAX_RESPONSE_BYTES)
                body_str = raw.decode("utf-8", errors="replace")
                return {"status": resp.status, "body": body_str[:_MAX_VARIABLE_LEN]}
    except aiohttp.ClientError as e:
        return {"status": -1, "body": str(e)[:256]}
    except Exception as e:
        log.warning("scenario_runner._execute_http unexpected: %s", e)
        return {"status": -1, "body": "Внутренняя ошибка запроса"}
    finally:
        await connector.close()


def _evaluate_condition(cfg: dict, variables: dict) -> bool:
    """Вычислить условие ветвления.

    Безопасность:
    - regex: ограничиваем длину паттерна (_MAX_REGEX_LEN) — защита от ReDoS.
      Значение переменной усекаем до 512 символов перед матчингом.
    - gt/lt: только числа через float() — никакого eval.
    - Любое исключение → False (fail-safe).
    """
    var_name = cfg.get("variable", "")
    operator = cfg.get("operator", "eq")
    expected = str(cfg.get("value", ""))
    actual = str(variables.get(var_name, ""))[:512]  # усекаем значение

    if operator == "eq":
        return actual.lower() == expected.lower()
    if operator == "contains":
        return expected.lower() in actual.lower()
    if operator == "regex":
        if len(expected) > _MAX_REGEX_LEN:
            log.warning("scenario_runner: regex слишком длинный (%d символов), пропускаем",
                        len(expected))
            return False
        try:
            # re.search с ограниченной строкой — безопасно при коротком паттерне
            return bool(re.search(expected, actual, re.IGNORECASE))
        except re.error as e:
            log.warning("scenario_runner: невалидный regex '%s': %s", expected[:32], e)
            return False
    if operator == "gt":
        try:
            return float(actual) > float(expected)
        except (ValueError, OverflowError):
            return False
    if operator == "lt":
        try:
            return float(actual) < float(expected)
        except (ValueError, OverflowError):
            return False
    return False


async def _advance(session: ScenarioSession, bot: Bot,
                   user_message_text: str | None = None,
                   steps_left: int = _MAX_STEPS_PER_CALL) -> None:
    """Продвигает сессию вперёд по графу от текущего узла.

    Если узел ждёт ввода (input) и user_message_text задан — сохраняет
    значение и идёт дальше. Останавливается на следующем input-узле или
    когда граф заканчивается (узел end / нет исходящих рёбер).
    """
    if steps_left <= 0:
        log.warning("scenario_runner: достигнут лимит шагов (бесконечный цикл?) "
                    "session_id=%s", session.id)
        await _end_session(session)
        return

    node = await _get_node(session.current_node_id)
    if not node:
        await _end_session(session)
        return

    variables = json.loads(session.variables_json or "{}")

    # --- если ждём ввода пользователя ---
    if session.waiting_input:
        if user_message_text is None:
            return  # ещё не пришло — ждём
        var_name = session.input_variable or "_input"
        # Ограничиваем длину и количество переменных
        value = user_message_text[:_MAX_VARIABLE_LEN]
        if len(variables) < _MAX_VARIABLES:
            variables[var_name] = value
        async with Session() as s:
            sess2 = await s.get(ScenarioSession, session.id)
            if not sess2:
                return
            sess2.variables_json = json.dumps(variables, ensure_ascii=False)
            sess2.waiting_input = False
            sess2.input_variable = None
            sess2.last_step_at = datetime.utcnow()
            await s.commit()
        # Перечитываем обновлённое состояние
        async with Session() as s:
            session = await s.get(ScenarioSession, session.id)
        if not session:
            return
        # Идём к следующим рёбрам
        edges = await _next_edges(node.id, session.scenario_id)
        if not edges:
            await _end_session(session)
            return
        next_node_id = edges[0].to_node_id
        async with Session() as s:
            sess2 = await s.get(ScenarioSession, session.id)
            if not sess2:
                return
            sess2.current_node_id = next_node_id
            await s.commit()
        async with Session() as s:
            session = await s.get(ScenarioSession, session.id)
        if not session:
            return
        await _advance(session, bot, steps_left=steps_left - 1)
        return

    # --- выполняем текущий узел ---
    cfg = json.loads(node.config_json or "{}")
    ntype = node.node_type

    if ntype == "trigger":
        pass  # триггер-узел — просто стартовая точка, ничего не делаем

    elif ntype == "message":
        await _send_node_message(bot, session.user_id, cfg, variables)

    elif ntype == "input":
        # Отправить prompt (если есть) и встать в ожидание
        prompt = cfg.get("prompt")
        if prompt:
            text = _render_template(prompt, variables)
            try:
                await bot.send_message(session.user_id, text)
            except Exception as e:
                log.warning("scenario_runner input prompt: %s", e)
        async with Session() as s:
            sess2 = await s.get(ScenarioSession, session.id)
            if not sess2:
                return
            sess2.waiting_input = True
            sess2.input_variable = cfg.get("variable_name", "_input")
            sess2.last_step_at = datetime.utcnow()
            await s.commit()
        return  # ждём следующего сообщения от пользователя

    elif ntype == "condition":
        result = _evaluate_condition(cfg, variables)
        branch = "true" if result else "false"
        edges = await _next_edges(node.id, session.scenario_id)
        next_node_id = None
        for edge in edges:
            ecfg = json.loads(edge.condition_json or "{}")
            if ecfg.get("branch") == branch:
                next_node_id = edge.to_node_id
                break
        # Если не нашли подходящее ребро — конец
        if not next_node_id:
            await _end_session(session)
            return
        async with Session() as s:
            sess2 = await s.get(ScenarioSession, session.id)
            if not sess2:
                return
            sess2.current_node_id = next_node_id
            sess2.last_step_at = datetime.utcnow()
            await s.commit()
        async with Session() as s:
            session = await s.get(ScenarioSession, session.id)
        if not session:
            return
        await _advance(session, bot, steps_left=steps_left - 1)
        return

    elif ntype == "delay":
        import asyncio
        seconds = max(0, min(int(cfg.get("seconds", 0)), 300))  # не больше 5 мин
        await asyncio.sleep(seconds)

    elif ntype == "http":
        result = await _execute_http(cfg, variables)
        # Сохраняем результат в переменные
        out_var = cfg.get("output_variable", "_http_body")
        if len(variables) < _MAX_VARIABLES:
            variables[out_var] = result.get("body", "")
            variables["_http_status"] = str(result.get("status", -1))
        async with Session() as s:
            sess2 = await s.get(ScenarioSession, session.id)
            if not sess2:
                return
            sess2.variables_json = json.dumps(variables, ensure_ascii=False)
            await s.commit()
        async with Session() as s:
            session = await s.get(ScenarioSession, session.id)
        if not session:
            return

    elif ntype == "end":
        await _end_session(session)
        return

    # --- переходим к следующему узлу ---
    edges = await _next_edges(node.id, session.scenario_id)
    if not edges:
        await _end_session(session)
        return
    next_node_id = edges[0].to_node_id
    async with Session() as s:
        sess2 = await s.get(ScenarioSession, session.id)
        if not sess2:
            return
        sess2.current_node_id = next_node_id
        sess2.last_step_at = datetime.utcnow()
        await s.commit()
    async with Session() as s:
        session = await s.get(ScenarioSession, session.id)
    if not session:
        return
    await _advance(session, bot, steps_left=steps_left - 1)


async def _end_session(session: ScenarioSession) -> None:
    async with Session() as s:
        sess2 = await s.get(ScenarioSession, session.id)
        if sess2:
            await s.delete(sess2)
            await s.commit()


async def _find_trigger_node(scenario_id: int) -> ScenarioNode | None:
    """Найти стартовый узел типа trigger в сценарии."""
    async with Session() as s:
        return await s.scalar(select(ScenarioNode).where(
            ScenarioNode.scenario_id == scenario_id,
            ScenarioNode.node_type == "trigger"))


# ---------- публичный API ----------

async def run_step(m: Message, bot: Bot, bot_db_id: int) -> bool:
    """Вызывается из user_message. Возвращает True если сообщение поглощено
    сценарием (дальше обрабатывать не нужно).

    Если пользователь написал /cancel — принудительно завершаем сессию.
    """
    user_id = m.from_user.id

    session = await _get_session(bot_db_id, user_id)
    if not session:
        return False

    # /cancel всегда прерывает сценарий
    if m.text and m.text.strip().lower() in ("/cancel", "/отмена"):
        await _end_session(session)
        try:
            await m.answer("❌ Сценарий прерван.")
        except Exception:
            pass
        return True

    if not session.waiting_input:
        # Сессия есть, но мы не ждём ввода — защита от гонки состояний
        return True

    user_text = m.text or m.caption or ""
    try:
        await _advance(session, bot, user_message_text=user_text)
    except Exception:
        log.exception("scenario_runner.run_step: необработанная ошибка session=%s",
                      session.id)
    return True


async def trigger_scenario(bot_db_id: int, user_id: int,
                           scenario_id: int, bot: Bot) -> bool:
    """Запустить сценарий для пользователя. Возвращает False если:
    - пользователь уже в другом сценарии,
    - триггерный узел не найден.
    При запуске текущая сессия ЗАМЕНЯЕТСЯ новой (один пользователь — один
    активный сценарий одновременно).
    """
    # Проверяем Pro
    async with Session() as s:
        cfg = await s.get(ChildBot, bot_db_id)
        scenario = await s.get(Scenario, scenario_id)
    if not cfg or not scenario or scenario.bot_id != bot_db_id:
        return False

    from services import referrals
    if not await referrals.is_pro(cfg.owner_id):
        return False

    trigger_node = await _find_trigger_node(scenario_id)
    if not trigger_node:
        log.warning("trigger_scenario: нет trigger-узла в scenario_id=%s", scenario_id)
        return False

    # Удаляем старую сессию если есть, создаём новую
    async with Session() as s:
        old = await s.scalar(select(ScenarioSession).where(
            ScenarioSession.bot_id == bot_db_id,
            ScenarioSession.user_id == user_id))
        if old:
            await s.delete(old)
        sess = ScenarioSession(
            bot_id=bot_db_id,
            user_id=user_id,
            scenario_id=scenario_id,
            current_node_id=trigger_node.id,
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)

    try:
        await _advance(sess, bot)
    except Exception:
        log.exception("trigger_scenario: необработанная ошибка session=%s", sess.id)
    return True


async def find_matching_scenario(bot_db_id: int,
                                 text: str | None) -> Scenario | None:
    """Найти активный сценарий по тексту сообщения (keyword/command).
    Используется в user_message перед обычным relay."""
    if not text:
        return None
    async with Session() as s:
        scenarios = list((await s.scalars(select(Scenario).where(
            Scenario.bot_id == bot_db_id,
            Scenario.is_active.is_(True)))).all())
    for sc in scenarios:
        if sc.trigger_type.value == "command":
            cmd = text.strip().lstrip("/").split("@")[0].lower()
            if sc.trigger_value and cmd == sc.trigger_value.lower():
                return sc
        elif sc.trigger_type.value == "keyword":
            if sc.trigger_value and sc.trigger_value.lower() in text.lower():
                return sc
        elif sc.trigger_type.value == "button":
            if sc.trigger_value and text.strip() == sc.trigger_value:
                return sc
    return None
