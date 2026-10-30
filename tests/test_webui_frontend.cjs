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

(async()=>{
  let passed=0;
  for(const [name,fn] of tests){try{await fn();console.log(`ok ${++passed} - ${name}`)}catch(e){console.error(`FAIL - ${name}`);throw e;}}
  console.log(`Passed ${passed} offline frontend tests; no bot or real API was used.`);
})().catch(e=>{console.error(e);process.exitCode=1;});
