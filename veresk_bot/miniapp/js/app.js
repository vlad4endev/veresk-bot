/* global VereskStatus, VereskOrder */

const tg = window.Telegram?.WebApp;
const SCREEN_ORDER = ["home", "order", "status", "done"];

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor("#402C60");
  tg.setBackgroundColor("#FAF7FF");

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

  if (tg) {
    tg.BackButton.show(screenName !== "home");
  }

  if (screenName === "status" && window.VereskStatus) {
    window.VereskStatus.openStatusScreen();
  }
  if (screenName === "home" && window.VereskStatus) {
    window.VereskStatus.refreshPreview();
  }
}

window.goTo = goTo;

document.getElementById("btn-order")?.addEventListener("click", () => {
  window.VereskOrder?.reset?.();
  goTo("order");
});

document.getElementById("order-back")?.addEventListener("click", () => goTo("home"));
document.getElementById("status-back")?.addEventListener("click", () => goTo("home"));
document.getElementById("btn-open-status")?.addEventListener("click", () => goTo("status"));
document.getElementById("btn-go-home")?.addEventListener("click", () => goTo("home"));
document.getElementById("btn-go-status")?.addEventListener("click", () => goTo("status"));

const params = new URLSearchParams(window.location.search);
const urlOrderId = params.get("order_id");
if (urlOrderId && window.VereskStatus) {
  window.VereskStatus.setOrderId(urlOrderId);
  document.addEventListener("DOMContentLoaded", () => goTo("status"));
} else {
  document.addEventListener("DOMContentLoaded", () => {
    if (window.VereskStatus) window.VereskStatus.refreshPreview();
  });
}
