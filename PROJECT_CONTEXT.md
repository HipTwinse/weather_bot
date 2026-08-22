# PROJECT CONTEXT — Weather Data Package Bot

## 1. Цель проекта
Telegram-бот принимает ICAO код аэропорта → автоматически собирает погодные данные → возвращает единый WEATHER DATA PACKAGE → данные передаются в отдельный Gemini Weather Analyzer.

## 2. Что НЕ входит в текущие Phase 1–3
- Polymarket API
- Order book
- Цены Polymarket
- Автоматические сделки
- Торговые решения
- EV engine

## 3. Основные источники данных
- **NOAA Aviation Weather:** METAR / TAF
- **Open-Meteo Multi-Model API:**
  - ECMWF IFS 0.25° (Primary Model)
  - GFS Global 13 km (Primary Model)
  - ICON Global 11 km (Primary Model)
  - GEM Global 15 km (Secondary Model, hourly-interpolated)
*Дополнительные источники будут добавляться только после проверки их необходимости.*

## 4. Главный принцип
Парсер собирает и структурирует фактические данные. Он НЕ является торговым аналитиком.

## 5. Принятые правила и архитектурные требования
- **Airport Resolution:** ICAO должен резолвиться в конкретный аэропорт.
- **Coordinates:** Координаты должны быть точными.
- **Timezone:** Используется IANA timezone (`Asia/Vladivostok`, `America/New_York`).
- **Local Date:** Локальная календарная дата аэропорта критична.
- **Primary Models:** ECMWF / GFS / ICON.
- **Secondary Model:** GEM.
- **Model Metadata:** Model Run / Reference Time нужно сохранять.
- **Hourly Dynamics:** Hourly profile должен показывать реальную динамику и определять Peak Hour.
- **Data Isolation:** RAW DATA / MODEL COMPARISON / DERIVED METRICS четко разделены.

## 6. Формат WEATHER DATA PACKAGE
Единый структурированный JSON/Text пакет, содержащий:
1. **Metadata:** ICAO, Coordinates, Timezone, Local Date, Elevation, City.
2. **Raw Data:** METAR / TAF (от NOAA Aviation Weather).
3. **Model Comparison:** ECMWF, GFS, ICON, GEM.
4. **Derived Metrics & Consensus:** Peak Hour, Temp Trends, Total Precip, Rain Probability %, Confidence Score.

## 7. Текущая структура проекта
weather_bot/
├── PROJECT_CONTEXT.md
├── requirements.txt
├── .env
├── airport_resolver.py
├── noaa_service.py
├── openmeteo_service.py
├── weather_synthesizer.py
├── test_openmeteo.py
└── test_synthesis.py

## 8. Этапы разработки (Status Board)
- **PHASE 0 — Environment Setup**: COMPLETED ✅
- **PHASE 1 — Airport Resolver**: COMPLETED ✅
- **PHASE 2 — Weather APIs & METAR/TAF Integration**: COMPLETED ✅
- **PHASE 3 — Data Aggregation & Package Generator**: COMPLETED ✅
- **PHASE 4 — Telegram Bot Interface & Package Formatter**: IN PROGRESS ⏳
- **PHASE 5 — Security, Rate Limiting & Deployment**: NOT STARTED 🛑

## 9. Журнал изменений (Log)
- Created PROJECT_CONTEXT.md
- Python 3.14 & pip verified
- VS Code environment set up
- Virtual environment (venv) created and activated
- Created `airport_resolver.py` module using `airportsdata` library
- Expanded `airport_resolver.py` metadata (IATA, City, Elevation in meters)
- Tested KJFK, UHHH, XXXX (PASSED)
- Updated `requirements.txt` with full dependencies
- Created `noaa_service.py` module for NOAA Aviation Weather API (METAR / TAF)
- Fixed observation_time_utc precision (obsTime priority) and HTTP 204 error handling
- Diagnosed UHHH METAR timeout and implemented retry-mechanism (2 retries, 1s delay for timeouts and 5xx errors)
- Re-tested NOAA integration with KJFK, UHHH, XXXX (ALL PASSED)
- Created `openmeteo_service.py` fetching 4 global forecast models (ECMWF, GFS, ICON, GEM) with full IANA timezone support, hourly precision, and DevSecOps retries with custom User-Agent headers.
- Created `weather_synthesizer.py` for model consensus calculation, temperature spread analysis, rain probability calculation, and Confidence Level determination (HIGH / MEDIUM / LOW).