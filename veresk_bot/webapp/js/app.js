(function () {
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    tg.setHeaderColor("#2a1f3d");
    tg.setBackgroundColor("#faf7f2");
  }

  const POLL_MS = 4000;
  const params = new URLSearchParams(window.location.search);
  const orderId = params.get("order_id");

  const $ = (id) => document.getElementById(id);

  function showError(text) {
    $("loader").classList.add("hidden");
    $("content").classList.add("hidden");
    $("error").classList.remove("hidden");
    $("error-text").textContent = text;
  }

  function getInitData() {
    return tg?.initData || "";
  }

  async function fetchOrder() {
    if (!orderId) {
      throw new Error("Не указан номер заказа");
    }
    const url = `/api/order?order_id=${encodeURIComponent(orderId)}`;
    const res = await fetch(url, {
      headers: {
        "X-Telegram-Init-Data": getInitData(),
      },
    });
    if (res.status === 401) {
      throw new Error("Откройте страницу через кнопку в боте Telegram");
    }
    if (res.status === 404) {
      throw new Error("Заказ не найден или уже завершён");
    }
    if (!res.ok) {
      throw new Error("Сервер временно недоступен");
    }
    return res.json();
  }

  function renderTimeline(steps) {
    const container = $("timeline");
    container.innerHTML = "";
    steps.forEach((step) => {
      const el = document.createElement("article");
      el.className = `timeline-step ${step.state}`;
      el.innerHTML = `
        <div class="step-marker">${step.icon}</div>
        <div class="step-body">
          <h4>${escapeHtml(step.title)}</h4>
          <p>${escapeHtml(step.subtitle)}</p>
        </div>
      `;
      container.appendChild(el);
    });
  }

  function escapeHtml(str) {
    const d = document.createElement("div");
    d.textContent = str || "";
    return d.innerHTML;
  }

  let lastStatusKey = null;

  function render(data) {
    const st = data.status;
    const details = data.details || {};

    $("loader").classList.add("hidden");
    $("error").classList.add("hidden");
    $("content").classList.remove("hidden");

    $("order-id").textContent = `#${data.order_id}`;
    $("status-icon").textContent = st.icon;
    $("status-label").textContent = st.label;
    $("status-subtitle").textContent = st.subtitle;
    $("progress-fill").style.width = `${st.progress}%`;
    $("progress-pct").textContent = String(st.progress);

    const hero = $("status-hero");
    hero.classList.remove("cancelled", "delivered");
    if (st.status === "cancelled") hero.classList.add("cancelled");
    if (st.status === "delivered") hero.classList.add("delivered");

    renderTimeline(st.steps || []);

    $("d-recipient").textContent = details.recipient || "—";
    $("d-date").textContent = details.date || "—";
    $("d-occasion").textContent = details.occasion || "—";
    $("d-budget").textContent = details.budget || "—";

    if (st.status === "delivered" && lastStatusKey !== "delivered") {
      launchConfetti();
      if (tg?.HapticFeedback) {
        tg.HapticFeedback.notificationOccurred("success");
      }
    }
  }

  function launchConfetti() {
    const layer = document.createElement("div");
    layer.className = "confetti";
    const colors = ["#6b4e9b", "#c9a87c", "#9b7ec8", "#4a8f6a", "#e8d4b0"];
    for (let i = 0; i < 48; i++) {
      const p = document.createElement("span");
      p.style.left = `${Math.random() * 100}%`;
      p.style.top = `${-10 + Math.random() * 20}%`;
      p.style.background = colors[i % colors.length];
      p.style.animationDelay = `${Math.random() * 0.8}s`;
      p.style.animationDuration = `${1.8 + Math.random() * 1.2}s`;
      layer.appendChild(p);
    }
    document.body.appendChild(layer);
    setTimeout(() => layer.remove(), 3500);
  }

  async function refresh() {
    try {
      const data = await fetchOrder();
      const key = data.status?.status;
      if (key !== lastStatusKey && lastStatusKey !== null && tg?.HapticFeedback) {
        tg.HapticFeedback.impactOccurred("light");
      }
      lastStatusKey = key;
      render(data);
      if (data.status?.is_terminal) {
        clearInterval(pollTimer);
      }
    } catch (e) {
      if (lastStatusKey === null) {
        showError(e.message || "Ошибка загрузки");
      }
    }
  }

  let pollTimer;

  async function init() {
    if (!orderId) {
      showError("Ссылка на заказ недействительна. Вернитесь в чат с ботом.");
      return;
    }
    await refresh();
    pollTimer = setInterval(refresh, POLL_MS);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refresh();
    });
  }

  init();
})();
