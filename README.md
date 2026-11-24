# Qless — Smart Campus Canteen & Food-Waste Optimisation Platform

*Less Waiting. Less Waste. Better Campus.*

**Tech Stack:** Python Flask, SQLite (automatic zero-config fallback) / MySQL (PyMySQL), Jinja2, Vanilla JavaScript, Chart.js, CSS Design Tokens.

## Complete Feature Set (All 8 Phases)

- **Authentication & Security:** Student/Staff/Manager role-based login, signup, 6-digit password reset, CSRF protection, `pbkdf2:sha256` hashing, Show/Hide Password toggles, Remember Me, and 1-Click Demo Login buttons.
- **Student Platform (`/menu`, `/cart`, `/orders`, `/account`):** Search, category & dietary filters, Ready Fast filter, Favorites wishlist, live canteen notice banner, shopping cart, 15-minute pickup slot picker with live capacity, instant 4-digit Pickup OTP + Digital QR Pickup Pass, 15-second live status auto-polling, Notification Bell Drawer, order cancellation with automatic stock restoration, 1-tap reorder, and post-pickup meal ratings.
- **Canteen Staff Platform (`/staff/dashboard`):** Live "Action Now" order queue with OTP handover verification, one-click order status transitions (`Confirmed -> Preparing -> Ready -> Collected`), live `+10 Stock` / `Sold Out` inventory controls, live student announcement broadcaster, and food-waste logger.
- **Manager Controls (`/manager/settings`):** Full Menu CRUD (Add new dish, update price/stock, archive dish), Staff/Manager account creation, and 15-minute pickup slot capacity controls.
- **Analytics & Sustainability (`/analytics`):** Interactive Chart.js dashboards (7-Day Orders & Revenue Trend, Prepared vs. Sold Portions, Food Waste by Reason), Smart Kitchen Prep Recommendations, Peak Pickup Windows, and one-click CSV exports (`/analytics/export/orders.csv`, `/analytics/export/waste.csv`).

## Run Locally

1. `python -m venv venv`
2. Activate virtual environment, then run: `pip install -r requirements.txt`
3. Start the server: `python app.py` (SQLite database `qless.db` is created and seeded automatically with demo accounts, 23 menu items, 32 daily pickup slots, and 7-day sample analytics data!)

### Demo Accounts (Password: `Qless@123`)
- **Student:** `aarav@campus.edu`
- **Staff:** `staff@qless.edu`
- **Manager:** `manager@qless.edu`

## Deploy on Render

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
- Works out of the box on Render's free tier using SQLite. To switch to MySQL later, simply set `DB_HOST`, `DB_USER`, `DB_PASSWORD`, and `DB_NAME` in Render Environment Variables.


## Upgrade notes (November 2026)

This update keeps the existing Flask + SQLite/MySQL architecture and adds/fixes:

- Restored the shared `templates/base.html` document shell. The uploaded copy was empty, which meant pages extending it (including login/signup) had no shared page structure, CSS or JavaScript loaded.
- Added viewport metadata, local stylesheets, Lucide icons, Chart.js and the shared JavaScript entry point to the base template.
- Added responsive layout refinements for phones, tablets and desktop, with accessible focus states and reduced-motion support.
- Added a smart pickup-slot suggestion: the cart preselects a future slot with the most remaining capacity to help spread demand.
- Expired and past pickup slots are no longer offered, and checkout validates that the chosen slot is still in the future.
- Students can no longer mark their own orders as collected. Staff must enter the correct four-digit OTP to complete handover.
- Demo UPI is clearly labelled as a demo and no longer marks an order paid without a payment-provider confirmation. Counter orders become paid at staff handover.
- Python modules are syntax-checked; database initialization and template syntax should be tested in an environment with the dependencies installed.

### Important deployment settings

For a persistent production login/database, configure an external MySQL database and set `DB_ENGINE=mysql`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, and `DB_NAME` in Render. The current code supports SQLite and MySQL, not Render Postgres without a database-layer migration. Render's local filesystem on a free web service is not a reliable persistent database location; SQLite is suitable for local development and demonstrations, not durable production accounts.

Set a unique, long `SECRET_KEY` in Render Environment Variables. Never publish real payment credentials or use the demo UPI option as proof of payment.

### Verification checklist

1. Open `/health` and confirm it returns `status: ok`.
2. Test demo student login: `aarav@campus.edu` / `Qless@123`.
3. Test staff login: `staff@qless.edu` / `Qless@123`.
4. Test manager login: `manager@qless.edu` / `Qless@123`.
5. As a student, add a dish, select a future slot and place a counter order.
6. As staff, accept the order, mark it ready, then enter the exact OTP to mark it collected.
7. Confirm a wrong or blank OTP is rejected and the student cannot self-mark collection.
8. Resize the browser to phone, tablet and desktop widths; test the menu, cart, login and staff dashboard.

Demo accounts are seeded automatically only when the selected SQLite database is empty. Existing MySQL databases keep their existing accounts and data.


## Latest ordering fixes (November 2026)

- Pickup slots are generated idempotently for the full campus-local day (Asia/Kolkata by default), even if an earlier deployment created only part of the day's slots.
- The default service window is 07:00–22:00 IST with 15-minute pickup windows. Set `CANTEEN_TIMEZONE`, `CANTEEN_OPEN_HOUR`, and `CANTEEN_CLOSE_HOUR` in configuration if your campus uses different hours.
- The cart shows item price, dietary type, average rating (or New if unrated), category, and canteen name.
- The student menu is organized into Breakfast, Lunch, Snacks, and Dinner. Existing `Meals` rows are normalized to `Lunch`.
- Additional demo menu choices are seeded once without duplicating existing dishes.
- Set `CANTEEN_NAME` in the Render environment to display your actual canteen name.
