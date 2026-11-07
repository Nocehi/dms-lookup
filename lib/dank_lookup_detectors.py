#!/usr/bin/env python3
"""Local data detectors for an explicit lookup query.

When the *whole* selection is a date, timestamp, URL, e-mail address, unit
quantity, colour code or prefixed integer, :func:`detect` returns a short
local preview for the card. It is pure computation on the query string: the
module opens no socket, starts no process, reads no file and keeps no state.
``tests/test_detectors.py`` enforces the import boundary.
"""

from __future__ import annotations

import colorsys
import datetime as _dt
import math
import os
import re
import unicodedata
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


DETECTOR_KINDS = ("url", "email", "color", "number_base", "timestamp", "date", "unit")
# Arithmetic calendars only; table-driven ones (lunar, 旧暦) come later.
CALENDARS = ("roc", "japanese", "ganzhi", "iso_week")
DEFAULT_MAX_QUERY_CHARS = 200
HARD_MAX_QUERY_CHARS = 1000
MAX_DETECTIONS = 3
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

Detection = dict[str, Any]


def detector_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise the optional ``detectors`` config object; tolerate bad types."""

    raw = config.get("detectors") if isinstance(config, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}
    kinds_raw = raw.get("kinds") if isinstance(raw.get("kinds"), Mapping) else {}
    kinds = {kind: kinds_raw.get(kind, True) is not False for kind in DETECTOR_KINDS}
    calendars_raw = raw.get("calendars", list(CALENDARS))
    if not isinstance(calendars_raw, (list, tuple)):
        calendars_raw = list(CALENDARS)
    calendars = tuple(name for name in CALENDARS if name in calendars_raw)
    order = raw.get("date_order", "auto")
    if order not in ("auto", "dmy", "mdy", "ymd"):
        order = "auto"
    try:
        limit = int(raw.get("max_query_chars", DEFAULT_MAX_QUERY_CHARS))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_QUERY_CHARS
    return {
        "enabled": raw.get("enabled", True) is not False,
        "kinds": kinds,
        "calendars": calendars,
        "date_order": order,
        "max_query_chars": min(HARD_MAX_QUERY_CHARS, max(1, limit)),
    }


def detect(
    query: str,
    config: Mapping[str, Any] | None = None,
    *,
    now: _dt.datetime | None = None,
) -> list[Detection]:
    """Return local previews when the whole query is a recognised value."""

    settings = detector_settings(config)
    if not settings["enabled"]:
        return []
    text = unicodedata.normalize("NFKC", str(query)).strip()
    if not text or len(text) > settings["max_query_chars"] or "\n" in text:
        return []
    now = now or _dt.datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    found: list[Detection] = []
    for kind in DETECTOR_KINDS:
        if not settings["kinds"][kind]:
            continue
        try:
            result = _DETECTORS[kind](text, settings, now)
        except (ValueError, OverflowError, ArithmeticError, UnicodeError):
            result = None
        if result:
            found.append(result)
            if len(found) >= MAX_DETECTIONS:
                break
    return found


def _detection(kind: str, label: str, lines: list[str], copy: list[tuple[str, str]], **extra: Any) -> Detection:
    value: Detection = {
        "kind": kind,
        "label": label,
        "lines": [line for line in lines if line],
        "copy": [{"label": name, "value": item} for name, item in copy if item],
    }
    value.update(extra)
    return value


# ---------------------------------------------------------------- numbers --

def format_number(value: float, significant: int = 4) -> str:
    if value == 0 or not math.isfinite(value):
        return "0" if value == 0 else str(value)
    magnitude = math.floor(math.log10(abs(value)))
    digits = significant - 1 - magnitude
    rounded = round(value, digits)
    if digits <= 0:
        return f"{rounded:,.0f}"
    text = f"{rounded:,.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# -------------------------------------------------------------- url/email --

TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid",
    "igshid", "mc_cid", "mc_eid", "_ga", "_gl", "si", "spm", "ref_src", "ref_url",
})
_URL_RE = re.compile(r"(?i)^(?:(?:https?|ftp)://|www\.)\S+$")
_EMAIL_RE = re.compile(r"(?i)^(?:mailto:)?([^@\s/:<>()\[\],;\"]+)@([^@\s/:<>()\[\],;\"]+\.[^@\s/:<>()\[\],;\"]+)$")


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("utm_") or lowered in TRACKING_PARAMS


def _host_lines(host: str) -> tuple[list[str], str]:
    """Return lines describing a host, revealing IDN/punycode differences."""

    lines: list[str] = []
    display = host
    if any(ord(ch) > 127 for ch in host):
        ascii_host = host.encode("idna").decode("ascii")
        lines.append(f"Punycode：{ascii_host}")
        lines.append("⚠ 網域含非 ASCII 字元，請核對是否為仿冒網域")
    elif "xn--" in host.lower():
        try:
            display = host.encode("ascii").decode("idna")
        except UnicodeError:
            display = host
        if display != host:
            lines.append(f"Unicode：{display}")
            lines.append("⚠ 網域為國際化網域（punycode），請核對是否為仿冒網域")
    return lines, display


def _detect_url(text: str, _settings: Mapping[str, Any], _now: _dt.datetime) -> Detection | None:
    if not _URL_RE.match(text):
        return None
    parse_text = text if "://" in text else "http://" + text
    parts = urlsplit(parse_text)
    host = parts.hostname
    if not host or "." not in host and host != "localhost":
        return None
    lines = [f"主機：{host}"]
    if parts.port is not None:
        lines[0] += f"（連接埠 {parts.port}）"
    host_extra, _display = _host_lines(host)
    lines.extend(host_extra)
    if parts.username is not None or parts.password is not None:
        lines.append(f"⚠ 網址含帳號資訊；實際連往的主機是 {host}")
    scheme = parts.scheme.lower() if "://" in text else "（未指定，預設 http）"
    lines.append(f"協定：{scheme}")
    if parts.scheme.lower() == "http" and "://" in text:
        lines.append("未加密連線（http）")
    path = unquote(parts.path)
    if path and path != "/":
        lines.append(f"路徑：{path}")
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    tracking = [name for name, _value in query_items if _is_tracking(name)]
    kept = [(name, value) for name, value in query_items if not _is_tracking(name)]
    if kept:
        lines.append("參數：" + "、".join(f"{name}={value}" if value else name for name, value in kept[:8]))
    copies = [("網址", text)]
    if tracking:
        lines.append("追蹤參數：" + "、".join(dict.fromkeys(tracking)))
        cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept, doseq=True), parts.fragment))
        if "://" not in text:
            cleaned = cleaned.split("://", 1)[1]
        copies.append(("去追蹤參數", cleaned))
    if parts.fragment:
        lines.append(f"錨點：{unquote(parts.fragment)}")
    return _detection("url", "網址", lines, copies)


def _detect_email(text: str, _settings: Mapping[str, Any], _now: _dt.datetime) -> Detection | None:
    match = _EMAIL_RE.match(text)
    if not match:
        return None
    local, domain = match.group(1), match.group(2).rstrip(".")
    if ".." in domain or domain.startswith(".") or domain.startswith("-"):
        return None
    lines = [f"帳號：{local}", f"網域：{domain}"]
    host_extra, _display = _host_lines(domain)
    lines.extend(host_extra)
    address = f"{local}@{domain}"
    return _detection("email", "電郵", lines, [("地址", address)])


# ------------------------------------------------------------------ color --

_HEX_COLOR_RE = re.compile(r"(?i)^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$")
_NUM = r"\s*(-?\d+(?:\.\d+)?)\s*"
_RGB_RE = re.compile(rf"(?i)^rgba?\({_NUM}(%?),{_NUM}(%?),{_NUM}(%?)(?:,{_NUM}(%?))?\)$")
_HSL_RE = re.compile(rf"(?i)^hsla?\({_NUM}(?:deg)?,{_NUM}%,{_NUM}%(?:,{_NUM}(%?))?\)$")


def _channel(value: str, percent: str) -> int:
    number = float(value)
    if percent:
        number = number * 255 / 100
    if not 0 <= number <= 255:
        raise ValueError("channel out of range")
    return round(number)


def _alpha(value: str | None, percent: str | None) -> float | None:
    if value is None:
        return None
    number = float(value) / (100 if percent else 1)
    if not 0 <= number <= 1:
        raise ValueError("alpha out of range")
    return number


def _detect_color(text: str, _settings: Mapping[str, Any], _now: _dt.datetime) -> Detection | None:
    alpha: float | None = None
    match = _HEX_COLOR_RE.match(text)
    if match:
        digits = match.group(1)
        # "#123" is far more often an issue number than a colour.
        if len(digits) in (3, 4) and not re.search(r"(?i)[a-f]", digits):
            return None
        if len(digits) in (3, 4):
            digits = "".join(ch * 2 for ch in digits)
        red, green, blue = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
        if len(digits) == 8:
            alpha = int(digits[6:8], 16) / 255
    elif (match := _RGB_RE.match(text)):
        red = _channel(match.group(1), match.group(2))
        green = _channel(match.group(3), match.group(4))
        blue = _channel(match.group(5), match.group(6))
        alpha = _alpha(match.group(7), match.group(8))
    elif (match := _HSL_RE.match(text)):
        hue = float(match.group(1)) % 360
        saturation, lightness = float(match.group(2)), float(match.group(3))
        if not (0 <= saturation <= 100 and 0 <= lightness <= 100):
            return None
        r, g, b = colorsys.hls_to_rgb(hue / 360, lightness / 100, saturation / 100)
        red, green, blue = (round(channel * 255) for channel in (r, g, b))
        alpha = _alpha(match.group(4), match.group(5))
    else:
        return None
    hex_value = f"#{red:02x}{green:02x}{blue:02x}"
    hue, light, sat = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
    lines = [
        f"HEX：{hex_value}" + (f"{round(alpha * 255):02x}" if alpha is not None else ""),
        f"RGB：rgb({red}, {green}, {blue})" if alpha is None
        else f"RGBA：rgba({red}, {green}, {blue}, {format_number(alpha, 3)})",
        f"HSL：hsl({round(hue * 360)}, {round(sat * 100)}%, {round(light * 100)}%)",
    ]
    return _detection("color", "顏色", lines, [("HEX", hex_value)], swatch=hex_value)


# ------------------------------------------------------------ number base --

_BASE_RE = re.compile(r"(?i)^(-?)0([xbo])([0-9a-f_]+)$")
_BASES = {"x": 16, "b": 2, "o": 8}


def _detect_number_base(text: str, _settings: Mapping[str, Any], _now: _dt.datetime) -> Detection | None:
    match = _BASE_RE.match(text)
    if not match:
        return None
    sign, prefix, digits = match.group(1), match.group(2).lower(), match.group(3)
    if digits.startswith("_") or digits.endswith("_") or "__" in digits:
        return None
    value = int(digits.replace("_", ""), _BASES[prefix])
    if value.bit_length() > 128:
        return None
    if sign:
        value = -value
    magnitude = abs(value)
    binary = format(magnitude, "b")
    if len(binary) <= 64:
        binary = " ".join(
            binary.zfill(-(-len(binary) // 4) * 4)[i:i + 4]
            for i in range(0, -(-len(binary) // 4) * 4, 4)
        )
    lines = [
        f"十進位：{value:,}",
        f"十六進位：{sign}0x{magnitude:X}",
        f"八進位：{sign}0o{magnitude:o}",
        f"二進位：{sign}0b{binary}",
    ]
    if prefix == "o" and not sign and magnitude <= 0o7777:
        lines.append("權限：" + _permission_string(magnitude))
    return _detection("number_base", "數字", lines, [("十進位", str(value))])


def _permission_string(mode: int) -> str:
    chars = []
    for shift in (6, 3, 0):
        bits = (mode >> shift) & 7
        chars.append(("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-"))
    return "".join(chars)


# ------------------------------------------------------------- date/time --

ROC_EPOCH = 1911
JAPANESE_ERAS = (
    ("令和", _dt.date(2019, 5, 1)),
    ("平成", _dt.date(1989, 1, 8)),
    ("昭和", _dt.date(1926, 12, 25)),
    ("大正", _dt.date(1912, 7, 30)),
    ("明治", _dt.date(1868, 1, 25)),
)
MONTHS = {
    name: index
    for index, names in enumerate((
        ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
        ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
        ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
    ), 1)
    for name in names
}
_MONTH = r"([A-Za-z]{3,9})\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_EN_MDY_RE = re.compile(rf"(?i)^(?:[A-Za-z]{{3,9}},?\s+)?{_MONTH}\s+{_DAY},?\s+(\d{{4}})$")
_EN_DMY_RE = re.compile(rf"(?i)^(?:[A-Za-z]{{3,9}},?\s+)?{_DAY}\s+{_MONTH},?\s+(\d{{4}})$")
_YMD_RE = re.compile(r"^(\d{4})([-/.])(\d{1,2})\2(\d{1,2})$")
_NUMERIC_RE = re.compile(r"^(\d{1,2})([-/.])(\d{1,2})\2(\d{4})$")
_CJK_RE = re.compile(r"^(?:(西元|公元|民國|令和|平成|昭和|大正|明治)\s*)?(\d{1,4}|元)\s*年(?:\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*[日號号])?)?$")
_CJK_MD_RE = re.compile(r"^(\d{1,2})\s*月\s*(\d{1,2})\s*[日號号]$")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?$", re.I)
_TIME_TZ_RE = re.compile(r"(?i)^(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(am|pm)?\s+(utc|gmt|z|[a-z]{2,4}|[+-]\d{1,2}(?::?\d{2})?|(?:utc|gmt)\s*[+-]\d{1,2}(?::?\d{2})?)$")
# Unambiguous abbreviations only: CST/IST/BST name several zones and are skipped.
TZ_ABBREVIATIONS = {
    "utc": 0, "gmt": 0, "z": 0, "jst": 9 * 60, "kst": 9 * 60, "hkt": 8 * 60,
    "pst": -8 * 60, "pdt": -7 * 60, "mst": -7 * 60, "mdt": -6 * 60,
    "est": -5 * 60, "edt": -4 * 60, "cet": 60, "cest": 2 * 60,
    "eet": 2 * 60, "eest": 3 * 60, "aest": 10 * 60, "aedt": 11 * 60,
    "nzst": 12 * 60, "nzdt": 13 * 60, "sgt": 8 * 60,
}


STEMS = "甲乙丙丁戊己庚辛壬癸"
BRANCHES = "子丑寅卯辰巳午未申酉戌亥"


def _era_lines(day: _dt.date, calendars: tuple[str, ...] = CALENDARS) -> list[str]:
    parts = []
    if "roc" in calendars and day.year > ROC_EPOCH:
        roc = day.year - ROC_EPOCH
        parts.append(f"民國 {'元' if roc == 1 else roc} 年")
    if "japanese" in calendars:
        for name, start in JAPANESE_ERAS:
            if day >= start:
                year = day.year - start.year + 1
                parts.append(f"{name} {'元' if year == 1 else year} 年")
                break
    if "ganzhi" in calendars:
        cycle = (day.year - 4) % 60
        ganzhi = f"{STEMS[cycle % 10]}{BRANCHES[cycle % 12]}年"
        # The sexagenary year turns at 立春/春節, not 1 January.
        parts.append(ganzhi + ("（立春／春節前屬前一年）" if day.month <= 2 else ""))
    lines = [" · ".join(parts)] if parts else []
    if "iso_week" in calendars:
        iso_year, week, _weekday = day.isocalendar()
        lines.append(f"ISO {iso_year}-W{week:02d} · 年內第 {day.timetuple().tm_yday} 日")
    return lines


def _relative(day: _dt.date, today: _dt.date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == -1:
        return "昨天"
    if delta > 0:
        return f"{delta:,} 天後"
    return f"{-delta:,} 天前"


def _date_lines(day: _dt.date, today: _dt.date, calendars: tuple[str, ...]) -> list[str]:
    return [f"{day.isoformat()}（{WEEKDAYS[day.weekday()]}）", _relative(day, today), *_era_lines(day, calendars)]


def _offset_text(moment: _dt.datetime) -> str:
    offset = moment.utcoffset() or _dt.timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def _local_moment_lines(moment: _dt.datetime, now: _dt.datetime) -> list[str]:
    local = moment.astimezone(now.tzinfo)
    utc = moment.astimezone(_dt.timezone.utc)
    lines = [
        f"本機：{local:%Y-%m-%d %H:%M:%S}（{WEEKDAYS[local.weekday()]}，UTC{_offset_text(local)}）",
        f"UTC：{utc:%Y-%m-%d %H:%M:%S}",
        _relative(local.date(), now.date()),
    ]
    return lines


def _date_order_first(settings: Mapping[str, Any]) -> str:
    order = settings.get("date_order", "auto")
    if order in ("dmy", "mdy"):
        return order
    locale_name = os.environ.get("LC_ALL") or os.environ.get("LC_TIME") or os.environ.get("LANG") or ""
    return "mdy" if locale_name.startswith(("en_US", "en_PH")) else "dmy"


def _make_date(year: int, month: int, day: int) -> _dt.date | None:
    if not 1 <= year <= 9999:
        return None
    try:
        return _dt.date(year, month, day)
    except ValueError:
        return None


def _detect_date(text: str, settings: Mapping[str, Any], now: _dt.datetime) -> Detection | None:
    today = now.date()
    calendars = tuple(settings.get("calendars", CALENDARS))
    if _ISO_DATETIME_RE.match(text):
        iso = text.replace(" ", "T")
        if iso[-1] in "zZ":
            iso = iso[:-1] + "+00:00"
        iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)
        moment = _dt.datetime.fromisoformat(iso)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=now.tzinfo)
            lines = [f"{moment:%Y-%m-%d %H:%M}（{WEEKDAYS[moment.weekday()]}，視為本機時間）",
                     _relative(moment.date(), today), *_era_lines(moment.date(), calendars)]
        else:
            lines = _local_moment_lines(moment, now) + _era_lines(moment.astimezone(now.tzinfo).date(), calendars)
        return _detection("date", "日期", lines, [("ISO", moment.isoformat())])

    match = _TIME_TZ_RE.match(text)
    if match:
        return _detect_time_with_zone(match, now)

    candidates: list[_dt.date] = []
    if (match := _YMD_RE.match(text)):
        date = _make_date(int(match.group(1)), int(match.group(3)), int(match.group(4)))
        candidates = [date] if date else []
    elif (match := _NUMERIC_RE.match(text)):
        first, second, year = int(match.group(1)), int(match.group(3)), int(match.group(4))
        dmy = _make_date(year, second, first)
        mdy = _make_date(year, first, second)
        ordered = [dmy, mdy] if _date_order_first(settings) == "dmy" else [mdy, dmy]
        candidates = [date for date in dict.fromkeys(ordered) if date]
    elif (match := _EN_MDY_RE.match(text)) or (match := _EN_DMY_RE.match(text)):
        if match.re is _EN_MDY_RE:
            month_name, day_text, year_text = match.group(1), match.group(2), match.group(3)
        else:
            day_text, month_name, year_text = match.group(1), match.group(2), match.group(3)
        month = MONTHS.get(month_name.lower())
        date = _make_date(int(year_text), month, int(day_text)) if month else None
        candidates = [date] if date else []
    elif (match := _CJK_RE.match(text)):
        return _detect_cjk_date(match, today, calendars)
    elif (match := _CJK_MD_RE.match(text)):
        date = _make_date(today.year, int(match.group(1)), int(match.group(2)))
        candidates = [date] if date else []
    if not candidates:
        return None
    if len(candidates) == 1:
        lines = _date_lines(candidates[0], today, calendars)
    else:
        lines = ["日月順序不明，兩種讀法："]
        for date in candidates:
            lines.append(f"{date.isoformat()}（{WEEKDAYS[date.weekday()]}，{_relative(date, today)}）")
    return _detection("date", "日期", lines, [("ISO", candidates[0].isoformat())])


def _detect_cjk_date(match: re.Match[str], today: _dt.date, calendars: tuple[str, ...]) -> Detection | None:
    era, year_text, month_text, day_text = match.groups()
    year_calendars = tuple(name for name in calendars if name != "iso_week")
    if era is None and month_text is None:
        return None  # "2026年" alone is too thin to be worth a card.
    year = 1 if year_text == "元" else int(year_text)
    if era == "民國":
        year += ROC_EPOCH
    elif era in dict(JAPANESE_ERAS):
        year += dict(JAPANESE_ERAS)[era].year - 1
    elif year_text == "元":
        return None
    if month_text is None:
        lines = [f"西元 {year} 年"]
        lines.extend(_era_lines(_dt.date(year, 12, 31), year_calendars) if 1 <= year <= 9999 else [])
        return _detection("date", "日期", lines, [("西元年", str(year))])
    date = _make_date(year, int(month_text), int(day_text) if day_text else 1)
    if date is None:
        return None
    if day_text is None:
        lines = [f"西元 {date.year} 年 {date.month} 月", *_era_lines(date, year_calendars)]
        return _detection("date", "日期", lines, [("ISO", f"{date.year:04d}-{date.month:02d}")])
    return _detection("date", "日期", _date_lines(date, today, calendars), [("ISO", date.isoformat())])


def _zone_offset(raw: str) -> int | None:
    token = raw.lower().replace(" ", "")
    if token in TZ_ABBREVIATIONS:
        return TZ_ABBREVIATIONS[token]
    match = re.match(r"^(?:utc|gmt)?([+-])(\d{1,2})(?::?(\d{2}))?$", token)
    if not match:
        return None
    hours, minutes = int(match.group(2)), int(match.group(3) or 0)
    if hours > 14 or minutes >= 60:
        return None
    total = hours * 60 + minutes
    return total if match.group(1) == "+" else -total


def _detect_time_with_zone(match: re.Match[str], now: _dt.datetime) -> Detection | None:
    meridiem = (match.group(4) or "").lower()
    if match.group(2) is None and not meridiem:
        return None  # "15 UTC" is too ambiguous without minutes or am/pm.
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    second = int(match.group(3) or 0)
    offset = _zone_offset(match.group(5))
    if offset is None:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    zone = _dt.timezone(_dt.timedelta(minutes=offset))
    source_today = now.astimezone(zone).date()
    moment = _dt.datetime.combine(source_today, _dt.time(hour, minute, second), zone)
    local = moment.astimezone(now.tzinfo)
    day_note = ""
    if local.date() != now.date():
        day_note = "，" + _relative(local.date(), now.date())
    lines = [
        f"本機：{local:%H:%M}（UTC{_offset_text(local)}{day_note}）",
        f"UTC：{moment.astimezone(_dt.timezone.utc):%H:%M}",
        "以來源時區的今天換算",
    ]
    return _detection("date", "時間", lines, [("本機時間", f"{local:%H:%M}")])


def _detect_timestamp(text: str, _settings: Mapping[str, Any], now: _dt.datetime) -> Detection | None:
    if not re.fullmatch(r"\d{10}|\d{13}", text):
        return None
    seconds = int(text) / (1000 if len(text) == 13 else 1)
    # 2001-09-09 .. 2100-01-01 keeps phone/order numbers out of this detector.
    if not 1_000_000_000 <= seconds < 4_102_444_800:
        return None
    moment = _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc)
    lines = ["Unix 時間戳" + ("（毫秒）" if len(text) == 13 else "（秒）"), *_local_moment_lines(moment, now)]
    return _detection("timestamp", "時間戳", lines, [("ISO", moment.astimezone(now.tzinfo).isoformat(timespec="seconds"))])


# ------------------------------------------------------------------ units --

# Each unit: category, factor to the category base (m, kg, L, m/s, m², B).
UNITS: dict[str, tuple[str, float]] = {}


def _units(category: str, factor: float, *names: str) -> None:
    for name in names:
        UNITS[name] = (category, factor)


_units("length", 1e-3, "mm", "毫米", "公釐")
_units("length", 1e-2, "cm", "厘米", "釐米", "公分")
_units("length", 1.0, "m", "米", "公尺", "meter", "meters", "metre", "metres")
_units("length", 1e3, "km", "公里", "千米", "kilometer", "kilometers", "kilometre", "kilometres")
_units("length", 0.0254, "in", "inch", "inches", "″", '"', "吋", "英寸")
_units("length", 0.3048, "ft", "foot", "feet", "′", "'", "呎", "英尺")
_units("length", 0.9144, "yd", "yard", "yards", "碼")
_units("length", 1609.344, "mi", "mile", "miles", "英里", "哩")
_units("length", 1852.0, "nmi", "海里", "浬")
_units("mass", 1e-6, "mg", "毫克")
_units("mass", 1e-3, "g", "克", "公克", "gram", "grams")
_units("mass", 1.0, "kg", "公斤", "千克", "kilogram", "kilograms", "kilo", "kilos")
_units("mass", 1e3, "t", "公噸", "tonne", "tonnes")
_units("mass", 0.45359237, "lb", "lbs", "pound", "pounds", "磅")
_units("mass", 0.028349523125, "oz", "ounce", "ounces", "盎司", "安士")
_units("mass", 6.35029318, "st", "stone")
_units("volume", 1e-3, "ml", "mL", "毫升")
_units("volume", 1.0, "l", "L", "公升", "升", "liter", "liters", "litre", "litres")
_units("volume", 3.785411784, "gal", "gallon", "gallons", "加侖")
_units("volume", 0.0295735295625, "fl oz", "floz")
_units("volume", 0.2365882365, "cup", "cups")
_units("speed", 1.0, "m/s")
_units("speed", 1 / 3.6, "km/h", "kph", "kmh", "公里/小時")
_units("speed", 0.44704, "mph")
_units("speed", 1852 / 3600, "kn", "kt", "knot", "knots", "節")
_units("area", 1.0, "m²", "m2", "sqm", "平方米", "平方公尺")
_units("area", 1e6, "km²", "km2", "平方公里")
_units("area", 1e4, "ha", "hectare", "hectares", "公頃")
_units("area", 4046.8564224, "acre", "acres", "英畝")
_units("area", 0.09290304, "ft²", "ft2", "sq ft", "sqft", "平方呎", "平方英尺")
_units("area", 400 / 121, "坪")
_units("data", 1.0, "B", "byte", "bytes")
for _index, _prefix in enumerate("KMGTP", 1):
    _units("data", 1000.0 ** _index, f"{_prefix}B")
    _units("data", 1024.0 ** _index, f"{_prefix}iB")
_units("data", 1000.0, "kB")

# Chinese catties and taels differ by region; every reading is shown.
REGIONAL_MASS = {
    "斤": (("台斤", 0.6), ("市斤", 0.5), ("港斤", 0.60478982)),
    "兩": (("台兩", 0.0375), ("市兩", 0.05), ("港兩", 0.0377993638)),
}
TEMPERATURE_UNITS = {
    "°c": "C", "℃": "C", "c": "C", "度c": "C", "攝氏": "C", "celsius": "C",
    "°f": "F", "℉": "F", "f": "F", "華氏": "F", "fahrenheit": "F",
    "kelvin": "K",
}
TARGETS = {
    "length": ("mm", "cm", "m", "km", "in", "ft", "yd", "mi"),
    "mass": ("g", "kg", "t", "lb", "oz", "斤"),
    "volume": ("ml", "L", "fl oz", "cup", "gal"),
    "speed": ("km/h", "m/s", "mph", "kn"),
    "area": ("m²", "坪", "ft²", "ha", "acre", "km²"),
}
CATEGORY_LABELS = {"length": "長度", "mass": "重量", "volume": "容量", "speed": "速度", "area": "面積", "data": "資料量"}
_QTY_RE = re.compile(r"^(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(.+?)$")
_FEET_INCHES_RE = re.compile(r"""^(\d+)\s*(?:'|′|ft|feet|呎)\s*(\d+(?:\.\d+)?)\s*(?:"|″|in|inches|吋)?$""", re.I)


def _unit_lookup(raw: str) -> tuple[str, float] | None:
    unit = raw.strip()
    if unit in UNITS:
        return UNITS[unit]
    lowered = unit.lower()
    for name, value in UNITS.items():
        # Data sizes are case-significant (mB vs MB); everything else is not.
        if value[0] != "data" and name.lower() == lowered:
            return value
    return None


IMPERIAL = frozenset({"in", "inch", "inches", "ft", "foot", "feet", "yd", "yard", "yards", "mi", "mile", "miles",
                      "oz", "ounce", "ounces", "lb", "lbs", "pound", "pounds", "st", "stone", "gal", "gallon",
                      "gallons", "fl oz", "floz", "cup", "cups", "mph", "acre", "acres", "ft²", "ft2", "sq ft", "sqft",
                      "″", '"', "′", "'", "吋", "英寸", "呎", "英尺", "碼", "英里", "哩", "磅", "盎司", "安士",
                      "加侖", "英畝", "平方呎", "平方英尺"})


# Regional units read best next to metric, so metric leads for them.
METRIC_FIRST = frozenset({"坪"})


def _is_imperial_line(line: str) -> bool:
    return line.split(" ", 1)[-1] in IMPERIAL


def _readable(line: str) -> bool:
    try:
        value = abs(float(line.split(" ", 1)[0].replace(",", "")))
    except ValueError:
        return False
    return 1 <= value < 1000


def _length_lines(metres: float, source: tuple[str, float]) -> list[str]:
    lines = []
    for target in TARGETS["length"]:
        if UNITS[target] == source:
            continue
        amount = metres / UNITS[target][1]
        if 0.1 <= abs(amount) < 100_000:
            lines.append(f"{format_number(amount)} {target}")
    return lines


def _detect_unit(text: str, _settings: Mapping[str, Any], _now: _dt.datetime) -> Detection | None:
    match = _FEET_INCHES_RE.match(text)
    if match:
        feet, inches = int(match.group(1)), float(match.group(2))
        if inches >= 12:
            return None
        metres = feet * 0.3048 + inches * 0.0254
        lines = [f"{feet} ft {format_number(inches)} in", f"{format_number(metres * 100)} cm", f"{format_number(metres)} m"]
        return _detection("unit", "長度", lines, [("cm", format_number(metres * 100).replace(",", "") + " cm")])

    match = _QTY_RE.match(text)
    if not match:
        return None
    number_text, unit_text = match.group(1), match.group(2).strip()
    amount = float(number_text.replace(",", ""))
    key = unit_text.lower().replace(" ", "")

    if key in TEMPERATURE_UNITS or unit_text in TEMPERATURE_UNITS:
        scale = TEMPERATURE_UNITS.get(key) or TEMPERATURE_UNITS[unit_text]
        celsius = {"C": amount, "F": (amount - 32) * 5 / 9, "K": amount - 273.15}[scale]
        if celsius < -273.15:
            return None
        values = {
            "C": f"{format_number(celsius)} °C",
            "F": f"{format_number(celsius * 9 / 5 + 32)} °F",
            "K": f"{format_number(celsius + 273.15)} K",
        }
        lines = [f"{format_number(amount)} {'K' if scale == 'K' else '°' + scale}"]
        lines += [values[name] for name in ("C", "F", "K") if name != scale]
        first = values["C" if scale != "C" else "F"]
        return _detection("unit", "溫度", lines, [("換算", first)])

    if unit_text in REGIONAL_MASS:
        lines = [f"{format_number(amount)} {unit_text}，各地標準不同："]
        copies = []
        for name, kg in REGIONAL_MASS[unit_text]:
            grams = amount * kg * 1000
            lines.append(f"{name}：{format_number(grams)} g（{format_number(grams / 453.59237)} lb）")
            copies.append((name, f"{format_number(grams)} g"))
        return _detection("unit", "重量", lines, copies)

    found = _unit_lookup(unit_text)
    if not found:
        return None
    category, factor = found
    base = amount * factor
    source = f"{number_text} {re.sub(r'(?<=m|t)2$', '²', unit_text)}"
    if category == "data":
        if base < 1:
            return None
        decimal = next((p for p in "PTGMK" if base >= 1000.0 ** ("KMGTP".index(p) + 1)), "")
        binary = next((p for p in "PTGMK" if base >= 1024.0 ** ("KMGTP".index(p) + 1)), "")
        lines = [source]
        if decimal:
            lines.append(f"{format_number(base / 1000.0 ** ('KMGTP'.index(decimal) + 1))} {decimal}B（十進位）")
        if binary:
            lines.append(f"{format_number(base / 1024.0 ** ('KMGTP'.index(binary) + 1))} {binary}iB（二進位）")
        lines.append(f"{base:,.0f} B")
        lines = [lines[0], *(line for line in lines[1:] if not line.startswith(source + "（"))]
        if unit_text in ("KB", "MB", "GB", "TB", "PB"):
            lines.append("註：作業系統常以 KB/MB/GB 表示二進位值")
        return _detection("unit", CATEGORY_LABELS[category], lines, [("位元組", f"{base:.0f}")])

    if category == "length":
        targets = _length_lines(base, found)
    else:
        targets = []
        for target in TARGETS[category]:
            if target == "斤":
                targets.append(f"{format_number(base / 0.6)} 台斤 · {format_number(base / 0.5)} 市斤")
                continue
            if UNITS[target] == found:
                continue
            converted = base / UNITS[target][1]
            if 0.01 <= abs(converted) < 1_000_000:
                targets.append(f"{format_number(converted)} {target}")
    # Lead with the other measurement system, then the most readable size.
    source_imperial = unit_text in IMPERIAL or unit_text.lower() in IMPERIAL or unit_text in METRIC_FIRST
    targets.sort(key=lambda line: (_is_imperial_line(line) == source_imperial, not _readable(line)))
    if not targets:
        return None
    lines = [source, *targets]
    return _detection("unit", CATEGORY_LABELS[category], lines, [("換算", targets[0])])


_DETECTORS: dict[str, Callable[[str, Mapping[str, Any], _dt.datetime], Detection | None]] = {
    "url": _detect_url,
    "email": _detect_email,
    "color": _detect_color,
    "number_base": _detect_number_base,
    "timestamp": _detect_timestamp,
    "date": _detect_date,
    "unit": _detect_unit,
}
