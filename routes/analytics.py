"""
analytics.py - Analytics Dashboard, Historical Demand Prediction & CSV Exports.

Provides:
  - GET /analytics                   -> Interactive Chart.js dashboard + Demand Prediction (staff & manager)
  - GET /analytics/export/orders.csv -> Downloadable orders report
  - GET /analytics/export/waste.csv  -> Downloadable food-waste report
"""

import csv
import io
from datetime import timedelta

from flask import Blueprint, Response, render_template, request

from db import campus_now, fetch_all, fetch_one
from routes.access import role_required

analytics_bp = Blueprint("analytics", __name__)


def _build_demand_predictions(canteen_id=None):
    """
    Predict tomorrow's portion demand per dish using weighted historical order data:
      - 14-day historical daily average
      - Recent 3-day momentum average
      - Waste ratio penalty when overproduction occurs
    """
    today = campus_now().date()
    days_14_ago = (today - timedelta(days=13)).isoformat()
    days_3_ago = (today - timedelta(days=2)).isoformat()

    canteen_filter = "AND mi.canteen_id = %s" if canteen_id else ""
    params = (days_14_ago, days_3_ago, days_14_ago, canteen_id) if canteen_id else (days_14_ago, days_3_ago, days_14_ago)

    rows = fetch_all(
        f"""
        SELECT mi.id, mi.name, mi.category, mi.prepared_batch_qty AS current_batch,
               c.name AS canteen_name,
               COALESCE(SUM(CASE WHEN ps.slot_date >= %s THEN oi.quantity ELSE 0 END), 0) AS sold_14d,
               COUNT(DISTINCT CASE WHEN ps.slot_date >= %s THEN ps.slot_date END) AS active_days_3d,
               COALESCE(SUM(CASE WHEN ps.slot_date >= %s THEN oi.quantity ELSE 0 END), 0) AS sold_3d,
               COUNT(DISTINCT ps.slot_date) AS active_days_14d,
               COALESCE((SELECT SUM(wl.quantity_wasted) FROM waste_logs wl WHERE wl.menu_item_id = mi.id), 0) AS wasted_total
        FROM menu_items mi
        JOIN canteens c ON c.id = mi.canteen_id
        LEFT JOIN order_items oi ON oi.menu_item_id = mi.id
        LEFT JOIN orders o ON o.id = oi.order_id AND o.status != 'cancelled'
        LEFT JOIN pickup_slots ps ON ps.id = o.slot_id
        WHERE mi.is_active = 1 {canteen_filter}
        GROUP BY mi.id, mi.name, mi.category, mi.prepared_batch_qty, c.name
        HAVING sold_14d > 0 OR wasted_total > 0
        ORDER BY sold_14d DESC, mi.name
        LIMIT 10
        """,
        params,
    )

    predictions = []
    for r in rows:
        sold_14d = int(r["sold_14d"] or 0)
        sold_3d = int(r["sold_3d"] or 0)
        wasted = int(r["wasted_total"] or 0)
        days_14 = max(1, int(r["active_days_14d"] or 1))
        days_3 = max(1, int(r["active_days_3d"] or 1))

        avg_14 = sold_14d / days_14
        avg_3 = sold_3d / days_3 if sold_3d > 0 else avg_14

        # Weighted forecast: 60% recent 3-day momentum + 40% 14-day baseline
        raw_forecast = 0.6 * avg_3 + 0.4 * avg_14
        # Adjust slightly upward (+10% buffer) unless chronic waste exists
        if wasted >= 5:
            predicted_tomorrow = max(1, int(round(raw_forecast * 0.85)))
            trend = "Reduce Waste (-15%)"
            level = "warning"
        elif avg_3 > avg_14 * 1.15:
            predicted_tomorrow = max(2, int(round(raw_forecast * 1.15)))
            trend = "Rising Demand (+15%)"
            level = "success"
        else:
            predicted_tomorrow = max(1, int(round(raw_forecast * 1.05)))
            trend = "Steady Demand"
            level = "info"

        # Recommended prep batch scales the daily forecast safely
        recommended_batch = max(predicted_tomorrow + 2, int(round(predicted_tomorrow * 1.2)))

        predictions.append({
            "name": r["name"],
            "canteen_name": r["canteen_name"],
            "category": r["category"],
            "sold_14d": sold_14d,
            "avg_daily": round(avg_14, 1),
            "predicted_tomorrow": predicted_tomorrow,
            "recommended_batch": recommended_batch,
            "wasted": wasted,
            "trend": trend,
            "level": level,
        })
    return predictions


@analytics_bp.route("/analytics")
@role_required("staff", "manager")
def dashboard():
    today = campus_now().date()
    seven_days_ago = (today - timedelta(days=6)).isoformat()

    canteen_param = request.args.get("canteen", "all").strip()
    selected_canteen_id = int(canteen_param) if canteen_param.isdigit() else None
    canteens = fetch_all("SELECT id, name, badge_color FROM canteens WHERE is_active = 1 ORDER BY id")

    canteen_order_filter = "AND o.canteen_id = %s" if selected_canteen_id else ""
    order_params = (selected_canteen_id,) if selected_canteen_id else ()

    totals = fetch_one(
        f"""
        SELECT COUNT(*) AS total_orders,
               COALESCE(SUM(o.total_amount), 0) AS total_revenue
        FROM orders o
        WHERE o.status != 'cancelled' {canteen_order_filter}
        """,
        order_params,
    )
    if selected_canteen_id:
        waste_totals = fetch_one(
            """
            SELECT COALESCE(SUM(wl.quantity_wasted), 0) AS total_qty,
                   COALESCE(SUM(wl.weight_kg), 0) AS total_kg
            FROM waste_logs wl
            JOIN menu_items mi ON mi.id = wl.menu_item_id
            WHERE mi.canteen_id = %s
            """,
            (selected_canteen_id,),
        )
        rating_totals = fetch_one(
            """
            SELECT ROUND(AVG(f.rating), 1) AS avg_rating, COUNT(*) AS total_reviews
            FROM feedback f
            JOIN orders o ON o.id = f.order_id
            WHERE o.canteen_id = %s
            """,
            (selected_canteen_id,),
        )
    else:
        waste_totals = fetch_one(
            "SELECT COALESCE(SUM(quantity_wasted), 0) AS total_qty, COALESCE(SUM(weight_kg), 0) AS total_kg FROM waste_logs"
        )
        rating_totals = fetch_one(
            "SELECT ROUND(AVG(rating), 1) AS avg_rating, COUNT(*) AS total_reviews FROM feedback"
        )

    cancelled_row = fetch_one(
        f"SELECT COUNT(*) AS total FROM orders o WHERE o.status = 'cancelled' {canteen_order_filter}",
        order_params,
    )

    # 7-day Orders & Revenue trend (IST)
    daily_params = (seven_days_ago, selected_canteen_id) if selected_canteen_id else (seven_days_ago,)
    daily_rows = fetch_all(
        f"""
        SELECT ps.slot_date AS day,
               COUNT(o.id) AS orders_count,
               COALESCE(SUM(o.total_amount), 0) AS revenue
        FROM pickup_slots ps
        JOIN orders o ON o.slot_id = ps.id AND o.status != 'cancelled'
        WHERE ps.slot_date >= %s {canteen_order_filter}
        GROUP BY ps.slot_date
        ORDER BY ps.slot_date
        """,
        daily_params,
    )
    daily_map = {str(r["day"]): r for r in daily_rows}
    trend_labels = []
    trend_orders = []
    trend_revenue = []
    for offset in range(6, -1, -1):
        d = (today - timedelta(days=offset)).isoformat()
        trend_labels.append(d[5:])
        row = daily_map.get(d)
        trend_orders.append(int(row["orders_count"]) if row else 0)
        trend_revenue.append(round(float(row["revenue"]), 2) if row else 0.0)

    # Top Dishes: Prepared Batch vs Sold
    dish_canteen_filter = "AND mi.canteen_id = %s" if selected_canteen_id else ""
    dish_rows = fetch_all(
        f"""
        SELECT mi.name,
               mi.prepared_batch_qty AS prepared,
               COALESCE(SUM(oi.quantity), 0) AS sold,
               COALESCE((SELECT SUM(wl.quantity_wasted) FROM waste_logs wl WHERE wl.menu_item_id = mi.id), 0) AS wasted
        FROM menu_items mi
        LEFT JOIN order_items oi ON oi.menu_item_id = mi.id
        LEFT JOIN orders o ON o.id = oi.order_id AND o.status != 'cancelled'
        WHERE mi.is_active = 1 {dish_canteen_filter}
        GROUP BY mi.id, mi.name, mi.prepared_batch_qty
        ORDER BY sold DESC, mi.prepared_batch_qty DESC
        LIMIT 8
        """,
        order_params,
    )

    # Waste by reason
    waste_reason_rows = fetch_all(
        """
        SELECT reason,
               COALESCE(SUM(quantity_wasted), 0) AS qty,
               COALESCE(SUM(weight_kg), 0) AS kg
        FROM waste_logs
        GROUP BY reason
        ORDER BY qty DESC
        """
    )

    # Peak pickup slots
    peak_slots = fetch_all(
        f"""
        SELECT ps.start_time, ps.end_time, COUNT(o.id) AS total_orders
        FROM pickup_slots ps
        JOIN orders o ON o.slot_id = ps.id AND o.status != 'cancelled'
        WHERE 1=1 {canteen_order_filter}
        GROUP BY ps.start_time, ps.end_time
        ORDER BY total_orders DESC
        LIMIT 6
        """,
        order_params,
    )
    for slot in peak_slots:
        st = str(slot["start_time"])[:5]
        et = str(slot["end_time"])[:5]
        slot["label"] = f"{st} - {et}"

    # Canteen breakdown
    canteen_breakdown = fetch_all(
        """
        SELECT c.name, COUNT(o.id) AS total_orders, COALESCE(SUM(o.total_amount), 0) AS revenue
        FROM canteens c
        LEFT JOIN orders o ON o.canteen_id = c.id AND o.status != 'cancelled'
        GROUP BY c.id, c.name
        ORDER BY total_orders DESC
        """
    )

    # Historical Demand Prediction
    demand_predictions = _build_demand_predictions(canteen_id=selected_canteen_id)

    return render_template(
        "staff/analytics.html",
        canteens=canteens,
        selected_canteen_id=selected_canteen_id,
        total_orders=int(totals["total_orders"] or 0),
        total_revenue=round(float(totals["total_revenue"] or 0), 2),
        total_waste_qty=int(waste_totals["total_qty"] or 0),
        total_waste_kg=round(float(waste_totals["total_kg"] or 0), 2),
        avg_rating=rating_totals["avg_rating"] or "-",
        total_reviews=int(rating_totals["total_reviews"] or 0),
        cancelled_orders=int(cancelled_row["total"] or 0),
        trend_labels=trend_labels,
        trend_orders=trend_orders,
        trend_revenue=trend_revenue,
        dish_rows=dish_rows,
        waste_reason_rows=waste_reason_rows,
        peak_slots=peak_slots,
        canteen_breakdown=canteen_breakdown,
        demand_predictions=demand_predictions,
    )


@analytics_bp.route("/analytics/export/orders.csv")
@role_required("staff", "manager")
def export_orders_csv():
    rows = fetch_all(
        """
        SELECT o.id, c.name AS canteen_name, ps.slot_date, ps.start_time, ps.end_time,
               u.full_name AS student_name, u.campus_id,
               o.status, o.payment_method, o.payment_status, o.upi_txn_ref,
               o.estimated_wait_mins, o.total_amount, o.pickup_otp, o.created_at
        FROM orders o
        JOIN canteens c ON c.id = o.canteen_id
        JOIN users u ON u.id = o.student_id
        JOIN pickup_slots ps ON ps.id = o.slot_id
        ORDER BY o.id DESC
        """
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Order ID", "Canteen", "Slot Date", "Slot Start", "Slot End",
        "Student Name", "Campus ID", "Status",
        "Payment Method", "Payment Status", "UPI UTR", "Est. Wait (min)",
        "Total (INR)", "Pickup OTP", "Created At",
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["canteen_name"], r["slot_date"], r["start_time"], r["end_time"],
            r["student_name"], r["campus_id"] or "", r["status"],
            r["payment_method"], r["payment_status"], r["upi_txn_ref"] or "",
            r["estimated_wait_mins"], r["total_amount"], r["pickup_otp"], r["created_at"],
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=qless_orders_report.csv"},
    )


@analytics_bp.route("/analytics/export/waste.csv")
@role_required("staff", "manager")
def export_waste_csv():
    rows = fetch_all(
        """
        SELECT wl.id, wl.waste_date, c.name AS canteen_name, mi.name AS dish_name, mi.category,
               wl.quantity_wasted, wl.weight_kg, wl.reason, wl.notes, wl.created_at
        FROM waste_logs wl
        JOIN menu_items mi ON mi.id = wl.menu_item_id
        JOIN canteens c ON c.id = mi.canteen_id
        ORDER BY wl.waste_date DESC, wl.id DESC
        """
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Log ID", "Date", "Canteen", "Dish", "Category",
        "Portions Wasted", "Weight (kg)", "Reason", "Notes", "Logged At",
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["waste_date"], r["canteen_name"], r["dish_name"], r["category"],
            r["quantity_wasted"], r["weight_kg"] or "", r["reason"],
            r["notes"] or "", r["created_at"],
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=qless_waste_report.csv"},
    )
