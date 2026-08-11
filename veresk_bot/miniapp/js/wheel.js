/* Mini App — экран колеса фортуны (конфиг пока локальный, позже с API админки) */

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
