/* global goTo, tg */

const STATUS_STEPS = [
  { key: "new", label: "Заказ принят", emoji: "✓" },
  { key: "confirmed", label: "Флорист подтвердил", emoji: "✓" },
  { key: "in_progress", label: "Букет собирается", emoji: "💐" },
  { key: "delivering", label: "Передан курьеру", emoji: "🚗" },
  { key: "delivered", label: "Доставлен", emoji: "🎉" },
];

let activeOrderId = null;
let pollTimer = null;
const POLL_MS = 15000;

function apiHeaders() {
  if (window.VereskTelegram?.apiHeaders) {
    const h = window.VereskTelegram.apiHeaders();
    return { "X-Telegram-Init-Data": h["X-Telegram-Init-Data"] || "" };
  }
  return { "X-Telegram-Init-Data": window.tg?.initData || "" };
}

function renderTimeline(containerId, steps) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const html = (steps || []).map((step) => {
    const cls = step.state || "wait";
    const emoji = cls === "done" ? "✓" : step.emoji || "·";
    const time = step.time ? `<span class="tl-time">${step.time}</span>` : "";
    return `
      <div class="tl-item ${cls}">
        <div class="tl-dot">${emoji}</div>
        <div class="tl-content">
          <span class="tl-title">${step.label}</span>
          ${time}
        </div>
      </div>`;
  }).join("");

  container.innerHTML = html;
}

function normalizeStepState(state) {
  if (state === "done") return "done";
  if (state === "current" || state === "active") return "current";
  return "wait";
}

function renderTimelineFromStatus(containerId, statusKey, apiSteps) {
  if (apiSteps?.length) {
    renderTimeline(
      containerId,
      apiSteps.map((s) => ({
        label: s.label,
        state: normalizeStepState(s.state),
        time: s.time,
        emoji: STATUS_STEPS.find((x) => x.key === s.key)?.emoji,
      }))
    );
    return;
  }

  const curIdx = STATUS_STEPS.findIndex((s) => s.key === statusKey);
  const steps = STATUS_STEPS.map((step, idx) => {
    let state = "wait";
    if (idx < curIdx) state = "done";
    if (idx === curIdx) state = "current";
    return {
      label: step.label,
      state,
      time: state === "wait" ? "Ожидается" : "",
      emoji: step.emoji,
    };
  });
  renderTimeline(containerId, steps);
}

function formatDateShort(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const months = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
    return `${d.getDate()} ${months[d.getMonth()]}`;
  } catch {
    return "";
  }
}

function applyOrderData(data) {
  if (!data) return;
  activeOrderId = data.order_id;
  const st = data.status || {};
  const details = data.details || {};

  const preview = document.getElementById("status-preview");
  const empty = document.getElementById("status-empty");
  if (preview && empty) {
    preview.classList.remove("hidden");
    empty.classList.add("hidden");
  }

  const sub = data.subtitle || `Букет для ${details.recipient || "—"}`;
  const dateStr = formatDateShort(data.created_at);

  document.getElementById("preview-order-id").textContent = `Заказ #${data.order_id}`;
  document.getElementById("preview-order-sub").textContent =
    dateStr ? `${dateStr} · ${sub}` : sub;
  document.getElementById("preview-badge").textContent = st.badge || "—";

  renderTimelineFromStatus("preview-timeline", st.status, st.steps);

  document.getElementById("status-order-id").textContent = data.order_id;
  document.getElementById("status-order-sub").textContent = sub;

  const table = document.getElementById("order-details-table");
  if (table) {
    const rows = [
      ["Клиент", details.name],
      ["Телефон", details.phone],
      ["Получатель", details.recipient],
      ["Дата", details.date],
      ["Повод", details.occasion],
      ["Кто получатель", details.relation],
      ["Бюджет", details.budget],
    ];
    table.innerHTML = rows
      .map(
        ([k, v]) => `
      <div class="detail-row">
        <span class="detail-key">${k}</span>
        <span class="detail-val">${v || "—"}</span>
      </div>`
      )
      .join("");
  }

  renderTimelineFromStatus("status-timeline", st.status, st.steps);
}

async function fetchStatus(orderId) {
  try {
    const resp = await fetch(`/api/order-status/${encodeURIComponent(orderId)}`, {
      headers: apiHeaders(),
    });
    if (!resp.ok) return null;
    return resp.json();
  } catch (e) {
    console.error("Status fetch failed:", e);
    return null;
  }
}

async function fetchActive() {
  try {
    const resp = await fetch("/api/order/active", { headers: apiHeaders() });
    if (!resp.ok) return null;
    const data = await resp.json();
    return data.order;
  } catch (e) {
    console.error("Active order fetch failed:", e);
    return null;
  }
}

async function fetchOrderHistory() {
  try {
    const resp = await fetch("/api/client/orders?limit=8", { headers: apiHeaders() });
    if (!resp.ok) return [];
    const data = await resp.json();
    return data.orders || [];
  } catch (e) {
    console.error("Order history fetch failed:", e);
    return [];
  }
}

function renderOrderHistory(orders) {
  const section = document.getElementById("history-section");
  const list = document.getElementById("order-history-list");
  if (!section || !list) return;

  if (!orders.length) {
    section.classList.add("hidden");
    return;
  }

  list.innerHTML = orders
    .map(
      (o) => `
    <button type="button" class="history-item" data-order-id="${o.order_id}">
      <div class="history-item-top">
        <span class="history-id">№${o.order_id}</span>
        <span class="history-badge">${o.status?.label || "—"}</span>
      </div>
      <div class="history-sub">🎁 ${o.recipient} · 📅 ${o.delivery_date}</div>
    </button>`
    )
    .join("");
  list.querySelectorAll("[data-order-id]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-order-id");
      if (!id) return;
      setOrderId(id);
      if (typeof goTo === "function") goTo("status");
    });
  });
  section.classList.remove("hidden");
}

async function refreshPreview() {
  const order = await fetchActive();
  if (!order) {
    document.getElementById("status-preview")?.classList.add("hidden");
    document.getElementById("status-empty")?.classList.remove("hidden");
  } else {
    applyOrderData(order);
  }
  const history = await fetchOrderHistory();
  renderOrderHistory(history);
}

function openStatusScreen() {
  const id = activeOrderId || new URLSearchParams(window.location.search).get("order_id");
  if (id) loadAndPoll(id);
  else refreshPreview().then(() => {
    if (activeOrderId) loadAndPoll(activeOrderId);
  });
}

async function loadAndPoll(orderId) {
  activeOrderId = orderId;
  const data = await fetchStatus(orderId);
  if (data) {
    applyOrderData(data);
  } else {
    window.tg?.showAlert?.(
      "Не удалось загрузить заказ. Закройте приложение и откройте снова из сообщения бота."
    );
  }

  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const fresh = await fetchStatus(orderId);
    if (fresh) {
      applyOrderData(fresh);
      if (fresh.status?.status === "delivered" || fresh.status?.status === "cancelled") {
        clearInterval(pollTimer);
      }
    }
  }, POLL_MS);
}

function setOrderId(id) {
  activeOrderId = id;
}

window.VereskStatus = {
  refreshPreview,
  openStatusScreen,
  setOrderId,
  renderTimeline,
  fetchStatus,
};

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    if (getActiveScreen() === "home") refreshPreview();
    if (getActiveScreen() === "status" && activeOrderId) fetchStatus(activeOrderId).then(applyOrderData);
  }
});

function getActiveScreen() {
  const active = document.querySelector(".screen.active");
  return active?.id?.replace("screen-", "") || "home";
}

document.addEventListener("DOMContentLoaded", () => {
  const urlOrderId = new URLSearchParams(window.location.search).get("order_id");
  if (urlOrderId) {
    setOrderId(urlOrderId);
    if (typeof goTo === "function") goTo("status");
    return;
  }
  refreshPreview();
});
