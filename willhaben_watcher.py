#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Willhaben Wien — монитор аренды квартир с алертами в Telegram.

Что делает:
  1. Забирает свежие объявления с willhaben.at (раздел Mietwohnungen, Wien).
  2. Фильтрует по твоим критериям (цена, комнаты, кухня, ohne Ablöse и т.д.).
  3. Помечает "приоритетные" и "критичные" находки.
  4. Дедуплицирует (один и тот же объект не присылается дважды).
  5. Шлёт красивый алерт в Telegram.

Тебе НЕ нужно ничего писать в коде — все настройки в блоке CONFIG ниже.
"""

import json
import os
import re
import sys
import time
import html
from datetime import datetime

import requests

# curl_cffi имитирует TLS-отпечаток настоящего Chrome — сильно повышает шанс
# пройти анти-бот защиту Willhaben с облачного (датацентр) IP. Если пакет
# не установлен, откатываемся на обычный requests.
try:
    from curl_cffi import requests as cffi_requests
    HAVE_CFFI = True
except Exception:
    HAVE_CFFI = False

# ============================================================================
#  CONFIG  —  МЕНЯЙ ТОЛЬКО ЗДЕСЬ
# ============================================================================

CONFIG = {
    # --- Жёсткие фильтры (объявление отбрасывается, если не подходит) ---
    "price_max": 1250,          # Максимальная цена в евро (по задумке — с Betriebskosten)
    "rooms_min": 2.5,           # Минимум комнат (2.5 — чтобы ловить и полторашки-«2,5»)

    # Месяцы допустимого заезда. "ab sofort" / раньше — тоже ОК.
    # Отсекаем только если явно указан более поздний месяц (октябрь+).
    "move_in_ok_months": [8, 9],       # август, сентябрь
    "move_in_reject_from_month": 10,   # начиная с октября — отбрасываем

    # --- Пороги для доп. уведомлений ---
    "priority_price_3room": 900,   # 3 комнаты дешевле этой цены -> критично-приоритетно
    "price_all_inclusive_max": 1200,  # если в эту сумму входят свет+вода -> приоритетно

    # --- Willhaben search URL ---
    # areaId для всей Вены. Можно заменить на конкретный район (см. README).
    # ISPRIVATE не задаём, чтобы ловить и частников, и агентства.
    # Твой фильтр: Вена, 3/4/5+ комнат, цена <= 1250. sfId убран (он привязан к
    # сохранённому поиску/сессии), rows=90 добавлен чтобы брать больше за раз.
    "search_url": (
        "https://www.willhaben.at/iad/immobilien/mietwohnungen/mietwohnung-angebote"
        "?isNavigation=true&areaId=900"
        "&NO_OF_ROOMS_BUCKET=3X3&NO_OF_ROOMS_BUCKET=4X4&NO_OF_ROOMS_BUCKET=5X5"
        "&PRICE_TO=1250&rows=90"
    ),

    # Сколько объявлений держать в памяти "уже отправленных" (чтобы seen.json не рос вечно)
    "seen_cap": 4000,

    # Пауза между сообщениями (сек). ~3.5с = ниже лимита группы (~20/мин),
    # чтобы всплеск новых объявлений не упирался в Telegram 429.
    "send_interval_sec": 3.5,

    # Максимум загрузок детальных страниц за один прогон (защита от всплеска).
    # Обычно новых 1–3, так что редко задействуется; лишние переносятся на след. прогон.
    "max_detail_fetches": 30,
}

# --- Ключевые слова (немецкий) для текстового анализа описаний ---
KW = {
    # Кухня как ПОКАЗЫВАЕМЫЙ признак (не фильтр). Отсекаем только явный негатив ниже.
    "kitchen": ["küche", "kueche", "einbauküche", "einbaukueche", "ebk", "küchenzeile",
                "kuechenzeile", "voll ausgestattete küche", "komplettküche", "komplettkueche",
                "markenküche", "markenkueche", "kochnische", "pantryküche", "kitchen"],
    # Явное отсутствие кухни -> отбрасываем
    "kitchen_negative": ["ohne küche", "ohne kueche", "keine küche", "keine kueche",
                         "ohne einbauküche", "ohne einbaukueche", "ohne kücheneinrichtung",
                         "küche nicht vorhanden", "ohne küchenzeile", "keine kücheneinrichtung"],
    "balcony": ["balkon", "terrasse", "loggia", "dachterrasse", "freifläche", "freiflaeche",
                "eigengarten", "gartenanteil"],
    "parking": ["parkplatz", "stellplatz", "garage", "tiefgarage", "abstellplatz",
                "carport", "pkw-stellplatz", "pkw-abstellplatz", "autoabstellplatz",
                "kfz-abstellplatz", "kfz-stellplatz", "garagenplatz"],
    "cellar": ["kellerabteil", "kellerraum", "abstellraum", "kellerabteile"],
    "pool": ["pool", "schwimmbad", "swimmingpool"],
    "gym": ["fitnessraum", "fitnessstudio", "gym", "fitnesscenter"],
    # признаки, что свет/вода/всё включено
    "all_inclusive": ["inkl. strom", "inklusive strom", "strom inklusive",
                      "inkl. wasser", "wasser inklusive", "all-inclusive",
                      "alle betriebskosten inkl", "inkl. aller kosten",
                      "warmmiete inkl", "pauschalmiete", "all inclusive"],
    # без комиссии агентства — просто подсвечиваем в карточке
    "provisionfree": ["provisionsfrei", "provisionsfreie", "provisionsfreier",
                      "keine provision", "ohne provision", "maklerfrei", "ohne makler",
                      "0% provision", "keine maklerprovision", "keine maklergebühr"],
    # стоп-слова (отбрасываем объявление)
    "gemeinde": ["gemeindewohnung", "vormerkschein", "wiener wohnen", "gemeindebau"],
    # ablöse: если есть требование ablöse с суммой — плохо; "keine/ohne ablöse" — хорошо
    "abloese_bad": ["ablöse", "abloese", "ablöseforderung", "abstandszahlung",
                    "abzulösen", "ablösevereinbarung", "gegen abstand", "abstandsablöse"],
    "abloese_good": ["keine ablöse", "ohne ablöse", "keine abloese", "ohne abloese",
                     "ablösefrei", "abloesefrei", "keine ablösezahlung"],
    # "Nachmieter gesucht" почти всегда = Ablöse (за кухню/мебель), а само слово
    # Ablöse часто только в полном описании. Считаем это Ablöse-риском, но с escape:
    # если явно написано "ohne/keine Ablöse" — пропускаем.
    "nachmieter": ["nachmieter", "nachmieterin", "nachmietersuche"],
    # уже зарезервировано/сдано — не присылать. Осторожно: НЕ берём одиночное
    # "vergeben", т.к. "zu vergeben" = наоборот "сдаётся/доступно".
    "reserved": ["reserviert", "bereits vergeben", "schon vergeben",
                 "already reserved", "vermietet", "bereits vermietet"],
}

# WG определяем регуляркой по границам слова (ловит "3er WG", "WG Wohnung",
# "WG-geeignet", "WG-tauglich", Wohngemeinschaft, Mitbewohner), но не задеваем
# посторонние слова, где "wg" внутри.
WG_RE = re.compile(
    r"\bwg\b|\bwg-|wohngemeinschaft|mitbewohner|mitbewohnerin|zimmer\s+in\s+einer\s+wg",
    re.IGNORECASE,
)

WILLHABEN_BASE = "https://www.willhaben.at"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
}

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")


# ============================================================================
#  Telegram
# ============================================================================

def send_telegram(text: str) -> bool:
    """Отправляет сообщение в Telegram. Токен и chat_id — из переменных окружения.
    Корректно обрабатывает 429 (Too Many Requests): читает retry_after,
    ждёт и повторяет, чтобы при всплеске объявлений ничего не потерять."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[WARN] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы — печатаю в консоль:\n")
        print(text)
        print("-" * 60)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }
    for attempt in range(6):
        try:
            r = requests.post(url, data=payload, timeout=20)
        except Exception as e:
            print(f"[ERROR] Telegram exception: {e}")
            time.sleep(3)
            continue

        if r.status_code == 200:
            return True

        if r.status_code == 429:
            # лимит скорости (для групп ~20 сообщений/мин) — уважаем retry_after
            try:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                retry_after = 5
            wait = retry_after + 1
            print(f"[WARN] Telegram 429: жду {wait}s и повторяю…")
            time.sleep(wait)
            continue

        # прочие ошибки — не ретраим
        print(f"[ERROR] Telegram {r.status_code}: {r.text[:200]}")
        return False

    print("[ERROR] Telegram: не удалось отправить после нескольких попыток (429).")
    return False


# ============================================================================
#  Загрузка и парсинг Willhaben
# ============================================================================

_SESSION = None
_SESSION_WARM = False


def _get_session():
    """Одна сессия на процесс (cookies переиспользуются для поиска и деталей)."""
    global _SESSION, _SESSION_WARM
    if _SESSION is None:
        if HAVE_CFFI:
            try:
                _SESSION = cffi_requests.Session(impersonate="chrome")
            except Exception:
                _SESSION = requests.Session()
                _SESSION.headers.update(HEADERS)
        else:
            _SESSION = requests.Session()
            _SESSION.headers.update(HEADERS)
    if not _SESSION_WARM:
        try:
            _SESSION.get("https://www.willhaben.at/iad/immobilien", timeout=30)
        except Exception:
            pass
        _SESSION_WARM = True
    return _SESSION


def http_get_text(url, retries=3):
    """GET страницы через общую сессию с ретраями. Возвращает (status, text)."""
    status, text = None, ""
    for i in range(1, retries + 1):
        try:
            s = _get_session()
            r = s.get(url, headers={"Accept-Language": "de-AT,de;q=0.9,en;q=0.8"},
                      timeout=30)
            status, text = r.status_code, r.text
        except Exception as e:
            print(f"[WARN] Попытка {i}/{retries} ({url[:60]}…): {e}")
            status = None
        if status == 200:
            return status, text
        if i < retries:
            time.sleep(i * 4)
    return status, text


def _strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&[a-z]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_description(data):
    """Из __NEXT_DATA__ детальной страницы вытаскивает полное описание.
    Берём самый длинный текст среди полей description / DESCRIPTION / BODY_DYN —
    это почти всегда основное описание объявления (а не короткие врезки)."""
    candidates = []

    def walk(node):
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and name.upper() in (
                    "DESCRIPTION", "BODY_DYN", "BODY", "PROPERTY_DESCRIPTION"):
                for v in (node.get("values") or []):
                    if isinstance(v, str):
                        candidates.append(v)
            d = node.get("description")
            if isinstance(d, str):
                candidates.append(d)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(data)
    return max(candidates, key=len) if candidates else ""


def fetch_detail_text(url):
    """Возвращает полный текст описания объявления (lowercase) с детальной
    страницы, или '' если не удалось (тогда наверху используется тизер)."""
    if not url or "willhaben.at" not in url:
        return ""
    status, html_text = http_get_text(url, retries=2)
    if status != 200 or not html_text:
        return ""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                  html_text, re.DOTALL)
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return ""
    return _strip_html(_extract_description(data)).lower()


def fetch_listings():
    """
    Забирает страницу поиска, вытаскивает JSON из __NEXT_DATA__ и
    возвращает список объявлений (raw dict'ы Willhaben).
    Делает до 3 попыток — Willhaben с облачного IP иногда отвечает 403 разово.
    """
    url = CONFIG["search_url"]
    print(f"[INFO] Fetch: {url}  (curl_cffi={'да' if HAVE_CFFI else 'нет'})")
    status, html_text = http_get_text(url, retries=3)

    if status == 403:
        print("[ERROR] 403 Forbidden после всех попыток — Willhaben заблокировал запрос "
              "(анти-бот / репутация IP). См. раздел 'Если Willhaben блокирует' в README.")
        return []
    if status != 200:
        print(f"[ERROR] HTTP {status} после всех попыток.")
        return []

    # Основной путь: JSON внутри <script id="__NEXT_DATA__">...</script>
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html_text, re.DOTALL)
    if not m:
        # запасной вариант: иногда блок называется иначе
        m = re.search(r'<script[^>]*type="application/json"[^>]*>(\{.*?"advertSummary".*?\})</script>',
                      html_text, re.DOTALL)
    if not m:
        print("[ERROR] Не найден __NEXT_DATA__. Возможно, Willhaben поменял верстку "
              "или включилась капча. Сохраняю дамп в debug_page.html")
        try:
            with open("debug_page.html", "w", encoding="utf-8") as f:
                f.write(html_text)
        except Exception:
            pass
        return []

    data = json.loads(m.group(1))

    # Пробуем несколько известных путей до массива объявлений
    adverts = _dig_adverts(data)
    print(f"[INFO] Найдено объявлений на странице: {len(adverts)}")
    return adverts


def _dig_adverts(data):
    """Ищет массив advertSummary в разных возможных местах структуры."""
    # Наиболее частый путь
    try:
        return data["props"]["pageProps"]["searchResult"]["advertSummaryList"]["advertSummary"]
    except (KeyError, TypeError):
        pass
    # Иногда лежит чуть иначе — рекурсивный поиск по ключу advertSummary
    found = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "advertSummary" and isinstance(v, list):
                    found.extend(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def get_attr(advert, *names):
    """
    Достаёт значение атрибута объявления по одному из возможных имён.
    Атрибуты лежат в advert['attributes']['attribute'] = [{name, values:[...]}, ...]
    """
    try:
        attrs = advert["attributes"]["attribute"]
    except (KeyError, TypeError):
        attrs = []
    wanted = {n.upper() for n in names}
    for a in attrs:
        if str(a.get("name", "")).upper() in wanted:
            vals = a.get("values") or []
            if vals:
                return vals[0]
    return None


def parse_advert(advert):
    """Нормализует raw-объявление в удобный dict."""
    ad_id = str(advert.get("id") or get_attr(advert, "AD_ID") or "")

    heading = (advert.get("description")
               or get_attr(advert, "HEADING")
               or "").strip()

    # Цена
    price_raw = (get_attr(advert, "PRICE", "PRICE_FOR_DISPLAY", "RENT/PER_MONTH_LETTINGS")
                 or "")
    price = _to_number(price_raw)

    # Комнаты
    rooms_raw = get_attr(advert, "NUMBER_OF_ROOMS", "ROOMS", "NO_OF_ROOMS") or ""
    rooms = _to_number(rooms_raw)

    # Площадь
    size_raw = get_attr(advert, "ESTATE_SIZE", "ESTATE_SIZE/LIVING_AREA",
                        "LIVING_AREA", "ESTATE_SIZE_LIVING") or ""
    size = _to_number(size_raw)

    # Локация / индекс / район
    postcode = get_attr(advert, "POSTCODE") or ""
    location = get_attr(advert, "LOCATION", "ADDRESS", "DISTRICT") or ""

    # Описание (для текстового анализа)
    body = (get_attr(advert, "BODY_DYN", "DESCRIPTION", "TEASER") or "")

    # Признак частник/агентство
    is_private = get_attr(advert, "ISPRIVATE")

    # Ссылка
    seo = get_attr(advert, "SEO_URL") or ""
    if seo:
        link = seo if seo.startswith("http") else f"{WILLHABEN_BASE}/iad/{seo.lstrip('/')}"
    elif ad_id:
        link = f"{WILLHABEN_BASE}/iad/immobilien/d/mietwohnungen/{ad_id}/"
    else:
        link = WILLHABEN_BASE

    # Дата публикации
    published = get_attr(advert, "PUBLISHED_String", "PUBLISHED") or ""

    full_text = f"{heading}\n{body}".lower()

    return {
        "id": ad_id,
        "heading": heading or "(без заголовка)",
        "price": price,
        "price_raw": price_raw,
        "rooms": rooms,
        "size": size,
        "postcode": str(postcode),
        "location": str(location),
        "link": link,
        "published": published,
        "is_private": is_private,
        "text": full_text,
    }


def _to_number(s):
    """
    Парсит числа Willhaben в float. Понимает форматы:
      '1.250 €' -> 1250   (точка = разделитель тысяч)
      '1250,50' -> 1250.5 (запятая = десятичная)
      '1.100,00 €' -> 1100.0
      '1100' -> 1100 ,  '78,5' -> 78.5 ,  '1100.00' -> 1100.0
    Возвращает None если не парсится.
    """
    if s is None:
        return None
    s = str(s)
    s = re.sub(r"[^\d,.\-]", "", s)   # оставляем только цифры/точки/запятые/минус
    if not s:
        return None

    if "," in s and "." in s:
        # немецкий: точки = тысячи, запятая = десятичная
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        # запятая = десятичная
        s = s.replace(",", ".")
    elif "." in s:
        # Только точки. Неоднозначно: тысячи или десятичная?
        parts = s.split(".")
        # Если ВСЕ сегменты после первого состоят ровно из 3 цифр -> тысячи (1.250, 1.100.500)
        if len(parts) > 1 and all(len(p) == 3 and p.isdigit() for p in parts[1:]):
            s = "".join(parts)
        # иначе оставляем как десятичную (78.5, 1100.0)
    try:
        return float(s)
    except ValueError:
        return None


def has_any(text, keywords):
    return any(k in text for k in keywords)


# ============================================================================
#  Фильтрация и оценка
# ============================================================================

def evaluate_cheap(ad):
    """Дешёвые фильтры по данным из выдачи (тизер/заголовок): цена, комнаты,
    Gemeinde, WG, reserviert. Возвращает (ok, rejects)."""
    reject = []
    text = ad["text"]

    if ad["price"] is not None and ad["price"] > CONFIG["price_max"]:
        reject.append(f"цена {ad['price']:.0f}€ > {CONFIG['price_max']}€")
    if ad["rooms"] is not None and ad["rooms"] < CONFIG["rooms_min"]:
        reject.append(f"комнат {ad['rooms']:.1f} < {CONFIG['rooms_min']}")
    if has_any(text, KW["gemeinde"]):
        reject.append("Gemeindewohnung / Vormerkschein")
    if WG_RE.search(text):
        reject.append("WG")
    if has_any(text, KW["reserved"]):
        reject.append("уже зарезервировано / сдано")

    return len(reject) == 0, reject


def evaluate_text(text, ad):
    """Фильтры, которым нужен ПОЛНЫЙ текст описания: Ablöse, отсутствие кухни,
    месяц заезда. text = тизер + полное описание с детальной страницы.
    Возвращает (ok, rejects)."""
    reject = []

    # Кухня (вариант Б): отсекаем только явное отсутствие
    if has_any(text, KW["kitchen_negative"]):
        reject.append("явно без кухни")

    # Ablöse: реальное слово Ablöse/синонимы в полном тексте, но не если ablösefrei
    if has_any(text, KW["abloese_bad"]) and not has_any(text, KW["abloese_good"]):
        reject.append("Ablöse в описании")

    # Месяц заезда: отсекаем только если явно поздний месяц
    month = detect_move_in_month(text)
    if month is not None and month >= CONFIG["move_in_reject_from_month"]:
        reject.append(f"заезд с {month:02d} месяца (поздно)")

    return len(reject) == 0, reject


def compute_tags(text, ad):
    """Признаки для карточки + приоритетные флаги (по полному тексту)."""
    tags = {
        "kitchen": has_any(text, KW["kitchen"]),
        "balcony": has_any(text, KW["balcony"]),
        "parking": has_any(text, KW["parking"]),
        "cellar": has_any(text, KW["cellar"]),
        "pool": has_any(text, KW["pool"]),
        "gym": has_any(text, KW["gym"]),
        "all_inclusive": has_any(text, KW["all_inclusive"]),
        "provisionfree": has_any(text, KW["provisionfree"]),
        "move_in_month": detect_move_in_month(text),
        "move_in_text": extract_move_in_text(text),
        "priority": [],
    }
    if (ad["rooms"] is not None and ad["rooms"] >= 3
            and ad["price"] is not None
            and ad["price"] < CONFIG["priority_price_3room"]):
        tags["priority"].append(
            f"3+ комнаты за {ad['price']:.0f}€ (< {CONFIG['priority_price_3room']}€)")
    if tags["parking"]:
        tags["priority"].append("есть паркоместо/гараж")
    if tags["pool"]:
        tags["priority"].append("есть бассейн")
    if tags["gym"]:
        tags["priority"].append("есть спортзал/фитнес")
    if tags["all_inclusive"] and (ad["price"] is None
                                  or ad["price"] <= CONFIG["price_all_inclusive_max"]):
        tags["priority"].append(
            f"похоже, всё включено (свет/вода) до {CONFIG['price_all_inclusive_max']}€")
    return tags


def extract_move_in_text(text):
    """Возвращает человекочитаемую дату заезда для карточки ('ab sofort',
    'ab September', 'ab 01.09.2025') или None, если не нашли."""
    if re.search(r"\bab\s+sofort\b|sofort\s+bezieh|sofort\s+verfügbar|bezugsfrei\s+ab\s+sofort", text):
        return "ab sofort"

    month_names = ("januar", "jänner", "februar", "märz", "maerz", "april", "mai",
                   "juni", "juli", "august", "september", "oktober", "november", "dezember")
    # дата: ab/verfügbar ab 01.09.2025
    m = re.search(r"(?:verfügbar|bezugsfrei|beziehbar|frei|ab)\s+ab?\s*"
                  r"(\d{1,2}\.\d{1,2}\.\d{2,4})", text)
    if not m:
        m = re.search(r"\bab\s+(\d{1,2}\.\d{1,2}\.\d{2,4})", text)
    if m:
        return f"ab {m.group(1)}"
    # месяц словом
    m = re.search(r"\bab\s+(anfang\s+|mitte\s+|ende\s+)?(" + "|".join(month_names) + r")",
                  text)
    if m:
        return f"ab {(m.group(1) or '').strip()} {m.group(2)}".replace("  ", " ").strip().title()
    m = re.search(r"(?:verfügbar|bezugsfrei|beziehbar|frei)\s+ab\s+(?:\w+\s+)?("
                  + "|".join(month_names) + r")", text)
    if m:
        return f"ab {m.group(1)}".title()
    return None


def detect_move_in_month(text):
    """
    Пытается вытащить месяц заезда из фраз 'ab september', 'verfügbar ab 01.10.2025',
    'bezugsfrei ab oktober' и т.п. Возвращает номер месяца или None.
    'ab sofort' / 'sofort' -> считаем текущим (ранний = ОК) -> вернём 0.
    """
    if re.search(r"\bab\s+sofort\b|\bsofort\s+bezieh|\bbezugsfrei\s+ab\s+sofort", text):
        return 0

    months = {
        "januar": 1, "jänner": 1, "february": 2, "februar": 2, "märz": 3, "maerz": 3,
        "april": 4, "mai": 5, "juni": 6, "juli": 7, "august": 8,
        "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    }
    # 'ab september' / 'verfügbar ab september'
    for name, num in months.items():
        if re.search(rf"\bab\s+(anfang\s+|mitte\s+|ende\s+)?{name}", text):
            return num
        if re.search(rf"(verfügbar|bezugsfrei|beziehbar|frei)\s+ab\s+(\w+\s+)?{name}", text):
            return num

    # даты вида 01.09.2025 / 1.9.25 после 'ab'
    m = re.search(r"\bab\s+\d{1,2}\.(\d{1,2})\.\d{2,4}", text)
    if m:
        try:
            mm = int(m.group(1))
            if 1 <= mm <= 12:
                return mm
        except ValueError:
            pass
    return None


# ============================================================================
#  Формирование сообщения
# ============================================================================

def build_message(ad, tags, price_drop_from=None):
    e = html.escape

    # Заголовок с уровнем важности
    if price_drop_from is not None:
        header = "📉 <b>ЦЕНА СНИЖЕНА</b>"
    elif tags["priority"]:
        header = "🔥 <b>ПРИОРИТЕТ</b>"
    else:
        header = "🏠 <b>Новая квартира</b>"

    lines = [header, ""]
    lines.append(f"<b>{e(ad['heading'])}</b>")

    # Если цена снижена — показываем было -> стало
    if price_drop_from is not None and ad["price"] is not None:
        lines.append(f"💶 <s>{price_drop_from:.0f} €</s> → <b>{ad['price']:.0f} €</b>")

    # Основные факты
    facts = []
    if ad["price"] is not None and price_drop_from is None:
        facts.append(f"💶 {ad['price']:.0f} €")
    if ad["rooms"] is not None:
        r = ad["rooms"]
        rooms_str = f"{r:.1f}".rstrip("0").rstrip(".")  # 3.0->3, 2.5->2,5
        facts.append(f"🚪 {rooms_str} комн.")
    if ad["size"] is not None:
        facts.append(f"📐 {ad['size']:.0f} m²")
    if facts:
        lines.append(" · ".join(facts))

    loc = " ".join(x for x in [ad["postcode"], ad["location"]] if x).strip()
    if loc:
        lines.append(f"📍 {e(loc)}")

    # Дата заезда
    if tags.get("move_in_text"):
        lines.append(f"📅 заезд: {e(tags['move_in_text'])}")

    # Без комиссии — подсвечиваем
    if tags.get("provisionfree"):
        lines.append("💸 <b>без комиссии (provisionsfrei)</b>")

    # Признаки квартиры
    feats = []
    if tags.get("kitchen"):
        feats.append("🍳 кухня")
    if tags["balcony"]:
        feats.append("🌇 балкон/терраса")
    if tags["parking"]:
        feats.append("🚗 паркоместо")
    if tags["cellar"]:
        feats.append("📦 Kellerabteil")
    if tags["pool"]:
        feats.append("🏊 бассейн")
    if tags["gym"]:
        feats.append("🏋️ фитнес")
    if feats:
        lines.append("✅ " + ", ".join(feats))

    # Kellerabteil — отдельная пометка (по твоей просьбе)
    if tags["cellar"]:
        lines.append("ℹ️ <i>Есть упоминание Kellerabteil</i>")

    # Приоритетные причины
    if tags["priority"]:
        lines.append("")
        lines.append("❗️ <b>Почему приоритет:</b>")
        for p in tags["priority"]:
            lines.append(f"   • {e(p)}")

    lines.append("")
    lines.append(f'👉 <a href="{e(ad["link"])}">Открыть на Willhaben</a>')

    return "\n".join(lines)


# ============================================================================
#  Хранилище "уже отправленных"
# ============================================================================

def signature(ad):
    """
    Подпись для «мягкой» дедупликации репостов: тот же заголовок + индекс +
    площадь + комнаты + цена. Если чего-то из этого нет — возвращаем None
    (тогда объявление НЕ душим, чтобы случайно не потерять вариант).
    """
    title = re.sub(r"[^a-z0-9äöüß]+", "", (ad["heading"] or "").lower())
    plz = str(ad["postcode"] or "").strip()
    size, rooms, price = ad["size"], ad["rooms"], ad["price"]
    if not title or not plz or size is None or rooms is None or price is None:
        return None
    return f"{title}|{plz}|{int(round(size))}|{rooms}|{int(round(price))}"


def load_seen():
    """
    Возвращает {"ids": {id: last_price}, "sigs": set(...)}.
    Совместимо со старыми форматами: список id  ->  ids={id:None};
    плоский dict {id: price}  ->  ids=этот dict.
    """
    empty = {"ids": {}, "sigs": set()}
    if not os.path.exists(SEEN_FILE):
        return empty
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):                       # самый старый формат
            return {"ids": {str(i): None for i in data}, "sigs": set()}
        if isinstance(data, dict) and "ids" in data:     # новый формат
            return {"ids": {str(k): v for k, v in data.get("ids", {}).items()},
                    "sigs": set(data.get("sigs", []))}
        if isinstance(data, dict):                       # промежуточный {id: price}
            return {"ids": {str(k): v for k, v in data.items()}, "sigs": set()}
        return empty
    except Exception:
        return empty


def save_seen(seen):
    cap = CONFIG["seen_cap"]
    ids = dict(list(seen["ids"].items())[-cap:])
    sigs = list(seen["sigs"])[-cap:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": ids, "sigs": sigs}, f, ensure_ascii=False)


# ============================================================================
#  main
# ============================================================================

def main():
    dry_run = "--dry-run" in sys.argv   # не сохранять seen, полезно для теста
    verbose = "--verbose" in sys.argv or dry_run
    # RESEND_ALL: разовая переотправка ВСЕХ текущих подходящих (env или флаг)
    resend_all = (os.environ.get("RESEND_ALL", "").strip().lower() in ("1", "true", "yes")
                  or "--resend-all" in sys.argv)

    seen = load_seen()                        # {"ids": {id: price}, "sigs": set()}
    ids = seen["ids"]
    sigs = seen["sigs"]
    first_run = (len(ids) == 0) and not resend_all

    adverts = fetch_listings()               # уже с ретраями внутри
    if not adverts:
        # Не валим прогон с ошибкой из-за разового сбоя сети/403 —
        # следующий запуск по расписанию попробует снова.
        print("[INFO] Нет объявлений (пустой ответ или блокировка). Выхожу без ошибки.")
        return

    new_matches = 0
    drop_matches = 0
    repost_skips = 0
    text_rejects = 0
    detail_fetches = 0
    checked = 0
    first_run_matches = 0

    for advert in adverts:
        try:
            ad = parse_advert(advert)
            if not ad["id"]:
                continue
            checked += 1

            # 1) Дешёвые фильтры по тизеру (цена, комнаты, WG, Gemeinde, reserviert)
            cheap_ok, cheap_reject = evaluate_cheap(ad)
            if verbose:
                st = "cheap-ok" if cheap_ok else "skip"
                print(f"  [{st}] id={ad['id']} {ad['price']}€ {ad['rooms']}к — "
                      f"{ad['heading'][:55]}"
                      + (f"  ({'; '.join(cheap_reject)})" if cheap_reject else ""))
            if not cheap_ok:
                continue   # не запоминаем: если позже подешевеет/изменится — поймаем как новое

            ad_id = ad["id"]
            is_new_id = ad_id not in ids
            prev_price = ids.get(ad_id)
            price_drop = (not is_new_id and prev_price is not None
                          and ad["price"] is not None and ad["price"] < prev_price)
            sig = signature(ad)
            is_repost = is_new_id and sig is not None and sig in sigs

            def remember():
                ids[ad_id] = ad["price"]
                if sig is not None:
                    sigs.add(sig)

            # Первый запуск — только запоминаем, ничего не шлём и детали не грузим
            if first_run:
                remember()
                first_run_matches += 1
                continue

            # Мягкий репост (тот же контент и цена под новым ID) — молчим
            if is_repost and not resend_all:
                remember()
                repost_skips += 1
                continue

            # Решаем, кандидат ли на отправку
            want_send = resend_all or is_new_id or price_drop
            if not want_send:
                remember()               # виденный дубль без снижения цены
                continue

            # 2) Тянем ПОЛНОЕ описание с детальной страницы (для точного фильтра)
            if detail_fetches >= CONFIG["max_detail_fetches"]:
                # превысили лимит за прогон — НЕ запоминаем, попробуем в следующий раз
                print(f"[INFO] Достигнут лимит загрузок деталей ({CONFIG['max_detail_fetches']}), "
                      f"откладываю id={ad_id} на следующий прогон.")
                continue
            detail = fetch_detail_text(ad["link"])
            detail_fetches += 1
            full_text = (ad["text"] + " " + detail) if detail else ad["text"]
            print(f"[detail] id={ad_id}: описание {len(detail)} симв."
                  + ("" if detail else "  (пусто -> использую тизер)"))

            # 3) Фильтры по полному тексту (Ablöse, отсутствие кухни, месяц заезда)
            text_ok, text_reject = evaluate_text(full_text, ad)
            remember()   # запоминаем в любом случае, чтобы не грузить деталь повторно
            if not text_ok:
                text_rejects += 1
                if verbose:
                    print(f"      -> text-skip: {'; '.join(text_reject)}")
                continue

            tags = compute_tags(full_text, ad)
            drop_from = prev_price if (price_drop and not resend_all) else None
            msg = build_message(ad, tags, price_drop_from=drop_from)

            ok = send_telegram(msg)
            if ok:
                if drop_from is not None:
                    drop_matches += 1
                else:
                    new_matches += 1
            # пауза после КАЖДОЙ попытки отправки (держимся ниже лимита группы)
            time.sleep(CONFIG["send_interval_sec"])
        except Exception as e:
            print(f"[WARN] Пропускаю объявление из-за ошибки обработки: {e}")
            continue

    if not dry_run:
        save_seen({"ids": ids, "sigs": sigs})

    print(f"[DONE] Проверено={checked}, новых={new_matches}, снижений цены={drop_matches}, "
          f"репостов пропущено={repost_skips}, отсеяно по описанию={text_rejects}, "
          f"деталей загружено={detail_fetches}, first_run={first_run}, "
          f"resend_all={resend_all}, dry_run={dry_run}")

    if first_run and not dry_run:
        send_telegram(
            "✅ <b>Мониторинг Willhaben запущен</b>\n\n"
            f"Сейчас под критерии подходит <b>{first_run_matches}</b> объявлений — "
            "их показывать не буду (они уже существуют).\n\n"
            "Как только появится <b>новое</b> подходящее — сразу пришлю сюда. "
            "🔥 отдельно помечу приоритетные, 📉 — снижение цены."
        )
        print("[INFO] Первый запуск: запомнил текущие объявления и отправил "
              "подтверждение. Реальные алерты пойдут с новых.")

    if resend_all and not dry_run:
        print(f"[INFO] resend_all: переотправлено {new_matches} объявлений.")


if __name__ == "__main__":
    main()
