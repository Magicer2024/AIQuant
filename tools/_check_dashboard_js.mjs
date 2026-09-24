// dashboard.html 内联 JS 语法检查（单文件前端、无构建 ⇒ 改完必须过这一步）
//
// 为什么不用 `node --check`：本沙箱下 node 进程内 spawn node 会 EBUSY
// （spawnSync ... EBUSY），故改为在**同一进程内**用 vm.Script 解析。
// vm.Script 只做语法解析、不执行，因此对 `const REVERSAL_LIMIT = 12;` 之类的
// 顶层声明、DOM 访问、未定义变量都不会报错——正是我们要的语义。
//
// 用法：node tools/_check_dashboard_js.mjs
// 退出码：0 = 全部通过；1 = 存在语法错误
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const FILE = process.argv[2] || 'dashboard.html';
// ⚠ 不要用 new URL(...).pathname：中文路径会被 URL 编码成 %E5%B0%8F…
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const target = path.isAbsolute(FILE) ? FILE : path.join(root, FILE);

if (!fs.existsSync(target)) {
  console.error(`❌ 找不到文件: ${target}`);
  process.exit(1);
}
const html = fs.readFileSync(target, 'utf8');

const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
let m, i = 0, failed = 0, total = 0;
while ((m = re.exec(html)) !== null) {
  const attrs = m[1] || '';
  const body = m[2] || '';
  if (/\bsrc\s*=/.test(attrs)) continue;   // 外链脚本跳过
  if (!body.trim()) continue;
  i++;
  total += body.length;
  try {
    new vm.Script(body, { filename: `${path.basename(target)}#block${i}` });
    console.log(`  ✅ block${i} (${body.length} chars)`);
  } catch (e) {
    failed++;
    console.log(`  ❌ block${i} 语法错误: ${e.name}: ${e.message}`);
  }
}

console.log(failed
  ? `\n❌ ${failed}/${i} 个内联 script 块语法错误`
  : `\n✅ ${path.basename(target)}：全部 ${i} 个内联 script 块语法正确（共 ${total} 字符）`);
process.exit(failed ? 1 : 0);
