/* Document Library: look at and manage documents. Files are only uploaded with the paperclip in the chat box. */
'use strict';

let libEls = {};
let nameFilter = '';
let contentQuery = '';

function renderLibrary(container) {
  const search = h('input', { class: 'input', type: 'search', placeholder: 'Filter by file name…', 'aria-label': 'Filter documents by name', value: nameFilter });
  search.addEventListener('input', () => { nameFilter = search.value.trim().toLowerCase(); contentQuery = ''; renderLibraryList(); });
  search.addEventListener('keydown', (e) => { if (e.key === 'Enter' && search.value.trim().length >= 2) runContentSearch(search.value.trim()); });
  const list = h('div', { id: 'libList' });
  libEls = { list, search };

  container.appendChild(h('div', { class: 'page' },
    h('h2', { text: 'Document Library' }),
    h('p', { class: 'sub', text: 'Preview or remove the documents your assistant can read. To add a document, attach a PDF in the chat.' }),
    h('div', { class: 'toolbar' }, h('div', { class: 'search' }, icon('search'), search),
      h('button', { class: 'btn', type: 'button', onclick: () => { if (search.value.trim().length >= 2) runContentSearch(search.value.trim()); else toast('Type at least 2 characters to search inside documents'); } }, 'Search inside documents')),
    list));
  renderLibraryList();
  loadDocs().then(() => { if (state.view === 'library') renderLibraryList(); });
}

function statusBadge(d) {
  if (d.status === 'processing') return h('span', { class: 'badge badge-warn' }, h('span', { class: 'spinner' }), 'Processing');
  if (d.status === 'failed') return h('span', { class: 'badge badge-danger' }, icon('alert'), 'Failed');
  return h('span', { class: 'badge badge-ok' }, icon('check'), 'Ready');
}

function renderLibraryList() {
  if (!libEls.list || !libEls.list.isConnected || contentQuery) return;      // search results are showing, leave them
  const list = clear(libEls.list);
  if (!state.docs.length) {
    list.appendChild(h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon('library')),
      h('h3', { text: 'No documents yet' }), h('p', { text: 'Attach a PDF with the paperclip in the chat and it will appear here.' }),
      h('button', { class: 'btn btn-accent', type: 'button', onclick: () => { go('chat'); chat.openFilePicker(); } }, icon('clip'), 'Attach a PDF in chat')));
    return;
  }
  const shown = state.docs.filter((d) => d.filename.toLowerCase().includes(nameFilter));
  if (!shown.length) { list.appendChild(h('div', { class: 'empty' }, h('h3', { text: 'No matching documents' }), h('p', { text: 'Try another name, or press Enter to search inside the documents.' }))); return; }
  list.appendChild(h('div', { class: 'doc-grid' }, shown.map(docCard)));
}

function docCard(d) {
  const ready = d.status === 'ready';
  return h('div', { class: 'doc' },
    h('div', { class: 'doc-ico' }, icon('file')),
    h('div', { style: 'min-width:0' },
      h('div', { class: 'doc-name', title: d.filename }, d.filename),
      h('div', { class: 'doc-meta' }, statusBadge(d), h('span', {}, formatBytes(d.size_bytes)), h('span', {}, formatDate(d.created_at)),
        ready && h('span', {}, `${d.pages} ${d.pages === 1 ? 'page' : 'pages'}`), ready && h('span', {}, `${d.chunks} chunks`)),
      d.status === 'failed' && h('div', { class: 'doc-err' }, d.error || 'Processing failed')),
    h('div', { class: 'doc-actions' },
      ready && h('button', { class: 'btn btn-sm', type: 'button', onclick: () => chatWithDoc(d) }, icon('chat'), 'Chat'),
      h('button', { class: 'btn btn-sm', type: 'button', onclick: () => previewDoc(d) }, icon('eye'), 'Preview'),
      d.status === 'failed' && h('button', { class: 'btn btn-sm', type: 'button', onclick: () => retryDoc(d) }, icon('refresh'), 'Retry'),
      h('button', { class: 'btn btn-sm btn-danger', type: 'button', onclick: () => deleteDoc(d), 'aria-label': 'Delete ' + d.filename }, icon('trash'), 'Delete')));
}

function previewDoc(d) {
  modal((close) => [
    h('div', { class: 'modal-head' }, h('span', { text: d.filename }), iconButton('x', 'Close preview', close)),
    h('iframe', { src: `/documents/${encodeURIComponent(d.id)}/file`, title: 'Preview of ' + d.filename }),
  ], { wide: true });
}
function chatWithDoc(d) { state.currentId = null; state.messages = []; state.scope = d.id; go('chat'); }
async function retryDoc(d) {
  try { await post(`/documents/${d.id}/reprocess`); toast('Processing started again'); await loadDocs(); renderLibraryList(); }
  catch (e) { toast(e.message, 'error'); }
}
async function deleteDoc(d) {
  if (!(await confirmDialog('Delete this document?', `"${d.filename}" will be removed from your library and from the search index. Chats that mention it keep their text. This cannot be undone.`, 'Delete'))) return;
  try {
    await api('/documents/' + d.id, { method: 'DELETE' });
    if (state.scope === d.id) state.scope = '';
    toast('Document deleted', 'success');
    await loadDocs(); await loadConversations();
    if (state.view === 'library') renderLibraryList();
  } catch (e) { toast(e.message, 'error'); }
}

/** Search inside the text of all documents, not just the file names. */
async function runContentSearch(q) {
  contentQuery = q;
  const list = clear(libEls.list);
  list.appendChild(h('div', { class: 'skeleton', style: 'height:74px;margin-bottom:8px' }));
  try {
    const { results } = await api('/documents/search?q=' + encodeURIComponent(q));
    if (contentQuery !== q || !libEls.list.isConnected) return;
    clear(list);
    list.appendChild(h('div', { class: 'toolbar' }, h('strong', { text: `Passages matching “${q}”` }),
      h('button', { class: 'btn btn-sm btn-ghost', type: 'button', onclick: () => { contentQuery = ''; nameFilter = ''; libEls.search.value = ''; renderLibraryList(); } }, 'Back to documents')));
    if (!results.length) { list.appendChild(h('div', { class: 'empty' }, h('h3', { text: 'No matching passages' }), h('p', { text: 'Try different keywords.' }))); return; }
    results.forEach((r) => list.appendChild(h('div', { class: 'hit' },
      h('div', { class: 'hit-head' }, h('strong', { text: r.filename + (r.page ? ' · page ' + r.page : '') }), h('span', { text: Math.round(Math.max(0, Math.min(1, r.score)) * 100) + '% relevant' })),
      h('p', { text: r.text }))));
  } catch (e) { clear(list).appendChild(h('div', { class: 'empty' }, h('h3', { text: 'Search failed' }), h('p', { text: e.message }))); contentQuery = ''; }
}
