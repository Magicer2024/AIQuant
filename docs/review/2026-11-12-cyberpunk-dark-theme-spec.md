# 赛博朋克深色改版 · 视觉规格 v1.0（2026-11-12）

- 作者：UIA｜审阅：管理A｜实现：前端A（任务分支）｜验收：测试A｜部署：DO
- 来源派单：seq=318（Yu Fang「页面赛博朋克一些、炫酷一些」）+ seq=320（管理A 基线事实与硬约束）
- 交付物：本文档 + `docs/review/2026-11-12-cyberpunk-before-after-mock.html`（静态对照 mock，浏览器直开）
- **本规格只是设计契约，未改任何生产文件**；实现以前端A 任务分支为准。

## 0. 硬约束回执（违反即返工项，逐条对应）

| # | 约束 | 本规格的处理 |
|---|---|---|
| H1 | 只改视觉层：不改 DOM 结构、不改文案语义与数据口径 | 全部改动收敛在 `:root` 变量组 + CSS 规则内的颜色/阴影字面量替换；装饰层清单（§5）逐项标注「纯 CSS」，**0 项需要动 HTML**。JS 仅 §6 图表重初始化涉及 `resolveKlinePalette()` 复用现有函数，无新 DOM 操作 |
| H2 | 深色为唯一主题，不复活切换器 | 直接改写 `:root` 本体，不建 `[data-theme]` 覆盖块、不建切换器链路（与现行「浅色唯一主题」结构对称，等于换皮不换骨） |
| H3 | R1 红涨绿跌不翻 | `--positive`=红 `#ff5555`（涨）、`--negative`=绿 `#2ee59d`（跌）。**色相未翻，只提亮饱和度适配深底** |
| H4 | 文字级最差背景 ≥4.5:1，逐色实测 | 全部由脚本按 WCAG 2.1 相对亮度公式实算（§2/§3/§4 表内即实测值）；「最差背景」=六档底色中最亮的 `--bg-panel-hover #16213a`，与徽标各自 tint 底 |
| H5 | 三条合规披露保留且深底达标 | 专项验收表 §7，实测 4.61~16.17:1 全过 |
| H6 | 刻度 token（fs/sp/r）不动 | `--fs-1..6 / --sp-1..6 / --r-* / --radius*` 原值保留，未列入改动表 |
| H7 | 字体只用系统 monospace 栈，不引外部 CDN | §8：`ui-monospace` 优先的系统栈，零网络请求 |
| H8 | 不推 main | 流程本身走任务分支 → 测试A 回归 → DO 部署，本规格不含 git 动作 |

## 1. 设计基调

- **一句话**：黑蓝底 + 青/品红双色霓虹，霓虹是氛围层（描边、辉光、渐变标题、网格），信息层文字保持高对比冷白。
- 分层逻辑：浅色版「白卡靠阴影分层」→ 深色版「亮卡靠**底色差 + 描边**分层」，页底最暗、hover 最亮，共六档。
- 双主色用法：青 `--accent`（交互/链接/选中/数据高亮，延续现 accent 的全部职责），品红 `--accent-2`（品牌渐变第二色、图表线 2、点缀），**品红不做正文色**（实测 4.91 过线，但霓虹彩字大面积使用伤可读性，限制在标题渐变/徽标/图形）。

## 2. 新 `:root` 全量变量表（旧值 → 新值）

对比度实测格式：`panel`=vs `#0b1120`（面板底，绝大多数文字实际所在底）、`hover`=vs `#16213a`（最亮底=最差底）。文字级判 4.5，图形级判 3.0。

### 2.1 背景分层（6 + 2 组装位）

```css
--bg-root:        #f2f5f9 → #05070d;   /* 页底：近黑蓝 */
--bg-panel:       #ffffff → #0b1120;   /* 面板 */
--bg-elevated:    #ffffff → #0e1526;   /* 浮层/渐变尾色 */
--bg-cell:        #f5f7fb → #121b30;   /* 卡内嵌套小格 */
--bg-panel-hover: #eef2f8 → #16213a;   /* hover 底（全页最亮底=文字最差背景） */
--bg-input:       #ffffff → #0a1222;   /* 输入槽 */
--bg-deep-rgb:    255,255,255 → 11,17,32;      /* header/nav material 组装位 */
--overlay-rgb:    16,24,40 → 2,4,10;           /* 深字遮罩基底 */
```

### 2.2 描边（4）

```css
--border:      #d9e0ea → #24304e;   --border-rgb: 217,224,234 → 36,48,78;
--border-glow: #b9c6da → #2f4b7c;   /* panel:hover 描边，向青过渡一档 */
--hairline:    rgba(16,24,40,.07) → rgba(125,170,230,.12);  /* 深底用亮线才可见 */
```

### 2.3 文字三档（3）——逐色实测

| 变量 | 旧值 | 新值 | panel | hover(最差) | cell | 判定 |
|---|---|---|---|---|---|---|
| `--text-primary` | #1c2333 | **#e6eeff** | 16.17 | **13.74** | 14.72 | ✓ |
| `--text-secondary` | #4a5468 | **#aebcdc** | 9.89 | **8.40** | 9.00 | ✓ |
| `--text-muted` | #5a677f | **#8b99bb** | 6.61 | **5.62** | 6.02 | ✓（现行为 5.08，新版余量更大） |

### 2.4 accent 双主色（6）

| 变量 | 旧值 | 新值 | panel | hover(最差) | 用途边界 |
|---|---|---|---|---|---|
| `--accent`（青） | #0060df | **#00d4f0** (`--accent-rgb: 0,212,240`) | 10.47 | **8.89** | 链接/选中/active/焦点，可作文字 |
| `--accent-bright` | #007aff | **#7defff** (`125,239,255`) | 14.04 | 11.93 | 仍限图形/焦点环/大色块（职责不变） |
| `--accent-dim` | #004bb4 | **#00a6c4** | — | — | 按下态/深一档图形 |
| `--accent-2`（品红） | #5b5bd6 | **#ff2ec4** (`255,46,196`) | 5.77 | **4.91** | 品牌渐变/图表线2/点缀；**不作正文色**（能过线但刻意收紧） |
| `--accent-3` | #8e32d8 | **#b96bff** (`185,107,255`) | 5.93 | **5.04** | 氛围渐变第三色/涨停图形 |

### 2.5 涨跌色（R1 不翻，2+2 组装位）

| 变量 | 旧值 | 新值 | panel | hover(最差) | 说明 |
|---|---|---|---|---|---|
| `--positive` | #d70015 红 | **#ff5555** (`--positive-rgb: 255,85,85`) | 5.99 | **5.09** | **涨=红**，色相不变仅提亮；旧值在深底仅 1.5:1，不可用 |
| `--negative` | #0a7a2f 绿 | **#2ee59d** (`--negative-rgb: 46,229,157`) | 11.49 | **9.76** | **跌=绿**，霓虹绿 |

### 2.6 状态色（8）

| 变量 | 旧值 | 新值 | panel | hover(最差) |
|---|---|---|---|---|
| `--status-good` / `--success` / `--green` | #067647 | **#2ee59d** (`6,118,71→46,229,157`) | 11.49 | 9.76 |
| `--status-warn` / `--warning` / `--gold` | #996100 | **#ffb454** (`153,97,0→255,180,84`) | 10.68 | 9.07 |
| `--status-bad` / `--danger` | #cc1f1f | **#ff6b6b**（bad）/ **#ff5555**（danger，`204,31,31→255,85,85`） | 6.79 / 5.99 | 5.76 / 5.09 |

注：`--danger` 与 `--positive` 同值红是刻意合并语义（危险=下跌反色在 A 股语境易混淆，danger 保留给错误态按钮/删除，红但配深底白字场景见 toast）。

### 2.7 徽标 tint 重算（双层制反转：亮字 + 鲜色 16% 合成深底）

浅色版是「14% 鲜色合成**白**底 + 深字」；深色版反转规则为「**16% 鲜色合成 `--bg-panel`** + 同鲜色字」，solid 直取值：

| 变量 | 旧值 | 新值（合成式） | 徽标字色 | panel底实测 | 最差(hover底上同比例合成) |
|---|---|---|---|---|---|
| `--tint-good` | #e3f7e8 | **#113334** | var(--status-good) #2ee59d | **8.28** | 6.83 |
| `--tint-warn` | #fff2dd | **#322b28** | var(--status-warn) #ffb454 | **7.88** | 6.47 |
| `--tint-bad` | #ffe4e2 | **#321f2c** | **#ff6b6b**（替换硬码 #b3261e） | **5.53** | 4.61 |
| `--tint-accent` | #dbecff | **#093041** | var(--accent) #00d4f0 | **7.73** | 6.30 |

（hover 列含义：徽标出现在 `bg-panel-hover` 面板上时 tint 会被环境光顶亮，按 hover 底同式重合成后仍全过 4.5。）

### 2.8 regime 档与涨跌停（4）

| 变量 | 旧值 | 新值 | panel | hover(最差) |
|---|---|---|---|---|
| `--cool` | #0060df | **#4db8ff** | 8.62 | 7.32 |
| `--cold` | #5a677f | **#8b99bb** | 6.61 | 5.62 |
| `--limit-up`（涨停·紫） | #8e32d8 | **#b96bff** | 5.93 | 5.04 |
| `--limit-down`（跌停·蓝） | #0077b6 | **#4dc3ff** | 9.47 | 8.05 |

regime 语义色序（多头→空头）在新底为：`#2ee59d(good) / #4db8ff(cool) / #8b99bb(cold) / #ff5555(bad)`，红绿仍各归语义，不与涨跌色冲突（R2/R3 徽标职责不变）。

### 2.9 阴影/渐变 → 辉光（5）

```css
--shadow-panel: 0 1px 2px rgba(0,0,0,.5), 0 8px 24px rgba(0,0,0,.35);
--shadow-float: 0 2px 8px rgba(0,0,0,.6), 0 20px 56px rgba(0,0,0,.55);
--shadow-hover: 0 0 0 1px rgba(0,212,240,.28), 0 0 20px rgba(0,212,240,.12);  /* hover=青色辉光描边 */
--grad-accent:  linear-gradient(120deg, #00d4f0 0%, #ff2ec4 100%);            /* 青→品红，标题渐变/nav下划线自动继承 */
```

### 2.10 图表色（11，canvas 侧，见 §6 时序）

| 变量 | 旧值 | 新值 | 判据 |
|---|---|---|---|
| `--chart-axis` | #5a677f | **#8b99bb** | 刻度文字实算 6.61 vs panel ✓4.5 |
| `--chart-grid` | #e5e9f0 | **#1c2740** | 非文字网格线，低显著性刻意压暗(1.27) |
| `--chart-split` | #f2f5f9 | **#141d33** | 同上 |
| `--chart-panel` | #ffffff | **#0b1120** | tooltip 底=panel 同色，其上文字按 §2.3 判 |
| `--chart-title` | #1c2333 | **#e6eeff** | 16.17 ✓ |
| `--chart-muted` | #5a677f | **#8b99bb** | 6.61 ✓ |
| `--chart-up` | #ff3b30 | **#ff5555** | 图形 5.99 ≥3 ✓ **红涨** |
| `--chart-down` | #34c759 | **#2ee59d** | 图形 11.49 ≥3 ✓ **绿跌** |
| `--chart-line-1` | #007aff | **#00d4f0** | 图形 10.47 ✓ |
| `--chart-line-2` | #ff9f0a | **#ff2ec4** | 图形 5.77 ✓ |
| `--chart-line-3` | #af52de | **#b96bff** | 图形 5.93 ✓ |

### 2.11 明确不动项

`--fs-1..6`、`--sp-1..6`、`--r-sm/md/lg`、`--radius/-md/-lg`、`--font-sans`、别名链（`--surface/--bg/--text/--bg-card/--card/--bg-hover`，均转引已改变量自动生效）。

## 3. CSS 规则内硬编码色替换表（仍属视觉层，全部在 dashboard.css / dashboard.html 内联 style 的 CSS 部分）

这些是变量体系外的散色，不改它们会出现「深底上白字白底」级破图。**均为颜色/阴影字面量替换，不触碰选择器与结构**：

| 位置（现行号，实现时以内容定位为准） | 现值 | 改为 | 实测 |
|---|---|---|---|
| badge-running/badge-warn `.hz-mid` 字色 `#8a5a00`（3 处） | #8a5a00 | `var(--status-warn)` | 7.88(tint-warn 底) |
| badge-error/badge-bad 字色 `#b3261e`（2 处） | #b3261e | `var(--status-bad)` | 5.53(tint-bad 底) |
| toast 底色组（success 底、`#f59e0b`、`#3b82f6`） | 浅底体系 | success=`#2ee59d`、warning=`#ffb454`、error=`#ff5555`、info=`#00d4f0`，**字色统一 `#05070d`**（深字） | 12.29 / 11.42 / **6.41 / 11.20**（管理A 复核更正，原稿 8.71/13.06 有误；仍全过 4.5） |
| toast 原白字 `color:#fff` | #fff | 随上行改深字 | — |
| skeleton 渐变/底色 `#eef1f6`、`#e3e8f0`（3 处） | 浅灰 | `#16213a` ↔ `#1d2a47` | 非文字，装饰 |
| `.segmented .seg-btn.active` 字色 `#fff` | 白字青底仅 1.4 | **`#04222c`**（深字） | 9.19 ✓ |
| 突破等待边线 `border-left:3px solid #ff9500` | #ff9500 | `var(--status-warn)` | 图形 ✓ |
| signal-badge `#ffab40`/`#7baaff`/`#5b88c2` + 同值 rgba（约 6 处） | 散彩 | 统一 `var(--status-warn)`/`var(--cool)`/`var(--cold)` 及对应 rgb 组装位 | 字≥7.3 全过 |
| `.price-sub.confirm-cta` 亮黄渐变更名保留 | #ffd24a→#ff9500 底 + `#1a1408` 字 | **保留不动**（天然「深字霓虹底」，已是赛博形态） | 12.70 / 8.32 ✓ |
| rp-* 里 `#b26a00`、`rgba(255,170,60,…)` | 浅底深字 | `var(--status-warn)`、`rgba(var(--warning-rgb),…)` | 7.88(tint 底) |
| dashboard.html 内联 `_sdSignalColor` JS 字面量 `'#ffaa3c'` | 深底图形色已可用（CR≈8.9） | **可不改**；若统一口径改 `'var(--status-warn)'`（`_sdColor` 已支持 var 解析） | — |

## 4. 三条合规披露专项验收表（H5）

| 披露 | 前景/背景（改版后） | 实测对比度 | 判定 |
|---|---|---|---|
| 首屏「扣成本后净期望为负…」小字（`--text-muted` on hero 面板 `--bg-panel`） | #8b99bb / #0b1120 | **6.61**（若处 hover 态面板：5.62） | ≥4.5 ✓ |
| 复盘区毛口径披露块（同 `--text-muted` on panel） | 同上 | **6.61** | ✓ |
| 徽标「观察·偏多」（`--status-good` on `--tint-good`） | #2ee59d / #113334 | **8.28**（hover 底面板上 6.83） | ✓ |
| 深析徽标 `span.sd-signal-level`（继承 `--text-primary` on panel） | #e6eeff / #0b1120 | **16.17** | ✓ |
| 「参考买入价（观察）」列头/格标（`--text-muted` on panel） | #8b99bb / #0b1120 | **6.61** | ✓ |
| 其下价格数值（`.pm-buy` 用 `--positive`） | #ff5555 / #0b1120 | **5.99**（hover 5.09） | ✓ |

**文案零改动**：以上元素文本、title 属性、tooltip 语义全部原样，只换 CSS 层。

## 5. 装饰层清单（全部纯 CSS，零 HTML 改动）

| # | 装饰 | 实现 | 是否动 HTML |
|---|---|---|---|
| D1 | 蓝图网格背景 | `body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,rgba(0,212,240,.05) 0 1px,transparent 1px 40px),repeating-linear-gradient(90deg,rgba(0,212,240,.05) 0 1px,transparent 1px 40px);pointer-events:none;z-index:0}` | 否（header/content 已是 z-index:1，直接盖得住） |
| D2 | 扫描线 | `body::after{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,rgba(125,239,255,.028) 0 1px,transparent 1px 3px);pointer-events:none;z-index:9999;mix-blend-mode:screen}` | 否 |
| D3 | 面板 hover 辉光 | 复用现有 `.panel:hover`（值换 §2.9 `--shadow-hover` 青辉光）+ `.panel::before` 顶线由「hover 才亮」改为常驻弱亮（opacity .35）、色走 `--grad-accent` 青→品红 | 否 |
| D4 | 品牌霓虹标题 | 现 `.header h1 span` 已用 `--grad-accent` background-clip，变量一改自动变青→品红；追加 `text-shadow:0 0 18px rgba(0,212,240,.25)` | 否 |
| D5 | 等宽数字 | `table, .price-val, .pm-price, .pm-score, .num {font-variant-numeric:tabular-nums}` + 价格类 `font-family:var(--font-mono)` | 否 |
| D6 | 状态点脉冲 | `.status-dot` 加 `box-shadow:0 0 8px var(--status-good)`；blink 动画已有 | 否 |
| D7 | 焦点环（顺带补 a11y） | `:focus-visible{outline:2px solid var(--accent-bright);outline-offset:2px}` | 否 |
| D8 | 滚动条 | `::-webkit-scrollbar{width:10px};thumb{background:#24304e;border-radius:5px}` | 否 |

**明确否决的装饰（需要动 DOM，与 H1 冲突）**：面板四角切角括号、hero 全息 diff 动画条、逐卡片扫光 keyframes。如 Yu Fang 后续坚持要切角，另立任务评估。

## 6. 图表色重初始化方案（cssVar 一次性读取问题的正面回答）

**事实**：图表色经 `cssVar()` → `getComputedStyle(document.documentElement)` 现取现用；`_klinePalette` 惰性解析（首次 `klineColor()` 时缓存，见 dashboard.html:5142-5152）；其余 chart option 在各自 build 函数内逐次读取。

**本次改版（深色=唯一主题）**：CSS 与页面同一次部署落地，浏览器加载时 `:root` 已是深色，**所有 cssVar 读取天然拿到新值，无需任何重初始化代码**。唯一时序前提：`<link>` dashboard.css 在 `<head>`、脚本在其后（现状已满足），实现时不得把 CSS 改成异步加载。

**备案（不实现，仅写入规格防将来踩坑）**：若未来重新引入运行时主题切换，必须提供

```js
function refreshChartTheme() {
    _klinePalette = null;                    // 清惰性缓存，下次 klineColor() 重新 cssVar
    Object.values(_charts || {}).forEach(c => {  // 已建实例全部重建 option
        if (c && c.setOption) c.setOption(buildOptionFor(c), true);  // notMerge=true
    });
}
```

即「清缓存 + 逐实例 setOption(notMerge)」两步，缺一就有旧色残留。测试A 回归时若发现任何 canvas 区仍是浅色系，按此判定为「build 函数外有第二处模块级缓存」，指回本条处理。

## 7. 字体栈（H7）

```css
--font-mono: ui-monospace, "Cascadia Mono", "Consolas", "Menlo", "Liberation Mono", monospace;
```

系统栈，零外部请求；`--font-sans` 不动。等宽主要用于表头/数字/状态点文案，中文仍走 sans。

## 8. before/after 对照

- 静态 mock：`docs/review/2026-11-12-cyberpunk-before-after-mock.html`（自包含单文件，左列现行浅色、右列新版深色，含 hero 披露行、徽标、价格格、表格、D1-D8 装饰效果；任意浏览器直开，无网络依赖）。
- 验收截图基线以该 mock 为准；实现分支落地后由测试A 按 §2~§4 全表抽查 WCAG（建议脚本化，允许 ±0.05 舍入）。

## 9. 给管理A的主动申报（规格内的取舍）

1. **品红刻意降级使用**：实测过线（4.91）但我把它限制在渐变/图表/点缀，不作正文与链接色。理由：青已承接全部 accent 职责，双色等权会让「链接是什么颜色」失去唯一答案。
2. **网格线压到 1.27:1**：非文字装饰线不受 3:1 约束（WCAG 对「inactive/装饰」豁免，图表数据线与轴文字均单列达标）。若你认为图表网格应可见性更强，可提到 `#243252`（1.6），我倾向保持克制，霓虹底噪已经由 D1 页底网格承担。
3. **danger 与 positive 同红**：见 §2.6 注。若你倾向区分，可 danger→`#ff3d3d`（panel 5.6），实现成本为零。
4. **深字霓虹底的三处保留**（seg active、toast、confirm-cta）：赛博朋克在深色 UI 里的正确姿势是「亮底小块配深字」而非「深底白字硬塞」，这三处实测 8.3~13.1 全为高过线。

（完——本文件未 commit，落盘即交付；git 动作按流程归前端A/管理A。）

## 10. 管理A 审阅裁定（2026-11-12，独立复核后追加）

**复核方法**：不引自报数。用 WCAG 2.1 相对亮度公式对 §2~§4 全部 27 组前景/背景独立重算；tint 四枚按「16% 鲜色 + 84% `--bg-panel`」独立重合成；散色清单、`_klinePalette` 惰性缓存位置（dashboard.html:5142-5152）、`<link>` 在 head（:11）逐条 grep 核实。

**复核结论**：27 组中 **25 组与规格逐位一致**；tint 四枚合成值 4/4 一致；散色清单全部属实。**2 组更正**：toast 深字 on error 实算 **6.41**（原稿 8.71）、on info 实算 **11.20**（原稿 13.06）——两者仍 ≥4.5，不构成合规失败，但数字已在 §3 行内更正。**规格通过，放行实现。**

**§9 四项取舍裁定**：

1. 品红限装饰不作正文 —— **批准**。理由成立：accent 职责唯一性比双色等权重要。
2. 图表网格 1.27:1 —— **批准保持克制**。装饰线豁免成立，轴文字 6.61 与数据线均单列达标；页底 D1 网格已承担氛围。测试A 回归时若目视图表网格完全不可辨，再提 `#243252`，不预先改。
3. danger 与 positive 同红 —— **不批准同值，改区分**：`--danger`/`--danger-rgb` = **`#ff3d3d` / `255,61,61`**。实测 panel **5.37** / hover **4.56**（过线但余量薄，测试A 重点抽查）；toast error 底随 `--danger`，深字 `#05070d` on `#ff3d3d` = **5.74**。`--status-bad` 与 `--tint-bad` 维持 `#ff6b6b`/`#321f2c` 不变（徽标语义与错误态语义分离）。理由：A股语境红=涨，错误态再同红会让「删除/报错」与「上涨」共用一个信号色。
4. seg active / toast / confirm-cta 亮底深字 —— **批准**。实测 9.19 / 6.41~12.29 / 12.70 全过。

**实现追加要求（前端A 必做，测试A 必查）**：

- **A1**：`#8a5a00` 实际 **6 处**（dashboard.css 3 + dashboard.html 内联 3），规格原表写 3 处；按内容定位全替换，不得漏 html 侧。其余散色同理以 grep 全量为准。
- **A2**：`dashboard.html:11` 的 `dashboard.css?v=20261017j` **必须 bump 缓存版本号**，否则浏览器拿旧 CSS，深色不生效且会被误判为实现失败。
- **A3**：实现完成后 grep 归零检查：§3 清单所列浅色字面量（`#8a5a00 #b3261e #eef1f6 #e3e8f0 #b26a00 #ffab40 #7baaff #5b88c2` 及 toast/seg 白字）在 css 与 html 中 **0 残留**；唯一豁免 = confirm-cta 的 `#ffd24a/#ff9500/#1a1408`（规格明确保留）与 `_sdSignalColor` 的 `'#ffaa3c'`（规格允许保留）。
- **A4**：不得把 CSS 改异步加载（§6 时序前提）；不得新增 `[data-theme]` 或切换器；刻度 token 一列不动。
- **A5**：commit 范围 = 本规格 + mock + `static/css/dashboard.css` + `dashboard.html` 的视觉层改动；**不改任何文案、DOM 结构、数据口径**；任务分支 `task/cyberpunk-theme-20261112`，不推 main。
