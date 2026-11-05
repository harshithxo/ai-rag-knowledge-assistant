/* Settings page: appearance, AI response settings, AI status and an advanced section. */
'use strict';

async function renderSettings(container) {
  let s, health;
  try { [s, health] = await Promise.all([api('/settings'), checkHealth()]); }
  catch (e) { container.appendChild(h('div', { class: 'empty' }, h('h3', { text: 'Could not load settings' }), h('p', { text: e.message }))); return; }
  if (state.view !== 'settings') return;
  state.limits.maxUploadMb = s.max_upload_size_mb;

  // Appearance (applies right away and is remembered in this browser)
  const theme = document.documentElement.dataset.theme;
  const themeButtons = ['dark', 'light'].map((t) => h('button', { type: 'button', role: 'radio', 'aria-checked': String(t === theme),
    onclick: () => { setTheme(t); themeButtons.forEach((b, i) => b.setAttribute('aria-checked', String(['dark', 'light'][i] === t))); } }, t[0].toUpperCase() + t.slice(1)));
  const appearance = h('div', { class: 'card' }, h('h3', { text: 'Appearance' }),
    h('p', { class: 'card-sub', text: 'Choose how DocMind looks. This changes immediately.' }),
    h('div', { class: 'seg', role: 'radiogroup', 'aria-label': 'Theme' }, themeButtons));

  // editable controls: sliders for everyday settings, number boxes for advanced ones
  const controls = [];
  const slider = (key, label, help, min, max, step, fmt = (v) => v) => {
    const out = h('output', { text: String(fmt(s[key])) });
    const input = h('input', { type: 'range', min, max, step, value: s[key], id: 'set-' + key });
    input.addEventListener('input', () => { out.textContent = String(fmt(input.value)); });
    controls.push({ key, read: () => Number(input.value), write: (v) => { input.value = v; out.textContent = String(fmt(v)); } });
    return h('div', { class: 'field' }, h('label', { for: 'set-' + key, text: label }), h('div', { class: 'range-row' }, input, out), h('div', { class: 'help', text: help }));
  };
  const number = (key, label, help, min, max, unit) => {
    const input = h('input', { class: 'input num-input', type: 'number', min, max, step: 1, value: s[key], id: 'set-' + key });
    controls.push({ key, read: () => Number(input.value), write: (v) => { input.value = v; } });
    return h('div', { class: 'field' }, h('label', { for: 'set-' + key, text: label + (unit ? ` (${unit})` : '') }), input, h('div', { class: 'help', text: help }));
  };

  // AI response settings
  const response = h('div', { class: 'card' }, h('h3', { text: 'AI response settings' }),
    h('p', { class: 'card-sub', text: 'Control how much of your documents the assistant reads for each question.' }),
    slider('retrieval_top_k', 'Passages per question', 'How many pieces of your documents are given to the model. More passages give broader answers but take longer.', 1, 15, 1),
    slider('retrieval_min_score', 'Relevance threshold', 'Passages that match the question less than this are ignored. Higher means stricter: fewer, but more relevant passages.', 0, 0.9, 0.05));

  // AI status (read-only)
  const o = health && (health.ai || health.ollama);
  const provider = (health && health.ai_provider) || 'ollama';
  const local = provider === 'ollama';
  const row = (label, ok, value, fix) => h('div', { class: 'status-row' }, h('span', { class: 'dot ' + (ok ? 'dot-ok' : 'dot-off') }),
    h('span', { class: 's-label', text: label }), h('span', { class: 's-value' }, value, !ok && fix ? h('div', { class: 'help', style: 'margin-top:2px', text: fix }) : null));
  const status = h('div', { class: 'card' }, h('h3', { text: 'AI system status' }),
    h('p', { class: 'card-sub', text: local ? 'DocMind runs its AI models locally with Ollama.' : 'DocMind uses configured OpenAI-compatible AI endpoints. Configuration is checked here; endpoint connectivity is tested when you send a request.' }),
    row(local ? 'Ollama connection' : 'AI API configuration', !!(o && o.reachable), o && o.reachable ? (o.configured_only ? 'Configured' : 'Connected') : 'Not configured', local ? 'Start Ollama (run “ollama serve” or open the Ollama app).' : 'Set AI_BASE_URL and AI_API_KEY in the server environment.'),
    row('Chat model', !!(o && o.llm_installed), s.llm_model, local ? `Install it with: ollama pull ${s.llm_model}` : 'Check LLM_MODEL with your AI provider.'),
    row('Embedding model', !!(o && o.embedding_installed), s.embedding_model, local ? `Install it with: ollama pull ${s.embedding_model}` : 'Check EMBEDDING_MODEL, endpoint and vector dimensions.'),
    h('div', { style: 'margin-top:14px' }, h('button', { class: 'btn btn-sm', type: 'button', onclick: () => go('settings') }, icon('refresh'), 'Check again')));

  // Advanced (collapsed by default)
  const advanced = h('details', { class: 'card advanced' }, h('summary', {}, icon('chevron'), 'Advanced settings'),
    h('div', { class: 'adv-body' },
      h('div', { class: 'adv-group', style: 'margin-top:0;padding-top:0;border:0', text: 'Editable' }),
      slider('llm_temperature', 'Creativity (temperature)', 'Keep this low for faithful, document-based answers. Higher values make answers more varied.', 0, 1, 0.05),
      number('chunk_size', 'Chunk size', 'How many characters each document passage has. Applies to documents uploaded from now on.', 200, 4000, 'characters'),
      number('chunk_overlap', 'Chunk overlap', 'Characters shared between neighbouring passages so sentences are not cut off. Must be smaller than the chunk size.', 0, 1000, 'characters'),
      number('max_upload_size_mb', 'Maximum upload size', 'Largest PDF you can attach.', 1, 100, 'MB'),
      h('div', { class: 'adv-group' }, icon('info'), 'Read-only: change these in the .env file and restart'),
      h('dl', { class: 'kv' }, h('dt', { text: 'AI provider' }), h('dd', { text: s.ai_provider || 'ollama' }),
        h('dt', { text: 'AI API URL' }), h('dd', { text: s.ai_base_url || s.ollama_base_url }),
        h('dt', { text: 'Chat model' }), h('dd', { text: s.llm_model }), h('dt', { text: 'Embedding model' }), h('dd', { text: s.embedding_model }))));

  const save = h('button', { class: 'btn btn-accent', type: 'button', onclick: async () => {
    save.disabled = true;
    try {
      const saved = await patch('/settings', Object.fromEntries(controls.map((c) => [c.key, c.read()])));
      controls.forEach((c) => c.write(saved[c.key]));
      state.limits.maxUploadMb = saved.max_upload_size_mb;
      toast('Settings saved', 'success');
    } catch (e) { toast(e.message, 'error'); }
    save.disabled = false;
  } }, 'Save changes');

  container.appendChild(h('div', { class: 'page' }, h('h2', { text: 'Settings' }), h('p', { class: 'sub', text: 'Appearance, how the assistant answers, and the status of your local AI.' }),
    h('div', { class: 'settings-stack' }, appearance, response, status, advanced,
      h('div', { class: 'save-bar' }, save, h('span', { class: 'run-hint', text: 'Saves the answer settings and the advanced settings.' })))));
}
