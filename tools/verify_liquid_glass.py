"""液态玻璃浏览器回归：离线加载真实 CSS、SVG 和控制器，不启动机器人。

运行：python tools/verify_liquid_glass.py
需要 Playwright 的 Chromium/WebKit。WebKit 测试不是 iPhone 真机验证。
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
IOS_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"


def wallpaper(width, height, reverse=False):
    im = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(im)
    for y in range(0, height, 20):
        for x in range(0, width, 20):
            color = (30 + x * 210 // width, 40 + y * 180 // height, 60 if (x + y) // 20 % 2 else 235)
            draw.rectangle((x, y, x + 19, y + 19), fill=color[::-1] if reverse else color)
    output = io.BytesIO()
    im.save(output, "PNG")
    return output.getvalue()


def page_source():
    webui = (ROOT / "webui.py").read_text(encoding="utf-8")
    html = re.search(r"INDEX_HTML = r'''(.*?)'''", webui, re.S).group(1)
    css = (ROOT / "static/app.css").read_text(encoding="utf-8")
    js = (ROOT / "static/app.js").read_text(encoding="utf-8")
    # 起点必须包含 glassRefraction / matchMediaCoarse / narrowViewport 这些
    # 被控制器内部引用的辅助函数，否则测试页会报 ReferenceError。
    controller = js[js.index("const glassRefraction="):js.index("// 把外观缓存下来")]
    html = html.replace('<link rel="stylesheet" href="/static/app.css">', f"<style>{css}</style>")
    html = re.sub(r'<link rel="icon"[^>]*>', '', html)
    html = html.replace('<img src="/assets/icon.jpg" alt="XcBot">', '')
    setup = """
    const state={bundle:{form_values:{'WebUI.background_image':'https://webui.test/wall-a.png','WebUI.liquid_glass':true,'WebUI.background_blur':0}}};
    const token=()=>'';
    function cacheAppearanceForLogin(){}
    function toggleMobileNav(){document.body.classList.toggle('nav-open')}
    function closeMobileNav(){document.body.classList.remove('nav-open')}
    function toggleTheme(){} function saveAll(){} function gotoPage(){}
    window.clicks=0;
    document.querySelector('#content').innerHTML=`
      <div class="card" id="lensCard">
        <h3>玻璃测试</h3><div class="field" id="lensField">
        <div class="label">表单文本</div><input id="lensInput" value="可编辑文字">
        <button class="btn" id="lensButton" onclick="clicks++">裸文本按钮</button>
        <button class="btn primary" id="primaryButton" onclick="clicks++">主按钮</button>
        <span class="pill">裸文本状态</span></div>
      </div><div class="card" style="height:1500px"><div class="field">滚动内容</div></div>`;
    document.querySelector('#pageTitle').textContent='液态玻璃';
    document.querySelector('#pageDesc').textContent='';
    window.setLook=(values)=>{Object.assign(state.bundle.form_values,values);applyWebuiAppearance()};
    """
    return html.replace('<script src="/static/app.js"></script>', f"<script>{setup}\n{controller}\napplyWebuiAppearance();</script>")


def wait_frame(page):
    page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")


def alignment(page, node_id):
    return page.evaluate("""id=>{
      const n=document.getElementById(id),r=n.getBoundingClientRect(),im=n.querySelector(':scope > .glass-lens-layer image');
      const w=Number(im.getAttribute('width')),h=Number(im.getAttribute('height'));
      return {x:Number(im.getAttribute('x'))+r.left+n.clientLeft-(innerWidth-w)/2,
              y:Number(im.getAttribute('y'))+r.top+n.clientTop-(innerHeight-h)/2};
    }""", node_id)


def run_engine(pw, engine, output_dir, no_webgl=False):
    print(f'启动 {engine} 隔离浏览器{"（禁用 WebGL）" if no_webgl else ""}', flush=True)
    # 默认无头 Chromium 走 SwiftShader 软光栅，failIfMajorPerformanceCaveat 会（正确地）
    # 拒绝它，页面就落到 CSS 路径。要测 GPU 分支必须显式要求真实 GL。
    launch_opts = {}
    if engine == 'chromium' and not no_webgl:
        launch_opts['args'] = ['--use-angle=gl', '--enable-gpu']
    browser = getattr(pw, engine).launch(timeout=20000, **launch_opts)
    opts = {"viewport": {"width": 390, "height": 844}}
    if engine == "webkit":
        opts.update(user_agent=IOS_UA, is_mobile=True, has_touch=True, device_scale_factor=1)
    context = browser.new_context(**opts)
    disable_gpu = """const getContext=HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext=function(type,...args){return /webgl/.test(type)?null:getContext.call(this,type,...args)};"""
    # WebKit 始终禁用 WebGL（测它的 SVG 克隆层）；
    # Chromium 的 no_webgl 变体用来验证 CSS 回退路径。
    if engine == 'webkit' or no_webgl:
        context.add_init_script(disable_gpu)
    html = page_source()
    images = {"/wall-a.png": wallpaper(640, 960), "/wall-b.png": wallpaper(1200, 600, True)}
    requests = []

    def route_request(route):
        if route.request.url.startswith('blob:'):
            route.continue_()
            return
        path = route.request.url.split("webui.test", 1)[-1].split("?", 1)[0]
        requests.append(path)
        if path in images:
            route.fulfill(body=images[path], content_type="image/png")
        elif path == "/":
            route.fulfill(body=html, content_type="text/html; charset=utf-8")
        else:
            route.fulfill(status=404, body="测试图片不存在")

    context.route("**/*", route_request)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("https://webui.test/", wait_until="load")
    wait_frame(page)
    if engine == "chromium" and no_webgl:
        # 没有 WebGL 时 Chromium 必须无声退回原有的 CSS 折射路径：
        # 不建画布、不建 surface、不加任何降级类，filter:url() 保持可用。
        page.wait_for_function("document.documentElement.dataset.liquidGlassEngine==='css'")
        assert page.locator('.glass-gpu-layer').count() == 0
        assert page.locator('.glass-lens-layer').count() == 0
        assert not page.evaluate("document.documentElement.classList.contains('gpu-liquid-glass')")
        assert not page.evaluate("document.documentElement.classList.contains('liquid-glass-fallback')")
        assert not page.evaluate("document.documentElement.classList.contains('ios-liquid-glass')")
        before = page.eval_on_selector('#lensCard', "n=>getComputedStyle(n,'::before').filter")
        assert 'xcbot-liquid-glass' in before, before
        page.set_viewport_size({"width": 1440, "height": 900})
        wait_frame(page)
        assert page.eval_on_selector('#lensCard', "n=>getComputedStyle(n,'::before').filter") == before
        assert not errors, errors
        print('PASS Chromium（无 WebGL）：无声回退到 CSS 折射路径')
        context.close()
        browser.close()
        return

    if engine == "chromium":
        # Chromium 现在也走 WebGL 画布（GPU 引擎）；lens 控制器不再写 liquid-glass-fallback，
        # 状态由 data-liquid-glass-engine 表达。
        page.wait_for_function("document.documentElement.dataset.liquidGlassEngine==='gpu'")
        assert not page.evaluate("document.documentElement.classList.contains('ios-liquid-glass')")
        assert not page.evaluate("document.documentElement.classList.contains('webkit-liquid-glass')")
        assert not page.evaluate("document.documentElement.classList.contains('liquid-glass-fallback')")
        assert page.locator('.glass-gpu-layer').count() == 1
        # GPU 生效时 CSS 折射层必须让位，否则等于双份开销。
        assert page.eval_on_selector('#lensCard', "n=>getComputedStyle(n,'::before').display") == 'none'
        before = page.eval_on_selector('#lensCard', "n=>getComputedStyle(n,'::before').filter")
        assert 'xcbot-liquid-glass' in before, before
        page.set_viewport_size({"width": 1440, "height": 900})
        wait_frame(page)
        assert page.eval_on_selector('#lensCard', "n=>getComputedStyle(n,'::before').filter") == before
        page.screenshot(path=str(output_dir / 'chromium-desktop.png'))
        assert not errors, errors
        print('PASS Chromium：GPU 画布接管，CSS 折射层已让位，无脚本错误')
        context.close()
        browser.close()
        return

    page.wait_for_function("document.documentElement.dataset.liquidGlassState==='svg-wallpaper-refraction'")
    page.wait_for_function("document.getElementById('lensCard').classList.contains('glass-lens-surface')")
    assert requests.count('/wall-a.png') >= 1
    for node_id in ('lensCard', 'lensField', 'lensButton'):
        a = alignment(page, node_id)
        assert abs(a['x']) < .25 and abs(a['y']) < .25, (node_id, a)
    styles = page.eval_on_selector('#lensButton > .glass-lens-layer', "n=>({z:getComputedStyle(n).zIndex,position:getComputedStyle(n).position,display:getComputedStyle(n).display,scale:Number(n.querySelector('feDisplacementMap').getAttribute('scale')),units:n.querySelector('filter').getAttribute('filterUnits')})")
    assert styles['z'] == '-1' and styles['position'] == 'absolute' and styles['display'] == 'block', styles
    assert styles['scale'] == 12 and styles['units'] == 'userSpaceOnUse', styles
    assert page.eval_on_selector('#primaryButton', "n=>getComputedStyle(n,'::before').display") == 'none'
    assert page.eval_on_selector('#primaryButton', "n=>getComputedStyle(n).backgroundImage") != 'none'
    page.locator('#lensButton').click()
    page.locator('#lensInput').fill('输入正常')
    assert page.evaluate('clicks') == 1
    page.mouse.move(380, 800)
    page.wait_for_timeout(300)
    print('PASS WebKit：壁纸副本位置正确，裸文本按钮可点，输入框可编辑，主按钮保留配色')

    # 像素对比：同一张截图仅移除位移滤镜，确认不是只有 CSS 声明而无实际渲染。
    clip = page.locator('#lensCard').bounding_box()
    refracted = page.screenshot(clip=clip)
    neutral = page.add_style_tag(content='.glass-lens-layer>g{filter:none !important}')
    plain = page.screenshot(clip=clip)
    neutral.evaluate('(n)=>n.remove()')
    diff = ImageChops.difference(Image.open(io.BytesIO(refracted)).convert('RGB'), Image.open(io.BytesIO(plain)).convert('RGB'))
    rgb = diff.tobytes()
    changed = sum(1 for i in range(0, len(rgb), 3) if max(rgb[i:i + 3]) > 8)
    assert changed > 300, f'SVG 位移没有可见差异: {changed}'
    (output_dir / 'webkit-refraction.png').write_bytes(refracted)
    (output_dir / 'webkit-no-filter.png').write_bytes(plain)
    print(f'PASS WebKit：启用/关闭位移滤镜有 {changed} 个明显变化像素')

    page.eval_on_selector('.main', '(n)=>n.scrollTop=100')
    wait_frame(page)
    a = alignment(page, 'lensCard')
    assert abs(a['x']) < .25 and abs(a['y']) < .25, a
    page.evaluate("document.getElementById('lensCard').insertAdjacentHTML('beforeend','<div class=\"field\" id=\"dynamicField\">新增内容</div>')")
    page.wait_for_function("document.getElementById('dynamicField').classList.contains('glass-lens-surface')")
    page.evaluate("document.querySelector('.main').scrollTop=0")
    wait_frame(page)
    page.locator('#navToggle').click()
    page.wait_for_function("Math.abs(document.querySelector('.sidebar').getBoundingClientRect().x)<1")
    wait_frame(page)
    a = page.eval_on_selector('.sidebar', "n=>{const r=n.getBoundingClientRect();return {x:r.x,z:getComputedStyle(n).zIndex,lens:n.classList.contains('glass-lens-surface')}}")
    assert abs(a['x']) < 1 and int(a['z']) > 55 and a['lens'], a
    page.eval_on_selector('.sidebar', "n=>{const d=document.createElement('div');d.style.height='1500px';d.textContent='侧栏滚动';n.appendChild(d)}")
    page.eval_on_selector('.sidebar', '(n)=>n.scrollTop=160')
    wait_frame(page)
    assert page.eval_on_selector('.sidebar > .glass-lens-layer', 'n=>parseFloat(n.style.top)') == 160
    page.locator('#navScrim').click(position={"x": 380, "y": 400})
    page.wait_for_timeout(350)
    print('PASS WebKit：滚动、动态插入、抽屉动画后仍对齐且导航可用')

    page.evaluate("setLook({'WebUI.background_image':'https://webui.test/wall-b.png','WebUI.background_blur':10})")
    page.wait_for_function("document.documentElement.dataset.liquidGlassState==='svg-wallpaper-refraction'&&Number(document.querySelector('#lensCard > .glass-lens-layer image')?.getAttribute('width'))>1600")
    assert requests.count('/wall-b.png') >= 1
    a = alignment(page, 'lensCard')
    assert abs(a['x']) < .25 and abs(a['y']) < .25, a
    page.evaluate("setLook({'WebUI.background_image':'https://webui.test/missing.png'})")
    page.wait_for_function("document.documentElement.dataset.liquidGlassState==='fallback-http-404'")
    failures_before = len(requests)
    page.evaluate('applyWebuiAppearance();applyWebuiAppearance();')
    wait_frame(page)
    assert page.evaluate("document.documentElement.dataset.liquidGlassState") == 'fallback-http-404'
    assert len(requests) == failures_before, '失败状态不应随每次 render 重发请求'
    assert not page.evaluate("document.documentElement.classList.contains('ios-liquid-glass')")
    page.evaluate("setLook({'WebUI.background_image':''})")
    assert page.evaluate("document.documentElement.dataset.liquidGlassState") == 'fallback-no-wallpaper'
    fallback = page.eval_on_selector('#lensCard', "n=>{const s=getComputedStyle(n);return s.backdropFilter||s.webkitBackdropFilter}")
    assert 'blur' in fallback, fallback
    assert page.eval_on_selector('#lensCard', "n=>getComputedStyle(n,'::before').display") == 'none'
    assert page.evaluate("getComputedStyle(document.body,'::before').backgroundImage") == 'none'
    page.evaluate("setLook({'WebUI.background_image':'https://webui.test/wall-a.png','WebUI.background_blur':0})")
    page.wait_for_function("document.documentElement.dataset.liquidGlassState==='svg-wallpaper-refraction'")
    page.evaluate("setLook({'WebUI.liquid_glass':false})")
    assert not page.evaluate("document.documentElement.classList.contains('webkit-liquid-glass')")
    assert page.evaluate("document.querySelectorAll('.glass-lens-surface').length") == 0
    assert page.locator('.glass-lens-layer').count() == 0
    page.evaluate("setLook({'WebUI.liquid_glass':true})")
    page.wait_for_function("document.getElementById('lensCard').classList.contains('glass-lens-surface')")
    page.set_viewport_size({'width': 844, 'height': 390})
    wait_frame(page)
    a = alignment(page, 'lensCard')
    assert abs(a['x']) < .25 and abs(a['y']) < .25, a
    page.evaluate("document.documentElement.dataset.theme='dark'")
    wait_frame(page)
    assert not errors, errors
    print('PASS WebKit：换图/模糊、失败降级、无网格、开关清理、横屏对齐全部通过')
    context.close()

    # 延迟回调用可控 Image 模拟，实际 CSS/渲染已在上面使用真实图片验证。
    context = browser.new_context(**opts)
    context.add_init_script(disable_gpu)
    context.route('**/*', route_request)
    context.add_init_script("""
      window.pendingImages=[];
      window.Image=class {
        constructor(){this.naturalWidth=640;this.naturalHeight=960;pendingImages.push(this)}
        set src(value){this.url=value}
      };
    """)
    page = context.new_page()
    page.goto('https://webui.test/')
    page.wait_for_function('pendingImages.length===1')
    page.evaluate('applyWebuiAppearance();applyWebuiAppearance()')
    assert page.evaluate('pendingImages.length') == 1
    page.evaluate("setLook({'WebUI.background_image':'https://webui.test/wall-b.png'})")
    page.wait_for_function('pendingImages.length===2')
    page.evaluate('pendingImages[0].onload()')
    assert page.evaluate("document.documentElement.dataset.liquidGlassState") == 'fallback-loading'
    page.evaluate('pendingImages[1].onload()')
    wait_frame(page)
    assert page.evaluate("document.documentElement.dataset.liquidGlassState") == 'svg-wallpaper-refraction'
    page.evaluate("setLook({'WebUI.background_image':'https://webui.test/wall-a.png'})")
    page.wait_for_function('pendingImages.length===3')
    page.evaluate("setLook({'WebUI.liquid_glass':false});pendingImages[2].onload()")
    wait_frame(page)
    assert page.evaluate("document.documentElement.dataset.liquidGlassState") == 'off'
    assert not page.evaluate("document.documentElement.classList.contains('ios-liquid-glass')")
    print('PASS WebKit：重复 render 不重复加载、过期图片和关闭后的回调无效')
    context.close()

    # 仿真仅前缀 blur、没有 SVG 资源两种分支，不把新版 WebKit 当旧 iOS 真机。
    context = browser.new_context(**opts)
    context.add_init_script(disable_gpu)
    context.route('**/*', route_request)
    context.add_init_script("""
      const supports=CSS.supports.bind(CSS);
      CSS.supports=(name,value)=>name==='backdrop-filter'?false:name==='-webkit-backdrop-filter'?true:supports(name,value);
    """)
    page = context.new_page()
    page.goto('https://webui.test/')
    page.wait_for_function("document.documentElement.dataset.liquidGlassState==='svg-wallpaper-refraction'")
    page.evaluate("setLook({'WebUI.background_image':''})")
    assert page.evaluate("document.documentElement.dataset.liquidGlassState") == 'fallback-no-wallpaper'
    context.close()

    context = browser.new_context(**opts)
    context.add_init_script(disable_gpu)
    context.route('**/*', route_request)
    page = context.new_page()
    blank_html = page_source().replace('applyWebuiAppearance();</script>', '</script>')
    page.set_content(blank_html)
    assert page.evaluate("getComputedStyle(document.body,'::before').backgroundImage") == 'none'
    assert page.evaluate("getComputedStyle(document.body,'::before').content") == 'none'
    page.evaluate("document.getElementById('xcbot-liquid-map').remove();applyWebuiAppearance()")
    assert page.evaluate("document.documentElement.dataset.liquidGlassState") == 'fallback-no-map'
    assert not page.evaluate("document.documentElement.classList.contains('ios-liquid-glass')")
    assert page.evaluate("document.querySelectorAll('.glass-lens-surface').length") == 0
    print('PASS WebKit：前缀支持、SVG 缺失降级、配置加载前无网格')
    context.close()
    browser.close()


def test_full_webkit(pw, output_dir):
    """完整脚本/外部 CSS/hash 路由/iOS Edge UA/DPR3，非控制器摘录。"""
    browser = pw.webkit.launch(timeout=20000)
    context = browser.new_context(viewport={'width': 390, 'height': 659}, user_agent=IOS_UA.replace('18_0', '16_2_0').replace('Safari/604.1', 'EdgiOS/137.0 Safari/604.1'), is_mobile=True, has_touch=True, device_scale_factor=3)
    html = re.search(r"INDEX_HTML = r'''(.*?)'''", (ROOT / 'webui.py').read_text(encoding='utf-8'), re.S).group(1)
    html = html.replace('/static/app.js"', '/static/app.js?v=full-test"').replace('/static/app.css"', '/static/app.css?v=full-test"')
    js = (ROOT / 'static/app.js').read_text(encoding='utf-8')
    css = (ROOT / 'static/app.css').read_text(encoding='utf-8')
    fields = [{'path': 'WebUI.background_image', 'label': '自定义背景图片', 'type': 'background_image'},
              {'path': 'WebUI.background_blur', 'label': '背景模糊度', 'type': 'number'},
              {'path': 'WebUI.liquid_glass', 'label': '液态玻璃UI', 'type': 'bool'}]
    bundle = {'form_values': {'WebUI.background_image': 'data/webui_bg/test.png', 'WebUI.background_blur': 0, 'WebUI.liquid_glass': True},
              'ui_schema': [{'key': 'webui', 'title': 'WebUI', 'desc': '外观', 'fields': fields}], 'status': {}, 'feature_switches': {}, 'config_fingerprint': 'test'}
    raw_image = wallpaper(640, 960)
    def route_request(route):
        u = route.request.url
        if u.startswith('blob:'):
            route.continue_()
        elif '/static/app.js' in u:
            route.fulfill(body=js, content_type='text/javascript')
        elif '/static/app.css' in u:
            route.fulfill(body=css, content_type='text/css')
        elif '/api/ui-state' in u:
            route.fulfill(json={'ok': True, 'data': bundle})
        elif '/api/auth/state' in u:
            route.fulfill(json={'ok': True, 'data': {'has_token': True}})
        elif '/api/webui/background' in u or '/assets/' in u:
            route.fulfill(body=b'broken-png' if 'broken.png' in u else raw_image, content_type='image/png')
        else:
            route.fulfill(body=html, content_type='text/html')
    context.route('**/*', route_request)
    context.add_init_script("""
      localStorage.webuiToken='sensitive-test-token';
      window.revokedBlobs=[];const revoke=URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL=u=>{revokedBlobs.push(u);revoke(u)};
      window.neutralGlass=false;
      const strengths=new WeakSet(),getLocation=WebGLRenderingContext.prototype.getUniformLocation;
      WebGLRenderingContext.prototype.getUniformLocation=function(p,name){const loc=getLocation.call(this,p,name);if(loc&&name==='u_strength')strengths.add(loc);return loc};
      const setFloat=WebGLRenderingContext.prototype.uniform1f;
      WebGLRenderingContext.prototype.uniform1f=function(loc,value){
        if(window.neutralGlass&&strengths.has(loc))value=0;
        return setFloat.call(this,loc,value);
      };
    """)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto('http://webui.test/#/webui')
    page.wait_for_function("document.documentElement.dataset.liquidGlassState==='gpu-wallpaper-refraction'")
    page.wait_for_function("!document.getAnimations().some(a=>a.playState==='running'&&a.effect?.target?.matches('.card'))")
    # 「查看玻璃状态」按钮已移除，直接调用诊断函数取报告。
    report = page.evaluate('showLiquidGlassStatus(), JSON.parse(document.getElementById("glassStatusDialog").querySelector("textarea").value)')
    report_text = json.dumps(report, ensure_ascii=False)
    assert 'sensitive-test-token' not in report_text and '/api/' not in report_text and 'blob:' not in report_text, report_text
    assert report['renderer'] == report['cssRenderer'] == 'fixed-webgl-v2', report
    assert report['mode'] == 'gpu-wallpaper-refraction' and report['sourceType'] == 'same-origin-blob'
    assert report['resources'] == {'app.css': 'full-test', 'app.js': 'full-test'}, report
    assert report['layers'] == 1 and report['gpu']['textureUploads'] == 1
    assert report['gpu']['refractionModel'] == 'xcbot-sdf-v1'
    assert report['gpu']['strengthProfile'] == 'pc-base-scale-40-soft-35%'
    assert report['sample']['position'] == 'fixed'
    assert report['gpu']['resolution'] == [780, 1318]
    page.evaluate('document.getElementById("glassStatusDialog").remove()')
    page.mouse.move(385, 630)
    page.eval_on_selector('.main', '(n)=>n.scrollTop=0')
    wait_frame(page)
    for selector in ('.topbar', '#content .field'):
        clip = page.locator(selector).first.bounding_box()
        before = page.screenshot(clip=clip)
        page.evaluate('window.neutralGlass=true;dispatchEvent(new Event("resize"))')
        page.wait_for_timeout(150)
        wait_frame(page)
        after = page.screenshot(clip=clip)
        page.evaluate('window.neutralGlass=false;dispatchEvent(new Event("resize"))')
        page.wait_for_timeout(150)
        wait_frame(page)
        diff = ImageChops.difference(Image.open(io.BytesIO(before)).convert('RGB'), Image.open(io.BytesIO(after)).convert('RGB')).tobytes()
        changed = sum(1 for i in range(0, len(diff), 3) if max(diff[i:i+3])>8)
        assert changed > 300, (selector, changed)
    page.screenshot(path=str(output_dir / 'webkit-gpu-edge.png'))
    for _ in range(3):
        page.evaluate('render()')
        wait_frame(page)
    assert page.locator('.glass-lens-layer').count() == 0
    assert page.locator('.glass-gpu-layer').count() == 1
    metrics = page.evaluate('''async()=>{
      const m=document.querySelector('.main');const frames=[];
      for(let i=0;i<65;i++){
        m.scrollTop=i*6;
        await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
        const c=document.querySelector('.glass-gpu-layer').getBoundingClientRect();
        frames.push({x:c.x,y:c.y,uploads:liquidGlassLensController.diagnostics().gpu.textureUploads});
      }
      return frames;
    }''')
    assert all(m['x']==0 and m['y']==0 and m['uploads']==1 for m in metrics), metrics
    page.wait_for_timeout(450)
    idle = page.evaluate('liquidGlassLensController.diagnostics().gpu.frames')
    page.wait_for_timeout(350)
    assert page.evaluate('liquidGlassLensController.diagnostics().gpu.frames') == idle
    assert page.evaluate("document.querySelector('.glass-gpu-layer').getContext('webgl').getError()") == 0
    print('PASS GPU：65步连续滚动固定画布位置不动，纹理仍只上传1次，闲置停止绘制')
    # 背景模糊是烘焙进纹理的（CPU 侧高斯），改值必须重建纹理才看得到效果。
    # 旧实现靠 shader 实时采样，这里断言「仍然是 1 次」——那正是模糊值
    # 不生效的根源，所以修复后这条要反过来断言确实重建了。
    uploads_before = page.evaluate('liquidGlassLensController.diagnostics().gpu.textureUploads')
    page.evaluate("setValue('WebUI.background_blur',10)")
    wait_frame(page)
    uploads_after = page.evaluate('liquidGlassLensController.diagnostics().gpu.textureUploads')
    assert uploads_after == uploads_before + 1, (uploads_before, uploads_after)
    page.set_viewport_size({'width': 844, 'height': 390})
    wait_frame(page)
    assert page.evaluate('liquidGlassLensController.diagnostics().gpu.resolution') == [1688,780]
    page.set_viewport_size({'width': 390, 'height': 659})
    page.evaluate("document.querySelector('.main').scrollTop=0")
    page.locator('#navToggle').click()
    page.wait_for_function("Math.abs(document.querySelector('.sidebar').getBoundingClientRect().x)<1")
    page.locator('#navScrim').click(position={'x':380,'y':400})
    page.evaluate("document.querySelector('.glass-gpu-layer').dispatchEvent(new Event('webglcontextlost',{cancelable:true}))")
    page.wait_for_function("document.documentElement.dataset.liquidGlassState==='svg-wallpaper-refraction'")
    assert page.locator('.glass-gpu-layer').count() == 0
    page.wait_for_function("document.querySelectorAll('.glass-lens-layer').length>0")
    assert page.evaluate('liquidGlassLensController.diagnostics().gpuFallbackReason') == 'context-lost'
    page.evaluate("setValue('WebUI.liquid_glass',false)")
    assert page.locator('.glass-lens-layer,.glass-gpu-layer').count() == 0
    assert page.evaluate('revokedBlobs.length') >= 1
    page.evaluate("setValue('WebUI.liquid_glass',true)")
    page.wait_for_function("document.documentElement.dataset.liquidGlassState==='gpu-wallpaper-refraction'")
    revoked = page.evaluate('revokedBlobs.length')
    page.evaluate("setValue('WebUI.background_image','data/webui_bg/broken.png')")
    page.wait_for_function("document.documentElement.dataset.liquidGlassState==='fallback-image-error'")
    assert page.evaluate('revokedBlobs.length') >= revoked + 2
    assert page.evaluate('liquidGlassLensController.diagnostics().sourceType') == 'none'
    assert page.locator('.glass-gpu-layer').count() == 0
    assert not errors, errors
    print('PASS 完整 WebKit 页面：GPU折射有像素差异，DPR上限2、诊断脱敏、旋转/丢失上下文SVG后备/重绘/Blob清理正常')
    context.close()
    browser.close()


def test_gpu_pixels(pw):
    """用精确像素核对方向/透明度/祖先裁剪，不能只看滤镜开关有差异。"""
    browser = pw.webkit.launch(timeout=20000)
    context = browser.new_context(viewport={'width': 200, 'height': 200})
    page = context.new_page()
    page.set_content('<html><body style="margin:0"></body></html>')
    source = (ROOT / 'static/app.js').read_text(encoding='utf-8')
    page.add_script_tag(content=source[source.index('function createLiquidGlassGpu'):source.index('const liquidGlassLensController=')])
    result = page.evaluate('''async()=>{
      const c=document.createElement('canvas');c.width=100;c.height=100;const x=c.getContext('2d');
      x.fillStyle='red';x.fillRect(0,0,100,50);x.fillStyle='blue';x.fillRect(0,50,100,50);x.clearRect(40,40,20,20);
      const image=new Image();image.src=c.toDataURL();await image.decode();
      const gpu=createLiquidGlassGpu(image,()=>{});
      const rect={rect:[0,0,200,200],radii:[0,0,0,0],clipBox:[0,0,200,200],clips:[],soft:true};
      const geometry={width:200,height:200,cover:[0,0,200,200],blur:0,base:[1,1,1]};
      const gl=gpu.canvas.getContext('webgl');
      function pixel(x,y){let p=new Uint8Array(4);gl.readPixels(x,199-y,1,1,gl.RGBA,gl.UNSIGNED_BYTE,p);return [...p]}
      gpu.draw([rect],geometry);const first={top:pixel(100,20),bottom:pixel(100,180),clear:pixel(100,100)};
      gpu.draw([{...rect,clips:[{rect:[20,20,160,160],radii:[30,30,30,30]}]}],geometry);
      first.outside=pixel(10,100);first.corner=pixel(21,21);first.inside=pixel(100,30);first.uploads=gpu.diagnostics().textureUploads;
      gpu.dispose();return first;
    }''')
    assert result['top'][0] > 240 and result['top'][2] < 90, result
    assert result['bottom'][2] > 240 and result['bottom'][0] < 90, result
    assert min(result['clear'][:3]) > 240, result
    assert result['outside'][3] == result['corner'][3] == 0, result
    assert result['inside'][3] > 240 and result['uploads'] == 1, result
    print('PASS GPU像素：上下方向、透明壁纸、祖先圆角裁剪与单纹理正确')
    context.close()
    browser.close()


def test_background_endpoint():
    # 仅加载待测函数，避免 import webui 触发项目初始化或读取真实配置。
    tree = ast.parse((ROOT / 'webui.py').read_text(encoding='utf-8'))
    names = {'save_uploaded_background', '_configured_background_image_response', '_json_response'}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in functions:
        node.returns = None
        for arg in node.args.args:
            arg.annotation = None
    with tempfile.TemporaryDirectory(prefix='xcbot-background-') as temp:
        base = Path(temp)
        config = base / 'config.json'
        namespace = dict(Path=Path, hashlib=hashlib, json=json, re=re, os=os, BASE_DIR=base,
                         CONFIG_PATH=config, BACKGROUND_UPLOAD_DIR=base / 'data/webui_bg',
                         MAX_BACKGROUND_BYTES=12 * 1024 * 1024, BACKGROUND_EXTS={'.png': 'image/png'},
                         read_json=lambda path, default: json.loads(path.read_text(encoding='utf-8')))
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'background-under-test', 'exec'), namespace)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                namespace['_configured_background_image_response'](self)

            def log_message(self, *args):
                pass

        def upload(raw):
            path = namespace['save_uploaded_background']('test.png', raw)
            config.write_text(json.dumps({'WebUI': {'background_image': path}}), encoding='utf-8')
            return path

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            first = wallpaper(160, 240)
            path_a = upload(first)
            url = f'http://127.0.0.1:{server.server_port}/api/webui/background?v={path_a}'
            with opener.open(url) as response:
                assert response.read() == first
                etag = response.headers['ETag']
                assert response.headers['Cache-Control'] == 'no-cache'
            second = wallpaper(240, 120, True)
            path_b = upload(second)
            assert not (base / path_a).exists() and (base / path_b).exists()
            # 同一服务进程、旧 URL/ETag、独立请求代表第二个设备，无需重启。
            request = urllib.request.Request(url, headers={'If-None-Match': etag})
            with opener.open(request) as response:
                assert response.read() == second
                assert response.headers['ETag'] != etag
                etag_b = response.headers['ETag']
            try:
                opener.open(urllib.request.Request(url, headers={'If-None-Match': etag_b}))
                raise AssertionError('相同 ETag 应返回 304')
            except urllib.error.HTTPError as error:
                assert error.code == 304, error.code
            print('PASS 背景接口：上传落盘、旧路径读取新图、无需重启、ETag 304')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def main():
    test_background_endpoint()
    output_dir = Path(tempfile.mkdtemp(prefix='xcbot-glass-'))
    with sync_playwright() as pw:
        for engine in ('chromium', 'webkit'):
            run_engine(pw, engine, output_dir)
        run_engine(pw, 'chromium', output_dir, no_webgl=True)
        test_full_webkit(pw, output_dir)
        test_gpu_pixels(pw)
    print('截图目录：', output_dir)
    print('说明：以上为 Playwright WebKit/Chromium；尚未代表 iPhone 真机已通过。')


if __name__ == '__main__':
    main()
