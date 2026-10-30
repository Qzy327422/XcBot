'use strict';
// 离线 VM 执行真实聊天室函数；不启动页面、不联网，也不替换待测渲染/切换逻辑。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

function section(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `找不到真实函数区段 ${start}`);
  return source.slice(a, b);
}

function harness(messages = [], sid = 'session-one') {
  const context = vm.createContext({
    localStorage: {},
    document: {getElementById: () => null},
    setTimeout, clearTimeout,
    fetch() { throw new Error('离线测试禁止网络访问'); },
  });
  const run = code => vm.runInContext(code, context);
  run(section('const esc=', '// 首次用 ?token='));
  run(section('const el=id=>', 'async function api('));
  run(section('let chatState=', 'let _storeList='));
  run(`let rendered='',renderCount=0;function render(){rendered=renderChatMsgs();renderCount++}`);
  const setSession = (items, sessionId = sid) => run(`chatState.current=${JSON.stringify(sessionId)};chatState.session=${JSON.stringify({id: sessionId, messages: items})}`);
  setSession(messages);
  return {run, setSession, html: () => run('renderChatMsgs()'),
    ids: () => JSON.parse(run('JSON.stringify(chatMessageReasonIds(chatState.session.messages))'))};
}

function reasonButtons(html) {
  return [...html.matchAll(/<button class="reason-head" onclick="([^"]+)">/g)].map(match => match[1]);
}

function click(h, index) {
  const handler = reasonButtons(h.html())[index];
  assert.ok(handler, `思考按钮 ${index} 必须存在`);
  h.run(handler.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
  return h.run('rendered');
}

function openCount(html) {
  return [...html.matchAll(/class="reason-block open"/g)].length;
}

const shared = {role: 'assistant', ts: 1700000000, request_id: 'same-request', reasoning: '同一份整轮思考'};

test('持久化 ID 优先：相同秒、请求号、正文、整轮思考的两个气泡只展开点击的那个', () => {
  const h = harness([{...shared, id: 'segment-one', content: '相同正文'}, {...shared, id: 'segment-two', content: '相同正文'}]);
  const ids = h.ids();
  assert.equal(new Set(ids).size, 2);
  assert.equal(h.run(`msgReasonId({id:'segment-one',content:'已改变'},999)`), ids[0]);
  assert.equal(openCount(h.html()), 0);
  let html = click(h, 0);
  assert.equal(openCount(html), 1);
  assert.equal([...html.matchAll(/class="reason-body"/g)].length, 1);
  assert.match(html, /同一份整轮思考/);
  assert.equal(h.run(`reasonIsOpen('session-one',${JSON.stringify(ids[1])})`), false);
  html = click(h, 1);
  assert.equal(openCount(html), 2);
  html = click(h, 0);
  assert.equal(openCount(html), 1);
  assert.equal(h.run('renderCount'), 3);
});

test('旧会话无 ID：同秒同请求的不同分段使用独立、确定的内容 key', () => {
  const messages = [{...shared, content: '旧分段一'}, {...shared, content: '旧分段二'}];
  const h = harness(messages), ids = h.ids();
  assert.equal(new Set(ids).size, 2);
  assert.equal(openCount(click(h, 1)), 1);
  assert.equal(h.run(`reasonIsOpen('session-one',${JSON.stringify(ids[0])})`), false);
  assert.deepEqual(harness(JSON.parse(JSON.stringify(messages))).ids(), ids);
  h.run('chatState.session=JSON.parse(JSON.stringify(chatState.session))');
  assert.deepEqual(h.ids(), ids);
  assert.equal(openCount(h.html()), 1);
});

test('旧会话没有 ts/request_id 也不退回全局下标，历史头部裁剪后 key 和展开状态不变', () => {
  const messages = [
    {role: 'user', content: '即将裁剪的提问'},
    {role: 'assistant', content: '第一段', reasoning: '整轮思考'},
    {role: 'assistant', content: '第二段', reasoning: '整轮思考'},
  ];
  const h = harness(messages), before = h.ids().slice(1);
  click(h, 1);
  h.setSession(messages.slice(1));
  assert.deepEqual(h.ids(), before);
  assert.equal(openCount(h.html()), 1);
  assert.equal(h.run(`reasonIsOpen('session-one',${JSON.stringify(before[1])})`), true);
  assert.deepEqual(harness(messages.slice(1)).ids(), before);
});

test('旧会话完全重复的分段仍唯一，从尾部计数使删除相同的头部消息后保留 key 不变', () => {
  const duplicate = {...shared, content: '完全相同的旧分段'};
  const messages = [{...duplicate}, {...duplicate}, {...duplicate}];
  const h = harness(messages), before = h.ids();
  assert.equal(new Set(before).size, 3);
  click(h, 1);
  h.setSession(messages.slice(1));
  assert.deepEqual(h.ids(), before.slice(1));
  assert.equal(openCount(h.html()), 1);
  assert.deepEqual(harness(messages.slice(1)).ids(), before.slice(1));
  h.setSession(messages.slice(2));
  assert.deepEqual(h.ids(), before.slice(2));
  assert.equal(openCount(h.html()), 0);
});

test('持久化 ID 在 JSON 重载、头部裁剪和其他 progress 消息插入后保持稳定', () => {
  const messages = [
    {role: 'user', id: 'user-one', content: '问题'},
    {...shared, id: 'progress-one', content: '工具前说明', reasoning: ''},
    {...shared, id: 'answer-one', content: '第一段'},
    {...shared, id: 'answer-two', content: '第二段'},
  ];
  const h = harness(messages), before = h.ids();
  click(h, 1);
  const trimmed = JSON.parse(JSON.stringify(messages.slice(2)));
  h.setSession(trimmed);
  assert.deepEqual(h.ids(), before.slice(2));
  assert.equal(openCount(h.html()), 1);
  h.setSession([{role: 'assistant', content: '另一个工具前说明'}, ...trimmed]);
  assert.deepEqual(h.ids().slice(1), before.slice(2));
  assert.equal(openCount(h.html()), 1);
  assert.deepEqual(harness(trimmed).ids(), before.slice(2));
});

test('旧分段 key 不受其他内容的 progress 插入和裁剪影响，推导不修改会话对象', () => {
  const messages = [{...shared, content: '甲'}, {...shared, content: '乙'}];
  const h = harness(messages), before = h.ids();
  const snapshot = h.run('JSON.stringify(chatState.session)');
  h.html();h.ids();
  assert.equal(h.run('JSON.stringify(chatState.session)'), snapshot);
  click(h, 0);
  h.setSession([{...shared, content: '工具前说明', reasoning: ''}, ...messages]);
  assert.deepEqual(h.ids().slice(1), before);
  assert.equal(openCount(h.html()), 1);
});

test('同一消息 ID 在不同会话中仍各自维护展开状态', () => {
  const messages = [{...shared, id: 'shared-message-id', content: '正文'}];
  const h = harness(messages);
  click(h, 0);
  h.setSession(messages, 'session-two');
  assert.equal(openCount(h.html()), 0);
  click(h, 0);
  h.setSession(messages);
  assert.equal(openCount(h.html()), 1);
});

test('真实 renderReasonBlock 保留整轮内容、转义 HTML，并与 live 思考状态隔离', () => {
  const messages = [{...shared, id: 'saved-answer', content: '正文', reasoning: '<think>整轮内容 & 后半段</think>'}];
  const h = harness(messages);
  h.run(`chatState.requests['session-one']={partial:'流式正文',reasoning:'流式思考'}`);
  assert.equal(reasonButtons(h.html()).length, 2);
  let html = click(h, 0);
  assert.equal(openCount(html), 1);
  assert.match(html, /&lt;think&gt;整轮内容 &amp; 后半段&lt;\/think&gt;/);
  assert.doesNotMatch(html, /<think>/);
  assert.match(html, /思考中/);
  html = click(h, 1);
  assert.equal(openCount(html), 2);
  assert.equal(h.run(`renderReasonBlock('session-one','empty','  ',false)`), '');
});

test('持久化 error 由真实 renderChatMsgs 渲染为红色错误泡，重载后仍显示且不混为普通思考', () => {
  const messages = [
    {id: 'user', role: 'user', content: '保留提问'},
    {...shared, id: 'partial', content: '安全半篇'},
    {...shared, id: 'error', error: true, content: '模型网络异常 <script>bad()</script>'},
  ];
  const h = harness(messages), html = h.html();
  assert.match(html, /保留提问/);
  assert.match(html, /安全半篇/);
  assert.match(html, /生成失败/);
  assert.match(html, /border-color:var\(--red,#f87171\);color:var\(--red,#f87171\)/);
  assert.match(html, /模型网络异常 &lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.equal(reasonButtons(html).length, 1);
  assert.equal(harness(JSON.parse(JSON.stringify(messages))).html(), html);
});

test('消息 ID 被编码为安全的内联参数，不让引号打断真实切换处理器', () => {
  const h = harness([{...shared, id: "unsafe'\"<>&", content: '正文'}]);
  assert.match(h.ids()[0], /^id_/);
  assert.doesNotMatch(h.ids()[0], /['"<>&]/);
  assert.equal(openCount(click(h, 0)), 1);
});
