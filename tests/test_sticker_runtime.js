const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
assert(html.includes('id="stickerBtn"'), 'main composer needs a manual sticker button');
assert(html.includes("fetch('api/stickers' + suffix"), 'picker must search through the backend');
assert(html.includes("body: JSON.stringify({ sticker_id: stickerId })"), 'picker must send the real sticker id');

function element() {
  const classes = new Set();
  return {
    children: [],
    attributes: {},
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    appendChild(child) { this.children.push(child); return child; },
    blur() { this.blurred = true; },
    get innerHTML() { return this._html || ''; },
    set innerHTML(value) { this._html = value; this.children = []; },
  };
}

const stickerBtn = element();
const stickerPop = element();
const stickerGrid = element();
const stickerSearch = element();
stickerSearch.value = '';
const stickerClose = element();
const stickerForm = element();
const box = element();
const posts = [];

const context = {
  stickerBtn,
  stickerPop,
  stickerGrid,
  stickerSearch,
  box,
  guBusy: false,
  fitKeyboard() {},
  setBusy(value) { this.busy = value; },
  note(message) { throw new Error(message); },
  document: {
    getElementById(id) {
      return { stickerClose, stickerSearchForm: stickerForm }[id];
    },
    createElement() { return element(); },
  },
  async fetch(url, options = {}) {
    if (url === 'api/stickers') {
      return {
        ok: true,
        async json() {
          return { ok: true, stickers: [{
            id: 'st_hug_001', sticker_id: 'st_hug_001',
            url: 'https://example.test/storage/v1/object/public/stickers/hug.gif',
            semantic_intent: '给对方一个抱抱。',
          }] };
        },
      };
    }
    if (url === 'api/send') {
      posts.push(JSON.parse(options.body));
      return { ok: true, async json() { return { ok: true }; } };
    }
    throw new Error('unexpected fetch ' + url);
  },
  encodeURIComponent,
};

const start = html.indexOf('let stickerLoaded = false;');
const end = html.indexOf('let guBusy = false;', start);
assert(start > 0 && end > start, 'sticker picker runtime block not found');
vm.createContext(context);
vm.runInContext(html.slice(start, end), context, { filename: 'sticker-runtime.js' });

(async () => {
  context.stickerBtn.onclick();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert(context.stickerPop.classList.contains('open'), 'button must open the picker');
  assert.equal(context.stickerBtn.attributes['aria-expanded'], 'true');
  assert.equal(context.stickerGrid.children.length, 1, 'resolved sticker must render as one card');
  assert.equal(context.stickerGrid.children[0].children[0].src,
    'https://example.test/storage/v1/object/public/stickers/hug.gif');

  await context.stickerGrid.children[0].onclick();
  assert.deepEqual(posts, [{ sticker_id: 'st_hug_001' }]);
  assert(!context.stickerPop.classList.contains('open'), 'sending must close the picker');
  console.log('manual sticker picker runtime passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
