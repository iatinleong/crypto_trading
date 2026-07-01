// 前端跟後端是同一個 FastAPI 服務，一律用目前頁面的 origin，本機/雲端部署都適用
const API = window.location.origin;

let chart, candleSeries, volumeSeries;
let ws = null;
let currentSymbol = 'BTCUSDT';
let currentInterval = '1m';
let currentSide = 'BUY';
let currentCandle = null;      // 目前開著的那根 K 棒
let lastChartUpdate = 0;       // 節流：最多 100ms 更新一次圖表
let cachedTrades = [];         // 目前幣對的已平倉交易紀錄（overlay 用）
let cachedOpenPosition = null; // 目前幣對的持倉（overlay 用）
let cachedLiveAnalysis = null; // 自動策略最近一輪算出的笔/線段/中枢（overlay 用）

// ── Chart ──────────────────────────────────────────────────────────────────

function initChart() {
  const el = document.getElementById('chart');
  chart = LightweightCharts.createChart(el, {
    width: el.clientWidth,
    height: el.clientHeight,
    layout: { background: { color: '#0d1117' }, textColor: '#c9d1d9' },
    grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#30363d' },
    timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350',
    borderVisible: false,
    wickUpColor: '#26a69a', wickDownColor: '#ef5350',
  });

  volumeSeries = chart.addHistogramSeries({
    color: '#26a69a',
    priceFormat: { type: 'volume' },
    priceScaleId: 'vol',
  });
  chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

  window.addEventListener('resize', () => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    syncOverlayCanvas();
    drawLiveOverlay();
  });

  chart.timeScale().subscribeVisibleLogicalRangeChange(() => drawLiveOverlay());
  syncOverlayCanvas();
}

// ── Overlay：進場點 / 出場點 / SL / TP ──────────────────────────────────────

function syncOverlayCanvas() {
  const panel = document.querySelector('.chart-panel');
  const canvas = document.getElementById('overlay-canvas');
  canvas.width  = panel.clientWidth;
  canvas.height = panel.clientHeight;
}

function timeToX(t) {
  return chart.timeScale().timeToCoordinate(t);
}
function priceToY(p) {
  return candleSeries.priceToCoordinate(p);
}

async function refreshTradeOverlayData() {
  try {
    const [tradesRes, posRes] = await Promise.all([
      fetch(`${API}/api/trades?symbol=${currentSymbol}&limit=50`),
      fetch(`${API}/api/positions`),
    ]);
    cachedTrades = await tradesRes.json();
    const positions = await posRes.json();
    cachedOpenPosition = positions.find(p => p.symbol === currentSymbol) || null;
    drawLiveOverlay();
  } catch (e) { /* ignore */ }
}

// 只有目前這個 symbol/interval 有啟動自動策略時，才能拿到它算出來的笔/線段/中枢
async function refreshLiveAnalysis() {
  try {
    const res = await fetch(`${API}/api/strategy/analysis?symbol=${currentSymbol}&interval=${currentInterval}`);
    cachedLiveAnalysis = res.ok ? await res.json() : null;
  } catch (e) {
    cachedLiveAnalysis = null;
  }
  drawLiveOverlay();
}

function drawLiveOverlay() {
  const canvas = document.getElementById('overlay-canvas');
  if (!canvas || !chart) return;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const nowTime = currentCandle ? currentCandle.time : Math.floor(Date.now() / 1000);
  const markers = [];

  // ── 自動策略目前看到的結構（中枢矩形 + 笔 + 線段） ──────────────────────
  if (cachedLiveAnalysis) {
    for (const zs of cachedLiveAnalysis.zhongshu || []) {
      const x1 = timeToX(zs.start_time), x2 = timeToX(zs.end_time);
      const y1 = priceToY(zs.zh), y2 = priceToY(zs.zl);
      if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
      ctx.fillStyle = 'rgba(240,185,11,0.07)';
      ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
      ctx.strokeStyle = 'rgba(240,185,11,0.40)'; ctx.lineWidth = 1;
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    }
    for (const bi of cachedLiveAnalysis.bis || []) {
      const x1 = timeToX(bi.start_time), x2 = timeToX(bi.end_time);
      const y1 = priceToY(bi.start_price), y2 = priceToY(bi.end_price);
      if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      ctx.strokeStyle = bi.direction === 'up' ? 'rgba(38,166,154,0.6)' : 'rgba(239,83,80,0.6)';
      ctx.lineWidth = 1.5; ctx.setLineDash([]); ctx.stroke();
    }
    for (const d of cachedLiveAnalysis.duans || []) {
      const x1 = timeToX(d.start_time), x2 = timeToX(d.end_time);
      const y1 = priceToY(d.start_price), y2 = priceToY(d.end_price);
      if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      ctx.strokeStyle = d.direction === 'up' ? 'rgba(240,185,11,0.85)' : 'rgba(138,43,226,0.85)';
      ctx.lineWidth = 3.5; ctx.stroke();
    }
  }

  function drawSlTpLine(x1, x2, sl, tp) {
    if (x1 == null || x2 == null) return;
    if (sl != null) {
      const y = priceToY(sl);
      if (y != null) {
        ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(x2, y);
        ctx.strokeStyle = 'rgba(239,83,80,0.6)'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]); ctx.stroke();
      }
    }
    if (tp != null) {
      const y = priceToY(tp);
      if (y != null) {
        ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(x2, y);
        ctx.strokeStyle = 'rgba(38,166,154,0.6)'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]); ctx.stroke();
      }
    }
    ctx.setLineDash([]);
  }

  // 已平倉交易：進場→出場的 SL/TP 線段 + 進出場箭頭
  for (const t of cachedTrades) {
    drawSlTpLine(timeToX(t.entry_time), timeToX(t.exit_time), t.sl, t.tp);
    const isBuy = t.side === 'BUY';
    markers.push({
      time: t.entry_time, position: isBuy ? 'belowBar' : 'aboveBar',
      color: isBuy ? '#4caf50' : '#f44336', shape: isBuy ? 'arrowUp' : 'arrowDown', text: '進場', size: 1,
    });
    const isWin = t.pnl > 0;
    markers.push({
      time: t.exit_time, position: isBuy ? 'aboveBar' : 'belowBar',
      color: isWin ? '#26a69a' : '#ef5350', shape: isBuy ? 'arrowDown' : 'arrowUp',
      text: t.exit_reason + (isWin ? ' +' : ' -'), size: 0.9,
    });
  }

  // 目前持倉：進場→現在的 SL/TP 線段（還沒平倉，右端延伸到最新一根K棒）
  if (cachedOpenPosition && cachedOpenPosition.entryTime) {
    drawSlTpLine(timeToX(cachedOpenPosition.entryTime), timeToX(nowTime), cachedOpenPosition.sl, cachedOpenPosition.tp);
    const isBuy = cachedOpenPosition.positionAmt > 0;
    markers.push({
      time: cachedOpenPosition.entryTime, position: isBuy ? 'belowBar' : 'aboveBar',
      color: isBuy ? '#4caf50' : '#f44336', shape: isBuy ? 'arrowUp' : 'arrowDown', text: '進場中', size: 1,
    });
  }

  markers.sort((a, b) => a.time - b.time);
  try { candleSeries.setMarkers(markers); } catch (_) {}
}

// ── K 線 ───────────────────────────────────────────────────────────────────

async function loadKlines() {
  try {
    const res = await fetch(`${API}/api/klines?symbol=${currentSymbol}&interval=${currentInterval}&limit=300`);
    const data = await res.json();
    candleSeries.setData(data.map(k => ({ time: k.time, open: k.open, high: k.high, low: k.low, close: k.close })));
    volumeSeries.setData(data.map(k => ({
      time: k.time, value: k.volume,
      color: k.close >= k.open ? 'rgba(38,166,154,.4)' : 'rgba(239,83,80,.4)',
    })));
    // 記住最後一根作為目前開著的 K 棒初始值
    if (data.length > 0) currentCandle = { ...data[data.length - 1] };
  } catch (e) {
    console.error('loadKlines:', e);
  }
}

// ── WebSocket（行情 + 帳戶即時推送）──────────────────────────────────────

function connectWS() {
  if (ws) { ws.onclose = null; ws.close(); }

  // 連後端 WS（後端透過 REST poll Binance 再推送）；https 頁面要用 wss
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${wsProtocol}//${window.location.host}/ws/${currentSymbol}/${currentInterval}`);

  const dot = document.getElementById('ws-status');
  const countEl = document.getElementById('ws-count');
  let msgCount = 0;
  let lastTick = 0;

  ws.onopen = () => { dot.classList.add('connected'); if (countEl) countEl.textContent = '0msg'; };
  ws.onclose = (e) => { dot.classList.remove('connected'); console.warn('[WS] closed', e.code, e.reason); setTimeout(connectWS, 3000); };
  ws.onerror = (e) => { dot.classList.remove('connected'); console.error('[WS] error', e); };

  ws.onmessage = ({ data }) => {
    const msg = JSON.parse(data);
    if (msg.type !== 'tick') return;

    msgCount++;
    if (countEl) countEl.textContent = msgCount + 'msg';

    // 更新價格
    document.getElementById('ticker-price').textContent = formatPrice(msg.price);

    // 更新 K 棒
    if (msg.kline) {
      currentCandle = msg.kline;
      try {
        candleSeries.update({ ...currentCandle });
        volumeSeries.update({
          time: currentCandle.time,
          value: currentCandle.volume,
          color: currentCandle.close >= currentCandle.open ? 'rgba(38,166,154,.4)' : 'rgba(239,83,80,.4)',
        });
      } catch (_) {}
    }

    // 更新帳戶 / 持倉（每次 tick 都更新，約 500ms 一次）
    refreshAccount();
    refreshPositions();
    drawLiveOverlay();   // 持倉線段的右端要跟著最新K棒延伸，用快取資料重繪即可（不用重新打API）
  };
}

// ── Ticker（24h 變化）─────────────────────────────────────────────────────

async function refreshTicker() {
  try {
    const res = await fetch(`${API}/api/ticker?symbol=${currentSymbol}`);
    const d = await res.json();
    const change = parseFloat(d.priceChangePercent);
    document.getElementById('ticker-price').textContent = formatPrice(parseFloat(d.lastPrice));
    const el = document.getElementById('ticker-change');
    el.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
    el.className = 'ticker-change ' + (change >= 0 ? 'pos' : 'neg');
  } catch (e) { /* ignore */ }
}

// ── 帳戶（每次 tick 觸發，近乎即時）─────────────────────────────────────

async function refreshAccount() {
  try {
    const res = await fetch(`${API}/api/account`);
    const d = await res.json();
    document.getElementById('acc-balance').textContent = `$${fmt(d.totalWalletBalance)}`;
    const pnl = d.totalUnrealizedProfit;
    const pnlEl = document.getElementById('acc-pnl');
    pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${fmt(pnl)}`;
    pnlEl.className = pnl >= 0 ? 'pos' : 'neg';
    document.getElementById('acc-avail').textContent = `$${fmt(d.availableBalance)}`;
    document.getElementById('acc-fees').textContent = `$${fmt(d.totalFees)}`;
  } catch (e) { /* ignore */ }
}

// ── 持倉 ───────────────────────────────────────────────────────────────────

async function refreshPositions() {
  try {
    const res = await fetch(`${API}/api/positions`);
    const positions = await res.json();
    const tbody = document.getElementById('positions-body');
    if (!positions.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="empty">無持倉</td></tr>';
      return;
    }
    tbody.innerHTML = positions.map(p => {
      const isLong = p.positionAmt > 0;
      const pnl = p.unRealizedProfit;
      return `<tr>
        <td>${p.symbol}</td>
        <td class="${isLong ? 'pos' : 'neg'}">${isLong ? '多' : '空'}</td>
        <td>${p.leverage}x</td>
        <td>${fmt(p.entryPrice)}</td>
        <td>${fmt(p.markPrice)}</td>
        <td>${Math.abs(p.positionAmt)}</td>
        <td class="${pnl >= 0 ? 'pos' : 'neg'}">${pnl >= 0 ? '+' : ''}${fmt(pnl)} (${p.percentage.toFixed(2)}%)</td>
        <td>${p.sl != null ? fmt(p.sl) : '—'}</td>
        <td>${p.tp != null ? fmt(p.tp) : '—'}</td>
        <td class="neg">${fmt(p.liqPrice)}</td>
        <td><button class="icon-btn" onclick="editSlTp('${p.symbol}', ${p.sl ?? 'null'}, ${p.tp ?? 'null'})">設定</button></td>
      </tr>`;
    }).join('');
  } catch (e) { /* ignore */ }
}

async function editSlTp(symbol, currentSl, currentTp) {
  const slInput = prompt(`設定 ${symbol} 止損（留空＝不設）`, currentSl ?? '');
  if (slInput === null) return;
  const tpInput = prompt(`設定 ${symbol} 止盈（留空＝不設）`, currentTp ?? '');
  if (tpInput === null) return;
  const sl = slInput.trim() === '' ? null : parseFloat(slInput);
  const tp = tpInput.trim() === '' ? null : parseFloat(tpInput);
  try {
    const res = await fetch(`${API}/api/position/${symbol}/sltp`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sl, tp }),
    });
    if (res.ok) refreshPositions();
    else { const err = await res.json(); alert(err.detail || '更新失敗'); }
  } catch (e) { alert('連線失敗'); }
}

// ── 委託單 ─────────────────────────────────────────────────────────────────

async function refreshOrders() {
  try {
    const res = await fetch(`${API}/api/orders?symbol=${currentSymbol}`);
    const orders = await res.json();
    const tbody = document.getElementById('orders-body');
    if (!orders.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">無委託單</td></tr>';
      return;
    }
    tbody.innerHTML = orders.map(o => `<tr>
      <td>${o.symbol}</td>
      <td class="${o.side === 'BUY' ? 'pos' : 'neg'}">${o.side === 'BUY' ? '買' : '賣'}</td>
      <td>${o.type}</td>
      <td>${fmt(parseFloat(o.price))}</td>
      <td>${o.origQty}</td>
      <td>${o.status}</td>
      <td><button class="cancel-btn" onclick="cancelOrder(${o.orderId})">撤銷</button></td>
    </tr>`).join('');
  } catch (e) { /* ignore */ }
}

// ── 下單 ───────────────────────────────────────────────────────────────────

async function placeOrder() {
  const qty      = parseFloat(document.getElementById('order-qty').value);
  const type     = document.getElementById('order-type').value;
  const price    = parseFloat(document.getElementById('order-price').value);
  const leverage = parseInt(document.getElementById('order-leverage').value);
  const slVal    = parseFloat(document.getElementById('order-sl').value);
  const tpVal    = parseFloat(document.getElementById('order-tp').value);

  if (!qty || qty <= 0)              { showMsg('請輸入數量', 'err'); return; }
  if (type === 'LIMIT' && (!price || price <= 0)) { showMsg('請輸入限價', 'err'); return; }

  const body = {
    symbol: currentSymbol,
    side: currentSide,
    order_type: type,
    quantity: qty,
    leverage,
    ...(type === 'LIMIT' ? { price } : {}),
    ...(slVal > 0 ? { sl: slVal } : {}),
    ...(tpVal > 0 ? { tp: tpVal } : {}),
  };

  try {
    const res = await fetch(`${API}/api/order`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const result = await res.json();
    if (res.ok) {
      showMsg(`✓ 訂單 #${result.orderId} 已送出`, 'ok');
      document.getElementById('order-qty').value = '';
      document.getElementById('order-price').value = '';
      document.getElementById('order-sl').value = '';
      document.getElementById('order-tp').value = '';
      refreshOrders();
      refreshAccount();
      refreshPositions();
    } else {
      showMsg(result.detail || '下單失敗', 'err');
    }
  } catch (e) {
    showMsg('連線失敗', 'err');
  }
}

async function cancelOrder(orderId) {
  if (!confirm('確定撤銷此委託單？')) return;
  try {
    await fetch(`${API}/api/order/${orderId}`, { method: 'DELETE' });
    refreshOrders();
  } catch (e) { alert('撤單失敗'); }
}

async function resetAccount() {
  if (!confirm('確定重置帳戶？所有持倉和委託單將清除，餘額重設為 $500。')) return;
  try {
    await fetch(`${API}/api/reset`, { method: 'POST' });
    refreshAccount();
    refreshPositions();
    refreshOrders();
    showMsg('帳戶已重置', 'ok');
  } catch (e) { alert('重置失敗'); }
}

// ── UI 事件 ────────────────────────────────────────────────────────────────

document.getElementById('symbol-select').addEventListener('change', e => {
  currentSymbol = e.target.value;
  loadKlines(); connectWS(); refreshTicker(); refreshPositions(); refreshOrders(); refreshStrategyStatus();
  refreshTradeOverlayData(); refreshLiveAnalysis();
});

document.getElementById('interval-select').addEventListener('change', e => {
  currentInterval = e.target.value;
  loadKlines(); connectWS(); refreshStrategyStatus();
  refreshTradeOverlayData(); refreshLiveAnalysis();
});

document.getElementById('order-type').addEventListener('change', e => {
  document.getElementById('price-row').style.display = e.target.value === 'LIMIT' ? 'flex' : 'none';
});

document.querySelectorAll('.side-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.side-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentSide = btn.dataset.side;
    const isLong = currentSide === 'BUY';
    const submitBtn = document.getElementById('submit-btn');
    submitBtn.textContent = isLong ? '做多 Long' : '做空 Short';
    submitBtn.className = 'submit-btn ' + (isLong ? 'long' : 'short');
  });
});

// ── 工具函式 ───────────────────────────────────────────────────────────────

function fmt(n) {
  if (n == null) return '—';
  return parseFloat(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function formatPrice(n) {
  if (!n) return '—';
  return parseFloat(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function showMsg(msg, type) {
  const el = document.getElementById('order-msg');
  el.textContent = msg;
  el.className = 'order-msg ' + type;
  setTimeout(() => { el.textContent = ''; el.className = 'order-msg'; }, 4000);
}

// ── 自動策略 ───────────────────────────────────────────────────────────────

async function refreshStrategyStatus() {
  try {
    const res = await fetch(`${API}/api/strategy/status`);
    const data = await res.json();
    const key = `${currentSymbol}/${currentInterval}`;
    const mine = data.armed.find(a => a.key === key);
    const btn = document.getElementById('strategy-toggle-btn');
    const statusEl = document.getElementById('strategy-status');
    if (mine) {
      btn.textContent = '■ 停止自動策略';
      btn.className = 'submit-btn short';
      let heartbeat = '尚未檢查過';
      if (mine.last_checked_at) {
        const secsAgo = Math.max(0, Math.floor(Date.now() / 1000 - mine.last_checked_at));
        heartbeat = secsAgo <= 45 ? `上次檢查 ${secsAgo}秒前 🟢` : `上次檢查 ${secsAgo}秒前 ⚠️可能已停止`;
      }
      statusEl.textContent = `${key} 運行中 · 風險${(mine.risk_pct*100).toFixed(1)}% · ${mine.leverage}x · ${heartbeat}` +
        (mine.last_signal_key ? ` · 上次訊號 ${mine.last_signal_key}` : '');
    } else {
      btn.textContent = '▶ 啟動自動策略';
      btn.className = 'submit-btn long';
      statusEl.textContent = '未啟動';
    }
  } catch (e) { /* ignore */ }
}

async function toggleStrategy() {
  const key = `${currentSymbol}/${currentInterval}`;
  const btn = document.getElementById('strategy-toggle-btn');
  const isRunning = btn.textContent.includes('停止');
  try {
    if (isRunning) {
      await fetch(`${API}/api/strategy/stop?symbol=${currentSymbol}&interval=${currentInterval}`, { method: 'POST' });
    } else {
      const risk_pct = parseFloat(document.getElementById('strategy-risk').value);
      const leverage = parseInt(document.getElementById('strategy-leverage').value);
      await fetch(`${API}/api/strategy/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: currentSymbol, interval: currentInterval, risk_pct, leverage }),
      });
    }
    refreshStrategyStatus();
  } catch (e) {
    document.getElementById('strategy-msg').textContent = '連線失敗';
  }
}

// ── 定期刷新委託單（不走 tick，因為限價單觸發後需要更新）─────────────────
setInterval(refreshOrders, 3000);
setInterval(refreshTicker, 5000);
setInterval(refreshStrategyStatus, 5000);
setInterval(refreshTradeOverlayData, 5000);
setInterval(refreshLiveAnalysis, 5000);

// ── 啟動 ───────────────────────────────────────────────────────────────────

initChart();
loadKlines();
connectWS();
refreshTicker();
refreshAccount();
refreshPositions();
refreshOrders();
refreshStrategyStatus();
refreshTradeOverlayData();
refreshLiveAnalysis();
