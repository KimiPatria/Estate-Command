import { renderAsk } from './ask.js';
import { getJSON } from '../lib/api.js';
import { esc } from '../lib/fmt.js';

/* ── the AI layer ───────────────────────────────────────────────────── */
/* Four rules hold this half of the page together, and they are all visible
   in the markup below rather than only in the server:

     1. Nothing generated is ever unlabelled. Every block of model-written
        text carries the model that wrote it.
     2. Every generated block carries its figure audit. The server checks each
        number in the text against the payload it was given; a failure shows
        as a red chip naming the figures, not as a footnote.
     3. Nothing generated is load-bearing. Each of these renders into a panel
        that already works without it, and a dead model leaves a one-line
        note where the text would have been.
     4. The manager can always see the evidence. The tool trace, the
        grounding payload and the measured status sit one click away. */

export const AI = {
  status: null,
  busy: false,
  examples: [],
  briefs: {},        // block_id -> brief payload
  interviews: {},    // capability id -> questions payload
  open: false,
  chats: [],         // {id, title, history: []}  history: most recent first
  activeChat: null,  // chat id
};

export const ASK_CHATS_KEY = 'askMapChats.v1';

export function activeChat() {
  return AI.chats.find(c => c.id === AI.activeChat) || null;
}

export function saveChats() {
  try {
    localStorage.setItem(ASK_CHATS_KEY,
      JSON.stringify({ chats: AI.chats, activeChat: AI.activeChat }));
  } catch (e) { /* private mode or storage disabled - chats stay in-memory */ }
}

export function loadChats() {
  try {
    const raw = localStorage.getItem(ASK_CHATS_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed && Array.isArray(parsed.chats) && parsed.chats.length) {
      AI.chats = parsed.chats.map(c => ({
        id: String(c.id || ('c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6))),
        title: typeof c.title === 'string' && c.title ? c.title : 'New chat',
        history: Array.isArray(c.history) ? c.history : [],
      }));
      AI.activeChat = AI.chats.some(c => c.id === parsed.activeChat)
        ? parsed.activeChat : AI.chats[0].id;
      return;
    }
  } catch (e) { /* corrupt or unavailable storage - start fresh below */ }
  newChat();
}

export function newChat() {
  const chat = {
    id: 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    title: 'New chat',
    history: [],
  };
  AI.chats.unshift(chat);
  AI.activeChat = chat.id;
  saveChats();
}

export function switchChat(id) {
  if (id === AI.activeChat) return;
  AI.activeChat = id;
  saveChats();
  renderAsk();
}

export function deleteChat(id) {
  const idx = AI.chats.findIndex(c => c.id === id);
  if (idx === -1) return;
  if (AI.chats[idx].history.length && !confirm('Delete this chat? This cannot be undone.')) return;
  AI.chats.splice(idx, 1);
  if (AI.activeChat === id) {
    if (!AI.chats.length) newChat();
    else AI.activeChat = AI.chats[0].id;
  }
  saveChats();
  renderAsk();
}


export function genBadge(model) {
  if (!model) return '';
  const short = String(model).replace(/^amazon\./, '').replace(/-v\d+:\d+$/, '')
    .replace(/nova-/, 'Nova ').replace(/^(\w)/, c => c.toUpperCase());
  return `<span class="gen"><span class="dot"></span>${esc(short)}</span>`;
}

export function auditChip(audit) {
  if (!audit || audit.checked === undefined) return '';
  if (audit.clean) {
    return `<span class="audit clean" title="Every figure in this text was found in the data behind it">
      ${audit.checked} figures checked</span>`;
  }
  return `<span class="audit dirty" title="These figures appear in no server-computed payload behind this text">
    unverified: ${audit.unverified.map(esc).join(', ')}</span>`;
}

export function aiFailure(payload, what) {
  return `<div class="empty">${esc(what)} unavailable.
    <span style="color:var(--muted)">${esc((payload && payload.reason) || 'The model did not respond.')}</span>
    <br>Everything else on this panel is computed on the server and is unaffected.</div>`;
}


export async function loadAiStatus() {
  try {
    AI.status = await getJSON('/gis/ai/status');
    const ex = await getJSON('/gis/ask/examples');
    AI.examples = ex.examples || [];
  } catch (e) {
    AI.status = null;   // the launcher still opens and explains itself
  }
}

