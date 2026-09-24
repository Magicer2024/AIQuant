/**
 * 「反转首日 · 历史表现」前端状态机冒烟测试（无浏览器、无依赖）。
 *
 * 为什么需要它：dashboard.html 是单文件前端、无构建，语法检查（_check_dashboard_js.mjs）
 * 只能证明「没写错语法」，证明不了「点开之后真的显示出来」。这套逻辑有 4 个状态互相
 * 牵连（收起/展开 × 有无缓存 × 窗口 × 只看已结算），而面板整体 `box.innerHTML = ...`
 * 重绘会把展开态 DOM 冲掉 —— 2026-09-24 正是这样踩了一脚（按钮写着「收起」但内容是空的）。
 *
 * 做法：把 dashboard.html 里「反转首日」那段真实 JS 抽出来，在 node vm 里跑，
 * 用迷你 DOM（只需 id 索引 + style/innerHTML/checked/value）承接渲染产物。
 * 断言的是**状态与 DOM 的同步关系**，不是像素。
 *
 * 用法：node tools/_smoke_reversal_history.mjs
 */
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const html = fs.readFileSync(path.join(ROOT, 'dashboard.html'), 'utf8');

const START = '// ── 反转首日观察';
const END = '// ─── 个股深度 · 买卖建议';
const s = html.indexOf(START), e = html.indexOf(END);
if (s < 0 || e < 0 || e <= s) {
    console.error('❌ 未能从 dashboard.html 定位「反转首日」代码段（锚点变了？）');
    process.exit(2);
}
const js = html.slice(s, e);

// ── 迷你 DOM ────────────────────────────────────────────────────────────────
const els = {};
function mkEl(id) {
    if (!els[id]) {
        els[id] = {
            id, style: {}, checked: false, value: '', _html: '', _text: '',
            get innerHTML() { return this._html; },
            // 注入的 HTML 里若含 id=，登记成新元素，并解析它的内联 style / checked
            // （浏览器解析 DOM 的等价物）。⚠ 不解析的话 style.display 恒为 undefined，
            // 「收起时必须 display:none」这类断言就成了假通过。
            set innerHTML(v) {
                this._html = String(v);
                const re = /<[a-zA-Z]+([^>]*\bid="([^"]+)"[^>]*)>/g;
                let m;
                while ((m = re.exec(this._html))) {
                    const el = mkEl(m[2]);
                    const attrs = m[1];
                    const sm = /style="([^"]*)"/.exec(attrs);
                    if (sm) {
                        for (const d of sm[1].split(';')) {
                            const i = d.indexOf(':');
                            if (i < 0) continue;
                            const k = d.slice(0, i).trim()
                                .replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                            el.style[k] = d.slice(i + 1).trim();
                        }
                    }
                    if (/(^|\s)checked(\s|=|$)/.test(attrs)) el.checked = true;
                }
            },
            get textContent() { return this._text; },
            set textContent(v) { this._text = String(v); },
        };
    }
    return els[id];
}
const document = { getElementById: id => els[id] || null };

// ── 请求桩：记录调用，返回可切换的载荷 ──────────────────────────────────────
let calls = [];
let payload = null;
const RequestManager = {
    fetch(key, url) {
        calls.push({ key, url });
        return Promise.resolve({ data: JSON.parse(JSON.stringify(payload)) });
    },
};

const ctx = vm.createContext({
    console, document, RequestManager, API: '/api',
    openKline: () => {}, window: {}, setTimeout, Date, JSON, Math, Number, String,
    Object, Array, RegExp, parseInt, parseFloat, isNaN,
});

const FIXTURE = {
    days: 60, total: 4, truncated: false,
    range: { start: '2026-07-27', end: '2026-09-24' },
    summary: { total: 4, win_rate: 50, avg_return: 1.23, profit_factor: 1.4,
               best_return: 8.11, worst_return: -3.02,
               stop_loss_count: 1, take_profit_count: 1 },
    exit_breakdown: [
        { reason: 'trailing_stop', label: '移动止盈离场', tone: 'positive', n: 1 },
        { reason: 'stop_loss', label: '止损离场', tone: 'negative', n: 1 },
        { reason: null, label: '持仓中/待建仓', tone: 'holding', n: 2 },
    ],
    items: [
        { code: '600001', name: '甲', scan_date: '2026-09-24', entry_price: 10.0,
          ext_pct: 1.5, vol_ratio: 2.2, kdj_k: 52, exit_return: null,
          exit_reason: null, exit_date: null, exit_label: '持仓中', exit_tone: 'holding',
          settled: false, t1_return: null, t2_return: null, t3_return: null, t5_return: null },
        { code: '600002', name: '乙', scan_date: '2026-09-23', entry_price: 11.0,
          ext_pct: 2.5, vol_ratio: 2.4, kdj_k: 61, exit_return: null,
          exit_reason: null, exit_date: null, exit_label: '持仓中', exit_tone: 'holding',
          settled: false, t1_return: 0.5, t2_return: null, t3_return: null, t5_return: null },
        { code: '600003', name: '丙', scan_date: '2026-09-22', entry_price: 12.0,
          ext_pct: 3.5, vol_ratio: 2.6, kdj_k: 85, exit_return: 8.11,
          exit_reason: 'trailing_stop', exit_date: '2026-09-25', exit_label: '移动止盈离场',
          exit_tone: 'positive', settled: true,
          t1_return: 2.1, t2_return: 4.1, t3_return: 6.6, t5_return: 8.11 },
        { code: '600004', name: '丁', scan_date: '2026-09-21', entry_price: 13.0,
          ext_pct: 4.5, vol_ratio: 2.8, kdj_k: 44, exit_return: -3.02,
          exit_reason: 'stop_loss', exit_date: '2026-09-23', exit_label: '止损离场',
          exit_tone: 'negative', settled: true,
          t1_return: -1.1, t2_return: -2.2, t3_return: -3.02, t5_return: null },
    ],
};
const TRACKED = {
    total: 490, win_rate: 46.4, avg_return: 0.62, profit_factor: 1.42,
    stop_loss_count: 179, take_profit_count: 179, t1: { win_rate: 44.3 },
};

const api = {};
vm.runInContext(
    js + `\n;globalThis.__api = { renderReversalTracked, renderReversalHistoryShell,
        renderReversalHistory, toggleReversalHistory, onReversalHistDaysChange,
        onReversalHistSettledChange, _hydrateRevHist, _revHist,
        REVERSAL_HISTORY_DAYS, REVERSAL_LIMIT };`,
    ctx, { filename: 'dashboard-reversal.js' });
Object.assign(api, ctx.__api);

const tick = () => new Promise(r => setTimeout(r, 0));
// 让「历史表现」列表处于**展开且加载新载荷**的状态。
// ⚠ 直接 toggle 是错的：列表可能已经展开，再 toggle 等于收起（曾因此写出假失败）。
async function showList(p) {
    payload = p;
    if (api._revHist.open) api.toggleReversalHistory();   // 先确保收起
    api.toggleReversalHistory();                          // 再展开 ⇒ 必发请求
    await tick();
}
let failed = 0;
function ck(name, cond, extra) {
    console.log(`  ${cond ? '✅' : '❌'} ${name}${!cond && extra ? '  → ' + extra : ''}`);
    if (!cond) failed++;
}

console.log('='.repeat(96));
console.log('「反转首日 · 历史表现」前端状态机冒烟测试');
console.log('='.repeat(96));
console.log(`抽取代码段 ${js.length} 字符 · 默认窗口 REVERSAL_HISTORY_DAYS=${api.REVERSAL_HISTORY_DAYS}`);

// ── 1) 初始渲染：容器必须收起（懒加载，不许一上来就请求）──────────────────
const panel = mkEl('reversalPicks');
panel.innerHTML = api.renderReversalTracked(TRACKED);
ck('shell 渲染出 revHistBox', !!els['revHistBox']);
ck('shell 渲染出 revHistBtn', !!els['revHistBtn']);
ck('shell 渲染出 revHistSettled 复选框', !!els['revHistSettled']);
ck('初始为收起（display:none）', els['revHistBox'].style.display === 'none',
   els['revHistBox'].style.display);
ck('初始未发起任何请求', calls.length === 0, JSON.stringify(calls));
// 初始文案写在 HTML 里（尚未被 JS 覆盖），故查 HTML 而非 textContent
ck('按钮文案含默认窗口', /查看近 60 天列表/.test(panel._html),
   (panel._html.match(/<button id="revHistBtn"[^>]*>([^<]*)</) || [])[1]);

// ── 2) 展开：必须显形 + 请求 + 渲染出表格 ─────────────────────────────────
payload = FIXTURE;
api.toggleReversalHistory();
ck('展开后 loading=true', api._revHist.loading === true);
ck('展开后立即显形', els['revHistBox'].style.display === '', els['revHistBox'].style.display);
await tick();
ck('发起 1 次请求', calls.length === 1, JSON.stringify(calls));
ck('请求命中 /reversal_history 且带 days=60',
   calls[0] && calls[0].url.includes('/investor/reversal_history') && calls[0].url.includes('days=60'),
   calls[0] && calls[0].url);
ck('key 按窗口区分', calls[0] && calls[0].key === 'reversal-hist-60', calls[0] && calls[0].key);
ck('展开后 loading 复位', api._revHist.loading === false);
const hb = els['revHistBox']._html;
ck('渲染出「历史表现」标题', hb.includes('历史表现'));
ck('渲染出成绩单卡片', hb.includes('结算胜率') && hb.includes('跟踪样本'));
ck('渲染出明细表', hb.includes('<table>') && hb.includes('600003'));
ck('渲染出出场分布 chips', hb.includes('移动止盈离场') && hb.includes('止损离场'));
ck('止盈用 positive(红)', hb.includes('var(--positive)'));
ck('按钮切换为「收起列表」', els['revHistBtn'].textContent === '收起列表',
   els['revHistBtn'].textContent);

// ── 3) 面板整体重绘（手动刷新/首屏重载）后展开态不得与 DOM 脱节 ────────────
panel.innerHTML = api.renderReversalTracked(TRACKED);   // 重绘 => 容器回到 display:none
api._hydrateRevHist();
ck('重绘后展开态被灌回（display 显形）', els['revHistBox'].style.display === '',
   els['revHistBox'].style.display);
ck('重绘后用缓存渲染（不重复请求）', calls.length === 1, JSON.stringify(calls));
ck('重绘后内容仍完整', els['revHistBox']._html.includes('600003'));

// ── 4) 收起 ───────────────────────────────────────────────────────────────
api.toggleReversalHistory();
ck('收起后 display:none', els['revHistBox'].style.display === 'none');
ck('收起后按钮文案复位', els['revHistBtn'].textContent === '查看近 60 天列表',
   els['revHistBtn'].textContent);
api._hydrateRevHist();
ck('收起态下重绘不得显形', els['revHistBox'].style.display === 'none');

// ── 5) 只看已结算（纯前端过滤，不得发请求）───────────────────────────────
api.toggleReversalHistory();
await tick();
const before = calls.length;
els['revHistSettled'].checked = true;
api.onReversalHistSettledChange();
ck('勾选后标记 settledOnly', api._revHist.settledOnly === true);
ck('勾选后不发请求', calls.length === before, JSON.stringify(calls));
ck('勾选后只列已结算（已过滤提示）', els['revHistBox']._html.includes('已过滤'));
ck('过滤后不含未结算票', !els['revHistBox']._html.includes('600001'));
ck('过滤后仍含已结算票', els['revHistBox']._html.includes('600004'));

// ── 6) 换窗口：缓存失效 + 按新窗口重取 ───────────────────────────────────
els['revHistSettled'].checked = false;
api.onReversalHistSettledChange();
els['revHistDays'].value = '30';
api.onReversalHistDaysChange();
ck('窗口切到 30', api._revHist.days === 30);
ck('换窗清缓存', api._revHist.data === null);
await tick();
ck('换窗后带 days=30 重取', calls[calls.length - 1].url.includes('days=30'),
   calls[calls.length - 1].url);
ck('换窗后按钮文案跟随', els['revHistBtn'].textContent === '收起列表',
   els['revHistBtn'].textContent);
api.toggleReversalHistory();
els['revHistDays'].value = '180';
api.onReversalHistDaysChange();
ck('未展开时换窗不发请求', calls[calls.length - 1].url.includes('days=30'),
   calls[calls.length - 1].url);

// ── 7) 空数据 / 空过滤不得抛异常 ─────────────────────────────────────────
await showList({ days: 60, total: 0, items: [], summary: {}, exit_breakdown: [],
                 range: { start: null, end: null } });
ck('空列表渲染 empty-state', els['revHistBox']._html.includes('empty-state'));
await showList({ days: 60, total: 2, items: FIXTURE.items.slice(0, 2),
                 summary: FIXTURE.summary, exit_breakdown: FIXTURE.exit_breakdown,
                 range: FIXTURE.range });
els['revHistSettled'].checked = true;
api.onReversalHistSettledChange();
ck('全未结算 + 只看已结算 → 专用提示',
   els['revHistBox']._html.includes('还没有已结算'), els['revHistBox']._html.slice(0, 120));

console.log('='.repeat(96));
console.log(failed === 0
    ? '✅ 全部通过：展开/重绘/过滤/换窗 四态与 DOM 保持同步'
    : `❌ ${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
