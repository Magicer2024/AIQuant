// 前端渲染验证（一次性）：① 整段 dashboard 脚本语法检查 ② 用真实接口数据试渲染历史表现块
// 用法：node tools/_verify_surge_render.js
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'dashboard.html'), 'utf8');
const sample = JSON.parse(fs.readFileSync(path.join(ROOT, 'logs', '_surge_hist_sample.json'), 'utf8'));

let pass = 0, fail = 0;
const check = (cond, msg) => { if (cond) pass++; else { fail++; console.log('   FAIL:', msg); } };

// ① 全量 script 语法检查
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
check(scripts.length > 0, '未提取到内联 script');
scripts.forEach((code, i) => {
    try { new vm.Script(code, { filename: `inline-script#${i}` }); pass++; }
    catch (e) { fail++; console.log(`   FAIL: script#${i} 语法错误 -> ${e.message}`); }
});

// ② 抽取 renderSurgeHistory 并用真实数据渲染
const src = scripts.join('\n');
const start = src.indexOf('function renderSurgeHistory(');
check(start >= 0, '未找到 renderSurgeHistory');
let depth = 0, end = -1;
for (let i = src.indexOf('{', start); i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
}
check(end > 0, 'renderSurgeHistory 函数体括号不平衡');
const fnSrc = src.slice(start, end);

let renderFn;
try { renderFn = eval('(' + fnSrc + ')'); pass++; }
catch (e) { fail++; console.log('   FAIL: eval 失败 ->', e.message); }

if (renderFn) {
    let out = '';
    try { out = renderFn(sample.history, sample.history_summary); pass++; }
    catch (e) { fail++; console.log('   FAIL: 渲染抛错 ->', e.message); }

    // 结构断言
    check(out.includes('grid-column:1 / -1'), '容器未横跨整行（会被压进 grid 一格）');
    check(out.includes('历史表现'), '缺少标题');
    ['T+1', 'T+2', 'T+3', 'T+5'].forEach(t => check(out.includes(`>${t}<`), `缺少 ${t} 表头`));
    check(!out.includes('T+4'), '不应出现 T+4 列（与推荐复盘对齐）');
    ['代码', '名称', '信号日', '买入(次日开盘)', '收益', '状态'].forEach(h =>
        check(out.includes(`<th>${h}</th>`), `缺少表头 ${h}`));
    check((out.match(/<tr>/g) || []).length === sample.history.length + 1,
        `行数不符：${(out.match(/<tr>/g) || []).length - 1} vs ${sample.history.length}`);
    check((out.match(/metric-card/g) || []).length === 13,
        `指标卡数量应为 13（4 多期 + 9 成绩单），实为 ${(out.match(/metric-card/g) || []).length}`);
    // 已结算笔数与号位一致：持有中/放弃不给收益数字
    const holds = sample.history.filter(h => h.status !== 'clear');
    holds.forEach(h => check(!out.includes(`>+${h.return_pct}%`), `${h.code} 未结算却渲染了收益`));
    // 语法检查：标签配平
    const open = (out.match(/<div/g) || []).length, close = (out.match(/<\/div>/g) || []).length;
    check(open === close, `div 未配平：${open} vs ${close}`);
    const to = (out.match(/<tr>/g) || []).length, tc = (out.match(/<\/tr>/g) || []).length;
    check(to === tc, 'tr 未配平');

    // ③ 隔日了结口径的展示契约（2026-09-22 新增）
    const hs0 = sample.history_summary || {};
    check(out.includes(hs0.exit_mode_label || '隔日了结'), '缺少出场口径说明');
    if (hs0.closed > 0 && hs0.closed < 30) {
        check(out.includes('样本不足 30 条'), '样本 <30 未提示统计不可读');
    }
    const skipped = (sample.history || []).filter(h => h.status === 'skipped');
    if (skipped.some(h => (h.status_label || '').indexOf('破止损') >= 0)) {
        check(out.includes('放弃·开盘破止损'), '缺少「开盘破止损」放弃标签');
    }
    check((sample.history || []).every(h =>
            h.status !== 'clear' || h.exit_reason !== 'max_hold_days'
            || (h.hold_days || 0) <= (hs0.max_hold_days || 1)),
        `持仓天数超出 max_hold_days=${hs0.max_hold_days}`);

    fs.writeFileSync(path.join(ROOT, 'logs', '_surge_hist_render.html'), out, 'utf8');
    console.log(`\n渲染产物已写入 logs/_surge_hist_render.html（${out.length} 字符）`);
}

console.log(`\n${'='.repeat(46)}\n${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
