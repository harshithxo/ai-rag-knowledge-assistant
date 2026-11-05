/* A small, safe Markdown renderer.
 *
 * Model output can't be trusted, so this never uses innerHTML. It parses the text and builds
 * DOM nodes with createElement and createTextNode. A "<script>" or "<img onerror=...>" in an
 * answer is therefore shown as plain text. Links only work for http(s) URLs.
 */
(function (scope) {
  function createMarkdownRenderer(doc) {
    const el = (tag, cls) => { const n = doc.createElement(tag); if (cls) n.className = cls; return n; };
    const text = (s) => doc.createTextNode(s);

    // groups: 1 `code`, 2 **bold**, 3 [label](url) with 4 = label and 5 = url, 6 *italic*
    const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*)|(\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\))|(\*[^*\s][^*\n]*?\*)/g;

    function inline(parent, src) {
      let last = 0, m;
      // Make a new regex on every call. inline() calls itself for bold text, and a shared
      // global regex would have its lastIndex reset by the inner call and loop forever.
      const re = new RegExp(INLINE.source, 'g');
      while ((m = re.exec(src)) !== null) {
        if (m.index > last) parent.appendChild(text(src.slice(last, m.index)));
        if (m[1]) { const c = el('code'); c.appendChild(text(m[1].slice(1, -1))); parent.appendChild(c); }
        else if (m[2]) { const b = el('strong'); inline(b, m[2].slice(2, -2)); parent.appendChild(b); }
        else if (m[3]) {
          const a = el('a'); a.textContent = m[4]; a.href = m[5]; a.target = '_blank'; a.rel = 'noopener noreferrer nofollow';
          parent.appendChild(a);
        } else if (m[6]) { const i = el('em'); i.textContent = m[6].slice(1, -1); parent.appendChild(i); }
        last = m.index + m[0].length;
      }
      if (last < src.length) parent.appendChild(text(src.slice(last)));
    }

    const RE = {
      fence: /^\s*```\s*([\w+#.-]*)\s*$/,
      heading: /^(#{1,4})\s+(.*)$/,
      ul: /^(\s*)[-*+]\s+(.*)$/,
      ol: /^(\s*)\d+[.)]\s+(.*)$/,
      quote: /^\s*>\s?(.*)$/,
      hr: /^\s*([-*_])(\s*\1){2,}\s*$/,
      tableSep: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
    };
    const splitRow = (line) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
    const startsBlock = (line, next) =>
      RE.fence.test(line) || RE.heading.test(line) || RE.ul.test(line) || RE.ol.test(line) ||
      RE.quote.test(line) || RE.hr.test(line) || (line.includes('|') && next !== undefined && RE.tableSep.test(next));

    function list(lines, i) {
      const first = lines[i].match(RE.ul) || lines[i].match(RE.ol);
      const ordered = RE.ol.test(lines[i]) && !RE.ul.test(lines[i]);
      const base = first[1].length;
      const listEl = el(ordered ? 'ol' : 'ul');
      let li = null;
      while (i < lines.length) {
        const mu = lines[i].match(RE.ul), mo = lines[i].match(RE.ol), m = mu || mo;
        if (!m) break;
        const indent = m[1].length;
        if (indent < base) break;
        if (indent > base && li) {
          const nested = list(lines, i); li.appendChild(nested.node); i = nested.next; continue;
        }
        li = el('li'); inline(li, m[2]); listEl.appendChild(li); i++;
      }
      return { node: listEl, next: i };
    }

    function render(src) {
      const root = el('div', 'md');
      const lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
      let i = 0;
      while (i < lines.length) {
        const line = lines[i];
        if (!line.trim()) { i++; continue; }

        const fence = line.match(RE.fence);
        if (fence) {
          const body = []; i++;
          while (i < lines.length && !RE.fence.test(lines[i])) body.push(lines[i++]);
          i++;
          const wrap = el('div', 'codeblock'), head = el('div', 'codeblock-head'), pre = el('pre'), code = el('code');
          head.appendChild(text(fence[1] || 'code'));
          const copy = el('button', 'btn btn-ghost btn-sm'); copy.type = 'button'; copy.textContent = 'Copy';
          const raw = body.join('\n');
          copy.addEventListener('click', () => {
            const done = () => { copy.textContent = 'Copied'; setTimeout(() => { copy.textContent = 'Copy'; }, 1200); };
            if (scope.navigator && scope.navigator.clipboard) scope.navigator.clipboard.writeText(raw).then(done);
          });
          head.appendChild(copy);
          code.textContent = raw; pre.appendChild(code); wrap.appendChild(head); wrap.appendChild(pre); root.appendChild(wrap);
          continue;
        }
        const h = line.match(RE.heading);
        if (h) { const n = el('h' + h[1].length); inline(n, h[2]); root.appendChild(n); i++; continue; }
        if (RE.hr.test(line)) { root.appendChild(el('hr')); i++; continue; }
        if (line.includes('|') && i + 1 < lines.length && RE.tableSep.test(lines[i + 1])) {
          const table = el('table'), thead = el('thead'), tbody = el('tbody'), htr = el('tr');
          splitRow(line).forEach((c) => { const th = el('th'); inline(th, c); htr.appendChild(th); });
          thead.appendChild(htr); i += 2;
          while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
            const tr = el('tr');
            splitRow(lines[i]).forEach((c) => { const td = el('td'); inline(td, c); tr.appendChild(td); });
            tbody.appendChild(tr); i++;
          }
          table.appendChild(thead); table.appendChild(tbody); root.appendChild(table); continue;
        }
        if (RE.ul.test(line) || RE.ol.test(line)) { const r = list(lines, i); root.appendChild(r.node); i = r.next; continue; }
        if (RE.quote.test(line)) {
          const q = []; while (i < lines.length && RE.quote.test(lines[i])) q.push(lines[i++].match(RE.quote)[1]);
          const bq = el('blockquote'); const p = el('p'); inline(p, q.join(' ')); bq.appendChild(p); root.appendChild(bq); continue;
        }
        const para = [line]; i++;
        while (i < lines.length && lines[i].trim() && !startsBlock(lines[i], lines[i + 1])) para.push(lines[i++]);
        const p = el('p');
        para.forEach((l, idx) => { if (idx) p.appendChild(el('br')); inline(p, l); });
        root.appendChild(p);
      }
      return root;
    }
    return render;
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { createMarkdownRenderer };
  else scope.renderMarkdown = createMarkdownRenderer(scope.document);
})(typeof window !== 'undefined' ? window : globalThis);
