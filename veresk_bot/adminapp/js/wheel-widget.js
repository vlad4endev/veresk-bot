/**
 * Veresk fortune wheel — общий виджет для Mini App и превью в админке.
 * API: VereskWheel.create(rootEl, { title, note, segments, onSpinEnd })
 */
(function (global) {
  "use strict";

  const DEFAULT_COLORS = [
    "#d64593",
    "#3a2558",
    "#e86aad",
    "#5a3d7a",
    "#c43d86",
    "#7b4bd6",
    "#f47db9",
    "#241a38",
  ];

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

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
    return lum > 0.62 ? "#241a38" : "#ffffff";
  }

  function buildSvg(segments) {
    const size = 320;
    const cx = size / 2;
    const cy = size / 2;
    const r = 148;
    const total = totalWeight(segments);
    if (!segments.length || total <= 0) {
      return `<svg class="vw-svg" viewBox="0 0 ${size} ${size}" aria-hidden="true">
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="#f3ebf6"/>
        <text x="${cx}" y="${cy}" text-anchor="middle" dominant-baseline="middle" fill="#9488a8" font-size="14">Нет секторов</text>
      </svg>`;
    }

    let angle = 0;
    const parts = [];
    const labels = [];
    segments.forEach((s) => {
      const span = (Math.max(0, Number(s.weight) || 0) / total) * 360;
      const start = angle;
      const end = angle + span;
      const mid = start + span / 2;
      parts.push(
        `<path d="${slicePath(cx, cy, r, start, end)}" fill="${esc(s.color)}" stroke="rgba(255,255,255,.55)" stroke-width="1.5"/>`
      );

      if (span >= 12) {
        const [tx, ty] = polar(cx, cy, r * 0.62, mid);
        const rot = mid;
        const text = String(s.label || "").slice(0, 18);
        const fs = span < 28 ? 9 : span < 45 ? 11 : 13;
        labels.push(
          `<text class="vw-label" x="${tx}" y="${ty}" fill="${contrastText(s.color)}" font-size="${fs}" text-anchor="middle" dominant-baseline="middle" transform="rotate(${rot} ${tx} ${ty})">${esc(text)}</text>`
        );
      }
      angle = end;
    });

    return `<svg class="vw-svg" viewBox="0 0 ${size} ${size}" aria-hidden="true">
      <defs>
        <filter id="vwInner" x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy="1" stdDeviation="1.2" flood-color="#000" flood-opacity=".12"/>
        </filter>
      </defs>
      <g filter="url(#vwInner)">${parts.join("")}</g>
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="rgba(58,37,88,.35)" stroke-width="6"/>
      <circle cx="${cx}" cy="${cy}" r="${r - 3}" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="2"/>
      ${labels.join("")}
    </svg>`;
  }

  function pickWinner(segments) {
    const total = totalWeight(segments);
    if (!segments.length || total <= 0) return { index: -1, segment: null, startDeg: 0, spanDeg: 0 };
    const roll = Math.random() * total;
    let acc = 0;
    let startDeg = 0;
    for (let i = 0; i < segments.length; i++) {
      const w = Math.max(0, Number(segments[i].weight) || 0);
      const spanDeg = (w / total) * 360;
      if (roll < acc + w) {
        return { index: i, segment: segments[i], startDeg, spanDeg };
      }
      acc += w;
      startDeg += spanDeg;
    }
    const last = segments.length - 1;
    const spanDeg = (Math.max(0, Number(segments[last].weight) || 0) / total) * 360;
    return { index: last, segment: segments[last], startDeg: 360 - spanDeg, spanDeg };
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

    root.classList.add("vw-root");
    root.innerHTML = `
      <div class="vw-meta">
        <div class="vw-title"></div>
        <div class="vw-note"></div>
      </div>
      <div class="vw-stage">
        <div class="vw-pointer" aria-hidden="true"></div>
        <div class="vw-disc"></div>
        <button type="button" class="vw-hub" aria-label="Крутить колесо">GO</button>
      </div>
      <div class="vw-result" hidden></div>
      <button type="button" class="vw-spin-btn">Крутить колесо</button>
    `;

    const titleEl = root.querySelector(".vw-title");
    const noteEl = root.querySelector(".vw-note");
    const discEl = root.querySelector(".vw-disc");
    const resultEl = root.querySelector(".vw-result");
    const hubBtn = root.querySelector(".vw-hub");
    const spinBtn = root.querySelector(".vw-spin-btn");

    function paint() {
      if (titleEl) titleEl.textContent = title || "Колесо фортуны";
      if (noteEl) noteEl.textContent = note || "Нажмите GO, чтобы крутить";
      if (discEl) {
        const keep = discEl.style.transform;
        discEl.innerHTML = buildSvg(segments);
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

    function spin(spinOpts) {
      const so = spinOpts || {};
      if (spinning) return Promise.reject(new Error("already spinning"));
      if (segments.length < 2 || totalWeight(segments) <= 0) {
        return Promise.reject(new Error("need segments"));
      }

      let picked;
      if (typeof so.winnerIndex === "number" && segments[so.winnerIndex]) {
        const total = totalWeight(segments);
        let startDeg = 0;
        for (let i = 0; i < so.winnerIndex; i++) {
          startDeg += (Math.max(0, Number(segments[i].weight) || 0) / total) * 360;
        }
        const spanDeg = (Math.max(0, Number(segments[so.winnerIndex].weight) || 0) / total) * 360;
        picked = { index: so.winnerIndex, segment: segments[so.winnerIndex], startDeg, spanDeg };
      } else {
        picked = pickWinner(segments);
      }

      const mid = picked.startDeg + picked.spanDeg / 2;
      const extraTurns = so.turns != null ? so.turns : 4 + Math.floor(Math.random() * 3);
      const target = extraTurns * 360 + (360 - mid);
      rotation = (rotation % 360) + target;
      spinning = true;
      discEl.classList.add("is-spinning");
      discEl.style.transform = `rotate(${rotation}deg)`;
      if (resultEl) {
        resultEl.hidden = true;
        resultEl.textContent = "";
      }
      hubBtn.disabled = true;
      spinBtn.disabled = true;

      const duration = so.durationMs != null ? so.durationMs : 4200;
      return new Promise((resolve) => {
        if (spinTimer) clearTimeout(spinTimer);
        spinTimer = setTimeout(() => {
          spinning = false;
          discEl.classList.remove("is-spinning");
          hubBtn.disabled = false;
          spinBtn.disabled = false;
          if (resultEl) {
            resultEl.hidden = false;
            resultEl.textContent = `🎉 ${picked.segment.label}`;
          }
          if (typeof options.onSpinEnd === "function") {
            options.onSpinEnd(picked.segment, picked.index);
          }
          resolve({ segment: picked.segment, index: picked.index });
        }, duration);
      });
    }

    function onSpinClick() {
      spin().catch((err) => {
        if (err && err.message === "need segments" && resultEl) {
          resultEl.hidden = false;
          resultEl.textContent = "Добавьте минимум 2 сектора";
        }
      });
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
        hubBtn.removeEventListener("click", onSpinClick);
        spinBtn.removeEventListener("click", onSpinClick);
        root.innerHTML = "";
        root.classList.remove("vw-root");
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
