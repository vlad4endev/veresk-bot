/* global VereskStatus, VereskOrder */

const tg = window.Telegram?.WebApp;
window.tg = tg;

const SCREEN_ORDER = ["home", "order", "status", "done"];

function hasTelegramAuth() {
  return Boolean(tg?.initData);
}

function updateTelegramGuard() {
  const guard = document.getElementById("tg-guard");
  if (!guard) return;
  guard.classList.toggle("hidden", hasTelegramAuth());
}

window.VereskTelegram = {
  getInitData: () => tg?.initData || "",
  apiHeaders: () => ({
    "Content-Type": "application/json",
    "X-Telegram-Init-Data": tg?.initData || "",
  }),
  hasAuth: hasTelegramAuth,
};

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor("#402C60");
  tg.setBackgroundColor("#FAF7FF");
  updateTelegramGuard();

  try {
    tg.BackButton.onClick(() => {
      const current = getCurrentScreen();
      if (current === "order" && window.VereskOrder?.getStep?.() > 1) {
        window.VereskOrder.prevStep();
        return;
      }
      if (current !== "home") goTo("home");
    });
  } catch (e) {
    console.warn("BackButton unavailable", e);
  }
} else {
  document.addEventListener("DOMContentLoaded", updateTelegramGuard);
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

  if (tg?.BackButton) {
    if (screenName !== "home") tg.BackButton.show();
    else tg.BackButton.hide();
  }

  if (screenName === "status" && window.VereskStatus) {
    window.VereskStatus.openStatusScreen();
  }
  if (screenName === "home" && window.VereskStatus) {
    window.VereskStatus.refreshPreview();
  }
}

window.goTo = goTo;

document.getElementById("order-back")?.addEventListener("click", () => goTo("home"));
document.getElementById("status-back")?.addEventListener("click", () => goTo("home"));
document.getElementById("btn-open-status")?.addEventListener("click", () => goTo("status"));
document.getElementById("btn-go-home")?.addEventListener("click", () => goTo("home"));
document.getElementById("btn-go-status")?.addEventListener("click", () => goTo("status"));

// Старт с order_id — в status.js после загрузки всех скриптов
