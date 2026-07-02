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
    "rooms_min": 3,             # Минимум комнат

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
    "seen_cap": 3000,
}

# --- Ключевые слова (немецкий) для текстового анализа описаний ---
KW = {
    "kitchen": ["küche", "kueche", "einbauküche", "einbaukueche", "ebk", "küchenzeile",
                "voll ausgestattete küche", "komplettküche", "kitchen"],
    "balcony": ["balkon", "terrasse", "loggia", "freifläche", "freiflaeche"],
    "parking": ["parkplatz", "stellplatz", "garage", "tiefgarage", "abstellplatz",
                "carport", "pkw-stellplatz", "autoabstellplatz"],
    "cellar": ["kellerabteil", "keller", "abstellraum", "cellar"],
    "pool": ["pool", "schwimmbad", "swimmingpool"],
    "gym": ["fitnessraum", "fitnessstudio", "gym", "fitness"],
    # признаки, что свет/вода/всё включено
    "all_inclusive": ["inkl. strom", "inklusive strom", "strom inklusive",
                      "inkl. wasser", "wasser inklusive", "all-inclusive",
                      "alle betriebskosten inkl", "inkl. aller kosten",
                      "warmmiete inkl", "pauschalmiete", "all inclusive"],
    # стоп-слова (отбрасываем объявление)
    "gemeinde": ["gemeindewohnung", "vormerkschein", "wiener wohnen", "gemeindebau"],
    "wg": ["wg-zimmer", "wg zimmer", "wohngemeinschaft", "wg-tauglich", "mitbewohner"],
    # ablöse: если есть требование ablöse с суммой — плохо; "keine/ohne ablöse" — хорошо
    "abloese_bad": ["ablöse", "abloese", "ablöseforderung"],
    "abloese_good": ["keine ablöse", "ohne ablöse", "keine abloese", "ohne abloese",
                     "ablösefrei", "abloesefrei", "keine ablösezahlung"],
    # уже зарезервировано/сдано — не присылать. Осторожно: НЕ берём одиночное
    # "vergeben", т.к. "zu vergeben" = наоборот "сдаётся/доступно".
    "reserved": ["reserviert", "bereits vergeben", "schon vergeben",
                 "already reserved", "vermietet", "bereits vermietet"],
}

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
    """Отправляет сообщение в Telegram. Токен и chat_id — из переменных окружения."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[WARN] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы — печатаю в консоль:\n")
        print(text)
        print("-" * 60)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }, timeout=20)
        if r.status_code != 200:
            print(f"[ERROR] Telegram {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[ERROR] Telegram exception: {e}")
        return False


# ============================================================================
#  Загрузка и парсинг Willhaben
# ============================================================================

def _http_get(url):
    """
    Забирает страницу. Приоритет — curl_cffi с имитацией Chrome (лучше проходит
    анти-бот). Сначала "прогреваем" сессию заходом на главную (получаем cookies),
    потом запрашиваем страницу поиска. Возвращает (status_code, text).
    """
    if HAVE_CFFI:
        try:
            s = cffi_requests.Session(impersonate="chrome")
            # прогрев cookies
            try:
                s.get("https://www.willhaben.at/iad/immobilien", timeout=30)
            except Exception:
                pass
            r = s.get(url, headers={"Accept-Language": "de-AT,de;q=0.9,en;q=0.8"},
                      timeout=30)
            return r.status_code, r.text
        except Exception as e:
            print(f"[WARN] curl_cffi не сработал ({e}), пробую requests...")

    # fallback: обычный requests
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get("https://www.willhaben.at/iad/immobilien", timeout=30)
    except Exception:
        pass
    r = s.get(url, timeout=30)
    return r.status_code, r.text


def fetch_listings():
    """
    Забирает страницу поиска, вытаскивает JSON из __NEXT_DATA__ и
    возвращает список объявлений (raw dict'ы Willhaben).
    Делает до 3 попыток — Willhaben с облачного IP иногда отвечает 403 разово.
    """
    url = CONFIG["search_url"]
    print(f"[INFO] Fetch: {url}  (curl_cffi={'да' if HAVE_CFFI else 'нет'})")

    attempts = 3
    status, html_text = None, ""
    for i in range(1, attempts + 1):
        try:
            status, html_text = _http_get(url)
        except Exception as e:
            print(f"[WARN] Попытка {i}/{attempts}: исключение при запросе: {e}")
            status = None
        if status == 200:
            break
        if i < attempts:
            wait = i * 5
            print(f"[WARN] Попытка {i}/{attempts}: статус {status}. Жду {wait}с и повторяю…")
            time.sleep(wait)

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

def evaluate(ad):
    """
    Возвращает (passed: bool, reasons_reject: list, tags: dict)
    tags содержит инфо для алерта: preferred features, priority flags и т.д.
    """
    reject = []
    text = ad["text"]

    # --- Жёсткие фильтры ---
    if ad["price"] is not None and ad["price"] > CONFIG["price_max"]:
        reject.append(f"цена {ad['price']:.0f}€ > {CONFIG['price_max']}€")

    if ad["rooms"] is not None and ad["rooms"] < CONFIG["rooms_min"]:
        reject.append(f"комнат {ad['rooms']:.0f} < {CONFIG['rooms_min']}")

    if has_any(text, KW["gemeinde"]):
        reject.append("Gemeindewohnung / Vormerkschein")

    if has_any(text, KW["wg"]):
        reject.append("WG")

    if has_any(text, KW["reserved"]):
        reject.append("уже зарезервировано / сдано")

    # Кухня: требуем упоминания кухни
    kitchen_ok = has_any(text, KW["kitchen"])
    if not kitchen_ok:
        reject.append("нет явного упоминания кухни")

    # Ablöse: если есть 'ablöse' и НЕТ 'ohne/keine ablöse' — отбрасываем
    if has_any(text, KW["abloese_bad"]) and not has_any(text, KW["abloese_good"]):
        reject.append("возможна Ablöse")

    # Месяц заезда: отсекаем только если явно поздний месяц
    month = detect_move_in_month(text)
    if month is not None and month >= CONFIG["move_in_reject_from_month"]:
        reject.append(f"заезд с {month:02d} месяца (поздно)")

    passed = len(reject) == 0

    # --- Желательные фичи (не влияют на passed) ---
    tags = {
        "balcony": has_any(text, KW["balcony"]),
        "parking": has_any(text, KW["parking"]),
        "cellar": has_any(text, KW["cellar"]),
        "pool": has_any(text, KW["pool"]),
        "gym": has_any(text, KW["gym"]),
        "all_inclusive": has_any(text, KW["all_inclusive"]),
        "move_in_month": month,
        "priority": [],   # список причин "приоритетно/критично"
    }

    # --- Приоритетные / критичные флаги ---
    # 3 комнаты дешевле порога
    if (ad["rooms"] is not None and ad["rooms"] >= 3
            and ad["price"] is not None
            and ad["price"] < CONFIG["priority_price_3room"]):
        tags["priority"].append(
            f"3+ комнаты за {ad['price']:.0f}€ (< {CONFIG['priority_price_3room']}€)")

    # Паркоместо/бассейн/спортзал включены
    if tags["parking"]:
        tags["priority"].append("есть паркоместо/гараж")
    if tags["pool"]:
        tags["priority"].append("есть бассейн")
    if tags["gym"]:
        tags["priority"].append("есть спортзал/фитнес")

    # Свет+вода включены в сумму до 1200
    if tags["all_inclusive"] and (ad["price"] is None
                                  or ad["price"] <= CONFIG["price_all_inclusive_max"]):
        tags["priority"].append(
            f"похоже, всё включено (свет/вода) до {CONFIG['price_all_inclusive_max']}€")

    return passed, reject, tags


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
        facts.append(f"🚪 {ad['rooms']:.0f} комн.")
    if ad["size"] is not None:
        facts.append(f"📐 {ad['size']:.0f} m²")
    if facts:
        lines.append(" · ".join(facts))

    loc = " ".join(x for x in [ad["postcode"], ad["location"]] if x).strip()
    if loc:
        lines.append(f"📍 {e(loc)}")

    # Желательные фичи
    feats = []
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

def load_seen():
    """
    Возвращает dict {id: last_seen_price}. Совместимо со старым форматом
    (простой список id) — он превращается в {id: None}.
    """
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):            # старый формат
            return {str(i): None for i in data}
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items()}
        return {}
    except Exception:
        return {}


def save_seen(seen_map):
    # держим только последние N записей (по порядку вставки)
    items = list(seen_map.items())[-CONFIG["seen_cap"]:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(items), f, ensure_ascii=False)


# ============================================================================
#  main
# ============================================================================

def main():
    dry_run = "--dry-run" in sys.argv   # не сохранять seen, полезно для теста
    verbose = "--verbose" in sys.argv or dry_run
    # RESEND_ALL: разовая переотправка ВСЕХ текущих подходящих (env или флаг)
    resend_all = (os.environ.get("RESEND_ALL", "").strip().lower() in ("1", "true", "yes")
                  or "--resend-all" in sys.argv)

    seen = load_seen()                       # {id: last_price}
    first_run = (len(seen) == 0) and not resend_all

    adverts = fetch_listings()               # уже с ретраями внутри
    if not adverts:
        # Не валим прогон с ошибкой из-за разового сбоя сети/403 —
        # следующий запуск по расписанию попробует снова.
        print("[INFO] Нет объявлений (пустой ответ или блокировка). Выхожу без ошибки.")
        return

    new_matches = 0
    drop_matches = 0
    checked = 0
    first_run_matches = 0
    seen_new = dict(seen)                     # обновляемая копия

    for advert in adverts:
        try:
            ad = parse_advert(advert)
            if not ad["id"]:
                continue
            checked += 1

            passed, reject, tags = evaluate(ad)

            if verbose:
                status = "PASS" if passed else "skip"
                pr = " [PRIORITY]" if tags["priority"] else ""
                print(f"  [{status}]{pr} id={ad['id']} "
                      f"{ad['price']}€ {ad['rooms']}к — {ad['heading'][:60]}"
                      + (f"  ({'; '.join(reject)})" if reject else ""))

            if not passed:
                continue

            ad_id = ad["id"]
            is_new = ad_id not in seen
            prev_price = seen.get(ad_id)
            price_drop = (not is_new and prev_price is not None
                          and ad["price"] is not None and ad["price"] < prev_price)

            # Всегда запоминаем актуальную цену
            seen_new[ad_id] = ad["price"]

            if first_run:
                # первый запуск — не спамим историей, только считаем
                first_run_matches += 1
                continue

            # Решаем, отправлять ли и в каком виде
            if resend_all:
                msg = build_message(ad, tags)     # переотправка как обычная карточка
            elif is_new:
                msg = build_message(ad, tags)
            elif price_drop:
                msg = build_message(ad, tags, price_drop_from=prev_price)
            else:
                continue                          # дубль без снижения цены — пропускаем

            ok = send_telegram(msg)
            if ok:
                if price_drop:
                    drop_matches += 1
                else:
                    new_matches += 1
                time.sleep(1)                     # не долбить Telegram API
        except Exception as e:
            print(f"[WARN] Пропускаю объявление из-за ошибки обработки: {e}")
            continue

    if not dry_run:
        save_seen(seen_new)

    print(f"[DONE] Проверено={checked}, новых={new_matches}, снижений цены={drop_matches}, "
          f"first_run={first_run}, resend_all={resend_all}, dry_run={dry_run}")

    if first_run and not dry_run:
        # Подтверждение в Telegram, что всё поднялось и работает end-to-end
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
