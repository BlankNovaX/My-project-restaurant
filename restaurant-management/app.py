"""Flask routes and web application factory for Restaurant Management System."""
from __future__ import annotations

import os
from functools import wraps
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for

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
        return {"current_user": current_user(), "csrf_token": session.get("csrf_token", ""), "currency": "฿"}

    def current_user():
        user_id = session.get("user_id")
        if not user_id:
            return None
        return next((u for u in getattr(g, "data", {}).get("users", []) if str(u.get("id")) == str(user_id)), None)

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
                user = utils.register_user(g.data, request.form.get("username"), request.form.get("password"))
                session.clear()
                session["user_id"] = user["id"]
                flash("สมัครสมาชิกเรียบร้อย", "success")
                return redirect(url_for("dashboard"))
            except utils.ValidationError as error:
                handle_validation(error)
        return render_template("auth.html", mode="register")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            try:
                user = utils.authenticate(g.data, request.form.get("username"), request.form.get("password"))
                if not user:
                    flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")
                else:
                    session.clear()
                    session["user_id"] = user["id"]
                    flash(f"ยินดีต้อนรับ {user['username']}", "success")
                    next_path = request.args.get("next", "")
                    if not next_path.startswith("/") or next_path.startswith("//"):
                        next_path = url_for("dashboard")
                    return redirect(next_path)
            except utils.ValidationError as error:
                handle_validation(error)
        return render_template("auth.html", mode="login")

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
            return render_template("customer.html", menu=utils.list_records(g.data["menu_items"], request.args.get("q", ""), request.args.get("category", ""), request.args.get("sort", "name"), request.args.get("direction", "asc"), request.args.get("page", 1)), categories=sorted({i["category"] for i in g.data["menu_items"]}))
        report = utils.daily_report(g.data)
        active_orders = [o for o in g.data["orders"] if o.get("status") not in {"paid", "cancelled"}]
        active_orders.sort(key=lambda o: o.get("created_at", ""))
        return render_template("dashboard.html", report=report, active_orders=active_orders, tables=g.data["tables"])

    @app.get("/menu")
    @roles_required("admin", "staff", "customer")
    def menu_list():
        user = current_user()
        listing = utils.list_records(g.data["menu_items"], request.args.get("q", ""), request.args.get("category", ""), request.args.get("sort", "name"), request.args.get("direction", "asc"), request.args.get("page", 1))
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
        return render_template("tables.html", tables=sorted(g.data["tables"], key=lambda t: t["number"], reverse=request.args.get("direction") == "desc"), orders=g.data["orders"])

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

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "restaurant-management"})

    @app.errorhandler(400)
    def bad_request(_error):
        return render_template("error.html", code=400, message="คำขอไม่ถูกต้องหรือหมดอายุ กรุณาลองใหม่"), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("error.html", code=403, message="คุณไม่มีสิทธิ์เข้าถึงหน้านี้"), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="ไม่พบหน้าหรือข้อมูลที่ต้องการ"), 404

    @app.errorhandler(500)
    def server_error(_error):
        app.logger.exception("Unhandled application error")
        if request.path.startswith("/api/"):
            return jsonify({"error": "เกิดข้อผิดพลาดภายในระบบ กรุณาลองใหม่"}), 500
        return render_template("error.html", code=500, message="เกิดข้อผิดพลาดภายในระบบ กรุณาลองใหม่"), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
