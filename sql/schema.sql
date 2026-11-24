-- schema.sql - Creates the Qless database and every table (MySQL).
-- WARNING: running this file again DELETES all existing Qless data.

CREATE DATABASE IF NOT EXISTS qless_db
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE qless_db;

SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS announcements;
DROP TABLE IF EXISTS favorites;
DROP TABLE IF EXISTS notifications;
DROP TABLE IF EXISTS feedback;
DROP TABLE IF EXISTS waste_logs;
DROP TABLE IF EXISTS daily_batches;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS pickup_slots;
DROP TABLE IF EXISTS menu_items;
DROP TABLE IF EXISTS canteens;
DROP TABLE IF EXISTS password_resets;
DROP TABLE IF EXISTS users;
SET FOREIGN_KEY_CHECKS = 1;

-- CANTEENS: multiple campus outlets with separate kitchen queues
CREATE TABLE canteens (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    name         VARCHAR(100) NOT NULL UNIQUE,
    location     VARCHAR(150) NOT NULL,
    badge_color  VARCHAR(30) NOT NULL DEFAULT 'indigo',
    upi_vpa      VARCHAR(100) NOT NULL DEFAULT 'qless.canteen@upi',
    open_hour    INT NOT NULL DEFAULT 7,
    close_hour   INT NOT NULL DEFAULT 22,
    is_active    TINYINT(1) NOT NULL DEFAULT 1,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- USERS: students, kitchen/counter staff and canteen managers
CREATE TABLE users (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    full_name           VARCHAR(100) NOT NULL,
    email               VARCHAR(120) NOT NULL UNIQUE,
    campus_id           VARCHAR(30) UNIQUE,
    password_hash       VARCHAR(255) NOT NULL,
    role                ENUM('student', 'staff', 'manager') NOT NULL DEFAULT 'student',
    dietary_preference  ENUM('any', 'veg', 'non_veg', 'egg') NOT NULL DEFAULT 'any',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- PASSWORD_RESETS: one-time codes for password recovery
CREATE TABLE password_resets (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    otp_code    CHAR(6) NOT NULL,
    expires_at  DATETIME NOT NULL,
    is_used     TINYINT(1) NOT NULL DEFAULT 0,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- MENU_ITEMS: dishes tagged by canteen and meal category
CREATE TABLE menu_items (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    canteen_id          INT NOT NULL DEFAULT 1,
    name                VARCHAR(100) NOT NULL,
    description         VARCHAR(255) NOT NULL,
    category            VARCHAR(40) NOT NULL,
    price               DECIMAL(8, 2) NOT NULL,
    food_type           ENUM('veg', 'non_veg', 'egg') NOT NULL,
    allergens           VARCHAR(120) NOT NULL DEFAULT '',
    prep_time_minutes   INT NOT NULL DEFAULT 10,
    image_url           VARCHAR(255),
    prepared_batch_qty  INT NOT NULL DEFAULT 0,
    stock_qty           INT NOT NULL DEFAULT 0,
    is_sold_out         TINYINT(1) NOT NULL DEFAULT 0,
    is_featured         TINYINT(1) NOT NULL DEFAULT 0,
    is_surplus          TINYINT(1) NOT NULL DEFAULT 0,
    is_active           TINYINT(1) NOT NULL DEFAULT 1,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (price >= 0),
    CHECK (stock_qty >= 0),
    CHECK (prepared_batch_qty >= 0),
    FOREIGN KEY (canteen_id) REFERENCES canteens(id)
);

-- DAILY_BATCHES: prepared batch history per dish per day
CREATE TABLE daily_batches (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    menu_item_id   INT NOT NULL,
    batch_date     DATE NOT NULL,
    prepared_qty   INT NOT NULL,
    UNIQUE KEY one_batch_per_item_per_day (menu_item_id, batch_date),
    FOREIGN KEY (menu_item_id) REFERENCES menu_items(id) ON DELETE CASCADE
);

-- PICKUP_SLOTS: 15-minute IST pickup windows
CREATE TABLE pickup_slots (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    slot_date   DATE NOT NULL,
    start_time  TIME NOT NULL,
    end_time    TIME NOT NULL,
    capacity    INT NOT NULL DEFAULT 12,
    UNIQUE KEY one_slot_per_start_time (slot_date, start_time),
    CHECK (capacity > 0)
);

-- ORDERS: student orders with canteen queue, wait estimate, OTP, and verified UPI tracking
CREATE TABLE orders (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    student_id          INT NOT NULL,
    canteen_id          INT NOT NULL DEFAULT 1,
    slot_id             INT NOT NULL,
    status              ENUM('confirmed', 'preparing', 'ready', 'collected', 'cancelled')
                        NOT NULL DEFAULT 'confirmed',
    payment_method      ENUM('upi', 'mock_upi', 'counter') NOT NULL DEFAULT 'counter',
    payment_status      ENUM('pending', 'paid', 'refunded') NOT NULL DEFAULT 'pending',
    upi_id              VARCHAR(100),
    upi_txn_ref         VARCHAR(40),
    payment_signature   VARCHAR(64),
    payment_verified_at DATETIME,
    estimated_wait_mins INT NOT NULL DEFAULT 8,
    total_amount        DECIMAL(8, 2) NOT NULL,
    pickup_otp          CHAR(4) NOT NULL,
    qr_token            VARCHAR(40) NOT NULL UNIQUE,
    uncollected_flag    TINYINT(1) NOT NULL DEFAULT 0,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    preparing_at        DATETIME,
    ready_at            DATETIME,
    collected_at        DATETIME,
    cancelled_at        DATETIME,
    FOREIGN KEY (student_id) REFERENCES users(id),
    FOREIGN KEY (canteen_id) REFERENCES canteens(id),
    FOREIGN KEY (slot_id) REFERENCES pickup_slots(id),
    INDEX idx_orders_slot_status (slot_id, status),
    INDEX idx_orders_canteen_status (canteen_id, status),
    INDEX idx_orders_student (student_id),
    INDEX idx_orders_created (created_at)
);

-- ORDER_ITEMS: dishes inside each order
CREATE TABLE order_items (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    order_id      INT NOT NULL,
    menu_item_id  INT NOT NULL,
    quantity      INT NOT NULL,
    unit_price    DECIMAL(8, 2) NOT NULL,
    CHECK (quantity > 0),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (menu_item_id) REFERENCES menu_items(id)
);

-- WASTE_LOGS: food waste logged by staff
CREATE TABLE waste_logs (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    menu_item_id     INT NOT NULL,
    waste_date       DATE NOT NULL,
    quantity_wasted  INT NOT NULL,
    weight_kg        DECIMAL(6, 2),
    reason           ENUM('overproduction', 'spoilage', 'uncollected') NOT NULL,
    notes            VARCHAR(255),
    logged_by        INT,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (quantity_wasted > 0),
    FOREIGN KEY (menu_item_id) REFERENCES menu_items(id),
    FOREIGN KEY (logged_by) REFERENCES users(id) ON DELETE SET NULL,
    INDEX idx_waste_date (waste_date)
);

-- FEEDBACK: customer rating and review per collected order
CREATE TABLE feedback (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    order_id    INT NOT NULL UNIQUE,
    student_id  INT NOT NULL,
    rating      INT NOT NULL,
    comment     VARCHAR(255),
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (rating BETWEEN 1 AND 5),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES users(id)
);

-- NOTIFICATIONS: real-time order ready and status alerts
CREATE TABLE notifications (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    message     VARCHAR(255) NOT NULL,
    type        ENUM('order_ready', 'slot_reminder', 'order_cancelled') NOT NULL,
    is_read     TINYINT(1) NOT NULL DEFAULT 0,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_notifications_user (user_id, is_read)
);

-- FAVORITES: student saved dishes
CREATE TABLE favorites (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL,
    menu_item_id  INT NOT NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY one_fav_per_user_item (user_id, menu_item_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (menu_item_id) REFERENCES menu_items(id) ON DELETE CASCADE
);

-- ANNOUNCEMENTS: live canteen notices
CREATE TABLE announcements (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    canteen_id  INT,
    message     VARCHAR(255) NOT NULL,
    created_by  INT,
    is_active   TINYINT(1) NOT NULL DEFAULT 1,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (canteen_id) REFERENCES canteens(id) ON DELETE SET NULL,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);