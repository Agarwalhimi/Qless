/*
  main.js - Interactive helpers used across Qless pages.

  1. Lucide icons
  2. Mobile navigation menu
  3. Dismissible flash notices
  4. Password visibility toggle
  5. 1-click demo login fill
  6. Notification bell drawer
  7. Real-time order status + automatic wait-time polling & ready alerts
  8. Dark / Light theme toggle
  9. Global live toast notifications with subtle audio chime for Ready alerts
 10. Instant client-side menu search
 11. 1-click copy Pickup OTP
 12. Payment method UPI field toggle on cart page
 13. Dish photo preview in manager form
*/

function drawIcons() {
    if (window.lucide) {
        window.lucide.createIcons({ attrs: { "stroke-width": 1.75 } });
    }
}

function setupMobileMenu() {
    const toggleButton = document.getElementById("nav-toggle");
    const menu = document.getElementById("nav-menu");
    if (!toggleButton || !menu) return;

    toggleButton.addEventListener("click", function () {
        const isOpen = menu.classList.toggle("is-open");
        toggleButton.setAttribute("aria-expanded", String(isOpen));
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && menu.classList.contains("is-open")) {
            menu.classList.remove("is-open");
            toggleButton.setAttribute("aria-expanded", "false");
            toggleButton.focus();
        }
    });
}

function setupFlashClose() {
    document.querySelectorAll(".flash__close").forEach(function (button) {
        button.addEventListener("click", function () {
            const flashEl = button.closest(".flash");
            if (flashEl) flashEl.remove();
        });
    });
}

function setupPasswordToggles() {
    document.querySelectorAll("[data-toggle-password]").forEach(function (button) {
        button.addEventListener("click", function () {
            const targetId = button.getAttribute("data-toggle-password");
            const input = document.getElementById(targetId);
            if (!input) return;
            const showing = input.type === "text";
            input.type = showing ? "password" : "text";
            button.setAttribute("aria-pressed", String(!showing));
            button.setAttribute("aria-label", showing ? "Show password" : "Hide password");
            button.innerHTML = showing
                ? '<i data-lucide="eye"></i>'
                : '<i data-lucide="eye-off"></i>';
            drawIcons();
        });
    });
}

function setupDemoLoginButtons() {
    const buttons = document.querySelectorAll("[data-demo-email]");
    const emailInput = document.getElementById("email");
    const passInput = document.getElementById("password");
    if (!emailInput || !passInput) return;

    buttons.forEach(function (button) {
        button.addEventListener("click", function () {
            emailInput.value = button.getAttribute("data-demo-email") || "";
            passInput.value = button.getAttribute("data-demo-pass") || "";
            emailInput.focus();
        });
    });
}

function playReadyChime() {
    try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtx) return;
        const ctx = new AudioCtx();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(660, ctx.currentTime);
        osc.frequency.setValueAtTime(880, ctx.currentTime + 0.12);
        gain.gain.setValueAtTime(0.08, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + 0.36);
    } catch (e) {
        // Ignore if browser blocks autoplay audio
    }
}

function showToast(message, variant) {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = "toast" + (variant === "ready" ? " toast--ready" : "");
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(function () {
        toast.classList.add("is-fading");
        setTimeout(function () { toast.remove(); }, 300);
    }, variant === "ready" ? 7000 : 5000);
}

function setupNotifications() {
    const bell = document.getElementById("notif-bell");
    const drawer = document.getElementById("notif-drawer");
    const list = document.getElementById("notif-list");
    const badge = document.getElementById("notif-badge");
    const markBtn = document.getElementById("notif-mark-read");
    if (!bell || !drawer || !list) return;

    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text || "";
        return div.innerHTML;
    }

    async function loadNotifications() {
        try {
            const res = await fetch("/api/notifications", { credentials: "same-origin" });
            if (!res.ok) return;
            const data = await res.json();

            if (badge) {
                badge.textContent = String(data.unread || 0);
                badge.hidden = !data.unread;
            }

            if (!data.notifications || data.notifications.length === 0) {
                list.innerHTML = '<p class="text-small text-soft" style="margin: 0;">No notifications yet.</p>';
                return;
            }

            list.innerHTML = data.notifications.map(function (n) {
                const cls = n.is_read ? "notif-item" : "notif-item is-unread";
                return '<div class="' + cls + '">' + escapeHtml(n.message) + "</div>";
            }).join("");
        } catch (e) {}
    }

    bell.addEventListener("click", function () {
        const willOpen = drawer.hidden;
        drawer.hidden = !willOpen;
        bell.setAttribute("aria-expanded", String(willOpen));
        if (willOpen) loadNotifications();
    });

    document.addEventListener("click", function (event) {
        if (!drawer.hidden && !drawer.contains(event.target) && !bell.contains(event.target)) {
            drawer.hidden = true;
            bell.setAttribute("aria-expanded", "false");
        }
    });

    if (markBtn) {
        markBtn.addEventListener("click", async function () {
            const csrf = bell.getAttribute("data-csrf") || "";
            await fetch("/api/notifications/read", {
                method: "POST",
                credentials: "same-origin",
                headers: { "X-CSRF-Token": csrf }
            });
            loadNotifications();
        });
    }
}

function setupOrderPolling() {
    const marker = document.querySelector("[data-poll-orders='true']");
    if (!marker) return;

    setInterval(async function () {
        try {
            const res = await fetch("/api/orders/status", { credentials: "same-origin" });
            if (!res.ok) return;
            const data = await res.json();
            if (!data.orders) return;

            for (const o of data.orders) {
                const card = document.querySelector('[data-order-id="' + o.id + '"]');
                if (!card) continue;

                // Update live wait time in place without reloading
                const waitEl = card.querySelector('[data-wait-order="' + o.id + '"]');
                if (waitEl && o.live_wait_mins > 0) {
                    const aheadText = o.queue_ahead > 0 ? " (" + o.queue_ahead + " ahead)" : "";
                    waitEl.textContent = "~" + o.live_wait_mins + " min wait" + aheadText;
                }

                // If status changed (e.g. confirmed -> preparing -> ready -> collected), notify & refresh
                const prevStatus = card.getAttribute("data-order-status");
                if (prevStatus && prevStatus !== o.status) {
                    if (o.status === "ready") {
                        showToast("Order #" + o.id + " is READY at " + o.canteen_name + "! Show OTP " + o.pickup_otp + ".", "ready");
                        playReadyChime();
                    }
                    setTimeout(function () { window.location.reload(); }, o.status === "ready" ? 1800 : 400);
                    return;
                }
            }
        } catch (e) {}
    }, 6000);
}

function setupThemeToggle() {
    const saved = localStorage.getItem("qless-theme");
    if (saved === "dark") {
        document.documentElement.setAttribute("data-theme", "dark");
    }

    const btn = document.getElementById("theme-toggle");
    if (!btn) return;

    function syncIcon() {
        const isDark = document.documentElement.getAttribute("data-theme") === "dark";
        btn.innerHTML = isDark ? '<i data-lucide="sun"></i>' : '<i data-lucide="moon"></i>';
        drawIcons();
    }
    syncIcon();

    btn.addEventListener("click", function () {
        const isDark = document.documentElement.getAttribute("data-theme") === "dark";
        if (isDark) {
            document.documentElement.removeAttribute("data-theme");
            localStorage.setItem("qless-theme", "light");
        } else {
            document.documentElement.setAttribute("data-theme", "dark");
            localStorage.setItem("qless-theme", "dark");
        }
        syncIcon();
    });
}

function setupLiveNotificationToasts() {
    const bell = document.getElementById("notif-bell");
    const badge = document.getElementById("notif-badge");
    if (!bell) return;

    let lastSeenId = null;

    async function poll() {
        try {
            const res = await fetch("/api/notifications", { credentials: "same-origin" });
            if (!res.ok) return;
            const data = await res.json();
            if (badge) {
                badge.textContent = String(data.unread || 0);
                badge.hidden = !data.unread;
            }
            if (data.notifications && data.notifications.length > 0) {
                const newest = data.notifications[0];
                if (lastSeenId !== null && newest.id > lastSeenId && !newest.is_read) {
                    const isReady = newest.type === "order_ready";
                    showToast(newest.message, isReady ? "ready" : "default");
                    if (isReady) playReadyChime();
                }
                lastSeenId = newest.id;
            } else if (lastSeenId === null) {
                lastSeenId = 0;
            }
        } catch (e) {}
    }

    poll();
    setInterval(poll, 6000);
}

function setupInstantMenuSearch() {
    const searchInput = document.querySelector('.filter-bar__search input[name="q"]');
    const cards = document.querySelectorAll(".menu-grid .dish-card");
    if (!searchInput || cards.length === 0) return;

    searchInput.addEventListener("input", function () {
        const term = searchInput.value.trim().toLowerCase();
        cards.forEach(function (card) {
            const text = card.textContent.toLowerCase();
            card.style.display = !term || text.includes(term) ? "" : "none";
        });
    });
}

function setupCopyOtpButtons() {
    document.querySelectorAll("[data-copy-otp]").forEach(function (btn) {
        btn.addEventListener("click", async function () {
            const otp = btn.getAttribute("data-copy-otp") || "";
            try {
                await navigator.clipboard.writeText(otp);
                showToast("Pickup OTP " + otp + " copied! Show it to canteen staff at pickup.");
            } catch (e) {
                showToast("Your Pickup OTP is " + otp);
            }
        });
    });
}

function setupPaymentMethodToggle() {
    const methodSelect = document.getElementById("payment_method");
    const upiField = document.getElementById("upi-id-field");
    if (!methodSelect || !upiField) return;

    function sync() {
        upiField.hidden = methodSelect.value !== "upi";
    }
    sync();
    methodSelect.addEventListener("change", sync);
}

function setupPhotoPreview() {
    const fileInput = document.getElementById("dish_photo");
    if (!fileInput) return;

    const preview = document.createElement("img");
    preview.className = "dish-preview-img";
    preview.alt = "Selected dish preview";
    preview.hidden = true;
    fileInput.parentElement.appendChild(preview);

    fileInput.addEventListener("change", function () {
        const file = fileInput.files && fileInput.files[0];
        if (!file) {
            preview.hidden = true;
            return;
        }
        preview.src = URL.createObjectURL(file);
        preview.hidden = false;
    });
}

document.addEventListener("DOMContentLoaded", function () {
    drawIcons();
    setupMobileMenu();
    setupFlashClose();
    setupPasswordToggles();
    setupDemoLoginButtons();
    setupNotifications();
    setupOrderPolling();
    setupThemeToggle();
    setupLiveNotificationToasts();
    setupInstantMenuSearch();
    setupCopyOtpButtons();
    setupPaymentMethodToggle();
    setupPhotoPreview();
});
