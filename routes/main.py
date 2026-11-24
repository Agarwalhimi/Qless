"""
main.py - Student ordering, live order tracking, multi-canteen queues,
automatic wait-time calculation, verified UPI payments, staff OTP handover,
and manager controls.

Every query works identically on both SQLite and MySQL.
"""

import os
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from werkzeug.utils import secure_filename

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for

from config import Config
from db import (
    DBIntegrityError,
    campus_now,
    ensure_today_slots,
    execute,
    fetch_all,
    fetch_one,
    sign_upi_payment,
)
from routes.access import login_required, role_required

main_bp = Blueprint("main", __name__)

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
CATEGORY_DEFAULT_PHOTOS = {
    "Breakfast": "/static/img/dishes/masala-dosa.jpg",
    "Lunch": "/static/img/dishes/veg-thali.jpg",
    "Dinner": "/static/img/dishes/veg-thali.jpg",
    "Snacks": "/static/img/dishes/samosa.jpg",
    "Rolls & Wraps": "/static/img/dishes/paneer-wrap.jpg",
    "Beverages": "/static/img/dishes/masala-chai.jpg",
}
VALID_DIETARY = ("any", "veg", "non_veg", "egg")
VALID_WASTE_REASONS = ("overproduction", "spoilage", "uncollected")
STATUS_FLOW = {
    "confirmed": "preparing",
    "preparing": "ready",
    "ready": "collected",
}
UPI_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{2,64}@[a-zA-Z0-9]{2,32}$")
UTR_PATTERN = re.compile(r"^[A-Z0-9]{8,22}$")


def _save_dish_photo(file_obj, fallback_url="", category="Snacks"):
    """Save an uploaded photo to static/img/uploads/ or use provided URL/category default."""
    if file_obj and getattr(file_obj, "filename", ""):
        clean_name = secure_filename(file_obj.filename)
        ext = os.path.splitext(clean_name)[1].lower()
        if ext in ALLOWED_IMAGE_EXTS:
            unique_name = f"{secrets.token_hex(6)}{ext}"
            upload_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "static",
                "img",
                "uploads",
            )
            os.makedirs(upload_dir, exist_ok=True)
            file_obj.save(os.path.join(upload_dir, unique_name))
            return f"/static/img/uploads/{unique_name}"
    if fallback_url and (
        fallback_url.startswith("/")
        or fallback_url.startswith("http://")
        or fallback_url.startswith("https://")
    ):
        return fallback_url[:255]
    return CATEGORY_DEFAULT_PHOTOS.get(category, "/static/img/dishes/veg-thali.jpg")


def _notify_user(user_id, message, notif_type="order_ready"):
    try:
        execute(
            "INSERT INTO notifications (user_id, message, type, is_read) VALUES (%s, %s, %s, 0)",
            (user_id, message[:250], notif_type),
        )
    except Exception:
        pass


def _notify_staff(message):
    try:
        staff_rows = fetch_all("SELECT id FROM users WHERE role IN ('staff', 'manager')")
        for s in staff_rows:
            _notify_user(s["id"], message, "slot_reminder")
    except Exception:
        pass


def _today_str():
    return campus_now().date().isoformat()


def _tomorrow_str():
    return (campus_now().date() + timedelta(days=1)).isoformat()


def _format_slot_label(start_time, end_time, slot_date=None):
    """Turn '12:00:00' and '12:15:00' into '12:00 - 12:15' (with Tomorrow prefix when applicable)."""
    def _clean(t):
        text = str(t)
        parts = text.split(":")
        if len(parts) >= 2:
            return f"{int(parts[0]):02d}:{parts[1]}"
        return text

    label = f"{_clean(start_time)} - {_clean(end_time)}"
    if slot_date and str(slot_date) == _tomorrow_str():
        return f"Tomorrow · {label}"
    return label


def _get_kitchen_workload(canteen_id, before_order_id=None):
    """Calculate live kitchen workload and automatic waiting time for a specific canteen."""
    if before_order_id is not None:
        row = fetch_one(
            """
            SELECT COUNT(DISTINCT o.id) AS active_orders,
                   COALESCE(SUM(oi.quantity), 0) AS active_items
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.id
            WHERE o.canteen_id = %s
              AND o.status IN ('confirmed', 'preparing')
              AND o.id < %s
            """,
            (canteen_id, before_order_id),
        )
    else:
        row = fetch_one(
            """
            SELECT COUNT(DISTINCT o.id) AS active_orders,
                   COALESCE(SUM(oi.quantity), 0) AS active_items
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.id
            WHERE o.canteen_id = %s
              AND o.status IN ('confirmed', 'preparing')
            """,
            (canteen_id,),
        )
    active_orders = int(row["active_orders"] or 0) if row else 0
    active_items = int(row["active_items"] or 0) if row else 0
    return active_orders, active_items


def _estimate_wait_minutes(canteen_id, item_prep_times=None, before_order_id=None, status=None):
    """
    Automatic waiting-time calculation based on live kitchen workload + dish prep time.
    Returns dict: {wait_mins, active_orders, active_items, load_label, load_class}.
    """
    if status == "ready":
        return {"wait_mins": 0, "active_orders": 0, "active_items": 0, "load_label": "Ready Now", "load_class": "success"}
    if status in ("collected", "cancelled"):
        return {"wait_mins": 0, "active_orders": 0, "active_items": 0, "load_label": "Completed", "load_class": "info"}

    active_orders, active_items = _get_kitchen_workload(canteen_id, before_order_id=before_order_id)
    base_prep = max(item_prep_times) if item_prep_times else 6
    if status == "preparing":
        base_prep = max(3, int(round(base_prep * 0.55)))

    queue_delay = active_orders * 3 + active_items
    wait_mins = max(3, min(45, int(base_prep + queue_delay)))

    if wait_mins <= 10:
        load_label, load_class = "Low Queue", "success"
    elif wait_mins <= 20:
        load_label, load_class = "Moderate Queue", "warning"
    else:
        load_label, load_class = "Busy Kitchen", "error"

    return {
        "wait_mins": wait_mins,
        "active_orders": active_orders,
        "active_items": active_items,
        "load_label": load_label,
        "load_class": load_class,
    }


def _get_canteens_with_workload():
    canteens = fetch_all(
        "SELECT id, name, location, badge_color, upi_vpa, open_hour, close_hour FROM canteens WHERE is_active = 1 ORDER BY id"
    )
    for c in canteens:
        est = _estimate_wait_minutes(c["id"])
        c.update(est)
    return canteens


def _get_available_slots_with_counts(canteen_id=None):
    """
    Return upcoming 15-minute IST pickup slots for today.
    If all of today's slots have passed (e.g. after 22:00 IST), automatically return
    tomorrow's IST 15-minute slots so students can always place pre-orders cleanly.
    """
    ensure_today_slots()
    today = _today_str()
    tomorrow = _tomorrow_str()
    now_str = campus_now().strftime("%H:%M:%S")

    def _query_slots(target_date, min_end_time=None):
        params = []
        canteen_filter = ""
        if canteen_id is not None:
            canteen_filter = "AND o.canteen_id = %s"
            params.append(canteen_id)

        time_filter = ""
        params.append(target_date)
        if min_end_time is not None:
            time_filter = "AND ps.end_time > %s"
            params.append(min_end_time)

        rows = fetch_all(
            f"""
            SELECT ps.id, ps.slot_date, ps.start_time, ps.end_time, ps.capacity,
                   COUNT(o.id) AS booked_count
            FROM pickup_slots ps
            LEFT JOIN orders o
                   ON o.slot_id = ps.id
                  AND o.status != 'cancelled'
                  {canteen_filter}
            WHERE ps.slot_date = %s {time_filter}
            GROUP BY ps.id, ps.slot_date, ps.start_time, ps.end_time, ps.capacity
            ORDER BY ps.start_time
            """,
            tuple(params),
        )
        for slot in rows:
            slot["label"] = _format_slot_label(slot["start_time"], slot["end_time"], slot["slot_date"])
            slot["remaining"] = max(0, int(slot["capacity"]) - int(slot["booked_count"]))
            slot["is_tomorrow"] = str(slot["slot_date"]) == tomorrow
        return rows

    slots = _query_slots(today, min_end_time=now_str)
    slot_day_note = "Today (IST)"
    if not slots or all(int(s["remaining"]) == 0 for s in slots):
        tomorrow_slots = _query_slots(tomorrow, min_end_time=None)
        if tomorrow_slots:
            slots = tomorrow_slots
            slot_day_note = "Tomorrow (IST — today's service window ended)"

    return slots, slot_day_note


# =========================================================
# Account (all logged-in roles)
# =========================================================

@main_bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        diet = request.form.get("dietary_preference", "any")
        if diet not in VALID_DIETARY:
            flash("Please choose a valid dietary preference.", "error")
        else:
            execute(
                "UPDATE users SET dietary_preference = %s WHERE id = %s",
                (diet, g.user["id"]),
            )
            g.user["dietary_preference"] = diet
            flash("Dietary preference updated.", "success")
        return redirect(url_for("main.account"))

    return render_template("temp/account.html")


# =========================================================
# Student: Multi-Canteen Menu, Favorites, Cart, UPI Checkout & Live Orders
# =========================================================

@main_bp.route("/menu")
@role_required("student")
def student_menu():
    search_q = request.args.get("q", "").strip().lower()
    category = request.args.get("category", "All").strip()
    diet = request.args.get("diet", "all").strip()
    canteen_param = request.args.get("canteen", "all").strip()
    fast_only = request.args.get("fast") == "1"
    fav_only = request.args.get("fav") == "1"

    canteens = _get_canteens_with_workload()
    selected_canteen_id = int(canteen_param) if canteen_param.isdigit() else None
    selected_canteen = next((c for c in canteens if c["id"] == selected_canteen_id), None)

    all_items = fetch_all(
        """
        SELECT mi.id, mi.canteen_id, c.name AS canteen_name, c.badge_color AS canteen_color,
               mi.name, mi.description, mi.category, mi.price, mi.food_type, mi.allergens,
               mi.prep_time_minutes, mi.image_url, mi.stock_qty, mi.is_sold_out, mi.is_featured, mi.is_surplus,
               COALESCE(ROUND(AVG(f.rating), 1), 0) AS average_rating,
               COUNT(DISTINCT f.id) AS rating_count
        FROM menu_items mi
        JOIN canteens c ON c.id = mi.canteen_id
        LEFT JOIN order_items oi ON oi.menu_item_id = mi.id
        LEFT JOIN feedback f ON f.order_id = oi.order_id
        WHERE mi.is_active = 1
        GROUP BY mi.id, mi.canteen_id, c.name, c.badge_color,
                 mi.name, mi.description, mi.category, mi.price, mi.food_type, mi.allergens,
                 mi.prep_time_minutes, mi.image_url, mi.stock_qty, mi.is_sold_out, mi.is_featured, mi.is_surplus
        ORDER BY mi.is_featured DESC, mi.category, mi.name
        """
    )

    fav_rows = fetch_all(
        "SELECT menu_item_id FROM favorites WHERE user_id = %s",
        (g.user["id"],),
    )
    favorite_ids = {row["menu_item_id"] for row in fav_rows}

    meal_order = ["Breakfast", "Lunch", "Snacks", "Dinner"]
    categories = (
        ["All"]
        + [c for c in meal_order if any(item["category"] == c for item in all_items)]
        + sorted({item["category"] for item in all_items if item["category"] not in meal_order})
    )
    cart = session.get("cart", {})

    filtered = []
    for item in all_items:
        item["is_favorite"] = item["id"] in favorite_ids
        item["in_cart_qty"] = int(cart.get(str(item["id"]), 0))
        if selected_canteen_id is not None and int(item["canteen_id"]) != selected_canteen_id:
            continue
        if category != "All" and item["category"] != category:
            continue
        if diet in ("veg", "egg", "non_veg") and item["food_type"] != diet:
            continue
        if fast_only and not item["is_featured"] and int(item["prep_time_minutes"]) > 5:
            continue
        if fav_only and not item["is_favorite"]:
            continue
        if search_q and search_q not in item["name"].lower() and search_q not in item["description"].lower():
            continue
        filtered.append(item)

    announcement = fetch_one(
        """
        SELECT a.message, a.created_at, c.name AS canteen_name
        FROM announcements a
        LEFT JOIN canteens c ON c.id = a.canteen_id
        WHERE a.is_active = 1
          AND (a.canteen_id IS NULL OR a.canteen_id = %s)
        ORDER BY a.id DESC LIMIT 1
        """,
        (selected_canteen_id or 1,),
    )

    return render_template(
        "temp/student_home.html",
        menu_items=filtered,
        canteens=canteens,
        selected_canteen_id=selected_canteen_id,
        selected_canteen=selected_canteen,
        categories=categories,
        selected_category=category,
        selected_diet=diet,
        search_q=search_q,
        fast_only=fast_only,
        fav_only=fav_only,
        low_stock_limit=Config.LOW_STOCK_LIMIT,
        announcement=announcement,
    )


@main_bp.route("/favorites/toggle/<int:item_id>", methods=["POST"])
@role_required("student")
def toggle_favorite(item_id):
    existing = fetch_one(
        "SELECT id FROM favorites WHERE user_id = %s AND menu_item_id = %s",
        (g.user["id"], item_id),
    )
    if existing:
        execute("DELETE FROM favorites WHERE id = %s", (existing["id"],))
        flash("Removed from favorites.", "info")
    else:
        try:
            execute(
                "INSERT INTO favorites (user_id, menu_item_id) VALUES (%s, %s)",
                (g.user["id"], item_id),
            )
            flash("Saved to your favorite dishes!", "success")
        except DBIntegrityError:
            pass
    return redirect(request.referrer or url_for("main.student_menu"))


@main_bp.route("/cart/add/<int:item_id>", methods=["POST"])
@role_required("student")
def cart_add(item_id):
    item = fetch_one(
        """
        SELECT mi.id, mi.name, mi.canteen_id, mi.stock_qty, mi.is_sold_out, c.name AS canteen_name
        FROM menu_items mi
        JOIN canteens c ON c.id = mi.canteen_id
        WHERE mi.id = %s AND mi.is_active = 1
        """,
        (item_id,),
    )
    if not item or item["is_sold_out"] or int(item["stock_qty"]) <= 0:
        flash("Sorry, that dish is currently sold out.", "error")
        return redirect(request.referrer or url_for("main.student_menu"))

    cart = dict(session.get("cart", {}))

    # Enforce single-canteen cart so each order routes cleanly to one kitchen queue & counter
    if cart:
        first_id = int(next(iter(cart.keys())))
        existing_item = fetch_one(
            """
            SELECT mi.canteen_id, c.name AS canteen_name
            FROM menu_items mi JOIN canteens c ON c.id = mi.canteen_id
            WHERE mi.id = %s
            """,
            (first_id,),
        )
        if existing_item and int(existing_item["canteen_id"]) != int(item["canteen_id"]):
            flash(
                f"Your cart currently has items from {existing_item['canteen_name']}. "
                f"Please checkout or clear your cart before ordering from {item['canteen_name']}.",
                "info",
            )
            return redirect(url_for("main.view_cart"))

    key = str(item_id)
    new_qty = int(cart.get(key, 0)) + 1

    if new_qty > int(item["stock_qty"]):
        flash(f"Only {item['stock_qty']} left for {item['name']}.", "error")
        return redirect(request.referrer or url_for("main.student_menu"))

    cart[key] = new_qty
    session["cart"] = cart
    flash(f"Added {item['name']} ({item['canteen_name']}) to your cart.", "success")
    return redirect(request.referrer or url_for("main.student_menu"))


@main_bp.route("/cart/update/<int:item_id>", methods=["POST"])
@role_required("student")
def cart_update(item_id):
    action = request.form.get("action", "inc")
    cart = dict(session.get("cart", {}))
    key = str(item_id)
    current_qty = int(cart.get(key, 0))

    if action == "dec":
        if current_qty <= 1:
            cart.pop(key, None)
        else:
            cart[key] = current_qty - 1
    elif action == "remove":
        cart.pop(key, None)
    else:
        item = fetch_one("SELECT stock_qty, is_sold_out FROM menu_items WHERE id = %s", (item_id,))
        if item and not item["is_sold_out"] and current_qty + 1 <= int(item["stock_qty"]):
            cart[key] = current_qty + 1
        else:
            flash("No more stock available for that item.", "error")

    session["cart"] = cart
    return redirect(request.referrer or url_for("main.view_cart"))


@main_bp.route("/cart/clear", methods=["POST"])
@role_required("student")
def cart_clear():
    session.pop("cart", None)
    flash("Cart cleared.", "info")
    return redirect(url_for("main.student_menu"))


@main_bp.route("/cart")
@role_required("student")
def view_cart():
    cart = session.get("cart", {})
    cart_items = []
    total_amount = 0.0
    canteen_id = 1
    canteen_name = Config.CANTEEN_NAME
    canteen_location = "Central Block"
    canteen_upi_vpa = Config.UPI_MERCHANT_VPA
    prep_times = []

    for item_id_str, qty in cart.items():
        if not str(item_id_str).isdigit():
            continue
        item = fetch_one(
            """
            SELECT mi.id, mi.canteen_id, c.name AS canteen_name, c.location AS canteen_location,
                   c.upi_vpa AS canteen_upi_vpa, c.badge_color AS canteen_color,
                   mi.name, mi.category, mi.price, mi.food_type, mi.image_url,
                   mi.prep_time_minutes, mi.stock_qty, mi.is_sold_out, mi.is_surplus,
                   COALESCE(ROUND(AVG(f.rating), 1), 0) AS average_rating,
                   COUNT(DISTINCT f.id) AS rating_count
            FROM menu_items mi
            JOIN canteens c ON c.id = mi.canteen_id
            LEFT JOIN order_items oi ON oi.menu_item_id = mi.id
            LEFT JOIN feedback f ON f.order_id = oi.order_id
            WHERE mi.id = %s
            GROUP BY mi.id, mi.canteen_id, c.name, c.location, c.upi_vpa, c.badge_color,
                     mi.name, mi.category, mi.price, mi.food_type, mi.image_url,
                     mi.prep_time_minutes, mi.stock_qty, mi.is_sold_out, mi.is_surplus
            """,
            (int(item_id_str),),
        )
        if not item:
            continue
        canteen_id = int(item["canteen_id"])
        canteen_name = item["canteen_name"]
        canteen_location = item["canteen_location"]
        canteen_upi_vpa = item["canteen_upi_vpa"]
        qty_int = int(qty)
        unit_p = round(float(item["price"]) * 0.75, 2) if item.get("is_surplus") else float(item["price"])
        item["price"] = unit_p
        subtotal = unit_p * qty_int
        total_amount += subtotal
        prep_times.append(int(item["prep_time_minutes"] or 8))
        cart_items.append({**item, "qty": qty_int, "subtotal": subtotal})

    slots, slot_day_note = _get_available_slots_with_counts(canteen_id=canteen_id if cart_items else None)
    available_slots = [slot for slot in slots if int(slot["remaining"]) > 0]
    recommended_slot_id = (
        max(available_slots, key=lambda slot: (int(slot["remaining"]), -int(slot["id"])))["id"]
        if available_slots
        else None
    )
    wait_info = _estimate_wait_minutes(canteen_id, item_prep_times=prep_times) if cart_items else None

    return render_template(
        "student/cart.html",
        cart_items=cart_items,
        total_amount=total_amount,
        slots=slots,
        slot_day_note=slot_day_note,
        recommended_slot_id=recommended_slot_id,
        canteen_name=canteen_name,
        canteen_location=canteen_location,
        canteen_upi_vpa=canteen_upi_vpa,
        wait_info=wait_info,
    )


@main_bp.route("/checkout", methods=["POST"])
@role_required("student")
def checkout():
    cart = session.get("cart", {})
    if not cart:
        flash("Your cart is empty. Add a dish first.", "error")
        return redirect(url_for("main.student_menu"))

    slot_id = request.form.get("slot_id", "").strip()
    payment_method = request.form.get("payment_method", "upi").strip()
    upi_id = request.form.get("upi_id", "").strip().lower()

    if payment_method not in ("upi", "counter"):
        payment_method = "counter"

    if payment_method == "upi" and upi_id and not UPI_ID_PATTERN.match(upi_id):
        flash("Please enter a valid UPI ID (for example: yourname@upi or 9876543210@ybl).", "error")
        return redirect(url_for("main.view_cart"))

    if not slot_id.isdigit():
        flash("Please choose a 15-minute pickup slot.", "error")
        return redirect(url_for("main.view_cart"))

    slot = fetch_one(
        "SELECT id, slot_date, start_time, end_time, capacity FROM pickup_slots WHERE id = %s",
        (int(slot_id),),
    )
    today = _today_str()
    tomorrow = _tomorrow_str()
    now_str = campus_now().strftime("%H:%M:%S")
    slot_valid = (
        slot is not None
        and (
            (str(slot["slot_date"]) == today and str(slot["end_time"])[:8] > now_str)
            or str(slot["slot_date"]) == tomorrow
        )
    )
    if not slot_valid:
        flash("That pickup slot has expired. Please choose an upcoming IST slot.", "error")
        return redirect(url_for("main.view_cart"))

    # Verify stock, compute total, and identify canteen
    line_items = []
    total_amount = 0.0
    canteen_ids = set()
    prep_times = []
    for item_id_str, qty in cart.items():
        if not str(item_id_str).isdigit():
            continue
        qty_int = int(qty)
        item = fetch_one(
            "SELECT id, canteen_id, name, price, prep_time_minutes, stock_qty, is_sold_out, is_surplus FROM menu_items WHERE id = %s",
            (int(item_id_str),),
        )
        if not item or item["is_sold_out"] or int(item["stock_qty"]) < qty_int:
            flash(f"Not enough stock left for {item['name'] if item else 'an item'}.", "error")
            return redirect(url_for("main.view_cart"))
        canteen_ids.add(int(item["canteen_id"]))
        unit_p = round(float(item["price"]) * 0.75, 2) if item.get("is_surplus") else float(item["price"])
        item["price"] = unit_p
        total_amount += unit_p * qty_int
        prep_times.append(int(item["prep_time_minutes"] or 8))
        line_items.append((item, qty_int))

    if not line_items:
        flash("Your cart is empty.", "error")
        return redirect(url_for("main.student_menu"))

    canteen_id = next(iter(canteen_ids))
    canteen = fetch_one("SELECT id, name FROM canteens WHERE id = %s", (canteen_id,))
    canteen_name = canteen["name"] if canteen else Config.CANTEEN_NAME

    booked = fetch_one(
        "SELECT COUNT(*) AS total FROM orders WHERE slot_id = %s AND canteen_id = %s AND status != 'cancelled'",
        (slot["id"], canteen_id),
    )["total"]
    if int(booked) >= int(slot["capacity"]):
        flash("That pickup slot is now full for this canteen. Please pick another slot.", "error")
        return redirect(url_for("main.view_cart"))

    wait_info = _estimate_wait_minutes(canteen_id, item_prep_times=prep_times)
    pickup_otp = f"{secrets.randbelow(9000) + 1000:04d}"
    qr_token = f"QL-{secrets.token_hex(6).upper()}"
    created_ist = campus_now().strftime("%Y-%m-%d %H:%M:%S")

    order_id = execute(
        """
        INSERT INTO orders
            (student_id, canteen_id, slot_id, status, payment_method, payment_status,
             upi_id, estimated_wait_mins, total_amount, pickup_otp, qr_token, created_at)
        VALUES (%s, %s, %s, 'confirmed', %s, 'pending', %s, %s, %s, %s, %s, %s)
        """,
        (
            g.user["id"],
            canteen_id,
            slot["id"],
            payment_method,
            upi_id or None,
            wait_info["wait_mins"],
            round(total_amount, 2),
            pickup_otp,
            qr_token,
            created_ist,
        ),
    )

    # Insert order items and decrement live inventory immediately
    for item, qty_int in line_items:
        execute(
            """
            INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price)
            VALUES (%s, %s, %s, %s)
            """,
            (order_id, item["id"], qty_int, float(item["price"])),
        )
        new_stock = max(0, int(item["stock_qty"]) - qty_int)
        execute(
            "UPDATE menu_items SET stock_qty = %s, is_sold_out = %s WHERE id = %s",
            (new_stock, 1 if new_stock == 0 else 0, item["id"]),
        )

    slot_label = _format_slot_label(slot["start_time"], slot["end_time"], slot["slot_date"])
    session.pop("cart", None)

    if payment_method == "upi":
        _notify_user(
            g.user["id"],
            f"Order #{order_id} created at {canteen_name} ({slot_label}). Complete UPI verification to confirm payment.",
            "slot_reminder",
        )
        flash(f"Order #{order_id} created! Complete UPI verification below to confirm payment.", "info")
        return redirect(url_for("main.verify_upi_payment", order_id=order_id))

    _notify_user(
        g.user["id"],
        f"Order #{order_id} placed at {canteen_name} for slot {slot_label} (~{wait_info['wait_mins']} min wait). Pickup OTP: {pickup_otp}.",
        "slot_reminder",
    )
    _notify_staff(
        f"New Order #{order_id} at {canteen_name} from {g.user['full_name']} for slot {slot_label} (Rs. {total_amount:.0f})."
    )
    flash(
        f"Order #{order_id} placed at {canteen_name} for {slot_label}! Est. wait ~{wait_info['wait_mins']} min · Pickup OTP: {pickup_otp}.",
        "success",
    )
    return redirect(url_for("main.student_orders"))


@main_bp.route("/orders/<int:order_id>/pay-upi", methods=["GET", "POST"])
@role_required("student")
def verify_upi_payment(order_id):
    order = fetch_one(
        """
        SELECT o.id, o.student_id, o.canteen_id, o.status, o.payment_method, o.payment_status,
               o.upi_id, o.upi_txn_ref, o.payment_signature, o.payment_verified_at,
               o.estimated_wait_mins, o.total_amount, o.pickup_otp, o.qr_token,
               c.name AS canteen_name, c.location AS canteen_location, c.upi_vpa AS merchant_vpa,
               ps.slot_date, ps.start_time, ps.end_time
        FROM orders o
        JOIN canteens c ON c.id = o.canteen_id
        JOIN pickup_slots ps ON ps.id = o.slot_id
        WHERE o.id = %s AND o.student_id = %s
        """,
        (order_id, g.user["id"]),
    )
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("main.student_orders"))

    if order["status"] == "cancelled":
        flash("This order was cancelled.", "info")
        return redirect(url_for("main.student_orders"))

    order["slot_label"] = _format_slot_label(order["start_time"], order["end_time"], order["slot_date"])
    merchant_vpa = order["merchant_vpa"] or Config.UPI_MERCHANT_VPA
    txn_note = f"QLess Order #{order['id']} - {order['canteen_name']}"
    upi_deep_link = (
        f"upi://pay?pa={quote_plus(merchant_vpa)}"
        f"&pn={quote_plus(order['canteen_name'])}"
        f"&am={float(order['total_amount']):.2f}"
        f"&cu=INR"
        f"&tn={quote_plus(txn_note)}"
        f"&tr=QLORD{order['id']}"
    )

    if request.method == "POST":
        if order["payment_status"] == "paid":
            flash(f"Order #{order_id} is already UPI-verified.", "success")
            return redirect(url_for("main.student_orders"))

        mode = request.form.get("verify_mode", "manual").strip()
        payer_upi = (request.form.get("upi_id") or order["upi_id"] or "").strip().lower()
        utr_ref = request.form.get("upi_txn_ref", "").strip().upper()

        if mode == "instant_demo":
            if not payer_upi:
                payer_upi = f"{g.user['email'].split('@')[0]}@okupi"
            utr_ref = f"4{campus_now().strftime('%y%m%d')}{secrets.randbelow(90000) + 10000}"
        else:
            if not payer_upi or not UPI_ID_PATTERN.match(payer_upi):
                flash("Enter a valid UPI ID (e.g. name@upi or mobile@ybl).", "error")
                return render_template("student/upi_payment.html", order=order, merchant_vpa=merchant_vpa, upi_deep_link=upi_deep_link)
            if not UTR_PATTERN.match(utr_ref):
                flash("Enter a valid 8–22 character UTR / UPI transaction reference number.", "error")
                return render_template("student/upi_payment.html", order=order, merchant_vpa=merchant_vpa, upi_deep_link=upi_deep_link)

        verified_at = campus_now().strftime("%Y-%m-%d %H:%M:%S")
        signature = sign_upi_payment(order["id"], order["total_amount"], utr_ref)
        execute(
            """
            UPDATE orders
            SET payment_method = 'upi',
                payment_status = 'paid',
                upi_id = %s,
                upi_txn_ref = %s,
                payment_signature = %s,
                payment_verified_at = %s
            WHERE id = %s
            """,
            (payer_upi, utr_ref, signature, verified_at, order["id"]),
        )
        _notify_user(
            g.user["id"],
            f"UPI Payment Verified (UTR {utr_ref}) for Order #{order_id} at {order['canteen_name']}. Pickup OTP: {order['pickup_otp']}.",
            "slot_reminder",
        )
        _notify_staff(
            f"UPI Verified Order #{order_id} at {order['canteen_name']} from {g.user['full_name']} (Rs. {order['total_amount']:.0f}, UTR {utr_ref})."
        )
        flash(f"UPI Payment Verified! UTR: {utr_ref} · Pickup OTP: {order['pickup_otp']}.", "success")
        return redirect(url_for("main.student_orders"))

    return render_template(
        "student/upi_payment.html",
        order=order,
        merchant_vpa=merchant_vpa,
        upi_deep_link=upi_deep_link,
    )


@main_bp.route("/orders")
@role_required("student")
def student_orders():
    status_filter = request.args.get("filter", "all").strip().lower()
    orders = fetch_all(
        """
        SELECT o.id, o.canteen_id, o.status, o.payment_method, o.payment_status,
               o.upi_id, o.upi_txn_ref, o.payment_signature, o.payment_verified_at,
               o.estimated_wait_mins, o.total_amount,
               o.pickup_otp, o.qr_token, o.created_at,
               c.name AS canteen_name, c.location AS canteen_location, c.badge_color AS canteen_color,
               ps.slot_date, ps.start_time, ps.end_time,
               f.rating AS user_rating, f.comment AS user_comment
        FROM orders o
        JOIN canteens c ON c.id = o.canteen_id
        JOIN pickup_slots ps ON ps.id = o.slot_id
        LEFT JOIN feedback f ON f.order_id = o.id
        WHERE o.student_id = %s
        ORDER BY o.id DESC
        """,
        (g.user["id"],),
    )

    counts = {"all": len(orders), "active": 0, "ready": 0, "completed": 0}
    filtered_orders = []

    for order in orders:
        order["slot_label"] = _format_slot_label(order["start_time"], order["end_time"], order["slot_date"])
        items = fetch_all(
            """
            SELECT oi.quantity, oi.unit_price, mi.name, mi.image_url, mi.prep_time_minutes, mi.food_type
            FROM order_items oi
            JOIN menu_items mi ON mi.id = oi.menu_item_id
            WHERE oi.order_id = %s
            """,
            (order["id"],),
        )
        order["items"] = items
        prep_times = [int(it["prep_time_minutes"] or 8) for it in items]
        wait_info = _estimate_wait_minutes(
            order["canteen_id"],
            item_prep_times=prep_times,
            before_order_id=order["id"],
            status=order["status"],
        )
        order["live_wait_mins"] = wait_info["wait_mins"]
        order["queue_ahead"] = wait_info["active_orders"]
        order["load_label"] = wait_info["load_label"]
        order["load_class"] = wait_info["load_class"]

        if order["status"] in ("confirmed", "preparing", "ready"):
            counts["active"] += 1
        if order["status"] == "ready":
            counts["ready"] += 1
        if order["status"] in ("collected", "cancelled"):
            counts["completed"] += 1

        if status_filter == "active" and order["status"] not in ("confirmed", "preparing", "ready"):
            continue
        if status_filter == "ready" and order["status"] != "ready":
            continue
        if status_filter == "completed" and order["status"] not in ("collected", "cancelled"):
            continue
        filtered_orders.append(order)

    return render_template(
        "student/orders.html",
        orders=filtered_orders,
        all_orders_count=counts["all"],
        counts=counts,
        status_filter=status_filter,
        canteen_name=Config.CANTEEN_NAME,
    )


@main_bp.route("/orders/<int:order_id>/cancel", methods=["POST"])
@role_required("student")
def cancel_order(order_id):
    order = fetch_one(
        "SELECT id, status, payment_status FROM orders WHERE id = %s AND student_id = %s",
        (order_id, g.user["id"]),
    )
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("main.student_orders"))

    if order["status"] != "confirmed":
        flash("Only confirmed orders that have not started cooking can be cancelled.", "error")
        return redirect(url_for("main.student_orders"))

    items = fetch_all(
        "SELECT menu_item_id, quantity FROM order_items WHERE order_id = %s",
        (order_id,),
    )
    for row in items:
        execute(
            "UPDATE menu_items SET stock_qty = stock_qty + %s, is_sold_out = 0 WHERE id = %s",
            (int(row["quantity"]), row["menu_item_id"]),
        )

    new_pay_status = "refunded" if order["payment_status"] == "paid" else order["payment_status"]
    execute(
        "UPDATE orders SET status = 'cancelled', payment_status = %s, cancelled_at = %s WHERE id = %s",
        (new_pay_status, campus_now().strftime("%Y-%m-%d %H:%M:%S"), order_id),
    )
    _notify_user(g.user["id"], f"Order #{order_id} was cancelled and stock was restored.", "order_cancelled")
    flash(f"Order #{order_id} cancelled and stock restored.", "info")
    return redirect(url_for("main.student_orders"))


@main_bp.route("/orders/<int:order_id>/receive", methods=["POST"])
@role_required("student")
def student_receive_order(order_id):
    """Students cannot self-confirm collection; staff must verify the 4-digit pickup OTP."""
    flash("Students cannot confirm their own pickup. Please show your 4-digit OTP to canteen staff at the counter.", "error")
    return redirect(url_for("main.student_orders"))


@main_bp.route("/orders/<int:order_id>/reorder", methods=["POST"])
@role_required("student")
def reorder(order_id):
    order = fetch_one(
        "SELECT id FROM orders WHERE id = %s AND student_id = %s",
        (order_id, g.user["id"]),
    )
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("main.student_orders"))

    items = fetch_all(
        """
        SELECT oi.menu_item_id, oi.quantity, mi.stock_qty, mi.is_sold_out
        FROM order_items oi
        JOIN menu_items mi ON mi.id = oi.menu_item_id
        WHERE oi.order_id = %s
        """,
        (order_id,),
    )
    cart = {}
    added = 0
    for row in items:
        if not row["is_sold_out"] and int(row["stock_qty"]) > 0:
            qty = min(int(row["quantity"]), int(row["stock_qty"]))
            cart[str(row["menu_item_id"])] = qty
            added += 1

    session["cart"] = cart
    if added:
        flash("Items added to your cart! Choose a 15-minute pickup slot to confirm.", "success")
        return redirect(url_for("main.view_cart"))
    flash("Those dishes are currently sold out.", "error")
    return redirect(url_for("main.student_menu"))


@main_bp.route("/orders/<int:order_id>/rate", methods=["POST"])
@role_required("student")
def rate_order(order_id):
    order = fetch_one(
        "SELECT id, status FROM orders WHERE id = %s AND student_id = %s",
        (order_id, g.user["id"]),
    )
    if not order or order["status"] != "collected":
        flash("You can rate an order after staff verifies your pickup OTP.", "error")
        return redirect(url_for("main.student_orders"))

    rating_str = request.form.get("rating", "").strip()
    comment = request.form.get("comment", "").strip()[:255]
    if not rating_str.isdigit() or not (1 <= int(rating_str) <= 5):
        flash("Please choose a rating between 1 and 5 stars.", "error")
        return redirect(url_for("main.student_orders"))

    try:
        execute(
            "INSERT INTO feedback (order_id, student_id, rating, comment) VALUES (%s, %s, %s, %s)",
            (order_id, g.user["id"], int(rating_str), comment),
        )
        flash("Thanks for rating your meal! Your feedback updates live dish ratings.", "success")
    except DBIntegrityError:
        flash("You already rated this order.", "info")

    return redirect(url_for("main.student_orders"))


# =========================================================
# Staff & Manager: Multi-Canteen Live Queues, OTP Handover, Stock & Feedback
# =========================================================

@main_bp.route("/staff/dashboard")
@role_required("staff", "manager")
def staff_dashboard():
    today = _today_str()
    ensure_today_slots()

    canteen_param = request.args.get("canteen", "all").strip()
    selected_canteen_id = int(canteen_param) if canteen_param.isdigit() else None
    canteens = _get_canteens_with_workload()
    selected_canteen = next((c for c in canteens if c["id"] == selected_canteen_id), None)

    canteen_clause = "AND orders.canteen_id = %s" if selected_canteen_id else ""
    base_params = (today, selected_canteen_id) if selected_canteen_id else (today,)

    orders_today = fetch_one(
        f"""
        SELECT COUNT(*) AS total
        FROM orders
        JOIN pickup_slots ON pickup_slots.id = orders.slot_id
        WHERE pickup_slots.slot_date = %s AND orders.status != 'cancelled' {canteen_clause}
        """,
        base_params,
    )["total"]

    revenue_row = fetch_one(
        f"""
        SELECT COALESCE(SUM(orders.total_amount), 0) AS total
        FROM orders
        JOIN pickup_slots ON pickup_slots.id = orders.slot_id
        WHERE pickup_slots.slot_date = %s AND orders.status != 'cancelled' {canteen_clause}
        """,
        base_params,
    )
    revenue_today = float(revenue_row["total"] or 0)

    if selected_canteen_id:
        waste_row = fetch_one(
            """
            SELECT COALESCE(SUM(wl.quantity_wasted), 0) AS qty, COALESCE(SUM(wl.weight_kg), 0) AS kg
            FROM waste_logs wl
            JOIN menu_items mi ON mi.id = wl.menu_item_id
            WHERE wl.waste_date = %s AND mi.canteen_id = %s
            """,
            (today, selected_canteen_id),
        )
        rating_row = fetch_one(
            """
            SELECT ROUND(AVG(f.rating), 1) AS avg_rating, COUNT(*) AS total
            FROM feedback f
            JOIN orders o ON o.id = f.order_id
            WHERE o.canteen_id = %s
            """,
            (selected_canteen_id,),
        )
    else:
        waste_row = fetch_one(
            "SELECT COALESCE(SUM(quantity_wasted), 0) AS qty, COALESCE(SUM(weight_kg), 0) AS kg FROM waste_logs WHERE waste_date = %s",
            (today,),
        )
        rating_row = fetch_one("SELECT ROUND(AVG(rating), 1) AS avg_rating, COUNT(*) AS total FROM feedback")

    order_canteen_clause = "AND o.canteen_id = %s" if selected_canteen_id else ""
    live_params = (today, selected_canteen_id) if selected_canteen_id else (today,)
    live_orders = fetch_all(
        f"""
        SELECT o.id, o.canteen_id, o.status, o.total_amount, o.pickup_otp,
               o.payment_method, o.payment_status, o.upi_txn_ref, o.estimated_wait_mins,
               c.name AS canteen_name, c.badge_color AS canteen_color,
               u.full_name AS student_name, u.campus_id,
               ps.slot_date, ps.start_time, ps.end_time
        FROM orders o
        JOIN canteens c ON c.id = o.canteen_id
        JOIN users u ON u.id = o.student_id
        JOIN pickup_slots ps ON ps.id = o.slot_id
        WHERE (o.status IN ('confirmed', 'preparing', 'ready') OR ps.slot_date = %s)
          {order_canteen_clause}
        ORDER BY CASE o.status
            WHEN 'confirmed' THEN 1
            WHEN 'preparing' THEN 2
            WHEN 'ready' THEN 3
            WHEN 'collected' THEN 4
            ELSE 5
        END, o.id DESC
        LIMIT 40
        """,
        live_params,
    )
    for order in live_orders:
        order["slot_label"] = _format_slot_label(order["start_time"], order["end_time"], order["slot_date"])
        order["items"] = fetch_all(
            """
            SELECT oi.quantity, mi.name, mi.image_url
            FROM order_items oi
            JOIN menu_items mi ON mi.id = oi.menu_item_id
            WHERE oi.order_id = %s
            """,
            (order["id"],),
        )

    if selected_canteen_id:
        menu_items = fetch_all(
            """
            SELECT mi.id, mi.canteen_id, c.name AS canteen_name, mi.name, mi.category,
                   mi.price, mi.image_url, mi.stock_qty, mi.is_sold_out, mi.is_surplus
            FROM menu_items mi
            JOIN canteens c ON c.id = mi.canteen_id
            WHERE mi.is_active = 1 AND mi.canteen_id = %s
            ORDER BY mi.is_sold_out DESC, mi.stock_qty ASC, mi.name
            """,
            (selected_canteen_id,),
        )
    else:
        menu_items = fetch_all(
            """
            SELECT mi.id, mi.canteen_id, c.name AS canteen_name, mi.name, mi.category,
                   mi.price, mi.image_url, mi.stock_qty, mi.is_sold_out, mi.is_surplus
            FROM menu_items mi
            JOIN canteens c ON c.id = mi.canteen_id
            WHERE mi.is_active = 1
            ORDER BY mi.is_sold_out DESC, mi.stock_qty ASC, mi.name
            """
        )

    recent_waste = fetch_all(
        """
        SELECT wl.id, wl.waste_date, wl.quantity_wasted, wl.weight_kg, wl.reason, wl.notes,
               mi.name AS dish_name, c.name AS canteen_name
        FROM waste_logs wl
        JOIN menu_items mi ON mi.id = wl.menu_item_id
        JOIN canteens c ON c.id = mi.canteen_id
        ORDER BY wl.id DESC
        LIMIT 8
        """
    )

    recent_feedback = fetch_all(
        """
        SELECT f.id, f.order_id, f.rating, f.comment, f.created_at,
               u.full_name AS student_name, c.name AS canteen_name
        FROM feedback f
        JOIN users u ON u.id = f.student_id
        JOIN orders o ON o.id = f.order_id
        JOIN canteens c ON c.id = o.canteen_id
        ORDER BY f.id DESC
        LIMIT 6
        """
    )

    announcement = fetch_one(
        "SELECT message, created_at FROM announcements WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
    )

    return render_template(
        "temp/staff_home.html",
        canteens=canteens,
        selected_canteen_id=selected_canteen_id,
        selected_canteen=selected_canteen,
        orders_today=orders_today,
        revenue_today=revenue_today,
        waste_qty_today=int(waste_row["qty"] or 0),
        waste_kg_today=float(waste_row["kg"] or 0),
        avg_rating=rating_row["avg_rating"] or "-",
        rating_count=rating_row["total"] or 0,
        live_orders=live_orders,
        menu_items=menu_items,
        recent_waste=recent_waste,
        recent_feedback=recent_feedback,
        announcement=announcement,
        low_stock_limit=Config.LOW_STOCK_LIMIT,
    )


@main_bp.route("/staff/orders/<int:order_id>/status", methods=["POST"])
@role_required("staff", "manager")
def staff_update_order_status(order_id):
    order = fetch_one("SELECT id, status, pickup_otp, canteen_id FROM orders WHERE id = %s", (order_id,))
    redirect_canteen = request.form.get("canteen_filter", "").strip()
    back_url = (
        url_for("main.staff_dashboard", canteen=redirect_canteen)
        if redirect_canteen.isdigit()
        else url_for("main.staff_dashboard")
    )

    if not order:
        flash("Order not found.", "error")
        return redirect(back_url)

    next_status = STATUS_FLOW.get(order["status"])
    if not next_status:
        flash("That order cannot be advanced further.", "info")
        return redirect(back_url)

    if next_status == "collected":
        entered_otp = request.form.get("pickup_otp", "").strip()
        if not entered_otp or not secrets.compare_digest(entered_otp, str(order["pickup_otp"])):
            flash(f"Incorrect pickup OTP for Order #{order_id}. Ask the student for their 4-digit OTP before handover.", "error")
            return redirect(back_url)

    order_meta = fetch_one(
        """
        SELECT o.student_id, o.pickup_otp, c.name AS canteen_name,
               ps.slot_date, ps.start_time, ps.end_time
        FROM orders o
        JOIN canteens c ON c.id = o.canteen_id
        JOIN pickup_slots ps ON ps.id = o.slot_id
        WHERE o.id = %s
        """,
        (order_id,),
    )
    slot_lbl = _format_slot_label(order_meta["start_time"], order_meta["end_time"], order_meta["slot_date"]) if order_meta else ""
    c_name = order_meta["canteen_name"] if order_meta else Config.CANTEEN_NAME
    now_ist = campus_now().strftime("%Y-%m-%d %H:%M:%S")

    if next_status == "preparing":
        execute("UPDATE orders SET status = 'preparing', preparing_at = %s WHERE id = %s", (now_ist, order_id))
        if order_meta:
            _notify_user(
                order_meta["student_id"],
                f"Order #{order_id} ACCEPTED at {c_name}! Preparing for your {slot_lbl} pickup slot.",
                "slot_reminder",
            )
        flash(f"Order #{order_id} accepted and moved to Preparing!", "success")
    elif next_status == "ready":
        execute("UPDATE orders SET status = 'ready', ready_at = %s WHERE id = %s", (now_ist, order_id))
        if order_meta:
            _notify_user(
                order_meta["student_id"],
                f"Order #{order_id} is READY for pickup at {c_name} ({slot_lbl})! Show OTP {order_meta['pickup_otp']} at the counter.",
                "order_ready",
            )
        flash(f"Order #{order_id} marked READY! Real-time notification sent to student.", "success")
    elif next_status == "collected":
        execute(
            """
            UPDATE orders
            SET status = 'collected',
                payment_status = CASE WHEN payment_method = 'counter' THEN 'paid' ELSE payment_status END,
                collected_at = %s
            WHERE id = %s
            """,
            (now_ist, order_id),
        )
        if order_meta:
            _notify_user(
                order_meta["student_id"],
                f"Order #{order_id} OTP verified & collected at {c_name}! Enjoy your meal and leave a quick rating.",
                "order_ready",
            )
        flash(f"Order #{order_id} OTP verified! Handed over and marked Collected.", "success")

    return redirect(back_url)


@main_bp.route("/staff/orders/<int:order_id>/reject", methods=["POST"])
@role_required("staff", "manager")
def staff_reject_order(order_id):
    order = fetch_one("SELECT id, student_id, status, payment_status FROM orders WHERE id = %s", (order_id,))
    if not order or order["status"] in ("collected", "cancelled"):
        flash("That order cannot be rejected.", "info")
        return redirect(url_for("main.staff_dashboard"))

    items = fetch_all("SELECT menu_item_id, quantity FROM order_items WHERE order_id = %s", (order_id,))
    for row in items:
        execute(
            "UPDATE menu_items SET stock_qty = stock_qty + %s, is_sold_out = 0 WHERE id = %s",
            (int(row["quantity"]), row["menu_item_id"]),
        )
    new_pay = "refunded" if order["payment_status"] == "paid" else order["payment_status"]
    execute(
        "UPDATE orders SET status = 'cancelled', payment_status = %s, cancelled_at = %s WHERE id = %s",
        (new_pay, campus_now().strftime("%Y-%m-%d %H:%M:%S"), order_id),
    )
    _notify_user(order["student_id"], f"Order #{order_id} was cancelled by the canteen. Stock has been restored.", "order_cancelled")
    flash(f"Order #{order_id} rejected and stock restored.", "info")
    return redirect(url_for("main.staff_dashboard"))


@main_bp.route("/staff/menu/<int:item_id>/stock", methods=["POST"])
@role_required("staff", "manager")
def staff_update_stock(item_id):
    action = request.form.get("action", "restock")
    item = fetch_one("SELECT id, name, stock_qty, is_sold_out FROM menu_items WHERE id = %s", (item_id,))
    if not item:
        flash("Menu item not found.", "error")
        return redirect(url_for("main.staff_dashboard"))

    if action == "restock":
        new_qty = int(item["stock_qty"]) + 10
        execute("UPDATE menu_items SET stock_qty = %s, is_sold_out = 0 WHERE id = %s", (new_qty, item_id))
        flash(f"Restocked {item['name']} (+10, now {new_qty} left).", "success")
    elif action == "toggle_sold_out":
        new_flag = 0 if item["is_sold_out"] else 1
        new_qty = max(int(item["stock_qty"]), 5) if new_flag == 0 and int(item["stock_qty"]) == 0 else int(item["stock_qty"])
        execute("UPDATE menu_items SET is_sold_out = %s, stock_qty = %s WHERE id = %s", (new_flag, new_qty, item_id))
        status_text = "Sold Out" if new_flag else "Available"
        flash(f"{item['name']} marked as {status_text}.", "info")
    elif action == "toggle_surplus":
        cur_s = fetch_one("SELECT is_surplus FROM menu_items WHERE id = %s", (item_id,))
        new_s = 0 if (cur_s and cur_s["is_surplus"]) else 1
        execute("UPDATE menu_items SET is_surplus = %s WHERE id = %s", (new_s, item_id))
        if new_s:
            flash(f"{item['name']} added to Zero-Waste Rescue Deals (-25% OFF)!", "success")
        else:
            flash(f"{item['name']} returned to regular price.", "info")

    return redirect(request.referrer or url_for("main.staff_dashboard"))


@main_bp.route("/staff/announcements", methods=["POST"])
@role_required("staff", "manager")
def staff_post_announcement():
    message = request.form.get("message", "").strip()
    canteen_id_raw = request.form.get("canteen_id", "").strip()
    canteen_id = int(canteen_id_raw) if canteen_id_raw.isdigit() else None
    if not message or len(message) > 240:
        flash("Please enter an announcement (up to 240 characters).", "error")
        return redirect(url_for("main.staff_dashboard"))

    execute("UPDATE announcements SET is_active = 0 WHERE is_active = 1")
    execute(
        "INSERT INTO announcements (canteen_id, message, created_by, is_active) VALUES (%s, %s, %s, 1)",
        (canteen_id, message, g.user["id"]),
    )
    flash("Live announcement updated for students.", "success")
    return redirect(url_for("main.staff_dashboard"))


@main_bp.route("/staff/waste", methods=["POST"])
@role_required("staff", "manager")
def staff_log_waste():
    item_id = request.form.get("menu_item_id", "")
    qty_str = request.form.get("quantity_wasted", "")
    weight_str = request.form.get("weight_kg", "").strip()
    reason = request.form.get("reason", "overproduction")
    notes = request.form.get("notes", "").strip()[:255]

    if not item_id.isdigit() or not qty_str.isdigit() or int(qty_str) <= 0:
        flash("Please select a dish and enter a valid wasted quantity.", "error")
        return redirect(url_for("main.staff_dashboard"))

    if reason not in VALID_WASTE_REASONS:
        reason = "overproduction"

    weight_val = None
    if weight_str:
        try:
            weight_val = round(float(weight_str), 2)
        except ValueError:
            weight_val = None

    execute(
        """
        INSERT INTO waste_logs (menu_item_id, waste_date, quantity_wasted, weight_kg, reason, notes, logged_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (int(item_id), _today_str(), int(qty_str), weight_val, reason, notes, g.user["id"]),
    )
    flash("Food waste entry logged.", "success")
    return redirect(url_for("main.staff_dashboard"))


@main_bp.route("/manager/settings")
@role_required("manager")
def manager_settings():
    slots, slot_day_note = _get_available_slots_with_counts()
    canteens = _get_canteens_with_workload()
    waste_summary = fetch_all(
        """
        SELECT reason, SUM(quantity_wasted) AS total_qty, COALESCE(SUM(weight_kg), 0) AS total_kg
        FROM waste_logs
        GROUP BY reason
        ORDER BY total_qty DESC
        """
    )
    top_dishes = fetch_all(
        """
        SELECT mi.name, SUM(oi.quantity) AS sold_qty, SUM(oi.quantity * oi.unit_price) AS revenue
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id AND o.status != 'cancelled'
        JOIN menu_items mi ON mi.id = oi.menu_item_id
        GROUP BY mi.id, mi.name
        ORDER BY sold_qty DESC
        LIMIT 6
        """
    )
    menu_items = fetch_all(
        """
        SELECT mi.id, mi.name, mi.category, mi.price, mi.food_type, mi.image_url,
               mi.stock_qty, mi.is_sold_out, c.name AS canteen_name
        FROM menu_items mi
        JOIN canteens c ON c.id = mi.canteen_id
        WHERE mi.is_active = 1
        ORDER BY c.id, mi.category, mi.name
        """
    )
    staff_users = fetch_all(
        "SELECT id, full_name, email, campus_id, role FROM users WHERE role IN ('staff', 'manager') ORDER BY role, full_name"
    )
    return render_template(
        "temp/manager_only.html",
        canteens=canteens,
        slots=slots,
        slot_day_note=slot_day_note,
        waste_summary=waste_summary,
        top_dishes=top_dishes,
        menu_items=menu_items,
        staff_users=staff_users,
    )


@main_bp.route("/manager/slots/<int:slot_id>/capacity", methods=["POST"])
@role_required("manager")
def manager_update_slot_capacity(slot_id):
    cap_str = request.form.get("capacity", "")
    if not cap_str.isdigit() or not (1 <= int(cap_str) <= 60):
        flash("Capacity must be a number between 1 and 60.", "error")
        return redirect(url_for("main.manager_settings"))

    execute("UPDATE pickup_slots SET capacity = %s WHERE id = %s", (int(cap_str), slot_id))
    flash("Pickup slot capacity updated.", "success")
    return redirect(url_for("main.manager_settings"))


# =========================================================
# Live JSON APIs (Real-time Notifications, Wait Time & Order Polling)
# =========================================================

@main_bp.route("/api/notifications")
@login_required
def api_notifications():
    rows = fetch_all(
        """
        SELECT id, message, type, is_read, created_at
        FROM notifications
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT 12
        """,
        (g.user["id"],),
    )
    unread = sum(1 for r in rows if not r["is_read"])
    return jsonify({"unread": unread, "notifications": rows})


@main_bp.route("/api/notifications/read", methods=["POST"])
@login_required
def api_notifications_read():
    execute("UPDATE notifications SET is_read = 1 WHERE user_id = %s AND is_read = 0", (g.user["id"],))
    return jsonify({"ok": True})


@main_bp.route("/api/orders/status")
@role_required("student")
def api_student_orders_status():
    rows = fetch_all(
        """
        SELECT o.id, o.canteen_id, o.status, o.payment_status, o.pickup_otp,
               c.name AS canteen_name, ps.slot_date, ps.start_time, ps.end_time
        FROM orders o
        JOIN canteens c ON c.id = o.canteen_id
        JOIN pickup_slots ps ON ps.id = o.slot_id
        WHERE o.student_id = %s
        ORDER BY o.id DESC
        LIMIT 12
        """,
        (g.user["id"],),
    )
    result = []
    for r in rows:
        wait = _estimate_wait_minutes(r["canteen_id"], before_order_id=r["id"], status=r["status"])
        result.append({
            "id": r["id"],
            "status": r["status"],
            "payment_status": r["payment_status"],
            "pickup_otp": r["pickup_otp"],
            "canteen_name": r["canteen_name"],
            "slot_label": _format_slot_label(r["start_time"], r["end_time"], r["slot_date"]),
            "live_wait_mins": wait["wait_mins"],
            "queue_ahead": wait["active_orders"],
            "load_label": wait["load_label"],
        })
    return jsonify({"orders": result})


# =========================================================
# Manager Menu CRUD & Staff Account Management
# =========================================================

@main_bp.route("/manager/menu/add", methods=["POST"])
@role_required("manager")
def manager_add_menu_item():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    category = request.form.get("category", "Snacks").strip()
    canteen_id_str = request.form.get("canteen_id", "1").strip()
    price_str = request.form.get("price", "").strip()
    food_type = request.form.get("food_type", "veg").strip()
    allergens = request.form.get("allergens", "").strip()[:120]
    prep_str = request.form.get("prep_time_minutes", "10").strip()
    stock_str = request.form.get("stock_qty", "20").strip()
    is_featured = 1 if request.form.get("is_featured") == "1" else 0

    if not name or not description:
        flash("Please enter a dish name and short description.", "error")
        return redirect(url_for("main.manager_settings"))

    try:
        price = round(float(price_str), 2)
        prep_time = max(1, int(prep_str))
        stock = max(0, int(stock_str))
        canteen_id = int(canteen_id_str) if canteen_id_str.isdigit() else 1
    except ValueError:
        flash("Price, prep time, and stock must be valid numbers.", "error")
        return redirect(url_for("main.manager_settings"))

    if food_type not in ("veg", "non_veg", "egg"):
        food_type = "veg"

    image_url = _save_dish_photo(
        request.files.get("photo_file"),
        request.form.get("image_url", "").strip(),
        category,
    )

    execute(
        """
        INSERT INTO menu_items
            (canteen_id, name, description, category, price, food_type, allergens,
             prep_time_minutes, image_url, is_featured, prepared_batch_qty, stock_qty, is_sold_out, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
        """,
        (canteen_id, name, description, category, price, food_type, allergens, prep_time, image_url, is_featured, stock, stock, 1 if stock == 0 else 0),
    )
    flash(f"Added '{name}' to the live campus menu!", "success")
    return redirect(url_for("main.manager_settings"))


@main_bp.route("/manager/menu/<int:item_id>/update", methods=["POST"])
@role_required("manager")
def manager_update_menu_item(item_id):
    price_str = request.form.get("price", "").strip()
    stock_str = request.form.get("stock_qty", "").strip()
    try:
        price = round(float(price_str), 2)
        stock = max(0, int(stock_str))
    except ValueError:
        flash("Invalid price or stock number.", "error")
        return redirect(url_for("main.manager_settings"))

    photo_file = request.files.get("photo_file")
    if photo_file and getattr(photo_file, "filename", ""):
        new_img = _save_dish_photo(photo_file)
        execute(
            "UPDATE menu_items SET price = %s, stock_qty = %s, is_sold_out = %s, image_url = %s WHERE id = %s",
            (price, stock, 1 if stock == 0 else 0, new_img, item_id),
        )
    else:
        execute(
            "UPDATE menu_items SET price = %s, stock_qty = %s, is_sold_out = %s WHERE id = %s",
            (price, stock, 1 if stock == 0 else 0, item_id),
        )
    flash("Menu item updated.", "success")
    return redirect(url_for("main.manager_settings"))


@main_bp.route("/manager/menu/<int:item_id>/archive", methods=["POST"])
@role_required("manager")
def manager_archive_menu_item(item_id):
    execute("UPDATE menu_items SET is_active = 0 WHERE id = %s", (item_id,))
    flash("Dish removed from the active menu.", "info")
    return redirect(url_for("main.manager_settings"))


@main_bp.route("/manager/staff/add", methods=["POST"])
@role_required("manager")
def manager_add_staff():
    from werkzeug.security import generate_password_hash
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    campus_id = request.form.get("campus_id", "").strip().upper()
    role = request.form.get("role", "staff")
    password = request.form.get("password", "")

    if role not in ("staff", "manager"):
        role = "staff"
    if not full_name or "@" not in email or len(password) < Config.MIN_PASSWORD_LENGTH:
        flash(f"Enter name, valid email, and password (at least {Config.MIN_PASSWORD_LENGTH} characters).", "error")
        return redirect(url_for("main.manager_settings"))

    try:
        execute(
            """
            INSERT INTO users (full_name, email, campus_id, password_hash, role, dietary_preference)
            VALUES (%s, %s, %s, %s, %s, 'any')
            """,
            (full_name, email, campus_id or None, generate_password_hash(password, method=Config.PASSWORD_HASH_METHOD), role),
        )
        flash(f"Created {role} account for {full_name} ({email}).", "success")
    except DBIntegrityError:
        flash("An account with that email or campus ID already exists.", "error")

    return redirect(url_for("main.manager_settings"))