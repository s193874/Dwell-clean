const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(require('path').join(__dirname, '..', 'web', 'index.html'), 'utf8');
assert(html.includes("el.className = item.who === 'user' ? 'bubble' : 'gu'"), 'mini chat must reuse main chat bubbles');
assert(!html.includes('.nk-mini-msg.user'), 'black custom mini user bubble must be removed');
assert(html.includes('.nk-notebook .nk-note-title.on'), 'notebook directory needs selected state');
assert(html.includes('border-bottom: 2px solid'), 'notebook title rows need a thicker bottom edge');
assert(html.includes('padding: 12px; border-radius: 0;'), 'notebook title rows must not have rounded corners');
assert(html.includes('id="nookBgBtn"'), 'reading page needs a background color control');
assert(html.includes("localStorage.getItem(NK_BG_KEY)"), 'reading background must be browser-scoped');
assert(html.includes("setProperty('--nook-bg'"), 'reading background must stay scoped to the nook sheet');
assert(html.includes('id="nookBgDefault"'), 'reading background needs a reset control');
assert(html.includes('data-bg="#fffdf6"'), 'reading background needs preset colors');
assert(html.includes('data-bg="#eaf3e4"'), 'reading background needs a green preset');
assert(html.includes('id="nookBgCustom"'), 'reading background needs a custom color option');
const start = html.indexOf('async function openChapter(');
const end = html.indexOf('function openToc()', start);
assert(start > 0 && end > start, 'shared-reading runtime functions not found');
const source = html.slice(start, end);

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.values.has(name) : Boolean(force);
    if (on) this.values.add(name); else this.values.delete(name);
    return on;
  }
}

class FakeElement {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.style = {};
    this.classList = new FakeClassList();
    this.textContent = '';
    this.disabled = false;
    this.hidden = false;
    this.scrollTop = 0;
    this.value = '';
    this.attributes = {};
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() { this.focused = true; }
  set innerHTML(_value) { this.children = []; }
  get innerHTML() { return ''; }
}

const posts = [];
const chapters = {
  0: { book: '测试书', title: '第一章', pages: ['第一页', '第二页'], index: 0, total: 2, chapters: ['第一章', '第二章'] },
  1: { book: '测试书', title: '第二章', pages: ['第三页'], index: 1, total: 2, chapters: ['第一章', '第二章'] },
};
const context = {
  console,
  document: { createElement: tag => new FakeElement(tag) },
  nkSlug: '', nkCh: 0, nkPage: 0, nkPages: [], nkChapters: [], nkAnnos: [], nkMode: 2,
  nkBody: new FakeElement(), nkTocBtn: new FakeElement('button'), nkUploadBtn: new FakeElement('button'),
  nkTogether: new FakeElement('button'),
  nkNote: () => {}, nkHead: () => new FakeElement(), paraWithMarks: text => {
    const el = new FakeElement('p'); el.textContent = text; return el;
  },
  nkGet: async path => {
    if (path.startsWith('annotations/')) return [];
    const match = path.match(/^chapter\/sample\/(\d+)$/);
    if (match) return chapters[Number(match[1])];
    throw new Error('unexpected GET ' + path);
  },
  fetch: async (path, options) => {
    posts.push({ path, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ ok: true }) };
  },
  note0: message => { throw new Error(message); },
  setTimeout, clearTimeout,
};
vm.createContext(context);
vm.runInContext(source, context, { filename: 'nook-runtime.js' });

(async () => {
  await context.openChapter('sample', 0, 0);
  assert.equal(vm.runInContext('nkPage', context), 0);
  assert(posts.some(item => item.path === 'api/nook/presence' && item.body.page === 0));
  let nav = context.nkBody.children.at(-1);
  await nav.children[1].onclick();
  assert.equal(vm.runInContext('nkPage', context), 1);
  assert(posts.some(item => item.path === 'api/nook/presence' && item.body.page === 1));
  nav = context.nkBody.children.at(-1);
  await nav.children[1].onclick();
  assert.equal(vm.runInContext('nkCh', context), 1);
  assert.equal(vm.runInContext('nkPage', context), 0);
  assert(posts.some(item => item.path === 'api/nook/presence' && item.body.ch === 1 && item.body.page === 0));
  const progress = posts.filter(item => item.path === 'api/nook/progress').at(-1);
  assert.deepEqual(progress.body, { slug: 'sample', ch: 1, page: 0, mode: 2 });
  console.log('nook pagination and automatic page sharing runtime passed');

  const noteStart = html.indexOf('async function nkNotebookPost(');
  const noteEnd = html.indexOf('function selectNkMini(', noteStart);
  assert(noteStart > 0 && noteEnd > noteStart, 'notebook runtime functions not found');
  const notePane = new FakeElement();
  const notes = [{ id: 7, title: '人物关系', summary: '两个人第一次见面', body: '正文里的完整记录', pinned: 1 }];
  const noteContext = {
    console,
    document: {
      createElement: tag => new FakeElement(tag),
      getElementById: id => id === 'nkMiniNotebook' ? notePane : null,
    },
    nkSlug: 'sample',
    nkSelectedNote: '',
    nkGet: async path => {
      assert.equal(path, 'notebook/sample');
      return { ok: true, slug: 'sample', notes };
    },
    fetch: async () => ({ ok: true, json: async () => ({ ok: true, notes }) }),
    icEl: () => new FakeElement('span'),
    note0: message => { throw new Error(message); },
    confirm: () => true,
    encodeURIComponent,
  };
  vm.createContext(noteContext);
  vm.runInContext(html.slice(noteStart, noteEnd), noteContext, { filename: 'nook-notebook-runtime.js' });
  const allText = root => {
    const values = [root.textContent || ''];
    root.children.forEach(child => values.push(allText(child)));
    return values.join('\n');
  };
  await noteContext.loadNkNotebook();
  assert(allText(notePane).includes('人物关系'));
  assert(!allText(notePane).includes('两个人第一次见面'));
  assert(!allText(notePane).includes('正文里的完整记录'));
  assert(!allText(notePane).includes('记事标题'));
  const toolbar = notePane.children[0];
  toolbar.children[0].onclick();
  assert(allText(notePane).includes('新增记事'));
  await noteContext.loadNkNotebook();
  const directory = notePane.children[1];
  directory.children[0].onclick();
  assert(allText(notePane).includes('两个人第一次见面'));
  assert(allText(notePane).includes('正文里的完整记录'));
  await notePane.children[0].onclick();
  const selectedDirectory = notePane.children[1];
  assert(selectedDirectory.children[0].className.includes(' on'));
  console.log('notebook title directory, selected state, hidden create form, and detail runtime passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
