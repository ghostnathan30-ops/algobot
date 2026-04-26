# UI Vendor Libraries — AlgoBot Dashboard

All files are served locally from `/static/vendor/`. No CDN required.

## HTML Import Snippet

Paste this block into the `<head>` of any dashboard HTML file:

```html
<!-- ── Styles ─────────────────────────────────────────── -->
<link rel="stylesheet" href="/static/vendor/tailwind.min.css">
<link rel="stylesheet" href="/static/vendor/apexcharts.min.css">
<link rel="stylesheet" href="/static/vendor/swiper-bundle.min.css">
<link rel="stylesheet" href="/static/vendor/aos.css">

<!-- ── Scripts (before </body>) ──────────────────────── -->
<script src="/static/vendor/lightweight-charts.standalone.production.js"></script>
<script src="/static/vendor/chart.min.js"></script>
<script src="/static/vendor/apexcharts.min.js"></script>
<script src="/static/vendor/gsap.min.js"></script>
<script src="/static/vendor/gsap-scrolltrigger.min.js"></script>
<script src="/static/vendor/swiper-bundle.min.js"></script>
<script src="/static/vendor/countup.min.js"></script>
<script src="/static/vendor/aos.js"></script>
```

## Library Reference

| File | Library | Version | Use Case |
|------|---------|---------|----------|
| `tailwind.min.css` | Tailwind CSS | 3.4.1 | Utility-first layout & styling |
| `lightweight-charts.standalone.production.js` | TradingView Lightweight Charts | 4.1.3 | Candlestick / equity line charts |
| `chart.min.js` | Chart.js | 4.4.2 | Donut charts, bar charts, win/loss |
| `apexcharts.min.js` + `.css` | ApexCharts | 3.46.0 | Animated interactive charts |
| `gsap.min.js` | GSAP | 3.12.5 | Smooth animations & transitions |
| `gsap-scrolltrigger.min.js` | GSAP ScrollTrigger | 3.12.5 | Scroll-driven animations |
| `swiper-bundle.min.js` + `.css` | Swiper.js | 11.0.7 | Strategy metric carousels / slideshows |
| `countup.min.js` | CountUp.js | 2.8.0 | Animated number counters (PnL, Win%) |
| `aos.js` + `.css` | AOS | 2.3.4 | Animate-on-scroll reveals |

## Quick Usage Examples

### CountUp (animated stat card)
```js
const cu = new countUp.CountUp('pnl-el', 4820.50, { prefix: '$', decimalPlaces: 2 });
cu.start();
```

### GSAP tab transition
```js
gsap.from('.tab-panel', { opacity: 0, y: 20, duration: 0.35, ease: 'power2.out' });
```

### Swiper carousel
```js
const swiper = new Swiper('.swiper', { loop: true, autoplay: { delay: 3000 } });
```

### AOS init
```js
AOS.init({ duration: 600, once: true });
// then add data-aos="fade-up" to any element
```

### TradingView Lightweight Chart (equity curve)
```js
const chart = LightweightCharts.createChart(document.getElementById('chart'), { width: 600, height: 300 });
const series = chart.addLineSeries({ color: '#10b981' });
series.setData([{ time: '2026-03-01', value: 50000 }, ...]);
```
