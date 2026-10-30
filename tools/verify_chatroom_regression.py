"""离线浏览器验证 3.7 聊天室功能；全部 HTTP 请求由内存夹具响应。"""
from __future__ import annotations

import copy
import re
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


# 检查控件最近父容器的内容边界及每层祖先，不能只看已被裁切的根滚动宽度。
CONTROL_BOUNDS = r"""selector=>{
  const issues=[];
  for(const n of document.querySelectorAll(selector)){
    const r=n.getBoundingClientRect();if(!r.width||!r.height||getComputedStyle(n).visibility==='hidden')continue;
    const name=n.id||n.className||n.tagName;
    for(let p=n.parentElement;p&&!p.matches('.main');p=p.parentElement){
      const b=p.getBoundingClientRect(),s=getComputedStyle(p);
      if(s.display==='contents'||!b.width)continue;
      const left=b.left+p.clientLeft+parseFloat(s.paddingLeft),right=b.left+p.clientLeft+p.clientWidth-parseFloat(s.paddingRight);
      if(r.left<left-1||r.right>right+1)issues.push([name,p.id||p.className||p.tagName,Math.round(r.left-left),Math.round(r.right-right)]);
    }
    if(!n.matches('input[type=checkbox],input[type=radio]')&&(r.width<24||r.height<20))issues.push(['控件过小',name,r.width,r.height]);
    for(const other of n.parentElement.querySelectorAll(':scope > select,:scope > input,:scope > textarea,:scope > button,:scope > label,:scope > span')){
      if(other===n)continue;const b=other.getBoundingClientRect();
      if(Math.min(r.right,b.right)-Math.max(r.left,b.left)>1&&Math.min(r.bottom,b.bottom)-Math.max(r.top,b.top)>1)issues.push(['控件重叠',name,other.id||other.tagName]);
    }
  }
  return issues;
}"""


def assert_controls(page, selector, label):
    issues = page.evaluate(CONTROL_BOUNDS, selector)
    assert not issues, (label, issues)


def assert_operable(page, selector, label):
    # 真实滚动到控件并检查命中层，避免透明玻璃层或裁切让控件无法点击。
    for control in page.locator(selector).all():
        if not control.is_visible() or not control.is_enabled():
            continue
        control.scroll_into_view_if_needed()
        try:
            control.click(trial=True, timeout=3000)
            control.focus()
            assert control.evaluate('n=>document.activeElement===n'), label
        except Exception as error:
            raise AssertionError((label, control.evaluate('n=>n.outerHTML'), str(error))) from error


def verify_layout(p, route, session, models, personas):
    output_dir = Path(tempfile.mkdtemp(prefix='xcbot-chat-layout-'))
    long_model = 'offline/' + 'LongModelWithoutSpaces' * 12
    long_persona = 'LongPersonaWithoutSpaces' * 12
    long_title = 'LongSessionWithoutSpaces' * 12
    models.append({'model': long_model, 'supports_multimodal': True})
    personas.append({'id': 'long', 'name': long_persona})
    checked = 0
    for engine in (p.chromium, p.webkit):
        browser = engine.launch(headless=True)
        mobile = ({'user_agent': p.devices['iPhone 13']['user_agent'], 'is_mobile': True, 'has_touch': True,
                   'device_scale_factor': 2} if engine.name == 'webkit' else {})
        context = browser.new_context(viewport={'width': 390, 'height': 844}, **mobile)
        context.route('**/*', route)
        context.add_init_script("localStorage.webuiToken='offline-test';")
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://xcbot.test/#/chatroom')
        page.wait_for_selector('.chatroom.ready #chatPersona:not([disabled])')
        # 只暂停动画以检查稳定几何；实际样式、完整脚本与事件均保留。
        page.add_style_tag(content='*,*::before,*::after{animation:none !important;transition:none !important}')
        for glass in (False, True):
            for theme in ('light', 'dark'):
                page.evaluate("""([glass,theme])=>{state.bundle.form_values['WebUI.liquid_glass']=glass;
                    setTheme(theme);applyWebuiAppearance()}""", [glass, theme])
                for width in (320, 390, 768, 980, 981, 1440):
                    for orientation, height in (('portrait', max(844, width+120)), ('landscape', min(600, width-80))):
                        page.set_viewport_size({'width': width, 'height': height})
                        for long_names in (False, True):
                            title = long_title if long_names else '回归测试'
                            model = long_model if long_names else 'test/model'
                            persona = 'long' if long_names else ''
                            session['title'] = title
                            page.evaluate("""title=>{chatState.session.title=title;
                                chatState.sessions[0].title=title;chatRender(false)}""", title)
                            with page.expect_response(lambda response: '/api/chat/model' in response.url and response.request.method == 'POST'):
                                page.locator('#chatModel').select_option(model)
                            page.wait_for_function('model=>chatState.session.model===model', arg=model)
                            with page.expect_response(lambda response: '/api/chat/persona' in response.url and response.request.method == 'POST'):
                                page.locator('#chatPersona').select_option(persona)
                            page.wait_for_function('persona=>chatState.session.persona===persona', arg=persona)
                            page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                            label = (engine.name, glass, theme, width, height, orientation, long_names)
                            assert_controls(page, '.chat-head select,.chat-head .pill,.chat-input textarea,.chat-input button,.sess-actions button,.chat-side>.btn', label)
                            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), label
                            assert page.locator('.chat-head>.pill').evaluate('n=>n.scrollWidth<=n.clientWidth+1'), label
                            assert page.locator('.chatroom').evaluate('n=>n.scrollWidth<=n.clientWidth+1'), label
                            if not long_names and width == 1440:
                                assert page.locator('.chat-head').evaluate('n=>{const centers=[...n.children].map(c=>{const r=c.getBoundingClientRect();return r.top+r.height/2});return Math.max(...centers)-Math.min(...centers)<1}'), label
                            assert_operable(page, '.chat-head select,#chatInput,.chat-input button,.sess-actions button,.chat-side>.btn', label)
                            draft = '离线输入' + 'NoSpaces' * 24
                            page.locator('#chatInput').fill(draft)
                            assert page.evaluate('chatState.drafts[chatState.current]') == draft, label
                            page.locator('.chat-input button[onclick="chatToggleStream()"]').click()
                            assert page.locator('#chatInput').input_value() == draft, label
                            assert_controls(page, '.chat-head select,.chat-head .pill,.chat-input textarea,.chat-input button', label)
                            assert page.locator('.sess-item .m').evaluate_all('ns=>ns.every(n=>n.scrollWidth<=n.clientWidth+1)'), label
                            page.locator('#chatInput').fill('')
                            if glass and long_names and ((width == 390 and orientation == 'portrait') or (width in (981, 1440) and orientation == 'landscape')):
                                page.locator('.chat-head').scroll_into_view_if_needed()
                                page.screenshot(path=str(output_dir / f'{engine.name}-{theme}-{width}-{orientation}.png'))
                            checked += 1
        assert not errors, (engine.name, errors)
        context.close()
        browser.close()
    print(f'PASS：{checked} 组 Chromium/WebKit 聊天室布局；长无空格模型/人格/会话、默认人格、横竖屏、明暗/玻璃开关；最近容器边界、选择/输入/焦点/点击均通过')
    print('聊天室截图目录：', output_dir)


def main():
    html = re.search(r"INDEX_HTML = r'''(.*?)'''", (ROOT / "webui.py").read_text(encoding="utf-8"), re.S).group(1)
    js = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/app.css").read_text(encoding="utf-8")
    session = {"id": "offline_session", "title": "回归测试", "model": "test/model", "persona": "p2", "messages": [
        {"id": "u1", "role": "user", "content": "失败时也保留的提问", "ts": 1, "request_id": "r1"},
        {"id": "error1", "role": "assistant", "content": "测试错误气泡", "error": True, "ts": 1, "request_id": "r1"},
        {"id": "u2", "role": "user", "content": "正常提问", "ts": 2, "request_id": "r2"},
        {"id": "a1", "role": "assistant", "content": "第一段回复", "reasoning": "本轮独立思考", "ts": 2, "request_id": "r2"},
        {"id": "a2", "role": "assistant", "content": "第二段回复", "reasoning": "本轮独立思考", "ts": 2, "request_id": "r2"},
    ]}
    bundle = {"form_values": {}, "ui_schema": [{"key": "welcome", "title": "欢迎", "fields": []}],
              "status": {}, "feature_switches": {}, "agent_tools": {}, "config_fingerprint": "offline"}
    requests = []
    models = [{"model": "test/model", "supports_multimodal": True}]
    personas = [{"id": "", "name": "跟随默认人格"}, {"id": "p2", "name": "专属人格"}]

    def route(route):
        url = route.request.url
        assert url.startswith('http://xcbot.test/'), url
        requests.append(url)
        # 所有写请求也只修改内存夹具，模型/人格选择走完整 app.js 事件链。
        if any(path in url for path in ('/api/chat/model', '/api/chat/persona')) and route.request.method == 'POST':
            posted = route.request.post_data_json
            session.update({key: posted[key] for key in ('model', 'persona') if key in posted})
            route.fulfill(json={"ok": True, "data": copy.deepcopy(session)})
            return
        if "/static/app.js" in url:
            route.fulfill(body=js, content_type="text/javascript")
        elif "/static/app.css" in url:
            route.fulfill(body=css, content_type="text/css")
        elif "/api/ui-state" in url:
            route.fulfill(json={"ok": True, "data": bundle})
        elif "/api/auth/state" in url:
            route.fulfill(json={"ok": True, "data": {"has_token": True}})
        elif "/api/chat/models" in url:
            route.fulfill(json={"ok": True, "data": models})
        elif "/api/chat/personas" in url:
            route.fulfill(json={"ok": True, "data": personas})
        elif "/api/chat/sessions" in url:
            route.fulfill(json={"ok": True, "data": [{"id": session["id"], "title": session["title"], "model": session["model"], "message_count": len(session["messages"])}]})
        elif "/api/chat/session?" in url:
            route.fulfill(json={"ok": True, "data": copy.deepcopy(session)})
        elif "/api/" in url:
            raise AssertionError("未预期的 API 请求：" + url)
        elif "/assets/" in url:
            route.fulfill(status=204, body="")
        else:
            route.fulfill(body=html, content_type="text/html; charset=utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1100, "height": 850})
        context.route("**/*", route)
        context.add_init_script("localStorage.webuiToken='offline-test';")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://xcbot.test/#/chatroom")
        page.wait_for_function("document.querySelectorAll('#chatMsgs .reason-head').length===2")
        assert page.locator("#chatPersona").input_value() == "p2"
        assert page.locator("#chatMsgs .msg.user").count() == 2
        assert page.locator("#chatMsgs .msg.assistant").count() == 3
        assert page.locator("#chatMsgs").inner_text().count("测试错误气泡") == 1
        color = page.get_by_text("测试错误气泡", exact=True).evaluate("n=>getComputedStyle(n).color")
        assert color == "rgb(248, 113, 113)", color
        page.locator("#chatMsgs .reason-head").first.click()
        page.wait_for_function("document.querySelectorAll('#chatMsgs .reason-body').length===1")
        assert page.locator("#chatMsgs .reason-body").inner_text() == "本轮独立思考"
        page.locator("#chatMsgs .reason-head").nth(1).click()
        page.wait_for_function("document.querySelectorAll('#chatMsgs .reason-body').length===2")
        page.reload()
        page.wait_for_function("document.querySelectorAll('#chatMsgs .reason-head').length===2")
        assert page.get_by_text("失败时也保留的提问", exact=True).count() == 1
        assert page.get_by_text("测试错误气泡", exact=True).count() == 1
        assert page.locator("#chatMsgs .msg.assistant").count() == 3
        assert not errors, errors
        assert all(url.startswith("http://xcbot.test/") for url in requests)
        context.close()
        browser.close()
        verify_layout(p, route, session, models, personas)
        assert all(url.startswith('http://xcbot.test/') for url in requests)
    print("PASS Chromium：会话人格、错误红泡、提问刷新保留、分段气泡、思考独立展开；无真实接口")


if __name__ == "__main__":
    main()
