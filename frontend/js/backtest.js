/* 賽博纏論 Backtest Dashboard */
'use strict';

// 前端跟後端是同一個 FastAPI 服務，一律用目前頁面的 origin，本機/雲端部署都適用
const API = window.location.origin;

// 週期對應每根K棒的小時數（跟後端 backtest_engine.py 的 INTERVAL_HOURS 保持一致）
const INTERVAL_HOURS = { '15m': 15/60, '30m': 0.5, '1h': 1, '4h': 4, '1d': 24 };
// 區間 token 對應的總小時數
const RANGE_HOURS = { '3m': 3*30*24, '6m': 6*30*24, '1y': 365*24, '2y': 2*365*24, '3y': 3*365*24 };

function resolveLimit(rawValue, interval) {
  if (RANGE_HOURS[rawValue]) {
    const hoursPerCandle = INTERVAL_HOURS[interval] || 1;
    return Math.ceil(RANGE_HOURS[rawValue] / hoursPerCandle);
  }
  return parseInt(rawValue);
}

// ── 圖表實例 ───────────────────────────────────────────────────────────────
let mainChart, candleSeries, volumeSeries;
let macdChart, macdHistSeries, macdLineSeries, macdSigSeries;
let equityChart, equitySeries;

// ── 回測數據緩存 ──────────────────────────────────────────────────────────
let cachedResult = null;   // 上次回測結果，用於 resize 重繪 overlay

// ═══════════════════════════════════════════════════════════════════════════
// 圖表初始化
// ═══════════════════════════════════════════════════════════════════════════

function initCharts() {
  const chartEl = document.getElementById('main-chart');
  mainChart = LightweightCharts.createChart(chartEl, {
    width:  chartEl.clientWidth,
    height: chartEl.clientHeight,
    layout: { background: { color: '#0d1117' }, textColor: '#c9d1d9' },
    grid:   { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#30363d' },
    timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false },
  });

  candleSeries = mainChart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350',
    borderVisible: false,
    wickUpColor: '#26a69a', wickDownColor: '#ef5350',
  });

  volumeSeries = mainChart.addHistogramSeries({
    priceScaleId: 'vol',
    color: '#26a69a',
    priceFormat: { type: 'volume' },
  });
  mainChart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.86, bottom: 0 } });

  // MACD chart
  const macdEl = document.getElementById('macd-chart');
  macdChart = LightweightCharts.createChart(macdEl, {
    width:  macdEl.clientWidth,
    height: macdEl.clientHeight,
    layout: { background: { color: '#0d1117' }, textColor: '#c9d1d9' },
    grid:   { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#30363d' },
    timeScale: { borderColor: '#30363d', visible: false },
    leftPriceScale: { visible: false },
  });

  macdHistSeries = macdChart.addHistogramSeries({ priceScaleId: 'right', color: '#26a69a', lineWidth: 1 });
  macdLineSeries = macdChart.addLineSeries({ color: '#58a6ff', lineWidth: 1, priceScaleId: 'right' });
  macdSigSeries  = macdChart.addLineSeries({ color: '#f0b90b', lineWidth: 1, priceScaleId: 'right' });

  // Equity chart
  const eqEl = document.getElementById('equity-chart');
  equityChart = LightweightCharts.createChart(eqEl, {
    width:  eqEl.clientWidth,
    height: eqEl.clientHeight,
    layout: { background: { color: '#161b22' }, textColor: '#8b949e' },
    grid:   { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Magnet },
    rightPriceScale: { borderColor: '#30363d' },
    timeScale: { borderColor: '#30363d', timeVisible: false },
  });
  equitySeries = equityChart.addAreaSeries({
    topColor:    'rgba(88,166,255,.3)',
    bottomColor: 'rgba(88,166,255,.0)',
    lineColor:   '#58a6ff',
    lineWidth: 1.5,
  });

  // 同步 main <-> macd 時間軸縮放/移動
  mainChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
    if (range) macdChart.timeScale().setVisibleLogicalRange(range);
  });
  macdChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
    if (range) mainChart.timeScale().setVisibleLogicalRange(range);
  });

  // Resize
  window.addEventListener('resize', onResize);
  // 重繪 overlay（主圖範圍改變時）
  mainChart.timeScale().subscribeVisibleLogicalRangeChange(() => drawOverlay());
}

function onResize() {
  const chartEl = document.getElementById('main-chart');
  mainChart.applyOptions({ width: chartEl.clientWidth, height: chartEl.clientHeight });

  const macdEl = document.getElementById('macd-chart');
  macdChart.applyOptions({ width: macdEl.clientWidth, height: macdEl.clientHeight });

  const eqEl = document.getElementById('equity-chart');
  equityChart.applyOptions({ width: eqEl.clientWidth, height: eqEl.clientHeight });

  syncCanvas();
  if (cachedResult) drawOverlay();
}

// ═══════════════════════════════════════════════════════════════════════════
// Canvas Overlay（笔 + 中枢矩形 + SL/TP 水平段）
// ═══════════════════════════════════════════════════════════════════════════

function syncCanvas() {
  const wrap = document.getElementById('chart-wrap');
  const canvas = document.getElementById('overlay-canvas');
  canvas.width  = wrap.clientWidth;
  canvas.height = wrap.clientHeight;
}

function timeToX(t) {
  return mainChart.timeScale().timeToCoordinate(t);
}
function priceToY(p) {
  return candleSeries.priceToCoordinate(p);
}

function drawOverlay() {
  const canvas = document.getElementById('overlay-canvas');
  const ctx    = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  if (!cachedResult) return;
  const { bis, duans, zhongshu, trades, signals } = cachedResult;

  // ── 中枢矩形 ───────────────────────────────────────────────────────────
  if (zhongshu) {
    for (const zs of zhongshu) {
      const x1 = timeToX(zs.start_time);
      const x2 = timeToX(zs.end_time);
      const y1 = priceToY(zs.zh);
      const y2 = priceToY(zs.zl);
      if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
      const w = x2 - x1;
      const h = y2 - y1;
      ctx.fillStyle   = 'rgba(240,185,11,0.07)';
      ctx.fillRect(x1, y1, w, h);
      ctx.strokeStyle = 'rgba(240,185,11,0.40)';
      ctx.lineWidth   = 1;
      ctx.strokeRect(x1, y1, w, h);
      // ZH / ZL 標籤
      ctx.fillStyle = 'rgba(240,185,11,0.7)';
      ctx.font = '9px monospace';
      ctx.fillText('ZH ' + fmt(zs.zh), x1 + 3, y1 + 10);
      ctx.fillText('ZL ' + fmt(zs.zl), x1 + 3, y2 - 3);
    }
  }

  // ── 笔線段 ─────────────────────────────────────────────────────────────
  if (bis) {
    ctx.lineWidth = 1.5;
    for (const bi of bis) {
      const x1 = timeToX(bi.start_time);
      const x2 = timeToX(bi.end_time);
      const y1 = priceToY(bi.start_price);
      const y2 = priceToY(bi.end_price);
      if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.strokeStyle = bi.direction === 'up'
        ? 'rgba(38,166,154,0.6)'
        : 'rgba(239,83,80,0.6)';
      ctx.setLineDash([]);
      ctx.stroke();
    }
  }

  // ── 線段 (Duan / Segment) ────────────────────────────────────────────────
  if (duans) {
    ctx.lineWidth = 3.5;
    for (const d of duans) {
      const x1 = timeToX(d.start_time);
      const x2 = timeToX(d.end_time);
      const y1 = priceToY(d.start_price);
      const y2 = priceToY(d.end_price);
      if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.strokeStyle = d.direction === 'up'
        ? 'rgba(240, 185, 11, 0.85)'  // 亮黃色
        : 'rgba(138, 43, 226, 0.85)'; // 紫色
      ctx.setLineDash([]);
      ctx.stroke();
    }
  }

  // ── 每筆交易的 SL / TP 水平線段 ─────────────────────────────────────────
  if (trades) {
    for (const t of trades) {
      const x1 = timeToX(t.entry_time);
      const x2 = timeToX(t.exit_time);
      if (x1 == null || x2 == null) continue;

      // SL 線（紅色虛線）
      const ySL = priceToY(t.sl);
      if (ySL != null) {
        ctx.beginPath();
        ctx.moveTo(x1, ySL);
        ctx.lineTo(x2, ySL);
        ctx.strokeStyle = 'rgba(239,83,80,0.55)';
        ctx.lineWidth   = 1;
        ctx.setLineDash([3, 3]);
        ctx.stroke();
      }
      // TP 線（綠色虛線）
      const yTP = priceToY(t.tp);
      if (yTP != null) {
        ctx.beginPath();
        ctx.moveTo(x1, yTP);
        ctx.lineTo(x2, yTP);
        ctx.strokeStyle = 'rgba(38,166,154,0.55)';
        ctx.lineWidth   = 1;
        ctx.setLineDash([3, 3]);
        ctx.stroke();
      }
      ctx.setLineDash([]);
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// 填充圖表資料
// ═══════════════════════════════════════════════════════════════════════════

function populateCharts(result) {
  const klines = result.klines;

  // 主圖 K 棒
  candleSeries.setData(klines.map(k => ({
    time: k.time, open: k.open, high: k.high, low: k.low, close: k.close,
  })));

  // 成交量
  volumeSeries.setData(klines.map(k => ({
    time: k.time, value: k.volume,
    color: k.close >= k.open ? 'rgba(38,166,154,.25)' : 'rgba(239,83,80,.25)',
  })));

  // MACD Histogram
  const histData = result.macd.histogram
    .map((v, i) => v != null ? {
      time: klines[i].time, value: v,
      color: v >= 0 ? 'rgba(38,166,154,.8)' : 'rgba(239,83,80,.8)',
    } : null)
    .filter(Boolean);
  macdHistSeries.setData(histData);

  // MACD Line
  macdLineSeries.setData(
    result.macd.macd_line
      .map((v, i) => v != null ? { time: klines[i].time, value: v } : null)
      .filter(Boolean)
  );

  // Signal Line
  macdSigSeries.setData(
    result.macd.signal_line
      .map((v, i) => v != null ? { time: klines[i].time, value: v } : null)
      .filter(Boolean)
  );

  // ── 信號標記（入場 + 出場） ───────────────────────────────────────────
  const markers = [];

  // 入場標記（帶信號類型文字）
  for (const sig of result.signals) {
    const isBuy = sig.side === 'BUY';
    markers.push({
      time:     sig.time,
      position: isBuy ? 'belowBar' : 'aboveBar',
      color:    isBuy ? '#4caf50' : '#f44336',
      shape:    isBuy ? 'arrowUp' : 'arrowDown',
      text:     sig.type,
      size:     1.2,
    });
  }

  // 出場標記（TP/SL 結果）
  for (const t of result.trades) {
    const isWin = t.pnl > 0;
    const isBuy = t.side === 'BUY';
    markers.push({
      time:     t.exit_time,
      position: isBuy ? 'aboveBar' : 'belowBar',
      color:    isWin ? '#26a69a' : '#ef5350',
      shape:    isBuy ? 'arrowDown' : 'arrowUp',
      text:     t.exit_reason + (isWin ? ' +' : ' -'),
      size:     0.9,
    });
  }

  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);

  // ── 資金曲線 ─────────────────────────────────────────────────────────
  equitySeries.setData(result.equity_curve.map(pt => ({
    time: pt.time, value: pt.equity,
  })));

  mainChart.timeScale().fitContent();
  macdChart.timeScale().fitContent();
  equityChart.timeScale().fitContent();
}

// ═══════════════════════════════════════════════════════════════════════════
// 填充統計面板
// ═══════════════════════════════════════════════════════════════════════════

function populateStats(s) {
  const ret  = s.total_return;
  const pnl  = s.total_pnl;
  const wr   = s.win_rate;
  const dd   = s.max_drawdown;

  set('s-return', (ret >= 0 ? '+' : '') + ret.toFixed(2) + '%', ret >= 0 ? 'pos' : 'neg');
  set('s-pnl',    (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2), pnl >= 0 ? 'pos' : 'neg');
  set('s-winrate', wr.toFixed(1) + '%', wr >= 50 ? 'pos' : wr >= 40 ? 'neutral' : 'neg');
  set('s-rr',      s.rr_ratio.toFixed(2), s.rr_ratio >= 1.5 ? 'pos' : s.rr_ratio >= 1 ? 'neutral' : 'neg');
  set('s-pf',      s.profit_factor >= 999 ? '∞' : s.profit_factor.toFixed(2),
                   s.profit_factor >= 1.5 ? 'pos' : s.profit_factor >= 1 ? 'neutral' : 'neg');
  set('s-trades',  s.total_trades, 'neutral');
  set('s-tpsl',    s.tp_count + ' / ' + s.sl_count, 'neutral');
  set('s-avgwin',  '+$' + s.avg_win.toFixed(2),  'pos');
  set('s-avgloss', '-$' + s.avg_loss.toFixed(2), 'neg');
  set('s-dd',      dd.toFixed(2) + '%',  dd < 10 ? 'pos' : dd < 20 ? 'neutral' : 'neg');
  set('s-fees',    '$' + s.total_fees.toFixed(2), 'neg');
  set('s-trading-fee', '$' + s.total_trading_fee.toFixed(2), 'neg');
  const ff = s.total_funding_fee;
  set('s-funding-fee', (ff >= 0 ? '$' : '+$') + Math.abs(ff).toFixed(2), ff >= 0 ? 'neg' : 'pos');
  set('s-init',    '$' + fmt(s.initial_capital), 'neutral');
  set('s-final',   '$' + fmt(s.final_capital), s.final_capital >= s.initial_capital ? 'pos' : 'neg');
}

function set(id, text, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'val ' + (cls || 'neutral');
}

function populateSigCounts(signals) {
  const counts = {};
  for (const s of signals) {
    counts[s.type] = (counts[s.type] || 0) + 1;
  }
  const el = document.getElementById('sig-counts');
  el.innerHTML = Object.entries(counts)
    .map(([t, c]) => `<span class="sig-chip ${t}">${t}×${c}</span>`)
    .join('');
}

// ═══════════════════════════════════════════════════════════════════════════
// 填充交易記錄
// ═══════════════════════════════════════════════════════════════════════════

function populateTrades(trades) {
  document.getElementById('trade-count').textContent = `共 ${trades.length} 筆`;
  const tbody = document.getElementById('trade-tbody');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="13" class="empty">無交易記錄（信號太少或資金不足）</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map((t, i) => {
    const isBuy  = t.side === 'BUY';
    const isWin  = t.pnl > 0;
    const pnlCls = isWin ? 'pos' : 'neg';
    const pillCls = t.exit_reason === 'TP' ? 'tp' : t.exit_reason === 'SL' ? 'sl' : 'cl';
    return `<tr>
      <td>${i + 1}</td>
      <td><span class="badge ${t.signal_type}">${t.signal_type}</span></td>
      <td class="${isBuy ? 'pos' : 'neg'}">${isBuy ? '多' : '空'}</td>
      <td>${fmtTime(t.entry_time)}</td>
      <td>${fmt(t.entry)}</td>
      <td>${fmt(t.exit_price)}</td>
      <td class="neg">${fmt(t.sl)}</td>
      <td class="pos">${fmt(t.tp)}</td>
      <td>${t.qty.toFixed(4)}</td>
      <td><span class="pill ${pillCls}">${t.exit_reason}</span></td>
      <td class="${pnlCls}">${isWin ? '+' : ''}${t.pnl.toFixed(2)}</td>
      <td class="neg">-${t.total_fees.toFixed(3)}</td>
      <td style="color:var(--muted);font-size:10px">${t.reason}</td>
    </tr>`;
  }).join('');
}

// ═══════════════════════════════════════════════════════════════════════════
// 主要入口：開始回測
// ═══════════════════════════════════════════════════════════════════════════

async function startBacktest() {
  const symbol   = document.getElementById('bt-symbol').value;
  const interval = document.getElementById('bt-interval').value;
  const limit    = resolveLimit(document.getElementById('bt-limit').value, interval);
  const capital  = parseFloat(document.getElementById('bt-capital').value);
  const leverage = parseInt(document.getElementById('bt-leverage').value);
  const riskPct  = parseFloat(document.getElementById('bt-risk').value);
  const takerFee = parseFloat(document.getElementById('bt-fee').value);

  if (!capital || capital < 100) {
    showStatus('本金至少 $100', 'err'); return;
  }

  showLoading(true);
  showStatus('正在向 Binance 取得歷史資料...', 'running');
  document.getElementById('btn-start').disabled = true;

  try {
    const res = await fetch(`${API}/api/backtest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol, interval, limit,
        initial_capital: capital,
        leverage,
        risk_pct: riskPct,
        taker_fee: takerFee,
      }),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || '後端錯誤');
    }

    const result = await res.json();
    cachedResult = result;

    showStatus('繪製圖表...', 'running');
    await new Promise(r => setTimeout(r, 20));  // let UI breathe

    populateCharts(result);
    populateStats(result.stats);
    populateSigCounts(result.signals);
    populateTrades(result.trades);

    syncCanvas();
    drawOverlay();

    const s = result.stats;
    showStatus(
      `完成｜${s.total_trades}筆交易  勝率${s.win_rate.toFixed(1)}%  ` +
      `收益${s.total_return >= 0 ? '+' : ''}${s.total_return.toFixed(2)}%`,
      'done'
    );
  } catch (e) {
    showStatus('錯誤：' + e.message, 'err');
    console.error(e);
  } finally {
    showLoading(false);
    document.getElementById('btn-start').disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════════════════════

function showLoading(on) {
  const el = document.getElementById('loading');
  el.classList.toggle('show', on);
}

function showStatus(msg, cls) {
  const el = document.getElementById('status-msg');
  el.textContent  = msg;
  el.className    = cls || '';
}

function fmt(n) {
  if (n == null) return '—';
  const f = parseFloat(n);
  if (f >= 1000) return f.toLocaleString('en-US', { maximumFractionDigits: 2 });
  if (f >= 1)    return f.toFixed(4);
  return f.toFixed(6);
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ═══════════════════════════════════════════════════════════════════════════
// 啟動
// ═══════════════════════════════════════════════════════════════════════════

initCharts();
syncCanvas();

// 自動執行一次（頁面開啟即回測）
startBacktest();
