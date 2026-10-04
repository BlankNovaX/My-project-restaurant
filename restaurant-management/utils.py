"""Business rules and JSON persistence for the restaurant application."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
SEED_FILE = BASE_DIR / "data.json"
DATA_FILE = Path(os.environ.get("RMS_DATA_FILE", "/tmp/restaurant-management-data.json" if os.environ.get("VERCEL") else str(BASE_DIR / "data.json")))
ROLES = {"admin", "staff", "customer"}
TABLE_STATUSES = {"Vacant", "Occupied", "Awaiting Checkout"}
ORDER_STATUSES = {"active", "preparing", "ready", "paid", "cancelled"}


class ValidationError(ValueError):
    """An expected, user-correctable input error."""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def empty_data() -> dict[str, Any]:
    return {"users": [], "menu_items": [], "tables": [], "orders": [], "reservations": [], "queue": [], "audit": []}


def load_data() -> dict[str, Any]:
    """Load data safely; initialize from the bundled sample on first run."""
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
        return data
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        # Keep the app usable if the data file is damaged; never expose internals.
        return empty_data()


def save_data(data: dict[str, Any]) -> bool:
    """Write JSON atomically. Returns False rather than leaking file errors."""
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
            data["users"].append({"id": str(uuid4()), "username": username, "password_hash": generate_password_hash(os.environ.get(env_name, default_password)), "role": role, "created_at": now_iso()})
            changed = True
    if changed:
        save_data(data)
    return changed


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


def register_user(data: dict[str, Any], username_value: Any, password_value: Any) -> dict[str, Any]:
    username = normalize_text(username_value, "ชื่อผู้ใช้", 40)
    password = normalize_text(password_value, "รหัสผ่าน", 128)
    if len(username) < 3 or len(password) < 8:
        raise ValidationError("ชื่อผู้ใช้ต้องยาวอย่างน้อย 3 ตัว และรหัสผ่านอย่างน้อย 8 ตัว")
    if any(user.get("username", "").casefold() == username.casefold() for user in data["users"]):
        raise ValidationError("ชื่อผู้ใช้นี้ถูกใช้แล้ว")
    user = {"id": str(uuid4()), "username": username, "password_hash": generate_password_hash(password), "role": "customer", "created_at": now_iso()}
    data["users"].append(user)
    audit(data, username, "register", "สมัครสมาชิก")
    if not save_data(data):
        raise ValidationError("บันทึกข้อมูลไม่สำเร็จ กรุณาลองใหม่")
    return user


def authenticate(data: dict[str, Any], username_value: Any, password_value: Any) -> dict[str, Any] | None:
    username = normalize_text(username_value, "ชื่อผู้ใช้", 40)
    password = normalize_text(password_value, "รหัสผ่าน", 128)
    user = next((u for u in data["users"] if u.get("username", "").casefold() == username.casefold()), None)
    if user and user.get("role") in ROLES and check_password_hash(user.get("password_hash", ""), password):
        return user
    return None


def save_menu_item(data: dict[str, Any], form: Any, actor: str, item_id: str | None = None) -> dict[str, Any]:
    name = normalize_text(form.get("name"), "ชื่อเมนู", 80)
    category = normalize_text(form.get("category"), "หมวดหมู่", 40)
    price = parse_float(form.get("price"), "ราคา", 0.01, 100000)
    image_url = normalize_text(form.get("image_url", ""), "URL รูปภาพ", 500, required=False)
    if image_url and not image_url.startswith(("https://", "http://")):
        raise ValidationError("URL รูปภาพต้องขึ้นต้นด้วย http:// หรือ https://")
    available = parse_bool(form.get("available", "false"), "สถานะพร้อมขาย")
    spiciness = normalize_text(form.get("spiciness", "ไม่เผ็ด"), "ระดับความเผ็ด", 20)
    portion = normalize_text(form.get("portion", "ปกติ"), "ขนาด", 20)
    addons = [normalize_text(v, "ตัวเลือกเพิ่มเติม", 40) for v in form.getlist("addons") if v.strip()][:10]
    item = find_by_id(data["menu_items"], item_id) if item_id else None
    if item:
        old_price = item.get("price")
        item.update(name=name, category=category, price=price, image_url=image_url, available=available, spiciness=spiciness, portion=portion, addons=addons)
        if old_price != price:
            audit(data, actor, "price_change", f"เปลี่ยนราคา {name}: {old_price} → {price:.2f}")
        else:
            audit(data, actor, "menu_update", f"แก้ไขเมนู {name}")
    else:
        item = {"id": max([int(r.get("id", 0)) for r in data["menu_items"]] + [0]) + 1, "name": name, "category": category, "price": price, "image_url": image_url, "available": available, "spiciness": spiciness, "portion": portion, "addons": addons, "created_at": now_iso()}
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
    save_data(data)
    return order


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
    save_data(data)
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
    save_data(data)


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
    save_data(data)
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
    save_data(data)
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
    save_data(data)
    return order
