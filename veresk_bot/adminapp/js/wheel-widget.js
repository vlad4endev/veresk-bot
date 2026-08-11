/**
 * Veresk fortune wheel — общий виджет для Mini App и превью в админке.
 * API: VereskWheel.create(rootEl, { title, note, segments, onSpinEnd })
 */
(function (global) {
  "use strict";

  // Мягкая палитра Veresk (светлый / тёмный чередуются)
  const DEFAULT_COLORS = [
    "#E879B0",
    "#3D2A55",
    "#F3C4DC",
    "#6B4C8A",
    "#D4569A",
    "#52406A",
    "#F0A8CB",
    "#2A1B3D",
  ];

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  const DEFAULT_LOGO = "assets/logo-circle.png?v=6";

  function totalWeight(segs) {
    return segs.reduce((sum, s) => sum + Math.max(0, Number(s.weight) || 0), 0);
  }

  function normalizeSegments(raw) {
    const list = Array.isArray(raw) ? raw : [];
    return list.map((s, i) => ({
      id: String(s.id || `seg-${i}`),
      label: String(s.label || "").trim() || `Приз ${i + 1}`,
      color: s.color || DEFAULT_COLORS[i % DEFAULT_COLORS.length],
      weight: Math.max(0, Number(s.weight) || 0),
    }));
  }

  function polar(cx, cy, r, deg) {
    const a = ((deg - 90) * Math.PI) / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  }

  function slicePath(cx, cy, r, startDeg, endDeg) {
    const large = endDeg - startDeg > 180 ? 1 : 0;
    const [x1, y1] = polar(cx, cy, r, startDeg);
    const [x2, y2] = polar(cx, cy, r, endDeg);
    if (endDeg - startDeg >= 359.9) {
      return `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx} ${cy + r} A ${r} ${r} 0 1 1 ${cx} ${cy - r} Z`;
    }
    return `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
  }

  function contrastText(hex) {
    const h = String(hex || "").replace("#", "");
    if (h.length !== 6) return "#ffffff";
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return lum > 0.58 ? "#2A1B3D" : "#ffffff";
  }

  function shortenLabel(label, span) {
    const raw = String(label || "").trim();
    if (span < 40) return raw.slice(0, 10);
    if (span < 55) return raw.slice(0, 14);
    return raw.slice(0, 18);
  }

  /** Визуально все сектора равные — вес влияет только на шанс. */
  function visualSlices(segments) {
    const n = Math.max(segments.length, 1);
    const span = 360 / n;
    return segments.map((s, i) => ({
      segment: s,
      index: i,
      startDeg: i * span,
      spanDeg: span,
      midDeg: i * span + span / 2,
    }));
  }

  function labelLines(text, span) {
    const raw = shortenLabel(text, span);
    if (raw.length <= 9 || span < 48) return [raw];
    const parts = raw.split(/\s+/);
    if (parts.length >= 2) {
      const mid = Math.ceil(parts.length / 2);
      return [parts.slice(0, mid).join(" "), parts.slice(mid).join(" ")].filter(Boolean);
    }
    const cut = Math.ceil(raw.length / 2);
    return [raw.slice(0, cut).trim(), raw.slice(cut).trim()].filter(Boolean);
  }

  function buildSvg(segments, highlightIndex) {
    const size = 320;
    const cx = size / 2;
    const cy = size / 2;
    const r = 138;
    if (!segments.length) {
      return `<svg class="vw-svg" viewBox="0 0 ${size} ${size}" aria-hidden="true">
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="#F7F0F8"/>
        <text x="${cx}" y="${cy}" text-anchor="middle" dominant-baseline="middle" fill="#9A8AAD" font-size="13" font-family="system-ui,sans-serif">Нет секторов</text>
      </svg>`;
    }

    const slices = visualSlices(segments);
    const parts = [];
    const labels = [];
    const ticks = [];
    const uid = "vw" + Math.random().toString(36).slice(2, 8);

    slices.forEach((sl) => {
      const s = sl.segment;
      const { startDeg, midDeg, spanDeg, index } = sl;
      const isWin = highlightIndex === index;
      const stroke = isWin ? "rgba(255,255,255,.98)" : "rgba(255,255,255,.85)";
      const sw = isWin ? 3.5 : 2.5;
      parts.push(
        `<path class="${isWin ? "vw-slice is-win" : "vw-slice"}" d="${slicePath(cx, cy, r, startDeg, startDeg + spanDeg)}" fill="${esc(s.color)}" stroke="${stroke}" stroke-width="${sw}"${isWin ? ` filter="url(#${uid}-win)"` : ""}/>`
      );

      const [t1x, t1y] = polar(cx, cy, r - 1, startDeg);
      const [t2x, t2y] = polar(cx, cy, r + 8, startDeg);
      ticks.push(
        `<line x1="${t1x}" y1="${t1y}" x2="${t2x}" y2="${t2y}" stroke="rgba(255,255,255,.7)" stroke-width="2.5" stroke-linecap="round"/>`
      );

      const lines = labelLines(s.label, spanDeg);
      const [tx, ty] = polar(cx, cy, r * 0.68, midDeg);
      const flip = midDeg > 90 && midDeg < 270;
      const rot = flip ? midDeg + 180 : midDeg;
      const fs = spanDeg < 45 ? 10 : spanDeg < 60 ? 11.5 : 13;
      const lineH = fs + 2;
      const startY = ty - ((lines.length - 1) * lineH) / 2;
      const fill = contrastText(s.color);
      const tspans = lines
        .map((line, i) => `<tspan x="${tx}" y="${startY + i * lineH}">${esc(line)}</tspan>`)
        .join("");
      labels.push(
        `<text class="vw-label${isWin ? " is-win" : ""}" fill="${fill}" font-size="${fs}" font-weight="${isWin ? 700 : 600}" text-anchor="middle" dominant-baseline="middle" transform="rotate(${rot} ${tx} ${ty})">${tspans}</text>`
      );
    });

    return `<svg class="vw-svg" viewBox="0 0 ${size} ${size}" aria-hidden="true">
      <defs>
        <radialGradient id="${uid}-glow" cx="50%" cy="42%" r="58%">
          <stop offset="0%" stop-color="#fff" stop-opacity=".22"/>
          <stop offset="55%" stop-color="#fff" stop-opacity="0"/>
          <stop offset="100%" stop-color="#2A1B3D" stop-opacity=".08"/>
        </radialGradient>
        <linearGradient id="${uid}-rim" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#FFFFFF"/>
          <stop offset="45%" stop-color="#F4ECF7"/>
          <stop offset="100%" stop-color="#E4D5EE"/>
        </linearGradient>
        <filter id="${uid}-win" x="-30%" y="-30%" width="160%" height="160%">
          <feDropShadow dx="0" dy="0" stdDeviation="6" flood-color="#FFD6EC" flood-opacity=".95"/>
          <feDropShadow dx="0" dy="0" stdDeviation="2" flood-color="#FFFFFF" flood-opacity=".9"/>
        </filter>
      </defs>
      <circle cx="${cx}" cy="${cy}" r="${r + 14}" fill="url(#${uid}-rim)"/>
      <circle cx="${cx}" cy="${cy}" r="${r + 11}" fill="none" stroke="rgba(61,42,85,.08)" stroke-width="1"/>
      <g>${parts.join("")}</g>
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="url(#${uid}-glow)"/>
      <g opacity=".95">${ticks.join("")}</g>
      ${labels.join("")}
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="rgba(255,255,255,.45)" stroke-width="1.5"/>
    </svg>`;
  }

  function pickWinner(segments) {
    const total = totalWeight(segments);
    if (!segments.length || total <= 0) return { index: -1, segment: null, startDeg: 0, spanDeg: 0 };
    const slices = visualSlices(segments);
    const roll = Math.random() * total;
    let acc = 0;
    for (let i = 0; i < segments.length; i++) {
      const w = Math.max(0, Number(segments[i].weight) || 0);
      if (roll < acc + w) {
        const sl = slices[i];
        return { index: i, segment: segments[i], startDeg: sl.startDeg, spanDeg: sl.spanDeg };
      }
      acc += w;
    }
    const last = slices[slices.length - 1];
    return {
      index: last.index,
      segment: last.segment,
      startDeg: last.startDeg,
      spanDeg: last.spanDeg,
    };
  }

  /**
   * @param {HTMLElement} root
   * @param {object} [opts]
   */
  function create(root, opts) {
    if (!root) throw new Error("VereskWheel.create: root required");
    const options = opts || {};
    let title = options.title || "";
    let note = options.note || "";
    let segments = normalizeSegments(options.segments || []);
    let rotation = 0;
    let spinning = false;
    let spinTimer = null;
    let revealTimers = [];
    let highlightIndex = -1;
    const logoUrl =
      options.logoUrl ||
      root.getAttribute("data-logo") ||
      DEFAULT_LOGO;

    root.classList.add("vw-root");
    root.innerHTML = `
      <div class="vw-meta">
        <div class="vw-kicker">Колесо фортуны</div>
        <div class="vw-title"></div>
        <div class="vw-note"></div>
      </div>
      <div class="vw-stage">
        <div class="vw-aura" aria-hidden="true"></div>
        <div class="vw-flash" aria-hidden="true"></div>
        <div class="vw-pointer" aria-hidden="true"><span></span></div>
        <div class="vw-disc"></div>
        <button type="button" class="vw-hub" aria-label="Крутить колесо">
          <img class="vw-hub-logo" src="${esc(logoUrl)}" alt="Veresk" width="48" height="48" decoding="async">
        </button>
      </div>
      <div class="vw-result" hidden>
        <div class="vw-result-kicker"></div>
        <div class="vw-result-prize"></div>
      </div>
      <button type="button" class="vw-spin-btn">Крутить</button>
    `;

    const titleEl = root.querySelector(".vw-title");
    const noteEl = root.querySelector(".vw-note");
    const discEl = root.querySelector(".vw-disc");
    const resultEl = root.querySelector(".vw-result");
    const resultKicker = root.querySelector(".vw-result-kicker");
    const resultPrize = root.querySelector(".vw-result-prize");
    const hubBtn = root.querySelector(".vw-hub");
    const spinBtn = root.querySelector(".vw-spin-btn");
    const stageEl = root.querySelector(".vw-stage");

    function clearRevealTimers() {
      revealTimers.forEach((id) => clearTimeout(id));
      revealTimers = [];
    }

    function paint() {
      if (titleEl) titleEl.textContent = title || "Розыгрыш";
      if (noteEl) noteEl.textContent = note || "Нажмите, чтобы крутить";
      if (discEl) {
        const keep = discEl.style.transform;
        discEl.innerHTML = buildSvg(segments, highlightIndex);
        discEl.style.transform = keep || `rotate(${rotation}deg)`;
      }
    }

    function setConfig(cfg) {
      if (!cfg) return;
      if (cfg.title != null) title = String(cfg.title);
      if (cfg.note != null) note = String(cfg.note);
      if (cfg.segments) segments = normalizeSegments(cfg.segments);
      paint();
    }

    function getConfig() {
      return {
        title,
        note,
        segments: segments.map((s, i) => ({
          ...s,
          order: i,
          chance_pct:
            totalWeight(segments) > 0
              ? Math.round((Math.max(0, s.weight) / totalWeight(segments)) * 1000) / 10
              : 0,
        })),
      };
    }

    function setResultState(kicker, prize, mode) {
      if (!resultEl) return;
      resultEl.hidden = false;
      resultEl.classList.remove("is-show", "is-tease", "is-win");
      if (mode) resultEl.classList.add(mode);
      if (resultKicker) resultKicker.textContent = kicker || "";
      if (resultPrize) resultPrize.textContent = prize || "";
      // reflow for animation restart
      void resultEl.offsetWidth;
      resultEl.classList.add("is-show");
    }

    function revealPrize(picked) {
      return new Promise((resolve) => {
        clearRevealTimers();
        highlightIndex = -1;
        paint();
        root.classList.add("is-teasing");
        stageEl?.classList.add("is-teasing");
        setResultState("Секунду…", "· · ·", "is-tease");

        revealTimers.push(
          setTimeout(() => {
            setResultState("И выпадает…", "?", "is-tease");
            hubBtn?.classList.add("is-pulse");
          }, 550)
        );

        revealTimers.push(
          setTimeout(() => {
            highlightIndex = picked.index;
            paint();
            stageEl?.classList.add("is-flash");
            setResultState("Ваш приз", picked.segment.label, "is-win");
            hubBtn?.classList.remove("is-pulse");
            hubBtn?.classList.add("is-win");
          }, 1250)
        );

        revealTimers.push(
          setTimeout(() => {
            root.classList.remove("is-teasing");
            stageEl?.classList.remove("is-teasing", "is-flash");
            resolve();
          }, 2100)
        );
      });
    }

    function spin(spinOpts) {
      const so = spinOpts || {};
      if (spinning) return Promise.reject(new Error("already spinning"));
      if (segments.length < 2 || totalWeight(segments) <= 0) {
        return Promise.reject(new Error("need segments"));
      }

      let picked;
      if (typeof so.winnerIndex === "number" && segments[so.winnerIndex]) {
        const slices = visualSlices(segments);
        const sl = slices[so.winnerIndex];
        picked = {
          index: so.winnerIndex,
          segment: segments[so.winnerIndex],
          startDeg: sl.startDeg,
          spanDeg: sl.spanDeg,
        };
      } else {
        picked = pickWinner(segments);
      }

      const jitter = (Math.random() * 0.7 + 0.15) * picked.spanDeg;
      const stopAt = picked.startDeg + jitter;
      const TURN_OPTIONS = [8, 10, 19, 30];
      const extraTurns =
        so.turns != null
          ? Math.max(8, Number(so.turns) || 8)
          : TURN_OPTIONS[Math.floor(Math.random() * TURN_OPTIONS.length)];
      const target = extraTurns * 360 + (360 - stopAt);
      const duration =
        so.durationMs != null ? so.durationMs : Math.round(2600 + extraTurns * 340);

      clearRevealTimers();
      highlightIndex = -1;
      paint();
      rotation = (rotation % 360) + target;
      spinning = true;
      root.classList.add("is-spinning");
      root.classList.remove("is-teasing");
      discEl.classList.add("is-spinning");
      stageEl?.classList.remove("is-flash", "is-teasing");
      hubBtn?.classList.remove("is-pulse", "is-win");
      discEl.style.transitionDuration = `${duration}ms`;
      discEl.style.transitionTimingFunction = "cubic-bezier(.08,.82,.16,1)";
      void discEl.offsetWidth;
      discEl.style.transform = `rotate(${rotation}deg)`;
      if (resultEl) {
        resultEl.hidden = true;
        resultEl.classList.remove("is-show", "is-tease", "is-win");
        if (resultKicker) resultKicker.textContent = "";
        if (resultPrize) resultPrize.textContent = "";
      }
      hubBtn.disabled = true;
      spinBtn.disabled = true;
      if (spinBtn) spinBtn.textContent = "Крутится…";

      return new Promise((resolve) => {
        if (spinTimer) clearTimeout(spinTimer);
        spinTimer = setTimeout(async () => {
          spinning = false;
          root.classList.remove("is-spinning");
          discEl.classList.remove("is-spinning");
          await revealPrize(picked);
          if (options.once) {
            if (spinBtn) spinBtn.textContent = "Приз получен";
            hubBtn.disabled = true;
            spinBtn.disabled = true;
          } else {
            if (spinBtn) spinBtn.textContent = "Крутить";
            hubBtn.disabled = false;
            spinBtn.disabled = false;
          }
          if (typeof options.onSpinEnd === "function") {
            options.onSpinEnd(picked.segment, picked.index);
          }
          resolve({ segment: picked.segment, index: picked.index, turns: extraTurns });
        }, duration);
      });
    }

    async function onSpinClick() {
      try {
        if (typeof options.resolveWinner === "function") {
          const resolved = await options.resolveWinner();
          if (!resolved || resolved.winnerIndex == null) return;
          await spin({
            winnerIndex: resolved.winnerIndex,
            turns: resolved.turns,
            durationMs: resolved.durationMs,
          });
          return;
        }
        await spin();
      } catch (err) {
        if (err && err.message === "need segments" && resultEl) {
          setResultState("Нужно больше секторов", "Минимум 2", "is-tease");
        } else if (err && err.message !== "already spinning") {
          console.warn("[wheel] spin failed", err);
        }
      }
    }

    hubBtn.addEventListener("click", onSpinClick);
    spinBtn.addEventListener("click", onSpinClick);
    paint();

    return {
      setConfig,
      getConfig,
      spin,
      paint,
      destroy() {
        if (spinTimer) clearTimeout(spinTimer);
        clearRevealTimers();
        hubBtn.removeEventListener("click", onSpinClick);
        spinBtn.removeEventListener("click", onSpinClick);
        root.innerHTML = "";
        root.classList.remove("vw-root", "is-spinning", "is-teasing");
      },
    };
  }

  global.VereskWheel = {
    create,
    normalizeSegments,
    totalWeight,
    DEFAULT_COLORS,
  };
})(typeof window !== "undefined" ? window : globalThis);
