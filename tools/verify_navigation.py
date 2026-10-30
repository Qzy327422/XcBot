"""离线检查新导航与标签草稿；不启动机器人、不访问真实接口。"""
import ast
import copy
import re
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from verify_chatroom_regression import assert_controls, assert_operable

ROOT = Path(__file__).resolve().parent.parent


def main():
    source = (ROOT / "webui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in ("field", "build_ui_schema")]
    namespace = {"Dict": dict, "Any": object}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "schema", "exec"), namespace)
    schema = namespace["build_ui_schema"]({})
    values = {f["path"]: copy.deepcopy(f.get("default")) for page in schema for f in page["fields"]}
    long_name = 'OfflineNameWithoutSpaces' * 12
    values.update({"ProactiveReply.group.enabled": False, "ProactiveReply.private.enabled": False,
                   "Agent.enabled": False, "WebUI.liquid_glass": False,
                   "Others.llm_providers": [{"id": "offline", "base_url": "", "keys": [],
                       "models": [{"name": long_name, "enabled": True}],
                       "planner_models": [{"name": long_name, "enabled": True}],
                       "embedding_models": [{"name": long_name, "enabled": True, "dimensions": 1024}]}],
                   "Others.personality_presets": [{"id": "default", "name": "默认人格", "prompt": "离线布局测试"}],
                   "Others.active_personality_preset": "default"})
    feature_tree = ast.parse((ROOT / "webui_core/features.py").read_text(encoding="utf-8"))
    feature_meta = next(ast.literal_eval(node.value) for node in feature_tree.body
                        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "FEATURE_META" for t in node.targets))
    bundle = {"ui_schema": schema, "form_values": values, "status": {},
              "feature_switches": {f["key"]: True for f in feature_meta},
              "feature_meta": feature_meta, "agent_tools": {}, "agent_tool_meta": [], "config_fingerprint": "offline"}
    html = re.search(r"INDEX_HTML = r'''(.*?)'''", source, re.S).group(1)
    js = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/app.css").read_text(encoding="utf-8")
    requests = []

    def route(req):
        url = req.request.url
        assert url.startswith('http://xcbot.test/'), url
        requests.append(url)
        if "/static/app.js" in url:
            req.fulfill(body=js, content_type="text/javascript")
        elif "/static/app.css" in url:
            req.fulfill(body=css, content_type="text/css")
        elif "/api/ui-state" in url:
            if req.request.method == 'POST':
                # Only the offline submitted fixture is persisted, never a real bot endpoint.
                posted = req.request.post_data_json
                for key in ('form_values', 'feature_switches', 'agent_tools'):
                    if key in posted:
                        bundle[key] = posted[key]
            req.fulfill(json={"ok": True, "data": bundle})
        elif "/api/auth/state" in url:
            req.fulfill(json={"ok": True, "data": {"has_token": True}})
        elif "/api/plugins/schemas" in url:
            req.fulfill(json={"ok": True, "data": {"weather": {"schema": {
                "show_detail": {"type": "bool", "label": "显示详细天气", "desc": "开启后显示湿度和风力", "default": False},
                "city": {"type": "string", "label": "城市", "default": long_name},
                "mode": {"type": "select", "label": "模式", "options": [long_name, "short"], "default": long_name},
                "notes": {"type": "textarea", "label": "备注", "default": long_name}},
                "values": {"show_detail": False, "city": long_name, "mode": long_name, "notes": long_name}}, "plain": {"schema": {}, "values": {}}}})
        elif "/api/knowledge/documents" in url:
            req.fulfill(json={"ok": True, "data": [{"id": "offline_doc", "name": "布局测试.txt", "status": "ready", "chunks": 3}]})
        elif "/api/agent/mcp" in url:
            req.fulfill(json={"ok": True, "data": {"available": True, "servers": {
                "local_demo": {"command": long_name, "args": [long_name], "enabled": True},
                "remote_demo": {"url": "http://xcbot.test/" + long_name, "enabled": False}}, "registered_tools": []}})
        elif "/assets/" in url:
            req.fulfill(status=204, body="")
        elif "/api/" in url:
            raise AssertionError("未预期接口：" + url)
        else:
            req.fulfill(body=html, content_type="text/html; charset=utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.route("**/*", route)
        context.add_init_script("localStorage.webuiToken='offline';")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto("http://xcbot.test/#/proactive")
        page.wait_for_selector("#settingsPanel_proactive")
        assert page.locator(".nav-group-title").count() == 3
        assert page.locator(".custom-nav-item[data-key=chatroom]").count() == 0
        assert page.locator(".custom-nav-item[data-key=debug]").count() == 0
        assert page.locator(".toolbar button[onclick*=chatroom]").count() == 1
        assert page.locator(".toolbar button[onclick*=debug]").count() == 1
        assert page.locator("#settingsPanel_proactive").inner_text().find("群聊") >= 0
        group_input = page.locator("#f_ProactiveReply_group_trigger_probability")
        group_input.fill("0.55")
        page.locator("#settingsTabs_proactive [data-tab=private]").click()
        page.wait_for_selector("#f_ProactiveReply_private_trigger_probability")
        assert page.evaluate("state.bundle.form_values['ProactiveReply.group.trigger_probability']") == 0.55
        page.locator("#settingsTabs_proactive [data-tab=group]").click()
        assert group_input.input_value() == "0.55"
        assert page.evaluate("state.dirty")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#settingsTabs_proactive [data-tab=private]").click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.evaluate("state.dirty=false;clearDraft();gotoPage('webui')")
        page.wait_for_selector("#settingsPanel_webui")
        assert page.locator("#f_WebUI_theme_preset").count() == 1
        assert page.locator(".glass-settings-family #f_WebUI_liquid_glass").count() == 0  # bool 使用开关，不生成 input ID
        assert page.locator(".glass-settings-controls>.setting-row").count() == 4
        assert page.locator(".glass-settings-controls>.field").count() == 0
        glass_text = page.locator(".glass-settings-family").inner_text()
        assert "禁用背景模糊" in glass_text and "折射强度" in glass_text
        toolbar = page.locator('.toolbar')
        toolbar.evaluate('n=>n.scrollLeft=0')
        assert toolbar.evaluate("n=>{const b=n.firstElementChild.getBoundingClientRect(),r=n.getBoundingClientRect();return b.left>=r.left-1&&b.right<=r.right+1}")
        assert toolbar.evaluate("n=>new Set([...n.children].map(c=>{const r=c.getBoundingClientRect();return Math.round(r.top+r.height/2)})).size===1")
        assert toolbar.evaluate('n=>n.scrollWidth>n.clientWidth')
        toolbar.evaluate('n=>n.scrollLeft=n.scrollWidth')
        assert toolbar.evaluate("n=>{const b=n.lastElementChild.getBoundingClientRect(),r=n.getBoundingClientRect();return b.left>=r.left-1&&b.right<=r.right+1}")
        page.locator("#settingsTabs_webui [data-tab=service]").click()
        page.wait_for_selector("#f_WebUI_port")
        page.locator("#settingsTabs_webui [data-tab=network]").click()
        page.wait_for_selector("#f_Others_http_proxy")
        page.evaluate("gotoPage('agent')")
        page.wait_for_selector("#agentSettingsPanel")
        page.locator("#agentSettingsTabs [data-tab=settings]").click()
        page.wait_for_selector("#f_Agent_max_rounds")
        page.locator("#f_Agent_max_rounds").fill("7")
        page.locator("#agentSettingsTabs [data-tab=mcp]").click()
        page.wait_for_selector("#section_MCP_")
        assert page.evaluate("state.bundle.form_values['Agent.max_rounds']") == 7
        assert page.evaluate("state.dirty")
        page.evaluate("state.dirty=false;clearDraft();gotoPage('features')")
        page.wait_for_selector(".feature-options-grid")
        for width in (390, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            short = page.locator(".feature-option").filter(has=page.get_by_role("heading", name="AI 对话", exact=True)).first
            long = page.locator(".feature-option").filter(has=page.get_by_role("heading", name="群聊响应", exact=True)).first
            assert short.bounding_box()["height"] < long.bounding_box()["height"] - 60
            assert short.bounding_box()["height"] < 220
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.evaluate("state.bundle.form_values['WebUI.liquid_glass']=true;render()")
        page.wait_for_function("document.documentElement.classList.contains('liquid-glass')")
        assert page.locator('.feature-option-fields>.field').count() == 0
        assert page.locator('.feature-option-fields>.setting-row').count() > 0
        assert page.locator('.feature-option-fields .glass-lens-layer').count() == 0
        page.evaluate("state.dirty=false;clearDraft();refreshAll(true)")
        page.wait_for_function("document.getElementById('saveState').textContent==='已同步'")
        page.evaluate("gotoPage('store')")
        page.wait_for_selector('details.plugin-config-card')
        details = page.locator('details.plugin-config-card').first
        assert not details.evaluate('n=>n.open')
        summary = details.locator('summary')
        summary.focus()
        page.keyboard.press('Enter')
        assert details.evaluate('n=>n.open')
        page.keyboard.press('Space')
        assert not details.evaluate('n=>n.open')
        summary.click()
        # Same-task open then replace tests the queued native toggle race.
        page.evaluate("refreshPluginPanel('config')")
        assert details.evaluate('n=>n.open')
        page.evaluate("render()")
        page.wait_for_selector('.plugin-setting-row')
        page.evaluate("state.bundle.form_values['WebUI.liquid_glass']=true;applyWebuiAppearance()")
        assert page.locator('.plugin-setting-row.feature,.plugin-setting-row.field').count() == 0
        assert page.locator('.plugin-setting-row .pill').count() == 0
        row = page.locator('.plugin-setting-row').first
        page.wait_for_function("!document.getElementById('content').classList.contains('page-anim')")
        assert row.locator('.switch').bounding_box()['x'] > row.bounding_box()['x'] + 20
        assert row.evaluate("n=>getComputedStyle(n,'::before').content") in ('none', 'normal')
        row.locator('.switch').click()
        assert page.evaluate("state.bundle.form_values['PluginConfigs.weather.show_detail']") is True
        page.evaluate("saveAll(true)")
        assert details.evaluate('n=>n.open')
        assert page.evaluate("state.bundle.form_values['PluginConfigs.weather.show_detail']") is True
        page.evaluate("loadPluginSchemas()")
        assert details.evaluate('n=>n.open')
        page.evaluate("_storeList=[];switchPluginTab('store');switchPluginTab('config')")
        assert details.evaluate('n=>n.open')
        assert page.evaluate("state.bundle.form_values['PluginConfigs.weather.show_detail']") is True
        page.evaluate("state.dirty=false;gotoPage('webui');gotoPage('store')")
        page.wait_for_selector('details.plugin-config-card[open]')
        assert page.get_by_text('该插件未提供配置项', exact=True).count() == 1

        output_dir = Path(tempfile.mkdtemp(prefix='xcbot-layout-'))
        # 后续只测稳定布局；不让入场位移混入几何检查。
        page.add_style_tag(content='*,*::before,*::after{animation:none !important;transition:none !important}')
        checked = 0
        for glass in (False, True):
            for theme in ('light', 'dark'):
                page.evaluate("""([glass,theme])=>{state.dirty=false;clearDraft();state.bundle.form_values['WebUI.liquid_glass']=glass;
                  setTheme(theme);applyWebuiAppearance()}""", [glass, theme])
                for width in (320, 390, 768, 980, 981, 1440):
                    page.set_viewport_size({'width': width, 'height': 900})
                    for key in ('webui', 'proactive', 'providers', 'persona', 'knowledge', 'features', 'agent', 'store'):
                        page.evaluate("key=>{state.dirty=false;if(key==='webui')state.settingsTabs.webui='appearance';gotoPage(key);document.querySelector('.main').scrollTop=0}", key)
                        page.wait_for_function("key=>document.getElementById('content').dataset.currentPage===key", arg=key)
                        if key == 'agent':
                            page.evaluate("switchAgentSettingsTab('mcp')")
                            page.wait_for_selector('.mcp-server-fields')
                        page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                        label = (glass, theme, width, key)
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), label
                        issues = page.evaluate("""()=>{
                          const problems=[],main=document.querySelector('.main').getBoundingClientRect();
                          for(const n of document.querySelectorAll('#content .card,#content .field,#content .feature,#content .setting-row')){
                            const r=n.getBoundingClientRect();if(!r.width||!r.height)continue;
                            if(r.left<main.left-1||r.right>main.right+1)problems.push(['越界',n.className,Math.round(r.left),Math.round(r.right)]);
                            for(let p=n.parentElement;p&&p!==document.body;p=p.parentElement){
                              if(!/(hidden|clip|auto|scroll)/.test(getComputedStyle(p).overflowX))continue;
                              const b=p.getBoundingClientRect(),left=b.left+p.clientLeft;
                              if(r.left<left-1||r.right>left+p.clientWidth+1)problems.push(['祖先裁切',n.className,p.className]);
                              break;
                            }
                          }
                          for(const p of document.querySelectorAll('#content,#content *')){
                            const items=[...p.children].filter(n=>n.matches('.card,.field,.feature,.setting-row')).map(n=>({n,r:n.getBoundingClientRect()})).filter(x=>x.r.width&&x.r.height);
                            for(let i=0;i<items.length;i++)for(let j=i+1;j<items.length;j++){
                              const a=items[i].r,b=items[j].r;
                              if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)
                                problems.push(['重叠',items[i].n.className,items[j].n.className]);
                            }
                          }
                          return problems;
                        }""")
                        assert not issues, (label, issues)
                        controls = '#content input:not([type=hidden]),#content select,#content textarea'
                        assert_controls(page, controls, label)
                        if key in ('providers', 'agent', 'store', 'knowledge'):
                            assert_operable(page, controls, label)
                        if key == 'providers':
                            # 三种模型行和展开的高级表单都用真实 DOM，长名称仅来自夹具。
                            for tab in ('chat', 'planner', 'embedding'):
                                page.locator(f'#providerTabs_0 [data-tab={tab}]').click()
                                if tab == 'chat':
                                    page.evaluate("state.modelRowOpen={'0:0':true};forceRender()")
                                page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                                assert_controls(page, controls, (label, tab))
                                assert_operable(page, '#providerPanel_0 input,#providerPanel_0 select', (label, tab))
                            page.locator('#providerTabs_0 [data-tab=chat]').click()
                        if width <= 980:
                            toolbar.evaluate('n=>n.scrollLeft=0')
                            assert toolbar.evaluate("n=>{const r=n.getBoundingClientRect(),b=n.firstElementChild.getBoundingClientRect();return b.left>=r.left-1&&b.right<=r.right+1}"), label
                            assert toolbar.evaluate("n=>new Set([...n.children].map(c=>{const r=c.getBoundingClientRect();return Math.round(r.top+r.height/2)})).size===1"), label
                            toolbar.evaluate('n=>n.scrollLeft=n.scrollWidth')
                            assert toolbar.evaluate("n=>{const r=n.getBoundingClientRect(),b=n.lastElementChild.getBoundingClientRect();return b.left>=r.left-1&&b.right<=r.right+1}"), label
                        if key == 'store':
                            gaps = page.locator('#pluginPanel').evaluate("n=>{const a=[...n.children].map(c=>c.getBoundingClientRect());return a.slice(1).map((r,i)=>r.top-a[i].bottom)}")
                            assert len(gaps) == 2 and all(abs(g-14)<1 for g in gaps), (label, gaps)
                            cards = page.locator('.plugin-config-cards').evaluate("n=>{const a=[...n.children].map(c=>c.getBoundingClientRect());return a.slice(1).map((r,i)=>r.top-a[i].bottom)}")
                            assert all(abs(g-14)<1 for g in cards), (label, cards)
                        if key == 'agent':
                            gaps = page.locator('.mcp-server-fields').evaluate_all("ns=>ns.flatMap(n=>{const a=[...n.children].map(c=>c.getBoundingClientRect());return a.slice(1).map((r,i)=>r.top-a[i].bottom)})")
                            assert len(gaps) == 3 and all(abs(g-12)<1 for g in gaps), (label, gaps)
                            notice_gap = page.locator('#section_MCP_ button[onclick="addMcpServer(false)"]').evaluate("n=>{const a=n.parentElement;return a.getBoundingClientRect().top-a.previousElementSibling.getBoundingClientRect().bottom}")
                            assert abs(notice_gap-12)<1, (label, notice_gap)
                        if glass and theme == 'light' and ((width == 390 and key in ('webui', 'store', 'agent')) or (width in (390, 981) and key == 'providers')):
                            page.locator('#content').scroll_into_view_if_needed()
                            page.screenshot(path=str(output_dir / f'{key}-{width}.png'))
                        checked += 1
        # 使用真实导航和滚轮，避免仅测 scrollTop 赋值掩盖 overflow:hidden 禁滚。
        for glass in (False, True):
            for theme in ('light', 'dark'):
                page.evaluate("""([glass,theme])=>{state.dirty=false;state.bundle.form_values['WebUI.liquid_glass']=glass;
                  setTheme(theme);applyWebuiAppearance()}""", [glass, theme])
                for height in (480, 600):
                    page.set_viewport_size({'width': 1440, 'height': height})
                    sidebar = page.locator('.sidebar')
                    sidebar.evaluate('n=>n.scrollTop=0')
                    assert sidebar.evaluate("n=>['auto','scroll'].includes(getComputedStyle(n).overflowY)&&n.scrollHeight>n.clientHeight"), (glass, theme, height)
                    main_scroll = page.locator('.main').evaluate('n=>n.scrollTop')
                    sidebar.hover()
                    page.mouse.wheel(0, 1800)
                    page.wait_for_function("document.querySelector('.sidebar').scrollTop>0")
                    page.wait_for_function("(()=>{const s=document.querySelector('.sidebar'),n=document.querySelector('#nav .custom-nav-item:last-child');return n.getBoundingClientRect().bottom<=s.getBoundingClientRect().bottom})()")
                    assert page.locator('.main').evaluate('n=>n.scrollTop') == main_scroll
                    page.mouse.wheel(0, -1800)
                    page.wait_for_function("document.querySelector('.sidebar').scrollTop===0")
        print('PASS：桌面480/600px高度，明暗/玻璃开关下侧栏滚轮均可上下滚动，末项可达且正文不联动')
        # 同一个面板也承载加载/失败/空态和下载页，刷新后不能丢掉布局容器。
        page.evaluate("state.dirty=false;gotoPage('store')")
        page.wait_for_selector('details.plugin-config-card')
        for schemas in (None, {'error': '离线测试错误'}, {}):
            # 同步取样，避免加载态自动刷新在两次跨进程查询间替换夹具。
            snapshot = page.evaluate("""schemas=>{_pluginSchemas=schemas;refreshPluginPanel('config');
                return [getComputedStyle(document.getElementById('pluginPanel')).display,
                    document.querySelectorAll('#pluginPanel>.card').length]}""", schemas)
            assert snapshot == ['grid', 1], (schemas, snapshot)
        page.evaluate("_storeList=[];switchPluginTab('store')")
        assert page.locator('#pluginPanel>.card').count() == 1
        assert page.locator('#pluginPanel').evaluate("n=>getComputedStyle(n).display") == 'grid'
        print(f'PASS：{checked} 组页面/主题/玻璃/尺寸组合无卡片重叠或越界；插件和 MCP 间距正确；顶栏两端可达')
        print('布局截图目录：', output_dir)
        assert not errors, errors
        assert all(url.startswith("http://xcbot.test/") for url in requests)
        context.close()
        browser.close()
    print("PASS：精简侧栏、手机顶栏横向滚动、关联玻璃设置、卡片内容自适应、标签草稿；无真实接口")


if __name__ == "__main__":
    main()
