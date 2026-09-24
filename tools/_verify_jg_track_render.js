// 机构席位建仓跟踪 · 前端渲染验证
// ① dashboard 内联脚本语法检查 ② 用真实接口数据渲染表格与指标卡并做结构断言
// 用法：node tools/_verify_jg_track_render.js
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'dashboard.html'), 'utf8');
const sample = JSON.parse(fs.readFileSync(path.join(ROOT, 'logs', '_jg_track_sample.json'), 'utf8'));
const sorted = JSON.parse(fs.readFileSync(path.join(ROOT, 'logs', '_jg_track_sample_sorted.json'), 'utf8'));
const items = sample.items || [];

let pass = 0, fail = 0;
const check = (cond, msg) => { if (cond) pass++; else { fail++; console.log('   FAIL:', msg); } };

// ① 内联脚本语法检查
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
check(scripts.length > 0, '未提取到内联 script');
scripts.forEach((code, i) => {
    try { new vm.Script(code, { filename: `inline-script#${i}` }); pass++; }
    catch (e) { fail++; console.log(`   FAIL: script#${i} 语法错误 -> ${e.message}`); }
});
const src = scripts.join('\n');

// ② 抽取三个函数并在 mock document 下执行
function extract(name) {
    const start = src.indexOf(`function ${name}(`);
    if (start < 0) return null;
    let depth = 0;
    for (let i = src.indexOf('{', start); i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
    }
    return null;
}
// loadJgTrack 是 async，不能与同步函数混在一段里执行（await 会整段报错），只查存在性与接线
const loadFn = extract('loadJgTrack');
check(!!loadFn, '未找到函数 loadJgTrack');
check(loadFn.includes('lhb_jg_track'), 'loadJgTrack 未调用 lhb_jg_track 接口');
check(loadFn.includes('jg-track'), 'loadJgTrack 未注册 RequestManager key');
check(/await\s+Promise\.all\([\s\S]*?loadJgTrack\(\)/.test(src), 'loadJgTrack 未接入首页并行加载');

const fns = ['_jgFmtAmt', 'renderJgTable', 'renderJgMetrics'].map(n => {
    const f = extract(n);
    check(!!f, `未找到函数 ${n}`);
    return f || '';
});

const els = {};
const ctx = {
    document: {
        getElementById: id => els[id] || (els[id] = { innerHTML: '', textContent: '', value: '' }),
    },
    console,
};
vm.createContext(ctx);
try { vm.runInContext(fns.join('\n'), ctx); pass++; }
catch (e) { fail++; console.log('   FAIL: 函数定义执行失败 ->', e.message); }

// ③ 表格渲染
let out = '';
try { out = ctx.renderJgTable(items); pass++; }
catch (e) { fail++; console.log('   FAIL: renderJgTable 抛错 ->', e.message); }

check((out.match(/<tr/g) || []).length === items.length + 1,
    `行数不符：${(out.match(/<tr/g) || []).length - 1} vs ${items.length}`);
['股票', '机构净买', '累计净买额', '占流通市值', '成本估算 / 价格带',
 '现价', '现价 vs 成本', '机构 买/卖', '最后上榜'].forEach(h =>
    check(out.includes(`<th>${h}</th>`), `缺少表头 ${h}`));
check((out.match(/<div/g) || []).length === (out.match(/<\/div>/g) || []).length,
    `div 未配平`);
check((out.match(/<tr/g) || []).length === (out.match(/<\/tr>/g) || []).length, 'tr 未配平');
check(out.includes('不是收益信号'), '缺少「不是收益信号」的定位声明');
check(out.includes('机构被套'), '缺少「现价低于机构成本＝机构被套」的风险说明');
check(out.includes('估算值'), '未声明成本为估算值');

// 金额格式化
check(ctx._jgFmtAmt(196000000) === '1.96亿', `亿级格式化错误: ${ctx._jgFmtAmt(196000000)}`);
check(ctx._jgFmtAmt(5500000) === '550万', `万级格式化错误: ${ctx._jgFmtAmt(5500000)}`);
check(ctx._jgFmtAmt(null) === '--', '空值未显示 --');

// 每行必须含成本或 --（不允许空白单元格）
items.forEach(it => {
    const hasCost = String(it.cost_est);
    check(out.includes(`>${Number(it.cost_est).toFixed(2)}<`) ||
          (it.cost_est == null && out.includes('--')),
        `${it.code} 成本未渲染`);
});
// 偏离符号与颜色一致（A 股红涨绿跌：高于成本 positive，低于 negative）
items.filter(x => x.vs_cost != null).forEach(it => {
    const want = it.vs_cost >= 0 ? 'positive' : 'negative';
    const txt = (it.vs_cost >= 0 ? '+' : '') + Number(it.vs_cost).toFixed(2) + '%';
    check(out.includes(`<td class="${want}"><b>${txt}</b></td>`),
        `${it.code} 偏离 ${txt} 未带 ${want} 类`);
});

// ④ 排序：vs_cost 升序（现价低于成本优先）
const sv = (sorted.items || []).map(x => x.vs_cost).filter(v => v != null);
let asc = true;
for (let i = 1; i < sv.length; i++) if (sv[i] < sv[i - 1]) { asc = false; break; }
check(asc, `sort=vs_cost 未按升序返回（前 5：${sv.slice(0, 5)}）`);

// ⑤ 指标卡
try { ctx.renderJgMetrics(sample); pass++; }
catch (e) { fail++; console.log('   FAIL: renderJgMetrics 抛错 ->', e.message); }
const mHtml = (els.jgTrackMetrics || {}).innerHTML || '';
check((mHtml.match(/metric-card/g) || []).length === 4, '指标卡应为 4 个');
['命中', '现价高于机构成本', '现价低于机构成本', '无行情未纳入'].forEach(l =>
    check(mHtml.includes(l), `缺少指标卡「${l}」`));

// ⑥ 空结果分支不报错
try { ctx.renderJgTable([]); pass++; }
catch (e) { fail++; console.log('   FAIL: 空数据渲染抛错 ->', e.message); }

fs.writeFileSync(path.join(ROOT, 'logs', '_jg_track_render.html'), out, 'utf8');
console.log(`\n样本 ${items.length} 行，渲染产物 ${out.length} 字符 -> logs/_jg_track_render.html`);
console.log(`\n${'='.repeat(46)}\n${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
