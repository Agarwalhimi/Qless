"""
public.py - The public website (no login needed).

Pages:
  /                 Home (with live canteen crowd meter, real food photos, and Zero-Waste Rescue Deals)
  /how-it-works     How It Works
  /sustainability   Sustainability
  /about            About
  /contact          Contact
  /open-app         Sends a logged-in user to their own home screen
"""

from flask import Blueprint, g, redirect, render_template, url_for

from db import fetch_all, fetch_one
from routes.access import home_url_for_role

public_bp = Blueprint("public", __name__)


@public_bp.route("/")
def home():
    # Fetch live crowd/wait estimate + real food photos + surplus rescue deals safely
    popular_dishes = []
    rescue_deals = []
    active_queue = 0
    est_wait_mins = 4
    try:
        popular_dishes = fetch_all(
            """
            SELECT id, name, description, category, price, food_type,
                   prep_time_minutes, image_url, stock_qty, is_sold_out, is_surplus
            FROM menu_items
            WHERE is_active = 1 AND is_sold_out = 0
            ORDER BY is_surplus DESC, is_featured DESC, id ASC
            LIMIT 6
            """
        )
        rescue_deals = fetch_all(
            """
            SELECT id, name, category, price, image_url, stock_qty
            FROM menu_items
            WHERE is_active = 1 AND is_sold_out = 0 AND is_surplus = 1
            ORDER BY stock_qty DESC
            LIMIT 4
            """
        )
        q_row = fetch_one("SELECT COUNT(*) AS total FROM orders WHERE status IN ('confirmed', 'preparing')")
        active_queue = int(q_row["total"] or 0) if q_row else 0
        est_wait_mins = max(3, min(18, 3 + active_queue * 2))
    except Exception:
        pass

    return render_template(
        "public/home.html",
        popular_dishes=popular_dishes,
        rescue_deals=rescue_deals,
        active_queue=active_queue,
        est_wait_mins=est_wait_mins,
    )


@public_bp.route("/how-it-works")
def how_it_works():
    return render_template("public/how_it_works.html")


@public_bp.route("/sustainability")
def sustainability():
    return render_template("public/sustainability.html")


@public_bp.route("/about")
def about():
    return render_template("public/about.html")


@public_bp.route("/contact")
def contact():
    return render_template("public/contact.html")


@public_bp.route("/open-app")
def open_app():
    if g.user is None:
        return redirect(url_for("auth.login"))
    return redirect(home_url_for_role(g.user["role"]))
