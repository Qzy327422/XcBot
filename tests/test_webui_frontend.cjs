'use strict';
// Offline regression tests: execute the actual frontend function sections, without bootstrapping it.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../static/app.css'), 'utf8');
function section(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `Missing actual source section: ${start}`);
  return source.slice(a, b);
}
function harness() {
  const nodes = new Map();
  const context = vm.createContext({console, setTimeout, clearTimeout, URLSearchParams,
    localStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
    window: {matchMedia:()=>({matches:false})},
    document: {getElementById: id => nodes.get(id) || null},
    confirm: () => true, requestAnimationFrame: fn => fn(),
  });
  vm.runInContext(section('let state=', '// 首次用 ?token='), context);
  vm.runInContext(section('const el=id=>', 'async function api('), context);
  vm.runInContext(section('async function api(', 'const pages='), context);
  vm.runInContext(section('function providers()', 'let knowledgeDocs='), context);
  vm.runInContext(section('function setValue(', 'function syncCurrentPageFieldsFromDom('), context);
  vm.runInContext(section('async function saveAll(', 'function isEditingField('), context);
  vm.runInContext(section('let _storeList=null;', 'setTheme(localStorage.webuiTheme'), context);
  vm.runInContext(`
    const token=()=>'';
    let renders=0,drafts=0,messages=[];
    state.bundle={form_values:{'Others.llm_providers':[],'Others.llm_rotation':[]},config_json:{PluginConfigs:{}}};
    state.current='providers';
    render=()=>{renders++};
    saveDraft=()=>{drafts++};
    toast=(...args)=>messages.push(args);
    syncCurrentPageFieldsFromDom=()=>syncProviderFieldsFromDom();
    flushPendingBackground=async()=>{};traceSavePending=async()=>{};
  `, context);
  return {context, nodes, run: code => vm.runInContext(code, context),
    json: code => JSON.parse(vm.runInContext(`JSON.stringify(${code})`, context))};
}
function deferred() { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; }
function classes() { const items = new Set(); return {add: x => items.add(x), remove: x => items.delete(x), contains: x => items.has(x), toggle(x,on) {if(on)items.add(x);else items.delete(x);}}; }
function node(value='') { return {value, innerHTML:'', textContent:'', style:{}, classList:classes(), offsetWidth:100}; }
const tests=[];
function test(name,fn) {tests.push([name,fn]);}

test('debug update details prefer dependency diagnostics and fall back to status text', () => {
  const h=harness();
  vm.runInContext(section('function dbgKV(', 'function renderDebug()'),h.context);
  vm.runInContext(section('function renderDebug()', 'async function debugRestart('),h.context);
  h.run(`let dbgSelfResult=null;state.bundle.status={debug:{update_install:{state:'error',text:'更新失败',detail:'ERROR: simulated pip failure'}}};`);
  assert.ok(h.run('renderDebug()').includes('ERROR: simulated pip failure'));
  h.run(`state.bundle.status.debug.update_install.detail='';`);
  assert.ok(h.run('renderDebug()').includes('更新失败'));
});

test('feature cards stay hidden through fallback without deleting saved values; QQ gates explain WebUI recovery', () => {
  const h=harness();
  vm.runInContext(section('function renderFeatures()', 'function toggleFeature('),h.context);
  const hidden=['weather','check_account','image_generation','ai_chat','compression_commands','quote','plugin_admin_commands','split_reply_quote'];
  h.context.hidden=hidden;
  h.run(`state.bundle={feature_switches:Object.fromEntries(hidden.map(k=>[k,false])),form_values:{},ui_schema:[{key:'features',fields:[]}],feature_meta:[...hidden,'group_chat','private_chat','plugins_external','http_send_api','future'].map(key=>({key,title:key,desc:'old description',group:'fallback'}))};`);
  const before=h.json('state.bundle'),html=h.run('renderFeatures()');
  for(const key of hidden)assert.ok(!html.includes(key),key);
  for(const key of ['group_chat','private_chat','plugins_external','http_send_api','future'])assert.ok(html.includes(key));
  assert.match(html,/命令、插件、模型/);assert.match(html,/通过 WebUI 重新开启/);
  assert.deepEqual(h.json('state.bundle'),before);
  assert.doesNotMatch(css,/\.feature-options-grid\{[^}]*column-width/);
});

test('AI memory settings stay expanded and conversation cards use compact heights', () => {
  const h=harness();
  vm.runInContext(section('function renderAiSettings(', 'function settingsTabs('),h.context);
  h.run(`renderField=(f)=>'<label>'+f.path+'</label>';state.bundle.form_values={};`);
  const html=h.run(`renderForm({key:'ai',fields:[{path:'reply',category:'对话'},{path:'Others.auto_compress_after_messages',category:'上下文与记忆'},{path:'Others.compression_keep_recent',category:'上下文与记忆'},{path:'Others.context_max_messages',category:'上下文与记忆'}]})`);
  assert.ok(html.includes('对话设置'));assert.ok(html.includes('上下文与记忆'));
  assert.ok(!html.includes('<details'));assert.ok(!html.includes('高级设置'));
  assert.match(html,/form-grid compact-form ai-conversation-fields/);
  assert.match(css,/\.ai-conversation-fields textarea\{height:120px;min-height:84px;resize:vertical\}/);
  assert.ok(html.includes('复用对话模型及其轮换'));
  for(const key of ['auto_compress_after_messages','compression_keep_recent','context_max_messages'])assert.equal(html.split(key).length-1,1);
});

test('AI split group retains unknown schema fields and both switches independently write their original keys', () => {
  const h=harness();
  vm.runInContext(section('function renderAiSettings(', 'function settingsTabs('),h.context);
  vm.runInContext(section('function toggleFeature(', 'async function checkUpdate('),h.context);
  vm.runInContext(section('function renderField(', '// 密码类字段'),h.context);
  vm.runInContext(section('function setBool(', 'function shouldAutoRefreshPage('),h.context);
  h.run(`state.bundle.feature_switches={split_reply_quote:false,ai_chat:false};state.bundle.form_values={'Others.llm_split.enabled':false};`);
  const fields=[
    {path:'Others.llm_reply_failover_keywords',type:'list',category:'上下文与过滤'},
    ...['enabled','mode','prompt_suffix','split_regex','filter_regex','max_chars_no_split','future_option'].map((k,i)=>({path:'Others.llm_split.'+k,type:i===0?'bool':i>1&&i<5?'textarea':'text',category:'future category'})),
    {path:'future_conversation',type:'text',category:'unknown'},
    {path:'future_split',type:'text',category:'分段回复'},
    {path:'Others.context_max_messages',type:'number',category:'上下文与记忆'},
  ];
  h.context.fields=fields;
  const html=h.run('renderAiSettings({fields})'),split=html.slice(html.indexOf('id="section_ai_split"'),html.indexOf('id="section_ai_memory"'));
  assert.ok(!html.includes('ai-filter-fields'));assert.ok(!html.includes('ai-split-options'));
  assert.match(css,/repeat\(auto-fill,minmax\(280px,1fr\)\)/);
  assert.equal((html.match(/toggleFeature\('split_reply_quote'/g)||[]).length,1);
  assert.ok(html.includes('回复时引用'));
  assert.ok(!html.includes('分段首段引用'));
  assert.ok(html.indexOf("toggleFeature('split_reply_quote'")<html.indexOf('id="section_ai_split"'));
  assert.ok(!split.includes("toggleFeature('split_reply_quote'"));
  for(const f of fields)assert.equal((html.match(new RegExp('id="f_'+f.path.replace(/[^a-zA-Z0-9]/g,'_')+'"','g'))||[]).length,f.type==='bool'?0:1,f.path);
  for(const f of fields.filter(f=>f.path.startsWith('Others.llm_split.')||f.category==='分段回复'))assert.ok(split.includes(f.path),f.path);
  assert.ok(!html.includes('ai_chat'));
  h.context.quoteSwitch=node();h.context.llmSwitch=node();
  h.run(`toggleFeature('split_reply_quote',quoteSwitch)`);
  assert.equal(h.run('state.bundle.feature_switches.split_reply_quote'),true);
  assert.equal(h.run(`state.bundle.form_values['Others.llm_split.enabled']`),false);
  h.run(`setBool('Others.llm_split.enabled',llmSwitch)`);
  assert.equal(h.run(`state.bundle.form_values['Others.llm_split.enabled']`),true);
  assert.equal(h.run('state.bundle.feature_switches.split_reply_quote'),true);
  h.run(`toggleFeature('split_reply_quote',quoteSwitch)`);
  assert.equal(h.run('state.bundle.feature_switches.split_reply_quote'),false);
  assert.equal(h.run(`state.bundle.form_values['Others.llm_split.enabled']`),true);
});

test('proactive page removes legacy_account_id and max_account_background_calls_per_day completely', () => {
  const h=harness();
  vm.runInContext(section('function settingsTabs(', 'function switchSettingsTab('),h.context);
  h.run(`renderField=(f,v)=>'<input data-path="'+f.path+'" value="'+(v||'')+'">';pages=()=>state.bundle.ui_schema;state.bundle.ui_schema=[{key:'proactive',fields:[{path:'ProactiveReply.group.enabled'},{path:'ProactiveReply.group.observation.enabled'},{path:'ProactiveReply.group.observation.trigger_probability'},{path:'ProactiveReply.group.observation.default_wait_seconds'},{path:'ProactiveReply.group.observation.min_wait_seconds'},{path:'ProactiveReply.group.observation.max_wait_seconds'},{path:'ProactiveReply.group.observation.debounce_seconds'},{path:'ProactiveReply.group.observation.max_debounce_seconds'},{path:'ProactiveReply.group.observation.min_continuation_interval_seconds'},{path:'ProactiveReply.private.enabled'}]}];state.bundle.form_values={'ProactiveReply.legacy_account_id':'999','ProactiveReply.group.observation.enabled':true};`);
  const html=h.run(`renderOrganizedSettings({key:'proactive',title:'主动回复',fields:state.bundle.ui_schema[0].fields})`);
  assert.equal((html.match(/data-path="ProactiveReply\.legacy_account_id"/g)||[]).length,0);
  assert.doesNotMatch(html,/proactive-legacy-box|旧会话所属机器人/);
  assert.equal((html.match(/data-path="ProactiveReply\.max_account_background_calls_per_day"/g)||[]).length,0);
  const advIdx=html.indexOf('<details class="proactive-obs-advanced">');
  assert.ok(advIdx>0);
  assert.ok(html.indexOf('ProactiveReply.group.observation.enabled')<advIdx);
  assert.ok(html.indexOf('ProactiveReply.group.observation.trigger_probability')<advIdx);
  assert.ok(html.indexOf('ProactiveReply.group.observation.default_wait_seconds')>advIdx);
});

test('plugin entry animates its actual root without restarting cards on panel replacement', () => {
  assert.match(css,/\.page-anim>\.plugin-root\{animation:glass-card-in \.58s \.04s/);
  assert.match(css,/html\.liquid-glass \.page-anim>\.plugin-root\{animation:glass-card-in \.82s \.05s/);
  assert.match(css,/@media\(prefers-reduced-motion:reduce\)\{\.page-anim>\.plugin-root[^}]*animation:none !important/);
  for(const bounds of [['  function onMotion(','  const motionEvents='],['  function update(){','  function imageFailed(']])assert.match(section(...bounds),/\.plugin-root,\.plugin-panel,\.plugin-config-cards/);
  assert.doesNotMatch(section('function refreshPluginPanel(', 'function renderStore('),/render\(|animateSubtab/);
});

test('three independent model types: manual/detected add, deduplicate, update and delete', () => {
  const h=harness();h.run(`addProvider();updateProvider(0,'id','one');`);
  h.nodes.set('modelInputValue',node('shared'));
  for(const [kind,key,remove,update] of [
    ['chat','models','removeProviderModel','updateProviderModel'],
    ['planner','planner_models','removePlannerModel','updatePlannerModel'],
    ['embedding','embedding_models','removeEmbeddingModel','updateEmbeddingModel'],
  ]) {
    h.run(`modelInputState={providerIndex:0,kind:'${kind}'};submitModelInput();modelInputState={providerIndex:0,kind:'${kind}'};submitModelInput();`);
    assert.equal(h.run(`providers()[0].${key}.length`),1);
    h.run(`${update}(0,0,'enabled',false)`);
    assert.equal(h.run(`providers()[0].${key}[0].enabled`),false);
    if(kind!=='embedding') {h.run(`${update}(0,0,'timeout_seconds',27)`);assert.equal(h.run(`providers()[0].${key}[0].timeout_seconds`),27);}
    h.run(`state.providerSubTab={0:'${kind}'};updateProvider(0,'detected_models',['shared','detected']);`);
    h.nodes.set('detected_select_0',node('detected'));
    h.run('addDetectedModel(0);addDetectedModel(0)');
    assert.equal(h.run(`providers()[0].${key}.length`),2);
    h.run(`${remove}(0,0);${remove}(0,0)`);
    assert.equal(h.run(`providers()[0].${key}.length`),0);
  }
  h.run(`for(const kind of ['chat','planner','embedding']){state.providerSubTab={0:kind};addDetectedModel(0)}`);
  for(const key of ['models','planner_models','embedding_models'])assert.equal(h.run(`providers()[0].${key}[0].name`),'detected');
});

test('provider no-op DOM synchronization and unrelated edits never rebuild rotation', () => {
  const h=harness();h.run(`addProvider();updateProvider(0,'id','one');state.dirty=false;state.lastInputAt=0;const savedRotation=rotation();`);
  for(const [id,value] of [['id','one'],['base',''],['proxy',''],['keys','']])h.nodes.set(`provider_${id}_0`,node(value));
  h.run('syncProviderFieldsFromDom()');assert.equal(h.run('state.dirty'),false);
  h.run(`updateProvider(0,'http_proxy','http://proxy');updateProvider(0,'detected_models',['m']);`);
  assert.equal(h.run('rotation()===savedRotation'),true);
});

test('API Keys generated inline handler is valid and splits CRLF/LF', () => {
  const h=harness();h.run('addProvider()');
  const html=h.run('renderProviderDetail(providers()[0],0)');
  const handler=html.match(/<textarea id="provider_keys_0"[^>]*oninput="([^"]*)"/)[1];
  const keys=[];vm.runInNewContext(`(function(){${handler}}).call({value:'first\\r\\n second\\n\\n'})`,{updateProvider:(i,k,v)=>keys.push(...v)});
  assert.deepEqual(keys,['first','second']);
});

test('search, selection, duplicate filtering and disabled zero results are tab-local', () => {
  const h=harness();h.run(`addProvider();updateProvider(0,'detected_models',['alpha','beta','beta']);updateProvider(0,'models',[{name:'alpha',enabled:true}]);`);
  const select=node('beta'),button=node(),count=node();
  h.nodes.set('detected_select_0',select);h.nodes.set('detected_add_0',button);h.nodes.set('detected_count_0',count);
  h.run(`filterDetectedModels(0,'be')`);assert.equal(select.value,'beta');assert.equal(button.disabled,false);
  h.run(`filterDetectedModels(0,'missing')`);assert.equal(select.disabled,true);assert.equal(button.disabled,true);assert.match(count.textContent,/^0 /);
  assert.match(h.run('renderDetectedModels(providers()[0],0)'),/id="detected_add_0"[^>]*disabled/);
  h.run(`state.providerSubTab={0:'planner'}`);
  assert.deepEqual(h.json(`detectedOptions(providers()[0],'planner')`),['alpha','beta']);
  h.run(`filterDetectedModels(0,'be');rememberDetectedSelection(0)`);
  h.run(`state.providerSubTab={0:'embedding'}`);assert.match(h.run('renderDetectedModels(providers()[0],0)'),/value="alpha" selected/);
  h.run(`state.providerSubTab={0:'planner'}`);assert.match(h.run('renderDetectedModels(providers()[0],0)'),/value="beta" selected/);
});

test('schema API is unwrapped once, defaults only fill missing fields and input writes one draft field', async () => {
  const h=harness();let calls=0;
  h.context.fetch=async()=>{calls++;return {status:200,json:async()=>({ok:true,data:{demo:{schema:{text:{type:'string',default:'fallback'},flag:{type:'bool',default:true},count:{type:'int',default:2},list:{type:'list'}},values:{text:'server-old',list:['one']}}}})}};
  h.run(`state.bundle.form_values['PluginConfigs.demo.text']='draft';`);
  const a=h.run('loadPluginSchemas()'),b=h.run('loadPluginSchemas()');assert.equal(a,b);await a;
  assert.equal(calls,1);assert.equal(h.run(`state.bundle.form_values['PluginConfigs.demo.text']`),'draft');
  assert.equal(h.run(`state.bundle.form_values['PluginConfigs.demo.flag']`),true);
  h.run(`_pluginCfgSyncInput('demo','count',{value:'8'});pluginCfgToggle('demo','flag',{classList:{toggle(){}}});`);
  assert.equal(h.run(`state.bundle.form_values['PluginConfigs.demo.count']`),8);
  assert.equal(h.run(`state.bundle.form_values['PluginConfigs.demo.flag']`),false);
  assert.equal(h.run(`_pluginSchemas.demo.values.count`),undefined);
  await h.run('loadPluginSchemas()');assert.equal(h.run(`state.bundle.form_values['PluginConfigs.demo.count']`),8);
  assert.match(h.run('renderPluginConfig()'),/value="draft"/);
  h.run(`_pluginCfgSyncInput('demo','list',{value:' a\\r\\n b\\n'})`);
  assert.deepEqual(h.json(`state.bundle.form_values['PluginConfigs.demo.list']`),['a','b']);
  assert.equal(h.run('state.dirty'),true);assert.ok(h.run('drafts')>0);
});

test('plugin details default closed; independent session state survives replacements without dirtying drafts', () => {
  const h=harness();h.run(`_pluginSchemas={one:{schema:{text:{type:'string'}},values:{}},two:{schema:{flag:{type:'bool'}},values:{}},plain:{schema:{}}};`);
  let html=h.run('renderPluginConfig()');
  assert.equal((html.match(/<details class="card plugin-config-card"/g)||[]).length,2);
  assert.doesNotMatch(html,/<details[^>]* open/);
  assert.match(html,/<summary class="plugin-config-summary">/);
  assert.match(html,/<div class="card">[^]*plain[^]*该插件未提供配置项/);
  const one={isConnected:true,open:true,dataset:{pluginName:'one'}},two={isConnected:true,open:false,dataset:{pluginName:'two'}};
  h.context.one=one;h.context.two=two;
  const panel=node();panel.querySelectorAll=()=>[one,two];h.nodes.set('pluginPanel',panel);
  // Snapshot must run synchronously, before the native queued toggle has fired.
  h.run(`state.current='store';refreshPluginPanel('config')`);
  assert.match(panel.innerHTML,/<details[^>]*data-plugin-name="one"[^>]* open>/);
  assert.doesNotMatch(panel.innerHTML,/<details[^>]*data-plugin-name="two"[^>]* open>/);
  one.isConnected=false;one.open=false;h.run('rememberPluginConfigOpen(one)');
  assert.equal(h.run(`pluginConfigOpen.has('one')`),true);
  panel.querySelectorAll=()=>[];h.run(`_storeList=[];switchPluginTab('store');switchPluginTab('config')`);
  assert.match(panel.innerHTML,/<details[^>]*data-plugin-name="one"[^>]* open>/);
  assert.equal(h.run('state.dirty'),false);assert.equal(h.run('drafts'),0);
  h.run(`_pluginCfgSyncInput('one','text',{value:'unsaved'});rememberPluginConfigOpen(two)`);
  assert.equal(h.run(`state.bundle.form_values['PluginConfigs.one.text']`),'unsaved');
  assert.match(h.run('renderPluginConfig()'),/value="unsaved"/);
  assert.match(section('function render(){','function renderWelcome('),/snapshotPluginConfigOpen\(\)/);
  assert.match(css,/\.plugin-config-summary:focus-visible/);
  assert.match(css,/\.plugin-panel,\.plugin-config-cards\{[^}]*gap:14px/);
});

test('折射恢复备份的 GPU 位移和 CSS/SVG 强度，不使用24px限幅', () => {
  const context=vm.createContext({Math,glassRefraction:()=>0});
  vm.runInContext(section('function applyFilterScale(', 'function makeSurface('),context);
  for(const [strength,base] of [[0,0],[1,8],[5,40],[10,80],[30,120]]){
    context.glassRefraction=()=>strength;
    for(const soft of [false,true]){
      const values=[],filter={querySelectorAll:()=>[0,1,2].map(i=>({setAttribute:(key,value)=>{values[i]=Number(value)}}))};
      context.applyFilterScale(filter,soft);
      assert.deepEqual(values,[0,1,2].map(i=>soft?Math.max(1,base*.3-i*base*.025):base*(i===0?1.1:i===2?.9:1)));
    }
  }
  assert.match(source,/vec2 bend=local\*\(k-1\.0\)\*u_strength;/);
  assert.match(source,/const BASE_SCALE=Math.min\(120,\(refraction<=10\?refraction:10\+Math.sqrt\(refraction-10\)\*3\)\*8\)/);
  assert.doesNotMatch(source,/glassDisplacementScale|short-side-sdf-cap24|float shortSide=/);
});

test('GPU resize rescans targets only when crossing the mobile drawer breakpoint', () => {
  let narrow=true,calls=0;
  const context=vm.createContext({narrowViewport:()=>narrow,schedule:()=>calls++});
  vm.runInContext(`let targetsDirty=false;`+section('let lastNarrow=narrowViewport();','function onScroll('),context);
  context.reset=()=>vm.runInContext('targetsDirty=false',context);
  vm.runInContext('onResize()',context);assert.equal(vm.runInContext('targetsDirty',context),false);
  narrow=false;vm.runInContext('onResize()',context);assert.equal(vm.runInContext('targetsDirty',context),true);
  context.reset();vm.runInContext('onResize()',context);assert.equal(vm.runInContext('targetsDirty',context),false);
  narrow=true;vm.runInContext('onResize()',context);assert.equal(vm.runInContext('targetsDirty',context),true);
  assert.equal(calls,4);
});

test('renderer arbitration keeps wallpaper compatibility and uses explicit lightweight theme fallback', () => {
  let webkit=false,gpu=true;
  const context=vm.createContext({URL,navigator:{connection:{}},location:{href:'https://offline.test/',origin:'https://offline.test'},
    liquidGlassWebKitTarget:()=>webkit,liquidGlassWebGLProbe:()=>gpu});
  vm.runInContext(section('function liquidGlassRenderPlan(', 'const liquidGlassLensController='),context);
  const plan=(enabled,url)=>JSON.parse(vm.runInContext(`JSON.stringify(liquidGlassRenderPlan(${enabled},${JSON.stringify(url)},true))`,context));
  assert.equal(plan(false,'').engine,'off');assert.equal(plan(true,'').engine,'gpu');
  gpu=false;assert.equal(plan(true,'').engine,'lightweight');assert.equal(plan(true,'/wall.png').engine,'css');
  webkit=true;assert.equal(plan(true,'').engine,'lightweight');assert.equal(plan(true,'/wall.png').engine,'svg');
  gpu=true;assert.equal(plan(true,'https://remote.test/a.png').engine,'svg');
  context.navigator.connection.saveData=true;assert.equal(plan(true,'').engine,'lightweight');
  assert.match(css,/html\.theme-glass-fallback[^]*:is\(\.field,\.feature,\.stat,\.btn,\.pill\)\{backdrop-filter:none/);
});

test('plugin schema escaping uses datasets, including hostile names and numeric attributes', () => {
  const h=harness();h.run(`const hostile='x\\\'\" onfocus=\"bad()';_pluginSchemas={[hostile]:{schema:{[hostile]:{type:'number',min:hostile,max:hostile,step:hostile}},values:{}}};`);
  const html=h.run('renderPluginConfig()');
  assert.match(html,/data-name="x&#39;&quot; onfocus=&quot;bad\(\)"/);
  assert.match(html,/min="x&#39;&quot; onfocus=&quot;bad\(\)"/);
  assert.match(html,/oninput="_pluginCfgSyncInput\(this.dataset.name,this.dataset.key,this\)"/);
  assert.doesNotMatch(html,/oninput="[^\"]*bad\(/);
  assert.doesNotMatch(html,/\sonfocus="bad/);
});

test('tab entry loads only its resource, deduplicates requests and retries failures', async () => {
  const h=harness();let calls=[];const wait=deferred();
  h.context.fetch=async url=>{calls.push(url);return {status:200,json:async()=>({ok:true,data:url.endsWith('schemas')?{}:await wait.promise})}};
  h.run(`state.current='store';ensurePluginTabLoaded();ensurePluginTabLoaded();renderStore();`);
  await h.run('_pluginSchemasFlight');assert.deepEqual(calls,['/api/plugins/schemas']);
  h.nodes.set('pluginPanel',node());h.run(`switchPluginTab('store');ensurePluginTabLoaded();renderStore();`);
  const a=h.run('loadStore()'),b=h.run('loadStore()');assert.equal(a,b);
  wait.resolve([]);await a;assert.equal(calls.filter(x=>x.endsWith('/store')).length,1);
  h.run(`switchPluginTab('config');switchPluginTab('store');`);assert.equal(calls.length,2);
  h.context.fetch=async()=>{throw Error('offline')};await h.run('loadStore()');
  assert.match(h.run('renderPluginStore()'),/重试/);
  h.context.fetch=async()=>({status:200,json:async()=>({ok:true,data:[]})});await h.run('loadStore()');assert.equal(h.run('_storeList.length'),0);
  h.context.fetch=async()=>{throw Error('offline')};await h.run('loadPluginSchemas()');assert.match(h.run('renderPluginConfig()'),/重试/);
  h.context.fetch=async()=>({status:200,json:async()=>({ok:true,data:{}})});await h.run('loadPluginSchemas()');assert.equal(h.run('Object.keys(_pluginSchemas).length'),0);
  assert.doesNotMatch(section('function gotoPage(', 'function leaveCancel('), /loadStore\(/);
});

test('detection merges latest edits, prevents reentry, never restores removed models/providers', async () => {
  const h=harness(),request=deferred();let calls=0;
  h.context.api=()=>{calls++;return request.promise;};
  h.run(`addProvider();updateProvider(0,'models',[{name:'old',enabled:true,context_window:0}]);`);
  const pending=h.run('detectProviderModels(0)');await h.run('detectProviderModels(0)');assert.equal(calls,1);
  h.run(`updateProviderModel(0,0,'name','edited');updateProviderModel(0,0,'timeout_seconds',18);updateProvider(0,'planner_models',[{name:'plan',enabled:true}]);`);
  request.resolve({models:['found'],context_windows:{edited:9000}});await pending;
  assert.equal(h.run('providers()[0].models[0].name'),'edited');assert.equal(h.run('providers()[0].models[0].timeout_seconds'),18);
  assert.equal(h.run('providers()[0].models[0].context_window'),0);assert.equal(h.run('providers()[0].planner_models[0].name'),'plan');
  assert.deepEqual(h.json('providers()[0].detected_models'),['found']);assert.equal(h.run('providerDetections.size'),0);
  const later=deferred();h.context.api=()=>later.promise;const p=h.run('detectProviderModels(0)');
  h.run('removeProviderModel(0,0);removeProvider(0);addProvider()');later.resolve({models:['stale']});await p;
  assert.deepEqual(h.json('providers()[0].detected_models'),[]);
});

test('changed connection parameters discard old results, including change-and-revert', async () => {
  for(const field of ['id','base_url','keys','http_proxy']) {
    const h=harness(),request=deferred();h.context.api=()=>request.promise;
    h.run('addProvider()');const p=h.run('detectProviderModels(0)');
    h.run(`const original=providers()[0]['${field}'];updateProvider(0,'${field}',${field==='keys'?"['different']":"'different'"});updateProvider(0,'${field}',original);`);
    request.resolve({models:['stale']});await p;assert.deepEqual(h.json('providers()[0].detected_models'),[],field);
  }
});

test('detection survives concurrent save normalization without resurrecting old values', async () => {
  const h=harness(),detect=deferred(),save=deferred();
  h.run(`addProvider();updateProvider(0,'models',[{name:'m',enabled:true,timeout_seconds:21}]);`);
  h.context.api=(url)=>url.includes('detect-models')?detect.promise:save.promise;
  const dp=h.run('detectProviderModels(0)'),sp=h.run('saveAll(true)');
  // Allow saveAll to pass its background-flush await and capture the submitted body.
  await Promise.resolve();await Promise.resolve();
  const server=h.json('state.bundle');server.form_values['Others.llm_providers'][0].models[0].timeout_seconds=22;
  save.resolve(server);await sp;
  h.run(`updateProviderModel(0,0,'timeout_seconds',31)`);
  detect.resolve({models:['after-save'],context_windows:{m:1000}});await dp;
  assert.equal(h.run('providers()[0].models[0].timeout_seconds'),31);
  assert.deepEqual(h.json('providers()[0].detected_models'),['after-save']);
});

test('detection follows provider identity across index shifts and failure can retry', async () => {
  const h=harness(),wait=deferred();h.context.api=()=>wait.promise;
  h.run(`addProvider();addProvider();updateProvider(1,'id','target')`);
  const pending=h.run('detectProviderModels(1)');h.run('removeProvider(0)');
  wait.resolve({models:['target-model']});await pending;
  assert.equal(h.run('providers()[0].id'),'target');assert.deepEqual(h.json('providers()[0].detected_models'),['target-model']);
  h.context.api=async()=>{throw Error('offline')};await h.run('detectProviderModels(0)');
  assert.equal(h.run('providerDetections.size'),0);assert.equal(h.run('providers()[0].detect_error'),'offline');
  h.context.api=async()=>({models:['retry-ok']});await h.run('detectProviderModels(0)');
  assert.deepEqual(h.json('providers()[0].detected_models'),['retry-ok']);
});

test('detection completing before save response retains its results and newer model edits', async () => {
  const h=harness(),detect=deferred(),save=deferred();
  h.run(`addProvider();updateProvider(0,'models',[{name:'m',enabled:true}]);`);
  const server=h.json('state.bundle');h.context.api=url=>url.includes('detect-models')?detect.promise:save.promise;
  const dp=h.run('detectProviderModels(0)'),sp=h.run('saveAll(true)');await Promise.resolve();await Promise.resolve();
  h.run(`updateProviderModel(0,0,'name','edited-during-save')`);
  detect.resolve({models:['detected-during-save']});await dp;save.resolve(server);await sp;
  assert.equal(h.run('providers()[0].models[0].name'),'edited-during-save');
  assert.deepEqual(h.json('providers()[0].detected_models'),['detected-during-save']);assert.equal(h.run('state.dirty'),true);
});

test('save reads latest server plugin values and retains edits made while awaiting save', async () => {
  const h=harness();h.run(`_pluginSchemas={demo:{schema:{text:{type:'string'}},values:{text:'stale'}}};state.bundle.form_values['PluginConfigs.demo.text']='submitted';`);
  h.context.api=async()=>({form_values:{'Others.llm_providers':[]},config_json:{PluginConfigs:{demo:{text:'normalized'}}}});
  await h.run('saveAll(true)');assert.equal(h.run(`state.bundle.form_values['PluginConfigs.demo.text']`),'normalized');
  assert.match(h.run('renderPluginConfig()'),/value="normalized"/);
  const wait=deferred();h.context.api=()=>wait.promise;
  const saving=h.run('saveAll(true)');await Promise.resolve();await Promise.resolve();
  h.run(`_pluginCfgSyncInput('demo','text',{value:'new draft'})`);
  wait.resolve({form_values:{'Others.llm_providers':[]},config_json:{PluginConfigs:{demo:{text:'normalized'}}}});await saving;
  assert.equal(h.run(`state.bundle.form_values['PluginConfigs.demo.text']`),'new draft');assert.equal(h.run('state.dirty'),true);
});

test('feature and tool edits made during save remain pending without losing server-normalized peers', async () => {
  const h=harness(),wait=deferred();
  h.run(`state.bundle.feature_switches={ai_chat:true,summary:true};state.bundle.agent_tools={send_message:{enabled:true,level:'user'},read:{enabled:true,level:'admin'}};`);
  const server=h.json('state.bundle');
  server.feature_switches.summary=false;
  let posted;
  h.context.api=(url,opt)=>{posted=JSON.parse(opt.body);return wait.promise};
  const pending=h.run('saveAll(true)');await Promise.resolve();await Promise.resolve();
  h.run(`state.bundle.feature_switches.ai_chat=false;state.bundle.agent_tools.send_message.enabled=false;`);
  wait.resolve(server);await pending;
  assert.equal(posted.feature_switches.ai_chat,true);
  assert.equal(posted.agent_tools.send_message.enabled,true);
  assert.equal(h.run('state.bundle.feature_switches.ai_chat'),false);
  assert.equal(h.run('state.bundle.feature_switches.summary'),false);
  assert.equal(h.run('state.bundle.agent_tools.send_message.enabled'),false);
  assert.equal(h.run('state.dirty'),true);
});

test('plugin/provider tab switches update only their panel, animate immediately and preserve fields', () => {
  const h=harness();h.run(`addProvider();_pluginSchemas={};_storeList=[];state.bundle.form_values['PluginConfigs.demo.text']='draft';`);
  const pluginPanel=node(),providerPanel=node(),keys=node('unsaved-key');
  h.nodes.set('pluginPanel',pluginPanel);h.nodes.set('providerPanel_0',providerPanel);h.nodes.set('provider_keys_0',keys);
  h.run(`switchPluginTab('store')`);assert.equal(pluginPanel.classList.contains('subtab-enter'),true);
  h.run(`switchProviderTab(0,'planner')`);assert.equal(providerPanel.classList.contains('subtab-enter'),true);
  assert.match(providerPanel.innerHTML,/回复判断模型/);assert.equal(keys.value,'unsaved-key');assert.deepEqual(h.json('providers()[0].keys'),['unsaved-key']);
  assert.equal(h.run(`state.bundle.form_values['PluginConfigs.demo.text']`),'draft');
  assert.equal(h.run('renders'),1); // Only addProvider rendered the full page.
  h.run(`renderProviderModels=()=>{throw Error('inactive chat rendered')};renderEmbeddingModels=()=>{throw Error('inactive embedding rendered')}`);
  assert.match(h.run('renderProviderDetail(providers()[0],0)'),/回复判断模型/);
  assert.match(h.run(`renderPlannerModels({planner_models:[{name:'p',enabled:true}]},0)`),/class="model-row planner-row"/);
  assert.match(h.run('renderStore()'),/^<div class="plugin-root">/);
  assert.match(css,/\.plugin-root,\.plugin-config-cards,\.subtab-panel\{grid-column:1\/-1/);
  assert.match(css,/\.model-row\.planner-row\{grid-template-columns:auto minmax\(0,1fr\) auto 120px auto\}/);
  assert.match(css,/\.subtab-enter\{animation:subtab-content-in/);
  assert.match(css,/@media \(prefers-reduced-motion: reduce\)\{\.subtab-enter\{animation:none !important\}\}/);
});

test('子页动画不被父卡片压制，结束清理忽略子元素事件', () => {
  const h=harness(),panel=node();h.context.panel=panel;
  h.run('animateSubtab(panel)');
  const end=panel.onanimationend;
  end({target:node(),animationName:'glass-subtab-content-in'});
  assert.equal(panel.classList.contains('subtab-enter'),true);
  end({target:panel,animationName:'glass-card-in'});
  assert.equal(panel.classList.contains('subtab-enter'),true);
  end({target:panel,animationName:'glass-subtab-content-in'});
  assert.equal(panel.classList.contains('subtab-enter'),false);
  assert.equal(panel.onanimationend,null);
  h.context.window.matchMedia=()=>({matches:true});h.run('animateSubtab(panel)');
  assert.equal(panel.classList.contains('subtab-enter'),false);
  assert.match(css,/\.page-anim > \.card > :not\(\.subtab-panel\)/);
  assert.match(css,/@keyframes glass-subtab-content-in\{from\{transform:translateY\(6px\)\}to\{transform:translateY\(0\)\}\}/);
  assert.match(section('  function onMotion(', '  const motionEvents='),/\.subtab-panel/);
  assert.match(section('  function update(){','  function imageFailed('),/\.subtab-panel/);
});

test('store submit button stays unique inside details before manual steps; header only refreshes', () => {
  const h=harness();h.run('_storeList=[]');const html=h.run('renderPluginStore()');
  assert.equal((html.match(/点我提交插件/g)||[]).length,1);
  assert.ok(html.indexOf('<details')<html.indexOf('点我提交插件'));
  assert.ok(html.indexOf('点我提交插件')<html.indexOf('手动提交 ▼'));
  assert.ok(html.indexOf('手动提交 ▼')<html.indexOf('1. Fork'));
  assert.match(html,/字段<br>6\. 提交 Pull Request/);
  assert.equal((html.slice(0,html.indexOf('<details')).match(/<button/g)||[]).length,1);
});

test('玻璃首绘完成后才接管，且绘制前已确定动画状态', () => {
  const flags=classes(),root={classList:flags,dataset:{},style:{setProperty(){}}};
  const context=vm.createContext({root,enabled:true,status:'ready',firstFrameReady:false,engine:'gpu',gpu:{},
    webkitTarget:false,source:'wallpaper',drawerTransition:false,document:{body:{classList:classes()}}});
  vm.runInContext(section('  function syncState(){','  function schedule(){'),context);
  vm.runInContext('syncState()',context);
  assert.equal(flags.contains('gpu-liquid-glass'),false);
  assert.equal(flags.contains('glass-render-pending'),true);
  vm.runInContext('firstFrameReady=true;syncState()',context);
  assert.equal(flags.contains('gpu-liquid-glass'),true);
  assert.equal(flags.contains('glass-render-pending'),false);
  const update=section('  function update(){','  function imageFailed(');
  assert.ok(update.indexOf('document.getAnimations')<update.indexOf('gpu.draw('));
  assert.ok(update.indexOf('gpu.draw(')<update.indexOf('firstFrameReady=true'));
  assert.match(section('  function clear(){','  // 元素级滤镜'),/firstFrameReady=false/);
  assert.match(css,/html\.glass-render-pending\.liquid-glass .*::before\{display:none !important\}/);
});

test('超视口父卡片保留玻璃区域，并沿用祖先裁剪而非把卡片截成新圆角', () => {
  const body={classList:classes()};
  function surface(left,top,width,height,parentElement,extra={}) {
    return {parentElement,clientLeft:0,clientTop:0,clientWidth:width,clientHeight:height,offsetWidth:width,offsetHeight:height,
      getBoundingClientRect:()=>({left,top,x:left,y:top,width,height,right:left+width,bottom:top+height}),
      matches:s=>s==='.card',closest:()=>null,querySelector:()=>({}),...extra};
  }
  const main=surface(10,10,370,824,body,{style:{overflowX:'hidden',overflowY:'auto'}});
  const card=surface(20,-100,350,1500,main),field=surface(40,200,310,100,card,{matches:s=>s==='.field,.feature,.stat,.btn,.pill'});
  const sidebar=surface(0,0,300,844,body,{style:{position:'fixed'}});
  const context=vm.createContext({document:{body,querySelector:s=>s==='.sidebar'?sidebar:null},targets:[card,field],narrowViewport:()=>true,
    getComputedStyle:n=>({position:'relative',visibility:'visible',display:'block',overflowX:'visible',overflowY:'visible',
      borderTopLeftRadius:'26px',borderTopRightRadius:'26px',borderBottomRightRadius:'26px',borderBottomLeftRadius:'26px',...n.style})});
  vm.runInContext(section('function gpuRegions(', '// 在 Chromium 上 GPU'),context);
  const regions=JSON.parse(vm.runInContext('JSON.stringify(gpuRegions(390,844))',context));
  assert.equal(regions.length,2);
  assert.deepEqual(regions[0].rect,[20,-100,350,1500]);
  assert.deepEqual(regions[0].clipBox,[10,10,380,834]);
  assert.equal(regions[0].soft,false);assert.equal(regions[1].soft,true);
  assert.deepEqual(regions[0].clips[0].rect,[10,10,370,824]);
  body.classList.add('nav-open');card.closest=s=>s==='.main'?main:null;
  context.drawerTransition=false;
  const opened=JSON.parse(vm.runInContext('JSON.stringify(gpuRegions(390,844))',context));
  assert.deepEqual(opened[0].clipBox,[10,10,380,834]);
  assert.deepEqual(opened[0].rect,regions[0].rect);
});

function roleLifeHarness({setInterval}={}){
  const h=harness();
  if(setInterval)h.context.setInterval=setInterval;
  vm.runInContext(section('// ==================== 角色生活 ====================', "\nconst AGENT_LEVEL_LABEL="),h.context);
  return h;
}
function roleLifeSample(overrides={}){
  return Object.assign({
    connected:true,current_account:'10001',known_accounts:['10001','20002'],
    roles:{'10001':['p1','p2'],'20002':['p9']},account:'10001',role_id:'p1',
    editable:true,read_only_reason:'',
    presets:[{id:'p1',name:'甲'},{id:'p2',name:'乙'}],
    models:[{provider_id:'x',model:'m',label:'x/m'}],
    default_config:{enabled:false,plan_enabled:true,activity_enabled:true,sleep_enabled:true,
      diary_enabled:true,dream_enabled:true,maintenance_enabled:false,timezone:'Asia/Shanghai',
      sleep_start:'00:30',sleep_hours:8,diary_time:'23:50',activities_per_day:5,
      role_daily_limit:24,retention_days:90,maintenance_interval_seconds:1800,
      model_override:'',preference:''},
    config:null,config_revision:'abc',
    status:{enabled:true,available:true,corrupt:false,account:'10001',role_id:'p1',
      revision:'rev-1',epoch:2,state_version:7,last_advance:0,date:'2026-03-10',
      sleeping:false,sleep:null,current_activity:null,
      plan:{plan_id:'p',serial:1,items:[
        {id:'x-p1-1',title:'晨间阅读',purpose:'看书',start:1,end:2,status:'completed',experience_id:'exp-1'},
        {id:'x-p1-2',title:'散步',purpose:'吹风',start:3,end:4,status:'planned',experience_id:''},
      ]},
      mood:{mood:'平静'},recent:[{id:'exp-1',kind:'activity',text:'看完一章',date:'2026-03-10',dream:false},
        {id:'dream-1',kind:'dream',text:'一条走廊',date:'2026-03-10',dream:true}],
      usage:{role_24h:3,role_limit:24,account_limit:192,account_id:'10001'},
      queue:0,jobs:[{id:'j1',kind:'plan',status:'failed',created_at:10,finished_at:11,
        error:'timeout',attempts:1,request_id:'r1',retry_of:''}],
      metrics:{last_error:'timeout',last_error_at:11,last_success_at:9},
      config:overrides.config||{enabled:true},next_node:{at:5,label:'写日记'}},
  },overrides)
}
function withRoleLife(h,setup){
  h.run(`roleLifeState={data:${JSON.stringify(setup)},loading:false,error:'',account:'10001',
    role:'p1',tab:'overview',subTab:'activity',page:{activity:1,diary:1,dream:1,plans:1,jobs:1},
    records:{activity:null,diary:null,dream:null,plans:null,jobs:null},
    pending:Object.create(null),draft:null,message:'',requests:Object.create(null)};`);
}

test('角色生活页展示概览、顶栏总开关、日常内容与损坏只读，不把计划当经历', () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  const html=h.run('renderRoleLife()');
  assert.match(html,/角色生活/);assert.doesNotMatch(html,/后台用量/);assert.doesNotMatch(html,/不额外保存模型 Key/);
  assert.match(html,/今天的安排/);assert.match(html,/最近留下的片段/);
  assert.match(html,/id="life-master-switch"/);assert.match(html,/当前人设：甲/);assert.match(html,/写日记/);
  assert.doesNotMatch(html,/id="life-account"|id="life-role"/);
  // “今天”页区分完成与未开始：只有 experience_id 的条目显示“经历已保存”
  h.run(`roleLifeState.tab='today'`);
  const today=h.run('renderRoleLife()');
  assert.match(today,/晨间阅读/);assert.match(today,/经历已保存/);assert.match(today,/散步/);
  assert.ok(today.indexOf('已完成')<today.indexOf('未开始'));
  // 损坏状态：只读，显示错误和清空，绝不能显示“用量 0”
  const bad=roleLifeSample();bad.status=Object.assign({},bad.status,{corrupt:true,available:false,jobs:[],metrics:{},error:'state identity mismatch'});
  bad.editable=false;withRoleLife(h,bad);
  const broken=h.run('renderRoleLife()');
  assert.match(broken,/状态不可读/);assert.match(broken,/state identity mismatch/);
  assert.match(today,/<span class="pill ok">已完成<\/span>/);
  assert.doesNotMatch(today,/&lt;span class=/);
  assert.doesNotMatch(broken,/用量[\s\S]{0,200}0 \/ 0/);
});

test('角色生活页顶部显示唯一总开关，统一走右上角保存且不重复开关', async () => {
  const h=roleLifeHarness();const sample=roleLifeSample({runtime_running:false,active_persona_name:'温柔猫娘'});
  withRoleLife(h,sample);
  h.run(`state.current='role-life';`);
  const html=h.run('renderRoleLife()');
  assert.match(html,/id="life-master-switch"/);assert.match(html,/角色生活总开关/);
  assert.match(html,/当前人设：温柔猫娘/);assert.match(html,/仅 WebUI/);
  assert.doesNotMatch(html,/待连接 · 只读|未连接 QQ/);
  // 设置页不再重复「启用此角色生活」，也不再出现底部保存栏
  h.run(`roleLifeState.tab='settings'`);
  const settings=h.run('renderRoleLife()');
  assert.doesNotMatch(settings,/life-savebar|roleLifeSaveConfig\(\)|启用此角色生活/);
  assert.doesNotMatch(settings,/roleLifeToggle\('enabled'/);
  // 拨动顶栏总开关会标记 state.dirty，点统一 saveAll() 直接保存角色生活配置
  const posted=[];
  h.context.fetch=async(url,opt)=>{
    posted.push({url,body:JSON.parse(opt.body)});
    return {status:200,ok:true,json:async()=>({ok:true,data:roleLifeSample({config:{enabled:true}})})};
  };
  h.context.toast=()=>{};h.context.render=()=>{};
  h.run("roleLifeSetDraft('enabled',true)");
  assert.equal(h.run('state.dirty'),true);
  await h.run('saveAll()');
  assert.equal(posted.length,1);
  assert.ok(posted[0].url.includes('/api/role-life/config'));
  assert.equal(posted[0].body.config.enabled,true);
  assert.equal(h.run('state.dirty'),false);
});

test('角色生活写操作携带 revision/epoch/state_version 与复用的 request_id', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  const posts=[];let fail=true;
  h.context.api=async(url,opt)=>{posts.push(JSON.parse(opt.body));if(fail)throw Error('network');return {ok:true}};
  h.context.toast=()=>{};
  h.context.loadRoleLife=async()=>{};
  h.context.render=()=>{};
  await h.run(`roleLifeAction('enqueue',{kind:'plan'})`);
  await h.run(`roleLifeAction('enqueue',{kind:'plan'})`);
  assert.equal(posts.length,2);
  for(const body of posts){
    assert.equal(body.expected_revision,'rev-1');assert.equal(body.expected_epoch,2);
    assert.equal(body.expected_state_version,7);
  }
  assert.equal(posts[0].request_id,posts[1].request_id); // 失败重试复用幂等 ID
  assert.equal(posts[0].action,'enqueue');assert.equal(posts[0].kind,'plan');
  fail=false;
  await h.run(`roleLifeAction('clear')`);
  assert.equal(posts[2].action,'clear');assert.equal(posts[2].expected_state_version,7);
  // 成功的清空丢弃自己的 request_id；失败过的入队保留它，供下一次网络重试复用
  const pendingIds=h.json('roleLifeState.requests');
  assert.equal(pendingIds['clear:'],undefined);
  assert.equal(pendingIds['enqueue:plan'],posts[0].request_id);
});

test('角色生活清空必须输入“清空”，损坏时传 allow_corrupt', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  const posts=[];
  h.context.api=async(url,opt)=>{posts.push(JSON.parse(opt.body));return {ok:true}};
  h.context.render=()=>{};h.context.toast=()=>{};h.context.loadRoleLife=async()=>{};
  h.context.prompt=()=>'取消';await h.run('roleLifeClear()');
  assert.equal(posts.length,0);
  h.context.prompt=()=>'清空';await h.run('roleLifeClear()');
  assert.equal(posts.length,1);assert.equal(posts[0].allow_corrupt,undefined);
  const bad=roleLifeSample();bad.status=Object.assign({},bad.status,{corrupt:true});
  withRoleLife(h,bad);h.context.prompt=()=>'清空';await h.run('roleLifeClear()');
  assert.equal(posts[1].allow_corrupt,true);
});

test('角色生活设置只保存草稿各键，模型无效时保留原值并显示暂停提示', () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  h.run(`roleLifeState.tab='settings';roleLifeState.draft=null;`);
  const html=h.run('renderRoleLife()');
  for(const key of ['plan_enabled','activity_enabled','sleep_enabled','diary_enabled','dream_enabled','mood_tone_enabled','env_awareness_enabled','maintenance_enabled'])assert.ok(html.includes("roleLifeToggle('"+key+"'"),key);
  assert.ok(!html.includes("roleLifeToggle('enabled'"));
  assert.ok(!html.includes("roleLifeToggle('chat_feedback_enabled'"));
  assert.doesNotMatch(html,/role_daily_limit/);assert.match(html,/retention_days/);
  assert.match(html,/maintenance_interval_seconds/);assert.match(html,/timezone/);
  assert.match(html,/指定模型不可用时暂不生成/);assert.match(html,/角色生活总开关/);
  assert.doesNotMatch(html,/gotoPage\('proactive'\)/);
  assert.doesNotMatch(html,/生活不等于群发/);
  // select 里已经保存但不在现有模型列表的 override 仍然显示
  h.run(`roleLifeState.draft={model_override:'gone/model'}`);
  assert.match(h.run('renderRoleLife()'),/gone\/model（当前保存值不可用，将暂停）/);
});

test('角色生活记录按 kind 分页并转义生成文本', () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  const hostile='<img src=x onerror=alert(1)>';
  h.run(`roleLifeState.tab='experiences';roleLifeState.subTab='diary';roleLifeState.records.diary={items:[{id:'d1',kind:'diary',text:${JSON.stringify(hostile)},date:'2026-03-10',dream:false}],page:1,page_size:20,total:1,pages:1};`);
  const html=h.run('renderRoleLife()');
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!html.includes('<img src=x'));
  h.run(`roleLifeState.records.dream={items:[{id:'m1',kind:'dream',dream:true,text:'梦',date:'2026-03-10'}],page:1,page_size:20,total:1,pages:1};roleLifeState.subTab='dream';`);
  const dreamHtml=h.run('renderRoleLife()');
  assert.match(dreamHtml,/梦境/);assert.doesNotMatch(dreamHtml,/不是现实经历/);
});

test('新增人格预设不复用已删除的数量 ID', () => {
  const h=harness();
  vm.runInContext(section('function personaNewId(', 'function addPersonaPreset('),h.context);
  vm.runInContext(section('function addPersonaPreset(', 'function selectPersonaPreset('),h.context);
  vm.runInContext(`
    state.bundle={form_values:{'Others.personality_presets':[{id:'default',name:'默认',prompt:''},{id:'p2',name:'乙',prompt:''}],'Others.active_personality_preset':'default'}};
    render=()=>{};setPersonaPresets=ps=>{state.bundle.form_values['Others.personality_presets']=ps};
  `,h.context);
  const removed=h.run(`JSON.parse(JSON.stringify(state.bundle.form_values['Others.personality_presets'].filter(p=>p.id!=='p2')))`);
  h.run(`state.bundle.form_values['Others.personality_presets']=${JSON.stringify(removed)};state.personaEditId=''`);
  h.run('addPersonaPreset()');
  const ids=h.json(`state.bundle.form_values['Others.personality_presets'].map(p=>p.id)`);
  assert.equal(ids.length,2);assert.ok(ids.includes('default'));
  assert.ok(!ids.includes('preset2'),'must not reuse the removed numeric id');
  h.run('addPersonaPreset()');
  assert.equal(h.run('new Set(state.bundle.form_values["Others.personality_presets"].map(p=>p.id)).size'),3);
});

test('角色生活损坏/未知用量不显示 0，清空文案简洁明确', async () => {
  const h=roleLifeHarness();const sample=roleLifeSample();
  sample.status=Object.assign({},sample.status,{corrupt:true,available:false,jobs:[],metrics:{},
    error:'state unreadable'});
  delete sample.status.revision;delete sample.status.epoch;delete sample.status.state_version;
  sample.status.budget_unknown=true;sample.status.budget_unknown_until=1893456000;
  withRoleLife(h,sample);
  const html=h.run('renderRoleLife()');
  assert.match(html,/保护性暂停/);assert.doesNotMatch(html,/0 \/ 0 次/);
  let promptText='';
  h.context.prompt=t=>{promptText=t;return '取消'};
  await h.run('roleLifeClear()');
  assert.match(promptText,/状态文件不可读/);assert.match(promptText,/不影响聊天记录/);
  assert.doesNotMatch(promptText,/完整保留原用量/);
  // 正常清空：不影响聊天记录，且不宣称清零预算
  withRoleLife(h,roleLifeSample());
  h.run('roleLifeState.tab="overview"');
  h.context.prompt=t=>{promptText=t;return '取消'};
  await h.run('roleLifeClear()');
  assert.match(promptText,/不影响聊天记录/);assert.doesNotMatch(promptText,/重置模型调用预算/);
});

test('角色生活恢复后显示备份文件名，未知用量时显示保护性暂停', () => {
  const h=roleLifeHarness();
  const sample=roleLifeSample();
  sample.status=Object.assign({},sample.status,{budget_unknown:false,budget_unknown_until:0,
    recovered_from_corrupt:true,corrupt_backup:'state.corrupt-1760000000000.json.bak'});
  withRoleLife(h,sample);
  const html=h.run('renderRoleLife()');
  assert.match(html,/state\.corrupt-1760000000000\.json\.bak/);
  assert.doesNotMatch(html,/已保护性暂停生成/);
  // usage 只在 usage 子对象里给 budget_unknown 时也要认
  const alt=roleLifeSample();
  alt.status=Object.assign({},alt.status,{usage:{role_24h:null,role_limit:24,account_limit:192,
    account_id:'10001',budget_unknown:true,budget_unknown_until:1893456000}});
  withRoleLife(h,alt);
  assert.match(h.run('renderRoleLife()'),/角色用量未知，已保护性暂停生成/);
  assert.doesNotMatch(h.run('renderRoleLife()'),/<progress/);
});

test('生活页切角色后晚到的旧概览与记录均不能覆盖新角色', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  const old=deferred(),fresh=deferred();let reads=0;
  h.context.api=()=>++reads===1?old.promise:fresh.promise;
  const first=h.run('loadRoleLife(true)'),second=h.run("roleLifeSelect('10001','p2')");
  fresh.resolve(roleLifeSample({role_id:'p2'}));await second;
  old.resolve(roleLifeSample({role_id:'p1'}));await first;
  assert.equal(h.run('roleLifeState.role'),'p2');assert.equal(h.run('roleLifeState.loading'),false);
  const pending=deferred();h.context.api=url=>url.includes('/records')?pending.promise:Promise.resolve(roleLifeSample({role_id:'p1'}));
  const record=h.run("roleLifeLoadRecords('diary')");
  await h.run("roleLifeSelect('10001','p1')");
  pending.resolve({items:[{text:'旧角色私密日记'}],page:1,pages:1});await record;
  assert.equal(h.run('roleLifeState.records.diary'),null);
});

test('生活页同角色翻页只接受最后请求，旧finally不取消新loading', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());const one=deferred(),two=deferred();let n=0;
  h.context.api=()=>++n===1?one.promise:two.promise;
  const a=h.run("roleLifeLoadRecords('diary')");h.run('roleLifeState.page.diary=2');
  const b=h.run("roleLifeLoadRecords('diary')");one.resolve({items:[{text:'第一页'}],page:1,pages:2});await a;
  assert.equal(h.run('roleLifeState.records.diary.loading'),true);
  two.resolve({items:[{text:'第二页'}],page:2,pages:2});await b;
  assert.equal(h.run('roleLifeState.records.diary.items[0].text'),'第二页');
});

test('生活页首次加载失败结束loading，已有数据刷新失败可见且禁写', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  h.run('roleLifeState.data=null');h.context.api=async()=>{throw Error('离线读取失败')};
  await h.run('loadRoleLife(true)');assert.equal(h.run('roleLifeState.loading'),false);
  assert.match(h.run('renderRoleLife()'),/离线读取失败/);assert.doesNotMatch(h.run('renderRoleLife()'),/正在读取角色生活/);
  withRoleLife(h,roleLifeSample());await h.run('loadRoleLife(true)');
  assert.match(h.run('renderRoleLife()'),/保留上次读取的内容/);assert.equal(h.run('roleLifeEditable()'),false);
});

test('生活页任务错误不是空记录，历史日程入口与时间线可达', () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  h.run("roleLifeState.tab='jobs';roleLifeState.records.jobs={error:'任务接口暂不可用',items:[]}");
  const html=h.run('renderRoleLife()');assert.match(html,/任务接口暂不可用/);assert.doesNotMatch(html,/后台还很安静/);
  h.run("roleLifeState.tab='today';roleLifeState.todayTab='plans';roleLifeState.records.plans={items:[{date:'2026-03-09',title:'旧日散步',status:'completed',start:1,end:2},{date:'2026-03-09',title:'晚间记录',status:'completed',start:3,end:4}],page:1,pages:1}");
  const plans=h.run('renderRoleLife()');assert.match(plans,/历史日程/);assert.match(plans,/旧日散步/);assert.doesNotMatch(plans,/上一页/);
});

test('生活设置保存锁住重复操作、晚回包不能写回另一角色', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());const wait=deferred();let calls=0;
  h.context.api=()=>{calls++;return wait.promise};h.run("roleLifeState.draft={preference:'已提交'}");
  const saving=h.run('roleLifeSaveConfig()');await h.run('roleLifeSaveConfig()');
  h.run("roleLifeSetDraft('preference','保存中不允许修改')");assert.equal(calls,1);
  assert.equal(h.run('roleLifeState.draft.preference'),'已提交');assert.equal(h.run('roleLifeState.saving'),true);
  wait.resolve(roleLifeSample({config:{preference:'已提交'}}));await saving;
  assert.equal(h.run('roleLifeState.saving'),false);assert.equal(h.run('roleLifeState.draft'),null);
  const late=deferred();h.context.api=()=>late.promise;h.run("roleLifeState.draft={preference:'旧角色'}");
  const old=h.run('roleLifeSaveConfig()');h.run("roleLifeState.generation=2;roleLifeState.role='p2';roleLifeState.data={role_id:'p2'};roleLifeState.draft={preference:'新角色'};roleLifeState.saving=false");
  late.resolve(roleLifeSample());await old;
  assert.equal(h.run('roleLifeState.data.role_id'),'p2');assert.equal(h.run('roleLifeState.draft.preference'),'新角色');
});

test('生活页状态版本更新刷新当前记录，时间按角色时区而非浏览器', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  h.run("roleLifeState.tab='experiences';roleLifeState.subTab='diary';roleLifeState.records.diary={items:[{text:'旧日记'}],page:1,pages:1}");
  const sample=roleLifeSample();sample.status.state_version=8;let recordCalls=0;
  h.context.api=async url=>url.includes('/records')?(recordCalls++,{items:[{text:'新日记'}],page:1,pages:1}):sample;
  await h.run('loadRoleLife(true)');await Promise.resolve();
  assert.equal(recordCalls,1);assert.equal(h.run('roleLifeState.records.diary.items[0].text'),'新日记');
  const stamp=Date.parse('2026-11-15T00:00:00Z')/1000;
  h.run("roleLifeState.data.config={timezone:'Asia/Shanghai'}");assert.equal(h.run(`roleLifeFmtTime(${stamp},true)`),'08:00');
  h.run("roleLifeState.data.config={timezone:'America/New_York'}");assert.equal(h.run(`roleLifeFmtTime(${stamp},true)`),'19:00');
});

test('生活页开关是可访问按钮且主操作唯一，危险操作不抢主位', () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  h.run("roleLifeState.tab='settings';roleLifeState.draft={preference:'草稿'}");const settings=h.run('renderRoleLife()');
  assert.match(settings,/id="life-master-switch"[^>]+role="switch"[^>]+aria-checked=/);
  assert.match(settings,/label for="life-field-sleep_start"/);assert.match(settings,/type="time"/);
  assert.equal((settings.match(/class="btn primary"/g)||[]).length,0);
  assert.doesNotMatch(settings,/life-savebar/);
  assert.match(settings,/life-danger-zone/);assert.match(settings,/aria-controls="life-panel-settings"/);
  h.run("roleLifeState.tab='overview'");const overview=h.run('renderRoleLife()');
  assert.equal((overview.match(/class="btn primary"/g)||[]).length,1);
  assert.doesNotMatch(overview,/后台用量|账号共享上限/);
});

test('生活页辅助文字具备可读对比度，顶栏总开关具备清晰样式', () => {
  const root=css.match(/\.role-life\{([^}]*)\}/)?.[1]||'';
  assert.match(root,/--muted:\s*rgba\(232,247,255,\.80\)/);
  assert.match(css,/html\[data-theme="light"\] \.role-life\{--muted:\s*rgba\(20,35,52,\.74\)\}/);
  const master=css.match(/\.life-master-control\{([^}]*)\}/)?.[1]||'';
  assert.match(master,/display:inline-flex/);
  assert.match(css,/\.role-life \.life-caption,\.role-life \.life-meta\{color:var\(--muted\)/);
  assert.match(css,/html\[data-theme="dark"\]:not\(\.liquid-glass\) \.role-life \.pill\{background:var\(--bg1\) !important\}/);
});

test('生活页保留换行、无数据不显示分页，临时读取错误没有清空操作', () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  h.run("roleLifeState.tab='experiences';roleLifeState.subTab='diary';roleLifeState.records.diary={items:[{text:'第一段\\n第二段',kind:'diary'}],page:1,pages:1}");
  assert.match(h.run('renderRoleLife()'),/class="life-prose">第一段\n第二段/);assert.match(css,/white-space:pre-wrap/);
  const sample=roleLifeSample();sample.status={read_error:true,error:'暂时读取失败'};withRoleLife(h,sample);
  const html=h.run('renderRoleLife()');assert.match(html,/暂时读不到状态/);assert.doesNotMatch(html,/清空并重建/);
  assert.equal(h.run('roleLifeEditable()'),false);
});

test('生活页保存错误转义且保留草稿，显式刷新不抹未保存设置', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  h.run("roleLifeState.tab='settings';roleLifeState.draft={preference:'我的草稿'}");
  h.context.api=async()=>{throw Error('<img src=x onerror=alert(1)>')};
  await h.run('roleLifeSaveConfig()');
  const error=h.run('renderRoleLife()');assert.match(error,/保存失败/);assert.ok(!error.includes('<img src=x'));
  assert.equal(h.run('roleLifeState.draft.preference'),'我的草稿');
  h.context.api=async()=>roleLifeSample({config:{preference:'服务端新值'}});
  await h.run('loadRoleLife(true)');assert.equal(h.run('roleLifeDraftValues().preference'),'我的草稿');
});

test('生活页离开保护与独立 WebUI 状态不阻断查看与编辑', () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());h.run("roleLifeState.draft={enabled:true}");
  h.context.confirm=()=>false;assert.equal(h.run('roleLifeLeave()'),false);assert.equal(h.run('roleLifeState.draft.enabled'),true);
  h.context.confirm=()=>true;assert.equal(h.run('roleLifeLeave()'),true);assert.equal(h.run('roleLifeState.draft'),null);
  const standalone=roleLifeSample({runtime_running:false,editable:true});
  withRoleLife(h,standalone);const html=h.run('renderRoleLife()');
  assert.match(html,/仅 WebUI/);assert.match(html,/看完一章/);assert.doesNotMatch(html,/连接 QQ 后，可以查看它的一天/);
});

test('生活页操作进行中禁止重复与切角色，错误后保留同一请求ID', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());const pending=deferred();let calls=0;
  h.context.api=()=>{calls++;return pending.promise};
  const first=h.run("roleLifeAction('regenerate_plan')");
  await h.run("roleLifeAction('regenerate_plan')");assert.equal(calls,1);
  assert.equal(h.run('roleLifeLeave()'),false);
  pending.reject(Error('连接中断'));await first;
  assert.equal(h.run('roleLifeBusy()'),false);assert.match(h.run('roleLifeState.message'),/连接中断/);
  const id=h.run("roleLifeState.requests['regenerate_plan:']");assert.ok(id);
  h.context.api=async(url,opt)=>{assert.equal(JSON.parse(opt.body).request_id,id);throw Error('仍未确认')};
  await h.run("roleLifeAction('regenerate_plan')");assert.equal(h.run("roleLifeState.requests['regenerate_plan:']"),id);
});

test('生活页服务端回退角色时清理旧记录，空页无伪造进度', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  h.run("roleLifeState.records.diary={items:[{text:'旧角色'}],page:1,pages:1};roleLifeState.draft={preference:'旧草稿'}");
  const newData=roleLifeSample({role_id:'p2'});newData.status.usage={role_24h:null,role_limit:24,account_limit:99};
  h.context.api=async()=>newData;await h.run('loadRoleLife(true)');
  assert.equal(h.run('roleLifeState.role'),'p2');assert.equal(h.run('roleLifeState.records.diary'),null);
  assert.equal(h.run('roleLifeState.draft'),null);assert.doesNotMatch(h.run('renderRoleLife()'),/<progress/);
});

test('生活页无需连接 QQ 即可查看全部内容标签与设置', () => {
  const h=roleLifeHarness();
  withRoleLife(h,roleLifeSample({runtime_running:false,editable:true}));
  for(const tab of ['overview','today','experiences','jobs','settings']){
    h.context.targetTab=tab;h.run('roleLifeState.tab=targetTab');
    const html=h.run('renderRoleLife()');
    assert.doesNotMatch(html,/还没有可读取的生活状态|待连接 · 只读/);
    assert.match(html,/id="life-master-switch"/);
  }
});

test('生活页自动刷新不被标签按钮焦点阻止，编辑输入和草稿仍暂停', () => {
  const intervals=[];
  const h=roleLifeHarness({setInterval:(callback,delay)=>intervals.push({callback,delay})});
  withRoleLife(h,roleLifeSample());h.run("state.current='role-life'");
  assert.equal(intervals.length,1);assert.equal(intervals[0].delay,15000);
  const refresh=intervals[0].callback,calls=[];
  h.context.loadRoleLife=(...args)=>calls.push(args);
  h.nodes.set('section_role_life',{contains:target=>!!target?.inside});
  const blocked=()=>{const before=calls.length;refresh();assert.equal(calls.length,before)};
  const allowed=()=>{const before=calls.length;refresh();assert.equal(calls.length,before+1);assert.deepEqual(calls.at(-1),[false,true])};
  h.context.document.activeElement={tagName:'BUTTON',inside:true};allowed();
  for(const tagName of ['INPUT','TEXTAREA','SELECT']){h.context.document.activeElement={tagName,inside:true};blocked()}
  h.context.document.activeElement={tagName:'INPUT',inside:false};allowed();
  h.context.document.activeElement=null;allowed();
  h.run("roleLifeState.tab='settings'");blocked();h.run("roleLifeState.tab='overview'");
  h.run("roleLifeState.draft={preference:'未保存'}");blocked();h.run('roleLifeState.draft=null');
  h.run('roleLifeState.loading=true');blocked();h.run('roleLifeState.loading=false');
  h.run('roleLifeState.saving=true');blocked();h.run('roleLifeState.saving=false');
  h.run('roleLifeState.pending.test=true');blocked();h.run('delete roleLifeState.pending.test');
  h.context.document.hidden=true;blocked();h.context.document.hidden=false;
  h.run("state.current='welcome'");blocked();
});

test('生活页离线时损坏和读取错误优先于待连接空态', () => {
  const h=roleLifeHarness();
  for(const status of [
    {available:false,corrupt:true,error:'离线文件损坏'},
    {available:false,read_error:true,error:'离线暂时读取失败'},
  ]){
    withRoleLife(h,roleLifeSample({connected:false,editable:false,status}));
    const html=h.run('renderRoleLife()');
    assert.match(html,status.corrupt?/状态不可读/:/暂时读不到状态/);
    assert.ok(html.includes(status.error));assert.doesNotMatch(html,/还没有可读取的生活状态/);
    if(status.read_error)assert.doesNotMatch(html,/清空并重建/);
  }
});

test('生活页标签键盘导航使用当前组并恢复焦点', () => {
  const h=roleLifeHarness();let clicked=-1,focused=-1,prevented=0;
  const tabs=[0,1,2].map(i=>({id:'tab'+i,click(){clicked=i}}));
  tabs.forEach((tab,i)=>h.nodes.set(tab.id,{focus(){focused=i}}));
  h.context.tabEvent={key:'ArrowRight',target:tabs[2],currentTarget:{querySelectorAll:()=>tabs},preventDefault(){prevented++}};
  h.run('roleLifeTabKey(tabEvent)');assert.equal(clicked,0);assert.equal(focused,0);assert.equal(prevented,1);
  h.context.tabEvent.key='End';h.context.tabEvent.target=tabs[0];h.run('roleLifeTabKey(tabEvent)');assert.equal(clicked,2);
  h.context.tabEvent.key='Home';h.run('roleLifeTabKey(tabEvent)');assert.equal(clicked,0);
});

test('生活页移除计费卡片与账号/人设下拉选择器', () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  const ov=h.run('renderRoleLife()');
  assert.match(ov,/今天的安排/);assert.match(ov,/最近留下的片段/);
  assert.doesNotMatch(ov,/后台用量|不额外保存模型 Key|失败与重试也计入/);
  assert.equal(h.run('roleLifeSelector()'),'');
  assert.doesNotMatch(ov,/id="life-account"|id="life-role"/);
});

test('生活页支持日程增删改触发、记录编辑删除、手动备注管理及联动开关', () => {
  const h=roleLifeHarness();
  const sample=roleLifeSample();
  sample.status.life_notes=[{id:'n1',date:'2026-03-10',text:'周末去公园看书',source:'manual'}];
  withRoleLife(h,sample);
  // 设置页保留情绪与节令开关，移除聊天小事反哺日常、重复总开关与底部保存栏
  h.run("roleLifeState.tab='settings'");
  const settingsHtml=h.run('renderRoleLife()');
  for(const k of ['mood_tone_enabled','env_awareness_enabled'])assert.ok(settingsHtml.includes("roleLifeToggle('"+k+"'"),k);
  assert.doesNotMatch(settingsHtml,/chat_feedback_enabled|weather_city|role_daily_limit|life-savebar/);
  assert.doesNotMatch(settingsHtml,/模型、额度与高级设置|每角色 24 小时调用上限/);
  assert.match(settingsHtml,/高级设置/);
  assert.equal((settingsHtml.match(/class="btn primary"/g)||[]).length,0);
  // 概览页卡片（即使多条 recent）不显示编辑/删除按钮
  h.run("roleLifeState.tab='overview'");
  const ovHtml=h.run('renderRoleLife()');
  assert.doesNotMatch(ovHtml,/roleLifeOpenExpEditor|roleLifeDeleteExperience/);
  // 今天日程支持添加/编辑/删除/生成经历与手动备注
  h.run("roleLifeState.tab='today';roleLifeState.planEditor={item_id:'',title:'',purpose:'',start:'15:00',end:'16:00'}");
  const todayHtml=h.run('renderRoleLife()');
  assert.match(todayHtml,/roleLifeOpenPlanEditor/);
  assert.match(todayHtml,/roleLifeDeletePlanItem/);
  assert.match(todayHtml,/roleLifeTriggerActivity/);
  assert.match(todayHtml,/保存安排/);
  assert.match(todayHtml,/周末去公园看书/);
  assert.equal((todayHtml.match(/class="btn primary"/g)||[]).length,1);
  // 生活记录页支持编辑与删除按钮，且概览页不渲染编辑按钮
  h.run("roleLifeState.tab='experiences';roleLifeState.subTab='diary';roleLifeState.records.diary={items:[{id:'d1',kind:'diary',text:'今天很安静',date:'2026-03-10'}],page:1,pages:1}");
  const recHtml=h.run('renderRoleLife()');
  assert.match(recHtml,/roleLifeOpenExpEditor/);
  assert.match(recHtml,/roleLifeDeleteExperience/);
});

test('角色生活页入场与子标签切换保留根节点并触发动画', () => {
  assert.match(css,/\.page-anim>\.role-life\{animation:glass-card-in \.58s \.04s/);
  assert.match(css,/html\.liquid-glass \.page-anim>\.role-life\{animation:glass-card-in \.82s \.05s/);
  assert.match(css,/@media\(prefers-reduced-motion:reduce\)\{\.page-anim>\.role-life[^}]*animation:none !important/);
  for(const bounds of [['  function onMotion(','  const motionEvents='],['  function update(){','  function imageFailed(']])assert.match(section(...bounds),/\.plugin-panel,\.plugin-config-cards,\.role-life/);
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  h.run(`state.current='role-life';roleLifeState.records={activity:{items:[]},diary:{items:[]},dream:{items:[]},plans:{items:[]}};`);
  const root={innerHTML:'',closest:()=>null,contains:()=>false,querySelectorAll:()=>[],querySelector:()=>null};
  Object.defineProperty(root,'outerHTML',{set(){throw new Error('must not replace outerHTML');}});
  h.nodes.set('section_role_life',root);
  h.run('roleLifeRender()');
  assert.ok(root.innerHTML.includes('role-life-bar'));
  assert.ok(!root.innerHTML.startsWith('<section class="role-life"'));
  const animated=[];
  h.context.animateSubtab=p=>animated.push(p.id);
  for(const id of ['life-panel-today','life-panel-experiences','life-record-panel','life-day-panel']){
    const n=node();n.id=id;h.nodes.set(id,n);
  }
  h.run("roleLifeSetTab('today')");
  h.run("roleLifeSetToday('plans')");
  h.run("roleLifeSetSubTab('diary')");
  h.run("roleLifeSetSubTab('dream')");
  assert.deepEqual(animated,['life-panel-today','life-day-panel','life-panel-experiences','life-record-panel']);
});

test('roleLifeAction 遇 409 冲突时清除幂等键并自动刷新', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
  const calls=[];
  let refreshed=0;
  h.context.fetch=async(url,opt)=>{
    calls.push({url,opt});
    if(url.includes('/api/role-life/action')){
      return {status:409,ok:false,json:async()=>({ok:false,error:'状态版本已变化'})};
    }
    refreshed++;
    return {status:200,ok:true,json:async()=>({ok:true,data:roleLifeSample({status:Object.assign({},roleLifeSample().status,{state_version:9})})})};
  };
  h.context.toast=()=>{};
  h.context.render=()=>{};
  await h.run("roleLifeAction('enqueue',{kind:'plan'})");
  assert.equal(calls.length>=2,true);
  assert.ok(calls.some(c=>c.url.includes('/api/role-life/action')));
  assert.equal(refreshed,1);
  assert.equal(h.run("roleLifeState.requests['enqueue:plan']"),undefined);
  assert.equal(h.run('roleLifeState.data.status.state_version'),9);
});

test('日程、记录与备注保存遇 400 或 409 失败时保留新编辑内容不丢失', async () => {
  for(const code of [400,409]){
    const h=roleLifeHarness();withRoleLife(h,roleLifeSample());
    const inputs={lifePlanTitle:node('新标题-'+code),lifePlanStart:node('11:00'),lifePlanEnd:node('12:00'),
      lifePlanPurpose:node('新说明'),lifeExpText:node('新经历正文-'+code),lifeNoteText:node('新备注内容-'+code)};
    const origGet=h.context.document.getElementById;
    h.context.document.getElementById=id=>inputs[id]||origGet(id);
    h.context.toast=()=>{};h.context.render=()=>{};
    h.context.fetch=async(url)=>{
      if(url.includes('/api/role-life/action')){
        return {status:code,ok:false,json:async()=>({ok:false,error:'模拟失败-'+code})};
      }
      return {status:200,ok:true,json:async()=>({ok:true,data:roleLifeSample()})};
    };
    h.run("roleLifeOpenPlanEditor('x-p1-2')");
    await h.run('roleLifeSavePlanEditor()');
    assert.equal(h.run('roleLifeState.planEditor.title'),'新标题-'+code);
    assert.match(h.run('roleLifePlanEditorHtml()'),new RegExp('新标题-'+code));
    h.run("roleLifeOpenExpEditor('exp-1','')");
    await h.run('roleLifeSaveExpEditor()');
    assert.equal(h.run('roleLifeState.expEditor.text'),'新经历正文-'+code);
    assert.match(h.run('roleLifeExpEditorHtml()'),new RegExp('新经历正文-'+code));
    h.run('roleLifeOpenNoteEditor()');
    await h.run('roleLifeSaveNote()');
    assert.equal(h.run('roleLifeState.noteEditor'),true);
    assert.equal(h.run('roleLifeState.noteDraft'),'新备注内容-'+code);
    assert.match(h.run('roleLifeNotesCard(true)'),new RegExp('新备注内容-'+code));
  }
});

test('角色生活设置保存携带 expected_config_revision，遇 409 刷新后端配置并保留草稿', async () => {
  const h=roleLifeHarness();withRoleLife(h,roleLifeSample({config:{sleep_hours:8,preference:''}}));
  const posted=[];
  h.context.fetch=async(url,opt)=>{
    if(url.includes('/api/role-life/config')){
      posted.push(JSON.parse(opt.body));
      return {status:409,ok:false,json:async()=>({ok:false,error:'已被其他页面修改'})};
    }
    return {status:200,ok:true,json:async()=>({ok:true,data:roleLifeSample({config_revision:'rev-new',config:{sleep_hours:7,preference:''}})})};
  };
  h.context.toast=()=>{};h.context.render=()=>{};
  h.run("roleLifeSetDraft('preference','喜欢晒太阳')");
  await h.run('roleLifeSaveConfig()');
  assert.equal(posted.length,1);
  assert.equal(posted[0].expected_config_revision,'abc');
  assert.deepEqual(posted[0].config,{preference:'喜欢晒太阳'});
  assert.equal(h.run('roleLifeState.data.config.sleep_hours'),7);
  assert.equal(h.run('roleLifeState.draft.preference'),'喜欢晒太阳');
});

test('角色生活页顶部说明功能作用，开启态展示速览指标、关闭态展示三步引导', () => {
  const h=roleLifeHarness();
  withRoleLife(h,roleLifeSample({config:{enabled:true,sleep_enabled:true,sleep_start:'00:30',sleep_hours:8}}));
  const onHtml=h.run('renderRoleLife()');
  assert.match(onHtml,/让机器人在后台按作息自己过日子/);
  assert.doesNotMatch(onHtml,/life-intro-chips|全站唯一档案|不依赖 QQ 连接|关掉零消耗/);
  assert.match(onHtml,/life-hero-stats/);
  assert.match(onHtml,/已完成 1 \/ 2 项/);
  const offSample=roleLifeSample({config:{enabled:false}});
  offSample.status=Object.assign({},offSample.status,{enabled:false});
  withRoleLife(h,offSample);
  const offHtml=h.run('renderRoleLife()');
  assert.match(offHtml,/life-guide-list/);
  assert.match(offHtml,/1\. 自己过日子/);
  assert.match(offHtml,/2\. 聊天更有活人感/);
  assert.match(offHtml,/3\. 随时可控/);
  h.run("roleLifeState.tab='settings'");
  assert.match(h.run('renderRoleLife()'),/life-toggle-grid/);
});

test('WebUI 界面外观页默认旧版经典卡片风，支持实时切换新版并隔离液态玻璃引擎', () => {
  const h=harness();
  const storage={};
  h.context.localStorage={
    getItem(k){return Object.prototype.hasOwnProperty.call(storage,k)?storage[k]:null},
    setItem(k,v){storage[k]=String(v)},
    removeItem(k){delete storage[k]},
  };
  const root={dataset:{webuiVersion:'classic'},style:{setProperty(){},getPropertyValue(){return ''}},classList:classes()};
  const glassCalls=[];
  h.context.document.documentElement=root;
  h.context.getComputedStyle=()=>({getPropertyValue:()=>''});
  h.context.liquidGlassController={setEnabled(on){glassCalls.push(['svg',!!on]);return !!on},syncRefraction(){}};
  h.context.liquidGlassLensController={setEnabled(on){glassCalls.push(['lens',!!on])},refresh(){}};
  vm.runInContext(section('function normalizeWebuiVersion(', 'function showLiquidGlassStatus('),h.context);
  vm.runInContext(section('const APPEARANCE_CACHE_KEY=', 'function shrinkBackgroundForLogin('),h.context);
  vm.runInContext(section('function settingsTabs(', 'function scrollToRouteSection('),h.context);
  const selectNode=node('classic');
  h.nodes.set('f_WebUI_ui_version',selectNode);
  h.run(`
    renderField=(f,v)=>'<div data-field="'+f.path+'">'+(v??'')+'</div>';
    pages=()=>state.bundle.ui_schema;
    state.bundle.ui_schema=[{key:'webui',title:'WebUI',fields:[
      {path:'WebUI.ui_version',category:'界面外观'},
      {path:'WebUI.theme_preset',category:'界面外观'},
      {path:'WebUI.liquid_glass',category:'界面外观'},
    ]}];
    state.bundle.form_values={'WebUI.ui_version':'classic','WebUI.theme_preset':'aurora','WebUI.background_image':'','WebUI.liquid_glass':true};
  `);
  // Default is classic (old cartoon card WebUI) with liquid glass intact
  assert.equal(h.run(`normalizeWebuiVersion('')`),'classic');
  h.run(`applyWebuiAppearance()`);
  assert.equal(root.dataset.webuiVersion,'classic');
  assert.equal(root.classList.contains('liquid-glass'),true);
  const classicHtml=h.run(`renderOrganizedSettings({key:'webui',title:'WebUI',fields:state.bundle.ui_schema[0].fields})`);
  assert.match(classicHtml,/旧版 WebUI · 经典卡片风（默认）/);
  assert.match(classicHtml,/新版 WebUI · 现代工作台/);
  assert.match(classicHtml,/当前：旧版经典卡片风/);
  assert.match(classicHtml,/ui-version-card is-active" data-ui-version="classic"/);
  assert.equal(h.run(`(html=>html.split('data-field="WebUI.ui_version"').length-1)`)(classicHtml),0);

  // Switch to modern (new workbench WebUI): liquid glass must be turned off automatically
  glassCalls.length=0;
  h.run(`switchWebuiVersion('modern')`);
  assert.equal(root.dataset.webuiVersion,'modern');
  assert.equal(root.classList.contains('liquid-glass'),false);
  assert.deepEqual(glassCalls,[['svg',false],['lens',false]]);
  assert.equal(selectNode.value,'modern');
  assert.equal(h.run(`state.bundle.form_values['WebUI.ui_version']`),'modern');
  assert.equal(h.run('state.dirty'),true);
  assert.equal(JSON.parse(storage.xcbotAppearanceCache).uiVersion,'modern');
  assert.equal(JSON.parse(storage.xcbotLoginLook).uiVersion,'modern');
  const modernHtml=h.run(`renderSettingsTab('webui','appearance')`);
  assert.match(modernHtml,/当前：新版现代工作台/);
  assert.match(modernHtml,/仅旧版经典卡片风生效/);
  assert.match(modernHtml,/ui-version-card is-active" data-ui-version="modern"/);

  // Switch back to classic: liquid glass re-enables automatically without losing setting
  glassCalls.length=0;
  h.run(`switchWebuiVersion('classic')`);
  assert.equal(root.dataset.webuiVersion,'classic');
  assert.equal(root.classList.contains('liquid-glass'),true);
  assert.deepEqual(glassCalls,[['svg',true],['lens',true]]);
  assert.equal(selectNode.value,'classic');
  assert.equal(h.run(`state.bundle.form_values['WebUI.ui_version']`),'classic');
  assert.equal(JSON.parse(storage.xcbotAppearanceCache).uiVersion,'classic');

  // CSS rules scope modern workbench cleanly under html[data-webui-version="modern"]
  assert.match(css,/html\[data-webui-version="modern"\]/);
  assert.match(css,/\.ui-version-grid/);
  assert.match(css,/@keyframes modern-page-in/);
  assert.match(css,/data-compact-count="3"/);
});

test('插件空配置极简、模型识图与前置勾选、日志自动滚动与开关动画修复', () => {
  const h=harness();
  // 1. Plugin config omits author boilerplate and footer save reminder card
  h.run(`_pluginSchemas={plain:{schema:{}},weather:{schema:{city:{type:'string',label:'城市'}}}};`);
  const pluginHtml=h.run('renderPluginConfig()');
  assert.match(pluginHtml,/<div class="card">[^]*plain[^]*该插件未提供配置项/);
  assert.doesNotMatch(pluginHtml,/插件作者声明 config_schema/);
  assert.doesNotMatch(pluginHtml,/PluginConfigs\.&lt;插件名&gt;\.&lt;字段&gt;/);
  assert.match(pluginHtml,/<span class="plugin-config-caret" aria-hidden="true"><svg /);

  // 2. Provider model row places enabled checkbox before model input and labels vision as 识图
  h.run(`state.modelRowOpen={'0:0':true};`);
  const modelHtml=h.run(`renderProviderModels({models:[{name:'gpt-4o',enabled:true,supports_multimodal:true,timeout_seconds:60}]},0)`);
  assert.match(modelHtml,/>识图<\/label>/);
  assert.doesNotMatch(modelHtml,/>多模态<\/label>/);
  assert.match(modelHtml,/class="model-row"><input type="checkbox" title="启用此模型"[^>]*><input value="gpt-4o"/);
  assert.match(modelHtml,/class="model-row-adv"/);

  // 3. Log auto-scroll captures atBottom within 72px and auto-scrolls freshly mounted elements
  vm.runInContext(section('function renderAutoScrollToggle()', 'function getMultimodalModels('),h.context);
  const logEl={id:'log_full',scrollTop:940,scrollHeight:1050,clientHeight:60,scrollTo(x,y){this.scrollTop=y}};
  const mountedEl={id:'log_new',scrollTop:0,scrollHeight:800,clientHeight:200,scrollTo(x,y){this.scrollTop=y}};
  h.context.document.querySelectorAll=()=>[logEl];
  const captured=h.run('captureLogScrollState()'); // remain = 1050 - 940 - 60 = 50 (< 72)
  assert.equal(captured.log_full.atBottom,true);
  logEl.scrollHeight=1200;
  h.context.document.querySelectorAll=()=>[logEl,mountedEl];
  h.context._prevScroll=captured;
  h.run('restoreLogScrollState(_prevScroll)');
  assert.equal(logEl.scrollTop,1200);
  assert.equal(mountedEl.scrollTop,800);

  // 4. CSS fixes modern RoleLife double-pill switch, narrow model checkbox row, carets, reveal animations & backdrop-root
  assert.match(css,/html\[data-webui-version="modern"\] \.role-life \.switch:before\s*\{\s*display:\s*none !important;/);
  assert.match(css,/\.model-row>input\[type="checkbox"\],\.embed-row>input\[type="checkbox"\]\{order:-2;flex:0 0 auto;margin:0\}/);
  assert.match(css,/@keyframes ui-reveal-down/);
  assert.match(css,/@keyframes ui-modal-pop/);
  assert.match(css,/html\[data-webui-version="modern"\] \.page-anim > \.role-life \{\s*animation: none !important;/);
});

test('弹窗回车即时渲染、日志自动滚动、统计页升级、追踪完整请求与MCP统一保存', async () => {
  const h=harness();
  // 1. Model modal Enter blurs input & forceRender bypasses isEditingField and page-anim
  vm.runInContext(section('function render(){','function renderWelcome('),h.context);
  vm.runInContext(section('function isEditingField(','async function refreshAll('),h.context);
  const inputNode=node('deepseek-chat');
  inputNode.tagName='INPUT';
  let blurred=false;
  inputNode.focus=()=>{h.context.document.activeElement=inputNode};
  inputNode.blur=()=>{blurred=true;h.context.document.activeElement=null};
  const modalNode=node();
  modalNode.contains=el=>el===inputNode;
  const contentNode=node();
  contentNode.dataset={currentPage:'providers'};
  contentNode.classList.add('page-anim');
  h.nodes.set('modelInputModal',modalNode);
  h.nodes.set('modelInputValue',inputNode);
  h.nodes.set('content',contentNode);
  h.context.document.activeElement=inputNode;
  h.context.document.querySelectorAll=()=>[];
  h.run(`
    snapshotPluginConfigOpen=()=>{};applyWebuiAppearance=()=>{};applyRoute=()=>{};
    captureLogScrollState=()=>({});renderNav=()=>{};updateNavIndicator=()=>{};renderConnectionStatus=()=>{};
    meta=()=>({key:'providers',title:'提供商',desc:''});
    state.current='providers';state._lastPage='providers';
    state.bundle.form_values['Others.llm_providers']=[{id:'p1',base_url:'https://api.example.com/v1',keys:['k1'],models:[]}];
    openModelInput(0,'chat');
    el('modelInputValue').value='deepseek-chat';
    submitModelInput();
  `);
  assert.equal(blurred,true);
  assert.equal(modalNode.style.display,'none');
  assert.match(contentNode.innerHTML,/deepseek-chat/);
  assert.equal(contentNode.classList.contains('page-anim'),false);

  // 2. Log auto-scroll pins to bottom even on large bursts (remain > 80) and pauses cleanly
  vm.runInContext(section('function renderAutoScrollToggle()', 'function getMultimodalModels('),h.context);
  const logBox={id:'log_full',scrollTop:100,scrollHeight:2400,clientHeight:300,scrollTo(x,y){this.scrollTop=y}};
  const btnNode=node();
  h.context.document.querySelectorAll=sel=>sel==='pre.log'?[logBox]:sel==='[data-asbtn]'?[btnNode]:[];
  h.run(`localStorage.webuiAutoScroll='true';scrollLogsToBottom();`);
  assert.equal(logBox.scrollTop,2400);
  h.run(`setAutoScrollState(false,false);`);
  assert.equal(h.run(`localStorage.webuiAutoScroll`),'false');
  assert.match(btnNode.innerHTML,/⬜ 自动滚动/);

  // 3. Stats page shows cache hit rate, success rate, avg tokens, 24h token trend and omits 统计说明
  vm.runInContext(section('function chartBars(', 'function dbgKV('),h.context);
  h.run(`state.bundle.statistics={
    generated_at:1700000000,
    summary:{message_count:10,api_calls:12,api_tokens:24000,model_count:2,success_calls:11,failure_calls:1,success_rate:91.7,avg_tokens_per_call:2182,total_prompt_tokens:10000,total_cached_tokens:4500,cache_hit_rate:45.0},
    message_scene:[{label:'群聊',value:8},{label:'规划器',value:3},{label:'角色生活',value:1}],
    message_trend:[{label:'10-01 12:00',value:5}],
    token_trend_1d:[{label:'10-01 12:00',value:12000}],
    model_ranking:[{model:'p1/m1',calls:10,tokens:20000,cache_hit_rate:45.0}],
    session_tokens_top10_1d:[{session:'群聊 100',calls:8,tokens:18000}],
    api_history:[{model:'p1/m1',status:'success',scene:'规划器 · 群聊 100',host:'api.example.com',time:'2026-01-01 12:00:00',message_count:3,tokens:2000,prompt_tokens:1000,cached_tokens:500,preview:'test'}]
  };`);
  const statsHtml=h.run('renderStats()');
  assert.match(statsHtml,/缓存平均命中率/);
  assert.match(statsHtml,/45%/);
  assert.match(statsHtml,/调用成功率/);
  assert.match(statsHtml,/平均单次 Tokens/);
  assert.match(statsHtml,/Token 消耗趋势/);
  assert.match(statsHtml,/缓存 50%/);
  assert.doesNotMatch(statsHtml,/仅统计真正触发 Bot 的消息/);
  assert.doesNotMatch(statsHtml,/统计说明/);

  // 4. Trace detail defaults 最终发送请求 and 上下文历史 to collapsed, supports retry attempt switching & raw JSON toggle
  vm.runInContext(section('let traceState=', '// 自检结果存在这里'),h.context);
  h.run(`
    traceState.openId='t1';
    traceState.reqOpen=false;traceState.ctxOpen=false;traceState.reqAttempt=-1;traceState.reqMode='structured';
    traceState.detail={
      id:'t1',ok:true,time:1700000000,duration_ms:1500,session_id:'group_100',context_type:'group',chat_id:'100',
      model:'m2',display_model:'p1/m2',attempt_count:2,retry_count:1,
      system_prompt:'sys prompt',prompt_chars:10,user_message:'hi',reply:'hello',
      history_count:1,history_overview:[{role:'user',chars:4,content:'prev'}],
      tokens:{prompt:500,completion:100,cached:200,total:600},
      request_payload:{model:'m2',display_model:'p1/m2',stream:false,messages:[{role:'system',content:'sys2'},{role:'user',content:'hi'}]},
      attempts:[
        {index:0,ok:false,model:'m1',display_model:'p1/m1',duration_ms:400,category:'rate_limit',error:'429',request_payload:{model:'m1',display_model:'p1/m1',messages:[{role:'system',content:'sys1'}]}},
        {index:1,ok:true,model:'m2',display_model:'p1/m2',duration_ms:1100,category:'success',request_payload:{model:'m2',display_model:'p1/m2',messages:[{role:'system',content:'sys2'},{role:'user',content:'hi'}]}}
      ]
    };
  `);
  const collapsedTrace=h.run('renderTraceDetail()');
  assert.match(collapsedTrace,/最终发送请求/);
  assert.doesNotMatch(collapsedTrace,/当次系统提示词/);
  assert.doesNotMatch(collapsedTrace,/sys2/);
  assert.doesNotMatch(collapsedTrace,/prev/);
  h.run(`traceState.reqOpen=true;`);
  const expandedFinal=h.run('renderTraceDetail()');
  assert.match(expandedFinal,/sys2/);
  assert.match(expandedFinal,/#1 p1\/m1 \(失败\)/);
  assert.match(expandedFinal,/#2 p1\/m2 \(成功 · 最终\)/);
  h.run(`traceState.reqAttempt=0;traceState.reqMode='raw';`);
  const expandedFirstRaw=h.run('renderTraceDetail()');
  assert.match(expandedFirstRaw,/sys1/);
  assert.match(expandedFirstRaw,/&quot;model&quot;: &quot;m1&quot;/);

  // 5. MCP save unified into saveAll(), no duplicate inner 保存配置 button, rename updates data-mcp-name
  vm.runInContext(section('const AGENT_LEVEL_LABEL=', '// ==================== 首次设置访问 Token'),h.context);
  h.run(`
    state.bundle.agent_tools={mcp_tools:{enabled:true,level:'admin'}};
    state.bundle.agent_tool_meta=[];
    state.bundle.ui_schema=[{key:'agent',title:'Agent',fields:[{path:'Agent.enabled',label:'启用 Agent',type:'bool',desc:'desc'}]}];
    state.current='agent';
    mcpState={loaded:true,loading:false,data:{available:true,config_path:'data/mcp_server.json',servers:{},registered_tools:[]},draft:null,dirty:false};
  `);
  const agentToolsHtml=h.run(`renderAgentContent('tools')`);
  assert.doesNotMatch(agentToolsHtml,/不会自动替你关闭主动回复/);
  const mcpHtml=h.run('renderMcpPanel()');
  assert.doesNotMatch(mcpHtml,/>保存配置</);
  h.run('addMcpServer(false)');
  assert.equal(h.run('mcpState.dirty'),true);
  assert.equal(h.run('state.dirty'),true);
  const renameInput={dataset:{mcpName:'server1'},closest:()=>cardMock};
  const cmdInput={dataset:{mcpName:'server1'}};
  const headingNode={textContent:'server1'};
  const cardMock={dataset:{mcpCard:'server1'},querySelector:()=>headingNode,querySelectorAll:()=>[renameInput,cmdInput]};
  h.context._renameInput=renameInput;
  h.context._cmdInput=cmdInput;
  h.run(`renameMcpServer('server1','my_mcp',_renameInput)`);
  assert.equal(cmdInput.dataset.mcpName,'my_mcp');
  assert.equal(headingNode.textContent,'my_mcp');
  h.run(`setMcpField(_cmdInput.dataset.mcpName,'command','uvx')`);
  assert.equal(h.json('mcpDraft()').my_mcp.command,'uvx');
  const calls=[];
  h.context.fetch=async (url,opt)=>{
    calls.push({url,body:JSON.parse(opt.body)});
    if(url==='/api/ui-state')return {status:200,ok:true,json:async()=>({ok:true,data:h.json('state.bundle')})};
    if(url==='/api/agent/mcp')return {status:200,ok:true,json:async()=>({ok:true,data:{available:true,servers:h.json('mcpDraft()'),registered_tools:[]}})};
    throw new Error('unexpected '+url);
  };
  await h.run('saveAll(true)');
  assert.equal(calls.some(c=>c.url==='/api/agent/mcp'&&c.body.action==='save'&&c.body.servers.my_mcp.command==='uvx'),true);
  assert.equal(h.run('mcpState.dirty'),false);

  // 6. CSS layout fixes for Agent settings alignment & welcome .stat vertical centering
  assert.match(css,/\.settings-panel-grid>\*\{grid-column:1 \/ -1;min-width:0\}/);
  assert.match(css,/html\[data-webui-version="modern"\] \.stat \{[\s\S]*justify-content:\s*center;/);
});

test('Phase 4 WebUI/RoleLife: Agent卡全员同步、多模态渲染、MCP SSE、遗漏默认值、经验回拉与离开清理', async () => {
  const h=harness();
  vm.runInContext(section('function renderField(','function renderAutoScrollToggle('),h.context);
  vm.runInContext(section('function getMultimodalModels(','function renderEndpointsEditor('),h.context);
  vm.runInContext(section('function seedPluginConfigValues(','function renderPluginStore('),h.context);
  vm.runInContext(section('let roleLifeState=','function roleLifeTab('),h.context);
  vm.runInContext(section('const AGENT_LEVEL_LABEL=','// ==================== 首次设置访问 Token'),h.context);
  vm.runInContext(section('function gotoPage(','function render(){'),h.context);

  // 1) P1-6: renderProviders renders multimodal fields + addMcpServer(true) renders SSE URL
  h.run(`
    meta=()=>({key:'providers',title:'提供商',fields:[
      {path:'Others.api_multimodal_model',label:'识图模型',type:'multimodal_model',desc:''},
      {path:'Others.api_multimodal_image_mode',label:'图片处理模式',type:'select',options:['relay','direct'],desc:''}
    ]});
    state.bundle.form_values['Others.llm_providers']=[{id:'p1',base_url:'https://x/v1',keys:['k1'],models:[{name:'gpt-4o',enabled:true,supports_multimodal:true}]}];
    state.bundle.form_values['Others.api_multimodal_model']='';
    state.bundle.form_values['Others.api_multimodal_image_mode']='relay';
    isValidPage=()=>true;forceRender=()=>{};clearDraft=()=>{};closeMobileNav=()=>{};updateRoute=()=>{};traceDiscardPending=()=>{};roleLifeRender=()=>{};refreshAll=async()=>{};
  `);
  const providersHtml=h.run('renderProviders()');
  assert.match(providersHtml,/识图与图片处理/);
  assert.match(providersHtml,/Others\.api_multimodal_model|f_Others_api_multimodal_model/);
  assert.match(providersHtml,/f_Others_api_multimodal_image_mode/);
  assert.match(providersHtml,/p1\/gpt-4o/);

  h.run(`mcpState={loaded:true,loading:false,data:{available:true,config_path:'',servers:{},registered_tools:[]},draft:null,dirty:false};addMcpServer(true);`);
  const sseHtml=h.run('renderMcpPanel()');
  assert.match(sseHtml,/SSE 地址/);
  assert.doesNotMatch(sseHtml,/启动命令/);
  // Stale renameMcpServer with unknown oldName does not write undefined
  h.run(`renameMcpServer('missing_old','new_name',null);`);
  assert.equal(h.json('mcpDraft()').new_name,undefined);

  // 2) P1-4: toggleAgentTool / setAgentToolLevel syncs all members of a grouped card
  h.run(`
    state.bundle.agent_tools={
      file_write:{enabled:true,level:'admin'},
      execute_shell:{enabled:true,level:'admin'},
      execute_python:{enabled:true,level:'admin'}
    };
    state.bundle.agent_tool_meta=[{key:'file_write',members:['file_write','execute_shell','execute_python']}];
  `);
  const swMock={classList:classes()};
  h.context._swMock=swMock;
  h.run(`toggleAgentTool('file_write',_swMock);setAgentToolLevel('file_write','user');`);
  const savedTools=h.json('state.bundle.agent_tools');
  assert.equal(savedTools.file_write.enabled,false);
  assert.equal(savedTools.execute_shell.enabled,false);
  assert.equal(savedTools.execute_python.enabled,false);
  assert.equal(savedTools.execute_shell.level,'user');
  assert.equal(savedTools.execute_python.level,'user');

  // 3) P2-5: missing plugin schema default is not seeded as null; leaveConfirm resets pendingBackground
  h.run(`
    _pluginSchemas={demo:{schema:{secret:{type:'string'},mode:{type:'select',default:'fast'}},values:{}}};
    seedPluginConfigValues();
  `);
  assert.equal(Object.hasOwn(h.json('state.bundle.form_values'),'PluginConfigs.demo.secret'),false);
  assert.equal(h.json('state.bundle.form_values')['PluginConfigs.demo.mode'],'fast');

  const leaveModalNode=node();
  leaveModalNode._pending={k:'welcome',section:''};
  h.nodes.set('leaveModal',leaveModalNode);
  h.run(`pendingBackground={filename:'bg.png',image_b64:'abc'};state.dirty=true;leaveConfirm();`);
  assert.equal(h.run('pendingBackground'),null);

  // 4) P2-2: roleLifeOpenExpEditor fetches full experience text when absent locally; clear resets page
  h.context.fetch=async (url)=>{
    if(String(url).startsWith('/api/role-life/records')){
      return {status:200,ok:true,json:async()=>({ok:true,data:{items:[{id:'exp-missing',text:'完整历史经历正文'}],pages:1}})};
    }
    if(url==='/api/role-life/action'){
      return {status:200,ok:true,json:async()=>({ok:true,data:{ok:true}})};
    }
    throw new Error('unexpected '+url);
  };
  h.run(`
    loadRoleLife=async()=>{};roleLifeLoadRecords=async()=>{};roleLifeBusy=()=>false;
    roleLifeState.account='global';roleLifeState.role='default';
    roleLifeState.data={account:'global',role_id:'default',editable:true,status:{enabled:true,available:true,revision:'r1',epoch:0,state_version:1,recent:[],plan:{items:[]}}};
    roleLifeState.records={activity:{items:[]},diary:{items:[]},dream:{items:[]}};
    roleLifeState.page={activity:3,diary:2,dream:4,plans:1,jobs:2};
  `);
  await h.run(`roleLifeOpenExpEditor('exp-missing','')`);
  assert.equal(h.json('roleLifeState.expEditor').text,'完整历史经历正文');
  await h.run(`roleLifeAction('clear')`);
  assert.equal(h.json('roleLifeState.page').activity,1);
  assert.equal(h.json('roleLifeState.page').diary,1);
  assert.equal(h.json('roleLifeState.page').dream,1);
});

(async()=>{
  let passed=0;
  for(const [name,fn] of tests){try{await fn();console.log(`ok ${++passed} - ${name}`)}catch(e){console.error(`FAIL - ${name}`);throw e;}}
  console.log(`Passed ${passed} offline frontend tests; no bot or real API was used.`);
})().catch(e=>{console.error(e);process.exitCode=1;});
