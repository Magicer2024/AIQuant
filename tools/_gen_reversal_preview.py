"""生成「反转首日观察」面板的静态预览（reports/reversal_panel_preview.html + .png）。

与旧版的区别：**数据来自真实路由**（Flask test_client 调 /api/investor/reversal_picks），
不再用 mock —— 预览即线上实际渲染结果，可肉眼直接验收（含「实盘跟踪」组）。
渲染仍复用 dashboard.html 里的**真实 render 函数 + 真实 CSS**，保证与线上同构。

用法：python tools/_gen_reversal_preview.py [--limit 12]
"""
import argparse
import io
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=12)
ap.add_argument("--hist-days", type=int, default=60,
                help="「历史表现」窗口（与前端 REVERSAL_HISTORY_DAYS 一致）")
args = ap.parse_args()

# ── 1) 真实数据：走真实蓝图（hermes venv 无 qlib，不能 import app）───────────
from flask import Flask
from routes.investor import investor_bp

_app = Flask(__name__)
_app.register_blueprint(investor_bp, url_prefix="/api/investor")
with _app.test_client() as client:
    resp = client.get(f"/api/investor/reversal_picks?limit={args.limit}")
    payload = (resp.get_json() or {}).get("data") or {}
    hresp = client.get(f"/api/investor/reversal_history?days={args.hist_days}&limit=600")
    hist = (hresp.get_json() or {}).get("data") or {}
print(f"GET /api/investor/reversal_picks?limit={args.limit} → HTTP {resp.status_code}")
print(f"  date={payload.get('date')} count={payload.get('count')} "
      f"source={payload.get('source')} tracked.total={(payload.get('tracked') or {}).get('total')}")
print(f"GET /api/investor/reversal_history?days={args.hist_days} → HTTP {hresp.status_code}")
print(f"  range={hist.get('range')} total={hist.get('total')} "
      f"win={hist.get('summary', {}).get('win_rate')}% "
      f"breakdown={[(b['label'], b['n']) for b in (hist.get('exit_breakdown') or [])]}")

# ── 2) 取真实 render 函数 + CSS ────────────────────────────────────────────
html = io.open("dashboard.html", encoding="utf-8").read()
js = html[html.index("// ── 反转首日观察"):
          html.index("// ─── 个股深度 · 买卖建议")].rstrip()
css = io.open("static/css/dashboard.css", encoding="utf-8").read()

# hint 文案按前端同逻辑拼（面板 hint 由 loadReversalPicks 写，预览里直接复算）
items = payload.get("items") or []
_mp = items[0].get("mkt_pct_day") if items else None
_src = payload.get("source")
_src_txt = (" · 来源：历史回填" if _src == "hist"
            else (" · 来源：实时+历史" if _src == "mixed" else ""))
hint = (f"信号日 {payload.get('date')}"
        + (f"（全市场均涨 {'+' if _mp >= 0 else ''}{_mp:.2f}%）" if _mp is not None else "")
        + " · 首日大阳 × 放量 × 首破 MA20 · 只标位置不推荐买入 · 观察池" + _src_txt)

_tracked_n = (payload.get("tracked") or {}).get("total") or 0
note = (f"<b>⚙ 真实数据预览</b>：数据来自 <code>GET /api/investor/reversal_picks"
        f"?limit={args.limit}</code>（HTTP {resp.status_code}）+ "
        f"<code>GET /api/investor/reversal_history?days={args.hist_days}</code>"
        f"（HTTP {hresp.status_code}），渲染复用 dashboard.html 的真实 render 函数 + 真实 CSS"
        f"（含「历史表现」列表的展开态 —— 线上默认收起、点击才取数）。<br>"
        f"信号日 <b>{payload.get('date')}</b> · 卡片 {len(items)} 只 · "
        f"数据来源 <b>{_src}</b> · 实盘跟踪组 <b>{_tracked_n} 条</b>（90 天窗）· "
        f"历史表现 <b>{hist.get('total')} 条</b>"
        f"（{args.hist_days} 天窗 {hist.get('range', {}).get('start')} ~ "
        f"{hist.get('range', {}).get('end')}）—— 主板口径，独立成组。<br>"
        f"⚠ 预览里的「历史表现」列表<b>已勾选「只看已结算」</b>（线上默认不勾选，"
        f"默认为按信号日降序的全量），否则截图顶部会是一屏尚未出场的「持仓中」。")

out = f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>反转首日观察 · 面板预览（真实数据）</title><style>
{css}
body {{ padding: 22px; background: var(--bg-root); }}
.preview-note {{ max-width:1180px;margin:0 auto 14px;padding:10px 14px;border-radius:8px;
  background:rgba(60,180,255,0.08);border:1px solid rgba(60,180,255,0.30);
  font-size:12.5px;color:var(--text-secondary);line-height:1.7; }}
.wrap {{ max-width:1180px;margin:0 auto; }}
</style></head><body>
<div class="preview-note">{note}</div>
<div class="wrap"><div class="panel">
  <div class="panel-header">
    <h3>反转首日观察 · 首次站上 MA20 的低位入场</h3>
    <span class="text-muted" style="font-size:12px" id="reversalPicksHint">{hint}</span>
  </div>
  <div id="reversalPicks" class="action-plan-grid"></div>
</div></div>
<script>
const DATA = {json.dumps(payload, ensure_ascii=False)};
const HIST = {json.dumps(hist, ensure_ascii=False)};
function openKline() {{}};
{js}
const box = document.getElementById('reversalPicks');
const _items = DATA.items || [];
box.innerHTML = renderReversalStats(DATA.stats || {{}}, DATA.enabled)
    + (_items.length
        ? _items.map(renderReversalCard).join('')
        : '<div class="empty-state" style="grid-column:1 / -1">该信号日无「反转首日」候选</div>')
    + renderReversalTracked(DATA.tracked);
// 「历史表现」线上默认收起（懒加载）；预览直接把展开态渲染出来，便于一次看全。
// 同时勾上「只看已结算」：否则按信号日降序时截图顶部全是尚未出场的「--」，
// 看不出止盈/止损配色是否正确（线上该项默认不勾选）。
_revHist.open = true;
_revHist.settledOnly = true;
_revHist.data = HIST;
const _cb = document.getElementById('revHistSettled');
if (_cb) _cb.checked = true;
_hydrateRevHist();   // 走线上同一个注入口，预览 = 线上展开态
_syncRevHistBtn();
</script></body></html>"""

os.makedirs("reports", exist_ok=True)
html_path = "reports/reversal_panel_preview.html"
io.open(html_path, "w", encoding="utf-8").write(out)
print("written", html_path, len(out), "chars")

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
png_path = os.path.abspath("reports/reversal_panel_preview.png")
if os.path.exists(CHROME):
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--window-size=1300,2600",
                    "--screenshot=" + png_path.replace("\\", "/"),
                    "file:///" + os.path.abspath(html_path).replace("\\", "/")],
                   check=False, capture_output=True)
    print("screenshot", "OK" if os.path.exists(png_path) else "FAILED")
else:
    print("Chrome 未找到，跳过截图")
