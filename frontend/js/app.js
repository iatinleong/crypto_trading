const API = 'http://localhost:8000';

let chart, candleSeries, volumeSeries;
let ws = null;
let currentSymbol = 'BTCUSDT';
let currentInterval = '1m';
let currentSide = 'BUY';
let currentCandle = null;      // 目前開著的那根 K 棒
let lastChartUpdate = 0;       // 節流：最多 100ms 更新一次圖表

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
  });
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

  // 連本地後端 WS（後端透過 REST poll Binance 再推送）
  ws = new WebSocket(`ws://localhost:8000/ws/${currentSymbol}/${currentInterval}`);

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
  } catch (e) { /* ignore */ }
}

// ── 持倉 ───────────────────────────────────────────────────────────────────

async function refreshPositions() {
  try {
    const res = await fetch(`${API}/api/positions`);
    const positions = await res.json();
    const tbody = document.getElementById('positions-body');
    if (!positions.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">無持倉</td></tr>';
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
      </tr>`;
    }).join('');
  } catch (e) { /* ignore */ }
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

  if (!qty || qty <= 0)              { showMsg('請輸入數量', 'err'); return; }
  if (type === 'LIMIT' && (!price || price <= 0)) { showMsg('請輸入限價', 'err'); return; }

  const body = {
    symbol: currentSymbol,
    side: currentSide,
    order_type: type,
    quantity: qty,
    leverage,
    ...(type === 'LIMIT' ? { price } : {}),
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
  if (!confirm('確定重置帳戶？所有持倉和委託單將清除，餘額重設為 $10,000。')) return;
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
  loadKlines(); connectWS(); refreshTicker(); refreshPositions(); refreshOrders();
});

document.getElementById('interval-select').addEventListener('change', e => {
  currentInterval = e.target.value;
  loadKlines(); connectWS();
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

// ── 定期刷新委託單（不走 tick，因為限價單觸發後需要更新）─────────────────
setInterval(refreshOrders, 3000);
setInterval(refreshTicker, 5000);

// ── 啟動 ───────────────────────────────────────────────────────────────────

initChart();
loadKlines();
connectWS();
refreshTicker();
refreshAccount();
refreshPositions();
refreshOrders();
