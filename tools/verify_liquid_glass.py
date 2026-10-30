"""液态玻璃浏览器回归：离线加载真实 CSS、SVG 和控制器，不启动机器人。

运行：python tools/verify_liquid_glass.py
需要 Playwright 的 Chromium/WebKit。WebKit 测试不是 iPhone 真机验证。
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat
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
    // 控制器摘录不包含缓存函数；完整脚本用例仍使用真实实现。
    function saveAppearanceCache(){}
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
      </div><div class="card" id="longCard" style="height:1500px"><div class="field">滚动内容</div></div>`;
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


def check_long_card(page, label, output_dir):
    """只比较长卡片无文字/子控件的空白区，防止子 field 掩盖父玻璃缺失。"""
    page.evaluate("setLook({'WebUI.background_blur':10,'WebUI.disable_background_blur':true,'WebUI.card_background_blur':3})")
    assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--user-bg-blur').trim()") == '0px'
    for theme in ('light', 'dark'):
        page.evaluate("theme=>document.documentElement.dataset.theme=theme", theme)
        for offset in (0, 320):
            page.evaluate("""offset=>{const m=document.querySelector('.main'),c=document.getElementById('longCard');
              m.scrollTop+=c.getBoundingClientRect().top-m.getBoundingClientRect().top-160+offset}""", offset)
            wait_frame(page)
            clip = {'x': 60, 'y': 430, 'width': 260, 'height': 100}
            assert page.evaluate("""()=>{const c=document.getElementById('longCard'),r=c.getBoundingClientRect();
              return r.height>innerHeight&&r.left<60&&r.right>320&&r.top<430&&r.bottom>530}""")
            before = page.screenshot(clip=clip)
            if page.evaluate("document.documentElement.dataset.liquidGlassEngine==='gpu'"):
                # 不靠边框/主题色判定：无绘制的旧实现此处 alpha 为 0。
                alpha = page.evaluate("""()=>{const c=document.querySelector('.glass-gpu-layer'),g=c.getContext('webgl'),p=new Uint8Array(4);
                  g.readPixels(Math.floor(190*c.width/innerWidth),c.height-1-Math.floor(480*c.height/innerHeight),1,1,g.RGBA,g.UNSIGNED_BYTE,p);return p[3]}""")
                assert alpha > 240, (label, theme, offset, alpha)
            neutral = page.add_style_tag(content='''
                .glass-gpu-layer{visibility:hidden !important}
                html.liquid-glass #longCard>.glass-lens-layer{visibility:hidden !important}
                html.liquid-glass #longCard::before{display:none !important}
            ''')
            after = page.screenshot(clip=clip)
            neutral.evaluate('(n)=>n.remove()')
            diff = ImageChops.difference(Image.open(io.BytesIO(before)).convert('RGB'), Image.open(io.BytesIO(after)).convert('RGB')).tobytes()
            changed = sum(1 for i in range(0, len(diff), 3) if max(diff[i:i+3]) > 8)
            assert changed > 300, (label, theme, offset, changed)
        page.screenshot(path=str(output_dir / f'{label}-long-card-{theme}.png'))
    page.evaluate("setLook({'WebUI.background_blur':0,'WebUI.disable_background_blur':false});document.documentElement.dataset.theme='dark';document.querySelector('.main').scrollTop=0")
    wait_frame(page)
    print(f'PASS {label}：超视口大卡片空白区有真实玻璃，明暗主题/滚动前后均有效')


def check_topbar_content_blur(page, label, output_dir):
    """验证实际正文被模糊，而不只是壁纸纹理有模糊。"""
    page.evaluate("""()=>{const n=document.createElement('div');n.id='headerBlurProbe';
      n.style.cssText='grid-column:1/-1;height:500px;background:repeating-linear-gradient(90deg,#000 0 4px,#fff 4px 8px)';
      document.getElementById('content').prepend(n);
      const m=document.querySelector('.main');m.scrollTop+=n.getBoundingClientRect().top-m.getBoundingClientRect().top;}""")
    pixel_blur_supported = True
    if label == 'webkit-svg':
        # Windows Playwright WebKit 可能解析 backdrop-filter，却不执行像素模糊。
        probe = page.context.new_page()
        probe.set_content('''<div style="position:absolute;inset:0;background:repeating-linear-gradient(90deg,#000 0 4px,#fff 4px 8px)"></div>
          <div id="blur" style="position:absolute;inset:40px;background:rgba(255,255,255,.1);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px)"></div>''')
        a = probe.screenshot()
        probe.locator('#blur').evaluate("n=>{n.style.backdropFilter='none';n.style.webkitBackdropFilter='none'}")
        b = probe.screenshot()
        pixel_blur_supported = ImageChops.difference(Image.open(io.BytesIO(a)).convert('RGB'), Image.open(io.BytesIO(b)).convert('RGB')).getbbox() is not None
        probe.close()
        if not pixel_blur_supported:
            print('LIMIT WebKit：极简页面也不执行 backdrop 像素模糊；本机仅验证双前缀计算样式，真实 Safari 视觉效果待真机确认')
    for theme in ('light', 'dark'):
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        wait_frame(page)
        assert page.locator('.topbar').evaluate("n=>(getComputedStyle(n).backdropFilter||getComputedStyle(n).webkitBackdropFilter).includes('blur(20px)')"), (label, theme)
        clip = page.locator('.topbar').bounding_box()
        assert page.locator('#headerBlurProbe').bounding_box()['y'] < clip['y'] + 2
        blurred = page.screenshot(clip=clip)
        neutral = page.add_style_tag(content='html.liquid-glass[data-theme] .topbar{backdrop-filter:none !important;-webkit-backdrop-filter:none !important}')
        sharp = page.screenshot(clip=clip)
        neutral.evaluate('(n)=>n.remove()')
        diff = ImageChops.difference(Image.open(io.BytesIO(blurred)).convert('RGB'), Image.open(io.BytesIO(sharp)).convert('RGB')).tobytes()
        changed = sum(1 for i in range(0, len(diff), 3) if max(diff[i:i+3]) > 8)
        (output_dir / f'{label}-header-blur-{theme}.png').write_bytes(blurred)
        (output_dir / f'{label}-header-sharp-{theme}.png').write_bytes(sharp)
        if pixel_blur_supported:
            assert changed > 1000, (label, theme, changed, str(output_dir))
    # 980px 是抽屉布局边界，981px 起不应继承移动端新增的顶栏模糊。
    for width in (980, 981, 1440):
        page.set_viewport_size({'width': width, 'height': 844})
        for theme in ('light', 'dark'):
            page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
            actual = page.locator('.topbar').evaluate("n=>getComputedStyle(n).backdropFilter||getComputedStyle(n).webkitBackdropFilter")
            assert (actual == 'none' if width > 980 else 'blur(20px)' in actual), (label, width, theme, actual)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.evaluate("document.getElementById('headerBlurProbe').remove();document.documentElement.dataset.theme='dark';document.querySelector('.main').scrollTop=0")
    wait_frame(page)
    if pixel_blur_supported:
        print(f'PASS {label}：明暗顶栏均真实模糊滚过的正文，保留前景文字和折射层')


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
        check_topbar_content_blur(page, 'chromium-css' if no_webgl else 'chromium-gpu', output_dir)
        check_long_card(page, 'chromium-css' if no_webgl else 'chromium-gpu', output_dir)
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
        page.evaluate("""()=>{const n=document.getElementById('lensCard');n.classList.add('glass-lens-surface');
          const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('glass-lens-layer');n.appendChild(svg)}""")
        assert page.eval_on_selector('#lensCard>.glass-lens-layer', "n=>getComputedStyle(n).display") == 'none'
        page.evaluate("document.querySelector('#lensCard>.glass-lens-layer').remove();document.getElementById('lensCard').classList.remove('glass-lens-surface')")
        # GPU 生效时 CSS 折射层必须让位，否则等于双份开销。
        assert page.eval_on_selector('#lensCard', "n=>getComputedStyle(n,'::before').display") == 'none'
        before = page.eval_on_selector('#lensCard', "n=>getComputedStyle(n,'::before').filter")
        assert 'xcbot-liquid-glass' in before, before
        check_topbar_content_blur(page, 'chromium-css' if no_webgl else 'chromium-gpu', output_dir)
        check_long_card(page, 'chromium-css' if no_webgl else 'chromium-gpu', output_dir)
        check_long_card_comparison(page, output_dir)
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
    strength = max(0.0, min(30.0, float(page.evaluate("state.bundle.form_values['WebUI.liquid_glass_refraction'] ?? 1"))))
    curved = strength if strength <= 10 else 10 + math.sqrt(strength - 10) * 3
    expected_scale = max(1, min(120, curved * 8) * 0.3)
    assert math.isclose(styles['scale'], expected_scale, rel_tol=1e-9, abs_tol=1e-9), (styles, expected_scale)
    assert styles['units'] == 'userSpaceOnUse', styles
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
    check_topbar_content_blur(page, 'webkit-svg', output_dir)
    check_long_card(page, 'webkit-svg', output_dir)

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
    assert abs(a['x']) < 1 and int(a['z']) > 55 and not a['lens'], a
    assert page.locator('.sidebar .glass-lens-layer').count() == 0
    page.eval_on_selector('.sidebar', "n=>{const d=document.createElement('div');d.style.height='1500px';d.textContent='侧栏滚动';n.appendChild(d)}")
    page.eval_on_selector('.sidebar', '(n)=>n.scrollTop=160')
    wait_frame(page)
    assert page.eval_on_selector('.sidebar', 'n=>n.scrollTop') == 160
    assert page.locator('.sidebar .glass-lens-layer').count() == 0
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
    assert page.evaluate("document.documentElement.dataset.liquidGlassState") == 'fallback-no-webgl'
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
    assert page.evaluate("document.documentElement.dataset.liquidGlassState") == 'fallback-no-webgl'
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
              {'path': 'WebUI.liquid_glass', 'label': '液态玻璃UI', 'type': 'bool'},
              {'path': 'WebUI.disable_background_blur', 'label': '禁用背景模糊', 'type': 'bool'},
              {'path': 'WebUI.liquid_glass_refraction', 'label': '折射强度', 'type': 'range', 'min': 0, 'max': 30},
              {'path': 'WebUI.card_background_blur', 'label': '卡片背景模糊度', 'type': 'range', 'min': 0, 'max': 10}]
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
    assert page.locator('#section_webui').bounding_box()['height'] > 659
    page.locator('.glass-settings-family>.section-head').scroll_into_view_if_needed()
    wait_frame(page)
    clip = page.locator('.glass-settings-family>.section-head').bounding_box()
    # 此处只有透明分组和文字，没有 field 子表面；只能由 WebUI 大卡片提供玻璃。
    before = page.screenshot(clip=clip)
    neutral = page.add_style_tag(content='.glass-gpu-layer{visibility:hidden !important}')
    after = page.screenshot(clip=clip)
    neutral.evaluate('(n)=>n.remove()')
    diff = ImageChops.difference(Image.open(io.BytesIO(before)).convert('RGB'), Image.open(io.BytesIO(after)).convert('RGB')).tobytes()
    changed = sum(1 for i in range(0, len(diff), 3) if max(diff[i:i+3]) > 8)
    assert changed > 300, ('WebUI 外层卡片', changed)
    page.screenshot(path=str(output_dir / 'webkit-gpu-webui-long-card.png'))
    page.evaluate("document.querySelector('.main').scrollTop=0")
    wait_frame(page)
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
    wait_frame(page)  # cover 恢复后先完成有效 sigma 的纹理更新，再记录抽屉测试基线。
    # 使用真实 setMobileNav 开关；读取抽屉覆盖区域的 GPU 像素，不能只看画布存在。
    uploads = page.evaluate('liquidGlassLensController.diagnostics().gpu.textureUploads')
    def behind_drawer_pixels():
        return page.evaluate("""()=>{const c=document.querySelector('.glass-gpu-layer'),g=c.getContext('webgl');
          const sx=c.width/innerWidth,sy=c.height/innerHeight,w=Math.floor(140*sx),h=Math.floor(140*sy),p=new Uint8Array(w*h*4);
          g.readPixels(Math.floor(60*sx),c.height-Math.floor(460*sy),w,h,g.RGBA,g.UNSIGNED_BYTE,p);return Array.from(p)}""")
    for theme in ('light', 'dark'):
        page.evaluate('theme=>setTheme(theme)', theme)
        for scroll in (0, 120):
            page.eval_on_selector('.main', '(n,y)=>n.scrollTop=y', scroll)
            page.locator('#navToggle').click()
            page.wait_for_function("Math.abs(document.querySelector('.sidebar').getBoundingClientRect().x)<1")
            page.wait_for_timeout(650)  # 覆盖原先只在稳定态触发的硬裁剪。
            wait_frame(page)
            before = behind_drawer_pixels()
            assert sum(a>240 for a in before[3::4]) > len(before)//4*.95, (theme, scroll, '抽屉后方玻璃透明')
            page.evaluate('window.neutralGlass=true;dispatchEvent(new Event("resize"))')
            wait_frame(page)
            after = behind_drawer_pixels()
            changed = sum(max(abs(before[i+j]-after[i+j]) for j in range(3))>1 for i in range(0,len(before),4))
            assert changed > 300, (theme, scroll, '抽屉后方无折射', changed)
            page.evaluate('window.neutralGlass=false;dispatchEvent(new Event("resize"))')
            wait_frame(page)
            page.screenshot(path=str(output_dir / f'drawer-behind-{theme}-{scroll}.png'))
            assert page.locator('.glass-gpu-layer').count() == 1
            assert page.evaluate('liquidGlassLensController.diagnostics().gpu.textureUploads') == uploads
            page.locator('#navScrim').click(position={'x':380,'y':400})
            page.wait_for_timeout(450)
            assert sum(a>240 for a in behind_drawer_pixels()[3::4]) > len(before)//4*.95
    page.evaluate('setMobileNav(true);setMobileNav(false);setMobileNav(true)')
    page.wait_for_timeout(650)
    assert sum(a>240 for a in behind_drawer_pixels()[3::4]) > len(before)//4*.95
    page.evaluate('setMobileNav(false)')
    page.wait_for_timeout(450)
    print('PASS 抽屉：明暗/滚动前后/快速反转，覆盖区卡片玻璃和折射持续存在；单画布且纹理不重复上传')
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


def test_long_displacement_pixels(pw):
    """实际执行同一 shader，只将输出换成位移编码，直接读 CSS 像素向量。"""
    browser = pw.webkit.launch(timeout=20000)
    context = browser.new_context(viewport={'width': 400, 'height': 844})
    page = context.new_page()
    page.set_content('<html><body style="margin:0"></body></html>')
    js = (ROOT / 'static/app.js').read_text(encoding='utf-8')
    gpu_source = js[js.index('function createLiquidGlassGpu'):js.index('const liquidGlassLensController=')]
    assert 'gl_FragColor=vec4(c*mask,mask);' in gpu_source
    gpu_source = gpu_source.replace('gl_FragColor=vec4(c*mask,mask);',
        'gl_FragColor=vec4(vec3(.5+bend.x*1.10/(u_rect.z*max(u_strength,1.0)),.5+bend.y*1.10/(u_rect.w*max(u_strength,1.0)),.5)*mask,mask);')
    page.add_script_tag(content=gpu_source)
    result = page.evaluate('''async()=>{
      const image=document.createElement('canvas');image.width=400;image.height=844;
      const gpu=createLiquidGlassGpu(image,()=>{}),gl=gpu.canvas.getContext('webgl'),results=[];
      for(const height of [600,2000,8000])for(const strength of [0,1,10,30]){
        document.documentElement.style.setProperty('--liquid-glass-refraction',strength);
        const curved=strength<=10?strength:10+Math.sqrt(strength-10)*3,scale=curved/5;
        let max=0,minAlpha=255,maxError=0,samples=0;
        const ss=(a,b,t)=>{t=Math.max(0,Math.min(1,(t-a)/(b-a)));return t*t*(3-2*t)};
        for(const top of [0,-height/2]){
          gpu.draw([{rect:[20,top,360,height],radii:[26,26,26,26],clipBox:[0,0,400,844],clips:[],soft:false}],
            {width:400,height:844,cover:[0,0,400,844],blur:0,base:[0,0,0],sourceOpacity:1});
          for(let y=Math.max(40,top+40);y<Math.min(804,top+height-40);y+=37)for(let x=50;x<355;x+=31){
            const p=new Uint8Array(4);gl.readPixels(x,843-y,1,1,gl.RGBA,gl.UNSIGNED_BYTE,p);
            const lx=x+.5-200,ly=y+.5-top-height/2,ux=lx/360,uy=ly/height;
            const qx=Math.abs(ux)-.28,qy=Math.abs(uy)-.28,sdf=Math.min(Math.max(qx,qy),0)+Math.hypot(Math.max(qx,0),Math.max(qy,0))-.16;
            const k=1-ss(0,1,ss(.14,-.02,sdf))*.34-.03;
            const expected=[.5+lx*(k-1)*scale*1.1/(360*Math.max(scale,1)),.5+ly*(k-1)*scale*1.1/(height*Math.max(scale,1))];
            maxError=Math.max(maxError,...expected.map((v,i)=>Math.abs(p[i]-v*255)));
            minAlpha=Math.min(minAlpha,p[3]);max=Math.max(max,Math.hypot((p[0]/255-.5)*360*Math.max(scale,1),(p[1]/255-.5)*height*Math.max(scale,1)));samples++;
          }
        }
        results.push({height,strength,max,minAlpha,maxError,samples});
      }
      gpu.dispose();return results;
    }''')
    for row in result:
        assert row['samples'] > 0 and row['minAlpha'] > 240 and row['maxError'] < 1.5, row
    for height in (600, 2000, 8000):
        levels = [r['max'] for r in result if r['height'] == height]
        assert levels[0] < levels[1] < levels[2] < levels[3], (height, levels)
    assert max(r['max'] for r in result if r['strength'] == 30) > 24, result
    print('PASS GPU折射恢复：360x600/2000/8000，强度0/1/10/30逐采样点匹配备份原始位移公式，玻璃alpha保留')
    context.close()
    browser.close()


def check_long_card_comparison(page, output_dir):
    page.evaluate("setLook({'WebUI.liquid_glass_refraction':30,'WebUI.card_background_blur':3});document.querySelector('.main').scrollTop=0")
    for height in (600,2000,8000):
        page.evaluate("height=>{const c=document.getElementById('longCard');c.style.width='360px';c.style.height=height+'px';const m=document.querySelector('.main');m.scrollTop+=c.getBoundingClientRect().top-m.getBoundingClientRect().top-160}",height)
        wait_frame(page)
        pixels=page.evaluate('''()=>{const c=document.querySelector('.glass-gpu-layer'),g=c.getContext('webgl'),p=new Uint8Array(4);
          g.readPixels(Math.floor(190*c.width/innerWidth),c.height-1-Math.floor(400*c.height/innerHeight),1,1,g.RGBA,g.UNSIGNED_BYTE,p);return [...p]}''')
        assert pixels[3]>240,(height,pixels)
        page.screenshot(path=str(output_dir/f'card-height-{height}-strength30.png'))
    page.evaluate("document.getElementById('longCard').style.width='';document.getElementById('longCard').style.height='1500px';setLook({'WebUI.liquid_glass_refraction':1});document.querySelector('.main').scrollTop=0")
    wait_frame(page)


def check_sidebar_resize_pixels(page):
    """Crossing the drawer breakpoint must rescan targets, not leave desktop sidebar clear."""
    for width in (390,981,1280,390,1280):
        page.set_viewport_size({'width':width,'height':844})
        wait_frame(page)
        alpha=page.evaluate('''()=>{const c=document.querySelector('.glass-gpu-layer'),g=c.getContext('webgl'),p=new Uint8Array(4);
          const s=document.querySelector('.sidebar').getBoundingClientRect(),x=innerWidth>980?s.left+40:20,y=500;
          g.readPixels(Math.floor(x*c.width/innerWidth),c.height-1-Math.floor(y*c.height/innerHeight),1,1,g.RGBA,g.UNSIGNED_BYTE,p);return p[3]}''')
        if width>980:
            assert alpha>240,('desktop sidebar missing GPU glass',width,alpha)
            assert page.locator('.sidebar').evaluate('n=>getComputedStyle(n).position')!='fixed'
    page.set_viewport_size({'width':390,'height':844})
    wait_frame(page)


def test_theme_texture(pw, output_dir):
    """Android UA 仅验证路径/缓存/像素，不当作安卓真机性能数据。"""
    android = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36'
    browser = pw.chromium.launch(args=['--use-angle=gl', '--enable-gpu'], timeout=20000)
    html = page_source().replace("'WebUI.background_image':'https://webui.test/wall-a.png'", "'WebUI.background_image':''")
    contexts = []
    for disabled in (False, True):
        context = browser.new_context(viewport={'width': 390, 'height': 844}, user_agent=android, is_mobile=True, has_touch=True)
        contexts.append(context)
        if disabled:
            context.add_init_script('''const original=HTMLCanvasElement.prototype.getContext;
              HTMLCanvasElement.prototype.getContext=function(type,...a){return /webgl/.test(type)?null:original.call(this,type,...a)}''')
        requests = []
        def route(r):
            if r.request.url.startswith('blob:'):
                r.continue_()
                return
            requests.append(r.request.url)
            if '/wall-a.png' in r.request.url:
                r.fulfill(body=wallpaper(640, 960), content_type='image/png')
            else:
                r.fulfill(body=html, content_type='text/html')
        context.route('**/*', route)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('https://webui.test/')
        wait_frame(page)
        assert not page.evaluate("document.documentElement.classList.contains('has-bg')")
        assert page.evaluate("state.bundle.form_values['WebUI.background_image']") == ''
        assert len(requests) == 1, requests
        if disabled:
            assert page.evaluate('liquidGlassLensController.diagnostics().engine') == 'lightweight'
            assert page.locator('.glass-gpu-layer,.glass-lens-layer').count() == 0
            assert page.locator('#lensCard').evaluate("n=>getComputedStyle(n,'::before').display") == 'none'
            for selector in ('#lensField', '#lensButton', '.pill'):
                assert page.locator(selector).first.evaluate("n=>getComputedStyle(n).backdropFilter") == 'none'
            assert 'blur(20px)' in page.locator('.topbar').evaluate('n=>getComputedStyle(n).backdropFilter')
            for width in (980,981):
                page.set_viewport_size({'width':width,'height':844})
                actual=page.locator('.topbar').evaluate('n=>getComputedStyle(n).backdropFilter')
                assert ('blur(20px)' in actual if width<=980 else actual=='none'), (width,actual)
            page.evaluate("setLook({'WebUI.liquid_glass':false})")
            assert not page.evaluate("document.documentElement.classList.contains('theme-glass-fallback')||document.documentElement.classList.contains('liquid-glass-fallback')")
            print('PASS 无壁纸禁用WebGL：明确轻量后备，无逐卡warp/小控件backdrop，移动顶栏可读性保留')
            context.close()
            continue
        page.wait_for_function("liquidGlassLensController.diagnostics().sourceType==='theme-gradient'&&liquidGlassLensController.diagnostics().gpu?.textureUploads===1")
        assert page.locator('.glass-gpu-layer').count() == 1
        assert page.locator('.glass-lens-layer').count() == 0
        check_sidebar_resize_pixels(page)
        print('PASS 无壁纸GPU：390→981→1280→390→1280，桌面常驻侧栏区域alpha持续>240')
        # A partial bundle with missing key preserves the current source; it is not a clear.
        page.evaluate("delete state.bundle.form_values['WebUI.background_image'];applyWebuiAppearance()")
        assert page.evaluate('liquidGlassLensController.diagnostics().sourceType') == 'theme-gradient'
        assert not page.evaluate("Object.hasOwn(state.bundle.form_values,'WebUI.background_image')")
        page.evaluate("setLook({'WebUI.background_image':'','WebUI.card_background_blur':0,'WebUI.liquid_glass_refraction':0})")
        wait_frame(page)
        # Compare raw opaque GPU RGB with real six-layer CSS background, no surfaces/tint.
        for theme in ('light', 'dark'):
            for preset in ('aurora','xcbot','midnight','sakura','forest','sunset','ocean'):
                page.evaluate("([theme,preset])=>{document.documentElement.dataset.theme=theme;setLook({'WebUI.theme_preset':preset})}", [theme,preset])
                wait_frame(page)
                pixel = page.evaluate('''()=>{const c=document.querySelector('.glass-gpu-layer'),g=c.getContext('webgl'),p=new Uint8Array(4);
                  g.readPixels(Math.floor(70*c.width/innerWidth),c.height-1-Math.floor(200*c.height/innerHeight),1,1,g.RGBA,g.UNSIGNED_BYTE,p);return [...p]}''')
                assert pixel[3] > 240, (theme,preset,pixel)
                neutral = page.add_style_tag(content='.app,.glass-gpu-layer{visibility:hidden !important}')
                background = Image.open(io.BytesIO(page.screenshot())).convert('RGB').getpixel((70,200))
                neutral.evaluate('(n)=>n.remove()')
                assert max(abs(pixel[i]-background[i]) for i in range(3)) <= 4, (theme,preset,pixel,background)
        baseline = page.evaluate('liquidGlassLensController.diagnostics()')
        for _ in range(4):
            page.evaluate('applyWebuiAppearance();liquidGlassLensController.refresh();document.querySelector(".main").scrollTop+=3')
            wait_frame(page)
        after = page.evaluate('liquidGlassLensController.diagnostics()')
        assert after['themeTextureBuilds'] == baseline['themeTextureBuilds']
        assert after['gpu']['textureUploads'] == baseline['gpu']['textureUploads']
        page.wait_for_timeout(500)
        frames = page.evaluate('liquidGlassLensController.diagnostics().gpu.frames')
        page.wait_for_timeout(300)
        assert page.evaluate('liquidGlassLensController.diagnostics().gpu.frames') == frames
        page.set_viewport_size({'width':844,'height':390})
        wait_frame(page)
        rotated = page.evaluate('liquidGlassLensController.diagnostics()')
        assert rotated['themeTextureBuilds'] == baseline['themeTextureBuilds']+1
        assert rotated['gpu']['textureUploads'] == baseline['gpu']['textureUploads']+1
        page.evaluate("setLook({'WebUI.background_image':'https://webui.test/wall-a.png'})")
        page.wait_for_function("liquidGlassLensController.diagnostics().sourceType==='same-origin-blob'&&document.querySelector('.glass-gpu-layer')")
        check_sidebar_resize_pixels(page)
        print('PASS 有壁纸GPU：390→981→1280→390→1280，桌面常驻侧栏区域alpha持续>240')
        check_desktop_sidebar_glass(page, output_dir)
        page.evaluate("delete state.bundle.form_values['WebUI.background_image'];applyWebuiAppearance()")
        assert page.evaluate('liquidGlassLensController.diagnostics().sourceType') == 'same-origin-blob'
        assert page.evaluate("document.documentElement.classList.contains('has-bg')")
        page.evaluate("setLook({'WebUI.background_image':''})")
        page.wait_for_function("liquidGlassLensController.diagnostics().sourceType==='theme-gradient'")
        assert page.locator('.glass-gpu-layer').count() == 1
        assert not page.evaluate("document.documentElement.classList.contains('has-bg')")
        page.evaluate("setLook({'WebUI.liquid_glass':false})")
        assert page.locator('.glass-gpu-layer,.glass-lens-layer').count() == 0
        assert not page.evaluate("document.documentElement.classList.contains('theme-glass-fallback')||document.documentElement.classList.contains('liquid-glass-fallback')")
        page.evaluate("setLook({'WebUI.liquid_glass':true})")
        page.wait_for_function("liquidGlassLensController.diagnostics().gpu?.textureUploads===1")
        page.evaluate("document.querySelector('.glass-gpu-layer').dispatchEvent(new Event('webglcontextlost',{cancelable:true}));applyWebuiAppearance()")
        assert page.evaluate('liquidGlassLensController.diagnostics().engine') == 'lightweight'
        assert page.locator('.glass-gpu-layer,.glass-lens-layer').count() == 0
        page.evaluate("setLook({'WebUI.liquid_glass':false})")
        assert not page.evaluate("document.documentElement.classList.contains('theme-glass-fallback')||document.documentElement.classList.contains('liquid-glass-fallback')")
        page.evaluate("setLook({'WebUI.liquid_glass':true})")
        page.wait_for_function("liquidGlassLensController.diagnostics().gpu?.textureUploads===1")
        assert not errors, errors
        assert all('/api/webui/background' not in r for r in requests)
        # Delayed old image callbacks cannot resurrect wallpaper after theme/disabled transitions.
        page.evaluate('''window.pendingImages=[];window.Image=class {
          constructor(){this.naturalWidth=640;this.naturalHeight=960;pendingImages.push(this)}
          set src(value){this.url=value}
        };setLook({'WebUI.background_image':'https://webui.test/wall-a.png'})''')
        page.wait_for_function('pendingImages.length===1')
        page.evaluate("setLook({'WebUI.background_image':''});pendingImages[0].onload()")
        wait_frame(page)
        assert page.evaluate('liquidGlassLensController.diagnostics().sourceType') == 'theme-gradient'
        assert page.locator('.glass-gpu-layer').count() == 1
        page.evaluate("setLook({'WebUI.background_image':'https://webui.test/wall-a.png'})")
        page.wait_for_function('pendingImages.length===2')
        page.evaluate("setLook({'WebUI.liquid_glass':false});pendingImages[1].onload()")
        assert page.locator('.glass-gpu-layer,.glass-lens-layer').count() == 0
        print('PASS Android UA无壁纸：明暗7预设RGB与CSS<=4误差，opaque不重复混底；单画布、重复render/滚动不重传、闲置停帧、旋转一次更新、换源清空开关/contextlost及过期图片清理')
        context.close()
    browser.close()


def check_desktop_sidebar_glass(page, output_dir):
    """整块侧栏的纹理、折射和固定边框，滚到底也不能截出额外矩形。"""
    page.set_viewport_size({'width': 1440, 'height': 600})
    page.evaluate("""()=>{const n=document.createElement('div');n.id='sidebarScrollProbe';n.style.height='1000px';document.querySelector('.sidebar').append(n)}""")
    page.evaluate("setLook({'WebUI.liquid_glass_refraction':5})")
    for theme in ('light', 'dark'):
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        for scroll in (0, 240, 10000):
            page.locator('.sidebar').evaluate('(n,y)=>n.scrollTop=y', scroll)
            wait_frame(page)
            style = page.locator('.sidebar').evaluate("n=>({after:getComputedStyle(n,'::after').display,border:getComputedStyle(n).borderTopWidth,rect:n.getBoundingClientRect().toJSON(),client:n.clientHeight,scroll:n.scrollTop})")
            assert style['after'] == 'none' and style['border'] == '1px', style
            assert style['rect']['top'] == 12 and style['rect']['bottom'] <= 589, style
            clip = {'x': 50, 'y': 240, 'width': 120, 'height': 120}
            def pixels():
                return page.evaluate("""()=>{const c=document.querySelector('.glass-gpu-layer'),g=c.getContext('webgl'),sx=c.width/innerWidth,sy=c.height/innerHeight,w=Math.floor(120*sx),h=Math.floor(120*sy),p=new Uint8Array(w*h*4);
                  g.readPixels(Math.floor(50*sx),c.height-Math.floor(360*sy),w,h,g.RGBA,g.UNSIGNED_BYTE,p);return Array.from(p)}""")
            before = pixels()
            assert min(before[3::4]) > 240, (theme,scroll,'整块侧栏没有GPU玻璃')
            page.evaluate("setLook({'WebUI.liquid_glass_refraction':0})")
            wait_frame(page)
            after = pixels()
            changed = sum(max(abs(before[i+j]-after[i+j]) for j in range(3))>2 for i in range(0,len(before),4))
            assert changed > 200, (theme,scroll,changed)
            page.evaluate("setLook({'WebUI.liquid_glass_refraction':5})")
            wait_frame(page)
            page.screenshot(path=str(output_dir / f'pc-sidebar-{theme}-{scroll}.png'))
    page.evaluate("document.getElementById('sidebarScrollProbe').remove();document.querySelector('.sidebar').scrollTop=0;setLook({'WebUI.liquid_glass_refraction':1})")
    page.set_viewport_size({'width':390,'height':844})
    wait_frame(page)
    print('PASS PC整块侧栏：明暗/滚动前中后玻璃alpha与强度像素差有效，本体边框固定、无随内容滚动的重复线框')


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
                assert response.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
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
                error.close()
            mutable = base / 'manual.png'
            mutable.write_bytes(first)
            config.write_text(json.dumps({'WebUI': {'background_image': 'manual.png'}}), encoding='utf-8')
            with opener.open(url) as response:
                assert response.read() == first
                assert response.headers['Cache-Control'] == 'no-cache'
                mutable_etag = response.headers['ETag']
            mutable.write_bytes(second)
            request = urllib.request.Request(url, headers={'If-None-Match': mutable_etag})
            with opener.open(request) as response:
                assert response.status == 200 and response.read() == second
                assert response.headers['ETag'] != mutable_etag
                assert response.headers['Cache-Control'] == 'no-cache'
            print('PASS 背景接口：上传落盘、旧路径读取新图、ETag 304、同名覆盖后重验证')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_texture_blur(pw, output_dir):
    """强制原生/无 filter 两条纹理路径，与独立高斯参考比较，不改变折射公式。"""
    js = (ROOT / 'static/app.js').read_text(encoding='utf-8')
    gpu_source = js[js.index('function createLiquidGlassGpu'):js.index('const liquidGlassLensController=')]
    browser = pw.chromium.launch()
    captures = {}
    for fallback in (False, True):
        context = browser.new_context(viewport={'width': 256, 'height': 256})
        if fallback:
            context.add_init_script("Object.defineProperty(CanvasRenderingContext2D.prototype,'filter',{configurable:true,get(){return undefined},set(v){}})")
        page = context.new_page()
        page.goto('about:blank')
        page.add_script_tag(content=gpu_source)
        result = page.evaluate('''()=>{
          document.documentElement.style.setProperty('--liquid-glass-refraction','0');
          const image=document.createElement('canvas');image.width=256;image.height=256;
          const c=image.getContext('2d');c.fillStyle='white';c.fillRect(0,0,256,256);
          c.fillStyle='black';c.beginPath();c.moveTo(20,30);c.lineTo(220,200);c.lineTo(40,215);c.fill();
          c.fillStyle='#0080ff';c.beginPath();c.arc(170,65,27,0,Math.PI*2);c.fill();
          const original=image.toDataURL(),gpu=createLiquidGlassGpu(image,()=>{}),gl=gpu.canvas.getContext('webgl');
          const regions=[{rect:[0,0,256,256],radii:[0,0,0,0],clipBox:[0,0,256,256],clips:[],soft:false}];
          const geometry={width:256,height:256,cover:[0,0,256,256],blur:6,base:[1,1,1],sourceOpacity:1};
          gpu.draw(regions,geometry);const data=gpu.canvas.toDataURL(),first=gpu.diagnostics();
          gpu.draw(regions,geometry);const repeated=gpu.diagnostics();
          gpu.draw(regions,{...geometry,cover:[0,0,128,128]});const resized=gpu.diagnostics();
          const edge=new Uint8Array(4);gl.readPixels(4,128,1,1,gl.RGBA,gl.UNSIGNED_BYTE,edge);
          c.clearRect(0,0,256,256);c.fillStyle='#ff0000';c.fillRect(0,0,128,256);
          gpu.draw(regions,{...geometry,sourceRevision:1});
          const transparentEdge=new Uint8Array(4);gl.readPixels(128,128,1,1,gl.RGBA,gl.UNSIGNED_BYTE,transparentEdge);
          gpu.draw(regions,{...geometry,blur:.57,sourceRevision:1});const smallBefore=gpu.diagnostics();
          gpu.draw(regions,{...geometry,blur:.59,sourceRevision:1});const smallAfter=gpu.diagnostics();
          gpu.dispose();return {original,data,first,repeated,resized,edge:[...edge],transparentEdge:[...transparentEdge],smallBefore,smallAfter};
        }''')
        import base64
        original = Image.open(io.BytesIO(base64.b64decode(result['original'].split(',', 1)[1]))).convert('RGB')
        actual = Image.open(io.BytesIO(base64.b64decode(result['data'].split(',', 1)[1]))).convert('RGB')
        expected = original.filter(ImageFilter.GaussianBlur(6))
        diff = ImageChops.difference(actual.crop((12,12,244,244)), expected.crop((12,12,244,244)))
        mean = sum(ImageStat.Stat(diff).mean)/3
        assert mean < 2.5, (fallback, mean)
        assert result['first']['textureSigma'] == 6 and result['resized']['textureSigma'] == 12
        assert result['first']['textureUploads'] == result['repeated']['textureUploads'] == 1
        assert result['resized']['textureUploads'] == 2
        assert result['smallAfter']['textureUploads'] == result['smallBefore']['textureUploads'] + 1
        assert result['smallAfter']['textureSigma'] == .59
        assert min(result['edge'][:3]) > 240 and result['edge'][3] > 240, result['edge']
        assert result['transparentEdge'][0] > 245 and 80 < result['transparentEdge'][1] < 180 and abs(result['transparentEdge'][1]-result['transparentEdge'][2]) <= 1, result['transparentEdge']
        assert result['first']['blurMethod'] == ('three-box-gaussian' if fallback else 'canvas-gaussian')
        actual.save(output_dir / ('blur-fallback.png' if fallback else 'blur-native.png'))
        captures[fallback] = actual
        context.close()
    browser.close()
    print('PASS 纹理模糊：原生/强制Safari后备均接近独立高斯参考、边缘不拉伸，cover变化正确重传、静态不重传')


def test_refresh_frames(pw, output_dir):
    """逐帧验证首绘接管、入场位移和画布透明裁剪，不等待稳定态才检查。"""
    for engine, width in (('chromium', 1440), ('chromium', 390), ('webkit', 390)):
        options = {'args': ['--use-angle=gl', '--enable-gpu']} if engine == 'chromium' else {}
        browser = getattr(pw, engine).launch(timeout=20000, **options)
        context = browser.new_context(viewport={'width': width, 'height': 844},
                                      **({'user_agent': IOS_UA, 'is_mobile': True, 'has_touch': True} if engine == 'webkit' else {}))
        html = page_source().replace("document.querySelector('#pageDesc').textContent='';",
                                    "document.querySelector('#pageDesc').textContent='';document.getElementById('content').classList.add('page-anim');")
        image = wallpaper(640, 960)
        def route_request(route):
            if route.request.url.startswith('blob:'):
                route.continue_()
            elif '/wall-a.png' in route.request.url:
                # 有意模拟纹理解码前的网络等待，覆盖刷新时的占位阶段。
                __import__('time').sleep(0.12)
                route.fulfill(body=image, content_type='image/png')
            else:
                route.fulfill(body=html, content_type='text/html')
        context.route('**/*', route_request)
        context.add_init_script("""
          window.refreshFrames=[];
          let stopAt=performance.now()+2400;
          function inspect(){
            const root=document.documentElement,canvas=document.querySelector('.glass-gpu-layer'),card=document.getElementById('lensCard');
            const pending=root.classList.contains('glass-render-pending'),gpu=root.classList.contains('gpu-liquid-glass');
            let draws=0,outside=0,painted=0,dy=null;
            if(canvas){
              const gl=canvas.getContext('webgl');draws=window.glassDraws||0;
              const r=card?.getBoundingClientRect();
              if(r&&gpu){
                const d=canvas.height/innerHeight,p=new Uint8Array(4);
                const sample=(x,y)=>{gl.readPixels(Math.floor(x*d),canvas.height-1-Math.floor(y*d),1,1,gl.RGBA,gl.UNSIGNED_BYTE,p);return p[3]};
                const x=r.left+r.width/2;
                if(r.top>2&&r.top<innerHeight-10){outside=sample(x,r.top-2);painted=sample(x,r.top+8)}
                dy=window.glassRects?.some(a=>Math.abs(a[0]-r.left-card.clientLeft)<1&&Math.abs(a[1]-r.top-card.clientTop)<1);
              }
            }
            const fallback=card?getComputedStyle(card,'::before').display:null;
            refreshFrames.push({pending,gpu,draws,outside,painted,dy,fallback});
            if(performance.now()<stopAt)requestAnimationFrame(()=>setTimeout(inspect,0));
          }
          const proto=WebGLRenderingContext.prototype,set=proto.uniform4fv,draw=proto.drawArrays,get=proto.getUniformLocation;
          const rects=new WeakSet();
          proto.getUniformLocation=function(p,n){const v=get.call(this,p,n);if(v&&n==='u_rect')rects.add(v);return v};
          proto.uniform4fv=function(l,v){if(rects.has(l)){window.glassRects=window.glassRects||[];glassRects.push(Array.from(v));if(glassRects.length>256)glassRects.splice(0,128)}return set.call(this,l,v)};
          proto.drawArrays=function(...args){window.glassDraws=(window.glassDraws||0)+1;return draw.apply(this,args)};
          requestAnimationFrame(inspect);
        """)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        for reload in (False, True):
            if reload:
                page.reload()
            else:
                page.goto('https://webui.test/')
            page.wait_for_function("document.documentElement.dataset.liquidGlassState==='gpu-wallpaper-refraction'")
            page.screenshot(path=str(output_dir / f'refresh-{engine}-{width}-entry.png'), animations='allow')
            page.wait_for_timeout(1700)
            frames = page.evaluate('refreshFrames')
            active = [frame for frame in frames if frame['gpu']]
            assert active and all(frame['draws'] > 0 for frame in active), (engine, width, '接管前未首绘')
            assert all(not frame['pending'] for frame in active), (engine, width, '双层渲染')
            assert all(frame['fallback'] in (None, 'none') for frame in frames if frame['pending']), (engine, width, '首帧占位仍有高饱和采样')
            measured = [frame for frame in active if frame['dy'] is not None]
            assert measured and all(frame['dy'] for frame in measured), (engine, width, '动画区域不同步')
            assert all(frame['outside'] == 0 for frame in measured), (engine, width, '玻璃超出边框', measured)
            assert any(frame['painted'] > 0 for frame in measured), (engine, width, '玻璃未绘制')
            assert page.evaluate('liquidGlassLensController.diagnostics().firstFrameReady')
            assert not errors, errors
        page.screenshot(path=str(output_dir / f'refresh-{engine}-{width}-stable.png'))
        print(f'PASS {engine} {width}px：刷新首绘后接管，动画期间逐帧对齐且框外透明')
        context.close()
        browser.close()


def test_subtab_entry(pw):
    """真实子页渲染/切换配合父卡片入场，覆盖1500ms后迟播的回归。"""
    js = (ROOT / 'static/app.js').read_text(encoding='utf-8')
    animation = js[js.index('function animateSubtab('):js.index('function switchProviderTab(')]
    settings = js[js.index('function settingsTabs('):js.index('function scrollToRouteSection(')]
    setup = """
      const el=id=>document.getElementById(id),esc=s=>String(s||'');
      const pages=()=>[{key:'proactive',fields:[]}];
      function syncCurrentPageFieldsFromDom(){}
      function enterSettings(){
        state.settingsTabs={proactive:'group'};
        document.getElementById('content').innerHTML=renderOrganizedSettings({key:'proactive',title:'主动回复'});
        document.getElementById('content').classList.add('page-anim');
        setTimeout(()=>document.getElementById('content').classList.remove('page-anim'),1500);
        liquidGlassLensController.refresh();
      }
    """
    for engine, width in (('chromium', 1440), ('chromium', 390), ('webkit', 390)):
        options = {'args': ['--use-angle=gl', '--enable-gpu']} if engine == 'chromium' else {}
        browser = getattr(pw, engine).launch(timeout=20000, **options)
        context = browser.new_context(viewport={'width': width, 'height': 844})
        html = page_source().replace('applyWebuiAppearance();</script>',
                                    f'applyWebuiAppearance();{animation}{settings}{setup}</script>')
        image = wallpaper(640, 960)
        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith('blob:') else
                      route.fulfill(body=image, content_type='image/png') if '/wall-a.png' in route.request.url else
                      route.fulfill(body=html, content_type='text/html'))
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('https://webui.test/')
        page.wait_for_function("document.documentElement.dataset.liquidGlassState==='gpu-wallpaper-refraction'")
        for delay in (None, 120, 1700):
            result = page.evaluate("""async delay=>{
              enterSettings();const panel=document.getElementById('settingsPanel_proactive'),card=panel.parentElement;
              const start=performance.now(),events=[],samples=[];
              panel.addEventListener('animationstart',e=>{if(e.target===panel)events.push({name:e.animationName,at:performance.now()-start})});
              if(delay!==null)setTimeout(()=>switchSettingsTab('proactive','private'),delay);
              await new Promise(resolve=>{function sample(){
                const r=panel.getBoundingClientRect(),c=card.getBoundingClientRect();
                samples.push({at:performance.now()-start,y:r.top-c.top,opacity:getComputedStyle(panel).opacity});
                if(performance.now()-start<2200)requestAnimationFrame(sample);else resolve();
              }requestAnimationFrame(sample)});
              return {events,samples,remaining:panel.classList.contains('subtab-enter')};
            }""", delay)
            assert len(result['events']) == (0 if delay is None else 1), (engine, width, delay, result['events'])
            if delay is not None:
                assert delay <= result['events'][0]['at'] < delay + 250, (engine, width, delay, result['events'])
                assert result['events'][0]['name'] == 'glass-subtab-content-in'
            assert all(float(sample['opacity']) == 1 for sample in result['samples'])
            settled = [sample['y'] for sample in result['samples'] if sample['at'] > (delay or 0) + 400]
            assert max(settled)-min(settled) < 1, (engine, width, delay, '动画结束后再次位移')
            assert not result['remaining']
        page.emulate_media(reduced_motion='reduce')
        assert not page.evaluate("animateSubtab(document.getElementById('settingsPanel_proactive'));document.getElementById('settingsPanel_proactive').classList.contains('subtab-enter')")
        assert not errors, errors
        print(f'PASS {engine} {width}px：进入群聊子页、入场中/结束后切页均无延迟重播')
        context.close()
        browser.close()


def settings_page_source():
    """只提取纯 schema 函数和真实渲染器，不 import WebUI、不读取 config。"""
    tree = ast.parse((ROOT / 'webui.py').read_text(encoding='utf-8'))
    pure = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                            and n.name in ('field', 'build_ui_schema')], type_ignores=[])
    namespace = {'Dict': dict, 'Any': object}
    exec(compile(pure, '<offline-schema>', 'exec'), namespace)
    schema = namespace['build_ui_schema']({})
    js = (ROOT / 'static/app.js').read_text(encoding='utf-8')
    def part(start, end):
        return js[js.index(start):js.index(end, js.index(start))]
    scripts = '\n'.join([
        part('const featureFieldMap=', '// 首次用 ?token=').split('\n')[0],
        part('function renderAiSettings(', 'function settingsTabs('),
        part('function renderField(', '// 密码类字段'),
        part('function renderFeatures()', 'async function checkUpdate('),
        part('function animateSubtab(', 'function switchProviderTab('),
        part('let _storeList=null;', 'setTheme(localStorage.webuiTheme'),
    ])
    setup = 'const offlineSchema='+json.dumps(schema, ensure_ascii=False)+';'+r"""
      const el=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      function setValue(path,value){state.bundle.form_values[path]=value}
      function setBool(path,node){const value=!node.classList.contains('on');node.classList.toggle('on',value);setValue(path,value)}
      function saveDraft(){} function api(){throw Error('Offline fixture must not call API')}
      function syncCurrentPageFieldsFromDom(){}
      state.bundle.ui_schema=offlineSchema;
      for(const page of offlineSchema)for(const field of page.fields||[])state.bundle.form_values[field.path]=field.default??(field.type==='bool'?false:field.type==='list'?[]:'');
      state.bundle.feature_meta=['ai_chat','compression_commands','quote','plugin_admin_commands','split_reply_quote','weather','check_account','image_generation','group_chat','private_chat','group_chat_context','sensitive_filter','weak_blacklist','poke_reply','group_join_welcome','emoji_plus_one','summary','plugins_external','http_send_api'].map(key=>({key,title:({group_chat:'群聊响应',private_chat:'私聊响应',group_chat_context:'群聊上下文感知',sensitive_filter:'消息屏蔽词',weak_blacklist:'弱黑名单',poke_reply:'拍一拍回复',group_join_welcome:'入群欢迎',emoji_plus_one:'表情 +1',summary:'群聊总结',plugins_external:'外部插件',http_send_api:'HTTP 发送接口'})[key]||key,desc:'此为离线功能说明，不调用机器人或模型。',group:'fallback'}));
      state.bundle.feature_switches=Object.fromEntries(state.bundle.feature_meta.map(item=>[item.key,true]));
      state.bundle.form_values['Others.llm_split.prompt_suffix']='请按语义分段，以 <split> 分隔。';
      state.bundle.form_values['Others.group_join_welcome_text']='欢迎 {at} 加入群聊！';
      _pluginSchemas={offline_demo:{schema:{enabled:{type:'bool'},prompt:{type:'textarea'}},values:{}}};_storeList=[];
      window.showTarget=(target,animate=false)=>{
        state.current=target;const content=el('content');clearTimeout(content._animTimer);content.classList.remove('page-anim');
        content.innerHTML=target==='store'?renderStore():target==='features'?renderFeatures():renderAiSettings(offlineSchema.find(p=>p.key==='ai'));
        el('pageTitle').textContent=target==='store'?'插件管理':target==='features'?'功能配置':'AI 对话';
        if(animate){void content.offsetWidth;content.classList.add('page-anim');content._animTimer=setTimeout(()=>content.classList.remove('page-anim'),1500)}
        liquidGlassLensController.refresh();
      };
    """
    return page_source().replace('applyWebuiAppearance();</script>',
                                 f'applyWebuiAppearance();{scripts}{setup}</script>')


def test_settings_layout_and_plugin_entry(pw, output_dir):
    """小型定向验证：真实字段/渲染、PC及窄屏、插件入口和GPU运动坐标。"""
    html = settings_page_source()
    browser = pw.chromium.launch(args=['--use-angle=gl', '--enable-gpu'])
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    image = wallpaper(640, 960)
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith('blob:') else
                  route.fulfill(body=image, content_type='image/png') if '/wall-a.png' in route.request.url else
                  route.fulfill(body=html, content_type='text/html'))
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('https://webui.test/')
    page.evaluate("setLook({'WebUI.background_image':'https://webui.test/wall-a.png','WebUI.liquid_glass':true,'WebUI.background_blur':0})")
    page.wait_for_function("liquidGlassLensController.diagnostics().firstFrameReady")
    for glass in (False, True):
        page.emulate_media(color_scheme='dark')
        page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
        page.evaluate("glass=>setLook({'WebUI.liquid_glass':glass})", glass)
        if glass:
            page.wait_for_function("document.documentElement.dataset.liquidGlassState==='gpu-wallpaper-refraction'")
        for width in (1920, 1440, 390):
            page.set_viewport_size({'width': width, 'height': 1000 if width > 700 else 844})
            # .btn transition:all 会在断点切换后短暂插值 flex-shrink；量稳定布局而非这一过渡帧。
            page.wait_for_timeout(250)
            for target in ('features', 'ai'):
                page.evaluate('target=>showTarget(target)', target)
                wait_frame(page)
                metrics = page.evaluate(r"""()=>{
                  const content=el('content'),r=content.getBoundingClientRect();
                  const nodes=[...content.querySelectorAll('.feature-option,.field,input,textarea,select')];
                  const grids=[...content.querySelectorAll('.feature-options-grid,.ai-conversation-fields')];
                  const rect=n=>{const b=n.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height,right:b.right}};
                  return {overflow:document.documentElement.scrollWidth-innerWidth,
                    outside:nodes.filter(n=>{const b=n.getBoundingClientRect();return b.x<r.x-1||b.right>r.right+1}).length,
                    grids:grids.map(g=>({columns:getComputedStyle(g).gridTemplateColumns.split(' ').length,items:[...g.children].map(rect),box:rect(g)})),
                    textarea:[...content.querySelectorAll('.ai-conversation-fields textarea')].map(n=>({h:n.offsetHeight,resize:getComputedStyle(n).resize})),
                    memory:!!el('f_Others_context_max_messages')&&!el('f_Others_context_max_messages').closest('details')};
                }""")
                assert metrics['overflow'] <= 1 and metrics['outside'] == 0, (glass, width, target, metrics)
                for grid in metrics['grids']:
                    assert grid['columns'] == 1 if width == 390 else 1 <= grid['columns'] <= 5
                    if target == 'ai':
                        for item in grid['items']:
                            peers = [p for p in grid['items'] if abs(p['y']-item['y']) < 1]
                            assert max(p['h'] for p in peers)-min(p['h'] for p in peers) < 1
                    if width > 700:
                        assert all(item['w'] < 420 for item in grid['items']), (glass, width, target, '小卡片不应横向拉满', grid)
                if target == 'ai':
                    assert metrics['memory'] and all(t['h'] == 120 and t['resize'] == 'vertical' for t in metrics['textarea'])
                if width == 390:
                    assert page.evaluate("getComputedStyle(document.querySelector('.toolbar')).overflowX") == 'auto'
                    assert page.evaluate("!document.querySelector('.toolbar').contains(el('saveState'))")
                    assert page.evaluate("document.querySelector('.toolbar').scrollWidth>document.querySelector('.toolbar').clientWidth"), page.evaluate("[...document.querySelectorAll('.toolbar,.toolbar>button')].map(n=>({text:n.textContent,w:n.clientWidth,scroll:n.scrollWidth,white:getComputedStyle(n).whiteSpace,flex:getComputedStyle(n).flex}))")
                    assert page.evaluate("[...document.querySelectorAll('.toolbar>button')].every(b=>getComputedStyle(b).whiteSpace==='nowrap')")
                    assert page.evaluate("el('saveState').getBoundingClientRect().bottom<=document.querySelector('.toolbar').getBoundingClientRect().top+1")
                page.screenshot(path=str(output_dir / f'settings-{target}-{width}-{glass}.png'), full_page=True)
                if glass and width in (1440, 390):
                    page.evaluate("document.querySelector('.main').scrollTop=document.querySelector('.main').scrollHeight")
                    wait_frame(page)
                    page.screenshot(path=str(output_dir / f'settings-{target}-{width}-bottom.png'))
                    page.evaluate("document.querySelector('.main').scrollTop=0")
                print(f'PASS layout glass={glass} {width}px {target}: columns={[g["columns"] for g in metrics["grids"]]}, overflow={metrics["overflow"]}, equal row heights')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        result = page.evaluate(r"""async()=>{
          window.entryRects=[];
          if(!window.entryHook){window.entryHook=true;const proto=WebGLRenderingContext.prototype,get=proto.getUniformLocation,set=proto.uniform4fv,rects=new WeakSet();
            proto.getUniformLocation=function(p,n){const l=get.call(this,p,n);if(l&&n==='u_rect')rects.add(l);return l};
            proto.uniform4fv=function(l,v){if(rects.has(l))entryRects.push(Array.from(v));return set.call(this,l,v)};
          }
          showTarget('store',true);const root=document.querySelector('.plugin-root'),events=[],samples=[],start=performance.now();
          root.addEventListener('animationstart',e=>{if(e.target===root)events.push({name:e.animationName,at:performance.now()-start})});
          setTimeout(()=>refreshPluginPanel('config'),150);
          setTimeout(()=>switchPluginTab('store'),300);
          await new Promise(resolve=>{function sample(){setTimeout(()=>{
            const card=el('pluginPanel').querySelector('.card'),r=card.getBoundingClientRect();
            samples.push({at:performance.now()-start,transform:getComputedStyle(root).transform,opacity:getComputedStyle(root).opacity,
              aligned:entryRects.some(a=>Math.abs(a[0]-r.left-card.clientLeft)<1&&Math.abs(a[1]-r.top-card.clientTop)<1)});entryRects.length=0;
            if(performance.now()-start<1800)requestAnimationFrame(sample);else resolve();
          },0)}requestAnimationFrame(sample)});
          return {events,samples,remaining:el('content').classList.contains('page-anim'),subtab:el('pluginPanel').classList.contains('subtab-enter')};
        }""")
        assert len(result['events']) == 1 and result['events'][0]['name'] == 'glass-card-in', result['events']
        assert all(s['opacity'] == '1' for s in result['samples'])
        assert any(s['transform'] != 'none' for s in result['samples'][:5])
        assert not result['remaining'] and not result['subtab']
        if glass:
            measured = [s for s in result['samples'] if 100 < s['at'] < 850]
            assert len(measured) >= 3 and all(s['aligned'] for s in measured), measured
        page.emulate_media(reduced_motion='reduce')
        page.evaluate("showTarget('store',true)")
        assert page.evaluate("getComputedStyle(document.querySelector('.plugin-root')).animationName") == 'none'
        page.emulate_media(reduced_motion='no-preference')
        print(f'PASS plugin glass={glass}: one transform-only root entry, async refresh/subtab no replay, reduced motion respected; samples={len(result["samples"])}')
    assert not errors, errors
    context.close()
    browser.close()


def main():
    test_background_endpoint()
    output_dir = Path(tempfile.mkdtemp(prefix='xcbot-glass-'))
    with sync_playwright() as pw:
        test_refresh_frames(pw, output_dir)
        test_subtab_entry(pw)
        test_texture_blur(pw, output_dir)
        for engine in ('chromium', 'webkit'):
            run_engine(pw, engine, output_dir)
        run_engine(pw, 'chromium', output_dir, no_webgl=True)
        test_full_webkit(pw, output_dir)
        test_gpu_pixels(pw)
        test_long_displacement_pixels(pw)
        test_theme_texture(pw, output_dir)
    print('截图目录：', output_dir)
    print('说明：以上为 Playwright WebKit/Chromium；尚未代表 iPhone 真机已通过。')


if __name__ == '__main__':
    main()
