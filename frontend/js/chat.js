/* Chat page: the chat list, file attachments (the only way to upload documents) and streaming answers. */
'use strict';

let chat = {};              // DOM elements of the open chat page
let staged = [];            // files chosen with the paperclip: {file, status, progress, docId, doc, error, xhr}
let busy = false;           // uploading or waiting for an answer
let streamCtl = null;       // AbortController of the running answer (used by the Stop button)
let live = null;            // DOM pieces of the answer that is being streamed
let lastRequest = null;     // remembered so "Retry" can ask again
let followUpToken = 0;

const SUGGESTIONS = [
  ['book', 'Summarize', 'Give me a summary of my documents'],
  ['bulb', 'Key takeaways', 'What are the most important points across my documents?'],
  ['search', 'Find details', 'What deadlines, dates or numbers are mentioned?'],
  ['help', 'Explain simply', 'Explain the main topic of my documents like I am a beginner'],
];

// ---------------------------------------------------------------- sidebar history
function renderHistory() {
  const box = clear($('#history'));
  if (!state.conversations.length) { box.appendChild(h('div', { class: 'history-empty' }, 'No conversations yet')); return; }
  for (const c of state.conversations) {
    box.appendChild(h('div', { class: 'hist-item' + (c.id === state.currentId && state.view === 'chat' ? ' active' : '') },
      h('button', { class: 'hist-title', type: 'button', title: c.title, onclick: () => openConversation(c.id) }, c.title),
      h('span', { class: 'hist-actions' },
        iconButton('edit', 'Rename chat', () => renameConversation(c), 'small'),
        iconButton('trash', 'Delete chat', () => deleteConversation(c), 'small'))));
  }
}

// ---------------------------------------------------------------- page layout
function renderChat(container, actions) {
  const convo = state.conversations.find((c) => c.id === state.currentId);
  $('#topTitle').textContent = convo ? convo.title : 'New chat';

  const scopeSelect = h('select', { class: 'select', 'aria-label': 'Search in', onchange: onScopeChange });
  actions.appendChild(scopeSelect);
  ensureExportButton(actions);

  const messages = h('div', { class: 'messages', id: 'messages' });
  const attachRow = h('div', { class: 'attach-row', hidden: true, 'aria-label': 'Attached files' });
  const textarea = h('textarea', { rows: '1', maxlength: '4000', placeholder: 'Ask about your documents…', 'aria-label': 'Message' });
  const sendBtn = h('button', { class: 'send', type: 'button', onclick: onSendClick });
  const fileInput = h('input', { type: 'file', accept: 'application/pdf,.pdf', multiple: true, hidden: true, 'aria-hidden': 'true', tabindex: '-1' });
  fileInput.addEventListener('change', () => { stageFiles([...fileInput.files]); fileInput.value = ''; });
  const attachBtn = iconButton('clip', 'Attach PDFs', () => fileInput.click());
  const composer = h('div', { class: 'composer' }, attachRow,
    h('div', { class: 'composer-row' }, attachBtn, textarea, sendBtn, fileInput));

  textarea.addEventListener('input', () => { textarea.style.height = 'auto'; textarea.style.height = Math.min(textarea.scrollHeight, 180) + 'px'; updateComposer(); });
  textarea.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); onSendClick(); } });
  // Dropping files on the message box works like the paperclip: they are added as attachments.
  ['dragenter', 'dragover'].forEach((ev) => composer.addEventListener(ev, (e) => { e.preventDefault(); composer.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach((ev) => composer.addEventListener(ev, (e) => { e.preventDefault(); composer.classList.remove('dragover'); }));
  composer.addEventListener('drop', (e) => stageFiles([...e.dataTransfer.files]));

  container.appendChild(h('div', { class: 'chat' }, messages,
    h('div', { class: 'composer-wrap' }, composer,
      h('div', { class: 'composer-hint' }, 'Enter to send · Shift+Enter for a new line · Answers come from your documents'))));
  chat = { messages, textarea, sendBtn, scopeSelect, attachRow, composer, openFilePicker: () => fileInput.click() };
  renderScopeSelect(); renderChips(); updateComposer(); renderMessages();
  if (!isMobile()) textarea.focus();
}

/** The export button needs a saved conversation, so a brand-new chat gets it after its first answer. */
function ensureExportButton(actions = $('#topActions')) {
  if (!state.currentId || state.view !== 'chat' || actions.querySelector('.export-btn')) return;
  actions.appendChild(iconButton('download', 'Export chat as Markdown', exportConversation, 'export-btn'));
}

function renderScopeSelect() {
  if (!chat.scopeSelect || !chat.scopeSelect.isConnected) return;
  const select = clear(chat.scopeSelect);
  select.appendChild(h('option', { value: '' }, 'All documents'));
  readyDocs().forEach((d) => select.appendChild(h('option', { value: d.id }, d.filename)));
  if (state.scope && !readyDocs().some((d) => d.id === state.scope)) state.scope = '';
  select.value = state.scope;
}

async function onScopeChange(e) {
  state.scope = e.target.value;
  if (!state.currentId) return;                       // a new chat sends the scope with its first question
  try {
    await patch('/conversations/' + state.currentId, state.scope ? { document_id: state.scope } : { clear_document: true });
    await loadConversations();
  } catch (err) { toast(err.message, 'error'); }
}

function updateComposer() {
  if (!chat.sendBtn || !chat.sendBtn.isConnected) return;
  const btn = clear(chat.sendBtn);
  const hasInput = chat.textarea.value.trim() || staged.length;
  if (streamCtl) {
    btn.appendChild(icon('stop')); btn.disabled = false; btn.setAttribute('aria-label', 'Stop generating'); btn.setAttribute('data-tip', 'Stop generating');
  } else if (busy) {
    btn.appendChild(h('span', { class: 'spinner' })); btn.disabled = true; btn.setAttribute('aria-label', 'Working…'); btn.removeAttribute('data-tip');
  } else {
    const label = chat.textarea.value.trim() ? 'Send message' : 'Upload files';
    btn.appendChild(icon('send')); btn.disabled = !hasInput; btn.setAttribute('aria-label', label); btn.setAttribute('data-tip', label);
  }
}

// ---------------------------------------------------------------- attachments (paperclip)
function stageFiles(files) {
  if (busy) { toast('Please wait until the current request finishes.'); return; }
  const mb = state.limits.maxUploadMb;
  for (const file of files) {
    if (!/\.pdf$/i.test(file.name)) { toast(`${file.name}: only PDF files are supported`, 'error'); continue; }
    if (file.size === 0) { toast(`${file.name} is empty`, 'error'); continue; }
    if (file.size > mb * 1048576) { toast(`${file.name} is larger than the ${mb} MB limit`, 'error'); continue; }
    if (staged.some((s) => s.file.name === file.name && s.file.size === file.size)) { toast(`${file.name} is already attached`); continue; }
    if (staged.length >= state.limits.maxFiles) { toast(`You can attach up to ${state.limits.maxFiles} files at once`, 'error'); break; }
    staged.push({ file, status: 'staged', progress: 0 });
  }
  renderChips(); updateComposer();
  if (chat.textarea) chat.textarea.focus();
}

function removeStaged(item) {
  if (item.xhr) item.xhr.abort();
  staged = staged.filter((s) => s !== item);
  renderChips(); updateComposer();
}

function chipStatus(item) {
  switch (item.status) {
    case 'staged': return formatBytes(item.file.size);
    case 'uploading': return `Uploading ${item.progress}%`;
    case 'processing': return 'Indexing…';
    case 'ready': return item.duplicate ? 'Already in your library' : 'Ready';
    default: return item.error || 'Failed';
  }
}
function fileChip(item) {
  const lead = item.status === 'processing' ? h('span', { class: 'spinner' })
    : item.status === 'ready' ? icon('check', 'ok') : item.status === 'failed' ? icon('alert') : icon('file');
  const removable = ['staged', 'ready', 'failed'].includes(item.status);
  return h('div', { class: `file-chip is-${item.status}` }, lead,
    h('span', { class: 'fc-text' }, h('span', { class: 'fc-name', title: item.file.name }, item.file.name),
      h('span', { class: 'fc-status', title: chipStatus(item) }, chipStatus(item))),
    removable && iconButton('x', `Remove ${item.file.name}`, () => removeStaged(item), 'small'),
    item.status === 'uploading' && h('div', { class: 'progress' }, h('div', { style: `width:${item.progress}%` })),
    item.status === 'processing' && h('div', { class: 'progress indeterminate' }, h('div')));
}
function renderChips() {
  if (!chat.attachRow || !chat.attachRow.isConnected) return;
  const row = clear(chat.attachRow);
  row.hidden = !staged.length;
  staged.forEach((item) => row.appendChild(fileChip(item)));
}

function failItem(item, message) { item.status = 'failed'; item.error = message; item.xhr = null; renderChips(); }

function uploadItem(item) {
  return new Promise((resolve) => {
    item.status = 'uploading'; item.progress = 0; item.error = ''; renderChips();
    const form = new FormData(); form.append('files', item.file);
    const xhr = new XMLHttpRequest(); item.xhr = xhr;
    xhr.open('POST', '/documents/upload');
    // Uploads use XMLHttpRequest rather than fetch, so attach the same CSRF
    // header used by the shared API helper.
    const csrf = typeof csrfCookie === 'function' ? decodeURIComponent(csrfCookie()) : '';
    if (csrf) xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { item.progress = Math.round((e.loaded / e.total) * 100); renderChips(); } };
    xhr.onload = () => {
      item.xhr = null;
      let data = {}; try { data = JSON.parse(xhr.responseText); } catch { /* handled below */ }
      if (xhr.status >= 400 || !data.results) { failItem(item, errorText(data, xhr.status)); return resolve(); }
      if (data.errors && data.errors.length) { failItem(item, data.errors[0].detail); return resolve(); }
      const result = data.results[0];
      item.docId = result.document.id; item.doc = result.document; item.duplicate = result.duplicate; item.progress = 100;
      item.status = result.document.status === 'failed' ? 'failed' : result.document.status === 'ready' ? 'ready' : 'processing';
      if (item.status === 'failed') item.error = result.document.error || 'Processing failed';
      renderChips(); resolve();
    };
    xhr.onerror = () => { failItem(item, 'Cannot reach the server'); resolve(); };
    xhr.onabort = () => resolve();
    xhr.send(form);
  });
}

/** Wait until the server has finished extracting, chunking and embedding the file. */
async function waitUntilIndexed(item) {
  for (let i = 0; i < 900 && item.status === 'processing'; i++) {       // up to ~15 minutes
    await sleep(1000);
    try {
      const doc = await api('/documents/' + item.docId);
      if (doc.status === 'ready') { item.doc = doc; item.status = 'ready'; }
      else if (doc.status === 'failed') failItem(item, doc.error || 'Processing failed');
    } catch (e) { if (/not found/i.test(e.message)) failItem(item, 'The document was removed'); }
    renderChips();
  }
  if (item.status === 'processing') failItem(item, 'Indexing is taking too long');
}

/** Upload every staged file, then wait for indexing. Returns true when all are ready. */
async function uploadStaged() {
  for (const item of staged.filter((s) => s.status === 'staged' || s.status === 'failed')) await uploadItem(item);
  await Promise.all(staged.filter((s) => s.status === 'processing').map(waitUntilIndexed));
  await loadDocs(); renderScopeSelect();
  return staged.length > 0 && staged.every((s) => s.status === 'ready');
}

// ---------------------------------------------------------------- sending
async function onSendClick() {
  if (streamCtl) { streamCtl.abort(); return; }                         // the button is "Stop" while answering
  if (busy) return;
  const text = chat.textarea.value.trim();
  if (!text && !staged.length) return;

  busy = true; updateComposer();
  let attachInfo = [];
  if (staged.length) {
    const ok = await uploadStaged();
    if (!ok) {
      busy = false; updateComposer();
      toast('Some files could not be added. Remove them or try again.', 'error');
      return;
    }
    attachInfo = staged.map((s) => ({ id: s.docId, filename: s.doc.filename }));
    if (!text) {                                                         // upload only: confirm, no question yet
      state.messages.push({ role: 'notice', files: staged.map((s) => ({ filename: s.doc.filename, pages: s.doc.pages, chunks: s.doc.chunks, duplicate: s.duplicate })) });
      staged = []; busy = false; renderChips(); updateComposer(); renderMessages();
      return;
    }
  }
  chat.textarea.value = ''; chat.textarea.style.height = 'auto';
  staged = []; renderChips();
  await askQuestion(text, attachInfo);
}

function submitSuggestion(text) { if (!busy) { chat.textarea.value = text; onSendClick(); } }

async function askQuestion(text, attachInfo = []) {
  lastRequest = { text, attachInfo };
  followUpToken++;
  const pending = { role: 'assistant', pending: true, content: '' };
  state.messages.push({ role: 'user', content: text, attachments: attachInfo }, pending);
  live = { status: 'Thinking…' };
  busy = true; streamCtl = new AbortController(); updateComposer();
  renderMessages(); scrollToBottom(true);

  const wasNew = !state.currentId;
  const idsBefore = new Set(state.conversations.map((c) => c.id));
  try {
    await streamPost('/chat/stream', {
      question: text, conversation_id: state.currentId, document_id: state.scope || null,
      attachment_ids: attachInfo.length ? attachInfo.map((a) => a.id) : null,
    }, (event) => onChatEvent(event, pending), streamCtl.signal);
    if (!pending.finished) failPending(pending, 'The answer was interrupted. Please try again.');
  } catch (e) {
    if (e.name === 'AbortError') await onStopped(pending, wasNew, idsBefore, text);
    else failPending(pending, e.message);
  } finally {
    busy = false; streamCtl = null; live = null; updateComposer();
    if (chat.textarea && chat.textarea.isConnected && !isMobile()) chat.textarea.focus();
  }
}

function onChatEvent(event, pending) {
  if (event.type === 'status') { if (live) { live.status = event.text; if (live.statusEl && !pending.content) live.statusEl.textContent = event.text; } }
  else if (event.type === 'token') { pending.content += event.text; scheduleLiveRender(pending); }
  else if (event.type === 'error') { failPending(pending, event.detail); }
  else if (event.type === 'done') {
    pending.finished = true;
    state.currentId = event.conversation_id; ensureExportButton();
    state.messages[state.messages.indexOf(pending)] = event.message;     // the saved message replaces the draft
    renderMessages(); scrollToBottom(false);
    loadConversations().then(() => { const c = state.conversations.find((x) => x.id === state.currentId); if (c && state.view === 'chat') $('#topTitle').textContent = c.title; });
    if (event.message.sources && event.message.sources.length) loadFollowUps(event.message);
  }
}

function failPending(pending, message) {
  pending.finished = true;
  const i = state.messages.indexOf(pending);
  if (i >= 0) state.messages[i] = { role: 'assistant', content: message, error: true };
  renderMessages();
}

async function onStopped(pending, wasNew, idsBefore, text) {
  pending.finished = true;
  if (!pending.content.trim()) {                                         // nothing was written: undo the question
    state.messages.splice(state.messages.indexOf(pending) - 1, 2);
    if (chat.textarea) chat.textarea.value = text;
    renderMessages(); updateComposer(); return;
  }
  pending.pending = false; pending.content = pending.content.trim() + '\n\n_(Response stopped)_';
  renderMessages();
  if (wasNew) {                                                          // the server saves the partial answer a moment later
    for (let i = 0; i < 8; i++) {
      await sleep(500); await loadConversations();
      const fresh = state.conversations.find((c) => !idsBefore.has(c.id));
      if (fresh) { state.currentId = fresh.id; renderHistory(); ensureExportButton(); break; }
    }
  } else await loadConversations();
}

/** Ask the server for suggested next questions once the answer is on screen, so they never slow the answer down. */
async function loadFollowUps(message) {
  const token = followUpToken;
  try {
    const { follow_ups } = await post('/chat/follow-ups', { message_id: message.id });
    if (token !== followUpToken || !follow_ups.length) return;           // the user already asked something else
    message.follow_ups = follow_ups;
    if (state.view === 'chat') renderMessages();
  } catch { /* suggestions are optional: stay silent */ }
}

// ---------------------------------------------------------------- rendering messages
function welcomeNode() {
  const ready = readyDocs().length;
  if (!ready) {
    return h('div', { class: 'welcome' }, h('div', { class: 'brand-mark' }, icon('logo')),
      h('h2', { text: 'Chat with your documents' }),
      h('p', { text: 'Attach a PDF with the paperclip, then ask questions about it. Everything runs locally on your computer with Ollama.' }),
      h('button', { class: 'btn btn-accent', type: 'button', onclick: () => chat.openFilePicker() }, icon('clip'), 'Attach a PDF'));
  }
  return h('div', { class: 'welcome' }, h('div', { class: 'brand-mark' }, icon('logo')),
    h('h2', { text: 'What would you like to know?' }),
    h('p', { text: `${ready} document${ready > 1 ? 's' : ''} ready. Ask a question, attach another PDF, or start with an idea.` }),
    h('div', { class: 'prompts' }, SUGGESTIONS.map(([ic, title, prompt]) =>
      h('button', { class: 'prompt-card', type: 'button', onclick: () => submitSuggestion(prompt) }, icon(ic), h('div', {}, h('strong', { text: title }), h('span', { text: prompt }))))));
}

function renderMessages() {
  if (!chat.messages || !chat.messages.isConnected) return;
  const stick = nearBottom();
  const box = clear(chat.messages);
  live && (live.contentEl = null);
  if (!state.messages.length) { box.appendChild(welcomeNode()); return; }
  const thread = h('div', { class: 'thread' });
  const last = state.messages.length - 1;
  state.messages.forEach((m, i) => thread.appendChild(messageNode(m, i === last)));
  box.appendChild(thread);
  if (stick) box.scrollTop = box.scrollHeight;
}

const nearBottom = () => !chat.messages || chat.messages.scrollHeight - chat.messages.scrollTop - chat.messages.clientHeight < 140;
function scrollToBottom(force) { if (chat.messages && (force || nearBottom())) chat.messages.scrollTop = chat.messages.scrollHeight; }

function attachmentChips(list) {
  return h('div', { class: 'msg-files' }, (list || []).map((a) => h('span', { class: 'file-chip static' }, icon('file'), h('span', { class: 'fc-name', title: a.filename }, a.filename))));
}

function messageNode(m, isLast) {
  if (m.role === 'notice') return noticeNode(m);
  if (m.role === 'user') {
    return h('div', { class: 'msg user' }, m.attachments && m.attachments.length ? attachmentChips(m.attachments) : null, h('div', { class: 'bubble', text: m.content }));
  }
  if (m.error) {
    return h('div', { class: 'msg assistant msg-error' }, h('div', { class: 'avatar' }, icon('alert')),
      h('div', { class: 'bubble' }, h('div', { class: 'content', role: 'alert' }, m.content),
        h('div', { class: 'msg-actions' }, h('button', { class: 'btn btn-sm', type: 'button', onclick: retryLast }, icon('refresh'), 'Try again'))));
  }
  if (m.pending) return pendingNode(m);
  return answerNode(m, isLast);
}

function noticeNode(m) {
  const n = m.files.length;
  return h('div', { class: 'msg msg-notice' }, h('div', { class: 'notice' },
    h('h3', {}, icon('check'), n === 1 ? 'Document ready' : `${n} documents ready`),
    h('ul', {}, m.files.map((f) => h('li', {}, `${f.filename}` + (f.duplicate ? ' (was already in your library)' : ` – ${f.pages} pages, ${f.chunks} passages indexed`)))),
    h('p', {}, 'Ask a question about ' + (n === 1 ? 'it' : 'them') + ' below. Manage files in the Document Library.')));
}

// Models sometimes write citation markers like [Source 1] or [Sources 1 and 2]. Answers never show them.
const CITATION = /\[Sources?\s*\d+(?:\s*(?:,|and|&)\s*\d+)*\]/gi;
const stripCitations = (text) => text.replace(CITATION, '').replace(/\s+([.,;:!?])/g, '$1');
// While an answer streams, a marker can be half written ("[Sour"), so that unfinished tail is hidden too.
const hidePartialCitation = (text) => text.replace(CITATION, '').replace(/\[(?:S[a-z]*\s?\d*[,\s\d]*)?$/i, '');

function pendingNode(m) {
  const statusEl = h('span', { text: (live && live.status) || 'Thinking…' });
  const status = h('div', { class: 'status-line' }, h('span', { class: 'dots' }, h('i'), h('i'), h('i')), statusEl);
  const contentEl = h('div', { class: 'live-content' });
  live.statusEl = statusEl; live.statusWrap = status; live.contentEl = contentEl;
  if (m.content) paintLive(m); else contentEl.appendChild(status);
  return h('div', { class: 'msg assistant', 'aria-live': 'off' }, h('div', { class: 'avatar' }, icon('logo')), h('div', { class: 'bubble' }, contentEl));
}

let liveFrame = 0;
function scheduleLiveRender(m) {
  if (liveFrame) return;
  liveFrame = requestAnimationFrame(() => { liveFrame = 0; paintLive(m); });
}
function paintLive(m) {
  if (!live || !live.contentEl || !live.contentEl.isConnected) return;
  const stick = nearBottom();
  const body = window.renderMarkdown(hidePartialCitation(m.content));
  body.classList.add('caret');
  clear(live.contentEl).appendChild(body);
  if (stick) scrollToBottom(true);
}

function answerNode(m, isLast) {
  const cleanAnswer = stripCitations(m.content || '').replace(/\n{3,}/g, '\n\n').trim();
  const body = h('div', { class: 'bubble' }, window.renderMarkdown(cleanAnswer));
  body.appendChild(h('div', { class: 'msg-actions' },
    iconButton('copy', 'Copy answer', () => copyText(cleanAnswer), 'small'),
    iconButton('download', 'Export answer as Markdown', () => download('answer-' + slug(m.content.slice(0, 30)) + '.md', answerMarkdown(m)), 'small')));
  if (isLast && m.follow_ups && m.follow_ups.length)
    body.appendChild(h('div', { class: 'chips' }, m.follow_ups.map((q) => h('button', { class: 'chip', type: 'button', onclick: () => submitSuggestion(q) }, q))));
  return h('div', { class: 'msg assistant' }, h('div', { class: 'avatar' }, icon('logo')), body);
}

function answerMarkdown(m) {
  return stripCitations(m.content || '').trim() + '\n';
}

function retryLast() {
  if (!lastRequest || busy) return;
  state.messages = state.messages.filter((m) => !m.error);
  state.messages.pop();                                                  // the unanswered question is asked again
  const { text, attachInfo } = lastRequest;
  askQuestion(text, attachInfo);
}

// ---------------------------------------------------------------- conversations
function newChat() {
  if (busy) { toast('Please wait until the current request finishes.'); return; }
  state.currentId = null; state.messages = []; staged = [];
  go('chat');
}
async function openConversation(id) {
  if (busy) { toast('Please wait until the current request finishes.'); return; }
  try {
    const data = await api('/conversations/' + id);
    state.currentId = id; state.messages = data.messages; state.scope = data.document_id || ''; staged = [];
    go('chat');
    scrollToBottom(true);
  } catch (e) { toast(e.message, 'error'); loadConversations(); }
}
async function renameConversation(c) {
  const title = await promptDialog('Rename chat', c.title);
  if (!title) return;
  try { await patch('/conversations/' + c.id, { title }); await loadConversations(); if (state.currentId === c.id && state.view === 'chat') $('#topTitle').textContent = title; }
  catch (e) { toast(e.message, 'error'); }
}
async function deleteConversation(c) {
  if (!(await confirmDialog('Delete this chat?', `"${c.title}" and its messages will be permanently deleted. Your documents are not affected.`, 'Delete'))) return;
  try {
    await api('/conversations/' + c.id, { method: 'DELETE' });
    if (state.currentId === c.id) { state.currentId = null; state.messages = []; }
    await loadConversations(); if (state.view === 'chat') go('chat');
    toast('Chat deleted', 'success');
  } catch (e) { toast(e.message, 'error'); }
}
async function exportConversation() {
  try {
    const md = await api(`/conversations/${state.currentId}/export`);
    const c = state.conversations.find((x) => x.id === state.currentId);
    download(slug(c && c.title) + '.md', md);
  } catch (e) { toast(e.message, 'error'); }
}
