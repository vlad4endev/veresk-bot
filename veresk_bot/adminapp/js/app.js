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
  const navItems = $$(".nav-item");

  function go(tab) {
    panels.forEach((p) => p.classList.toggle("active", p.id === tab));
    const navKey =
      ({ compose: "home", detail: "home", personal: "home", client: "clients" })[tab] ||
      tab;
    navItems.forEach((n) => n.classList.toggle("active", n.dataset.nav === navKey));
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (tab === "compose") setStep(0);
    if (tab === "home") loadHome();
    if (tab === "clients") loadClients();
    if (tab === "accounts") loadAccounts();
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

  $("#loginForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const pwd = $("#loginPassword").value;
    const errEl = $("#loginErr");
    errEl.style.display = "none";
    try {
      const res = await AdminAPI.login(pwd);
      AdminAPI.setToken(res.token);
      await showApp();
    } catch (err) {
      errEl.textContent = "Неверный пароль";
      errEl.style.display = "block";
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
    if (!items.length) {
      box.innerHTML =
        '<div class="empty-state"><div class="t">Пока нет рассылок</div>Создайте первую</div>';
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

    $("#detailBody").innerHTML = `
      <div class="dhead">
        <span class="em">${esc(c.emoji)}</span>
        <div><div class="n">${esc(c.title)}</div></div>
        <div class="spacer"></div>
        <span class="status ${esc(c.status_class)}">${esc(c.status_label)}</span>
      </div>
      <div class="dmeta">
        <div class="mi"><div class="k">Когда</div><div class="v">${esc(c.when)}</div></div>
        <div class="mi"><div class="k">Кому</div><div class="v">${esc(c.segment_label)} · ${fmtNum(c.total_count)} чел.</div></div>
        <div class="mi"><div class="k">Где</div><div class="v">${chans}</div></div>
      </div>
      <div class="subh" style="margin-bottom:10px">Текст сообщения</div>
      <div class="phone msgcard">
        <div class="ptop"><div class="dot">V</div>Veresk</div>
        <div class="bubble">${msgHtml}<div class="tm">${sent ? "✓✓" : ""}</div></div>
      </div>
      ${
        sent
          ? `<div class="det-actions">
              <button class="btn primary" id="btnRepeat">Повторить рассылку</button>
              ${c.status === "sending" ? "" : ""}
            </div>
            <div class="subh">Как дошло</div>
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
          : `<div class="notsent">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16h.01"/></svg>
              <div>${c.status === "scheduled" ? "Рассылка запланирована. " : "Рассылка ещё не отправлена. "}Получатели и статистика появятся после отправки.</div>
            </div>
            <div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:16px">
              <button class="btn primary big" id="btnSendNow">Отправить сейчас</button>
              <button class="btn big" onclick="go('compose')">Редактировать</button>
            </div>`
      }`;

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

  async function loadClients() {
    const box = $("#clientsBody");
    box.innerHTML = '<tr><td colspan="5" class="loading">Загрузка…</td></tr>';
    try {
      const data = await AdminAPI.clients({
        segment: clientSegment,
        page_size: 100,
      });
      if (!data.items.length) {
        box.innerHTML =
          '<tr><td colspan="5"><div class="empty-state">Клиентов пока нет — нажмите «Синхронизировать»</div></td></tr>';
        $("#clientsHint").textContent = "0 клиентов";
        return;
      }
      box.innerHTML = data.items
        .map(
          (c) => `<tr data-id="${c.id}">
          <td class="who"><div class="nm">${esc(c.name)}</div></td>
          <td class="hide-mob">${esc(c.phone_masked)}</td>
          <td>${String(c.channels)
            .split(",")
            .map((ch) => {
              const t = ch.trim();
              if (!t) return "";
              return `<span class="chan ${t === "MAX" ? "max" : "tg"}">${esc(t)}</span>`;
            })
            .join(" ")}</td>
          <td>${esc(c.last_order_label)}</td>
          <td class="hide-mob">${esc(c.segment_label)}</td>
        </tr>`
        )
        .join("");
      box.querySelectorAll("tr[data-id]").forEach((tr) => {
        tr.addEventListener("click", () => openClientById(+tr.dataset.id));
      });
      $("#clientsHint").textContent = `Показано ${data.items.length} из ${fmtNum(data.total)}`;
    } catch (err) {
      if (err.status === 401) return showLogin();
      box.innerHTML = '<tr><td colspan="5">Ошибка загрузки</td></tr>';
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

  $("#btnSync")?.addEventListener("click", async () => {
    const btn = $("#btnSync");
    btn.disabled = true;
    btn.textContent = "Синхронизация…";
    try {
      const res = await AdminAPI.sync();
      alert(
        res.ok
          ? `Готово: ${res.customers} клиентов, ${res.events} событий`
          : "Ошибка: " + (res.error || "unknown")
      );
      await loadClients();
    } catch (err) {
      alert("Ошибка синхронизации: " + (err.data?.error || err.message));
    }
    btn.disabled = false;
    btn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v12M8 11l4 4 4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg> Синхронизировать из Posiflora';
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
      if (c.phone)
        chips += `<span class="contact-chip"><span class="ci2 ph">☎</span>${esc(c.phone)}</span>`;
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
      $("#clAnnivBtn").style.display = anniv ? "inline-block" : "none";
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
    $("#personalForm").style.display = "block";
    $("#personalDone").style.display = "none";
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
      $("#personalForm").style.display = "none";
      $("#personalDone").style.display = "block";
    } catch (err) {
      alert("Ошибка: " + (err.data?.error || err.message));
    }
  });

  // ── accounts ────────────────────────────────────────────────────────────

  async function loadAccounts() {
    const box = $("#accountsList");
    box.innerHTML = '<div class="loading">Загрузка…</div>';
    try {
      const data = await AdminAPI.accounts();
      if (!data.telethon_configured) {
        $("#tgHint").textContent =
          "Задайте TELEGRAM_API_ID и TELEGRAM_API_HASH в .env, чтобы подключать номера.";
      } else {
        $("#tgHint").textContent =
          "Подключите личный номер Telegram: телефон → код из SMS → (при необходимости) пароль 2FA.";
      }
      box.innerHTML = (data.items || [])
        .map((a) => {
          const isMax = a.kind === "max_bot";
          const ico = isMax ? "MX" : "TG";
          const icoCls = isMax ? "max" : "tg";
          let statusLabel = "Готов";
          let statusColor = "var(--ok)";
          if (a.status === "warmup") {
            statusLabel = a.warmup_until
              ? "Прогрев до " + a.warmup_until
              : "Прогрев";
            statusColor = "var(--warn)";
          } else if (a.status === "unavailable" || a.status === "blocked") {
            statusLabel = isMax && a.placeholder ? "Не подключён" : a.status;
            statusColor = "var(--ink-3)";
          }
          const sub = isMax
            ? a.placeholder
              ? "MAX · зарезервировано под будущего бота"
              : `MAX · сегодня ${a.sent_today} из ${a.daily_limit}`
            : `Telegram · сегодня ${a.sent_today} из ${a.daily_limit}`;
          return `<div class="acct">
            <div class="ico ${icoCls}">${ico}</div>
            <div class="m"><div class="n">${esc(a.phone_masked || a.label)}</div><div class="p">${esc(sub)}</div></div>
            <span class="tagi" style="color:${statusColor}"><span class="d" style="background:${statusColor}"></span>${esc(statusLabel)}</span>
          </div>`;
        })
        .join("");
    } catch (err) {
      if (err.status === 401) return showLogin();
      box.innerHTML = '<div class="empty-state">Ошибка загрузки</div>';
    }
  }

  $("#btnConnectTg")?.addEventListener("click", () => {
    $("#acctForm").classList.toggle("hidden");
  });

  $("#tgSendCode")?.addEventListener("click", async () => {
    const phone = $("#tgPhone").value.trim();
    if (!phone) return alert("Укажите телефон");
    try {
      const res = await AdminAPI.tgStart(phone);
      if (!res.ok) return alert(res.error || "Ошибка");
      state.tgPhone = res.phone || phone;
      $("#tgCodeStep").classList.remove("hidden");
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

  $("#wgAll")?.addEventListener("click", async () => {
    try {
      const data = await AdminAPI.events(60);
      renderEvents(data.items || []);
    } catch (_) {}
  });

  // boot
  tryAuth();
})();
