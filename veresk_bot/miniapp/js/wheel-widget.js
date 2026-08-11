/**
 * Veresk fortune wheel — брендбук UI.
 * Данные (title, note, segments) только из настроек / opts.
 * API: VereskWheel.create(rootEl, { title, note, segments, once, resolveWinner, onSpinEnd })
 */
(function (global) {
  "use strict";

  const C = {
    pink: "#FF92CE",
    plum: "#402C60",
    white: "#FFFFFF",
    pinkDeep: "#E86BB4",
    pinkTint: "#FFD9EE",
  };

  /* Лист из фирменного паттерна */
  const LEAF =
    "M-0 50C-16.45 39.03 -27.66 20.67 -28.67 -0.59C-29.59 -19.63 -22.13 -37.12 -9.55 -49.54L-1.5 18.65L-0 -50C13.7 -38.84 22.8 -22.14 23.72 -3.1C24.73 18.16 15.32 37.51 -0 50";

  const FILLS = [
    { bg: C.plum, ink: C.white, leaf: C.pink, leafOp: 0.3 },
    { bg: C.pink, ink: C.plum, leaf: C.white, leafOp: 0.52 },
    { bg: C.white, ink: C.plum, leaf: C.pink, leafOp: 0.4 },
  ];

  const DEFAULT_COLORS = [C.pink, C.plum, C.white, C.pink, C.plum, C.white, C.pink, C.plum];

  const WORDMARK = `<svg class="vw-mark" viewBox="0 0 1000 231" role="img" aria-label="Veresk">
    <path d="M565.08 88.04C573.56 78.45 584.91 73.56 598.93 73.56C621.76 73.56 632.84 84.97 632.84 108.45C632.84 113.93 632.26 119.47 631.21 125.02L550.28 128.41C551.72 111.06 556.74 97.5 565.08 88.04M622.8 227.27C631.21 225.12 637.93 222.51 642.82 219.71C647.71 216.97 652.28 213.77 656.32 210.25C660.49 206.73 663.04 204.25 664.15 202.82C665.19 201.45 665.97 200.21 666.49 199.23L666.75 198.64L659.51 191.01L653.25 196.03C649.67 198.97 643.6 201.9 635.19 204.71C626.71 207.58 617.58 209.01 608 209.01C589.6 209.01 575.19 203.34 565.21 192.12C555.37 181.04 550.22 164.41 549.82 142.75L659.38 142.75C659.38 142.75 667.6 132.45 667.6 113.02C667.6 95.41 661.73 81.78 650.06 72.32C638.52 63 621.69 58.24 600.04 58.24C571.21 58.24 548.32 66.13 532.15 81.58C515.98 97.1 507.76 118.17 507.76 144.25C507.76 170.08 515.85 191.21 531.82 206.93C547.74 222.64 569.32 230.6 595.87 230.6C605.39 230.6 614.45 229.49 622.8 227.27"/>
    <path d="M266.79 88.04C275.27 78.45 286.62 73.56 300.64 73.56C323.46 73.56 334.49 84.97 334.49 108.45C334.49 113.93 333.96 119.47 332.92 125.02L251.99 128.41C253.42 111.06 258.45 97.5 266.79 88.04M358.03 210.25C362.2 206.73 364.75 204.25 365.85 202.82C366.9 201.45 367.68 200.21 368.2 199.23L368.46 198.64L361.22 191.01L354.96 196.03C351.38 198.97 345.31 201.9 336.9 204.71C328.42 207.58 319.29 209.01 309.7 209.01C291.31 209.01 276.9 203.34 266.92 192.12C257.08 181.04 251.92 164.41 251.53 142.75L361.09 142.75C361.09 142.75 369.31 132.45 369.31 113.02C369.31 95.41 363.44 81.78 351.77 72.32C340.22 63 323.33 58.24 301.68 58.24C272.92 58.24 250.03 66.13 233.86 81.58C217.69 97.1 209.47 118.17 209.47 144.25C209.47 170.08 217.56 191.21 233.53 206.93C249.45 222.64 271.03 230.6 297.57 230.6C307.1 230.6 316.16 229.49 324.51 227.27C332.86 225.12 339.64 222.51 344.53 219.71C349.42 216.97 353.98 213.77 358.03 210.25"/>
    <path d="M402.11 227.86L437.79 227.86L437.79 117.84C437.79 117.84 463.35 88.63 505.54 92.15C505.54 79.17 504.37 68.02 502.02 58.95L501.89 58.24L501.17 58.24C493.67 58.24 486.17 59.74 478.81 62.67C471.44 65.61 465.31 68.87 460.68 72.32C456.05 75.84 451.55 79.95 447.37 84.58C443.07 89.34 440.39 92.47 439.15 94.24C438.18 95.47 437.39 96.65 436.74 97.69L436.42 97.37C436.42 97.37 435.11 70.11 423.5 48.58L389.07 52.24C389.07 52.24 402.11 80.61 402.11 122.73ZM402.11 227.86"/>
    <path d="M757.66 230.6C778.79 230.6 795.36 226.69 806.96 218.99C818.64 211.23 824.57 199.49 824.57 184.23C824.57 172.43 820.66 162.84 812.96 155.73C805.4 148.82 791.64 140.54 771.81 131.21C771.16 131.02 770.51 130.82 769.92 130.56C769.47 130.36 768.88 130.04 768.1 129.58C751.53 121.95 740.05 115.89 733.86 111.39C727.86 107.08 724.86 101.54 724.86 94.95C724.86 81.26 734.77 74.61 755.18 74.61C760.86 74.61 766.27 75.58 771.23 77.54C776.18 79.43 780.23 81.65 783.1 84C786.1 86.34 788.7 89.15 790.99 92.34C793.27 95.6 794.77 97.82 795.42 99C796.07 100.23 796.53 101.21 796.73 101.8L796.92 102.45C796.92 102.45 804.81 100.95 813.29 88.95C816.36 84.58 817.59 78.58 817.27 74.41L817.27 73.95C817.27 73.95 809.57 66.78 790.66 62.08C780.81 59.67 769.66 58.24 757.66 58.24C736.73 58.24 720.49 62.21 709.53 70.04C698.45 78 692.84 89.34 692.84 103.95C692.84 116.93 697.01 127.17 705.3 134.54C713.45 141.71 726.23 149.02 743.38 156.19C762.68 164.47 775.86 170.86 782.51 175.23C789.03 179.47 792.16 185.01 792.16 192.19C792.16 199.36 789.16 204.71 783.03 208.62C776.84 212.6 768.68 214.56 758.64 214.56C752.58 214.56 746.58 213.45 740.97 211.16C735.29 208.88 730.6 206.34 727.08 203.47C723.62 200.67 720.23 197.4 717.16 193.69C714.1 190.04 712.14 187.56 711.36 186.19C710.51 184.82 709.93 183.71 709.47 182.8L709.21 182.27C709.21 182.27 698.97 184.3 693.1 195.58C690.69 200.14 689.32 204.9 689.32 209.93L689.32 210.32C689.32 210.32 700.86 220.88 719.38 225.71C730.73 228.71 743.51 230.6 757.66 230.6"/>
    <path d="M1000 227.79L923.5 130.23L985.39 61.04L952.46 61.04L949.07 64.04L945.74 69.45C943.72 73.11 939.09 79.56 931.98 88.63C924.81 97.76 916.07 107.93 905.9 119.02L899.57 126.39L885.29 127.95C885.29 127.95 885.29 93.97 885.29 75.71C885.16 29.67 873.81 0 873.81 0L838.66 8.87C838.66 8.87 848.64 45.19 848.64 81.39L848.64 227.86L885.29 227.86L885.29 143.41L892.59 143.41L956.76 227.79ZM1000 227.79"/>
    <path d="M135.71 227.86L232.03 4.37L209.99 4.37L130.23 191.14L46.76 4.37L0 4.37L3.07 9.46C5.28 12.65 8.8 18.65 13.37 27.46C18 36.26 22.83 46.37 27.65 57.52L104.02 227.86ZM135.71 227.86"/>
  </svg>`;

  const EMBLEM =
    "M56.51 95.31C58.07 90.35 60.33 84.48 63.02 78.13L76.92 71.99L63.69 80.2C67.44 81.59 71.76 81.44 75.58 79.42C79.85 77.16 82.54 73.1 83.21 68.66C79.17 66.71 74.3 66.65 70.02 68.89C68.13 69.9 66.54 71.26 65.3 72.86C67.42 68.08 69.72 63.09 72.13 58.12L84.02 52.86L72.32 60.13C75.64 61.36 79.46 61.23 82.84 59.45C86.61 57.45 89 53.84 89.6 49.93C86.02 48.2 81.7 48.14 77.92 50.14C76.82 50.72 75.85 51.44 74.99 52.27C79.33 43.5 83.83 34.97 87.9 27.66C91.4 34.27 93.39 41.88 93.39 49.99C93.39 72.96 77.39 91.99 56.51 95.31M6.61 49.99C6.61 41.88 8.6 34.27 12.1 27.66C16.17 34.97 20.67 43.5 25.01 52.27C24.15 51.44 23.18 50.72 22.08 50.14C18.3 48.14 13.98 48.2 10.4 49.93C11 53.84 13.39 57.45 17.16 59.45C20.54 61.23 24.36 61.36 27.68 60.13L15.98 52.86L27.87 58.12C30.28 63.09 32.58 68.08 34.7 72.86C33.46 71.26 31.87 69.9 29.98 68.89C25.7 66.65 20.83 66.71 16.79 68.66C17.46 73.1 20.15 77.16 24.42 79.42C28.24 81.44 32.56 81.59 36.31 80.2L23.08 71.99L36.98 78.13C39.67 84.48 41.93 90.35 43.49 95.31C22.61 91.99 6.61 72.96 6.61 49.99M50 4.15C62.39 4.15 73.54 9.65 81.45 18.42C80.14 22.64 78.35 27.46 76.27 32.61C76.32 31.86 76.3 31.09 76.22 30.34C75.73 25.75 73.07 21.93 69.38 19.76C66.23 22.65 64.44 26.95 64.93 31.54C65.36 35.64 67.54 39.13 70.64 41.37L70.1 26.52L72.7 41.14C70.62 45.95 68.37 50.88 66.1 55.74C66.22 54.7 66.22 53.63 66.1 52.55C65.56 47.47 62.61 43.22 58.5 40.83C55.01 44.04 53.03 48.81 53.56 53.89C54.05 58.46 56.47 62.34 59.91 64.82L59.31 48.33L62.12 64.15C57.48 73.8 53.05 82.54 50 88.48C46.95 82.54 42.52 73.8 37.88 64.15L40.69 48.33L40.09 64.82C43.53 62.34 45.95 58.46 46.44 53.89C46.97 48.81 44.99 44.04 41.5 40.83C37.39 43.22 34.44 47.47 33.9 52.55C33.78 53.63 33.78 54.7 33.9 55.74C31.63 50.88 29.38 45.95 27.3 41.14L29.9 26.52L29.36 41.37C32.46 39.13 34.64 35.64 35.07 31.54C35.56 26.95 33.77 22.65 30.62 19.76C26.93 21.93 24.27 25.75 23.78 30.34C23.7 31.09 23.68 31.86 23.73 32.61C21.65 27.46 19.86 22.64 18.55 18.42C26.46 9.65 37.61 4.15 50 4.15M50 0C22.39 0 0 22.38 0 49.99C0 77.6 22.39 99.99 50 99.99C77.61 99.99 100 77.6 100 49.99C100 22.38 77.61 0 50 0";

  const POINTER_SVG = `<svg viewBox="-32 -54 58 108" aria-hidden="true">
    <path d="${LEAF}" fill="#FFFFFF" stroke="#402C60" stroke-width="3.4" stroke-linejoin="round"/>
  </svg>`;

  const NS = "http://www.w3.org/2000/svg";
  const CX = 200;
  const CY = 200;
  const R_FACE = 172;
  const R_BEAD = 190;

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

  function toneOf(i) {
    return FILLS[i % 3];
  }

  function polar(r, deg) {
    const a = ((deg - 90) * Math.PI) / 180;
    return [CX + r * Math.cos(a), CY + r * Math.sin(a)];
  }

  function el(n, a, p) {
    const e = document.createElementNS(NS, n);
    for (const k in a) e.setAttribute(k, a[k]);
    if (p) p.appendChild(e);
    return e;
  }

  function labelLines(text) {
    const raw = String(text || "").trim();
    if (!raw) return ["Приз"];
    const parts = raw.split(/\s+/);
    if (parts.length === 1) {
      if (raw.length <= 10) return [raw];
      const cut = Math.ceil(raw.length / 2);
      return [raw.slice(0, cut), raw.slice(cut)];
    }
    if (parts.length === 2) return parts;
    const mid = Math.ceil(parts.length / 2);
    return [parts.slice(0, mid).join(" "), parts.slice(mid).join(" ")];
  }

  function heroIndex(segments) {
    let best = 0;
    let w = -1;
    segments.forEach((s, i) => {
      const ww = Number(s.weight) || 0;
      if (ww > w) {
        w = ww;
        best = i;
      }
    });
    /* «главный» — самый редкий (мин. вес среди ненулевых) выглядит hero-нее;
       в макете hero — букет. Берём минимальный ненулевой вес. */
    let minW = Infinity;
    let minI = 0;
    segments.forEach((s, i) => {
      const ww = Number(s.weight) || 0;
      if (ww > 0 && ww < minW) {
        minW = ww;
        minI = i;
      }
    });
    return Number.isFinite(minW) ? minI : best;
  }

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

  function buildSvg(segments) {
    const n = segments.length;
    if (!n) {
      return `<svg class="vw-svg" viewBox="0 0 400 400" aria-hidden="true">
        <circle cx="200" cy="200" r="172" fill="#2C1D43"/>
        <text x="200" y="200" text-anchor="middle" fill="#FFD9EE" font-size="14">Нет секторов</text>
      </svg>`;
    }

    const SEG = 360 / n;
    const svg = el("svg", {
      class: "vw-svg",
      viewBox: "0 0 400 400",
      role: "img",
      "aria-label": "Колесо фортуны",
    });
    const defs = el("defs", {}, svg);
    el("path", { id: "vwLeaf", d: LEAF }, defs);

    const sh = el("radialGradient", { id: "vwShade", cx: ".5", cy: ".5", r: ".5" }, defs);
    el("stop", { offset: ".55", "stop-color": "#402C60", "stop-opacity": "0" }, sh);
    el("stop", { offset: "1", "stop-color": "#221635", "stop-opacity": ".26" }, sh);
    const gl = el("linearGradient", { id: "vwSheen", x1: ".14", y1: "0", x2: ".8", y2: "1" }, defs);
    el("stop", { offset: "0", "stop-color": "#fff", "stop-opacity": ".30" }, gl);
    el("stop", { offset: ".44", "stop-color": "#fff", "stop-opacity": ".05" }, gl);
    el("stop", { offset: "1", "stop-color": "#fff", "stop-opacity": "0" }, gl);

    el(
      "circle",
      {
        cx: CX,
        cy: CY,
        r: 199,
        fill: "none",
        stroke: C.pink,
        "stroke-opacity": ".20",
        "stroke-width": "1.2",
      },
      svg
    );

    const face = el("g", {}, svg);
    const hi = heroIndex(segments);

    segments.forEach((p, i) => {
      const t = toneOf(i);
      const c = i * SEG;
      const [x0, y0] = polar(R_FACE, c - SEG / 2);
      const [x1, y1] = polar(R_FACE, c + SEG / 2);
      el(
        "path",
        {
          d: `M${CX},${CY}L${x0.toFixed(2)},${y0.toFixed(2)}A${R_FACE},${R_FACE} 0 0 1 ${x1.toFixed(2)},${y1.toFixed(2)}Z`,
          fill: t.bg,
          "data-seg": String(i),
        },
        face
      );
    });

    segments.forEach((p, i) => {
      const t = toneOf(i);
      const c = i * SEG;
      const [lx, ly] = polar(66, c);
      const s = i === hi ? 0.3 : 0.26;
      el(
        "use",
        {
          href: "#vwLeaf",
          transform: `translate(${lx.toFixed(2)} ${ly.toFixed(2)}) rotate(${c + 8}) scale(${s})`,
          fill: t.leaf,
          opacity: String(t.leafOp),
        },
        face
      );
    });

    if (hi > -1) {
      const [ax, ay] = polar(R_FACE - 9, hi * SEG - SEG / 2 + 3);
      const [bx, by] = polar(R_FACE - 9, hi * SEG + SEG / 2 - 3);
      el(
        "path",
        {
          d: `M${ax.toFixed(2)},${ay.toFixed(2)}A${R_FACE - 9},${R_FACE - 9} 0 0 1 ${bx.toFixed(2)},${by.toFixed(2)}`,
          fill: "none",
          stroke: C.pink,
          "stroke-width": "2.4",
          "stroke-linecap": "round",
        },
        face
      );
    }

    segments.forEach((p, i) => {
      const c = i * SEG;
      const flip = c >= 180 && c < 360;
      const g = el("g", { transform: `rotate(${flip ? c + 90 : c - 90} ${CX} ${CY})` }, face);
      const x = flip ? CX - 116 : CX + 116;
      const ink = toneOf(i).ink;
      const lines = labelLines(p.label);
      const em = lines.length > 1 ? 1 : 0;
      lines.forEach((line, li) => {
        const t = el(
          "text",
          {
            x: String(x),
            y: String(CY + (li ? 11 : -8)),
            "text-anchor": "middle",
            "dominant-baseline": "middle",
            fill: ink,
            "font-size": "15",
            "font-weight": li === em ? "700" : "400",
            "letter-spacing": "-.2",
            "font-family": "DM Sans, system-ui, sans-serif",
          },
          g
        );
        t.textContent = line;
      });
    });

    el("circle", { cx: CX, cy: CY, r: R_FACE, fill: "url(#vwSheen)", "pointer-events": "none" }, face);
    el("circle", { cx: CX, cy: CY, r: R_FACE, fill: "url(#vwShade)", "pointer-events": "none" }, face);
    el(
      "circle",
      { cx: CX, cy: CY, r: R_FACE - 0.8, fill: "none", stroke: C.white, "stroke-width": "1.6" },
      svg
    );
    el(
      "circle",
      { cx: CX, cy: CY, r: R_FACE + 3.5, fill: "none", stroke: C.plum, "stroke-width": "7" },
      svg
    );

    const beads = el("g", {}, svg);
    for (let i = 0; i < n * 3; i++) {
      const big = i % 3 === 0;
      const [x, y] = polar(R_BEAD, i * (360 / (n * 3)) - SEG / 2);
      el(
        "circle",
        {
          cx: x.toFixed(2),
          cy: y.toFixed(2),
          r: big ? 5.4 : 2.7,
          fill: C.pink,
          opacity: big ? "1" : ".62",
        },
        beads
      );
    }

    const wrap = document.createElement("div");
    wrap.appendChild(svg);
    return wrap.innerHTML;
  }

  function buildPatternSvg() {
    let s = 20240816;
    const rnd = () => ((s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
    const svg = el("svg", { viewBox: "0 0 400 800", preserveAspectRatio: "xMidYMid slice" });
    el("path", { id: "vwBLeaf", d: LEAF }, el("defs", {}, svg));
    const cols = [C.pink, C.white, C.pink, C.plum];
    ["vw-drift-a", "vw-drift-b"].forEach((cls, gi) => {
      const g = el("g", { class: cls }, svg);
      for (let i = 0; i < 15; i++) {
        const x = rnd() * 400;
        const y = rnd() * 800;
        const sc = (0.16 + rnd() * 0.42) * (gi ? 0.8 : 1);
        el(
          "use",
          {
            href: "#vwBLeaf",
            transform: `translate(${x.toFixed(1)} ${y.toFixed(1)}) rotate(${(rnd() * 360).toFixed(0)}) scale(${sc.toFixed(2)})`,
            fill: cols[(rnd() * cols.length) | 0],
            opacity: (0.05 + rnd() * 0.075).toFixed(3),
          },
          g
        );
      }
    });
    const wrap = document.createElement("div");
    wrap.appendChild(svg);
    return wrap.innerHTML;
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

  function create(root, opts) {
    if (!root) throw new Error("VereskWheel.create: root required");
    const options = opts || {};
    let title = options.title || "";
    let note = options.note || "";
    let segments = normalizeSegments(options.segments || []);
    let rotation = 0;
    let spinning = false;
    let spinTimer = null;
    let highlightIndex = -1;
    const reduce =
      typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

    root.classList.add("vw-root");
    root.innerHTML = `
      <div class="vw-pattern" aria-hidden="true">${buildPatternSvg()}</div>
      <canvas class="vw-fx" hidden></canvas>
      <header class="vw-head">
        ${WORDMARK}
        <p class="vw-eyebrow"></p>
        <h1 class="vw-title">Колесо фортуны</h1>
        <p class="vw-note"></p>
      </header>
      <div class="vw-dial">
        <div class="vw-dial-box">
          <div class="vw-aura" aria-hidden="true"></div>
          <div class="vw-disc"></div>
          <button type="button" class="vw-hub" aria-label="Крутить колесо">
            <svg viewBox="0 0 100 100" aria-hidden="true"><path fill="${C.pink}" d="${EMBLEM}"/></svg>
          </button>
          <div class="vw-pointer" aria-hidden="true">${POINTER_SVG}</div>
        </div>
      </div>
      <footer class="vw-foot">
        <div class="vw-chip"><em></em><span class="vw-chip-text">Одна попытка</span></div>
        <button type="button" class="vw-spin-btn">Крутить</button>
        <p class="vw-fine">Подарок действует 14 дней · один подарок на человека</p>
      </footer>
      <div class="vw-result" hidden></div>
    `;

    const eyebrowEl = root.querySelector(".vw-eyebrow");
    const noteEl = root.querySelector(".vw-note");
    const discEl = root.querySelector(".vw-disc");
    const hubBtn = root.querySelector(".vw-hub");
    const spinBtn = root.querySelector(".vw-spin-btn");
    const chipText = root.querySelector(".vw-chip-text");
    const pointerEl = root.querySelector(".vw-pointer");

    function paint() {
      if (eyebrowEl) eyebrowEl.textContent = title || "Розыгрыш";
      if (noteEl) noteEl.textContent = note || "Крутите один раз и забирайте подарок.";
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
        segments: segments.map((s) => ({ ...s })),
      };
    }

    function setBusy(busy, label) {
      hubBtn.disabled = busy || Boolean(options.once && label === "done");
      spinBtn.disabled = busy || Boolean(options.once && label === "done");
      if (label === "spinning") {
        spinBtn.textContent = "Крутится…";
        if (chipText) chipText.textContent = "Крутится…";
      } else if (label === "done") {
        spinBtn.textContent = "Приз получен";
        if (chipText) chipText.textContent = "Попытка использована";
      } else if (label === "retry") {
        spinBtn.textContent = "Крутить ещё раз";
        if (chipText) chipText.textContent = "Ещё одна попытка";
      } else {
        spinBtn.textContent = "Крутить";
        if (chipText) chipText.textContent = "Одна попытка";
      }
    }

    function revealPrize(picked) {
      highlightIndex = picked.index;
      const path = discEl.querySelector(`[data-seg="${picked.index}"]`);
      if (path) path.style.filter = "brightness(1.12) saturate(1.08)";
      return Promise.resolve();
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

      const n = segments.length;
      const SEG = 360 / n;
      const turns =
        so.turns != null
          ? Math.max(1, Number(so.turns) || 1)
          : reduce
            ? 1
            : 6 + Math.floor(Math.random() * 2);
      const jitter = (Math.random() * 2 - 1) * (SEG / 2 - 7);
      const base = ((-picked.index * SEG - rotation) % 360 + 360) % 360;
      const from = rotation;
      const delta = turns * 360 + base + jitter;
      const duration =
        so.durationMs != null ? so.durationMs : reduce ? 900 : Math.round(4800 + turns * 80);

      spinning = true;
      root.classList.add("is-spinning");
      discEl.classList.add("is-spinning");
      setBusy(true, "spinning");
      highlightIndex = -1;
      paint();

      const easeOut = (t) => 1 - Math.pow(1 - t, 4.2);
      const per = 360 / (n * 3);
      let last = from;
      let t0 = null;

      return new Promise((resolve) => {
        const step = (ts) => {
          if (t0 === null) t0 = ts;
          const t = Math.min(1, (ts - t0) / duration);
          rotation = from + delta * easeOut(t);
          discEl.style.transform = `rotate(${rotation}deg)`;
          if (pointerEl) {
            if (Math.floor(rotation / per) !== Math.floor(last / per)) {
              const vel = (rotation - last) * (1000 / 16);
              pointerEl.style.transform = `translate(-50%,0) rotate(${Math.max(-12, -vel / 90)}deg)`;
            } else {
              pointerEl.style.transform = "translate(-50%,0)";
            }
          }
          last = rotation;
          if (t < 1) {
            requestAnimationFrame(step);
          } else {
            if (pointerEl) pointerEl.style.transform = "translate(-50%,0)";
            spinning = false;
            root.classList.remove("is-spinning");
            discEl.classList.remove("is-spinning");
            revealPrize(picked).then(() => {
              const isRetry = Boolean(so.retry);
              if (options.once && !isRetry) setBusy(false, "done");
              else setBusy(false, isRetry ? "retry" : "idle");
              if (typeof options.onSpinEnd === "function") {
                options.onSpinEnd(picked.segment, picked.index, { retry: isRetry });
              }
              resolve({
                segment: picked.segment,
                index: picked.index,
                turns,
                retry: isRetry,
              });
            });
          }
        };
        requestAnimationFrame(step);
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
            retry: Boolean(resolved.retry),
          });
          return;
        }
        await spin();
      } catch (err) {
        setBusy(false, "idle");
        if (err && err.message !== "already spinning") {
          console.warn("[wheel] spin failed", err);
        }
      }
    }

    hubBtn.addEventListener("click", onSpinClick);
    spinBtn.addEventListener("click", onSpinClick);
    paint();

    if (!reduce) {
      discEl.style.transition = "transform 1.1s cubic-bezier(.19,1,.22,1), opacity .8s";
      discEl.style.opacity = "0";
      discEl.style.transform = "rotate(-24deg) scale(.9)";
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          discEl.style.opacity = "1";
          discEl.style.transform = "rotate(0deg) scale(1)";
          setTimeout(() => {
            discEl.style.transition = "none";
            rotation = 0;
          }, 1200);
        });
      });
    }

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
        root.classList.remove("vw-root", "is-spinning");
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
