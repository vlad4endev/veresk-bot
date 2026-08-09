/* Mini App — экран колеса фортуны (конфиг пока локальный, позже с API админки) */

(function () {
  const DEFAULT_CONFIG = {
    title: "Весенний розыгрыш",
    note: "Крутите колесо — получите подарок от Veresk",
    segments: [
      { id: "s1", label: "Скидка 10%", color: "#d64593", weight: 30 },
      { id: "s2", label: "Скидка 15%", color: "#3a2558", weight: 18 },
      { id: "s3", label: "Бесплатная доставка", color: "#e86aad", weight: 22 },
      { id: "s4", label: "Попробуйте ещё", color: "#5a3d7a", weight: 20 },
      { id: "s5", label: "Мини-букет", color: "#c43d86", weight: 10 },
    ],
  };

  let widget = null;
  let lastPrize = null;

  function mountWheel(config) {
    const root = document.getElementById("miniWheelMount");
    if (!root || !window.VereskWheel?.create) return null;
    if (widget) {
      widget.setConfig(config || DEFAULT_CONFIG);
      return widget;
    }
    widget = window.VereskWheel.create(root, {
      ...(config || DEFAULT_CONFIG),
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

  function openWheelScreen(config) {
    mountWheel(config || DEFAULT_CONFIG);
    if (typeof window.goTo === "function") window.goTo("wheel");
  }

  window.VereskFortuneWheel = {
    DEFAULT_CONFIG,
    mount: mountWheel,
    open: openWheelScreen,
    getWidget: () => widget,
    getLastPrize: () => lastPrize,
  };

  document.getElementById("wheel-back")?.addEventListener("click", () => {
    if (typeof window.goTo === "function") window.goTo("home");
  });
})();
