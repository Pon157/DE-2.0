from typing import Any
from aiogram import Bot


def btn(text: str, *, callback_data: str | None = None, url: str | None = None,
        style: str | None = None, icon: str | None = None) -> dict:
    """Инлайн-кнопка с поддержкой премиум-иконки и стиля."""
    b: dict[str, Any] = {"text": text}
    if callback_data:
        b["callback_data"] = callback_data
    if url:
        b["url"] = url
    if style:
        b["style"] = style                      # primary / success / danger
    if icon:
        b["icon_custom_emoji_id"] = icon
    return b


async def send_rich(bot: Bot, chat_id: int | str, text: str,
                    keyboard: list[list[dict]] | None = None,
                    photo: str | None = None, **kw) -> dict:
    """sendMessage/sendPhoto через сырой API — поддерживает style и icon_custom_emoji_id."""
    payload: dict[str, Any] = {"chat_id": chat_id, "parse_mode": "HTML", **kw}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    if photo:
        payload.update(photo=photo, caption=text)
        method = "sendPhoto"
    else:
        payload["text"] = text
        method = "sendMessage"
    return await bot.session.make_request_raw(bot, method, payload) \
        if hasattr(bot.session, "make_request_raw") else \
        await _fallback(bot, method, payload)


async def _fallback(bot: Bot, method: str, payload: dict) -> dict:
    import json, aiohttp
    if "reply_markup" in payload:
        payload["reply_markup"] = json.dumps(payload["reply_markup"])
    url = f"https://api.telegram.org/bot{bot.token}/{method}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload) as r:
            return await r.json()
