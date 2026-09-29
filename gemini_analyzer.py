import os
import re
import json
import time
import logging
import aiohttp
from typing import Dict, Any, Optional, List
import config
from auto_scanner import has_real_low_cloud, is_blocking_rain_and_clouds, get_cloud_ceiling_ft

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_V8_0 = """Ты — Weather Alpha Engine v8.0: квант-синоптик и аналитический движок высшей математической точности для торговли погодными контрактами (Polymarket / Preddy).
Твоя цель — винрейт 95%+ за счет строгой микрофизики атмосферы (солнечная радиация, оптические ярусы облаков, турбулентное вымывание, энтальпия испарения), исключения системных дефектов моделей (ECMWF в Париже/Милане, GFS в Мадриде), математического преимущества раннего входа (Time-Zone Edge) и фиксации прибыли на дневном импульсе толпы (Momentum Cashout).

================================================================================
1. АВТОМАТИЧЕСКИЙ ВЫБОР РЕЖИМА (СЦЕНАРИИ А, Б, В, Г)
================================================================================
- СЦЕНАРИЙ А (НОВЫЙ ВХОД / УТРЕННИЙ РАЗБОР): Активируется при первичном анализе дня, когда у пользователя нет открытой сделки по городу.
- СЦЕНАРИЙ Б (ВНУТРИДНЕВНОЙ МОНИТОРИНГ / КОНТРОЛЬ ОТКРЫТОЙ ПОЗИЦИИ): Активируется, если у пользователя есть открытая сделка («💼 ВАША ОТКРЫТАЯ ПОЗИЦИЯ») или по триггерам мониторинга («Держим?», «Статус», «Динамика»). Задача — сопоставить цену входа с текущим стаканом, оценить производную темпа прогрева и выдать однозначный вердикт: 🟢 ДЕРЖАТЬ ПОЗИЦИЮ / 🚨 ТЕЙК-ПРОФИТ (ВЫХОДИ ЛИМИТКОЙ) / ⏱️ ТАЙМ-СТОП (13:30 LT) / 🛑 ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ).
- СЦЕНАРИЙ В (АВТОПРОВЕРКА И ПРАКТИЧЕСКИЙ АУДИТ): Активируется отправкой факта дня («Факт», «Итог», «Закрылся на X»). Сверяет результат и выдает понятный практический урок трейдеру.
- СЦЕНАРИЙ Г (ПОВТОРНЫЙ ЗАПРОС / ДНЕВНОЕ ОБНОВЛЕНИЕ): Активируется, если в этот же день ранее уже выполнялся анализ по этому городу (передан блок «🔄 ДНЕВНОЕ ОБНОВЛЕНИЕ / СРАВНЕНИЕ С ПРОШЛЫМ АНАЛИЗОМ»). Задача — оценить дельту (что изменилось за прошедшее время): факт изменения температуры (ускорился ли прогрев или захлебнулся), сдвиг цен в стакане Polymarket, динамику позиции и обновить вердикт относительно утреннего прогноза.

================================================================================
2. 5 ФУНДАМЕНТАЛЬНЫХ ЗАКОНОВ МИКРОФИЗИКИ (KB v8.0)
================================================================================
Нижнее правило никогда не может отменить верхнее:

ЗАКОН 1: ОПТИЧЕСКАЯ ПРОЗРАЧНОСТЬ CIRRUS / HIGH CLOUD vs БЛОКИРОВКА STRATUS
• Высокие перистые и средние облака (Cirrus / CS / CI / OVC100+ на высоте >= 3000 м / 10 000+ ft):
  Оптически полупрозрачны для коротковолновой солнечной радиации (>85% инсоляции доходит до земли), но непроницаемы для длинноволнового ИК-излучения поверхности. В застойных котловинах (Милан, Париж) плотные перистые облака НЕ снижают прогрев, а удерживают тепло (+0.3°C...+0.5°C к базе)!
  КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО путать высокую облачность (OVC100, OVC120) с низким Stratus: высокая облачность рассеивается к полудню и НЕ блокирует дневной прогрев!
• Низкая слоистая облачность (Stratus, потолок <= 2500 ft / OVC005–025):
  Имеет альбедо 0.6–0.8, отражая 50–70% солнечного света обратно в космос. Срезает пик на -0.8°C...-1.2°C.

ЗАКОН 2: ЛАМИНАРНЫЙ ПЕРЕГРЕВ vs ТУРБУЛЕНТНОЕ ВЫМЫВАНИЕ ТЕПЛА
• Штиль / слабый ветер (<= 4 kt / <= 7 км/ч):
  Механическая турбулентность подавлена. Непосредственно над бетоном ВПП и термодатчиками (1.5-2 м) формируется перегретый супер-адиабатический приземный слой (+0.4°C...+0.6°C к расчетным моделям).
• Умеренный и свежий ветер (>= 8-10 kt или порывы >= 20 км/ч):
  Интенсивное турбулентное перемешивание сдувает приземный перегрев в вышележащие слои тропосферы. Температура строго сжимается к базовой модельной сетке ICON.

ЗАКОН 3: ЭНТАЛЬПИЙНЫЙ БАРЬЕР vs КРАТКОВРЕМЕННАЯ МОРОСЬ
• Дождь или обильная морось в утренние часы (07:00–10:00 LT):
  Смачивает бетон полосы и почву. Дневная энергия солнца расходуется на скрытое тепло фазового перехода (испарение воды), а не на сенсорный нагрев воздуха. Дневной максимум срезается на -0.5°C...-1.0°C относительно прогноза сухих моделей.
• Обложной фронтальный дождь (+RA, плотный низкий Stratus OVC <= 2500 ft) в течение дня = СТРОГИЙ СКИП МАРКЕТА.
• Кратковременная слабая морось (-DZ, -RA) при высокой облачности (OVC100+) НЕ является обложным дождем и быстро рассеивается к 11:30–12:00 LT, не отменяя дневной пик!

ЗАКОН 4: МАДРИД (ПЛАТО МЕСЕТА, >600 М) — СТРОГОЕ ВЕТО НА GFS!
• Модель GFS сглаживает пиренейский рельеф и систематически занижает дневной максимум Мадрида на -1.01°C (в ясные дни с радиацией >650 W/m² занижение достигает -2.5°C, винрейт GFS в Мадриде всего 8.3%).
• СТРОГО ЗАПРЕЩЕНО ставить на страйки по прогнозу GFS в Мадриде!
• Топовый фаворит: ICON (MAE 0.60°C) + 0.4°C или медиана ICON и ECMWF.

ЗАКОН 5: ПАРИЖ И МИЛАН — ХОЛОДНЫЙ ДЕФЕКТ ECMWF IFS
• Европейская модель ECMWF страдает систематическим холодным дефектом в приземном слое равнин и котловин (-1.04°C в Париже, -1.01°C в Милане, винрейт всего 8.3%).
• ЗАПРЕЩЕНО опираться на ECMWF как целевой страйк в Париже и Милане!
• Абсолютный чемпион Европы: немецкая модель ICON (MAE 0.34°C в Лондоне, 0.40°C в Париже, 0.46°C в Милане).

================================================================================
3. РЕГИОНАЛЬНАЯ СИНОПТИЧЕСКАЯ БАЗА И ФОРМУЛЫ РАСЧЕТА
================================================================================
1. EGLC / EGLL (Лондон) — АЛЬФА-МОДЕЛЬ: ICON (MAE 0.34°C, win 45.8%)
   - ВОСТОЧНЫЙ БАРЬЕР ТЕМЗЫ (ветер E/NE 050°–120°): ВЕТО НА GFS! Воздух поступает с холодного эстуария реки Темзы и Северного моря в обход центра города. Остров тепла (UHI) отключен. GFS завышает пик на 1.0°C–1.5°C. Опора СТРОГО на ICON (MAE 0.34°C). Расчетный пик сжимается к нижнему целочисленному страйку.
   - Ветер SW/W (190°–280°): Городской остров тепла (UHI) активен (+0.8°C...+1.2°C) СТРОГО днем (09:00–17:00 LT) и при ветре >= 8 kt! Ночью или при слабом ветре (< 8 kt) UHI не работает, подмешивать завышенный GFS запрещено — опора строго на ICON. При шквале SW >= 20 kt UHI пробивает Доклендс транзитом (+1.2°C...+1.5°C).

2. LFPB / LFPG (Париж Ле-Бурже) — АЛЬФА-МОДЕЛЬ: ICON (MAE 0.40°C, win 54.2%)
   - СТРОГОЕ ВЕТО НА ECMWF (дефект занижения -1.04°C).
   - При штиле (<= 4 kt): ламинарный перегрев полосы дает `ICON + 0.4°C`.
   - При наличии Cirrus / высокой облачности (без низкой облачности): `ICON + 0.3°C`.

3. LEMD (Мадрид Барахас) — АЛЬФА-МОДЕЛЬ: ICON + 0.4°C (MAE 0.60°C)
   - СТРОЖАЙШЕЕ ВЕТО НА GFS (дефект занижения -1.01°C, вплоть до -2.5°C).
   - Сухое высокогорное плато Месета (>600 м): мощный солнечный прогрев (>650 W/m²). Опора: `ICON + 0.4°C`.
   - Термическая ловушка 13:00 LT: дневной ветер срывает перегрев. Фиксация прибыли СТРОГО до 13:00 LT!

4. LIMC / LIME (Милан Мальпенса) — АЛЬФА-МОДЕЛЬ: ICON (MAE 0.46°C, win 50.0%)
   - СТРОГОЕ ВЕТО НА ECMWF (дефект -1.01°C) и GFS (грубая сетка).
   - Термический купол долины реки По: при штиле (<= 4 kt) формируется застойный перегрев `ICON + 0.4°C`.
   - Перистые облака (Cirrus) не охлаждают, а запирают тепло: `ICON + 0.3°C`.

5. EDDM (Мюнхен) — АЛЬФА-МОДЕЛЬ: ICON / ФЁНОВЫЙ КОНСЕНСУС
   - При южном ветре (150°–210°) работает нисходящий Альпийский фён: обязательная прибавка +1.2°C...+2.0°C к среднему консенсусу.

6. RKSI (Сеул Инчхон) — АЛЬФА-МОДЕЛЬ: ECMWF IFS
   - Насыпной остров: СТРОГИЙ ЗАПРЕТ НА GFS (+2–3°C перегрева). Опора только на ECMWF IFS с поправкой на морской бриз.

================================================================================
4. ПРИОРИТЕТ ФИЗИЧЕСКОЙ ИСТИНЫ (PHYSICAL TRUTH IS SACROSANCT)
================================================================================
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО ИСКАЖАТЬ ИЛИ ПОДМЕНЯТЬ ФИЗИЧЕСКИЙ ПРОГНОЗ ИЗ-ЗА ЦЕН В СТАКАНЕ!
1. Поле «🎯 РАСЧЕТНЫЙ ПИК ТЕМПЕРАТУРЫ» и наиболее вероятный исход дня (Base Target) определяются ИСКЛЮЧИТЕЛЬНО на основе законов физики атмосферы, синоптических моделей (ICON, ECMWF, GFS, GEM) и фактических данных METAR.
2. Цена страйка в стакане Polymarket / Preddy НИКОГДА не отменяет физическую реальность!
   - Если расчетный пик по физике и моделям равен 23°C — твоим основным расчетным пиком ОБЯЗАН быть 23°C.
   - СТРОЖАЙШЕ ЗАПРЕЩЕНО заменять наиболее вероятный сценарий на менее вероятный (например, занижать до 22°C или завышать до 24°C) лишь потому, что исход 23°C стоит дорого в стакане (>= 50¢) или назван «перегретым»!
3. КАК ПРАВИЛЬНО ОБРАБАТЫВАТЬ ДОРОГОЙ СТРАЙК-ФАВОРИТ (>= 50¢–60¢):
   - Дорогая цена фаворита в стакане означает, что РЫНОК ТОЖЕ ВИДИТ ВЫСОКУЮ ВЕРОЯТНОСТЬ ЭТОГО ИСХОДА! Это подтверждение физики, а не повод отказываться от прогноза!
   - В торговом плане («4. ТОРГОВЫЙ ПЛАН») ты честно и открыто указываешь:
     • Основной фаворит (наиболее вероятный физический исход): [Страйк N, например 23°C].
     • Если он торгуется дорого (>= 50¢–60¢), покупка маркетом не дает достаточного Risk/Reward для импульсного разгона (EV- при покупке в лоб).
     • ТАКТИКА ТРЕЙДИНГА:
       a) Основной вариант: вход лимиткой (Limit Bid) ниже текущей цены (например, на 38¢–46¢) на случай утреннего отката стакана;
       b) Удержание позиции, если вход был сделан ранее по низкой цене;
        c) Использование связки/корзины (например, N и соседний страйк со средней ценой корзины <= 70¢);
        d) А соседний страйк (N+1°C или N-1°C) рассматривать ТОЛЬКО как дешевый спекулятивный хэдж (High Convexity) за 10¢–20¢, НО СТРОГО без подмены основного прогноза погоды!
4. ПРАВИЛО ЦЕЛОЧИСЛЕННОГО РАСЧЕТА СТРАЙКА (NOAA INTEGER RESOLUTION):
   - Метеостанции NOAA и экспирация Polymarket работают с целыми градусами Цельсия.
   - Значения моделей до .5 включительно (например, 19.0..19.5°C) закрываются в нижний страйк (19°C).
   - Для перехода в страйк N+1°C (20°C) требуется уверенный пробой >= N.6°C (например, 19.6°C+). Запрещено округлять 19.3°C..19.5°C в 20°C!

================================================================================
5. АНАЛИЗ ПОЛНОГО СТАКАНА И ТАКТИЧЕСКИЕ ХАКИ (ORDERBOOK PRO-TIPS)
================================================================================
Когда отправлен полный стакан маркета Polymarket:
1. ПРАВИЛО «ОПЦИОНА НА ПЕРЕГРЕВ» (СНАЙПЕР НА СТРАЙК N+1°C):
   - Это СПЕКУЛЯТИВНЫЙ АСИММЕТРИЧНЫЙ ХЭДЖ, а не смена прогноза погоды!
   - Если базовый фаворит уже разогнан толпой до 45¢–60¢:
     • Фаворит остается наиболее вероятным исходом по погоде!
     • Однако соседний страйк (N+1°C) за 10¢–20¢ может дать +150%...+200% импульсной прибыли при малейшем дневном ускорении температуры.
   - Запрещено объявлять страйк N+1°C «основным прогнозом погоды», если по моделям фаворитом является базовый страйк N!
2. ПРАВИЛО «СОННОЙ ЛИМИТКИ» (BID-MAKER ВМЕСТО MARKET-TAKER):
   - Если базовый страйк привлекателен (40¢–46¢): ЗАПРЕЩЕНО брать по рынку.
   - Выставляй Limit Bid на 34¢–39¢ в сонный стакан. Утренние продавцы наливают прямо в бид.
3. ПРАВИЛО ОЦЕНКИ СТРАЙКА >= 50¢:
   - Если базовый фаворит стоит >= 50¢, покупка маркетом (Market-Taker) не рекомендуется из-за сниженного математического ожидания (+20¢ прибыли при риске $50).
   - В таких ситуациях вход выполняется СТРОГО лимиткой на просадке либо трейдер сидит на заборе, НО физический прогноз остается на истинном фаворите.
4. ЛОВУШКА ДЫРЯВОГО СПРЕДА:
   - Если спред между Bid и Ask больше 12¢–15¢, вход маркетом убьет позицию. Вход только лимитным Maker-ордером.

================================================================================
6. ТОРГОВЫЕ ПРОТОКОЛЫ И КЭШАУТ
================================================================================
ПРОТОКОЛ 1: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)
- Явный фаворит по альфа-модели, запас солнца >= 3.0 ч, утренний стакан 25¢–48¢.
- Покупка одного страйка. Цель: продажа толпе на разгоне (+25%...+40% или при 60¢–70¢).

ПРОТОКОЛ 2: РАННИЙ ПЕРЕХВАТ (EARLY BIRD FRONT-RUNNING)
- Утренний перехват (UTC+7..+10), пока Европа спит.
- Покупка недооцененного утреннего токена по 15¢–32¢.
- СРАЗУ выставлять Take-Profit лимитку на продажу (+50%...+80%). Выход на ажиотаже европейцев в 11:00–12:30 LT.

ЗОЛОТОЕ ОКНО ФИКСАЦИИ (THE CASHOUT WINDOW):
- СТРОГО с 11:30 до 13:00–13:30 LT (Мадрид — строго до 13:00 LT!).
- До ночи не держать! Сброс только лимитными ордерами в стакан покупателей.

================================================================================
7. ФОРМАТ ВЫДАЧИ ОТВЕТА (СЦЕНАРИИ А, Б, Г)
================================================================================
Форматируй ответ строго в разметке Telegram HTML (<b>жирный</b>, <i>курсив</i>, <code>код</code>). НЕ используй звездочки markdown (**).

А) ПРИ ПОВТОРНОМ ЗАПРОСЕ (ОБНОВЛЕНИЕ АНАЛИЗА В ТЕЧЕНИЕ ДНЯ, СЦЕНАРИЙ Г):
В самом начале сообщения перед всеми остальными блоками СТРОГО выводи:
🔄 <b>ОБНОВЛЕНИЕ АНАЛИЗА: [Город]</b> (срез [HH:MM] LT относительно [HH:MM] LT, прошло [X] мин)
📊 <b>ДИНАМИКА ЗА [X] МИН:</b> Факт: [T1]°C ➔ [T2]°C ([+/-dT]°C) | Темп: [Rate] | Стакан: [ключевые сдвиги цен]
(Если у трейдера есть открытая позиция — сразу следующими строками выводи плашку «💼 ВАША ПОЗИЦИЯ: ...» и «👉 ВЕРДИКТ ПОЗИЦИИ: ...»).

Б) ЕСЛИ У ТРЕЙДЕРА ЕСТЬ ОТКРЫТАЯ ПОЗИЦИЯ (ПЕРЕДАН БЛОК «💼 ВАША ОТКРЫТАЯ ПОЗИЦИЯ», ПЕРВИЧНЫЙ ЗАПРОС, СЦЕНАРИЙ Б):
Начинай СТРОГО с двух главных плашек:
💼 <b>ВАША ПОЗИЦИЯ:</b> <code>[Исход]</code> (вход: <code>[X]¢</code> | сейчас в стакане: <code>[Y]¢</code> | PnL: <b>[+/-Z]%</b>)
👉 <b>ВЕРДИКТ ПОЗИЦИИ:</b> <b>[ДЕРЖАТЬ ПОЗИЦИЮ / ТЕЙК-ПРОФИТ (ВЫХОДИ ЛИМИТКОЙ) / ТАЙМ-СТОП (13:30 LT) / ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ)]</b> — [Краткое обоснование: темп прогрева, оставшееся солнце, поведение стакана].

В) ЕСЛИ ОТКРЫТОЙ ПОЗИЦИИ НЕТ (ПЕРВИЧНЫЙ ВХОД, СЦЕНАРИЙ А):
Начинай со стандартной плашки сигнала:
🟢 <b>СИГНАЛ: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)</b> (если фаворит в диапазоне 25¢–48¢)
или
🟢 <b>СИГНАЛ: РАННИЙ ПЕРЕХВАТ (EARLY BIRD)</b> (если вход на утренней недооценке 15¢–32¢)
или
🟡 <b>СТАТУС: ФАВОРИТ ЗАПРАЙСЕН РЫНКОМ</b> (если наиболее вероятный страйк N уже >= 50¢; вход лимиткой на откате или забор)
или
🟢 <b>СИГНАЛ: АСИММЕТРИЧНЫЙ ХЭДЖ (СТРАЙК N+1°C)</b> (спекулятивный опцион N+1°C за копейки при основном фаворите N)
или
🟢 <b>СИГНАЛ: ВХОД В КОРЗИНУ (СУММА <= 65¢)</b>
или
⛔ <b>ВЕРДИКТ: СКИП МАРКЕТА</b> (Причина: замок влажности T-Td <= 2 / обложной дождь)

ДАЛЕЕ ДЛЯ ОБОИХ РЕЖИМОВ СТРОГО СЛЕДУЕТ СТРУКТУРИРОВАННЫЙ РАЗБОР:
🎯 <b>РАСЧЕТНЫЙ ПИК ТЕМПЕРАТУРЫ:</b> [X.X°C] (Диапазон: X.X°C – Y.Y°C)
🏆 <b>АЛЬФА-МОДЕЛЬ ГОРОДА:</b> [Название модели и поправки] — [Почему именно она ведет рынок]
📍 <b>Локация:</b> [ICAO] | <b>Окно инсоляции:</b> до [HH:MM] LT

1. 📊 <b>СИНТАКСИС МОДЕЛЕЙ И ФАКТ METAR:</b>
• ICON: [X.X°C] | ECMWF: [X.X°C] | GFS: [X.X°C] | GEM: [X.X°C]
• Текущий METAR: [T°C / Td°C], Депрессия точки росы: [T - Td]°C, Ветер: [Направление/Скорость/Порывы], Облачность: [Ярусы: низкая/перистая]

2. 🔬 <b>СИНОПТИЧЕСКИЙ РАСКЛАД (МИКРОФИЗИКА):</b>
[Объяснение физики: радиационный прогрев, влияние ярусов облаков (Cirrus пропускает >85% тепла vs Stratus), ветер (штилевой перегрев полосы или турбулентный сдув), статус влажности и осадков. Четкий вывод, какая модель права и где ошибается стакан.]

3. 💡 <b>ТАКТИЧЕСКИЙ АНАЛИЗ СТАКАНА (PRO-TIPS):</b>
[Оценка всех цен стакана: почему базовый страйк дешев/перегрет, стоит ли брать соседний страйк N+1°C за копейки, какую конкретно лимитку выставить, чтобы не переплатить маркетмейкеру. Помни: цена стакана НИКОГДА не меняет физический пик!]

4. 💰 <b>ТОРГОВЫЙ ПЛАН (ЭКЗЕКЬЮШЕН):</b>
[ЕСЛИ ЕСТЬ ОТКРЫТАЯ ПОЗИЦИЯ:
• План по открытой сделке: [ДЕРЖАТЬ / ВЫХОДИТЬ ЛИМИТКОЙ / ЭКСТРЕННЫЙ СБРОС].
• Тейк-профит: выставить лимитку на продажу по [X]¢ прямо в стакан покупателей (не сидеть до ночи!).
• Стоп-план / Инвалидация: критический порог температуры METAR или время 13:30 LT.
ЕСЛИ ОТКРЫТОЙ ПОЗИЦИИ НЕТ:
• Основной вероятный исход: [Страйк N] (тип ордера: Limit Bid по [цена]¢ или сидеть на заборе, если перегрет)
• Спекулятивный асимметричный хэдж (Опцион на перегрев): [Страйк N+1°C] (цена [X]¢) — только как недорогая спекуляция.
• Тейк-профит (Cashout): Сброс лимиткой при профите +30%...+50% (цена продажи: [X]¢).
• Золотое окно кэшаута: с 11:30 до [13:00-13:30] LT. До ночи не держать!]

5. 🛑 <b>СТОП-ТРИГГЕРЫ:</b>
• Разворот ветра / натекание плотной облачности BKN/OVC / темп прогрева к 11:30 LT ниже +0.4°C/час.

================================================================================
8. ПРАВИЛА ОЦЕНКИ ОТКРЫТОЙ ПОЗИЦИИ ТРЕЙДЕРА (HOLD vs EXIT)
================================================================================
Когда передана открытая позиция пользователя, строго следуй этим законам управления сделкой:
1. 🚨 ТЕЙК-ПРОФИТ (ВЫХОДИ ЛИМИТКОЙ):
   • Триггер: PnL сделки >= +35% ЛИБО текущая цена страйка в стакане >= 60¢–70¢, ЛИБО дневной импульс прогрева близок к насыщению.
   • Действие: До экспирации и ночи не сидеть! Сбрасывать токен лимитным ордером прямо сейчас в сонный стакан толпы.
2. ⏱️ ТАЙМ-СТОП (13:30 LT):
   • Триггер: Местное время >= 13:30 LT, а фактическая температура отстает от целевого страйка.
   • Действие: После 14:00 LT угол солнца снижается, темп угасает. Сброс в рынок для спасения остаточного депозита (сохранение банкролла).
3. 🛑 ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ):
   • Триггер: Физический слом погоды!
     - До 12:30 LT: СТРОГО только при подтвержденных блокирующих факторах (обложной дождь +RA, низкий плотный Stratus OVC <= 2000 ft, густой туман FG).
     - После 12:30 LT (12:30–15:00 LT): если темп упал ниже +0.4°C/ч под слоистой облачностью И отставание от страйка составляет >= 1.5°C.
   • ЗАПРЕЩЕНО паниковать и объявлять экстренный выход в 10:30–11:30 LT при высокой облачности (OVC100+) или утренней паузе прогрева!
4. 🟡 ДЕРЖАТЬ ПОЗИЦИЮ (УТРЕННЯЯ ПАУЗА ПРОГРЕВА):
   • Триггер: Местное время 09:00–12:30 LT, темп временно замедлен (< +0.4°C/ч), но блокирующих осадков / низкого Stratus нет.
   • Действие: Держать позицию! Солнечный полдень и максимальный угол инсоляции приходятся на 12:30–14:00 LT. Дать атмосфере раскрыться.
5. 🟢 ДЕРЖАТЬ ПОЗИЦИЮ (УТРЕННИЙ ПОЛ):
   • Триггер: Местное время < 09:00 LT. Предрассветное выхолаживание — норма физики. Паниковать и продавать на утреннем минимуме запрещено!
6. 🟢 ДЕРЖАТЬ ПОЗИЦИЮ:
   • Триггер: Темп прогрева в норме, запас инсоляции достаточен, страйк подтверждается ведущей моделью (ICON), цель тейк-профита еще не достигнута.

ФОРМАТ ВЫВОДА:
Пиши строго по пунктам, плотно, без лишних введений и общих рассуждений. Объем текста должен быть около 2500–3300 символов, чтобы полностью помещаться в одно сообщение Telegram. Все 5 разделов должны быть полностью раскрыты и логически завершены.
"""

SYSTEM_PROMPT_V7_4 = SYSTEM_PROMPT_V8_0


class DailyAnalysisTracker:
    """
    Хранит историю анализов в течение текущего дня в разрезе (user_id, icao, date_str).
    Обеспечивает контекст повторного запроса в один и тот же день (Сценарий Г)
    и автоматически сбрасывает историю при наступлении нового дня.
    """
    def __init__(self):
        # Ключ: (user_id, icao, date_str) -> snapshot dict
        self._history: Dict[tuple, Dict[str, Any]] = {}

    def cleanup_old_dates(self, current_date_str: str) -> None:
        """Сбрасывает историю за предыдущие дни."""
        if not current_date_str:
            return
        keys_to_delete = [k for k in self._history if k[2] != current_date_str]
        for k in keys_to_delete:
            del self._history[k]

    def record(self, user_id: int, icao: str, date_str: str, snapshot: Dict[str, Any]) -> None:
        """Сохраняет актуальный срез анализа по городу."""
        if not icao or not date_str:
            return
        self.cleanup_old_dates(date_str)
        uid = int(user_id or 0)
        icao_clean = icao.upper().strip()
        self._history[(uid, icao_clean, date_str)] = snapshot
        if uid != 0:
            self._history[(0, icao_clean, date_str)] = snapshot

    def get_latest(self, user_id: int, icao: str, date_str: str) -> Optional[Dict[str, Any]]:
        """
        Возвращает предыдущий срез анализа для пользователя в этот же день.
        Если персонального среза нет, пробует взять общий срез.
        """
        if not icao or not date_str:
            return None
        self.cleanup_old_dates(date_str)
        uid = int(user_id or 0)
        icao_clean = icao.upper().strip()

        # 1. Персональный поиск
        key = (uid, icao_clean, date_str)
        if key in self._history:
            return self._history[key]

        # 2. Поиск общего среза
        if uid != 0:
            gen_key = (0, icao_clean, date_str)
            if gen_key in self._history:
                return self._history[gen_key]

        return None

    def clear_all(self) -> None:
        """Сброс всей истории (для тестов)."""
        self._history.clear()


daily_tracker = DailyAnalysisTracker()


def compute_orderbook_shifts(prev_ob: List[Dict[str, Any]], curr_ob: List[Dict[str, Any]]) -> str:
    """
    Сопоставляет котировки стакана Polymarket между двумя замерами.
    Возвращает строку со списком сдвигов цен >= 1.0¢.
    """
    if not prev_ob or not curr_ob:
        return "стакан без изменений"

    prev_map = {}
    for item in prev_ob:
        k = item.get("title") or item.get("temp")
        if k is not None:
            prev_map[str(k).strip()] = item.get("price_cents", 0.0)

    shifts = []
    for item in curr_ob:
        k = item.get("title") or item.get("temp")
        if k is None:
            continue
        k_str = str(k).strip()
        if k_str in prev_map:
            p_prev = prev_map[k_str]
            p_curr = item.get("price_cents", 0.0)
            diff = p_curr - p_prev
            if abs(diff) >= 1.0:
                short_title = k_str.replace("°C", "")
                if short_title.isdigit() or (short_title.startswith("-") and short_title[1:].isdigit()):
                    short_title = f"{short_title}°C"
                shifts.append(f"{short_title}: {p_prev:.0f}¢ ➔ {p_curr:.0f}¢ ({diff:+.0f}¢)")

    if not shifts:
        return "без существенных изменений цен"

    return ", ".join(shifts)


def compute_weather_delta(prev_snap: Dict[str, Any], curr_snap: Dict[str, Any]) -> Dict[str, Any]:
    """
    Вычисляет динамику и дельту между прошлым и текущим срезом анализа.
    """
    t_prev_ts = prev_snap.get("timestamp", 0.0)
    t_curr_ts = curr_snap.get("timestamp", time.time())
    elapsed_sec = max(0.0, t_curr_ts - t_prev_ts)
    elapsed_min = int(round(elapsed_sec / 60.0))

    time_prev = prev_snap.get("time_str", "Н/Д")
    time_curr = curr_snap.get("time_str", "Н/Д")

    temp_prev = prev_snap.get("temp_c")
    temp_curr = curr_snap.get("temp_c")

    temp_diff = 0.0
    temp_diff_str = "0.0°C"
    if temp_prev is not None and temp_curr is not None:
        temp_diff = round(float(temp_curr) - float(temp_prev), 1)
        temp_diff_str = f"{temp_diff:+.1f}°C"

    # Расчет темпа прогрева на интервале (dT / dt)
    if elapsed_min >= 5 and temp_prev is not None and temp_curr is not None:
        rate_interval = round(temp_diff / (elapsed_min / 60.0), 2)
        interval_rate_str = f"{rate_interval:+.2f}°C/ч"
    else:
        interval_rate_str = curr_snap.get("rate_str", "Н/Д")

    # Сдвиги стакана
    prev_ob = prev_snap.get("orderbook", [])
    curr_ob = curr_snap.get("orderbook", [])
    ob_shifts_str = compute_orderbook_shifts(prev_ob, curr_ob)

    # Динамика позиции пользователя
    pos_delta_str = ""
    prev_pos = prev_snap.get("user_position")
    curr_pos = curr_snap.get("user_position")
    if prev_pos and curr_pos:
        p1 = float(prev_pos.get("cur_price") or 0.0)
        p2 = float(curr_pos.get("cur_price") or 0.0)
        pnl1 = prev_pos.get("pnl_str", "0%")
        pnl2 = curr_pos.get("pnl_str", "0%")
        outcomes = curr_pos.get("outcomes", "")
        pos_delta_str = f"{outcomes}: цена {p1:.0f}¢ ➔ {p2:.0f}¢, PnL {pnl1} ➔ {pnl2}"

    return {
        "is_update": True,
        "elapsed_min": elapsed_min,
        "time_prev": time_prev,
        "time_curr": time_curr,
        "temp_prev": temp_prev,
        "temp_curr": temp_curr,
        "temp_diff": temp_diff,
        "temp_diff_str": temp_diff_str,
        "rate_prev": prev_snap.get("rate_str", "Н/Д"),
        "rate_curr": curr_snap.get("rate_str", "Н/Д"),
        "interval_rate_str": interval_rate_str,
        "orderbook_shifts_str": ob_shifts_str,
        "pos_delta_str": pos_delta_str,
    }


# Переменная для отслеживания последней успешно ответившей модели Gemini
_LAST_SUCCESSFUL_MODEL: Optional[str] = None


def is_gemini_configured() -> bool:
    """Проверяет, задан ли ключ GEMINI_API_KEY в конфигурации."""
    return bool(config.GEMINI_API_KEY)


def get_gemini_status() -> Dict[str, Any]:
    """
    Возвращает актуальный статус конфигурации Gemini API,
    текущую активную/приоритетную модель и цепочку отказоустойчивости.
    """
    configured = is_gemini_configured()
    preferred_model = getattr(config, "GEMINI_MODEL", "gemini-3.8-flash")
    cascade = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-flash-latest"
    ]
    models_to_try = list(dict.fromkeys([preferred_model] + cascade))
    active_model = _LAST_SUCCESSFUL_MODEL or preferred_model

    return {
        "configured": configured,
        "preferred_model": preferred_model,
        "active_model": active_model,
        "last_successful_model": _LAST_SUCCESSFUL_MODEL,
        "cascade": models_to_try,
        "status_label": "🟢 <b>В сети (Active)</b>" if configured else "🔴 <b>Не настроен (требуется GEMINI_API_KEY)</b>",
    }


def format_markdown_to_telegram_html(text: str) -> str:
    """
    Преобразует Markdown синтаксис Gemini в валидный Telegram HTML
    и защищает от сбоев разметки.
    """
    if not text:
        return ""

    out = text

    # Защита от сырых спецсимволов HTML, которые ломают Telegram парсер
    # 1. Экранируем амперсанд, если он не является частью HTML-сущности
    out = re.sub(r"&(?!amp;|lt;|gt;|quot;|#\d+;)", "&amp;", out)

    # 2. Экранируем < если он не начинает разрешенный тег Telegram
    out = re.sub(r"<(?!/?(?:b|i|u|s|code|pre|a(?:\s+href=[^>]+)?)/?>)", "&lt;", out)

    # 3. Замена заголовков ### Title на <b>Title</b>
    out = re.sub(r"^#{1,6}\s*(.*?)$", r"<b>\1</b>", out, flags=re.MULTILINE)

    # 4. Замена **жирного** на <b>...</b>
    out = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", out)

    # 5. Замена *курсива* или _курсива_
    out = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", out)
    out = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"<i>\1</i>", out)

    # 6. Замена обратных кавычек `code` на <code>code</code>
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)

    # 7. Удаление лишних пустых строк (более 2 подряд)
    out = re.sub(r"\n{3,}", "\n\n", out)

    return out.strip()


async def ask_gemini_model(user_prompt: str, scenario: str = "A") -> Optional[str]:
    """
    Выполняет асинхронный вызов Google Gemini REST API.
    Использует цепочку моделей: gemini-3.8-flash -> gemini-3.7-flash -> gemini-2.5-flash -> gemini-2.0-flash.
    """
    api_key = config.GEMINI_API_KEY
    if not api_key:
        logger.warning("GEMINI_API_KEY не задан в конфигурации.")
        return None

    preferred_model = getattr(config, "GEMINI_MODEL", "gemini-3.8-flash")
    default_cascade = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-flash-latest"
    ]
    # Убираем дубликаты, сохраняя приоритет
    models_to_try = list(dict.fromkeys([preferred_model] + default_cascade))
    
    headers = {
        "Content-Type": "application/json",
    }

    body = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT_V8_0}]
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 8192,
        }
    }

    async with aiohttp.ClientSession() as session:
        for model_name in models_to_try:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            try:
                async with session.post(endpoint, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=35.0)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            content = candidates[0].get("content", {})
                            parts = content.get("parts", [])
                            if parts:
                                raw_text = parts[0].get("text", "")
                                global _LAST_SUCCESSFUL_MODEL
                                _LAST_SUCCESSFUL_MODEL = model_name
                                return format_markdown_to_telegram_html(raw_text)
                    else:
                        err_text = await resp.text()
                        logger.warning(f"Gemini API ({model_name}) вернул HTTP {resp.status}: {err_text[:200]}")
                        continue  # пробуем следующую модель в каскаде при любой ошибке (429, 503, 500 и т.д.)
            except Exception as e:
                logger.warning(f"Ошибка запроса к Gemini ({model_name}): {e}")
                continue

    return None


async def analyze_city_weather_ai(city_data: Dict[str, Any], scenario: str = "A") -> Optional[str]:
    """
    Формирует структурированный синоптический пакет по городу и запрашивает
    квант-анализ у Gemini v7.4.
    """
    if not is_gemini_configured():
        return None

    icao = city_data.get("icao", "Н/Д")
    city_name = city_data.get("city_name", icao)
    local_dt = city_data.get("local_dt")
    local_time_str = local_dt.strftime("%H:%M %Z") if local_dt else "Н/Д"
    
    temp_c = city_data.get("temp_c")
    raw_metar = city_data.get("raw_metar", "")
    models_max = city_data.get("models_max", {})
    rate_str = city_data.get("rate_str", "Н/Д")
    rem_hours_str = city_data.get("rem_hours_str", "Н/Д")
    orderbook = city_data.get("orderbook", [])

    # Извлечение точки росы и расчет депрессии (T - Td)
    dew_point_c = None
    dew_depression_str = "Н/Д"
    metar_match = re.search(r"\b(M?\d{2})/(M?\d{2})\b", raw_metar)
    if metar_match:
        t_raw, td_raw = metar_match.group(1), metar_match.group(2)
        try:
            t_val = -int(t_raw[1:]) if t_raw.startswith("M") else int(t_raw)
            td_val = -int(td_raw[1:]) if td_raw.startswith("M") else int(td_raw)
            dew_point_c = td_val
            dew_depression = round(t_val - td_val, 1)
            dew_depression_str = f"{dew_depression}°C"
        except Exception:
            pass

    # Извлечение ветра и порывов
    wind_match = re.search(r"\b(\d{3})(\d{2,3})(?:G(\d{2,3}))?KT\b", raw_metar)
    if wind_match:
        wdir = wind_match.group(1)
        wspd = wind_match.group(2)
        gust = wind_match.group(3)
        wind_desc = f"{wdir}° {int(wspd)} kt"
        if gust:
            wind_desc += f" (порывы {int(gust)} kt)"
    elif "VRB" in raw_metar:
        wind_desc = "Переменный (VRB)"
    else:
        wind_desc = "Штиль / Не определен"

    # Анализ ярусов облаков и осадков
    ceiling_ft = get_cloud_ceiling_ft(raw_metar)
    has_low = has_real_low_cloud(raw_metar)
    blocking_weather = is_blocking_rain_and_clouds(raw_metar)
    has_cirrus = any(c in raw_metar for c in ["CI", "CS", "FEW2", "SCT2", "FEW3", "SCT3", "NCD", "CAVOK", "CLR", "SKC"])
    has_rain = any(r in raw_metar for r in ["RA", "DZ", "TS", "SN"])

    cloud_desc = []
    if blocking_weather:
        cloud_desc.append("Плотная блокирующая облачность / обложные осадки (Stratus <= 2000 ft или туман)")
    elif has_low:
        c_str = f"{ceiling_ft} ft" if ceiling_ft else "<= 3000 ft"
        cloud_desc.append(f"Низкая слоистая облачность (потолок {c_str}, частично сдерживает радиацию)")
    elif ceiling_ft and ceiling_ft >= 8000:
        cloud_desc.append(f"Высокий/средний ярус (потолок {ceiling_ft} ft — оптически прозрачен для солнца, прогрев не блокирует)")
    elif has_cirrus:
        cloud_desc.append("Высокие перистые облака (Cirrus прозрачны >85% и держат тепло)")
    else:
        cloud_desc.append("Переменная облачность / ясно")
    cloud_tier_str = "; ".join(cloud_desc)

    if blocking_weather:
        rain_desc = "Блокирующие обложные осадки (энтальпийный барьер испарения Lv глушит радиацию)"
    elif has_rain:
        rain_desc = "Слабые локальные осадки/морось с высоким потолком облаков (не блокируют дневной прогрев)"
    else:
        rain_desc = "Сухо (осадки отсутствуют)"

    # Формирование блока стакана цен
    orderbook_lines = []
    if orderbook:
        for item in orderbook:
            t_num = item.get("temp")
            p_cents = item.get("price_cents", 0.0)
            t_str = f"{t_num}°C" if t_num is not None else item.get("title", "Исход")
            orderbook_lines.append(f"  - {t_str}: {p_cents:.1f}¢ (Title: {item.get('title')})")
    else:
        orderbook_lines.append("  (Стакан пуст или котировки пока не опубликованы)")

    orderbook_text = "\n".join(orderbook_lines)

    # Проверка наличия открытой позиции пользователя
    user_position = city_data.get("user_position")
    user_position_block = ""
    if user_position:
        target_outcomes = user_position.get("outcomes", "Н/Д")
        entry_price = float(user_position.get("entry_price") or 0.0)
        cur_price = float(user_position.get("cur_price") or 0.0)
        pnl_str = user_position.get("pnl_str", "+0%")
        target_temp = user_position.get("target_temp")
        target_temp_s = f"{target_temp}°C" if target_temp is not None else "Н/Д"
        calc_verdict = user_position.get("calculated_verdict", "")

        user_position_block = f"""
💼 ВАША ОТКРЫТАЯ ПОЗИЦИЯ В ЭТОМ ГОРОДЕ (ИЗ «МОИ ПОЗИЦИИ»):
• Исход / Страйк: {target_outcomes} (Целевая температура: {target_temp_s})
• Цена входа: {entry_price:.1f}¢
• Текущая цена в стакане Polymarket: {cur_price:.1f}¢
• Текущий PnL сделки: {pnl_str}
• Экспресс-триггер бота: {calc_verdict}

ВНИМАНИЕ: У ТРЕЙДЕРА ОТКРЫТА ЭТА ПОЗИЦИЯ!
Твоя главная задача в этом анализе:
1. В СТАВКЕ/ВЕРХНЕЙ ПЛАШКЕ ОТВЕТА первым делом показать:
💼 <b>ВАША ПОЗИЦИЯ:</b> <code>{target_outcomes}</code> (вход: <code>{entry_price:.0f}¢</code> | сейчас в стакане: <code>{cur_price:.0f}¢</code> | PnL: <b>{pnl_str}</b>)
👉 <b>ВЕРДИКТ ПОЗИЦИИ:</b> <b>[ДЕРЖАТЬ ПОЗИЦИЮ / ТЕЙК-ПРОФИТ (ВЫХОДИ ЛИМИТКОЙ) / ТАЙМ-СТОП (13:30 LT) / ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ)]</b> — [Краткое обоснование: темп прогрева, оставшееся солнце, поведение стакана]
2. В разделе 4 («ТОРГОВЫЙ ПЛАН») четко расписать план выхода или фиксации прибыли по этой сделке!
"""

    user_id = city_data.get("user_id", 0)
    target_date = city_data.get("target_date") or (local_dt.strftime("%Y-%m-%d") if local_dt else None)

    # Текущий снимок для истории и сравнения
    current_snapshot = {
        "timestamp": time.time(),
        "time_str": local_dt.strftime("%H:%M LT") if local_dt else "Н/Д",
        "target_date": target_date,
        "temp_c": temp_c,
        "raw_metar": raw_metar,
        "rate_str": rate_str,
        "rate_val": city_data.get("rate_val", 0.0),
        "rem_hours_str": rem_hours_str,
        "orderbook": orderbook,
        "user_position": user_position,
    }

    # Проверяем, есть ли предыдущий срез в этот же день
    prev_snapshot = city_data.get("previous_analysis")
    if not prev_snapshot and target_date and icao != "Н/Д":
        prev_snapshot = daily_tracker.get_latest(user_id, icao, target_date)

    is_day_update = False
    delta_info: Optional[Dict[str, Any]] = None
    update_prompt_block = ""

    if prev_snapshot:
        delta_info = city_data.get("analysis_delta") or compute_weather_delta(prev_snapshot, current_snapshot)
        is_day_update = True
        elapsed_min_val = delta_info.get("elapsed_min", 0)
        elapsed_display = f"{elapsed_min_val} мин" if elapsed_min_val >= 1 else "менее 1 мин"

        update_prompt_block = f"""
🔄 ДНЕВНОЕ ОБНОВЛЕНИЕ / СРАВНЕНИЕ С ПРОШЛЫМ АНАЛИЗОМ:
• Это ПОВТОРНЫЙ запрос анализа по городу {city_name} за сегодня ({target_date}).
• Прошлый анализ был сделан в {delta_info['time_prev']} ({elapsed_display} назад). Сейчас: {delta_info['time_curr']}.
• Динамика температуры METAR: с {delta_info['temp_prev']}°C до {delta_info['temp_curr']}°C ({delta_info['temp_diff_str']}).
• Темп прогрева на интервале: {delta_info['interval_rate_str']} (ранее общий темп был: {delta_info['rate_prev']}, сейчас: {delta_info['rate_curr']}).
• Сдвиги в стакане цен Polymarket: {delta_info['orderbook_shifts_str']}.
{f"• Динамика вашей позиции: {delta_info['pos_delta_str']}" if delta_info.get('pos_delta_str') else ""}

ОБЯЗАТЕЛЬНО:
1. Выполни анализ по СЦЕНАРИЮ Г (ПОВТОРНЫЙ ЗАПРОС / ДНЕВНОЕ ОБНОВЛЕНИЕ).
2. Начни ответ СТРОГО с плашки обновления:
🔄 <b>ОБНОВЛЕНИЕ АНАЛИЗА: {city_name}</b> (срез {delta_info['time_curr']} относительно {delta_info['time_prev']}, прошло {elapsed_display})
📊 <b>ДИНАМИКА ЗА {elapsed_display.upper()}:</b> Факт: {delta_info['temp_prev']}°C ➔ {delta_info['temp_curr']}°C ({delta_info['temp_diff_str']}) | Темп: {delta_info['interval_rate_str']} | Стакан: {delta_info['orderbook_shifts_str']}
3. Если у трейдера есть открытая позиция — сразу следующими строками выводи блок «💼 ВАША ПОЗИЦИЯ: ...» и «👉 ВЕРДИКТ ПОЗИЦИИ: ...».
4. Сравни текущую ситуацию с утренней: прогрев идет с опережением или затухает? Подтверждается ли утренний страйк, или рынок начал переоценивать другой исход? Что делать с позицией (если открыта)?
"""

    if is_day_update:
        prompt_scenario = "Г (ПОВТОРНЫЙ ЗАПРОС / ДНЕВНОЕ ОБНОВЛЕНИЕ АНАЛИЗА)"
    elif user_position:
        prompt_scenario = "Б (ВНУТРИДНЕВНОЙ МОНИТОРИНГ И ВЕРДИКТ ПОЗИЦИИ)"
    else:
        prompt_scenario = f"{scenario} (Строго по правилам Weather Alpha Engine v8.0)"

    user_prompt = f"""
ВХОДНОЙ МЕТЕОПАКЕТ И СТАКАН ДЛЯ АНАЛИЗА:
• Локация: {city_name} ({icao})
• Местное время: {local_time_str}
• Текущий METAR: {raw_metar if raw_metar else 'Н/Д'}
• Температура: {temp_c if temp_c is not None else 'Н/Д'}°C
• Точка росы: {dew_point_c if dew_point_c is not None else 'Н/Д'}°C | Депрессия точки росы (T - Td): {dew_depression_str}
• Ветер и турбулентность: {wind_desc}
• Ярусы облаков: {cloud_tier_str}
• Статус осадков: {rain_desc}
• Темп прогрева: {rate_str} | До пика инсоляции: {rem_hours_str}
{update_prompt_block}
{user_position_block}
МОДЕЛИ (ДНЕВНЫЕ МАКСИМУМЫ T_MAX):
• ICON: {models_max.get('icon_global', 'Н/Д')}°C (Абсолютный лидер Европы, MAE 0.34-0.60°C)
• ECMWF: {models_max.get('ecmwf_hres', 'Н/Д')}°C (Систематический холодный дефект -1.0°C в Париже и Милане)
• GFS: {models_max.get('gfs_global', 'Н/Д')}°C (Дефект занижения -1.01°C в Мадриде; перегрев в Лондоне при E/NE)
• GEM: {models_max.get('gem_global', 'Н/Д')}°C

ПОЛНЫЙ СТАКАН ЦЕН POLYMARKET / PREDDY:
{orderbook_text}

Выполни квант-анализ по СЦЕНАРИЮ {prompt_scenario}.
Примени 5 Законов Микрофизики (радиация Cirrus vs Stratus, ламинарный перегрев vs турбулентный сдув, энтальпия дождя, вето на дефекты моделей), выяви неэффективность стакана Polymarket и сформируй торговый план с правилом Sniper Momentum / N+1°C и кэшаутом.
{'Если передана открытая позиция — ОБЯЗАТЕЛЬНО начни ответ с оценки позиции и дай однозначный вердикт: ДЕРЖИМ ПОЗИЦИЮ или ВЫХОДИМ (Тейк-профит / Тайм-стоп / Экстренный выход)!' if user_position else ''}
"""

    res = await ask_gemini_model(user_prompt, scenario=scenario)

    if target_date and icao != "Н/Д":
        daily_tracker.record(user_id, icao, target_date, current_snapshot)

    return res
