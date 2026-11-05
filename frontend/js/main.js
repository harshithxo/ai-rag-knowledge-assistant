/* App shell: page navigation, sidebar, theme and start-up. */
'use strict';

const TITLES = { chat: 'Chat', library: 'Document Library', settings: 'Settings' };
const PAGES = { chat: renderChat, library: renderLibrary, settings: renderSettings };

function go(view) {
  state.view = view;
  const app = $('#app');
  app.classList.remove('nav-open'); syncSidebarButton();
  document.querySelectorAll('.nav-item').forEach((n) => { const on = n.dataset.view === view; n.classList.toggle('active', on); on ? n.setAttribute('aria-current', 'page') : n.removeAttribute('aria-current'); });
  const actions = clear($('#topActions'));
  $('#topTitle').textContent = TITLES[view];
  PAGES[view](clear($('#view')), actions);
  const account = h('button', { class: 'btn btn-quiet', type: 'button', text: 'Account', onclick: async () => {
    try {
      const { user } = await api('/auth/me');
      modal((close) => {
        const name = h('input', { value: user.full_name, maxlength: '100', 'aria-label': 'Full name' });
        const current = h('input', { type: 'password', placeholder: 'Current password', autocomplete: 'current-password' });
        const next = h('input', { type: 'password', placeholder: 'New password (10+ characters)', autocomplete: 'new-password' });
        const confirm = h('input', { type: 'password', placeholder: 'Confirm new password', autocomplete: 'new-password' });
        const save = h('button', { class: 'btn btn-accent', type: 'button', text: 'Save profile', onclick: async () => {
          try { await patch('/auth/profile', { full_name: name.value }); toast('Profile updated', 'success'); close(); }
          catch (e) { toast(e.message, 'error'); }
        }});
        const change = h('button', { class: 'btn btn-accent', type: 'button', text: 'Change password', onclick: async () => {
          try { await post('/auth/change-password', { current_password: current.value, new_password: next.value, confirm_password: confirm.value }); toast('Password changed. Sign in again.', 'success'); location.reload(); }
          catch (e) { toast(e.message, 'error'); }
        }});
        return h('div', { class: 'settings-form' },
          h('div', { class: 'settings-heading' }, h('h2', {}, 'Account settings'),
            h('p', {}, user.email + (user.is_verified ? ' · Verified' : ' · Unverified'))),
          h('section', { class: 'settings-section' }, h('h3', {}, 'Profile'),
            h('label', {}, 'Full name', name),
            save),
          h('section', { class: 'settings-section password-section' }, h('h3', {}, 'Change password'),
            h('label', {}, 'Current password', current),
            h('label', {}, 'New password', next),
            h('label', {}, 'Confirm new password', confirm),
            change),
          h('div', { class: 'settings-footer' },
            h('button', { class: 'btn btn-quiet', type: 'button', text: 'Close', onclick: close })));
      });
    } catch (e) { toast(e.message, 'error'); }
  }});
  const signout = h('button', { class: 'btn btn-quiet', type: 'button', text: 'Sign out', onclick: async () => {
    try { const csrf = decodeURIComponent(document.cookie.split('; ').find(x => x.startsWith('docmind_csrf='))?.split('=').slice(1).join('=') || '');
      await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf } });
    } finally { location.reload(); }
  }});
  actions.append(account, signout);
  renderHistory();
}

// ---------------------------------------------------------------- sidebar
// On desktop the panel icon collapses or expands the sidebar. On mobile it opens or closes it as an overlay.
function syncSidebarButton() {
  const app = $('#app');
  const open = isMobile() ? app.classList.contains('nav-open') : !app.classList.contains('sidebar-collapsed');
  $('#toggleSidebar').setAttribute('aria-expanded', String(open));
}
function toggleSidebar() {
  const app = $('#app');
  if (isMobile()) app.classList.toggle('nav-open');
  else {
    app.classList.toggle('sidebar-collapsed');
    try { localStorage.setItem('sidebar', app.classList.contains('sidebar-collapsed') ? 'collapsed' : 'open'); } catch { /* private mode */ }
  }
  syncSidebarButton();
}
function closeMobileSidebar() { $('#app').classList.remove('nav-open'); syncSidebarButton(); }

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('theme', t); } catch { /* private mode */ }
}

$('#toggleSidebar').addEventListener('click', toggleSidebar);
$('#closeSidebar').addEventListener('click', () => { closeMobileSidebar(); $('#toggleSidebar').focus(); });
$('#scrim').addEventListener('click', closeMobileSidebar);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && isMobile() && $('#app').classList.contains('nav-open')) { closeMobileSidebar(); $('#toggleSidebar').focus(); } });
window.addEventListener('resize', debounce(() => { if (!isMobile()) $('#app').classList.remove('nav-open'); syncSidebarButton(); }, 100));
document.querySelectorAll('.nav-item').forEach((n) => n.addEventListener('click', () => go(n.dataset.view)));
$('#newChatBtn').addEventListener('click', newChat);

(async function boot() {
  try { if (localStorage.getItem('sidebar') === 'collapsed') $('#app').classList.add('sidebar-collapsed'); } catch { /* ignore */ }
  syncSidebarButton();
  go('chat');
  api('/settings').then((s) => { state.limits.maxUploadMb = s.max_upload_size_mb; }).catch(() => {});
  await Promise.all([loadDocs(), loadConversations(), checkHealth()]);
  if (state.view === 'chat') { renderScopeSelect(); if (!state.messages.length) renderMessages(); }
  setInterval(checkHealth, 30000);
})();
