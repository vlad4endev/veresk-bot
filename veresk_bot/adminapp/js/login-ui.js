/**
 * Декор экрана входа: фирменный паттерн листьев и бусины печати.
 * Визуал как в макете — без демо-логики формы.
 */
(function () {
  const C = { pink: "#FF92CE", plum: "#402C60", white: "#FFFFFF" };
  const LEAF =
    "M-0 50C-16.45 39.03 -27.66 20.67 -28.67 -0.59C-29.59 -19.63 -22.13 -37.12 -9.55 -49.54L-1.5 18.65L-0 -50C13.7 -38.84 22.8 -22.14 23.72 -3.1C24.73 18.16 15.32 37.51 -0 50";
  const NS = "http://www.w3.org/2000/svg";
  const el = (n, a, p) => {
    const e = document.createElementNS(NS, n);
    for (const k in a) e.setAttribute(k, a[k]);
    if (p) p.appendChild(e);
    return e;
  };
  const $ = (s) => document.querySelector(s);

  function beads() {
    const g = $("#loginBeads");
    if (!g) return;
    g.innerHTML = "";
    for (let i = 0; i < 24; i++) {
      const a = ((i * 15 - 90) * Math.PI) / 180;
      const big = i % 2 === 0;
      el(
        "circle",
        {
          cx: (100 + 78 * Math.cos(a)).toFixed(2),
          cy: (100 + 78 * Math.sin(a)).toFixed(2),
          r: big ? 3.6 : 1.9,
          fill: C.plum,
          opacity: big ? 0.9 : 0.45,
        },
        g
      );
    }
  }

  function paper() {
    const host = $("#loginPaper");
    if (!host || host.querySelector("svg")) return;
    let s = 20240816;
    const rnd = () => ((s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
    const svg = el("svg", { viewBox: "0 0 900 900", preserveAspectRatio: "xMidYMid slice" });
    el("path", { id: "vLoginLeaf", d: LEAF }, el("defs", {}, svg));
    for (let k = 0; k < 2; k++) {
      const g = el("g", {}, svg);
      for (let i = 0; i < 26; i++) {
        const plum = rnd() > 0.72;
        el(
          "use",
          {
            href: "#vLoginLeaf",
            transform: `translate(${(rnd() * 900).toFixed(1)} ${(rnd() * 900).toFixed(1)}) rotate(${(rnd() * 360) | 0}) scale(${((0.22 + rnd() * 0.62) * (k ? 0.72 : 1)).toFixed(2)})`,
            fill: plum ? C.plum : C.pink,
            opacity: (plum ? 0.05 + rnd() * 0.05 : 0.13 + rnd() * 0.16).toFixed(3),
          },
          g
        );
      }
    }
    host.appendChild(svg);
  }

  function init() {
    beads();
    paper();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
