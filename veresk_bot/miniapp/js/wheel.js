/* Mini App — экран колеса фортуны (1 спин после анкеты → окно с призом) */

(function () {
  const DEFAULT_CONFIG = {
    title: "Розыгрыш месяца",
    note: "Анкета пройдена. Крутите один раз и забирайте подарок.",
    segments: [
      { id: "s1", label: "Скидка 10%", color: "#FF92CE", weight: 30 },
      { id: "s2", label: "Скидка 15%", color: "#402C60", weight: 18 },
      { id: "s3", label: "Бесплатная доставка", color: "#FFFFFF", weight: 22 },
      { id: "s4", label: "Попробуйте ещё", color: "#FF92CE", weight: 20 },
      { id: "s5", label: "Мини-букет", color: "#402C60", weight: 10 },
    ],
  };

  const LOGO_SRC = "assets/logo-circle.png?v=6";

  let widget = null;
  let lastPrize = null;
  let cachedConfig = null;
  let cachedPlay = null;
  let loading = null;
  let mounting = null;

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function detectChannel() {
    const maxApp = window.WebApp;
    if (maxApp?.initData && !window.Telegram?.WebApp?.initData) return "max";
    if (window.Telegram?.WebApp?.initData) return "telegram";
    if (maxApp?.initData) return "max";
    return "telegram";
  }

  function authHeaders() {
    const channel = detectChannel();
    const headers = { "Content-Type": "application/json" };
    const tgInit = window.Telegram?.WebApp?.initData || "";
    const maxInit = window.WebApp?.initData || "";
    if (tgInit) headers["X-Telegram-Init-Data"] = tgInit;
    if (maxInit) headers["X-Max-Init-Data"] = maxInit;
    return { channel, headers };
  }

  function formatPrizeLabel(label, discountPct) {
    const text = String(label || "").trim() || "Приз";
    const pct = Number(discountPct);
    if (Number.isFinite(pct) && pct > 0 && !text.includes("%")) {
      return `${text} (−${pct}%)`;
    }
    return text;
  }

  function isRetryPrize(segmentOrPlay) {
    const id = String(segmentOrPlay?.prize_id || segmentOrPlay?.id || "")
      .trim()
      .toLowerCase();
    if (id === "s4" || id === "retry" || id === "try_again" || id === "try-again") {
      return true;
    }
    const label = String(segmentOrPlay?.prize_label || segmentOrPlay?.label || "")
      .trim()
      .toLowerCase()
      .replace(/ё/g, "е");
    if (!label) return false;
    if (label.includes("попробуй") && label.includes("ещ")) return true;
    if (label.includes("try again") || label.includes("try_again")) return true;
    return false;
  }

  function setWheelHeader(mode) {
    const screen = document.getElementById("screen-wheel");
    const header = document.querySelector("#screen-wheel .screen-header");
    if (screen) screen.classList.toggle("is-prize", mode === "prize");
    /* шапка экрана скрыта — брендовый UI внутри виджета */
    if (header) header.hidden = true;
  }

  function destroyWidget() {
    if (widget) {
      try {
        widget.destroy?.();
      } catch (_) {
        /* ignore */
      }
      widget = null;
    }
  }

  function mountRoot() {
    return document.getElementById("miniWheelMount");
  }

  function showPrizePanel(play, opts) {
    const root = mountRoot();
    if (!root) return;
    destroyWidget();

    const prizeLabel = formatPrizeLabel(
      play?.prize_label || play?.label || opts?.segment?.label,
      play?.discount_pct ?? opts?.discount_pct
    );

    cachedPlay = play || cachedPlay;
    lastPrize = {
      id: play?.prize_id || opts?.segment?.id || "",
      label: prizeLabel,
    };
    setWheelHeader("prize");

    root.innerHTML = `
      <div class="vw-prize-panel" role="status" aria-live="polite">
        <div class="vw-prize-hero">
          <div class="vw-prize-logo-wrap">
            <img class="vw-prize-logo" src="${esc(LOGO_SRC)}" alt="Veresk" width="88" height="88" decoding="async">
          </div>
        </div>
        <p class="vw-prize-hi">Ваш подарок</p>
        <h2 class="vw-prize-name">${esc(prizeLabel)}</h2>
        <p class="vw-prize-note">Покажите этот экран флористу при заказе</p>
        <p class="vw-prize-brand">trail of happiness</p>
      </div>
    `;
  }

  async function fetchWheelConfig(force) {
    if (!force && cachedConfig) return cachedConfig;
    if (loading) return loading;
    loading = (async () => {
      try {
        const resp = await fetch("/api/wheel", { credentials: "same-origin" });
        if (!resp.ok) throw new Error("wheel_http_" + resp.status);
        const data = await resp.json();
        if (!data || !Array.isArray(data.segments) || data.segments.length < 2) {
          throw new Error("wheel_bad_payload");
        }
        cachedConfig = data;
        return cachedConfig;
      } catch (err) {
        console.warn("[wheel] config fallback", err);
        if (!cachedConfig) cachedConfig = DEFAULT_CONFIG;
        return cachedConfig;
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  async function fetchMyPlay() {
    const { channel, headers } = authHeaders();
    if (!headers["X-Telegram-Init-Data"] && !headers["X-Max-Init-Data"]) {
      return { played: false, play: null, unauthorized: true };
    }
    try {
      const resp = await fetch(`/api/wheel/me?channel=${encodeURIComponent(channel)}`, {
        credentials: "same-origin",
        headers,
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.status === 401) {
        return { played: false, play: null, unauthorized: true };
      }
      if (!resp.ok) {
        throw new Error(data.detail || data.error || "me_failed");
      }
      return {
        played: Boolean(data.played),
        play: data.play || null,
        unauthorized: false,
      };
    } catch (err) {
      console.warn("[wheel] me", err);
      return { played: false, play: null, unauthorized: false };
    }
  }

  async function requestSpin() {
    const { channel, headers } = authHeaders();
    const resp = await fetch("/api/wheel/spin", {
      method: "POST",
      credentials: "same-origin",
      headers,
      body: JSON.stringify({ channel }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 409 || data.already_played) {
      const err = new Error(data.detail || "already_played");
      err.status = 409;
      err.code = "already_played";
      err.data = data;
      throw err;
    }
    if (!resp.ok) {
      const err = new Error(data.detail || data.error || "spin_failed");
      err.status = resp.status;
      err.code = data.error || "";
      err.data = data;
      throw err;
    }
    return data;
  }

  async function requestPrizeNotify() {
    try {
      const { channel, headers } = authHeaders();
      await fetch("/api/wheel/notify", {
        method: "POST",
        credentials: "same-origin",
        headers,
        body: JSON.stringify({ channel }),
      });
    } catch (err) {
      console.warn("[wheel] notify", err);
    }
  }

  function mountWheel(config) {
    const root = mountRoot();
    if (!root || !window.VereskWheel?.create) return null;
    const cfg = config || cachedConfig || DEFAULT_CONFIG;
    destroyWidget();
    root.innerHTML = "";
    setWheelHeader("wheel");

    widget = window.VereskWheel.create(root, {
      ...cfg,
      once: true,
      async resolveWinner() {
        try {
          const result = await requestSpin();
          if (result.config) {
            cachedConfig = result.config;
            widget.setConfig(result.config);
          }
          const retry = Boolean(result.retry) || isRetryPrize(result.segment);
          if (retry) {
            cachedPlay = null;
          } else {
            cachedPlay = result.play || cachedPlay;
          }
          return { winnerIndex: result.winner_index, retry };
        } catch (err) {
          if (err.code === "already_played" || err.status === 409) {
            const play = err.data?.play || {
              prize_label: err.data?.segment?.label,
              prize_id: err.data?.segment?.id,
              discount_pct: err.data?.discount_pct,
            };
            if (isRetryPrize(play) || isRetryPrize(err.data?.segment)) {
              cachedPlay = null;
              return null;
            }
            showPrizePanel(play, {
              segment: err.data?.segment,
              discount_pct: err.data?.discount_pct,
            });
            return null;
          }
          const code = err.data?.error || err.code || "";
          const msg =
            err.status === 401
              ? "Откройте колесо из Telegram или MAX"
              : code === "survey_required"
                ? err.data?.detail ||
                  "Сначала заполните анкету в боте — после неё откроется колесо"
                : err.data?.detail || err.message || "Не удалось крутить";
          alert(msg);
          throw err;
        }
      },
      onSpinEnd(segment, _index, meta) {
        lastPrize = segment;
        const retry = Boolean(meta?.retry) || isRetryPrize(segment);
        try {
          window.tg?.HapticFeedback?.notificationOccurred?.(retry ? "warning" : "success");
          window.WebApp?.HapticFeedback?.notificationOccurred?.(retry ? "warning" : "success");
        } catch (_) {
          /* ignore */
        }
        if (retry) {
          cachedPlay = null;
          return;
        }
        // После анимации — окно с призом и поздравление в боте
        window.setTimeout(() => {
          showPrizePanel(
            cachedPlay || {
              prize_id: segment?.id,
              prize_label: segment?.label,
              discount_pct: null,
            },
            { segment }
          );
          requestPrizeNotify();
        }, 900);
      },
    });
    return widget;
  }

  async function mountScreen(config, force) {
    if (mounting) return mounting;
    mounting = (async () => {
      const cfg = config || (await fetchWheelConfig(Boolean(force)));
      const me = await fetchMyPlay();
      if (me.played && me.play) {
        showPrizePanel(me.play);
        return null;
      }
      return mountWheel(cfg);
    })();
    try {
      return await mounting;
    } finally {
      mounting = null;
    }
  }

  async function openWheelScreen(config) {
    if (config) cachedConfig = config;
    if (typeof window.goTo === "function") {
      window.goTo("wheel");
      return;
    }
    await mountScreen(config, true);
  }

  window.VereskFortuneWheel = {
    DEFAULT_CONFIG,
    mount: async (config) => mountScreen(config, false),
    open: openWheelScreen,
    getWidget: () => widget,
    getLastPrize: () => lastPrize,
    getPlay: () => cachedPlay,
    reload: async () => {
      cachedConfig = null;
      cachedPlay = null;
      return mountScreen(null, true);
    },
  };
})();
