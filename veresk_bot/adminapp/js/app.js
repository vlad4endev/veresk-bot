/** Админ-панель Veresk — UI на реальных данных API */

(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** Российский номер → 10 национальных цифр, иначе "". */
  function phoneNationalDigits(phone) {
    let digits = String(phone || "").replace(/\D/g, "");
    if (digits.length === 11 && (digits[0] === "7" || digits[0] === "8")) {
      digits = digits.slice(1);
    }
    return digits.length === 10 ? digits : "";
  }

  /** Видимый формат: +7(999)999-99-99. Пустая строка, если номер невалиден. */
  function formatPhoneDisplay(phone) {
    const d = phoneNationalDigits(phone);
    if (!d) return "";
    return `+7(${d.slice(0, 3)})${d.slice(3, 6)}-${d.slice(6, 8)}-${d.slice(8, 10)}`;
  }

  /** tel: href в виде +79999999999. Пустая строка, если номер невалиден. */
  function phoneTelHref(phone) {
    const d = phoneNationalDigits(phone);
    return d ? `+7${d}` : "";
  }

  /** Чип телефона: кликабельный tel: при валидном номере, иначе экранированный текст. */
  function phoneContactChipHtml(phone) {
    const raw = String(phone || "").trim();
    if (!raw) return "";
    const display = formatPhoneDisplay(raw);
    const tel = phoneTelHref(raw);
    if (display && tel) {
      return `<span class="contact-chip"><span class="ci2 ph">☎</span><a class="phone-link" href="tel:${esc(tel)}">${esc(display)}</a></span>`;
    }
    return `<span class="contact-chip"><span class="ci2 ph">☎</span>${esc(raw)}</span>`;
  }

  function initials(n) {
    const p = String(n || "").trim().split(/\s+/);
    return ((p[0]?.[0] || "") + (p[1]?.[0] || "")).toUpperCase() || "?";
  }

  function fmtNum(n) {
    return String(n ?? 0).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  const state = {
    curClient: null,
    curCampaign: null,
    curPerson: null,
    eventsDays: 7,
    eventsExpanded: false,
    eventsCache: [],
    wizard: {
      segment: "regular",
      audienceMode: "segment", // segment | pick
      selectedCustomers: [], // [{id, name, phone, phone_masked, messengers}]
      message: "",
      when: "now",
      date: "",
      time: "10:00",
      channels: ["tg"],
      willSend: null,
      keepMessage: false,
      media: null, // { media_path, media_filename, media_mime, media_kind, localUrl }
    },
    tgPhone: "",
    maxPhone: "",
    step: 0,
  };

  const panels = $$(".panel");
  const navItems = $$(".nav-item, .bnav-item[data-nav]");

  const PERM_CATALOG_FALLBACK = [
    { id: "home", label: "Главная" },
    { id: "chats", label: "Чаты" },
    { id: "clients", label: "Клиенты" },
    { id: "wheel", label: "Фортуна" },
    { id: "aichat", label: "ИИ чат" },
    { id: "bots", label: "Боты" },
    { id: "settings", label: "Настройки" },
    { id: "access", label: "Доступ (сотрудники)" },
  ];
  const PERM_DEFAULTS = {
    home: true,
    clients: true,
    chats: true,
    bots: false,
    wheel: false,
    settings: false,
    aichat: false,
    access: false,
  };
  let authMe = null;
  let permCatalog = PERM_CATALOG_FALLBACK.slice();

  function normalizePerms(raw) {
    const out = {};
    PERM_CATALOG_FALLBACK.forEach((p) => {
      out[p.id] = false;
    });
    if (!raw || typeof raw !== "object") {
      return Object.assign(out, PERM_DEFAULTS);
    }
    Object.keys(out).forEach((k) => {
      out[k] = !!raw[k];
    });
    return out;
  }

  function canAccess(section) {
    const perms = (authMe && authMe.permissions) || {};
    if (authMe && (authMe.source === "env" || authMe.role === "admin")) return true;
    if (section === "compose" || section === "detail" || section === "personal") {
      return !!perms.home;
    }
    if (section === "client") return !!perms.clients;
    if (section === "settings") return !!(perms.settings || perms.access);
    return !!perms[section];
  }

  function firstAllowedTab() {
    const order = ["home", "chats", "clients", "wheel", "aichat", "bots", "settings"];
    return order.find((t) => canAccess(t)) || "home";
  }

  function applyNavPermissions() {
    const perms = normalizePerms(authMe && authMe.permissions);
    if (authMe && (authMe.source === "env" || authMe.role === "admin")) {
      Object.keys(perms).forEach((k) => {
        perms[k] = true;
      });
    }
    $$(".nav-item[data-nav], .bnav-item[data-nav]").forEach((el) => {
      const key = el.dataset.nav;
      el.hidden = key ? !perms[key] && !(key === "settings" && (perms.settings || perms.access)) : false;
      if (key === "settings") el.hidden = !(perms.settings || perms.access);
    });
    $$(".bnav-item.bnav-create, .create-btn").forEach((el) => {
      el.hidden = !perms.home;
    });
    const mtopSettings = $("#mtopSettings");
    if (mtopSettings) mtopSettings.hidden = !(perms.settings || perms.access);
    const settingsBotsLink = $("#settingsBotsLink");
    if (settingsBotsLink) settingsBotsLink.hidden = !perms.bots;
    $$(".settings-tab").forEach((tab) => {
      const pane = tab.dataset.settings;
      if (pane === "users") tab.hidden = !perms.access;
      else tab.hidden = !perms.settings;
    });
  }

  const HIDE_BNAV_TABS = new Set([
    "compose",
    "detail",
    "personal",
    "client",
    "settings",
    "bots",
    "wheel",
  ]);

  function go(tab) {
    if (tab === "accounts") tab = "settings";
    const gateTab =
      ({ compose: "home", detail: "home", personal: "home", client: "clients" })[tab] || tab;
    if (!canAccess(gateTab === "settings" ? "settings" : gateTab) && !canAccess(tab)) {
      tab = firstAllowedTab();
    }
    panels.forEach((p) => p.classList.toggle("active", p.id === tab));
    const navKey =
      ({ compose: "home", detail: "home", personal: "home", client: "clients" })[tab] ||
      tab;
    navItems.forEach((n) => n.classList.toggle("active", n.dataset.nav === navKey));
    document.body.classList.toggle("hide-bnav", HIDE_BNAV_TABS.has(tab));
    const mtopHi = $(".mtop-hi");
    const mtopSub = $(".mtop-sub");
    if (mtopHi && mtopSub) {
      if (tab === "clients") {
        mtopHi.textContent = "Клиенты";
        mtopSub.textContent = "База из Posiflora";
      } else if (tab === "wheel") {
        mtopHi.textContent = "Фортуна";
        mtopSub.textContent = "Настройки колеса";
      } else if (tab === "home") {
        mtopHi.textContent = "Здравствуйте";
        mtopSub.textContent = "Что отправим клиентам сегодня?";
      }
    }
    if (tab !== "chats") {
      document.body.classList.remove("tg-thread-open");
      $("#tgShell")?.classList.remove("thread-open");
      stopTgPoll();
      stopMaxSSE();
    }
    if (tab !== "settings" && typeof openStaffCreateModal === "function") {
      openStaffCreateModal(false);
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (tab === "compose") {
      if (typeof resetComposeForm === "function") resetComposeForm();
      setStep(0);
    }
    if (tab === "wheel" && typeof initWheelEditor === "function") initWheelEditor();
    if (tab === "home") loadHome();
    if (tab === "clients") loadClients();
    if (tab === "bots") loadBotsStatus();
    if (tab === "settings") loadSettings();
    if (tab === "aichat") initAiChat();
    if (tab === "chats") loadChats();
  }
  window.go = go;

  // ── auth ────────────────────────────────────────────────────────────────

  let adminKeepaliveTimer = null;
  const ADMIN_KEEPALIVE_MS = 15 * 60 * 1000; // продлеваем вход каждые 15 мин

  function stopAdminKeepalive() {
    if (adminKeepaliveTimer) {
      clearInterval(adminKeepaliveTimer);
      adminKeepaliveTimer = null;
    }
  }

  function startAdminKeepalive() {
    stopAdminKeepalive();
    adminKeepaliveTimer = setInterval(async () => {
      if (!AdminAPI.getToken()) {
        stopAdminKeepalive();
        return;
      }
      try {
        await AdminAPI.me();
      } catch (err) {
        if (err.status === 401) {
          stopAdminKeepalive();
          AdminAPI.setToken("");
          showLogin();
          const errEl = $("#loginErr");
          if (errEl) {
            errEl.textContent =
              "Сессия истекла — войдите снова. Пока вы работаете в панели, вход продлевается сам.";
            errEl.style.display = "block";
          }
        }
      }
    }, ADMIN_KEEPALIVE_MS);
  }

  async function refreshSideUser() {
    try {
      const me = await AdminAPI.me();
      authMe = me;
      if (Array.isArray(me.permission_catalog) && me.permission_catalog.length) {
        permCatalog = me.permission_catalog;
      }
      me.permissions = normalizePerms(me.permissions);
      authMe.permissions = me.permissions;
      applyNavPermissions();
      const name =
        me.name ||
        formatPhoneDisplay(me.phone || me.username) ||
        me.username ||
        "Админ";
      const role = me.role_label || (me.source === "env" ? "Системный" : "Veresk");
      if ($("#sideUserName")) $("#sideUserName").textContent = name;
      if ($("#sideUserRole")) $("#sideUserRole").textContent = role;
      if ($("#sideUserAv")) $("#sideUserAv").textContent = initials(name);
    } catch (_) {
      /* ignore */
    }
  }

  function clearAuthPending() {
    document.documentElement.classList.remove("auth-pending");
    $("#authBoot")?.classList.add("hidden");
  }

  async function showApp() {
    clearAuthPending();
    $("#loginScreen").classList.add("hidden");
    $("#appShell").classList.remove("hidden");
    startAdminKeepalive();
    await refreshSideUser();
    const start = firstAllowedTab();
    if (start === "home") await loadHome();
    else go(start);
  }

  function showLogin() {
    stopAdminKeepalive();
    clearAuthPending();
    $("#appShell").classList.add("hidden");
    $("#loginScreen").classList.remove("hidden");
    setTimeout(focusLogin, 50);
  }

  async function doLogout() {
    try {
      await AdminAPI.logout();
    } catch (_) {
      /* сессия могла уже истечь — всё равно чистим локально */
    }
    AdminAPI.setToken("");
    showLogin();
  }

  $("#btnLogout")?.addEventListener("click", () => {
    void doLogout();
  });

  async function tryAuth() {
    if (!AdminAPI.getToken()) {
      showLogin();
      return;
    }
    try {
      await AdminAPI.me();
      await showApp();
    } catch {
      AdminAPI.setToken("");
      showLogin();
    }
  }

  const LOGIN_KEY = "veresk_admin_login";

  function focusLogin() {
    const userEl = $("#loginUsername");
    const saved = localStorage.getItem(LOGIN_KEY) || "";
    if (saved && !userEl.value) userEl.value = saved;
    (userEl.value ? $("#loginPassword") : userEl).focus();
  }

  function setLoginBusy(busy) {
    $("#loginSubmit").disabled = busy;
    $("#loginSpinner").classList.toggle("hidden", !busy);
    $("#loginSubmitLabel").textContent = busy ? "Входим…" : "Войти";
  }

  function showLoginError(text) {
    const errEl = $("#loginErr");
    errEl.textContent = text;
    errEl.style.display = "block";
    const card = $("#loginForm");
    card.classList.remove("shake");
    void card.offsetWidth; // перезапуск анимации
    card.classList.add("shake");
  }

  // Показать/скрыть пароль
  $("#passToggle")?.addEventListener("click", () => {
    const inp = $("#loginPassword");
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    $("#passToggle .eye-show").classList.toggle("hidden", show);
    $("#passToggle .eye-hide").classList.toggle("hidden", !show);
    $("#passToggle").setAttribute("aria-label", show ? "Скрыть пароль" : "Показать пароль");
    inp.focus();
  });

  // Подсказка про Caps Lock
  $("#loginPassword")?.addEventListener("keyup", (e) => {
    if (typeof e.getModifierState === "function") {
      $("#capsHint").classList.toggle("hidden", !e.getModifierState("CapsLock"));
    }
  });
  $("#loginPassword")?.addEventListener("blur", () => {
    $("#capsHint").classList.add("hidden");
  });

  // Скрываем ошибку, как только начали исправлять
  ["#loginUsername", "#loginPassword"].forEach((sel) => {
    $(sel)?.addEventListener("input", () => {
      $("#loginErr").style.display = "none";
    });
  });

  $("#loginForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = $("#loginUsername").value.trim();
    const pwd = $("#loginPassword").value;
    if (!username) {
      showLoginError("Введите логин");
      $("#loginUsername").focus();
      return;
    }
    if (!pwd) {
      showLoginError("Введите пароль");
      $("#loginPassword").focus();
      return;
    }
    $("#loginErr").style.display = "none";
    setLoginBusy(true);
    try {
      const res = await AdminAPI.login(username, pwd);
      AdminAPI.setToken(res.token);
      localStorage.setItem(LOGIN_KEY, username);
      $("#loginPassword").value = "";
      await showApp();
    } catch (err) {
      if (err.status === 503) {
        showLoginError("Админка не настроена: задайте ADMIN_USERNAME и ADMIN_PASSWORD в .env");
      } else if (err.status === 403) {
        showLoginError("Доступ отключён. Обратитесь к администратору.");
      } else if (err.status === 401) {
        showLoginError("Неверный логин или пароль");
        $("#loginPassword").select();
      } else {
        showLoginError("Сервер недоступен. Попробуйте ещё раз.");
      }
    } finally {
      setLoginBusy(false);
    }
  });

  // ── home ────────────────────────────────────────────────────────────────

  async function loadHome() {
    const eventsBox = $("#eventsList");
    const listBox = $("#campaignsList");
    eventsBox.innerHTML = '<div class="loading">Загрузка…</div>';
    listBox.innerHTML = '<div class="loading">Загрузка…</div>';
    try {
      const [stats, events, campaigns] = await Promise.all([
        AdminAPI.stats(),
        AdminAPI.events(state.eventsDays),
        AdminAPI.campaigns(),
      ]);
      $("#statCustomers").textContent = fmtNum(stats.customers);
      const accLabel =
        stats.accounts_total > 0
          ? `${stats.accounts_ready} из ${stats.accounts_total}`
          : "0";
      $("#statAccounts").textContent = accLabel;
      const sentMonth = stats.sent_month;
      if (stats.delivery_rate != null) {
        $("#statDelivery").textContent = stats.delivery_rate + "%";
        const lbl = $("#statDeliveryLabel");
        if (lbl) {
          lbl.textContent =
            sentMonth != null
              ? `успешно · ${fmtNum(sentMonth)} за месяц`
              : "успешно за месяц";
        }
      } else {
        $("#statDelivery").textContent = sentMonth != null ? fmtNum(sentMonth) : "—";
        const lbl = $("#statDeliveryLabel");
        if (lbl) lbl.textContent = "отправлено за месяц";
      }
      syncEventsFilters();
      renderEvents(events);
      renderCampaigns(campaigns.items || []);
    } catch (err) {
      if (err.status === 401) return showLogin();
      eventsBox.innerHTML = '<div class="empty-state">Не удалось загрузить</div>';
      listBox.innerHTML = '<div class="empty-state">Не удалось загрузить</div>';
      $("#campaignsHead")?.classList.add("is-empty");
      const countEl = $("#campaignsCount");
      if (countEl) countEl.textContent = "";
    }
  }

  function eventIcon(kind) {
    if (kind === "bday") return "🎂";
    if (kind === "anniv") return "💍";
    return "🎉";
  }

  function syncEventsFilters() {
    $$("#wgFilters .wg-f").forEach((btn) => {
      const on = +btn.dataset.days === state.eventsDays;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    const allBtn = $("#wgAll");
    if (allBtn) {
      allBtn.textContent = state.eventsExpanded ? "Свернуть" : "Показать все";
    }
  }

  async function loadEvents(days) {
    const box = $("#eventsList");
    box.innerHTML = '<div class="loading">Загрузка…</div>';
    try {
      const data = await AdminAPI.events(days);
      state.eventsDays = days;
      syncEventsFilters();
      renderEvents(data);
    } catch (err) {
      if (err.status === 401) return showLogin();
      box.innerHTML =
        '<div class="empty-state"><div class="t">Не удалось загрузить события</div><p class="d">Обновите страницу или синхронизируйте базу.</p></div>';
    }
  }

  function eventFromCache(id) {
    return state.eventsCache.find((e) => +e.id === +id) || null;
  }

  function openPersonalFromEvent(e) {
    if (!e) return;
    if (!e.customer_id) {
      alert("Не найден клиент для этого события");
      return;
    }
    const available = parsePersonalChannels(e.channel || e.channel_class);
    if (!available.length || e.channel_class === "none" || e.channel === "нет канала") {
      alert("У клиента нет канала для отправки (Telegram или MAX)");
      return;
    }
    const kindBit = e.kind_label || e.title || "Событие";
    const dateBit = e.next_date_label || e.date_label || "";
    // Предпочитаем channel_class с бэка, если он среди доступных
    const preferred =
      e.channel_class === "max" && available.includes("max")
        ? "max"
        : e.channel_class === "tg" && available.includes("tg")
          ? "tg"
          : available[0];
    openPersonal({
      type: e.kind === "anniv" ? "anniv" : e.kind === "bday" ? "bday" : "plain",
      customer_id: e.customer_id,
      name: e.customer_name,
      contact: e.phone_masked,
      availableChannels: available,
      chanClass: preferred,
      evText: `${kindBit} · ${e.when_label || ""}${dateBit ? " · " + dateBit : ""}`,
      whenClass: e.when_class,
    });
  }

  function renderEvents(payload) {
    const box = $("#eventsList");
    const items = Array.isArray(payload) ? payload : payload?.items || [];
    const meta = Array.isArray(payload) ? {} : payload || {};
    state.eventsCache = items;

    const sub = $("#wgEventsSub");
    if (sub) {
      const total = meta.total ?? items.length;
      const today = meta.today_count ?? items.filter((e) => e.days_until === 0).length;
      const auto = meta.auto_count ?? items.filter((e) => e.auto_send).length;
      const parts = [];
      if (today) parts.push(`${today} сегодня`);
      parts.push(`${total} за ${state.eventsDays} дн.`);
      if (auto) parts.push(`${auto} с авто`);
      sub.textContent = parts.join(" · ") || "Дни рождения и годовщины из Posiflora";
    }

    if (!items.length) {
      box.innerHTML = `<div class="empty-state">
        <div class="t">Нет событий в ближайшие ${state.eventsDays} дн.</div>
        <p class="d">Синхронизируйте клиентов из Posiflora или выберите больший период.</p>
      </div>`;
      return;
    }

    const limit = state.eventsExpanded ? items.length : Math.min(8, items.length);
    const shown = items.slice(0, limit);
    const hidden = items.length - shown.length;

    box.innerHTML =
      shown
        .map((e) => {
          const unreachable = e.channel_class === "none" || e.channel === "нет канала";
          const dateBit = e.next_date_label || e.date_label || "";
          const kindBit = e.kind_label || e.title || "Событие";
          const subBits = [e.phone_masked, e.channel, dateBit].filter(Boolean).join(" · ");
          const sendLabel = e.greeted_today
            ? "Ещё раз"
            : e.kind === "bday" || e.kind === "anniv"
              ? "Поздравить"
              : "Написать";
          const statusBits = [];
          if (e.auto_send) statusBits.push('<span class="ev-pill auto">Авто</span>');
          if (e.greeted_today) statusBits.push('<span class="ev-pill ok">Уже сегодня</span>');
          return `<article class="ev${e.auto_send ? " is-auto" : ""}${e.greeted_today ? " is-greeted" : ""}${unreachable ? " is-off" : ""}" data-id="${e.id}">
          <div class="ev-ic" aria-hidden="true">${eventIcon(e.kind)}</div>
          <button type="button" class="ev-main" data-client="${e.customer_id}" title="Открыть карточку клиента">
            <div class="ev-n"><span class="ev-kind">${esc(kindBit)}</span><span class="ev-sep">·</span><span class="ev-who">${esc(e.customer_name || "Клиент")}</span></div>
            <div class="ev-s">${esc(subBits)}</div>
            ${statusBits.length ? `<div class="ev-tags">${statusBits.join("")}</div>` : ""}
          </button>
          <span class="ev-when ${esc(e.when_class || "later")}">${esc(e.when_label || "—")}</span>
          <div class="ev-act">
            <label class="ev-auto" title="Автопоздравление в день события">
              <input type="checkbox" data-auto="${e.id}" ${e.auto_send ? "checked" : ""}>
              <span class="ev-auto-ui" aria-hidden="true"><span class="ev-auto-knob"></span></span>
              <span class="ev-auto-txt">Авто</span>
            </label>
            <button type="button" class="ev-cta${unreachable ? " is-disabled" : ""}" data-greet="${e.id}" ${unreachable ? "disabled" : ""}>
              ${esc(sendLabel)}
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
            </button>
          </div>
        </article>`;
        })
        .join("") +
      (hidden > 0
        ? `<button type="button" class="wg-more" id="wgMore">Показать ещё ${hidden}</button>`
        : "");
  }

  // Делегирование кликов — не ломается при перерисовке списка
  $("#eventsList")?.addEventListener("click", (ev) => {
    const greet = ev.target.closest("[data-greet]");
    if (greet) {
      ev.preventDefault();
      openPersonalFromEvent(eventFromCache(greet.dataset.greet));
      return;
    }
    const clientBtn = ev.target.closest("[data-client]");
    if (clientBtn && !ev.target.closest("[data-auto], [data-greet], .ev-auto")) {
      openClientById(+clientBtn.dataset.client);
      return;
    }
    if (ev.target.closest("#wgMore")) {
      state.eventsExpanded = true;
      syncEventsFilters();
      renderEvents({
        items: state.eventsCache,
        total: state.eventsCache.length,
        today_count: state.eventsCache.filter((e) => e.days_until === 0).length,
        auto_count: state.eventsCache.filter((e) => e.auto_send).length,
      });
    }
  });

  $("#eventsList")?.addEventListener("change", async (ev) => {
    const inp = ev.target.closest("[data-auto]");
    if (!inp) return;
    const id = +inp.dataset.auto;
    const row = inp.closest(".ev");
    row?.classList.toggle("is-auto", inp.checked);
    const cached = eventFromCache(id);
    if (cached) cached.auto_send = inp.checked;
    try {
      await AdminAPI.setEventAuto(id, inp.checked);
    } catch (err) {
      inp.checked = !inp.checked;
      row?.classList.toggle("is-auto", inp.checked);
      if (cached) cached.auto_send = inp.checked;
      alert("Не удалось сохранить автопоздравление");
    }
  });

  function recipientStatusMeta(status) {
    const map = {
      pending: { label: "В очереди", cls: "sending" },
      sent: { label: "Отправлено", cls: "done" },
      delivered: { label: "Доставлено", cls: "done" },
      failed: { label: "Ошибка", cls: "err" },
    };
    return map[status] || { label: status || "—", cls: "neutral" };
  }

  function formatSentAt(iso) {
    if (!iso) return "—";
    const s = String(iso);
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    if (!m) return s.slice(0, 16);
    const months = [
      "янв",
      "фев",
      "мар",
      "апр",
      "мая",
      "июн",
      "июл",
      "авг",
      "сен",
      "окт",
      "ноя",
      "дек",
    ];
    return `${+m[3]} ${months[+m[2] - 1] || m[2]} · ${m[4]}:${m[5]}`;
  }

  function msgStatusLabel(status) {
    return (
      {
        pending: "В очереди",
        sent: "Отправлено",
        delivered: "Доставлено",
        failed: "Ошибка",
        queued: "В очереди",
      }[status] ||
      status ||
      "—"
    );
  }

  function renderCampaigns(items) {
    const box = $("#campaignsList");
    const head = $("#campaignsHead");
    const countEl = $("#campaignsCount");
    const n = items.length;
    if (countEl) {
      countEl.textContent = n
        ? n === 1
          ? "1 рассылка"
          : `${fmtNum(n)} рассылок`
        : "Пока пусто";
    }
    if (head) head.classList.toggle("is-empty", !n);
    if (!n) {
      box.innerHTML = `<div class="empty-rich">
        <div class="er-ic" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
            <path d="M4 6h16v12H4z"/><path d="m4 7 8 6 8-6"/>
          </svg>
        </div>
        <div class="t">Пока нет рассылок</div>
        <p class="d">Создайте первую — напишите постоянным клиентам или всем из базы за три шага.</p>
        <button class="btn primary" type="button" onclick="go('compose')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14M5 12h14"/></svg>
          Создать рассылку
        </button>
      </div>`;
      return;
    }
    const dot = {
      sending: "var(--tg)",
      plan: "var(--warn)",
      done: "var(--ok)",
      draft: "var(--ink-3)",
      err: "#c0492f",
    };
    box.innerHTML = items
      .map((c) => {
        const chans = String(c.channels || "")
          .split(",")
          .filter(Boolean)
          .map((ch) => {
            const t = ch.trim();
            const cls = t === "MAX" ? "max" : "tg";
            return `<span class="chan ${cls}">${esc(t)}</span>`;
          })
          .join("");
        let res = c.when_short || c.when || "";
        if (c.status === "sending") {
          res = `${fmtNum(c.ok_count ?? c.sent_count)} из ${fmtNum(c.total_count)}`;
        } else if (c.status === "done") {
          const failBit =
            c.failed_count > 0 ? ` · ${fmtNum(c.failed_count)} ошибок` : "";
          res = `${fmtNum(c.ok_count ?? c.sent_count)} отправлено${failBit}`;
        } else if (c.status === "draft" || c.status === "scheduled") {
          res = `${esc(c.when_short || c.when)} · ${fmtNum(c.total_count)} чел.`;
        }
        return `<button class="rrow" data-cid="${c.id}">
          <span class="em">${esc(c.emoji)}</span>
          <span class="rname">
            <span class="n">${esc(c.title)}</span>
            <span class="who">${esc(c.segment_label)} · ${fmtNum(c.total_count)} чел.${
          c.when && c.status !== "sending"
            ? ` · ${esc(c.when_short || "")}`
            : ""
        }</span>
            <span class="m-status"><span class="d" style="background:${dot[c.status_class] || "var(--ink-3)"}"></span>${esc(c.status_label)}</span>
          </span>
          <span class="col-chan">${chans}</span>
          <span class="col-status">
            <span class="status ${esc(c.status_class)}"><span class="d" style="background:${dot[c.status_class] || "var(--ink-3)"}"></span>${esc(c.status_label)}</span>
            ${res ? `<div class="res">${res}</div>` : ""}
          </span>
          <span class="chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6"/></svg></span>
        </button>`;
      })
      .join("");
    box.querySelectorAll("[data-cid]").forEach((btn) => {
      btn.addEventListener("click", () => openDetail(+btn.dataset.cid));
    });
  }

  // ── detail ──────────────────────────────────────────────────────────────

  async function openDetail(id) {
    go("detail");
    $("#detailBody").innerHTML = '<div class="loading">Загрузка…</div>';
    try {
      const c = await AdminAPI.campaign(id);
      state.curCampaign = c;
      const recipients = await AdminAPI.recipients(id);
      renderDetail(c, recipients);
    } catch {
      $("#detailBody").innerHTML = '<div class="empty-state">Не найдено</div>';
    }
  }
  window.openDetail = openDetail;

  function renderDetail(c, recipients) {
    const started = c.status === "sending" || c.status === "done" || c.status === "error";
    const chans = String(c.channels || "")
      .split(",")
      .filter(Boolean)
      .map((ch) => {
        const t = ch.trim();
        return `<span class="chan ${t === "MAX" ? "max" : "tg"}">${esc(t)}</span>`;
      })
      .join(" ");

    function recipientRowsHtml(list) {
      if (!list.length) {
        return '<tr><td colspan="4" class="empty-state">Нет получателей</td></tr>';
      }
      return list
        .map((r) => {
          const st = recipientStatusMeta(r.status);
          const errTitle = r.error ? ` title="${esc(r.error)}"` : "";
          const errLine =
            r.status === "failed" && r.error
              ? `<div class="h err-h">${esc(r.error)}</div>`
              : "";
          return `<tr>
        <td class="who"><div class="nm">${esc(r.name)}</div><div class="h">${esc(r.phone_masked)}</div>${errLine}</td>
        <td class="hide-mob"><span class="chan ${r.channel === "max" ? "max" : "tg"}">${r.channel === "max" ? "MAX" : "Telegram"}</span></td>
        <td>${esc(formatSentAt(r.sent_at))}</td>
        <td><span class="status ${st.cls}"${errTitle}><span class="d" style="background:currentColor"></span>${esc(st.label)}</span></td>
      </tr>`;
        })
        .join("");
    }

    const msgHtml = esc(c.message).replace(/\n/g, "<br>");
    const recipientsHtml = recipientRowsHtml(recipients.items || []);
    const pending =
      c.pending_count ??
      Math.max(
        0,
        (c.total_count || 0) - (c.ok_count ?? c.sent_count ?? 0) - (c.failed_count || 0)
      );
    const okCount = c.ok_count ?? c.sent_count ?? 0;

    const leftActions = started
      ? `<div class="det-actions">
          <button class="btn primary" id="btnRepeat">Повторить рассылку</button>
        </div>`
      : `<div class="notsent">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16h.01"/></svg>
          <div>${
            c.status === "scheduled"
              ? `Запланирована на ${esc(c.when_short || c.when)}. ${fmtNum(c.total_count)} получателей уже в очереди.`
              : `Черновик: ${fmtNum(c.total_count)} получателей готовы. Нажмите «Отправить сейчас», чтобы запустить.`
          }</div>
        </div>
        <div class="det-actions">
          <button class="btn primary big" id="btnSendNow">Отправить сейчас</button>
          <button class="btn big" id="btnRepeat">Повторить как новую</button>
        </div>`;

    const rightBody = `<div class="subh">${started ? "Как дошло" : "Очередь отправки"}</div>
        <div class="dstrip">
          <div class="stat"><div class="n">${fmtNum(okCount)}</div><div class="l">отправлено</div></div>
          <div class="stat"><div class="n">${fmtNum(pending)}</div><div class="l">в очереди</div></div>
          <div class="stat"><div class="n">${fmtNum(c.failed_count)}</div><div class="l">ошибок</div></div>
          <div class="stat"><div class="n">${fmtNum(c.total_count)}</div><div class="l">всего</div></div>
        </div>
        <div class="subh">Получатели <span class="rcount">· ${fmtNum(recipients.total ?? (recipients.items || []).length)} человек</span></div>
        <div class="searchbox">
          <svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
          <input type="text" id="rSearch" placeholder="Поиск по имени или телефону">
        </div>
        <div class="tbl-wrap" style="margin-top:12px">
          <table><thead><tr><th>Клиент</th><th class="hide-mob">Где</th><th>Когда</th><th>Статус</th></tr></thead>
          <tbody id="rBody">${recipientsHtml}</tbody></table>
        </div>`;

    $("#detailBody").innerHTML = `
      <div class="dhead">
        <span class="em">${esc(c.emoji)}</span>
        <div><div class="n">${esc(c.title)}</div></div>
        <div class="spacer"></div>
        <span class="status ${esc(c.status_class)}">${esc(c.status_label)}</span>
      </div>
      <div class="detail-grid">
        <div class="detail-left">
          <div class="subh" style="margin-bottom:10px">Текст сообщения</div>
          <div class="phone msgcard phone-sticky">
            <div class="ptop"><div class="dot">V</div>Veresk</div>
            <div class="bubble">${
              c.has_media && c.media_path
                ? `<div class="detail-media"><img src="${esc(
                    AdminAPI.campaignMediaUrl(c.media_path)
                  )}" alt=""></div>`
                : ""
            }${msgHtml}<div class="tm">${started ? "✓✓" : ""}</div></div>
          </div>
          ${leftActions}
        </div>
        <div class="detail-right">
          <div class="dmeta">
            <div class="mi"><div class="k">Когда</div><div class="v">${esc(c.when)}</div></div>
            <div class="mi"><div class="k">Кому</div><div class="v">${esc(c.segment_label)} · ${fmtNum(c.total_count)} чел.</div></div>
            <div class="mi"><div class="k">Где</div><div class="v">${chans}</div></div>
          </div>
          ${rightBody}
        </div>
      </div>`;

    $("#btnSendNow")?.addEventListener("click", async () => {
      await AdminAPI.patchCampaign(c.id, { send_now: true });
      openDetail(c.id);
    });
    $("#btnRepeat")?.addEventListener("click", () => {
      state.wizard.keepMessage = true;
      const ta = $("#msg");
      if (ta) ta.value = c.message;
      const seg = c.segment || "all";
      state.wizard.segment = seg;
      const chans = String(c.channels || "tg")
        .toLowerCase()
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean);
      const wantTg = chans.some((x) => x === "tg" || x === "telegram");
      const wantMax = chans.some((x) => x === "max");
      $("#chanTg")?.classList.toggle("on", wantTg || (!wantTg && !wantMax));
      $("#chanTg")?.setAttribute(
        "aria-pressed",
        $("#chanTg")?.classList.contains("on") ? "true" : "false"
      );
      $("#chanMax")?.classList.toggle("on", wantMax);
      $("#chanMax")?.setAttribute(
        "aria-pressed",
        wantMax ? "true" : "false"
      );

      if (seg === "selected") {
        const byId = new Map();
        (recipients.items || []).forEach((r) => {
          if (!r.customer_id || byId.has(r.customer_id)) return;
          byId.set(r.customer_id, {
            id: r.customer_id,
            name: r.name || "Клиент",
            phone: r.phone || "",
            phone_masked: r.phone_masked || r.phone || "",
            messengers: null,
          });
        });
        state.wizard.selectedCustomers = [...byId.values()];
        state.wizard.audienceMode = "pick";
        $$(".aud-mode-btn").forEach((b) => {
          const on = b.dataset.aud === "pick";
          b.classList.toggle("on", on);
          b.setAttribute("aria-selected", on ? "true" : "false");
        });
        const segBlock = $("#audSegmentBlock");
        const pickBlock = $("#audPickBlock");
        if (segBlock) segBlock.hidden = true;
        if (pickBlock) pickBlock.hidden = false;
        renderPickSelected();
      } else {
        state.wizard.audienceMode = "segment";
        state.wizard.selectedCustomers = [];
        $$(".aud-mode-btn").forEach((b) => {
          const on = b.dataset.aud === "segment";
          b.classList.toggle("on", on);
          b.setAttribute("aria-selected", on ? "true" : "false");
        });
        const segBlock = $("#audSegmentBlock");
        const pickBlock = $("#audPickBlock");
        if (segBlock) segBlock.hidden = false;
        if (pickBlock) pickBlock.hidden = true;
        $$("#s0 .choice").forEach((btn) =>
          btn.classList.toggle("on", btn.dataset.seg === seg)
        );
      }
      go("compose");
      setStep(1);
    });
    $("#rSearch")?.addEventListener("input", async () => {
      const q = $("#rSearch").value.trim();
      const data = await AdminAPI.recipients(c.id, { search: q });
      const body = $("#rBody");
      if (!body) return;
      body.innerHTML = recipientRowsHtml(data.items || []);
    });
  }

  // ── clients ─────────────────────────────────────────────────────────────

  let clientSegment = "all";
  let clientSearch = "";
  let clientsSearchTimer = null;

  function clientPhoneUnderNameHtml(phone) {
    const raw = String(phone || "").trim();
    if (!raw) return `<span class="ph">нет телефона</span>`;
    const display = formatPhoneDisplay(raw) || raw;
    const tel = phoneTelHref(raw);
    if (tel) {
      return `<span class="ph"><a href="tel:${esc(tel)}" data-stop>${esc(display)}</a></span>`;
    }
    return `<span class="ph">${esc(display)}</span>`;
  }

  function clientSegmentPillHtml(c) {
    const seg = String(c.segment || "all");
    const cls = ["regular", "new", "inactive"].includes(seg) ? seg : "other";
    const label = c.segment_label || seg;
    return `<span class="seg-pill ${esc(cls)}"><span class="d"></span>${esc(label)}</span>`;
  }

  function clientChannelsHtml(channels, messengers) {
    if (messengers && (messengers.tg || messengers.max)) {
      return messengerBadgesHtml(messengers);
    }
    const parts = String(channels || "")
      .split(",")
      .map((ch) => ch.trim())
      .filter(Boolean);
    if (!parts.length) {
      return `<span class="ch-none">нет канала</span>`;
    }
    return parts
      .map((t) => {
        const cls = t === "MAX" ? "max" : "tg";
        return `<span class="chan ${cls}">${esc(t)}</span>`;
      })
      .join("");
  }

  function clientLastOrderHtml(c) {
    const label = c.last_order_label;
    if (!label) {
      return `<div class="last-order muted"><span class="lo">Нет заказов</span></div>`;
    }
    return `<div class="last-order"><span class="lo">${esc(label)}</span></div>`;
  }

  function clientNextEventHtml(ev) {
    if (!ev) return '<span class="nev-none">—</span>';
    let when = ev.when_label;
    if (ev.days_until > 30 && ev.next_date) {
      const [y, m, d] = ev.next_date.split("-");
      const months = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
      when = `${+d} ${months[+m - 1] || m}`;
    }
    const soonCls =
      ev.days_until === 0 ? " today" : ev.days_until <= 7 ? " soon" : "";
    return `<span class="nev${soonCls}">
      <span class="nev-ic">${eventIcon(ev.kind)}</span>
      <span class="nev-b"><span class="nev-t">${esc(ev.title)}</span><span class="nev-d">${esc(when)}</span></span>
    </span>`;
  }

  async function loadClients() {
    const box = $("#clientsBody");
    box.innerHTML = '<tr><td colspan="6" class="loading">Загрузка…</td></tr>';
    try {
      const params = {
        segment: clientSegment,
        page_size: 100,
      };
      if (clientSearch) params.search = clientSearch;
      const data = await AdminAPI.clients(params);
      if (!data.items.length) {
        const emptyMsg = clientSearch
          ? "Никого не нашли по запросу"
          : "Клиентов пока нет — нажмите «Синхронизировать»";
        box.innerHTML = `<tr><td colspan="6"><div class="empty-state"><div class="t">${emptyMsg}</div></div></td></tr>`;
        $("#clientsHint").textContent = clientSearch ? "0 по запросу" : "0 клиентов";
        return;
      }
      box.innerHTML = data.items
        .map(
          (c) => `<tr data-id="${c.id}">
          <td>
            <div class="cl-who">
              <span class="cl-who-av">${esc(initials(c.name))}</span>
              <div class="cl-who-b">
                <div class="nm">${esc(c.name)}</div>
                ${clientPhoneUnderNameHtml(c.phone)}
              </div>
            </div>
          </td>
          <td>${clientSegmentPillHtml(c)}</td>
          <td><div class="ch-cell-inner">${clientChannelsHtml(c.channels, c.messengers)}</div></td>
          <td class="hide-mob">${clientLastOrderHtml(c)}</td>
          <td>${clientNextEventHtml(c.next_event)}</td>
          <td class="cl-chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6"/></svg></td>
        </tr>`
        )
        .join("");
      box.querySelectorAll("tr[data-id]").forEach((tr) => {
        tr.addEventListener("click", (e) => {
          if (e.target.closest("[data-stop]")) return;
          openClientById(+tr.dataset.id);
        });
      });
      const shown = data.items.length;
      const total = data.total;
      $("#clientsHint").textContent =
        shown === total
          ? `${fmtNum(total)} клиент${total === 1 ? "" : total > 1 && total < 5 ? "а" : "ов"}`
          : `Показано ${fmtNum(shown)} из ${fmtNum(total)}`;
    } catch (err) {
      if (err.status === 401) return showLogin();
      box.innerHTML = '<tr><td colspan="6" class="empty-state">Ошибка загрузки</td></tr>';
    }
  }

  $$("#clients .seg button").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("#clients .seg button").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      clientSegment = btn.dataset.seg || "all";
      loadClients();
    });
  });

  $("#clientsSearch")?.addEventListener("input", () => {
    clearTimeout(clientsSearchTimer);
    clientsSearchTimer = setTimeout(() => {
      clientSearch = ($("#clientsSearch").value || "").trim();
      loadClients();
    }, 280);
  });

  $("#btnSync")?.addEventListener("click", async () => {
    const btn = $("#btnSync");
    btn.disabled = true;
    btn.textContent = "Синхронизация…";
    try {
      const res = await AdminAPI.sync();
      alert(
        res.ok
          ? `Готово: ${res.customers} клиентов, ${res.events} событий, ${res.orders || 0} заказов`
          : "Ошибка: " + (res.error || "unknown")
      );
      await loadClients();
    } catch (err) {
      alert("Ошибка синхронизации: " + (err.data?.error || err.message));
    }
    btn.disabled = false;
    btn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v12M8 11l4 4 4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg> Синхронизировать';
  });

  async function openClientById(id) {
    go("client");
    try {
      const c = await AdminAPI.client(id);
      state.curClient = c;
      $("#clAv").textContent = initials(c.name);
      $("#clName").textContent = c.name;
      $("#clSeg").textContent = c.segment_label;
      let chips = "";
      chips += phoneContactChipHtml(c.phone);
      String(c.channels || "")
        .split(",")
        .forEach((x) => {
          x = x.trim();
          if (!x) return;
          const cl = x === "MAX" ? "max" : "tg";
          chips += `<span class="contact-chip"><span class="ci2 ${cl}">${cl === "max" ? "MX" : "TG"}</span>${esc(x)}</span>`;
        });
      $("#clContacts").innerHTML = chips;
      const bday = (c.events || []).find((e) => e.kind === "bday");
      const anniv = (c.events || []).find((e) => e.kind === "anniv");
      $("#clBday").textContent = bday
        ? `${bday.date_label || bday.date_from}${bday.when_label ? " · " + bday.when_label : ""}`
        : "—";
      $("#clAnniv").textContent = anniv
        ? `${anniv.date_label || anniv.date_from}${anniv.when_label ? " · " + anniv.when_label : ""}`
        : "—";
      $("#clAnnivBtn")?.classList.toggle("hidden", !anniv);
      $("#clSince").textContent = c.since_label || "—";
      $("#clLast").textContent = c.last_order_label || "—";
      const fortuneEl = $("#clFortune");
      if (fortuneEl) {
        const plays = Array.isArray(c.fortune) ? c.fortune : [];
        if (!plays.length) {
          fortuneEl.textContent = "—";
        } else {
          fortuneEl.innerHTML = plays
            .map((p) => {
              const ch = String(p.channel || "").toLowerCase() === "max" ? "MAX" : "TG";
              const prize = esc(p.prize_label || "Приз");
              const disc =
                p.discount_pct != null && p.discount_pct !== ""
                  ? ` (−${esc(p.discount_pct)}%)`
                  : "";
              const when = formatWheelWhen(p.created_at);
              const whenBit = when
                ? `<span class="cl-fortune-when">${esc(when)}</span>`
                : "";
              return `<span class="contact-chip cl-fortune-chip"><span class="ci2 ${ch === "MAX" ? "max" : "tg"}">${ch}</span><span class="cl-fortune-body"><span class="cl-fortune-prize">${prize}${disc}</span>${whenBit}</span></span>`;
            })
            .join(" ");
        }
      }
      $("#clEvents").innerHTML =
        (c.events || [])
          .map((e) => {
            const when = e.when_label || "";
            const dateBit = e.next_date_label || e.date_label || e.date_from || "";
            const auto = e.auto_send ? " auto" : "";
            const greeted = e.greeted_today
              ? `<span class="cev-badge ok">поздравили сегодня</span>`
              : e.auto_send
                ? `<span class="cev-badge auto">авто</span>`
                : "";
            return `<div class="cev${auto}" data-eid="${e.id}">
          <span class="cev-ic">${eventIcon(e.kind)}</span>
          <div class="cev-b">
            <div class="cev-n">${esc(e.kind_label || e.title)}${greeted}</div>
            <div class="cev-d">${esc(dateBit)}${when ? " · " + esc(when) : ""}</div>
          </div>
          <label class="sw cev-sw" title="Автопоздравление">
            <input type="checkbox" data-auto="${e.id}" ${e.auto_send ? "checked" : ""}>
            <span class="track"></span>
          </label>
          <button class="mini-btn" data-kind="${esc(e.kind)}" data-eid="${e.id}">${
              e.kind === "other" ? "Написать" : "Поздравить"
            }</button>
        </div>`;
          })
          .join("") || '<p class="hint">Нет событий в Posiflora</p>';
      $("#clEvents").querySelectorAll("[data-kind]").forEach((btn) => {
        btn.addEventListener("click", () => congratsCurrent(btn.dataset.kind));
      });
      $("#clEvents").querySelectorAll("[data-auto]").forEach((inp) => {
        inp.addEventListener("change", async () => {
          const row = inp.closest(".cev");
          row?.classList.toggle("auto", inp.checked);
          try {
            await AdminAPI.setEventAuto(+inp.dataset.auto, inp.checked);
          } catch {
            inp.checked = !inp.checked;
            row?.classList.toggle("auto", inp.checked);
          }
        });
      });
      const stats = c.order_stats || {};
      const statsEl = $("#clOrderStats");
      if (statsEl) {
        statsEl.textContent = stats.orders_count
          ? `${stats.orders_count} шт · ${Math.round(stats.total_spent).toLocaleString("ru-RU")} ₽ · средний чек ${Number(stats.avg_order).toLocaleString("ru-RU")} ₽`
          : "";
      }
      const ordersBody = $("#clOrdersBody");
      if (ordersBody) {
        ordersBody.innerHTML = (c.orders || [])
          .map(
            (o) => `<tr>
            <td>${esc((o.ordered_at || "").slice(0, 10) || "—")}</td>
            <td class="who"><div class="nm">${esc(o.number ? "№" + o.number : "Заказ")}</div></td>
            <td>${Number(o.amount || 0).toLocaleString("ru-RU")} ₽</td>
            <td class="hide-mob">${esc(o.comment || "—")}</td>
            <td><span class="status done">${esc(o.status || "—")}</span></td>
          </tr>`
          )
          .join("") || '<tr><td colspan="5" class="empty-state">Покупок пока нет</td></tr>';
      }
      const msgBody = $("#clMsgBody");
      msgBody.innerHTML = (c.messages || [])
        .map((m) => {
          const st = recipientStatusMeta(m.status);
          return `<tr>
          <td>${esc(formatSentAt(m.date) !== "—" ? formatSentAt(m.date) : (m.date || "").slice(0, 10) || "—")}</td>
          <td class="who"><div class="nm">${esc(m.title)}</div></td>
          <td class="hide-mob"><span class="chan ${m.channel === "max" ? "max" : "tg"}">${m.channel === "max" ? "MAX" : "Telegram"}</span></td>
          <td><span class="status ${st.cls}">${esc(msgStatusLabel(m.status))}</span></td>
        </tr>`;
        })
        .join("") || '<tr><td colspan="4" class="empty-state">Пока нет сообщений</td></tr>';
    } catch {
      $("#clName").textContent = "Ошибка загрузки";
    }
  }
  window.openClientById = openClientById;

  function congratsCurrent(type) {
    const c = state.curClient;
    if (!c) return;
    const available = parsePersonalChannels(c.channels);
    if (!available.length) {
      alert("У клиента нет канала для отправки (Telegram или MAX)");
      return;
    }
    openPersonal({
      type: type === "anniv" ? "anniv" : type === "bday" ? "bday" : "plain",
      customer_id: c.id,
      name: c.name,
      contact: c.phone_masked || c.phone,
      availableChannels: available,
      chanClass: available[0],
      evText: type === "bday" ? "День рождения" : type === "anniv" ? "Годовщина" : c.segment_label,
      whenClass: "today",
    });
  }
  window.congratsCurrent = congratsCurrent;

  $("#clWrite")?.addEventListener("click", () => congratsCurrent("plain"));
  $("#clOpenChat")?.addEventListener("click", () => {
    const c = state.curClient;
    if (!c?.phone) return alert("У клиента нет телефона");
    openChatWithClient(c);
  });

  async function askAiAboutClient(customer) {
    const c = customer || state.curClient;
    if (!c?.id) return alert("Сначала откройте карточку клиента");
    aiChat.focusCustomerId = +c.id;
    aiChat.focusCustomerName = String(c.name || "");
    go("aichat");
    await refreshAiChatConfig();
    setAiChatEnabled(aiChat.configured);
    const name = c.name || "клиент";
    const phone = c.phone_masked || c.phone || "";
    const prompt =
      `Расскажи всё важное про клиента «${name}»` +
      (phone ? ` (${phone})` : "") +
      ` id=${c.id}: сегмент, заказы, события, заметки, сообщения, анкеты ботов. ` +
      `Предложи, что написать лично, если уместно.`;
    if (aiChat.configured) {
      await sendAiChat(prompt);
    } else {
      alert("Подключите ИИ в Настройках → Сервисы");
    }
  }
  window.askAiAboutClient = askAiAboutClient;

  $("#clAskAi")?.addEventListener("click", () => askAiAboutClient());

  // ── personal ────────────────────────────────────────────────────────────

  /** Разбирает строку каналов клиента/события → ["tg"] | ["max"] | ["tg","max"]. */
  function parsePersonalChannels(raw) {
    const s = String(raw || "").trim();
    if (!s || s === "—" || s === "нет канала" || s === "none") return [];
    const parts = s.split(/[,·|/]+/).map((x) => x.trim()).filter(Boolean);
    const hasTg = parts.some((p) => {
      const t = p.toLowerCase();
      return t === "tg" || t === "telegram" || t === "телеграм";
    });
    const hasMax = parts.some((p) => {
      const t = p.toLowerCase();
      return t === "max" || t === "макс";
    });
    // "TG · MAX" / "Telegram, MAX" уже покрыты split; на всякий случай regex по всей строке
    const list = [];
    if (hasTg || /\b(tg|telegram|телеграм)\b/i.test(s)) list.push("tg");
    if (hasMax || /\b(max|макс)\b/i.test(s)) list.push("max");
    return list;
  }

  function channelLabel(chanClass) {
    return chanClass === "max" ? "MAX" : "Telegram";
  }

  function syncPersonalChannelUi() {
    const p = state.curPerson;
    if (!p) return;
    const available = p.availableChannels || [];
    const selected = p.chanClass === "max" ? "max" : "tg";
    const tgBtn = $("#pChanTg");
    const maxBtn = $("#pChanMax");
    const err = $("#pChanError");
    const sendBtn = $("#pSend");

    if (tgBtn) {
      const on = available.includes("tg");
      tgBtn.hidden = !on;
      tgBtn.disabled = !on;
      tgBtn.classList.toggle("on", on && selected === "tg");
      tgBtn.setAttribute("aria-pressed", on && selected === "tg" ? "true" : "false");
    }
    if (maxBtn) {
      const on = available.includes("max");
      maxBtn.hidden = !on;
      maxBtn.disabled = !on;
      maxBtn.classList.toggle("on", on && selected === "max");
      maxBtn.setAttribute("aria-pressed", on && selected === "max" ? "true" : "false");
    }
    if (err) err.hidden = available.length > 0;
    if (sendBtn) sendBtn.disabled = !available.length || !available.includes(selected);

    p.chan = channelLabel(selected);
    const contact = p.contact || "—";
    if ($("#pContact")) {
      $("#pContact").innerHTML = `<span class="chan ${esc(selected)}">${esc(p.chan)}</span> · ${esc(contact)}`;
    }
  }

  function setPersonalChannel(chan) {
    const p = state.curPerson;
    if (!p) return;
    const next = chan === "max" ? "max" : "tg";
    if (!(p.availableChannels || []).includes(next)) return;
    p.chanClass = next;
    syncPersonalChannelUi();
  }

  function openPersonal(d) {
    const fn = (d.name || "").split(" ")[0] || "друг";
    const tpl = {
      bday: `С днём рождения, ${fn}! 🎂💐\n\nОт всей души поздравляем и дарим вам скидку 15% на любой букет всю неделю. Ваш Veresk 🌷`,
      anniv: `${fn}, поздравляем с годовщиной! 💍\n\nОтметьте этот особенный день красивым букетом — дарим −15%. Ваш Veresk 🌷`,
      plain: `Здравствуйте, ${fn}! 🌷\n\n`,
    };
    const available =
      Array.isArray(d.availableChannels) && d.availableChannels.length
        ? d.availableChannels.filter((c) => c === "tg" || c === "max")
        : parsePersonalChannels(d.chan || d.chanClass || "tg");
    let chanClass = d.chanClass === "max" ? "max" : "tg";
    if (!available.includes(chanClass)) {
      chanClass = available[0] || "tg";
    }
    $("#pAv").textContent = initials(d.name);
    $("#pName").textContent = d.name;
    const ev = $("#pEv");
    ev.textContent = d.evText || "";
    ev.className = "ev-when " + (d.whenClass || "later");
    $("#pmsg").value = tpl[d.type] || tpl.plain;
    updatePPreview();
    $("#pSendLabel").textContent = "Отправить " + fn;
    $("#personalForm")?.classList.remove("hidden");
    $("#personalDone")?.classList.add("hidden");
    state.curPerson = {
      ...d,
      fn,
      availableChannels: available,
      chanClass,
      chan: channelLabel(chanClass),
      contact: d.contact || "—",
      type: d.type || "plain",
    };
    syncPersonalChannelUi();
    adaptPersonalAiChips(state.curPerson.type);
    setPAiOpen(false);
    setPAiStatus("");
    if (pAiUndoRow) pAiUndoRow.hidden = true;
    if (pAiPrompt) pAiPrompt.value = "";
    go("personal");
  }
  window.openPersonal = openPersonal;

  function updatePPreview() {
    $("#ppreview").innerHTML = esc($("#pmsg").value).replace(/\n/g, "<br>");
  }
  $("#pmsg")?.addEventListener("input", updatePPreview);

  $$("#pChanToggles .chan-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      setPersonalChannel(btn.getAttribute("data-channel") || "tg");
    })
  );

  $("#pSend")?.addEventListener("click", async () => {
    const p = state.curPerson;
    if (!p?.customer_id) return alert("Нет клиента");
    const channel = p.chanClass === "max" ? "max" : "tg";
    if (!(p.availableChannels || []).includes(channel)) {
      return alert("У клиента нет канала для отправки (Telegram или MAX)");
    }
    try {
      await AdminAPI.personal({
        customer_id: p.customer_id,
        message: $("#pmsg").value,
        channel,
      });
      $("#doneName").textContent = p.fn;
      $("#doneChan").textContent = channelLabel(channel);
      $("#personalForm")?.classList.add("hidden");
      $("#personalDone")?.classList.remove("hidden");
    } catch (err) {
      alert("Ошибка: " + (err.data?.message || err.data?.error || err.message));
    }
  });

  // ── AI editor (personal message) ─────────────────────────────────────────
  let pAiPrevText = "";
  const pAiEditor = $("#pAiEditor");
  const pAiToggle = $("#pAiToggle");
  const pAiPrompt = $("#pAiPrompt");
  const pAiStatus = $("#pAiStatus");
  const pAiUndoRow = $("#pAiUndoRow");
  const pMsgTa = $("#pmsg");

  /**
   * Подсказки чипов под повод (ДР / годовщина / обычное).
   * Заполните строки — ими ИИ будет пользоваться при клике на чип.
   */
  function adaptPersonalAiChips(type) {
    // type: "bday" | "anniv" | "plain"
    // TODO (ваш вклад): подставьте формулировки под тон Veresk.
    // Можно менять только тексты — ключи bday/anniv/thanks/soft лучше не трогать.
    const defaults = {
      bday: {
        bday: "Тёплое поздравление с днём рождения и скидка 15% на букет на неделю",
        anniv: "Короткое поздравление с днём рождения без акцента на скидку",
        thanks: "Поблагодарить за доверие к салону и поздравить с днём рождения",
        soft: "Очень короткое тёплое поздравление с ДР от Veresk",
      },
      anniv: {
        bday: "Тёплое поздравление с годовщиной со скидкой 15%",
        anniv: "Поздравление с годовщиной, предложить букет со скидкой 15%",
        thanks: "Поблагодарить за то, что отмечают важный день с Veresk",
        soft: "Мягкое поздравление с годовщиной без скидки",
      },
      plain: {
        bday: "Тёплое поздравление с днём рождения и скидка 15% на букет на неделю",
        anniv: "Поздравление с годовщиной, предложить букет со скидкой 15%",
        thanks: "Поблагодарить за заказ, пригласить снова без давления",
        soft: "Мягко напомнить о себе и предложить заглянуть за букетом",
      },
    };
    const map = defaults[type] || defaults.plain;
    $$("#pAiChips .ai-chip").forEach((chip) => {
      const key = chip.dataset.chip;
      if (key && map[key]) chip.dataset.prompt = map[key];
      // Подсветить чип, совпадающий с поводом
      chip.classList.toggle("on", type === key);
    });
  }

  function personalOccasionLabel(type) {
    if (type === "bday") return "день рождения";
    if (type === "anniv") return "годовщина";
    return (state.curPerson && state.curPerson.evText) || "";
  }

  function setPAiOpen(open) {
    if (!pAiEditor || !pAiToggle) return;
    pAiEditor.hidden = !open;
    pAiToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      pAiPrompt?.focus();
      pAiEditor.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function setPAiStatus(text, kind) {
    if (!pAiStatus) return;
    if (!text) {
      pAiStatus.hidden = true;
      pAiStatus.textContent = "";
      pAiStatus.className = "ai-editor-status";
      return;
    }
    pAiStatus.hidden = false;
    pAiStatus.textContent = text;
    pAiStatus.className = "ai-editor-status" + (kind ? " " + kind : "");
  }

  function setPAiBusy(busy) {
    ["pAiGenerate", "pAiImprove", "pAiToggle"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.disabled = busy;
    });
    $$("#pAiChips .ai-chip").forEach((c) => {
      c.disabled = busy;
    });
    if (pAiPrompt) pAiPrompt.disabled = busy;
    const gen = $("#pAiGenerate");
    if (gen) {
      gen.innerHTML = busy
        ? "Генерирую…"
        : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/><circle cx="12" cy="12" r="3.2"/></svg> Сгенерировать`;
    }
  }

  async function runPersonalAiCompose(mode) {
    const prompt = (pAiPrompt?.value || "").trim();
    const current = pMsgTa?.value || "";
    if (mode === "write" && !prompt) {
      setPAiStatus("Кратко опишите, о чём сообщение — или нажмите подсказку сверху", "err");
      pAiPrompt?.focus();
      return;
    }
    if (mode === "improve" && !current.trim()) {
      setPAiStatus("Сначала напишите или вставьте черновик в поле ниже", "err");
      pMsgTa?.focus();
      return;
    }
    const person = state.curPerson || {};
    setPAiBusy(true);
    setPAiStatus(mode === "improve" ? "Улучшаю текст…" : "Пишу текст…");
    try {
      const res = await AdminAPI.aiCompose({
        prompt,
        current_text: current,
        segment: "personal",
        mode,
        client_name: person.fn || (person.name || "").split(" ")[0] || "",
        occasion: personalOccasionLabel(person.type),
      });
      const text = (res.text || "").trim();
      if (!text) throw new Error("empty");
      pAiPrevText = current;
      pMsgTa.value = text;
      updatePPreview();
      if (pAiUndoRow) pAiUndoRow.hidden = false;
      setPAiStatus("Готово — текст вставлен. Превью обновлено.", "ok");
      pMsgTa.focus();
    } catch (err) {
      const detail =
        err.data?.detail ||
        (err.data?.error === "ai_not_configured"
          ? "Подключите ИИ в Настройках → Сервисы"
          : null) ||
        err.message ||
        "Не удалось сгенерировать";
      setPAiStatus(detail, "err");
    }
    setPAiBusy(false);
  }

  pAiToggle?.addEventListener("click", () => {
    const open = pAiToggle.getAttribute("aria-expanded") !== "true";
    setPAiOpen(open);
    if (open) setPAiStatus("");
  });
  $("#pAiClose")?.addEventListener("click", () => setPAiOpen(false));
  $("#pAiGenerate")?.addEventListener("click", () => runPersonalAiCompose("write"));
  $("#pAiImprove")?.addEventListener("click", () => runPersonalAiCompose("improve"));
  $("#pAiUndo")?.addEventListener("click", () => {
    if (pMsgTa && pAiPrevText !== undefined) {
      pMsgTa.value = pAiPrevText;
      updatePPreview();
    }
    if (pAiUndoRow) pAiUndoRow.hidden = true;
    setPAiStatus("Вернули предыдущий текст", "ok");
  });
  $$("#pAiChips .ai-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      $$("#pAiChips .ai-chip").forEach((c) => c.classList.remove("on"));
      chip.classList.add("on");
      if (pAiPrompt) pAiPrompt.value = chip.dataset.prompt || chip.textContent;
      runPersonalAiCompose("write");
    });
  });
  pAiPrompt?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      runPersonalAiCompose("write");
    }
  });

  // ── settings ────────────────────────────────────────────────────────────

  let settingsTab = "accounts";
  let logsFilter = "all";
  let accountsCache = null;

  function firstAllowedSettingsTab() {
    const perms = normalizePerms(authMe && authMe.permissions);
    if (authMe && (authMe.source === "env" || authMe.role === "admin")) {
      return settingsTab || "accounts";
    }
    const order = ["accounts", "bots", "integrations", "users", "logs"];
    // bots settings tab is under settings - needs settings perm; users needs access
    const allowed = order.filter((pane) => {
      if (pane === "users") return !!perms.access;
      return !!perms.settings;
    });
    if (allowed.includes(settingsTab)) return settingsTab;
    return allowed[0] || "accounts";
  }

  function setSettingsTab(name) {
    settingsTab = name || "accounts";
    const perms = normalizePerms(authMe && authMe.permissions);
    const isFull = authMe && (authMe.source === "env" || authMe.role === "admin");
    if (!isFull) {
      if (settingsTab === "users" && !perms.access) settingsTab = firstAllowedSettingsTab();
      if (settingsTab !== "users" && !perms.settings) settingsTab = firstAllowedSettingsTab();
    }
    $$(".settings-tab").forEach((b) =>
      b.classList.toggle("on", b.dataset.settings === settingsTab)
    );
    $$(".settings-pane").forEach((p) =>
      p.classList.toggle("active", p.dataset.pane === settingsTab)
    );
    if (settingsTab === "users") loadUsersPane();
    if (settingsTab === "logs") renderLogsPane();
    if (settingsTab === "integrations") loadIntegrationsPane();
    if (settingsTab === "bots") loadBotsPane();
  }

  async function loadSettings() {
    setSettingsTab(firstAllowedSettingsTab());
    if (canAccess("settings")) {
      await loadAccounts();
      try {
        const s = await AdminAPI.maxSettings();
        updateSettingsGlanceMax(!!s.configured);
      } catch (_) {
        updateSettingsGlanceMax(false);
      }
    }
    if (settingsTab === "bots") loadBotsPane();
    if (settingsTab === "users") loadUsersPane();
    if (settingsTab === "logs") renderLogsPane();
    if (settingsTab === "integrations") loadIntegrationsPane();
  }

  $$(".settings-tab").forEach((btn) => {
    btn.addEventListener("click", () => setSettingsTab(btn.dataset.settings));
  });

  $$("#logsFilter button").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("#logsFilter button").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      logsFilter = btn.dataset.log || "all";
      renderLogsPane();
    });
  });

  async function loadAccounts(opts = {}) {
    const box = $("#accountsList");
    if (!box) return;
    const checkLive = !!opts.check;
    box.innerHTML = checkLive
      ? '<div class="loading">Проверка коннекта…</div>'
      : '<div class="loading">Загрузка…</div>';
    try {
      const data = await AdminAPI.accounts(checkLive ? { check: "1" } : {});
      accountsCache = data;
      const configured = !!data.telethon_configured;
      const hint = $("#tgHint");
      if (hint) {
        hint.textContent = configured
          ? "Ключи заданы. Подключите номер: телефон → код из чата «Telegram» → коннект."
          : "Шаг 1: сохраните API-ключи. Шаг 2: подключите номер телефона.";
      }
      await loadTgApiStatus();
      const tgItems = (data.items || []).filter((a) => a.kind === "tg_userbot");
      const maxUserbotItems = (data.items || []).filter((a) => a.kind === "max_userbot");
      renderMaxUserbotList(maxUserbotItems, data);
      const ready = tgItems.filter((a) => !["warmup", "unavailable", "blocked"].includes(String(a.status || ""))).length;
      const liveOk = tgItems.filter((a) => a.session_ok === true).length;
      updateTgSetupStatus(configured, tgItems.length, ready, checkLive ? liveOk : null);
      updateSettingsGlance(configured, tgItems.length);
      renderTgSessionBanner(tgItems, configured);
      const connectBtn = $("#btnConnectTg");
      if (connectBtn) {
        connectBtn.classList.toggle("is-locked", !configured);
        connectBtn.title = configured ? "Подключить номер" : "Сначала сохраните API-ключи (шаг 1)";
      }
      const checkBtn = $("#btnCheckTgAll");
      if (checkBtn) {
        checkBtn.disabled = !configured || !tgItems.length;
        checkBtn.title = !tgItems.length
          ? "Нет аккаунтов для проверки"
          : "Проверить живые сессии Telegram";
      }
      const apiDetails = $("#tgApiForm");
      if (apiDetails && apiDetails.tagName === "DETAILS") {
        apiDetails.open = !configured;
      }
      if (!tgItems.length) {
        box.innerHTML = `<div class="empty-rich" style="padding:28px 16px">
          <div class="er-ic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="6" y="2" width="12" height="20" rx="3"/><path d="M11 18h2"/></svg></div>
          <div class="t">Нет подключённых номеров</div>
          <p class="d">${configured ? "Нажмите «Подключить», чтобы добавить Telegram-аккаунт." : "Сначала сохраните API-ключи выше — затем подключите номер."}</p>
        </div>`;
      } else {
        box.innerHTML = tgItems.map((a) => renderTgAccountCard(a)).join("");

        box.querySelectorAll("[data-tg-check]").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const id = btn.getAttribute("data-tg-check");
            if (!id) return;
            btn.disabled = true;
            btn.textContent = "…";
            try {
              const res = await AdminAPI.tgCheckAccount(id);
              if (res.ok) {
                alert(
                  "Полный коннект активен" +
                    (res.username ? " · @" + res.username : "") +
                    (res.label ? " · " + res.label : "")
                );
              } else {
                alert("Нет коннекта: " + (res.error || "сессия не авторизована"));
              }
              loadAccounts({ check: true });
            } catch (err) {
              alert(err.data?.error || err.message);
              btn.disabled = false;
              btn.textContent = "Проверить";
            }
          });
        });

        box.querySelectorAll("[data-tg-del]").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const id = btn.getAttribute("data-tg-del");
            if (!id) return;
            if (!confirm("Отключить этот Telegram-аккаунт? Сессия будет удалена."))
              return;
            btn.disabled = true;
            try {
              await AdminAPI.tgDeleteAccount(id);
              loadAccounts();
            } catch (err) {
              alert(err.data?.error || err.message);
              btn.disabled = false;
            }
          });
        });
      }
      if (settingsTab === "bots") loadBotsPane();
    } catch (err) {
      if (err.status === 401) return showLogin();
      box.innerHTML = '<div class="empty-state">Ошибка загрузки</div>';
    }
  }

  async function loadBotsPane() {
    const box = $("#botsOverview");
    if (!box) return;
    let maxConfigured = false;
    let maxMeta = "";
    try {
      const s = await AdminAPI.maxSettings();
      maxConfigured = !!s.configured;
      if (s.bot_username) maxMeta = "@" + s.bot_username;
      else if (s.bot_name) maxMeta = s.bot_name;
      else if (s.token_masked) maxMeta = s.token_masked;
      renderMaxSettings(s);
    } catch (_) {
      renderMaxSettings(null);
    }
    box.innerHTML = maxConfigured
      ? `<span class="status-pill ok"><span class="d"></span>Подключён${maxMeta ? " · " + esc(maxMeta) : ""}</span>`
      : `<span class="status-pill warn"><span class="d"></span>Токен не задан</span>`;
    updateSettingsGlanceMax(maxConfigured);
    // Список личных MAX-номеров
    try {
      const data = accountsCache || (await AdminAPI.accounts());
      accountsCache = data;
      const maxItems = (data.items || []).filter((a) => a.kind === "max_userbot");
      renderMaxUserbotList(maxItems, data);
      if (data.max_userbot_ready) {
        box.innerHTML =
          `<span class="status-pill ok"><span class="d"></span>Личный аккаунт` +
          (maxConfigured ? " + бот" : "") +
          `</span>`;
      }
    } catch (_) {}
  }

  function renderMaxSettings(s) {
    const grid = $("#maxStatusGrid");
    const tokenStatus = $("#maxTokenStatus");
    const whStatus = $("#maxWebhookStatus");
    const hint = $("#maxWebhookHint");
    const urlInput = $("#maxWebhookUrl");
    const floristInput = $("#maxFloristChatId");

    if (!s) {
      if (grid) grid.innerHTML = "";
      return;
    }

    const botLabel = s.bot_username
      ? "@" + s.bot_username
      : s.bot_name || (s.configured ? "Бот подключён" : "Нет бота");
    const tokenSrc = s.from_panel ? "из панели" : s.from_env ? "из .env" : "—";
    const whMode = s.webhook_enabled
      ? "Webhook · мгновенно"
      : s.configured
        ? "Long polling · с задержкой"
        : "Сначала токен";
    const whCls = s.webhook_enabled ? "ok" : s.configured ? "warn" : "muted";

    if (grid) {
      grid.innerHTML = `
        <div class="max-stat ${s.configured ? "ok" : "warn"}">
          <div class="max-stat-k">Бот</div>
          <div class="max-stat-v">${esc(botLabel)}</div>
          <div class="max-stat-s">${s.configured ? esc(tokenSrc) : "Укажите токен ниже"}</div>
        </div>
        <div class="max-stat ${whCls}">
          <div class="max-stat-k">Realtime</div>
          <div class="max-stat-v">${esc(whMode)}</div>
          <div class="max-stat-s">${
            s.webhook_url
              ? esc(s.webhook_url.length > 42 ? s.webhook_url.slice(0, 40) + "…" : s.webhook_url)
              : "URL webhook не задан"
          }</div>
        </div>
        <div class="max-stat muted">
          <div class="max-stat-k">Флорист</div>
          <div class="max-stat-v">${s.florist_chat_id ? esc(String(s.florist_chat_id)) : "Выкл."}</div>
          <div class="max-stat-s">Уведомления об анкетах в MAX</div>
        </div>`;
    }

    if (tokenStatus) {
      if (s.configured) {
        const bits = [];
        if (s.from_env) bits.push(".env");
        if (s.from_panel) bits.push("панель");
        if (s.token_masked) bits.push(s.token_masked);
        tokenStatus.innerHTML =
          '<span class="status-pill ok"><span class="d"></span>Активен' +
          (bits.length ? " · " + esc(bits.join(" · ")) : "") +
          "</span>";
      } else {
        tokenStatus.innerHTML =
          '<span class="status-pill warn"><span class="d"></span>Нужен токен</span>';
      }
    }

    if (whStatus) {
      if (s.webhook_enabled) {
        const src = s.webhook_url_source === "env" ? " · .env" : s.webhook_url_source === "panel" ? " · панель" : "";
        whStatus.innerHTML =
          '<span class="status-pill ok"><span class="d"></span>Включён' +
          esc(src) +
          (s.webhook_secret_set ? " · секрет есть" : "") +
          "</span>";
      } else if (s.webhook_url) {
        whStatus.innerHTML =
          '<span class="status-pill warn"><span class="d"></span>URL есть, нужен токен</span>';
      } else {
        whStatus.innerHTML =
          '<span class="status-pill muted"><span class="d"></span>Необязательно</span>';
      }
    }

    if (hint) {
      hint.textContent = s.suggested_webhook_url
        ? "Обычно: " + s.suggested_webhook_url
        : "Пример: https://admin.veresk-flowers.ru/api/max/webhook";
      hint.dataset.suggest = s.suggested_webhook_url || "";
    }

    if (urlInput && !urlInput.dataset.touched) {
      urlInput.value = s.webhook_url || "";
    }
    if (floristInput && !floristInput.dataset.touched) {
      floristInput.value = s.florist_chat_id ? String(s.florist_chat_id) : "";
    }

    // keep suggest for button
    window.__maxSuggestedWebhook = s.suggested_webhook_url || "";
  }

  async function loadMaxTokenStatus() {
    try {
      const s = await AdminAPI.maxSettings();
      renderMaxSettings(s);
    } catch (_) {}
  }

  function botStatusLabel(status) {
    return (
      {
        online: "Онлайн",
        idle: "Токен ок, процесс молчит",
        offline: "Нет ответа",
        not_configured: "Не настроен",
      }[status] || status || "—"
    );
  }

  function botStatusClass(status) {
    if (status === "online") return "ok";
    if (status === "idle") return "warn";
    if (status === "offline") return "err";
    return "muted";
  }

  function fmtRelTime(iso) {
    if (!iso) return "ещё не было";
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return esc(iso);
    const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (sec < 45) return "только что";
    if (sec < 3600) return Math.floor(sec / 60) + " мин назад";
    if (sec < 86400) return Math.floor(sec / 3600) + " ч назад";
    return Math.floor(sec / 86400) + " дн назад";
  }

  function renderBotCard(kind, data) {
    const isTg = kind === "telegram";
    const title = isTg ? "Telegram-бот" : "MAX-бот";
    const channel = isTg ? "tg" : "max";
    const handle = data.username
      ? "@" + data.username
      : data.name || (isTg ? "BotFather токен" : "Токен не задан");
    const st = data.status || "offline";
    const stClass = botStatusClass(st);
    return `<article class="bot-card bot-${channel}">
      <div class="bot-card-top">
        <div class="bot-card-brand">
          <div class="bot-card-ico ${channel}">${isTg ? "TG" : "MAX"}</div>
          <div>
            <div class="bot-card-title">${title}</div>
            <div class="bot-card-meta">${esc(handle)}</div>
          </div>
        </div>
        <span class="status-pill ${stClass}"><span class="d"></span>${esc(botStatusLabel(st))}</span>
      </div>
      <div class="bot-card-metrics">
        <div><div class="n">${fmtNum(data.starts || 0)}</div><div class="l">Запусков</div><div class="s">сегодня ${fmtNum(data.starts_today || 0)}</div></div>
        <div><div class="n">${fmtNum(data.surveys || 0)}</div><div class="l">Анкет</div><div class="s">сегодня ${fmtNum(data.surveys_today || 0)}</div></div>
        <div><div class="n">${fmtNum(data.starts_total || 0)}</div><div class="l">Всего /start</div><div class="s">с повторами</div></div>
      </div>
      <div class="bot-card-foot">
        <span>Активность: ${fmtRelTime(data.last_seen)}</span>
        ${data.error && st !== "online" ? `<span class="bot-err">${esc(String(data.error).slice(0, 80))}</span>` : ""}
        ${isTg ? "" : `<button type="button" class="btn tiny" data-goto-settings="bots">Настроить</button>`}
      </div>
    </article>`;
  }

  async function loadBotsStatus() {
    const grid = $("#botsStatusGrid");
    const totals = $("#botsTotals");
    if (!grid) return;
    grid.innerHTML = '<div class="loading">Загрузка…</div>';
    try {
      const data = await AdminAPI.botsStatus();
      const t = data.totals || {};
      if (totals) {
        totals.innerHTML = `
          <div class="stat"><div class="n">${fmtNum(t.starts || 0)}</div><div class="l">Запусков всего</div></div>
          <div class="stat"><div class="n">${fmtNum(t.surveys || 0)}</div><div class="l">Анкет всего</div></div>
          <div class="stat"><div class="n">${fmtNum(t.starts_today || 0)}</div><div class="l">Сегодня запусков</div></div>
          <div class="stat"><div class="n">${fmtNum(t.surveys_today || 0)}</div><div class="l">Сегодня анкет</div></div>`;
      }
      grid.innerHTML =
        renderBotCard("telegram", data.telegram || {}) +
        renderBotCard("max", data.max || {});
      grid.querySelectorAll("[data-goto-settings]").forEach((b) => {
        b.addEventListener("click", () => {
          go("settings");
          setSettingsTab(b.dataset.gotoSettings || "bots");
        });
      });
    } catch (err) {
      if (err.status === 401) return showLogin();
      grid.innerHTML = '<div class="empty-state">Не удалось загрузить статус ботов</div>';
    }
  }

  $("#btnBotsRefresh")?.addEventListener("click", () => loadBotsStatus());

  function fmtWarmupDate(raw) {
    if (!raw) return "";
    const s = String(raw).trim();
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) return m[3] + "." + m[2] + "." + m[1];
    return s;
  }

  function renderTgAccountCard(a) {
    let statusLabel = "Готов";
    let statusColor = "var(--ok)";
    if (a.status === "warmup") {
      statusLabel = a.warmup_until
        ? "Прогрев до " + fmtWarmupDate(a.warmup_until)
        : "Прогрев";
      statusColor = "var(--warn)";
    } else if (a.status === "unavailable" || a.status === "blocked") {
      statusLabel = a.status === "blocked" ? "Заблокирован" : "Нет сессии";
      statusColor = "var(--ink-3)";
    }

    let connectLabel = "";
    let connectColor = "";
    if (a.session_ok === true) {
      connectLabel = "Коннект ок";
      connectColor = "var(--ok)";
    } else if (a.session_ok === false) {
      connectLabel = "Нет коннекта";
      connectColor = "#c0492f";
    }

    const nameBits = [];
    if (a.label && a.label !== a.phone && a.label !== a.phone_masked) {
      nameBits.push(a.label);
    }
    if (a.tg_username) nameBits.push("@" + a.tg_username);

    const aliveHint = a.last_ok_at
      ? "ок " + fmtRelTime(a.last_ok_at)
      : a.last_checked_at
        ? "проверка " + fmtRelTime(a.last_checked_at)
        : "";

    const sent = a.sent_today != null ? a.sent_today : 0;
    const limit = a.daily_limit != null ? a.daily_limit : 200;
    const id = a.id != null ? String(a.id) : "";

    return `<div class="acct" data-acct-id="${esc(id)}">
      <div class="acct-id">
        <div class="ico tg" aria-hidden="true">TG</div>
        <div class="m">
          <div class="n">${esc(a.phone_masked || a.label || "Telegram")}</div>
          <div class="p">${esc(nameBits.join(" · ") || "Личный аккаунт")}</div>
        </div>
      </div>
      <div class="acct-info">
        <div class="acct-quota"><strong>${esc(String(sent))}</strong> из ${esc(String(limit))} сегодня</div>
        <div class="acct-tags">
          ${connectLabel ? `<span class="tagi" style="color:${connectColor}"><span class="d" style="background:${connectColor}"></span>${esc(connectLabel)}</span>` : ""}
          <span class="tagi" style="color:${statusColor}"><span class="d" style="background:${statusColor}"></span>${esc(statusLabel)}</span>
          ${aliveHint ? `<span class="acct-alive">${esc(aliveHint)}</span>` : ""}
        </div>
      </div>
      <div class="acct-actions">
        <button type="button" class="btn btn-sm" data-tg-check="${esc(id)}" ${!id ? "disabled" : ""}>Проверить</button>
        <button type="button" class="btn btn-sm danger" data-tg-del="${esc(id)}" ${!id ? "disabled" : ""}>Отключить</button>
      </div>
    </div>`;
  }

  function renderTgSessionBanner(tgItems, configured) {
    const banner = $("#tgSessionBanner");
    if (!banner) return;
    if (!configured || !tgItems.length) {
      banner.classList.add("hidden");
      banner.innerHTML = "";
      return;
    }
    const dead = tgItems.filter(
      (a) =>
        a.status === "unavailable" ||
        a.session_ok === false ||
        (a.last_error && a.session_ok !== true && a.status === "unavailable")
    );
    if (!dead.length) {
      banner.classList.add("hidden");
      banner.innerHTML = "";
      return;
    }
    const names = dead
      .map((a) => a.phone_masked || a.label || "номер")
      .slice(0, 3)
      .join(", ");
    banner.classList.remove("hidden");
    banner.innerHTML =
      "<strong>Нужно переподключить Telegram</strong> — сессия прервалась (" +
      esc(names) +
      (dead.length > 3 ? "…" : "") +
      "). Нажмите «Подключить» с тем же номером или «Проверить», если это сбой сети." +
      '<div class="ban-actions">' +
      '<button type="button" class="btn btn-sm primary" id="tgBannerReconnect">Подключить снова</button>' +
      '<button type="button" class="btn btn-sm" id="tgBannerCheck">Проверить сейчас</button>' +
      "</div>";
    $("#tgBannerReconnect")?.addEventListener("click", () => {
      openConnectForm(true);
    });
    $("#tgBannerCheck")?.addEventListener("click", () => {
      runTgKeepalive();
    });
  }

  async function runTgKeepalive() {
    const btn = $("#btnCheckTgAll");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Продление…";
    }
    try {
      const res = await AdminAPI.tgKeepalive();
      if (res.skipped) {
        alert("Сначала сохраните API-ключи Telegram");
      } else if (res.bad_count > 0) {
        alert(
          "Проверено " +
            res.checked +
            ": " +
            res.ok_count +
            " ок, " +
            res.bad_count +
            " нужно переподключить"
        );
      }
      await loadAccounts({ check: true });
    } catch (err) {
      alert(err.data?.error || err.message);
      loadAccounts({ check: true });
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Проверить все";
      }
    }
  }

  function updateTgSetupStatus(configured, total, ready, liveOk) {
    const el = $("#tgSetupStatus");
    if (!el) return;
    const chips = [];
    chips.push(
      configured
        ? `<span class="status-pill ok"><span class="d"></span>API-ключи</span>`
        : `<span class="status-pill warn"><span class="d"></span>Нужны API-ключи</span>`
    );
    if (total) {
      chips.push(
        `<span class="status-pill ${ready ? "ok" : "warn"}"><span class="d"></span>${ready} из ${total} готовы</span>`
      );
      if (liveOk != null) {
        chips.push(
          liveOk > 0
            ? `<span class="status-pill ok"><span class="d"></span>Коннект · ${liveOk}</span>`
            : `<span class="status-pill err"><span class="d"></span>Нет живого коннекта</span>`
        );
      }
    } else {
      chips.push(`<span class="status-pill warn"><span class="d"></span>Нет номеров</span>`);
    }
    el.innerHTML = chips.join("");
  }

  function updateSettingsGlance(tgConfigured, tgCount) {
    const el = $("#settingsGlance");
    if (!el) return;
    const maxOk = el.dataset.maxOk === "1";
    el.innerHTML = [
      `<span class="glance-chip ${tgConfigured && tgCount ? "ok" : "warn"}"><span class="d"></span>TG · ${tgCount || 0}</span>`,
      `<span class="glance-chip ${maxOk ? "ok" : "warn"}"><span class="d"></span>MAX · ${maxOk ? "ок" : "нет"}</span>`,
    ].join("");
  }

  function updateSettingsGlanceMax(ok) {
    const el = $("#settingsGlance");
    if (!el) return;
    el.dataset.maxOk = ok ? "1" : "0";
    const tgConfigured = !!(accountsCache && accountsCache.telethon_configured);
    const tgCount = ((accountsCache && accountsCache.items) || []).filter((a) => a.kind !== "max_bot").length;
    updateSettingsGlance(tgConfigured, tgCount);
  }

  const staffState = {
    items: [],
    envAdmin: null,
    selectedId: null,
    search: "",
    pendingPassword: null,
    catalog: null,
  };

  function staffPermCatalog() {
    return (staffState.catalog && staffState.catalog.length
      ? staffState.catalog
      : permCatalog) || PERM_CATALOG_FALLBACK;
  }

  function staffPermsOf(u) {
    return normalizePerms(u && u.permissions);
  }

  function renderPermToggles(containerId, perms, { prefix = "staffPerm" } = {}) {
    const box = typeof containerId === "string" ? $(containerId) : containerId;
    if (!box) return;
    const catalog = staffPermCatalog();
    const map = normalizePerms(perms);
    box.innerHTML = catalog
      .map((p) => {
        const on = !!map[p.id];
        return `<label class="staff-perm ${on ? "on" : ""}" data-perm="${esc(p.id)}">
          <input type="checkbox" id="${esc(prefix)}_${esc(p.id)}" ${on ? "checked" : ""}>
          <span class="tick"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6 9 17l-5-5"/></svg></span>
          <span>${esc(p.label)}</span>
        </label>`;
      })
      .join("");
    box.querySelectorAll(".staff-perm").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.preventDefault();
        const inp = el.querySelector("input");
        if (!inp) return;
        inp.checked = !inp.checked;
        el.classList.toggle("on", inp.checked);
      });
    });
  }

  function readPermToggles(root) {
    const scope = typeof root === "string" ? $(root) : root || document;
    const out = {};
    staffPermCatalog().forEach((p) => {
      out[p.id] = false;
    });
    if (!scope) return out;
    scope.querySelectorAll(".staff-perm[data-perm]").forEach((el) => {
      const id = el.dataset.perm;
      const inp = el.querySelector("input");
      if (id) out[id] = !!(inp && inp.checked);
    });
    return out;
  }

  function setPermToggles(root, enabled) {
    const scope = typeof root === "string" ? $(root) : root;
    if (!scope) return;
    scope.querySelectorAll(".staff-perm").forEach((el) => {
      const inp = el.querySelector("input");
      if (!inp) return;
      inp.checked = !!enabled;
      el.classList.toggle("on", !!enabled);
    });
  }

  function fmtStaffDate(iso) {
    if (!iso) return "ещё не входил";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
      return d.toLocaleString("ru-RU", {
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (_) {
      return String(iso).slice(0, 16);
    }
  }

  function staffDisplayName(u) {
    if (!u) return "—";
    return u.name || formatPhoneDisplay(u.phone) || "Без имени";
  }

  function filteredStaffItems() {
    const q = (staffState.search || "").trim().toLowerCase();
    const rows = staffState.items || [];
    if (!q) return rows;
    const qDigits = q.replace(/\D/g, "");
    return rows.filter((u) => {
      const name = String(u.name || "").toLowerCase();
      const phone = formatPhoneDisplay(u.phone) || u.phone || "";
      const digits = String(u.phone || "").replace(/\D/g, "");
      return name.includes(q) || phone.toLowerCase().includes(q) || (qDigits && digits.includes(qDigits));
    });
  }

  function selectedStaff() {
    if (staffState.selectedId == null) return null;
    return (staffState.items || []).find((x) => Number(x.id) === Number(staffState.selectedId)) || null;
  }

  function renderStaffList() {
    const box = $("#staffList");
    const toolbar = $("#staffToolbar");
    const countEl = $("#staffCount");
    if (!box) return;
    const total = (staffState.items || []).length;
    if (toolbar) toolbar.hidden = total < 4;
    const hint = $("#staffHint");
    if (hint) hint.hidden = total > 0;
    if (countEl) countEl.textContent = total ? total + " " + (total === 1 ? "человек" : total < 5 ? "человека" : "человек") : "";

    const items = filteredStaffItems();
    if (!total) {
      box.innerHTML = `<div class="staff-empty">
        <div class="t">Пока никого нет</div>
        <p>Добавьте первого сотрудника — он сможет входить по телефону.</p>
        <button type="button" class="btn primary" id="staffEmptyAdd">Добавить сотрудника</button>
      </div>`;
      $("#staffEmptyAdd")?.addEventListener("click", () => openStaffCreateModal(true));
      renderStaffCard();
      renderStaffSysNote();
      return;
    }
    if (!items.length) {
      box.innerHTML = `<div class="staff-empty"><div class="t">Никого не найдено</div><p>Попробуйте другой запрос</p></div>`;
      renderStaffCard();
      return;
    }
    box.innerHTML = items
      .map((u) => {
        const active = !!u.is_active;
        const phone = formatPhoneDisplay(u.phone) || u.phone || "—";
        const on = Number(u.id) === Number(staffState.selectedId) ? "on" : "";
        const perms = staffPermsOf(u);
        const count = Object.values(perms).filter(Boolean).length;
        return `<button type="button" class="staff-person ${on} ${active ? "" : "off"}" data-staff-id="${esc(u.id)}">
          <div class="av">${esc(initials(staffDisplayName(u)))}</div>
          <div class="meta">
            <div class="nm">${esc(staffDisplayName(u))}</div>
            <div class="ph">${esc(phone)} · ${count} раздел${count === 1 ? "" : count > 4 ? "ов" : "а"}</div>
          </div>
          <span class="st ${active ? "" : "off"}">${active ? "Может входить" : "Отключён"}</span>
          <span class="chev" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6"/></svg></span>
        </button>`;
      })
      .join("");
    box.querySelectorAll("[data-staff-id]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = Number(btn.dataset.staffId);
        staffState.selectedId = Number(staffState.selectedId) === id ? null : id;
        staffState.pendingPassword = null;
        renderStaffList();
        renderStaffCard();
      });
    });
    renderStaffCard();
    renderStaffSysNote();
  }

  function renderStaffSysNote() {
    const el = $("#staffSysNote");
    if (!el) return;
    if (!staffState.envAdmin) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    el.innerHTML = `Ещё есть <b>основной вход</b> на сервере (логин <b>${esc(staffState.envAdmin.username || "admin")}</b>) — полный доступ ко всему.`;
  }

  function renderStaffCard() {
    const card = $("#staffCard");
    if (!card) return;
    const u = selectedStaff();
    if (!u) {
      card.hidden = true;
      card.innerHTML = "";
      return;
    }
    card.hidden = false;
    const phone = formatPhoneDisplay(u.phone) || u.phone || "—";
    const active = !!u.is_active;
    const pending = staffState.pendingPassword;
    card.innerHTML = `
      <div class="staff-detail-head">
        <div class="av">${esc(initials(staffDisplayName(u)))}</div>
        <div class="info">
          <div class="nm">${esc(staffDisplayName(u))}</div>
          <div class="ph">${esc(phone)}</div>
        </div>
        <button type="button" class="close" id="staffCloseCard" aria-label="Закрыть">✕</button>
      </div>
      <div class="staff-detail-body">
        <label class="field">
          <span>Имя</span>
          <input type="text" id="staffEditName" value="${esc(u.name || "")}" autocomplete="name">
        </label>
        <div class="staff-facts">
          <span>Телефон: <b>${esc(phone)}</b></span>
          <span>Входил: <b>${esc(fmtStaffDate(u.last_login_at))}</b></span>
        </div>
        <div>
          <div class="staff-perms-head">
            <div class="ttl">Доступ к разделам</div>
            <div class="links">
              <button type="button" id="staffPermAll">Все</button>
              <button type="button" id="staffPermNone">Сбросить</button>
            </div>
          </div>
          <div class="staff-perms" id="staffEditPerms"></div>
        </div>
        <div class="staff-pass-box">
          <div class="ttl">Пароль для входа</div>
          <div class="sub">Старый пароль скрыт. Можно выдать новый и сразу скопировать.</div>
          <button type="button" class="btn primary" id="staffCardResetPass">Выдать новый пароль</button>
          <div class="staff-pass-reveal" id="staffPassReveal" ${pending ? "" : "hidden"}>
            <div>
              <div class="lbl">Новый пароль — передайте сотруднику</div>
              <code id="staffPassRevealVal">${esc(pending || "")}</code>
            </div>
            <button type="button" class="btn" id="staffPassCopy">Копировать</button>
          </div>
        </div>
        <div class="staff-actions">
          <button type="button" class="btn" id="staffToggleActive">${active ? "Отключить вход" : "Включить вход"}</button>
          <button type="button" class="btn danger" id="staffDeleteBtn">Удалить</button>
          <span class="spacer"></span>
          <button type="button" class="btn primary" id="staffSaveBtn">Сохранить</button>
        </div>
        <div class="form-status" id="staffCardStatus"></div>
      </div>`;

    renderPermToggles("#staffEditPerms", staffPermsOf(u), { prefix: "staffEdit" });
    $("#staffPermAll")?.addEventListener("click", () => setPermToggles("#staffEditPerms", true));
    $("#staffPermNone")?.addEventListener("click", () => setPermToggles("#staffEditPerms", false));
    $("#staffCloseCard")?.addEventListener("click", () => {
      staffState.selectedId = null;
      staffState.pendingPassword = null;
      renderStaffList();
    });
    $("#staffCardResetPass")?.addEventListener("click", () => resetStaffPassword(u.id));
    $("#staffPassCopy")?.addEventListener("click", () => {
      const val = $("#staffPassRevealVal")?.textContent || "";
      if (val) copyText(val);
    });
    $("#staffSaveBtn")?.addEventListener("click", () => saveStaffUser(u.id));
    $("#staffToggleActive")?.addEventListener("click", () => toggleStaffActive(u.id, !active));
    $("#staffDeleteBtn")?.addEventListener("click", () => deleteStaffUser(u.id));
    if (pending) setStaffCardStatus("Скопируйте пароль и передайте сотруднику");
  }

  function setStaffCardStatus(text, isError) {
    const el = $("#staffCardStatus");
    if (!el) return;
    el.textContent = text || "";
    el.className = "form-status" + (text ? (isError ? " err" : " ok") : "");
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      setStaffCardStatus("Пароль скопирован", false);
    } catch (_) {
      setStaffCardStatus("Не удалось скопировать — выделите вручную", true);
    }
  }

  async function loadUsersPane() {
    const list = $("#staffList");
    if (!list) return;
    list.innerHTML = '<div class="loading">Загрузка…</div>';
    try {
      const users = await AdminAPI.users();
      staffState.items = users.items || [];
      staffState.envAdmin = users.env_admin || null;
      if (
        staffState.selectedId != null &&
        !staffState.items.some((u) => Number(u.id) === Number(staffState.selectedId))
      ) {
        staffState.selectedId = null;
        staffState.pendingPassword = null;
      }
      renderStaffList();
    } catch (err) {
      if (err.status === 401) return showLogin();
      list.innerHTML = '<div class="empty-state">Не удалось загрузить</div>';
    }
  }

  function openStaffCreateModal(show) {
    const modal = $("#staffCreateModal");
    if (!modal) return;
    modal.hidden = !show;
    if (show) {
      $("#staffCreateForm")?.reset();
      if ($("#staffRole")) $("#staffRole").value = "employee";
      $("#staffPassHint").hidden = true;
      $("#staffName")?.focus();
      generateStaffCreatePassword();
    }
  }

  async function generateStaffCreatePassword() {
    try {
      const res = await AdminAPI.generatePassword();
      const inp = $("#staffPassword");
      if (inp) {
        inp.value = res.password || "";
        $("#staffPassHint").hidden = false;
      }
    } catch (_) {
      /* ignore */
    }
  }

  async function saveStaffUser(id) {
    setStaffCardStatus("Сохраняю…");
    try {
      const res = await AdminAPI.updateUser(id, {
        name: $("#staffEditName")?.value?.trim() || "",
        permissions: readPermToggles("#staffEditPerms"),
      });
      const idx = staffState.items.findIndex((x) => Number(x.id) === Number(id));
      if (idx >= 0) {
        staffState.items[idx] = {
          ...res.user,
          permissions: normalizePerms(res.user.permissions),
        };
      }
      staffState.selectedId = res.user.id;
      renderStaffList();
      setStaffCardStatus("Сохранено");
    } catch (err) {
      if (err.status === 401) return showLogin();
      setStaffCardStatus(err.data?.detail || "Не удалось сохранить", true);
    }
  }

  async function toggleStaffActive(id, next) {
    setStaffCardStatus(next ? "Включаю…" : "Отключаю…");
    try {
      const res = await AdminAPI.updateUser(id, { is_active: next });
      const idx = staffState.items.findIndex((x) => Number(x.id) === Number(id));
      if (idx >= 0) staffState.items[idx] = res.user;
      staffState.selectedId = res.user.id;
      renderStaffList();
      setStaffCardStatus(next ? "Вход включён" : "Вход отключён");
    } catch (err) {
      if (err.status === 401) return showLogin();
      setStaffCardStatus(err.data?.detail || "Не удалось изменить", true);
    }
  }

  async function resetStaffPassword(id) {
    setStaffCardStatus("Создаю пароль…");
    try {
      const res = await AdminAPI.resetUserPassword(id, {});
      staffState.pendingPassword = res.password || "";
      const idx = staffState.items.findIndex((x) => Number(x.id) === Number(id));
      if (idx >= 0 && res.user) staffState.items[idx] = res.user;
      renderStaffCard();
      setStaffCardStatus("Новый пароль готов — скопируйте");
    } catch (err) {
      if (err.status === 401) return showLogin();
      setStaffCardStatus(err.data?.detail || "Не удалось выдать пароль", true);
    }
  }

  async function deleteStaffUser(id) {
    const u = selectedStaff();
    const label = staffDisplayName(u) || "сотрудника";
    if (!confirm(`Удалить ${label}? Войти с этим телефоном больше нельзя.`)) return;
    setStaffCardStatus("Удаляю…");
    try {
      await AdminAPI.deleteUser(id);
      staffState.items = staffState.items.filter((x) => Number(x.id) !== Number(id));
      staffState.selectedId = null;
      staffState.pendingPassword = null;
      renderStaffList();
    } catch (err) {
      if (err.status === 401) return showLogin();
      setStaffCardStatus(err.data?.detail || "Не удалось удалить", true);
    }
  }

  $("#staffAddBtn")?.addEventListener("click", () => openStaffCreateModal(true));
  $$("[data-staff-close]").forEach((el) => {
    el.addEventListener("click", () => openStaffCreateModal(false));
  });
  $("#staffGenPassBtn")?.addEventListener("click", () => generateStaffCreatePassword());
  $("#staffSearch")?.addEventListener("input", (e) => {
    staffState.search = e.target.value || "";
    renderStaffList();
  });
  $("#staffPhone")?.addEventListener("blur", () => {
    const inp = $("#staffPhone");
    if (!inp) return;
    const formatted = formatPhoneDisplay(inp.value);
    if (formatted) inp.value = formatted;
  });
  $("#staffCreateForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#staffName")?.value?.trim() || "";
    const phone = $("#staffPhone")?.value?.trim() || "";
    const password = $("#staffPassword")?.value?.trim() || "";
    if (!phoneNationalDigits(phone)) {
      alert("Укажите номер телефона");
      $("#staffPhone")?.focus();
      return;
    }
    if (!password || password.length < 6) {
      alert("Сгенерируйте или введите пароль");
      $("#staffPassword")?.focus();
      return;
    }
    const btn = $("#staffCreateSubmit");
    if (btn) btn.disabled = true;
    try {
      const res = await AdminAPI.createUser({
        name,
        phone,
        role: "employee",
        password,
        permissions: readPermToggles("#staffCreatePerms"),
        return_password: true,
      });
      staffState.items.unshift({
        ...res.user,
        permissions: normalizePerms(res.user.permissions),
      });
      staffState.selectedId = res.user.id;
      staffState.pendingPassword = res.password || password;
      openStaffCreateModal(false);
      renderStaffList();
      setTimeout(() => {
        const card = $("#staffCard");
        if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 50);
    } catch (err) {
      if (err.status === 401) return showLogin();
      alert(err.data?.detail || "Не удалось создать сотрудника");
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  function renderLogsPane() {
    const box = $("#logsList");
    if (!box) return;
    const demo = [
      {
        kind: "sync",
        time: "—",
        title: "Журнал пока пуст",
        detail:
          "Скоро здесь появятся события синхронизации Posiflora, запуски рассылок и ошибки каналов.",
      },
    ];
    const items =
      logsFilter === "all" ? demo : demo.filter((x) => x.kind === logsFilter);
    if (!items.length) {
      box.innerHTML = `<div class="logs-empty"><div class="t">Нет записей</div>В этом фильтре пока нет событий</div>`;
      return;
    }
    box.innerHTML = items
      .map(
        (l) => `<div class="log-row">
        <div class="log-time">${esc(l.time)}</div>
        <div class="log-msg"><div class="log-title">${esc(l.title)}</div><div class="log-detail">${esc(l.detail)}</div></div>
        <span class="log-kind ${esc(l.kind)}">${esc({ sync: "Синхр.", mail: "Рассылка", error: "Ошибка", info: "Инфо" }[l.kind] || l.kind)}</span>
      </div>`
      )
      .join("");
  }

  async function loadIntegrationsPane() {
    const box = $("#integrationsList");
    if (!box) return;
    box.innerHTML = '<div class="loading">Загрузка…</div>';

    let syncWhenHtml = "Ещё не синхронизировали";
    let syncOk = false;
    let syncHasRun = false;
    let syncError = "";
    let customersCount = 0;
    let ai = {
      configured: false,
      provider: "openai",
      providers: [
        {
          id: "openai",
          label: "OpenAI",
          api_base: "https://api.openai.com/v1",
          model: "gpt-4o-mini",
          hint: "Ключ с platform.openai.com",
          needs_folder: false,
        },
        {
          id: "openrouter",
          label: "OpenRouter",
          api_base: "https://openrouter.ai/api/v1",
          model: "openai/gpt-4o-mini",
          hint: "Один ключ — доступ к разным моделям",
          needs_folder: false,
        },
        {
          id: "deepseek",
          label: "DeepSeek",
          api_base: "https://api.deepseek.com/v1",
          model: "deepseek-v4-pro",
          hint: "Ключ с platform.deepseek.com · deepseek-v4-pro (умная) / deepseek-v4-flash",
          needs_folder: false,
        },
        {
          id: "yandexgpt",
          label: "YandexGPT",
          api_base: "https://llm.api.cloud.yandex.net/v1",
          model: "yandexgpt-lite/latest",
          hint: "API-ключ и Folder ID из Yandex Cloud",
          needs_folder: true,
        },
        {
          id: "custom",
          label: "Свой API",
          api_base: "https://api.openai.com/v1",
          model: "gpt-4o-mini",
          hint: "Любой OpenAI-совместимый endpoint",
          needs_folder: false,
        },
      ],
      api_base: "https://api.openai.com/v1",
      model: "gpt-4o-mini",
      folder_id: "",
      api_key_masked: null,
      from_env: false,
    };

    try {
      const [stats, aiSettings] = await Promise.all([
        AdminAPI.stats(),
        AdminAPI.aiSettings().catch(() => null),
      ]);
      customersCount = Number(stats.customers) || 0;
      const sync = stats.sync || {};
      if (sync.at) {
        syncHasRun = true;
        syncOk = !sync.error;
        syncError = sync.error ? String(sync.error) : "";
        syncWhenHtml = syncOk
          ? "Последняя · " + fmtRelTime(sync.at)
          : "Ошибка синхронизации";
      } else if (sync.error) {
        syncHasRun = true;
        syncError = String(sync.error);
        syncWhenHtml = "Ошибка синхронизации";
      }
      if (aiSettings) {
        ai = Object.assign(ai, aiSettings);
        if (Array.isArray(aiSettings.providers) && aiSettings.providers.length) {
          ai.providers = aiSettings.providers;
        }
      }
    } catch (_) {}

    const providerLabel =
      (ai.providers || []).find((p) => p.id === ai.provider)?.label ||
      ai.provider ||
      "ИИ";

    const hero = $("#integHeroStatus");
    if (hero) {
      hero.innerHTML = [
        `<span class="status-pill ${syncOk ? "ok" : syncHasRun && syncError ? "err" : "warn"}"><span class="d"></span>Posiflora · ${
          syncOk ? "ок" : syncHasRun && syncError ? "ошибка" : "нет синхр."
        }</span>`,
        `<span class="status-pill ${ai.configured ? "ok" : "warn"}"><span class="d"></span>ИИ · ${
          ai.configured ? esc(providerLabel) : "выкл"
        }</span>`,
      ].join("");
    }

    const pfStatus = syncOk
      ? `<span class="status-pill ok"><span class="d"></span>Синхронизирована</span>`
      : syncHasRun && syncError
        ? `<span class="status-pill err"><span class="d"></span>Ошибка синхронизации</span>`
        : `<span class="status-pill warn"><span class="d"></span>Ждёт первой синхронизации</span>`;

    const aiStatus = ai.configured
      ? `<span class="status-pill ok"><span class="d"></span>${esc(providerLabel)}${
          ai.from_env ? " · .env" : ""
        }</span>`
      : `<span class="status-pill warn"><span class="d"></span>Не подключён</span>`;

    const providerChips = (ai.providers || [])
      .map((p) => {
        const saved = !!p.configured || !!p.api_key_set;
        return `<button type="button" class="ai-prov ${
          p.id === ai.provider ? "on" : ""
        }${saved ? " has-key" : ""}" data-provider="${esc(p.id)}" data-base="${esc(
          p.api_base || ""
        )}" data-model="${esc(p.model || "")}" data-hint="${esc(
          p.hint || ""
        )}" data-folder="${p.needs_folder ? "1" : "0"}" data-key-set="${
          saved ? "1" : "0"
        }" data-key-masked="${esc(p.api_key_masked || "")}" data-folder-id="${esc(
          p.folder_id || ""
        )}" title="${saved ? "Ключ сохранён" : "Ключ не задан"}">${esc(p.label)}${
          saved ? '<span class="ai-prov-dot" aria-hidden="true"></span>' : ""
        }</button>`;
      })
      .join("");

    const curProv =
      (ai.providers || []).find((p) => p.id === ai.provider) || ai.providers[0] || {};
    const needsFolder = !!curProv.needs_folder || ai.provider === "yandexgpt";
    const showBase = ai.provider === "custom";

    box.innerHTML = `
      <div class="set-block svc-block">
        <div class="set-block-head">
          <div class="svc-title-row">
            <div class="svc-ico pf" aria-hidden="true">PF</div>
            <div>
              <h4>Posiflora</h4>
              <p>Клиенты, заказы и даты из CRM — основа сегментов и поводов написать.</p>
            </div>
          </div>
          ${pfStatus}
        </div>
        <div class="svc-metrics">
          <div class="svc-metric">
            <div class="n">${fmtNum(customersCount)}</div>
            <div class="l">клиентов в базе</div>
          </div>
          <div class="svc-metric svc-metric-wide">
            <div class="n-sm">${syncWhenHtml}</div>
            <div class="l">${
              syncError
                ? esc(String(syncError).slice(0, 140))
                : customersCount
                  ? "Данные из Posiflora готовы к сегментам и рассылкам"
                  : "Нажмите «Синхронизировать», чтобы подтянуть клиентов"
            }</div>
          </div>
        </div>
        <div class="form-actions svc-actions">
          <button class="btn primary" type="button" id="integSyncBtn">Синхронизировать</button>
          <button class="btn" type="button" onclick="go('clients')">Открыть клиентов</button>
        </div>
      </div>

      <div class="set-block svc-block">
        <div class="set-block-head">
          <div class="svc-title-row">
            <div class="svc-ico ai" aria-hidden="true">AI</div>
            <div>
              <h4>ИИ-помощник</h4>
              <p>Черновики рассылок и ответы в разделе «ИИ чат».</p>
            </div>
          </div>
          ${aiStatus}
        </div>

        <div class="svc-ai-form" id="aiSettingsForm">
          <div class="svc-field">
            <label>Провайдер</label>
            <div class="ai-prov-row" id="aiProviderRow">${providerChips}</div>
            <p class="ai-prov-hint" id="aiProvHint">${esc(curProv.hint || "")}</p>
          </div>

          <div class="svc-field">
            <label for="aiApiKey">API-ключ <span class="ai-key-status" id="aiKeyStatus">${
              curProv.api_key_set || curProv.configured
                ? "· сохранён для " + esc(curProv.label || "")
                : "· для этого оператора ещё не задан"
            }</span></label>
            <input id="aiApiKey" type="password" autocomplete="off" placeholder="${
              curProv.api_key_set || curProv.configured
                ? "Оставьте пустым, чтобы не менять · " + esc(curProv.api_key_masked || ai.api_key_masked || "••••")
                : "Вставьте ключ выбранного оператора"
            }">
            <p class="form-foot" id="aiKeyFoot">Ключи OpenRouter, DeepSeek и остальных хранятся отдельно — можно переключаться без потери.</p>
          </div>

          <div class="svc-field" id="aiFolderRow" ${needsFolder ? "" : "hidden"}>
            <label for="aiFolderId">Folder ID (Yandex Cloud)</label>
            <input id="aiFolderId" type="text" autocomplete="off" value="${esc(
              ai.folder_id || ""
            )}" placeholder="b1g…">
          </div>

          <div class="form-grid-2 svc-field">
            <div id="aiBaseWrap" ${showBase ? "" : "hidden"}>
              <label for="aiApiBase">Базовый URL</label>
              <input id="aiApiBase" type="url" autocomplete="off" value="${esc(
                ai.api_base || ""
              )}" placeholder="https://…/v1">
            </div>
            <div id="aiModelWrap" ${showBase ? "" : 'style="grid-column:1/-1"'}>
              <label for="aiModel">Модель</label>
              <input id="aiModel" type="text" autocomplete="off" value="${esc(
                ai.model || ""
              )}" placeholder="${esc(curProv.model || "model")}">
            </div>
          </div>

          <div class="form-actions svc-actions">
            <button type="button" class="btn primary" id="aiSettingsSave">Сохранить</button>
            <button type="button" class="btn" id="aiSettingsClear" ${
              ai.configured ? "" : "disabled"
            }>Отключить</button>
            <button type="button" class="btn" onclick="go('aichat')">Открыть ИИ-чат</button>
          </div>
          <p class="form-foot">DeepSeek: <code>deepseek-v4-pro</code>. OpenRouter: <code>openai/gpt-4o-mini</code>. YandexGPT: <code>yandexgpt-lite/latest</code>.</p>
        </div>
      </div>`;

    let selectedProvider = ai.provider || "openai";
    const providersById = Object.fromEntries(
      (ai.providers || []).map((p) => [p.id, p])
    );

    function applyProviderUi(btn) {
      selectedProvider = btn.dataset.provider;
      $$("#aiProviderRow .ai-prov").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      const meta = providersById[selectedProvider] || {};
      const hint = $("#aiProvHint");
      if (hint) hint.textContent = btn.dataset.hint || meta.hint || "";

      const model = $("#aiModel");
      if (model) {
        model.value = meta.model || btn.dataset.model || "";
        model.dataset.autofill = "1";
      }
      const base = $("#aiApiBase");
      if (base) base.value = meta.api_base || btn.dataset.base || "";

      const folderRow = $("#aiFolderRow");
      if (folderRow) folderRow.hidden = btn.dataset.folder !== "1";
      const folderInp = $("#aiFolderId");
      if (folderInp) folderInp.value = meta.folder_id || btn.dataset.folderId || "";

      const keyInp = $("#aiApiKey");
      if (keyInp) {
        keyInp.value = "";
        const hasKey = btn.dataset.keySet === "1" || !!meta.api_key_set || !!meta.configured;
        const masked = meta.api_key_masked || btn.dataset.keyMasked || "";
        keyInp.placeholder = hasKey
          ? "Оставьте пустым, чтобы не менять · " + (masked || "••••")
          : "Вставьте ключ выбранного оператора";
      }
      const keyStatus = $("#aiKeyStatus");
      if (keyStatus) {
        const hasKey = btn.dataset.keySet === "1" || !!meta.api_key_set || !!meta.configured;
        keyStatus.textContent = hasKey
          ? "· сохранён для " + (meta.label || selectedProvider)
          : "· для этого оператора ещё не задан";
      }

      const baseWrap = $("#aiBaseWrap");
      if (baseWrap) baseWrap.hidden = selectedProvider !== "custom";
      const modelWrap = $("#aiModelWrap");
      if (modelWrap) {
        if (selectedProvider === "custom") modelWrap.removeAttribute("style");
        else modelWrap.style.gridColumn = "1 / -1";
      }
    }

    $$("#aiProviderRow .ai-prov").forEach((btn) => {
      btn.addEventListener("click", () => applyProviderUi(btn));
    });
    $("#aiModel")?.addEventListener("input", () => {
      if ($("#aiModel")) $("#aiModel").dataset.autofill = "0";
    });

    $("#integSyncBtn")?.addEventListener("click", async () => {
      const btn = $("#integSyncBtn");
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Синхронизация…";
      }
      try {
        const res = await AdminAPI.sync();
        alert(
          res.ok
            ? `Готово: ${res.customers} клиентов, ${res.events} событий, ${res.orders || 0} заказов`
            : "Ошибка: " + (res.error || "unknown")
        );
        loadIntegrationsPane();
      } catch (err) {
        alert("Ошибка: " + (err.data?.error || err.message));
      }
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Синхронизировать";
      }
    });

    $("#aiSettingsSave")?.addEventListener("click", async () => {
      const btn = $("#aiSettingsSave");
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Сохраняю…";
      }
      try {
        const body = {
          provider: selectedProvider,
          api_key: ($("#aiApiKey")?.value || "").trim(),
          api_base: ($("#aiApiBase")?.value || "").trim(),
          model: ($("#aiModel")?.value || "").trim(),
          folder_id: ($("#aiFolderId")?.value || "").trim(),
        };
        const selMeta = providersById[selectedProvider] || {};
        const selHasKey = !!(selMeta.configured || selMeta.api_key_set);
        if (!body.api_key && !selHasKey) {
          alert("Укажите API-ключ для " + (selMeta.label || selectedProvider));
        } else if (
          selectedProvider === "yandexgpt" &&
          !body.folder_id &&
          !(selMeta.folder_id || ai.folder_id)
        ) {
          alert("Для YandexGPT укажите Folder ID");
        } else {
          await AdminAPI.aiSaveSettings(body);
          alert(
            selHasKey && !body.api_key
              ? "Активирован " + (selMeta.label || selectedProvider) + " (ключ сохранён ранее)"
              : "Настройки ИИ сохранены для " + (selMeta.label || selectedProvider)
          );
          loadIntegrationsPane();
          return;
        }
      } catch (err) {
        alert(err.data?.detail || err.data?.error || err.message || "Ошибка");
      }
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Сохранить";
      }
    });

    $("#aiSettingsClear")?.addEventListener("click", async () => {
      if (
        !confirm(
          "Удалить сохранённые ключи всех операторов (OpenAI, OpenRouter, DeepSeek…)? Останется только ключ из .env, если он задан."
        )
      )
        return;
      try {
        await AdminAPI.aiSaveSettings({ clear: true });
        loadIntegrationsPane();
      } catch (err) {
        alert(err.data?.detail || err.message || "Ошибка");
      }
    });
  }

  async function loadTgApiStatus() {
    const box = $("#tgApiStatus");
    if (!box) return;
    try {
      const s = await AdminAPI.tgSettings();
      if (s.configured) {
        const src = s.from_env ? " · .env" : "";
        box.innerHTML =
          '<span class="status-pill ok"><span class="d"></span>Заданы' +
          src +
          (s.api_id ? " · ID " + esc(String(s.api_id)) : "") +
          "</span>";
        if (s.api_id && !$("#tgApiId").value) $("#tgApiId").value = s.api_id;
      } else {
        box.innerHTML =
          '<span class="status-pill warn"><span class="d"></span>Не заданы</span>';
      }
    } catch (_) {}
  }

  $("#tgApiSave")?.addEventListener("click", async () => {
    const apiId = $("#tgApiId").value.trim();
    const apiHash = $("#tgApiHash").value.trim();
    if (!apiId || !apiHash) return alert("Укажите API ID и API Hash");
    try {
      const res = await AdminAPI.tgSaveSettings(apiId, apiHash);
      if (!res.ok) return alert(res.error || "Ошибка");
      $("#tgApiHash").value = "";
      alert("Ключи сохранены");
      loadAccounts();
    } catch (err) {
      alert(err.data?.error || err.message);
    }
  });

  function maxSettingsError(err) {
    const code = err?.data?.error || err?.message;
    const detail = err?.data?.detail;
    if (detail) return detail;
    if (
      code === "Failed to fetch" ||
      code === "NetworkError when attempting to fetch resource." ||
      code === "Load failed"
    ) {
      return "Нет связи с API (часто контейнер bot упал). На сервере: docker compose ps && docker compose logs --tail=80 bot";
    }
    if (code === "max_unreachable") {
      return "MAX API недоступен. Проверьте интернет и повторите.";
    }
    if (code === "max_ssl_error") {
      return (
        detail ||
        "Ошибка SSL к MAX. Перезапустите админку (python run_admin_local.py) — нужен Russian Trusted CA."
      );
    }
    if (code === "token_required") {
      return "Сначала сохраните токен бота (шаг 1). Токен выдаёт @MasterBot в MAX.";
    }
    if (code === "invalid_token") {
      return "Неверный токен — проверьте у @MasterBot";
    }
    if (code === "invalid_webhook_url") {
      return "Некорректный URL webhook (нужен https:// без порта)";
    }
    if (code === "invalid_webhook_secret") {
      return "Секрет: 5–256 символов (A–Z, a–z, 0–9, _, -)";
    }
    return code || "Ошибка сохранения";
  }

  $("#maxTokenSave")?.addEventListener("click", async () => {
    const token = $("#maxBotToken").value.trim();
    if (!token) return alert("Вставьте токен от @MasterBot в поле «Токен»");
    try {
      const res = await AdminAPI.maxSaveSettings({ token });
      if (!res.ok) return alert(maxSettingsError({ data: res }));
      $("#maxBotToken").value = "";
      const who = res.bot_username
        ? "@" + res.bot_username
        : res.bot_name || "бот";
      alert(
        "MAX подключён: " +
          who +
          "\n\nСценарий анкеты активен. Напишите боту /start в MAX — или откройте Чаты → MAX."
      );
      loadAccounts();
      loadBotsPane();
    } catch (err) {
      alert(maxSettingsError(err));
    }
  });

  $("#maxTokenClear")?.addEventListener("click", async () => {
    if (!confirm("Убрать токен MAX из панели? (значение из .env останется)"))
      return;
    try {
      await AdminAPI.maxClearSettings();
      $("#maxBotToken").value = "";
      loadAccounts();
      loadBotsPane();
    } catch (err) {
      alert(maxSettingsError(err));
    }
  });

  $("#maxWebhookUrl")?.addEventListener("input", () => {
    $("#maxWebhookUrl").dataset.touched = "1";
  });
  $("#maxFloristChatId")?.addEventListener("input", () => {
    $("#maxFloristChatId").dataset.touched = "1";
  });

  $("#maxWebhookSuggest")?.addEventListener("click", () => {
    const url =
      window.__maxSuggestedWebhook ||
      $("#maxWebhookHint")?.dataset.suggest ||
      "https://admin.veresk-flowers.ru/api/max/webhook";
    const input = $("#maxWebhookUrl");
    if (!input) return;
    input.value = url;
    input.dataset.touched = "1";
    input.focus();
  });

  $("#maxWebhookSave")?.addEventListener("click", async () => {
    const url = ($("#maxWebhookUrl")?.value || "").trim();
    const secret = ($("#maxWebhookSecret")?.value || "").trim();
    if (!url) return alert("Укажите HTTPS URL webhook");
    if (!url.startsWith("https://"))
      return alert("URL должен начинаться с https://");
    try {
      const st = await AdminAPI.maxSettings();
      if (!st.configured) {
        return alert(
          "Сначала шаг 1: сохраните токен бота от @MasterBot.\nБез токена Max не примет подписку на webhook."
        );
      }
    } catch (_) {
      /* проверим на сервере */
    }
    const body = {
      webhook_url: url,
      register_webhook: true,
    };
    if (secret) body.webhook_secret = secret;
    const btn = $("#maxWebhookSave");
    if (btn) btn.disabled = true;
    try {
      const res = await AdminAPI.maxSaveSettings(body);
      if (!res.ok) return alert(maxSettingsError({ data: res }));
      if ($("#maxWebhookSecret")) $("#maxWebhookSecret").value = "";
      delete $("#maxWebhookUrl")?.dataset.touched;
      const sub = res.subscribe;
      if (sub && sub.success === false) {
        alert(
          "Сохранено, но Max не принял подписку:\n" +
            (sub.error || "неизвестная ошибка")
        );
      } else {
        alert("Webhook сохранён и подписан в Max");
      }
      loadBotsPane();
    } catch (err) {
      alert(maxSettingsError(err));
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  $("#maxWebhookClear")?.addEventListener("click", async () => {
    if (
      !confirm(
        "Отключить webhook? Вернётся long polling (если запущен max_bot). Значения из .env останутся."
      )
    )
      return;
    try {
      await AdminAPI.maxClearWebhook();
      const urlInput = $("#maxWebhookUrl");
      if (urlInput) {
        urlInput.value = "";
        delete urlInput.dataset.touched;
      }
      if ($("#maxWebhookSecret")) $("#maxWebhookSecret").value = "";
      loadBotsPane();
    } catch (err) {
      alert(err.data?.error || err.message);
    }
  });

  $("#maxFloristSave")?.addEventListener("click", async () => {
    const raw = ($("#maxFloristChatId")?.value || "").trim();
    try {
      const res = await AdminAPI.maxSaveSettings({
        florist_chat_id: raw === "" ? "" : raw,
      });
      if (!res.ok) return alert(res.detail || res.error || "Ошибка");
      delete $("#maxFloristChatId")?.dataset.touched;
      alert(
        res.florist_chat_id
          ? "Chat ID флориста сохранён: " + res.florist_chat_id
          : "Уведомления флористу в MAX выключены"
      );
      loadBotsPane();
    } catch (err) {
      alert(err.data?.detail || err.data?.error || err.message);
    }
  });

  $("#maxOpenChats")?.addEventListener("click", () => {
    localStorage.setItem("veresk_chats_channel", "max");
    try {
      tgState.channel = "max";
    } catch (_) {
      /* tgState ещё не объявлен при раннем клике — loadChats прочитает localStorage */
    }
    go("chats");
  });

  function setConnectStep(step) {
    $$("#acctForm .connect-step").forEach((el) => {
      el.classList.toggle("on", Number(el.dataset.cstep) <= step);
    });
  }

  function setMaxConnectStep(step) {
    $$("#maxAcctForm .connect-step").forEach((el) => {
      el.classList.toggle("on", Number(el.dataset.maxCstep) <= step);
    });
  }

  function resetConnectForm() {
    stopTgQrPoll();
    $("#tgConnectFields")?.classList.remove("hidden");
    $("#tgConnectDone")?.classList.add("hidden");
    $("#tgCodeStep")?.classList.add("hidden");
    $("#tg2faWrap")?.classList.add("hidden");
    $("#tgQrStep")?.classList.add("hidden");
    $("#tgQr2faWrap")?.classList.add("hidden");
    if ($("#tgPhone")) $("#tgPhone").value = "";
    if ($("#tgCode")) $("#tgCode").value = "";
    if ($("#tg2fa")) $("#tg2fa").value = "";
    if ($("#tgQr2fa")) $("#tgQr2fa").value = "";
    const status = $("#tgQrStatus");
    if (status) status.textContent = "Ожидаем сканирование…";
    state.tgQrLoginId = null;
    state.tgQrUrl = null;
    setConnectStep(1);
  }

  function openConnectForm(show) {
    const form = $("#acctForm");
    if (!form) return;
    if (!show && state.tgQrLoginId) {
      AdminAPI.tgQrCancel(state.tgQrLoginId).catch(() => {});
    }
    form.classList.toggle("hidden", !show);
    if (show) {
      resetConnectForm();
    } else {
      resetConnectForm();
    }
  }

  function showConnectDone(res) {
    setConnectStep(3);
    $("#tgConnectFields")?.classList.add("hidden");
    const done = $("#tgConnectDone");
    done?.classList.remove("hidden");
    const text = $("#tgConnectDoneText");
    if (text) {
      const bits = [];
      if (res.session_ok) bits.push("Сессия авторизована");
      else bits.push("Аккаунт сохранён" + (res.session_error ? " — " + res.session_error : ""));
      if (res.label) bits.push(res.label);
      if (res.tg_username) bits.push("@" + res.tg_username);
      else if (res.username) bits.push("@" + res.username);
      if (res.phone) bits.push(res.phone);
      text.textContent = bits.join(" · ") + ". Можно отправлять рассылки.";
    }
  }

  function resetMaxConnectForm() {
    $("#maxConnectFields")?.classList.remove("hidden");
    $("#maxConnectDone")?.classList.add("hidden");
    $("#maxCodeStep")?.classList.add("hidden");
    $("#max2faWrap")?.classList.add("hidden");
    if ($("#maxPhone")) $("#maxPhone").value = "";
    if ($("#maxCode")) $("#maxCode").value = "";
    if ($("#max2fa")) $("#max2fa").value = "";
    setMaxConnectStep(1);
  }

  function openMaxConnectForm(show) {
    const form = $("#maxAcctForm");
    if (!form) return;
    form.classList.toggle("hidden", !show);
    if (show) {
      resetMaxConnectForm();
      $("#maxPhone")?.focus();
    } else {
      resetMaxConnectForm();
    }
  }

  function showMaxConnectDone(res) {
    setMaxConnectStep(3);
    $("#maxConnectFields")?.classList.add("hidden");
    const done = $("#maxConnectDone");
    done?.classList.remove("hidden");
    const text = $("#maxConnectDoneText");
    if (text) {
      const bits = [];
      if (res.session_ok) bits.push("Сессия авторизована");
      else bits.push("Аккаунт сохранён" + (res.session_error ? " — " + res.session_error : ""));
      if (res.label) bits.push(res.label);
      if (res.max_user_id) bits.push("id " + res.max_user_id);
      if (res.phone) bits.push(res.phone);
      text.textContent = bits.join(" · ") + ". Рассылки MAX пойдут от этого номера.";
    }
  }

  function maxErrorText(errOrRes) {
    if (!errOrRes) return "Ошибка";
    const data = errOrRes.data || errOrRes;
    const status = errOrRes.status || data.status;
    if (data.error === "pymax_missing" || /pymax|maxapi/i.test(String(data.error || ""))) {
      return (
        data.detail ||
        "Библиотека maxapi-python не установлена (нужен Python ≥3.10). pip install maxapi-python≥2.3.0 и пересоберите bot."
      );
    }
    if (data.error === "cancelled" || data.error === "confirm_timeout" || data.error === "connection_closed") {
      return (
        data.detail ||
        "Соединение с MAX прервалось. Нажмите «Получить код» ещё раз, затем введите SMS-код и пароль 2FA."
      );
    }
    if (data.error === "bad_2fa" || data.error === "need_2fa") {
      return data.detail || "Введите пароль двухфакторной защиты MAX (это не код из SMS).";
    }
    if (
      data.error === "bad_response" ||
      data.error === "request_failed" ||
      errOrRes.message === "request_failed"
    ) {
      if (status === 404) {
        return (
          "API личного номера MAX не найден (404). UI уже новый, а контейнер bot — старый. " +
          "На сервере: docker compose build bot && docker compose up -d bot"
        );
      }
      if (status === 502 || status === 504) {
        return (
          "Шлюз не дождался ответа бота (HTTP " +
          status +
          "). Перезапустите bot или увеличьте timeout nginx."
        );
      }
      return (
        data.detail ||
        "Не удалось выполнить запрос" +
          (status ? " (HTTP " + status + ")" : "") +
          ". Если только что обновили код — пересоберите контейнер bot."
      );
    }
    return data.detail || data.message || data.error || errOrRes.message || "Ошибка";
  }

  function renderMaxAccountCard(a) {
    let statusLabel = "Готов";
    let statusColor = "var(--ok)";
    if (a.status === "warmup") {
      statusLabel = a.warmup_until
        ? "Прогрев до " + fmtWarmupDate(a.warmup_until)
        : "Прогрев";
      statusColor = "var(--warn)";
    } else if (a.status === "unavailable" || a.status === "blocked") {
      statusLabel = a.status === "blocked" ? "Заблокирован" : "Нет сессии";
      statusColor = "var(--ink-3)";
    }
    const sent = a.sent_today != null ? a.sent_today : 0;
    const limit = a.daily_limit != null ? a.daily_limit : 150;
    const id = a.id != null ? String(a.id) : "";
    const nameBits = [];
    if (a.label && a.label !== a.phone && a.label !== a.phone_masked) nameBits.push(a.label);
    return `<div class="acct" data-acct-id="${esc(id)}">
      <div class="acct-id">
        <div class="ico max" aria-hidden="true">MX</div>
        <div class="m">
          <div class="n">${esc(a.phone_masked || a.label || "MAX")}</div>
          <div class="p">${esc(nameBits.join(" · ") || "Личный аккаунт")}</div>
        </div>
      </div>
      <div class="acct-info">
        <div class="acct-quota"><strong>${esc(String(sent))}</strong> из ${esc(String(limit))} сегодня</div>
        <div class="acct-tags">
          <span class="tagi" style="color:${statusColor}"><span class="d" style="background:${statusColor}"></span>${esc(statusLabel)}</span>
        </div>
      </div>
      <div class="acct-actions">
        <button type="button" class="btn btn-sm" data-max-check="${esc(id)}" ${!id ? "disabled" : ""}>Проверить</button>
        <button type="button" class="btn btn-sm danger" data-max-del="${esc(id)}" ${!id ? "disabled" : ""}>Отключить</button>
      </div>
    </div>`;
  }

  function renderMaxUserbotList(items, data) {
    const box = $("#maxAccountsList");
    if (!box) return;
    const pymaxOk = data?.pymax_installed !== false;
    if (!items.length) {
      box.innerHTML = `<div class="empty-rich" style="padding:20px 12px">
        <div class="t">Нет личного MAX-номера</div>
        <p class="d">${
          pymaxOk
            ? "Нажмите «Подключить», чтобы войти по телефону и слать рассылки от имени аккаунта."
            : "На сервере нужен Python ≥3.10 и пакет maxapi-python."
        }</p>
      </div>`;
      return;
    }
    box.innerHTML = items.map((a) => renderMaxAccountCard(a)).join("");
    box.querySelectorAll("[data-max-check]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-max-check");
        if (!id) return;
        btn.disabled = true;
        btn.textContent = "…";
        try {
          const res = await AdminAPI.tgCheckAccount(id);
          if (res.ok) {
            alert("Коннект активен" + (res.label ? " · " + res.label : ""));
          } else {
            alert("Нет коннекта: " + (res.error || "сессия не авторизована"));
          }
          loadAccounts({ check: true });
        } catch (err) {
          alert(maxErrorText(err));
          btn.disabled = false;
          btn.textContent = "Проверить";
        }
      });
    });
    box.querySelectorAll("[data-max-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-max-del");
        if (!id) return;
        if (!confirm("Отключить этот MAX-аккаунт? Сессия будет удалена.")) return;
        btn.disabled = true;
        try {
          await AdminAPI.tgDeleteAccount(id);
          loadAccounts();
        } catch (err) {
          alert(maxErrorText(err));
          btn.disabled = false;
        }
      });
    });
  }

  $("#btnConnectTg")?.addEventListener("click", () => {
    const locked = $("#btnConnectTg")?.classList.contains("is-locked");
    if (locked) {
      const api = $("#tgApiForm");
      if (api && api.tagName === "DETAILS") api.open = true;
      api?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      alert("Сначала сохраните ключи Telegram API в блоке выше (шаг 1).");
      return;
    }
    openConnectForm($("#acctForm")?.classList.contains("hidden"));
  });

  $("#btnCheckTgAll")?.addEventListener("click", () => {
    runTgKeepalive();
  });

  $("#tgConnectCancel")?.addEventListener("click", () => openConnectForm(false));
  $("#tgConnectDoneClose")?.addEventListener("click", () => {
    openConnectForm(false);
    loadAccounts({ check: true });
  });

  function tgErrorText(errOrRes) {
    if (!errOrRes) return "Ошибка";
    const data = errOrRes.data || errOrRes;
    if (data.error === "telethon_missing" || /telethon/i.test(String(data.error || ""))) {
      return (
        data.detail ||
        "Библиотека Telethon не установлена на сервере. Выполните: pip install telethon==1.36.0 и перезапустите админку."
      );
    }
    return data.detail || data.error || errOrRes.message || "Ошибка";
  }

  let tgQrPollTimer = null;
  let tgQrBusy = false;

  function stopTgQrPoll() {
    if (tgQrPollTimer) {
      clearTimeout(tgQrPollTimer);
      tgQrPollTimer = null;
    }
    tgQrBusy = false;
  }

  function renderTgQr(url) {
    const box = $("#tgQrBox");
    if (!box || !url) return;
    box.innerHTML = "";
    if (typeof QRCode === "undefined") {
      box.innerHTML =
        '<p class="form-foot" style="padding:12px;text-align:center">Не удалось загрузить генератор QR. Обновите страницу (Cmd+Shift+R).</p>';
      return;
    }
    try {
      // qrcodejs: рисует в переданный DOM-элемент (img/table/canvas)
      // eslint-disable-next-line no-new
      new QRCode(box, {
        text: String(url),
        width: 220,
        height: 220,
        colorDark: "#1a1a1a",
        colorLight: "#ffffff",
        correctLevel: QRCode.CorrectLevel.M,
      });
    } catch (err) {
      console.warn("QR render failed", err);
      box.textContent = "Ошибка отрисовки QR. Нажмите «Обновить QR».";
    }
  }

  async function scheduleTgQrPoll() {
    stopTgQrPoll();
    const tick = async () => {
      const loginId = state.tgQrLoginId;
      if (!loginId || tgQrBusy) return;
      tgQrBusy = true;
      try {
        const res = await AdminAPI.tgQrPoll(loginId);
        if (res.need_2fa) {
          stopTgQrPoll();
          $("#tgQr2faWrap")?.classList.remove("hidden");
          const status = $("#tgQrStatus");
          if (status) status.textContent = "Скан принят. Введите пароль 2FA.";
          $("#tgQr2fa")?.focus();
          return;
        }
        if (res.pending) {
          if (res.url && res.url !== state.tgQrUrl) {
            state.tgQrUrl = res.url;
            renderTgQr(res.url);
          }
          const status = $("#tgQrStatus");
          if (status) status.textContent = "Ожидаем сканирование… не закрывайте это окно";
          tgQrPollTimer = setTimeout(tick, 1500);
          return;
        }
        if (!res.ok) {
          const status = $("#tgQrStatus");
          if (status) status.textContent = tgErrorText(res);
          if (res.expired) {
            tgQrPollTimer = setTimeout(tick, 3000);
            return;
          }
          alert(tgErrorText(res));
          return;
        }
        stopTgQrPoll();
        state.tgQrLoginId = null;
        state.tgQrUrl = null;
        showConnectDone(res);
        loadAccounts({ check: true });
      } catch (err) {
        const status = $("#tgQrStatus");
        if (status) status.textContent = tgErrorText(err);
        tgQrPollTimer = setTimeout(tick, 2500);
      } finally {
        tgQrBusy = false;
      }
    };
    tgQrPollTimer = setTimeout(tick, 800);
  }

  $("#tgQrStart")?.addEventListener("click", async () => {
    const btn = $("#tgQrStart");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Готовим QR…";
    }
    stopTgQrPoll();
    try {
      const res = await AdminAPI.tgQrStart();
      if (!res.ok) return alert(tgErrorText(res));
      if (res.already_authorized || res.account_id) {
        showConnectDone(res);
        loadAccounts({ check: true });
        return;
      }
      state.tgQrLoginId = res.login_id;
      state.tgQrUrl = res.url || null;
      $("#tgQrStep")?.classList.remove("hidden");
      setConnectStep(2);
      const hint = $("#tgQrHint");
      if (hint && res.detail) hint.textContent = res.detail;
      renderTgQr(res.url);
      const status = $("#tgQrStatus");
      if (status) status.textContent = "Ожидаем сканирование… не закрывайте это окно";
      scheduleTgQrPoll();
    } catch (err) {
      alert(tgErrorText(err));
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Войти по QR";
      }
    }
  });

  $("#tgQrRefresh")?.addEventListener("click", async () => {
    const loginId = state.tgQrLoginId;
    if (!loginId) return alert("Сначала нажмите «Войти по QR»");
    try {
      const res = await AdminAPI.tgQrRefresh(loginId);
      if (!res.ok) return alert(tgErrorText(res));
      state.tgQrUrl = res.url || null;
      renderTgQr(res.url);
      const status = $("#tgQrStatus");
      if (status) status.textContent = "QR обновлён. Отсканируйте снова.";
      scheduleTgQrPoll();
    } catch (err) {
      alert(tgErrorText(err));
    }
  });

  $("#tgQrConfirm2fa")?.addEventListener("click", async () => {
    const loginId = state.tgQrLoginId;
    const password = $("#tgQr2fa")?.value?.trim();
    if (!loginId) return alert("Сначала отсканируйте QR");
    if (!password) return alert("Введите пароль 2FA");
    const btn = $("#tgQrConfirm2fa");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Проверка…";
    }
    try {
      const res = await AdminAPI.tgQr2fa(loginId, password);
      if (res.need_2fa) {
        alert(tgErrorText(res));
        return;
      }
      if (!res.ok) return alert(tgErrorText(res));
      stopTgQrPoll();
      state.tgQrLoginId = null;
      showConnectDone(res);
      loadAccounts({ check: true });
    } catch (err) {
      alert(tgErrorText(err));
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Подтвердить 2FA";
      }
    }
  });

  $("#tgSendCode")?.addEventListener("click", async () => {
    const phone = $("#tgPhone").value.trim();
    if (!phone) return alert("Укажите телефон");
    const btn = $("#tgSendCode");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Отправка…";
    }
    try {
      const res = await AdminAPI.tgStart(phone);
      if (!res.ok) return alert(tgErrorText(res));
      state.tgPhone = res.phone || phone;
      if (res.already_authorized) {
        showConnectDone(res);
        return;
      }
      if ($("#tgCode")) $("#tgCode").value = "";
      $("#tgCodeStep").classList.remove("hidden");
      setConnectStep(2);
      const hint =
        res.code_hint ||
        res.detail ||
        "Код в приложении Telegram → чат «Telegram». SMS для входа через админку не приходят.";
      const hintEl = $("#tgCodeHint");
      if (hintEl) hintEl.textContent = hint;
      $("#tgCode")?.focus();
      alert(hint);
    } catch (err) {
      alert(tgErrorText(err));
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Получить код";
      }
    }
  });

  $("#tgResendCode")?.addEventListener("click", async () => {
    const phone = state.tgPhone || $("#tgPhone")?.value?.trim();
    if (!phone) return alert("Сначала запросите код");
    const btn = $("#tgResendCode");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Отправка…";
    }
    try {
      const res = await AdminAPI.tgResend(phone);
      if (!res.ok) return alert(tgErrorText(res));
      if ($("#tgCode")) $("#tgCode").value = "";
      const hint =
        res.code_hint ||
        res.detail ||
        "Код запрошен ещё раз. Смотрите чат «Telegram» в приложении — не SMS.";
      const hintEl = $("#tgCodeHint");
      if (hintEl) hintEl.textContent = hint;
      $("#tgCode")?.focus();
      alert(hint);
    } catch (err) {
      alert(tgErrorText(err));
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Запросить код ещё раз";
      }
    }
  });

  $("#tgConfirm")?.addEventListener("click", async () => {
    const code = $("#tgCode").value.trim();
    const password = $("#tg2fa").value.trim() || undefined;
    const btn = $("#tgConfirm");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Подключение…";
    }
    try {
      const res = await AdminAPI.tgConfirm(state.tgPhone, code, password);
      if (res.need_2fa) {
        $("#tg2faWrap").classList.remove("hidden");
        alert("Введите пароль двухфакторной аутентификации");
        return;
      }
      if (!res.ok) {
        alert(tgErrorText(res));
        if (res.need_new_code) {
          if ($("#tgCode")) $("#tgCode").value = "";
          $("#tgCode")?.focus();
        }
        return;
      }
      showConnectDone(res);
    } catch (err) {
      alert(tgErrorText(err));
      if (err.data?.need_new_code) {
        if ($("#tgCode")) $("#tgCode").value = "";
        $("#tgCode")?.focus();
      }
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Подтвердить";
      }
    }
  });

  // ── MAX personal account connect ─────────────────────────────────────────

  $("#btnConnectMax")?.addEventListener("click", () => {
    openMaxConnectForm($("#maxAcctForm")?.classList.contains("hidden"));
  });
  $("#maxConnectCancel")?.addEventListener("click", () => openMaxConnectForm(false));
  $("#maxConnectDoneClose")?.addEventListener("click", () => {
    openMaxConnectForm(false);
    loadAccounts({ check: true });
    if (settingsTab === "bots") loadBotsPane();
  });

  $("#maxSendCode")?.addEventListener("click", async () => {
    const phone = $("#maxPhone").value.trim();
    if (!phone) return alert("Укажите телефон");
    const btn = $("#maxSendCode");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Отправка…";
    }
    try {
      // reset:true — снести битую сессию после прошлой неудачи на этом номере
      const res = await AdminAPI.maxUserbotStart(phone, { reset: true });
      if (!res.ok) return alert(maxErrorText(res));
      state.maxPhone = res.phone || phone;
      if (res.already_authorized) {
        showMaxConnectDone(res);
        loadAccounts();
        return;
      }
      if ($("#maxCode")) $("#maxCode").value = "";
      $("#maxCodeStep").classList.remove("hidden");
      setMaxConnectStep(2);
      $("#maxCode")?.focus();
      alert("Код отправлен. Введите код из SMS или приложения MAX.");
    } catch (err) {
      alert(maxErrorText(err));
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Получить код";
      }
    }
  });

  $("#maxConfirm")?.addEventListener("click", async () => {
    const code = $("#maxCode").value.trim();
    const password = $("#max2fa").value.trim() || undefined;
    const btn = $("#maxConfirm");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Подключение…";
    }
    try {
      if (!state.maxPhone) {
        alert("Сначала нажмите «Получить код»");
        return;
      }
      // На шаге 2FA достаточно пароля — код уже принят на сервере
      const res = await AdminAPI.maxUserbotConfirm(state.maxPhone, code, password);
      if (res.need_2fa) {
        $("#max2faWrap").classList.remove("hidden");
        setMaxConnectStep(2);
        $("#max2fa")?.focus();
        alert(maxErrorText(res));
        return;
      }
      if (!res.ok) {
        alert(maxErrorText(res));
        if (res.need_new_code) {
          if ($("#maxCode")) $("#maxCode").value = "";
          $("#maxCodeStep")?.classList.add("hidden");
          setMaxConnectStep(1);
          $("#maxPhone")?.focus();
        }
        return;
      }
      showMaxConnectDone(res);
      loadAccounts({ check: true });
    } catch (err) {
      const data = err.data || {};
      if (data.need_2fa) {
        $("#max2faWrap").classList.remove("hidden");
        $("#max2fa")?.focus();
        alert(maxErrorText(err));
      } else {
        alert(maxErrorText(err));
        if (data.need_new_code) {
          $("#maxCodeStep")?.classList.add("hidden");
          setMaxConnectStep(1);
        }
      }
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Подтвердить";
      }
    }
  });

  // ── wizard ──────────────────────────────────────────────────────────────

  const DEFAULT_MSG =
    "Здравствуйте, {имя}!\n\nТолько для вас — весенние букеты со скидкой 15%.\n\nЗаказать: veresk.flowers";

  const SEG_LABELS = {
    regular: "Постоянные",
    all: "Все клиенты",
    new: "Новые",
    inactive: "Давно не заказывали",
    selected: "Выбранные клиенты",
  };

  const AI_CHIP_PROMPTS = {
    regular: {
      promo: "Тёплое предложение со скидкой 15% для постоянных клиентов, благодарность за доверие",
      holiday: "Напомнить постоянным клиентам о празднике и предложить заказать букет заранее",
      new: "Анонс новинок для постоянных клиентов, пригласить посмотреть в салоне",
      winback: "Мягко напомнить постоянным клиентам о себе без давления",
    },
    all: {
      promo: "Скидка 15% на весенние букеты, тёплое предложение для клиентов",
      holiday: "Напомнить о предстоящем празднике и предложить заказать букет заранее",
      new: "Анонс новых букетов в салоне, пригласить посмотреть",
      winback: "Короткое дружелюбное приглашение заглянуть за букетом",
    },
    new: {
      promo: "Приветствие новым клиентам со скидкой 15% на первый/следующий букет",
      holiday: "Познакомить новых клиентов с салоном к празднику, мягко предложить букет",
      new: "Показать новинки новым клиентам, пригласить в салон",
      winback: "Тёплое продолжение знакомства с новыми клиентами без давления",
    },
    inactive: {
      promo: "Мягко вернуть клиентов со скидкой 15%, без давления и упрёков",
      holiday: "Напомнить о празднике клиентам, которые давно не заказывали",
      new: "Показать, что в салоне появились новые букеты — пригласить вернуться",
      winback: "Мягко вернуть клиентов, которые давно не заказывали, без давления",
    },
  };

  function selectedChannels() {
    const list = [];
    if ($("#chanTg")?.classList.contains("on")) list.push("tg");
    if ($("#chanMax")?.classList.contains("on")) list.push("max");
    return list.length ? list : ["tg"];
  }

  function channelsLabel(list) {
    return list
      .map((c) => (c === "max" ? "MAX" : "Telegram"))
      .join(" + ");
  }

  function currentAudienceMode() {
    return state.wizard.audienceMode === "pick" ? "pick" : "segment";
  }

  function currentSegment() {
    if (currentAudienceMode() === "pick") return "selected";
    return $("#s0 .choice.on")?.dataset.seg || state.wizard.segment || "all";
  }

  function segmentLabel(seg) {
    if (seg === "selected") {
      const n = (state.wizard.selectedCustomers || []).length;
      return n ? `Выбранные · ${fmtNum(n)}` : "Выбранные клиенты";
    }
    return SEG_LABELS[seg] || "Клиенты";
  }

  function selectedCustomerIds() {
    return (state.wizard.selectedCustomers || []).map((c) => c.id);
  }

  function hasAudience() {
    if (currentAudienceMode() === "pick") {
      return (
        selectedCustomerIds().length > 0 && (state.wizard.willSend || 0) > 0
      );
    }
    return (state.wizard.willSend || 0) > 0;
  }

  function setAudienceError(show) {
    const el = $("#audienceError");
    if (!el) return;
    el.hidden = !show;
    if (show && currentAudienceMode() === "pick" && !selectedCustomerIds().length) {
      el.textContent =
        "Выберите хотя бы одного клиента с Telegram или MAX.";
    } else if (show) {
      el.textContent =
        "Нет получателей для отправки — смените сегмент, канал или подключите аккаунт в Настройках.";
    }
  }

  function messengerBadgesHtml(messengers, { compact = false } = {}) {
    const m = messengers || {};
    const badges = [];
    const tg = m.tg || {};
    const mx = m.max || {};
    if (tg.linked) {
      badges.push(
        `<span class="ms-badge tg"${compact ? "" : ' title="Есть Telegram id"'}>Telegram</span>`
      );
    } else if (tg.by_phone || tg.reachable) {
      badges.push(
        `<span class="ms-badge tg soft"${
          compact ? "" : ' title="Можно отправить по телефону"'
        }>${compact ? "TG·тел" : "TG · телефон"}</span>`
      );
    }
    if (mx.linked) {
      badges.push(
        `<span class="ms-badge max"${compact ? "" : ' title="Есть MAX id"'}>MAX</span>`
      );
    }
    if (!badges.length) {
      badges.push(`<span class="ms-badge none">нет</span>`);
    }
    return badges.join("");
  }

  function clientHasMessenger(c) {
    const m = c?.messengers || {};
    return (
      (m.tg && (m.tg.linked || m.tg.reachable)) || (m.max && m.max.linked)
    );
  }

  function setAudienceMode(mode) {
    const next = mode === "pick" ? "pick" : "segment";
    state.wizard.audienceMode = next;
    $$(".aud-mode-btn").forEach((b) => {
      const on = b.dataset.aud === next;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    const segBlock = $("#audSegmentBlock");
    const pickBlock = $("#audPickBlock");
    if (segBlock) segBlock.hidden = next === "pick";
    if (pickBlock) pickBlock.hidden = next !== "pick";
    if (next === "pick") {
      state.wizard.segment = "selected";
      renderPickSelected();
      loadPickClients(($("#pickSearch")?.value || "").trim());
    } else if (state.wizard.segment === "selected") {
      state.wizard.segment =
        $("#s0 .choice.on")?.dataset.seg || "regular";
    }
    refreshMatchPreview();
  }

  function renderPickSelected() {
    const wrap = $("#pickSelected");
    const chips = $("#pickChips");
    const countEl = $("#pickSelectedCount");
    const list = state.wizard.selectedCustomers || [];
    if (countEl) countEl.textContent = String(list.length);
    if (wrap) wrap.hidden = !list.length;
    if (!chips) return;
    chips.innerHTML = list
      .map(
        (c) => `<span class="aud-chip" data-id="${c.id}">
          <span class="aud-chip-name">${esc(c.name || "Клиент")}</span>
          <span class="aud-chip-ms">${messengerBadgesHtml(c.messengers, { compact: true })}</span>
          <button type="button" class="aud-chip-x" data-remove="${c.id}" aria-label="Убрать">×</button>
        </span>`
      )
      .join("");
  }

  function renderPickResults(items) {
    const box = $("#pickResults");
    if (!box) return;
    const selected = new Set(selectedCustomerIds());
    if (!items.length) {
      const q = ($("#pickSearch")?.value || "").trim();
      box.innerHTML = `<div class="aud-pick-empty">${
        q
          ? "Никого не нашли по фильтру"
          : "Клиентов пока нет — синхронизируйте базу"
      }</div>`;
      return;
    }
    box.innerHTML = items
      .map((c) => {
        const on = selected.has(c.id);
        const hasAny = clientHasMessenger(c);
        const disabled = !hasAny && !on;
        return `<button type="button" class="aud-pick-row${on ? " on" : ""}${
          disabled ? " disabled" : ""
        }" data-pick-id="${c.id}" ${disabled ? "disabled" : ""} role="option" aria-selected="${on}">
          <span class="aud-pick-av">${esc(initials(c.name))}</span>
          <span class="aud-pick-meta">
            <div class="aud-pick-name">${esc(c.name || "Клиент")}</div>
            <div class="aud-pick-phone">${esc(c.phone_masked || c.phone || "—")}</div>
          </span>
          <span class="aud-pick-ms">${messengerBadgesHtml(c.messengers)}</span>
        </button>`;
      })
      .join("");
  }

  function togglePickCustomer(c) {
    if (!c || !c.id) return;
    const list = state.wizard.selectedCustomers || [];
    const idx = list.findIndex((x) => x.id === c.id);
    if (idx >= 0) {
      list.splice(idx, 1);
    } else {
      if (!clientHasMessenger(c)) return;
      list.push({
        id: c.id,
        name: c.name || "Клиент",
        phone: c.phone || "",
        phone_masked: c.phone_masked || c.phone || "",
        messengers: c.messengers || null,
      });
    }
    state.wizard.selectedCustomers = list;
    renderPickSelected();
    if (pickSearchCache.length) renderPickResults(pickSearchCache);
    refreshMatchPreview();
  }

  let pickSearchTimer = null;
  let pickSearchCache = [];
  let pickSearchSeq = 0;
  let pickListTotal = 0;

  async function loadPickClients(query) {
    const box = $("#pickResults");
    const hint = $("#pickListHint");
    const seq = ++pickSearchSeq;
    const q = (query || "").trim();
    if (box) box.innerHTML = `<div class="aud-pick-loading">${q ? "Ищем…" : "Загрузка…"}</div>`;
    try {
      const params = { page_size: 100 };
      if (q) params.search = q;
      const data = await AdminAPI.clients(params);
      if (seq !== pickSearchSeq) return;
      pickSearchCache = data.items || [];
      pickListTotal = data.total || pickSearchCache.length;
      if (hint) {
        hint.textContent = q
          ? `найдено ${fmtNum(pickSearchCache.length)}${
              pickListTotal > pickSearchCache.length
                ? ` из ${fmtNum(pickListTotal)}`
                : ""
            }`
          : `${fmtNum(pickListTotal)} в базе · нажмите, чтобы выбрать`;
      }
      renderPickResults(pickSearchCache);
    } catch (_) {
      if (seq !== pickSearchSeq) return;
      pickSearchCache = [];
      pickListTotal = 0;
      if (hint) hint.textContent = "не удалось загрузить";
      if (box)
        box.innerHTML = `<div class="aud-pick-empty">Не удалось загрузить список</div>`;
    }
  }

  function updateAudienceContext() {
    const el = $("#wizAudienceText");
    if (!el) return;
    const seg = currentSegment();
    const will = state.wizard.willSend;
    const ch = channelsLabel(selectedChannels());
    const count =
      will == null ? "…" : will > 0 ? fmtNum(will) + " доставок" : "нет доставок";
    el.textContent = `${segmentLabel(seg)} · ${count} · ${ch}`;
  }

  function adaptAiChipsForSegment() {
    const seg = currentSegment() === "selected" ? "all" : currentSegment();
    const map = AI_CHIP_PROMPTS[seg] || AI_CHIP_PROMPTS.all;
    $$("#aiChips .ai-chip").forEach((chip) => {
      const key = chip.dataset.chip;
      if (key && map[key]) chip.dataset.prompt = map[key];
    });
  }

  function refreshSendSummary() {
    const seg = currentSegment();
    const will = state.wizard.willSend;
    const channels = selectedChannels();
    if ($("#sumSeg")) $("#sumSeg").textContent = segmentLabel(seg);
    if ($("#sumChannels")) $("#sumChannels").textContent = channelsLabel(channels);
    if ($("#sumWho")) {
      $("#sumWho").textContent =
        will == null ? "…" : will > 0 ? fmtNum(will) + " доставок" : "нет получателей";
    }
    if ($("#sumMsg")) {
      const raw = (msgTa?.value || "").trim().replace(/\s+/g, " ");
      $("#sumMsg").textContent = raw
        ? raw.slice(0, 90) + (raw.length > 90 ? "…" : "")
        : "—";
    }
    const mediaRow = $("#sumMediaRow");
    const mediaLabel = $("#sumMedia");
    if (mediaRow) {
      const has = !!(state.wizard.media && state.wizard.media.media_path);
      mediaRow.hidden = !has;
      if (mediaLabel) {
        mediaLabel.textContent = has
          ? state.wizard.media.media_filename || "прикреплено"
          : "—";
      }
    }
    updateWhenSummary();
  }

  function updateWhenSummary() {
    const when = state.wizard.when || $("#s2 .choice.on")?.dataset.when || "now";
    const sumWhen = $("#sumWhen");
    if (!sumWhen) return;
    if (when === "now") sumWhen.textContent = "сейчас";
    else
      sumWhen.textContent =
        ($("#wizDate").value || "") + ", " + ($("#wizTime").value || "10:00");
  }

  function syncWizardCta() {
    const nextBtn = $("#wnext");
    if (!nextBtn || state.step >= 3) return;
    if (state.step === 2) {
      const when = state.wizard.when || $("#s2 .choice.on")?.dataset.when || "now";
      nextBtn.textContent = when === "now" ? "Отправить сейчас" : "Запланировать";
    } else {
      nextBtn.textContent = "Далее";
    }
  }

  async function refreshMatchPreview() {
    const segment = currentSegment();
    const channels = selectedChannels();
    const mode = currentAudienceMode();
    state.wizard.channels = channels;
    state.wizard.segment = segment;
    const box = $("#matchPreview");
    if (mode === "pick" && !selectedCustomerIds().length) {
      state.wizard.willSend = 0;
      if ($("#matchWill")) $("#matchWill").textContent = "0 доставок";
      if ($("#matchTg")) $("#matchTg").textContent = "0";
      if ($("#matchMax")) $("#matchMax").textContent = "0";
      if ($("#matchTgRow")) $("#matchTgRow").hidden = !channels.includes("tg");
      if ($("#matchMaxRow")) $("#matchMaxRow").hidden = !channels.includes("max");
      const note = $("#matchNote");
      if (note) {
        note.textContent = "Выберите клиентов из списка — справа видны Telegram и MAX.";
        note.className = "match-preview-note";
      }
      if (box) box.hidden = false;
      updateAudienceContext();
      if (state.step === 2) refreshSendSummary();
      syncComposeNext();
      return;
    }
    try {
      const params = {
        segment,
        channels: channels.join(","),
      };
      if (mode === "pick") {
        params.customer_ids = selectedCustomerIds().join(",");
      }
      const data = await AdminAPI.mailingPreview(params);
      const will = data.will_send || 0;
      state.wizard.willSend = will;
      const tgN = (data.reachable && data.reachable.tg) || 0;
      const maxN = (data.reachable && data.reachable.max) || 0;
      if ($("#matchWill")) $("#matchWill").textContent = fmtNum(will) + " доставок";
      if ($("#matchTg")) $("#matchTg").textContent = fmtNum(tgN);
      if ($("#matchMax")) $("#matchMax").textContent = fmtNum(maxN);
      if ($("#matchTgRow")) $("#matchTgRow").hidden = !channels.includes("tg");
      if ($("#matchMaxRow")) $("#matchMaxRow").hidden = !channels.includes("max");

      const tgAcc = data.accounts && data.accounts.tg;
      const maxAcc = data.accounts && data.accounts.max;
      if ($("#chanTgMeta")) {
        $("#chanTgMeta").textContent = tgAcc?.ready
          ? tgAcc.count > 1
            ? tgAcc.count + " акк."
            : "аккаунт готов"
          : "нет аккаунта";
      }
      if ($("#chanMaxMeta")) {
        const modeMax = maxAcc?.mode;
        if (modeMax === "userbot") {
          $("#chanMaxMeta").textContent = maxAcc.label
            ? "личный · " + maxAcc.label
            : maxAcc.userbot_count > 1
              ? maxAcc.userbot_count + " акк."
              : "личный аккаунт";
        } else if (modeMax === "bot") {
          $("#chanMaxMeta").textContent = "бот подключён";
        } else {
          $("#chanMaxMeta").textContent = "не подключён";
        }
      }
      $("#chanTg")?.classList.toggle("is-off", !tgAcc?.ready);
      $("#chanMax")?.classList.toggle("is-off", !maxAcc?.ready);

      // обновить messengers у выбранных, если сервер вернул selected
      if (mode === "pick" && Array.isArray(data.selected)) {
        const byId = Object.fromEntries(
          data.selected.map((s) => [s.id, s.messengers])
        );
        state.wizard.selectedCustomers = (
          state.wizard.selectedCustomers || []
        ).map((c) =>
          byId[c.id] ? { ...c, messengers: byId[c.id] } : c
        );
        renderPickSelected();
      }

      const note = $("#matchNote");
      if (note) {
        const skipped = data.skipped || {};
        const skipTotal = Object.values(skipped).reduce((a, b) => a + b, 0);
        if (!will) {
          note.textContent =
            mode === "pick"
              ? "У выбранных нет доставки в отмеченные каналы — смените канал или клиента."
              : "Нет доставляемых получателей — подключите аккаунты или выберите другой сегмент/канал.";
          note.className = "match-preview-note warn";
        } else if (skipTotal) {
          note.textContent =
            "Пропущено " +
            fmtNum(skipTotal) +
            " (нет привязки к каналу или аккаунт недоступен).";
          note.className = "match-preview-note warn";
        } else {
          note.textContent =
            mode === "pick"
              ? "Выбранные клиенты сверены с Telegram и MAX."
              : "Все выбранные клиенты сверены с аккаунтами.";
          note.className = "match-preview-note ok";
        }
      }
      if (box) box.hidden = false;
      if (will > 0) setAudienceError(false);
      updateAudienceContext();
      if (state.step === 2) refreshSendSummary();
      syncComposeNext();
    } catch (_) {
      state.wizard.willSend = null;
      if (box) box.hidden = true;
      updateAudienceContext();
      syncComposeNext();
    }
  }

  async function refreshSegmentCounts() {
    try {
      const s = await AdminAPI.segments();
      $$("#s0 .choice").forEach((c) => {
        const key = c.dataset.seg;
        const n = s[key] ?? 0;
        c.dataset.count = String(n);
        const cc = c.querySelector(".cc");
        if (cc) cc.textContent = fmtNum(n) + " человек";
      });
      await refreshMatchPreview();
    } catch (_) {}
  }

  function clearComposeMedia() {
    if (state.wizard.media?.localUrl) {
      try {
        URL.revokeObjectURL(state.wizard.media.localUrl);
      } catch (_) {}
    }
    state.wizard.media = null;
    const fileInp = $("#mediaFile");
    if (fileInp) fileInp.value = "";
    syncMediaUi();
  }

  function syncMediaUi() {
    const media = state.wizard.media;
    const wrap = $("#mediaPreviewWrap");
    const img = $("#mediaPreviewImg");
    const nameEl = $("#mediaPreviewName");
    const pick = $("#mediaPick");
    const hint = $("#mediaHint");
    const bubbleMedia = $("#msgMediaPreview");
    const bubbleImg = $("#msgMediaImg");
    const err = $("#mediaError");
    if (err) err.hidden = true;
    if (wrap) wrap.hidden = !media;
    if (pick) pick.hidden = !!media;
    if (hint) hint.hidden = !!media;
    if (media) {
      if (img) img.src = media.localUrl || AdminAPI.campaignMediaUrl(media.media_path);
      if (nameEl) nameEl.textContent = media.media_filename || "фото";
      if (bubbleMedia) bubbleMedia.hidden = false;
      if (bubbleImg)
        bubbleImg.src = media.localUrl || AdminAPI.campaignMediaUrl(media.media_path);
    } else {
      if (bubbleMedia) bubbleMedia.hidden = true;
      if (bubbleImg) bubbleImg.removeAttribute("src");
    }
  }

  function setMediaError(text) {
    const err = $("#mediaError");
    if (!err) return;
    if (!text) {
      err.hidden = true;
      err.textContent = "";
      return;
    }
    err.hidden = false;
    err.textContent = text;
  }

  async function onMediaFileChosen(file) {
    if (!file) return;
    if (!String(file.type || "").startsWith("image/")) {
      setMediaError("Можно прикрепить только фото");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setMediaError("Файл больше 10 МБ");
      return;
    }
    const box = $("#mediaAttach");
    box?.classList.add("busy");
    setMediaError("");
    const localUrl = URL.createObjectURL(file);
    try {
      const res = await AdminAPI.uploadCampaignMedia(file);
      if (state.wizard.media?.localUrl) {
        try {
          URL.revokeObjectURL(state.wizard.media.localUrl);
        } catch (_) {}
      }
      state.wizard.media = {
        media_path: res.media_path,
        media_kind: res.media_kind || "photo",
        media_filename: res.media_filename || file.name,
        media_mime: res.media_mime || file.type,
        localUrl,
      };
      syncMediaUi();
    } catch (err) {
      try {
        URL.revokeObjectURL(localUrl);
      } catch (_) {}
      const msg =
        err.data?.message ||
        (err.data?.error === "only_images"
          ? "Можно прикрепить только фото"
          : err.data?.error === "file_too_large"
            ? "Файл больше 10 МБ"
            : null) ||
        err.message ||
        "Не удалось загрузить фото";
      setMediaError(msg);
    }
    box?.classList.remove("busy");
  }

  $("#mediaPick")?.addEventListener("click", () => $("#mediaFile")?.click());
  $("#mediaClear")?.addEventListener("click", () => {
    clearComposeMedia();
  });
  $("#mediaFile")?.addEventListener("change", () => {
    const file = $("#mediaFile")?.files?.[0];
    onMediaFileChosen(file);
  });

  function resetComposeForm() {
    setAudienceError(false);
    setMsgError(false);
    setAiOpen(false);
    if (aiUndoRow) aiUndoRow.hidden = true;

    // повтор рассылки: оставляем текст/аудиторию, которые уже выставили снаружи
    if (state.wizard.keepMessage) {
      state.wizard.keepMessage = false;
      state.wizard.when = "now";
      $$("#s2 .choice").forEach((c) =>
        c.classList.toggle("on", c.dataset.when === "now")
      );
      const datebox = $("#datebox");
      if (datebox) datebox.style.display = "none";
      updatePreview();
      updateAudienceContext();
      syncMediaUi();
      if (currentAudienceMode() === "pick") {
        renderPickSelected();
        loadPickClients(($("#pickSearch")?.value || "").trim());
      }
      refreshMatchPreview();
      return;
    }

    state.wizard.segment = "regular";
    state.wizard.audienceMode = "segment";
    state.wizard.selectedCustomers = [];
    state.wizard.when = "now";
    state.wizard.willSend = null;
    state.wizard.channels = ["tg"];
    clearComposeMedia();
    $$(".aud-mode-btn").forEach((b) => {
      const on = b.dataset.aud === "segment";
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    const segBlock = $("#audSegmentBlock");
    const pickBlock = $("#audPickBlock");
    if (segBlock) segBlock.hidden = false;
    if (pickBlock) pickBlock.hidden = true;
    if ($("#pickSearch")) $("#pickSearch").value = "";
    pickSearchCache = [];
    renderPickSelected();
    renderPickResults([]);
    $$("#s0 .choice").forEach((c) =>
      c.classList.toggle("on", c.dataset.seg === "regular")
    );
    $("#chanTg")?.classList.add("on");
    $("#chanTg")?.setAttribute("aria-pressed", "true");
    $("#chanMax")?.classList.remove("on");
    $("#chanMax")?.setAttribute("aria-pressed", "false");
    $$("#s2 .choice").forEach((c) =>
      c.classList.toggle("on", c.dataset.when === "now")
    );
    const datebox = $("#datebox");
    if (datebox) datebox.style.display = "none";
    if (msgTa) msgTa.value = DEFAULT_MSG;
    updatePreview();
    updateAudienceContext();
  }

  $$(".aud-mode-btn").forEach((btn) =>
    btn.addEventListener("click", () => setAudienceMode(btn.dataset.aud))
  );

  $("#pickSearch")?.addEventListener("input", () => {
    clearTimeout(pickSearchTimer);
    const q = ($("#pickSearch").value || "").trim();
    pickSearchTimer = setTimeout(() => {
      loadPickClients(q);
    }, 280);
  });

  $("#pickResults")?.addEventListener("click", (e) => {
    const row = e.target.closest("[data-pick-id]");
    if (!row || row.disabled) return;
    const id = Number(row.dataset.pickId);
    const fromCache = pickSearchCache.find((c) => c.id === id);
    const fromSelected = (state.wizard.selectedCustomers || []).find(
      (c) => c.id === id
    );
    togglePickCustomer(fromCache || fromSelected || { id });
  });

  $("#pickChips")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-remove]");
    if (!btn) return;
    const id = Number(btn.dataset.remove);
    state.wizard.selectedCustomers = (
      state.wizard.selectedCustomers || []
    ).filter((c) => c.id !== id);
    renderPickSelected();
    if (pickSearchCache.length) renderPickResults(pickSearchCache);
    refreshMatchPreview();
  });

  $$("#s0 .choice").forEach((c) =>
    c.addEventListener("click", () => {
      $$("#s0 .choice").forEach((x) => x.classList.remove("on"));
      c.classList.add("on");
      state.wizard.segment = c.dataset.seg;
      refreshMatchPreview();
    })
  );

  $$("#wizChannels .chan-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      btn.classList.toggle("on");
      btn.setAttribute("aria-pressed", btn.classList.contains("on") ? "true" : "false");
      if (!selectedChannels().length) {
        btn.classList.add("on");
        btn.setAttribute("aria-pressed", "true");
      }
      refreshMatchPreview();
    })
  );

  $$("#s2 .choice").forEach((c) =>
    c.addEventListener("click", () => {
      $$("#s2 .choice").forEach((x) => x.classList.remove("on"));
      c.classList.add("on");
      state.wizard.when = c.dataset.when;
      $("#datebox").style.display = c.dataset.when === "later" ? "flex" : "none";
      updateWhenSummary();
      syncWizardCta();
    })
  );

  $("#wizDate")?.addEventListener("change", updateWhenSummary);
  $("#wizTime")?.addEventListener("change", updateWhenSummary);
  $("#wizTime")?.addEventListener("input", updateWhenSummary);

  const msgTa = $("#msg");
  const msgError = $("#msgError");
  const msgPreviewEl = $("#msgPreview");

  function messageHasText() {
    return !!(msgTa && msgTa.value.trim());
  }

  function setMsgError(show) {
    if (!msgError) return;
    msgError.hidden = !show;
    msgTa?.classList.toggle("has-error", !!show);
  }

  function syncComposeNext() {
    const nextBtn = $("#wnext");
    if (!nextBtn || state.step >= 3) return;
    if (state.step === 0) {
      nextBtn.disabled = !hasAudience();
    } else if (state.step === 1) {
      nextBtn.disabled = !messageHasText();
    } else if (state.step === 2) {
      nextBtn.disabled = !hasAudience() || !messageHasText();
    }
    syncWizardCta();
  }

  function updatePreview() {
    if (!msgPreviewEl || !msgTa) return;
    const raw = msgTa.value;
    const disc = "15%";
    if (!raw.trim()) {
      msgPreviewEl.innerHTML = `<span class="pv-empty">Текст появится здесь…</span>`;
    } else {
      msgPreviewEl.innerHTML = esc(raw)
        .replace(/\{имя\}/g, '<b class="pv-var">Мария</b>')
        .replace(/\{скидка\}/gi, `<b class="pv-var">${esc(disc)}</b>`)
        .replace(/\n/g, "<br>");
    }
    if (messageHasText()) setMsgError(false);
    syncComposeNext();
  }

  function insertAtCursor(ta, chunk) {
    if (!ta || !chunk) return;
    const start = ta.selectionStart ?? ta.value.length;
    const end = ta.selectionEnd ?? ta.value.length;
    const before = ta.value.slice(0, start);
    const after = ta.value.slice(end);
    const needsSpaceBefore =
      before.length && !/\s$/.test(before) && !/^[\s.,!?;:]/.test(chunk);
    const insert = (needsSpaceBefore ? " " : "") + chunk;
    ta.value = before + insert + after;
    const pos = before.length + insert.length;
    ta.focus();
    ta.setSelectionRange(pos, pos);
  }

  msgTa?.addEventListener("input", updatePreview);
  $$("#s1 .ins").forEach((b) =>
    b.addEventListener("click", () => {
      const chunk = b.dataset.insert || "";
      insertAtCursor(msgTa, chunk);
      updatePreview();
    })
  );
  updatePreview();

  // ── AI editor (compose step) ─────────────────────────────────────────────
  let aiPrevText = "";
  const aiEditor = $("#aiEditor");
  const aiToggle = $("#aiToggle");
  const aiPrompt = $("#aiPrompt");
  const aiStatus = $("#aiStatus");
  const aiUndoRow = $("#aiUndoRow");

  function setAiOpen(open) {
    if (!aiEditor || !aiToggle) return;
    aiEditor.hidden = !open;
    aiToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      aiPrompt?.focus();
      aiEditor.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function setAiStatus(text, kind) {
    if (!aiStatus) return;
    if (!text) {
      aiStatus.hidden = true;
      aiStatus.textContent = "";
      aiStatus.className = "ai-editor-status";
      return;
    }
    aiStatus.hidden = false;
    aiStatus.textContent = text;
    aiStatus.className = "ai-editor-status" + (kind ? " " + kind : "");
  }

  function setAiBusy(busy) {
    ["aiGenerate", "aiImprove", "aiToggle"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.disabled = busy;
    });
    $$("#aiChips .ai-chip").forEach((c) => {
      c.disabled = busy;
    });
    if (aiPrompt) aiPrompt.disabled = busy;
    const gen = $("#aiGenerate");
    if (gen) {
      gen.innerHTML = busy
        ? "Генерирую…"
        : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/><circle cx="12" cy="12" r="3.2"/></svg> Сгенерировать`;
    }
  }

  async function runAiCompose(mode) {
    const prompt = (aiPrompt?.value || "").trim();
    const current = msgTa?.value || "";
    if (mode === "write" && !prompt) {
      setAiStatus("Кратко опишите, о чём сообщение — или нажмите подсказку сверху", "err");
      aiPrompt?.focus();
      return;
    }
    if (mode === "improve" && !current.trim()) {
      setAiStatus("Сначала напишите или вставьте черновик в поле ниже", "err");
      msgTa?.focus();
      return;
    }
    const segment = currentSegment();
    setAiBusy(true);
    setAiStatus(mode === "improve" ? "Улучшаю текст…" : "Пишу текст…");
    try {
      const res = await AdminAPI.aiCompose({
        prompt,
        current_text: current,
        segment,
        mode,
      });
      const text = (res.text || "").trim();
      if (!text) throw new Error("empty");
      aiPrevText = current;
      msgTa.value = text;
      updatePreview();
      if (aiUndoRow) aiUndoRow.hidden = false;
      setAiStatus("Готово — текст вставлен в сообщение. Превью обновлено.", "ok");
      msgTa.focus();
    } catch (err) {
      const detail =
        err.data?.detail ||
        (err.data?.error === "ai_not_configured"
          ? "Подключите ИИ в Настройках → Сервисы"
          : null) ||
        err.message ||
        "Не удалось сгенерировать";
      setAiStatus(detail, "err");
    }
    setAiBusy(false);
  }

  aiToggle?.addEventListener("click", () => {
    const open = aiToggle.getAttribute("aria-expanded") !== "true";
    setAiOpen(open);
    if (open) setAiStatus("");
  });
  $("#aiClose")?.addEventListener("click", () => setAiOpen(false));
  $("#aiGenerate")?.addEventListener("click", () => runAiCompose("write"));
  $("#aiImprove")?.addEventListener("click", () => runAiCompose("improve"));
  $("#aiUndo")?.addEventListener("click", () => {
    if (msgTa && aiPrevText !== undefined) {
      msgTa.value = aiPrevText;
      updatePreview();
    }
    if (aiUndoRow) aiUndoRow.hidden = true;
    setAiStatus("Вернули предыдущий текст", "ok");
  });
  $$("#aiChips .ai-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      $$("#aiChips .ai-chip").forEach((c) => c.classList.remove("on"));
      chip.classList.add("on");
      if (aiPrompt) aiPrompt.value = chip.dataset.prompt || chip.textContent;
      runAiCompose("write");
    });
  });
  aiPrompt?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      runAiCompose("write");
    }
  });

  (function initDate() {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    const iso = d.toISOString().slice(0, 10);
    const dateInp = $("#wizDate");
    if (dateInp) dateInp.value = iso;
  })();

  const stepEls = $$(".stepper .step");
  const barEls = $$(".stepper .bar");
  const ids = ["s0", "s1", "s2", "s3"];
  const wback = $("#wback");
  const wnext = $("#wnext");
  const wnav = $("#wnav");

  function setStep(i) {
    state.step = i;
    ids.forEach((id, idx) =>
      document.getElementById(id).classList.toggle("active", idx === i)
    );
    stepEls.forEach((s, idx) => {
      s.classList.toggle("active", idx === i);
      s.classList.toggle("done", idx < i);
    });
    barEls.forEach((b, idx) => {
      b.classList.toggle("done", idx < i);
      b.style.background = idx < i ? "var(--accent)" : "var(--line)";
    });
    wback.style.display = i > 0 && i < 3 ? "inline-flex" : "none";
    wnav.style.display = i < 3 ? "flex" : "none";
    if (i === 0) {
      refreshSegmentCounts();
    }
    if (i === 1) {
      updateAudienceContext();
      adaptAiChipsForSegment();
      updatePreview();
      setMsgError(false);
    }
    if (i === 2) {
      refreshSendSummary();
      refreshMatchPreview();
    }
    syncComposeNext();
  }

  wnext?.addEventListener("click", async () => {
    if (state.step === 0) {
      if (!hasAudience()) {
        setAudienceError(true);
        $("#matchPreview")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
        return;
      }
      setStep(1);
      return;
    }
    if (state.step === 1) {
      if (!messageHasText()) {
        setMsgError(true);
        msgTa?.focus();
        return;
      }
      setStep(2);
      return;
    }
    const segment = currentSegment();
    const sendNow =
      state.wizard.when === "now" || $("#s2 .choice.on")?.dataset.when === "now";
    let scheduled_at = null;
    if (!sendNow) {
      scheduled_at = `${$("#wizDate").value}T${$("#wizTime").value || "10:00"}:00`;
    }
    const title =
      msgTa.value.split("\n")[0].slice(0, 60).replace(/\{имя\}/g, "").trim() ||
      "Рассылка";
    try {
      wnext.disabled = true;
      const channels = selectedChannels();
      const media = state.wizard.media;
      const body = {
        title,
        message: msgTa.value,
        segment,
        channels: channels.join(","),
        emoji: "🌷",
        send_now: sendNow,
        scheduled_at,
        media_path: media?.media_path || undefined,
        media_kind: media?.media_kind || undefined,
        media_filename: media?.media_filename || undefined,
        media_mime: media?.media_mime || undefined,
      };
      if (currentAudienceMode() === "pick") {
        body.customer_ids = selectedCustomerIds();
        body.segment = "selected";
      }
      const created = await AdminAPI.createCampaign(body);
      clearComposeMedia();
      setStep(3);
      const will =
        (created.match && created.match.will_send) ||
        created.total_count ||
        0;
      $("#successText").textContent = sendNow
        ? `Рассылка запущена: ${fmtNum(will)} сообщений уходят порциями от имени аккаунтов.`
        : `Сообщение уйдёт ${fmtNum(will)} получателям ${scheduled_at || ""} через ${channelsLabel(channels)}.`;
    } catch (err) {
      const detail =
        err.data?.message ||
        (err.data?.error === "no_reachable_recipients"
          ? "Нет получателей после сверки с аккаунтами"
          : null) ||
        err.data?.error ||
        err.message;
      alert("Ошибка: " + detail);
      syncComposeNext();
    }
    if (state.step < 3) wnext.disabled = false;
  });
  wback?.addEventListener("click", () => {
    if (state.step > 0) setStep(state.step - 1);
  });
  $("#wexit")?.addEventListener("click", () => go("home"));
  $("#wdone")?.addEventListener("click", () => go("home"));
  $("#wagain")?.addEventListener("click", () => {
    go("compose");
    setStep(0);
  });

  $("#wgAll")?.addEventListener("click", async () => {
    state.eventsExpanded = !state.eventsExpanded;
    syncEventsFilters();
    if (state.eventsExpanded && state.eventsDays < 60) {
      await loadEvents(60);
      return;
    }
    renderEvents({
      items: state.eventsCache,
      total: state.eventsCache.length,
      today_count: state.eventsCache.filter((e) => e.days_until === 0).length,
      auto_count: state.eventsCache.filter((e) => e.auto_send).length,
    });
  });

  $$("#wgFilters .wg-f").forEach((btn) => {
    btn.addEventListener("click", () => {
      const days = +btn.dataset.days || 7;
      state.eventsExpanded = false;
      loadEvents(days);
    });
  });

  // ── AI chat ──────────────────────────────────────────────────────────────

  const AI_CHAT_KEY_BASE = "veresk_ai_chat_v2";
  const AI_WELCOME =
    "Я вижу CRM, заказы, заметки, анкеты ботов, рассылки и колесо. Спросите имя/телефон клиента — или откройте карточку и нажмите «Спросить ИИ».";

  const AI_CHIP_SETS = {
    day: [
      {
        label: "Кого поздравить",
        prompt:
          "Кого поздравить в ближайшие 14 дней? Дай список с датами и короткий приоритет, кому писать первым.",
      },
      {
        label: "План на смену",
        prompt:
          "Краткий план на смену: события на 7 дней, идея одной рассылки и чек-лист перед отправкой. Без выдуманных скидок.",
      },
      {
        label: "Сводка CRM",
        prompt:
          "Кратко: сколько клиентов по сегментам, доставляемость и что проверить перед новой рассылкой?",
      },
    ],
    copy: [
      {
        label: "Вернуть inactive",
        prompt:
          "Идея и готовый текст рассылки для сегмента inactive: вернуть клиентов тёплым предложением без выдуманных скидок. Плейсхолдер {имя}.",
      },
      {
        label: "Текст к 8 Марта",
        prompt:
          "Напиши короткий текст рассылки к 8 Марта для постоянных клиентов, плейсхолдер {имя}. Без выдуманных скидок.",
      },
      {
        label: "Личное ДР",
        prompt:
          "Черновик личного поздравления с днём рождения: тёплый, короткий, на «вы», плейсхолдер {имя}. В блоке ```текст.",
      },
    ],
    crm: [
      {
        label: "Как искать клиента",
        prompt:
          "Объясни коротко: как лучше спрашивать тебя про клиента (имя, телефон, или кнопка «Спросить ИИ» в карточке) и что ты видишь: CRM, заметки, заказы, анкеты ботов, колесо, чаты.",
      },
      {
        label: "Перед рассылкой",
        prompt:
          "Чек-лист перед рассылкой: сегмент, текст, каналы TG/MAX, аккаунты ready, что не обещать клиенту.",
      },
      {
        label: "Regular идея",
        prompt:
          "Идея короткой рассылки для сегмента regular на эту неделю + готовый текст с {имя}, без выдуманных акций.",
      },
    ],
  };

  let aiChatReady = false;
  let aiAbort = null;
  const aiChat = {
    configured: false,
    busy: false,
    messages: [], // {role, content}
    suggestions: [],
    chipCat: "day",
    focusCustomerId: null,
    focusCustomerName: "",
  };

  function aiStorageKey() {
    const uid =
      (authMe && (authMe.user_id || authMe.id || authMe.username)) ||
      localStorage.getItem(LOGIN_KEY) ||
      "anon";
    return AI_CHAT_KEY_BASE + ":" + String(uid);
  }

  function aiWelcomeHtml() {
    const cards = [
      ...AI_CHIP_SETS.day.slice(0, 2),
      AI_CHIP_SETS.copy[0],
      AI_CHIP_SETS.crm[1],
    ];
    return `
      <div class="ai-msg ai-msg-bot">
        <div class="ai-bubble">
          <div class="ai-bubble-label">Veresk ИИ</div>
          <p>${esc(AI_WELCOME)}</p>
          <div class="ai-welcome-cards">
            ${cards
              .map(
                (c) =>
                  `<button type="button" class="ai-welcome-card" data-prompt="${esc(
                    c.prompt
                  )}"><span>${esc(c.label)}</span></button>`
              )
              .join("")}
          </div>
        </div>
      </div>`;
  }

  function aiFormatInline(text) {
    let s = esc(text);
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*]+?)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/`([^`]+?)`/g, "<code class=\"ai-inline-code\">$1</code>");
    return s;
  }

  function aiFormatBubble(text) {
    const raw = String(text || "");
    const parts = [];
    const re = /```(?:текст|text|msg)?\s*\n?([\s\S]*?)```/gi;
    let last = 0;
    let m;
    while ((m = re.exec(raw))) {
      if (m.index > last) {
        parts.push({ type: "text", value: raw.slice(last, m.index) });
      }
      parts.push({ type: "draft", value: m[1].trim() });
      last = m.index + m[0].length;
    }
    if (last < raw.length) parts.push({ type: "text", value: raw.slice(last) });
    if (!parts.length) parts.push({ type: "text", value: raw });

    return parts
      .map((p) => {
        if (p.type === "draft") {
          return (
            `<div class="ai-draft">` +
            `<div class="ai-draft-top"><span>Готовый текст</span>` +
            `<button type="button" class="ai-draft-copy" data-copy="${esc(
              p.value
            )}">Копировать</button></div>` +
            `<pre class="ai-draft-body">${esc(p.value)}</pre></div>`
          );
        }
        const html = aiFormatInline(p.value)
          .replace(/\n\n+/g, "</p><p>")
          .replace(/\n/g, "<br>");
        return `<p>${html}</p>`;
      })
      .join("");
  }

  function renderAiFollowups() {
    const box = $("#aiFollowups");
    if (!box) return;
    const tips = aiChat.suggestions || [];
    if (!tips.length || !aiChat.configured || aiChat.busy) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    box.innerHTML = tips
      .map(
        (t) =>
          `<button type="button" class="ai-chip ai-follow-chip" data-prompt="${esc(
            t
          )}">${esc(t)}</button>`
      )
      .join("");
  }

  function renderAiChips() {
    const box = $("#aiQuickChips");
    if (!box) return;
    const set = AI_CHIP_SETS[aiChat.chipCat] || AI_CHIP_SETS.day;
    box.innerHTML = set
      .map(
        (c) =>
          `<button type="button" class="ai-chip" data-prompt="${esc(
            c.prompt
          )}">${esc(c.label)}</button>`
      )
      .join("");
  }

  function renderAiMessages() {
    const box = $("#aiMessages");
    if (!box) return;
    const regen = $("#aiRegenBtn");
    if (!aiChat.messages.length) {
      box.innerHTML = aiWelcomeHtml();
      if (regen) regen.hidden = true;
      renderAiFollowups();
      return;
    }
    box.innerHTML = aiChat.messages
      .map((m, idx) => {
        if (m.role === "user") {
          const body = esc(m.content).replace(/\n/g, "<br>");
          return `<div class="ai-msg ai-msg-user"><div class="ai-bubble"><p>${body}</p></div></div>`;
        }
        const isLast = idx === aiChat.messages.length - 1;
        return (
          `<div class="ai-msg ai-msg-bot" data-idx="${idx}">` +
          `<div class="ai-bubble"><div class="ai-bubble-label">Veresk ИИ</div>${aiFormatBubble(
            m.content
          )}` +
          `<div class="ai-msg-actions">` +
          `<button type="button" class="ai-act" data-ai-act="copy" data-idx="${idx}">Копировать</button>` +
          (isLast
            ? `<button type="button" class="ai-act" data-ai-act="regen">Ещё раз</button>`
            : "") +
          `</div></div></div>`
        );
      })
      .join("");
    if (aiChat.busy) {
      box.insertAdjacentHTML(
        "beforeend",
        `<div class="ai-msg ai-msg-bot" id="aiTyping"><div class="ai-bubble"><div class="ai-bubble-label">Veresk ИИ</div><p class="ai-typing"><span></span><span></span><span></span></p></div></div>`
      );
    }
    if (regen) {
      regen.hidden = !(
        aiChat.configured &&
        !aiChat.busy &&
        aiChat.messages.some((m) => m.role === "user")
      );
    }
    renderAiFollowups();
    box.scrollTop = box.scrollHeight;
  }

  function persistAiChat() {
    try {
      localStorage.setItem(
        aiStorageKey(),
        JSON.stringify({
          messages: aiChat.messages.slice(-30),
          suggestions: (aiChat.suggestions || []).slice(0, 6),
        })
      );
    } catch (_) {}
  }

  function loadAiChat() {
    try {
      let raw = localStorage.getItem(aiStorageKey());
      if (!raw) raw = localStorage.getItem("veresk_ai_chat_v1");
      if (!raw) return;
      const parsed = JSON.parse(raw);
      const list = Array.isArray(parsed)
        ? parsed
        : Array.isArray(parsed?.messages)
          ? parsed.messages
          : [];
      aiChat.messages = list
        .filter(
          (m) =>
            m &&
            (m.role === "user" || m.role === "assistant") &&
            typeof m.content === "string" &&
            m.content.trim()
        )
        .slice(-30);
      if (Array.isArray(parsed?.suggestions)) {
        aiChat.suggestions = parsed.suggestions.slice(0, 6);
      }
    } catch (_) {
      aiChat.messages = [];
    }
  }

  function setAiChatEnabled(on) {
    aiChat.configured = !!on;
    const input = $("#aiInput");
    const send = $("#aiSend");
    const chips = $("#aiQuickChips");
    const cats = $("#aiChipCats");
    const hint = $("#aiChatHint");
    const stop = $("#aiStopBtn");
    if (input) {
      input.disabled = !on || aiChat.busy;
      input.placeholder = on
        ? "Имя клиента, телефон, «кого поздравить», текст рассылки…"
        : "Сначала подключите ИИ в Настройках…";
    }
    if (send) send.disabled = !on || aiChat.busy;
    if (chips) chips.hidden = !on;
    if (cats) cats.hidden = !on;
    if (stop) stop.hidden = !aiChat.busy;
    if (hint) {
      hint.innerHTML = on
        ? (aiChat.focusCustomerId
            ? `Фокус: клиент #${aiChat.focusCustomerId}${aiChat.focusCustomerName ? " · " + esc(aiChat.focusCustomerName) : ""}. Агент видит полное досье и может запрашивать весь сервис.`
            : "Агент имеет доступ к CRM, ботам, рассылкам и колесу. Готовые тексты — «Копировать».")
        : 'Подключите ИИ в <button type="button" class="linkish" onclick="go(\'settings\')">Настройки → Сервисы</button>.';
    }
  }

  async function refreshAiChatConfig() {
    try {
      const s = await AdminAPI.aiSettings();
      setAiChatEnabled(!!s.configured);
    } catch (_) {
      setAiChatEnabled(false);
    }
  }

  function aiCopyText(text, btn) {
    const t = String(text || "");
    if (!t) return;
    const markDone = () => {
      if (!btn) return;
      const prev = btn.textContent;
      btn.textContent = "Скопировано";
      btn.classList.add("on");
      setTimeout(() => {
        btn.textContent = prev || "Копировать";
        btn.classList.remove("on");
      }, 1400);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(markDone).catch(() => {
        fallbackCopy(t);
        markDone();
      });
    } else {
      fallbackCopy(t);
      markDone();
    }
  }

  function fallbackCopy(t) {
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch (_) {}
    ta.remove();
  }

  async function regenerateAiChat() {
    if (aiChat.busy || !aiChat.configured) return;
    // Удаляем последний ответ ассистента и повторяем последний user
    while (
      aiChat.messages.length &&
      aiChat.messages[aiChat.messages.length - 1].role === "assistant"
    ) {
      aiChat.messages.pop();
    }
    const lastUser = [...aiChat.messages].reverse().find((m) => m.role === "user");
    if (!lastUser) return;
    aiChat.messages.pop();
    await sendAiChat(lastUser.content);
  }

  async function sendAiChat(text) {
    const content = String(text || "").trim();
    if (!content || aiChat.busy || !aiChat.configured) return;
    aiChat.messages.push({ role: "user", content });
    aiChat.suggestions = [];
    aiChat.busy = true;
    setAiChatEnabled(true);
    renderAiMessages();
    persistAiChat();
    const input = $("#aiInput");
    if (input) {
      input.value = "";
      input.style.height = "auto";
    }
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    aiAbort = controller;
    try {
      const history = aiChat.messages.map((m) => ({
        role: m.role,
        content: m.content,
      }));
      const payload = { messages: history };
      if (aiChat.focusCustomerId) {
        payload.customer_id = aiChat.focusCustomerId;
      }
      const res = await AdminAPI.aiChat(
        payload,
        controller ? { signal: controller.signal } : undefined
      );
      const reply = String(res.reply || res.message || "").trim();
      if (!reply) throw new Error("empty");
      aiChat.messages.push({ role: "assistant", content: reply });
      if (Array.isArray(res.suggestions) && res.suggestions.length) {
        aiChat.suggestions = res.suggestions.map(String).slice(0, 6);
      }
    } catch (err) {
      if (err?.name === "AbortError" || err?.message === "aborted") {
        aiChat.messages.push({
          role: "assistant",
          content: "⏹️ Генерацию остановили. Можете задать вопрос заново.",
        });
      } else {
        const detail =
          err?.data?.detail ||
          err?.message ||
          "Не удалось получить ответ. Проверьте настройки ИИ.";
        aiChat.messages.push({
          role: "assistant",
          content: "⚠️ " + detail,
        });
        if (err?.data?.error === "ai_not_configured" || err?.status === 503) {
          setAiChatEnabled(false);
        }
        if (err?.status === 403) {
          setAiChatEnabled(false);
          if ($("#aiChatHint")) {
            $("#aiChatHint").textContent =
              "Нет права «ИИ чат». Попросите администратора открыть доступ.";
          }
        }
      }
    } finally {
      aiAbort = null;
      aiChat.busy = false;
      if (aiChat.configured) setAiChatEnabled(true);
      renderAiMessages();
      persistAiChat();
    }
  }

  function initAiChat() {
    refreshAiChatConfig();
    if (aiChatReady) {
      renderAiChips();
      renderAiMessages();
      return;
    }
    aiChatReady = true;
    loadAiChat();
    renderAiChips();
    renderAiMessages();

    const form = $("#aiChatForm");
    const input = $("#aiInput");
    form?.addEventListener("submit", (e) => {
      e.preventDefault();
      sendAiChat(input?.value || "");
    });
    input?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendAiChat(input.value);
      }
    });
    input?.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 140) + "px";
    });
    $("#aiClearChat")?.addEventListener("click", () => {
      aiChat.messages = [];
      aiChat.suggestions = [];
      aiChat.focusCustomerId = null;
      aiChat.focusCustomerName = "";
      persistAiChat();
      setAiChatEnabled(aiChat.configured);
      renderAiMessages();
    });
    $("#aiRegenBtn")?.addEventListener("click", () => regenerateAiChat());
    $("#aiStopBtn")?.addEventListener("click", () => {
      if (aiAbort) aiAbort.abort();
    });
    $("#aiQuickChips")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-prompt]");
      if (!btn) return;
      sendAiChat(btn.getAttribute("data-prompt") || "");
    });
    $("#aiFollowups")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-prompt]");
      if (!btn) return;
      sendAiChat(btn.getAttribute("data-prompt") || "");
    });
    $("#aiChipCats")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-cat]");
      if (!btn) return;
      aiChat.chipCat = btn.getAttribute("data-cat") || "day";
      $$("#aiChipCats .ai-cat").forEach((b) =>
        b.classList.toggle("on", b === btn)
      );
      renderAiChips();
    });
    $("#aiMessages")?.addEventListener("click", (e) => {
      const card = e.target.closest(".ai-welcome-card[data-prompt]");
      if (card) {
        sendAiChat(card.getAttribute("data-prompt") || "");
        return;
      }
      const draftBtn = e.target.closest(".ai-draft-copy[data-copy]");
      if (draftBtn) {
        aiCopyText(draftBtn.getAttribute("data-copy") || "", draftBtn);
        return;
      }
      const act = e.target.closest("[data-ai-act]");
      if (!act) return;
      const kind = act.getAttribute("data-ai-act");
      if (kind === "regen") {
        regenerateAiChat();
        return;
      }
      if (kind === "copy") {
        const idx = Number(act.getAttribute("data-idx"));
        const msg = aiChat.messages[idx];
        if (msg) aiCopyText(msg.content, act);
      }
    });
  }

  // ── Chats (Telegram + MAX) ────────────────────────────────────────────────

  const TG_ACCOUNT_KEY = "veresk_tg_chat_account";
  const MAX_ACCOUNT_KEY = "veresk_max_chat_account";
  const TG_ONLY_USERS_KEY = "veresk_tg_only_users";
  const CHATS_CHANNEL_KEY = "veresk_chats_channel";
  const TG_MAX_ATTACH = 10;
  const TG_MAX_FILE_MB = 50;
  const tgState = {
    ready: false,
    channel: localStorage.getItem(CHATS_CHANNEL_KEY) === "max" ? "max" : "tg",
    maxConfigured: false,
    maxMode: "none", // userbot | bot | none
    maxLabel: "MAX",
    accounts: [],
    accountId: null,
    dialogs: [],
    peerId: null,
    peer: null,
    clientInfo: null,
    messages: [],
    attachments: [],
    loadingDialogs: false,
    loadingMessages: false,
    sending: false,
    searchTimer: null,
    pollTimer: null,
    pollTick: 0,
    dialogsInflight: null,
    messagesInflight: null,
    maxEs: null,
    lastQuery: "",
    onlyUsers: localStorage.getItem(TG_ONLY_USERS_KEY) !== "0",
    historyHint: "",
    avatarObserver: null,
  };

  function isMaxChannel() {
    return tgState.channel === "max";
  }

  function isMaxUserbot() {
    return isMaxChannel() && tgState.maxMode === "userbot";
  }

  function currentMaxAccountId() {
    const sel = $("#tgAccountSelect");
    if (isMaxUserbot() && sel?.value) return Number(sel.value);
    const v = tgState.accountId || localStorage.getItem(MAX_ACCOUNT_KEY) || "";
    return v ? Number(v) : null;
  }

  function applyChatsChannelUi() {
    const root = $("#chats");
    if (root) {
      root.dataset.channel = tgState.channel;
      root.dataset.maxMode = isMaxChannel() ? tgState.maxMode || "none" : "";
    }
    $$(".ch-chan-btn").forEach((btn) => {
      const on = btn.getAttribute("data-ch-channel") === tgState.channel;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    const emptyHint = $("#tgThreadEmptyHint");
    const maxLabel = $("#maxAccountLabel");
    const input = $("#tgInput");
    const onlyWrap = $("#tgOnlyUsersWrap");
    if (isMaxChannel()) {
      if (isMaxUserbot()) {
        if (emptyHint) emptyHint.textContent = "Выберите чат из списка";
        if (maxLabel) maxLabel.hidden = true;
        if (onlyWrap) onlyWrap.hidden = false;
      } else {
        if (emptyHint) emptyHint.textContent = "Кто писал боту — в списке";
        if (maxLabel) {
          maxLabel.hidden = false;
          maxLabel.textContent = tgState.maxLabel || "MAX-бот";
        }
        if (onlyWrap) onlyWrap.hidden = true;
      }
      if (input) {
        input.placeholder = "Сообщение…";
        input.maxLength = 4000;
      }
    } else {
      if (emptyHint) emptyHint.textContent = "Выберите чат из списка";
      if (maxLabel) maxLabel.hidden = true;
      if (onlyWrap) onlyWrap.hidden = false;
      if (input) {
        input.placeholder = "Сообщение…";
        input.maxLength = 4096;
      }
    }
  }

  async function setChatsChannel(channel) {
    const next = channel === "max" ? "max" : "tg";
    if (tgState.channel === next) return;
    tgState.channel = next;
    localStorage.setItem(CHATS_CHANNEL_KEY, next);
    tgState.peerId = null;
    tgState.peer = null;
    tgState.messages = [];
    tgState.dialogs = [];
    tgState.historyHint = "";
    clearTgAttachments();
    resetTgClientUi();
    showTgThread(false);
    stopMaxSSE();
    applyChatsChannelUi();
    await loadChats();
  }

  function stopTgPoll() {
    if (tgState.pollTimer) {
      clearInterval(tgState.pollTimer);
      tgState.pollTimer = null;
    }
  }

  function stopMaxSSE() {
    if (tgState.maxEs) {
      try {
        tgState.maxEs.close();
      } catch (_) {
        /* ignore */
      }
      tgState.maxEs = null;
    }
  }

  function applyMaxSseEvent(event) {
    if (!event || !isMaxChannel()) return;
    const peerId = event.peer_id;
    if (event.dialog && peerId) {
      const idx = tgState.dialogs.findIndex((d) => String(d.peer_id) === String(peerId));
      const merged = {
        ...(idx >= 0 ? tgState.dialogs[idx] : {}),
        ...event.dialog,
        peer_id: peerId,
      };
      if (idx >= 0) tgState.dialogs.splice(idx, 1);
      tgState.dialogs.unshift(merged);
      renderTgDialogs();
    }
    if (event.message && peerId) {
      if (String(tgState.peerId) !== String(peerId)) return;
      const msg = event.message;
      const realId = String(msg.id || "");
      if (realId && !realId.startsWith("tmp:")) {
        const exists = tgState.messages.some((m) => String(m.id) === realId);
        if (exists) return;
      }
      const tmpIdx = tgState.messages.findIndex(
        (m) => m._pending && m.out && m.text === msg.text
      );
      if (tmpIdx >= 0) {
        tgState.messages[tmpIdx] = msg;
      } else {
        tgState.messages = [...tgState.messages, msg];
      }
      tgState.historyHint = "";
      renderTgMessages({ stickBottom: true });
    }
  }

  function startMaxSSE() {
    stopMaxSSE();
    // Realtime SSE только для бот-инбокса; личный номер — через poll
    if (!isMaxChannel() || isMaxUserbot() || !tgState.maxConfigured || !AdminAPI.getToken())
      return;
    try {
      tgState.maxEs = AdminAPI.maxChatEvents(applyMaxSseEvent);
    } catch (err) {
      console.warn("MAX SSE unavailable", err);
    }
  }

  function startTgPoll() {
    stopTgPoll();
    tgState.pollTick = 0;
    const interval = isMaxChannel() && !isMaxUserbot() ? 20000 : 10000;
    tgState.pollTimer = setInterval(() => {
      if (!$("#chats")?.classList.contains("active")) return;
      tgState.pollTick = (tgState.pollTick || 0) + 1;
      const tick = tgState.pollTick;
      if (isMaxChannel()) {
        if (!tgState.maxConfigured) return;
        // Список диалогов — реже; открытый чат — чаще
        if (tick % 2 === 0) refreshTgDialogs({ silent: true });
        if (isMaxUserbot() && tgState.peerId) {
          openTgPeer(tgState.peerId, { silent: true, keepScroll: true });
        }
        return;
      }
      if (!tgState.accountId) return;
      if (tick % 2 === 0) refreshTgDialogs({ silent: true });
      if (tgState.peerId) openTgPeer(tgState.peerId, { silent: true, keepScroll: true });
    }, interval);
  }

  function tgInitials(title) {
    const raw = String(title || "?").trim();
    const maxId = raw.match(/^MAX\s+(\d+)$/i);
    if (maxId) return maxId[1].slice(-2);
    const parts = raw.split(/\s+/).filter(Boolean);
    if (!parts.length) return "?";
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }

  function tgAvatarInner(title, avatarUrl, fallbackUrl) {
    const initials = `<span class="tg-av-fallback">${esc(tgInitials(title))}</span>`;
    const url = avatarUrl || fallbackUrl || "";
    if (!url) return initials;
    return `<img class="tg-av-img" src="${esc(url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.classList.add('broken')">${initials}`;
  }

  /** Инициалы сразу; аватар подгружаем только для видимых строк (не N+1 под лок Telethon). */
  function tgAvatarLazyShell(title, avatarUrl) {
    const initials = `<span class="tg-av-fallback">${esc(tgInitials(title))}</span>`;
    const url = (avatarUrl || "").trim();
    if (!url) return initials;
    return `<span class="tg-av-lazy" data-avatar-src="${esc(url)}">${initials}</span>`;
  }

  function ensureTgAvatarObserver() {
    if (tgState.avatarObserver || typeof IntersectionObserver === "undefined") return;
    tgState.avatarObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const host = entry.target;
          tgState.avatarObserver.unobserve(host);
          const src = host.getAttribute("data-avatar-src");
          if (!src || host.querySelector("img.tg-av-img")) continue;
          const img = document.createElement("img");
          img.className = "tg-av-img";
          img.alt = "";
          img.loading = "lazy";
          img.decoding = "async";
          img.referrerPolicy = "no-referrer";
          img.onerror = () => img.classList.add("broken");
          img.src = src;
          host.prepend(img);
        }
      },
      { root: $("#tgDialogList"), rootMargin: "80px 0px", threshold: 0.01 }
    );
  }

  function observeTgDialogAvatars() {
    ensureTgAvatarObserver();
    if (!tgState.avatarObserver) {
      // Fallback без IO: подгружаем видимые сразу
      $$("#tgDialogList .tg-av-lazy[data-avatar-src]").forEach((host) => {
        const src = host.getAttribute("data-avatar-src");
        if (!src || host.querySelector("img")) return;
        const img = document.createElement("img");
        img.className = "tg-av-img";
        img.alt = "";
        img.loading = "lazy";
        img.decoding = "async";
        img.referrerPolicy = "no-referrer";
        img.onerror = () => img.classList.add("broken");
        img.src = src;
        host.prepend(img);
      });
      return;
    }
    $$("#tgDialogList .tg-av-lazy[data-avatar-src]").forEach((el) => {
      tgState.avatarObserver.observe(el);
    });
  }

  function tgTimeLabel(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const now = new Date();
    const sameDay =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate();
    if (sameDay) {
      return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    }
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    if (
      d.getFullYear() === yesterday.getFullYear() &&
      d.getMonth() === yesterday.getMonth() &&
      d.getDate() === yesterday.getDate()
    ) {
      return "вчера";
    }
    return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  }

  function tgMsgTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  function currentTgAccountId() {
    const sel = $("#tgAccountSelect");
    const v = sel?.value || tgState.accountId || localStorage.getItem(TG_ACCOUNT_KEY) || "";
    return v ? Number(v) : null;
  }

  function renderTgAccounts() {
    const sel = $("#tgAccountSelect");
    if (!sel) return;
    const items = tgState.accounts || [];
    const storageKey = isMaxUserbot() ? MAX_ACCOUNT_KEY : TG_ACCOUNT_KEY;
    if (!items.length) {
      sel.innerHTML = `<option value="">Нет аккаунтов</option>`;
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    const preferred = String(
      tgState.accountId || localStorage.getItem(storageKey) || items[0].id
    );
    sel.innerHTML = items
      .map((a) => {
        const label = esc(a.label || a.phone_masked || a.phone || `Аккаунт ${a.id}`);
        const status = a.status && a.status !== "ready" ? ` · ${esc(a.status)}` : "";
        return `<option value="${a.id}">${label}${status}</option>`;
      })
      .join("");
    if ([...sel.options].some((o) => o.value === preferred)) sel.value = preferred;
    tgState.accountId = Number(sel.value);
    localStorage.setItem(storageKey, String(tgState.accountId));
  }

  function filteredTgDialogs() {
    const items = tgState.dialogs || [];
    if ((!isMaxChannel() || isMaxUserbot()) && tgState.onlyUsers) {
      return items.filter((d) => (d.kind || "user") === "user");
    }
    return items;
  }

  function renderTgDialogs() {
    const box = $("#tgDialogList");
    if (!box) return;
    const items = filteredTgDialogs();
    const maxMode = isMaxChannel();
    const maxUserbot = isMaxUserbot();

    if (maxMode && !maxUserbot) {
      if (!tgState.maxConfigured) {
        box.innerHTML = `<div class="tg-empty"><div class="t">MAX не подключён</div>Подключите личный номер или токен бота в Настройках → MAX.<div style="margin-top:12px"><button class="btn primary" type="button" onclick="go('settings')">Настройки</button></div></div>`;
        return;
      }
      if (tgState.loadingDialogs && !items.length) {
        box.innerHTML = `<div class="tg-empty">Загружаем чаты…</div>`;
        return;
      }
      if (!items.length) {
        box.innerHTML = `<div class="tg-empty"><div class="t">${
          tgState.onlyUsers ? "Нет чатов клиентов" : "Диалогов пока нет"
        }</div>${
          tgState.onlyUsers
            ? "Снимите фильтр «Клиенты», чтобы увидеть все диалоги."
            : "Они появятся, когда клиент напишет боту или заполнит анкету в MAX."
        }</div>`;
        return;
      }
    } else if (maxMode && maxUserbot) {
      if (!tgState.accountId) {
        box.innerHTML = `<div class="tg-empty"><div class="t">Нет аккаунта</div>Подключите номер MAX в Настройках.</div>`;
        return;
      }
      if (tgState.loadingDialogs && !items.length) {
        box.innerHTML = `<div class="tg-empty">Загружаем чаты…</div>`;
        return;
      }
      if (!items.length) {
        box.innerHTML = `<div class="tg-empty"><div class="t">${
          tgState.onlyUsers ? "Нет чатов клиентов" : "Чатов пока нет"
        }</div>${
          tgState.onlyUsers
            ? "Снимите фильтр «Клиенты», чтобы увидеть все диалоги аккаунта."
            : "Нажмите «Новый чат», чтобы написать клиенту."
        }</div>`;
        return;
      }
    } else {
      if (!tgState.accountId) {
        box.innerHTML = `<div class="tg-empty"><div class="t">Нет аккаунта</div>Подключите Telegram в Настройках.</div>`;
        return;
      }
      if (tgState.loadingDialogs && !items.length && !(tgState.dialogs || []).length) {
        box.innerHTML = `<div class="tg-empty">Загружаем чаты…</div>`;
        return;
      }
      if (!items.length) {
        box.innerHTML = `<div class="tg-empty"><div class="t">${
          tgState.onlyUsers ? "Нет чатов клиентов" : "Чатов пока нет"
        }</div>${
          tgState.onlyUsers
            ? "Снимите фильтр «Клиенты», чтобы увидеть все диалоги аккаунта."
            : "Нажмите «Новый чат», чтобы написать клиенту."
        }</div>`;
        return;
      }
    }

    const accountId = tgState.accountId;
    box.innerHTML = items
      .map((d) => {
        const kind = d.kind || "user";
        const peerKey = d.peer_id != null ? d.peer_id : d.id;
        const active = String(peerKey) === String(tgState.peerId) ? " active" : "";
        const preview = d.last_message
          ? (d.last_out ? `<span class="you">Вы: </span>` : "") + esc(d.last_message)
          : "Нет сообщений";
        const unread =
          d.unread > 0
            ? `<span class="tg-unread">${d.unread > 99 ? "99+" : d.unread}</span>`
            : "";
        // Не ставим src аватара сразу на все строки — иначе N запросов /avatar
        // сериализуются под одним Telethon-локом и подвешивают чаты.
        const avatarUrl =
          d.avatar_url ||
          (!maxMode && accountId && d.peer_id != null
            ? AdminAPI.chatAvatarUrl(d.peer_id, accountId)
            : "");
        const avInner = tgAvatarLazyShell(d.title, avatarUrl);
        return `
          <button type="button" class="tg-dialog${active}" data-peer="${esc(peerKey)}">
            <span class="tg-dialog-av ${esc(maxMode && !maxUserbot ? "user" : kind)}">
              ${avInner}
            </span>
            <span class="tg-dialog-main">
              <span class="tg-dialog-top">
                <span class="tg-dialog-title">${esc(d.title || "Без имени")}</span>
                <span class="tg-dialog-time">${esc(tgTimeLabel(d.date))}</span>
              </span>
              <span class="tg-dialog-bottom">
                <span class="tg-dialog-preview">${preview}</span>
                ${unread}
              </span>
            </span>
          </button>`;
      })
      .join("");
    observeTgDialogAvatars();
  }

  function tgMediaBlock(m) {
    if (!m?.has_media || !tgState.peerId) return "";
    const kind = m.media_kind || "";
    if (["geo", "contact", "poll"].includes(kind)) {
      return `<div class="tg-media-fallback">${esc(m.preview || "Медиа")}</div>`;
    }
    let url = m.media_url || "";
    if (!url) {
      if (isMaxChannel()) {
        url = AdminAPI.maxChatMediaUrl(
          tgState.peerId,
          m.id,
          isMaxUserbot() ? tgState.accountId : null
        );
      } else if (tgState.accountId) {
        url = AdminAPI.chatMediaUrl(tgState.peerId, m.id, tgState.accountId);
      }
    }
    if (!url) {
      return `<div class="tg-media-fallback">${esc(m.preview || "Медиа")}</div>`;
    }
    if (kind === "photo" || kind === "sticker" || kind === "animation" || kind === "webpage") {
      const cls = kind === "sticker" ? "tg-media-img sticker" : "tg-media-img";
      return `<a class="tg-media" href="${esc(url)}" target="_blank" rel="noopener"><img class="${cls}" src="${esc(url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.closest('.tg-media').classList.add('failed')"></a>`;
    }
    if (kind === "video" || kind === "video_note") {
      const round = kind === "video_note" ? " round" : "";
      return `<video class="tg-media-video${round}" src="${esc(url)}" controls playsinline preload="metadata"></video>`;
    }
    if (kind === "voice" || kind === "audio") {
      return `<audio class="tg-media-audio" src="${esc(url)}" controls preload="metadata"></audio>`;
    }
    const label = m.file_name || m.preview || "Файл";
    return `<a class="tg-media-file" href="${esc(url)}" target="_blank" rel="noopener">📎 ${esc(label)}</a>`;
  }

  function renderTgMessages({ stickBottom = true, keepScroll = false } = {}) {
    const box = $("#tgMessages");
    if (!box) return;
    const prevHeight = box.scrollHeight;
    const prevTop = box.scrollTop;
    const nearBottom = prevHeight - prevTop - box.clientHeight < 80;
    const msgs = tgState.messages || [];
    const olderBtn =
      !isMaxChannel() && msgs.length >= 40
        ? `<button type="button" class="btn tg-load-more" id="tgLoadOlder">Раньше</button>`
        : "";
    const hint =
      isMaxChannel() && tgState.historyHint
        ? `<div class="tg-empty" style="padding:16px 12px"><div class="t">История пока недоступна</div>${esc(
            tgState.historyHint
          )}</div>`
        : "";
    box.innerHTML =
      olderBtn +
      hint +
      msgs
        .map((m) => {
          const side = m.out ? "out" : "in";
          const flags = `${m._pending ? " pending" : ""}${m._failed ? " failed" : ""}`;
          const raw = (m.text || "").replace(/\n{3,}/g, "\n\n").trim();
          const media = tgMediaBlock(m);
          const text = raw
            ? `<div class="tg-bubble-text">${esc(raw)}</div>`
            : media
              ? ""
              : `<div class="tg-bubble-text"><span class="tg-bubble-media">${esc(m.preview || "Медиа")}</span></div>`;
          const meta = m._failed
            ? "не отправлено"
            : m._pending
              ? "…"
              : esc(tgMsgTime(m.date));
          return `<div class="tg-msg ${side}${flags}" data-id="${esc(
            m.id
          )}"><div class="tg-bubble">${media}${text}<div class="tg-bubble-meta">${meta}</div></div></div>`;
        })
        .join("");
    if (keepScroll) {
      if (nearBottom) box.scrollTop = box.scrollHeight;
      else box.scrollTop = box.scrollHeight - prevHeight + prevTop;
    } else if (stickBottom) {
      box.scrollTop = box.scrollHeight;
    }
    $("#tgLoadOlder")?.addEventListener("click", () => loadOlderTgMessages());
  }

  function showTgThread(show) {
    const empty = $("#tgThreadEmpty");
    const active = $("#tgThreadActive");
    const shell = $("#tgShell");
    if (empty) empty.hidden = !!show;
    if (active) active.hidden = !show;
    if (show) {
      shell?.classList.add("thread-open");
      document.body.classList.add("tg-thread-open");
      if (
        window.matchMedia("(max-width: 899px)").matches &&
        !history.state?.tgThread
      ) {
        history.pushState({ tgThread: true }, "");
      }
    } else {
      shell?.classList.remove("thread-open");
      document.body.classList.remove("tg-thread-open");
    }
  }

  function setTgPeerHeader(peer) {
    tgState.peer = peer || null;
    $("#tgPeerName").textContent = peer?.title || "Чат";
    const bits = [];
    if (peer?.username) bits.push("@" + String(peer.username).replace(/^@/, ""));
    if (peer?.phone) bits.push("+" + String(peer.phone).replace(/^\+/, ""));
    if (peer?.max_user_id && isMaxChannel()) bits.push("id " + peer.max_user_id);
    if (peer?.kind && peer.kind !== "user") bits.push(peer.kind);
    $("#tgPeerSub").textContent =
      bits.join(" · ") || (isMaxChannel() ? "MAX" : "Telegram");
    const av = $("#tgPeerAv");
    if (av) {
      const kind = peer?.kind || "user";
      av.className = "tg-peer-av " + kind;
      const peerId = peer?.peer_id || peer?.id || tgState.peerId;
      const tgFallback =
        !isMaxChannel() && peerId && tgState.accountId
          ? AdminAPI.chatAvatarUrl(peerId, tgState.accountId)
          : "";
      av.innerHTML = tgAvatarInner(peer?.title, peer?.avatar_url, tgFallback);
    }
  }

  function mergePeerIntoDialogs(peer) {
    if (!peer) return;
    const peerKey = peer.peer_id != null ? peer.peer_id : peer.id;
    if (peerKey == null) return;
    tgState.dialogs = (tgState.dialogs || []).map((d) => {
      if (String(d.peer_id) !== String(peerKey) && String(d.id) !== String(peerKey)) {
        return d;
      }
      return {
        ...d,
        title: peer.title || d.title,
        phone: peer.phone || d.phone,
        username: peer.username || d.username,
        avatar_url: peer.avatar_url || d.avatar_url,
        max_user_id: peer.max_user_id != null ? peer.max_user_id : d.max_user_id,
        unread: 0,
      };
    });
  }

  function resetTgClientUi() {
    tgState.clientInfo = null;
    const chip = $("#tgClientChip");
    const createBtn = $("#tgCreateClientBtn");
    const openBtn = $("#tgOpenClientBtn");
    if (chip) {
      chip.hidden = true;
      chip.textContent = "";
      chip.className = "tg-client-chip";
    }
    if (createBtn) {
      createBtn.hidden = true;
      createBtn.disabled = false;
      createBtn.textContent = "Создать";
    }
    if (openBtn) {
      openBtn.hidden = true;
      openBtn.dataset.clientId = "";
    }
  }

  function renderTgClientStatus(info) {
    tgState.clientInfo = info || null;
    const chip = $("#tgClientChip");
    const createBtn = $("#tgCreateClientBtn");
    const openBtn = $("#tgOpenClientBtn");
    if (!chip || !createBtn || !openBtn) return;

    if (!info) {
      resetTgClientUi();
      return;
    }

    const status = info.status || "";
    const inBase = status === "in_base" && !!info.in_base && !!info.customer?.id;
    const customerId = inBase ? info.customer.id : null;

    chip.hidden = false;
    chip.textContent = info.label || "";
    chip.title = info.hint || "";
    chip.className =
      "tg-client-chip " +
      (inBase ? "ok" : status === "missing" ? "warn" : "muted");

    createBtn.hidden = !info.can_create;
    createBtn.disabled = false;
    createBtn.textContent = "Создать";

    // Не показываем «Карточка», если клиента нет в базе
    openBtn.hidden = !customerId;
    openBtn.dataset.clientId = customerId ? String(customerId) : "";
    openBtn.textContent = "Карточка";
  }

  async function refreshTgClientStatus({ silent = false } = {}) {
    const peerId = tgState.peerId;
    if (peerId == null) {
      resetTgClientUi();
      return;
    }
    if (isMaxChannel()) {
      try {
        const accountId = isMaxUserbot() ? currentMaxAccountId() : null;
        const data = await AdminAPI.maxChatClientStatus(peerId, accountId);
        if (String(tgState.peerId) !== String(peerId)) return;
        if (data.peer) {
          const cur = tgState.peer || {};
          setTgPeerHeader({ ...cur, ...data.peer });
        }
        renderTgClientStatus(data);
      } catch (err) {
        if (!silent) {
          resetTgClientUi();
          const chip = $("#tgClientChip");
          if (chip) {
            chip.hidden = false;
            chip.className = "tg-client-chip muted";
            chip.textContent = "Не удалось проверить клиента";
            chip.title = err.data?.error || err.message || "";
          }
        }
      }
      return;
    }
    const accountId = currentTgAccountId();
    if (!accountId) {
      resetTgClientUi();
      return;
    }
    try {
      const data = await AdminAPI.chatClientStatus(peerId, accountId);
      if (String(tgState.peerId) !== String(peerId)) return;
      if (data.peer) {
        const cur = tgState.peer || {};
        setTgPeerHeader({ ...cur, ...data.peer });
      }
      renderTgClientStatus(data);
    } catch (err) {
      if (!silent) {
        resetTgClientUi();
        const chip = $("#tgClientChip");
        if (chip) {
          chip.hidden = false;
          chip.className = "tg-client-chip muted";
          chip.textContent = "Не удалось проверить клиента";
          chip.title = err.data?.error || err.message || "";
        }
      }
    }
  }

  function openTgCreateClientModal(show) {
    const modal = $("#tgCreateClientModal");
    if (!modal) return;
    modal.hidden = !show;
    if (!show) return;
    const peer = tgState.peer || tgState.clientInfo?.peer || {};
    const info = tgState.clientInfo || {};
    const nameEl = $("#tgCreateClientName");
    const phoneEl = $("#tgCreateClientPhone");
    if (nameEl) {
      nameEl.value =
        peer.title ||
        [peer.first_name, peer.last_name].filter(Boolean).join(" ") ||
        "";
    }
    if (phoneEl) {
      const phone = peer.phone || info.peer?.phone || "";
      phoneEl.value = phone ? (String(phone).startsWith("+") ? phone : "+" + phone) : "";
      phoneEl.focus();
    }
  }

  async function createTgClientFromChat({ phone, name } = {}) {
    const peerId = tgState.peerId;
    if (peerId == null) return;
    const maxChannel = isMaxChannel();
    const accountId = maxChannel
      ? isMaxUserbot()
        ? currentMaxAccountId()
        : null
      : currentTgAccountId();
    if (!maxChannel && !accountId) return;
    const createBtn = $("#tgCreateClientBtn");
    const submitBtn = $("#tgCreateClientSubmit");
    if (createBtn) {
      createBtn.disabled = true;
      createBtn.textContent = "Создаём…";
    }
    if (submitBtn) submitBtn.disabled = true;
    try {
      const body = {};
      if (accountId) body.account_id = accountId;
      if (phone) body.phone = phone;
      if (name) body.name = name;
      const data = maxChannel
        ? await AdminAPI.maxChatClientCreate(peerId, body)
        : await AdminAPI.chatClientCreate(peerId, body);
      openTgCreateClientModal(false);
      if (data.peer) setTgPeerHeader({ ...(tgState.peer || {}), ...data.peer });
      renderTgClientStatus({
        status: "in_base",
        label: data.label || "Клиент уже в базе",
        hint: data.hint || (data.created ? "Сохранён в базе и в Posiflora" : ""),
        can_create: false,
        need_phone: false,
        in_base: true,
        customer: data.customer,
        peer: data.peer,
      });
      alert(
        data.created
          ? "Клиент создан в базе и в Posiflora"
          : "Этот человек уже есть в базе клиентов"
      );
    } catch (err) {
      if (err.data?.need_phone || err.data?.error === "phone_required") {
        openTgCreateClientModal(true);
        if (err.data?.peer) {
          setTgPeerHeader({ ...(tgState.peer || {}), ...err.data.peer });
        }
        alert(err.data?.message || "Укажите номер телефона клиента");
      } else {
        alert(
          "Не удалось создать клиента: " +
            (err.data?.message || err.data?.error || err.message)
        );
      }
      if (createBtn) {
        createBtn.disabled = false;
        createBtn.textContent = "Создать";
      }
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  }

  async function onTgCreateClientClick() {
    const info = tgState.clientInfo;
    if (!info?.can_create) return;
    if (info.need_phone || !(tgState.peer?.phone || info.peer?.phone)) {
      openTgCreateClientModal(true);
      return;
    }
    await createTgClientFromChat();
  }

  async function submitTgCreateClient(e) {
    e.preventDefault();
    const phone = ($("#tgCreateClientPhone")?.value || "").trim();
    const name = ($("#tgCreateClientName")?.value || "").trim();
    if (!phone) {
      alert("Укажите телефон");
      return;
    }
    await createTgClientFromChat({ phone, name });
  }

  async function refreshTgDialogs({ silent = false } = {}) {
    if (tgState.dialogsInflight) {
      if (silent) return tgState.dialogsInflight;
      try {
        await tgState.dialogsInflight;
      } catch (_) {
        /* previous silent may fail quietly */
      }
    }
    const run = _refreshTgDialogsInner({ silent });
    tgState.dialogsInflight = run.finally(() => {
      if (tgState.dialogsInflight === run) tgState.dialogsInflight = null;
    });
    return tgState.dialogsInflight;
  }

  async function _refreshTgDialogsInner({ silent = false } = {}) {
    if (isMaxChannel()) {
      if (!tgState.maxConfigured) {
        tgState.dialogs = [];
        renderTgDialogs();
        return;
      }
      if (isMaxUserbot()) {
        const accountId = currentMaxAccountId();
        if (!accountId) {
          tgState.dialogs = [];
          renderTgDialogs();
          return;
        }
        if (!silent) tgState.loadingDialogs = true;
        if (!silent) renderTgDialogs();
        try {
          const params = { account_id: accountId, limit: 60 };
          if (tgState.lastQuery) params.q = tgState.lastQuery;
          if (tgState.onlyUsers) params.clients_only = "1";
          const data = await AdminAPI.maxChatDialogs(params);
          tgState.maxMode = data.mode || "userbot";
          tgState.dialogs = data.items || [];
          tgState.accountId = data.account_id || accountId;
          localStorage.setItem(MAX_ACCOUNT_KEY, String(tgState.accountId));
          applyChatsChannelUi();
        } catch (err) {
          if (!silent) {
            const msg =
              err.data?.message || err.data?.error || err.message || "Не удалось загрузить чаты";
            $("#tgDialogList").innerHTML = `<div class="tg-empty"><div class="t">Ошибка</div>${esc(
              msg
            )}${
              err.data?.error === "max_not_configured" || err.data?.error === "no_telegram_accounts"
                ? `<div style="margin-top:12px"><button class="btn primary" type="button" onclick="go('settings')">Открыть настройки</button></div>`
                : ""
            }</div>`;
            return;
          }
        } finally {
          tgState.loadingDialogs = false;
        }
        renderTgDialogs();
        return;
      }

      if (!silent) tgState.loadingDialogs = true;
      if (!silent) renderTgDialogs();
      try {
        const params = { limit: 60 };
        if (tgState.lastQuery) params.q = tgState.lastQuery;
        if (tgState.onlyUsers) params.clients_only = "1";
        const data = await AdminAPI.maxChatDialogs(params);
        tgState.maxConfigured = data.configured !== false;
        tgState.maxMode = data.mode || "bot";
        tgState.dialogs = data.items || [];
        applyChatsChannelUi();
      } catch (err) {
        if (!silent) {
          const msg =
            err.data?.message || err.data?.error || err.message || "Не удалось загрузить чаты";
          $("#tgDialogList").innerHTML = `<div class="tg-empty"><div class="t">Ошибка</div>${esc(
            msg
          )}${
            err.data?.error === "max_not_configured"
              ? `<div style="margin-top:12px"><button class="btn primary" type="button" onclick="go('settings')">Открыть настройки</button></div>`
              : ""
          }</div>`;
          return;
        }
      } finally {
        tgState.loadingDialogs = false;
      }
      renderTgDialogs();
      return;
    }

    const accountId = currentTgAccountId();
    if (!accountId) {
      tgState.dialogs = [];
      renderTgDialogs();
      return;
    }
    if (!silent) tgState.loadingDialogs = true;
    if (!silent) renderTgDialogs();
    try {
      const params = { account_id: accountId, limit: 60 };
      if (tgState.lastQuery) params.q = tgState.lastQuery;
      if (tgState.onlyUsers) params.clients_only = "1";
      const data = await AdminAPI.chatDialogs(params);
      tgState.dialogs = data.items || [];
      tgState.accountId = data.account_id || accountId;
    } catch (err) {
      if (!silent) {
        const msg =
          err.data?.message || err.data?.error || err.message || "Не удалось загрузить чаты";
        $("#tgDialogList").innerHTML = `<div class="tg-empty"><div class="t">Ошибка</div>${esc(
          msg
        )}${
          err.data?.error === "no_telegram_accounts"
            ? `<div style="margin-top:12px"><button class="btn primary" type="button" onclick="go('settings')">Открыть настройки</button></div>`
            : ""
        }</div>`;
        return;
      }
    } finally {
      tgState.loadingDialogs = false;
    }
    renderTgDialogs();
  }

  function tgMessagesFingerprint(messages) {
    if (!messages || !messages.length) return "";
    const last = messages[messages.length - 1];
    return `${messages.length}:${last?.id || ""}:${last?.date || ""}`;
  }

  async function openTgPeer(peerId, { silent = false, keepScroll = false } = {}) {
    if (peerId == null || peerId === "") return;
    if (silent && tgState.messagesInflight) return tgState.messagesInflight;
    const run = _openTgPeerInner(peerId, { silent, keepScroll });
    if (silent) {
      tgState.messagesInflight = run.finally(() => {
        if (tgState.messagesInflight === run) tgState.messagesInflight = null;
      });
      return tgState.messagesInflight;
    }
    return run;
  }

  async function _openTgPeerInner(peerId, { silent = false, keepScroll = false } = {}) {
    if (isMaxChannel()) {
      const switched = String(tgState.peerId) !== String(peerId);
      tgState.peerId = peerId;
      if (!silent) {
        if (switched) {
          clearTgAttachments();
          resetTgClientUi();
          tgState.historyHint = "";
        }
        tgState.loadingMessages = true;
        showTgThread(true);
        renderTgDialogs();
      }
      try {
        const params = { limit: 50 };
        if (isMaxUserbot()) {
          const accountId = currentMaxAccountId();
          if (accountId) params.account_id = accountId;
        }
        const data = await AdminAPI.maxChatMessages(peerId, params);
        if (data.peer?.peer_id && String(data.peer.peer_id) !== String(peerId)) {
          tgState.peerId = data.peer.peer_id;
        }
        if (!silent || data.peer?.title) setTgPeerHeader(data.peer);
        const next = data.messages || [];
        const changed = tgMessagesFingerprint(next) !== tgMessagesFingerprint(tgState.messages);
        if (changed || !silent) {
          tgState.messages = next;
          tgState.historyHint = data.history_unavailable
            ? data.hint || "История появится после следующего сообщения клиента"
            : "";
          renderTgMessages({ stickBottom: !keepScroll, keepScroll });
        }
        mergePeerIntoDialogs(data.peer);
        if (!silent) renderTgDialogs();
        showTgThread(true);
        if (!silent) focusTgComposer();
        if (!silent) refreshTgClientStatus({ silent: true });
      } catch (err) {
        if (!silent) {
          alert("Не удалось открыть чат: " + (err.data?.error || err.message));
        }
      } finally {
        tgState.loadingMessages = false;
      }
      return;
    }

    const accountId = currentTgAccountId();
    if (!accountId) {
      if (!silent) {
        alert("Выберите Telegram-аккаунт сверху, затем откройте чат.");
      }
      return;
    }
    const switched = String(tgState.peerId) !== String(peerId);
    tgState.peerId = peerId;
    if (!silent) {
      if (switched) {
        clearTgAttachments();
        resetTgClientUi();
      }
      tgState.loadingMessages = true;
      showTgThread(true);
      renderTgDialogs();
    }
    try {
      const data = await AdminAPI.chatMessages(peerId, {
        account_id: accountId,
        limit: silent ? 40 : 50,
        mark_read: silent ? "0" : "1",
        enrich_peer: silent ? "0" : "1",
      });
      if (!silent && data.peer) setTgPeerHeader(data.peer);
      const next = data.messages || [];
      const changed = tgMessagesFingerprint(next) !== tgMessagesFingerprint(tgState.messages);
      if (changed || !silent) {
        tgState.messages = next;
        tgState.historyHint = "";
        renderTgMessages({ stickBottom: !keepScroll, keepScroll });
      }
      if (!silent) {
        mergePeerIntoDialogs(data.peer);
        renderTgDialogs();
        showTgThread(true);
        focusTgComposer();
        refreshTgClientStatus({ silent: true });
      }
    } catch (err) {
      if (!silent) {
        alert("Не удалось открыть чат: " + (err.data?.error || err.message));
      }
    } finally {
      tgState.loadingMessages = false;
    }
  }

  function focusTgComposer() {
    /* Autofocus on phones opens the keyboard and jumps the fixed thread layout */
    if (window.matchMedia("(max-width: 899px)").matches) return;
    $("#tgInput")?.focus();
  }

  async function loadOlderTgMessages() {
    const accountId = currentTgAccountId();
    if (!accountId || !tgState.peerId || !tgState.messages.length) return;
    const oldest = tgState.messages[0]?.id;
    if (!oldest) return;
    const box = $("#tgMessages");
    const prevHeight = box?.scrollHeight || 0;
    try {
      const data = await AdminAPI.chatMessages(tgState.peerId, {
        account_id: accountId,
        limit: 40,
        offset_id: oldest,
        mark_read: "0",
      });
      const older = data.messages || [];
      if (!older.length) return;
      const seen = new Set(tgState.messages.map((m) => m.id));
      const merged = [...older.filter((m) => !seen.has(m.id)), ...tgState.messages];
      tgState.messages = merged;
      renderTgMessages({ stickBottom: false, keepScroll: false });
      if (box) box.scrollTop = box.scrollHeight - prevHeight;
    } catch (err) {
      alert("Ошибка: " + (err.data?.error || err.message));
    }
  }

  function clearTgAttachments() {
    for (const a of tgState.attachments) {
      if (a.url) URL.revokeObjectURL(a.url);
    }
    tgState.attachments = [];
    const asDoc = $("#tgAsDocument");
    if (asDoc) asDoc.checked = false;
    renderTgAttachments();
  }

  function tgFileKind(file) {
    const t = (file.type || "").toLowerCase();
    if (t.startsWith("image/")) return "image";
    if (t.startsWith("video/")) return "video";
    if (t.startsWith("audio/")) return "audio";
    return "file";
  }

  function addTgAttachments(fileList) {
    const incoming = [...(fileList || [])].filter(Boolean);
    if (!incoming.length) return;
    const room = TG_MAX_ATTACH - tgState.attachments.length;
    if (room <= 0) {
      alert(`Максимум ${TG_MAX_ATTACH} файлов`);
      return;
    }
    const take = incoming.slice(0, room);
    for (const file of take) {
      if (file.size > TG_MAX_FILE_MB * 1024 * 1024) {
        alert(`«${file.name}» больше ${TG_MAX_FILE_MB} МБ`);
        continue;
      }
      const kind = tgFileKind(file);
      tgState.attachments.push({
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        file,
        kind,
        url: kind === "image" || kind === "video" ? URL.createObjectURL(file) : "",
      });
    }
    if (incoming.length > take.length) {
      alert(`Добавлены не все файлы — лимит ${TG_MAX_ATTACH}`);
    }
    renderTgAttachments();
  }

  function removeTgAttachment(id) {
    const idx = tgState.attachments.findIndex((a) => a.id === id);
    if (idx < 0) return;
    const [a] = tgState.attachments.splice(idx, 1);
    if (a?.url) URL.revokeObjectURL(a.url);
    renderTgAttachments();
  }

  function renderTgAttachments() {
    const bar = $("#tgAttachBar");
    const list = $("#tgAttachList");
    if (!bar || !list) return;
    const items = tgState.attachments;
    bar.hidden = items.length === 0;
    list.innerHTML = items
      .map((a) => {
        let thumb = `<span class="tg-attach-ico">📎</span>`;
        if (a.kind === "image" && a.url) {
          thumb = `<img src="${esc(a.url)}" alt="">`;
        } else if (a.kind === "video" && a.url) {
          thumb = `<video src="${esc(a.url)}" muted></video>`;
        } else if (a.kind === "audio") {
          thumb = `<span class="tg-attach-ico">🎵</span>`;
        }
        const size =
          a.file.size >= 1024 * 1024
            ? (a.file.size / (1024 * 1024)).toFixed(1) + " МБ"
            : Math.max(1, Math.round(a.file.size / 1024)) + " КБ";
        return `<div class="tg-attach-item" data-attach="${esc(a.id)}">
          <div class="tg-attach-thumb">${thumb}</div>
          <div class="tg-attach-meta">
            <div class="tg-attach-name">${esc(a.file.name || "файл")}</div>
            <div class="tg-attach-size">${esc(size)}</div>
          </div>
          <button type="button" class="tg-attach-rm" data-rm="${esc(a.id)}" aria-label="Убрать">✕</button>
        </div>`;
      })
      .join("");
  }

  async function sendTgMessage() {
    const input = $("#tgInput");
    const text = (input?.value || "").trim();
    if (!tgState.peerId || tgState.sending) return;

    if (isMaxChannel()) {
      const files = tgState.attachments.map((a) => a.file);
      if (!text && !files.length) return;
      const tmpId = "tmp:" + Date.now();
      const optimistic = {
        id: tmpId,
        text: text || "",
        preview: text ? text.slice(0, 120) : files.length ? "Медиа" : "",
        out: true,
        date: new Date().toISOString(),
        has_media: !!files.length,
        media_kind: files.length ? "media" : null,
        _pending: true,
      };
      tgState.messages = [...tgState.messages, optimistic];
      tgState.historyHint = "";
      if (input && !files.length) {
        input.value = "";
        input.style.height = "auto";
      }
      renderTgMessages({ stickBottom: true });
      tgState.sending = true;
      const btn = $("#tgSendBtn");
      if (btn) btn.disabled = true;
      try {
        let data;
        if (files.length) {
          const fd = new FormData();
          if (isMaxUserbot()) {
            const accountId = currentMaxAccountId();
            if (accountId) fd.append("account_id", String(accountId));
          }
          if (text) fd.append("caption", text);
          if ($("#tgAsDocument")?.checked) fd.append("as_document", "1");
          for (const f of files) fd.append("files", f, f.name);
          data = await AdminAPI.maxChatSendMedia(tgState.peerId, fd);
          clearTgAttachments();
          if (input) {
            input.value = "";
            input.style.height = "auto";
          }
        } else {
          data = await AdminAPI.maxChatSend(tgState.peerId, {
            text,
            account_id: isMaxUserbot() ? currentMaxAccountId() : undefined,
          });
        }
        if (data.message?.peer_id && String(data.message.peer_id) !== String(tgState.peerId)) {
          tgState.peerId = data.message.peer_id;
        }
        if (data.peer_id != null && String(data.peer_id) !== String(tgState.peerId)) {
          tgState.peerId = data.peer_id;
        }
        const idx = tgState.messages.findIndex((m) => m.id === tmpId);
        const added = data.messages || (data.message ? [data.message] : []);
        if (added.length) {
          if (idx >= 0) {
            tgState.messages.splice(idx, 1, ...added);
          } else {
            tgState.messages = [...tgState.messages, ...added];
          }
        } else if (idx >= 0) {
          tgState.messages[idx] = { ...optimistic, _pending: false, id: tmpId };
        }
        renderTgMessages({ stickBottom: true });
        await refreshTgDialogs({ silent: true });
      } catch (err) {
        const idx = tgState.messages.findIndex((m) => m.id === tmpId);
        if (idx >= 0) {
          tgState.messages[idx]._pending = false;
          tgState.messages[idx]._failed = true;
          renderTgMessages({ stickBottom: true });
        }
        alert("Не отправлено: " + (err.data?.error || err.message));
      } finally {
        tgState.sending = false;
        if (btn) btn.disabled = false;
        input?.focus();
      }
      return;
    }

    const accountId = currentTgAccountId();
    const files = tgState.attachments.map((a) => a.file);
    if (!accountId || (!text && !files.length)) return;

    tgState.sending = true;
    const btn = $("#tgSendBtn");
    if (btn) btn.disabled = true;
    try {
      let data;
      if (files.length) {
        const fd = new FormData();
        fd.append("account_id", String(accountId));
        if (text) fd.append("caption", text);
        if ($("#tgAsDocument")?.checked) fd.append("as_document", "1");
        for (const f of files) fd.append("files", f, f.name);
        data = await AdminAPI.chatSendMedia(tgState.peerId, fd);
        clearTgAttachments();
        if (input) {
          input.value = "";
          input.style.height = "auto";
        }
        const added = data.messages || (data.message ? [data.message] : []);
        if (added.length) {
          tgState.messages = [...tgState.messages, ...added];
          renderTgMessages({ stickBottom: true });
        } else {
          await openTgPeer(tgState.peerId, { silent: true });
        }
      } else {
        data = await AdminAPI.chatSend(tgState.peerId, {
          account_id: accountId,
          text,
        });
        if (input) {
          input.value = "";
          input.style.height = "auto";
        }
        if (data.message) {
          tgState.messages = [...tgState.messages, data.message];
          renderTgMessages({ stickBottom: true });
        } else {
          await openTgPeer(tgState.peerId, { silent: true });
        }
      }
      await refreshTgDialogs({ silent: true });
    } catch (err) {
      alert("Не отправлено: " + (err.data?.error || err.message));
    } finally {
      tgState.sending = false;
      if (btn) btn.disabled = false;
      input?.focus();
    }
  }

  function openTgNewChatModal(open) {
    const modal = $("#tgNewChatModal");
    if (!modal) return;
    modal.hidden = !open;
    if (open) {
      $("#tgNewPhone").value = "";
      $("#tgNewUsername").value = "";
      $("#tgNewName").value = "";
      $("#tgNewMessage").value = "";
      setTimeout(() => $("#tgNewPhone")?.focus(), 50);
    }
  }

  async function submitTgNewChat(e) {
    e.preventDefault();
    const maxUserbot = isMaxUserbot();
    const accountId = maxUserbot ? currentMaxAccountId() : currentTgAccountId();
    if (!accountId) {
      alert(maxUserbot ? "Сначала подключите номер MAX" : "Сначала подключите Telegram-аккаунт");
      return;
    }
    const phone = ($("#tgNewPhone")?.value || "").trim();
    const username = ($("#tgNewUsername")?.value || "").trim();
    const name = ($("#tgNewName")?.value || "").trim();
    const message = ($("#tgNewMessage")?.value || "").trim();
    if (maxUserbot) {
      if (!phone) {
        alert("Укажите телефон клиента");
        return;
      }
    } else if (!phone && !username) {
      alert("Укажите телефон или @username");
      return;
    }
    const btn = $("#tgNewChatSubmit");
    if (btn) btn.disabled = true;
    try {
      const data = maxUserbot
        ? await AdminAPI.maxChatCreate({
            account_id: accountId,
            phone,
            name,
            message,
          })
        : await AdminAPI.chatCreate({
            account_id: accountId,
            phone,
            username,
            name,
            message,
          });
      openTgNewChatModal(false);
      await refreshTgDialogs({ silent: true });
      const peerId = data.peer?.peer_id;
      if (peerId != null) await openTgPeer(peerId);
    } catch (err) {
      alert("Не удалось создать чат: " + (err.data?.error || err.message));
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function loadChats() {
    bindTgChatsOnce();
    const saved = localStorage.getItem(CHATS_CHANNEL_KEY);
    if (saved === "max" || saved === "tg") tgState.channel = saved;
    applyChatsChannelUi();

    if (isMaxChannel()) {
      try {
        const status = await AdminAPI.maxChatStatus();
        tgState.maxMode = status.mode || (status.accounts?.length ? "userbot" : status.bot_configured ? "bot" : "none");
        tgState.maxConfigured = !!status.configured && (status.ok !== false || !!status.accounts?.length);
        tgState.maxLabel =
          status.label ||
          (status.bot_username ? "@" + status.bot_username : "") ||
          status.bot_name ||
          "MAX";
        tgState.accounts = status.accounts || [];
        if (tgState.maxMode === "userbot") {
          renderTgAccounts();
        }
        applyChatsChannelUi();
        if (!tgState.maxConfigured) {
          tgState.dialogs = [];
          renderTgDialogs();
          stopTgPoll();
          stopMaxSSE();
          return;
        }
        await refreshTgDialogs();
        startMaxSSE();
        startTgPoll();
      } catch (err) {
        tgState.maxConfigured = false;
        tgState.maxMode = "none";
        stopMaxSSE();
        $("#tgDialogList").innerHTML = `<div class="tg-empty"><div class="t">Ошибка</div>${esc(
          err.data?.error || err.message
        )}</div>`;
      }
      return;
    }

    stopMaxSSE();

    try {
      const data = await AdminAPI.chatAccounts();
      tgState.accounts = data.items || [];
      if (!data.telethon_configured && !tgState.accounts.length) {
        $("#tgDialogList").innerHTML = `<div class="tg-empty"><div class="t">Telegram не настроен</div>Укажите API ID/Hash и подключите аккаунт в Настройках.<div style="margin-top:12px"><button class="btn primary" type="button" onclick="go('settings')">Настройки</button></div></div>`;
        renderTgAccounts();
        return;
      }
      renderTgAccounts();
      await refreshTgDialogs();
      startTgPoll();
    } catch (err) {
      $("#tgDialogList").innerHTML = `<div class="tg-empty"><div class="t">Ошибка</div>${esc(
        err.data?.error || err.message
      )}</div>`;
    }
  }

  function bindTgChatsOnce() {
    if (tgState.ready) return;
    tgState.ready = true;

    $$(".ch-chan-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        setChatsChannel(btn.getAttribute("data-ch-channel") || "tg");
      });
    });

    $("#tgAccountSelect")?.addEventListener("change", async () => {
      tgState.accountId = isMaxChannel() ? currentMaxAccountId() : currentTgAccountId();
      if (tgState.accountId) {
        localStorage.setItem(
          isMaxChannel() ? MAX_ACCOUNT_KEY : TG_ACCOUNT_KEY,
          String(tgState.accountId)
        );
      }
      tgState.peerId = null;
      tgState.messages = [];
      showTgThread(false);
      await refreshTgDialogs();
    });

    $("#tgRefreshDialogs")?.addEventListener("click", () => refreshTgDialogs());
    $("#tgNewChatBtn")?.addEventListener("click", () => {
      if (isMaxChannel() && !isMaxUserbot()) return;
      const title = $("#tgNewChatTitle");
      const desc = $("#tgNewChatModal .page-desc");
      const userField = $("#tgNewUsername")?.closest("label");
      if (isMaxUserbot()) {
        if (title) title.textContent = "Новый чат MAX";
        if (desc) desc.textContent = "Откройте диалог по номеру телефона — как в MAX.";
        if (userField) userField.hidden = true;
      } else {
        if (title) title.textContent = "Новый чат";
        if (desc)
          desc.textContent = "Откройте диалог по номеру телефона или @username — как в Telegram.";
        if (userField) userField.hidden = false;
      }
      openTgNewChatModal(true);
    });
    $("#tgNewChatForm")?.addEventListener("submit", submitTgNewChat);
    $$("[data-tg-close]").forEach((el) =>
      el.addEventListener("click", () => openTgNewChatModal(false))
    );

    const onlyUsersEl = $("#tgOnlyUsers");
    if (onlyUsersEl) {
      onlyUsersEl.checked = !!tgState.onlyUsers;
      onlyUsersEl.addEventListener("change", () => {
        tgState.onlyUsers = !!onlyUsersEl.checked;
        localStorage.setItem(TG_ONLY_USERS_KEY, tgState.onlyUsers ? "1" : "0");
        refreshTgDialogs();
      });
    }

    $("#tgCreateClientBtn")?.addEventListener("click", () => onTgCreateClientClick());
    $("#tgOpenClientBtn")?.addEventListener("click", () => {
      const id = +($("#tgOpenClientBtn")?.dataset.clientId || 0);
      if (id) openClientById(id);
    });
    $("#tgCreateClientForm")?.addEventListener("submit", submitTgCreateClient);
    $$("[data-tg-client-close]").forEach((el) =>
      el.addEventListener("click", () => openTgCreateClientModal(false))
    );

    $("#tgDialogList")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-peer]");
      if (!btn) return;
      e.preventDefault();
      const peer = btn.getAttribute("data-peer");
      if (peer == null || peer === "") return;
      openTgPeer(peer);
    });

    $("#tgBackToList")?.addEventListener("click", () => {
      if (history.state?.tgThread) {
        history.back();
        return;
      }
      tgState.peerId = null;
      showTgThread(false);
      renderTgDialogs();
    });

    window.addEventListener("popstate", () => {
      if (!document.body.classList.contains("tg-thread-open")) return;
      if (!window.matchMedia("(max-width: 899px)").matches) return;
      tgState.peerId = null;
      showTgThread(false);
      renderTgDialogs();
    });

    $("#tgComposer")?.addEventListener("submit", (e) => {
      e.preventDefault();
      sendTgMessage();
    });

    $("#tgAttachBtn")?.addEventListener("click", () => {
      $("#tgFileInput")?.click();
    });
    $("#tgFileInput")?.addEventListener("change", (e) => {
      addTgAttachments(e.target.files);
      e.target.value = "";
    });
    $("#tgAttachList")?.addEventListener("click", (e) => {
      const rm = e.target.closest("[data-rm]");
      if (!rm) return;
      removeTgAttachment(rm.getAttribute("data-rm"));
    });

    const input = $("#tgInput");
    input?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendTgMessage();
      }
    });
    input?.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 140) + "px";
    });
    input?.addEventListener("paste", (e) => {
      const items = [...(e.clipboardData?.items || [])];
      const files = items
        .filter((it) => it.kind === "file")
        .map((it) => it.getAsFile())
        .filter(Boolean);
      if (!files.length) return;
      e.preventDefault();
      addTgAttachments(files);
    });

    const dropZone = $("#tgThread") || $("#tgComposerWrap");
    dropZone?.addEventListener("dragover", (e) => {
      if (![...e.dataTransfer.types].includes("Files")) return;
      e.preventDefault();
      $("#tgComposerWrap")?.classList.add("dragover");
    });
    dropZone?.addEventListener("dragleave", (e) => {
      if (e.target === dropZone || e.currentTarget === dropZone) {
        $("#tgComposerWrap")?.classList.remove("dragover");
      }
    });
    dropZone?.addEventListener("drop", (e) => {
      $("#tgComposerWrap")?.classList.remove("dragover");
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      if (!tgState.peerId) return;
      addTgAttachments(e.dataTransfer.files);
    });

    $("#tgDialogSearch")?.addEventListener("input", (e) => {
      clearTimeout(tgState.searchTimer);
      tgState.searchTimer = setTimeout(() => {
        tgState.lastQuery = (e.target.value || "").trim();
        refreshTgDialogs();
      }, 280);
    });
  }

  async function openChatWithClient(client) {
    const phone = (client?.phone || "").trim();
    const name = (client?.name || "").trim();
    if (!phone) {
      alert("У клиента нет телефона");
      return;
    }
    go("chats");
    if (isMaxChannel()) await setChatsChannel("tg");
    else await loadChats();
    const accountId = currentTgAccountId();
    if (!accountId) {
      alert("Подключите Telegram-аккаунт в Настройках");
      return;
    }
    try {
      const data = await AdminAPI.chatCreate({
        account_id: accountId,
        phone,
        name,
      });
      await refreshTgDialogs({ silent: true });
      if (data.peer?.peer_id != null) await openTgPeer(data.peer.peer_id);
    } catch (err) {
      alert("Не удалось открыть чат: " + (err.data?.error || err.message));
    }
  }
  window.openChatWithClient = openChatWithClient;

  // ── Колесо фортуны (превью = тот же виджет, что в Mini App) ──────────────

  const WHEEL_COLORS =
    (window.VereskWheel && window.VereskWheel.DEFAULT_COLORS) ||
    ["#d64593", "#3a2558", "#e86aad", "#5a3d7a", "#c43d86", "#7b4bd6", "#f47db9", "#241a38"];
  const WHEEL_DEFAULT_SEGS = [
    { id: "s1", label: "Скидка 10%", color: "#E879B0", weight: 30 },
    { id: "s2", label: "Скидка 15%", color: "#3D2A55", weight: 18 },
    { id: "s3", label: "Бесплатная доставка", color: "#F3C4DC", weight: 22 },
    { id: "s4", label: "Попробуйте ещё", color: "#6B4C8A", weight: 20 },
    { id: "s5", label: "Мини-букет", color: "#D4569A", weight: 10 },
  ];

  const wheelState = {
    inited: false,
    segs: [],
    widget: null,
  };

  function uidWheelSeg() {
    return "w" + Math.random().toString(36).slice(2, 9);
  }

  function wheelTotalWeight(segs) {
    if (window.VereskWheel?.totalWeight) return window.VereskWheel.totalWeight(segs);
    return segs.reduce((sum, s) => sum + Math.max(0, Number(s.weight) || 0), 0);
  }

  function wheelChancePct(seg, segs) {
    const total = wheelTotalWeight(segs);
    if (!total) return 0;
    return Math.round((Math.max(0, Number(seg.weight) || 0) / total) * 1000) / 10;
  }

  function collectWheelPayload() {
    return {
      title: ($("#wheelTitle")?.value || "").trim(),
      note: ($("#wheelNote")?.value || "").trim(),
      segments: wheelState.segs.map((s, i) => ({
        id: s.id,
        label: String(s.label || "").trim(),
        color: s.color,
        weight: Math.max(0, Number(s.weight) || 0),
        order: i,
        chance_pct: wheelChancePct(s, wheelState.segs),
      })),
    };
  }
  window.collectWheelPayload = collectWheelPayload;

  function validateWheelDraft() {
    const payload = collectWheelPayload();
    if (!payload.title) return "Укажите название колеса";
    if (payload.segments.length < 2) return "Нужно минимум 2 сектора";
    if (payload.segments.some((s) => !s.label)) return "У каждого сектора должно быть название";
    if (wheelTotalWeight(payload.segments) <= 0) return "Сумма весов должна быть больше 0";
    return "";
  }

  function ensureWheelWidget() {
    const mount = $("#wheelWidgetMount");
    if (!mount || !window.VereskWheel?.create) return null;
    if (!wheelState.widget) {
      wheelState.widget = window.VereskWheel.create(mount, {
        title: "",
        note: "",
        segments: [],
      });
    }
    return wheelState.widget;
  }

  function syncWheelWidget() {
    const widget = ensureWheelWidget();
    const payload = collectWheelPayload();
    const countEl = $("#wheelSegCount");
    const legend = $("#wheelLegend");
    if (countEl) countEl.textContent = String(wheelState.segs.length);
    if (widget) {
      widget.setConfig({
        title: payload.title || "Колесо фортуны",
        note: payload.note || "Так увидит клиент в Mini App",
        segments: payload.segments,
      });
    }
    if (legend) {
      legend.innerHTML = wheelState.segs
        .map((s) => {
          const pct = wheelChancePct(s, wheelState.segs);
          return `<li><span class="wheel-legend-swatch" style="background:${esc(s.color)}"></span><span>${esc(s.label || "Без названия")}</span><b>${pct}%</b></li>`;
        })
        .join("");
    }
  }

  function renderWheelSegs() {
    const box = $("#wheelSegs");
    if (!box) return;
    if (!wheelState.segs.length) {
      box.innerHTML =
        '<div class="empty" style="padding:18px;text-align:center;color:var(--ink-3)">Пока нет секторов — нажмите «Добавить»</div>';
      syncWheelWidget();
      return;
    }
    box.innerHTML = wheelState.segs
      .map((s, i) => {
        const pct = wheelChancePct(s, wheelState.segs);
        return `
        <div class="wheel-seg" role="listitem" data-id="${esc(s.id)}">
          <input class="wheel-seg-color" type="color" value="${esc(s.color)}" data-field="color" aria-label="Цвет сектора ${i + 1}">
          <div class="wheel-seg-fields">
            <div>
              <label for="wheelSegLabel_${esc(s.id)}">Приз</label>
              <input id="wheelSegLabel_${esc(s.id)}" type="text" maxlength="48" value="${esc(s.label)}" data-field="label" placeholder="Название приза">
            </div>
          </div>
          <div class="wheel-seg-fields wheel-seg-weight">
            <label for="wheelSegWeight_${esc(s.id)}">Вес</label>
            <input id="wheelSegWeight_${esc(s.id)}" type="number" min="0" step="1" value="${esc(s.weight)}" data-field="weight">
          </div>
          <div class="wheel-seg-meta">
            <span class="wheel-seg-chance">${pct}%</span>
            <button type="button" class="wheel-seg-rm" data-rm="${esc(s.id)}" aria-label="Удалить сектор" title="Удалить">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 8h14M10 12v6M14 12v6M9 8V6a1 1 0 011-1h4a1 1 0 011 1v2M6 8l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12"/></svg>
            </button>
          </div>
        </div>`;
      })
      .join("");
    syncWheelWidget();
  }

  function pickWheelColor(index) {
    return WHEEL_COLORS[index % WHEEL_COLORS.length];
  }

  function addWheelSeg() {
    const i = wheelState.segs.length;
    wheelState.segs.push({
      id: uidWheelSeg(),
      label: `Приз ${i + 1}`,
      color: pickWheelColor(i),
      weight: 10,
    });
    renderWheelSegs();
    const errEl = $("#wheelError");
    if (errEl) errEl.hidden = true;
  }

  function removeWheelSeg(id) {
    wheelState.segs = wheelState.segs.filter((s) => s.id !== id);
    renderWheelSegs();
  }

  function updateWheelSeg(id, field, value) {
    const seg = wheelState.segs.find((s) => s.id === id);
    if (!seg) return;
    if (field === "label") seg.label = value;
    else if (field === "color") seg.color = value || pickWheelColor(0);
    else if (field === "weight") seg.weight = Math.max(0, Number(value) || 0);

    if (field === "color") {
      renderWheelSegs();
      return;
    }

    if (field === "weight") {
      $$(".wheel-seg").forEach((el) => {
        const s = wheelState.segs.find((x) => x.id === el.getAttribute("data-id"));
        const badge = el.querySelector(".wheel-seg-chance");
        if (s && badge) badge.textContent = `${wheelChancePct(s, wheelState.segs)}%`;
      });
    } else {
      const row = $(`.wheel-seg[data-id="${CSS.escape(id)}"]`);
      const chance = row?.querySelector(".wheel-seg-chance");
      if (chance) chance.textContent = `${wheelChancePct(seg, wheelState.segs)}%`;
    }
    syncWheelWidget();
  }

  function spinWheelPreview() {
    const errEl = $("#wheelError");
    const widget = ensureWheelWidget();
    if (!widget) return;
    syncWheelWidget();
    widget.spin().catch((err) => {
      if (errEl && err && err.message === "need segments") {
        errEl.textContent = "Для прокрутки нужно минимум 2 сектора с весом > 0";
        errEl.hidden = false;
      }
    });
    if (errEl) errEl.hidden = true;
  }

  function applyWheelConfig(cfg) {
    const data = cfg || {};
    const title = $("#wheelTitle");
    const note = $("#wheelNote");
    const err = $("#wheelError");
    if (title) title.value = String(data.title || "");
    if (note) note.value = String(data.note || "");
    if (err) err.hidden = true;
    const segs = Array.isArray(data.segments) ? data.segments : [];
    wheelState.segs = segs.map((s, i) => ({
      id: String(s.id || uidWheelSeg()),
      label: String(s.label || "").trim(),
      color: s.color || WHEEL_COLORS[i % WHEEL_COLORS.length],
      weight: Math.max(0, Number(s.weight) || 0),
    }));
    if (wheelState.segs.length < 2) {
      wheelState.segs = WHEEL_DEFAULT_SEGS.map((s) => ({ ...s, id: uidWheelSeg() }));
      if (title && !title.value) title.value = "Весенний розыгрыш";
      if (note && !note.value) note.value = "Крутите колесо — получите подарок от Veresk";
    }
    ensureWheelWidget();
    renderWheelSegs();
  }

  function resetWheelEditor() {
    applyWheelConfig({
      title: "Весенний розыгрыш",
      note: "Крутите колесо — получите подарок от Veresk",
      segments: WHEEL_DEFAULT_SEGS,
    });
  }

  async function loadWheelEditor() {
    const errEl = $("#wheelError");
    try {
      const cfg = await AdminAPI.wheelConfig();
      applyWheelConfig(cfg);
    } catch (err) {
      console.warn("[wheel] load failed, using defaults", err);
      resetWheelEditor();
      if (errEl && err.status !== 401) {
        errEl.textContent =
          "Не удалось загрузить сохранённые настройки — показаны значения по умолчанию";
        errEl.hidden = false;
      }
    }
    loadWheelPlays();
  }

  function wheelInitials(name) {
    const parts = String(name || "")
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    if (!parts.length) return "V";
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }

  function formatWheelWhen(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return String(iso);
      return d.toLocaleString("ru-RU", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (_) {
      return String(iso);
    }
  }

  function renderWheelPlays(data) {
    const box = $("#wheelPlaysList");
    const stats = $("#wheelPlaysStats");
    if (!box) return;
    const items = Array.isArray(data?.items) ? data.items : [];
    if (stats) {
      stats.hidden = false;
      stats.innerHTML = `
        <span class="wheel-plays-pill">Всего <b>${esc(data?.total ?? items.length)}</b></span>
        <span class="wheel-plays-pill">Telegram <b>${esc(data?.telegram ?? 0)}</b></span>
        <span class="wheel-plays-pill">MAX <b>${esc(data?.max ?? 0)}</b></span>
      `;
    }
    if (!items.length) {
      box.innerHTML = '<div class="wheel-plays-empty">Пока никто не крутил колесо</div>';
      return;
    }
    box.innerHTML = items
      .map((p) => {
        const channel = p.channel === "max" ? "max" : "telegram";
        const channelLabel = channel === "max" ? "MAX" : "Telegram";
        const name = p.full_name || [p.first_name, p.last_name].filter(Boolean).join(" ") || "Без имени";
        const disc =
          p.discount_pct != null && p.discount_pct !== ""
            ? `<span class="wheel-play-discount">−${esc(p.discount_pct)}%</span>`
            : "";
        const tgId = p.tg_user_id != null ? p.tg_user_id : channel === "telegram" ? p.user_id : "";
        const maxId = p.max_user_id != null ? p.max_user_id : channel === "max" ? p.user_id : "";
        const uname = p.username ? `@${p.username}` : "";
        return `
          <article class="wheel-play">
            <div class="wheel-play-avatar" aria-hidden="true">${esc(wheelInitials(name))}</div>
            <div class="wheel-play-main">
              <div class="wheel-play-name">${esc(name)}</div>
              <div class="wheel-play-meta">
                <span class="wheel-play-channel is-${channel === "max" ? "max" : "tg"}">${channelLabel}</span>
                ${uname ? `<span>${esc(uname)}</span>` : ""}
                <span class="wheel-play-time">${esc(formatWheelWhen(p.created_at))}</span>
              </div>
            </div>
            <div class="wheel-play-ids">
              ${tgId !== "" && tgId != null ? `<span><b>TG ID</b>${esc(tgId)}</span>` : ""}
              ${maxId !== "" && maxId != null ? `<span><b>MAX ID</b>${esc(maxId)}</span>` : ""}
              ${!tgId && !maxId ? `<span><b>ID</b>${esc(p.user_id || "—")}</span>` : ""}
            </div>
            <div class="wheel-play-prize">
              <div class="wheel-play-prize-label">${esc(p.prize_label || "Приз")}</div>
              ${disc}
            </div>
          </article>
        `;
      })
      .join("");
  }

  async function loadWheelPlays() {
    const box = $("#wheelPlaysList");
    if (!box) return;
    box.innerHTML = '<div class="wheel-plays-empty">Загрузка участников…</div>';
    try {
      const data = await AdminAPI.wheelPlays({ limit: 200 });
      renderWheelPlays(data);
    } catch (err) {
      console.warn("[wheel] plays", err);
      box.innerHTML =
        '<div class="wheel-plays-empty">Не удалось загрузить участников</div>';
    }
  }

  async function saveWheelEditor() {
    const msg = validateWheelDraft();
    const errEl = $("#wheelError");
    const btn = $("#wheelSave");
    if (msg) {
      if (errEl) {
        errEl.textContent = msg;
        errEl.hidden = false;
      }
      return;
    }
    if (errEl) errEl.hidden = true;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Сохраняю…";
    }
    try {
      const saved = await AdminAPI.wheelSave(collectWheelPayload());
      applyWheelConfig(saved);
      alert("Настройки фортуны сохранены");
    } catch (err) {
      const detail =
        err.data?.detail || err.data?.error || err.message || "Ошибка сохранения";
      if (errEl) {
        errEl.textContent = detail;
        errEl.hidden = false;
      } else {
        alert(detail);
      }
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Сохранить";
      }
    }
  }

  function initWheelEditor() {
    if (!wheelState.inited) {
      const segsBox = $("#wheelSegs");
      segsBox?.addEventListener("input", (e) => {
        const row = e.target.closest(".wheel-seg");
        if (!row) return;
        const field = e.target.getAttribute("data-field");
        if (!field) return;
        updateWheelSeg(row.getAttribute("data-id"), field, e.target.value);
      });
      segsBox?.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-rm]");
        if (!btn) return;
        removeWheelSeg(btn.getAttribute("data-rm"));
      });
      $("#wheelAddSeg")?.addEventListener("click", addWheelSeg);
      $("#wheelSpinDemo")?.addEventListener("click", spinWheelPreview);
      $("#wheelExit")?.addEventListener("click", () => go("home"));
      $("#wheelTitle")?.addEventListener("input", syncWheelWidget);
      $("#wheelNote")?.addEventListener("input", syncWheelWidget);
      $("#wheelSave")?.addEventListener("click", () => {
        saveWheelEditor();
      });
      $("#wheelPlaysRefresh")?.addEventListener("click", () => loadWheelPlays());
      wheelState.inited = true;
      loadWheelEditor();
    } else {
      loadWheelEditor();
    }
  }
  window.initWheelEditor = initWheelEditor;

  // boot
  tryAuth();
})();
