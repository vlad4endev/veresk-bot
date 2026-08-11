/**
 * Veresk fortune wheel — Mini App hero variant.
 * API: VereskWheel.create(rootEl, { title, note, segments, once, resolveWinner, onSpinEnd })
 */
(function (global) {
  "use strict";

  const DEFAULT_COLORS = [
    "#F47CB8",
    "#402C60",
    "#FFFFFF",
    "#F47CB8",
    "#402C60",
    "#FFFFFF",
    "#F47CB8",
    "#402C60",
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
    // Brand: dark plum on white/light, white on plum/pink
    return lum > 0.62 ? "#402C60" : "#ffffff";
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
    const raw = String(text || "").trim();
    const maxChars = span < 50 ? 16 : span < 72 ? 22 : 26;
    let clipped = raw;
    if (raw.length > maxChars) {
      const slice = raw.slice(0, maxChars);
      const sp = slice.lastIndexOf(" ");
      clipped = sp > maxChars * 0.35 ? slice.slice(0, sp) : slice;
    }
    if (clipped.length <= 11 || span < 52) return [clipped];
    const parts = clipped.split(/\s+/);
    if (parts.length >= 2) {
      const mid = Math.ceil(parts.length / 2);
      return [parts.slice(0, mid).join(" "), parts.slice(mid).join(" ")].filter(Boolean);
    }
    const cut = Math.ceil(clipped.length / 2);
    return [clipped.slice(0, cut).trim(), clipped.slice(cut).trim()].filter(Boolean);
  }

  function buildSvg(segments, highlightIndex) {
    const size = 360;
    const cx = size / 2;
    const cy = size / 2;
    const r = 148;
    const PLUM = "#402C60";
    const PINK = "#F47CB8";
    if (!segments.length) {
      return `<svg class="vw-svg" viewBox="0 0 ${size} ${size}" aria-hidden="true">
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="#FFF0F6"/>
        <text x="${cx}" y="${cy}" text-anchor="middle" dominant-baseline="middle" fill="${PLUM}" font-size="14" font-family="Orchidea Pro,Georgia,serif">Нет секторов</text>
      </svg>`;
    }

    const slices = visualSlices(segments);
    const parts = [];
    const labels = [];
    const ticks = [];
    const beads = [];
    const uid = "vw" + Math.random().toString(36).slice(2, 8);

    // Точки как на логотипе из брендбука
    for (let i = 0; i < 28; i++) {
      const deg = (360 / 28) * i;
      const [bx, by] = polar(cx, cy, r + 16, deg);
      const on = i % 2 === 0;
      beads.push(
        `<circle cx="${bx}" cy="${by}" r="${on ? 2.6 : 1.5}" fill="${on ? PINK : PLUM}" opacity="${on ? 1 : 0.85}"/>`
      );
    }

    slices.forEach((sl) => {
      const s = sl.segment;
      const { startDeg, midDeg, spanDeg, index } = sl;
      const isWin = highlightIndex === index;
      const fillColor = s.color || PLUM;
      parts.push(
        `<path class="${isWin ? "vw-slice is-win" : "vw-slice"}" d="${slicePath(cx, cy, r, startDeg, startDeg + spanDeg)}" fill="${esc(fillColor)}" stroke="${PLUM}" stroke-width="${isWin ? 2.8 : 1.6}"${isWin ? ` filter="url(#${uid}-win)"` : ""}/>`
      );

      const [t1x, t1y] = polar(cx, cy, r - 1, startDeg);
      const [t2x, t2y] = polar(cx, cy, r + 7, startDeg);
      ticks.push(
        `<line x1="${t1x}" y1="${t1y}" x2="${t2x}" y2="${t2y}" stroke="${PLUM}" stroke-width="2" stroke-linecap="square"/>`
      );

      const lines = labelLines(s.label, spanDeg);
      const [tx, ty] = polar(cx, cy, r * 0.62, midDeg);
      const flip = midDeg > 90 && midDeg < 270;
      const rot = flip ? midDeg + 180 : midDeg;
      const fs = spanDeg < 50 ? 12 : spanDeg < 72 ? 14 : 15.5;
      const lineH = fs + 2;
      const startY = ty - ((lines.length - 1) * lineH) / 2;
      const fill = contrastText(fillColor);
      const tspans = lines
        .map((line, i) => `<tspan x="${tx}" y="${startY + i * lineH}">${esc(line)}</tspan>`)
        .join("");
      labels.push(
        `<text class="vw-label${isWin ? " is-win" : ""}" fill="${fill}" font-size="${fs}" font-weight="600" font-family="Orchidea Pro, Georgia, serif" text-anchor="middle" dominant-baseline="middle" transform="rotate(${rot} ${tx} ${ty})">${tspans}</text>`
      );
    });

    return `<svg class="vw-svg" viewBox="0 0 ${size} ${size}" aria-hidden="true">
      <defs>
        <filter id="${uid}-win" x="-25%" y="-25%" width="150%" height="150%">
          <feDropShadow dx="0" dy="0" stdDeviation="4" flood-color="${PINK}" flood-opacity=".85"/>
        </filter>
      </defs>
      <circle cx="${cx}" cy="${cy}" r="${r + 20}" fill="#FFFFFF" stroke="${PLUM}" stroke-width="2.5"/>
      <circle cx="${cx}" cy="${cy}" r="${r + 12}" fill="none" stroke="${PINK}" stroke-width="2"/>
      <g>${beads.join("")}</g>
      <g>${parts.join("")}</g>
      <g>${ticks.join("")}</g>
      ${labels.join("")}
      <circle cx="${cx}" cy="${cy}" r="36" fill="#FFFFFF" stroke="${PINK}" stroke-width="2"/>
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

    root.classList.add("vw-root", "vw-root--hero");
    root.innerHTML = `
      <div class="vw-meta">
        <div class="vw-title"></div>
        <div class="vw-note"></div>
      </div>
      <div class="vw-stage">
        <div class="vw-aura" aria-hidden="true"></div>
        <div class="vw-orbit" aria-hidden="true"></div>
        <div class="vw-flash" aria-hidden="true"></div>
        <div class="vw-pointer" aria-hidden="true"><span></span></div>
        <div class="vw-disc"></div>
        <button type="button" class="vw-hub" aria-label="Крутить колесо">
          <img class="vw-hub-logo" src="${esc(logoUrl)}" alt="Veresk" width="56" height="56" decoding="async">
        </button>
      </div>
      <div class="vw-result" hidden>
        <div class="vw-result-kicker"></div>
        <div class="vw-result-prize"></div>
      </div>
      <button type="button" class="vw-spin-btn"><span>Крутить</span></button>
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

    function setResultState(kicker, prize, cls) {
      if (!resultEl) return;
      resultEl.hidden = false;
      resultEl.classList.remove("is-show", "is-tease", "is-win");
      if (resultKicker) resultKicker.textContent = kicker || "";
      if (resultPrize) resultPrize.textContent = prize || "";
      void resultEl.offsetWidth;
      resultEl.classList.add("is-show");
      if (cls) resultEl.classList.add(cls);
    }

    function paint() {
      if (titleEl) titleEl.textContent = title || "Розыгрыш";
      if (noteEl) noteEl.textContent = note || "Один подарок после анкеты";
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
        segments: segments.map((s) => ({ ...s })),
      };
    }

    function revealPrize(picked) {
      return new Promise((resolve) => {
        highlightIndex = picked.index;
        paint();
        hubBtn?.classList.add("is-pulse");
        stageEl?.classList.add("is-flash", "is-teasing");
        root.classList.add("is-teasing");
        setResultState("Ваш приз", picked.segment?.label || "Приз", "is-win");

        revealTimers.push(
          setTimeout(() => {
            hubBtn?.classList.remove("is-pulse");
            hubBtn?.classList.add("is-win");
          }, 120)
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
      if (spinBtn) spinBtn.innerHTML = "<span>Крутится…</span>";

      return new Promise((resolve) => {
        if (spinTimer) clearTimeout(spinTimer);
        spinTimer = setTimeout(async () => {
          spinning = false;
          root.classList.remove("is-spinning");
          discEl.classList.remove("is-spinning");
          await revealPrize(picked);
          if (options.once) {
            if (spinBtn) spinBtn.innerHTML = "<span>Приз получен</span>";
            hubBtn.disabled = true;
            spinBtn.disabled = true;
          } else {
            if (spinBtn) spinBtn.innerHTML = "<span>Крутить</span>";
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
        root.classList.remove("vw-root", "vw-root--hero", "is-spinning", "is-teasing");
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
