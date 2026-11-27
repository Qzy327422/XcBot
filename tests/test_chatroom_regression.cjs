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

test('持久化 ID 优先：相同秒、正文、整轮思考的两个气泡只展开点击的那个', () => {
  const h = harness([{...shared, request_id: 'req-one', id: 'segment-one', content: '相同正文'}, {...shared, request_id: 'req-two', id: 'segment-two', content: '相同正文'}]);
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

test('旧会话无 ID：同秒的不同回合分段使用独立、确定的内容 key', () => {
  const messages = [{...shared, request_id: 'req-one', content: '旧分段一'}, {...shared, request_id: 'req-two', content: '旧分段二'}];
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
  const duplicate = {...shared, request_id: '', content: '完全相同的旧分段'};
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
    {...shared, request_id: 'req-two', id: 'answer-two', content: '第二段'},
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

function runOnclick(h, html, cls, idx = 0) {
  const escRe = new RegExp(`class="${cls}"[^>]*onclick="([^"]+)"`, 'g');
  const matches = [...html.matchAll(escRe)];
  assert.ok(matches[idx], `应存在第 ${idx} 个 ${cls}`);
  const code = matches[idx][1]
    .replaceAll('&#39;', "'")
    .replaceAll('&quot;', '"')
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&amp;', '&');
  h.run(code);
  return h.html();
}

test('split 分段合并为单轮消息：去重重复思考标签、只保留一个头像底栏与副本复制', () => {
  const h = harness([
    {role: 'user', id: 'u1', content: '你好'},
    {role: 'assistant', id: 's1', ts: 1700000100, request_id: 'split-req', reasoning: '唯一思考内容', duration_ms: 4200, content: '第一段'},
    {role: 'assistant', id: 's2', ts: 1700000100, request_id: 'split-req', reasoning: '唯一思考内容', content: '第二段'},
  ]);
  const html = h.html();
  assert.equal([...html.matchAll(/class="msg assistant turn-group"/g)].length, 1);
  assert.equal([...html.matchAll(/turn-avatar/g)].length, 0);
  assert.equal([...html.matchAll(/<div class="turn-head"><span class="turn-name">XcBot<\/span><\/div>/g)].length, 1);
  assert.equal(reasonButtons(html).length, 1);
  assert.equal([...html.matchAll(/<div class="bubble">/g)].length, 3);
  assert.match(html, /class="turn-copy-btn"/);
  assert.match(html, /耗时 4\.2 秒/);
  assert.equal(_containsEmoji(html), false);
});

function _containsEmoji(s) {
  return /(💭|🧑|⚠️|👁|🖼|📎)/.test(String(s || ''));
}

test('去 Emoji 校验：头像、附件、模型选项与错误提示全部采用 SVG/纯文本', () => {
  const h = harness([
    {role: 'user', id: 'u1', content: '测试'},
    {role: 'assistant', id: 'e1', error: true, content: '超时了'},
  ]);
  h.run(`chatState.models=[{model:'gpt-4o',supports_multimodal:true}];chatState.attachments['session-one']=[{name:'pic.png',type:'image/png'},{name:'doc.txt',type:'text/plain'}];`);
  const msgsHtml = h.html();
  const modelHtml = h.run('renderModelOptions()');
  const filesHtml = h.run('renderChatFiles()');
  for (const part of [msgsHtml, modelHtml, filesHtml]) {
    assert.equal(_containsEmoji(part), false);
  }
  assert.match(modelHtml, /· 识图/);
  assert.equal([...filesHtml.matchAll(/class="chat-file-ic"><svg /g)].length, 2);
  assert.match(msgsHtml, /class="turn-err-ic"[^>]*><svg /);
});

test('穿插 steps 支持一二级点击展开、HTML 转义及流式运行态展示', () => {
  const steps = [
    {type: 'thinking', text: '初步判断耗时链路', duration_ms: 800},
    {type: 'text', text: '我先查一下配置文件'},
    {type: 'thinking', text: '需要看代码与配置文件详情', duration_ms: 700},
    {type: 'tool', call_id: 'c1', round: 1, name: 'grep_files', arguments: '{"pattern":"foo<script>"}', status: 'ok', ok: true, duration_ms: 42, result: 'line1\nline2<img onerror=alert(1)>'},
    {type: 'tool', call_id: 'c2', round: 1, name: 'file_read', arguments: '{"path":"webui.py"}', status: 'error', ok: false, duration_ms: 18, result: 'Permission denied'},
  ];
  const h = harness([
    {role: 'user', id: 'u1', content: '为什么慢'},
    {role: 'assistant', id: 'a1', ts: 1700000200, request_id: 'r-steps', content: '最终结论', steps, duration_ms: 3600},
  ]);
  let html = h.html();
  assert.equal(reasonButtons(html).length, 1);
  assert.match(html, /class="bubble turn-prose".*我先查一下配置文件/);
  assert.match(html, /思考 · 搜索 1 次、读取 1 次/);
  assert.doesNotMatch(html, /含异常/);
  assert.equal([...html.matchAll(/class="act-list"/g)].length, 0);

  // 一级展开：显示第 2 轮思考与两个工具行，但不显示二级详情
  html = runOnclick(h, html, 'act-head', 0);
  assert.equal([...html.matchAll(/class="act-list"/g)].length, 1);
  assert.equal([...html.matchAll(/class="act-row /g)].length, 3);
  assert.equal([...html.matchAll(/class="act-detail"/g)].length, 0);

  // 二级展开：点击首个工具行（grep_files，即 act-row-head[1]），转义参数和结果
  html = runOnclick(h, html, 'act-row-head', 1);
  assert.equal([...html.matchAll(/class="act-detail"/g)].length, 1);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;img onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<script>|<img onerror/);

  // 再点一次一级头收起
  html = runOnclick(h, html, 'act-head', 0);
  assert.equal([...html.matchAll(/class="act-list"/g)].length, 0);

  // 流式运行态：正在执行工具时显示 spinner 和实时状态文案
  h.run(`chatState.requests['session-one']={partial:'',reasoning:'',steps:[{type:'thinking',text:'想一下',duration_ms:100},{type:'tool',call_id:'live1',round:1,name:'web_search',arguments:'{"query":"xcbot"}',status:'running',duration_ms:0,result:''}]};`);
  const liveHtml = h.html();
  assert.match(liveHtml, /class="act-group[^"]*is-live"/);
  assert.match(liveHtml, /chat-spin/);
  assert.match(liveHtml, /正在联网搜索 xcbot…/);

  // 首段正文到来后：纯思考态切换为“思考过程”，停止旋转指示器
  h.run(`chatState.requests['session-one']={partial:'开始输出正文',reasoning:'想完了',steps:[{type:'thinking',text:'想完了',duration_ms:1200}]};`);
  const answerLiveHtml = h.html();
  assert.match(answerLiveHtml, /思考过程/);
  assert.doesNotMatch(answerLiveHtml, /chat-spin/);
});

test('聊天室支持任意文件选择器、拖拽导入及用户消息精简附件芯片', () => {
  const h = harness([
    {
      role: 'user',
      id: 'u-att-1',
      user_text: '请总结这两个文件',
      content: '请总结这两个文件\n\n[文件：spec.docx]\n超长正文内容',
      attachments: [
        {name: 'spec.docx', type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', size: 2048, chars: 1200},
        {name: 'shot.png', type: 'image/png', size: 512},
      ],
    },
    {
      role: 'user',
      id: 'u-att-2',
      user_text: '',
      content: '[文件：only.py]\nprint(1)',
      attachments: [{name: 'only.py', type: 'text/plain', size: 10, chars: 8}],
    },
  ]);
  const roomHtml = h.run('renderChatroom()');
  const inputTag = roomHtml.match(/<input id="chatFileInput"[^>]*>/);
  assert.ok(inputTag, '应渲染文件选择器');
  assert.doesNotMatch(inputTag[0], /\baccept=/);
  assert.match(roomHtml, /ondrop="chatDrop\(event\)"/);
  assert.match(roomHtml, /id="chatDropMask"/);

  const msgsHtml = h.html();
  assert.equal([...msgsHtml.matchAll(/class="user-att-chip"/g)].length, 3);
  assert.match(msgsHtml, /spec\.docx/);
  assert.match(msgsHtml, /1\.2k 字/);
  assert.match(msgsHtml, /请总结这两个文件/);
  assert.doesNotMatch(msgsHtml, /超长正文内容/);
  assert.equal([...msgsHtml.matchAll(/class="bubble"/g)].length, 1);
});

test('流式思考气泡带滚动锚点，自动滚至底部且保留用户上滑位置', () => {
  const h = harness([{role: 'user', id: 'u1', content: 'hi'}]);
  h.run(`chatState.requests['session-one']={partial:'',reasoning:'思考片段',steps:[{type:'thinking',text:'思考片段',duration_ms:200}]};`);
  // 点开流式思考气泡
  let html = click(h, 0);
  assert.match(html, /class="reason-body" data-scroll-key="session-one:live_g0" data-live-scroll="1"/);

  // 模拟 DOM 节点：首次打开或原本在底部 -> 滚到 scrollHeight；用户手动上滑后 -> 保留原 scrollTop
  const result = h.run(`(() => {
    function makeNode(sh, st, ch, live) {
      return {
        scrollHeight: sh, scrollTop: st, clientHeight: ch,
        getAttribute: k => k === 'data-scroll-key' ? 'session-one:live_g0' : (k === 'data-live-scroll' ? (live ? '1' : null) : null)
      };
    }
    const bottomBefore = makeNode(500, 300, 200, true);
    const savedBottom = chatCaptureInnerScroll({querySelectorAll: () => [bottomBefore]});
    const bottomAfter = makeNode(820, 0, 200, true);
    chatRestoreInnerScroll({querySelectorAll: () => [bottomAfter]}, savedBottom);

    const scrolledUpBefore = makeNode(500, 40, 200, true);
    const savedUp = chatCaptureInnerScroll({querySelectorAll: () => [scrolledUpBefore]});
    const scrolledUpAfter = makeNode(820, 0, 200, true);
    chatRestoreInnerScroll({querySelectorAll: () => [scrolledUpAfter]}, savedUp);

    const freshOpened = makeNode(450, 0, 200, true);
    chatRestoreInnerScroll({querySelectorAll: () => [freshOpened]}, {});
    return [bottomAfter.scrollTop, scrolledUpAfter.scrollTop, freshOpened.scrollTop];
  })()`);
  assert.deepEqual(Array.from(result), [820, 40, 450]);
});

test('流式点开思考后触发工具或回复完成，均保留用户的展开状态不自动收起', () => {
  // 1) 单段思考展开 -> 工具加入同一轮，activity group 与内部思考行保持展开
  const h = harness([{role: 'user', id: 'u1', content: '查资料'}]);
  h.run(`chatState.requests['session-one']={partial:'',reasoning:'先想一下',steps:[{type:'thinking',text:'先想一下',duration_ms:300}]};`);
  let html = click(h, 0);
  assert.equal(openCount(html), 1);

  h.run(`chatState.requests['session-one'].steps.push({type:'tool',call_id:'t1',round:1,name:'web_search',arguments:'{"query":"test"}',status:'running',duration_ms:0,result:''});`);
  html = h.html();
  assert.match(html, /class="act-group open is-live"/);
  assert.match(html, /class="act-row open"/);
  assert.match(html, /data-scroll-key="session-one:live_g0_th_0"/);

  // 2) 工具链路完成后落盘（带 steps）：继承展开状态到持久化消息，再清理 live 临时键
  h.run(`
    chatState.session.messages.push({
      id: 'saved-tools',
      role: 'assistant',
      request_id: 'req-tools',
      content: '查完了',
      steps: [
        {type:'thinking',text:'先想一下',duration_ms:300},
        {type:'tool',call_id:'t1',round:1,name:'web_search',arguments:'{"query":"test"}',status:'ok',ok:true,duration_ms:50,result:'ok'}
      ]
    });
    delete chatState.requests['session-one'];
    chatCarryLiveFoldToPersisted('session-one', chatState.session);
    chatClearLiveFoldState('session-one');
  `);
  const savedHtml = h.html();
  assert.match(savedHtml, /class="act-group open /);
  assert.match(savedHtml, /class="act-row open"/);
  assert.equal(h.run(`chatActivityOpen('session-one', 'live_g0')`), false);

  // 3) 普通纯思考回复完成落盘（无 steps，仅 reasoning）：同样保留展开态
  const h2 = harness([{role: 'user', id: 'u2', content: '简单问题'}]);
  h2.run(`chatState.requests['session-one']={partial:'',reasoning:'纯思考内容',steps:[{type:'thinking',text:'纯思考内容',duration_ms:400}]};`);
  click(h2, 0);
  h2.run(`
    chatState.session.messages.push({
      id: 'saved-plain',
      role: 'assistant',
      request_id: 'req-plain',
      content: '直接答案',
      reasoning: '纯思考内容'
    });
    delete chatState.requests['session-one'];
    chatCarryLiveFoldToPersisted('session-one', chatState.session);
    chatClearLiveFoldState('session-one');
  `);
  assert.equal(openCount(h2.html()), 1);
  assert.equal(h2.run(`reasonIsOpen('session-one', 'live_g0')`), false);
});
