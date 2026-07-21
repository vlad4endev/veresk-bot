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
    wizard: { segment: "regular", message: "", when: "later", date: "", time: "10:00" },
    step: 0,
    tgPhone: "",
  };

  const panels = $$(".panel");
  const navItems = $$(".nav-item, .bnav-item[data-nav]");

  function go(tab) {
    if (tab === "accounts") tab = "settings";
    panels.forEach((p) => p.classList.toggle("active", p.id === tab));
    const navKey =
      ({ compose: "home", detail: "home", personal: "home", client: "clients" })[tab] ||
      tab;
    navItems.forEach((n) => n.classList.toggle("active", n.dataset.nav === navKey));
    document.body.classList.toggle("hide-bnav", tab === "compose");
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (tab === "compose") setStep(0);
    if (tab === "home") loadHome();
    if (tab === "clients") loadClients();
    if (tab === "settings") loadSettings();
    if (tab === "aichat") initAiChat();
  }
  window.go = go;

  // ── auth ────────────────────────────────────────────────────────────────

  async function showApp() {
    $("#loginScreen").classList.add("hidden");
    $("#appShell").classList.remove("hidden");
    await loadHome();
  }

  function showLogin() {
    $("#appShell").classList.add("hidden");
    $("#loginScreen").classList.remove("hidden");
    setTimeout(focusLogin, 50);
  }

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
        AdminAPI.events(14),
        AdminAPI.campaigns(),
      ]);
      $("#statCustomers").textContent = fmtNum(stats.customers);
      const accLabel =
        stats.accounts_total > 0
          ? `${stats.accounts_ready} из ${stats.accounts_total}`
          : "0";
      $("#statAccounts").textContent = accLabel;
      $("#statDelivery").textContent =
        stats.delivery_rate != null ? stats.delivery_rate + "%" : "—";
      renderEvents(events.items || []);
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

  function renderEvents(items) {
    const box = $("#eventsList");
    if (!items.length) {
      box.innerHTML =
        '<div class="empty-state"><div class="t">Нет ближайших событий</div>Синхронизируйте базу из Posiflora</div>';
      return;
    }
    box.innerHTML = items
      .slice(0, 8)
      .map((e) => {
        const auto = e.auto_send ? " auto" : "";
        return `<div class="ev${auto}" data-id="${e.id}">
          <span class="ev-ic">${eventIcon(e.kind)}</span>
          <div class="ev-b clickable" data-client="${e.customer_id}" title="Открыть карточку">
            <div class="ev-n">${esc(e.title)} · ${esc(e.customer_name)}</div>
            <div class="ev-s">${esc(e.phone_masked)} · ${esc(e.channel)}</div>
          </div>
          <span class="ev-when ${esc(e.when_class)}">${esc(e.when_label)}</span>
          <div class="ev-act">
            <label class="sw" title="Поздравлять автоматически">
              <input type="checkbox" data-auto="${e.id}" ${e.auto_send ? "checked" : ""}>
              <span class="track"></span>Авто
            </label>
            <button class="ev-send" data-personal="${encodeURIComponent(JSON.stringify({
              type: e.kind === "anniv" ? "anniv" : e.kind === "bday" ? "bday" : "plain",
              customer_id: e.customer_id,
              name: e.customer_name,
              contact: e.phone_masked,
              chan: e.channel,
              chanClass: e.channel_class,
              evText: e.title + " · " + e.when_label,
              whenClass: e.when_class,
            }))}">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m3 11 18-8-8 18-2-8z"/></svg>
              ${e.kind === "bday" || e.kind === "anniv" ? "Поздравить" : "Отправить"}
            </button>
            <span class="auto-note">✓ Поздравим сами</span>
          </div>
        </div>`;
      })
      .join("");

    box.querySelectorAll("[data-client]").forEach((el) => {
      el.addEventListener("click", () => openClientById(+el.dataset.client));
    });
    box.querySelectorAll("[data-auto]").forEach((inp) => {
      inp.addEventListener("change", async () => {
        const id = +inp.dataset.auto;
        inp.closest(".ev").classList.toggle("auto", inp.checked);
        try {
          await AdminAPI.setEventAuto(id, inp.checked);
        } catch {
          inp.checked = !inp.checked;
          inp.closest(".ev").classList.toggle("auto", inp.checked);
        }
      });
    });
    box.querySelectorAll("[data-personal]").forEach((btn) => {
      btn.addEventListener("click", () => {
        try {
          const d = JSON.parse(decodeURIComponent(btn.getAttribute("data-personal")));
          openPersonal(d);
        } catch (_) {}
      });
    });
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
        const res =
          c.status === "sending"
            ? `${fmtNum(c.sent_count)} из ${fmtNum(c.total_count)}`
            : c.status === "done"
              ? `отправлено ${fmtNum(c.sent_count)}`
              : "";
        return `<button class="rrow" data-cid="${c.id}">
          <span class="em">${esc(c.emoji)}</span>
          <span class="rname">
            <span class="n">${esc(c.title)}</span>
            <span class="who">${esc(c.segment_label)} · ${fmtNum(c.total_count)} чел.</span>
            <span class="m-status"><span class="d" style="background:${dot[c.status_class] || "var(--ink-3)"}"></span>${esc(c.status_label)}</span>
          </span>
          <span class="col-chan">${chans}</span>
          <span class="col-status">
            <span class="status ${esc(c.status_class)}"><span class="d" style="background:${dot[c.status_class] || "var(--ink-3)"}"></span>${esc(c.status_label)}</span>
            ${res ? `<div class="res">${esc(res)}</div>` : ""}
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
    const sent = c.status === "sending" || c.status === "done";
    const chans = String(c.channels || "")
      .split(",")
      .filter(Boolean)
      .map((ch) => {
        const t = ch.trim();
        return `<span class="chan ${t === "MAX" ? "max" : "tg"}">${esc(t)}</span>`;
      })
      .join(" ");
    const msgHtml = esc(c.message).replace(/\n/g, "<br>");
    let recipientsHtml = "";
    (recipients.items || []).forEach((r) => {
      const stClass =
        r.status === "failed"
          ? "err"
          : r.status === "pending"
            ? "sending"
            : r.status === "delivered" || r.status === "sent"
              ? "done"
              : "neutral";
      const stLabel =
        {
          pending: "Ожидает",
          sent: "Отправлено",
          delivered: "Доставлено",
          failed: "Не доставлено",
        }[r.status] || r.status;
      recipientsHtml += `<tr>
        <td class="who"><div class="nm">${esc(r.name)}</div><div class="h">${esc(r.phone_masked)}</div></td>
        <td class="hide-mob"><span class="chan ${r.channel === "max" ? "max" : "tg"}">${r.channel === "max" ? "MAX" : "Telegram"}</span></td>
        <td>${esc(r.sent_at ? r.sent_at.slice(11, 16) : "—")}</td>
        <td><span class="status ${stClass}"><span class="d" style="background:currentColor"></span>${esc(stLabel)}</span></td>
      </tr>`;
    });

    const leftActions = sent
      ? `<div class="det-actions">
          <button class="btn primary" id="btnRepeat">Повторить рассылку</button>
        </div>`
      : `<div class="notsent">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16h.01"/></svg>
          <div>${c.status === "scheduled" ? "Рассылка запланирована. " : "Рассылка ещё не отправлена. "}Получатели и статистика появятся после отправки.</div>
        </div>
        <div class="det-actions">
          <button class="btn primary big" id="btnSendNow">Отправить сейчас</button>
          <button class="btn big" onclick="go('compose')">Редактировать</button>
        </div>`;

    const rightBody = sent
      ? `<div class="subh">Как дошло</div>
        <div class="dstrip">
          <div class="stat"><div class="n">${fmtNum(c.sent_count)}</div><div class="l">отправлено</div></div>
          <div class="stat"><div class="n">${fmtNum(c.delivered_count || "—")}</div><div class="l">доставлено</div></div>
          <div class="stat"><div class="n">${fmtNum(c.failed_count)}</div><div class="l">ошибок</div></div>
          <div class="stat"><div class="n">${fmtNum(c.total_count)}</div><div class="l">всего</div></div>
        </div>
        <div class="subh">Получатели <span class="rcount">· ${fmtNum(recipients.total)} человек</span></div>
        <div class="searchbox">
          <svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
          <input type="text" id="rSearch" placeholder="Поиск по имени или телефону">
        </div>
        <div class="tbl-wrap" style="margin-top:12px">
          <table><thead><tr><th>Клиент</th><th class="hide-mob">Где</th><th>Когда</th><th>Статус</th></tr></thead>
          <tbody id="rBody">${recipientsHtml || '<tr><td colspan="4" class="empty-state">Нет получателей</td></tr>'}</tbody></table>
        </div>`
      : `<div class="empty-rich" style="padding:28px 16px">
          <div class="er-ic" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 3v4M8 7h8"/><circle cx="12" cy="14" r="6"/><path d="M12 12v3"/></svg>
          </div>
          <div class="t">Ждём отправки</div>
          <p class="d">После запуска здесь появятся статистика и список получателей.</p>
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
            <div class="bubble">${msgHtml}<div class="tm">${sent ? "✓✓" : ""}</div></div>
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
      $("#msg").value = c.message;
      go("compose");
      setStep(1);
    });
    $("#rSearch")?.addEventListener("input", async () => {
      const q = $("#rSearch").value.trim();
      const data = await AdminAPI.recipients(c.id, { search: q });
      const body = $("#rBody");
      if (!body) return;
      body.innerHTML = (data.items || [])
        .map(
          (r) => `<tr>
          <td class="who"><div class="nm">${esc(r.name)}</div><div class="h">${esc(r.phone_masked)}</div></td>
          <td class="hide-mob"><span class="chan ${r.channel === "max" ? "max" : "tg"}">${r.channel === "max" ? "MAX" : "Telegram"}</span></td>
          <td>${esc(r.sent_at ? r.sent_at.slice(11, 16) : "—")}</td>
          <td>${esc(r.status)}</td>
        </tr>`
        )
        .join("");
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

  function clientChannelsHtml(channels) {
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
          <td><div class="ch-cell-inner">${clientChannelsHtml(c.channels)}</div></td>
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
      $("#clBday").textContent = bday ? bday.date_from : "—";
      $("#clAnniv").textContent = anniv ? anniv.date_from : "—";
      $("#clAnnivBtn")?.classList.toggle("hidden", !anniv);
      $("#clSince").textContent = c.since_label || "—";
      $("#clLast").textContent = c.last_order_label || "—";
      $("#clEvents").innerHTML = (c.events || [])
        .map(
          (e) => `<div class="cev">
          <span class="cev-ic">${eventIcon(e.kind)}</span>
          <div class="cev-b"><div class="cev-n">${esc(e.title)}</div><div class="cev-d">${esc(e.date_from)}</div></div>
          <button class="mini-btn" data-kind="${esc(e.kind)}">${e.kind === "other" ? "Написать" : "Поздравить"}</button>
        </div>`
        )
        .join("") || '<p class="hint">Нет событий</p>';
      $("#clEvents").querySelectorAll("[data-kind]").forEach((btn) => {
        btn.addEventListener("click", () => congratsCurrent(btn.dataset.kind));
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
        .map(
          (m) => `<tr>
          <td>${esc((m.date || "").slice(0, 10) || "—")}</td>
          <td class="who"><div class="nm">${esc(m.title)}</div></td>
          <td class="hide-mob"><span class="chan ${m.channel === "max" ? "max" : "tg"}">${m.channel === "max" ? "MAX" : "Telegram"}</span></td>
          <td><span class="status ${m.status === "failed" ? "err" : "done"}">${esc(m.status)}</span></td>
        </tr>`
        )
        .join("") || '<tr><td colspan="4" class="empty-state">Пока нет сообщений</td></tr>';
    } catch {
      $("#clName").textContent = "Ошибка загрузки";
    }
  }
  window.openClientById = openClientById;

  function congratsCurrent(type) {
    const c = state.curClient;
    if (!c) return;
    const chanLabel = (c.channels || "Telegram").split(",")[0].trim() || "Telegram";
    openPersonal({
      type: type === "anniv" ? "anniv" : type === "bday" ? "bday" : "plain",
      customer_id: c.id,
      name: c.name,
      contact: c.phone_masked || c.phone,
      chan: chanLabel,
      chanClass: chanLabel === "MAX" ? "max" : "tg",
      evText: type === "bday" ? "День рождения" : type === "anniv" ? "Годовщина" : c.segment_label,
      whenClass: "today",
    });
  }
  window.congratsCurrent = congratsCurrent;

  $("#clWrite")?.addEventListener("click", () => congratsCurrent("plain"));

  // ── personal ────────────────────────────────────────────────────────────

  function openPersonal(d) {
    const fn = (d.name || "").split(" ")[0] || "друг";
    const tpl = {
      bday: `С днём рождения, ${fn}! 🎂💐\n\nОт всей души поздравляем и дарим вам скидку 15% на любой букет всю неделю. Ваш Veresk 🌷`,
      anniv: `${fn}, поздравляем с годовщиной! 💍\n\nОтметьте этот особенный день красивым букетом — дарим −15%. Ваш Veresk 🌷`,
      plain: `Здравствуйте, ${fn}! 🌷\n\n`,
    };
    $("#pAv").textContent = initials(d.name);
    $("#pName").textContent = d.name;
    $("#pContact").innerHTML = `<span class="chan ${esc(d.chanClass)}">${esc(d.chan)}</span> · ${esc(d.contact)}`;
    const ev = $("#pEv");
    ev.textContent = d.evText || "";
    ev.className = "ev-when " + (d.whenClass || "later");
    $("#pmsg").value = tpl[d.type] || tpl.plain;
    updatePPreview();
    $("#pSendLabel").textContent = "Отправить " + fn;
    $("#personalForm")?.classList.remove("hidden");
    $("#personalDone")?.classList.add("hidden");
    state.curPerson = { ...d, fn, chan: d.chan };
    go("personal");
  }
  window.openPersonal = openPersonal;

  function updatePPreview() {
    $("#ppreview").innerHTML = esc($("#pmsg").value).replace(/\n/g, "<br>");
  }
  $("#pmsg")?.addEventListener("input", updatePPreview);

  $("#pSend")?.addEventListener("click", async () => {
    const p = state.curPerson;
    if (!p?.customer_id) return alert("Нет клиента");
    try {
      await AdminAPI.personal({
        customer_id: p.customer_id,
        message: $("#pmsg").value,
        channel: p.chanClass === "max" ? "max" : "tg",
      });
      $("#doneName").textContent = p.fn;
      $("#doneChan").textContent = p.chan;
      $("#personalForm")?.classList.add("hidden");
      $("#personalDone")?.classList.remove("hidden");
    } catch (err) {
      alert("Ошибка: " + (err.data?.error || err.message));
    }
  });

  // ── settings ────────────────────────────────────────────────────────────

  let settingsTab = "accounts";
  let logsFilter = "all";
  let accountsCache = null;

  function setSettingsTab(name) {
    settingsTab = name || "accounts";
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
    setSettingsTab(settingsTab);
    await loadAccounts();
    // статус MAX в шапке — даже если вкладка другая
    try {
      const s = await AdminAPI.maxSettings();
      updateSettingsGlanceMax(!!s.configured);
    } catch (_) {
      updateSettingsGlanceMax(false);
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

  async function loadAccounts() {
    const box = $("#accountsList");
    if (!box) return;
    box.innerHTML = '<div class="loading">Загрузка…</div>';
    try {
      const data = await AdminAPI.accounts();
      accountsCache = data;
      const configured = !!data.telethon_configured;
      const hint = $("#tgHint");
      if (hint) {
        hint.textContent = configured
          ? "Подключите номер: телефон → код из Telegram/SMS → при необходимости пароль 2FA."
          : "Сначала раскройте блок «Ключи Telegram API» ниже, затем подключайте номера.";
      }
      await loadTgApiStatus();
      const tgItems = (data.items || []).filter((a) => a.kind !== "max_bot");
      const ready = tgItems.filter((a) => !["warmup", "unavailable", "blocked"].includes(String(a.status || ""))).length;
      updateTgSetupStatus(configured, tgItems.length, ready);
      updateSettingsGlance(configured, tgItems.length);
      const connectBtn = $("#btnConnectTg");
      if (connectBtn) {
        connectBtn.classList.toggle("is-locked", !configured);
        connectBtn.title = configured ? "Подключить номер" : "Сначала сохраните API-ключи";
      }
      const apiDetails = $("#tgApiForm");
      if (apiDetails && apiDetails.tagName === "DETAILS") {
        apiDetails.open = !configured;
      }
      if (!tgItems.length) {
        box.innerHTML = `<div class="empty-rich" style="padding:28px 16px">
          <div class="er-ic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="6" y="2" width="12" height="20" rx="3"/><path d="M11 18h2"/></svg></div>
          <div class="t">Нет подключённых номеров</div>
          <p class="d">${configured ? "Нажмите «Подключить», чтобы добавить Telegram-аккаунт." : "Сохраните API-ключи в блоке ниже — затем появится кнопка подключения."}</p>
        </div>`;
      } else {
        box.innerHTML = tgItems
          .map((a) => {
            let statusLabel = "Готов";
            let statusColor = "var(--ok)";
            if (a.status === "warmup") {
              statusLabel = a.warmup_until
                ? "Прогрев до " + a.warmup_until
                : "Прогрев";
              statusColor = "var(--warn)";
            } else if (a.status === "unavailable" || a.status === "blocked") {
              statusLabel = a.status;
              statusColor = "var(--ink-3)";
            }
            return `<div class="acct">
              <div class="ico tg">TG</div>
              <div class="m"><div class="n">${esc(a.phone_masked || a.label)}</div><div class="p">Telegram · сегодня ${esc(String(a.sent_today))} из ${esc(String(a.daily_limit))}</div></div>
              <span class="tagi" style="color:${statusColor}"><span class="d" style="background:${statusColor}"></span>${esc(statusLabel)}</span>
            </div>`;
          })
          .join("");
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
    } catch (_) {}
    loadMaxTokenStatus();
    box.innerHTML = maxConfigured
      ? `<span class="status-pill ok"><span class="d"></span>Подключён${maxMeta ? " · " + esc(maxMeta) : ""}</span>`
      : `<span class="status-pill warn"><span class="d"></span>Токен не задан</span>`;
    updateSettingsGlanceMax(maxConfigured);
  }

  function updateTgSetupStatus(configured, total, ready) {
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

  async function loadUsersPane() {
    const box = $("#usersList");
    if (!box) return;
    box.innerHTML = '<div class="loading">Загрузка…</div>';
    try {
      const me = await AdminAPI.me();
      const name = me.username || "admin";
      const saved = localStorage.getItem("veresk_admin_login") || name;
      box.innerHTML = `<div class="user-row">
        <div class="av">${esc(initials(saved))}</div>
        <div style="flex:1;min-width:0">
          <div class="nm">${esc(saved)}</div>
          <div class="rl">Администратор · вход из .env</div>
        </div>
        <span class="badge-soft ok">Активен</span>
      </div>`;
    } catch (err) {
      if (err.status === 401) return showLogin();
      box.innerHTML = '<div class="empty-state">Не удалось загрузить</div>';
    }
  }

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
    let syncLabel = "Статус неизвестен";
    let syncOk = false;
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
          hint: "Ключ с openrouter.ai/keys",
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
      const sync = stats.sync || {};
      if (sync.at) {
        syncOk = !sync.error;
        syncLabel = syncOk
          ? "Последняя синхронизация · " + String(sync.at).replace("T", " ").slice(0, 16)
          : "Ошибка · " + String(sync.error || "unknown");
      } else if (sync.error) {
        syncLabel = "Ошибка · " + String(sync.error);
      } else {
        syncLabel = "Ещё не синхронизировали";
      }
      if (aiSettings) {
        ai = Object.assign(ai, aiSettings);
        if (Array.isArray(aiSettings.providers) && aiSettings.providers.length) {
          ai.providers = aiSettings.providers;
        }
      }
    } catch (_) {}

    const providerLabel =
      (ai.providers || []).find((p) => p.id === ai.provider)?.label || ai.provider || "ИИ";
    const aiBadge = ai.configured
      ? '<span class="badge-soft ok">OK</span>'
      : '<span class="badge-soft warn">Настроить</span>';
    const aiMeta = ai.configured
      ? esc(providerLabel) +
        " · " +
        (ai.from_env ? "ключ из .env · " : "") +
        esc(ai.api_key_masked || "••••") +
        " · " +
        esc(ai.model || "")
      : "Выберите оператора и укажите API-ключ";

    const providerChips = (ai.providers || [])
      .map(
        (p) =>
          `<button type="button" class="ai-prov ${
            p.id === ai.provider ? "on" : ""
          }" data-provider="${esc(p.id)}" data-base="${esc(p.api_base)}" data-model="${esc(
            p.model
          )}" data-hint="${esc(p.hint)}" data-folder="${p.needs_folder ? "1" : "0"}">${esc(
            p.label
          )}</button>`
      )
      .join("");

    const curProv =
      (ai.providers || []).find((p) => p.id === ai.provider) || ai.providers[0] || {};
    const needsFolder = !!curProv.needs_folder || ai.provider === "yandexgpt";
    const showBase = ai.provider === "custom";

    box.innerHTML = `
      <div class="integ-card">
        <div class="integ-card-top">
          <div class="integ-ico pf">PF</div>
          <div style="flex:1;min-width:0">
            <div class="n">Posiflora</div>
            <div class="p">Клиенты, события и заказы для сегментов и поводов написать</div>
          </div>
          <span class="badge-soft ${syncOk ? "ok" : "warn"}">${syncOk ? "OK" : "Настроить"}</span>
        </div>
        <div class="meta">${esc(syncLabel)}</div>
        <div class="integ-actions">
          <button class="btn" type="button" id="integSyncBtn">Синхронизировать сейчас</button>
          <button class="btn" type="button" onclick="go('clients')">Открыть клиентов</button>
        </div>
      </div>
      <div class="integ-card integ-card-form">
        <div class="integ-card-top">
          <div class="integ-ico ai">AI</div>
          <div style="flex:1;min-width:0">
            <div class="n">ИИ-редактор</div>
            <div class="p">Тексты рассылок · OpenAI, OpenRouter, YandexGPT</div>
          </div>
          ${aiBadge}
        </div>
        <div class="meta" id="aiSettingsMeta">${aiMeta}</div>
        <div class="integ-ai-form" id="aiSettingsForm">
          <label>Оператор</label>
          <div class="ai-prov-row" id="aiProviderRow">${providerChips}</div>
          <p class="ai-prov-hint" id="aiProvHint">${esc(curProv.hint || "")}</p>
          <label for="aiApiKey">API-ключ</label>
          <input id="aiApiKey" type="password" autocomplete="off" placeholder="${
            ai.configured
              ? "Оставьте пустым, чтобы не менять · " + esc(ai.api_key_masked || "••••")
              : "Вставьте ключ оператора"
          }">
          <div class="form-grid-2" id="aiFolderRow" ${needsFolder ? "" : "hidden"}>
            <div style="grid-column:1/-1">
              <label for="aiFolderId">Folder ID (Yandex Cloud)</label>
              <input id="aiFolderId" type="text" autocomplete="off" value="${esc(
                ai.folder_id || ""
              )}" placeholder="b1g…">
            </div>
          </div>
          <div class="form-grid-2">
            <div id="aiBaseWrap" ${showBase ? "" : "hidden"}>
              <label for="aiApiBase">Базовый URL</label>
              <input id="aiApiBase" type="url" autocomplete="off" value="${esc(
                ai.api_base || ""
              )}" placeholder="https://…/v1">
            </div>
            <div ${showBase ? "" : 'style="grid-column:1/-1"'}>
              <label for="aiModel">Модель</label>
              <input id="aiModel" type="text" autocomplete="off" value="${esc(
                ai.model || ""
              )}" placeholder="${esc(curProv.model || "model")}">
            </div>
          </div>
          <div class="form-actions">
            <button type="button" class="btn primary" id="aiSettingsSave">Сохранить</button>
            <button type="button" class="btn" id="aiSettingsClear" ${
              ai.configured ? "" : "disabled"
            }>Отключить</button>
            <button type="button" class="btn" onclick="go('aichat')">ИИ чат</button>
          </div>
          <p class="form-foot">OpenRouter — модели вида <code>openai/gpt-4o-mini</code>. YandexGPT — <code>yandexgpt-lite/latest</code> или <code>yandexgpt/latest</code>.</p>
        </div>
      </div>
      <div class="integ-card">
        <div class="integ-card-top">
          <div class="integ-ico tg">TG</div>
          <div style="flex:1;min-width:0">
            <div class="n">Каналы отправки</div>
            <div class="p">Telegram-номера и MAX-бот настраиваются в соседних разделах</div>
          </div>
          <span class="badge-soft muted">Быстрый переход</span>
        </div>
        <div class="integ-actions">
          <button class="btn" type="button" data-goto-settings="accounts">Telegram</button>
          <button class="btn" type="button" data-goto-settings="bots">MAX</button>
        </div>
      </div>`;

    let selectedProvider = ai.provider || "openai";

    function applyProviderUi(btn) {
      selectedProvider = btn.dataset.provider;
      $$("#aiProviderRow .ai-prov").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      const hint = $("#aiProvHint");
      if (hint) hint.textContent = btn.dataset.hint || "";
      const model = $("#aiModel");
      if (model && (!model.value || model.dataset.autofill !== "0")) {
        model.value = btn.dataset.model || "";
      }
      const base = $("#aiApiBase");
      if (base) base.value = btn.dataset.base || "";
      const folderRow = $("#aiFolderRow");
      if (folderRow) folderRow.hidden = btn.dataset.folder !== "1";
      const baseWrap = $("#aiBaseWrap");
      if (baseWrap) baseWrap.hidden = selectedProvider !== "custom";
      const modelWrap = baseWrap?.parentElement?.querySelector("#aiModel")?.parentElement;
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
        btn.textContent = "Синхронизировать сейчас";
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
        if (!body.api_key && !ai.configured) {
          alert("Укажите API-ключ");
        } else if (selectedProvider === "yandexgpt" && !body.folder_id && !ai.folder_id) {
          alert("Для YandexGPT укажите Folder ID");
        } else {
          await AdminAPI.aiSaveSettings(body);
          alert("Настройки ИИ сохранены");
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
      if (!confirm("Отключить ИИ в панели? Останется ключ из .env, если он задан.")) return;
      try {
        await AdminAPI.aiSaveSettings({ clear: true });
        loadIntegrationsPane();
      } catch (err) {
        alert(err.data?.detail || err.message || "Ошибка");
      }
    });

    box.querySelectorAll("[data-goto-settings]").forEach((b) => {
      b.addEventListener("click", () => setSettingsTab(b.dataset.gotoSettings));
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

  async function loadMaxTokenStatus() {
    const box = $("#maxTokenStatus");
    if (!box) return;
    try {
      const s = await AdminAPI.maxSettings();
      if (s.configured) {
        const bits = [];
        if (s.from_env) bits.push(".env");
        if (s.bot_username) bits.push("@" + s.bot_username);
        else if (s.bot_name) bits.push(s.bot_name);
        if (s.token_masked) bits.push(s.token_masked);
        box.innerHTML =
          '<span class="status-pill ok"><span class="d"></span>Активен' +
          (bits.length ? " · " + esc(bits.join(" · ")) : "") +
          "</span>";
      } else {
        box.innerHTML =
          '<span class="status-pill warn"><span class="d"></span>Вставьте токен и нажмите «Сохранить»</span>';
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

  $("#maxTokenSave")?.addEventListener("click", async () => {
    const token = $("#maxBotToken").value.trim();
    if (!token) return alert("Вставьте токен от @MasterBot");
    try {
      const res = await AdminAPI.maxSaveSettings(token);
      if (!res.ok) return alert(res.detail || res.error || "Ошибка");
      $("#maxBotToken").value = "";
      const who = res.bot_username
        ? "@" + res.bot_username
        : res.bot_name || "бот";
      alert("MAX подключён: " + who);
      loadAccounts();
      loadBotsPane();
    } catch (err) {
      const msg =
        err.data?.detail ||
        (err.data?.error === "invalid_token"
          ? "Неверный токен — проверьте у @MasterBot"
          : err.data?.error || err.message);
      alert(msg);
    }
  });

  $("#maxTokenClear")?.addEventListener("click", async () => {
    if (!confirm("Отключить токен MAX из панели? (значение из .env останется)"))
      return;
    try {
      await AdminAPI.maxClearSettings();
      $("#maxBotToken").value = "";
      loadAccounts();
      loadBotsPane();
    } catch (err) {
      alert(err.data?.error || err.message);
    }
  });

  function setConnectStep(step) {
    $$(".connect-step").forEach((el) => {
      el.classList.toggle("on", Number(el.dataset.cstep) <= step);
    });
  }

  function openConnectForm(show) {
    const form = $("#acctForm");
    if (!form) return;
    form.classList.toggle("hidden", !show);
    if (show) {
      setConnectStep(1);
      $("#tgPhone")?.focus();
    } else {
      $("#tgCodeStep")?.classList.add("hidden");
      $("#tg2faWrap")?.classList.add("hidden");
      setConnectStep(1);
    }
  }

  $("#btnConnectTg")?.addEventListener("click", () => {
    const locked = $("#btnConnectTg")?.classList.contains("is-locked");
    if (locked) {
      const api = $("#tgApiForm");
      if (api && api.tagName === "DETAILS") api.open = true;
      api?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      alert("Сначала сохраните ключи Telegram API в блоке ниже.");
      return;
    }
    openConnectForm($("#acctForm")?.classList.contains("hidden"));
  });

  $("#tgConnectCancel")?.addEventListener("click", () => openConnectForm(false));

  $("#tgSendCode")?.addEventListener("click", async () => {
    const phone = $("#tgPhone").value.trim();
    if (!phone) return alert("Укажите телефон");
    try {
      const res = await AdminAPI.tgStart(phone);
      if (!res.ok) return alert(res.error || "Ошибка");
      state.tgPhone = res.phone || phone;
      $("#tgCodeStep").classList.remove("hidden");
      setConnectStep(2);
      $("#tgCode")?.focus();
      alert("Код отправлен в Telegram / SMS");
    } catch (err) {
      alert(err.data?.error || err.message);
    }
  });

  $("#tgConfirm")?.addEventListener("click", async () => {
    const code = $("#tgCode").value.trim();
    const password = $("#tg2fa").value.trim() || undefined;
    try {
      const res = await AdminAPI.tgConfirm(state.tgPhone, code, password);
      if (res.need_2fa) {
        $("#tg2faWrap").classList.remove("hidden");
        alert("Введите пароль двухфакторной аутентификации");
        return;
      }
      if (!res.ok) return alert(res.error || "Ошибка");
      alert("Аккаунт подключён");
      $("#acctForm").classList.add("hidden");
      loadAccounts();
    } catch (err) {
      alert(err.data?.error || err.message);
    }
  });

  // ── wizard ──────────────────────────────────────────────────────────────

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
      const on = $("#s0 .choice.on");
      if (on) $("#sumWho").textContent = fmtNum(on.dataset.count) + " клиентов";
    } catch (_) {}
  }

  $$("#s0 .choice").forEach((c) =>
    c.addEventListener("click", () => {
      $$("#s0 .choice").forEach((x) => x.classList.remove("on"));
      c.classList.add("on");
      state.wizard.segment = c.dataset.seg;
      $("#sumWho").textContent = fmtNum(c.dataset.count) + " клиентов";
    })
  );

  $$("#s2 .choice").forEach((c) =>
    c.addEventListener("click", () => {
      $$("#s2 .choice").forEach((x) => x.classList.remove("on"));
      c.classList.add("on");
      state.wizard.when = c.dataset.when;
      $("#datebox").style.display = c.dataset.when === "later" ? "flex" : "none";
    })
  );

  const msgTa = $("#msg");
  function updatePreview() {
    $("#msgPreview").innerHTML = esc(msgTa.value)
      .replace(/\{имя\}/g, "Мария")
      .replace(/\n/g, "<br>");
  }
  msgTa?.addEventListener("input", updatePreview);
  $$(".ins").forEach((b) =>
    b.addEventListener("click", () => {
      const map = {
        "+ Имя клиента": "{имя}",
        "+ Скидка": "{скидка}",
        "+ Ссылка": "veresk.flowers",
      };
      msgTa.value += " " + (map[b.textContent] || "");
      msgTa.focus();
      updatePreview();
    })
  );

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
      // на мобиле прокрутить к панели
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
    const segment = $("#s0 .choice.on")?.dataset.seg || state.wizard.segment || "all";
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
      setAiStatus("Готово — текст вставлен в сообщение. Справа — превью.", "ok");
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

  // дата по умолчанию — завтра
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
    wnext.textContent = i === 2 ? "Запланировать" : "Далее";
    if (i === 0) refreshSegmentCounts();
    if (i === 2) {
      const when = state.wizard.when;
      const sumWhen = $("#sumWhen");
      if (when === "now") sumWhen.textContent = "сейчас";
      else
        sumWhen.textContent =
          ($("#wizDate").value || "") + ", " + ($("#wizTime").value || "10:00");
    }
  }

  wnext?.addEventListener("click", async () => {
    if (state.step < 2) {
      setStep(state.step + 1);
      return;
    }
    // создать кампанию
    const segBtn = $("#s0 .choice.on");
    const segment = segBtn?.dataset.seg || "all";
    const sendNow = state.wizard.when === "now" || $("#s2 .choice.on")?.dataset.when === "now";
    let scheduled_at = null;
    if (!sendNow) {
      scheduled_at = `${$("#wizDate").value}T${$("#wizTime").value || "10:00"}:00`;
    }
    const title =
      msgTa.value.split("\n")[0].slice(0, 60).replace(/\{имя\}/g, "").trim() ||
      "Рассылка";
    try {
      wnext.disabled = true;
      await AdminAPI.createCampaign({
        title,
        message: msgTa.value,
        segment,
        channels: "tg",
        emoji: "🌷",
        send_now: sendNow,
        scheduled_at,
      });
      setStep(3);
      $("#successText").textContent = sendNow
        ? "Рассылка запущена. Сообщения уходят порциями."
        : `Сообщение уйдёт ${fmtNum(segBtn?.dataset.count || 0)} клиентам ${scheduled_at || ""}.`;
    } catch (err) {
      alert("Ошибка: " + (err.data?.error || err.message));
    }
    wnext.disabled = false;
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
    try {
      const data = await AdminAPI.events(60);
      renderEvents(data.items || []);
    } catch (_) {}
  });

  // ── AI chat (scaffold) ───────────────────────────────────────────────────

  let aiChatReady = false;

  function initAiChat() {
    if (aiChatReady) return;
    aiChatReady = true;
    const form = $("#aiChatForm");
    const input = $("#aiInput");
    form?.addEventListener("submit", (e) => {
      e.preventDefault();
      // Backend подключается позже
    });
    input?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) e.preventDefault();
    });
    $("#aiClearChat")?.addEventListener("click", () => {
      const box = $("#aiMessages");
      if (!box) return;
      box.innerHTML = `
        <div class="ai-msg ai-msg-bot">
          <div class="ai-bubble">
            <div class="ai-bubble-label">Veresk ИИ</div>
            <p>Диалог очищен. Чат для аналитики и помощи появится здесь после подключения ИИ.</p>
          </div>
        </div>`;
    });
  }

  // boot
  tryAuth();
})();
