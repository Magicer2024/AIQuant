// 验证 exitStatChip 渲染（从 dashboard.html 提取真实函数体执行，非重写）
const fs = require('fs');
const src = fs.readFileSync('dashboard.html', 'utf8');

// 按大括号配平提取函数体
function extract(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到 ' + name);
  const j = src.indexOf('{', i);
  let d = 0, k = j;
  for (; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (d === 0) break; }
  }
  return src.slice(i, k + 1);
}

const code = extract('exitStatChip');
eval(code);

let pass = 0, fail = 0;
function ok(cond, msg) {
  if (cond) { pass++; console.log('  PASS  ' + msg); }
  else { fail++; console.log('  FAIL  ' + msg); }
}

console.log('=== exitStatChip 渲染验证 ===');

// 1. 正常负收益
let h = exitStatChip('short', { short: { segments: 52, hold: 1, clear: 48, avg_pct: -0.96, pnl_at_pos: -9.4, pos_pct: 20, win_rate: 36.7, comp_pct: -42.5, cum_pnl: -47.2 } });
ok(h.includes('平均每笔 -0.96%'), '平均每笔渲染');
ok(h.includes('按仓位累计 -9.4%'), '按仓位累计渲染');
ok(h.includes('胜率 36.7%'), '胜率渲染（不再是"累计胜率"）');
ok(!h.replace(/title="[^"]*"/g, '').includes('等权累计'), '可见文案已无"等权累计"（仅 tooltip 保留弃用说明）');
ok(h.includes('已弃用'), 'tooltip 保留了弃用说明');
ok(h.includes('negative'), '负值带 negative 类');
ok(h.includes('Σ(单笔收益 × 20%)'), 'tooltip 说明含仓位公式');
ok(h.includes('已卖出 48 + 持仓中 1'), 'tooltip 笔数拆分正确');

// 2. 正收益
h = exitStatChip('mid', { mid: { segments: 122, hold: 36, clear: 86, avg_pct: 0.35, pnl_at_pos: 4.2, pos_pct: 20, win_rate: 55.1 } });
ok(h.includes('平均每笔 +0.35%'), '正值带 + 号');
ok(h.includes('按仓位累计 +4.2%'), '正值累计带 + 号');
ok(h.includes('positive'), '正值带 positive 类');

// 3. 边界：缺字段不炸
h = exitStatChip('long', {});
ok(typeof h === 'string', '空 perf 不抛异常');
h = exitStatChip('long', null);
ok(typeof h === 'string', 'perf=null 不抛异常');
h = exitStatChip('long', { long: { segments: 3, hold: 3, clear: 0, avg_pct: null, pnl_at_pos: null, win_rate: null } });
ok(h === '', '全 null 时返回空串');

// 4. 向后兼容：老接口只返回 comp_pct 时不再误渲染成"等权累计"
h = exitStatChip('short', { short: { segments: 10, hold: 2, clear: 8, avg_pct: -1.5, comp_pct: -30, cum_pnl: -12, win_rate: 30 } });
ok(!h.includes('等权累计') && !h.includes('-30'), '缺 pnl_at_pos 时不回退到旧的顺序复利口径');

console.log(`\n结果: ${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
