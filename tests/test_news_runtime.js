const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const start = html.indexOf('function appendNewsSource(');
const end = html.indexOf('function renderPaper(', start);
assert(start > 0 && end > start, 'news source renderer not found');

const document = {
  createTextNode: textContent => ({ nodeType: 3, textContent }),
  createElement: tagName => ({ tagName, children: [], appendChild(child) { this.children.push(child); } }),
};
const context = { document, URL };
vm.createContext(context);
vm.runInContext(html.slice(start, end), context, { filename: 'news-runtime.js' });

const linked = { children: [], appendChild(child) { this.children.push(child); } };
context.appendNewsSource(linked, '来源：[示例站](https://example.test/story?id=1)');
assert.equal(linked.children[0].textContent, '　来源：');
assert.equal(linked.children[1].tagName, 'a');
assert.equal(linked.children[1].textContent, '示例站');
assert.equal(linked.children[1].href, 'https://example.test/story?id=1');
assert.equal(linked.children[1].target, '_blank');
assert.equal(linked.children[1].rel, 'noopener noreferrer');

const multiple = { children: [], appendChild(child) { this.children.push(child); } };
context.appendNewsSource(
  multiple,
  '来源：[甲站](https://one.example/story)、[乙站](https://two.example/story)'
);
assert.equal(multiple.children.length, 4);
assert.equal(multiple.children[1].textContent, '甲站');
assert.equal(multiple.children[2].textContent, '、');
assert.equal(multiple.children[3].textContent, '乙站');

const unsafe = { children: [], appendChild(child) { this.children.push(child); } };
context.appendNewsSource(unsafe, '来源：[坏链接](javascript:alert(1))');
assert.equal(unsafe.children.length, 1);
assert.equal(unsafe.children[0].nodeType, 3);
assert.equal(unsafe.children[0].textContent, '　来源：[坏链接](javascript:alert(1))');

console.log('news source link runtime passed');
