const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(require('path').join(__dirname, '..', 'web', 'index.html'), 'utf8');
assert(html.includes('endThink(m.seq);'), 'historical thinking must bind to its answer');
assert(html.includes("d.type === 'message_regenerated'"), 'live regeneration event must be handled');

const start = html.indexOf('function applyMessageEditBySeq(');
const end = html.indexOf('function openMessageMenu(', start);
assert(start > 0 && end > start, 'message regeneration runtime functions not found');

const bubble = { dataset: { messageSeq: '22' }, textContent: '旧回答' };
const thinkingLine = {};
const thinking = { messageSeq: 22, text: '旧思考', paused: false, lineEl: thinkingLine };
const thinkingSheet = { classList: { contains: name => name === 'open' } };
const context = {
  document: { querySelectorAll: () => [bubble] },
  applyMessageEdit: (el, text) => { el.textContent = text; },
  thinkStore: [thinking],
  curThinkIdx: 0,
  sheets: { think: thinkingSheet },
  thinkBodyEl: { textContent: '' },
  thinkSpin: (buf, on) => { buf.spinning = on; },
};

vm.createContext(context);
vm.runInContext(html.slice(start, end), context, { filename: 'message-runtime.js' });
context.applyMessageRegenerationBySeq(22, '新回答', '新思考');

assert.equal(bubble.dataset.messageSeq, '22', 'answer sequence must stay in place');
assert.equal(bubble.textContent, '新回答');
assert.equal(thinking.messageSeq, 22, 'thinking must stay bound to the same answer');
assert.equal(thinking.text, '新思考');
assert.equal(thinking.paused, true);
assert.equal(thinking.spinning, false);
assert.equal(context.thinkBodyEl.textContent, '新思考');

const quoteStart = html.indexOf('function selectedMessageQuote(');
const quoteEnd = html.indexOf('function clearQuoteReply(', quoteStart);
assert(quoteStart > 0 && quoteEnd > quoteStart, 'selected quote helper not found');
const messageEl = { contains: node => node === textNode || node === messageEl };
const textNode = { nodeType: 3, parentNode: messageEl };
const selectedRange = {
  startContainer: textNode,
  endContainer: textNode,
  startOffset: 6,
  endOffset: 11,
  toString: () => 'world',
};
const selection = { rangeCount: 1, isCollapsed: false, getRangeAt: () => selectedRange };
const quoteContext = {
  window: { getSelection: () => selection },
  document: {
    createRange: () => ({
      selectNodeContents: () => {},
      setEnd: (_node, offset) => { quoteContext.prefixOffset = offset; },
      toString: () => 'hello world'.slice(0, quoteContext.prefixOffset),
    }),
  },
};
vm.createContext(quoteContext);
vm.runInContext(html.slice(quoteStart, quoteEnd), quoteContext, { filename: 'quote-runtime.js' });
assert.deepEqual(
  JSON.parse(JSON.stringify(quoteContext.selectedMessageQuote(messageEl))),
  { text: 'world', start_offset: 6, end_offset: 11 },
  'selected text must keep exact source offsets',
);

const mapStart = html.indexOf('const messageNodes = new Map();');
const mapEnd = html.indexOf('function findRowBySeq(', mapStart);
assert(mapStart > 0 && mapEnd > mapStart, 'stable message map helpers not found');
const mapContext = {};
vm.createContext(mapContext);
vm.runInContext(html.slice(mapStart, mapEnd), mapContext, { filename: 'message-map-runtime.js' });
const firstNode = { isConnected: true };
const duplicateNode = { isConnected: true };
mapContext.rememberMessageNode(44, firstNode);
mapContext.rememberMessageNode(44, duplicateNode);
assert.equal(mapContext.messageNode(44), firstNode, 'same seq must keep the first connected node');
firstNode.isConnected = false;
mapContext.rememberMessageNode(44, duplicateNode);
assert.equal(mapContext.messageNode(44), duplicateNode, 'a detached node may be replaced during catch-up');
mapContext.clearMessageNodes();
assert.equal(mapContext.messageNode(44), null);
assert(html.includes('const existing = findBubbleBySeq(seq);'), 'user echo/history must deduplicate by seq');
assert(html.includes("known.classList.contains('nook-evt')"), 'reading events must deduplicate by seq');
const timestampStart = html.indexOf('function formatNookTimestamp(');
const timestampEnd = html.indexOf('function addNookEvent(', timestampStart);
assert(timestampStart > 0 && timestampEnd > timestampStart, 'reading timestamp formatter not found');
const timestampContext = {};
vm.createContext(timestampContext);
vm.runInContext(html.slice(timestampStart, timestampEnd), timestampContext, { filename: 'nook-time-runtime.js' });
const formattedTime = timestampContext.formatNookTimestamp('2026-08-23T07:00:00+08:00', 0);
assert(/^2026-08-\d{2} \d{2}:\d{2}$/.test(formattedTime), 'reading ISO time must render as a compact local timestamp');
assert(!formattedTime.includes('T'), 'raw ISO timestamps must not leak into the UI');

const embeddingPayloadStart = html.indexOf('function embeddingFormPayload(');
const embeddingPayloadEnd = html.indexOf('function appendEmbeddingEditor(', embeddingPayloadStart);
assert(embeddingPayloadStart > 0 && embeddingPayloadEnd > embeddingPayloadStart, 'embedding relay payload helper not found');
const embeddingContext = {};
vm.createContext(embeddingContext);
vm.runInContext(html.slice(embeddingPayloadStart, embeddingPayloadEnd), embeddingContext, { filename: 'embedding-runtime.js' });
assert.deepEqual(
  JSON.parse(JSON.stringify(embeddingContext.embeddingFormPayload(' https://relay.example/v1 ', ' secret ', ' embed-model ', 'test'))),
  { action: 'test', base: 'https://relay.example/v1', token: 'secret', model: 'embed-model' },
  'embedding relay fields must be trimmed and keep the selected action',
);
assert(html.includes("fetch('api/embeddingconf'"), 'embedding relay must call the backend config endpoint');
assert(html.includes("token.type = 'password'"), 'embedding token input must be password-masked');
console.log('message answer/thinking in-place regeneration runtime passed');
