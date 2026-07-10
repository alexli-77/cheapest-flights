/*!
 * charts.js — flight-watch 零依赖 SVG 图表回退模块
 * ---------------------------------------------------------------------------
 * 红队要求 ECharts 必须 vendor 进 repo（国内 CDN 不可靠），但构建环境网络受限，
 * 无法下载 echarts.min.js。因此这里提供一个纯 SVG、无依赖的极轻量图表实现，
 * 覆盖 dashboard 需要的 折线 / 柱状 / 热力 三种图。
 *
 * 如需升级为完整 ECharts：把官方 echarts.min.js 放到 docs/vendor/echarts.min.js，
 * 并在 index.html / route.html 中取消对应 <script> 注释即可。页面已做特性检测：
 *   window.echarts 存在 → 用 ECharts；否则 → 用本模块 window.FWCharts。
 *
 * 全部 API：
 *   FWCharts.renderLine(el, {series, xLabels, markLines, yName, formatter})
 *   FWCharts.renderBar(el, {categories, values, name, colorFor, formatter})
 *   FWCharts.renderHeatmap(el, {xLabels, yLabels, cells, min, max, formatter})
 * el 为容器 DOM 元素；每次调用会清空并重绘，随窗口 resize 自适应宽度。
 */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var PALETTE = [
    "#5b8ff9", "#61ddaa", "#f6bd16", "#e8684a", "#6dc8ec",
    "#9270ca", "#ff9d4d", "#269a99", "#ff99c3", "#5d7092"
  ];

  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name);
      return (v && v.trim()) || fallback;
    } catch (e) {
      return fallback;
    }
  }

  function theme() {
    return {
      text: cssVar("--fw-text", "#333"),
      sub: cssVar("--fw-text-sub", "#888"),
      grid: cssVar("--fw-border", "#e5e5e5"),
      axis: cssVar("--fw-text-sub", "#999")
    };
  }

  function el(tag, attrs) {
    var n = document.createElementNS(NS, tag);
    if (attrs) {
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k)) {
          n.setAttribute(k, attrs[k]);
        }
      }
    }
    return n;
  }

  // Shared tooltip element (one per document).
  var tip = null;
  function tooltip() {
    if (tip) return tip;
    tip = document.createElement("div");
    tip.className = "fw-chart-tip";
    tip.style.cssText =
      "position:fixed;z-index:9999;pointer-events:none;display:none;" +
      "padding:6px 9px;border-radius:6px;font-size:12px;line-height:1.5;" +
      "background:rgba(30,30,34,.92);color:#fff;box-shadow:0 2px 8px rgba(0,0,0,.25);" +
      "max-width:240px;white-space:nowrap;";
    document.body.appendChild(tip);
    return tip;
  }
  function showTip(evt, html) {
    var t = tooltip();
    t.innerHTML = html;
    t.style.display = "block";
    var x = evt.clientX + 14, y = evt.clientY + 14;
    if (x + t.offsetWidth > window.innerWidth) x = evt.clientX - t.offsetWidth - 14;
    if (y + t.offsetHeight > window.innerHeight) y = evt.clientY - t.offsetHeight - 14;
    t.style.left = x + "px";
    t.style.top = y + "px";
  }
  function hideTip() {
    if (tip) tip.style.display = "none";
  }

  function clear(container) {
    while (container.firstChild) container.removeChild(container.firstChild);
  }

  function baseSvg(container, height) {
    var w = Math.max(container.clientWidth || 320, 240);
    var h = height || 300;
    var svg = el("svg", {
      width: w, height: h, viewBox: "0 0 " + w + " " + h,
      style: "display:block;width:100%;height:" + h + "px;overflow:visible"
    });
    return { svg: svg, w: w, h: h };
  }

  function niceExtent(min, max) {
    if (min === max) { min -= 1; max += 1; }
    var pad = (max - min) * 0.08;
    return [Math.max(0, min - pad), max + pad];
  }

  function fmtDefault(v) { return String(v); }

  // ---------------------------------------------------------------- LINE
  function renderLine(container, opt) {
    opt = opt || {};
    var fmt = opt.formatter || fmtDefault;
    var th = theme();
    clear(container);
    var b = baseSvg(container, opt.height || 320);
    var svg = b.svg, W = b.w, H = b.h;
    var pad = { l: 52, r: 16, t: 16, b: 40 };
    var iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    var xLabels = opt.xLabels || [];
    var series = opt.series || [];
    var markLines = opt.markLines || [];

    var allY = [];
    series.forEach(function (s) {
      (s.data || []).forEach(function (p) {
        if (p != null && p[1] != null) allY.push(p[1]);
      });
    });
    markLines.forEach(function (m) { if (m.y != null) allY.push(m.y); });
    if (!allY.length) {
      container.appendChild(svg);
      var t = el("text", { x: W / 2, y: H / 2, fill: th.sub, "text-anchor": "middle", "font-size": 13 });
      t.textContent = "暂无数据";
      svg.appendChild(t);
      return;
    }
    var ext = niceExtent(Math.min.apply(null, allY), Math.max.apply(null, allY));
    var yMin = ext[0], yMax = ext[1];
    var n = xLabels.length || 1;
    function X(i) { return pad.l + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw); }
    function Y(v) { return pad.t + ih - ((v - yMin) / (yMax - yMin)) * ih; }

    // grid + y axis ticks
    var ticks = 4;
    for (var g = 0; g <= ticks; g++) {
      var vy = yMin + (g / ticks) * (yMax - yMin);
      var yy = Y(vy);
      svg.appendChild(el("line", { x1: pad.l, y1: yy, x2: pad.l + iw, y2: yy, stroke: th.grid, "stroke-width": 1 }));
      var lbl = el("text", { x: pad.l - 8, y: yy + 4, fill: th.sub, "text-anchor": "end", "font-size": 11 });
      lbl.textContent = fmt(Math.round(vy));
      svg.appendChild(lbl);
    }
    // x labels (thin out to avoid overlap)
    var step = Math.ceil(n / Math.max(2, Math.floor(iw / 60)));
    for (var i = 0; i < n; i++) {
      if (i % step !== 0 && i !== n - 1) continue;
      var xt = el("text", { x: X(i), y: pad.t + ih + 22, fill: th.sub, "text-anchor": "middle", "font-size": 10 });
      xt.textContent = (xLabels[i] || "").slice(5); // MM-DD
      svg.appendChild(xt);
    }

    // markLines (target price etc.)
    markLines.forEach(function (m) {
      var yy = Y(m.y);
      svg.appendChild(el("line", {
        x1: pad.l, y1: yy, x2: pad.l + iw, y2: yy,
        stroke: m.color || "#e8684a", "stroke-width": 1.5, "stroke-dasharray": "6 4"
      }));
      var mt = el("text", { x: pad.l + iw, y: yy - 5, fill: m.color || "#e8684a", "text-anchor": "end", "font-size": 10 });
      mt.textContent = m.label || ("目标 " + fmt(m.y));
      svg.appendChild(mt);
    });

    // series lines + points
    series.forEach(function (s, si) {
      var color = s.color || PALETTE[si % PALETTE.length];
      var pts = [];
      (s.data || []).forEach(function (p, i) {
        if (p == null || p[1] == null) return;
        pts.push([X(p[0] != null ? p[0] : i), Y(p[1]), p[1], i]);
      });
      if (pts.length > 1) {
        var d = "M" + pts.map(function (q) { return q[0] + " " + q[1]; }).join(" L");
        svg.appendChild(el("path", { d: d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round" }));
      }
      pts.forEach(function (q) {
        var c = el("circle", { cx: q[0], cy: q[1], r: 3.5, fill: color, stroke: cssVar("--fw-card-bg", "#fff"), "stroke-width": 1 });
        c.style.cursor = "pointer";
        (function (val, idx) {
          c.addEventListener("mousemove", function (e) {
            showTip(e, "<b>" + (s.name || "") + "</b><br>" + (xLabels[idx] || "") + "<br>" + fmt(val));
          });
          c.addEventListener("mouseleave", hideTip);
        })(q[2], q[3]);
        svg.appendChild(c);
      });
    });

    container.appendChild(svg);
    attachLegend(container, series.map(function (s, i) {
      return { name: s.name, color: s.color || PALETTE[i % PALETTE.length] };
    }));
  }

  // ---------------------------------------------------------------- BAR
  function renderBar(container, opt) {
    opt = opt || {};
    var fmt = opt.formatter || fmtDefault;
    var th = theme();
    clear(container);
    var cats = opt.categories || [];
    var vals = opt.values || [];
    var b = baseSvg(container, opt.height || 300);
    var svg = b.svg, W = b.w, H = b.h;
    var pad = { l: 52, r: 16, t: 16, b: 46 };
    var iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

    if (!vals.length) {
      container.appendChild(svg);
      var t = el("text", { x: W / 2, y: H / 2, fill: th.sub, "text-anchor": "middle", "font-size": 13 });
      t.textContent = "暂无航司数据";
      svg.appendChild(t);
      return;
    }
    var maxV = Math.max.apply(null, vals) * 1.1;
    var minV = 0;
    function Y(v) { return pad.t + ih - ((v - minV) / (maxV - minV)) * ih; }
    var ticks = 4;
    for (var g = 0; g <= ticks; g++) {
      var vy = minV + (g / ticks) * (maxV - minV);
      var yy = Y(vy);
      svg.appendChild(el("line", { x1: pad.l, y1: yy, x2: pad.l + iw, y2: yy, stroke: th.grid, "stroke-width": 1 }));
      var lbl = el("text", { x: pad.l - 8, y: yy + 4, fill: th.sub, "text-anchor": "end", "font-size": 11 });
      lbl.textContent = fmt(Math.round(vy));
      svg.appendChild(lbl);
    }
    var bw = iw / cats.length;
    var barW = Math.min(46, bw * 0.6);
    cats.forEach(function (cat, i) {
      var v = vals[i];
      var cx = pad.l + bw * i + bw / 2;
      var yTop = Y(v);
      var color = (opt.colorFor && opt.colorFor(cat, i)) || PALETTE[i % PALETTE.length];
      var rect = el("rect", {
        x: cx - barW / 2, y: yTop, width: barW, height: pad.t + ih - yTop,
        rx: 3, fill: color
      });
      rect.style.cursor = "pointer";
      rect.addEventListener("mousemove", function (e) {
        showTip(e, "<b>" + cat + "</b><br>" + fmt(v));
      });
      rect.addEventListener("mouseleave", hideTip);
      svg.appendChild(rect);
      var lab = el("text", { x: cx, y: pad.t + ih + 16, fill: th.sub, "text-anchor": "middle", "font-size": 11 });
      lab.textContent = cat;
      svg.appendChild(lab);
      var vlab = el("text", { x: cx, y: yTop - 5, fill: th.text, "text-anchor": "middle", "font-size": 10 });
      vlab.textContent = fmt(v);
      svg.appendChild(vlab);
    });
    container.appendChild(svg);
  }

  // ---------------------------------------------------------------- HEATMAP
  function renderHeatmap(container, opt) {
    opt = opt || {};
    var fmt = opt.formatter || fmtDefault;
    var th = theme();
    clear(container);
    var xs = opt.xLabels || [];  // fetch_date
    var ys = opt.yLabels || [];  // depart_date
    var cells = opt.cells || [];  // [xi, yi, value]
    var rows = ys.length, cols = xs.length;
    var cellH = 26;
    var b = baseSvg(container, opt.height || (rows * cellH + 70));
    var svg = b.svg, W = b.w;
    var pad = { l: 96, r: 16, t: 12, b: 44 };
    var iw = W - pad.l - pad.r;
    var cw = cols ? iw / cols : iw;

    if (!cells.length) {
      container.appendChild(svg);
      var t = el("text", { x: W / 2, y: 40, fill: th.sub, "text-anchor": "middle", "font-size": 13 });
      t.textContent = "暂无数据";
      svg.appendChild(t);
      return;
    }
    var min = opt.min, max = opt.max;
    if (min == null || max == null) {
      var vv = cells.map(function (c) { return c[2]; });
      min = Math.min.apply(null, vv); max = Math.max.apply(null, vv);
    }
    function colorFor(v) {
      var t = max === min ? 0.5 : (v - min) / (max - min);
      // low price = green (good), high price = red (bad)
      var r = Math.round(80 + t * 175);
      var g = Math.round(200 - t * 150);
      var bl = Math.round(120 - t * 60);
      return "rgb(" + r + "," + g + "," + bl + ")";
    }
    // y labels
    ys.forEach(function (yl, yi) {
      var yy = pad.t + yi * cellH + cellH / 2 + 4;
      var lab = el("text", { x: pad.l - 8, y: yy, fill: th.sub, "text-anchor": "end", "font-size": 10 });
      lab.textContent = yl;
      svg.appendChild(lab);
    });
    // x labels
    var step = Math.ceil(cols / Math.max(2, Math.floor(iw / 60)));
    xs.forEach(function (xl, xi) {
      if (xi % step !== 0 && xi !== cols - 1) return;
      var xx = pad.l + xi * cw + cw / 2;
      var lab = el("text", { x: xx, y: pad.t + rows * cellH + 18, fill: th.sub, "text-anchor": "middle", "font-size": 10 });
      lab.textContent = (xl || "").slice(5);
      svg.appendChild(lab);
    });
    cells.forEach(function (c) {
      var xi = c[0], yi = c[1], v = c[2];
      var x = pad.l + xi * cw, y = pad.t + yi * cellH;
      var rect = el("rect", {
        x: x + 1, y: y + 1, width: Math.max(1, cw - 2), height: cellH - 2,
        rx: 2, fill: colorFor(v)
      });
      rect.style.cursor = "pointer";
      rect.addEventListener("mousemove", function (e) {
        showTip(e, (ys[yi] || "") + " 出发<br>" + (xs[xi] || "") + " 抓取<br><b>" + fmt(v) + "</b>");
      });
      rect.addEventListener("mouseleave", hideTip);
      svg.appendChild(rect);
    });
    container.appendChild(svg);
  }

  // ---------------------------------------------------------------- legend
  function attachLegend(container, items) {
    var legend = document.createElement("div");
    legend.className = "fw-chart-legend";
    legend.style.cssText = "display:flex;flex-wrap:wrap;gap:10px;margin-top:8px;font-size:12px;justify-content:center";
    items.forEach(function (it) {
      if (!it.name) return;
      var span = document.createElement("span");
      span.style.cssText = "display:inline-flex;align-items:center;gap:5px;color:var(--fw-text-sub,#888)";
      span.innerHTML = '<i style="width:10px;height:10px;border-radius:2px;display:inline-block;background:' +
        it.color + '"></i>' + it.name;
      legend.appendChild(span);
    });
    container.appendChild(legend);
  }

  window.FWCharts = {
    renderLine: renderLine,
    renderBar: renderBar,
    renderHeatmap: renderHeatmap,
    palette: PALETTE
  };
})();
