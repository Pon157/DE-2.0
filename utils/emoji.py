# Каталог премиум-эмодзи (пак News Emoji). Использование в тексте: em("fire")
# Использование на кнопках: см. styled_button() ниже — само распознаёт эмодзи
# в начале текста кнопки и проставляет icon_custom_emoji_id (Bot API 9.4).
from aiogram.types import InlineKeyboardButton

EMOJI_IDS = {
    "eyes": "5210956306952758910", "smile": "5461117441612462242",
    "zap": "5456140674028019486", "comet": "5224607267797606837",
    "bags": "5229064374403998351", "no_entry": "5260293700088511294",
    "prohibited": "5240241223632954241", "excl": "5274099962655816924",
    "double_excl": "5440660757194744323", "interrobang": "5314504236132747481",
    "question": "5436113877181941026", "warn": "5447644880824181073",
    "warn2": "5420323339723881652", "globe": "5447410659077661506",
    "speech": "5443038326535759644", "thought": "5467538555158943525",
    "question2": "5452069934089641166", "chart": "5231200819986047254",
    "up": "5449683594425410231", "down": "5447183459602669338",
    "candle": "5451882707875276247", "chart_up": "5244837092042750681",
    "chart_down": "5246762912428603768", "check": "5206607081334906820",
    "cross": "5210952531676504517", "cool": "5222079954421818267",
    "bell": "5458603043203327669", "sunglasses": "5391112412445288650",
    "clown": "5269531045165816230", "lips": "5395444514028529554",
    "pin": "5397782960512444700", "dollar": "5409048419211682843",
    "money_wings": "5233326571099534068", "money2": "5231449120635370684",
    "money3": "5278751923338490157", "money4": "5290017777174722330",
    "money5": "5231005931550030290", "currency_exchange": "5402186569006210455",
    "play": "5264919878082509254", "red": "5411225014148014586",
    "green": "5416081784641168838", "arrow_right": "5416117059207572332",
    "fire": "5424972470023104089", "boom": "5276032951342088188",
    "mic": "5294339927318739359", "mic2": "5224736245665511429",
    "megaphone": "5424818078833715060", "shush": "5431609822288033666",
    "thumb_down": "5449875686837726134", "loudspeaker": "5460795800101594035",
    "search": "5231012545799666522", "shield": "5251203410396458957",
    "link": "5271604874419647061", "pc": "5282843764451195532",
    "copyright": "5323442290708985472", "info": "5334544901428229844",
    "thumb_up": "5337080053119336309", "play2": "5348125953090403204",
    "pause": "5359543311897998264", "hundred": "5341498088408234504",
    "refresh": "5375338737028841420", "top": "5415655814079723871",
    "new": "5382357040008021292", "soon": "5440621591387980068",
    "pin2": "5391032818111363540", "plus": "5397916757333654639",
    "gem": "5427168083074628963", "star": "5438496463044752972",
    "sparkles": "5325547803936572038", "crown": "5217822164362739968",
    "trash": "5445267414562389170", "bookmark": "5222444124698853913",
    "envelope": "5253742260054409879", "lock": "5296369303661067030",
    "surprised": "5303479226882603449", "clip": "5305265301917549162",
    "gear": "5341715473882955310", "gamepad": "5361741454685256344",
    "speaker": "5388632425314140043", "hourglass": "5386367538735104399",
    "down_arrow": "5406745015365943482", "sun": "5402477260982731644",
    "cloud": "5399913388845322366", "moon": "5449569374065152798",
    "snow": "5449449325434266744", "rainbow": "5409109841538994759",
    "droplet": "5393512611968995988", "calendar": "5413879192267805083",
    "bulb": "5422439311196834318", "gold": "5440539497383087970",
    "silver": "5447203607294265305", "bronze": "5453902265922376865",
    "note": "5463107823946717464", "free": "5406756500108501710",
    "pencil": "5395444784611480792", "siren": "5395695537687123235",
    "shopping": "5406683434124859552", "home": "5416041192905265756",
    "flag": "5460755126761312667", "party": "5461151367559141950",
}

# Юникод-символ (как он реально встречается в текстах кнопок по всему
# проекту) -> ключ в EMOJI_IDS. Это позволяет автоматически "апгрейдить"
# уже написанные кнопки до премиум-эмодзи, не переписывая каждый вызов
# вручную — см. styled_button()/strip_icon() ниже.
UNICODE_TO_KEY = {
    "👀": "eyes", "🙂": "smile", "⚡️": "zap", "⚡": "zap", "☄️": "comet",
    "🛍": "shopping", "⛔️": "no_entry", "⛔": "no_entry", "🚫": "prohibited",
    "❗️": "excl", "❗": "excl", "‼️": "double_excl", "⁉️": "interrobang",
    "❓": "question", "⚠️": "warn", "⚠": "warn", "🌐": "globe", "💬": "speech",
    "💭": "thought", "📊": "chart", "🔼": "up", "🔽": "down", "🕯": "candle",
    "📈": "chart_up", "📉": "chart_down", "✔️": "check", "✅": "check",
    "❌": "cross", "🆒": "cool", "🔔": "bell", "🥸": "sunglasses",
    "🤡": "clown", "🫦": "lips", "📌": "pin", "💵": "dollar",
    "💸": "money_wings", "💱": "currency_exchange", "▶️": "play", "▶": "play",
    "🔴": "red", "🟢": "green", "➡️": "arrow_right", "🔥": "fire",
    "💥": "boom", "🎙": "mic", "🎤": "mic2", "📣": "megaphone",
    "🤫": "shush", "👎": "thumb_down", "🗣️": "loudspeaker", "🗣": "loudspeaker",
    "🔍": "search", "🛡": "shield", "🔗": "link", "🖥": "pc", "©": "copyright",
    "ℹ️": "info", "ℹ": "info", "👍": "thumb_up", "⏸": "pause",
    "💯": "hundred", "🔄": "refresh", "🔝": "top", "🆕": "new",
    "🔜": "soon", "📍": "pin2", "➕": "plus", "💎": "gem", "⭐️": "star",
    "⭐": "star", "✨": "sparkles", "👑": "crown", "🗑": "trash",
    "🗑️": "trash", "🔖": "bookmark", "✉️": "envelope", "✉": "envelope",
    "🔒": "lock", "😮": "surprised", "📎": "clip", "⚙️": "gear", "⚙": "gear",
    "🎮": "gamepad", "🔈": "speaker", "⌛": "hourglass", "⬇️": "down_arrow",
    "☀️": "sun", "🌧": "cloud", "🌛": "moon", "❄️": "snow",
    "🌈": "rainbow", "💧": "droplet", "🗓": "calendar", "💡": "bulb",
    "🥇": "gold", "🥈": "silver", "🥉": "bronze", "🎵": "note",
    "🆓": "free", "✏️": "pencil", "🚨": "siren", "🏠": "home",
    "🚩": "flag", "🎉": "party",
    # доп. эмодзи, часто встречающиеся в текстах кнопок этого проекта —
    # ближайшие смысловые аналоги из того же пака
    "🤖": "gear", "👥": "loudspeaker", "🔘": "gear", "⏯": "play",
    "🏷": "bookmark", "🧵": "link", "📡": "globe", "🎨": "sparkles",
    "💳": "money_wings", "🎯": "flag", "🎁": "party", "🔙": "arrow_right",
    "⬅️": "arrow_right", "⬅": "arrow_right",
}

# Стиль (цвет) кнопки Bot API 9.4 по смыслу эмодзи — чисто визуальная
# подсказка, не требует настройки от владельца бота.
DANGER_EMOJI = {"🗑", "🗑️", "❌", "🚫", "⛔️", "⛔", "🔴"}
SUCCESS_EMOJI = {"✅", "✔️", "🎉", "💳", "🟢"}


def em(name: str) -> str:
    """Возвращает HTML премиум-эмодзи для использования В ТЕКСТЕ сообщения
    (<tg-emoji>), с юникод-фолбэком если ID не найден."""
    eid = EMOJI_IDS.get(name)
    fb = _FALLBACK_CHAR.get(name, "▫️")
    if not eid:
        return fb
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'


_FALLBACK_CHAR = {v: k for k, v in UNICODE_TO_KEY.items()}


def strip_icon(text: str) -> tuple[str, str | None]:
    """'🗑 Удалить' -> ('Удалить', '<id>'). Если эмодзи в начале текста не
    распознан — возвращает текст как есть и None вместо id."""
    parts = text.split(" ", 1)
    if len(parts) == 2 and parts[0] in UNICODE_TO_KEY:
        key = UNICODE_TO_KEY[parts[0]]
        return parts[1], EMOJI_IDS.get(key)
    return text, None


def button_style_hint(text: str) -> str | None:
    first = text.split(" ", 1)[0] if text else ""
    if first in DANGER_EMOJI:
        return "danger"
    if first in SUCCESS_EMOJI:
        return "success"
    return None


def styled_button(text: str, callback_data: str | None = None, url: str | None = None,
                  style: str | None = None) -> InlineKeyboardButton:
    """Строит InlineKeyboardButton с автоматическим премиум-эмодзи
    (icon_custom_emoji_id) вместо юникод-эмодзи в начале текста, плюс
    авто-подобранным цветом (Bot API 9.4), если style не передан явно.
    Используется ВЕЗДЕ вместо голого InlineKeyboardButton(...), чтобы у
    всех кнопок бота (и в конструкторе, и в дочерних ботах) были премиум-
    иконки, а не просто юникод-эмодзи внутри текста."""
    clean_text, icon = strip_icon(text)
    final_style = style if style is not None else button_style_hint(text)
    kwargs = {"text": clean_text or text, "style": final_style, "icon_custom_emoji_id": icon}
    if callback_data is not None:
        return InlineKeyboardButton(callback_data=callback_data, **kwargs)
    return InlineKeyboardButton(url=url, **kwargs)
