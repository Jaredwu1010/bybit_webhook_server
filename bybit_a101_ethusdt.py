<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <title>Webhook Logs Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body class="bg-gray-100 text-gray-800 p-6">
  <h1 class="text-2xl font-bold mb-4">📊 Webhook Logs Dashboard</h1>

  <!-- ✅ 選擇策略 ID + 手動 RESET -->
  <div class="mb-6 flex items-center space-x-4">
    <label for="strategy" class="font-semibold">選擇策略：</label>
    <select id="strategy" class="p-2 border rounded" onchange="filterTable()"></select>
    <button onclick="resetStrategy()" class="bg-red-500 hover:bg-red-600 text-white px-4 py-2 rounded">⛔ 手動 RESET</button>
  </div>

  <!-- ✅ 圖表區塊 -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
    <div class="bg-white p-4 rounded shadow">
      <h2 class="font-bold mb-2">📉 MDD 分佈圖</h2>
      <canvas id="mddChart" height="160"></canvas>
    </div>
    <div class="bg-white p-4 rounded shadow">
      <h2 class="font-bold mb-2">📈 策略績效曲線</h2>
      <canvas id="equityChart" height="160"></canvas>
    </div>
  </div>

  <!-- ✅ JSON 下載按鈕 -->
  <div class="mb-4">
    <a href="/download/log.json" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded">📥 下載 log.json</a>
  </div>

  <!-- ✅ Logs 資料表 -->
  <div class="overflow-auto rounded-xl shadow-lg border bg-white p-4">
    <table id="logsTable" class="min-w-full table-auto border-collapse text-sm">
      <thead>
        <tr class="bg-gray-200">
          <th class="p-2 border">時間</th>
          <th class="p-2 border">策略 ID</th>
          <th class="p-2 border">事件</th>
          <th class="p-2 border">Equity</th>
          <th class="p-2 border">Drawdown</th>
          <th class="p-2 border">下單動作</th>
        </tr>
      </thead>
      <tbody>
        {% for row in records %}
        <tr class="border-b">
          <td class="p-2 border">{{ row.timestamp }}</td>
          <td class="p-2 border">{{ row.strategy_id }}</td>
          <td class="p-2 border">{{ row.event }}</td>
          <td class="p-2 border">{{ row.equity or '' }}</td>
          <td class="p-2 border">{{ row.drawdown or '' }}</td>
          <td class="p-2 border">{{ row.order_action or '' }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

<script>
const originalRows = [...document.querySelectorAll("#logsTable tbody tr")];
const strategySelect = document.getElementById("strategy");

function filterTable() {
  const selected = strategySelect.value;
  document.querySelectorAll("#logsTable tbody tr").forEach(row => {
    row.style.display = selected === "all" || row.cells[1].innerText === selected ? "" : "none";
  });
}

function resetStrategy() {
  const sid = strategySelect.value;
  if (sid === "all") return alert("請選擇策略");
  const secret = prompt("請輸入密碼：");
  if (!secret) return;
  fetch("/webhook", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ strategy_id: sid, signal_type: "reset", secret })
  }).then(r => r.json()).then(d => alert("✅ 重設結果：" + JSON.stringify(d)));
}

window.onload = () => {
  // ⬇️ 建立選單（去除重複）
  const unique = [...new Set(originalRows.map(r => r.cells[1].innerText.replace(/_\d+$/, '')))]
  unique.forEach(s => {
    const opt = document.createElement("option");
    opt.value = s; opt.innerText = s; strategySelect.appendChild(opt);
  });
  const allOpt = document.createElement("option");
  allOpt.value = "all"; allOpt.innerText = "全部"; strategySelect.prepend(allOpt);
  strategySelect.value = "all";

  // 圖表資料處理
  const raw = [...document.querySelectorAll("#logsTable tbody tr")].map(row => ({
    strategy_id: row.cells[1].innerText,
    drawdown: parseFloat(row.cells[4].innerText || 0),
    equity: parseFloat(row.cells[3].innerText || 0),
    event: row.cells[2].innerText
  }));

  // MDD 柱狀圖
  const mddBins = [0, 2, 5, 10, 15, 20, 25];
  const mddCounts = mddBins.map((bin, i) => raw.filter(r => r.drawdown > bin && r.drawdown <= mddBins[i+1] || (i === mddBins.length - 1 && r.drawdown > bin)).length);
  new Chart(document.getElementById("mddChart"), {
    type: "bar",
    data: {
      labels: ["0~2", "2~5", "5~10", "10~15", "15~20", "20~25", ">25"],
      datasets: [{ label: "筆數", data: mddCounts }]
    }, options: { responsive: true, plugins: { legend: { display: false } } }
  });

  // Equity 總績效曲線
  const equityPoints = raw.filter(r => !isNaN(r.equity)).map(r => r.equity);
  const equityCurve = equityPoints.reduce((acc, cur, i) => { acc.push(i === 0 ? cur : acc[i-1] + (cur - acc[i-1])); return acc; }, []);
  new Chart(document.getElementById("equityChart"), {
    type: "line",
    data: {
      labels: equityPoints.map((_, i) => i + 1),
      datasets: [{ label: "績效", data: equityCurve, fill: false }]
    }, options: { responsive: true, plugins: { legend: { display: false } } }
  });
};
</script>
</body>
</html>
