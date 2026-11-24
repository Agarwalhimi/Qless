"""
config.py - Every setting for Qless lives here.

Secrets (passwords, keys) come from the .env file.
Business rules (slot length, low-stock limit, etc.) are plain numbers
you can safely edit below.
"""

import os
from datetime import timedelta

from dotenv import load_dotenv

# Read the .env file so os.getenv() can see its values
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    # ---------- Flask basics ----------
    SECRET_KEY = os.getenv("SECRET_KEY") or "qless-local-development-only-change-before-deploy"
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

    # ---------- Session / cookie safety ----------
    SESSION_COOKIE_HTTPONLY = True      # JavaScript cannot read the login cookie
    SESSION_COOKIE_SAMESITE = "Lax"     # Helps block cross-site request tricks
    # Automatically use Secure cookies when deployed on Render (HTTPS)
    SESSION_COOKIE_SECURE = os.getenv("RENDER", "") != "" or os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)   # Stay logged in for one shift

    # ---------- Passwords and recovery ----------
    MIN_PASSWORD_LENGTH = 8             # Shortest password allowed at signup
    PASSWORD_HASH_METHOD = "pbkdf2:sha256"  # Fast, low-RAM, works on every server
    RESET_OTP_MINUTES = 10              # A password-reset code works for 10 minutes
    SHOW_RESET_OTP_ON_SCREEN = True     # Demo mode: show the code on screen (no email server yet)

    # ---------- Database settings ----------
    DB_ENGINE = os.getenv("DB_ENGINE", "auto").strip().lower()
    SQLITE_PATH = os.getenv("SQLITE_PATH", os.path.join(BASE_DIR, "qless.db"))

    DB_HOST = os.getenv("DB_HOST", "")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_NAME = os.getenv("DB_NAME", "qless_db")

    # ---------- Pickup slot rules (IST) ----------
    SLOT_LENGTH_MINUTES = 15            # Each pickup slot is 15 minutes
    DEFAULT_SLOT_CAPACITY = 12          # Max orders per slot per canteen
    CANTEEN_OPEN_HOUR = int(os.getenv("CANTEEN_OPEN_HOUR", "7"))    # 07:00 IST
    CANTEEN_CLOSE_HOUR = int(os.getenv("CANTEEN_CLOSE_HOUR", "22")) # 22:00 IST
    TIMEZONE = os.getenv("CANTEEN_TIMEZONE", "Asia/Kolkata")
    CANTEEN_NAME = os.getenv("CANTEEN_NAME", "Main Campus Canteen")

    # ---------- UPI payment verification ----------
    UPI_MERCHANT_VPA = os.getenv("UPI_MERCHANT_VPA", "qless.canteen@upi")
    UPI_MERCHANT_NAME = os.getenv("UPI_MERCHANT_NAME", "QLess Campus Canteen")
    UPI_WEBHOOK_SECRET = os.getenv("UPI_WEBHOOK_SECRET", SECRET_KEY)

    # ---------- Order rules ----------
    CANCEL_BEFORE_SLOT_MINUTES = 10     # Student can cancel until 10 min before pickup
    UNCOLLECTED_AFTER_MINUTES = 30      # Flag order for staff after 30 min uncollected
    LOW_STOCK_LIMIT = 5                 # Show "Low Stock" badge at 5 or fewer left

    # ---------- Live updates ----------
    POLL_INTERVAL_SECONDS = 6           # Fast live polling for ready alerts & queue updates