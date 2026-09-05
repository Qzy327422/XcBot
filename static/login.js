const el=id=>document.getElementById(id);
// 图标一律用 SVG，不用 emoji：emoji 由系统字体渲染，各平台造型不一、还会跟不上主题色。
const SVG_SUN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
const SVG_MOON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
const SVG_EYE='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>';
const SVG_EYE_OFF='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 6.2A9.9 9.9 0 0 1 12 6c6.4 0 10 7 10 7a17 17 0 0 1-2.4 3.2M6.5 7.6A17 17 0 0 0 2 13s3.6 7 10 7a9.7 9.7 0 0 0 4.2-.9"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M3 3l18 18"/></svg>';
function setTheme(t){document.documentElement.dataset.theme=t;localStorage.webuiTheme=t;const b=el('themeBtn');if(b)b.innerHTML=t==='light'?SVG_SUN:SVG_MOON}
function toggleTheme(){setTheme((document.documentElement.dataset.theme||'dark')==='dark'?'light':'dark')}
// 还原主界面的外观（主题预设 + 自定义背景图），让登录页和登录后长得一样。
// 数据来自主界面登录后写入的 localStorage，不向后端请求任何东西——登录页没有
// Token，要取配置或背景图就得开一个匿名可访问的接口，登录前不该有那种出口。
// 没有缓存（首次访问、清过浏览器数据）时什么都不做，退回内置渐变色。
function applyCachedLook(){
  try{
    const look=JSON.parse(localStorage.getItem('xcbotLoginLook')||'null');
    if(!look)return;
    const root=document.documentElement;
    if(look.preset)root.dataset.webuiPreset=String(look.preset);
    const blur=Math.max(0,Math.min(40,Number(look.blur||0)));
    root.style.setProperty('--user-bg-blur',blur+'px');
    // 只接受 data: 与 http(s): 两种形态，别的一律忽略：这个值虽然出自本地缓存，
    // 但仍会被拼进 CSS url()，不做限制等于给 XSS 留一个注入点。
    const bg=String(look.bg||'');
    if(bg&&(/^data:image\//i.test(bg)||/^https?:\/\//i.test(bg))){
      root.style.setProperty('--user-bg-image','url("'+bg.replace(/["\\]/g,'')+'")');
      root.classList.add('has-bg');
    }
  }catch(e){}
}
function togglePwd(){const t=el('tok'),b=el('eyeBtn');const show=t.type==='password';t.type=show?'text':'password';if(b){b.innerHTML=show?SVG_EYE_OFF:SVG_EYE;b.title=show?'隐藏':'显示'}}
// 让浏览器/密码管理器记住 Token。Chromium 的「保存密码」启发式只在表单真的提交并
// 发生导航时触发，本页是 fetch 校验 + location.href 跳转，那条启发式不会命中，
// 所以校验通过后显式用凭据管理 API 登记一次。
// 本页只有一个 Token 框、没有账号框（加一个纯摆设的账号框太突兀），凭据的 id
// 这里固定用 'webui'——它只是密码管理器条目上显示的名字，不参与任何校验。
// 非安全上下文（用 http:// 加公网 IP 或域名访问，localhost 除外）下
// PasswordCredential 不存在，直接跳过；Firefox 不实现该接口，靠自身启发式处理。
// 加超时上限：store() 在部分浏览器里会等用户处理保存气泡，不能让它卡住跳转。
async function rememberCred(t){try{if(!window.PasswordCredential||!navigator.credentials||!navigator.credentials.store)return;
const p=navigator.credentials.store(new PasswordCredential({id:'webui',password:t,name:'XcBot WebUI'}));
await Promise.race([p,new Promise(r=>setTimeout(r,1200))])}catch(e){}}
async function login(e){e.preventDefault();const t=el('tok').value.trim(),m=el('msg'),b=el('btn'),box=el('box');if(!t){m.textContent='请输入访问 Token';return}b.disabled=true;b.textContent='验证中...';try{const r=await fetch('/api/ui-state',{headers:{'X-WebUI-Token':t},cache:'no-store'});const j=await r.json().catch(()=>({ok:false,error:'验证失败'}));
// 服务端会带上「还可尝试 N 次」或「已锁定 N 分钟」，原样显示，别用固定文案盖掉
if(!r.ok||!j.ok)throw new Error(j.error||'Token 不正确');
localStorage.webuiToken=t;await rememberCred(t);location.href='/'}catch(err){m.textContent=err&&err.message?err.message:'Token 不正确或已失效';box.classList.remove('shake');void box.offsetWidth;box.classList.add('shake')}finally{b.disabled=false;b.textContent='登录'}}
applyCachedLook();
setTheme(localStorage.webuiTheme||'dark');
// 图标在这里注入，不写死在 HTML 里：SVG 内联进 LOGIN_HTML 会让那一行更难读，
// 而且开合状态的两个图标本来就得由 JS 换。
(function(){const b=el('eyeBtn');if(b){b.innerHTML=SVG_EYE;b.title='显示'}})();