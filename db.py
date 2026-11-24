"""
db.py - The ONE place that talks to the database (SQLite or MySQL).

Supports:
  - Multiple campus canteens with separate menus and kitchen queues
  - India Standard Time (Asia/Kolkata) 15-minute pickup slots (today + tomorrow)
  - Verified UPI payment fields (upi_id, upi_txn_ref, payment_signature, payment_verified_at)
  - Automatic wait-time storage and 14-day historical order seeding for demand prediction
  - Safe schema migrations on existing SQLite databases
"""

import hashlib
import hmac
import os
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from config import Config

try:
    import pymysql
    from pymysql.cursors import DictCursor
    from pymysql.err import IntegrityError as MySQLIntegrityError
    HAS_PYMYSQL = True
except ImportError:
    pymysql = None
    DictCursor = None
    MySQLIntegrityError = None
    HAS_PYMYSQL = False

if MySQLIntegrityError is not None:
    DBIntegrityError = (sqlite3.IntegrityError, MySQLIntegrityError)
else:
    DBIntegrityError = (sqlite3.IntegrityError,)

_ACTIVE_ENGINE = None


def get_db_engine():
    """Decide whether to use 'mysql' or 'sqlite' and remember the choice."""
    global _ACTIVE_ENGINE
    if _ACTIVE_ENGINE is not None:
        return _ACTIVE_ENGINE

    mode = Config.DB_ENGINE

    if mode == "sqlite" or not HAS_PYMYSQL:
        _ACTIVE_ENGINE = "sqlite"
        return _ACTIVE_ENGINE

    if mode == "mysql":
        _ACTIVE_ENGINE = "mysql"
        return _ACTIVE_ENGINE

    if Config.DB_HOST:
        try:
            conn = pymysql.connect(
                host=Config.DB_HOST,
                port=Config.DB_PORT,
                user=Config.DB_USER,
                password=Config.DB_PASSWORD,
                database=Config.DB_NAME,
                charset="utf8mb4",
                connect_timeout=3,
                cursorclass=DictCursor,
            )
            conn.close()
            _ACTIVE_ENGINE = "mysql"
            return _ACTIVE_ENGINE
        except Exception as err:
            print(f"[DB] MySQL unavailable ({err}); falling back to SQLite at {Config.SQLITE_PATH}")

    _ACTIVE_ENGINE = "sqlite"
    return _ACTIVE_ENGINE


def _prepare_sql(sql, engine):
    """Convert MySQL %s placeholders to SQLite ? placeholders when needed."""
    if engine == "sqlite":
        return sql.replace("%s", "?")
    return sql


def _normalize_params(params):
    """Convert datetime/date objects to ISO strings for clean SQLite binding."""
    if params is None:
        return ()
    cleaned = []
    for value in params:
        if isinstance(value, datetime):
            cleaned.append(value.strftime("%Y-%m-%d %H:%M:%S"))
        elif isinstance(value, date):
            cleaned.append(value.isoformat())
        else:
            cleaned.append(value)
    return tuple(cleaned)


def get_connection():
    """Open a new database connection using the active engine."""
    engine = get_db_engine()
    if engine == "mysql":
        return pymysql.connect(
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            database=Config.DB_NAME,
            charset="utf8mb4",
            cursorclass=DictCursor,
        )

    conn = sqlite3.connect(Config.SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def fetch_all(sql, params=None):
    engine = get_db_engine()
    connection = get_connection()
    try:
        query = _prepare_sql(sql, engine)
        if engine == "sqlite":
            cursor = connection.execute(query, _normalize_params(params))
            return [dict(row) for row in cursor.fetchall()]

        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()
    finally:
        connection.close()


def fetch_one(sql, params=None):
    engine = get_db_engine()
    connection = get_connection()
    try:
        query = _prepare_sql(sql, engine)
        if engine == "sqlite":
            cursor = connection.execute(query, _normalize_params(params))
            row = cursor.fetchone()
            return dict(row) if row is not None else None

        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchone()
    finally:
        connection.close()


def execute(sql, params=None):
    engine = get_db_engine()
    connection = get_connection()
    try:
        query = _prepare_sql(sql, engine)
        if engine == "sqlite":
            cursor = connection.execute(query, _normalize_params(params))
            connection.commit()
            return cursor.lastrowid

        with connection.cursor() as cursor:
            cursor.execute(query, params)
            connection.commit()
            return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sign_upi_payment(order_id, amount, upi_txn_ref):
    """Create a deterministic HMAC-SHA256 signature proving UPI verification."""
    payload = f"{order_id}:{float(amount):.2f}:{str(upi_txn_ref).strip().upper()}".encode("utf-8")
    secret = Config.UPI_WEBHOOK_SECRET.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()[:32]


# =========================================================
# Automatic database setup & seeding (SQLite)
# =========================================================

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS canteens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    location     TEXT NOT NULL,
    badge_color  TEXT NOT NULL DEFAULT 'indigo',
    upi_vpa      TEXT NOT NULL DEFAULT 'qless.canteen@upi',
    open_hour    INTEGER NOT NULL DEFAULT 7,
    close_hour   INTEGER NOT NULL DEFAULT 22,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name           TEXT NOT NULL,
    email               TEXT NOT NULL UNIQUE,
    campus_id           TEXT UNIQUE,
    password_hash       TEXT NOT NULL,
    role                TEXT NOT NULL DEFAULT 'student',
    dietary_preference  TEXT NOT NULL DEFAULT 'any',
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS password_resets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    otp_code    TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    is_used     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS menu_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    canteen_id          INTEGER NOT NULL DEFAULT 1,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL,
    category            TEXT NOT NULL,
    price               REAL NOT NULL,
    food_type           TEXT NOT NULL,
    allergens           TEXT NOT NULL DEFAULT '',
    prep_time_minutes   INTEGER NOT NULL DEFAULT 10,
    image_url           TEXT,
    prepared_batch_qty  INTEGER NOT NULL DEFAULT 0,
    stock_qty           INTEGER NOT NULL DEFAULT 0,
    is_sold_out         INTEGER NOT NULL DEFAULT 0,
    is_featured         INTEGER NOT NULL DEFAULT 0,
    is_surplus          INTEGER NOT NULL DEFAULT 0,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (canteen_id) REFERENCES canteens(id)
);

CREATE TABLE IF NOT EXISTS daily_batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_item_id   INTEGER NOT NULL,
    batch_date     TEXT NOT NULL,
    prepared_qty   INTEGER NOT NULL,
    UNIQUE (menu_item_id, batch_date),
    FOREIGN KEY (menu_item_id) REFERENCES menu_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pickup_slots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_date   TEXT NOT NULL,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    capacity    INTEGER NOT NULL DEFAULT 12,
    UNIQUE (slot_date, start_time)
);

CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id          INTEGER NOT NULL,
    canteen_id          INTEGER NOT NULL DEFAULT 1,
    slot_id             INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'confirmed',
    payment_method      TEXT NOT NULL DEFAULT 'counter',
    payment_status      TEXT NOT NULL DEFAULT 'pending',
    upi_id              TEXT,
    upi_txn_ref         TEXT,
    payment_signature   TEXT,
    payment_verified_at TEXT,
    estimated_wait_mins INTEGER NOT NULL DEFAULT 8,
    total_amount        REAL NOT NULL,
    pickup_otp          TEXT NOT NULL,
    qr_token            TEXT NOT NULL UNIQUE,
    uncollected_flag    INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    preparing_at        TEXT,
    ready_at            TEXT,
    collected_at        TEXT,
    cancelled_at        TEXT,
    FOREIGN KEY (student_id) REFERENCES users(id),
    FOREIGN KEY (canteen_id) REFERENCES canteens(id),
    FOREIGN KEY (slot_id) REFERENCES pickup_slots(id)
);

CREATE TABLE IF NOT EXISTS order_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL,
    menu_item_id  INTEGER NOT NULL,
    quantity      INTEGER NOT NULL,
    unit_price    REAL NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (menu_item_id) REFERENCES menu_items(id)
);

CREATE TABLE IF NOT EXISTS waste_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_item_id     INTEGER NOT NULL,
    waste_date       TEXT NOT NULL,
    quantity_wasted  INTEGER NOT NULL,
    weight_kg        REAL,
    reason           TEXT NOT NULL,
    notes            TEXT,
    logged_by        INTEGER,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (menu_item_id) REFERENCES menu_items(id),
    FOREIGN KEY (logged_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL UNIQUE,
    student_id  INTEGER NOT NULL,
    rating      INTEGER NOT NULL,
    comment     TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    message     TEXT NOT NULL,
    type        TEXT NOT NULL,
    is_read     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS favorites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    menu_item_id  INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, menu_item_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (menu_item_id) REFERENCES menu_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS announcements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canteen_id  INTEGER,
    message     TEXT NOT NULL,
    created_by  INTEGER,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (canteen_id) REFERENCES canteens(id) ON DELETE SET NULL,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);
"""

DEMO_CANTEENS = [
    ("Main Campus Canteen", "Central Block, Ground Floor", "indigo", "qless.main@upi", 7, 22),
    ("Tech Park Cafe", "Engineering Block B, Atrium", "teal", "qless.techpark@upi", 8, 21),
    ("Hostel Night Canteen", "Hostel Courtyard, Gate 2", "rose", "qless.hostel@upi", 7, 23),
]

DEMO_USERS = [
    ("Meera Iyer",   "manager@qless.edu",  "EMP001",  "manager", "any"),
    ("Ravi Kumar",   "staff@qless.edu",    "EMP002",  "staff",   "any"),
    ("Aarav Sharma", "aarav@campus.edu",   "STU2101", "student", "any"),
    ("Diya Patel",   "diya@campus.edu",    "STU2102", "student", "veg"),
    ("Rohan Mehta",  "rohan@campus.edu",   "STU2103", "student", "non_veg"),
    ("Ananya Singh", "ananya@campus.edu",  "STU2104", "student", "veg"),
    ("Kabir Khan",   "kabir@campus.edu",   "STU2105", "student", "any"),
    ("Isha Reddy",   "isha@campus.edu",    "STU2106", "student", "egg"),
    ("Vikram Nair",  "vikram@campus.edu",  "STU2107", "student", "any"),
    ("Sneha Das",    "sneha@campus.edu",   "STU2108", "student", "veg"),
]

# (canteen_id, name, description, category, price, food_type, allergens, prep_time, is_featured, prepared, stock, is_sold_out)
DEMO_MENU = [
    (1, "Idli Sambar",      "Three soft steamed idlis with hot sambar and coconut chutney.",     "Breakfast",     40.00,  "veg",     "",             5,  1, 40, 32, 0),
    (1, "Masala Dosa",      "Crisp rice crepe filled with spiced potato, served with chutneys.", "Breakfast",     60.00,  "veg",     "",             10, 0, 35, 26, 0),
    (1, "Poha",             "Light flattened-rice breakfast with peanuts, onion and lemon.",     "Breakfast",     35.00,  "veg",     "Peanuts",      5,  1, 40, 30, 0),
    (1, "Aloo Paratha",     "Whole-wheat paratha stuffed with potato, served with curd.",        "Breakfast",     50.00,  "veg",     "Gluten,Dairy", 10, 0, 30, 22, 0),
    (2, "Egg Bhurji Pav",   "Spicy scrambled eggs served with buttered pav.",                    "Breakfast",     55.00,  "egg",     "Egg,Gluten",   10, 0, 25, 18, 0),
    (1, "Veg Thali",        "Dal, two sabzis, rice, roti, salad and a sweet.",                   "Lunch",         90.00,  "veg",     "Dairy,Gluten", 10, 0, 60, 41, 0),
    (1, "Paneer Rice Bowl", "Paneer butter masala over steamed rice with salad.",                "Lunch",         110.00, "veg",     "Dairy",        12, 0, 35, 24, 0),
    (1, "Chole Bhature",    "Spicy chickpea curry with two fluffy fried bhature.",               "Lunch",         70.00,  "veg",     "Gluten",       12, 0, 30, 4,  0),
    (1, "Veg Biryani",      "Fragrant basmati rice cooked with vegetables, served with raita.",  "Lunch",         85.00,  "veg",     "Dairy",        12, 0, 40, 28, 0),
    (3, "Chicken Biryani",  "Hyderabadi-style chicken biryani with raita.",                      "Lunch",         120.00, "non_veg", "",             15, 0, 40, 14, 0),
    (2, "Egg Fried Rice",   "Wok-tossed rice with egg, vegetables and soy sauce.",               "Lunch",         75.00,  "egg",     "Egg,Soy",      10, 0, 30, 20, 0),
    (1, "Rajma Chawal",     "Kidney bean curry with steamed rice and pickle.",                   "Lunch",         70.00,  "veg",     "",             8,  0, 35, 25, 0),
    (1, "Samosa",           "Two crispy samosas with green chutney.",                            "Snacks",        15.00,  "veg",     "Gluten",       3,  1, 80, 64, 0),
    (2, "Veg Sandwich",     "Grilled sandwich with vegetables, cheese and mint chutney.",        "Snacks",        40.00,  "veg",     "Gluten,Dairy", 5,  1, 40, 31, 0),
    (1, "Pav Bhaji",        "Buttery mashed vegetable curry with toasted pav.",                  "Snacks",        65.00,  "veg",     "Gluten,Dairy", 8,  0, 35, 12, 0),
    (3, "Veg Maggi",        "Masala noodles cooked with vegetables.",                            "Snacks",        35.00,  "veg",     "Gluten",       6,  1, 40, 33, 0),
    (2, "Fruit Salad",      "Seasonal fresh-cut fruit bowl.",                                    "Snacks",        45.00,  "veg",     "",             3,  1, 25, 19, 0),
    (2, "Paneer Wrap",      "Grilled paneer tikka with onions and mint in a soft wrap.",         "Snacks",        70.00,  "veg",     "Gluten,Dairy", 8,  0, 25, 15, 0),
    (2, "Chicken Roll",     "Spiced chicken with onions and sauce in a paratha roll.",           "Snacks",        80.00,  "non_veg", "Gluten",       10, 0, 30, 21, 0),
    (3, "Egg Roll",         "Egg-coated paratha roll with onions and chutney.",                  "Snacks",        55.00,  "egg",     "Gluten,Egg",   8,  0, 30, 23, 0),
    (1, "Masala Chai",      "Hot milk tea with ginger and cardamom.",                            "Snacks",        15.00,  "veg",     "Dairy",        3,  1, 100, 78, 0),
    (2, "Cold Coffee",      "Chilled blended coffee with milk and ice.",                         "Snacks",        50.00,  "veg",     "Dairy",        4,  1, 40, 29, 0),
    (2, "Fresh Lime Soda",  "Sweet or salted lime soda, served cold.",                           "Snacks",        30.00,  "veg",     "",             3,  1, 50, 41, 0),
]

# Dish-to-canteen assignment map for migrating existing DBs
DISH_CANTEEN_MAP = {
    "Egg Bhurji Pav": 2,
    "Egg Fried Rice": 2,
    "Veg Sandwich": 2,
    "Fruit Salad": 2,
    "Paneer Wrap": 2,
    "Chicken Roll": 2,
    "Cold Coffee": 2,
    "Fresh Lime Soda": 2,
    "Paneer Tikka Sandwich": 2,
    "Chicken Momos": 2,
    "Veg Momos": 2,
    "Masala Fries": 2,
    "Chicken Biryani": 3,
    "Veg Maggi": 3,
    "Egg Roll": 3,
    "Dinner Veg Thali": 3,
    "Dinner Chicken Biryani": 3,
    "Paneer Butter Masala": 3,
    "Paneer Paratha": 3,
}


def campus_now():
    """Return current datetime in campus timezone (Asia/Kolkata IST)."""
    try:
        return datetime.now(ZoneInfo(Config.TIMEZONE))
    except Exception:
        return datetime.now()


# Backwards-compatible alias
_campus_now = campus_now


def ensure_slots_for_date(target_date):
    """Idempotently ensure all 15-minute IST pickup slots exist for target_date."""
    date_str = target_date.isoformat()
    current = datetime.combine(target_date, time(Config.CANTEEN_OPEN_HOUR, 0))
    closing = datetime.combine(target_date, time(Config.CANTEEN_CLOSE_HOUR, 0))
    step = timedelta(minutes=Config.SLOT_LENGTH_MINUTES)

    while current < closing:
        end = min(current + step, closing)
        try:
            execute(
                """INSERT INTO pickup_slots (slot_date, start_time, end_time, capacity)
                   VALUES (%s, %s, %s, %s)""",
                (date_str, current.strftime("%H:%M:%S"), end.strftime("%H:%M:%S"),
                 Config.DEFAULT_SLOT_CAPACITY),
            )
        except DBIntegrityError:
            pass
        current = end


def ensure_today_slots():
    """Backfill all 15-minute IST slots for today AND tomorrow so ordering works 24/7."""
    now = campus_now()
    ensure_slots_for_date(now.date())
    ensure_slots_for_date(now.date() + timedelta(days=1))


def ensure_canteens_and_migrations():
    """Safely create canteens table & add new columns on existing SQLite DBs."""
    if get_db_engine() == "sqlite":
        conn = get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS canteens (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    name         TEXT NOT NULL UNIQUE,
                    location     TEXT NOT NULL,
                    badge_color  TEXT NOT NULL DEFAULT 'indigo',
                    upi_vpa      TEXT NOT NULL DEFAULT 'qless.canteen@upi',
                    open_hour    INTEGER NOT NULL DEFAULT 7,
                    close_hour   INTEGER NOT NULL DEFAULT 22,
                    is_active    INTEGER NOT NULL DEFAULT 1,
                    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        finally:
            conn.close()

        # Seed canteens
        existing_c = fetch_one("SELECT COUNT(*) AS total FROM canteens")["total"]
        if existing_c == 0:
            for c_name, loc, color, vpa, oh, ch in DEMO_CANTEENS:
                try:
                    execute(
                        """INSERT INTO canteens (name, location, badge_color, upi_vpa, open_hour, close_hour)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (c_name, loc, color, vpa, oh, ch),
                    )
                except DBIntegrityError:
                    pass

        # Safe column migrations for menu_items
        menu_cols = {r["name"] for r in fetch_all("PRAGMA table_info(menu_items)")}
        if "canteen_id" not in menu_cols:
            execute("ALTER TABLE menu_items ADD COLUMN canteen_id INTEGER NOT NULL DEFAULT 1")
            for dish_name, cid in DISH_CANTEEN_MAP.items():
                execute("UPDATE menu_items SET canteen_id = %s WHERE name = %s", (cid, dish_name))
        if "is_surplus" not in menu_cols:
            execute("ALTER TABLE menu_items ADD COLUMN is_surplus INTEGER NOT NULL DEFAULT 0")

        # Safe column migrations for orders
        order_cols = {r["name"] for r in fetch_all("PRAGMA table_info(orders)")}
        migrations = [
            ("canteen_id", "ALTER TABLE orders ADD COLUMN canteen_id INTEGER NOT NULL DEFAULT 1"),
            ("upi_id", "ALTER TABLE orders ADD COLUMN upi_id TEXT"),
            ("upi_txn_ref", "ALTER TABLE orders ADD COLUMN upi_txn_ref TEXT"),
            ("payment_signature", "ALTER TABLE orders ADD COLUMN payment_signature TEXT"),
            ("payment_verified_at", "ALTER TABLE orders ADD COLUMN payment_verified_at TEXT"),
            ("estimated_wait_mins", "ALTER TABLE orders ADD COLUMN estimated_wait_mins INTEGER NOT NULL DEFAULT 8"),
        ]
        for col, ddl in migrations:
            if col not in order_cols:
                execute(ddl)

        # Safe column migrations for announcements
        ann_cols = {r["name"] for r in fetch_all("PRAGMA table_info(announcements)")}
        if "canteen_id" not in ann_cols:
            execute("ALTER TABLE announcements ADD COLUMN canteen_id INTEGER")


def ensure_extended_menu():
    """Add Breakfast, Lunch, Snacks, and Dinner choices across all 3 canteens without duplicating."""
    execute("UPDATE menu_items SET category = 'Lunch' WHERE category = 'Meals'")
    extra_items = [
        (1, "Upma", "Soft semolina upma with vegetables and curry leaves.", "Breakfast", 35, "veg", "", 6, 1, 35, 35, 0, "/static/img/dishes/poha.jpg"),
        (3, "Paneer Paratha", "Stuffed paneer paratha served with curd.", "Breakfast", 65, "veg", "Gluten,Dairy", 10, 0, 25, 25, 0, "/static/img/dishes/aloo-paratha.jpg"),
        (1, "Veg Pulao", "Mildly spiced vegetable rice with raita.", "Lunch", 75, "veg", "Dairy", 10, 0, 35, 35, 0, "/static/img/dishes/veg-biryani.jpg"),
        (1, "Dal Rice Bowl", "Comforting yellow dal served with steamed rice.", "Lunch", 65, "veg", "", 8, 1, 40, 40, 0, "/static/img/dishes/rajma-chawal.jpg"),
        (3, "Chicken Rice Bowl", "Spiced chicken served over steamed rice.", "Lunch", 110, "non_veg", "", 12, 0, 25, 25, 0, "/static/img/dishes/chicken-biryani.jpg"),
        (2, "Paneer Tikka Sandwich", "Toasted sandwich filled with paneer tikka.", "Snacks", 60, "veg", "Gluten,Dairy", 7, 0, 30, 30, 0, "/static/img/dishes/veg-sandwich.jpg"),
        (2, "Chicken Momos", "Steamed chicken dumplings with spicy chutney.", "Snacks", 70, "non_veg", "Gluten", 10, 0, 25, 25, 0, "/static/img/dishes/chicken-roll.jpg"),
        (2, "Veg Momos", "Steamed vegetable dumplings with spicy chutney.", "Snacks", 60, "veg", "Gluten", 10, 0, 25, 25, 0, "/static/img/dishes/veg-sandwich.jpg"),
        (3, "Dinner Veg Thali", "Dal, seasonal sabzi, rice, roti and salad.", "Dinner", 95, "veg", "Gluten", 12, 0, 40, 40, 0, "/static/img/dishes/veg-thali.jpg"),
        (3, "Dinner Chicken Biryani", "Aromatic chicken biryani with raita.", "Dinner", 130, "non_veg", "Dairy", 15, 0, 30, 30, 0, "/static/img/dishes/chicken-biryani.jpg"),
        (3, "Paneer Butter Masala", "Paneer in creamy tomato gravy served with rice.", "Dinner", 105, "veg", "Dairy", 12, 0, 30, 30, 0, "/static/img/dishes/paneer-rice-bowl.jpg"),
        (2, "Masala Fries", "Crispy fries tossed in house masala.", "Snacks", 45, "veg", "", 6, 1, 35, 35, 0, "/static/img/dishes/samosa.jpg"),
        (1, "Dinner Dal Khichdi", "Warm spiced lentil-rice khichdi with pickle and papad.", "Dinner", 75, "veg", "Dairy", 10, 1, 35, 30, 0, "/static/img/dishes/rajma-chawal.jpg"),
        (2, "Grilled Paneer Bowl", "Herb-grilled paneer over tossed veggies and rice.", "Dinner", 115, "veg", "Dairy", 12, 0, 25, 22, 0, "/static/img/dishes/paneer-rice-bowl.jpg"),
    ]
    for item in extra_items:
        cid, name, description, category, price, food_type, allergens, prep, featured, prepared, stock, surplus, image = item
        exists = fetch_one("SELECT id FROM menu_items WHERE name = %s LIMIT 1", (name,))
        if not exists:
            execute(
                """INSERT INTO menu_items
                   (canteen_id, name, description, category, price, food_type, allergens, prep_time_minutes,
                    is_featured, prepared_batch_qty, stock_qty, is_sold_out, is_surplus, image_url)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)""",
                (cid, name, description, category, price, food_type, allergens, prep, featured,
                 prepared, stock, surplus, image),
            )


def _hash_demo_password(plain_password):
    from werkzeug.security import generate_password_hash
    return generate_password_hash(plain_password, method=Config.PASSWORD_HASH_METHOD)


def init_db():
    """Create tables, run safe migrations, and seed demo data automatically."""
    engine = get_db_engine()
    if engine == "sqlite":
        db_dir = os.path.dirname(Config.SQLITE_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = get_connection()
        try:
            conn.executescript(SQLITE_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        ensure_canteens_and_migrations()

        user_count = fetch_one("SELECT COUNT(*) AS total FROM users")["total"]
        if user_count == 0:
            demo_hash = _hash_demo_password("Qless@123")
            for full_name, email, campus_id, role, diet in DEMO_USERS:
                execute(
                    """
                    INSERT INTO users (full_name, email, campus_id, password_hash, role, dietary_preference)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (full_name, email, campus_id, demo_hash, role, diet),
                )

        menu_count = fetch_one("SELECT COUNT(*) AS total FROM menu_items")["total"]
        if menu_count == 0:
            for item in DEMO_MENU:
                execute(
                    """
                    INSERT INTO menu_items
                        (canteen_id, name, description, category, price, food_type, allergens,
                         prep_time_minutes, is_featured, prepared_batch_qty, stock_qty, is_sold_out)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    item,
                )

        ann_count = fetch_one("SELECT COUNT(*) AS total FROM announcements")["total"]
        if ann_count == 0:
            manager = fetch_one("SELECT id FROM users WHERE role = 'manager' LIMIT 1")
            execute(
                "INSERT INTO announcements (canteen_id, message, created_by, is_active) VALUES (NULL, %s, %s, 1)",
                (
                    "Fresh Samosas and Masala Chai are ready fast at Main Campus Canteen! Pre-order your 15-minute slot to skip the queue.",
                    manager["id"] if manager else None,
                ),
            )

    try:
        ensure_today_slots()
        ensure_extended_menu()
        backfill_dish_photos()
        ensure_surplus_column_and_seed()
        if engine == "sqlite":
            seed_sample_history_if_empty()
    except Exception as err:
        print(f"[DB] Could not initialize slots/history: {err}")


def seed_sample_history_if_empty():
    """Populate realistic 10-day historical orders across all 3 canteens for demand prediction & analytics."""
    existing_orders = fetch_one("SELECT COUNT(*) AS total FROM orders")["total"]
    if existing_orders > 0:
        return

    students = fetch_all("SELECT id FROM users WHERE role = 'student' ORDER BY id")
    manager = fetch_one("SELECT id FROM users WHERE role = 'manager' LIMIT 1")
    dishes = fetch_all("SELECT id, canteen_id, name, price FROM menu_items ORDER BY id")
    if not students or not dishes:
        return

    today = campus_now().date()
    # (days_ago, start_time, end_time, [(dish_idx, qty)], status, rating, comment, canteen_id)
    sample_patterns = [
        (9, "08:30:00", "08:45:00", [(0, 2), (20, 2)], "collected", 5, "Hot idlis and fast pickup!", 1),
        (8, "12:15:00", "12:30:00", [(5, 2), (12, 2)], "collected", 4, "Good portions, zero queue.", 1),
        (7, "13:00:00", "13:15:00", [(6, 2), (13, 1)], "collected", 5, "Paneer bowl was fresh.", 1),
        (6, "12:15:00", "12:30:00", [(8, 2), (20, 2)], "collected", 4, "Ready right on time.", 1),
        (5, "12:45:00", "13:00:00", [(9, 2), (15, 1)], "collected", 5, "Loved the biryani at Hostel Canteen!", 3),
        (4, "16:30:00", "16:45:00", [(13, 2), (21, 2)], "collected", 5, "Cold coffee & sandwich were super quick.", 2),
        (3, "12:15:00", "12:30:00", [(5, 3), (12, 3)], "collected", 5, "No lunch rush waiting!", 1),
        (3, "17:00:00", "17:15:00", [(18, 2), (22, 1)], "collected", 4, "Crisp chicken roll at Tech Park Cafe.", 2),
        (2, "12:30:00", "12:45:00", [(1, 2), (20, 2)], "collected", 5, "Crisp dosa, zero queue.", 1),
        (2, "20:00:00", "20:15:00", [(9, 2), (19, 2)], "collected", 5, "Best dinner biryani on campus.", 3),
        (1, "08:45:00", "09:00:00", [(0, 3), (2, 2)], "collected", 5, "Great breakfast slot.", 1),
        (1, "12:15:00", "12:30:00", [(5, 2), (11, 2)], "collected", 4, "Nice Rajma Chawal and Thali.", 1),
        (1, "16:45:00", "17:00:00", [(13, 2), (21, 3)], "collected", 5, "Tech Park Cafe is super fast.", 2),
        (0, "12:15:00", "12:30:00", [(5, 1), (20, 1)], "ready",     None, None, 1),
        (0, "12:30:00", "12:45:00", [(13, 1), (21, 1)], "preparing", None, None, 2),
        (0, "19:30:00", "19:45:00", [(9, 1), (15, 1)], "confirmed", None, None, 3),
    ]

    for idx, (days_ago, st, et, item_pairs, status, rating, comment, cid) in enumerate(sample_patterns):
        d_str = (today - timedelta(days=days_ago)).isoformat()
        slot = fetch_one(
            "SELECT id FROM pickup_slots WHERE slot_date = %s AND start_time = %s",
            (d_str, st),
        )
        if not slot:
            slot_id = execute(
                "INSERT INTO pickup_slots (slot_date, start_time, end_time, capacity) VALUES (%s, %s, %s, %s)",
                (d_str, st, et, Config.DEFAULT_SLOT_CAPACITY),
            )
        else:
            slot_id = slot["id"]

        student_id = students[idx % len(students)]["id"]
        total = sum(float(dishes[i]["price"]) * q for i, q in item_pairs if i < len(dishes))
        otp = f"{(1042 + idx * 137) % 9000 + 1000}"
        qr = f"QL-DEMO{idx + 1:03d}"
        utr = f"432{idx + 10:02d}88910{idx % 10}"
        created_ts = f"{d_str} {st}"

        order_id = execute(
            """
            INSERT INTO orders
                (student_id, canteen_id, slot_id, status, payment_method, payment_status,
                 upi_id, upi_txn_ref, estimated_wait_mins, total_amount, pickup_otp, qr_token, created_at)
            VALUES (%s, %s, %s, %s, 'upi', 'paid', 'student@okupi', %s, %s, %s, %s, %s, %s)
            """,
            (student_id, cid, slot_id, status, utr, 8 + (idx % 6), round(total, 2), otp, qr, created_ts),
        )
        sig = sign_upi_payment(order_id, total, utr)
        execute(
            "UPDATE orders SET payment_signature = %s, payment_verified_at = %s WHERE id = %s",
            (sig, created_ts, order_id),
        )

        for dish_idx, qty in item_pairs:
            if dish_idx < len(dishes):
                execute(
                    "INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price) VALUES (%s, %s, %s, %s)",
                    (order_id, dishes[dish_idx]["id"], qty, float(dishes[dish_idx]["price"])),
                )

        if rating is not None:
            execute(
                "INSERT INTO feedback (order_id, student_id, rating, comment) VALUES (%s, %s, %s, %s)",
                (order_id, student_id, rating, comment),
            )

        if status == "ready":
            execute(
                "INSERT INTO notifications (user_id, message, type, is_read) VALUES (%s, %s, 'order_ready', 0)",
                (student_id, f"Order #{order_id} is READY for pickup at Main Campus Canteen! Show OTP {otp} at the counter."),
            )

    sample_waste = [
        (3, 5, 6, 1.8, "overproduction", "Extra Veg Thali portions left after 3 PM"),
        (2, 7, 3, 0.9, "uncollected",    "Uncollected afternoon orders"),
        (1, 2, 5, 1.1, "overproduction", "Breakfast Poha batch slightly too large"),
        (0, 14, 2, 0.6, "spoilage",      "Pav Bhaji gravy cooled down"),
    ]
    for days_ago, dish_idx, qty, kg, reason, notes in sample_waste:
        if dish_idx < len(dishes):
            execute(
                """
                INSERT INTO waste_logs (menu_item_id, waste_date, quantity_wasted, weight_kg, reason, notes, logged_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    dishes[dish_idx]["id"],
                    (today - timedelta(days=days_ago)).isoformat(),
                    qty,
                    kg,
                    reason,
                    notes,
                    manager["id"] if manager else None,
                ),
            )


DISH_PHOTO_MAP = {
    "Idli Sambar": "/static/img/dishes/idli-sambar.jpg",
    "Masala Dosa": "/static/img/dishes/masala-dosa.jpg",
    "Poha": "/static/img/dishes/poha.jpg",
    "Aloo Paratha": "/static/img/dishes/aloo-paratha.jpg",
    "Egg Bhurji Pav": "/static/img/dishes/egg-bhurji-pav.jpg",
    "Veg Thali": "/static/img/dishes/veg-thali.jpg",
    "Paneer Rice Bowl": "/static/img/dishes/paneer-rice-bowl.jpg",
    "Chole Bhature": "/static/img/dishes/chole-bhature.jpg",
    "Veg Biryani": "/static/img/dishes/veg-biryani.jpg",
    "Chicken Biryani": "/static/img/dishes/chicken-biryani.jpg",
    "Egg Fried Rice": "/static/img/dishes/egg-fried-rice.jpg",
    "Rajma Chawal": "/static/img/dishes/rajma-chawal.jpg",
    "Samosa": "/static/img/dishes/samosa.jpg",
    "Veg Sandwich": "/static/img/dishes/veg-sandwich.jpg",
    "Pav Bhaji": "/static/img/dishes/pav-bhaji.jpg",
    "Veg Maggi": "/static/img/dishes/veg-maggi.jpg",
    "Fruit Salad": "/static/img/dishes/fruit-salad.jpg",
    "Paneer Wrap": "/static/img/dishes/paneer-wrap.jpg",
    "Chicken Roll": "/static/img/dishes/chicken-roll.jpg",
    "Egg Roll": "/static/img/dishes/egg-roll.jpg",
    "Masala Chai": "/static/img/dishes/masala-chai.jpg",
    "Cold Coffee": "/static/img/dishes/cold-coffee.jpg",
    "Fresh Lime Soda": "/static/img/dishes/fresh-lime-soda.jpg",
}


def backfill_dish_photos():
    """Ensure every menu item has an image_url assigned."""
    rows = fetch_all("SELECT id, name, category, image_url FROM menu_items WHERE image_url IS NULL OR image_url = '' OR image_url LIKE '%.svg'")
    cat_defaults = {
        "Breakfast": "/static/img/dishes/masala-dosa.jpg",
        "Lunch": "/static/img/dishes/veg-thali.jpg",
        "Meals": "/static/img/dishes/veg-thali.jpg",
        "Dinner": "/static/img/dishes/veg-thali.jpg",
        "Snacks": "/static/img/dishes/samosa.jpg",
        "Rolls & Wraps": "/static/img/dishes/paneer-wrap.jpg",
        "Beverages": "/static/img/dishes/masala-chai.jpg",
    }
    for r in rows:
        img = DISH_PHOTO_MAP.get(r["name"]) or cat_defaults.get(r["category"], "/static/img/dishes/veg-thali.jpg")
        execute("UPDATE menu_items SET image_url = %s WHERE id = %s", (img, r["id"]))


def ensure_surplus_column_and_seed():
    """Safely seed 3 Zero-Waste Rescue Deals if none exist."""
    existing = fetch_one("SELECT COUNT(*) AS total FROM menu_items WHERE is_surplus = 1")
    if existing and existing["total"] == 0:
        for dish_name in ("Poha", "Veg Biryani", "Samosa"):
            execute("UPDATE menu_items SET is_surplus = 1 WHERE name = %s", (dish_name,))
