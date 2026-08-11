/* global VereskStatus, VereskOrder */

const tg = window.Telegram?.WebApp;
const maxApp = window.WebApp;
window.tg = tg;
window.maxApp = maxApp;

const SCREEN_ORDER = ["home", "order", "status", "done", "wheel"];

function isMaxHost() {
  // MAX Bridge: window.WebApp с initData, без Telegram.WebApp.initData
  return Boolean(maxApp?.initData) && !tg?.initData;
}

function hasMessengerAuth() {
  return Boolean(tg?.initData || maxApp?.initData);
}

function updateMessengerGuard() {
  const guard = document.getElementById("tg-guard");
  if (!guard) return;
  guard.classList.toggle("hidden", hasMessengerAuth());
}

function applyHostClass() {
  const root = document.documentElement;
  root.classList.toggle("is-max", isMaxHost());
  root.classList.toggle("is-telegram", Boolean(tg?.initData));
  if (isMaxHost() || location.search.includes("wheel=1") || location.hash === "#wheel") {
    root.classList.add("is-wheel-host");
  }
}

window.VereskTelegram = {
  getInitData: () => tg?.initData || maxApp?.initData || "",
  apiHeaders: () => {
    const headers = { "Content-Type": "application/json" };
    if (tg?.initData) headers["X-Telegram-Init-Data"] = tg.initData;
    if (maxApp?.initData) {
      headers["X-Max-Init-Data"] = maxApp.initData;
      headers["X-Max-WebApp-Init-Data"] = maxApp.initData;
    }
    return headers;
  },
  hasAuth: hasMessengerAuth,
  isMax: isMaxHost,
};

function bootMessengerShell() {
  applyHostClass();
  updateMessengerGuard();

  if (tg?.initData) {
    try {
      tg.ready();
      tg.expand();
      tg.setHeaderColor?.("#402C60");
      tg.setBackgroundColor?.("#FFFFFF");
    } catch (e) {
      console.warn("Telegram WebApp init", e);
    }
    try {
      tg.BackButton?.onClick?.(() => {
        const current = getCurrentScreen();
        if (current === "wheel") {
          try {
            tg.close();
          } catch (_) {
            /* ignore */
          }
          return;
        }
        if (current === "order" && window.VereskOrder?.getStep?.() > 1) {
          window.VereskOrder.prevStep();
          return;
        }
        if (current !== "home") goTo("home");
      });
    } catch (e) {
      console.warn("BackButton unavailable", e);
    }
    return;
  }

  if (maxApp?.initData) {
    try {
      maxApp.ready?.();
      maxApp.expand?.();
      // часть клиентов MAX — requestFullscreen / viewport
      maxApp.requestFullscreen?.();
    } catch (e) {
      console.warn("MAX WebApp init", e);
    }
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootMessengerShell);
} else {
  bootMessengerShell();
}

function getCurrentScreen() {
  const active = document.querySelector(".screen.active");
  if (!active) return "home";
  return active.id.replace("screen-", "");
}

function goTo(screenName) {
  const screens = document.querySelectorAll(".screen");
  const currentIdx = SCREEN_ORDER.indexOf(getCurrentScreen());
  const targetIdx = SCREEN_ORDER.indexOf(screenName);

  screens.forEach((s) => {
    s.classList.remove("active", "slide-left", "slide-right");
    const id = s.id.replace("screen-", "");
    const idx = SCREEN_ORDER.indexOf(id);
    if (idx < targetIdx) s.classList.add("slide-left");
    else if (idx > targetIdx) s.classList.add("slide-right");
  });

  document.getElementById(`screen-${screenName}`).classList.add("active");
  document.documentElement.classList.toggle("is-wheel-screen", screenName === "wheel");

  if (tg?.BackButton) {
    if (screenName === "home" || screenName === "wheel") tg.BackButton.hide();
    else tg.BackButton.show();
  }

  if (screenName === "status" && window.VereskStatus) {
    window.VereskStatus.openStatusScreen();
  }
  if (screenName === "home" && window.VereskStatus) {
    window.VereskStatus.refreshPreview();
  }
  if (screenName === "wheel" && window.VereskFortuneWheel) {
    window.VereskFortuneWheel.mount();
  }
}

window.goTo = goTo;

document.getElementById("order-back")?.addEventListener("click", () => goTo("home"));
document.getElementById("status-back")?.addEventListener("click", () => goTo("home"));
document.getElementById("btn-open-status")?.addEventListener("click", () => goTo("status"));
document.getElementById("btn-go-home")?.addEventListener("click", () => goTo("home"));
document.getElementById("btn-go-status")?.addEventListener("click", () => goTo("status"));

// Старт с колесом: ?wheel=1, #wheel, Telegram/MAX start_param=wheel / wheel_promo
(function bootWheelDeepLink() {
  try {
    const params = new URLSearchParams(location.search);
    const hash = (location.hash || "").replace(/^#/, "");
    const startParam = String(
      tg?.initDataUnsafe?.start_param ||
        maxApp?.initDataUnsafe?.start_param ||
        params.get("WebAppStartParam") ||
        ""
    ).toLowerCase();
    const wantWheel =
      params.get("wheel") === "1" ||
      params.get("screen") === "wheel" ||
      hash === "wheel" ||
      startParam === "wheel" ||
      startParam.startsWith("wheel_");
    const wantPromo =
      params.get("promo") === "1" ||
      startParam === "wheel_promo" ||
      startParam.includes("promo");
    if (wantWheel) {
      document.documentElement.classList.add("is-wheel-host");
      if (wantPromo) {
        document.documentElement.classList.add("is-wheel-promo");
      }
      const open = () => {
        if (wantPromo) window.VereskFortuneWheel?.setPromoMode?.(true);
        window.VereskFortuneWheel?.open?.();
      };
      document.addEventListener("DOMContentLoaded", open);
      if (document.readyState !== "loading") open();
    }
  } catch (e) {
    console.warn("wheel deeplink", e);
  }
})();

// Старт с order_id — в status.js после загрузки всех скриптов
