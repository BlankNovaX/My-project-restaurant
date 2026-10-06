"""Business rules and JSON persistence for the restaurant application."""
from __future__ import annotations

import json
import base64
import math
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
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
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ROLES = {"admin", "staff", "customer"}
TABLE_STATUSES = {"Vacant", "Occupied", "Awaiting Checkout"}
ORDER_STATUSES = {"active", "preparing", "ready", "served", "received", "payment_pending", "paid", "cancelled"}
PASSWORD_HISTORY_SIZE = 5
DEFAULT_MENU_OPTIONS = {
    "spiciness": [("ระดับ 0 · ไม่เผ็ด", 0), ("ระดับ 1 · เผ็ดน้อย", 0), ("ระดับ 2 · เผ็ดกลาง", 0), ("ระดับ 3 · เผ็ดมาก", 0), ("ระดับ 4 · เผ็ดพิเศษ", 0)],
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
    return bool(os.environ.get("VERCEL")) and not remote_storage_enabled() and not bool(DATABASE_URL)


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
    return {"users": [], "menu_items": [], "tables": [], "orders": [], "reservations": [], "future_reservations": [], "queue": [], "carts": {}, "notifications": [], "audit": [], "_revision": 0, "settings": {"restaurant_name": "อิ่มอร่อย", "opening_days": "ทุกวัน", "opening_hours": "10:00 - 22:00", "welcome_message": "อร่อยง่าย สั่งได้เลย", "featured_menu_ids": [], "hero_title": "อร่อยง่าย สั่งได้เลย", "announcement": "", "logo_url": "", "hero_image_url": "", "primary_color": "#176b50", "accent_color": "#d9ef93", "page_background": "#f6f5ef", "card_style": "rounded", "show_featured": True}}


def apply_schema_defaults(data: dict[str, Any]) -> bool:
    """Upgrade older JSON data in place while preserving existing restaurant content."""
    changed = False
    for key, default in (("future_reservations", []), ("notifications", []), ("_revision", 0)):
        if not isinstance(data.get(key), type(default)):
            data[key] = default
            changed = True
    for user in data.get("users", []):
        if not isinstance(user.get("password_history"), list):
            user["password_history"] = []
            changed = True
    for item in data.get("menu_items", []):
        if "stock_quantity" in item:
            item.pop("stock_quantity", None)
            changed = True
    for order in data.get("orders", []):
        if "stock_deducted" in order:
            order.pop("stock_deducted", None)
            changed = True
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
            legacy_spice_levels = {"ไม่เผ็ด": "ระดับ 0 · ไม่เผ็ด", "เผ็ดน้อย": "ระดับ 1 · เผ็ดน้อย", "เผ็ดกลาง": "ระดับ 2 · เผ็ดกลาง", "เผ็ดมาก": "ระดับ 3 · เผ็ดมาก"}
            if item.get("spiciness") in legacy_spice_levels:
                item["spiciness"] = legacy_spice_levels[item["spiciness"]]
                changed = True
            for option in item["options"].get("spiciness", []):
                if isinstance(option, dict) and option.get("name") in legacy_spice_levels:
                    option["name"] = legacy_spice_levels[option["name"]]
                    changed = True
    return changed


def load_data() -> dict[str, Any]:
    """Load data safely; initialize from the bundled sample on first run."""
    if DATABASE_URL:
        return _load_postgres_data()
    if bool(UPSTASH_URL) != bool(UPSTASH_TOKEN):
        raise StorageError("ต้องกำหนด Upstash REST URL และ token ให้ครบทั้งคู่")
    if remote_storage_enabled():
        stored = _redis_command(["GET", REDIS_DATA_KEY])
        if stored is None:
            initial = json.loads(SEED_FILE.read_text(encoding="utf-8")) if SEED_FILE.exists() else empty_data()
            apply_schema_defaults(initial)
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
    if DATABASE_URL:
        return _save_postgres_data(data)
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


def _postgres_connect():
    try:
        import psycopg
        return psycopg.connect(DATABASE_URL, connect_timeout=8)
    except Exception as error:
        raise StorageError("เชื่อมต่อ PostgreSQL ไม่ได้ กรุณาตรวจสอบ DATABASE_URL และติดตั้ง psycopg") from error


def _ensure_postgres_table(connection: Any) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS restaurant_state (id SMALLINT PRIMARY KEY CHECK (id = 1), revision BIGINT NOT NULL, payload JSONB NOT NULL)")


def _load_postgres_data() -> dict[str, Any]:
    try:
        with _postgres_connect() as connection:
            _ensure_postgres_table(connection)
            row = connection.execute("SELECT revision, payload FROM restaurant_state WHERE id = 1").fetchone()
            if row is None:
                initial = json.loads(SEED_FILE.read_text(encoding="utf-8")) if SEED_FILE.exists() else empty_data()
                initial["_revision"] = 0
                connection.execute("INSERT INTO restaurant_state (id, revision, payload) VALUES (1, 0, %s::jsonb) ON CONFLICT (id) DO NOTHING", (json.dumps(initial, ensure_ascii=False),))
                row = connection.execute("SELECT revision, payload FROM restaurant_state WHERE id = 1").fetchone()
            revision, payload = row
        data = payload if isinstance(payload, dict) else json.loads(payload)
        data["_revision"] = int(revision)
        if apply_schema_defaults(data):
            _save_postgres_data(data)
        return data
    except StorageError:
        raise
    except Exception as error:
        raise StorageError("อ่านข้อมูล PostgreSQL ไม่สำเร็จ") from error


def _save_postgres_data(data: dict[str, Any]) -> bool:
    revision = int(data.get("_revision", 0))
    payload = dict(data)
    payload.pop("_revision", None)
    try:
        with _postgres_connect() as connection:
            _ensure_postgres_table(connection)
            row = connection.execute("UPDATE restaurant_state SET revision = revision + 1, payload = %s::jsonb WHERE id = 1 AND revision = %s RETURNING revision", (json.dumps(payload, ensure_ascii=False), revision)).fetchone()
            if row is None:
                raise StorageError("มีผู้ใช้อื่นบันทึกข้อมูลพร้อมกัน กรุณาโหลดหน้าใหม่แล้วลองอีกครั้ง")
            data["_revision"] = int(row[0])
        return True
    except StorageError:
        raise
    except Exception as error:
        raise StorageError("บันทึกข้อมูล PostgreSQL ไม่สำเร็จ") from error


def audit(data: dict[str, Any], actor: str, action: str, detail: str) -> None:
    data["audit"].append({"id": str(uuid4()), "at": now_iso(), "actor": actor, "action": action, "detail": detail})
    data["audit"] = data["audit"][-500:]


def notify(data: dict[str, Any], user_id: Any, title: str, message: str, link: str = "") -> None:
    data.setdefault("notifications", []).append({"id": str(uuid4()), "user_id": str(user_id), "title": title, "message": message, "link": link, "created_at": now_iso(), "read_at": None})
    data["notifications"] = data["notifications"][-1000:]


def notify_customer(data: dict[str, Any], order: dict[str, Any], title: str, message: str) -> None:
    if order.get("customer_id"):
        notify(data, order["customer_id"], title, message, f"/my-orders/{order['id']}")


def notifications_for(data: dict[str, Any], user_id: Any) -> list[dict[str, Any]]:
    return sorted((item for item in data.get("notifications", []) if str(item.get("user_id")) == str(user_id)), key=lambda item: item.get("created_at", ""), reverse=True)


def mark_notification_read(data: dict[str, Any], user_id: Any, notification_id: Any) -> None:
    notification = next((item for item in data.get("notifications", []) if str(item.get("id")) == str(notification_id) and str(item.get("user_id")) == str(user_id)), None)
    if not notification:
        raise ValidationError("ไม่พบการแจ้งเตือน")
    notification["read_at"] = now_iso()
    if not save_data(data):
        raise ValidationError("บันทึกสถานะการแจ้งเตือนไม่สำเร็จ")


def mark_all_notifications_read(data: dict[str, Any], user_id: Any) -> None:
    now = now_iso()
    for notification in data.get("notifications", []):
        if str(notification.get("user_id")) == str(user_id) and not notification.get("read_at"):
            notification["read_at"] = now
    if not save_data(data):
        raise ValidationError("บันทึกสถานะการแจ้งเตือนไม่สำเร็จ")


def staff_user_ids(data: dict[str, Any]) -> list[str]:
    return [str(user.get("id")) for user in data.get("users", []) if user.get("role") in {"admin", "staff"}]


def notify_staff(data: dict[str, Any], title: str, message: str, link: str = "/orders") -> None:
    for user_id in staff_user_ids(data):
        notify(data, user_id, title, message, link)


def ensure_demo_users(data: dict[str, Any]) -> bool:
    """Set up local demo accounts; override credentials with environment variables."""
    changed = False
    for username, role, env_name, default_password in (
        (os.environ.get("RMS_ADMIN_USER", "admin"), "admin", "RMS_ADMIN_PASSWORD", "admin1234"),
        (os.environ.get("RMS_STAFF_USER", "staff"), "staff", "RMS_STAFF_PASSWORD", "staff1234"),
    ):
        if not any(u.get("username") == username for u in data["users"]):
            password = _validate_password(os.environ.get(env_name, default_password), "รหัสผ่านบัญชีเริ่มต้น")
            data["users"].append({"id": str(uuid4()), "username": username, "password_hash": generate_password_hash(password), "password_history": [], "security_answer_hash": generate_password_hash("helloworld"), "role": role, "created_at": now_iso()})
            changed = True
        else:
            existing = next(u for u in data["users"] if u.get("username") == username)
            if not isinstance(existing.get("password_history"), list):
                existing["password_history"] = []
                changed = True
            if existing.get("role") == role and not existing.get("security_answer_hash"):
                existing["security_answer_hash"] = generate_password_hash("helloworld")
                changed = True
    if changed:
        if not save_data(data):
            raise StorageError("บันทึกบัญชีเริ่มต้นไม่สำเร็จ")
    return changed


def update_restaurant_name(data: dict[str, Any], name_value: Any, actor: str) -> str:
    return update_restaurant_profile(data, {"restaurant_name": name_value}, actor)["restaurant_name"]


def _logo_data_url(upload: Any) -> str:
    try:
        raw = upload.read(512 * 1024 + 1)
    except (OSError, ValueError) as error:
        raise ValidationError("อ่านไฟล์โลโก้ไม่สำเร็จ กรุณาเลือกไฟล์ใหม่") from error
    if len(raw) > 512 * 1024:
        raise ValidationError("ไฟล์โลโก้มีขนาดใหญ่เกินไป (สูงสุด 512 KB)")
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
    )
    content_type = next((mime for signature, mime in signatures if raw.startswith(signature)), None)
    if not content_type and len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        content_type = "image/webp"
    if not content_type:
        raise ValidationError("ไฟล์โลโก้ต้องเป็น PNG, JPG, GIF หรือ WEBP ที่ถูกต้อง")
    return f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"


def update_restaurant_profile(data: dict[str, Any], form: Any, actor: str, logo_upload: Any = None) -> dict[str, Any]:
    settings = data.setdefault("settings", {})
    name = normalize_text(form.get("restaurant_name", settings.get("restaurant_name", "อิ่มอร่อย")), "ชื่อร้าน", 80)
    opening_days = normalize_text(form.get("opening_days", settings.get("opening_days", "ทุกวัน")), "วันเปิดร้าน", 100)
    opening_hours = normalize_text(form.get("opening_hours", settings.get("opening_hours", "10:00 - 22:00")), "เวลาเปิดร้าน", 100)
    welcome_message = normalize_text(form.get("welcome_message", settings.get("welcome_message", "อร่อยง่าย สั่งได้เลย")), "ข้อความหน้าร้าน", 160)
    hero_title = normalize_text(form.get("hero_title", settings.get("hero_title", "")), "หัวข้อหน้าร้าน", 80, required=False)
    announcement = normalize_text(form.get("announcement", settings.get("announcement", "")), "ข้อความประกาศ", 200, required=False)
    if parse_bool(form.get("remove_logo", "false"), "ลบโลโก้"):
        logo_url = ""
    elif logo_upload is not None and getattr(logo_upload, "filename", ""):
        logo_url = _logo_data_url(logo_upload)
    else:
        stored_logo = settings.get("logo_url", "")
        logo_url = stored_logo if isinstance(stored_logo, str) else ""
    hero_image_url = normalize_text(form.get("hero_image_url", settings.get("hero_image_url", "")), "URL ภาพปก", 500, required=False)
    if logo_url and not logo_url.startswith("data:image/") and len(logo_url) > 500:
        raise ValidationError("ข้อมูลโลโก้ไม่ถูกต้อง")
    for label, image_url in (("โลโก้", logo_url if not logo_url.startswith("data:image/") else ""), ("ภาพปก", hero_image_url)):
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


def normalize_username(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValidationError("ชื่อผู้ใช้ห้ามมีช่องว่างด้านหน้า/ท้าย")
    if not 3 <= len(value) <= 40 or any(char.isspace() for char in value):
        raise ValidationError("ชื่อผู้ใช้ต้องยาว 3–40 ตัว และห้ามมีช่องว่าง")
    allowed_punctuation = {".", "_", "-"}
    for index, char in enumerate(value):
        category = unicodedata.category(char)
        if not (category[0] in {"L", "N"} or category[0] == "M" and index > 0 or char in allowed_punctuation):
            raise ValidationError("ชื่อผู้ใช้ใช้ได้เฉพาะตัวอักษร ตัวเลข จุด ขีดล่าง และขีดกลาง")
    if value[0] in allowed_punctuation or value[-1] in allowed_punctuation:
        raise ValidationError("ชื่อผู้ใช้ต้องเริ่มและจบด้วยตัวอักษรหรือตัวเลข")
    return value


def normalize_login_identity(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or any(char.isspace() for char in value):
        raise ValidationError("ชื่อผู้ใช้หรืออีเมลห้ามมีช่องว่าง")
    if "@" in value:
        identity = value.casefold()
        if len(identity) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", identity):
            raise ValidationError("รูปแบบอีเมลไม่ถูกต้อง")
        return identity
    return normalize_username(value)


def _validate_password(value: Any, label: str = "รหัสผ่าน", minimum: int = 8) -> str:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValidationError(f"{label}ห้ามมีช่องว่าง")
    if not minimum <= len(value) <= 128:
        raise ValidationError(f"{label}ต้องมี {minimum}–128 ตัวอักษร")
    if any(not char.isprintable() for char in value):
        raise ValidationError(f"{label}มีอักขระควบคุมที่ไม่รองรับ")
    return value


def _password_matches_hash(password: str, password_hash: Any) -> bool:
    if not isinstance(password_hash, str) or not password_hash:
        return False
    try:
        return check_password_hash(password_hash, password)
    except (TypeError, ValueError):
        return False


def _replace_password(user: dict[str, Any], new_password: str) -> None:
    history = user.get("password_history", [])
    if not isinstance(history, list):
        history = []
    recent_hashes = [user.get("password_hash"), *history[:PASSWORD_HISTORY_SIZE - 1]]
    if any(_password_matches_hash(new_password, old_hash) for old_hash in recent_hashes):
        raise ValidationError(f"ห้ามใช้รหัสผ่านซ้ำกับ {PASSWORD_HISTORY_SIZE} รหัสล่าสุด")
    old_hash = user.get("password_hash")
    prior_hashes = [old_hash] if isinstance(old_hash, str) and old_hash else []
    prior_hashes.extend(value for value in history if isinstance(value, str) and value not in prior_hashes)
    user["password_history"] = prior_hashes[:PASSWORD_HISTORY_SIZE - 1]
    user["password_hash"] = generate_password_hash(new_password)


def prepare_registration(data: dict[str, Any], username_value: Any, email_value: Any, password_value: Any, confirm_password_value: Any, security_answer_value: Any = None) -> dict[str, str]:
    username = normalize_username(username_value)
    raw_email = normalize_text(email_value, "อีเมล", 254)
    email = raw_email.casefold()
    if raw_email != email_value or any(char.isspace() for char in email) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValidationError("อีเมลต้องไม่มีช่องว่างและอยู่ในรูปแบบที่ถูกต้อง")
    password = _validate_password(password_value)
    confirm_password = _validate_password(confirm_password_value, "ยืนยันรหัสผ่าน")
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
    return {"username": username, "email": email, "password_hash": generate_password_hash(password), "password_history": [], "security_answer_hash": generate_password_hash(security_answer), "created_at": now_iso()}


def complete_registration(data: dict[str, Any], pending: Any) -> dict[str, Any]:
    if not isinstance(pending, dict):
        raise ValidationError("ไม่พบข้อมูลสมัครสมาชิก กรุณาเริ่มใหม่")
    username = normalize_username(pending.get("username"))
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
    user = {"id": str(uuid4()), "username": username, "email": email, "password_hash": password_hash, "password_history": [], "security_answer_hash": security_answer_hash, "role": "customer", "created_at": now_iso()}
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
    username = normalize_login_identity(username_value)
    password = _validate_password(password_value, "รหัสผ่าน", 1)
    user = next((u for u in data["users"] if u.get("username", "").casefold() == username.casefold() or u.get("email", "").casefold() == username.casefold()), None)
    if user and user.get("role") in ROLES and _password_matches_hash(password, user.get("password_hash")):
        return user
    return None


def change_user_password(data: dict[str, Any], user_id: Any, current_password_value: Any, new_password_value: Any, confirm_password_value: Any) -> None:
    user = next((row for row in data.get("users", []) if str(row.get("id")) == str(user_id)), None)
    if not user:
        raise ValidationError("ไม่พบบัญชีผู้ใช้")
    current_password = _validate_password(current_password_value, "รหัสผ่านปัจจุบัน", 1)
    new_password = _validate_password(new_password_value, "รหัสผ่านใหม่")
    confirm_password = _validate_password(confirm_password_value, "ยืนยันรหัสผ่านใหม่")
    if not _password_matches_hash(current_password, user.get("password_hash")):
        raise ValidationError("รหัสผ่านปัจจุบันไม่ถูกต้อง")
    if new_password != confirm_password:
        raise ValidationError("รหัสผ่านใหม่และช่องยืนยันไม่ตรงกัน")
    _replace_password(user, new_password)
    audit(data, user.get("username", "unknown"), "password_change", "เปลี่ยนรหัสผ่าน")
    if not save_data(data):
        raise StorageError("บันทึกการเปลี่ยนรหัสผ่านไม่สำเร็จ กรุณาลองใหม่")


def reset_user_password(data: dict[str, Any], identity_value: Any, answer_value: Any, new_password_value: Any, confirm_password_value: Any) -> None:
    identity = normalize_login_identity(identity_value)
    answer = normalize_text(answer_value, "คำตอบยืนยันตัวตน", 120).casefold()
    new_password = _validate_password(new_password_value, "รหัสผ่านใหม่")
    confirm_password = _validate_password(confirm_password_value, "ยืนยันรหัสผ่านใหม่")
    if new_password != confirm_password:
        raise ValidationError("รหัสผ่านใหม่และช่องยืนยันไม่ตรงกัน")
    user = next((row for row in data.get("users", []) if row.get("username", "").casefold() == identity or row.get("email", "").casefold() == identity), None)
    if not user or not _password_matches_hash(answer, user.get("security_answer_hash")):
        raise ValidationError("ข้อมูลยืนยันตัวตนไม่ถูกต้อง กรุณาตรวจสอบชื่อบัญชีและคำตอบ")
    _replace_password(user, new_password)
    audit(data, user.get("username", "unknown"), "password_reset", "รีเซ็ตรหัสผ่านด้วยคำตอบยืนยันตัวตน")
    if not save_data(data):
        raise StorageError("บันทึกรหัสผ่านใหม่ไม่สำเร็จ กรุณาลองใหม่")


def update_security_answer(data: dict[str, Any], user_id: Any, current_password_value: Any, answer_value: Any) -> None:
    user = next((row for row in data.get("users", []) if str(row.get("id")) == str(user_id)), None)
    if not user:
        raise ValidationError("ไม่พบบัญชีผู้ใช้")
    current_password = normalize_text(current_password_value, "รหัสผ่านปัจจุบัน", 128)
    answer = normalize_text(answer_value, "คำตอบยืนยันตัวตน", 120).casefold()
    if not _password_matches_hash(current_password, user.get("password_hash")):
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
    item = find_by_id(data["menu_items"], item_id) if item_id else None
    name = normalize_text(form.get("name"), "ชื่อเมนู", 80)
    category = normalize_text(form.get("category"), "หมวดหมู่", 40)
    price = parse_float(form.get("price"), "ราคา", 0.01, 100000)
    image_url = normalize_text(form.get("image_url", ""), "URL รูปภาพ", 500, required=False)
    if image_url and not image_url.startswith(("https://", "http://")):
        raise ValidationError("URL รูปภาพต้องขึ้นต้นด้วย http:// หรือ https://")
    available = parse_bool(form.get("available", "false"), "สถานะพร้อมขาย")
    spiciness = normalize_text(form.get("spiciness", "ระดับ 0 · ไม่เผ็ด"), "ระดับความเผ็ด", 20)
    portion = normalize_text(form.get("portion", "ปกติ"), "ขนาด", 20)
    options = {
        "spiciness": parse_option_lines(form.get("spiciness_options", "ระดับ 0 · ไม่เผ็ด|0\nระดับ 1 · เผ็ดน้อย|0\nระดับ 2 · เผ็ดกลาง|0\nระดับ 3 · เผ็ดมาก|0\nระดับ 4 · เผ็ดพิเศษ|0"), "ระดับความเผ็ด"),
        "portion": parse_option_lines(form.get("portion_options", "เล็ก|0\nปกติ|0\nใหญ่|0"), "ขนาด"),
        "addons": parse_option_lines(form.get("addon_options", ""), "ท็อปปิ้ง", required=False),
    }
    addons = [option["name"] for option in options["addons"]]
    if item:
        old_price = item.get("price")
        item.pop("stock_quantity", None)
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


def maximum_table_capacity(data: dict[str, Any]) -> int:
    return max((int(table.get("seats", 0)) for table in data.get("tables", [])), default=0)


def active_customer_reservation(data: dict[str, Any], customer_id: Any) -> dict[str, Any] | None:
    reservations = [row for row in data.get("reservations", []) if str(row.get("customer_id")) == str(customer_id) and row.get("status") in {"seated", "waiting"}]
    reservations.sort(key=lambda row: row.get("created_at", ""), reverse=True)
    return reservations[0] if reservations else None


def reservation_queue_position(data: dict[str, Any], reservation: dict[str, Any] | None) -> int | None:
    if not reservation or reservation.get("status") != "waiting":
        return None
    index = next((index for index, entry in enumerate(data.get("queue", [])) if str(entry.get("reservation_id")) == str(reservation.get("id"))), None)
    return index + 1 if index is not None else None


def assign_waiting_customers(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Assign waiting parties to newly vacant tables without staff approval."""
    assigned = []
    queue = data.setdefault("queue", [])
    tables = sorted((table for table in data.get("tables", []) if table.get("status") == "Vacant" and not any(str(order.get("table_id")) == str(table.get("id")) and order.get("status") not in {"paid", "cancelled"} for order in data.get("orders", [])) and not any(str(reservation.get("table_id")) == str(table.get("id")) and reservation.get("status") == "seated" for reservation in data.get("reservations", []))), key=lambda table: (int(table.get("seats", 0)), int(table.get("number", 0))))
    for table in tables:
        index = next((i for i, entry in enumerate(queue) if (reservation := find_by_id(data.get("reservations", []), entry.get("reservation_id"))) and reservation.get("status") == "waiting" and int(reservation.get("party_size", 0)) <= int(table.get("seats", 0))), None)
        if index is None:
            continue
        entry = queue.pop(index)
        reservation = find_by_id(data.get("reservations", []), entry.get("reservation_id"))
        if not reservation:
            continue
        reservation.update(status="seated", table_id=table["id"], seated_at=now_iso())
        table["status"] = "Occupied"
        assigned.append(reservation)
        audit(data, "system", "reservation_seated", f"จองคิว {reservation.get('username')} ได้โต๊ะ {table.get('number')}")
        notify(data, reservation.get("customer_id"), "ได้โต๊ะแล้ว", f"ถึงคิวของคุณแล้ว เชิญที่โต๊ะ {table.get('number')} ({reservation.get('party_size')} คน)", "/dashboard")
    return assigned


def create_customer_reservation(data: dict[str, Any], customer: dict[str, Any], party_size_value: Any) -> dict[str, Any]:
    existing = active_customer_reservation(data, customer["id"])
    if existing:
        raise ValidationError("คุณมีการจองโต๊ะหรือกำลังรอคิวอยู่แล้ว")
    party_size = parse_int(party_size_value, "จำนวนผู้ใช้บริการ", 1, 500)
    max_capacity = maximum_table_capacity(data)
    if max_capacity == 0:
        raise ValidationError("ร้านยังไม่มีโต๊ะที่เปิดจอง กรุณาติดต่อร้าน")
    if party_size > max_capacity:
        raise ValidationError(f"โต๊ะใหญ่ที่สุดรับได้ {max_capacity} คน กรุณาจองกลุ่มไม่เกิน {max_capacity} คน แล้วแยกจองคนที่เหลือ")
    available = sorted((table for table in data.get("tables", []) if table.get("status") == "Vacant" and int(table.get("seats", 0)) >= party_size), key=lambda table: (int(table.get("seats", 0)), int(table.get("number", 0))))
    reservation = {"id": str(uuid4()), "customer_id": customer["id"], "username": customer["username"], "party_size": party_size, "table_id": None, "status": "waiting", "created_at": now_iso()}
    data.setdefault("reservations", []).append(reservation)
    if available:
        table = available[0]
        table["status"] = "Occupied"
        reservation.update(status="seated", table_id=table["id"], seated_at=now_iso())
        message = f"โต๊ะ {table['number']} ว่างพอดี เชิญนั่งได้เลย"
        audit(data, customer["username"], "reservation_seated", f"จองโต๊ะ {table['number']} สำหรับ {party_size} คน")
    else:
        queue = data.setdefault("queue", [])
        queue.append({"reservation_id": reservation["id"], "customer_id": customer["id"], "party_size": party_size, "created_at": reservation["created_at"]})
        position = len(queue)
        message = f"โต๊ะยังไม่ว่าง เพิ่มเข้าคิวแล้ว ลำดับที่ {position}"
        audit(data, customer["username"], "reservation_queued", f"เข้าคิวโต๊ะสำหรับ {party_size} คน ลำดับ {position}")
    if not save_data(data):
        raise ValidationError("บันทึกการจองไม่สำเร็จ กรุณาลองใหม่")
    return {"reservation": reservation, "message": message}


def cancel_customer_reservation(data: dict[str, Any], customer: dict[str, Any]) -> None:
    reservation = active_customer_reservation(data, customer["id"])
    if not reservation or reservation.get("status") != "waiting":
        raise ValidationError("ยกเลิกได้เฉพาะรายการที่กำลังรอคิว")
    reservation["status"] = "cancelled"
    data["queue"] = [entry for entry in data.get("queue", []) if str(entry.get("reservation_id")) != str(reservation["id"])]
    audit(data, customer["username"], "reservation_cancel", "ยกเลิกรอคิวโต๊ะ")
    if not save_data(data):
        raise ValidationError("ยกเลิกรายการไม่สำเร็จ กรุณาลองใหม่")


def create_future_reservation(data: dict[str, Any], customer: dict[str, Any], party_size_value: Any, starts_at_value: Any) -> dict[str, Any]:
    party_size = parse_int(party_size_value, "จำนวนผู้ใช้บริการ", 1, 500)
    try:
        starts_at = datetime.strptime(normalize_text(starts_at_value, "วันและเวลาจอง", 30), "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo("Asia/Bangkok"))
    except ValueError as error:
        raise ValidationError("กรุณาเลือกวันและเวลาจองให้ถูกต้อง") from error
    now = datetime.now(ZoneInfo("Asia/Bangkok"))
    if starts_at < now + timedelta(minutes=10) or starts_at > now + timedelta(days=90):
        raise ValidationError("จองล่วงหน้าได้ตั้งแต่ 10 นาทีถึง 90 วัน")
    maximum = maximum_table_capacity(data)
    if party_size > maximum:
        raise ValidationError(f"โต๊ะใหญ่ที่สุดรับได้ {maximum} คน กรุณาแยกจองกลุ่มที่เหลือ")
    active = active_future_reservations(data, customer["id"])
    if active:
        raise ValidationError("คุณมีรายการจองล่วงหน้าที่ยังไม่เสร็จอยู่แล้ว")
    capacity = sum(int(table.get("seats", 0)) for table in data.get("tables", []))
    overlap = 0
    for row in data.get("future_reservations", []):
        if row.get("status") not in {"pending", "confirmed"}:
            continue
        try:
            existing_at = datetime.fromisoformat(row["starts_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if abs((existing_at - starts_at).total_seconds()) < 90 * 60:
            overlap += int(row.get("party_size", 0))
    if overlap + party_size > capacity:
        raise ValidationError("ช่วงเวลานี้มีผู้จองเต็มความจุร้านแล้ว กรุณาเลือกเวลาอื่น")
    reservation = {"id": str(uuid4()), "customer_id": customer["id"], "username": customer["username"], "party_size": party_size, "starts_at": starts_at.isoformat(timespec="minutes"), "status": "pending", "created_at": now_iso()}
    data.setdefault("future_reservations", []).append(reservation)
    notify_staff(data, "มีคำขอจองล่วงหน้า", f"{customer['username']} ขอจอง {party_size} คน วันที่ {starts_at.strftime('%d/%m/%Y %H:%M')}", "/future-reservations")
    audit(data, customer["username"], "future_reservation_request", f"จองล่วงหน้า {party_size} คน {starts_at.isoformat(timespec='minutes')}")
    if not save_data(data):
        raise ValidationError("บันทึกการจองไม่สำเร็จ กรุณาลองใหม่")
    return reservation


def active_future_reservations(data: dict[str, Any], customer_id: Any) -> list[dict[str, Any]]:
    cutoff = datetime.now(ZoneInfo("Asia/Bangkok")) - timedelta(minutes=90)
    active = []
    for row in data.get("future_reservations", []):
        if str(row.get("customer_id")) != str(customer_id) or row.get("status") not in {"pending", "confirmed"}:
            continue
        try:
            if datetime.fromisoformat(row["starts_at"]) >= cutoff:
                active.append(row)
        except (KeyError, TypeError, ValueError):
            continue
    active.sort(key=lambda row: row.get("starts_at", ""))
    return active


def update_future_reservation(data: dict[str, Any], reservation_id: Any, status_value: Any, actor: str) -> dict[str, Any]:
    reservation = find_by_id(data.get("future_reservations", []), reservation_id)
    status = normalize_text(status_value, "สถานะจอง", 20)
    if not reservation or reservation.get("status") != "pending" or status not in {"confirmed", "cancelled"}:
        raise ValidationError("เปลี่ยนสถานะการจองนี้ไม่ได้")
    reservation["status"] = status
    reservation["updated_at"] = now_iso()
    notify(data, reservation["customer_id"], "อัปเดตการจองล่วงหน้า", "ร้านยืนยันการจองแล้ว" if status == "confirmed" else "ร้านไม่สามารถรับการจองนี้ได้ กรุณาติดต่อร้าน", "/")
    audit(data, actor, "future_reservation_update", f"{reservation['username']} → {status}")
    if not save_data(data):
        raise ValidationError("บันทึกสถานะการจองไม่สำเร็จ")
    return reservation


def cancel_future_reservation(data: dict[str, Any], reservation_id: Any, customer: dict[str, Any]) -> dict[str, Any]:
    reservation = find_by_id(data.get("future_reservations", []), reservation_id)
    if not reservation or str(reservation.get("customer_id")) != str(customer["id"]) or reservation.get("status") not in {"pending", "confirmed"}:
        raise ValidationError("ยกเลิกการจองนี้ไม่ได้")
    reservation["status"] = "cancelled"
    reservation["updated_at"] = now_iso()
    audit(data, customer["username"], "future_reservation_cancel", f"ยกเลิกการจองล่วงหน้า {reservation.get('starts_at')}")
    if not save_data(data):
        raise ValidationError("ยกเลิกการจองไม่สำเร็จ")
    return reservation


def finish_table_if_clear(data: dict[str, Any], table_id: Any, payment_confirmed: bool = False) -> bool:
    open_orders = [order for order in data.get("orders", []) if str(order.get("table_id")) == str(table_id) and order.get("status") not in {"paid", "cancelled"}]
    if open_orders:
        return False
    seated = [reservation for reservation in data.get("reservations", []) if str(reservation.get("table_id")) == str(table_id) and reservation.get("status") == "seated"]
    if seated and not payment_confirmed:
        return False
    table = find_by_id(data.get("tables", []), table_id)
    if table:
        table["status"] = "Vacant"
    for reservation in data.get("reservations", []):
        if str(reservation.get("table_id")) == str(table_id) and reservation.get("status") == "seated":
            reservation["status"] = "completed"
            reservation["checked_out_at"] = now_iso()
    assign_waiting_customers(data)
    return True


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
    if table:
        seated_parties = [row for row in data.get("reservations", []) if str(row.get("table_id")) == str(table["id"]) and row.get("status") == "seated"]
        if any(int(row.get("party_size", 0)) > seats for row in seated_parties):
            raise ValidationError("จำนวนที่นั่งใหม่น้อยกว่าจำนวนลูกค้าที่กำลังใช้โต๊ะ")
        active_order = any(str(order.get("table_id")) == str(table["id"]) and order.get("status") not in {"paid", "cancelled"} for order in data.get("orders", []))
        if status == "Vacant" and active_order:
            raise ValidationError("ยังตั้งโต๊ะว่างไม่ได้ เพราะมีออเดอร์ที่ยังไม่ชำระหรือยังไม่ได้รับการยืนยัน")
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
    if status == "Vacant":
        for reservation in data.get("reservations", []):
            if str(reservation.get("table_id")) == str(table["id"]) and reservation.get("status") == "seated":
                reservation["status"] = "completed"
                reservation["checked_out_at"] = now_iso()
        assign_waiting_customers(data)
    if not save_data(data):
        raise ValidationError("บันทึกข้อมูลไม่สำเร็จ")
    return table


def delete_table(data: dict[str, Any], table_id: str, actor: str) -> None:
    table = find_by_id(data["tables"], table_id)
    if not table:
        raise ValidationError("ไม่พบโต๊ะที่ต้องการ")
    if any(int(o.get("table_id", -1)) == int(table_id) and o.get("status") not in {"paid", "cancelled"} for o in data["orders"]):
        raise ValidationError("ไม่สามารถลบโต๊ะที่มีออเดอร์ที่ยังไม่ปิดได้")
    if any(str(row.get("table_id")) == str(table_id) and row.get("status") == "seated" for row in data.get("reservations", [])):
        raise ValidationError("ไม่สามารถลบโต๊ะที่มีลูกค้ากำลังนั่งอยู่")
    data["tables"].remove(table)
    audit(data, actor, "table_delete", f"ลบโต๊ะ {table['number']}")
    if not save_data(data):
        raise ValidationError("บันทึกข้อมูลไม่สำเร็จ")


def force_release_table(data: dict[str, Any], table_id: Any, actor: str) -> dict[str, Any]:
    """Admin-only workflow: cancel service at an occupied table and make it available."""
    table = find_by_id(data.get("tables", []), table_id)
    if not table:
        raise ValidationError("ไม่พบโต๊ะที่ต้องการยกเลิก")
    cancelled_orders = []
    for order in data.get("orders", []):
        if str(order.get("table_id")) != str(table["id"]) or order.get("status") in {"paid", "cancelled"}:
            continue
        order.pop("stock_deducted", None)
        order["status"] = "cancelled"
        order["cancelled_at"] = now_iso()
        order["cancelled_by"] = actor
        order["cancellation_reason"] = "ผู้ดูแลยกเลิกการใช้โต๊ะ"
        cancelled_orders.append(order)
        audit(data, actor, "admin_force_table_release", f"ยกเลิกออเดอร์ {order['id']} จากโต๊ะ {table['number']}")
        notify_customer(data, order, "ร้านยกเลิกการใช้โต๊ะ", f"ผู้ดูแลยกเลิกการใช้โต๊ะ {table['number']} และยกเลิกออเดอร์ #{order['id']} กรุณาติดต่อพนักงาน")
    cancelled_reservations = 0
    for reservation in data.get("reservations", []):
        if str(reservation.get("table_id")) == str(table["id"]) and reservation.get("status") == "seated":
            reservation["status"] = "cancelled"
            reservation["cancelled_at"] = now_iso()
            reservation["cancelled_by"] = actor
            cancelled_reservations += 1
            notify(data, reservation.get("customer_id"), "ยกเลิกการใช้โต๊ะ", f"ผู้ดูแลยกเลิกการใช้โต๊ะ {table['number']} กรุณาติดต่อพนักงาน")
    table["status"] = "Vacant"
    audit(data, actor, "admin_force_table_release", f"บังคับยกเลิกการใช้โต๊ะ {table['number']}; ยกเลิก {len(cancelled_orders)} ออเดอร์")
    assign_waiting_customers(data)
    if not save_data(data):
        raise ValidationError("ยกเลิกการใช้โต๊ะไม่สำเร็จ กรุณาลองใหม่")
    return {"table": table, "cancelled_orders": len(cancelled_orders), "cancelled_reservations": cancelled_reservations}


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
    spicy = _selected_option(configured.get("spiciness", []), spicy_value or menu.get("spiciness"), "ระดับความเผ็ด")
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


def prepare_customer_cart_line(data: dict[str, Any], menu_id_value: Any, quantity_value: Any, selections: Any = None) -> dict[str, Any]:
    menu_id = parse_int(menu_id_value, "เมนู", 1, 100000)
    quantity = parse_int(quantity_value, "จำนวน", 1, 50)
    menu = find_by_id(data.get("menu_items", []), menu_id)
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
    spice_options = configured.get("spiciness", [])
    portion_options = configured.get("portion", [])
    if not spice_options or not portion_options:
        raise ValidationError("ตัวเลือกเมนูยังไม่สมบูรณ์ กรุณาแจ้งร้าน")
    spicy = _selected_option(spice_options, spicy_value or spice_options[0]["name"], "ระดับความเผ็ด")
    portion = _selected_option(portion_options, portion_value or portion_options[0]["name"], "ขนาด")
    if len(addons_value) > 10 or len(set(addons_value)) != len(addons_value):
        raise ValidationError("เลือกท็อปปิ้งซ้ำหรือมากเกินไป")
    addons = [_selected_option(configured.get("addons", []), value, "ท็อปปิ้ง") for value in addons_value]
    base_price = parse_float(menu.get("price"), "ราคาเมนู", 0, 100000)
    unit_price = round(base_price + float(spicy.get("price", 0)) + float(portion.get("price", 0)) + sum(float(option.get("price", 0)) for option in addons), 2)
    return {"line_id": str(uuid4()), "menu_id": menu_id, "name": menu["name"], "qty": quantity, "base_price": base_price, "unit_price": unit_price, "options": {"spiciness": spicy["name"], "portion": portion["name"], "addons": [option["name"] for option in addons]}}


def customer_cart(data: dict[str, Any], customer_id: Any) -> list[dict[str, Any]]:
    carts = data.setdefault("carts", {})
    value = carts.get(str(customer_id), [])
    return value if isinstance(value, list) else []


def add_customer_cart_item(data: dict[str, Any], customer: dict[str, Any], menu_id_value: Any, quantity_value: Any, selections: Any = None) -> list[dict[str, Any]]:
    line = prepare_customer_cart_line(data, menu_id_value, quantity_value, selections)
    cart = customer_cart(data, customer["id"])
    existing = next((item for item in cart if int(item.get("menu_id", -1)) == line["menu_id"] and item.get("options", {}) == line["options"]), None)
    if existing:
        if int(existing.get("qty", 0)) + line["qty"] > 50:
            raise ValidationError("สั่งเมนูเดียวกันได้ไม่เกิน 50 จานต่อรายการ")
        existing["qty"] += line["qty"]
    else:
        if len(cart) >= 50:
            raise ValidationError("ตะกร้ามีได้ไม่เกิน 50 รายการ")
        cart.append(line)
    data.setdefault("carts", {})[str(customer["id"])] = cart
    if not save_data(data):
        raise ValidationError("บันทึกตะกร้าไม่สำเร็จ กรุณาลองใหม่")
    return cart


def update_customer_cart_item(data: dict[str, Any], customer: dict[str, Any], line_id: Any, quantity_value: Any) -> list[dict[str, Any]]:
    quantity = parse_int(quantity_value, "จำนวน", 0, 50)
    cart = customer_cart(data, customer["id"])
    line = next((item for item in cart if str(item.get("line_id")) == str(line_id)), None)
    if not line:
        raise ValidationError("ไม่พบรายการในตะกร้า")
    if quantity == 0:
        cart.remove(line)
    else:
        line["qty"] = quantity
    data.setdefault("carts", {})[str(customer["id"])] = cart
    if not save_data(data):
        raise ValidationError("บันทึกตะกร้าไม่สำเร็จ กรุณาลองใหม่")
    return cart


def create_customer_table_order(data: dict[str, Any], customer: dict[str, Any], reservation: dict[str, Any] | None) -> dict[str, Any]:
    if not reservation or reservation.get("status") != "seated" or str(reservation.get("customer_id")) != str(customer["id"]):
        raise ValidationError("ต้องได้โต๊ะก่อนจึงจะสั่งอาหารได้")
    table = find_by_id(data.get("tables", []), reservation.get("table_id"))
    if not table or table.get("status") not in {"Occupied", "Awaiting Checkout"}:
        raise ValidationError("โต๊ะของคุณไม่พร้อมสั่งอาหาร กรุณาจองโต๊ะใหม่")
    if int(reservation.get("party_size", 0)) > int(table.get("seats", 0)):
        raise ValidationError("จำนวนคนเกินที่นั่งของโต๊ะ กรุณาติดต่อร้าน")
    cart = customer_cart(data, customer["id"])
    if not cart:
        raise ValidationError("ยังไม่มีเมนูในตะกร้า")
    items = []
    for entry in cart:
        options = entry.get("options", {})
        selections = {"spiciness": options.get("spiciness"), "portion": options.get("portion"), "addons": options.get("addons", [])}
        line = prepare_customer_cart_line(data, entry.get("menu_id"), entry.get("qty"), selections)
        line["line_id"] = str(uuid4())
        items.append(line)
    order = {"id": str(uuid4())[:8].upper(), "table_id": table["id"], "customer_id": customer["id"], "customer_name": customer["username"], "party_size": reservation["party_size"], "order_type": "dine_in", "items": items, "status": "active", "created_at": now_iso(), "updated_at": now_iso()}
    data.setdefault("orders", []).append(order)
    data.setdefault("carts", {}).pop(str(customer["id"]), None)
    table["status"] = "Occupied"
    reservation["order_id"] = order["id"]
    audit(data, customer["username"], "dine_in_order_create", f"ยืนยันออเดอร์ {order['id']} โต๊ะ {table['number']} รวม {len(items)} รายการ")
    notify_staff(data, "ออเดอร์ใหม่", f"ลูกค้าส่งออเดอร์ #{order['id']} โต๊ะ {table['number']}", "/orders")
    if not save_data(data):
        raise ValidationError("ส่งออเดอร์ไม่สำเร็จ กรุณาลองใหม่")
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
        finish_table_if_clear(data, order.get("table_id"))
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
    order.pop("stock_deducted", None)
    order["status"] = "cancelled"
    finish_table_if_clear(data, order.get("table_id"))
    audit(data, actor, "order_cancel", f"ยกเลิกออเดอร์ {order_id}")
    notify_customer(data, order, "ออเดอร์ถูกยกเลิก", f"ออเดอร์ #{order_id} ถูกยกเลิกโดยร้าน")
    if not save_data(data):
        raise ValidationError("บันทึกการยกเลิกออเดอร์ไม่สำเร็จ กรุณาลองใหม่")


def update_order_status(data: dict[str, Any], order_id: str, status_value: Any, actor: str) -> dict[str, Any]:
    status = normalize_text(status_value, "สถานะออเดอร์", 20)
    order = find_by_id(data["orders"], order_id)
    transitions = {"active": "preparing", "preparing": "ready", "ready": "served"}
    if not order or transitions.get(order.get("status")) != status or not order.get("items"):
        raise ValidationError("เปลี่ยนสถานะออเดอร์ไม่ได้")
    order["status"] = status
    order["updated_at"] = now_iso()
    if status in {"ready", "served"} and order["items"]:
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
    status_message = {"preparing": "ร้านเริ่มเตรียมอาหารแล้ว", "ready": "ครัวทำอาหารเสร็จแล้ว กำลังจัดเสิร์ฟ", "served": "พนักงานเสิร์ฟอาหารถึงโต๊ะแล้ว"}[status]
    notify_customer(data, order, "อัปเดตสถานะออเดอร์", f"ออเดอร์ #{order_id}: {status_message}")
    if not save_data(data):
        raise ValidationError("บันทึกสถานะออเดอร์ไม่สำเร็จ กรุณาลองใหม่")
    return order


def confirm_customer_order_received(data: dict[str, Any], order_id: str, customer: dict[str, Any]) -> dict[str, Any]:
    """Record the customer's confirmation after staff marks the order served."""
    order = find_by_id(data.get("orders", []), order_id)
    if not order or str(order.get("customer_id")) != str(customer.get("id")):
        raise ValidationError("ไม่พบออเดอร์ของบัญชีนี้")
    if order.get("order_type") != "dine_in" or order.get("status") != "served":
        raise ValidationError("ยืนยันรับอาหารได้หลังพนักงานกดเสิร์ฟถึงโต๊ะแล้วเท่านั้น")
    order["status"] = "received"
    order["customer_received_at"] = now_iso()
    audit(data, customer.get("username", "ลูกค้า"), "customer_received_order", f"ยืนยันรับอาหารออเดอร์ {order_id}")
    notify_staff(data, "ลูกค้ายืนยันรับอาหารแล้ว", f"ออเดอร์ #{order_id} โต๊ะ {order.get('table_id')} ลูกค้ายืนยันว่าได้รับอาหารครบแล้ว", f"/orders/{order_id}")
    if not save_data(data):
        raise ValidationError("บันทึกการยืนยันรับอาหารไม่สำเร็จ กรุณาลองใหม่")
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
    if order.get("order_type") == "dine_in" and order.get("status") != "payment_pending":
        raise ValidationError("รอลูกค้ากดขอชำระเงินก่อน แล้วจึงยืนยันรับชำระและปล่อยโต๊ะได้")
    bill = calculate_bill(order, form.get("discount", 0), form.get("vat_rate", 7), form.get("service_rate", 0))
    order["bill"] = bill
    order["status"] = "paid"
    order["paid_at"] = now_iso()
    order["payment_confirmed_by"] = actor
    table = find_by_id(data["tables"], order["table_id"])
    if not finish_table_if_clear(data, order.get("table_id"), payment_confirmed=True) and table:
        table["status"] = "Awaiting Checkout"
    audit(data, actor, "checkout", f"ชำระออเดอร์ {order_id}: {bill['total']:.2f} บาท")
    notify_customer(data, order, "ร้านยืนยันการชำระเงินแล้ว", f"ออเดอร์ #{order_id} ชำระเงินเรียบร้อย ยอด {bill['total']:.2f} บาท")
    if not save_data(data):
        raise ValidationError("บันทึกการชำระเงินไม่สำเร็จ กรุณาลองใหม่")
    return bill


def customer_table_checkout_preview(data: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
    reservation = active_customer_reservation(data, customer["id"])
    if not reservation or reservation.get("status") != "seated":
        raise ValidationError("ยังไม่มีโต๊ะที่สามารถ checkout ได้")
    table = find_by_id(data.get("tables", []), reservation.get("table_id"))
    if not table:
        raise ValidationError("ไม่พบโต๊ะที่กำลังใช้งาน")
    orders = [order for order in data.get("orders", []) if str(order.get("customer_id")) == str(customer["id"]) and str(order.get("table_id")) == str(table["id"]) and order.get("order_type") == "dine_in" and order.get("status") not in {"paid", "cancelled"}]
    if any(order.get("status") == "served" for order in orders):
        raise ValidationError("พนักงานเสิร์ฟแล้ว กรุณาไปที่ประวัติออเดอร์ เปิดรายการ แล้วกดยืนยันว่าได้รับอาหารก่อนขอชำระเงิน")
    if any(order.get("status") not in {"received", "payment_pending"} for order in orders):
        raise ValidationError("รอให้พนักงานเสิร์ฟและลูกค้ายืนยันรับอาหารก่อน แล้วจึงขอชำระเงินได้")
    if not orders:
        raise ValidationError("ยังไม่มีรายการชำระเงิน กรุณาแจ้งพนักงานให้ตรวจสอบและยืนยันปล่อยโต๊ะ")
    bills = [{"order": order, "bill": calculate_bill(order)} for order in orders]
    return {"reservation": reservation, "table": table, "bills": bills, "total": round(sum(row["bill"]["total"] for row in bills), 2)}


def customer_table_checkout(data: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
    preview = customer_table_checkout_preview(data, customer)
    pending = [row for row in preview["bills"] if row["order"].get("status") == "payment_pending"]
    ready = [row for row in preview["bills"] if row["order"].get("status") == "received"]
    if pending and not ready:
        raise ValidationError("ส่งคำขอชำระเงินแล้ว กรุณารอร้านตรวจสอบ")
    for row in ready:
        order = row["order"]
        order["bill"] = row["bill"]
        order["status"] = "payment_pending"
        order["payment_requested_at"] = now_iso()
        order["payment_method"] = "customer_requested_at_restaurant"
        audit(data, customer["username"], "payment_request", f"ขอชำระออเดอร์ {order['id']}: {row['bill']['total']:.2f} บาท")
        notify_staff(data, "ลูกค้าขอชำระเงิน", f"ออเดอร์ #{order['id']} โต๊ะ {preview['table']['number']} ยอด {row['bill']['total']:.2f} บาท", f"/orders/{order['id']}")
        notify(data, customer["id"], "ส่งคำขอชำระเงินแล้ว", f"รอร้านยืนยันการชำระเงิน ออเดอร์ #{order['id']}", f"/my-orders/{order['id']}")
    preview["table"]["status"] = "Awaiting Checkout"
    if not save_data(data):
        raise ValidationError("checkout โต๊ะไม่สำเร็จ กรุณาลองใหม่")
    preview["payment_requested"] = bool(ready)
    preview["request_total"] = round(sum(row["bill"]["total"] for row in ready), 2)
    return preview


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
