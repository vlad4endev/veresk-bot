/* Mini App — экран колеса фортуны */

(function () {
  const DEFAULT_CONFIG = {
    title: "Весенний розыгрыш",
    note: "Крутите колесо — получите подарок от Veresk",
    segments: [
      { id: "s1", label: "Скидка 10%", color: "#E879B0", weight: 30 },
      { id: "s2", label: "Скидка 15%", color: "#3D2A55", weight: 18 },
      { id: "s3", label: "Бесплатная доставка", color: "#F3C4DC", weight: 22 },
      { id: "s4", label: "Попробуйте ещё", color: "#6B4C8A", weight: 20 },
      { id: "s5", label: "Мини-букет", color: "#D4569A", weight: 10 },
    ],
  };

  let widget = null;
  let lastPrize = null;
  let cachedConfig = null;
  let loading = null;

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

  function mountWheel(config) {
    const root = document.getElementById("miniWheelMount");
    if (!root || !window.VereskWheel?.create) return null;
    const cfg = config || cachedConfig || DEFAULT_CONFIG;
    if (widget) {
      widget.setConfig(cfg);
      return widget;
    }
    widget = window.VereskWheel.create(root, {
      ...cfg,
      onSpinEnd(segment) {
        lastPrize = segment;
        try {
          window.tg?.HapticFeedback?.notificationOccurred?.("success");
        } catch (_) {
          /* ignore */
        }
      },
    });
    return widget;
  }

  async function openWheelScreen(config) {
    const cfg = config || (await fetchWheelConfig(true));
    mountWheel(cfg);
    if (typeof window.goTo === "function") window.goTo("wheel");
  }

  window.VereskFortuneWheel = {
    DEFAULT_CONFIG,
    mount: async (config) => {
      const cfg = config || (await fetchWheelConfig(true));
      return mountWheel(cfg);
    },
    open: openWheelScreen,
    getWidget: () => widget,
    getLastPrize: () => lastPrize,
    reload: async () => {
      cachedConfig = null;
      const cfg = await fetchWheelConfig(true);
      return mountWheel(cfg);
    },
  };

  document.getElementById("wheel-back")?.addEventListener("click", () => {
    if (typeof window.goTo === "function") window.goTo("home");
  });
})();
