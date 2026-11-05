// Run with: node tests/frontend/markdown.test.mjs
// Uses a very small fake DOM so no browser is needed. Setting innerHTML on it throws,
// so the test fails loudly if the renderer ever tries to use it.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { createMarkdownRenderer } = createRequire(import.meta.url)('../../frontend/markdown.js');

class Node_ {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; this.attrs = {}; this.className = ''; this.listeners = {}; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener() {}
  set textContent(v) { this.children = [new TextNode(String(v))]; }
  get textContent() { return this.children.map((c) => c.textContent).join(''); }
  set innerHTML(_) { throw new Error('innerHTML must never be used'); }
  find(tag) { const out = []; const walk = (n) => { if (n.tag === tag) out.push(n); (n.children || []).forEach(walk); }; walk(this); return out; }
}
class TextNode { constructor(t) { this.text = t; this.tag = '#text'; } get textContent() { return this.text; } }
const doc = { createElement: (t) => new Node_(t), createTextNode: (t) => new TextNode(t) };
const render = createMarkdownRenderer(doc);

// 1. Raw HTML from a model is rendered as inert text, never as elements.
let out = render('<script>alert(1)</script> <img src=x onerror=alert(1)>');
assert.equal(out.find('script').length, 0);
assert.equal(out.find('img').length, 0);
assert.ok(out.textContent.includes('<script>alert(1)</script>'));

// 2. javascript: links are not turned into anchors; https links are.
out = render('[bad](javascript:alert(1)) and [good](https://example.com)');
const links = out.find('a');
assert.equal(links.length, 1);
assert.equal(links[0].href, 'https://example.com');
assert.equal(links[0].rel, 'noopener noreferrer nofollow');

// 3. Markdown structures.
out = render('# Title\n\n- a\n- b\n  - nested\n\n1. one\n2. two\n\n**bold** and `code`');
assert.equal(out.find('h1').length, 1);
assert.equal(out.find('ul').length, 2);
assert.equal(out.find('li').length, 5);
assert.equal(out.find('strong').length, 1);
assert.equal(out.find('code').length, 1);

// 4. Code fences keep content literally (including HTML-looking text).
out = render('```python\nprint("<b>hi</b>")\n```');
assert.equal(out.find('pre').length, 1);
assert.equal(out.find('b').length, 0);
assert.ok(out.find('code')[0].textContent.includes('<b>hi</b>'));

// 5. Tables.
out = render('| A | B |\n|---|---|\n| 1 | 2 |');
assert.equal(out.find('table').length, 1);
assert.equal(out.find('td').length, 2);

// 5b. A half-written answer (unclosed code fence, partial list) must render without throwing while streaming.
render('Text\n\n```js\nconst a = 1;');
render('- one\n- tw');

// 6. Empty or null input doesn't throw.
assert.equal(render('').children.length, 0);
assert.equal(render(null).children.length, 0);
console.log('markdown renderer: all tests passed');
