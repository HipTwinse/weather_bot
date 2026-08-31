import asyncio
from datetime import datetime
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import zoneinfo

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from timezonefinder import TimezoneFinder

import config
from airport_resolver import resolve_airport
from database import add_position, delete_position, get_user_positions
from noaa_service import get_noaa_package
from openmeteo_service import fetch_openmeteo_forecast
from weather_synthesizer import (
    build_raw_data_package_dict,
    build_summary_caption,
    synthesize_forecast,
)

logger = logging.getLogger(__name__)
router = Router()

# Офлайн-движок поиска часовых поясов по координатам
tf = TimezoneFinder()

# Глобальное хранилище активных городов для радара
active_radar_cities: Set[str] = {"EGLC", "LFPB", "EDDM", "KJFK", "RKSI"}

# База отслеживаемых городов для радара
ALL_RADAR_CITIES = {
    "EGLC": "🇬🇧 Лондон (EGLC)",
    "LFPB": "🇫🇷 Париж (LFPB)",
    "EDDM": "🇩🇪 Мюнхен (EDDM)",
    "KJFK": "🇺🇸 Нью-Йорк (KJFK)",
    "RJTT": "🇯🇵 Токио (RJTT)",
    "RKSI": "🇰🇷 Сеул (RKSI)",
    "ZSPD": "🇨🇳 Шанхай (ZSPD)",
    "LEMD": "🇪🇸 Мадрид (LEMD)",
    "LIMC": "🇮🇹 Милан (LIMC)",
    "LTAC": "🇹🇷 Анкара (LTAC)",
    "NZWN": "🇳🇿 Веллингтон (NZWN)",
    "UHHH": "🇷🇺 Хабаровск (UHHH)",
}


# Состояния сканирования маркета и добавления позиций (FSM)
class MarketScanStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_balance = State()


class AddPositionStates(StatesGroup):
    waiting_for_city = State()
    waiting_for_outcomes = State()


# Главное меню Telegram
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🔍 Сканировать маркет"),
            KeyboardButton(text="📌 Мои позиции"),
        ],
        [
            KeyboardButton(text="⚙️ Радар аномалий"),
            KeyboardButton(text="🌍 Избранные города"),
        ],
        [
            KeyboardButton(text="📖 Справка / Помощь"),
        ],
    ],
    resize_keyboard=True,
)

# Интерактивная клавиатура с избранными торговыми городами
cities_inline_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 Лондон (EGLC)", callback_data="icao:EGLC"),
            InlineKeyboardButton(text="🇫🇷 Париж (LFPB)", callback_data="icao:LFPB"),
        ],
        [
            InlineKeyboardButton(text="🇯🇵 Токио (RJTT)", callback_data="icao:RJTT"),
            InlineKeyboardButton(text="🇰🇷 Сеул (RKSI)", callback_data="icao:RKSI"),
        ],
        [
            InlineKeyboardButton(text="🇨🇳 Шанхай (ZSPD)", callback_data="icao:ZSPD"),
            InlineKeyboardButton(text="🇩🇪 Мюнхен (EDDM)", callback_data="icao:EDDM"),
        ],
        [
            InlineKeyboardButton(text="🇪🇸 Мадрид (LEMD)", callback_data="icao:LEMD"),
            InlineKeyboardButton(text="🇮🇹 Милан (LIMC)", callback_data="icao:LIMC"),
        ],
        [
            InlineKeyboardButton(text="🇹🇷 Анкара (LTAC)", callback_data="icao:LTAC"),
            InlineKeyboardButton(text="🇳🇿 Веллингтон (NZWN)", callback_data="icao:NZWN"),
        ],
        [
            InlineKeyboardButton(text="🇷🇺 Хабаровск (UHHH)", callback_data="icao:UHHH"),
        ],
    ]
)

# Клавиатура выбора города для добавления позиции
position_city_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 Лондон (EGLC)", callback_data="pos_city:EGLC"),
            InlineKeyboardButton(text="🇫🇷 Париж (LFPB)", callback_data="pos_city:LFPB"),
        ],
        [
            InlineKeyboardButton(text="🇩🇪 Мюнхен (EDDM)", callback_data="pos_city:EDDM"),
            InlineKeyboardButton(text="🇺🇸 Нью-Йорк (KJFK)", callback_data="pos_city:KJFK"),
        ],
        [
            InlineKeyboardButton(text="🇮🇹 Милан (LIMC)", callback_data="pos_city:LIMC"),
            InlineKeyboardButton(text="🇪🇸 Мадрид (LEMD)", callback_data="pos_city:LEMD"),
        ],
        [
            InlineKeyboardButton(text="🇰🇷 Сеул (RKSI)", callback_data="pos_city:RKSI"),
            InlineKeyboardButton(text="🇯🇵 Токио (RJTT)", callback_data="pos_city:RJTT"),
        ],
    ]
)

# Быстрые кнопки для ввода баланса
balance_quick_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="$20 (Tier 1)", callback_data="set_bal:20"),
            InlineKeyboardButton(text="$40 (Tier 2)", callback_data="set_bal:40"),
            InlineKeyboardButton(text="$80 (Tier 3)", callback_data="set_bal:80"),
        ],
        [
            InlineKeyboardButton(text="$150 (Tier 4)", callback_data="set_bal:150"),
            InlineKeyboardButton(text="$500 (Tier 5)", callback_data="set_bal:500"),
            InlineKeyboardButton(text="$1,500 (Tier 6)", callback_data="set_bal:1500"),
        ],
    ]
)

CITY_KEYWORD_MAP = {
    "UHHH": ["khabarovsk", "хабаровск", "uhhh", "новый"],
    "EGLC": ["london", "лондон", "eglc", "city airport", "heathrow", "gatwick"],
    "LFPB": ["paris", "париж", "lfpb", "le bourget", "charles de gaulle", "orly"],
    "RJTT": ["tokyo", "токио", "rjtt", "haneda", "narita"],
    "RKSI": ["seoul", "сеул", "rksi", "incheon", "gimpo"],
    "ZSPD": ["shanghai", "шанхай", "zspd", "pudong", "hongqiao"],
    "EDDM": ["munich", "мюнхен", "eddm"],
    "LEMD": ["madrid", "мадрид", "lemd", "barajas"],
    "LIMC": ["milan", "милан", "limc", "malpensa", "linate"],
    "LTAC": ["ankara", "анкара", "ltac", "esenboga"],
    "NZWN": ["wellington", "веллингтон", "nzwn"],
    "KJFK": ["new york", "нью-йорк", "nyc", "kjfk", "jfk"],
    "KORD": ["chicago", "чикаго", "kord", "ohare"],
    "KMIA": ["miami", "майами", "kmia"],
    "KLAX": ["los angeles", "лос-анджелес", "klax", "lax"],
}

MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com/events"


def calculate_tier_sizing(balance: float) -> Tuple[str, float, str]:
    """Рассчитывает сайзинг строго по Прогрессивной сетке ставок (Roadmap to $3,000)."""
    if balance < 25.0:
        return "Tier 1 ($5 – $25)", 1.00, "$2.00 – $3.00 ($1.00 на исход)"
    elif balance < 50.0:
        return "Tier 2 ($25 – $50)", 2.50, "$3.00 – $5.00"
    elif balance < 100.0:
        return "Tier 3 ($50 – $100)", 5.00, "$6.00 – $10.00"
    elif balance < 300.0:
        return "Tier 4 ($100 – $300)", 10.00, "$12.00 – $25.00"
    elif balance < 1000.0:
        return "Tier 5 ($300 – $1,000)", 30.00, "$35.00 – $80.00"
    else:
        return "Tier 6 ($1,000 – $3,000)", 100.00, "$100.00 – $250.00"


def get_radar_keyboard() -> InlineKeyboardMarkup:
    """Динамически генерирует кнопки включения/выключения радара."""
    buttons = []
    row = []
    for icao, name in ALL_RADAR_CITIES.items():
        is_active = icao in active_radar_cities
        status_icon = "🟢 ВКЛ" if is_active else "🔴 ВЫКЛ"
        btn = InlineKeyboardButton(
            text=f"{status_icon} {name}",
            callback_data=f"toggle_radar:{icao}",
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _extract_airport_coords(airport_data: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    if not airport_data:
        return None, None, None
    lat = airport_data.get("lat") if airport_data.get("lat") is not None else airport_data.get("latitude")
    lon = airport_data.get("lon") if airport_data.get("lon") is not None else airport_data.get("longitude")
    tz = airport_data.get("timezone") or airport_data.get("iana_timezone")
    return lat, lon, tz


def _detect_city_icao(text_context: str) -> Optional[str]:
    normalized = text_context.lower()
    for icao, keywords in CITY_KEYWORD_MAP.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", normalized):
                return icao
    return None


def _detect_market_target_date(text_context: str) -> Optional[str]:
    normalized = text_context.lower()
    pattern = r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sep|sept|october|oct|november|nov|december|dec)\s+(\d{1,2})\b"
    match = re.search(pattern, normalized)
    if match:
        month_str = match.group(1)
        day_str = match.group(2)
        month_num = MONTH_NAMES.get(month_str)
        if month_num:
            day_num = int(day_str)
            current_year = datetime.now().year
            return f"{current_year}-{month_num:02d}-{day_num:02d}"

    iso_match = re.search(r"\b(202\d-\d{2}-\d{2})\b", normalized)
    if iso_match:
        return iso_match.group(1)

    return None


def _parse_coordinates(text: str) -> Optional[Tuple[float, float]]:
    pattern = r"^(-?\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)$"
    match = re.match(pattern, text.strip())
    if match:
        try:
            lat = float(match.group(1))
            lon = float(match.group(2))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return lat, lon
        except ValueError:
            return None
    return None


def _parse_market_identifier(url_or_text: str) -> Tuple[Optional[str], Optional[str]]:
    text = url_or_text.strip()
    preddy_id_match = re.search(r"preddy\.trade/event/[^/]+/(\d+)", text)
    if preddy_id_match:
        return "id", preddy_id_match.group(1)

    tma_match = re.search(r"startapp=([a-zA-Z0-9_-]+)", text)
    if tma_match:
        val = tma_match.group(1)
        return ("id" if val.isdigit() else "slug"), val

    poly_match = re.search(r"polymarket\.com/event/([a-zA-Z0-9_-]+)", text)
    if poly_match:
        return "slug", poly_match.group(1)

    clean_val = text.split("?")[0].rstrip("/").split("/")[-1]
    if clean_val.isdigit():
        return "id", clean_val
    elif clean_val:
        return "slug", clean_val

    return None, None


async def _fetch_polymarket_orderbook(param_type: str, param_val: str) -> Optional[Dict[str, Any]]:
    url = f"{POLYMARKET_GAMMA_API}?{param_type}={param_val}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6.0)) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                if isinstance(data, list) and len(data) > 0:
                    return data[0]
                elif isinstance(data, dict) and "markets" in data:
                    return data
                return None
    except Exception as error:
        logger.error(f"Сбой при запросе к Polymarket API: {error}")
        return None


async def _collect_weather_data(user_query: str, explicit_date: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[BufferedInputFile], Optional[str]]:
    airport_data = await asyncio.to_thread(resolve_airport, user_query)
    if not airport_data:
        return False, None, None, f"❌ Аэропорт с кодом <code>{user_query}</code> не найден в базе данных."

    lat, lon, tz_name = _extract_airport_coords(airport_data)
    if lat is None or lon is None or not tz_name:
        return False, None, None, f"⚠️ Неполные метаданные для аэропорта <code>{user_query}</code>."

    try:
        local_tz = zoneinfo.ZoneInfo(tz_name)
        target_date_local = explicit_date if explicit_date else datetime.now(local_tz).strftime("%Y-%m-%d")
    except Exception as e:
        logger.error(f"Недопустимый часовой пояс '{tz_name}' для {user_query}: {e}")
        return False, None, None, f"⚠️ Недопустимый часовой пояс <code>{tz_name}</code>."

    async def _get_openmeteo():
        return await asyncio.to_thread(fetch_openmeteo_forecast, lat, lon, tz_name, target_date_local)

    async def _get_noaa():
        return await asyncio.to_thread(get_noaa_package, user_query)

    raw_forecast_payload, noaa_payload = await asyncio.gather(_get_openmeteo(), _get_noaa(), return_exceptions=True)

    if isinstance(raw_forecast_payload, Exception) or not isinstance(raw_forecast_payload, dict):
        raw_forecast_payload = {}
    if isinstance(noaa_payload, Exception) or not isinstance(noaa_payload, dict):
        noaa_payload = {}

    synth_result = (
        await asyncio.to_thread(synthesize_forecast, raw_forecast_payload)
        if raw_forecast_payload
        else {"success": False, "error": "Нет данных"}
    )

    summary_text = build_summary_caption(airport_data, synth_result, noaa_payload, target_date_local)
    package_dict = build_raw_data_package_dict(airport_data, raw_forecast_payload, synth_result, noaa_payload)

    json_bytes = json.dumps(package_dict, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"weather_package_{user_query}_{target_date_local}.json"
    document_file = BufferedInputFile(file=json_bytes, filename=filename)

    return True, summary_text, document_file, None


async def _render_final_scan_report(event_data: Dict[str, Any], user_balance: float, raw_input: str, target_message: Message):
    title = event_data.get("title", "Погодный маркет")
    slug = event_data.get("slug", "")
    description = event_data.get("description", "")
    markets = event_data.get("markets", [])

    if not markets:
        await target_message.answer("⚠️ В этом событии нет активных котировок.", parse_mode="HTML")
        return

    tier_name, bet_size, corridor_info = calculate_tier_sizing(user_balance)
    
    report_lines = [
        f"📊 <b>{title}</b>\n",
        f"💼 <b>Твой депозит:</b> <code>${user_balance:.2f}</code> ({tier_name})",
        f"🎯 <b>Точечный вход:</b> <code>${bet_size:.2f}</code>",
        f"🛡️ <b>Коридор:</b> <code>{corridor_info}</code>\n",
        "<b>Текущие котировки исходов (Стакан):</b>"
    ]

    for item in markets:
        question = item.get("groupItemTitle") or item.get("question", "Исход")
        prices_str = item.get("outcomePrices", '["0", "0"]')

        try:
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            yes_price = float(prices[0]) if len(prices) > 0 else 0.0
        except Exception:
            yes_price = 0.0

        if yes_price > 0.0:
            shares_count = int(bet_size / yes_price)
            potential_payout = shares_count * 1.0
            net_profit = potential_payout - bet_size
            roi = ((1.0 / yes_price) - 1.0) * 100
            price_cents = round(yes_price * 100, 1)

            report_lines.append(
                f"• <b>{question}</b>: <code>{price_cents}¢</code> (${yes_price:.2f})\n"
                f"  └ <i>Вход (${bet_size:.2f}):</i> <b>{shares_count} shares</b> | Профит: <b>+${net_profit:.2f} (+{roi:.0f}%)</b>"
            )
        else:
            report_lines.append(f"• <b>{question}</b>: <code>0¢</code> (Нет ликвидности)")

    orderbook_block = "\n".join(report_lines)

    search_context = f"{title} {slug} {description} {raw_input}"
    detected_icao = _detect_city_icao(search_context)
    detected_date = _detect_market_target_date(search_context)

    if detected_icao:
        date_label = f" на {detected_date}" if detected_date else ""
        status_msg = await target_message.answer(
            f"⚡ <i>Считываю метеомодели и прогноз для {detected_icao}{date_label}...</i>",
            parse_mode="HTML",
        )
        success, summary_text, document_file, _ = await _collect_weather_data(detected_icao, explicit_date=detected_date)

        if success and summary_text:
            unified_report = f"{orderbook_block}\n\n{'━' * 22}\n\n{summary_text}"
            await status_msg.edit_text(unified_report, parse_mode="HTML")
            await target_message.answer_document(
                document=document_file,
                caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{document_file.filename}</code>",
                parse_mode="HTML",
            )
            return

    await target_message.answer(orderbook_block, parse_mode="HTML")
    await target_message.answer(
        "🌍 <b>Город не распознан автоматически.</b> Выбери его из списка ниже:",
        parse_mode="HTML",
        reply_markup=cities_inline_keyboard,
    )


# -------------------------------------------------------------
# БЛОК 1: Главное меню и команды
# -------------------------------------------------------------

@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 <b>Weather & Alpha Bot активен!</b>\n\n"
        "🔹 <b>Анализ погоды:</b> Отправь ICAO-код (например, <code>KJFK</code>) или координаты.\n"
        "🔹 <b>Сканер маркетов:</b> Нажми <b>«🔍 Сканировать маркет»</b>.\n"
        "🔹 <b>Контроль сделок:</b> Нажми <b>«📌 Мои позиции»</b> для отслеживания открытых сделок.\n"
        "🔹 <b>Радар аномалий:</b> Нажми <b>«⚙️ Радар аномалий»</b> для выбора городов."
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(Command("help"), StateFilter("*"))
@router.message(F.text == "📖 Справка / Помощь", StateFilter("*"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    help_text = (
        "📖 <b>Справка по работе с ботом:</b>\n\n"
        "1. <b>«🔍 Сканировать маркет»:</b> Полный разбор стакана и метеопакета.\n"
        "2. <b>«📌 Мои позиции»:</b> Добавь купленные исходы (например, 23, 24), и бот будет предупреждать о фронтах, разворотах ветра и окнах прогрева.\n"
        "3. <b>«⚙️ Радар аномалий»:</b> Управление фоновым слежением за городами."
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_keyboard)


# -------------------------------------------------------------
# БЛОК 2: Меню «📌 Мои позиции» (Управление сделками)
# -------------------------------------------------------------

@router.message(F.text == "📌 Мои позиции", StateFilter("*"))
@router.message(Command("positions"), StateFilter("*"))
async def cmd_my_positions(message: Message, state: FSMContext):
    await state.clear()
    positions = get_user_positions(message.from_user.id)

    if not positions:
        text = (
            "📌 <b>У тебя пока нет активных сделок на контроле.</b>\n\n"
            "Нажми <b>«➕ Добавить сделку»</b>, чтобы радар персонально следил за твоей позицией "
            "(контролировал сломы погоды, морской бриз, окно солнца и максимумы)!"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить сделку", callback_data="add_new_pos")]]
        )
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    text_lines = ["📌 <b>Твои активные сделки под защитой радара:</b>\n"]
    buttons = []

    for pos in positions:
        city_label = ALL_RADAR_CITIES.get(pos["icao"], pos["icao"])
        text_lines.append(
            f"• <b>{city_label}</b> | Исходы: <code>{pos['outcomes']}</code> | Дата: <code>{pos['target_date']}</code>"
        )
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Закрыть: {pos['icao']} ({pos['outcomes']})",
                callback_data=f"del_pos:{pos['id']}"
            )
        ])

    buttons.append([InlineKeyboardButton(text="➕ Добавить еще сделку", callback_data="add_new_pos")])

    await message.answer(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data == "add_new_pos")
async def process_add_pos_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddPositionStates.waiting_for_city)
    await callback.message.edit_text(
        "🌍 <b>Шаг 1 из 2: Выбери город твоей открытой сделки:</b>",
        parse_mode="HTML",
        reply_markup=position_city_keyboard,
    )


@router.callback_query(F.data.startswith("pos_city:"))
async def process_pos_city_selected(callback: CallbackQuery, state: FSMContext):
    icao_code = callback.data.split(":")[1]
    
    # Автоматически определяем целевую дату по часовому поясу аэропорта
    airport_data = resolve_airport(icao_code) or {}
    tz_name = airport_data.get("timezone", "UTC")
    try:
        local_date = datetime.now(zoneinfo.ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    except Exception:
        local_date = datetime.now().strftime("%Y-%m-%d")

    await state.update_data(pos_icao=icao_code, pos_date=local_date)
    await state.set_state(AddPositionStates.waiting_for_outcomes)

    city_name = ALL_RADAR_CITIES.get(icao_code, icao_code)
    await callback.message.edit_text(
        f"🎯 <b>Шаг 2 из 2: Локация {city_name}</b>\n\n"
        "Напиши сообщением купленные исходы (например: <code>23, 24</code> или <code>80-81F</code>):",
        parse_mode="HTML",
    )


@router.message(AddPositionStates.waiting_for_outcomes, F.text)
async def process_pos_outcomes_input(message: Message, state: FSMContext):
    outcomes_text = message.text.strip()
    data = await state.get_data()
    icao = data.get("pos_icao", "EGLC")
    target_date = data.get("pos_date", datetime.now().strftime("%Y-%m-%d"))

    # Добавляем в базу данных SQLite
    add_position(
        user_id=message.from_user.id,
        icao=icao,
        outcomes=outcomes_text,
        target_date=target_date
    )

    # Автоматически добавляем город в активный радар
    active_radar_cities.add(icao)

    await state.clear()
    city_name = ALL_RADAR_CITIES.get(icao, icao)
    
    success_text = (
        f"✅ <b>Позиция успешно взята под радарный контроль!</b>\n\n"
        f"📍 <b>Город:</b> {city_name}\n"
        f"🎯 <b>Купленные исходы:</b> <code>{outcomes_text}</code>\n"
        f"📅 <b>Целевая дата:</b> <code>{target_date}</code>\n\n"
        f"🛡️ <i>Радар каждые 15 минут будет проверять Front Crash, смену ветра и таймер инсоляции. "
        f"При угрозе позиции ты мгновенно получишь уведомление!</i>"
    )
    await message.answer(success_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.callback_query(F.data.startswith("del_pos:"))
async def process_del_pos_callback(callback: CallbackQuery):
    pos_id = int(callback.data.split(":")[1])
    delete_position(pos_id, callback.from_user.id)
    await callback.answer("✅ Сделка закрыта и снята с контроля.")
    
    # Обновляем список
    positions = get_user_positions(callback.from_user.id)
    if not positions:
        await callback.message.edit_text(
            "📌 <b>Все сделки закрыты. Активных позиций на радаре нет.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить сделку", callback_data="add_new_pos")]]
            )
        )
        return

    buttons = []
    text_lines = ["📌 <b>Твои активные сделки под защитой радара:</b>\n"]
    for pos in positions:
        city_label = ALL_RADAR_CITIES.get(pos["icao"], pos["icao"])
        text_lines.append(
            f"• <b>{city_label}</b> | Исходы: <code>{pos['outcomes']}</code> | Дата: <code>{pos['target_date']}</code>"
        )
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Закрыть: {pos['icao']} ({pos['outcomes']})",
                callback_data=f"del_pos:{pos['id']}"
            )
        ])

    buttons.append([InlineKeyboardButton(text="➕ Добавить еще сделку", callback_data="add_new_pos")])

    await callback.message.edit_text(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# -------------------------------------------------------------
# БЛОК 3: Инлайн-колбэки (Сканер, Города, Тумблеры)
# -------------------------------------------------------------

@router.message(F.text == "⚙️ Радар аномалий", StateFilter("*"))
@router.message(Command("radar"), StateFilter("*"))
async def cmd_radar_settings(message: Message, state: FSMContext):
    await state.clear()
    active_count = len(active_radar_cities)
    text = (
        f"⚙️ <b>Панель управления Радаром аномалий</b>\n\n"
        f"📡 Сейчас активно городов: <b>{active_count} из {len(ALL_RADAR_CITIES)}</b>\n\n"
        "Нажимай на кнопки ниже, чтобы включать (🟢) или выключать (🔴) фоновое отслеживание стаканов и METAR:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_radar_keyboard())


@router.message(F.text == "🌍 Избранные города", StateFilter("*"))
@router.message(Command("cities"), StateFilter("*"))
async def cmd_cities_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🌍 <b>Выбери город для моментального метеопакета:</b>",
        parse_mode="HTML",
        reply_markup=cities_inline_keyboard,
    )


@router.message(F.text == "🔍 Сканировать маркет", StateFilter("*"))
async def btn_scan_trigger(message: Message, state: FSMContext):
    await state.set_state(MarketScanStates.waiting_for_link)
    await message.answer(
        "📥 <b>Режим сканирования активирован!</b>\n\n"
        "Отправь ссылку на погодный маркет из <b>Preddy</b> или <b>Polymarket</b> следующим сообщением 👇",
        parse_mode="HTML",
    )


@router.message(Command("scan"), StateFilter("*"))
async def cmd_scan_market(message: Message, state: FSMContext):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await state.set_state(MarketScanStates.waiting_for_link)
        await message.answer(
            "📥 <b>Режим сканирования активирован!</b>\n\n"
            "Отправь ссылку на маркет следующим сообщением 👇",
            parse_mode="HTML",
        )
        return

    param_type, param_val = _parse_market_identifier(args[1])
    if not param_type or not param_val:
        await message.answer("❌ <b>Не удалось распознать ссылку.</b>", parse_mode="HTML")
        return

    event_data = await _fetch_polymarket_orderbook(param_type, param_val)
    if not event_data:
        await message.answer("❌ <b>Маркет не найден.</b> Проверь ссылку.", parse_mode="HTML")
        return

    await state.update_data(event_data=event_data, raw_link=args[1])
    await state.set_state(MarketScanStates.waiting_for_balance)
    await message.answer(
        f"📊 <b>Маркет найден:</b> {event_data.get('title', 'Событие')}\n\n"
        "💰 <b>Введи твой текущий баланс на Preddy ($)</b> сообщением или нажми кнопку ниже:",
        parse_mode="HTML",
        reply_markup=balance_quick_keyboard,
    )


@router.callback_query(F.data.startswith("set_bal:"))
async def process_quick_balance(callback: CallbackQuery, state: FSMContext):
    balance_val = float(callback.data.split(":")[1])
    data = await state.get_data()
    event_data = data.get("event_data")
    raw_link = data.get("raw_link", "")

    await state.clear()
    await callback.message.delete()

    if event_data:
        await _render_final_scan_report(event_data, balance_val, raw_link, callback.message)
    else:
        await callback.message.answer("⚠️ Сессия истекла. Нажми <b>«🔍 Сканировать маркет»</b> заново.", parse_mode="HTML")


@router.callback_query(F.data.startswith("icao:"))
async def process_city_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    icao_code = callback.data.split(":")[1]
    await callback.answer(f"Сбор данных для {icao_code}...")
    await _execute_weather_pipeline(icao_code, callback.message)


@router.callback_query(F.data.startswith("toggle_radar:"))
async def process_toggle_radar(callback: CallbackQuery):
    icao_code = callback.data.split(":")[1]
    
    if icao_code in active_radar_cities:
        active_radar_cities.remove(icao_code)
        await callback.answer(f"🔴 {icao_code} отключен от радара")
    else:
        active_radar_cities.add(icao_code)
        await callback.answer(f"🟢 {icao_code} включен в радар")
        
    active_count = len(active_radar_cities)
    text = (
        f"⚙️ <b>Панель управления Радаром аномалий</b>\n\n"
        f"📡 Сейчас активно городов: <b>{active_count} из {len(ALL_RADAR_CITIES)}</b>\n\n"
        "Нажимай на кнопки ниже, чтобы включать (🟢) или выключать (🔴) фоновое отслеживание стаканов и METAR:"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_radar_keyboard())


# -------------------------------------------------------------
# БЛОК 4: Обработка ссылок и текста (FSM + ICAO)
# -------------------------------------------------------------

@router.message(MarketScanStates.waiting_for_link, F.text)
async def process_market_link_input(message: Message, state: FSMContext):
    user_text = message.text.strip()
    param_type, param_val = _parse_market_identifier(user_text)

    if not param_type or not param_val:
        await message.answer(
            "❌ <b>Не удалось распознать ссылку.</b>\n"
            "Убедись, что отправляешь корректную ссылку с Preddy или Polymarket.",
            parse_mode="HTML",
        )
        return

    status_msg = await message.answer("⚡ <i>Считываю котировки маркета...</i>", parse_mode="HTML")
    event_data = await _fetch_polymarket_orderbook(param_type, param_val)

    if not event_data:
        await status_msg.edit_text("❌ <b>Маркет не найден.</b> Проверь ссылку.", parse_mode="HTML")
        return

    await state.update_data(event_data=event_data, raw_link=user_text)
    await state.set_state(MarketScanStates.waiting_for_balance)

    await status_msg.delete()
    await message.answer(
        f"📊 <b>Маркет найден:</b> {event_data.get('title', 'Событие')}\n\n"
        "💰 <b>Введи твой текущий баланс на Preddy ($)</b> сообщением (например, <code>25</code> или <code>150</code>) или выбери кнопку:",
        parse_mode="HTML",
        reply_markup=balance_quick_keyboard,
    )


@router.message(MarketScanStates.waiting_for_balance, F.text)
async def process_manual_balance_input(message: Message, state: FSMContext):
    user_text = message.text.strip().replace("$", "").replace(",", ".")
    
    try:
        user_balance = float(user_text)
        if user_balance <= 0:
            user_balance = 25.0
    except ValueError:
        user_balance = 25.0

    data = await state.get_data()
    event_data = data.get("event_data")
    raw_link = data.get("raw_link", "")

    await state.clear()

    if event_data:
        await _render_final_scan_report(event_data, user_balance, raw_link, message)
    else:
        await message.answer("⚠️ Сессия истекла. Нажми <b>«🔍 Сканировать маркет»</b> заново.", parse_mode="HTML")


async def _execute_coordinates_pipeline(lat: float, lon: float, target_message: Message):
    status_msg = await target_message.answer(
        f"🧭 <i>Определяю часовой пояс и собираю метеомодели для [{lat:.4f}, {lon:.4f}]...</i>",
        parse_mode="HTML",
    )

    try:
        tz_name = await asyncio.to_thread(tf.timezone_at, lng=lon, lat=lat) or "UTC"
        local_tz = zoneinfo.ZoneInfo(tz_name)
        target_date_local = datetime.now(local_tz).strftime("%Y-%m-%d")

        custom_location_data = {
            "icao": "GEO",
            "name": f"Координаты [{lat:.4f}, {lon:.4f}]",
            "city": f"Точка {lat:.3f}, {lon:.3f}",
            "country": "GEO",
            "latitude": lat,
            "longitude": lon,
            "timezone": tz_name,
        }

        raw_forecast_payload = await asyncio.to_thread(
            fetch_openmeteo_forecast, lat, lon, tz_name, target_date_local
        )

        if not isinstance(raw_forecast_payload, dict):
            raw_forecast_payload = {}

        synth_result = (
            await asyncio.to_thread(synthesize_forecast, raw_forecast_payload)
            if raw_forecast_payload
            else {"success": False, "error": "Нет данных"}
        )

        noaa_payload = {
            "metar": {
                "available": True,
                "raw": f"GEO {lat:.4f}/{lon:.4f} (Анализ по сетке численных моделей)",
                "temp_c": None,
                "dewpoint_c": None,
                "wind_speed_kts": None,
            },
            "taf": {"available": False, "raw": "TAF доступен только для официальных станций ICAO"},
        }

        summary_text = build_summary_caption(
            custom_location_data, synth_result, noaa_payload, target_date_local
        )

        package_dict = build_raw_data_package_dict(
            custom_location_data, raw_forecast_payload, synth_result, noaa_payload
        )

        json_bytes = json.dumps(package_dict, ensure_ascii=False, indent=2).encode("utf-8")
        clean_filename = f"weather_package_coords_{abs(lat):.2f}_{abs(lon):.2f}_{target_date_local}.json"
        document_file = BufferedInputFile(file=json_bytes, filename=clean_filename)

        await status_msg.delete()
        await target_message.answer(summary_text, parse_mode="HTML")
        await target_message.answer_document(
            document=document_file,
            caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{clean_filename}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Ошибка при обработке координат: {e}", exc_info=True)
        await status_msg.edit_text("❌ <b>Произошла ошибка при обработке координат.</b>", parse_mode="HTML")


async def _execute_weather_pipeline(user_query: str, target_message: Message):
    status_msg = await target_message.answer(
        f"🔍 <i>Сбор данных и генерация RAW Data Package для {user_query}...</i>",
        parse_mode="HTML",
    )

    try:
        success, summary_text, document_file, err_msg = await _collect_weather_data(user_query)
        if not success:
            await status_msg.edit_text(err_msg, parse_mode="HTML")
            return

        await status_msg.delete()
        await target_message.answer(summary_text, parse_mode="HTML")
        await target_message.answer_document(
            document=document_file,
            caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{document_file.filename}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Ошибка при обработке запроса: {e}", exc_info=True)
        await status_msg.edit_text("❌ <b>Произошла ошибка при обработке запроса.</b>", parse_mode="HTML")


@router.message(F.text)
async def process_weather_request(message: Message):
    user_text = message.text.strip()

    coords = _parse_coordinates(user_text)
    if coords:
        lat, lon = coords
        await _execute_coordinates_pipeline(lat, lon, message)
        return

    user_query = user_text.upper()
    if len(user_query) == 4 and user_query.isalpha():
        await _execute_weather_pipeline(user_query, message)
        return

    await message.answer(
        "⚠️ <b>Формат не распознан.</b>\n\n"
        "• Отправь <b>4-значный ICAO-код</b> (например: <code>KJFK</code> или <code>UHHH</code>)\n"
        "• Отправь <b>координаты</b> (например: <code>48.52, 135.18</code>)\n"
        "• Нажми <b>«📌 Мои позиции»</b> для контроля сделок\n"
        "• Или нажми <b>«🔍 Сканировать маркет»</b> для анализа ссылки.",
        parse_mode="HTML",
    )