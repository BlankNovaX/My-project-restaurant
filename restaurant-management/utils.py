"""Business rules and JSON persistence for the restaurant application."""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
SEED_FILE = BASE_DIR / "sample_data.json"
DATA_FILE = Path(os.environ.get("RMS_DATA_FILE", "/tmp/restaurant-management-data.json" if os.environ.get("VERCEL") else str(BASE_DIR / "data.json")))
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_DATA_KEY = os.environ.get("RMS_REDIS_DATA_KEY", "restaurant-management:data")
ROLES = {"admin", "staff", "customer"}
TABLE_STATUSES = {"Vacant", "Occupied", "Awaiting Checkout"}
ORDER_STATUSES = {"active", "preparing", "ready", "paid", "cancelled"}
DEFAULT_MENU_OPTIONS = {
    "spiciness": [("ไม่เผ็ด", 0), ("เผ็ดน้อย", 0), ("เผ็ดกลาง", 0), ("เผ็ดมาก", 0)],
    "portion": [("เล็ก", 0), ("ปกติ", 0), ("ใหญ่", 0)],
    "addons": [],
}


class ValidationError(ValueError):
    """An expected, user-correctable input error."""


class StorageError(RuntimeError):
    """A configured persistent storage service is unavailable or invalid."""


def remote_storage_enabled() -> bool:
    return bool(UPSTASH_URL and UPSTASH_TOKEN)


def uses_ephemeral_storage() -> bool:
    return bool(os.environ.get("VERCEL")) and not remote_storage_enabled()


def contrasting_text_color(hex_color: str) -> str:
    """Choose accessible dark/light foreground text for a validated HEX color."""
    if not isinstance(hex_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", hex_color):
        hex_color = "#176b50"
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    luminance = sum(weight * (channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4) for weight, channel in zip((0.2126, 0.7152, 0.0722), channels))
    return "#ffffff" if luminance < 0.42 else "#20312c"


def _redis_command(command: list[str]) -> Any:
    try:
        request = Request(
            UPSTASH_URL,
            data=json.dumps(command, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
        if "error" in result:
            raise StorageError("บริการจัดเก็บข้อมูลขัดข้อง")
        return result.get("result")
    except StorageError:
        raise
    except (URLError, HTTPError, OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise StorageError("เชื่อมต่อบริการจัดเก็บข้อมูลไม่ได้") from error


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def empty_data() -> dict[str, Any]:
    return {"users": [], "menu_items": [], "tables": [], "orders": [], "reservations": [], "queue": [], "audit": [], "settings": {"restaurant_name": "อิ่มอร่อย", "opening_days": "ทุกวัน", "opening_hours": "10:00 - 22:00", "welcome_message": "อร่อยง่าย สั่งได้เลย", "featured_menu_ids": [], "hero_title": "อร่อยง่าย สั่งได้เลย", "announcement": "", "logo_url": "", "hero_image_url": "", "primary_color": "#176b50", "accent_color": "#d9ef93", "page_background": "#f6f5ef", "card_style": "rounded", "show_featured": True}}


def apply_schema_defaults(data: dict[str, Any]) -> bool:
    """Upgrade older JSON data in place while preserving existing restaurant content."""
    changed = False
    settings = data.setdefault("settings", {})
    design_defaults = (("opening_days", "ทุกวัน"), ("opening_hours", "10:00 - 22:00"), ("welcome_message", "อร่อยง่าย สั่งได้เลย"), ("featured_menu_ids", []), ("hero_title", "อร่อยง่าย สั่งได้เลย"), ("announcement", ""), ("logo_url", ""), ("hero_image_url", ""), ("primary_color", "#176b50"), ("accent_color", "#d9ef93"), ("page_background", "#f6f5ef"), ("card_style", "rounded"), ("show_featured", True))
    for key, value in design_defaults:
        if key not in settings:
            settings[key] = value
            changed = True
    for item in data.get("menu_items", []):
        if not isinstance(item.get("options"), dict):
            legacy_addons = item.get("addons", [])
            options = {key: [{"name": name, "price": price} for name, price in values] for key, values in DEFAULT_MENU_OPTIONS.items()}
            options["addons"] = [{"name": str(name), "price": 0.0} for name in legacy_addons if isinstance(name, str)]
            item["options"] = options
            changed = True
        else:
            for key, values in DEFAULT_MENU_OPTIONS.items():
                if not isinstance(item["options"].get(key), list):
                    legacy_addons = item.get("addons", [])
                    item["options"][key] = ([{"name": str(name), "price": 0.0} for name in legacy_addons if isinstance(name, str)] if key == "addons" else [{"name": name, "price": price} for name, price in values])
                    changed = True
    return changed


def load_data() -> dict[str, Any]:
    """Load data safely; initialize from the bundled sample on first run."""
    if bool(UPSTASH_URL) != bool(UPSTASH_TOKEN):
        raise StorageError("ต้องกำหนด Upstash REST URL และ token ให้ครบทั้งคู่")
    if remote_storage_enabled():
        stored = _redis_command(["GET", REDIS_DATA_KEY])
        if stored is None:
            initial = json.loads(SEED_FILE.read_text(encoding="utf-8")) if SEED_FILE.exists() else empty_data()
            if not save_data(initial):
                raise StorageError("บันทึกข้อมูลเริ่มต้นไม่สำเร็จ")
            return initial
        try:
            data = json.loads(stored)
            if not isinstance(data, dict):
                raise ValueError("invalid data root")
            defaults = empty_data()
            for key, value in defaults.items():
                if not isinstance(data.get(key), type(value)):
                    data[key] = value
            if apply_schema_defaults(data) and not save_data(data):
                raise StorageError("ปรับปรุงรูปแบบข้อมูลร้านไม่สำเร็จ")
            return data
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            raise StorageError("ข้อมูลในบริการจัดเก็บไม่ถูกต้อง") from error
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not DATA_FILE.exists():
            source = SEED_FILE if SEED_FILE.exists() and SEED_FILE.resolve() != DATA_FILE.resolve() else None
            initial = json.loads(source.read_text(encoding="utf-8")) if source else empty_data()
            save_data(initial)
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid data root")
        defaults = empty_data()
        for key, value in defaults.items():
            if not isinstance(data.get(key), type(value)):
                data[key] = value
        if apply_schema_defaults(data) and not save_data(data):
            raise StorageError("ปรับปรุงรูปแบบข้อมูลร้านไม่สำเร็จ")
        return data
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
        # Do not silently replace an unreadable file with empty data on a later save.
        raise StorageError("อ่านไฟล์ข้อมูลไม่ได้ กรุณาตรวจสอบไฟล์ JSON ก่อนใช้งานต่อ") from error


def save_data(data: dict[str, Any]) -> bool:
    """Write JSON atomically. Returns False rather than leaking file errors."""
    if bool(UPSTASH_URL) != bool(UPSTASH_TOKEN):
        raise StorageError("ต้องกำหนด Upstash REST URL และ token ให้ครบทั้งคู่")
    if remote_storage_enabled():
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return _redis_command(["SET", REDIS_DATA_KEY, serialized]) == "OK"
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, DATA_FILE)
        return True
    except OSError:
        return False


def audit(data: dict[str, Any], actor: str, action: str, detail: str) -> None:
    data["audit"].append({"id": str(uuid4()), "at": now_iso(), "actor": actor, "action": action, "detail": detail})
    data["audit"] = data["audit"][-500:]


def ensure_demo_users(data: dict[str, Any]) -> bool:
    """Set up local demo accounts; override credentials with environment variables."""
    changed = False
    for username, role, env_name, default_password in (
        (os.environ.get("RMS_ADMIN_USER", "admin"), "admin", "RMS_ADMIN_PASSWORD", "admin1234"),
        (os.environ.get("RMS_STAFF_USER", "staff"), "staff", "RMS_STAFF_PASSWORD", "staff1234"),
    ):
        if not any(u.get("username") == username for u in data["users"]):
            data["users"].append({"id": str(uuid4()), "username": username, "password_hash": generate_password_hash(os.environ.get(env_name, default_password)), "security_answer_hash": generate_password_hash("helloworld"), "role": role, "created_at": now_iso()})
            changed = True
        else:
            existing = next(u for u in data["users"] if u.get("username") == username)
            if existing.get("role") == role and not existing.get("security_answer_hash"):
                existing["security_answer_hash"] = generate_password_hash("helloworld")
                changed = True
    if changed:
        if not save_data(data):
            raise StorageError("บันทึกบัญชีเริ่มต้นไม่สำเร็จ")
    return changed


def update_restaurant_name(data: dict[str, Any], name_value: Any, actor: str) -> str:
    return update_restaurant_profile(data, {"restaurant_name": name_value}, actor)["restaurant_name"]


def update_restaurant_profile(data: dict[str, Any], form: Any, actor: str) -> dict[str, Any]:
    settings = data.setdefault("settings", {})
    name = normalize_text(form.get("restaurant_name", settings.get("restaurant_name", "อิ่มอร่อย")), "ชื่อร้าน", 80)
    opening_days = normalize_text(form.get("opening_days", settings.get("opening_days", "ทุกวัน")), "วันเปิดร้าน", 100)
    opening_hours = normalize_text(form.get("opening_hours", settings.get("opening_hours", "10:00 - 22:00")), "เวลาเปิดร้าน", 100)
    welcome_message = normalize_text(form.get("welcome_message", settings.get("welcome_message", "อร่อยง่าย สั่งได้เลย")), "ข้อความหน้าร้าน", 160)
    hero_title = normalize_text(form.get("hero_title", settings.get("hero_title", "")), "หัวข้อหน้าร้าน", 80, required=False)
    announcement = normalize_text(form.get("announcement", settings.get("announcement", "")), "ข้อความประกาศ", 200, required=False)
    logo_url = normalize_text(form.get("logo_url", settings.get("logo_url", "")), "URL โลโก้", 500, required=False)
    hero_image_url = normalize_text(form.get("hero_image_url", settings.get("hero_image_url", "")), "URL ภาพปก", 500, required=False)
    for label, image_url in (("โลโก้", logo_url), ("ภาพปก", hero_image_url)):
        if image_url and not re.fullmatch(r"https?://[^\s<>\"']+", image_url, flags=re.IGNORECASE):
            raise ValidationError(f"URL {label} ต้องเป็นลิงก์ http:// หรือ https:// ที่ถูกต้อง")
    colors = {}
    for key, label, default in (("primary_color", "สีหลัก", "#176b50"), ("accent_color", "สีเน้น", "#d9ef93"), ("page_background", "สีพื้นหลัง", "#f6f5ef")):
        value = normalize_text(form.get(key, settings.get(key, default)), label, 7)
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValidationError(f"{label}ต้องเป็นรหัสสี HEX 6 หลัก")
        colors[key] = value.lower()
    card_style = normalize_text(form.get("card_style", settings.get("card_style", "rounded")), "รูปแบบการ์ด", 20)
    if card_style not in {"rounded", "soft", "square"}:
        raise ValidationError("รูปแบบการ์ดไม่ถูกต้อง")
    if hasattr(form, "getlist"):
        featured_flags = form.getlist("show_featured")
        show_featured = any(parse_bool(value, "แสดงเมนูแนะนำ") for value in featured_flags) if featured_flags else False
    else:
        show_featured = parse_bool(form.get("show_featured", settings.get("show_featured", True)), "แสดงเมนูแนะนำ")
    if hasattr(form, "getlist"):
        selected_ids = form.getlist("featured_menu_ids")
    else:
        selected_ids = form.get("featured_menu_ids", settings.get("featured_menu_ids", []))
        if not isinstance(selected_ids, list):
            selected_ids = [selected_ids] if selected_ids else []
    if len(selected_ids) > 12:
        raise ValidationError("เลือกเมนูแนะนำได้ไม่เกิน 12 รายการ")
    valid_ids = {str(item.get("id")) for item in data.get("menu_items", [])}
    if any(str(item_id) not in valid_ids for item_id in selected_ids):
        raise ValidationError("พบเมนูแนะนำที่ไม่มีอยู่ กรุณาโหลดหน้าใหม่")
    settings.update(restaurant_name=name, opening_days=opening_days, opening_hours=opening_hours, welcome_message=welcome_message, featured_menu_ids=list(dict.fromkeys(str(item_id) for item_id in selected_ids)), hero_title=hero_title, announcement=announcement, logo_url=logo_url, hero_image_url=hero_image_url, card_style=card_style, show_featured=show_featured, **colors)
    audit(data, actor, "restaurant_profile", "แก้ไขข้อมูลและหน้าตกแต่งร้าน")
    if not save_data(data):
        raise ValidationError("บันทึกข้อมูลร้านไม่สำเร็จ")
    return settings


def normalize_text(value: Any, label: str, max_length: int = 100, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"กรุณากรอก{label}ให้ถูกต้อง")
    result = value.strip()
    if required and not result:
        raise ValidationError(f"กรุณากรอก{label}")
    if len(result) > max_length:
        raise ValidationError(f"{label}ยาวเกิน {max_length} ตัวอักษร")
    return result


def parse_int(value: Any, label: str, minimum: int = 0, maximum: int = 100000) -> int:
    try:
        if isinstance(value, bool) or str(value).strip() == "":
            raise ValueError
        number = int(str(value).strip())
        if str(number) != str(value).strip() and not isinstance(value, int):
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError(f"{label}ต้องเป็นจำนวนเต็ม") from None
    if not minimum <= number <= maximum:
        raise ValidationError(f"{label}ต้องอยู่ระหว่าง {minimum} ถึง {maximum}")
    return number


def parse_float(value: Any, label: str, minimum: float = 0, maximum: float = 1_000_000) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValidationError(f"{label}ต้องเป็นตัวเลข") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValidationError(f"{label}ต้องอยู่ระหว่าง {minimum:g} ถึง {maximum:g}")
    return round(number, 2)


def parse_bool(value: Any, label: str = "สถานะ") -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "available", "พร้อมขาย"}:
        return True
    if normalized in {"0", "false", "no", "off", "unavailable", "หมด"}:
        return False
    raise ValidationError(f"{label}ไม่ถูกต้อง")


def find_by_id(rows: list[dict[str, Any]], item_id: str | int) -> dict[str, Any] | None:
    target = str(item_id)
    return next((row for row in rows if str(row.get("id")) == target), None)


def paginate(items: list[dict[str, Any]], page_value: Any, per_page: int = 8) -> dict[str, Any]:
    total_pages = max(1, math.ceil(len(items) / per_page))
    try:
        page = max(1, min(int(page_value), total_pages))
    except (TypeError, ValueError):
        page = 1
    start = (page - 1) * per_page
    selected = []
    cursor = start
    stop = min(start + per_page, len(items))
    while cursor < stop:
        selected.append(items[cursor])
        cursor += 1
    return {"items": selected, "page": page, "total_pages": total_pages, "total": len(items)}


def list_records(rows: list[dict[str, Any]], query: str = "", category: str = "", sort_by: str = "name", direction: str = "asc", page: Any = 1, per_page: int = 8, filter_field: str = "category") -> dict[str, Any]:
    """Search, filter, sort, and paginate a collection using allowlisted fields."""
    clean_query = query.casefold().strip()
    filtered = []
    for row in rows:
        haystack = " ".join(str(row.get(k, "")) for k in ("name", "category", "description", "status", "table_id")).casefold()
        if clean_query and clean_query not in haystack:
            continue
        if category and str(row.get(filter_field, "")) != category:
            continue
        filtered.append(row)
    allowed = {"name", "category", "price", "created_at", "status", "id", "table_id"}
    key = sort_by if sort_by in allowed else "name"
    filtered.sort(key=lambda r: (r.get(key) is None, str(r.get(key, "")).casefold() if isinstance(r.get(key), str) else r.get(key, 0)), reverse=direction == "desc")
    return paginate(filtered, page, per_page)


def prepare_registration(data: dict[str, Any], username_value: Any, email_value: Any, password_value: Any, confirm_password_value: Any, security_answer_value: Any = None) -> dict[str, str]:
    username = normalize_text(username_value, "ชื่อผู้ใช้", 40)
    email = normalize_text(email_value, "อีเมล", 254).casefold()
    password = normalize_text(password_value, "รหัสผ่าน", 128)
    confirm_password = normalize_text(confirm_password_value, "ยืนยันรหัสผ่าน", 128)
    security_answer = normalize_text(security_answer_value, "คำตอบยืนยันตัวตน", 120).casefold()
    if len(username) < 3 or len(password) < 8:
        raise ValidationError("ชื่อผู้ใช้ต้องยาวอย่างน้อย 3 ตัว และรหัสผ่านอย่างน้อย 8 ตัว")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValidationError("รูปแบบอีเมลไม่ถูกต้อง")
    if password != confirm_password:
        raise ValidationError("รหัสผ่านและช่องยืนยันรหัสผ่านไม่ตรงกัน")
    if len(security_answer) < 2:
        raise ValidationError("คำตอบยืนยันตัวตนต้องมีอย่างน้อย 2 ตัวอักษร")
    if any(user.get("username", "").casefold() == username.casefold() for user in data["users"]):
        raise ValidationError("ชื่อผู้ใช้นี้ถูกใช้แล้ว")
    if any(user.get("email", "").casefold() == email for user in data["users"] if user.get("email")):
        raise ValidationError("อีเมลนี้ถูกใช้แล้ว")
    return {"username": username, "email": email, "password_hash": generate_password_hash(password), "security_answer_hash": generate_password_hash(security_answer), "created_at": now_iso()}


def complete_registration(data: dict[str, Any], pending: Any) -> dict[str, Any]:
    if not isinstance(pending, dict):
        raise ValidationError("ไม่พบข้อมูลสมัครสมาชิก กรุณาเริ่มใหม่")
    username = normalize_text(pending.get("username"), "ชื่อผู้ใช้", 40)
    email = normalize_text(pending.get("email"), "อีเมล", 254).casefold()
    password_hash = pending.get("password_hash")
    security_answer_hash = pending.get("security_answer_hash")
    if not isinstance(password_hash, str) or len(password_hash) > 512:
        raise ValidationError("ข้อมูลยืนยันสมัครไม่ถูกต้อง กรุณาเริ่มใหม่")
    if not isinstance(security_answer_hash, str) or len(security_answer_hash) > 512:
        raise ValidationError("ไม่พบคำตอบยืนยันตัวตน กรุณาสมัครใหม่")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValidationError("รูปแบบอีเมลไม่ถูกต้อง")
    if any(user.get("username", "").casefold() == username.casefold() for user in data["users"]):
        raise ValidationError("ชื่อผู้ใช้นี้ถูกใช้แล้ว กรุณาสมัครด้วยชื่ออื่น")
    if any(user.get("email", "").casefold() == email for user in data["users"] if user.get("email")):
        raise ValidationError("อีเมลนี้ถูกใช้แล้ว กรุณาสมัครด้วยอีเมลอื่น")
    user = {"id": str(uuid4()), "username": username, "email": email, "password_hash": password_hash, "security_answer_hash": security_answer_hash, "role": "customer", "created_at": now_iso()}
    data["users"].append(user)
    audit(data, username, "register", "สมัครสมาชิก")
    if not save_data(data):
        raise ValidationError("บันทึกข้อมูลไม่สำเร็จ กรุณาลองใหม่")
    return user


def register_user(data: dict[str, Any], username_value: Any, password_value: Any) -> dict[str, Any]:
    """Register immediately for internal callers that don't need confirmation."""
    pending = prepare_registration(data, username_value, f"{username_value}@example.invalid", password_value, password_value)
    return complete_registration(data, pending)


def authenticate(data: dict[str, Any], username_value: Any, password_value: Any) -> dict[str, Any] | None:
    username = normalize_text(username_value, "ชื่อผู้ใช้หรืออีเมล", 254)
    password = normalize_text(password_value, "รหัสผ่าน", 128)
    user = next((u for u in data["users"] if u.get("username", "").casefold() == username.casefold() or u.get("email", "").casefold() == username.casefold()), None)
    if user and user.get("role") in ROLES and check_password_hash(user.get("password_hash", ""), password):
        return user
    return None


def change_user_password(data: dict[str, Any], user_id: Any, current_password_value: Any, new_password_value: Any, confirm_password_value: Any) -> None:
    user = next((row for row in data.get("users", []) if str(row.get("id")) == str(user_id)), None)
    if not user:
        raise ValidationError("ไม่พบบัญชีผู้ใช้")
    current_password = normalize_text(current_password_value, "รหัสผ่านปัจจุบัน", 128)
    new_password = normalize_text(new_password_value, "รหัสผ่านใหม่", 128)
    confirm_password = normalize_text(confirm_password_value, "ยืนยันรหัสผ่านใหม่", 128)
    if not check_password_hash(user.get("password_hash", ""), current_password):
        raise ValidationError("รหัสผ่านปัจจุบันไม่ถูกต้อง")
    if len(new_password) < 8:
        raise ValidationError("รหัสผ่านใหม่ต้องมีอย่างน้อย 8 ตัวอักษร")
    if new_password != confirm_password:
        raise ValidationError("รหัสผ่านใหม่และช่องยืนยันไม่ตรงกัน")
    user["password_hash"] = generate_password_hash(new_password)
    audit(data, user.get("username", "unknown"), "password_change", "เปลี่ยนรหัสผ่าน")
    if not save_data(data):
        raise StorageError("บันทึกการเปลี่ยนรหัสผ่านไม่สำเร็จ กรุณาลองใหม่")


def reset_user_password(data: dict[str, Any], identity_value: Any, answer_value: Any, new_password_value: Any, confirm_password_value: Any) -> None:
    identity = normalize_text(identity_value, "ชื่อผู้ใช้หรืออีเมล", 254).casefold()
    answer = normalize_text(answer_value, "คำตอบยืนยันตัวตน", 120).casefold()
    new_password = normalize_text(new_password_value, "รหัสผ่านใหม่", 128)
    confirm_password = normalize_text(confirm_password_value, "ยืนยันรหัสผ่านใหม่", 128)
    if len(new_password) < 8:
        raise ValidationError("รหัสผ่านใหม่ต้องมีอย่างน้อย 8 ตัวอักษร")
    if new_password != confirm_password:
        raise ValidationError("รหัสผ่านใหม่และช่องยืนยันไม่ตรงกัน")
    user = next((row for row in data.get("users", []) if row.get("username", "").casefold() == identity or row.get("email", "").casefold() == identity), None)
    if not user or not user.get("security_answer_hash") or not check_password_hash(user["security_answer_hash"], answer):
        raise ValidationError("ข้อมูลยืนยันตัวตนไม่ถูกต้อง กรุณาตรวจสอบชื่อบัญชีและคำตอบ")
    user["password_hash"] = generate_password_hash(new_password)
    audit(data, user.get("username", "unknown"), "password_reset", "รีเซ็ตรหัสผ่านด้วยคำตอบยืนยันตัวตน")
    if not save_data(data):
        raise StorageError("บันทึกรหัสผ่านใหม่ไม่สำเร็จ กรุณาลองใหม่")


def update_security_answer(data: dict[str, Any], user_id: Any, current_password_value: Any, answer_value: Any) -> None:
    user = next((row for row in data.get("users", []) if str(row.get("id")) == str(user_id)), None)
    if not user:
        raise ValidationError("ไม่พบบัญชีผู้ใช้")
    current_password = normalize_text(current_password_value, "รหัสผ่านปัจจุบัน", 128)
    answer = normalize_text(answer_value, "คำตอบยืนยันตัวตน", 120).casefold()
    if not check_password_hash(user.get("password_hash", ""), current_password):
        raise ValidationError("รหัสผ่านปัจจุบันไม่ถูกต้อง")
    if len(answer) < 2:
        raise ValidationError("คำตอบยืนยันตัวตนต้องมีอย่างน้อย 2 ตัวอักษร")
    user["security_answer_hash"] = generate_password_hash(answer)
    audit(data, user.get("username", "unknown"), "security_answer_change", "ตั้งหรือเปลี่ยนคำตอบยืนยันตัวตน")
    if not save_data(data):
        raise StorageError("บันทึกคำตอบยืนยันตัวตนไม่สำเร็จ กรุณาลองใหม่")


def parse_option_lines(value: Any, label: str, required: bool = True) -> list[dict[str, Any]]:
    if not isinstance(value, str):
        raise ValidationError(f"ข้อมูล{label}ไม่ถูกต้อง")
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|" not in line:
            raise ValidationError(f"{label}: ใช้รูปแบบ ชื่อตัวเลือก|ราคาเพิ่ม หนึ่งรายการต่อบรรทัด")
        name_value, price_value = line.rsplit("|", 1)
        name = normalize_text(name_value, f"ชื่อตัวเลือก{label}", 40)
        price = parse_float(price_value.strip(), f"ราคาเพิ่ม{label}", 0, 100000)
        if name.casefold() in seen:
            raise ValidationError(f"{label}มีชื่อตัวเลือกซ้ำกัน")
        seen.add(name.casefold())
        options.append({"name": name, "price": price})
        if len(options) > 20:
            raise ValidationError(f"{label}กำหนดได้ไม่เกิน 20 รายการ")
    if required and not options:
        raise ValidationError(f"กรุณากำหนด{label}อย่างน้อยหนึ่งรายการ")
    return options


def save_menu_item(data: dict[str, Any], form: Any, actor: str, item_id: str | None = None) -> dict[str, Any]:
    name = normalize_text(form.get("name"), "ชื่อเมนู", 80)
    category = normalize_text(form.get("category"), "หมวดหมู่", 40)
    price = parse_float(form.get("price"), "ราคา", 0.01, 100000)
    image_url = normalize_text(form.get("image_url", ""), "URL รูปภาพ", 500, required=False)
    if image_url and not image_url.startswith(("https://", "http://")):
        raise ValidationError("URL รูปภาพต้องขึ้นต้นด้วย http:// หรือ https://")
    available = parse_bool(form.get("available", "false"), "สถานะพร้อมขาย")
    spiciness = normalize_text(form.get("spiciness", "ไม่เผ็ด"), "ระดับ", 20)
    portion = normalize_text(form.get("portion", "ปกติ"), "ขนาด", 20)
    options = {
        "spiciness": parse_option_lines(form.get("spiciness_options", "ไม่เผ็ด|0\nเผ็ดน้อย|0\nเผ็ดกลาง|0\nเผ็ดมาก|0"), "ระดับ"),
        "portion": parse_option_lines(form.get("portion_options", "เล็ก|0\nปกติ|0\nใหญ่|0"), "ขนาด"),
        "addons": parse_option_lines(form.get("addon_options", ""), "ท็อปปิ้ง", required=False),
    }
    addons = [option["name"] for option in options["addons"]]
    item = find_by_id(data["menu_items"], item_id) if item_id else None
    if item:
        old_price = item.get("price")
        item.update(name=name, category=category, price=price, image_url=image_url, available=available, spiciness=spiciness, portion=portion, addons=addons, options=options)
        if old_price != price:
            audit(data, actor, "price_change", f"เปลี่ยนราคา {name}: {old_price} → {price:.2f}")
        else:
            audit(data, actor, "menu_update", f"แก้ไขเมนู {name}")
    else:
        item = {"id": max([int(r.get("id", 0)) for r in data["menu_items"]] + [0]) + 1, "name": name, "category": category, "price": price, "image_url": image_url, "available": available, "spiciness": spiciness, "portion": portion, "addons": addons, "options": options, "created_at": now_iso()}
        data["menu_items"].append(item)
        audit(data, actor, "menu_create", f"เพิ่มเมนู {name}")
    if not save_data(data):
        raise ValidationError("บันทึกข้อมูลไม่สำเร็จ")
    return item


def delete_menu_item(data: dict[str, Any], item_id: str, actor: str) -> None:
    item = find_by_id(data["menu_items"], item_id)
    if not item:
        raise ValidationError("ไม่พบเมนูที่ต้องการ")
    data["menu_items"].remove(item)
    audit(data, actor, "menu_delete", f"ลบเมนู {item['name']}")
    if not save_data(data):
        raise ValidationError("บันทึกข้อมูลไม่สำเร็จ")


def save_table(data: dict[str, Any], form: Any, actor: str, table_id: str | None = None) -> dict[str, Any]:
    number = parse_int(form.get("number"), "หมายเลขโต๊ะ", 1, 999)
    seats = parse_int(form.get("seats"), "จำนวนที่นั่ง", 1, 50)
    status = normalize_text(form.get("status", "Vacant"), "สถานะโต๊ะ", 30)
    if status not in TABLE_STATUSES:
        normalized_status = status.casefold()
        if normalized_status == "vacant":
            status = "Vacant"
        elif normalized_status == "occupied":
            status = "Occupied"
        elif normalized_status == "awaiting checkout":
            status = "Awaiting Checkout"
        else:
            raise ValidationError("สถานะโต๊ะไม่ถูกต้อง")
    table = find_by_id(data["tables"], table_id) if table_id else None
    duplicate = next((r for r in data["tables"] if int(r.get("number", 0)) == number and r is not table), None)
    if duplicate:
        raise ValidationError("หมายเลขโต๊ะนี้มีแล้ว")
    if table:
        table.update(number=number, seats=seats, status=status)
        audit(data, actor, "table_update", f"แก้ไขโต๊ะ {number}")
    else:
        table = {"id": max([int(r.get("id", 0)) for r in data["tables"]] + [0]) + 1, "number": number, "seats": seats, "status": status}
        data["tables"].append(table)
        audit(data, actor, "table_create", f"เพิ่มโต๊ะ {number}")
    if not save_data(data):
        raise ValidationError("บันทึกข้อมูลไม่สำเร็จ")
    return table


def delete_table(data: dict[str, Any], table_id: str, actor: str) -> None:
    table = find_by_id(data["tables"], table_id)
    if not table:
        raise ValidationError("ไม่พบโต๊ะที่ต้องการ")
    if any(int(o.get("table_id", -1)) == int(table_id) and o.get("status") not in {"paid", "cancelled"} for o in data["orders"]):
        raise ValidationError("ไม่สามารถลบโต๊ะที่มีออเดอร์ที่ยังไม่ปิดได้")
    data["tables"].remove(table)
    audit(data, actor, "table_delete", f"ลบโต๊ะ {table['number']}")
    if not save_data(data):
        raise ValidationError("บันทึกข้อมูลไม่สำเร็จ")


def create_order(data: dict[str, Any], table_id_value: Any, actor: str) -> dict[str, Any]:
    table_id = parse_int(table_id_value, "โต๊ะ", 1, 100000)
    table = find_by_id(data["tables"], table_id)
    if not table:
        raise ValidationError("ไม่พบโต๊ะนี้")
    if table["status"] == "Awaiting Checkout":
        raise ValidationError("โต๊ะกำลังรอชำระเงิน")
    order = next((o for o in data["orders"] if int(o.get("table_id", -1)) == table_id and o.get("status") not in {"paid", "cancelled"}), None)
    if not order:
        order = {"id": str(uuid4())[:8].upper(), "table_id": table_id, "items": [], "status": "active", "created_at": now_iso(), "updated_at": now_iso()}
        data["orders"].append(order)
    table["status"] = "Occupied"
    audit(data, actor, "order_create", f"เปิดออเดอร์โต๊ะ {table['number']}")
    if not save_data(data):
        raise ValidationError("บันทึกออเดอร์ไม่สำเร็จ กรุณาลองใหม่")
    return order


def _selected_option(options: list[dict[str, Any]], value: Any, label: str) -> dict[str, Any]:
    name = normalize_text(value, label, 40)
    selected = next((option for option in options if option.get("name") == name), None)
    if not selected:
        raise ValidationError(f"ตัวเลือก{label}ไม่ถูกต้อง กรุณาเลือกจากรายการ")
    return selected


def create_customer_order(data: dict[str, Any], customer: dict[str, Any], menu_id_value: Any, quantity_value: Any, selections: Any = None) -> dict[str, Any]:
    """Add a menu item to the signed-in customer's active online order."""
    menu_id = parse_int(menu_id_value, "เมนู", 1, 100000)
    quantity = parse_int(quantity_value, "จำนวน", 1, 50)
    menu = find_by_id(data["menu_items"], menu_id)
    if not menu or not menu.get("available"):
        raise ValidationError("เมนูนี้หมดหรือไม่พร้อมขาย")
    configured = menu.get("options") or {}
    if hasattr(selections, "getlist"):
        addons_value = selections.getlist("addons")
        spicy_value = selections.get("spiciness")
        portion_value = selections.get("portion")
    elif isinstance(selections, dict):
        addons_value = selections.get("addons", [])
        if not isinstance(addons_value, list):
            addons_value = [addons_value] if addons_value else []
        spicy_value = selections.get("spiciness")
        portion_value = selections.get("portion")
    else:
        addons_value, spicy_value, portion_value = [], None, None
    spicy = _selected_option(configured.get("spiciness", []), spicy_value or menu.get("spiciness"), "ระดับ")
    portion = _selected_option(configured.get("portion", []), portion_value or menu.get("portion"), "ขนาด")
    if not spicy_value:
        spicy = configured.get("spiciness", [spicy])[0]
    if not portion_value:
        portion = configured.get("portion", [portion])[0]
    if len(addons_value) > 10 or len(set(addons_value)) != len(addons_value):
        raise ValidationError("เลือกท็อปปิ้งซ้ำหรือมากเกินไป")
    addons = [_selected_option(configured.get("addons", []), value, "ท็อปปิ้ง") for value in addons_value]
    base_price = parse_float(menu.get("price"), "ราคาเมนู", 0, 100000)
    unit_price = round(base_price + float(spicy.get("price", 0)) + float(portion.get("price", 0)) + sum(float(option.get("price", 0)) for option in addons), 2)
    choices = {"spiciness": spicy["name"], "portion": portion["name"], "addons": [option["name"] for option in addons]}
    order = next((o for o in data["orders"] if str(o.get("customer_id")) == str(customer["id"]) and o.get("order_type") == "online" and o.get("status") == "active"), None)
    if not order:
        order = {"id": str(uuid4())[:8].upper(), "table_id": 0, "customer_id": customer["id"], "order_type": "online", "items": [], "status": "active", "created_at": now_iso(), "updated_at": now_iso()}
        data["orders"].append(order)
    line = next((item for item in order["items"] if int(item.get("menu_id", -1)) == menu_id and item.get("options", {}) == choices), None)
    if line:
        if int(line.get("qty", 0)) + quantity > 50:
            raise ValidationError("สั่งเมนูเดียวกันได้ไม่เกิน 50 จานต่อรายการ")
        line["qty"] += quantity
    else:
        order["items"].append({"line_id": str(uuid4()), "menu_id": menu_id, "name": menu["name"], "qty": quantity, "base_price": base_price, "unit_price": unit_price, "options": choices})
    order["updated_at"] = now_iso()
    audit(data, customer["username"], "online_order_item", f"สั่ง {menu['name']} x{quantity} ({choices['spiciness']}, {choices['portion']})")
    if not save_data(data):
        raise ValidationError("บันทึกออเดอร์ไม่สำเร็จ กรุณาลองใหม่")
    return order


def change_customer_order_item(data: dict[str, Any], order_id: str, customer: dict[str, Any], menu_id_value: Any, quantity_value: Any, line_id_value: Any = None) -> dict[str, Any]:
    order = find_by_id(data["orders"], order_id)
    if not order or str(order.get("customer_id")) != str(customer["id"]):
        raise ValidationError("ไม่พบออเดอร์ของบัญชีนี้")
    if order.get("status") != "active":
        raise ValidationError("แก้ไขออเดอร์ได้ก่อนร้านเริ่มเตรียมอาหารเท่านั้น")
    if line_id_value:
        line = next((item for item in order.get("items", []) if str(item.get("line_id")) == str(line_id_value)), None)
        delta = parse_int(quantity_value, "จำนวน", -50, 50)
        if not line or delta == 0 or int(line.get("qty", 0)) + delta < 0 or int(line.get("qty", 0)) + delta > 50:
            raise ValidationError("ปรับจำนวนรายการนี้ไม่ได้")
        line["qty"] += delta
        if line["qty"] == 0:
            order["items"].remove(line)
        if not order["items"]:
            order["status"] = "cancelled"
        order["updated_at"] = now_iso()
        audit(data, customer["username"], "online_order_item_change", f"ปรับจำนวนรายการในออเดอร์ {order_id}: {delta:+d}")
        if not save_data(data):
            raise ValidationError("บันทึกออเดอร์ไม่สำเร็จ กรุณาลองใหม่")
        return order
    return change_order_item(data, order_id, menu_id_value, quantity_value, customer["username"])


def change_order_item(data: dict[str, Any], order_id: str, menu_id: Any, quantity_value: Any, actor: str) -> dict[str, Any]:
    menu_id_int = parse_int(menu_id, "เมนู", 1, 100000)
    quantity = parse_int(quantity_value, "จำนวน", -100, 100)
    if quantity == 0:
        raise ValidationError("จำนวนต้องไม่เป็นศูนย์")
    order = find_by_id(data["orders"], order_id)
    menu = find_by_id(data["menu_items"], menu_id_int)
    if not order or order.get("status") in {"paid", "cancelled"}:
        raise ValidationError("ไม่พบออเดอร์ที่ยังใช้งาน")
    if not menu or (not menu.get("available") and quantity > 0):
        raise ValidationError("เมนูนี้หมดหรือไม่พร้อมขาย")
    line = next((i for i in order["items"] if int(i.get("menu_id", -1)) == menu_id_int), None)
    old_qty = line["qty"] if line else 0
    new_qty = old_qty + quantity
    if new_qty < 0:
        raise ValidationError("จำนวนที่จะลดมากกว่าจำนวนที่สั่ง")
    if new_qty == 0 and line:
        order["items"].remove(line)
    elif line:
        line["qty"] = new_qty
    else:
        order["items"].append({"menu_id": menu_id_int, "name": menu["name"], "qty": new_qty, "unit_price": menu["price"]})
    if not order["items"]:
        order["status"] = "cancelled"
        table = find_by_id(data["tables"], order["table_id"])
        if table:
            table["status"] = "Vacant"
        audit(data, actor, "order_cancel", f"ยกเลิกออเดอร์ {order_id} หลังรายการหมด")
    else:
        order["updated_at"] = now_iso()
        audit(data, actor, "order_item_change", f"ปรับจำนวน {menu['name']} ในออเดอร์ {order_id}: {quantity:+d}")
    if not save_data(data):
        raise ValidationError("บันทึกออเดอร์ไม่สำเร็จ กรุณาลองใหม่")
    return order


def cancel_order(data: dict[str, Any], order_id: str, actor: str) -> None:
    order = find_by_id(data["orders"], order_id)
    if not order or order.get("status") in {"paid", "cancelled"}:
        raise ValidationError("ไม่พบออเดอร์ที่ยกเลิกได้")
    order["status"] = "cancelled"
    table = find_by_id(data["tables"], order["table_id"])
    if table:
        table["status"] = "Vacant"
    audit(data, actor, "order_cancel", f"ยกเลิกออเดอร์ {order_id}")
    if not save_data(data):
        raise ValidationError("บันทึกการยกเลิกออเดอร์ไม่สำเร็จ กรุณาลองใหม่")


def update_order_status(data: dict[str, Any], order_id: str, status_value: Any, actor: str) -> dict[str, Any]:
    status = normalize_text(status_value, "สถานะออเดอร์", 20)
    order = find_by_id(data["orders"], order_id)
    if status not in {"preparing", "ready"} or not order or order.get("status") in {"paid", "cancelled"} or not order.get("items"):
        raise ValidationError("เปลี่ยนสถานะออเดอร์ไม่ได้")
    order["status"] = status
    order["updated_at"] = now_iso()
    if status == "ready" and order["items"]:
        table = find_by_id(data["tables"], order["table_id"])
        if table:
            table["status"] = "Awaiting Checkout"
    elif status == "preparing" and order.get("items"):
        table = find_by_id(data["tables"], order["table_id"])
        if table:
            table["status"] = "Occupied"
    else:
        order["status"] = "active"
    audit(data, actor, "kds_update", f"ออเดอร์ {order_id} → {status}")
    if not save_data(data):
        raise ValidationError("บันทึกสถานะออเดอร์ไม่สำเร็จ กรุณาลองใหม่")
    return order


def order_total(order: dict[str, Any]) -> float:
    return round(sum(float(i.get("unit_price", 0)) * int(i.get("qty", 0)) for i in order.get("items", [])), 2)


def calculate_bill(order: dict[str, Any], discount_value: Any = 0, vat_rate_value: Any = 7, service_rate_value: Any = 0) -> dict[str, float]:
    subtotal = order_total(order)
    discount_rate = parse_float(discount_value, "ส่วนลด (%)", 0, 100)
    vat_rate = parse_float(vat_rate_value, "VAT (%)", 0, 30)
    service_rate = parse_float(service_rate_value, "ค่าบริการ (%)", 0, 30)
    discount = round(subtotal * discount_rate / 100, 2)
    service = round((subtotal - discount) * service_rate / 100, 2)
    vat = round((subtotal - discount + service) * vat_rate / 100, 2)
    return {"subtotal": subtotal, "discount": discount, "discount_rate": discount_rate, "service": service, "service_rate": service_rate, "vat": vat, "vat_rate": vat_rate, "total": round(subtotal - discount + service + vat, 2)}


def checkout(data: dict[str, Any], order_id: str, form: Any, actor: str) -> dict[str, Any]:
    order = find_by_id(data["orders"], order_id)
    if not order or order.get("status") in {"paid", "cancelled"} or not order.get("items"):
        raise ValidationError("ออเดอร์นี้ยังชำระเงินไม่ได้")
    bill = calculate_bill(order, form.get("discount", 0), form.get("vat_rate", 7), form.get("service_rate", 0))
    order["bill"] = bill
    order["status"] = "paid"
    order["paid_at"] = now_iso()
    table = find_by_id(data["tables"], order["table_id"])
    if table:
        table["status"] = "Vacant"
    audit(data, actor, "checkout", f"ชำระออเดอร์ {order_id}: {bill['total']:.2f} บาท")
    if not save_data(data):
        raise ValidationError("บันทึกการชำระเงินไม่สำเร็จ กรุณาลองใหม่")
    return bill


def daily_report(data: dict[str, Any]) -> dict[str, Any]:
    today = datetime.now().astimezone().date().isoformat()
    paid = [o for o in data["orders"] if o.get("status") == "paid" and str(o.get("paid_at", "")).startswith(today)]
    sales = round(sum(float(o.get("bill", {}).get("total", 0)) for o in paid), 2)
    best: dict[str, int] = {}
    for order in paid:
        for item in order.get("items", []):
            best[item["name"]] = best.get(item["name"], 0) + int(item.get("qty", 0))
    best_sellers = sorted([{"name": name, "qty": qty} for name, qty in best.items()], key=lambda x: (-x["qty"], x["name"]))[:5]
    return {"date": today, "sales": sales, "orders": len(paid), "best_sellers": best_sellers}


def move_table_order(data: dict[str, Any], order_id: str, target_table_value: Any, actor: str) -> dict[str, Any]:
    order = find_by_id(data["orders"], order_id)
    target_id = parse_int(target_table_value, "โต๊ะปลายทาง", 1, 100000)
    target = find_by_id(data["tables"], target_id)
    if not order or order.get("status") in {"paid", "cancelled"} or not target:
        raise ValidationError("ย้ายออเดอร์ไม่ได้")
    old = find_by_id(data["tables"], order["table_id"])
    if target_id != int(order["table_id"]) and target.get("status") != "Vacant":
        raise ValidationError("โต๊ะปลายทางไม่ว่าง กรุณาเลือกโต๊ะว่าง")
    order["table_id"] = target_id
    target["status"] = "Occupied"
    if old and not any(int(o.get("table_id", -1)) == int(old["id"]) and o.get("status") not in {"paid", "cancelled"} for o in data["orders"]):
        old["status"] = "Vacant"
    audit(data, actor, "table_move", f"ย้ายออเดอร์ {order_id} ไปโต๊ะ {target['number']}")
    if not save_data(data):
        raise ValidationError("บันทึกการย้ายโต๊ะไม่สำเร็จ กรุณาลองใหม่")
    return order
