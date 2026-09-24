"""生成「机构席位建仓跟踪」栏目离线预览页（自包含样式，浅色主题）。

把前端渲染回归产物 logs/_jg_track_render.html 包进一个带样式的完整页面，
便于不启动 Flask 也能看到栏目真实长相。

用法：python tools/_make_jg_preview.py
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CSS = """
:root{
  --bg:#f2f4f8; --bg-card:#ffffff; --text:#1a1d2e; --text-secondary:#4a5170;
  --text-muted:#8a93b5; --border:rgba(16,20,40,0.10);
  --positive:#d93036; --negative:#0a9e5f; --gold:#c8901a;
}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--text);
  font:13px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.panel{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;
  padding:16px 18px;margin-bottom:18px}
.panel-header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.panel-header h3{margin:0;font-size:15px;font-weight:700}
.text-muted{color:var(--text-muted)}
.metrics-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.metric-card{background:#f7f8fc;border:1px solid var(--border);border-radius:8px;
  padding:10px 12px;text-align:center}
.metric-card .val{font-size:24px;font-weight:700;line-height:1.2}
.metric-card .lbl{font-size:11.5px;color:var(--text-muted);margin-top:2px}
.positive{color:var(--positive)!important;font-weight:600}
.negative{color:var(--negative)!important;font-weight:600}
.table-wrapper{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-weight:600;color:var(--text-secondary);font-size:11.5px;
  padding:7px 9px;border-bottom:1.5px solid var(--border);white-space:nowrap}
td{padding:7px 9px;border-bottom:1px solid var(--border);vertical-align:top}
tbody tr:hover{background:#f7f8fc}
.empty-state{text-align:center;padding:26px;color:var(--text-muted)}
.note{font-size:11.5px;color:var(--text-muted);line-height:1.7;margin-top:10px}
.legend{font-size:12px;color:var(--text-secondary);margin-bottom:12px;padding:8px 12px;
  background:#fff8e6;border:1px solid rgba(200,144,26,.35);border-radius:8px;line-height:1.7}
"""


def main():
    src = os.path.join(ROOT, "logs", "_jg_track_render.html")
    if not os.path.exists(src):
        raise SystemExit("先生成渲染产物：node tools/_verify_jg_track_render.js")
    body = open(src, encoding="utf-8").read()

    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>机构席位建仓跟踪 · 离线预览</title>
<style>{CSS}</style></head>
<body>
<div class="panel">
  <div class="panel-header">
    <h3>机构席位建仓跟踪 · 龙虎榜机构专用席位</h3>
    <span class="text-muted" style="font-size:12px">2026-07-01 ~ 2026-09-22 · 机构专用席位（单日口径）· 资金动向参考</span>
  </div>
  <div class="legend">
    <b>本栏目是数据表渲染产物（离线预览，非线上页面）</b>：<br>
    · 表格上方本应有 4 张指标卡（命中 / 现价高于成本 / 现价低于成本 / 无行情未纳入）与筛选下拉，此处只展示表格。<br>
    · <b>成本估算</b> = Σ(机构净买额 × 上榜日收盘价) / Σ(机构净买额)，只对净买为正的上榜日加权 —— 交易所不披露机构真实成交价，此为近似值。<br>
    · 颜色遵循 A 股惯例：<span class="positive">红 = 现价高于机构成本</span>　<span class="negative">绿 = 现价低于机构成本（机构被套）</span>
  </div>
  {body}
</div>
</body></html>"""

    out = os.path.join(ROOT, "logs", "jg_track_preview.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"预览页已生成：{out}（{len(page)} 字符）")


if __name__ == "__main__":
    main()
