// 出场跟踪面板渲染校验（只读，不改动任何文件）
// 1) 从 dashboard.html 抽出真实渲染函数，喂 /investor/exit_advice 的真实 payload；
// 2) 断言表头列数 == 每行单元格数（历史上出现过 10 表头 / 11 单元格的整列错位）；
// 3) 断言输出里没有 undefined / NaN / [object Object]；
// 4) 打印首行与各类状态行，供人工核对列语义。
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
// DASH=... 可指向别的副本（例如校验 git 暂存区里的版本是否与前端一致）
const html = fs.readFileSync(process.env.DASH || path.join(ROOT, 'dashboard.html'), 'utf8');
const payload = JSON.parse(fs.readFileSync(path.join(ROOT, 'logs', '_exit_payload.json'), 'utf8'));

function slice(marker, endMarker) {
    const i = html.indexOf(marker);
    if (i < 0) throw new Error('找不到片段起点: ' + marker);
    const j = html.indexOf(endMarker, i);
    if (j < 0) throw new Error('找不到片段终点: ' + endMarker);
    return html.slice(i, j);
}

const code = [
    slice('function renderPickExitMini', '// 切换某 hz 出场跟踪明细折叠态'),
    slice('const EXIT_EXPAND_KEY', '// 策略规则命中'),
].join('\n');

global.localStorage = { getItem: () => null, setItem: () => {} };
global.openKline = () => {};
const sandbox = new Function(
    code + '\nreturn {renderPickExitMini, renderExitGroupBody, renderExitRows, ' +
    'exitStatChip, exitSplitChips, exitCapChip, EXIT_HORIZON_META};')();

const counts = (s, re) => (s.match(re) || []).length;
let failures = [];

['short', 'mid', 'long'].forEach(hz => {
    const list = (payload.groups || {})[hz] || [];
    const perf = (payload.summary || {}).perf || {};
    const p = perf[hz] || {};
    const table = sandbox.renderExitGroupBody(list, p);
    const th = counts(table, /<th[ >]/g);
    const headMatch = table.match(/<thead>[\s\S]*?<\/thead>/);
    const rows = headMatch ? [] : [];
    const bodyHtml = table.replace(/[\s\S]*<tbody>/, '').replace(/<\/tbody>[\s\S]*/, '');
    const trs = bodyHtml.split('</tr>').filter(t => t.indexOf('<td') >= 0);
    const tds = trs.map(t => counts(t, /<td[ >]/g));
    const bad = tds.filter(c => c !== th);
    console.log(`\n[${hz}] 段数 ${list.length} | 表头 ${th} 列 | 行 ${trs.length} 条 | ` +
                `单元格数集合 ${JSON.stringify([...new Set(tds)].sort())}`);
    if (!trs.length && list.length) failures.push(`${hz}: 有 ${list.length} 段却渲染出 0 行`);
    if (bad.length) failures.push(`${hz}: ${bad.length} 行与表头 ${th} 列不对齐（${bad.join(',')}）`);

    const mini = sandbox.renderPickExitMini(hz, list, perf, payload.start_date);
    const text = mini + table;
    [/\bundefined\b/, /\bNaN\b/, /\[object Object\]/].forEach(re => {
        const m = text.match(re);
        if (m) failures.push(`${hz}: 渲染结果含 ${m[0]}（请检查空值分支）`);
    });
    if (!/出场跟踪/.test(mini)) failures.push(`${hz}: 标题缺失`);
    if (payload.start_date && mini.indexOf(payload.start_date) < 0) {
        failures.push(`${hz}: 标题未使用后端 start_date（仍疑似硬编码）`);
    }
    if (hz === 'short') {
        const chips = sandbox.exitSplitChips(hz, list);
        console.log(`  分组 chips: ${(chips.match(/>[^<]+</g) || []).map(s => s.slice(1, -1)).join(' / ') || '（无）'}`);
        console.log(`  统计 chips: ${(sandbox.exitStatChip(hz, perf).match(/>[^<]+</g) || []).map(s => s.slice(1, -1)).join(' / ')}`);
        console.log(`  max_hold = ${p.max_hold}`);
    }
    // 展示层名额标注（2026-10-14）：hidden_n>0 时 chips 与明细行标签都必须出现
    if ((p.hidden_n || 0) > 0) {
        const capChip = sandbox.exitCapChip(hz, perf);
        if (!capChip) failures.push(`${hz}: perf.hidden_n=${p.hidden_n} 但展示层 chips 未渲染`);
        const nTag = (table.match(/未展示/g) || []).length;
        const nHidden = list.filter(it => it.reco_shown === false).length;
        if (nTag !== nHidden) {
            failures.push(`${hz}: 行内「未展示」标签 ${nTag} 个 ≠ reco_shown=false 的 ${nHidden} 段`);
        }
        console.log(`  展示层 chips: ${(capChip.match(/>[^<]+</g) || []).map(s => s.slice(1, -1)).join(' / ')}` +
                    ` | 行内标签 ${nTag}/${nHidden} 段`);
    }
});

// 抽样打印：每个周期取一条已卖出 + 短线的 持仓/未成交 各一行，
// 人工核对「止损/最高浮盈/市价」在三周期字段集不同（stop_loss vs stop_price）时都成立
const short = payload.groups.short || [];
const sample = [];
['clear', 'hold', 'expired', 'watching'].forEach(st => {
    const it = short.find(x => x.status === st);
    if (it) sample.push(it);
});
['mid', 'long'].forEach(hz => {
    const it = (payload.groups[hz] || []).find(x => x.status === 'clear');
    if (it) sample.push(it);
});
const header = (sandbox.renderExitGroupBody(sample, (payload.summary.perf || {}).short))
    .match(/<thead>[\s\S]*?<\/thead>/)[0]
    .replace(/<\/?th[^>]*>/g, '|').replace(/\|+/g, ' | ').trim();
console.log('\n表头：' + header);
console.log('\n抽样行（已剥标签，按列顺序）：');
sample.forEach(it => {
    const row = sandbox.renderExitRows([it], (payload.summary.perf || {}).short.max_hold);
    const cells = row.replace(/<[^>]+>/g, ' ')
        .replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
    console.log(`  ${it.status.padEnd(8)} → ${cells}`);
});

console.log('\n' + (failures.length ? '❌ 校验失败：\n  - ' + failures.join('\n  - ')
                                    : '✅ 表头/单元格列数一致，无 undefined/NaN，start_date 取后端值'));
process.exit(failures.length ? 1 : 0);
