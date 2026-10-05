"""Flask routes and web application factory for Restaurant Management System."""
from __future__ import annotations

import os
import io
import json
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for

import utils


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "development-only-change-this-secret"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
    )

    @app.before_request
    def prepare_request():
        g.data = utils.load_data()
        utils.ensure_demo_users(g.data)
        if "csrf_token" not in session:
            from secrets import token_urlsafe
            session["csrf_token"] = token_urlsafe(32)
        if request.method == "POST" and request.form.get("csrf_token") != session.get("csrf_token"):
            abort(400)

    @app.context_processor
    def shared_template_values():
        settings = getattr(g, "data", {}).get("settings", {})
        primary = settings.get("primary_color", "#176b50")
        user = current_user()
        reservation = utils.active_customer_reservation(g.data, user["id"]) if user and user.get("role") == "customer" else None
        table = utils.find_by_id(g.data.get("tables", []), reservation.get("table_id")) if reservation and reservation.get("status") == "seated" else None
        queue_position = utils.reservation_queue_position(g.data, reservation)
        cart_count = sum(int(line.get("qty", 0)) for line in utils.customer_cart(g.data, user["id"])) if user and user.get("role") == "customer" else 0
        table_label = f"โต๊ะ {table['number']} · {reservation['party_size']} คน" if table else (f"รอคิว #{queue_position} · {reservation['party_size']} คน" if reservation else "")
        notifications = utils.notifications_for(g.data, user["id"]) if user else []
        unread_notifications = sum(1 for item in notifications if not item.get("read_at"))
        future_reservations = utils.active_future_reservations(g.data, user["id"]) if user else []
        return {"current_user": user, "csrf_token": session.get("csrf_token", ""), "currency": "฿", "restaurant_name": settings.get("restaurant_name", "อิ่มอร่อย"), "store_settings": settings, "primary_text_color": utils.contrasting_text_color(primary), "storage_warning": utils.uses_ephemeral_storage(), "customer_reservation": reservation, "customer_table": table, "customer_queue_position": queue_position, "customer_cart_count": cart_count, "customer_table_label": table_label, "unread_notifications": unread_notifications, "customer_future_reservations": future_reservations}

    def current_user():
        identity = session.get("identity")
        if isinstance(identity, dict) and identity.get("role") in utils.ROLES and identity.get("id"):
            return identity
        user_id = session.get("user_id")
        if not user_id:
            return None
        stored = next((u for u in getattr(g, "data", {}).get("users", []) if str(u.get("id")) == str(user_id)), None)
        if stored:
            return {"id": stored["id"], "username": stored["username"], "role": stored["role"]}
        return None

    def roles_required(*roles):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                user = current_user()
                if not user:
                    flash("กรุณาเข้าสู่ระบบก่อน", "warning")
                    return redirect(url_for("login", next=request.path))
                if user.get("role") not in roles:
                    abort(403)
                return view(*args, **kwargs)
            return wrapped
        return decorator

    def handle_validation(error: Exception):
        flash(str(error), "error")
        return None

    @app.get("/")
    def index():
        if current_user():
            return redirect(url_for("dashboard"))
        return render_template("landing.html", menu=g.data["menu_items"])

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            try:
                pending = utils.prepare_registration(
                    g.data,
                    request.form.get("username"),
                    request.form.get("email"),
                    request.form.get("password"),
                    request.form.get("confirm_password"),
                    request.form.get("security_answer"),
                )
                session["pending_registration"] = pending
                flash("ตรวจสอบชื่อบัญชี แล้วกดยืนยันเพื่อสร้างบัญชี", "success")
                return redirect(url_for("register_confirm"))
            except utils.ValidationError as error:
                handle_validation(error)
        return render_template("auth.html", mode="register")

    @app.route("/register/confirm", methods=["GET", "POST"])
    def register_confirm():
        pending = session.get("pending_registration")
        if not isinstance(pending, dict):
            flash("ไม่พบข้อมูลสมัครสมาชิก กรุณากรอกข้อมูลอีกครั้ง", "warning")
            return redirect(url_for("register"))
        try:
            created_at = datetime.fromisoformat(pending.get("created_at", ""))
            expired = (datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)).total_seconds() > 20 * 60
        except (TypeError, ValueError):
            expired = True
        if expired:
            session.pop("pending_registration", None)
            flash("ข้อมูลยืนยันหมดอายุ กรุณาสมัครใหม่", "warning")
            return redirect(url_for("register"))
        if request.method == "POST":
            try:
                user = utils.complete_registration(g.data, pending)
                session.clear()
                session["identity"] = {"id": user["id"], "username": user["username"], "role": user["role"]}
                flash("ยืนยันและสมัครสมาชิกเรียบร้อยแล้ว", "success")
                return redirect(url_for("dashboard"))
            except utils.ValidationError as error:
                session.pop("pending_registration", None)
                handle_validation(error)
                return redirect(url_for("register"))
        return render_template("register_confirm.html", pending=pending)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            try:
                user = utils.authenticate(g.data, request.form.get("username"), request.form.get("password"))
                if not user:
                    flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")
                else:
                    session.clear()
                    session["identity"] = {"id": user["id"], "username": user["username"], "role": user["role"]}
                    flash(f"ยินดีต้อนรับ {user['username']}", "success")
                    next_path = request.args.get("next", "")
                    if not next_path.startswith("/") or next_path.startswith("//"):
                        next_path = url_for("dashboard")
                    return redirect(next_path)
            except utils.ValidationError as error:
                handle_validation(error)
        return render_template("auth.html", mode="login")

    @app.route("/password/forgot", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            try:
                utils.reset_user_password(
                    g.data,
                    request.form.get("identity"),
                    request.form.get("security_answer"),
                    request.form.get("new_password"),
                    request.form.get("confirm_password"),
                )
                flash("เปลี่ยนรหัสผ่านเรียบร้อยแล้ว เข้าสู่ระบบด้วยรหัสผ่านใหม่ได้เลย", "success")
                return redirect(url_for("login"))
            except (utils.ValidationError, utils.StorageError) as error:
                handle_validation(error)
        return render_template("password_forgot.html")

    @app.route("/account/password", methods=["GET", "POST"])
    @roles_required("admin", "staff", "customer")
    def change_password():
        if request.method == "POST":
            try:
                utils.change_user_password(
                    g.data,
                    current_user()["id"],
                    request.form.get("current_password"),
                    request.form.get("new_password"),
                    request.form.get("confirm_password"),
                )
                flash("เปลี่ยนรหัสผ่านเรียบร้อยแล้ว", "success")
                return redirect(url_for("dashboard"))
            except (utils.ValidationError, utils.StorageError) as error:
                handle_validation(error)
        return render_template("password_change.html")

    @app.route("/account/recovery-answer", methods=["GET", "POST"])
    @roles_required("admin", "staff", "customer")
    def change_recovery_answer():
        if request.method == "POST":
            try:
                utils.update_security_answer(
                    g.data,
                    current_user()["id"],
                    request.form.get("current_password"),
                    request.form.get("security_answer"),
                )
                flash("บันทึกคำตอบยืนยันตัวตนแล้ว", "success")
                return redirect(url_for("dashboard"))
            except (utils.ValidationError, utils.StorageError) as error:
                handle_validation(error)
        return render_template("recovery_answer.html")

    @app.post("/logout")
    @roles_required("admin", "staff", "customer")
    def logout():
        session.clear()
        flash("ออกจากระบบแล้ว", "success")
        return redirect(url_for("index"))

    @app.get("/dashboard")
    @roles_required("admin", "staff", "customer")
    def dashboard():
        user = current_user()
        if user["role"] == "customer":
            listing = utils.list_records(g.data["menu_items"], request.args.get("q", ""), request.args.get("category", ""), request.args.get("sort", "name"), request.args.get("direction", "asc"), request.args.get("page", 1))
            settings = g.data.get("settings", {})
            featured_ids = settings.get("featured_menu_ids", [])
            featured = [item for item in g.data["menu_items"] if str(item.get("id")) in {str(item_id) for item_id in featured_ids}]
            if not featured:
                featured = [item for item in g.data["menu_items"] if item.get("available")][:3]
            return render_template("customer.html", listing=listing, categories=sorted({i["category"] for i in g.data["menu_items"]}), settings=settings, featured=featured, max_table_capacity=utils.maximum_table_capacity(g.data))
        report = utils.daily_report(g.data)
        active_orders = [o for o in g.data["orders"] if o.get("status") not in {"paid", "cancelled"}]
        active_orders.sort(key=lambda o: o.get("created_at", ""))
        return render_template("dashboard.html", report=report, active_orders=active_orders, tables=g.data["tables"])

    @app.get("/menu")
    @roles_required("admin", "staff", "customer")
    def menu_list():
        user = current_user()
        listing = utils.list_records(g.data["menu_items"], request.args.get("q", ""), request.args.get("category", ""), request.args.get("sort", "name"), request.args.get("direction", "asc"), request.args.get("page", 1))
        if user["role"] == "customer":
            settings = g.data.get("settings", {})
            featured_ids = settings.get("featured_menu_ids", [])
            featured = [item for item in g.data["menu_items"] if str(item.get("id")) in {str(item_id) for item_id in featured_ids}]
            if not featured:
                featured = [item for item in g.data["menu_items"] if item.get("available")][:3]
            return render_template("customer.html", listing=listing, categories=sorted({i["category"] for i in g.data["menu_items"]}), settings=settings, featured=featured, max_table_capacity=utils.maximum_table_capacity(g.data))
        return render_template("menu.html", listing=listing, categories=sorted({i["category"] for i in g.data["menu_items"]}), can_edit=user["role"] in {"admin", "staff"})

    @app.route("/menu/new", methods=["GET", "POST"])
    @roles_required("admin", "staff")
    def menu_new():
        if request.method == "POST":
            try:
                utils.save_menu_item(g.data, request.form, current_user()["username"])
                flash("เพิ่มเมนูแล้ว", "success")
                return redirect(url_for("menu_list"))
            except utils.ValidationError as error:
                handle_validation(error)
        return render_template("menu_form.html", item=None)

    @app.route("/menu/<int:item_id>/edit", methods=["GET", "POST"])
    @roles_required("admin", "staff")
    def menu_edit(item_id):
        item = utils.find_by_id(g.data["menu_items"], item_id)
        if not item:
            abort(404)
        if request.method == "POST":
            try:
                utils.save_menu_item(g.data, request.form, current_user()["username"], str(item_id))
                flash("บันทึกเมนูแล้ว", "success")
                return redirect(url_for("menu_list"))
            except utils.ValidationError as error:
                handle_validation(error)
        return render_template("menu_form.html", item=item)

    @app.post("/menu/<int:item_id>/delete")
    @roles_required("admin")
    def menu_delete(item_id):
        try:
            utils.delete_menu_item(g.data, str(item_id), current_user()["username"])
            flash("ลบเมนูแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("menu_list"))

    @app.post("/menu/<int:item_id>/toggle")
    @roles_required("admin", "staff")
    def menu_toggle(item_id):
        item = utils.find_by_id(g.data["menu_items"], item_id)
        if not item:
            abort(404)
        if not item.get("available") and int(item.get("stock_quantity", 999999)) <= 0:
            flash("สต็อกเป็นศูนย์ กรุณาปรับจำนวนคงเหลือก่อนเปิดขาย", "warning")
            return redirect(url_for("menu_list"))
        item["available"] = not bool(item.get("available"))
        utils.audit(g.data, current_user()["username"], "stock_toggle", f"{item['name']}: {'พร้อมขาย' if item['available'] else 'หมด'}")
        if not utils.save_data(g.data):
            flash("บันทึกสถานะไม่สำเร็จ", "error")
        else:
            flash("อัปเดตสถานะสินค้าแล้ว", "success")
        return redirect(url_for("menu_list"))

    @app.route("/tables", methods=["GET", "POST"])
    @roles_required("admin", "staff")
    def tables():
        if request.method == "POST":
            try:
                utils.save_table(g.data, request.form, current_user()["username"])
                flash("เพิ่มโต๊ะแล้ว", "success")
                return redirect(url_for("tables"))
            except utils.ValidationError as error:
                handle_validation(error)
        return render_template("tables.html", tables=sorted(g.data["tables"], key=lambda t: t["number"], reverse=request.args.get("direction") == "desc"), orders=g.data["orders"], reservations=sorted(g.data.get("reservations", []), key=lambda r: r.get("created_at", ""), reverse=True), queue=g.data.get("queue", []), future_reservations=sorted(g.data.get("future_reservations", []), key=lambda r: r.get("starts_at", "")))

    @app.post("/tables/<int:table_id>/edit")
    @roles_required("admin", "staff")
    def table_edit(table_id):
        try:
            utils.save_table(g.data, request.form, current_user()["username"], str(table_id))
            flash("บันทึกโต๊ะแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("tables"))

    @app.post("/tables/<int:table_id>/delete")
    @roles_required("admin")
    def table_delete(table_id):
        try:
            utils.delete_table(g.data, str(table_id), current_user()["username"])
            flash("ลบโต๊ะแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("tables"))

    @app.post("/reservations/future/<reservation_id>")
    @roles_required("admin", "staff")
    def future_reservation_update(reservation_id):
        try:
            utils.update_future_reservation(g.data, reservation_id, request.form.get("status"), current_user()["username"])
            flash("อัปเดตการจองล่วงหน้าแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("future_reservations"))

    @app.get("/future-reservations")
    @roles_required("admin", "staff")
    def future_reservations():
        bookings = sorted(g.data.get("future_reservations", []), key=lambda row: row.get("starts_at", ""))
        return render_template("future_reservations.html", reservations=bookings)

    @app.get("/orders")
    @roles_required("admin", "staff")
    def orders():
        listing = utils.list_records(g.data["orders"], request.args.get("q", ""), request.args.get("status", ""), request.args.get("sort", "created_at"), request.args.get("direction", "desc"), request.args.get("page", 1), filter_field="status")
        return render_template("orders.html", listing=listing, tables=g.data["tables"], order_total=utils.order_total)

    @app.post("/orders/new")
    @roles_required("admin", "staff")
    def order_new():
        try:
            order = utils.create_order(g.data, request.form.get("table_id"), current_user()["username"])
            return redirect(url_for("order_detail", order_id=order["id"]))
        except utils.ValidationError as error:
            handle_validation(error)
            return redirect(url_for("tables"))

    @app.get("/orders/<order_id>")
    @roles_required("admin", "staff")
    def order_detail(order_id):
        order = utils.find_by_id(g.data["orders"], order_id)
        if not order:
            abort(404)
        return render_template("order_detail.html", order=order, menu=g.data["menu_items"], table=utils.find_by_id(g.data["tables"], order["table_id"]), tables=g.data["tables"], order_total=utils.order_total)

    @app.post("/orders/<order_id>/items")
    @roles_required("admin", "staff")
    def order_item(order_id):
        try:
            utils.change_order_item(g.data, order_id, request.form.get("menu_id"), request.form.get("quantity"), current_user()["username"])
            flash("ปรับรายการอาหารแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("order_detail", order_id=order_id))

    @app.post("/orders/<order_id>/cancel")
    @roles_required("admin", "staff")
    def order_cancel(order_id):
        try:
            utils.cancel_order(g.data, order_id, current_user()["username"])
            flash("ยกเลิกออเดอร์แล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("orders"))

    @app.post("/orders/<order_id>/move")
    @roles_required("admin", "staff")
    def order_move(order_id):
        try:
            utils.move_table_order(g.data, order_id, request.form.get("table_id"), current_user()["username"])
            flash("ย้ายโต๊ะแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("order_detail", order_id=order_id))

    @app.get("/kitchen")
    @roles_required("admin", "staff")
    def kitchen():
        active = [o for o in g.data["orders"] if o.get("status") in {"active", "preparing", "ready"}]
        active.sort(key=lambda o: o.get("created_at", ""))
        return render_template("kitchen.html", orders=active, tables=g.data["tables"])

    @app.post("/kitchen/<order_id>/status")
    @roles_required("admin", "staff")
    def kitchen_status(order_id):
        try:
            utils.update_order_status(g.data, order_id, request.form.get("status"), current_user()["username"])
            flash("อัปเดตสถานะครัวแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("kitchen"))

    @app.route("/orders/<order_id>/checkout", methods=["GET", "POST"])
    @roles_required("admin", "staff")
    def checkout(order_id):
        order = utils.find_by_id(g.data["orders"], order_id)
        if not order:
            abort(404)
        if order.get("order_type") == "dine_in" and order.get("status") != "payment_pending":
            flash("รอลูกค้ากดขอชำระเงินก่อน แล้วจึงยืนยันรับชำระและปล่อยโต๊ะได้", "warning")
            return redirect(url_for("order_detail", order_id=order_id))
        if request.method == "POST":
            try:
                utils.checkout(g.data, order_id, request.form, current_user()["username"])
                return redirect(url_for("receipt", order_id=order_id))
            except utils.ValidationError as error:
                handle_validation(error)
        preview = utils.calculate_bill(order)
        return render_template("checkout.html", order=order, preview=preview, table=utils.find_by_id(g.data["tables"], order["table_id"]))

    @app.get("/orders/<order_id>/receipt")
    @roles_required("admin", "staff")
    def receipt(order_id):
        order = utils.find_by_id(g.data["orders"], order_id)
        if not order or not order.get("bill"):
            abort(404)
        return render_template("receipt.html", order=order, table=utils.find_by_id(g.data["tables"], order["table_id"]))

    @app.get("/reports")
    @roles_required("admin")
    def reports():
        return render_template("reports.html", report=utils.daily_report(g.data), audit=list(reversed(g.data["audit"][-30:])))

    @app.get("/admin/backup.json")
    @roles_required("admin")
    def download_backup():
        payload = {key: value for key, value in g.data.items() if key != "_revision"}
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return send_file(io.BytesIO(content), mimetype="application/json", as_attachment=True, download_name=f"restaurant-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.json")

    @app.route("/settings", methods=["GET", "POST"])
    @roles_required("admin")
    def settings():
        if request.method == "POST":
            try:
                utils.update_restaurant_profile(g.data, request.form, current_user()["username"], request.files.get("logo_file"))
                flash("บันทึกข้อมูลและหน้าตกแต่งร้านแล้ว", "success")
                return redirect(url_for("settings"))
            except utils.ValidationError as error:
                handle_validation(error)
        return render_template("settings.html", settings=g.data.get("settings", {}), menu_items=g.data["menu_items"])

    @app.post("/reservations")
    @roles_required("customer")
    def customer_reserve_table():
        try:
            result = utils.create_customer_reservation(g.data, current_user(), request.form.get("party_size"))
            flash(result["message"], "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("dashboard"))

    @app.post("/reservations/cancel")
    @roles_required("customer")
    def customer_reservation_cancel():
        try:
            utils.cancel_customer_reservation(g.data, current_user())
            flash("ยกเลิกคิวแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("dashboard"))

    @app.post("/reservations/future")
    @roles_required("customer")
    def customer_future_reservation():
        try:
            reservation = utils.create_future_reservation(g.data, current_user(), request.form.get("party_size"), request.form.get("starts_at"))
            flash(f"ส่งคำขอจองวันที่ {reservation['starts_at'][:16]} แล้ว รอร้านยืนยัน", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("dashboard"))

    @app.post("/reservations/future/<reservation_id>/cancel")
    @roles_required("customer")
    def customer_future_reservation_cancel(reservation_id):
        try:
            utils.cancel_future_reservation(g.data, reservation_id, current_user())
            flash("ยกเลิกการจองล่วงหน้าแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("dashboard"))

    @app.get("/notifications")
    @roles_required("admin", "staff", "customer")
    def notification_list():
        return render_template("notifications.html", notifications=utils.notifications_for(g.data, current_user()["id"]))

    @app.post("/notifications/read-all")
    @roles_required("admin", "staff", "customer")
    def notification_read_all():
        try:
            utils.mark_all_notifications_read(g.data, current_user()["id"])
            flash("อ่านการแจ้งเตือนทั้งหมดแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("notification_list"))

    @app.post("/notifications/<notification_id>/read")
    @roles_required("admin", "staff", "customer")
    def notification_read(notification_id):
        try:
            utils.mark_notification_read(g.data, current_user()["id"], notification_id)
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("notification_list"))

    @app.get("/my-orders/cart")
    @roles_required("customer")
    def customer_cart():
        customer = current_user()
        reservation = utils.active_customer_reservation(g.data, customer["id"])
        if not reservation or reservation.get("status") != "seated":
            flash("ต้องได้โต๊ะก่อนจึงจะยืนยันออเดอร์ได้", "warning")
            return redirect(url_for("dashboard"))
        cart = utils.customer_cart(g.data, customer["id"])
        total = round(sum(float(line.get("unit_price", 0)) * int(line.get("qty", 0)) for line in cart), 2)
        return render_template("customer_cart.html", cart=cart, total=total, table=utils.find_by_id(g.data["tables"], reservation["table_id"]))

    @app.post("/my-orders/cart/update")
    @roles_required("customer")
    def customer_cart_update():
        try:
            utils.update_customer_cart_item(g.data, current_user(), request.form.get("line_id"), request.form.get("quantity"))
            flash("อัปเดตตะกร้าแล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("customer_cart"))

    @app.post("/my-orders/cart/confirm")
    @roles_required("customer")
    def customer_cart_confirm():
        try:
            reservation = utils.active_customer_reservation(g.data, current_user()["id"])
            order = utils.create_customer_table_order(g.data, current_user(), reservation)
            flash(f"ยืนยันและส่งออเดอร์ #{order['id']} ให้ร้านแล้ว", "success")
            return redirect(url_for("customer_orders"))
        except utils.ValidationError as error:
            handle_validation(error)
            return redirect(url_for("customer_cart"))

    @app.get("/my-orders")
    @roles_required("customer")
    def customer_orders():
        user = current_user()
        own_orders = [o for o in g.data["orders"] if str(o.get("customer_id")) == str(user["id"]) and o.get("order_type") in {"online", "dine_in"}]
        own_orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
        return render_template("customer_orders.html", orders=own_orders, order_total=utils.order_total)

    @app.get("/my-orders/<order_id>")
    @roles_required("customer")
    def customer_order_detail(order_id):
        user = current_user()
        order = utils.find_by_id(g.data["orders"], order_id)
        if not order or str(order.get("customer_id")) != str(user["id"]) or order.get("order_type") not in {"online", "dine_in"}:
            abort(404)
        return render_template("customer_order.html", order=order, menu=g.data["menu_items"], order_total=utils.order_total)

    @app.post("/my-orders/add")
    @roles_required("customer")
    def customer_order_add():
        try:
            customer = current_user()
            reservation = utils.active_customer_reservation(g.data, customer["id"])
            if not reservation or reservation.get("status") != "seated":
                raise utils.ValidationError("ต้องได้โต๊ะก่อนจึงจะเลือกเมนูได้")
            utils.add_customer_cart_item(g.data, customer, request.form.get("menu_id"), request.form.get("quantity", 1), request.form)
            flash("เพิ่มเมนูลงตะกร้าแล้ว เลือกเมนูอื่นต่อหรือเข้าไปยืนยันออเดอร์", "success")
            return redirect(url_for("menu_list"))
        except utils.ValidationError as error:
            handle_validation(error)
            return redirect(url_for("dashboard"))

    @app.route("/table/checkout", methods=["GET", "POST"])
    @roles_required("customer")
    def customer_table_checkout_page():
        try:
            if request.method == "POST":
                result = utils.customer_table_checkout(g.data, current_user())
                flash(f"ส่งคำขอชำระเงินยอด {result['request_total']:.2f} บาทแล้ว รอร้านตรวจสอบและยืนยันก่อนปล่อยโต๊ะ", "success")
                return redirect(url_for("dashboard"))
            preview = utils.customer_table_checkout_preview(g.data, current_user())
            return render_template("customer_table_checkout.html", preview=preview)
        except utils.ValidationError as error:
            handle_validation(error)
            return redirect(url_for("dashboard"))

    @app.post("/my-orders/<order_id>/items")
    @roles_required("customer")
    def customer_order_item(order_id):
        try:
            utils.change_customer_order_item(g.data, order_id, current_user(), request.form.get("menu_id"), request.form.get("quantity"), request.form.get("line_id"))
            flash("ปรับจำนวนในออเดอร์แล้ว", "success")
        except utils.ValidationError as error:
            handle_validation(error)
        return redirect(url_for("customer_order_detail", order_id=order_id))

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "restaurant-management"})

    @app.errorhandler(400)
    def bad_request(_error):
        if request.path.startswith("/menu"):
            flash("ทำรายการเมนูไม่สำเร็จ กลับสู่หน้าหลักแล้ว", "warning")
            return redirect(url_for("index"))
        return render_template("error.html", code=400, message="คำขอไม่ถูกต้องหรือหมดอายุ กรุณาลองใหม่"), 400

    @app.errorhandler(403)
    def forbidden(_error):
        if request.path.startswith("/menu"):
            flash("คุณไม่มีสิทธิ์เข้าถึงเมนูหน้านี้ กลับสู่หน้าหลักแล้ว", "warning")
            return redirect(url_for("index"))
        return render_template("error.html", code=403, message="คุณไม่มีสิทธิ์เข้าถึงหน้านี้"), 403

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/menu"):
            flash("ไม่พบเมนูหรือหน้าที่เลือก กลับสู่หน้าหลักแล้ว", "warning")
            return redirect(url_for("index"))
        return render_template("error.html", code=404, message="ไม่พบหน้าหรือข้อมูลที่ต้องการ"), 404

    @app.errorhandler(413)
    def request_too_large(_error):
        return render_template("error.html", code=413, message="ไฟล์ที่อัปโหลดมีขนาดใหญ่เกินกำหนด กรุณาเลือกไฟล์โลโก้ไม่เกิน 512 KB"), 413

    @app.errorhandler(utils.StorageError)
    def storage_error(error):
        app.logger.warning("Storage operation failed: %s", error)
        return render_template("error.html", code=503, message=str(error)), 503

    @app.errorhandler(500)
    def server_error(_error):
        app.logger.exception("Unhandled application error")
        if request.path.startswith("/menu"):
            flash("เปิดหน้าเมนูไม่สำเร็จ กลับสู่หน้าหลักแล้ว", "warning")
            return redirect(url_for("index"))
        if request.path.startswith("/api/"):
            return jsonify({"error": "เกิดข้อผิดพลาดภายในระบบ กรุณาลองใหม่"}), 500
        return render_template("error.html", code=500, message="เกิดข้อผิดพลาดภายในระบบ กรุณาลองใหม่"), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
