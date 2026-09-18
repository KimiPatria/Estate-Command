/* Catch identifiers that are used but never defined.
 *
 * `vite build` resolves imports and will fail on a missing export, but an
 * identifier that resolves to nothing at all is, to a bundler, just a global
 * it assumes the browser provides. So `esc(x)` in a module that forgot to
 * import `esc` builds clean and throws the moment that line runs.
 *
 * This walks each module's AST, collects everything in scope - declarations,
 * imports, parameters, catch bindings, labels - and reports any remaining free
 * identifier that is not a known browser or standard global.
 *
 *     node test/undefined.mjs
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { parseAst } from 'rollup/parseAst';

const ROOT = 'src';

const GLOBALS = new Set([
  // standard
  'globalThis', 'undefined', 'NaN', 'Infinity', 'Object', 'Array', 'String',
  'Number', 'Boolean', 'Symbol', 'BigInt', 'Math', 'JSON', 'Date', 'RegExp',
  'Map', 'Set', 'WeakMap', 'WeakSet', 'Promise', 'Proxy', 'Reflect', 'Error',
  'TypeError', 'RangeError', 'SyntaxError', 'Intl', 'Function', 'parseInt',
  'parseFloat', 'isNaN', 'isFinite', 'encodeURIComponent', 'decodeURIComponent',
  'encodeURI', 'decodeURI', 'structuredClone', 'queueMicrotask',
  // browser
  'window', 'document', 'console', 'navigator', 'location', 'history', 'screen',
  'fetch', 'Headers', 'Request', 'Response', 'AbortController', 'URL',
  'URLSearchParams', 'FormData', 'Blob', 'File', 'FileReader', 'localStorage',
  'sessionStorage', 'indexedDB', 'performance', 'requestAnimationFrame',
  'cancelAnimationFrame', 'setTimeout', 'clearTimeout', 'setInterval',
  'clearInterval', 'getComputedStyle', 'matchMedia', 'alert', 'confirm',
  'prompt', 'Event', 'CustomEvent', 'EventTarget', 'Element', 'HTMLElement',
  'Node', 'NodeList', 'DOMParser', 'XMLHttpRequest', 'WebSocket', 'Worker',
  'IntersectionObserver', 'ResizeObserver', 'MutationObserver', 'Image',
  'CSS', 'devicePixelRatio', 'crypto', 'btoa', 'atob',
]);

function walkFiles(dir, out = []) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) walkFiles(p, out);
    else if (p.endsWith('.js')) out.push(p);
  }
  return out;
}

/* Every name a module binds at any scope. Deliberately flat: this is looking
   for names that exist nowhere in the file, not for shadowing mistakes. */
function collectBound(node, bound) {
  if (!node || typeof node !== 'object') return;
  const t = node.type;

  if (t === 'ImportDeclaration') {
    for (const s of node.specifiers) bound.add(s.local.name);
  } else if (t === 'VariableDeclarator') {
    bindPattern(node.id, bound);
  } else if (t === 'FunctionDeclaration' || t === 'FunctionExpression'
          || t === 'ArrowFunctionExpression') {
    if (node.id) bound.add(node.id.name);
    for (const p of node.params) bindPattern(p, bound);
  } else if (t === 'ClassDeclaration' || t === 'ClassExpression') {
    if (node.id) bound.add(node.id.name);
  } else if (t === 'CatchClause' && node.param) {
    bindPattern(node.param, bound);
  } else if (t === 'LabeledStatement') {
    bound.add(node.label.name);
  }

  for (const k of Object.keys(node)) {
    const v = node[k];
    if (Array.isArray(v)) v.forEach(c => collectBound(c, bound));
    else if (v && typeof v.type === 'string') collectBound(v, bound);
  }
}

function bindPattern(p, bound) {
  if (!p) return;
  switch (p.type) {
    case 'Identifier': bound.add(p.name); break;
    case 'ObjectPattern': p.properties.forEach(x =>
      bindPattern(x.type === 'RestElement' ? x.argument : x.value, bound)); break;
    case 'ArrayPattern': p.elements.forEach(x => bindPattern(x, bound)); break;
    case 'AssignmentPattern': bindPattern(p.left, bound); break;
    case 'RestElement': bindPattern(p.argument, bound); break;
  }
}

/* Identifiers in a value position: not property keys, not member properties,
   not the names in an import or a label. */
function collectUsed(node, used, parent = null, key = null) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'Identifier') {
    const isMemberProp = parent && parent.type === 'MemberExpression'
      && key === 'property' && !parent.computed;
    const isPropKey = parent && (parent.type === 'Property' || parent.type === 'PropertyDefinition')
      && key === 'key' && !parent.computed;
    const isMethodKey = parent && parent.type === 'MethodDefinition' && key === 'key';
    const isImport = parent && /^Import(Specifier|DefaultSpecifier|NamespaceSpecifier)$/.test(parent.type);
    const isExportSpec = parent && parent.type === 'ExportSpecifier';
    const isLabel = parent && /^(LabeledStatement|BreakStatement|ContinueStatement)$/.test(parent.type);
    if (!isMemberProp && !isPropKey && !isMethodKey && !isImport && !isExportSpec && !isLabel) {
      used.add(node.name);
    }
    return;
  }
  for (const k of Object.keys(node)) {
    const v = node[k];
    if (Array.isArray(v)) v.forEach(c => collectUsed(c, used, node, k));
    else if (v && typeof v.type === 'string') collectUsed(v, used, node, k);
  }
}

let bad = 0;
for (const file of walkFiles(ROOT)) {
  const src = readFileSync(file, 'utf8');
  let ast;
  try {
    ast = parseAst(src, { allowReturnOutsideFunction: false });
  } catch (e) {
    console.log(`  PARSE ${relative('.', file)}: ${e.message}`);
    bad++;
    continue;
  }
  const bound = new Set();
  const used = new Set();
  collectBound(ast, bound);
  collectUsed(ast, used);
  const free = [...used].filter(n => !bound.has(n) && !GLOBALS.has(n)).sort();
  if (free.length) {
    console.log(`  ${relative('.', file)}: ${free.join(', ')}`);
    bad += free.length;
  }
}

console.log('');
if (bad) {
  console.log(`FAIL  ${bad} undefined identifier(s). Each one is a runtime`);
  console.log('      ReferenceError the build cannot see.');
  process.exit(1);
}
console.log('OK    no undefined identifiers');
