/* Shared helpers and state for the DocMind frontend. Plain JavaScript, no build step.
 * Text from the server or the user is only ever added with textContent or renderMarkdown,
 * never innerHTML. The scripts are loaded in order by index.html and share these globals. */
'use strict';

// ---------------------------------------------------------------- tiny DOM helpers
const $ = (sel, root = document) => root.querySelector(sel);

/** h('div', {class: 'x', onclick: fn}, child, 'text') builds a DOM element. */
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value == null) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'text') node.textContent = value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.appendChild(typeof child === 'string' || typeof child === 'number' ? document.createTextNode(String(child)) : child);
  }
  return node;
}
function icon(name, cls) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon' + (cls ? ' ' + cls : ''));
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name);
  svg.appendChild(use);
  return svg;
}
/** A button with just an icon. It always gets an aria-label and a tooltip (data-tip). */
function iconButton(name, label, onclick, extraClass) {
  return h('button', { class: 'icon-btn' + (extraClass ? ' ' + extraClass : ''), type: 'button', 'aria-label': label, 'data-tip': label, onclick }, icon(name));
}
const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const formatBytes = (n) => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(0) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const formatDate = (iso) => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); };
const slug = (s) => (s || 'export').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 50) || 'export';
const isMobile = () => window.matchMedia('(max-width: 820px)').matches;

// ---------------------------------------------------------------- network helpers
function csrfCookie() { return document.cookie.split('; ').find((x) => x.startsWith('docmind_csrf='))?.split('=').slice(1).join('=') || ''; }
function securedOptions(options = {}) {
  const headers = new Headers(options.headers || {});
  const method = (options.method || 'GET').toUpperCase();
  if (!['GET','HEAD','OPTIONS'].includes(method)) { const token = decodeURIComponent(csrfCookie()); if (token) headers.set('X-CSRF-Token', token); }
  return { ...options, headers, credentials: 'same-origin' };
}
async function api(path, options = {}) {
  options = securedOptions(options);
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('Cannot reach the server. Is the backend running?'); }
  const type = response.headers.get('content-type') || '';
  const data = type.includes('json') ? await response.json().catch(() => ({})) : await response.text();
  if (!response.ok) throw new Error(errorText(data, response.status));
  return data;
}
function errorText(data, status) {
  const detail = typeof data === 'object' && data ? data.detail : null;
  const msg = Array.isArray(detail) ? detail.map((d) => d.msg).join('; ') : detail;
  return msg || `Request failed (${status})`;
}
const jsonBody = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
const post = (path, body) => api(path, jsonBody('POST', body));
const patch = (path, body) => api(path, jsonBody('PATCH', body));

/** POST JSON and read the newline-delimited JSON (NDJSON) reply as it arrives.
 *  onEvent is called once per event, e.g. {type:'token', text:'Hel'}. */
async function streamPost(path, body, onEvent, signal) {
  let response;
  try { response = await fetch(path, securedOptions({ ...jsonBody('POST', body), signal })); }
  catch (e) { if (e.name === 'AbortError') throw e; throw new Error('Cannot reach the server. Is the backend running?'); }
  if (!response.ok) throw new Error(errorText(await response.json().catch(() => ({})), response.status));
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const flush = (line) => { line = line.trim(); if (line) onEvent(JSON.parse(line)); };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf('\n')) >= 0) { flush(buffer.slice(0, newline)); buffer = buffer.slice(newline + 1); }
  }
  flush(buffer);
}

// ---------------------------------------------------------------- toasts, clipboard, download
function toast(message, kind = 'info') {
  const node = h('div', { class: 'toast ' + kind, role: kind === 'error' ? 'alert' : 'status' }, message);
  $('#toasts').appendChild(node);
  setTimeout(() => node.remove(), kind === 'error' ? 6500 : 3500);
}
async function copyText(value) {
  try { await navigator.clipboard.writeText(value); toast('Copied to clipboard', 'success'); }
  catch {
    const ta = h('textarea', { style: 'position:fixed;opacity:0' }); ta.value = value; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast('Copied to clipboard', 'success'); } catch { toast('Copy failed', 'error'); }
    ta.remove();
  }
}
function download(filename, content, type = 'text/markdown') {
  const url = URL.createObjectURL(new Blob([content], { type: type + ';charset=utf-8' }));
  const a = h('a', { href: url, download: filename }); document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------------- dialogs
let lastFocus = null;
let currentDismiss = null;
function closeModal() { clear($('#modalRoot')); currentDismiss = null; if (lastFocus && lastFocus.isConnected) lastFocus.focus(); }
/** Escape or a click outside the box dismisses the dialog. Buttons inside use the `close` function they are given. */
function dismissModal() { const callback = currentDismiss; closeModal(); if (callback) callback(); }
function modal(content, { wide = false, onDismiss } = {}) {
  lastFocus = document.activeElement;
  const box = h('div', { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true' }, content(closeModal));
  const backdrop = h('div', { class: 'modal-backdrop', onclick: (e) => { if (e.target === backdrop) dismissModal(); } }, box);
  clear($('#modalRoot')).appendChild(backdrop);
  currentDismiss = onDismiss || null;
  const first = box.querySelector('input, button'); if (first) first.focus();
}
function confirmDialog(title, message, confirmLabel, danger = true) {
  return new Promise((resolve) => {
    modal((close) => [
      h('h3', { text: title }), h('p', { text: message }),
      h('div', { class: 'modal-actions' },
        h('button', { class: 'btn', type: 'button', onclick: () => { close(); resolve(false); } }, 'Cancel'),
        h('button', { class: 'btn ' + (danger ? 'btn-danger' : 'btn-accent'), type: 'button', onclick: () => { close(); resolve(true); } }, confirmLabel)),
    ], { onDismiss: () => resolve(false) });
  });
}
function promptDialog(title, value) {
  return new Promise((resolve) => {
    modal((close) => {
      const input = h('input', { class: 'input', maxlength: '80', value, 'aria-label': title });
      const submit = () => { const v = input.value.trim(); close(); resolve(v || null); };
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
      setTimeout(() => input.select(), 30);
      return [h('h3', { text: title }), input,
        h('div', { class: 'modal-actions' },
          h('button', { class: 'btn', type: 'button', onclick: () => { close(); resolve(null); } }, 'Cancel'),
          h('button', { class: 'btn btn-accent', type: 'button', onclick: submit }, 'Save'))];
    }, { onDismiss: () => resolve(null) });
  });
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('#modalRoot').firstChild) dismissModal(); });

// ---------------------------------------------------------------- shared state
const state = {
  view: 'chat',
  docs: [],                 // all documents in the library
  conversations: [],
  currentId: null,          // open conversation (null means a new chat that isn't saved yet)
  messages: [],             // messages of the open conversation, plus temporary notices
  scope: '',                // '' searches all documents, otherwise one document id
  limits: { maxUploadMb: 20, maxFiles: 10 },
};
const readyDocs = () => state.docs.filter((d) => d.status === 'ready');
let pollTimer = null;

async function loadDocs() {
  try { state.docs = (await api('/documents')).documents; } catch (e) { toast(e.message, 'error'); }
  const badge = $('#docCount');
  badge.hidden = !state.docs.length; badge.textContent = state.docs.length;
  schedulePolling();
}
async function loadConversations() {
  try { state.conversations = (await api('/conversations')).conversations; } catch { /* keep the old list */ }
  renderHistory();
}
/** While a document is still being indexed, refresh the list every 2 seconds. */
function schedulePolling() {
  const busy = state.docs.some((d) => d.status === 'processing');
  if (busy && !pollTimer) {
    pollTimer = setInterval(async () => {
      await loadDocs();
      if (state.view === 'library') renderLibraryList();
      if (state.view === 'chat') renderScopeSelect();
    }, 2000);
  } else if (!busy && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
async function checkHealth() {
  const dot = $('#health .dot'), label = $('#healthText');
  try {
    const res = await api('/health'); const o = res.ai || res.ollama;
    const provider = res.ai_provider || 'ollama';
    if (!o.reachable) { dot.className = 'dot dot-off'; label.textContent = provider === 'ollama' ? 'Ollama is not running' : 'AI provider is not configured'; }
    else if (!o.llm_installed || !o.embedding_installed) { dot.className = 'dot dot-warn'; label.textContent = 'An AI model is not configured (see Settings)'; }
    else { dot.className = o.configured_only ? 'dot dot-warn' : 'dot dot-ok'; label.textContent = o.configured_only ? 'AI provider configured' : res.llm_model + ' is ready'; }
    return res;
  } catch { dot.className = 'dot dot-off'; label.textContent = 'Backend is offline'; return null; }
}
