/* A short light brushstroke on the paper, drawn only as the mouse moves. */
(() => {
  "use strict";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const layer = document.createElement("div");
  layer.className = "trail";
  layer.setAttribute("aria-hidden", "true");
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  layer.append(canvas);
  document.body.append(layer);

  const lifetime = 300;
  const reach = 86;
  const center = 105;
  const size = center * 2;
  const samples = [];
  let frame = 0;
  let scale = 1;

  function clear() {
    cancelAnimationFrame(frame);
    frame = 0;
    samples.length = 0;
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, size, size);
  }

  function resize() {
    scale = Math.min(devicePixelRatio || 1, 2);
    canvas.style.width = canvas.style.height = size + "px";
    canvas.width = canvas.height = Math.ceil(size * scale);
    clear();
  }

  function latestPath() {
    const path = [{ ...samples.at(-1), distance: 0 }];
    let distance = 0;
    for (let i = samples.length - 2; i >= 0; i -= 1) {
      const point = samples[i];
      const next = samples[i + 1];
      const step = Math.hypot(next.x - point.x, next.y - point.y);
      if (distance + step > reach) {
        const fraction = (reach - distance) / step;
        path.push({ x: next.x + (point.x - next.x) * fraction, y: next.y + (point.y - next.y) * fraction, distance: reach });
        break;
      }
      distance += step;
      path.push({ ...point, distance });
    }
    return path.reverse();
  }

  function trace(edge) {
    for (let i = 0; i < edge.length - 1; i += 1) {
      const point = edge[i];
      const next = edge[i + 1];
      ctx.quadraticCurveTo(point.x, point.y, (point.x + next.x) / 2, (point.y + next.y) / 2);
    }
    ctx.lineTo(edge.at(-1).x, edge.at(-1).y);
  }

  function paintBand(path, radius, stops) {
    const tail = path[0], head = path.at(-1);
    const length = Math.max(tail.distance, 1);
    const left = [], right = [];
    path.forEach((point, i) => {
      const previous = path[i - 1] ?? point, next = path[i + 1] ?? point;
      const tx = next.x - previous.x, ty = next.y - previous.y;
      const tangent = Math.hypot(tx, ty) || 1;
      const taper = Math.sin(Math.PI * point.distance / length) ** .72;
      const width = radius * (.11 + .89 * taper);
      const dx = -ty / tangent * width, dy = tx / tangent * width;
      left.push({ x: point.x + dx, y: point.y + dy });
      right.push({ x: point.x - dx, y: point.y - dy });
    });
    const gradient = ctx.createLinearGradient(tail.x, tail.y, head.x, head.y);
    stops.forEach(([position, color]) => gradient.addColorStop(position, color));
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(left[0].x, left[0].y);
    trace(left);
    right.reverse();
    ctx.lineTo(right[0].x, right[0].y);
    trace(right);
    ctx.closePath();
    ctx.fill();
  }

  function draw(now) {
    frame = 0;
    while (samples.length && now - samples[0].at > lifetime) samples.shift();
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, size, size);
    if (samples.length < 2) return;
    const head = samples.at(-1);
    const x = head.x - center, y = head.y - center;
    canvas.style.transform = `translate3d(${x}px, ${y}px, 0)`;
    ctx.translate(-x, -y);
    ctx.globalAlpha = Math.max(0, 1 - (now - head.at) / lifetime) ** 1.5;
    const path = latestPath();
    paintBand(path, 8, [[0, "#8c78d200"], [.46, "#a48cda2b"], [.78, "#efb29238"], [1, "#f8d6b400"]]);
    paintBand(path, 3.7, [[0, "#705acb00"], [.43, "#8d74cf64"], [.76, "#9b7eda92"], [1, "#f0b79a35"]]);
    paintBand(path, 1.3, [[0, "#9f86de00"], [.42, "#b69ae899"], [.72, "#fcf3ffc9"], [1, "#fff2d929"]]);
    frame = requestAnimationFrame(draw);
  }

  window.addEventListener("pointermove", (event) => {
    if (reduced.matches || document.hidden || event.pointerType !== "mouse") return;
    if (event.target.closest?.("button, a, form, input, textarea, canvas, h1, p, .aura-mark, .thread, .glass, .sheet-backdrop")) {
      if (samples.length) clear();
      return;
    }
    const x = event.clientX, y = event.clientY, at = performance.now();
    const previous = samples.at(-1);
    if (previous && at - previous.at > 84) clear();
    else if (previous && Math.hypot(x - previous.x, y - previous.y) < 2) return;
    samples.push({ x, y, at });
    if (samples.length > 32) samples.shift();
    if (!frame) frame = requestAnimationFrame(draw);
  }, { passive: true });
  window.addEventListener("resize", resize, { passive: true });
  window.addEventListener("scroll", clear, { passive: true });
  document.documentElement.addEventListener("pointerleave", clear);
  reduced.addEventListener("change", clear);
  document.addEventListener("visibilitychange", clear);
  resize();
})();
