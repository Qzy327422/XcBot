let state={bundle:null,current:'welcome',section:'',dirty:false,saving:false,lastInputAt:0,expectedReloadAfterUpdate:false,apiFailCount:0,reloadTimer:null};
let modelInputState={providerIndex:-1,kind:'chat'};
const featureFieldMap={sensitive_filter:['Others.sensitive_words'],ai_chat:['Others.llm_split.enabled','Others.llm_split.mode','Others.llm_split.prompt_suffix','Others.llm_split.split_regex','Others.llm_split.filter_regex','Others.llm_split.max_chars_no_split'],group_chat:['Others.group_random_reply_probability','Others.group_random_reply_quote'],group_chat_context:['Others.group_chat_context_max_messages'],emoji_plus_one:['Others.emoji_plus_one_cooldown_seconds'],poke_reply:['Others.poke_cooldown_seconds'],group_join_welcome:['Others.group_join_welcome_text','Others.group_join_welcome_send_avatar'],http_send_api:[],split_reply_quote:[],weak_blacklist:['Others.weak_blacklist_trigger_probability','Others.weak_blacklist_users'],summary:['Others.summary_max_messages'],compression_commands:[],plugins_external:[]};
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
// 首次用 ?token= 打开时把凭据转存进 localStorage，并立刻从地址栏抹掉：
// 留在 URL 里会进浏览器历史、Referer 和反代日志。
(function(){try{const u=new URL(location.href),t=u.searchParams.get('token');
if(t){localStorage.webuiToken=t;u.searchParams.delete('token');
history.replaceState(null,'',u.pathname+(u.search||'')+(u.hash||''))}}catch(e){}})();
const token=()=>localStorage.webuiToken||'';
const el=id=>document.getElementById(id);
const DRAFT_KEY='xcbotWebuiFormDraft';
function loadDraft(){try{return JSON.parse(localStorage.getItem(DRAFT_KEY)||'null')||null}catch(e){return null}}
function saveDraft(){try{if(state.bundle?.form_values)localStorage.setItem(DRAFT_KEY,JSON.stringify({fingerprint:state.bundle.config_fingerprint||'',values:state.bundle.form_values}))}catch(e){}}
function clearDraft(){try{localStorage.removeItem(DRAFT_KEY)}catch(e){}}
function sameDraftValues(a,b){try{return JSON.stringify(a)===JSON.stringify(b)}catch(e){return false}}
function applyDraft(bundle){const draft=loadDraft();if(!draft||!bundle?.form_values)return;if(!draft.fingerprint||!bundle.config_fingerprint||draft.fingerprint!==bundle.config_fingerprint){clearDraft();return}if(sameDraftValues(draft.values||{},bundle.form_values||{})){clearDraft();state.dirty=false;state.lastInputAt=0;return}const changed=Object.keys(draft.values||{}).some(k=>JSON.stringify(draft.values[k])!==JSON.stringify(bundle.form_values[k]));if(!changed){clearDraft();state.dirty=false;state.lastInputAt=0;return}bundle.form_values=Object.assign({},bundle.form_values,draft.values||{});state.dirty=true;state.lastInputAt=Date.now();const save=el('saveState');if(save)save.textContent='有未保存草稿'}
async function api(path,opt={}){opt.headers=Object.assign({'Content-Type':'application/json','X-WebUI-Token':token()},opt.headers||{});const r=await fetch(path,opt),j=await r.json().catch(()=>({ok:false,error:'请求失败'}));if(r.status===401){localStorage.removeItem('webuiToken');location.href='/auth/login';const err=new Error('请先登录');err.status=401;throw err}if(!j.ok){const err=new Error(j.error||'请求失败');err.status=r.status;throw err}return j.data??j}
function toast(msg,ok=true){const t=el('toast'),save=el('saveState');if(t){t.textContent=msg;t.className='toast show '+(ok?'ok':'bad');clearTimeout(t._timer);t._timer=setTimeout(()=>t.classList.remove('show'),2600)}if(save)save.textContent=msg}
const NAV_GROUPS=[
  {key:'daily',title:'日常使用',pages:['welcome','stats']},
  {key:'bot',title:'机器人与 AI',pages:['bot','providers','persona','role-life','ai','features','proactive','agent','knowledge','store']},
  {key:'system',title:'系统与运行',pages:['security','connection','webui','trace','logs']},
];
const pages=()=>state.bundle?.ui_schema||[];
function navigationGroups(){
  const available=new Map(pages().map(p=>[p.key,p]));
  const groups=NAV_GROUPS.map(g=>({...g,items:g.pages.map(key=>available.get(key)).filter(Boolean)}));
  const known=new Set(NAV_GROUPS.flatMap(g=>g.pages));
  groups[groups.length-1].items.push(...pages().filter(p=>!known.has(p.key)&&!['chatroom','debug'].includes(p.key)));
  return groups.filter(g=>g.items.length);
}
function navGroupCollapsed(key){try{return JSON.parse(localStorage.webuiNavCollapsed||'{}')[key]===true}catch(e){return false}}
function toggleNavGroup(key){
  let collapsed;try{collapsed=JSON.parse(localStorage.webuiNavCollapsed||'{}')}catch(e){collapsed={}}
  const next=!navGroupCollapsed(key);collapsed[key]=next;localStorage.webuiNavCollapsed=JSON.stringify(collapsed);
  const nav=el('nav');
  const groupEl=[...(nav?.querySelectorAll?.('.nav-group')||[])].find(g=>g.dataset?.group===key);
  if(groupEl){
    groupEl.classList.toggle('is-collapsed',next);
    groupEl.querySelector?.('.nav-group-title')?.setAttribute('aria-expanded',String(!next));
    const itemsEl=groupEl.querySelector?.('.nav-group-items');
    if(!next&&itemsEl){
      groupEl.classList.remove('nav-group-opening');void itemsEl.offsetWidth;groupEl.classList.add('nav-group-opening');
      clearTimeout(groupEl._openTimer);groupEl._openTimer=setTimeout(()=>groupEl.classList.remove('nav-group-opening'),280);
    }else{
      groupEl.classList.remove('nav-group-opening');
    }
    requestAnimationFrame(updateNavIndicator);
    return;
  }
  if(nav)nav.dataset.signature='';renderNav();requestAnimationFrame(updateNavIndicator);
}
const routeAlias={config:'bot',normal:'bot',dashboard:'welcome',home:'welcome',statistics:'stats',debug:'debug',webui_appearance:'webui'};
const publicRoute={welcome:'welcome',stats:'stats',bot:'config',ai:'ai',providers:'providers',knowledge:'knowledge',persona:'persona',features:'features',agent:'agent',security:'security',connection:'connection',webui:'webui',logs:'logs',chatroom:'chatroom',debug:'debug',store:'store',trace:'trace'};
function splitHashRoute(){let raw=decodeURIComponent((location.hash||'').replace(/^#\/?/,''));if(!raw)return {page:localStorage.webuiPage||'welcome',section:''};const parts=raw.split('#');const page=(parts.shift()||'').replace(/^\//,'')||'welcome';return {page,section:parts.join('#')}}
function normalizeRoutePage(page){return routeAlias[page]||page||'welcome'}
function routeForPage(page,section=''){const base=publicRoute[page]||page||'welcome';return '#/'+base+(section?'#'+encodeURIComponent(section):'')}
const extraRoutes=['chatroom','debug','store'];
function isValidPage(k){return extraRoutes.includes(k)||pages().some(p=>p.key===k)}
function applyRoute(){const r=splitHashRoute();let page=normalizeRoutePage(r.page);if(pages().length&&!isValidPage(page))page='welcome';if(page==='chatroom'&&state.current!=='chatroom'){chatState._enterBottom=true;if(chatState._loaded)chatRefreshHeaderOptions()}state.current=page;state.section=r.section||'';localStorage.webuiPage=page}
function updateRoute(page,section='',replace=false){const target=routeForPage(page,section);if(location.hash===target){applyRoute();return}if(replace)history.replaceState(null,'',target);else location.hash=target;applyRoute()}
const meta=()=>pages().find(x=>x.key===state.current)||pages()[0]||{title:'WebUI',desc:''};
function setTheme(t){document.documentElement.dataset.theme=t;localStorage.webuiTheme=t;const btn=el('themeBtn');if(btn)btn.innerHTML=t==='light'?'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px;vertical-align:-2px;margin-right:5px"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>浅色':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px;vertical-align:-2px;margin-right:5px"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>深色'}
// 背景图的文件名带内容哈希，换图必然换 URL，不需要额外的缓存击穿参数。
/*
 * Liquid glass displacement map adapted from shuding/liquid-glass.
 * Copyright (c) 2025 Shu Ding. MIT License.
 * The warp-layer wiring (backdrop-filter blur + plain filter:url) follows liquid-glass-react.
 * Copyright (c) 2025 MAX ROVENSKY. MIT License.
 */
// 读取折射强度（0-30，默认 5；5 等同于旧版的原始强度）。
// 注意 getPropertyValue 在未设置时返回 ""，Number("")===0，会把强度静默归零，
// 所以必须先判空再转数字。
const glassRefraction=()=>{
  const raw=getComputedStyle(document.documentElement).getPropertyValue('--liquid-glass-refraction').trim();
  const v=raw===''?NaN:Number(raw);
  return Math.max(0,Math.min(30,isNaN(v)?5:v));
};
// 触屏/移动设备判定用媒体查询而不是 UA 嗅探。
const matchMediaCoarse=()=>matchMedia('(pointer:coarse)').matches;
const narrowViewport=()=>matchMedia('(max-width:980px)').matches;
const liquidGlassWebKitTarget=()=>{
  const ua=navigator.userAgent||'';
  const ios=/iPhone|iPad|iPod/i.test(ua)||(/Macintosh/i.test(ua)&&Number(navigator.maxTouchPoints||0)>1);
  return ios||(/AppleWebKit/i.test(ua)&&!/(Chrome|CriOS|Edg|OPR)/i.test(ua));
};
const liquidGlassController=(()=>{
  const SIZE=256;
  let built=false;
  const root=()=>document.documentElement;
  const smoothStep=(a,b,t)=>{t=Math.max(0,Math.min(1,(t-a)/(b-a)));return t*t*(3-2*t)};
  const len=(x,y)=>Math.sqrt(x*x+y*y);
  const roundedRectSDF=(x,y,w,h,r)=>{const qx=Math.abs(x)-w+r,qy=Math.abs(y)-h+r;return Math.min(Math.max(qx,qy),0)+len(Math.max(qx,0),Math.max(qy,0))-r};
  function nodes(){return {image:document.getElementById('xcbot-liquid-map'),displacement:['r','g','b'].map(k=>document.getElementById('xcbot-liquid-displacement-'+k)).filter(Boolean)}}
  // CSS.supports 只能确认语法支持；WebKit 的壁纸折射另走显式图片源，
  // 不把支持 blur 当作支持 backdrop 与 SVG 位移的组合。
  function unsupported(){
    const css=window.CSS;
    if(liquidGlassWebKitTarget())return css?.supports?.('filter','url("#xcbot-liquid-glass")')?'':'no-filter';
    if(/firefox/i.test(navigator.userAgent))return 'browser';
    const supports=!!(css?.supports?.('backdrop-filter','blur(1px)')||css?.supports?.('-webkit-backdrop-filter','blur(1px)'));
    return supports?'':'no-backdrop';
  }
  function setState(reason){const bad=!!reason;root().classList.toggle('liquid-glass-fallback',bad);root().dataset.liquidGlassState=bad?('fallback-'+reason):'ready'}
  // k<1 使中心也有轻微放大折射，靠近圆角边缘时 k 急剧变小形成边缘弯曲
  function build(){
    const n=nodes();if(!n.image||n.displacement.length!==3)return false;
    const cv=document.createElement('canvas');cv.width=SIZE;cv.height=SIZE;
    const ctx=cv.getContext('2d');if(!ctx)return false;
    const img=ctx.createImageData(SIZE,SIZE),raw=[];let max=0;
    for(let y=0;y<SIZE;y++)for(let x=0;x<SIZE;x++){
      const ux=x/(SIZE-1),uy=y/(SIZE-1),ix=ux-.5,iy=uy-.5;
      const sdf=roundedRectSDF(ix,iy,.44,.44,.16);
      const scaled=smoothStep(0,1,smoothStep(.14,-.02,sdf));
      const k=1-scaled*.34-.03;
      const dx=(ix*k+.5-ux)*SIZE,dy=(iy*k+.5-uy)*SIZE;
      raw.push(dx,dy);max=Math.max(max,Math.abs(dx),Math.abs(dy));
    }
    max=Math.max(1,max);let i=0;
    for(let p=0;p<img.data.length;p+=4){
      img.data[p]=Math.max(0,Math.min(255,(raw[i++]/max+.5)*255));
      img.data[p+1]=Math.max(0,Math.min(255,(raw[i++]/max+.5)*255));
      img.data[p+2]=128;img.data[p+3]=255;
    }
    ctx.putImageData(img,0,0);
    const href=cv.toDataURL('image/png');
    n.image.setAttribute('href',href);
    n.image.setAttributeNS('http://www.w3.org/1999/xlink','xlink:href',href);
    const refraction=glassRefraction();
    // 滑块上限抬到 30 后位移量会非常大，超过 ~120 就会把边缘扯成明显断层，
    // 这里用平方根收敛，让小数值仍然线性、大数值不会失控。
    const BASE_SCALE=Math.min(120,(refraction<=10?refraction:10+Math.sqrt(refraction-10)*3)*8);
    n.displacement[0].setAttribute('scale',String(BASE_SCALE*1.1));
    n.displacement[1].setAttribute('scale',String(BASE_SCALE));
    n.displacement[2].setAttribute('scale',String(BASE_SCALE*.9));
    if(liquidGlassWebKitTarget()){
      document.getElementById('xcbot-liquid-glass-soft')?.remove();
      const soft=n.image.parentNode.cloneNode(true);
      soft.id='xcbot-liquid-glass-soft';
      soft.querySelectorAll('[id]').forEach(node=>node.removeAttribute('id'));
      const softBase=Math.min(120,(refraction<=10?refraction:10+Math.sqrt(refraction-10)*3)*8);
      soft.querySelectorAll('feDisplacementMap').forEach((node,i)=>node.setAttribute('scale',String(Math.max(1,softBase*0.3-i*softBase*0.025))));
      n.image.parentNode.parentNode.appendChild(soft);
    }
    // build() 成功后记下本次实际使用的强度，供 syncRefraction 判断是否需要重建。
    lastRefraction=refraction;
    return true
  }
  // 记录 build() 时实际用的强度，syncRefraction 据此判断要不要重建。
  let lastRefraction=null;
  function start(){
    const bad=unsupported();if(bad){setState(bad);return false}
    if(!built){try{built=build()}catch(e){built=false}}
    if(!built){setState('no-map');return false}
    setState('');return true
  }
  function syncRefraction(force){
    if(!built)return false;
    const r=glassRefraction();
    // 不能提前把 lastRefraction 改成新值：build() 才是真正写入位移图的地方，
    // 先改会导致「值变了但没重建」，滑块拖动时看起来毫无反应。
    if(!force&&r===lastRefraction)return false;
    try{build();lastRefraction=r}catch(e){return false}
    return true;
  }
  return {
    setEnabled(v){
      if(v){return start()}
      root().classList.remove('liquid-glass-fallback');root().dataset.liquidGlassState='off';return false
    },
    syncRefraction
  };
})();
// 折射强度：0-10 映射到 0-1.0，5 为原始强度（1.0）
function refractionStrength(){return glassRefraction()/5}
// 固定画布只更新玻璃区域，纹理坐标始终相对视口；避免副本随 DOM 滚动后再跳回。
function createLiquidGlassGpu(image,onLost){
  const canvas=document.createElement('canvas');canvas.className='glass-gpu-layer';
  canvas.setAttribute('aria-hidden','true');
  // 按需绘制后会停帧；保留缓冲，避免 WebKit 在下一次合成时丢掉静态结果。
  const gl=canvas.getContext('webgl',{alpha:true,premultipliedAlpha:true,preserveDrawingBuffer:true,antialias:false,depth:false,stencil:false});
  if(!gl)throw new Error('no-webgl');
  let program=null,buffer=null,texture=null,disposed=false,lost=false;
  // 纹理空间的模糊半径随 cover 缩放变化；首次 draw 才上传，滚动不重建。
  let textureRevision=-1,textureKernel='';
  const shaders=[];
  const stats={textureUploads:0,frames:0,regions:0,lastJsMs:0,resolution:[0,0]};
  function cleanup(){
    if(disposed)return;disposed=true;
    canvas.removeEventListener('webglcontextlost',contextLost);canvas.remove();
    if(texture)gl.deleteTexture(texture);if(buffer)gl.deleteBuffer(buffer);
    if(program)gl.deleteProgram(program);shaders.forEach(s=>gl.deleteShader(s));
  }
  function contextLost(e){e.preventDefault();lost=true;onLost()}
  function shader(type,source){
    const s=gl.createShader(type);shaders.push(s);gl.shaderSource(s,source);gl.compileShader(s);
    if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw new Error('shader-compile');return s;
  }
  try{
    const vertex=shader(gl.VERTEX_SHADER,`
      precision highp float;
      attribute vec2 a_pos;
      uniform vec4 u_rect;
      uniform vec2 u_view;
      varying vec2 v_pos;
      void main(){v_pos=u_rect.xy+a_pos*u_rect.zw;gl_Position=vec4(v_pos.x/u_view.x*2.0-1.0,1.0-v_pos.y/u_view.y*2.0,0.0,1.0);}
    `);
    const fragment=shader(gl.FRAGMENT_SHADER,`
      precision highp float;
      uniform sampler2D u_image;
      uniform vec4 u_rect,u_radii,u_cover,u_clipBox;
      uniform vec4 u_clips[8],u_clipRadii[8];
      uniform int u_clipCount;
      uniform vec3 u_base;
      uniform float u_strength,u_pixel,u_sourceOpacity;
      varying vec2 v_pos;
      float radiusAt(vec2 p,vec4 radii){return p.y<0.0?(p.x<0.0?radii.x:radii.y):(p.x<0.0?radii.w:radii.z);}
      float ss(float a,float b,float t){t=clamp((t-a)/(b-a),0.0,1.0);return t*t*(3.0-2.0*t);}
      float roundedSdf(vec2 p){
        vec2 q=abs(p)-vec2(.44)+vec2(.16);
        return min(max(q.x,q.y),0.0)+length(max(q,0.0))-.16;
      }
      float pcScaleAt(vec2 uv){
        vec2 p=uv-.5;
        float scaled=ss(0.0,1.0,ss(.14,-.02,roundedSdf(p)));
        return 1.0-scaled*.34-.03;
      }
      float distanceToBox(vec2 p,vec4 box,vec4 radii){
        vec2 local=p-box.xy-box.zw*.5;float r=radiusAt(local,radii);
        vec2 q=abs(local)-box.zw*.5+r;
        return length(max(q,0.0))+min(max(q.x,q.y),0.0)-r;
      }
      vec3 sampleImage(vec2 p){
        vec2 uv=clamp((p-u_cover.xy)/u_cover.zw,0.0,1.0);uv.y=1.0-uv.y;
        // 纹理已在 uploadTexture 中完成原生高斯或三遍 box 高斯近似，
        // 这里只做单点采样。之前在这里撒离散采样点会因为间距小于图像细节
        // 而产生「多份拷贝」的重影，看着头晕；预模糊是连续的，不会有这个问题。
        vec4 c=texture2D(u_image,uv);
        return mix(u_base,c.rgb,c.a*u_sourceOpacity);
      }
      void main(){
        if(v_pos.x<u_clipBox.x||v_pos.y<u_clipBox.y||v_pos.x>u_clipBox.z||v_pos.y>u_clipBox.w)discard;
        float dist=distanceToBox(v_pos,u_rect,u_radii);
        float mask=1.0-smoothstep(-u_pixel,0.0,dist);
        for(int i=0;i<8;i++){
          if(i<u_clipCount)mask*=1.0-smoothstep(-u_pixel,0.0,distanceToBox(v_pos,u_clips[i],u_clipRadii[i]));
        }
        if(mask<.01)discard;
        vec2 local=v_pos-u_rect.xy-u_rect.zw*.5;
        vec2 uv=local/u_rect.zw+.5;
        float k=pcScaleAt(uv);
        // 与 PC 的 canvas 位移图一致：中心也缩放，边缘曲率额外增强。
        vec2 bend=local*(k-1.0)*u_strength;
        vec3 c=sampleImage(v_pos-bend);
        c.r=sampleImage(v_pos-bend*1.10).r;
        c.b=sampleImage(v_pos-bend*.90).b;
        gl_FragColor=vec4(c*mask,mask);
      }
    `);
    program=gl.createProgram();gl.attachShader(program,vertex);gl.attachShader(program,fragment);gl.linkProgram(program);
    if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw new Error('shader-link');
    gl.useProgram(program);
    buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
    gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([0,0,1,0,0,1,0,1,1,0,1,1]),gl.STATIC_DRAW);
    const pos=gl.getAttribLocation(program,'a_pos');gl.enableVertexAttribArray(pos);gl.vertexAttribPointer(pos,2,gl.FLOAT,false,0,0);
    texture=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,texture);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
    const probeContext=document.createElement('canvas').getContext('2d');
    const nativeBlur=!!probeContext&&typeof probeContext.filter==='string';
    const max=Math.min(nativeBlur?4096:2048,gl.getParameter(gl.MAX_TEXTURE_SIZE));
    // 把壁纸按目标模糊半径预先高斯模糊一次，再上传给 GPU。
    // 为什么要预模糊而不是在 shader 里多点采样：采样点之间的距离一旦超过
    // 图像细节的尺度，拿到的就是同一批细节的「多份拷贝」，看起来就是重影，
    // 越大越晕。CPU 侧的 Gaussian blur 是连续卷积，不存在离散空洞。
    // 模糊半径按纹理像素算：屏幕 px / cover 缩放比。
    function uploadTexture(texBlur){
      let source=image;
      if(Math.max(image.naturalWidth,image.naturalHeight)>max){
        const scaled=document.createElement('canvas'),ratio=max/Math.max(image.naturalWidth,image.naturalHeight);
        scaled.width=Math.max(1,Math.round(image.naturalWidth*ratio));scaled.height=Math.max(1,Math.round(image.naturalHeight*ratio));
        scaled.getContext('2d').drawImage(image,0,0,scaled.width,scaled.height);source=scaled;
      }
      if(texBlur>0.1){
        const cv=document.createElement('canvas');
        cv.width=source.naturalWidth||source.width;cv.height=source.naturalHeight||source.height;
        const ctx=cv.getContext('2d',{willReadFrequently:false});
        if(ctx&&nativeBlur){
          // 延展边缘后模糊，再裁回原坐标；不能拉伸整张图片来填充模糊边界。
          const pad=Math.ceil(texBlur*3),w=cv.width,h=cv.height;
          const padded=document.createElement('canvas');padded.width=w+pad*2;padded.height=h+pad*2;
          const pc=padded.getContext('2d');if(!pc)throw new Error('blur-canvas');
          pc.drawImage(source,pad,pad);
          pc.imageSmoothingEnabled=false;
          pc.drawImage(source,0,0,w,1,pad,0,w,pad);pc.drawImage(source,0,h-1,w,1,pad,pad+h,w,pad);
          pc.drawImage(source,0,0,1,h,0,pad,pad,h);pc.drawImage(source,w-1,0,1,h,pad+w,pad,pad,h);
          for(const [sx,sy,dx,dy] of [[0,0,0,0],[w-1,0,pad+w,0],[0,h-1,0,pad+h],[w-1,h-1,pad+w,pad+h]])pc.drawImage(source,sx,sy,1,1,dx,dy,pad,pad);
          ctx.filter=`blur(${texBlur.toFixed(3)}px)`;ctx.drawImage(padded,-pad,-pad);ctx.filter='none';
          source=cv;
        }else if(ctx&&texBlur>0.05){
          // 无 Canvas filter 的 Safari：在原纹理上做三遍分离 box 高斯近似，
          // 不缩成小图再放大，避免斜线台阶；预乘 alpha 后滤波，透明边不会发黑。
          const w=cv.width,h=cv.height;
          ctx.drawImage(source,0,0);
          const pixels=ctx.getImageData(0,0,w,h),a=new Float32Array(pixels.data.length),b=new Float32Array(a.length);
          for(let i=0;i<a.length;i+=4){const alpha=pixels.data[i+3]/255;a[i]=pixels.data[i]*alpha;a[i+1]=pixels.data[i+1]*alpha;a[i+2]=pixels.data[i+2]*alpha;a[i+3]=pixels.data[i+3]}
          // 相邻半径连续混合，避免整数 box 核让小数模糊档位完全相同。
          const radius=Math.floor((Math.sqrt(1+4*texBlur*texBlur)-1)/2);
          const blend=(texBlur*texBlur-radius*(radius+1))/(2*radius+2);
          function boxPass(input,output,vertical){
            const length=vertical?h:w,lines=vertical?w:h,stride=vertical?w*4:4,diameter=radius*2+1;
            for(let line=0;line<lines;line++){
              const start=vertical?line*4:line*w*4;
              for(let channel=0;channel<4;channel++){
                let sum=0;
                for(let k=-radius;k<=radius;k++)sum+=input[start+Math.max(0,Math.min(length-1,k))*stride+channel];
                for(let p=0;p<length;p++){
                  const outer=sum+input[start+Math.max(0,p-radius-1)*stride+channel]+input[start+Math.min(length-1,p+radius+1)*stride+channel];
                  output[start+p*stride+channel]=(1-blend)*sum/diameter+blend*outer/(diameter+2);
                  sum+=input[start+Math.min(length-1,p+radius+1)*stride+channel]-input[start+Math.max(0,p-radius)*stride+channel];
                }
              }
            }
          }
          for(let pass=0;pass<3;pass++){boxPass(a,b,false);boxPass(b,a,true)}
          for(let i=0;i<a.length;i+=4){const alpha=a[i+3];pixels.data[i+3]=alpha;for(let c=0;c<3;c++)pixels.data[i+c]=alpha>0?a[i+c]*255/alpha:0}
          ctx.putImageData(pixels,0,0);source=cv;
        }
      }
      if(texBlur>0.1&&source===image)throw new Error('blur-canvas');
      gl.bindTexture(gl.TEXTURE_2D,texture);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,true);
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,source);
      if(gl.getError()!==gl.NO_ERROR)throw new Error('texture-upload');stats.textureUploads++;
      stats.textureSize=[source.naturalWidth||source.width,source.naturalHeight||source.height];
    }
    const locations={};['rect','view','radii','cover','clipBox','clips[0]','clipRadii[0]','clipCount','base','strength','pixel','image','sourceOpacity'].forEach(k=>locations[k]=gl.getUniformLocation(program,'u_'+k));
    gl.uniform1i(locations.image,0);gl.enable(gl.BLEND);gl.blendFunc(gl.ONE,gl.ONE_MINUS_SRC_ALPHA);
    gl.clearColor(0,0,0,0);gl.disable(gl.DEPTH_TEST);
    canvas.addEventListener('webglcontextlost',contextLost);document.body.insertBefore(canvas,document.body.firstChild);
    const clipData=new Float32Array(32),radiusData=new Float32Array(32);
    const maxBuffer=gl.getParameter(gl.MAX_RENDERBUFFER_SIZE);
    return {canvas,dispose:cleanup,diagnostics:()=>({...stats,resolution:stats.resolution.slice(),refractionModel:'xcbot-sdf-v1',strengthProfile:'pc-base-scale-40-soft-35%'}),draw(regions,geometry){
      if(disposed||lost)return;
      const start=performance.now(),{width,height,cover,blur,base,animating,sourceRevision=0,sourceOpacity=.82}=geometry;
      const sourceW=image.naturalWidth||image.width,sourceH=image.naturalHeight||image.height;
      const textureWidth=Math.max(sourceW,sourceH)>max?Math.max(1,Math.round(sourceW*max/Math.max(sourceW,sourceH))):sourceW;
      const sigma=cover[2]>0?blur*textureWidth/cover[2]:blur;
      const kernel=sigma<=0.1?'none':nativeBlur?sigma.toFixed(3):String(sigma);
      if(sourceRevision!==textureRevision||kernel!==textureKernel){
        uploadTexture(sigma);textureRevision=sourceRevision;textureKernel=kernel;
        stats.blurMethod=sigma>0.1?(nativeBlur?'canvas-gaussian':'three-box-gaussian'):'none';
        stats.textureSigma=sigma;
        gl.bindTexture(gl.TEXTURE_2D,texture);gl.uniform1i(locations.image,0);
      }
      // 像素预算：移动端 GPU 带宽有限，dpr 2.75 直接按 2 仍偏大；
      // 桌面 4K 高 DPI 下 maxBuffer 也不起约束，会撑出上百 MB 的缓冲。
      // 用预算反推 dpr，移动端再压一档，动画期间进一步降到 1.0
      // （本质是模糊折射，1.0 与 1.5 的差异几乎不可见）。
      // iOS 已经验收过、本身也流畅，保持原来的 dpr 2 不动；
      // 降 dpr 只针对出问题的 Chromium 移动端。
      // 这里刻意内联判断而不调用外部辅助函数：本函数会被测试单独抽取注入。
      const ua=navigator.userAgent||'';
      const webkitIOS=/iPhone|iPad|iPod/i.test(ua)||(/AppleWebKit/i.test(ua)&&!/(Chrome|CriOS|Edg|OPR)/i.test(ua));
      const mobileGpu=(navigator.maxTouchPoints>0||window.matchMedia?.('(pointer:coarse)').matches)&&!webkitIOS;
      const dprCap=(mobileGpu?1.5:2)*(animating&&mobileGpu?0.67:1);
      const budget=mobileGpu?1600000:4200000;
      const dpr=Math.max(1,Math.min(dprCap,devicePixelRatio||1,Math.sqrt(budget/(width*height)),maxBuffer/width,maxBuffer/height));
      const w=Math.max(1,Math.round(width*dpr)),h=Math.max(1,Math.round(height*dpr));
      if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h}
      if(canvas.style.width!==width+'px')canvas.style.width=width+'px';
      if(canvas.style.height!==height+'px')canvas.style.height=height+'px';
      gl.viewport(0,0,w,h);gl.disable(gl.SCISSOR_TEST);gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.SCISSOR_TEST);
      gl.uniform2f(locations.view,width,height);gl.uniform4fv(locations.cover,cover);gl.uniform3fv(locations.base,base);
      gl.uniform1f(locations.pixel,Math.max(1/dpr,.75));gl.uniform1f(locations.sourceOpacity,sourceOpacity);
      // 折射强度每次 draw 只读一次样式，不再在每个 region 里 getComputedStyle。
      // 同样内联：本函数会被测试单独抽取注入。
      const rawStrength=getComputedStyle(document.documentElement).getPropertyValue('--liquid-glass-refraction').trim();
      const strengthNum=rawStrength===''?NaN:Number(rawStrength);
      // 与 SVG 分支一致地收敛：0-10 保持线性（5=1.0 原始强度），
      // 10 以上用平方根放大，避免高值把采样推出纹理边界。
      const sr=Math.max(0,Math.min(30,isNaN(strengthNum)?5:strengthNum));
      const strengthBase=(sr<=10?sr:10+Math.sqrt(sr-10)*3)/5;
      for(const item of regions){
        const x0=Math.max(0,Math.floor(Math.max(item.rect[0],item.clipBox[0])*dpr)),x1=Math.min(w,Math.ceil(Math.min(item.rect[0]+item.rect[2],item.clipBox[2])*dpr));
        const y0=Math.max(0,Math.floor(Math.max(item.rect[1],item.clipBox[1])*dpr)),y1=Math.min(h,Math.ceil(Math.min(item.rect[1]+item.rect[3],item.clipBox[3])*dpr));
        gl.scissor(x0,h-y1,Math.max(0,x1-x0),Math.max(0,y1-y0));
        gl.uniform4fv(locations.rect,item.rect);gl.uniform4fv(locations.radii,item.radii);
        gl.uniform4fv(locations.clipBox,item.clipBox);gl.uniform1f(locations.strength,(item.soft?.35:1.0)*strengthBase);
        clipData.fill(0);radiusData.fill(0);const count=Math.min(8,item.clips.length);
        for(let i=0;i<count;i++){clipData.set(item.clips[i].rect,i*4);radiusData.set(item.clips[i].radii,i*4)}
        gl.uniform4fv(locations['clips[0]'],clipData);gl.uniform4fv(locations['clipRadii[0]'],radiusData);gl.uniform1i(locations.clipCount,count);
        gl.drawArrays(gl.TRIANGLES,0,6);
      }
      stats.frames++;stats.regions=regions.length;stats.resolution=[w,h];stats.lastJsMs=Number((performance.now()-start).toFixed(2));
    }};
  }catch(e){cleanup();throw new Error(['no-webgl','shader-compile','shader-link','texture-upload'].includes(e.message)?e.message:'gpu-init')}
}
// 渲染器仲裁：决定液态玻璃走哪条路径。
//   off  — 总开关关闭
//   css  — 只用 CSS（每个元素 ::before 的 backdrop-filter + SVG 折射），Chromium 的默认回退
//   svg  — WebKit 的每元素 SVG 克隆层
//   gpu  — 一张 WebGL 画布统一绘制所有玻璃区域（最快，优先）
// WebGL 探测结果按页面生命周期缓存：applyWebuiAppearance 每次 render 都会调用，
// 不缓存会不断新建上下文，撞上 Chromium 约 16 个的上下文上限。
const liquidGlassWebGLProbe=(()=>{
  let cached=null;
  return function(){
    if(cached!==null)return cached;
    try{
      const canvas=document.createElement('canvas');
      canvas.width=1;canvas.height=1;
      // 属性必须和 createLiquidGlassGpu 保持一致。
      // failIfMajorPerformanceCaveat 会挡掉 SwiftShader 软光栅——
      // 那种情况下 WebGL "可用"，但比 CSS 还慢，是最容易把优化变成负优化的场景。
      const gl=canvas.getContext('webgl',{alpha:true,premultipliedAlpha:true,preserveDrawingBuffer:true,antialias:false,depth:false,stencil:false,failIfMajorPerformanceCaveat:true});
      if(!gl){cached=false;return cached}
      const lost=gl.getExtension('WEBGL_lose_context');
      if(lost)lost.loseContext();
      cached=true;
    }catch(e){cached=false}
    return cached;
  };
})();
function liquidGlassRenderPlan(value,url,svgReady){
  const webkit=liquidGlassWebKitTarget();
  if(!value)return {engine:'off',reason:'disabled'};
  // 无壁纸使用内部主题纹理；能力不足时明确轻量后备，不逐卡运行 SVG warp。
  if(!url)return navigator.connection?.saveData===true?{engine:'lightweight',reason:'save-data'}:
    liquidGlassWebGLProbe()?{engine:'gpu',reason:'theme-gradient'}:{engine:'lightweight',reason:'no-webgl'};
  let sameOrigin=false;
  try{sameOrigin=new URL(url,location.href).origin===location.origin}catch(e){}
  // 跨域壁纸读不到像素，GPU 无法上传纹理。放在 WebGL 探测之前，避免白建上下文。
  if(!sameOrigin)return {engine:webkit?'svg':'css',reason:'external-image'};
  if(navigator.connection?.saveData===true)return {engine:webkit?'svg':'css',reason:'save-data'};
  if(!liquidGlassWebGLProbe())return {engine:webkit?'svg':'css',reason:'no-webgl'};
  // 仍可能因为 image 解码或 shader 失败而落到 startGpu 的降级。
  return {engine:'gpu',reason:'ready'};
}
const liquidGlassLensController=(()=>{
  const root=document.documentElement,ns='http://www.w3.org/2000/svg',xlink='http://www.w3.org/1999/xlink';
  const selector='.card,.sidebar,.topbar,.chat-side,.chat-main,.chat-composer,.field,.feature,.stat,.btn:not(.primary),.pill';
  const mainSelector='.main';
  let enabled=false,engine='off',image=null,source=null,imageSrc='',blobUrl='',status='off',generation=0,frame=0,listening=false,targetsDirty=true,sequence=0,request=null;
  // WebKit（Safari/iOS）沿用原有语义：控制器负责写 fallback 状态。
  // Chromium 上只负责 GPU 画布，失败要无声退回它原本的 CSS 折射路径，
  // 不能写 liquid-glass-fallback，否则会把可用的 filter:url() 折射降级成纯模糊。
  const webkitTarget=liquidGlassWebKitTarget();
  let targets=[],gpu=null,gpuReason='',movingUntil=0,lastJsMs=0,animationTracking=false,coverViewport=null,drawerTransition=false,firstFrameReady=false;
  const surfaces=new Map();
  let themeCanvas=null,themeKey='',themeRevision=0,themeBuilds=0,themeFailure='',themeDirty=true,themeSize='';
  // 与 body 的六个 ellipse farthest-corner 径向层及线性底色一致，倒序合成。
  // 只保留一张有上限的内部纹理；不创建假壁纸、不写用户缓存、不发图片请求。
  function syncThemeTexture(width,height){
    const size=width+'x'+height;if(!themeDirty&&themeSize===size)return;
    const style=getComputedStyle(root),light=root.dataset.theme==='light';
    const colors=['--bg0','--bg1','--accent-rgb','--accent2-rgb'].map(k=>style.getPropertyValue(k).trim());
    const key=JSON.stringify([width,height,light,root.dataset.webuiPreset,...colors]);
    themeDirty=false;themeSize=size;if(themeKey===key)return;
    if(!themeCanvas)themeCanvas=document.createElement('canvas');
    const ratio=Math.min(1,1536/Math.max(width,height));
    const w=Math.max(1,Math.round(width*ratio)),h=Math.max(1,Math.round(height*ratio));
    themeCanvas.width=w;themeCanvas.height=h;
    const ctx=themeCanvas.getContext('2d');if(!ctx)throw new Error('no-theme-canvas');
    const base=ctx.createLinearGradient(0,0,0,h);base.addColorStop(0,colors[0]);base.addColorStop(1,colors[1]);ctx.fillStyle=base;ctx.fillRect(0,0,w,h);
    const layers=[[.5,.5,0,light?.35:.28,.55],[.8,.2,1,light?.30:.24,.50],[.2,.8,0,light?.25:.20,.60],[.4,.6,1,light?.25:.20,.55],[.7,.7,0,light?.20:.16,.50],[.3,.3,1,light?.20:.16,.60]];
    for(const [x,y,color,alpha,stop] of layers.reverse()){
      const cx=x*w,cy=y*h,rx=Math.max(cx,w-cx)*Math.SQRT2,ry=Math.max(cy,h-cy)*Math.SQRT2;
      ctx.save();ctx.translate(cx,cy);ctx.scale(rx,ry);
      const gradient=ctx.createRadialGradient(0,0,0,0,0,stop),rgb=colors[2+color];
      gradient.addColorStop(0,`rgba(${rgb},${alpha})`);gradient.addColorStop(1,`rgba(${rgb},0)`);
      ctx.fillStyle=gradient;ctx.fillRect(-cx/rx,-cy/ry,w/rx,h/ry);ctx.restore();
    }
    // CanvasImageSource 与 Image 共用尺寸读取，natural* 只作内部兼容属性。
    themeCanvas.naturalWidth=w;themeCanvas.naturalHeight=h;
    image=themeCanvas;themeKey=key;themeRevision++;themeBuilds++;
  }
  const themeObserver=new MutationObserver(records=>{
    if(records.some(r=>r.oldValue!==root.getAttribute(r.attributeName))){themeDirty=true;schedule()}
  });
  // 记录上一次观察到的抽屉开合状态，用于判断 drawer class 变化是否真的翻转。
  let wasDrawerOpen=document.body.classList.contains('nav-open')||document.body.classList.contains('drawer-intent-open');
  const resizeObserver=window.ResizeObserver?new ResizeObserver(()=>schedule()):null;
  // 自己插入/移除的装饰 SVG 不应触发整页重新扫描。
  const mutationObserver=new MutationObserver(records=>{
    let changed=false;
    for(const record of records){
      if(record.type==='attributes'&&record.attributeName==='class'){
        const drawerOpen=document.body.classList.contains('nav-open')||document.body.classList.contains('drawer-intent-open');
        drawerTransition=true;
        movingUntil=performance.now()+360;
        animationTracking=true;
        // 抽屉开合会改变 targets 的过滤条件（侧栏进出），必须重新扫描，
        // 否则关闭后侧栏永远不会被加回玻璃目标。
        // 注意：这个 observer 挂的是 document.body 的 subtree + class 过滤，
        // 页面里任何元素换 class（导航高亮、开关切换）都会走到这里。
        // 只有抽屉状态真的翻转时才重扫，否则每次点击都全量 querySelectorAll，
        // 翻页动画会明显掉帧。
        if(drawerOpen!==wasDrawerOpen){targetsDirty=true;wasDrawerOpen=drawerOpen}
        changed=true;continue
      }
      if(record.addedNodes.length||record.removedNodes.length){
        const relevant=[...record.addedNodes,...record.removedNodes].some(n=>!(n instanceof Element&&n.matches('.glass-lens-layer')));
        if(relevant){targetsDirty=true;changed=true}
      }
    }
    if(changed)schedule();
  });
  function syncState(){
    const ready=enabled&&status==='ready'&&firstFrameReady;
    root.classList.toggle('glass-render-pending',enabled&&!ready&&(engine==='gpu'||engine==='svg'));
    if(engine==='off')root.classList.remove('liquid-glass-fallback');
    // webkit-liquid-glass 只服务 WebKit 的回退样式（把模糊挪到元素本体），
    // 对 Chromium 是错误分支，会丢折射，所以只在 WebKit 上置位。
    root.classList.toggle('webkit-liquid-glass',enabled&&webkitTarget);
    // ios-liquid-glass 收窄为「WebKit 的 SVG 克隆层就绪」，Chromium 永不置位。
    root.classList.toggle('ios-liquid-glass',ready&&webkitTarget&&engine==='svg');
    root.classList.toggle('gpu-liquid-glass',ready&&!!gpu);
    root.classList.toggle('theme-glass-fallback',engine==='lightweight');
    root.dataset.liquidGlassEngine=engine;
    root.classList.toggle('drawer-transition',drawerTransition);
    root.style.setProperty('--drawer-open',document.body.classList.contains('nav-open')?'1':'0');
    // 壁纸 CSS 路径保留原控制器状态；GPU 与无壁纸轻量后备显式报告来源。
    // 不把 Chromium 壁纸加载失败误降级为逐卡纯模糊。
    if(engine==='lightweight'){
      root.classList.add('liquid-glass-fallback');root.dataset.liquidGlassState='fallback-'+status;
    }else if(ready&&gpu){
      root.classList.remove('liquid-glass-fallback');root.dataset.liquidGlassState=source==='theme-gradient'?'gpu-theme-refraction':'gpu-wallpaper-refraction';
    }else if(enabled&&webkitTarget){
      root.classList.toggle('liquid-glass-fallback',!ready);
      root.dataset.liquidGlassState=ready?(gpu?(source==='theme-gradient'?'gpu-theme-refraction':'gpu-wallpaper-refraction'):'svg-wallpaper-refraction'):'fallback-'+status;
    }
  }
  function schedule(){
    if(!enabled||status!=='ready'||frame)return;
    frame=requestAnimationFrame(()=>{frame=0;update()});
  }
  let lastNarrow=narrowViewport();
  function onResize(){
    const narrow=narrowViewport();if(narrow!==lastNarrow){lastNarrow=narrow;targetsDirty=true}
    schedule();
  }
  function onScroll(){movingUntil=performance.now()+220;schedule()}
  function onVisible(){if(document.visibilityState==='visible'){targetsDirty=true;schedule()}}
  function onMotion(event){
    if(event.target instanceof Element&&event.target.matches(selector+',.page-anim,.chatroom,.subtab-panel,.subtab-enter,.plugin-root,.plugin-panel,.plugin-config-cards,.role-life')){
      // 翻页动画持续 0.82s，延迟最长 0.61s，总共约 1.43s。
      // 动画期间必须持续更新玻璃位置，否则模糊区域会停留在初始位置。
      movingUntil=performance.now()+(event.type==='animationstart'?1500:220);
      schedule();
    }
  }
  const motionEvents=['animationstart','animationend','animationcancel','transitionrun','transitionstart','transitionend','transitioncancel'];
  function listen(on){
    if(on===listening)return;
    listening=on;
    if(on){
      lastNarrow=narrowViewport();
      document.addEventListener('scroll',onScroll,{passive:true,capture:true});
      document.addEventListener('touchmove',onScroll,{passive:true,capture:true});
      document.addEventListener('touchend',onScroll,{passive:true,capture:true});
      window.addEventListener('resize',onResize,{passive:true});
      window.visualViewport?.addEventListener('resize',onResize,{passive:true});
      window.visualViewport?.addEventListener('scroll',schedule,{passive:true});
      // 切回前台时 rAF 曾被节流，画布可能停在旧状态，补一次重绘。
      document.addEventListener('visibilitychange',onVisible,false);
      motionEvents.forEach(type=>document.addEventListener(type,onMotion,true));
      mutationObserver.observe(document.body,{attributes:true,attributeFilter:['class'],childList:true,subtree:true});
      themeObserver.observe(root,{attributes:true,attributeOldValue:true,attributeFilter:['data-theme','data-webui-preset','style']});
      targetsDirty=true;
    }else{
      document.removeEventListener('scroll',onScroll,true);
      document.removeEventListener('touchmove',onScroll,true);
      document.removeEventListener('touchend',onScroll,true);
      window.removeEventListener('resize',onResize);
      window.visualViewport?.removeEventListener('resize',onResize);
      window.visualViewport?.removeEventListener('scroll',schedule);
      document.removeEventListener('visibilitychange',onVisible,false);
      motionEvents.forEach(type=>document.removeEventListener(type,onMotion,true));
      mutationObserver.disconnect();themeObserver.disconnect();resizeObserver?.disconnect();
    }
  }
  function removeSurface(node,surface){node.classList.remove('glass-lens-surface');surface.svg.remove();surfaces.delete(node)}
  function clear(){
    if(frame)cancelAnimationFrame(frame);
    frame=0;movingUntil=0;firstFrameReady=false;listen(false);
    surfaces.forEach((surface,node)=>removeSurface(node,surface));targets=[];
    if(gpu)gpu.dispose();gpu=null;gpuReason='';
    root.classList.remove('gpu-liquid-glass');
    if(request)request.abort();request=null;
    if(blobUrl)URL.revokeObjectURL(blobUrl);blobUrl='';imageSrc='';coverViewport=null;animationTracking=false;
    themeCanvas=null;themeKey='';themeSize='';themeDirty=true;
  }
  // 元素级滤镜是模板的克隆体，强度改了要逐个同步，否则只有模板变了、
  // 页面上实际使用的克隆还是旧 scale（iPhone 上就会看到和卡片不一致）。
  function applyFilterScale(filter,soft){
    const r=glassRefraction();
    // 和 build() 用同一套收敛曲线，保证模板与克隆体强度一致。
    const base=Math.min(120,(r<=10?r:10+Math.sqrt(r-10)*3)*8);
    filter.querySelectorAll('feDisplacementMap').forEach((n,i)=>{
      const scale=soft?Math.max(1,base*0.3-i*base*0.025):base*(i===0?1.1:i===2?.9:1);
      n.setAttribute('scale',String(scale));
    });
  }
  function makeSurface(node){
    const svg=document.createElementNS(ns,'svg'),defs=document.createElementNS(ns,'defs');
    const soft=node.matches('.field,.feature,.stat,.btn,.pill');
    const template=document.getElementById(soft?'xcbot-liquid-glass-soft':'xcbot-liquid-glass');
    if(!template)return null;
    const filter=template.cloneNode(true),id='xcbot-lens-'+(++sequence);
    filter.id=id;filter.querySelectorAll('[id]').forEach(n=>n.removeAttribute('id'));
    filter.setAttribute('filterUnits','userSpaceOnUse');filter.setAttribute('primitiveUnits','userSpaceOnUse');
    filter.setAttribute('x','0');filter.setAttribute('y','0');
    const blur=document.createElementNS(ns,'feGaussianBlur');
    blur.setAttribute('in','SourceGraphic');blur.setAttribute('result','lens-source');
    filter.querySelectorAll('feDisplacementMap').forEach(n=>n.setAttribute('in','lens-source'));
    applyFilterScale(filter,soft);
    filter.insertBefore(blur,filter.firstChild);defs.appendChild(filter);
    const group=document.createElementNS(ns,'g'),picture=document.createElementNS(ns,'image');
    group.setAttribute('filter','url(#'+id+')');
    picture.setAttribute('href',imageSrc);picture.setAttributeNS(xlink,'xlink:href',imageSrc);
    picture.setAttribute('preserveAspectRatio','none');group.appendChild(picture);
    svg.classList.add('glass-lens-layer');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');
    svg.setAttribute('preserveAspectRatio','none');svg.append(defs,group);
    node.insertBefore(svg,node.firstChild);node.classList.add('glass-lens-surface');
    const surface={svg,filter,map:filter.querySelector('feImage'),blur,picture,group,soft};surfaces.set(node,surface);
    return surface;
  }
  function attr(node,key,value){const next=String(value);if(node.getAttribute(key)!==next)node.setAttribute(key,next)}
  function gpuRegions(width,height){
    const cache=new Map(),inDrawer=document.body.classList.contains('nav-open')||document.body.classList.contains('drawer-intent-open');
    // 每帧只求值一次；宽屏侧栏常驻，窄屏侧栏才是浮动抽屉。
    const narrow=narrowViewport();
    function box(node){
      if(cache.has(node))return cache.get(node);
      const r=node.getBoundingClientRect(),s=getComputedStyle(node);
      // offsetWidth/clientWidth 为 0 时相除会得到 Infinity，||1 拦不住，
      // 必须显式判有限，否则 rect 会变成 NaN/Infinity 被当成可见。
      const ow=node.offsetWidth,oh=node.offsetHeight;
      const sx=ow>0&&r.width>0?r.width/ow:1,sy=oh>0&&r.height>0?r.height/oh:1;
      const rect=[r.left+node.clientLeft*sx,r.top+node.clientTop*sy,node.clientWidth*sx,node.clientHeight*sy];
      const radii=['borderTopLeftRadius','borderTopRightRadius','borderBottomRightRadius','borderBottomLeftRadius'].map(key=>{
        const raw=s[key],v=parseFloat(raw)||0;return Math.max(0,Math.min(rect[2]/2,rect[3]/2,(raw.includes('%')?v*Math.min(rect[2],rect[3])/100:v*Math.min(sx,sy))-node.clientLeft));
      });
      const value={rect,radii,style:s};cache.set(node,value);return value;
    }
    const regions=[];
    targets.forEach((node,index)=>{
      const own=box(node),r=own.rect,s=own.style;
      if(!r[2]||!r[3]||!isFinite(r[0])||!isFinite(r[1])||r[0]+r[2]<=0||r[1]+r[3]<=0||r[0]>=width||r[1]>=height||s.visibility==='hidden')return;
      // 侧栏永远不进 GPU 玻璃绘制：
      //   · 抽屉关闭（窄屏）：它在屏幕外，画了纯属浪费；
      //   · 抽屉打开：侧栏是浮层，滑动中原有的做法是按「最终矩形」提前画好折射，
      //     于是滑入过程中会闪出一块固定不动的玻璃框（iOS 上尤其明显）。
      //     抽屉本来就有一层不透明底色，不需要折射，直接跳过最干净。
      // 宽屏下侧栏是常驻栏，照旧当成普通玻璃目标。
      if(node.closest('.sidebar')&&(inDrawer||narrow))return;
      // 长卡片也保留玻璃，只裁剪可见区；整块跳过会让父卡片只剩边框。
      const clipBox=[0,0,width,height],clips=[];
      let fixed=s.position==='fixed';
      for(let parent=node.parentElement;parent&&parent!==document.body;parent=parent.parentElement){
        const b=box(parent),p=b.rect,ps=b.style;
        if(ps.visibility==='hidden'||ps.display==='none')return;
        // fixed 抽屉不受普通祖先的 overflow 裁剪；主布局没有变换 containing block。
        if(!fixed){
          const cx=/(hidden|clip|auto|scroll)/.test(ps.overflowX),cy=/(hidden|clip|auto|scroll)/.test(ps.overflowY);
          if(cx){clipBox[0]=Math.max(clipBox[0],p[0]);clipBox[2]=Math.min(clipBox[2],p[0]+p[2])}
          if(cy){clipBox[1]=Math.max(clipBox[1],p[1]);clipBox[3]=Math.min(clipBox[3],p[1]+p[3])}
          if(cx&&cy)clips.push({rect:p,radii:b.radii});
        }
        if(ps.position==='fixed')fixed=true;
      }
      // 抽屉和遮罩均半透明，后方卡片必须继续绘制；不能按抽屉宽度裁成原图。
      if(Math.min(r[0]+r[2],clipBox[2])<=Math.max(r[0],clipBox[0])||Math.min(r[1]+r[3],clipBox[3])<=Math.max(r[1],clipBox[1]))return;
      const order=node.closest('.sidebar')&&inDrawer?200:node.closest('.topbar')?100:0;
      regions.push({rect:r,radii:own.radii,clipBox,clips,soft:node.matches('.field,.feature,.stat,.btn,.pill'),order,index,area:r[2]*r[3]});
    });
    // 小控件的折射肉眼几乎分辨不出，却和卡片一样贵。
    // 先丢掉极小的药丸/按钮，再按尺寸上限截断，减少 draw call 和 overdraw。
    const budgetCount=narrow?96:192;
    const kept=regions.filter(x=>!(x.soft&&x.area<1200));
    if(kept.length>budgetCount){
      const byArea=kept.slice().sort((a,b)=>b.area-a.area).slice(0,budgetCount);
      const keepSet=new Set(byArea);
      for(let i=kept.length-1;i>=0;i--)if(!keepSet.has(kept[i]))kept.splice(i,1);
    }
    kept.sort((a,b)=>a.order-b.order||a.index-b.index);return kept;
  }
  // 在 Chromium 上 GPU 建不起来就直接放弃控制器，交还给 CSS 折射路径；
  // WebKit 则退到每元素的 SVG 克隆层。两者都不能写 liquid-glass-fallback。
  function abandonGpu(reason){
    firstFrameReady=false;
    if(source==='theme-gradient'){
      themeFailure=reason;enabled=false;generation++;image=null;source=null;clear();engine='lightweight';status=reason;gpuReason=reason;syncState();return true;
    }
    gpuReason=reason;
    if(webkitTarget){engine='svg';return false}
    engine='css';enabled=false;status='off';
    generation++;image=null;source=null;
    clear();gpuReason=reason;
    syncState();
    return true;
  }
  function startGpu(serial){
    // 外链不经 CORS 读像素，保留无需 canvas 读取权限的原生 SVG 后备。
    if(!blobUrl&&source!=='theme-gradient'){
      abandonGpu('external-image');
      return;
    }
    try{
      gpu=createLiquidGlassGpu(image,()=>{
        if(serial!==generation||!enabled)return;
        if(gpu)gpu.dispose();gpu=null;
        if(abandonGpu('context-lost'))return;
        syncState();targetsDirty=true;schedule();
      });
    }catch(e){
      if(abandonGpu(e.message))return;
    }
  }
  function update(){
    if(!enabled||status!=='ready'||!image)return;
    const started=performance.now();
    const wasTracking=animationTracking;
    const running=!!document.getAnimations?.().some(a=>(a.playState==='running'||a.pending)&&a.effect?.target?.matches?.(selector+',.page-anim,.chatroom,.sidebar,.main,.app,.subtab-panel,.subtab-enter,.plugin-root,.plugin-panel,.plugin-config-cards,.role-life'));
    animationTracking=running||started<movingUntil;
    if(targetsDirty){
      // 抽屉态的侧栏不进 targets：三条渲染路径（GPU / SVG 克隆 / CSS）
      // 因此都不必再各自判断，也不会画出那块浮空的玻璃框。
      const drawerNow=document.body.classList.contains('nav-open')||document.body.classList.contains('drawer-intent-open');
      const narrowNow=narrowViewport();
      const next=[...document.querySelectorAll(selector)].filter(node=>!(node.closest('.sidebar')&&(drawerNow||narrowNow))),retained=new Set(next);
      // 同样先收集再删除（理由见下方 SVG 分支的同名处理）。
      const stale=[];
      surfaces.forEach((surface,node)=>{if(!retained.has(node)||!node.contains(surface.svg))stale.push([node,surface])});
      stale.forEach(([node,surface])=>removeSurface(node,surface));
      targets=next;targetsDirty=false;
      resizeObserver?.disconnect();targets.forEach(node=>resizeObserver?.observe(node));
    }
    const themeSource=source==='theme-gradient';
    const blur=themeSource?0:Math.max(0,parseFloat(getComputedStyle(root).getPropertyValue('--user-bg-blur'))||0);
    // 卡片背景模糊度只影响玻璃内的采样模糊，不参与几何换算。
    // 几何（cover / 缩放）必须和壁纸本身的模糊度一致，否则折射采到的区域
    // 和元素底下真实的壁纸对不上，iOS 上就会看到明显错位。
    const cardBlur=Math.max(0,parseFloat(getComputedStyle(root).getPropertyValue('--card-bg-blur'))||0);
    // 玻璃内部采的是原始图片纹理，不经过壁纸那层 CSS blur；而 CSS 回退路径下
    // 胶囊的 backdrop-filter 是在「已经模糊过的背景」上再叠一层卡片模糊。
    // 两条路径要对齐，采样模糊就必须把两者的量叠加。
    // 这里刻意用相加而不是 hypot：hypot(40,10)=41.2，背景模糊一大，
    // 「卡片背景模糊度」整个滑块（0-10）就只能让结果动 1.2px，肉眼看不出变化。
    // 相加能让滑块每一档都产生可见差异，这才是用户在调这个参数时期望的反馈。
    const sampleBlur=blur+cardBlur;
    const viewport=window.visualViewport;
    const width=Math.max(1,Math.round(viewport?.width||innerWidth)),height=Math.max(1,Math.round(viewport?.height||innerHeight));
    if(themeSource){try{syncThemeTexture(width,height)}catch(e){abandonGpu('theme-source');return}}
    const nextCover=Math.max((width+blur*4)/image.naturalWidth,(height+blur*4)/image.naturalHeight);
    if(!coverViewport||Math.abs(coverViewport.width-width)>=1||Math.abs(coverViewport.height-height)>=1||Math.abs(coverViewport.blur-blur)>=.01){
      coverViewport={width,height,blur,scale:nextCover};
    }
    const scale=coverViewport.scale;
    const imageW=image.naturalWidth*scale,imageH=image.naturalHeight*scale;
    const baseX=(width-imageW)/2,baseY=(height-imageH)/2;
    if(gpu){
      let base=[0,0,0];
      if(!themeSource){
        const color=getComputedStyle(document.body).backgroundColor;
        const theme=getComputedStyle(root).getPropertyValue('--bg0').trim();
        base=[1,1,1];
        if(/^#[0-9a-f]{6}$/i.test(theme))base=[1,3,5].map(i=>parseInt(theme.slice(i,i+2),16)/255);
        else if(/^#[0-9a-f]{3}$/i.test(theme))base=[1,2,3].map(i=>parseInt(theme[i]+theme[i],16)/255);
        else{const components=(color.match(/[\d.]+/g)||[]).slice(0,3).map(Number);if(components.length===3)base=components.map(x=>x/255)}
      }
      try{
        gpu.draw(gpuRegions(width,height),{width,height,cover:themeSource?[0,0,width,height]:[baseX,baseY,imageW,imageH],blur:sampleBlur,base,animating:animationTracking,sourceRevision:themeSource?themeRevision:0,sourceOpacity:themeSource?1:.82});
        if(!firstFrameReady){firstFrameReady=true;syncState()}
      }
      catch(e){if(gpu)gpu.dispose();gpu=null;if(abandonGpu('texture-upload'))return;syncState();schedule()}
    }else{
    // SVG 后备路径，滤镜以面板 padding box 定域而不是整张壁纸的包围盒。
    const drawerOpen=document.body.classList.contains('nav-open')||document.body.classList.contains('drawer-intent-open');
    const narrowNow=narrowViewport();
    // 与 GPU 路径一致：抽屉状态下侧栏不参与折射（见 gpuRegions 的说明）。
    // 已经有克隆层的要顺手摘掉，否则跳过之后旧的 SVG 会留在 DOM 里继续显示。
    // 先收集再删除：在 forEach 里直接 delete 会改动正在迭代的 Map，
    // 后续条目可能被跳过，旧的克隆层就留在 DOM 上继续显示。
    if(drawerOpen||narrowNow){
      const stale=[];
      surfaces.forEach((surface,node)=>{if(node.closest('.sidebar'))stale.push([node,surface])});
      stale.forEach(([node,surface])=>removeSurface(node,surface));
    }
    const measurements=targets.map(node=>{
      const r=node.getBoundingClientRect();
      if(!r.width||!r.height||r.bottom<0||r.right<0||r.top>height||r.left>width)return null;
      if(node.closest('.sidebar')&&(drawerOpen||narrowNow))return null;
      const sx=r.width/node.offsetWidth||1,sy=r.height/node.offsetHeight||1;
      return {node,w:node.clientWidth,h:node.clientHeight,x:(baseX-r.left)/sx-node.clientLeft,y:(baseY-r.top)/sy-node.clientTop,iw:imageW/sx,ih:imageH/sy,scrollX:node.scrollLeft,scrollY:node.scrollTop};
    }).filter(Boolean);
    measurements.forEach(({node,w,h,x,y,iw,ih,scrollX,scrollY})=>{
      const s=surfaces.get(node)||makeSurface(node);if(!s)return;
      attr(s.svg,'width',w);attr(s.svg,'height',h);attr(s.svg,'viewBox',`0 0 ${w} ${h}`);
      attr(s.filter,'width',w);attr(s.filter,'height',h);attr(s.map,'width',w);attr(s.map,'height',h);
      attr(s.blur,'stdDeviation',sampleBlur);
      applyFilterScale(s.filter,s.soft);
      attr(s.picture,'width',iw.toFixed(3));attr(s.picture,'height',ih.toFixed(3));attr(s.picture,'x',x.toFixed(3));attr(s.picture,'y',y.toFixed(3));
      if(s.svg.style.left!==scrollX+'px')s.svg.style.left=scrollX+'px';
      if(s.svg.style.top!==scrollY+'px')s.svg.style.top=scrollY+'px';
    });
    if(!firstFrameReady&&surfaces.size){firstFrameReady=true;syncState()}
    }
    lastJsMs=Number((performance.now()-started).toFixed(2));
    if(!animationTracking)drawerTransition=false;
    // 动画刚结束的那一帧必须再画一次：上面的 region 是**这次**调用用的，
    // 而动画最后一帧的 transform 已经到位。少了这一次重绘，画布会停在
    // 倒数第二帧的位置，卡片却已经落位 —— 视觉上就是刷新后卡片"跳一下"。
    if(animationTracking||wasTracking)schedule();
  }
  function imageFailed(reason='image-error'){
    if(blobUrl)URL.revokeObjectURL(blobUrl);
    blobUrl='';image=null;imageSrc='';status=reason;
    // Chromium 没有 SVG 克隆层可退，直接交还给 CSS 折射路径。
    if(!webkitTarget){
      engine='css';enabled=false;status='off';generation++;source=null;
      clear();syncState();return;
    }
    syncState();
  }
  async function load(url,serial){
    let src=url;
    try{
      if(new URL(url,location.href).origin===location.origin){
        request=new AbortController();
        const response=await fetch(url,{signal:request.signal,cache:'no-cache'});
        if(!response.ok)throw new Error('http-'+response.status);
        const blob=await response.blob();
        if(serial!==generation||!enabled)return;
        if(!blob.type.startsWith('image/'))throw new Error('image-type');
        blobUrl=URL.createObjectURL(blob);src=blobUrl;
      }
      if(serial!==generation||!enabled)return;
      const next=new Image();
      next.onload=()=>{
        if(serial!==generation||!enabled)return;
        if(!next.naturalWidth||!next.naturalHeight){imageFailed();return}
        image=next;imageSrc=src;status='ready';startGpu(serial);syncState();if(enabled){listen(true);schedule()}
      };
      next.onerror=()=>{if(serial===generation&&enabled)imageFailed()};
      next.src=src;
    }catch(e){
      if(serial!==generation||!enabled)return;
      imageFailed(/^http-\d{3}$|^image-type$/.test(e.message)?e.message:'image-error');
    }
  }
  return {setEnabled(value,url,svgReady){
    const plan=liquidGlassRenderPlan(!!value,url,svgReady);
    if(!value||url)themeFailure='';
    if(value&&!url&&themeFailure){plan.engine='lightweight';plan.reason=themeFailure}
    const previousEngine=engine;engine=plan.engine;
    // off / css：控制器完全惰性，不加载图片、不建 surface。
    // css 是 Chromium 的原生回退路径，由 CSS 自己完成，这里绝不能插手。
    if(engine==='off'||engine==='css'||engine==='lightweight'){
      const wasActive=enabled;
      enabled=false;status=engine==='lightweight'?plan.reason:'off';drawerTransition=false;
      if(wasActive||gpu||surfaces.size||image){generation++;image=null;source=null;clear()}
      syncState();
      return;
    }
    enabled=true;
    // GPU 不需要 SVG 位移贴图；svg 引擎才需要 svgReady。
    const nextSource=engine==='gpu'?(url||'theme-gradient'):(svgReady&&url?url:'');
    if(previousEngine===engine&&source===nextSource){syncState();schedule();return}
    source=nextSource;image=null;const serial=++generation;clear();
    // svg 引擎沿用原有判定顺序：先看 SVG 位移贴图是否就绪，再看有没有壁纸。
    // GPU 不需要贴图，只看壁纸。
    status=engine==='svg'?(!svgReady?'no-map':!nextSource?'no-wallpaper':'loading'):(!nextSource?'no-wallpaper':'loading');
    syncState();
    if(nextSource==='theme-gradient'){
      try{
        const viewport=window.visualViewport;
        syncThemeTexture(Math.max(1,Math.round(viewport?.width||innerWidth)),Math.max(1,Math.round(viewport?.height||innerHeight)));
        status='ready';startGpu(serial);syncState();if(enabled){listen(true);schedule()}
      }catch(e){abandonGpu('theme-source')}
    }else if(nextSource)load(nextSource,serial);
  },refresh(){schedule()},diagnostics(){return {engine,renderer:gpu?'fixed-webgl-v2':(engine==='lightweight'?'theme-lightweight':engine==='css'?'css-backdrop':'native-svg-fallback-v2'),status,firstFrameReady,imageSize:image?[image.naturalWidth,image.naturalHeight]:null,layers:gpu?1:surfaces.size,sourceType:source==='theme-gradient'?'theme-gradient':blobUrl?'same-origin-blob':imageSrc?'external-image':'none',themeTextureBuilds:themeBuilds,themeTextureRevision:themeRevision,drawerOpen:document.body.classList.contains('nav-open'),animationTracking,cover:coverViewport?{width:coverViewport.width,height:coverViewport.height,blur:coverViewport.blur}:null,gpu:gpu?gpu.diagnostics():null,gpuFallbackReason:gpuReason||null,lastUpdateJsMs:lastJsMs}}};
})();
function normalizeWebuiVersion(raw){
  const s=String(raw||'').trim().toLowerCase();
  return ['modern','new','v2','新版','现代'].includes(s)?'modern':'classic';
}
function applyWebuiAppearance(){const v=state.bundle?.form_values||{},root=document.documentElement;const uiVerKnown=Object.prototype.hasOwnProperty.call(v,'WebUI.ui_version');const uiVersion=uiVerKnown?normalizeWebuiVersion(v['WebUI.ui_version']):(root.dataset.webuiVersion||'classic');root.dataset.webuiVersion=uiVersion;const preset=String(v['WebUI.theme_preset']||'aurora');root.dataset.webuiPreset=preset;const liquid=!!v['WebUI.liquid_glass'];const effectiveLiquid=(uiVersion==='classic')&&liquid;const disableBgBlur=effectiveLiquid&&!!v['WebUI.disable_background_blur'];const blur=disableBgBlur?0:Math.max(0,Math.min(40,Number(v['WebUI.background_blur']||0)));root.style.setProperty('--user-bg-blur',blur+'px');
    // 区分「键不存在」（bundle 还没加载完/部分刷新）与「键存在但为空」（用户主动清除）。
    // 前者绝不能动 has-bg 与 --user-bg-image，否则切页时背景会被莫名摘掉、整片变白。
    // 键不存在时 bgUrl 也不能给空——liquidGlassLensController.setEnabled 拿到空会重置
    // 玻璃引擎状态。从 computed style 取回当前的 --user-bg-image 沿用。
    const bgImageKnown=Object.prototype.hasOwnProperty.call(v,'WebUI.background_image');
    const img=bgImageKnown?String(v['WebUI.background_image']||'').trim():'';
    let bgUrl=img?(/^https?:\/\//i.test(img)?img:'/api/webui/background?token='+encodeURIComponent(token()||'')+'&v='+encodeURIComponent(img)):'';
    if(bgImageKnown){
      root.style.setProperty('--user-bg-image',bgUrl?'url("'+bgUrl.replace(/"/g,'%22')+'")':'none');
      root.classList.toggle('has-bg',!!img);
    }else{
      // 沿用现有 --user-bg-image（形如 url("...") 或 none），剥掉引号供玻璃引擎用
      const cur=(getComputedStyle(root).getPropertyValue('--user-bg-image')||'').trim();
      const m=/^url\(["']?(.*?)["']?\)$/.exec(cur);
      bgUrl=m?m[1]:'';
    }
    const refractionRaw=v['WebUI.liquid_glass_refraction'];
    // 用 ?? 而不是 ||：滑块拖到 0 是有效值，不能被当成「未设置」回退成 5。
    const liquidGlassRefraction=Math.max(0,Math.min(30,Number(refractionRaw??1)));root.style.setProperty('--liquid-glass-refraction',String(liquidGlassRefraction));const cardBgBlur=Math.max(0,Math.min(10,Number(v['WebUI.card_background_blur']??3)));root.style.setProperty('--card-bg-blur',cardBgBlur+'px');root.classList.toggle('liquid-glass',effectiveLiquid);const svgReady=liquidGlassController.setEnabled(effectiveLiquid);liquidGlassLensController.setEnabled(effectiveLiquid,bgUrl,svgReady);if(effectiveLiquid){liquidGlassController.syncRefraction();liquidGlassLensController.refresh()}
    // 键不存在时不要写登录缓存：会把本还有的 src/bg 一并清掉，下次进登录页就没背景。
    if(bgImageKnown){saveAppearanceCache({uiVersion:uiVersion,preset:preset,blur:blur,bgUrl:bgUrl,cardBlur:cardBgBlur,refraction:liquidGlassRefraction});cacheAppearanceForLogin(preset,blur,img,bgUrl,uiVersion)}
    else if(uiVerKnown){try{const prev=JSON.parse(localStorage.getItem(APPEARANCE_CACHE_KEY)||'null')||{};saveAppearanceCache({...prev,uiVersion:uiVersion});const lprev=JSON.parse(localStorage.getItem(LOGIN_LOOK_KEY)||'null')||{};localStorage.setItem(LOGIN_LOOK_KEY,JSON.stringify({...lprev,uiVersion:uiVersion}))}catch(e){}}}
function showLiquidGlassStatus(){
  document.getElementById('glassStatusDialog')?.remove();
  const root=document.documentElement,sample=document.querySelector('.glass-gpu-layer')||document.querySelector('.glass-lens-surface>.glass-lens-layer');
  const versions={};
  document.querySelectorAll('script[src],link[rel="stylesheet"]').forEach(node=>{
    const u=new URL(node.src||node.href,location.href),name=u.pathname.split('/').pop();
    if(!['app.js','app.css'].includes(name))return;
    const v=u.searchParams.get('v')||'';versions[name]=/^[a-zA-Z0-9_.-]{1,80}$/.test(v)?v:'无版本号';
  });
  const report={...liquidGlassLensController.diagnostics(),
    mode:root.dataset.liquidGlassState||'unknown',cssRenderer:getComputedStyle(root).getPropertyValue('--glass-renderer-css').trim()||'旧样式/未加载',
    enabled:root.classList.contains('liquid-glass'),webkitTarget:liquidGlassWebKitTarget(),wallpaperConfigured:root.classList.contains('has-bg'),
    resources:versions,viewport:[innerWidth,innerHeight,devicePixelRatio],
    sample:sample?{size:[sample.getAttribute('width'),sample.getAttribute('height')],filter:sample.querySelector('g')?.getAttribute('filter'),position:getComputedStyle(sample).position,zIndex:getComputedStyle(sample).zIndex}:null,
    browser:navigator.userAgent.slice(0,300)};
  const overlay=document.createElement('div');overlay.id='glassStatusDialog';overlay.className='glass-status-overlay';
  const panel=document.createElement('section');panel.className='glass-status-panel';panel.setAttribute('role','dialog');panel.setAttribute('aria-modal','true');panel.setAttribute('aria-label','液态玻璃状态');
  const title=document.createElement('h3');title.textContent='液态玻璃状态';
  const note=document.createElement('p');note.textContent='ready 表示绘制已就绪，不代表真机流畅度已确认。JsMs 是主线程提交耗时，不是 GPU 耗时或帧率。可复制此信息，不含访问 Token。';
  const text=document.createElement('textarea');text.readOnly=true;text.setAttribute('aria-label','玻璃诊断信息');text.value=JSON.stringify(report,null,2);
  const copy=document.createElement('button');copy.type='button';copy.textContent='选中诊断信息';copy.onclick=()=>{text.focus();text.select();text.setSelectionRange(0,text.value.length)};
  const close=document.createElement('button');close.type='button';close.textContent='关闭';close.onclick=()=>overlay.remove();
  overlay.onclick=e=>{if(e.target===overlay)overlay.remove()};overlay.onkeydown=e=>{if(e.key==='Escape')overlay.remove()};
  panel.append(title,note,text,copy,close);overlay.appendChild(panel);document.body.appendChild(overlay);close.focus();
}
// 把外观缓存下来给登录页用。登录页没有 Token，取不了任何接口，所以不能让它去
// 请求配置或背景图——那需要开一个匿名可访问的出口，登录前多一个数据面。
// 换成在这里（已登录、已鉴权）把结果存进 localStorage，登录页只读本地缓存。
// 缓存不到就退回渐变色，功能不受影响。
// 主界面外观缓存：刷新后先用上次的壁纸/参数立即画一帧，
// 等 /api/ui-state 回来再按真实配置覆盖。否则刷新后会有一段时间
// 只有主题渐变、壁纸和玻璃都还没出现。
const APPEARANCE_CACHE_KEY='xcbotAppearanceCache';
function saveAppearanceCache(o){
  try{localStorage.setItem(APPEARANCE_CACHE_KEY,JSON.stringify(o))}catch(e){}
}
function applyCachedAppearance(){
  try{
    const o=JSON.parse(localStorage.getItem(APPEARANCE_CACHE_KEY)||'null');
    if(!o)return;
    const root=document.documentElement;
    if(o.uiVersion==='classic'||o.uiVersion==='modern')root.dataset.webuiVersion=o.uiVersion;
    if(o.preset)root.dataset.webuiPreset=o.preset;
    if(typeof o.blur==='number')root.style.setProperty('--user-bg-blur',o.blur+'px');
    if(o.bgUrl){
      root.style.setProperty('--user-bg-image','url("'+String(o.bgUrl).replace(/"/g,'%22')+'")');
      root.classList.add('has-bg');
    }
    if(typeof o.cardBlur==='number')root.style.setProperty('--card-bg-blur',o.cardBlur+'px');
    if(typeof o.refraction==='number')root.style.setProperty('--liquid-glass-refraction',String(o.refraction));
  }catch(e){}
}
const LOGIN_LOOK_KEY='xcbotLoginLook';
function cacheAppearanceForLogin(preset,blur,img,bgUrl,uiVersion){
  try{
    const look={uiVersion:uiVersion||'classic',preset:preset,blur:blur,ts:Date.now()};
    const prev=JSON.parse(localStorage.getItem(LOGIN_LOOK_KEY)||'null')||{};
    if(!img){localStorage.setItem(LOGIN_LOOK_KEY,JSON.stringify(look));return}
    // 图片按来源标记：同一张图不重复转码
    if(prev.src===img&&prev.bg){look.src=img;look.bg=prev.bg;localStorage.setItem(LOGIN_LOOK_KEY,JSON.stringify(look));return}
    look.src=img;
    localStorage.setItem(LOGIN_LOOK_KEY,JSON.stringify(look));
    // 外链图直接存 URL；本地图要经鉴权接口取，转成 data URL 才能给登录页用
    if(/^https?:\/\//i.test(img)){look.bg=img;localStorage.setItem(LOGIN_LOOK_KEY,JSON.stringify(look));return}
    shrinkBackgroundForLogin(bgUrl).then(d=>{
      if(!d)return;
      try{const cur=JSON.parse(localStorage.getItem(LOGIN_LOOK_KEY)||'null')||{};
        if(cur.src!==img)return;   // 期间又换了图，丢弃这次结果
        cur.bg=d;localStorage.setItem(LOGIN_LOOK_KEY,JSON.stringify(cur))}catch(e){}
    }).catch(()=>{});
  }catch(e){/* 隐私模式下 localStorage 可能不可写，登录页退回渐变色即可 */}
}
// 缩成小图再转 data URL：原图可能有好几 MB，localStorage 通常只有 5MB 配额，
// 直接存会抛 QuotaExceededError 把整个缓存写坏。登录页那张图还叠了模糊，
// 1280 宽足够，jpeg 0.72 质量下通常在 200KB 内。
function shrinkBackgroundForLogin(url){
  return new Promise((resolve,reject)=>{
    if(!url)return resolve('');
    const im=new Image();
    im.onload=()=>{
      try{
        const max=1280,r=Math.min(1,max/Math.max(im.naturalWidth||max,1));
        const c=document.createElement('canvas');
        c.width=Math.max(1,Math.round((im.naturalWidth||max)*r));
        c.height=Math.max(1,Math.round((im.naturalHeight||max)*r));
        c.getContext('2d').drawImage(im,0,0,c.width,c.height);
        const d=c.toDataURL('image/jpeg',.72);
        resolve(d&&d.length<3200000?d:'');
      }catch(e){resolve('')}
    };
    im.onerror=()=>resolve('');
    im.src=url;
  });
}
function toggleTheme(){setTheme((document.documentElement.dataset.theme||'light')==='dark'?'light':'dark')}
// 移动端抽屉导航：窄屏下侧栏浮在内容之上，靠 body.nav-open 控制
function isMobileNav(){return window.matchMedia('(max-width:980px)').matches}
function setMobileNav(open){
  const b=document.body;if(!b)return;
  clearTimeout(b._drawerTransitionTimer);
  const root=document.documentElement,sidebar=document.querySelector('.sidebar'),scrim=el('navScrim');

  // 宽屏没有抽屉。但 gotoPage() 每次点菜单都会无条件调 closeMobileNav()，
  // 以前这里不分屏幕就往下跑，等于给常驻侧栏硬刷了一层不透明底色
  // （浅色 rgba(255,255,255,.68)、深色 rgba(6,18,28,.72)），
  // 于是 PC 端点菜单的瞬间整条侧栏变白，320ms 后才被移除——
  // 和液态玻璃、壁纸都无关。宽屏直接清干净并返回。
  if(!isMobileNav()){
    b.classList.remove('drawer-transition','drawer-intent-open','nav-open');
    root.classList.remove('drawer-transition');
    if(sidebar){
      sidebar.style.removeProperty('background');
      sidebar.style.removeProperty('backdrop-filter');
      sidebar.style.removeProperty('-webkit-backdrop-filter');
    }
    if(scrim){scrim.style.opacity='';scrim.style.pointerEvents=''}
    return;
  }

  b.classList.add('drawer-transition');
  executeSwitch();

  function executeSwitch(){
    // iOS 首帧不能等 CSS 级联：先写底色和遮罩，再切类
    if(sidebar){
      const light=root.dataset.theme==='light';
      sidebar.style.setProperty('background',light?'rgba(255,255,255,.68)':'rgba(6,18,28,.72)','important');
      sidebar.style.setProperty('backdrop-filter','none','important');
      sidebar.style.setProperty('-webkit-backdrop-filter','none','important');
    }
    if(scrim){scrim.style.opacity=open?'1':'0';scrim.style.pointerEvents=open?'auto':'none'}
    b.classList.toggle('drawer-intent-open',!!open);
    b.classList.toggle('nav-open',!!open);
    const t=el('navToggle');if(t)t.setAttribute('aria-expanded',open?'true':'false');
    b._drawerTransitionTimer=setTimeout(()=>{
      b.classList.remove('drawer-transition');
      root.classList.remove('drawer-transition');
      if(sidebar&&!b.classList.contains('nav-open')){
        sidebar.style.removeProperty('background');sidebar.style.removeProperty('backdrop-filter');
        sidebar.style.removeProperty('-webkit-backdrop-filter');sidebar.style.removeProperty('contain');
        sidebar.style.removeProperty('will-change');
      }
      liquidGlassLensController.refresh();
    },320);
  }
}
function toggleMobileNav(){setMobileNav(!document.body.classList.contains('nav-open'))}
function closeMobileNav(){setMobileNav(false)}
function gotoPage(k,section='',opts={}){closeMobileNav();if(state.dirty){const m=el('leaveModal');if(m){if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();m.style.display='flex';m._pending={k,section};requestAnimationFrame(()=>el('leaveConfirmBtn')?.focus?.());return}}if(state.bundle&&!isValidPage(k))k='welcome';if(!opts.discard)syncCurrentPageFieldsFromDom();if(k==='chatroom'){chatState._enterBottom=true}window.__pageSwitched=true;state._navigating=true;state.dirty=false;state.lastInputAt=0;clearDraft();updateRoute(k,section);render();const live=['welcome','logs','stats','debug','chatroom','store'].includes(k);/* 确认放弃改动时也要强制回读服务端，否则非 live 页面会继续显示未保存的编辑值 */Promise.resolve((live||opts.discard)?refreshAll(true):null).finally(()=>{state._navigating=false});if(k==='knowledge')loadKnowledge();if(k==='role-life')loadRoleLife();if(k==='trace'&&!traceState.openId)traceLoad()}
function closeSubmitModal(){const m=el('submitModal');if(!m)return;if(document.activeElement&&m.contains?.(document.activeElement))document.activeElement.blur?.();m.style.display='none'}
function leaveCancel(){const m=el('leaveModal');if(m){if(document.activeElement&&m.contains?.(document.activeElement))document.activeElement.blur?.();m.style.display='none';m._pending=null}}
function leaveConfirm(){const m=el('leaveModal');if(!m)return;const p=m._pending;if(document.activeElement&&m.contains?.(document.activeElement))document.activeElement.blur?.();m.style.display='none';m._pending=null;state.dirty=false;if(typeof pendingBackground!=='undefined')pendingBackground=null;if(typeof roleLifeState!=='undefined'&&roleLifeState&&roleLifeState.draft){roleLifeState.draft=null;roleLifeState.message='';roleLifeState.messageKind=''}if(typeof discardMcpDraft==='function')discardMcpDraft();traceDiscardPending();/* 确认离开=丢弃未保存改动：不同步 DOM，并强制从服务端重载 */if(p)gotoPage(p.k||p.page,p.section||'',{discard:true})}
function renderNav(){
  const nav=el('nav');if(!nav)return;
  const groups=navigationGroups(),signature=JSON.stringify(groups.map(g=>[g.key,navGroupCollapsed(g.key),g.items.map(p=>[p.key,p.title,p.icon])]));
  if(nav.dataset.signature!==signature||!el('navIndicator')){
    nav.dataset.signature=signature;
    nav.innerHTML='<div class="custom-nav">'+groups.map(g=>`<section class="nav-group ${navGroupCollapsed(g.key)?'is-collapsed':''}" data-group="${esc(g.key)}"><button type="button" class="nav-group-title" data-group="${esc(g.key)}" aria-expanded="${!navGroupCollapsed(g.key)}" onclick="toggleNavGroup(this.dataset.group)"><span>${esc(g.title)}</span><span class="nav-group-caret" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span></button><div class="nav-group-items">${g.items.map(p=>`<div class="custom-nav-item" role="button" tabindex="0" data-key="${esc(p.key)}" onclick="gotoPage(this.dataset.key)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();gotoPage(this.dataset.key)}"><div class="custom-nav-icon">${p.icon||'•'}</div><div class="custom-nav-label">${esc(p.title)}</div></div>`).join('')}</div></section>`).join('')+'<div class="active-indicator-floater" id="navIndicator"></div><div class="nav-bg-floater" id="navBg"></div></div>';
  }
  nav.querySelectorAll('.custom-nav-item').forEach(item=>{
    const active=item.dataset.key===state.current;item.classList.toggle('active',active);
    if(active)item.setAttribute('aria-current','page');else item.removeAttribute('aria-current');
    if(active){const group=item.closest('.nav-group');group?.classList.remove('is-collapsed');group?.querySelector('.nav-group-title')?.setAttribute('aria-expanded','true')}
  });
}
const updateNavIndicator=()=>{const nav=el('nav'),items=nav?nav.querySelectorAll('.custom-nav-item'):[],floater=el('navIndicator'),floaterBg=el('navBg');if(!floater||!nav)return;const host=nav.querySelector('.custom-nav')||nav;let activeEl=null;for(let i=0;i<items.length;i++){if(items[i].classList.contains('active')){activeEl=items[i];break}}if(activeEl){const hostTop=host.getBoundingClientRect().top;const itemTop=activeEl.getBoundingClientRect().top;const top=itemTop-hostTop+host.scrollTop;floater.style.opacity='1';floater.style.top=(top+10)+'px';floater.style.height=Math.max(0,activeEl.offsetHeight-20)+'px';if(floaterBg){floaterBg.style.opacity='1';floaterBg.style.top=top+'px';floaterBg.style.height=activeEl.offsetHeight+'px'}}else{floater.style.opacity='0';if(floaterBg)floaterBg.style.opacity='0'}};
function renderConnectionStatus(){const s=state.bundle?.status||{},cs=s.connection_status||{},cfg=s.connection||{};const statusEl=el('connectionStatus'),detailEl=el('connectionDetail');if(statusEl){const text=cs.text||'未知状态';statusEl.textContent=text;statusEl.className='pill '+((cs.state==='connected')?'ok':(cs.state==='failed'||cs.state==='disconnected'||cs.state==='stopped')?'bad':'')}if(detailEl){const lines=[];if(cs.detail)lines.push(cs.detail);const endpoint=[cfg.host&&cfg.port?`${cfg.host}:${cfg.port}`:''].filter(Boolean).join(' · ');if(endpoint)lines.push(endpoint);detailEl.textContent=lines.join(' | ')||'暂无连接详情'}}
function render(){if(!state.bundle)return;snapshotPluginConfigOpen();applyWebuiAppearance();applyRoute();const isSwitch=state._lastPage!==state.current;state._lastPage=state.current;const logScroll=captureLogScrollState();renderNav();requestAnimationFrame(updateNavIndicator);renderConnectionStatus();const ub=el('pluginUploadBtn');if(ub)ub.style.display=state.current==='store'?'':'none';const m=meta(),titleEl=el('pageTitle'),descEl=el('pageDesc'),brandEl=el('brandName'),contentEl=el('content');const isChat=state.current==='chatroom',isDebug=state.current==='debug';if(isChat&&contentEl){contentEl.classList.remove('page-anim');clearTimeout(contentEl._animTimer)}if(titleEl)titleEl.textContent=isChat?'聊天室':isDebug?'调试':m.title;if(descEl){const desc=isChat?'':isDebug?'运行资源、缓存和会话状态等可统计的全部调试信息':(Object.prototype.hasOwnProperty.call(m,'desc')?m.desc:'所有数值均可在此直接修改并保存');descEl.textContent=desc;descEl.style.display=desc?'':'none'}if(brandEl)brandEl.textContent=state.bundle.status?.project||'XcBot';if(contentEl){
  if(isSwitch&&contentEl._chatAnimRenderTimer){clearTimeout(contentEl._chatAnimRenderTimer);contentEl._chatAnimRenderTimer=null}
  // 只允许真实页面切换触发一次动画。refreshAll/chatBootstrap/hashchange 可能在同一帧内再次 render，
  // 不能再用全局 pageSwitched 标记，否则会把同一页重新播放一遍。
  const playAnim=!!(!isChat&&(isSwitch||!contentEl.dataset.currentPage));
  const animating=contentEl.classList.contains('page-anim');
  const forced=!!state._forceRender;
  // 真正切页：整页替换并只播一次动画
  if(playAnim){
    contentEl.innerHTML=isChat?renderChatroom():isDebug?renderDebug():state.current==='welcome'?renderWelcome():state.current==='stats'?renderStats():state.current==='providers'?renderProviders():state.current==='knowledge'?renderKnowledge():state.current==='persona'?renderPersona():state.current==='role-life'?renderRoleLife():state.current==='features'?renderFeatures():state.current==='agent'?renderAgent():state.current==='logs'?renderLogs():state.current==='store'?renderStore():state.current==='trace'?renderTrace():renderForm(m);
    contentEl.dataset.currentPage=state.current;
    contentEl.classList.remove('page-anim');
    void contentEl.offsetWidth;
    contentEl.classList.add('page-anim');
    clearTimeout(contentEl._animTimer);
    // 等 staggered delay(0.61s)+duration(0.82s) 播完再摘掉 class，避免中途被刷新掐断
    contentEl._animTimer=setTimeout(()=>contentEl.classList.remove('page-anim'),1500);
     window.__pageSwitched=false;
    if(state.current==='store')ensurePluginTabLoaded();
    if(state.current==='trace'&&traceState.list===null&&!traceState.loading)traceLoad();
  }else if(animating&&!isChat&&!forced){
     // 非聊天室页面继续等待动画结束后刷新。
     if(!contentEl._chatAnimRenderTimer){
       contentEl._chatAnimRenderTimer=setTimeout(()=>{
         contentEl._chatAnimRenderTimer=null;
         if(state.bundle)render();
       },1550);
     }
  }else if(state.current==='role-life'&&contentEl.dataset.currentPage==='role-life'&&!forced){
    // 生活页只由自己的数据请求与操作更新，避免全局5秒刷新打断输入/展开/滚动。
  }else if(state.current==='logs'){
    const pre=el('log_full');
    if(pre){
      const logs=(state.bundle.logs||[]).slice(-1000);
      const html=logs.map(x=>{
        const msg=_strip_ansi_js(x.message).replace(/\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\]\s*/g,'');
        if(!msg.trim())return null;
        const line=`[${x.time}] [${x.stream}] ${msg}`;
        const isErr=/error|crit|\*crit|\*err|exception|traceback|❌/i.test(msg);
        const isWarn=/warn|\*warn|⚠️/i.test(msg);
        const style=isErr?'background:rgba(220,50,50,.35);border-radius:3px':isWarn?'background:rgba(200,150,0,.35);border-radius:3px':'';
        return style?`[${esc(x.time)}] [${esc(x.stream)}] <span style="${style}">${esc(msg)}</span>`:esc(line);
      }).filter(x=>x!==null).join('\n')||'暂无日志';
      pre.innerHTML=html;
    }else{
      contentEl.innerHTML=renderLogs();
      contentEl.dataset.currentPage='logs';
    }
  }else if(state.current==='trace'&&!traceState.openId&&contentEl.dataset.currentPage==='trace'&&!forced){
    // 追踪列表：自动刷新时不要整页重建，否则用户正在翻的滚动位置会被冲掉。
    // 只有列表指纹变了才替换 DOM，替换后再把 scrollTop 放回去。
    const sig=traceListSignature();
    if(sig!==traceState.sig){
      const sc=captureTraceScroll();
      traceState.sig=sig;
      contentEl.innerHTML=renderTrace();
      restoreTraceScroll(sc);
    }
  }else if(isChat||forced||!shouldPauseAutoRefresh()){
    if(forced&&animating){contentEl.classList.remove('page-anim');clearTimeout(contentEl._animTimer)}
    if(isChat){
      if(isSwitch){
        chatState._enterBottom=true;
      }else{
        const box=el('chatMsgs');
        if(box){
          if(!chatState._enterBottom&&!chatState._stickBottom&&chatState._restoreScroll==null){
            if(chatNearBottom())chatState._stickBottom=true;
            else chatState._restoreScroll=box.scrollTop;
          }
          if(chatState._innerScroll==null)chatState._innerScroll=chatCaptureInnerScroll(box);
        }
      }
    }
    const focused=document.activeElement;
    const isInput=focused&&(focused.tagName==='INPUT'||focused.tagName==='TEXTAREA'||focused.tagName==='SELECT');
    const focusId=isInput?focused.id:null;
    const selStart=isInput?focused.selectionStart:null;
    const selEnd=isInput?focused.selectionEnd:null;
    contentEl.innerHTML=isChat?renderChatroom():isDebug?renderDebug():state.current==='welcome'?renderWelcome():state.current==='stats'?renderStats():state.current==='providers'?renderProviders():state.current==='knowledge'?renderKnowledge():state.current==='persona'?renderPersona():state.current==='role-life'?renderRoleLife():state.current==='features'?renderFeatures():state.current==='agent'?renderAgent():state.current==='logs'?renderLogs():state.current==='store'?renderStore():state.current==='trace'?renderTrace():renderForm(m);
    contentEl.dataset.currentPage=state.current;
    if(focusId){
      const newFocused=document.getElementById(focusId);
      if(newFocused){
        newFocused.focus();
        if(selStart!=null&&selEnd!=null)try{newFocused.setSelectionRange(selStart,selEnd)}catch(e){}
      }
    }
  }
}if(isChat)afterChatroomRender();if(state.current==='role-life'&&!roleLifeState.data&&!roleLifeState.loading&&!roleLifeState.error)requestAnimationFrame(()=>loadRoleLife());if(state.section)requestAnimationFrame(()=>scrollToRouteSection(state.section));if(state.current==='welcome'||state.current==='logs'){if(isSwitch){if(localStorage.webuiAutoScroll!=='false'){requestAnimationFrame(()=>{snapLogsToBottom();setTimeout(snapLogsToBottom,80)})}}else scheduleLogScrollAfterRender(logScroll)}}
function renderWelcome(){const s=state.bundle.status||{},u=s.update||{},ui=s.update_install||{},cs=s.connection_status||{},cc=s.connection||{},ru=s.resource_usage||{},updateBusy=['downloading','extracting','installing','migrating','dependencies','restarting'].includes(ui.state),updatePillClass=u.status==='latest'?'ok':(u.status==='outdated'||u.status==='error'||ui.state==='error')?'bad':'';return `<div class="card"><div class="section-head"><div><h3>运行概览</h3></div><span class="pill ${cs.state==='connected'?'ok':(cs.state==='failed'||cs.state==='disconnected'||cs.state==='stopped')?'bad':''}">${esc(cs.text||'未知状态')}</span></div><div class="mini-stats"><div class="stat"><b>${esc(s.uptime_text||Math.floor((s.uptime_seconds||0)/60)+'分钟')}</b><span>运行时间</span></div><div class="stat"><b>${esc((ru.cpu_percent_normalized??ru.cpu_percent??0)+'% / '+(ru.memory_mb??0)+' MB')}</b><span>CPU/内存占用</span></div><div class="field" style="grid-column:span 2;min-height:auto;display:flex;flex-direction:column;justify-content:center"><div class="label" style="margin-bottom:6px"><span>获取更新</span><span class="pill ${updatePillClass}">${esc(ui.state&&ui.state!=='idle'?ui.text:(u.message||'暂未检查更新'))}</span></div><div class="desc" style="margin-top:0">当前 ${esc(s.version||'--')} / 最新 ${esc(u.latest_version||'--')}</div><div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px"><button class="btn" ${updateBusy?'disabled':''} onclick="checkUpdate()">检查更新</button><button class="btn primary" ${(updateBusy||!u.has_update)?'disabled':''} onclick="installUpdate()">安装更新</button></div><div class="desc">${esc(ui.detail||u.detail||u.release_name||'安装更新会自动下载、覆盖程序、迁移配置并重启。安装后需手动刷新页面。')}</div></div></div></div><div class="card half"><div class="section-head"><div><h3>详细信息</h3><p>环境与启动参数</p></div></div><div class="kv">${[['版本号',(s.project||'')+' '+(s.version||'')],['机器人名',s.bot_name],['运行目录',s.cwd],['Python',s.python],['平台',s.platform],['启动参数',JSON.stringify(s.argv||[])]].map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1])}</div>`).join('')}</div></div><div class="card half"><div class="section-head"><div><h3>连接状态</h3><p>OneBot / Hyper 实时状态</p></div></div><div class="kv">${[['当前状态',cs.text||'未知状态'],['状态详情',cs.detail||'暂无'],['连接地址',(cc.host&&cc.port)?`${cc.host}:${cc.port}`:''],['监听地址',(cc.listener_host&&cc.listener_port)?`${cc.listener_host}:${cc.listener_port}`:''],['连接模式',cc.mode||'']].map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1])}</div>`).join('')}</div></div><div class="card"><div class="section-head"><div><h3>最近日志</h3></div><div style="display:flex;gap:8px;flex-wrap:wrap">${renderAutoScrollToggle()}<button class="btn" onclick="gotoPage('logs')">查看全部</button></div></div>${renderLogPre(160,'recent')}</div>`}
function chartBars(items,max=0,unit='',color='linear-gradient(135deg,var(--accent),var(--accent2))'){const arr=Array.isArray(items)?items:[];const peak=max||Math.max(1,...arr.map(x=>Number(x.value)||0));return `<div style="display:grid;gap:10px">${arr.length?arr.map(x=>`<div><div style="display:flex;justify-content:space-between;gap:12px;margin-bottom:6px"><span style="font-weight:600">${esc(x.label||x.model||x.session||'-')}</span><span class="pill">${esc((x.value??x.tokens??0)+unit)}</span></div><div style="height:10px;border-radius:999px;background:var(--line);overflow:hidden"><div style="height:100%;width:${Math.max(4,Math.round(((Number(x.value??x.tokens)||0)/peak)*100))}%;background:${color};border-radius:999px"></div></div></div>`).join(''):`<div class="desc">暂无数据</div>`}</div>`}
function chartLine(items,unit=''){const arr=Array.isArray(items)?items:[];if(!arr.length)return '<div class="desc">暂无数据</div>';const values=arr.map(x=>Number(x.value)||0),peak=Math.max(1,...values),width=640,height=170,padX=16,padTop=16,padBottom=28,innerW=width-padX*2,innerH=height-padTop-padBottom;const points=arr.map((x,i)=>{const px=padX+(arr.length===1?innerW/2:(i/(arr.length-1))*innerW);const py=padTop+innerH-(Math.min(Number(x.value)||0,peak)/peak)*innerH;return {x:px,y:py,label:String(x.label||''),value:Number(x.value)||0}});const line=points.map((p,i)=>`${i?'L':'M'}${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(' ');const area=`M${points[0].x.toFixed(2)} ${height-padBottom} `+points.map((p,i)=>`${i?'L':'M'}${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(' ')+` L${points[points.length-1].x.toFixed(2)} ${height-padBottom} Z`;const guides=[0,.25,.5,.75,1].map(r=>{const y=(padTop+innerH-(innerH*r)).toFixed(2);return `<line x1="${padX}" y1="${y}" x2="${width-padX}" y2="${y}" stroke="var(--line2)" stroke-width="1"/>`}).join('');const markers=points.map(p=>`<circle cx="${p.x.toFixed(2)}" cy="${p.y.toFixed(2)}" r="3.5" fill="var(--accent)" stroke="var(--bg1)" stroke-width="1.5"><title>${esc(p.label)}：${esc(p.value+unit)}</title></circle>`).join('');const labels=points.filter((_,i)=>arr.length<=8||i===0||i===arr.length-1||i%Math.ceil(arr.length/6)===0).map(p=>`<div style="font-size:11px;color:var(--muted);min-width:0;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.label)}</div>`).join('');return `<div style="display:grid;gap:10px"><div style="border:1px solid var(--line2);border-radius:12px;padding:12px;background:var(--glass3)"><svg viewBox="0 0 ${width} ${height}" width="100%" height="170" preserveAspectRatio="none" role="img" aria-label="趋势图">${guides}<path d="${area}" fill="rgba(var(--accent-rgb),.14)"></path><path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></path>${markers}</svg><div style="display:grid;grid-template-columns:repeat(${Math.max(2,Math.min(points.filter((_,i)=>arr.length<=8||i===0||i===arr.length-1||i%Math.ceil(arr.length/6)===0).length,6))},minmax(0,1fr));gap:8px;margin-top:8px">${labels}</div></div><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap">${[arr[0],arr[arr.length-1],arr.reduce((a,b)=>Number(a.value)>=Number(b.value)?a:b)].map((x,i)=>`<span class="pill">${esc((i===0?'起点':i===1?'终点':'峰值')+'：'+(x.label||'-')+' '+(x.value||0)+unit)}</span>`).join('')}</div></div>`}
function donutSegments(items,centerLabel='调用总量'){const arr=(Array.isArray(items)?items:[]).filter(x=>Number(x.value)>0);if(!arr.length)return '<div class="desc">暂无数据</div>';const total=arr.reduce((s,x)=>s+(Number(x.value)||0),0)||1;let acc=0;const colors=['var(--accent)','var(--accent3,#a78bfa)','var(--accent2)','#fb7185','#f59e0b','#60a5fa'];const stops=[];arr.forEach((x,i)=>{const start=Math.round(acc/total*360);acc+=Number(x.value)||0;const end=Math.round(acc/total*360);stops.push(`${colors[i%colors.length]} ${start}deg ${end}deg`)});return `<div class="donut-layout"><div class="donut-chart" style="--donut-gradient:conic-gradient(${stops.join(',')})"><div class="donut-center"><div><strong>${total}</strong><span>${esc(centerLabel)}</span></div></div></div><div style="display:grid;gap:10px">${arr.map((x,i)=>{const pct=Math.round((Number(x.value)||0)/total*100);return `<div style="display:grid;gap:6px"><div style="display:flex;justify-content:space-between;gap:12px;align-items:center"><div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${colors[i%colors.length]}"></span><span>${esc(x.label)}</span></div><span class="pill">${esc(x.value)} · ${pct}%</span></div><div style="height:8px;border-radius:999px;background:var(--line);overflow:hidden"><div style="height:100%;width:${Math.max(2,pct)}%;background:${colors[i%colors.length]};border-radius:999px"></div></div></div>`}).join('')}</div></div>`}
function fmtStatNum(n){const v=Number(n||0);if(!Number.isFinite(v))return '0';if(v>=1e6)return (v/1e6).toFixed(2)+'M';if(v>=1e4)return (v/1e3).toFixed(1)+'k';return v.toLocaleString()}
function renderStats(){
  const st=state.bundle.statistics||{},sum=st.summary||{},models=st.model_ranking||[],top10=st.session_tokens_top10_1d||[],apiHistory=st.api_history||[];
  const cacheRate=sum.cache_hit_rate!=null?`${sum.cache_hit_rate}%`:'--';
  const cacheSub=sum.total_prompt_tokens>0?`命中 ${fmtStatNum(sum.total_cached_tokens||0)} / 输入 ${fmtStatNum(sum.total_prompt_tokens)}`:'待新请求采样';
  const successRate=sum.success_rate!=null?`${sum.success_rate}%`:'--';
  const successSub=(sum.success_calls||sum.failure_calls)?`成功 ${sum.success_calls||0} / 失败 ${sum.failure_calls||0}`:'暂无完成记录';
  const updatedText=st.generated_at?`更新于 ${new Date(st.generated_at*1000).toLocaleTimeString()}`:'实时聚合';
  return `<div class="card"><div class="section-head"><div><h3>核心指标</h3><p>${esc(updatedText)}</p></div><button class="btn" onclick="refreshAll(true)">刷新统计</button></div><div class="mini-stats stats-metrics"><div class="stat"><b>${esc(fmtStatNum(sum.api_calls??sum.message_count??0))}</b><span>模型调用次数</span></div><div class="stat"><b>${esc(fmtStatNum(sum.api_tokens||0))}</b><span>累计 Tokens</span></div><div class="stat"><b>${esc(cacheRate)}</b><span>缓存平均命中率</span><div class="stat-sub">${esc(cacheSub)}</div></div><div class="stat"><b>${esc(successRate)}</b><span>调用成功率</span><div class="stat-sub">${esc(successSub)}</div></div><div class="stat"><b>${esc(fmtStatNum(sum.avg_tokens_per_call||0))}</b><span>平均单次 Tokens</span></div><div class="stat"><b>${esc(sum.model_count||0)}</b><span>参与模型数</span></div></div></div><div class="card half"><div class="section-head"><div><h3>调用来源占比</h3><p>群聊、私聊、规划器与角色生活分布</p></div></div>${donutSegments(st.message_scene||[],'调用总量')}</div><div class="card half"><div class="section-head"><div><h3>调用趋势</h3><p>最近 24 个统计时段</p></div></div>${chartLine(st.message_trend||[],' 次')}</div><div class="card half"><div class="section-head"><div><h3>Token 消耗趋势</h3><p>最近 24 小时用量变化</p></div></div>${chartLine(st.token_trend_1d||[],' Tok')}</div><div class="card half"><div class="section-head"><div><h3>最近 1 天会话 Tokens Top 10</h3><p>按会话维度统计 Token 消耗</p></div></div>${chartBars(top10.map(x=>({label:`${x.session} · ${x.calls}次`,value:x.tokens})),0,' Tok','linear-gradient(135deg,#fb7185,#f59e0b)')}</div><div class="card"><div class="section-head"><div><h3>模型调用排名及用量</h3><p>按 Tokens 与调用次数排序</p></div></div>${chartBars(models.slice(0,10).map(x=>({label:`${x.model} · ${x.calls}次${x.cache_hit_rate!=null?` · 缓存 ${x.cache_hit_rate}%`:''}`,value:x.tokens})),0,' Tok','linear-gradient(135deg,var(--accent2),var(--accent3))')}</div><div class="card"><div class="section-head"><div><h3>模型调用历史</h3><p>最近 30 条调用记录</p></div></div>${apiHistory.length?`<div style="display:grid;gap:10px">${apiHistory.map(x=>{const hitPill=x.prompt_tokens>0?`<span class="pill">缓存 ${Math.round(Math.min(100,(x.cached_tokens||0)*100/x.prompt_tokens))}%</span>`:'';return `<div class="field" style="padding:14px"><div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap"><div><div style="font-weight:900">${esc(x.model)} <span class="pill ${x.status==='success'?'ok':x.status==='failed'?'bad':''}">${esc(x.status)}</span></div><div class="desc">${esc(x.scene)} · ${esc(x.host)} · ${esc(x.time)}</div></div><div style="display:flex;gap:8px;flex-wrap:wrap"><span class="pill">${esc(x.message_count||0)} 条消息</span><span class="pill">${esc(x.tokens||0)} Tok</span>${hitPill}</div></div><div class="desc" style="margin-top:10px">${esc(x.preview||'')||'（无预览）'}</div></div>`}).join('')}</div>`:`<div class="desc">暂无模型调用历史。</div>`}</div>`
}
function dbgKV(rows){return `<div class="kv">${rows.map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1]??'不可用')}</div>`).join('')}</div>`}
function dbgUptime(s){if(s==null)return '--';s=Number(s);const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'天':'')+(h||d?h+'时':'')+m+'分'}
function renderDebug(){
  const d=state.bundle.status?.debug||{},ru=d.resource_usage||state.bundle.status?.resource_usage||{},rt=d.runtime||{},gc=d.gc||{},web=d.webui||{},conn=d.connection||{},sys=d.system||{},proc=d.process||{},paths=d.paths||{},ui=d.update_install||{};
  const hasBot=Object.keys(rt).length>0;
  const procCpu=ru.cpu_percent_process??ru.cpu_percent??0,normCpu=ru.cpu_percent_normalized??ru.cpu_percent??0,sysCpu=ru.cpu_percent_system??'--';
  const cnt=rt.counters||{};
  const connStateMap={connected:'已连接',connecting:'连接中',disconnected:'已断开',failed:'失败',starting:'启动中',stopped:'已停止',unknown:'未知'};
  const fmtTs=ts=>ts?new Date(Number(ts)*1000).toLocaleString():'--';
  let html=`<div class="card"><div class="section-head"><div><h3>进程资源</h3><p>当前 Python 进程的实时采样</p></div><button class="btn" onclick="refreshAll(true)">刷新调试数据</button></div><div class="mini-stats"><div class="stat"><b>${esc((ru.rss_mb??ru.memory_mb??0)+' MB')}</b><span>RSS 内存</span></div><div class="stat"><b>${esc(normCpu+'%')}</b><span>CPU(整机)</span></div><div class="stat"><b>${esc(procCpu+'%')}</b><span>CPU(单核)</span></div><div class="stat"><b>${esc(ru.threads??'--')}</b><span>线程</span></div><div class="stat"><b>${esc(cnt.uptime_seconds!=null?dbgUptime(cnt.uptime_seconds):(state.bundle.status?.uptime_text||'--'))}</b><span>运行时长</span></div><div class="stat"><b>${esc(connStateMap[conn.state]||conn.state||'--')}</b><span>Bot 连接</span></div></div></div>`;
  if(!hasBot){html+=`<div class="card"><div class="desc">⚠️ 未检测到运行中的 Bot 主进程（或 WebUI 以独立模式启动）。以下仅显示 WebUI 自身可见的数据，启动 Bot 后将展示功能开关、API/Key、插件、权限、上下文等完整运行时统计。</div>${d.runtime_error?`<pre class="log" style="margin-top:12px">${esc(d.runtime_error)}</pre>`:''}</div>`}
  const rows=[['PID',proc.pid??state.bundle.status?.pid],['进程状态',proc.status],['进程创建',fmtTs(proc.create_time)],['进程CPU(整机)',`${normCpu}%`],['进程CPU(单核)',`${procCpu}%`],['系统总CPU',`${sysCpu}%`],['CPU逻辑核心',ru.cpu_count??'--'],['RSS 内存',`${ru.rss_mb??ru.memory_mb??0} MB`],['虚拟内存',`${ru.vms_mb??0} MB`],['线程数',ru.threads??'--'],['打开文件',ru.open_files??'--'],['网络连接',ru.connections??'--'],['句柄数',proc.num_handles??'--'],['日志缓冲',web.log_buffer??0],['GC 对象',gc.tracked_objects??'--'],['GC 计数',(gc.counts||[]).join(' / ')],['GC 阈值',(gc.thresholds||[]).join(' / ')]];
  html+=`<div class="card half"><div class="section-head"><div><h3>运行快照</h3><p>${esc(fmtTs(d.generated_at))}</p></div></div>${dbgKV(rows)}</div>`;
  const sysRows=[['系统内存',sys.memory_used_mb!=null?`${sys.memory_used_mb} / ${sys.memory_total_mb} MB (${sys.memory_percent}%)`:undefined],['可用内存',sys.memory_available_mb!=null?`${sys.memory_available_mb} MB`:undefined],['磁盘',sys.disk_used_gb!=null?`${sys.disk_used_gb} / ${sys.disk_total_gb} GB (${sys.disk_percent}%)`:undefined],['磁盘剩余',sys.disk_free_gb!=null?`${sys.disk_free_gb} GB`:undefined],['系统启动',fmtTs(sys.boot_time)],['WebUI 监听',web.host&&web.port?`${web.host}:${web.port}`:undefined],['访问 Token',web.has_access_token!=null?(web.has_access_token?'已设置':'未设置'):undefined],['更新缓存',web.update_cache!=null?(web.update_cache?`有 (约 ${web.update_cache_age_seconds??'?'}s)`:'无'):undefined],['更新安装状态',ui.state||'idle'],['更新详情',ui.detail||ui.text||'']];
  html+=`<div class="card half"><div class="section-head"><div><h3>系统 / WebUI</h3><p>主机与服务侧信息</p></div></div>${dbgKV(sysRows)}</div>`;
  const pathRows=[['运行目录',paths.base_dir||state.bundle.status?.cwd],['config.json',paths.config_exists!=null?`${paths.config_exists?'存在':'缺失'} (${paths.config_size_kb??0} KB)`:undefined],['temps',paths.temps?`${paths.temps.files} 文件 / ${paths.temps.size_mb} MB${paths.temps.truncated?' (截断)':''}`:undefined],['data',paths.data?`${paths.data.files} 文件 / ${paths.data.size_mb} MB${paths.data.truncated?' (截断)':''}`:undefined],['plugins',paths.plugins?`${paths.plugins.files} 文件 / ${paths.plugins.size_mb} MB`:undefined],['WebUI 聊天室',rt.webui_chat?`${rt.webui_chat.sessions} 会话 / ${rt.webui_chat.size_mb} MB`:undefined]];
  html+=`<div class="card half"><div class="section-head"><div><h3>目录体量</h3><p>定位磁盘膨胀</p></div></div>${dbgKV(pathRows)}</div>`;
  const ts=rt.token_stats||{},nc=rt.nickname_cache||{},cdb=rt.chat_db||{},mem=rt.ai_memory||{};
  const cacheRows=[['昵称缓存',nc.items!=null?`${nc.items}${nc.max?' / '+nc.max:''}`:undefined],['chat_db 群数',cdb.groups],['chat_db 历史总数',cdb.history_total],['Token 总量',ts.total_tokens],['Token 会话/用户/群',(ts.sessions!=null)?`${ts.sessions} / ${ts.users} / ${ts.groups}`:undefined],['AI记忆(私/群)',(mem.private!=null)?`${mem.private} / ${mem.group}`:undefined],['拍一拍冷却',cnt.poke_cooldowns],['总结记录群',cnt.summary_groups],['正在生成',cnt.generating!=null?(cnt.generating?'是':'否'):undefined],['主循环运行',cnt.running!=null?(cnt.running?'是':'否'):undefined],['机器人名',cnt.bot_name],['版本',cnt.version_name],['命令前缀',cnt.reminder]];
  html+=`<div class="card half"><div class="section-head"><div><h3>缓存 / 计数器</h3><p>定位跑久后变胖的对象</p></div></div>${dbgKV(cacheRows)}</div>`;
  const ak=rt.api_keys;if(ak&&!ak.error){html+=`<div class="card"><div class="section-head"><div><h3>API / Key 状态</h3><p>当前：${esc(ak.current||'--')} ｜ 槽位数：${esc(ak.model_slots??'--')} ｜ 切换日志：${esc(ak.switch_logs_count??0)}</p></div></div><div class="mini-stats"><div class="stat"><b>${esc(ak.total??0)}</b><span>Key 总数</span></div><div class="stat"><b>${esc(ak.active??0)}</b><span>可用</span></div><div class="stat"><b>${esc(ak.cooldown??0)}</b><span>冷却中</span></div><div class="stat"><b>${esc(ak.disabled??0)}</b><span>已禁用</span></div><div class="stat"><b>${esc(ak.multimodal??0)}</b><span>多模态</span></div><div class="stat"><b>${esc(ak.fail_total??0)}</b><span>累计失败</span></div></div>${(ak.items&&ak.items.length)?`<div style="display:grid;gap:8px;margin-top:12px">${ak.items.map(it=>`<div class="field" style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center"><b>${esc(it.api_name||('API-'+it.id))}</b><span class="pill">${esc(it.model||'')}</span><span class="pill">${esc(it.status||'')}</span>${it.supports_multimodal?'<span class="pill">多模态</span>':''}${it.fail_count?`<span class="pill" style="border-color:rgba(255,120,120,.4)">失败${esc(it.fail_count)}</span>`:''}${it.is_current?'<span class="pill" style="border-color:rgba(56,213,255,.5)">当前</span>':''}${it.is_default?'<span class="pill">首槽</span>':''}${it.last_error?`<span class="desc" style="flex-basis:100%">${esc(it.last_error)}</span>`:''}</div>`).join('')}</div>`:''}${(ak.switch_logs&&ak.switch_logs.length)?`<div style="margin-top:14px"><div class="label">最近切换</div><div style="display:grid;gap:6px;margin-top:8px">${ak.switch_logs.slice().reverse().map(l=>`<div class="desc">${esc(l.time||'')}  #${esc(l.from)} → #${esc(l.to)}  ${esc(l.reason||'')}${l.manual?' [手动]':''}</div>`).join('')}</div></div>`:''}${(ak.recent_errors&&ak.recent_errors.length)?`<div style="margin-top:14px"><div class="label">最近错误</div><div style="display:grid;gap:6px;margin-top:8px">${ak.recent_errors.map(e=>`<div class="desc">${esc(e.model||'')} ×${esc(e.fail_count||0)}：${esc(e.error||'')}</div>`).join('')}</div></div>`:''}</div>`}else if(ak&&ak.error){html+=`<div class="card"><h3>API / Key 状态</h3><pre class="log">${esc(ak.error)}</pre></div>`}
  const fs=rt.feature_switches;if(fs&&Object.keys(fs).length){const on=Object.keys(fs).filter(k=>fs[k]),off=Object.keys(fs).filter(k=>!fs[k]);html+=`<div class="card half"><div class="section-head"><div><h3>功能热开关</h3><p>开 ${on.length} / 关 ${off.length}</p></div></div><div style="display:flex;flex-wrap:wrap;gap:6px">${on.map(k=>`<span class="pill" style="border-color:rgba(66,230,164,.45)">${esc(k)}</span>`).join('')}${off.map(k=>`<span class="pill" style="opacity:.55">${esc(k)}:关</span>`).join('')}</div></div>`}
  const pl=rt.plugins;if(pl){html+=`<div class="card half"><div class="section-head"><div><h3>插件</h3><p>已加载 ${pl.loaded} ｜ 禁用 ${pl.disabled} ｜ 失败 ${pl.failed}</p></div></div>${(pl.loaded_names&&pl.loaded_names.length)?`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${pl.loaded_names.map(n=>`<span class="pill">${esc(n)}</span>`).join('')}</div>`:'<div class="desc">无已加载插件</div>'}${(pl.disabled_names&&pl.disabled_names.length)?`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${pl.disabled_names.map(n=>`<span class="pill" style="opacity:.6">禁用:${esc(n)}</span>`).join('')}</div>`:''}${(pl.failed_names&&pl.failed_names.length)?`<div style="display:flex;flex-wrap:wrap;gap:6px">${pl.failed_names.map(n=>`<span class="pill" style="border-color:rgba(255,120,120,.4)">失败:${esc(n)}</span>`).join('')}</div>`:''}</div>`}
  const pm=rt.permissions;if(pm){html+=`<div class="card half"><div class="section-head"><div><h3>权限 / 名单</h3></div></div>${dbgKV([['管理员数',pm.admin],['黑名单数',pm.blacklist],['管理员预览',(pm.admin_preview||[]).join(', ')||'--'],['黑名单预览',(pm.blacklist_preview||[]).join(', ')||'--']])}</div>`}
  const cs=rt.connection_snapshot;if(cs){html+=`<div class="card half"><div class="section-head"><div><h3>连接快照</h3><p>${rt.hot_switch?'⚠️ 热切换进行中':'稳定'} ｜ WebUI: ${esc(connStateMap[conn.state]||conn.state||'--')}${conn.detail?' · '+esc(conn.detail):''}</p></div></div>${dbgKV([['模式',cs.mode],['主机',(cs.host||'')+':'+(cs.port||'')],['监听',(cs.listener_host||'')+':'+(cs.listener_port||'')],['重试',cs.retries]])}</div>`}
  const cm=rt.cmc;if(cm){const comp=cm.compression||{};html+=`<div class="card"><div class="section-head"><div><h3>上下文管理器</h3><p>当前内存中加载的会话</p></div></div>${dbgKV([['群上下文',cm.group_contexts],['私聊上下文',cm.private_contexts],['加载消息总数',cm.loaded_messages],['OpenAI客户端池',cm.client_pool],['压缩-会话数',comp.total_sessions],['压缩-总次数',comp.total_compressions],['压缩阈值',comp.threshold],['保留最近',comp.keep_recent],['压缩客户端池',comp.client_pool]])}</div>`}
  const topCtx=cm?.top_contexts||[];if(topCtx.length){html+=`<div class="card"><div class="section-head"><div><h3>活跃上下文 Top</h3><p>按消息数排序</p></div></div><div style="display:grid;gap:10px">${topCtx.map(c=>`<div class="field" style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap"><b>${esc(c.kind)}:${esc(c.id)}</b><span class="pill">${esc(c.messages)} 条</span><span class="pill">${esc(c.tokens)} tokens</span><span class="pill">${esc(c.calls)} 次调用</span></div>`).join('')}</div></div>`}
  const topTok=ts.top_sessions||[];if(topTok.length){html+=`<div class="card"><div class="section-head"><div><h3>Token Top 会话</h3></div></div><div style="display:grid;gap:8px">${topTok.map(s=>`<div class="field" style="display:flex;justify-content:space-between;gap:10px"><b>${esc(s.session)}</b><span class="pill">${esc(s.tokens)} tokens</span></div>`).join('')}</div></div>`}
  const groups=cdb.top_groups||[];html+=`<div class="card"><div class="section-head"><div><h3>chat_db 群历史</h3><p>按历史条数排序</p></div></div>${groups.length?`<div style="display:grid;gap:10px">${groups.map(g=>`<div class="field" style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap"><b>${esc(g.group)}</b><span class="pill">${esc(g.history)} 条历史</span><span class="pill">${esc(g.tokens)} tokens</span></div>`).join('')}</div>`:'<div class="desc">暂无 chat_db 数据。</div>'}</div>`;
  const thr=proc.thread_names||[];if(thr.length){html+=`<div class="card"><div class="section-head"><div><h3>Python 线程</h3><p>${thr.length} 个命名线程</p></div></div><div style="display:flex;flex-wrap:wrap;gap:6px">${thr.map(n=>`<span class="pill">${esc(n)}</span>`).join('')}</div></div>`}
  html+=`<div class="card"><div class="section-head"><div><h3>出站自检</h3><p>让机器人给自己的 QQ 发一条私聊消息，验证 OneBot 出站链路是否通畅</p></div></div><div style="display:grid;gap:10px"><div class="field"><div class="label"><span>消息内容</span></div><input id="dbgSelfMsg" type="text" placeholder="留空则自动生成带时间戳的自检消息"><div class="desc">发送目标固定为机器人自己的 QQ（self_id），不会打扰其他用户</div></div><div style="display:flex;gap:10px;flex-wrap:wrap"><button class="btn primary" onclick="debugSendSelfMessage(this)">发送调试消息</button></div>${dbgSelfResult?`<div class="field" style="white-space:pre-wrap;word-break:break-word;font-family:Consolas,JetBrains Mono,monospace;font-size:12px;line-height:1.6${dbgSelfResult.ok?'':';color:var(--bad)'}">${esc(dbgSelfResult.text)}</div>`:''}</div></div>`;
  html+=`<div class="card"><div class="section-head"><div><h3>报错测试</h3><p>向日志发送测试消息</p></div></div><div style="display:flex;gap:10px;flex-wrap:wrap"><button class="btn" onclick="api('/api/debug/test-log?level=error').catch(()=>{})">发送 ERROR（红）</button><button class="btn" onclick="api('/api/debug/test-log?level=warn').catch(()=>{})">发送 WARN（黄）</button><button class="btn" onclick="api('/api/debug/test-log?level=info').catch(()=>{})">发送普通 INFO</button></div></div>`;
  html+=`<div class="card"><div class="section-head"><div><h3>重启程序</h3><p>拉起新进程并退出当前进程，配置与记忆会先保存；QQ 连接会短暂断开</p></div></div><div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center"><button class="btn" style="border-color:rgba(255,160,120,.55)" onclick="debugRestart(this)">重启程序</button><span class="desc">未保存的设置不会被保留，重启前请先点右上角保存</span></div></div>`;
  return html}
async function debugRestart(btn){
  if(!confirm('确定重启程序？QQ 连接会短暂断开，未保存的设置将丢失。'))return;
  if(btn){btn.disabled=true;btn.textContent='正在重启…'}
  try{
    const r=await api('/api/debug/restart',{method:'POST',body:'{}'});
    toast(r?.message||'正在重启…',true);
    // 复用自动更新那套轮询：新进程起来能返回 /api/ui-state 时自动刷新页面
    scheduleReloadAfterUpdate();
  }catch(e){
    // 重启中连接被切断属正常，照样进轮询等新进程
    toast('已发出重启指令，等待新进程启动…',true);
    scheduleReloadAfterUpdate();
  }
}
let traceState={list:null,detail:null,openId:'',enabled:null,savedEnabled:null,loading:false,detailLoading:false,error:'',switching:false,reqOpen:false,reqMode:'structured',reqAttempt:-1,ctxOpen:false,toolsOpen:false,sig:'',total:null,span:null,max:100};
function traceTs(ts){if(!ts)return '--';const d=new Date(Number(ts)*1000);const p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}/${d.getMonth()+1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`}
function traceMs(ms){const n=Number(ms||0);return n>=1000?(n/1000).toFixed(2)+'s':n+'ms'}
const TRACE_CAT={success:'成功',rate_limit:'限流 429',busy:'服务繁忙 503',server:'服务端错误',auth:'鉴权失败',model_missing:'模型不存在',quota:'额度不足',choices:'返回体异常',failover_keyword:'命中切换关键词',other:'其他错误'};
function traceScene(r){const t=r.context_type==='group'?'群':(r.context_type==='private'?'私聊':(r.context_type||'会话'));return r.chat_id?`${t} ${r.chat_id}`:(r.session_id||t)}
// 追踪列表指纹：只有条数或首条 id 变了才需要重建 DOM。
// 用首条 id 而不是全量 JSON，是因为新记录只会加在顶部，够判断了。
function traceListSignature(){
  const l=traceState.list;
  if(!Array.isArray(l))return 'null';
  // 不能只看条数和首条 ID：回复发出后 attach_send 会把分段数与 message_id
  // 回填到已有记录上，这两个值不变，DOM 就永远不刷新。把每条的发送状态也算进来。
  const sent=l.map(r=>(r?.id||'')+':'+(r?.send_parts??'')+':'+((r?.message_ids||[]).length)).join(',');
  // loading 也要进指纹：不然刷新时顶部进度条不会出现/消失。
  return l.length+'|'+(l[0]?.id||'')+'|'+(traceState.total??'')+'|'+(traceState.enabled?1:0)+'|'+(traceTogglePending()?1:0)+'|'+(traceState.loading?1:0)+'|'+sent;
}
// 记录所有可能的滚动位置。真正滚动的容器随主题而异：
// app.css 给 .main 设了 overflow-y:auto，窄屏媒体查询下又回落到 window，
// 所以这里全都记一遍，恢复时哪个有值就放回哪个。
function captureTraceScroll(){
  const main=document.querySelector('.main');
  const c=el('content');
  return {
    page:window.scrollY||document.documentElement.scrollTop||0,
    main:main?main.scrollTop:0,
    content:c?c.scrollTop:0,
  };
}
function restoreTraceScroll(prev){
  if(!prev)return;
  const apply=()=>{
    const main=document.querySelector('.main');
    if(main&&prev.main)main.scrollTop=prev.main;
    const c=el('content');
    if(c&&prev.content)c.scrollTop=prev.content;
    if(prev.page)window.scrollTo(0,prev.page);
  };
  apply();
  // DOM 替换后高度要一帧才稳定，补一次
  requestAnimationFrame(apply);
}
async function traceLoad(silent){
  traceState.loading=true;traceState.error='';
  // 不清空 list：清空会让列表闪成"正在加载…"再跳回来。
  // 首次进入时 list 本就是 null，加载态照样能显示。
  // 自动轮询（silent）不要重绘，否则每次轮询都闪一下顶部进度条。
  if(!silent&&state.current==='trace')render();
  try{const d=await api('/api/trace/list');traceState.list=Array.isArray(d.records)?d.records:[];traceState.savedEnabled=!!d.enabled;if(traceState.enabled===null||!traceTogglePending())traceState.enabled=!!d.enabled;traceState.total=d.count||0;traceState.span=d.span_hours;traceState.max=d.max_records||0}
  catch(e){traceState.error=e.message;traceState.list=[]}
  finally{traceState.loading=false;if(state.current==='trace')render()}
}
async function traceOpen(id){
  traceState.openId=id;traceState.detail=null;traceState.error='';traceState.sig='';traceState.reqOpen=false;traceState.reqAttempt=-1;traceState.ctxOpen=false;traceState.toolsOpen=false;traceState.detailLoading=true;render();
  try{traceState.detail=await api('/api/trace/detail?id='+encodeURIComponent(id))}
  catch(e){traceState.error=e.message}
  finally{traceState.detailLoading=false;if(state.current==='trace')render()}
}
function traceBack(){traceState.openId='';traceState.detail=null;traceState.error='';traceState.reqOpen=false;traceState.reqAttempt=-1;traceState.ctxOpen=false;traceState.toolsOpen=false;traceState.sig='';render();traceLoad()}
function traceRerenderKeepScroll(){const sc=captureTraceScroll();forceRender();restoreTraceScroll(sc)}
function traceToggleReq(){traceState.reqOpen=!traceState.reqOpen;traceRerenderKeepScroll()}
function traceSetReqMode(mode){traceState.reqMode=mode==='raw'?'raw':'structured';traceRerenderKeepScroll()}
function traceSelectReqAttempt(idx){traceState.reqAttempt=Number.isInteger(idx)?idx:-1;traceRerenderKeepScroll()}
function traceJumpToAttemptReq(idx){
  traceState.reqOpen=true;traceState.reqAttempt=Number.isInteger(idx)?idx:-1;forceRender();
  requestAnimationFrame(()=>el('traceRequestCard')?.scrollIntoView?.({block:'start',behavior:'smooth'}));
}
function traceToggleCtx(){traceState.ctxOpen=!traceState.ctxOpen;traceRerenderKeepScroll()}
function traceToggleTools(){traceState.toolsOpen=!traceState.toolsOpen;traceRerenderKeepScroll()}
const TRACE_CARET_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
function traceResolveRequest(d){
  const attempts=Array.isArray(d?.attempts)?d.attempts:[];
  const withReq=attempts.map((a,i)=>({idx:i,attempt:a,payload:a?.request_payload})).filter(x=>x.payload&&typeof x.payload==='object'&&Object.keys(x.payload).length);
  let selectedIdx=-1,payload=null,isLegacy=false;
  if(traceState.reqAttempt>=0&&attempts[traceState.reqAttempt]?.request_payload&&Object.keys(attempts[traceState.reqAttempt].request_payload).length){
    selectedIdx=traceState.reqAttempt;
    payload=attempts[traceState.reqAttempt].request_payload;
  }else if(withReq.length){
    const last=withReq[withReq.length-1];
    selectedIdx=last.idx;
    payload=last.payload;
  }else if(d?.request_payload&&typeof d.request_payload==='object'&&Object.keys(d.request_payload).length){
    payload=d.request_payload;
  }else{
    isLegacy=true;
    payload={model:d?.model||d?.display_model||'',messages:d?.system_prompt?[{role:'system',content:d.system_prompt}]:[]};
  }
  return {payload,attempts:withReq,selectedIdx,isLegacy};
}
function traceCopyRequest(){
  const d=traceState.detail;if(!d)return;
  const {payload}=traceResolveRequest(d);
  const text=JSON.stringify(payload||{},null,2);
  if(navigator.clipboard?.writeText){
    navigator.clipboard.writeText(text).then(()=>toast('已复制完整请求 JSON',true)).catch(()=>toast('复制失败',false));
  }else{toast('当前浏览器不支持一键复制',false)}
}
function traceFormatMsgContent(content){
  if(content==null)return '--';
  if(typeof content==='string')return content;
  if(Array.isArray(content)){
    return content.map(part=>{
      if(typeof part==='string')return part;
      if(!part||typeof part!=='object')return String(part);
      if(part.type==='text')return String(part.text||'');
      if(part.type==='image_url'){
        const url=typeof part.image_url==='string'?part.image_url:(part.image_url?.url||'');
        return `[image] ${url}`;
      }
      return JSON.stringify(part,null,2);
    }).join('\n');
  }
  return JSON.stringify(content,null,2);
}
function renderTraceRequestCard(d){
  const {payload,attempts,selectedIdx,isLegacy}=traceResolveRequest(d);
  const open=!!traceState.reqOpen;
  const msgs=Array.isArray(payload?.messages)?payload.messages:[];
  const tools=Array.isArray(payload?.tools)?payload.tools:[];
  const modelName=payload?.display_model||payload?.model||d.display_model||d.model||'--';
  const summaryParts=[`模型 ${modelName}`,`${msgs.length} 条 messages`];
  if(tools.length)summaryParts.push(`${tools.length} 个 tools`);
  if(payload?.reasoning_effort)summaryParts.push(`思考 ${payload.reasoning_effort}`);
  const rightPill=attempts.length>1?`${attempts.length} 次尝试`:(isLegacy?'旧版记录':`${msgs.length} 条`);
  let body='';
  if(open){
    const mode=traceState.reqMode==='raw'?'raw':'structured';
    const attemptBar=attempts.length>1?`<div class="trace-req-attempts"><span class="desc" style="margin:0">切换尝试：</span>${attempts.map((x,k)=>{const active=x.idx===selectedIdx;const isLast=k===attempts.length-1;const stLabel=x.attempt.ok?'成功':'失败';const name=x.attempt.display_model||x.attempt.model||('尝试'+(x.idx+1));return `<button type="button" class="btn ${active?'primary':''}" onclick="traceSelectReqAttempt(${x.idx})">#${x.idx+1} ${esc(name)} (${stLabel}${isLast?' · 最终':''})</button>`}).join('')}</div>`:'';
    const metaPills=[];
    if(payload?.model)metaPills.push(`<span class="pill">model: ${esc(payload.model)}</span>`);
    if(payload?.base_url)metaPills.push(`<span class="pill">endpoint: ${esc(payload.base_url)}</span>`);
    if(payload?.stream!==undefined)metaPills.push(`<span class="pill">stream: ${payload.stream?'true':'false'}</span>`);
    if(payload?.reasoning_effort)metaPills.push(`<span class="pill">reasoning_effort: ${esc(payload.reasoning_effort)}</span>`);
    if(payload?.tool_choice)metaPills.push(`<span class="pill">tool_choice: ${esc(typeof payload.tool_choice==='string'?payload.tool_choice:JSON.stringify(payload.tool_choice))}</span>`);
    if(payload?.timeout!=null)metaPills.push(`<span class="pill">timeout: ${esc(payload.timeout)}s</span>`);
    if(tools.length)metaPills.push(`<span class="pill">tools: ${esc(tools.length)}</span>`);
    let detailHtml='';
    if(mode==='raw'){
      detailHtml=`<pre class="trace-text trace-json">${esc(JSON.stringify(payload||{},null,2))}</pre>`;
    }else{
      const msgHtml=msgs.length?`<div class="trace-msg-list">${msgs.map((m,i)=>{
        const role=String(m?.role||'unknown').toLowerCase();
        const roleCls=['system','user','assistant','tool'].includes(role)?`trace-role-${role}`:'';
        const formatted=traceFormatMsgContent(m?.content);
        const tcJson=Array.isArray(m?.tool_calls)&&m.tool_calls.length?JSON.stringify(m.tool_calls,null,2):'';
        const chars=(formatted||'').length+tcJson.length;
        return `<div class="field trace-msg-item"><div class="trace-msg-head"><b>#${i+1}</b><span class="trace-role-badge ${roleCls}">${esc(role)}</span>${m?.name?`<span class="pill">${esc(m.name)}</span>`:''}${m?.tool_call_id?`<span class="pill">call_id: ${esc(m.tool_call_id)}</span>`:''}<span class="pill">${esc(chars)} 字</span></div>${formatted?`<div class="trace-text">${esc(formatted)}</div>`:''}${tcJson?`<div class="desc" style="margin:6px 0 4px">tool_calls</div><div class="trace-text">${esc(tcJson)}</div>`:''}</div>`;
      }).join('')}</div>`:'<div class="desc">无 messages 字段</div>';
      const toolsHtml=tools.length?`<details class="trace-tools-fold"><summary><span>工具定义（tools · ${tools.length} 个）</span><span class="desc" style="margin:0">点击查看 Schema</span></summary><pre class="trace-text trace-json">${esc(JSON.stringify(tools,null,2))}</pre></details>`:'';
      detailHtml=msgHtml+toolsHtml;
    }
    body=`<div class="trace-fold-body">${isLegacy?'<div class="desc" style="margin-bottom:10px">该条为旧版追踪记录，仅保留了系统提示词；之后的新对话将记录完整请求体。</div>':''}<div class="trace-req-bar">${attemptBar}<div class="trace-req-actions"><button type="button" class="btn ${mode==='structured'?'primary':''}" onclick="traceSetReqMode('structured')">分块视图</button><button type="button" class="btn ${mode==='raw'?'primary':''}" onclick="traceSetReqMode('raw')">原始 JSON</button><button type="button" class="btn" onclick="traceCopyRequest()">复制 JSON</button></div></div>${metaPills.length?`<div class="trace-req-meta">${metaPills.join('')}</div>`:''}${detailHtml}</div>`;
  }
  return `<div class="card" id="traceRequestCard"><div class="section-head trace-fold-head" style="${open?'':'margin-bottom:0'}" onclick="traceToggleReq()"><div><h3>最终发送请求</h3><p>${esc(summaryParts.join(' · '))} · 点击${open?'收起':'展开'}</p></div><div class="trace-fold-right"><span class="pill">${esc(rightPill)}</span><span class="trace-caret ${open?'is-open':''}" aria-hidden="true">${TRACE_CARET_SVG}</span></div></div>${body}</div>`;
}
function traceTogglePending(){return traceState.savedEnabled!==null&&traceState.enabled!==null&&!!traceState.enabled!==!!traceState.savedEnabled}
function traceToggle(sw){
  // 与功能配置页的开关保持一致：只改本地状态并标记未保存，点“保存设置”才真正生效
  const next=!sw.classList.contains('on');
  sw.classList.toggle('on',next);
  traceState.enabled=next;
  state.dirty=true;state.lastInputAt=Date.now();
  const save=el('saveState');if(save)save.textContent='有未保存更改'
}
async function traceSavePending(){
  if(!traceTogglePending())return;
  const want=!!traceState.enabled;
  const d=await api('/api/trace/switch',{method:'POST',body:JSON.stringify({enabled:want})});
  traceState.savedEnabled=!!d.enabled;traceState.enabled=!!d.enabled;
  traceLoad()
}
function traceDiscardPending(){
  if(traceState.savedEnabled!==null)traceState.enabled=!!traceState.savedEnabled
}
async function traceClear(){
  if(!confirm('清空所有追踪记录？'))return;
  try{await api('/api/trace/clear',{method:'POST',body:'{}'});toast('已清空',true);traceLoad()}
  catch(e){toast(e.message,false)}
}
function renderTrace(){return traceState.openId?renderTraceDetail():renderTraceList()}
function renderTraceHead(){
  const on=traceState.enabled===true;
  const pending=traceTogglePending()?'<span class="pill" style="border-color:rgba(255,200,120,.5)">未保存</span>':'';
  const meta=[];
  if(traceState.total!=null)meta.push(`当前 ${esc(traceState.total)} 条`);
  if(traceState.span!=null)meta.push(`覆盖约 ${esc(traceState.span)} 小时`);
  return `<div class="section-head"><div><h3>AI 对话追踪</h3><p>最多保留 ${esc(traceState.max||100)} 条（可在功能配置→系统设置调整），超出丢最旧的${meta.length?'　·　'+meta.join('　·　'):''}</p></div><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span class="desc">记录开关</span><div class="switch ${on?'on':''}" onclick="traceToggle(this)"></div>${pending}<button class="btn" onclick="traceLoad()">刷新</button><button class="btn" onclick="traceClear()">清空</button></div></div>`
}
function renderTraceList(){
  let body='';
  if(traceState.error)body=`<div class="desc" style="color:var(--bad)">${esc(traceState.error)}</div>`;
  else if(traceState.list===null)body=traceLoadingBlock('正在读取追踪记录…');
  else if(!traceState.list.length)body=`<div class="desc">最近 24 小时暂无记录。${traceState.enabled?'发生 AI 对话后会自动出现。':'追踪开关当前为关闭状态，开启后才会记录。'}</div>`;
  else body=`${traceState.loading?traceLoadingBar():''}<div style="display:grid;gap:10px">${traceState.list.map(r=>{
    const okPill=r.ok?'<span class="pill" style="border-color:rgba(66,230,164,.45)">成功</span>':'<span class="pill" style="border-color:rgba(255,120,120,.5)">失败</span>';
    const toolPill=r.tool_call_count>0?`<span class="pill" style="border-color:rgba(120,180,255,.5)">🔧 ${esc(r.tool_call_count)}${r.agent_rounds>1?`/${esc(r.agent_rounds)}轮`:''}${r.tool_calls_truncated?'+':''}</span>`:'';
    const retry=r.retry_count>0?`<span class="pill" style="border-color:rgba(255,200,120,.45)">重试 ${esc(r.retry_count)} 次</span>`:'';
    const parts=r.send_parts?`<span class="pill">${esc(r.send_parts)} 段</span>`:'';
    const img=r.images?`<span class="pill">图 ${esc(r.images)}</span>`:'';
    const fuPill=r.follow_up_count>0?`<span class="pill" style="border-color:rgba(120,180,255,.55)">💬 插话 ${esc(r.follow_up_count)}</span>`:'';
    const thinkPill=r.reasoning_chars>0?`<span class="pill" style="border-color:rgba(190,150,255,.5)">💭 思考 ${esc(r.reasoning_chars)} 字</span>`:'';
    return `<div class="field" style="cursor:pointer" data-trace-open="${esc(r.id)}">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><b>${esc(traceTs(r.time))}</b>${okPill}<span class="pill">${esc(traceScene(r))}</span><span class="pill">${esc(r.model||'--')}</span>${toolPill}${thinkPill}${fuPill}${retry}<span class="pill">${esc(r.total_tokens)} tokens</span><span class="pill">${esc(traceMs(r.duration_ms))}</span>${parts}${img}</div>
      <div class="desc" style="margin-top:6px">问：${esc(r.user_preview||'--')}</div>
      <div class="desc">${r.ok?'答：'+esc(r.reply_preview||'--'):'错误：'+esc(r.error_short||'--')}</div>
    </div>`}).join('')}</div>`;
  return `<div class="card">${renderTraceHead()}${body}</div>`
}
// 加载态：首次进入用进度条 + 骨架块占位；刷新已有列表时只在顶部走进度条，
// 不把已经显示出来的内容替换掉（那样会闪一下，翻到一半的位置也丢了）。
function traceLoadingBar(){return '<div class="load-bar" role="progressbar" aria-label="加载中"><span></span></div>'}
function traceLoadingBlock(label,rows){return `${traceLoadingBar()}<div class="desc" style="margin-bottom:10px">${esc(label||'加载中…')}</div><div style="display:grid;gap:10px">${'<div class="skel-row"></div>'.repeat(rows||5)}</div>`}
function traceBox(text,label){
  // 高度随内容走：用 div 而不是固定高度的 pre.log，超长才出现滚动条
  return `<div class="label">${label}</div><div class="trace-text">${esc(text||'--')}</div>`
}
function renderTraceDetail(){
  const d=traceState.detail;
  const head=(body)=>`<div class="card"><div class="section-head"><div><h3>追踪详情</h3></div><button class="btn" onclick="traceBack()">返回列表</button></div>${body}</div>`;
  if(traceState.error)return head(`<div class="desc" style="color:var(--bad)">${esc(traceState.error)}</div>`);
  if(!d)return head(traceLoadingBlock('正在读取本次对话的完整链路…',4));
  const tk=d.tokens||{},send=d.send||{},att=d.attempts||[],ov=d.history_overview||[];
  const okPill=d.ok?'<span class="pill" style="border-color:rgba(66,230,164,.45)">成功</span>':'<span class="pill" style="border-color:rgba(255,120,120,.5)">失败</span>';
  let html=`<div class="card"><div class="section-head"><div><h3>追踪详情</h3><p>${esc(traceTs(d.time))} ｜ ${esc(traceScene(d))} ｜ 总耗时 ${esc(traceMs(d.duration_ms))}</p></div><button class="btn" onclick="traceBack()">返回列表</button></div><div style="display:flex;gap:8px;flex-wrap:wrap">${okPill}<span class="pill">${esc(d.display_model||d.model||'--')}</span><span class="pill">会话 ${esc(d.session_id||'--')}</span><span class="pill">尝试 ${esc(d.attempt_count)} 次</span></div>${d.error?`<div class="desc" style="margin-top:10px;color:var(--bad)">最终错误：${esc(d.error)}</div>`:''}</div>`;
  const cached=Number(tk.cached||0);
  html+=`<div class="card"><div class="section-head"><div><h3>Token 统计</h3><p>接口返回的真实用量${cached?'':'（该接口未报告缓存命中）'}</p></div></div><div class="mini-stats"><div class="stat"><b>${esc(tk.prompt||0)}</b><span>输入</span></div><div class="stat"><b>${esc(tk.completion||0)}</b><span>输出</span></div><div class="stat"><b>${esc(cached)}</b><span>命中</span></div><div class="stat"><b>${esc(tk.total||0)}</b><span>总计</span></div></div>${(tk.relay_total?`<div class="desc" style="margin-top:10px">其中图片转述额外消耗：总 ${esc(tk.relay_total)} ｜ 输入 ${esc(tk.relay_prompt||0)} ｜ 输出 ${esc(tk.relay_completion||0)}</div>`:'')}</div>`;
  html+=`<div class="card"><div class="section-head"><div><h3>消息发送链路</h3><p>本轮用户消息 → 模型回复 → 实际分段发送</p></div></div>
    ${traceBox(d.user_message,'用户消息'+(d.user_truncated?'（已截断）':''))}
    ${d.images?`<div class="desc" style="margin-top:6px">附带图片 ${esc(d.images)} 张</div>`:''}
    ${d.reasoning?`<div style="margin-top:12px">${traceBox(d.reasoning,'思考过程（不发送给用户）'+(d.reasoning_truncated?'（已截断）':''))}</div>`:''}
    <div style="margin-top:12px">${traceBox(d.reply,'模型回复'+(d.reply_truncated?'（已截断）':''))}</div>
    <div class="desc" style="margin-top:10px">分段发送：${send.parts!=null?esc(send.parts)+' 段':'未记录'}${(send.message_ids&&send.message_ids.length)?' ｜ message_id: '+esc(send.message_ids.join(', ')):''}</div></div>`;
  const tc=d.tool_calls||[];
  const fu=d.follow_ups||[];
  // Follow-Up 按「渠道尝试#轮次」建索引，插到工具链路对应的轮次后面显示。
  // 注入发生在「那一轮工具结果回灌之后」，所以挂在该轮末尾语义正好。
  const fuByRound={};
  fu.forEach(f=>{const k=(f.provider_attempt||1)+'#'+(f.round||0);(fuByRound[k]||(fuByRound[k]=[])).push(f)});
  const fuBlock=(items)=>items.map(f=>{
    const cls=f.delivered?'rgba(120,180,255,.55)':'rgba(255,160,90,.6)';
    const tag=f.delivered
      ?(f.injected_after_tool?`已注入（挂在 ${esc(f.injected_after_tool)} 结果后）`:'已注入（追加一轮对话）')
      :'未送达（循环已结束）';
    return `<div class="field" style="border-color:${cls};margin-bottom:10px">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><b>💬 用户插话</b><span class="pill" style="border-color:${cls}">${tag}</span>${f.images?`<span class="pill" style="border-color:rgba(120,180,255,.55)">图 ${esc(f.images)}</span>`:''}${f.time?`<span class="pill">${esc(traceTs(f.time))}</span>`:''}</div>
      <div style="display:grid;gap:6px;margin-top:6px">${f.texts.map((t,i)=>`<div class="trace-text" style="max-height:120px">${f.texts.length>1?`${i+1}. `:''}${esc(t)}</div>`).join('')}</div>
    </div>`}).join('');
  const TC_STATUS={ok:['成功','rgba(66,230,164,.45)'],error:['失败','rgba(255,120,120,.5)'],
                   running:['执行中','rgba(255,200,120,.45)'],cancelled:['已中断','rgba(255,160,90,.5)']};
  if(tc.length){
    // 按「第几次渠道尝试 + 第几轮」两级分组。只按 round 分的话，
    // 渠道尝试 1 的第 1 轮和渠道尝试 2 的第 1 轮会混进同一组，
    // 页面就把两次先后发生的重试显示成「并发」。
    const rounds={};
    tc.forEach(c=>{const k=(c.provider_attempt||1)+'#'+(c.round||0);(rounds[k]||(rounds[k]=[])).push(c)});
    const roundKeys=Object.keys(rounds).sort((a,b)=>{
      const [pa,ra]=a.split('#').map(Number),[pb,rb]=b.split('#').map(Number);
      return pa-pb||ra-rb});
    const total=d.tool_call_count!=null?d.tool_call_count:tc.length;
    const dropped=d.tool_calls_dropped||0;
    const roundsTotal=d.agent_rounds_total||roundKeys.length;
    const truncNote=dropped>0?`，受上限影响仅展示最近 ${esc(tc.length)} 次（已省略最早 ${esc(dropped)} 次）`:'';
    const fuNote=d.follow_up_count?`，用户插话 ${esc(d.follow_up_count)} 条`:'';
    // 默认收起：一次多轮任务的明细能有几十屏，展开着会把下面的模型链路和
    // 提示词挤到看不见。折叠交互与「上下文历史」保持一致。
    const toolsOpen=!!traceState.toolsOpen;
    html+=`<div class="card"><div class="section-head trace-fold-head" style="${toolsOpen?'':'margin-bottom:0'}" onclick="traceToggleTools()"><div><h3>Agent 工具调用链路</h3><p>共 ${esc(total)} 次调用，${esc(roundsTotal)} 轮${d.agent_llm_calls?`，模型请求 ${esc(d.agent_llm_calls)} 次`:''}${fuNote}${truncNote} · 点击${toolsOpen?'收起':'展开'}</p></div><div class="trace-fold-right"><span class="pill">${esc(tc.length)} 条明细</span><span class="trace-caret ${toolsOpen?'is-open':''}" aria-hidden="true">${TRACE_CARET_SVG}</span></div></div>`
      +(!toolsOpen?'':'<div class="trace-fold-body">'+roundKeys.map(rk=>{
        const items=rounds[rk];
        const [pa,rn]=rk.split('#').map(Number);
        const attemptTag=pa>1?`第 ${esc(pa)} 次渠道尝试 ｜ `:'';
        return `<div style="margin-bottom:14px"><div class="desc" style="margin-bottom:6px">${attemptTag}第 ${esc(rn)} 轮 ｜ ${esc(items.length)} 个工具${items.length>1?'（同轮并发）':''}</div><div style="display:grid;gap:10px">`
          +items.map(c=>{
            const st=TC_STATUS[c.status]||TC_STATUS[c.ok?'ok':'error'];
            const border=st[1];
            const tags=[`<span class="pill" style="border-color:${st[1]}">${esc(st[0])}</span>`,
                        `<span class="pill">${esc(traceMs(c.duration_ms))}</span>`];
            const cap=c.side_effect_capable!==undefined?c.side_effect_capable:c.side_effect;
            const est=c.effect_state||(cap?'fired':'none');
            if(cap){
              const EFF={fired:['已产生副作用','rgba(255,190,90,.55)'],
                         unknown:['副作用未知','rgba(255,140,90,.6)'],
                         abandoned:['未执行','rgba(150,150,150,.45)'],
                         none:['未执行','rgba(150,150,150,.45)']};
              const e=EFF[est]||EFF.none;
              tags.push(`<span class="pill" style="border-color:${e[1]}">${esc(e[0])}</span>`);
            }
            if(c.streak>=3)tags.push(`<span class="pill" style="border-color:rgba(255,120,120,.5)">连续第 ${esc(c.streak)} 次</span>`);
            if(c.notice)tags.push('<span class="pill">已提示换策略</span>');
            if(c.provider_attempt>1)tags.push(`<span class="pill">第 ${esc(c.provider_attempt)} 次渠道尝试</span>`);
            const mode=c.result_mode||(c.materialized?'overflow_file':'raw');
            if(mode==='overflow_file')tags.push('<span class="pill" style="border-color:rgba(120,180,255,.5)">结果已落盘</span>');
            else if(mode==='truncated')tags.push('<span class="pill" style="border-color:rgba(255,200,120,.5)">结果已截断</span>');
            const badResult=c.status==='error'||c.status==='cancelled';
            return `<div class="field" style="border-color:${border}">
              <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><b>${esc(c.name||'--')}</b>${tags.join('')}</div>
              <div class="desc" style="margin-top:6px">参数${c.args_truncated?`（共 ${esc(c.args_chars)} 字，已截断）`:''}</div>
              <div class="trace-text" style="max-height:140px">${esc(c.arguments||'{}')}</div>
              <div class="desc" style="margin-top:6px">${badResult?'错误':'结果'}${c.result_truncated?`（共 ${esc(c.result_chars)} 字，已截断）`:c.raw_result_chars?`（${esc(c.raw_result_chars)} 字）`:''}</div>
              <div class="trace-text" style="max-height:200px${badResult?';color:var(--bad)':''}">${esc(c.result||'--')}</div>
              ${c.sent_to_model?`<div class="desc" style="margin-top:6px">模型实际收到（${mode==='overflow_file'?'超长已落盘，只回灌预览与文件路径':'超长已截断，未能落盘'}）${c.overflow_error?` ｜ 落盘失败：${esc(c.overflow_error)}`:''}</div>
              <div class="trace-text" style="max-height:160px">${esc(c.sent_to_model)}</div>`:''}
            </div>`}).join('')
          +`</div>`
          // 该轮工具跑完后如果用户插了话，紧跟在这一轮末尾显示
          +((fuByRound[rk]||[]).length?`<div style="margin-top:10px">${fuBlock(fuByRound[rk])}</div>`:'')
          +`</div>`}).join('')
        // round=0 是「循环结束后才发现的未送达插话」，没有对应轮次，单独放最后
        +((fuByRound['1#0']||[]).length?fuBlock(fuByRound['1#0']):'')+'</div>')
      +`</div>`;
  }else if(fu.length){
    // 没调工具但有插话（模型直接文本回复的场景），单独一张卡片展示
    html+=`<div class="card"><div class="section-head"><div><h3>用户插话</h3><p>Agent 执行途中收到 ${esc(d.follow_up_count||0)} 条补充指令</p></div></div>${fuBlock(fu)}</div>`;
  }
  html+=`<div class="card"><div class="section-head"><div><h3>模型调用链路</h3><p>共 ${esc(att.length)} 次尝试${d.retry_count?`，重试 ${esc(d.retry_count)} 次`:''}</p></div></div>${att.length?`<div style="display:grid;gap:10px">${att.map((a,ai)=>{
    const border=a.ok?'rgba(66,230,164,.45)':'rgba(255,120,120,.5)';
    const hasReq=a.request_payload&&typeof a.request_payload==='object'&&Object.keys(a.request_payload).length>0;
    return `<div class="field" style="border-color:${border}">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><b>#${esc((a.index||0)+1)}</b><span class="pill">${esc(a.display_model||a.model||'--')}</span><span class="pill">${esc(traceMs(a.duration_ms))}</span><span class="pill">${esc(TRACE_CAT[a.category]||a.category||'--')}</span>${a.multimodal?'<span class="pill">识图</span>':''}<span class="pill">${esc(a.message_count||0)} 条消息</span>${hasReq?`<button type="button" class="btn" style="margin-left:auto;padding:4px 10px;font-size:12px" onclick="traceJumpToAttemptReq(${ai})">查看本次请求</button>`:''}</div>
      <div class="desc" style="margin-top:6px">${esc(a.base_url||'--')} ｜ key ${esc(a.key_hint||'--')}</div>
      ${a.error?`<div class="desc" style="color:var(--bad)">${esc(a.error_type||'')}: ${esc(a.error)}</div>`:''}
    </div>`}).join('')}</div>`:'<div class="desc">无尝试记录</div>'}</div>`;
  html+=renderTraceRequestCard(d);
  const ctxOpen=!!traceState.ctxOpen;
  html+=`<div class="card"><div class="section-head trace-fold-head" style="${ctxOpen?'':'margin-bottom:0'}" onclick="traceToggleCtx()"><div><h3>上下文历史</h3><p>本次调用前 ${esc(d.history_count||0)} 条 · 点击${ctxOpen?'收起':'展开'}</p></div><div class="trace-fold-right"><span class="pill">${esc(ov.length)} 条</span><span class="trace-caret ${ctxOpen?'is-open':''}" aria-hidden="true">${TRACE_CARET_SVG}</span></div></div>
    ${ctxOpen?`<div class="trace-fold-body">${ov.length?`<div style="display:grid;gap:8px">${ov.map((m,i)=>`<div class="field"><div style="display:flex;gap:8px;align-items:center"><b>#${i+1}</b><span class="pill">${esc(m.role)}</span><span class="pill">${esc(m.chars)} 字</span>${m.truncated?'<span class="pill">已截断</span>':''}</div><div class="trace-text" style="margin-top:6px">${esc(m.content||'--')}</div></div>`).join('')}</div>`:'<div class="desc">无历史上下文</div>'}</div>`:''}</div>`;
  return html
}
// 自检结果存在这里，而不是直接写进 DOM：调试页每 5 秒自动刷新一次会重建整页，
// 写进 DOM 的内容会被冲掉（表现为框过几秒自己消失）。
let dbgSelfResult=null;
async function debugSendSelfMessage(btn){
  const input=el('dbgSelfMsg'),text=input?input.value:'';
  if(btn){btn.disabled=true;btn.textContent='发送中...'}
  try{
    const d=await api('/api/debug/self-message',{method:'POST',body:JSON.stringify({text})});
    const parts=[`已发送到 QQ ${d.self_id||'?'}`];
    if(d.sent_text)parts.push(`内容：${d.sent_text}`);
    if(d.message_id!=null)parts.push(`message_id: ${d.message_id}`);
    dbgSelfResult={ok:true,text:parts.join('\n')};
    toast('自检消息已发送',true)
  }catch(e){
    dbgSelfResult={ok:false,text:'发送失败：'+e.message};
    toast(e.message,false)
  }finally{
    if(btn){btn.disabled=false;btn.textContent='发送调试消息'}
    forceRender()
  }
}
function providers(){const v=state.bundle?.form_values||{};let ps=Array.isArray(v['Others.llm_providers'])?v['Others.llm_providers']:[];ps.forEach(providerIdentity);return ps}
function rotation(){const v=state.bundle.form_values||{};return Array.isArray(v['Others.llm_rotation'])?v['Others.llm_rotation']:[]}
function providerModelLabel(pid,m){return (pid?pid+'/':'')+m}
function enabledProviderModels(){const out=[];providers().forEach(p=>(p.models||[]).forEach(m=>{if(m.enabled&&p.id&&m.name)out.push({provider_id:p.id,model:m.name,label:providerModelLabel(p.id,m.name),supports_multimodal:!!m.supports_multimodal})}));return out}
function ensureProviderSelection(){const ps=providers();if(!state.providerIndex||state.providerIndex>=ps.length)state.providerIndex=0;return state.providerIndex||0}
const PROVIDER_IDENTITY=Symbol('providerIdentity');
const providerDetections=new Map();
const detectedViews=new Map();
function providerIdentity(p){return p[PROVIDER_IDENTITY]||(p[PROVIDER_IDENTITY]=Symbol('provider'))}
function providerDetectParams(p){return JSON.stringify({provider_id:p.id,base_url:p.base_url,keys:p.keys||[],http_proxy:p.http_proxy||''})}
function providerRotationSignature(ps){return JSON.stringify(ps.flatMap(p=>(p.models||[]).filter(m=>m.enabled&&p.id&&m.name).map(m=>providerModelLabel(p.id,m.name))))}
function adoptProviderIdentities(previous,next){
  // 保存响应会规范化并重建对象；仅唯一且连接参数未变的提供商继承请求身份。
  for(const p of next){const matches=previous.filter(x=>x.id===p.id&&providerDetectParams(x)===providerDetectParams(p));if(matches.length===1&&next.filter(x=>x.id===p.id).length===1)p[PROVIDER_IDENTITY]=providerIdentity(matches[0])}
}
function setProviders(ps){
  const previous=providers(),rotationChanged=providerRotationSignature(previous)!==providerRotationSignature(ps);
  for(const p of ps)providerIdentity(p);
  for(const [id,request] of providerDetections){const p=ps.find(x=>providerIdentity(x)===id);if(!p||providerDetectParams(p)!==request.params)providerDetections.delete(id)}
  if(sameDraftValues(previous,ps))return;
  setValue('Others.llm_providers',ps);if(rotationChanged)syncRotationWithEnabled();
}
function setRotation(r){setValue('Others.llm_rotation',r)}
function syncRotationWithEnabled(){const enabled=enabledProviderModels(),valid=new Set(enabled.map(x=>x.label)),cur=rotation().filter(x=>valid.has(providerModelLabel(x.provider_id,x.model))),seen=new Set(cur.map(x=>providerModelLabel(x.provider_id,x.model)));enabled.forEach(x=>{if(!seen.has(x.label))cur.push({provider_id:x.provider_id,model:x.model})});if(!sameDraftValues(rotation(),cur))setValue('Others.llm_rotation',cur)}
function renderProvidersMultimodalCard(){if(typeof renderField!=='function')return '';const vals=state.bundle?.form_values||{},fields=(typeof meta==='function'&&meta()?.fields)||[];const modelField=fields.find(f=>f.path==='Others.api_multimodal_model')||{path:'Others.api_multimodal_model',label:'识图模型',type:'multimodal_model',desc:'主模型不支持识图且收到图片时使用的识图模型'};const modeField=fields.find(f=>f.path==='Others.api_multimodal_image_mode')||{path:'Others.api_multimodal_image_mode',label:'图片处理模式',type:'select',options:['relay','direct'],desc:'relay=识图模型先转述图片再交给主模型回复；direct=直接由识图模型回复图片消息'};return `<div class="card"><div class="section-head"><div><h3>识图与图片处理</h3><p>配置非多模态主模型收到图片时的回退策略</p></div></div><div class="form-grid">${renderField(modelField,vals[modelField.path])}${renderField(modeField,vals[modeField.path]??'relay')}</div></div>`}
function renderProviders(){const ps=providers(),idx=ensureProviderSelection(),p=ps[idx]||null;return `<div class="card"><div class="section-head"><div><h3>提供商</h3><p>检测模型、勾选模型并设置轮换顺序</p></div><button class="btn primary" onclick="addProvider()">新增提供商</button></div><div class="split-pane"><div class="split-pane-list">${ps.length?ps.map((x,i)=>`<button class="btn ${i===idx?'primary':''}" style="text-align:left" onclick="syncProviderFieldsFromDom();state.providerIndex=${i};forceRender()">${esc(x.id||('provider'+(i+1)))}</button>`).join(''):'<div class="desc">暂无提供商</div>'}</div><div>${p?renderProviderDetail(p,idx):'<div class="desc">点击新增提供商开始配置。</div>'}</div></div></div>${renderProvidersMultimodalCard()}${renderRotationList()}`}
function renderProviderDetail(p,idx){
  // 提供商内三个子标签页：对话模型 / 回复判断模型 / 嵌入模型
  const tab = state.providerSubTab?.[idx] || 'chat';

  const detecting=providerDetections.has(providerIdentity(p));
  return `<div class="form-grid">
    <div class="field"><div class="label"><span>提供商 ID</span><button class="btn" onclick="removeProvider(${idx})">删除</button></div><input id="provider_id_${idx}" value="${esc(p.id||'')}" oninput="updateProvider(${idx},'id',this.value.trim())"><div class="desc">Bot 显示名为 提供商ID/模型名</div></div>
    <div class="field"><div class="label"><span>Base URL</span></div><input id="provider_base_${idx}" value="${esc(p.base_url||'')}" placeholder="https://.../v1" oninput="updateProvider(${idx},'base_url',this.value.trim())"></div>
    <div class="field"><div class="label"><span>HTTP 代理（可选）</span></div><input id="provider_proxy_${idx}" value="${esc(p.http_proxy||'')}" placeholder="http://127.0.0.1:7890" oninput="updateProvider(${idx},'http_proxy',this.value.trim())"><div class="desc">仅作用于当前提供商，留空直连（支持 http / https）</div></div>
    <div class="field" style="grid-column:1/-1"><div class="label"><span>API Keys（每行一个）</span><button class="btn" id="providerDetectBtn_${idx}" ${detecting?'disabled':''} onclick="detectProviderModels(${idx})">${detecting?'检测中…':'检测模型'}</button></div><textarea id="provider_keys_${idx}" style="min-height:92px" placeholder="同一提供商的多个 API Key，每行一个" oninput="updateProvider(${idx},'keys',this.value.split(/\\r?\\n/).map(x=>x.trim()).filter(Boolean))">${esc(Array.isArray(p.keys)?p.keys.join('\n'):'')}</textarea><div class="desc">同一提供商可填写多个 Key；系统会按 Key 轮换，某个 Key 失败时暂时冷却，避免反复使用。</div><div class="desc" id="providerDetect_${idx}">${esc(detecting?'检测中…':p.detect_error||'')}</div><div id="providerDetected_${idx}">${renderDetectedModels(p,idx)}</div></div>
    <div style="grid-column:1/-1;margin-top:12px"><div class="subtab-nav" id="providerTabs_${idx}">${['chat','planner','embedding'].map((kind,i)=>`<button class="btn ${tab===kind?'primary':''}" data-tab="${kind}" onclick="switchProviderTab(${idx},this.dataset.tab)">${['对话模型','回复判断模型','嵌入模型'][i]}</button>`).join('')}</div><div class="subtab-panel" id="providerPanel_${idx}">${renderProviderTab(p,idx,tab)}</div></div>
  </div>`;
}
function animateSubtab(panel){
  if(!panel)return;
  panel.classList.remove('subtab-enter');
  panel.onanimationend=panel.onanimationcancel=null;
  if(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)return;
  void panel.offsetWidth;
  panel.classList.add('subtab-enter');
  panel.onanimationend=panel.onanimationcancel=event=>{
    if(event.target!==panel||!['subtab-content-in','glass-subtab-content-in'].includes(event.animationName))return;
    panel.classList.remove('subtab-enter');
    panel.onanimationend=panel.onanimationcancel=null;
  };
}
function switchProviderTab(idx,tab){
  if(!['chat','planner','embedding'].includes(tab)||(state.providerSubTab?.[idx]||'chat')===tab)return;
  syncProviderFieldsFromDom();rememberDetectedSelection(idx);
  (state.providerSubTab||(state.providerSubTab={}))[idx]=tab;
  const p=providers()[idx],panel=el('providerPanel_'+idx);if(!p||!panel)return;
  panel.innerHTML=renderProviderTab(p,idx,tab);
  const detected=el('providerDetected_'+idx);if(detected)detected.innerHTML=renderDetectedModels(p,idx);
  el('providerTabs_'+idx)?.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('primary',b.dataset.tab===tab));animateSubtab(panel);
}
function renderProviderTab(p,idx,tab){
  let tabContent = '';
  if (tab === 'chat') {
    tabContent = `<div class="section-header"><h4>对话模型</h4><button class="btn" onclick="openModelInput(${idx},'chat')">手动添加模型</button></div><div style="display:grid;gap:8px;margin-top:12px">${renderProviderModels(p,idx)}</div>`;
  } else if (tab === 'planner') {
    tabContent = `<div class="section-header"><h4>回复判断模型</h4><button class="btn" onclick="openModelInput(${idx},'planner')">手动添加模型</button></div><div class="desc" style="margin:12px 0 8px">用于主动回复的规划器判断，建议选择轻量快速的模型。</div><div style="display:grid;gap:8px">${renderPlannerModels(p,idx)}</div>`;
  } else {
    tabContent = `<div class="section-header"><h4>嵌入模型</h4><button class="btn" onclick="openModelInput(${idx},'embedding')">添加嵌入模型</button></div><div style="display:grid;gap:8px;margin-top:12px">${renderEmbeddingModels(p,idx)}</div><div class="desc" style="margin-top:12px">嵌入模型复用本提供商的 Base URL 和 Key，知识库可在这里选择。</div>`;
  }

  return tabContent;
}
const REASONING_LABELS=[['','默认'],['none','关闭'],['minimal','最小'],['low','低'],['medium','中'],['high','高']];
function modelRowOpen(idx,mi){return !!(state.modelRowOpen&&state.modelRowOpen[idx+':'+mi])}
function toggleModelRow(idx,mi){if(!state.modelRowOpen)state.modelRowOpen={};const k=idx+':'+mi;state.modelRowOpen[k]=!state.modelRowOpen[k];forceRender()}
function fmtWindow(n){n=Number(n||0);if(!n)return '';return n>=1000?(Math.round(n/100)/10+'k'):String(n)}
function renderProviderModels(p,idx){const models=Array.isArray(p.models)?p.models:[];if(!models.length)return '<div class="desc">暂无模型。可以检测或手动添加。</div>';return models.map((m,mi)=>{const open=modelRowOpen(idx,mi);
// 折起时用小徽标提示这个模型有非默认的高级设置，免得改过又忘了
const badges=[m.reasoning_effort?('思考:'+(REASONING_LABELS.find(x=>x[0]===m.reasoning_effort)||['','?'])[1]):'',m.context_window?('窗口:'+fmtWindow(m.context_window)):''].filter(Boolean).join(' · ');
return `<div class="field" style="padding:10px;display:grid;gap:8px"><div class="model-row"><input type="checkbox" title="启用此模型" aria-label="启用模型" ${m.enabled?'checked':''} onchange="updateProviderModel(${idx},${mi},'enabled',this.checked)"><input value="${esc(m.name||'')}" oninput="updateProviderModel(${idx},${mi},'name',this.value.trim())"><label style="display:flex;gap:6px;align-items:center"><input type="checkbox" ${m.supports_multimodal?'checked':''} onchange="updateProviderModel(${idx},${mi},'supports_multimodal',this.checked)">识图</label><span>超时</span><input type="number" step="1" value="${esc(m.timeout_seconds||60)}" oninput="updateProviderModel(${idx},${mi},'timeout_seconds',num(this.value))"><button class="btn" title="思考等级与上下文窗口" onclick="toggleModelRow(${idx},${mi})">${open?'▾':'⚙'}${badges&&!open?' ·':''}</button><button class="btn" onclick="removeProviderModel(${idx},${mi})">删除</button></div>${(!open&&badges)?`<div class="desc" style="margin:0">${esc(badges)}</div>`:''}${open?`<div class="model-row-adv" style="display:grid;gap:8px;padding-top:4px;border-top:1px solid var(--line2)"><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px"><label style="display:grid;gap:4px"><span class="desc" style="margin:0">思考等级</span><select onchange="updateProviderModel(${idx},${mi},'reasoning_effort',this.value)">${REASONING_LABELS.map(([v,t])=>`<option value="${esc(v)}" ${(m.reasoning_effort||'')===v?'selected':''}>${esc(t)}</option>`).join('')}</select></label><label style="display:grid;gap:4px"><span class="desc" style="margin:0">上下文窗口（token）</span><input type="number" step="1000" placeholder="0＝不限制" value="${esc(m.context_window||'')}" oninput="updateProviderModel(${idx},${mi},'context_window',num(this.value))"></label></div><div class="desc">思考等级选「默认」时跟随模型自身行为；上下文窗口填 0 表示沿用全局设置。</div></div>`:''}</div>`}).join('')}
function renderEmbeddingModels(p,idx){const models=Array.isArray(p.embedding_models)?p.embedding_models:[];if(!models.length)return '<div class="desc">暂无嵌入模型。</div>';return models.map((m,mi)=>`<div class="embed-row field"><input type="checkbox" ${m.enabled!==false?'checked':''} onchange="updateEmbeddingModel(${idx},${mi},'enabled',this.checked)"><input value="${esc(m.name||'')}" oninput="updateEmbeddingModel(${idx},${mi},'name',this.value.trim())"><input type="number" placeholder="维度（可选）" value="${esc(m.dimensions||'')}" oninput="updateEmbeddingModel(${idx},${mi},'dimensions',num(this.value))"><button class="btn" onclick="removeEmbeddingModel(${idx},${mi})">删除</button></div>`).join('')}
function updateEmbeddingModel(i,mi,k,v){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].embedding_models=[...(ps[i].embedding_models||[])];ps[i].embedding_models[mi]=Object.assign({},ps[i].embedding_models[mi]||{});ps[i].embedding_models[mi][k]=v;setProviders(ps)}
function removeEmbeddingModel(i,mi){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].embedding_models=[...(ps[i].embedding_models||[])];ps[i].embedding_models.splice(mi,1);setProviders(ps);forceRender()}
function renderPlannerModels(p,idx){const models=Array.isArray(p.planner_models)?p.planner_models:[];if(!models.length)return '<div class="desc">暂无回复判断模型。</div>';return models.map((m,mi)=>`<div class="field" style="padding:10px;display:grid;gap:8px"><div class="model-row planner-row"><input type="checkbox" ${m.enabled?'checked':''} onchange="updatePlannerModel(${idx},${mi},'enabled',this.checked)"><input value="${esc(m.name||'')}" oninput="updatePlannerModel(${idx},${mi},'name',this.value.trim())"><span>超时</span><input type="number" step="1" value="${esc(m.timeout_seconds||60)}" oninput="updatePlannerModel(${idx},${mi},'timeout_seconds',num(this.value))"><button class="btn" onclick="removePlannerModel(${idx},${mi})">删除</button></div></div>`).join('')}
function updatePlannerModel(i,mi,k,v){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].planner_models=[...(ps[i].planner_models||[])];ps[i].planner_models[mi]=Object.assign({},ps[i].planner_models[mi]||{});ps[i].planner_models[mi][k]=v;setProviders(ps)}
function removePlannerModel(i,mi){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].planner_models=[...(ps[i].planner_models||[])];ps[i].planner_models.splice(mi,1);setProviders(ps);forceRender()}
function providerModelListKey(tab){return tab==='planner'?'planner_models':tab==='embedding'?'embedding_models':'models'}
function detectedView(p,tab){const id=providerIdentity(p);if(!detectedViews.has(id))detectedViews.set(id,{});const views=detectedViews.get(id);return views[tab]||(views[tab]={query:'',selected:''})}
function detectedOptions(p,tab,query=''){
  const existing=new Set((p[providerModelListKey(tab)]||[]).map(m=>m.name)),q=query.trim().toLowerCase();
  return [...new Set(p.detected_models||[])].filter(x=>!existing.has(x)&&(!q||String(x).toLowerCase().includes(q)));
}
function rememberDetectedSelection(i){const p=providers()[i],select=el('detected_select_'+i);if(p&&select)detectedView(p,state.providerSubTab?.[i]||'chat').selected=select.value}
function renderDetectedModels(p,idx){
  const tab=state.providerSubTab?.[idx]||'chat',detected=p.detected_models||[];
  if(!detected.length)return '';
  const view=detectedView(p,tab),options=detectedOptions(p,tab,view.query);
  if(!options.includes(view.selected))view.selected=options[0]||'';
  const btnText=tab==='embedding'?'添加为嵌入模型':tab==='planner'?'添加为回复判断模型':'添加为对话模型';
  return `<div class="field" style="margin-top:12px;padding:12px"><div class="label"><span>检测结果</span><span class="pill" id="detected_count_${idx}">${options.length} 个可添加 / ${detected.length} 个已检测</span></div><input id="detected_search_${idx}" value="${esc(view.query)}" placeholder="搜索模型名称" oninput="filterDetectedModels(${idx},this.value)"><select id="detected_select_${idx}" size="${Math.min(10,Math.max(3,options.length))}" ${options.length?'':'disabled'} style="margin-top:10px" onchange="rememberDetectedSelection(${idx})">${options.map(x=>`<option value="${esc(x)}" ${x===view.selected?'selected':''}>${esc(x)}</option>`).join('')}</select><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><button class="btn primary" id="detected_add_${idx}" onclick="addDetectedModel(${idx})" ${options.length?'':'disabled'}>${btnText}</button><button class="btn" onclick="clearDetectedModels(${idx})">清空检测结果</button></div><div class="desc">支持按名称搜索；只添加当前选中的模型。</div></div>`;
}
function filterDetectedModels(i,value){
  const p=providers()[i],tab=state.providerSubTab?.[i]||'chat',select=el('detected_select_'+i);if(!p||!select)return;
  const view=detectedView(p,tab);view.query=String(value||'');
  const options=detectedOptions(p,tab,view.query),selected=select.value||view.selected;
  view.selected=options.includes(selected)?selected:options[0]||'';
  select.innerHTML=options.map(x=>`<option value="${esc(x)}" ${x===view.selected?'selected':''}>${esc(x)}</option>`).join('');
  select.value=view.selected;select.size=Math.min(10,Math.max(3,options.length));select.disabled=!options.length;
  const btn=el('detected_add_'+i);if(btn)btn.disabled=!options.length;
  const count=el('detected_count_'+i);if(count)count.textContent=options.length+' 个可添加 / '+(p.detected_models||[]).length+' 个已检测';
}
function openModelInput(i,kind){
  modelInputState={providerIndex:i,kind:kind==='embedding'?'embedding':kind==='planner'?'planner':'chat'};
  const modal=el('modelInputModal'),title=el('modelInputTitle'),label=el('modelInputLabel'),input=el('modelInputValue');
  if(!modal||!input)return;
  if(title)title.textContent=modelInputState.kind==='embedding'?'添加嵌入模型':modelInputState.kind==='planner'?'添加回复判断模型':'添加对话模型';
  if(label)label.textContent=modelInputState.kind==='embedding'?'嵌入模型名称':modelInputState.kind==='planner'?'回复判断模型名称':'对话模型名称';
  input.value='';
  modal.style.display='flex';
  requestAnimationFrame(()=>input.focus())
}
function closeModelInput(){const modal=el('modelInputModal'),input=el('modelInputValue');if(input&&input.blur)input.blur();if(document.activeElement&&modal?.contains?.(document.activeElement))document.activeElement.blur?.();if(modal)modal.style.display='none';modelInputState={providerIndex:-1,kind:'chat'}}
function submitModelInput(){
  const input=el('modelInputValue'),name=String(input?.value||'').trim(),i=modelInputState.providerIndex;
  if(!name||i<0)return toast('请输入模型名称',false);
  const ps=[...providers()];
  ps[i]=Object.assign({},ps[i]||{});

  if(modelInputState.kind==='embedding'){
    const exists=new Set((ps[i].embedding_models||[]).map(x=>x.name));
    if(exists.has(name))return toast('该嵌入模型已存在',false);
    ps[i].embedding_models=[...(ps[i].embedding_models||[]),{name,enabled:true,dimensions:0}];
  } else if(modelInputState.kind==='planner'){
    const exists=new Set((ps[i].planner_models||[]).map(x=>x.name));
    if(exists.has(name))return toast('该回复判断模型已存在',false);
    ps[i].planner_models=[...(ps[i].planner_models||[]),{name,enabled:true,timeout_seconds:60}];
  } else {
    const exists=new Set((ps[i].models||[]).map(x=>x.name));
    if(exists.has(name))return toast('该对话模型已存在',false);
    ps[i].models=[...(ps[i].models||[]),{name,enabled:true,supports_multimodal:false,timeout_seconds:60,reasoning_effort:'',context_window:Number((ps[i].detected_windows||{})[name]||0)}];
  }

  setProviders(ps);
  closeModelInput();
  forceRender();
}
function renderRotationList(){const r=rotation(),enabled=enabledProviderModels();return `<div class="card"><div class="section-head"><div><h3>对话模型轮换列表</h3><p>按顺序轮换</p></div></div><div style="display:grid;gap:8px">${r.length?r.map((x,i)=>`<div class="rot-row field"><b>${esc(providerModelLabel(x.provider_id,x.model))}</b><div><button class="btn" ${i===0?'disabled':''} onclick="moveRotation(${i},-1)">上移</button> <button class="btn" ${i===r.length-1?'disabled':''} onclick="moveRotation(${i},1)">下移</button></div></div>`).join(''):'<div class="desc">勾选模型后会出现在这里。</div>'}</div><div class="desc">可用模型：${esc(enabled.map(x=>x.label).join('，')||'无')}</div></div>`}
function forceRender(){const d=state.dirty,t=state.lastInputAt,prev=state._forceRender;state.dirty=false;state.lastInputAt=0;state._forceRender=true;try{render()}finally{state._forceRender=prev;state.dirty=d;state.lastInputAt=t}}
function addProvider(){const ps=[...providers(),{id:'provider'+(providers().length+1),base_url:'',keys:[],http_proxy:'',models:[],detected_models:[]}];state.providerIndex=ps.length-1;setProviders(ps);forceRender()}
function removeProvider(i){if(!confirm('确定删除该提供商？'))return;const ps=[...providers()];ps.splice(i,1);state.providerIndex=0;setProviders(ps);forceRender()}
function updateProvider(i,k,v){const ps=[...providers()];if(!ps[i])return;providerIdentity(ps[i]);ps[i]=Object.assign({},ps[i]);ps[i][k]=v;setProviders(ps);refreshProviderDetectionUi()}
function syncProviderFieldsFromDom(){const ps=[...providers()];let changed=false;ps.forEach((p,i)=>{const id=el('provider_id_'+i),base=el('provider_base_'+i),keys=el('provider_keys_'+i),proxy=el('provider_proxy_'+i);if(id||base||keys||proxy){const next=Object.assign({},p);if(id)next.id=id.value.trim();if(base)next.base_url=base.value.trim();if(proxy)next.http_proxy=proxy.value.trim();if(keys)next.keys=keys.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);if(!sameDraftValues(p,next)){ps[i]=next;changed=true}}});if(changed)setProviders(ps)}
function updateProviderModel(i,mi,k,v){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].models=[...(ps[i].models||[])];ps[i].models[mi]=Object.assign({},ps[i].models[mi]||{});ps[i].models[mi][k]=v;setProviders(ps)}
function addDetectedModel(i){
  const tab = state.providerSubTab?.[i] || 'chat';
  const sel=el('detected_select_'+i),name=(sel?.value||'').trim();
  if(!name)return;

  const ps=[...providers()];
  ps[i]=Object.assign({},ps[i]||{});

  if (tab === 'embedding') {
    // 添加为嵌入模型
    const exists=new Set((ps[i].embedding_models||[]).map(x=>x.name));
    if(!exists.has(name)){
      ps[i].embedding_models=[...(ps[i].embedding_models||[]),{name,enabled:true,dimensions:0}];
    }
  } else if (tab === 'planner') {
    // 添加为回复判断模型
    const exists=new Set((ps[i].planner_models||[]).map(x=>x.name));
    if(!exists.has(name)){
      ps[i].planner_models=[...(ps[i].planner_models||[]),{name,enabled:true,timeout_seconds:60}];
    }
  } else {
    // 添加为对话模型（默认）
    const exists=new Set((ps[i].models||[]).map(x=>x.name));
    if(!exists.has(name)){
      const win=Number((ps[i].detected_windows||{})[name]||0);
      ps[i].models=[...(ps[i].models||[]),{name,enabled:true,supports_multimodal:false,timeout_seconds:60,reasoning_effort:'',context_window:win>0?win:0}];
    }
  }

  setProviders(ps);
  forceRender();
}
function clearDetectedModels(i){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].detected_models=[];ps[i].detect_error='';setProviders(ps);forceRender()}
function removeProviderModel(i,mi){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].models=[...(ps[i].models||[])];ps[i].models.splice(mi,1);setProviders(ps);forceRender()}
function moveRotation(i,d){const r=[...rotation()],j=i+d;if(j<0||j>=r.length)return;[r[i],r[j]]=[r[j],r[i]];setRotation(r);forceRender()}
function refreshProviderDetectionUi(){
  const i=state.providerIndex||0,p=providers()[i];if(state.current!=='providers'||!p)return;
  const busy=providerDetections.has(providerIdentity(p)),btn=el('providerDetectBtn_'+i),box=el('providerDetect_'+i);
  if(btn){btn.disabled=busy;btn.textContent=busy?'检测中…':'检测模型'}
  if(box)box.textContent=busy?'检测中…':p.detect_error||'';
  const results=el('providerDetected_'+i);if(results){rememberDetectedSelection(i);results.innerHTML=renderDetectedModels(p,i)}
}
async function detectProviderModels(i){
  syncProviderFieldsFromDom();const p=providers()[i];if(!p)return;
  const id=providerIdentity(p);if(providerDetections.has(id))return;
  const request={params:providerDetectParams(p)};providerDetections.set(id,request);refreshProviderDetectionUi();
  const currentIndex=()=>providers().findIndex(x=>providerIdentity(x)===id&&providerDetectParams(x)===request.params);
  try{
    const data=await api('/api/providers/detect-models',{method:'POST',body:request.params});
    const index=currentIndex();if(index<0||providerDetections.get(id)!==request)return;
    // 只合并检测字段，不拿 await 前的模型/提供商数组覆盖编辑或保存后的状态。
    const ps=[...providers()],latest=ps[index];
    ps[index]=Object.assign({},latest,{detected_models:[...new Set(data.models||[])],detected_windows:data.context_windows||{},detect_error:data.error||''});
    setProviders(ps);refreshProviderDetectionUi();
    toast(data.error?'检测失败：'+data.error:'检测到 '+ps[index].detected_models.length+' 个模型',!data.error);
  }catch(e){
    const index=currentIndex();if(index>=0&&providerDetections.get(id)===request){const ps=[...providers()];ps[index]=Object.assign({},ps[index],{detect_error:e.message});setProviders(ps);toast(e.message,false)}
  }finally{if(providerDetections.get(id)===request)providerDetections.delete(id);refreshProviderDetectionUi()}
}
let knowledgeDocs=[];
let knowledgeUploadState={active:false,name:'',phase:''};
async function loadKnowledge(){try{knowledgeDocs=await api('/api/knowledge/documents');forceRender()}catch(e){toast(e.message,false)}}
async function uploadKnowledgeFile(input){const file=input?.files?.[0];if(!file)return;if(knowledgeUploadState.active){toast('已有文档正在索引，请稍后再上传',false);return}if(file.size>16*1024*1024){toast('文件超过 16MB 限制',false);return}knowledgeUploadState={active:true,name:file.name,phase:'正在读取文件…'};forceRender();try{const text=await new Promise((ok,bad)=>{const r=new FileReader();r.onload=()=>ok(String(r.result||'').split(',',2)[1]||'');r.onerror=bad;r.readAsDataURL(file)});knowledgeUploadState.phase='正在上传并建立索引…';forceRender();let path='/api/knowledge/documents/upload';try{const caps=await api('/api/knowledge/capabilities');if(caps?.upload_path)path=caps.upload_path}catch(e){throw new Error('当前运行的 WebUI 后端版本不支持知识库上传，请重启机器人后再试')}const result=await api(path,{method:'POST',body:JSON.stringify({filename:file.name,content_b64:text})});const warning=String(result?.warning||'').trim();const status=String(result?.status||'');const message=status==='indexing'?'文档已接收，索引正在进行中，请稍后刷新':(warning?('文档已建立全文索引；'+warning):'文档已上传并完成索引');toast(message,true);await loadKnowledge()}catch(e){toast(e.message,false);await loadKnowledge()}finally{knowledgeUploadState={active:false,name:'',phase:''};input.value='';forceRender()}}
async function deleteKnowledgeDocument(id){if(!confirm('确定删除这份知识库文档？'))return;try{await api('/api/knowledge/documents/delete',{method:'POST',body:JSON.stringify({document_id:id})});toast('文档已删除');await loadKnowledge()}catch(e){toast(e.message,false)}}
function renderKnowledge(){const m=meta(),v=state.bundle.form_values||{},docs=Array.isArray(knowledgeDocs)?knowledgeDocs:[],uploading=knowledgeUploadState.active;return `<div class="card"><div class="section-head"><div><h3>知识库设置</h3><p>向量模式默认开启；关闭后自动使用 SQLite FTS 全文检索。</p></div></div><div class="form-grid">${(m.fields||[]).map(f=>renderField(f,v[f.path])).join('')}</div></div><div class="card"><div class="section-head"><div><h3>文档</h3><p>支持 TXT、Markdown、PDF（仅可提取文字的 PDF；扫描件不支持 OCR）。</p></div><label class="btn primary ${uploading?'disabled':''}">${uploading?'索引中…':'上传文档'}<input type="file" accept=".txt,.md,.markdown,.pdf" style="display:none" ${uploading?'disabled':''} onchange="uploadKnowledgeFile(this)"></label></div>${uploading?`<div class="desc" style="margin-bottom:10px">${esc(knowledgeUploadState.name)}：${esc(knowledgeUploadState.phase)} 请勿重复上传。</div>`:''}${docs.length?`<div style="display:grid;gap:8px">${docs.map(d=>{const modeText=d.index_mode==='vector'?'向量':'全文';const tail=d.error?`<span style="color:var(--bad)">${esc(d.error)}</span>`:(d.warning?`<span style="color:var(--warn,#e0a030)">${esc(d.warning)}</span>`:'');return `<div class="field" style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap"><div><b>${esc(d.name)}</b><div class="desc">${esc(d.status)} · ${esc(modeText)}检索 · ${esc(d.chunks||0)} 个片段${tail?' · ':''}${tail}</div></div><button class="btn" onclick="deleteKnowledgeDocument('${esc(d.id)}')">删除</button></div>`}).join('')}</div>`:'<div class="desc">暂无文档。上传后会自动建立向量索引；未配置嵌入模型或关闭向量模式时仍可使用 SQLite FTS 检索。</div>'}</div>`}
function personaEditId(){const v=state.bundle.form_values||{},presets=Array.isArray(v['Others.personality_presets'])?v['Others.personality_presets']:[];if(!presets.length)return '';if(state.personaEditId&&presets.some(p=>p.id===state.personaEditId))return state.personaEditId;return v['Others.active_personality_preset']||presets[0].id}
function renderPersona(){const v=state.bundle.form_values||{},presets=Array.isArray(v['Others.personality_presets'])?v['Others.personality_presets']:[],active=v['Others.active_personality_preset']||(presets[0]?.id||''),privateActive=v['Others.private_personality_preset']||'',groupActive=v['Others.group_personality_preset']||'',editId=personaEditId(),cur=presets.find(x=>x.id===editId)||presets[0]||{id:'default',name:'默认',prompt:v['Others.personality_prompt']||''};const opts=(sel,follow)=>`${follow?`<option value="" ${sel===''?'selected':''}>跟随默认</option>`:''}${presets.map(p=>`<option value="${esc(p.id)}" ${p.id===sel?'selected':''}>${esc(p.name||p.id)}</option>`).join('')}`;return `<div class="card"><div class="section-head"><div><h3>人格设定</h3><p>选择预设并编辑人设，私聊和群聊可分别指定</p></div><button class="btn primary" onclick="addPersonaPreset()">新增预设</button></div><div class="field" style="margin-bottom:14px"><div class="label"><span>使用人格</span></div><div class="persona-picker"><label>默认<select onchange="setPersonaActive('Others.active_personality_preset',this.value)">${opts(active,false)}</select></label><label>私聊<select onchange="setPersonaActive('Others.private_personality_preset',this.value)">${opts(privateActive,true)}</select></label><label>群聊<select onchange="setPersonaActive('Others.group_personality_preset',this.value)">${opts(groupActive,true)}</select></label></div><div class="desc">私聊 / 群聊选「跟随默认」时沿用默认人格；WebUI 聊天室使用私聊人格。</div></div><div class="split-pane"><div class="split-pane-list">${presets.map(p=>`<button class="btn ${p.id===cur.id?'primary':''}" style="text-align:left" data-pp-act="select" data-pp-id="${esc(p.id)}">${esc(p.name||p.id)}${p.id===active?' ·默认':''}</button>`).join('')}</div><div class="field"><div class="label"><span>编辑人设：${esc(cur.name||cur.id)}</span><span><button class="btn" data-pp-act="rename" data-pp-id="${esc(cur.id)}">重命名</button> <button class="btn" data-pp-act="remove" data-pp-id="${esc(cur.id)}">删除</button></span></div><textarea style="min-height:420px" data-pp-act="prompt" data-pp-id="${esc(cur.id)}">${esc(cur.prompt||'')}</textarea><div class="desc">可使用 {bot_name} 与 {user_name} 占位符</div></div></div></div>`}
function setPersonaActive(path,id){setValue(path,id);if(path==='Others.active_personality_preset'){const p=(state.bundle.form_values['Others.personality_presets']||[]).find(x=>x.id===id);if(p)setValue('Others.personality_prompt',p.prompt||'')}forceRender()}
// 只写预设列表本身，不动「默认人格」。改内容 / 重命名 / 切换编辑目标都不应该
// 顺手改变哪个预设在生效——那正是之前点左侧列表会偷偷换掉默认人格的原因。
function setPersonaPresets(presets){setValue('Others.personality_presets',presets);const active=state.bundle.form_values['Others.active_personality_preset'];const cur=presets.find(x=>x.id===active);if(cur)setValue('Others.personality_prompt',cur.prompt||'')}
// 新预设 ID 不能按“当前数量+1”生成：删掉中间某个预设后再新增，会复用
// 刚被删除的 id，等于把旧角色的生活直接转交给新的人设。这里用随机标识，
// 只保证在现有列表内唯一；已有合法 id 永远不改。
function personaNewId(presets){
  const used=new Set((presets||[]).map(p=>String(p&&p.id||'')));
  let seed='';
  try{seed=String((globalThis.crypto&&globalThis.crypto.randomUUID)?globalThis.crypto.randomUUID():'')}catch(e){seed=''}
  if(!seed)seed=Date.now().toString(36)+Math.random().toString(36).slice(2,10);
  let id='preset_'+seed.replace(/[^A-Za-z0-9]/g,'').slice(0,24);
  let n=2;while(used.has(id)){id='preset_'+seed.replace(/[^A-Za-z0-9]/g,'').slice(0,20)+'_'+n;n++}
  return id
}
function addPersonaPreset(){const presets=[...(state.bundle.form_values['Others.personality_presets']||[])],id=personaNewId(presets);presets.push({id,name:'预设 '+(presets.length+1),prompt:''});state.personaEditId=id;setPersonaPresets(presets);forceRender()}
function selectPersonaPreset(id){state.personaEditId=id;forceRender()}
function updatePersonaPrompt(id,prompt){const presets=[...(state.bundle.form_values['Others.personality_presets']||[])].map(p=>p.id===id?Object.assign({},p,{prompt}):p);setPersonaPresets(presets)}
function renamePersonaPreset(id){const presets=[...(state.bundle.form_values['Others.personality_presets']||[])];const p=presets.find(x=>x.id===id);if(!p)return;const name=prompt('预设名称',p.name||p.id);if(name==null)return;p.name=name.trim()||p.id;state.personaEditId=id;setPersonaPresets(presets);forceRender()}
function removePersonaPreset(id){const v=state.bundle.form_values||{};const presets=[...(v['Others.personality_presets']||[])];if(presets.length<=1){toast('至少保留一个预设',false);return}const next=presets.filter(x=>x.id!==id);state.personaEditId=next[0].id;
// 被删的预设如果正在被使用，把对应选择迁走：默认人格落到第一个，
// 私聊/群聊回到「跟随默认」，否则保存时会因为 id 不存在被静默重置。
if(v['Others.active_personality_preset']===id)setValue('Others.active_personality_preset',next[0].id);
if(v['Others.private_personality_preset']===id)setValue('Others.private_personality_preset','');
if(v['Others.group_personality_preset']===id)setValue('Others.group_personality_preset','');
setPersonaPresets(next);forceRender()}
function renderAiSettings(m){
  const values=state.bundle.form_values||{},fields=m.fields||[];
  // 路径优先于旧 category；未来新增的分段字段也必须留在同一用途组。
  const split=fields.filter(f=>f.path.startsWith('Others.llm_split.')||f.category==='分段回复');
  const memory=fields.filter(f=>!split.includes(f)&&f.category==='上下文与记忆');
  const conversation=fields.filter(f=>!split.includes(f)&&!memory.includes(f));
  const enabled=split.find(f=>f.path==='Others.llm_split.enabled')||{path:'Others.llm_split.enabled',label:'启用 LLM 分段回复',type:'bool',desc:'仅对大模型生成结果生效。'};
  const options=split.filter(f=>f!==enabled);
  const quote=!!state.bundle.feature_switches?.split_reply_quote;
  return `<div class="card ai-settings-card" id="section_ai"><div class="section-head"><div><h3>对话设置</h3><p>配置回复过滤与分段回复</p></div></div><div class="form-grid compact-form ai-conversation-fields">${conversation.map(f=>renderField(f,values[f.path],true)).join('')}<div class="field setting-row"><div class="label"><span>回复时引用</span><div class="switch ${quote?'on':''}" onclick="toggleFeature('split_reply_quote',this)"></div></div><div class="desc">普通 AI 回复自动引用触发消息（仅群聊生效），多段仅首段引用</div></div><div class="field ai-split-toggles" id="section_ai_split">${renderField(enabled,values[enabled.path],true,true)}</div>${options.map(f=>renderField(f,values[f.path],true)).join('')}</div></div><div class="card ai-settings-card" id="section_ai_memory"><div class="section-head"><div><h3>上下文与记忆</h3><p>按完整对话轮次管理上下文，压缩复用对话模型及其轮换</p></div></div><div class="form-grid compact-form ai-conversation-fields">${memory.map(f=>renderField(f,values[f.path],true)).join('')}</div></div>`;
}
function renderForm(m){if(m.key==='ai')return renderAiSettings(m);if(m.key==='proactive'||m.key==='webui')return renderOrganizedSettings(m);const v=state.bundle.form_values||{};const fields=m.fields||[];const byCat={},order=[];fields.forEach(f=>{const cat=f.category||'基础';if(!byCat[cat]){byCat[cat]=[];order.push(cat)}byCat[cat].push(f)});if(order.length<=1){return `<div class="card" id="section_${esc(m.key||'config')}"><div class="section-head"><div><h3>${esc(m.title)}</h3><p>${esc(m.desc)}</p></div></div><div class="form-grid">${fields.map(f=>renderField(f,v[f.path])).join('')}</div></div>`}return order.map((cat,i)=>{const sid=i===0?(m.key||'config'):('cfg_'+cat).replace(/[^a-zA-Z0-9_-]/g,'_');return `<div class="card" id="section_${esc(sid)}"><div class="section-head"><div><h3>${esc(i===0?m.title:cat)}</h3><p>${esc(i===0?m.desc:cat+'相关设置')}</p></div></div><div class="form-grid">${byCat[cat].map(f=>renderField(f,v[f.path])).join('')}</div></div>`}).join('')}
function settingsTabs(key){
  if(key==='proactive')return [{key:'group',title:'群聊主动回复',desc:'群内接话、冷场搭话与后续观察'}, {key:'private',title:'私聊主动回复',desc:'主动关怀、未回复限制与后续观察'}];
  return [{key:'appearance',title:'界面外观',desc:'主题、背景与液态玻璃'}, {key:'service',title:'访问与服务',desc:'监听地址、端口与访问认证'}, {key:'network',title:'更新与网络',desc:'更新仓库、镜像与通用网络代理'}];
}
function settingsTabFields(key,tab){
  const fields=(pages().find(p=>p.key===key)?.fields||[]);
  if(key==='proactive')return fields.filter(f=>f.path.startsWith(`ProactiveReply.${tab}.`));
  return fields.filter(f=>{
    const category=f.category||'';
    if(tab==='appearance')return category==='界面外观'||category==='液态玻璃UI'||/WebUI\.(ui_version|theme_preset|background_|liquid_glass|disable_background_blur|card_background_blur)/.test(f.path);
    if(tab==='service')return ['WebUI.host','WebUI.port','WebUI.access_token'].includes(f.path);
    return f.path.startsWith('Others.');
  });
}
function activeSettingsTab(key){
  const tabs=settingsTabs(key),tab=state.settingsTabs?.[key];
  return tabs.some(t=>t.key===tab)?tab:tabs[0].key;
}
function renderSettingsTab(key,tab){
  const fields=settingsTabFields(key,tab),values=state.bundle.form_values||{};
  if(key==='proactive'){
    const prefix=`ProactiveReply.${tab}.`,enabled=!!values[prefix+'enabled'];
    const obsAll=fields.filter(f=>f.path.includes('.observation.'));
    const obsMain=obsAll.filter(f=>/\.(enabled|trigger_probability)$/.test(f.path));
    const obsAdv=obsAll.filter(f=>!obsMain.includes(f));
    const groups=[
      {title:'启用与频率',desc:'开启当前场景后，可按需要调整冷场搭话时机。',fields:fields.filter(f=>!f.path.includes('.observation.')&&!/\.(whitelist|blacklist)$/.test(f.path))},
      {title:'允许与禁止对象',desc:'名单都留空则不限对象；黑名单优先。',fields:fields.filter(f=>/\.(whitelist|blacklist)$/.test(f.path))},
      {title:'发言后继续观察',desc:'发言后短时间留意对方是否继续聊，由规划器决定是否续聊。',fields:obsMain,extra:obsAdv},
    ];
    return `<div class="settings-status"><span class="pill ${enabled?'ok':''}">${tab==='group'?'群聊':'私聊'}主动回复 · ${enabled?'已开启':'未开启'}</span><span class="desc">规划器决定是否发言，对话模型生成正文。<button class="inline-link" onclick="gotoPage('providers')">配置模型</button></span></div>`+groups.map(g=>`<section class="settings-block"><div class="section-head"><div><h3>${g.title}</h3><p>${g.desc}</p></div></div><div class="form-grid">${g.fields.map(f=>renderField(f,values[f.path])).join('')}</div>${g.extra?.length?`<details class="proactive-obs-advanced"><summary>高级等待与合并秒数（通常无需调整）</summary><div class="form-grid">${g.extra.map(f=>renderField(f,values[f.path])).join('')}</div></details>`:''}</section>`).join('');
  }
  if(key==='webui'&&tab==='appearance'){
    const glassFields=fields.filter(f=>['WebUI.liquid_glass','WebUI.disable_background_blur','WebUI.liquid_glass_refraction','WebUI.card_background_blur'].includes(f.path));
    const wallpaperFields=fields.filter(f=>f.path!=='WebUI.ui_version'&&!glassFields.includes(f));
    const curVer=normalizeWebuiVersion(values['WebUI.ui_version']);
    const verBlock=`<section class="settings-block" id="section_webui_version"><div class="section-head"><div><h3>WebUI 界面版本</h3><p>切换经典卡片风与现代工作台界面，点击立即预览，点右上角「保存设置」持久化。</p></div><span class="pill ok">${curVer==='modern'?'当前：新版现代工作台':'当前：旧版经典卡片风'}</span></div><select id="f_WebUI_ui_version" style="display:none" onchange="switchWebuiVersion(this.value)"><option value="classic" ${curVer==='classic'?'selected':''}>classic</option><option value="modern" ${curVer==='modern'?'selected':''}>modern</option></select><div class="ui-version-grid"><button type="button" class="ui-version-card ${curVer==='classic'?'is-active':''}" data-ui-version="classic" aria-pressed="${curVer==='classic'}" onclick="switchWebuiVersion('classic')"><div class="ui-version-preview ui-preview-classic" aria-hidden="true"><div class="up-frame"><div class="up-side"><span class="up-brand"></span><span class="up-nav active"></span><span class="up-nav"></span></div><div class="up-main"><div class="up-cards"><span class="up-bubble"></span><span class="up-bubble"></span><span class="up-bubble wide"></span></div></div></div></div><div class="ui-version-info"><div class="ui-version-head"><strong>旧版 WebUI · 经典卡片风（默认）</strong><span class="pill ${curVer==='classic'?'ok':''}">${curVer==='classic'?'使用中':'点击切换'}</span></div><p class="desc">保留原版大圆角悬浮窗、极光渐变底色与液态玻璃折射效果。</p></div></button><button type="button" class="ui-version-card ${curVer==='modern'?'is-active':''}" data-ui-version="modern" aria-pressed="${curVer==='modern'}" onclick="switchWebuiVersion('modern')"><div class="ui-version-preview ui-preview-modern" aria-hidden="true"><div class="up-side"><span class="up-brand"></span><span class="up-nav active"></span><span class="up-nav"></span><span class="up-nav"></span></div><div class="up-main"><div class="up-top"><span class="up-title"></span><span class="up-btn"></span></div><div class="up-panel"><div class="up-row"><span></span><em></em></div><div class="up-row"><span></span><em></em></div><div class="up-row"><span></span><em></em></div></div></div></div><div class="ui-version-info"><div class="ui-version-head"><strong>新版 WebUI · 现代工作台</strong><span class="pill ${curVer==='modern'?'ok':''}">${curVer==='modern'?'使用中':'点击切换'}</span></div><p class="desc">全屏扁平工作台布局，横向分组表单行与清晰层级。</p></div></button></div></section>`;
    return verBlock+`<section class="settings-block"><div class="section-head"><div><h3>主题与背景</h3><p>管理主题色、背景图片与背景模糊。</p></div></div><div class="form-grid compact-form">${wallpaperFields.map(f=>renderField(f,values[f.path])).join('')}</div></section><section class="settings-block glass-settings-family" id="section_glass_settings"><div class="section-head"><div><h3>液态玻璃</h3><p>仅旧版经典卡片风生效，用于控制半透明毛玻璃折射效果。</p></div>${curVer==='modern'?'<span class="pill">仅旧版经典卡片风生效</span>':''}</div><div class="glass-settings-controls">${glassFields.map(f=>renderField(f,values[f.path],false,true)).join('')}</div></section>`;
  }
  return `<div class="form-grid compact-form">${fields.map(f=>renderField(f,values[f.path])).join('')}</div>`;
}
function renderOrganizedSettings(m){
  const key=m.key,tabs=settingsTabs(key),tab=activeSettingsTab(key);
  return `<div class="card organized-settings" id="section_${key}"><div class="section-head"><div><h3>${esc(m.title)}</h3><p>${esc(m.desc||'')}</p></div></div><div class="subtab-nav settings-tabs" id="settingsTabs_${key}" role="tablist" aria-label="${esc(m.title)}">${tabs.map(t=>`<button class="btn ${tab===t.key?'primary':''}" role="tab" aria-selected="${tab===t.key}" data-page="${key}" data-tab="${t.key}" onclick="switchSettingsTab(this.dataset.page,this.dataset.tab)">${t.title}</button>`).join('')}</div><div class="subtab-panel" id="settingsPanel_${key}">${renderSettingsTab(key,tab)}</div></div>`;
}
function switchSettingsTab(key,tab){
  if(!['proactive','webui'].includes(key)||!settingsTabs(key).some(t=>t.key===tab)||activeSettingsTab(key)===tab)return;
  syncCurrentPageFieldsFromDom();(state.settingsTabs||(state.settingsTabs={}))[key]=tab;
  const panel=el('settingsPanel_'+key);if(!panel)return;
  panel.innerHTML=renderSettingsTab(key,tab);
  el('settingsTabs_'+key)?.querySelectorAll('[data-tab]').forEach(b=>{b.classList.toggle('primary',b.dataset.tab===tab);b.setAttribute('aria-selected',String(b.dataset.tab===tab))});
  animateSubtab(panel);liquidGlassLensController.refresh();
}
function switchWebuiVersion(ver){
  const next=normalizeWebuiVersion(ver);
  const sel=el('f_WebUI_ui_version');if(sel)sel.value=next;
  setValue('WebUI.ui_version',next);
  forceRender();
  requestAnimationFrame(()=>{
    if(typeof updateNavIndicator==='function')updateNavIndicator();
    if(typeof liquidGlassLensController!=='undefined')liquidGlassLensController.refresh();
  });
}
function scrollToRouteSection(section){const safe=String(section||'').replace(/[^a-zA-Z0-9_-]/g,'_');const target=document.getElementById('section_'+safe)||document.querySelector(`[data-section="${CSS.escape(String(section||''))}"]`);if(target)target.scrollIntoView({block:'start',behavior:'smooth'})}
function enabledEmbeddingProviders(){return providers().filter(p=>p.id&&Array.isArray(p.embedding_models)&&p.embedding_models.some(m=>m.enabled!==false&&m.name))}
function renderEmbeddingProviderSelect(path,value,id){const items=enabledEmbeddingProviders();return `<select id="${id}" onchange="setValue('${path}',this.value);forceRender()"><option value="">请选择</option>${items.map(p=>`<option value="${esc(p.id)}" ${String(p.id)===String(value)?'selected':''}>${esc(p.id)}</option>`).join('')}</select>`}
function enabledEmbeddingModels(){const out=[];enabledEmbeddingProviders().forEach(p=>(p.embedding_models||[]).forEach(m=>{if(m.enabled!==false&&m.name)out.push({provider_id:p.id,model:m.name,label:providerModelLabel(p.id,m.name)})}));return out}
function renderEmbeddingModelRefSelect(path,value,id){const items=enabledEmbeddingModels();return `<select id="${id}" onchange="setValue('${path}',this.value)"><option value="">请选择向量模型</option>${items.map(m=>`<option value="${esc(m.label)}" ${String(m.label)===String(value)?'selected':''}>${esc(m.label)}</option>`).join('')}</select>`}
// 数值滑块：拖动时实时预览，右侧显示当前值。
// 用 input 事件而不是 change，拖到什么程度就看到什么效果。
function renderRangeField(f,v,id){
  const min=f.min??0,max=f.max??100,step=f.step??1;
  const raw=v===undefined||v===null||v===''?f.default:Number(v);
  const val=Number.isFinite(raw)?Math.max(min,Math.min(max,raw)):(f.default??min);
  return `<div class="range-wrap"><input id="${id}" type="range" min="${min}" max="${max}" step="${step}" value="${val}" oninput="setRangeValue('${f.path}',this)"><output class="range-val" for="${id}">${val}</output></div>`;
}
function setRangeValue(path,input){
  const value=Number(input.value);
  const out=input.parentElement?.querySelector('.range-val');
  if(out)out.textContent=String(value);
  setValue(path,value);
}
function renderField(f,v,compact=false,flat=false){const id='f_'+f.path.replace(/[^a-zA-Z0-9]/g,'_');let input='';if(f.type==='bool')input=`<div class="switch ${v?'on':''}" onclick="setBool('${f.path}',this)"></div>`;else if(f.type==='select')input=`<select id="${id}" onchange="setValue('${f.path}',this.value)">${(f.options||[]).map(o=>`<option value="${esc(o)}" ${String(o)===String(v)?'selected':''}>${esc(o)}</option>`).join('')}</select>`;else if(f.type==='multimodal_model')input=renderMultimodalModelSelect(f.path,v,id);else if(f.type==='embedding_model_ref')input=renderEmbeddingModelRefSelect(f.path,v,id);else if(f.type==='background_image')input=renderBackgroundImageField(f.path,v,id);else if(f.type==='list')input=`<textarea ${compact?'style="min-height:84px"':''} id="${id}" oninput="setValue('${f.path}',this.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))">${esc(Array.isArray(v)?v.join('\n'):(v??''))}</textarea>`;else if(f.type==='textarea')input=`<textarea ${compact?'style="min-height:84px"':'style="min-height:280px"'} id="${id}" oninput="setValue('${f.path}',this.value)">${esc(v??'')}</textarea>`;else if(f.type==='endpoints')input=renderEndpointsEditor(v||[]);else if(f.type==='range')input=renderRangeField(f,v,id);else{input=`<input id="${id}" type="${f.type==='password'?'password':f.type==='number'?'number':'text'}" step="any" ${f.min!=null?`min="${f.min}"`:''} ${f.max!=null?`max="${f.max}"`:''} value="${esc(v??'')}" oninput="setValue('${f.path}',${f.type==='number'?'num(this.value)':'this.value'})">`;if(f.type==='password')input=`<div class="pwd-wrap">${input}<button class="pwd-eye" type="button" title="显示" aria-label="显示或隐藏内容" onclick="togglePwdField('${id}',this)">${SVG_EYE}</button></div>`}return `<div class="${flat?'setting-row':'field'}"><div class="label"><span>${esc(f.label)}</span>${f.type==='bool'?input:''}</div>${f.type==='bool'?'':input}<div class="desc">${esc(f.desc||f.path)}</div></div>`}
// 密码类字段的显示/隐藏。图标用 SVG 而不是 emoji，并在 CSS 里关掉 Edge 自带的
// ::-ms-reveal，否则输入框右侧会同时出现浏览器的眼睛和我们的眼睛。
const SVG_EYE='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>';
const SVG_EYE_OFF='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 6.2A9.9 9.9 0 0 1 12 6c6.4 0 10 7 10 7a17 17 0 0 1-2.4 3.2M6.5 7.6A17 17 0 0 0 2 13s3.6 7 10 7a9.7 9.7 0 0 0 4.2-.9"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M3 3l18 18"/></svg>';
function togglePwdField(id,btn){const i=el(id);if(!i)return;const show=i.type==='password';i.type=show?'text':'password';if(btn){btn.innerHTML=show?SVG_EYE_OFF:SVG_EYE;btn.title=show?'隐藏':'显示'}}
const num=v=>{const n=Number(v);return Number.isFinite(n)?n:v};
function setValue(path,value){state.bundle.form_values[path]=value;if(path==='manage_users'){state.bundle.manage_users=Array.isArray(value)?value:[];state.bundle.super_users=Array.isArray(value)?value:[]}if(path==='black_list'){state.bundle.blacklist_file=Array.isArray(value)?value:[]}if(path.startsWith('WebUI.'))applyWebuiAppearance();state.dirty=true;state.lastInputAt=Date.now();const save=el('saveState');if(save)save.textContent='有未保存更改'}
function syncCurrentPageFieldsFromDom(){if(!state.bundle)return;if(state.current==='providers')syncProviderFieldsFromDom();const page=meta();const values=state.bundle.form_values||{};(page.fields||[]).forEach(f=>{const id='f_'+f.path.replace(/[^a-zA-Z0-9]/g,'_');if(f.type==='bool'){return}
    // background_image 没有可同步的文本 input——renderBackgroundImageField 只画按钮和
    // 隐藏 file input，这里的同步逻辑把它当普通字段处理的话，会把 form_values 里的
    // 路径改写成空串，下帧 applyWebuiAppearance 就把 has-bg 摘了，背景整片变白。
    if(f.type==='background_image'){return}
    if(f.type==='endpoints'){const arr=Array.isArray(values[f.path])?[...values[f.path]]:[];values[f.path]=arr.map((ep,i)=>{const base=document.getElementById('ep_base_'+i),model=document.getElementById('ep_model_'+i),keys=document.getElementById('ep_keys_'+i),mm=document.getElementById('ep_mm_'+i);return {base_url:base?base.value:(ep.base_url||''),model:model?model.value:(ep.model||''),supports_multimodal:mm?!!mm.checked:!!ep.supports_multimodal,keys:(keys?keys.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean):(Array.isArray(ep.keys)?ep.keys:[]))}});return}const el=document.getElementById(id);if(!el)return;if(f.type==='list')values[f.path]=el.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);else if(f.type==='textarea'||f.type==='text'||f.type==='password'||f.type==='select'||f.type==='multimodal_model'||f.type==='embedding_model_ref')values[f.path]=el.value;else if(f.type==='number')values[f.path]=num(el.value)});state.bundle.form_values=values;const manageUsers=values.manage_users;if(Array.isArray(manageUsers)){state.bundle.manage_users=manageUsers;state.bundle.super_users=manageUsers}const blackList=values.black_list;if(Array.isArray(blackList)){state.bundle.blacklist_file=blackList}}
function setJsonValue(path,txt){try{setValue(path,JSON.parse(txt||'null'))}catch(e){state.dirty=true;const save=el('saveState');if(save)save.textContent='JSON 暂未通过校验'}}
function setBool(path,el){const v=!el.classList.contains('on');el.classList.toggle('on',v);setValue(path,v)}
function shouldAutoRefreshPage(){return state.current==='welcome'||state.current==='logs'||state.current==='stats'||state.current==='debug'||(state.current==='trace'&&!traceState.openId)}
function renderFeatures(){
  const map=state.bundle.feature_switches||{},groups={};
  // 仅过滤展示，保留插件运行时使用的旧开关及其配置。
  const hidden=new Set(['check_account','weather','image_generation','ai_chat','compression_commands','quote','plugin_admin_commands','split_reply_quote']);
  const visibleMeta=(state.bundle.feature_meta||[]).filter(x=>!hidden.has(x.key));
  const fields=(state.bundle.ui_schema.find(x=>x.key==='features')||{}).fields||[];
  const fieldMap=Object.fromEntries(fields.map(f=>[f.path,f]));
  const featureGroups=[
    {title:'对话与触发',keys:['group_chat','private_chat','group_chat_context','sensitive_filter','weak_blacklist']},
    {title:'群聊互动',keys:['poke_reply','group_join_welcome','emoji_plus_one','summary']},
    {title:'插件与接口',keys:['plugins_external','http_send_api']},
  ];
  const assigned=new Set();featureGroups.forEach(g=>{const items=g.keys.map(key=>visibleMeta.find(x=>x.key===key)).filter(Boolean);if(items.length){groups[g.title]=items;items.forEach(x=>assigned.add(x.key))}});
  visibleMeta.filter(x=>!assigned.has(x.key)).forEach(x=>(groups[x.group]||(groups[x.group]=[])).push(x));
  const groupDescriptions={'对话与触发':'管理群聊、私聊总开关与消息过滤规则','群聊互动':'拍一拍、入群欢迎、表情复读与群聊总结','插件与接口':'外部插件加载与 HTTP 消息发送接口'};
  const gateDescriptions={group_chat:'群聊总开关。关闭后，命令、插件、模型与主动回复将全部停止，需通过 WebUI 重新开启。',private_chat:'私聊总开关。关闭后，命令、插件、模型与主动回复将全部停止，需通过 WebUI 重新开启。'};
  const related=it=>(featureFieldMap[it.key]||[]).map(p=>fieldMap[p]).filter(Boolean);
  const isHeavyFeature=rel=>rel.length>1||rel.some(f=>['textarea','list','json'].includes(f.type));
  const option=it=>{
    const rel=related(it);
    const heavy=isHeavyFeature(rel);
    return `<div class="feature feature-option${rel.length?' feature-option-config':''}${rel.length>1?' feature-option-wide':''}${heavy?' feature-option-heavy':''}" data-feature="${esc(it.key)}"><div class="feature-option-head"><h4>${esc(it.title)}</h4><div class="switch ${map[it.key]?'on':''}" onclick="toggleFeature('${it.key}',this)"></div></div><p>${esc(gateDescriptions[it.key]||it.desc)}</p>${rel.length?`<div class="feature-option-fields">${rel.map(f=>renderField(f,state.bundle.form_values[f.path],true,true)).join('')}</div>`:''}</div>`;
  };
  const switchCards=Object.keys(groups).map(g=>{
    const sid=g.replace(/[^a-zA-Z0-9_-]/g,'_');
    const compactCount=groups[g].filter(it=>!isHeavyFeature(related(it))).length;
    return `<div class="card" id="section_${sid}" data-section="${esc(g)}"><div class="section-head"><div><h3>${esc(g)}</h3><p>${esc(groupDescriptions[g]||groups[g].map(it=>it.desc).join('；'))}</p></div><span class="pill">${groups[g].length} 项</span></div><div class="feature-options-grid" data-compact-count="${compactCount}">${groups[g].map(option).join('')}</div></div>`;
  }).join('');
  // 功能配置页原先只画 feature 开关，fields 里独立配置（如系统设置）不会显示。
  // 把没挂到某个开关下的 fields 按 category 单独成卡片，挂在底部。
  const linked=new Set(Object.values(featureFieldMap||{}).flat());
  const extraFields=fields.filter(f=>!linked.has(f.path));
  const byCat={};
  extraFields.forEach(f=>{const cat=f.category||'其他';(byCat[cat]||(byCat[cat]=[])).push(f)});
  const extraCards=Object.keys(byCat).map(cat=>{
    const sid=('extra_'+cat).replace(/[^a-zA-Z0-9_-]/g,'_');
    return `<div class="card" id="section_${sid}" data-section="${esc(cat)}"><div class="section-head"><div><h3>${esc(cat)}</h3><p>功能相关设置</p></div><span class="pill">${byCat[cat].length} 项</span></div><div class="form-grid">${byCat[cat].map(f=>renderField(f,state.bundle.form_values[f.path])).join('')}</div></div>`;
  }).join('');
  return switchCards+extraCards
}
function toggleFeature(key,sw){state.bundle.feature_switches[key]=!state.bundle.feature_switches[key];sw.classList.toggle('on',state.bundle.feature_switches[key]);state.dirty=true;state.lastInputAt=Date.now();const save=el('saveState');if(save)save.textContent='有未保存更改'}
async function checkUpdate(){try{const data=await api('/api/update/check',{method:'POST',body:'{}'});if(state.bundle?.status)state.bundle.status.update=data;render();toast(data.message||'已检查更新',true);await refreshAll(true)}catch(e){toast(e.message,false)}}
function scheduleReloadAfterUpdate(){if(state.reloadTimer)return;state.expectedReloadAfterUpdate=true;state.reloadTimer=setInterval(async()=>{try{const r=await fetch('/api/ui-state',{headers:{'X-WebUI-Token':token()},cache:'no-store'});if(!r.ok)return;clearInterval(state.reloadTimer);state.reloadTimer=null;location.reload()}catch(e){}} ,1500)}
async function installUpdate(){if(!confirm('将自动下载并安装最新版本，随后迁移配置并重启，是否继续？'))return;try{const data=await api('/api/update/install',{method:'POST',body:'{}'});if(state.bundle?.status){state.bundle.status.update=data.update||state.bundle.status.update;state.bundle.status.update_install=data.install||state.bundle.status.update_install}state.expectedReloadAfterUpdate=true;render();toast('已开始安装更新，请稍后手动刷新页面',true)}catch(e){toast(e.message,false)}}
function renderLogs(){return `<div class="card"><div class="section-head"><div><h3>实时日志</h3><p>查看实时日志</p></div><div style="display:flex;gap:8px;flex-wrap:wrap">${renderAutoScrollToggle()}<a class="btn" href="/api/raw-log${token()?('?token='+encodeURIComponent(token())):''}" target="_blank">打开完整日志</a></div></div>${renderLogPre(1000,'full')}</div>`}
function ansiToHtml(s){
  const xe=t=>t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const C=['#4e4e4e','#cc5555','#55aa55','#aaaa55','#5599ee','#aa55aa','#55aaaa','#aaaaaa',
           '#777777','#ff6666','#66dd66','#ffff66','#66aaff','#ff77ff','#55ffff','#eeeeee'];
  function a256(n){
    if(n<16)return C[n];
    if(n<232){n-=16;const b=n%6,g=Math.floor(n/6)%6,r=Math.floor(n/36),v=x=>x?55+x*40:0;return `rgb(${v(r)},${v(g)},${v(b)})`}
    const k=8+(n-232)*10;return `rgb(${k},${k},${k})`;
  }
  const parts=s.split(/\x1b\[([0-9;]*)m/);
  let out='',col=null,bold=false,open=false;
  const closeSpan=()=>{if(open){out+='</span>';open=false}};
  const openSpan=()=>{if(col||bold){out+=`<span style="${col?'color:'+col+';':''}${bold?'font-weight:bold':''}">`;open=true}};
  for(let i=0;i<parts.length;i++){
    if(i%2===0){if(parts[i]){closeSpan();openSpan();out+=xe(parts[i])}}
    else{
      const codes=(parts[i]||'0').split(';').map(Number);
      for(let j=0;j<codes.length;j++){
        const n=codes[j];
        if(n===0){col=null;bold=false}
        else if(n===1)bold=true;
        else if(n>=30&&n<=37)col=C[n-30];
        else if(n>=90&&n<=97)col=C[n-90+8];
        else if(n===38&&codes[j+1]===5&&codes.length>j+2){col=a256(codes[j+2]);j+=2}
        else if(n===38&&codes[j+1]===2&&codes.length>j+4){col=`rgb(${codes[j+2]},${codes[j+3]},${codes[j+4]})`;j+=4}
      }
    }
  }
  closeSpan();return out;
}
function renderLogPre(limit,name='main'){const logs=(state.bundle.logs||[]).slice(-limit);const html=logs.map(x=>{const msg=_strip_ansi_js(x.message).replace(/\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\]\s*/g,'');if(!msg.trim())return null;const line=`[${x.time}] [${x.stream}] ${msg}`;const isErr=/error|crit|\*crit|\*err|exception|traceback|❌/i.test(msg);const isWarn=/warn|\*warn|⚠️/i.test(msg);const style=isErr?'background:rgba(220,50,50,.35);border-radius:3px':isWarn?'background:rgba(200,150,0,.35);border-radius:3px':'';return style?`[${esc(x.time)}] [${esc(x.stream)}] <span style="${style}">${esc(msg)}</span>`:esc(line)}).filter(x=>x!==null).join('\n')||'暂无日志';return `<pre id="log_${name}" class="log ${name==='recent'?'compact':''}">${html}</pre>`}
function _strip_ansi_js(s){return s.replace(/\x1b\[[0-9;]*[mGKHF]/g,'').replace(/\x1b[@-Z\\-_]/g,'')}
function renderAutoScrollToggle(){const on=localStorage.webuiAutoScroll!=='false';return `<button class="btn" type="button" data-asbtn onclick="toggleAutoScroll()">${on?'✅':'⬜'} 自动滚动</button>`}
function snapLogsToBottom(){window._logAutoScrollAt=Date.now();document.querySelectorAll('pre.log').forEach(x=>{x.scrollTop=x.scrollHeight;x.scrollTo?.(0,x.scrollHeight)})}
function setAutoScrollState(on,snap=false){const flag=!!on;localStorage.webuiAutoScroll=String(flag);document.querySelectorAll('[data-asbtn]').forEach(b=>b.innerHTML=(flag?'✅':'⬜')+' 自动滚动');if(flag&&snap){snapLogsToBottom();requestAnimationFrame(snapLogsToBottom)}}
function toggleAutoScroll(){const on=!(localStorage.webuiAutoScroll!=='false');setAutoScrollState(on,on)}
function captureLogScrollState(){const state={};document.querySelectorAll('pre.log').forEach(x=>{const remain=x.scrollHeight-x.scrollTop-x.clientHeight;state[x.id||'log']={top:x.scrollTop,height:x.scrollHeight,client:x.clientHeight,atBottom:remain<72}});return state}
function restoreLogScrollState(prev={}){const auto=localStorage.webuiAutoScroll!=='false';if(auto){snapLogsToBottom();return}document.querySelectorAll('pre.log').forEach(x=>{const old=prev[x.id||'log'];if(old){x.scrollTop=old.top;x.scrollTo?.(0,old.top)}})}
function scheduleLogScrollAfterRender(prev){requestAnimationFrame(()=>{restoreLogScrollState(prev);setTimeout(()=>restoreLogScrollState(prev),80)})}
function scheduleLogAutoScroll(){scheduleLogScrollAfterRender(captureLogScrollState())}
function scrollLogsToBottom(){if(localStorage.webuiAutoScroll==='false')return;snapLogsToBottom()}
function scrollRecentLogsToBottom(){scrollLogsToBottom()}
function getMultimodalModels(){const seen=new Set(),models=[];enabledProviderModels().forEach(x=>{if(x.supports_multimodal&&x.label&&!seen.has(x.label)){seen.add(x.label);models.push(x.label)}});return models}
// 选中的背景图先存在这里，点「保存设置」时才真正上传。
// 不能存进 form_values：那份状态会被 localStorage 草稿序列化，
// 十几 MB 的 base64 会把 localStorage 撑爆。
let pendingBackground=null;
function renderBackgroundImageField(path,v,id){
  const state_=pendingBackground?`已选择「${esc(pendingBackground.filename)}」，保存后生效`
    :v?`当前：${esc(String(v).split('/').pop())}`:'未设置，使用主题背景';
  return `<div class="bg-image-control" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <input type="file" id="${id}_file" accept="image/*" style="display:none" onchange="pickBackgroundImage(this)">
    <button class="btn" onclick="el('${id}_file').click()">选择图片</button>
    ${(v||pendingBackground)?`<button class="btn" onclick="clearBackgroundImage('${path}')">清除</button>`:''}
    <span class="desc" style="margin:0">${state_}</span>
  </div>`
}
async function pickBackgroundImage(input){
  const file=input.files&&input.files[0];input.value='';
  if(!file)return;
  if(file.size>12*1024*1024){toast('图片过大（上限 12MB）',false);return}
  try{
    const b64=await new Promise((res,rej)=>{
      const r=new FileReader();
      r.onload=()=>res(String(r.result||'').split(',')[1]||'');
      r.onerror=()=>rej(r.error||new Error('读取失败'));
      r.readAsDataURL(file)
    });
    pendingBackground={filename:file.name,image_b64:b64};
    state.dirty=true;state.lastInputAt=Date.now();
    const save=el('saveState');if(save)save.textContent='有未保存更改';
    forceRender()
  }catch(e){toast(e.message,false)}
}
function clearBackgroundImage(path){pendingBackground=null;setValue(path,'');forceRender()}
async function flushPendingBackground(){
  if(!pendingBackground)return;
  const payload=pendingBackground;
  // 先清引用：上传接口自己会把路径写进 config，失败时也不该反复重传
  pendingBackground=null;
  const d=await api('/api/webui/background/upload',{method:'POST',body:JSON.stringify(payload)});
  if(state.bundle?.form_values)state.bundle.form_values['WebUI.background_image']=d?.path||'';
}
function renderMultimodalModelSelect(path,v,id){const models=getMultimodalModels();const value=String(v??'');const options=['',...models];if(value&&!models.includes(value))options.push(value);return `<select id="${id}" onchange="setValue('${path}',this.value)">${options.map(o=>`<option value="${esc(o)}" ${String(o)===value?'selected':''}>${esc(o||'不启用识图转述')}</option>`).join('')}</select>${models.length?'':`<div class="desc" style="margin-top:8px">先在「提供商」页勾选模型的「识图」选项并保存，即可在此选择。</div>`}`}
function renderEndpointsEditor(list){const rows=(Array.isArray(list)?list:[]).map((ep,i)=>`<div class="field" style="padding:12px"><div class="label"><span>接口 #${i+1}</span><button class="btn" type="button" onclick="removeEndpoint(${i})">删除</button></div><input id="ep_base_${i}" placeholder="base_url" value="${esc(ep.base_url||'')}" oninput="updateEndpoint(${i},'base_url',this.value)"><div style="height:8px"></div><input id="ep_model_${i}" placeholder="model" value="${esc(ep.model||'')}" oninput="updateEndpoint(${i},'model',this.value)"><div style="height:8px"></div><label style="display:flex;align-items:center;gap:10px;margin:6px 0 10px 0"><input id="ep_mm_${i}" type="checkbox" ${ep.supports_multimodal?'checked':''} onchange="updateEndpoint(${i},'supports_multimodal',this.checked)"><span>支持识图</span></label><textarea id="ep_keys_${i}" style="min-height:84px" placeholder="keys，一行一个" oninput="updateEndpoint(${i},'keys',this.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))">${esc(Array.isArray(ep.keys)?ep.keys.join('\n'):'')}</textarea></div>`).join('');return `<div style="display:grid;gap:10px">${rows||'<div class="desc">暂无接口，点击下方按钮新增</div>'}<button class="btn" type="button" onclick="addEndpoint()">新增大模型接口</button></div>`}
function updateEndpoint(index,key,value){const arr=Array.isArray(state.bundle.form_values['Others.llm_endpoints'])?[...state.bundle.form_values['Others.llm_endpoints']]:[];arr[index]=Object.assign({base_url:'',model:'',keys:[],supports_multimodal:false},arr[index]||{});arr[index][key]=value;setValue('Others.llm_endpoints',arr)}
function addEndpoint(){const arr=Array.isArray(state.bundle.form_values['Others.llm_endpoints'])?[...state.bundle.form_values['Others.llm_endpoints']]:[];arr.push({base_url:'',model:'',keys:[],supports_multimodal:false});setValue('Others.llm_endpoints',arr);forceRender()}
function removeEndpoint(index){const arr=Array.isArray(state.bundle.form_values['Others.llm_endpoints'])?[...state.bundle.form_values['Others.llm_endpoints']]:[];arr.splice(index,1);setValue('Others.llm_endpoints',arr);forceRender()}
async function saveAll(silent=false){
  if(state.current==='role-life'&&typeof roleLifeSaveConfig==='function'){
    if(!roleLifeHasDraft()){if(!silent)toast('设置已同步',true);return}
    await roleLifeSaveConfig();
    return;
  }
  if(!state.bundle||state.saving)return;
  state.saving=true;
  const save=el('saveState');if(save)save.textContent='正在保存...';
  try{
    syncCurrentPageFieldsFromDom();saveDraft();await flushPendingBackground();
    const manageUsers=state.bundle.form_values?.manage_users??state.bundle.manage_users;
    const blackList=state.bundle.form_values?.black_list??state.bundle.blacklist_file;
    const submitted=JSON.parse(JSON.stringify({form_values:state.bundle.form_values||{},feature_switches:state.bundle.feature_switches||{},agent_tools:state.bundle.agent_tools||{}}));
    const saved=await api('/api/ui-state',{method:'POST',body:JSON.stringify({...submitted,super_users:manageUsers,manage_users:manageUsers,blacklist_file:blackList})});
    const latest=state.bundle.form_values||{};
    const pending=Object.keys(latest).filter(k=>!sameDraftValues(latest[k],submitted.form_values[k]));
    let hasPending=pending.length>0;
    adoptProviderIdentities(providers(),saved.form_values?.['Others.llm_providers']||[]);
    saved.form_values=saved.form_values||{};
    for(const k of pending)saved.form_values[k]=latest[k];
    // 三类设置都可能在请求期间继续编辑，不能只保护普通表单字段。
    for(const section of ['feature_switches','agent_tools']){
      const current=state.bundle[section]||{},before=submitted[section],target=saved[section]||(saved[section]={});
      for(const key of new Set([...Object.keys(current),...Object.keys(before)])){
        if(sameDraftValues(current[key],before[key]))continue;
        hasPending=true;
        if(Object.prototype.hasOwnProperty.call(current,key))target[key]=current[key];else delete target[key];
      }
    }
    clearDraft();state.bundle=saved;seedPluginConfigValues();await traceSavePending();if(typeof saveMcpPending==='function')await saveMcpPending();
    state.dirty=hasPending||(typeof mcpState!=='undefined'&&!!mcpState.dirty);state.lastInputAt=state.dirty?Date.now():0;if(state.dirty)saveDraft();forceRender();
    if(!silent)toast(state.dirty?'已保存，仍有未保存修改':'已同步',true);
  }catch(e){toast(e.message,false)}finally{state.saving=false}
}
function isEditingField(){const el=document.activeElement;return !!(el&&['INPUT','TEXTAREA','SELECT'].includes(el.tagName))}
function shouldPauseAutoRefresh(){if(shouldAutoRefreshPage())return !!isEditingField();return !!(state.dirty||isEditingField()||(Date.now()-(state.lastInputAt||0)<15000))}
async function refreshAll(force=false){if(!force&&!shouldAutoRefreshPage())return;if(shouldPauseAutoRefresh()&&!force)return;try{const uiPath=state.current==='logs'?'/api/ui-state?log_limit=1000':'/api/ui-state';const data=await api(uiPath);state.apiFailCount=0;if(shouldPauseAutoRefresh()&&!force)return;applyDraft(data);adoptProviderIdentities(providers(),data.form_values?.['Others.llm_providers']||[]);state.bundle=data;seedPluginConfigValues();const installState=data?.status?.update_install?.state||'';if(['restarting','idle'].includes(installState)&&state.expectedReloadAfterUpdate){scheduleReloadAfterUpdate()}if(state.current==='chatroom'&&el('chatMsgs')){applyWebuiAppearance();renderConnectionStatus()}else render();const save=el('saveState');if(save&&!state.dirty)save.textContent='已同步'}catch(e){state.apiFailCount=(state.apiFailCount||0)+1;if(state.expectedReloadAfterUpdate&&state.apiFailCount>=2)scheduleReloadAfterUpdate();if(!state.expectedReloadAfterUpdate)toast(e.message,false)}}
let chatState={sessions:[],current:null,session:null,models:[],personas:[],loading:false,stream:localStorage.webuiChatStream!=='0',requests:Object.create(null),errors:Object.create(null),attachments:Object.create(null),drafts:Object.create(null),reasonOpen:Object.create(null),activityOpen:Object.create(null),toolOpen:Object.create(null)};
const CHAT_ICONS={
  spark:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/><path d="M19 3v4"/><path d="M21 5h-4"/></svg>',
  search:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  file:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/><path d="M8 13h8"/><path d="M8 17h5"/></svg>',
  terminal:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>',
  globe:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8"/><path d="M3.6 15h16.8"/><path d="M12 3a14 14 0 0 0 0 18"/><path d="M12 3a14 14 0 0 1 0 18"/></svg>',
  plug:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/></svg>',
  clock:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>',
  calc:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="2" width="16" height="20" rx="2"/><line x1="8" x2="16" y1="6" y2="6"/><line x1="8" x2="8" y1="14" y2="14"/><line x1="12" x2="12" y1="14" y2="14"/><line x1="16" x2="16" y1="14" y2="14"/><line x1="8" x2="8" y1="18" y2="18"/><line x1="12" x2="16" y1="18" y2="18"/></svg>',
  users:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  wrench:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76Z"/></svg>',
  image:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>',
  paperclip:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
  alert:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>',
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  x:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" x2="6" y1="6" y2="18"/><line x1="6" x2="18" y1="6" y2="18"/></svg>',
  spinner:'<svg class="chat-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>',
  chevron:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg>',
  copy:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
};
const _turnCopyCache=Object.create(null);
function chatBotDisplayName(){try{if(typeof state!=='undefined'&&state?.bundle?.status?.bot_name)return state.bundle.status.bot_name}catch(e){}return 'XcBot'}
function chatBotHeaderHtml(isError){
  return isError
    ?`<div class="turn-head is-error"><span class="turn-err-ic" aria-hidden="true">${CHAT_ICONS.alert}</span><span class="turn-name">生成失败</span></div>`
    :`<div class="turn-head"><span class="turn-name">${esc(chatBotDisplayName())}</span></div>`;
}
function chatFmtDuration(ms){
  const n=Math.max(0,Math.round(Number(ms||0)));
  if(!n)return '';
  if(n<1000)return n+'ms';
  if(n<60000)return (n/1000).toFixed(n<10000?1:0)+'s';
  const m=Math.floor(n/60000),s=Math.floor((n%60000)/1000);
  return m+'m'+String(s).padStart(2,'0')+'s';
}
function chatFmtDurationCN(ms){
  const n=Math.max(0,Math.round(Number(ms||0)));
  if(n<1000)return '';
  if(n<60000)return (n/1000).toFixed(1)+' 秒';
  const m=Math.floor(n/60000),s=Math.floor((n%60000)/1000);
  return s?`${m} 分 ${s} 秒`:`${m} 分`;
}
function chatFmtTime(ts){
  if(!ts)return '';
  const d=new Date(Number(ts)*1000);if(Number.isNaN(d.getTime()))return '';
  const now=new Date(),p=n=>String(n).padStart(2,'0');
  const hhmm=`${p(d.getHours())}:${p(d.getMinutes())}`;
  if(d.getFullYear()===now.getFullYear()&&d.getMonth()===now.getMonth()&&d.getDate()===now.getDate())return hhmm;
  return `${d.getMonth()+1}月${d.getDate()}日 ${hhmm}`;
}
function chatSafeJsonParse(raw){try{return JSON.parse(String(raw||''))}catch(e){return null}}
function chatClipSummary(text,max=48){const s=String(text||'').replace(/\s+/g,' ').trim();return s.length>max?s.slice(0,max-1)+'…':s}
function chatToolMeta(name,argsRaw){
  const n=String(name||''),a=chatSafeJsonParse(argsRaw)||{};
  if(n==='web_search')return {icon:'globe',verb:'联网搜索',detail:chatClipSummary(a.query||a.q||''),group:'联网搜索'};
  if(n==='fetch_webpage')return {icon:'globe',verb:'读取网页',detail:chatClipSummary(a.url||''),group:'读取网页'};
  if(n==='grep_files')return {icon:'search',verb:'搜索代码',detail:chatClipSummary(a.pattern||a.query||''),group:'搜索'};
  if(n==='file_list')return {icon:'search',verb:'列目录',detail:chatClipSummary(a.path||a.dir||'.'),group:'搜索'};
  if(n==='file_read')return {icon:'file',verb:'读取',detail:chatClipSummary(a.path||a.file||''),group:'读取'};
  if(n==='read_image')return {icon:'image',verb:'查看图片',detail:chatClipSummary(a.path||''),group:'查看图片'};
  if(n==='file_write')return {icon:'file',verb:'写入文件',detail:chatClipSummary(a.path||''),group:'写入文件'};
  if(n==='file_edit')return {icon:'file',verb:'编辑文件',detail:chatClipSummary(a.path||''),group:'编辑文件'};
  if(n==='execute_python')return {icon:'terminal',verb:'运行 Python',detail:chatClipSummary(a.code||''),group:'运行代码'};
  if(n==='execute_shell')return {icon:'terminal',verb:'执行命令',detail:chatClipSummary(a.command||a.cmd||''),group:'执行命令'};
  if(n==='calculate')return {icon:'calc',verb:'计算',detail:chatClipSummary(a.expression||a.expr||''),group:'计算'};
  if(n==='get_current_time')return {icon:'clock',verb:'查询时间',detail:'',group:'查询时间'};
  if(n==='call_plugin')return {icon:'plug',verb:'调用插件',detail:chatClipSummary(a.name||a.plugin||''),group:'调用插件'};
  if(n==='list_plugins')return {icon:'plug',verb:'查看插件',detail:'',group:'查看插件'};
  if(n==='future_task')return {icon:'clock',verb:'定时任务',detail:chatClipSummary(a.action||a.content||''),group:'定时任务'};
  if(n.startsWith('get_group')||n.startsWith('set_group')||n==='recall_message'||n==='set_essence_msg'||n==='send_poke'||n==='send_image'||n==='send_file'||n==='send_message'){
    const map={get_group_member_info:'查群成员',get_group_info:'查群信息',get_group_member_list:'查成员列表',set_group_ban:'群禁言',recall_message:'撤回消息',set_group_card:'改群名片',set_essence_msg:'设精华',send_poke:'拍一拍',send_image:'发图片',send_file:'发文件',send_message:'发送消息'};
    return {icon:'users',verb:map[n]||n,detail:chatClipSummary(a.user_id||a.group_id||a.message_id||''),group:map[n]||n};
  }
  if(n.startsWith('mcp__')){
    const parts=n.split('__');
    const short=parts.slice(2).join('__')||n;
    return {icon:'plug',verb:short,detail:chatClipSummary(parts[1]||'MCP'),group:'MCP 工具'};
  }
  return {icon:'wrench',verb:n,detail:chatClipSummary(argsRaw||''),group:n};
}
function chatToolResultBadge(t){
  if(t.status==='running')return '执行中…';
  if(t.status==='cancelled')return '已中断';
  if(t.status==='error'||t.ok===false)return '失败';
  const res=String(t.result||'').trim();
  if(!res)return '完成';
  const lines=res.split(/\r?\n/).length;
  if(lines>=2)return lines+' 行';
  const chars=Number(t.result_chars||res.length);
  return chars>=1000?(Math.round(chars/100)/10+'k 字'):(chars+' 字');
}
function chatFormatToolPayload(raw){
  const s=String(raw||'').trim();
  if(!s)return '';
  const parsed=chatSafeJsonParse(s);
  if(parsed&&typeof parsed==='object'){
    try{return JSON.stringify(parsed,null,2)}catch(e){}
  }
  return s;
}
function chatGroupSteps(steps){
  const out=[];let cur=null;
  (Array.isArray(steps)?steps:[]).forEach(step=>{
    if(!step||typeof step!=='object')return;
    if(step.type==='text'){
      const t=String(step.text||'').trim();
      if(t){cur=null;out.push({kind:'prose',text:t})}
      return;
    }
    if(step.type==='thinking'||step.type==='tool'){
      if(!cur){cur={kind:'activity',items:[]};out.push(cur)}
      cur.items.push(step);
    }
  });
  return out;
}
function chatActivitySummary(items,isLive){
  const tools=items.filter(x=>x.type==='tool');
  const thinks=items.filter(x=>x.type==='thinking');
  const running=tools.find(t=>t.status==='running');
  if(isLive&&running){
    const meta=chatToolMeta(running.name,running.arguments);
    const done=tools.filter(t=>t.status!=='running').length;
    const prefix=done?`已执行 ${done} 项，`:'';
    return {icons:[meta.icon],title:`${prefix}正在${meta.verb}${meta.detail?(' '+meta.detail):''}…`,running:true,totalMs:0};
  }
  if(!tools.length){
    const chars=thinks.reduce((s,x)=>s+String(x.text||'').length,0);
    const ms=thinks.reduce((s,x)=>s+Number(x.duration_ms||0),0);
    return {icons:['spark'],title:isLive?'正在思考…':'思考过程',meta:chars>=1000?(Math.round(chars/100)/10+'k 字'):(chars+' 字'),running:!!isLive,totalMs:ms};
  }
  const counts=new Map(),iconSet=[];
  if(thinks.length)iconSet.push('spark');
  tools.forEach(t=>{
    const m=chatToolMeta(t.name,t.arguments);
    counts.set(m.group,(counts.get(m.group)||0)+1);
    if(!iconSet.includes(m.icon)&&iconSet.length<3)iconSet.push(m.icon);
  });
  const parts=[...counts.entries()].slice(0,3).map(([g,c])=>`${g} ${c} 次`);
  const extra=counts.size>3?` 等 ${tools.length} 项`:'';
  const thinkPrefix=(thinks.length&&parts.length)?'思考 · ':'';
  const totalMs=items.reduce((s,x)=>s+Number(x.duration_ms||0),0);
  return {icons:iconSet,title:thinkPrefix+parts.join('、')+extra,meta:'',running:false,totalMs};
}
function chatActivityOpen(sid,key){return !!chatState.activityOpen[reasonKey(sid,key)]}
function chatToggleActivity(sid,key){const k=reasonKey(sid,key);chatState.activityOpen[k]=!chatState.activityOpen[k];chatRender(false)}
function chatToolOpen(sid,key){return !!chatState.toolOpen[reasonKey(sid,key)]}
function chatToggleTool(sid,key){const k=reasonKey(sid,key);chatState.toolOpen[k]=!chatState.toolOpen[k];chatRender(false)}
function chatCopyTurn(key,btn){
  const text=_turnCopyCache[key]||'';
  if(!text)return;
  const mark=()=>{
    if(!btn)return;
    const old=btn.innerHTML;
    btn.classList.add('is-copied');
    btn.innerHTML=`<span class="turn-copy-ic">${CHAT_ICONS.check}</span><span>已复制</span>`;
    setTimeout(()=>{btn.classList.remove('is-copied');btn.innerHTML=old},1200);
  };
  if(navigator.clipboard?.writeText)navigator.clipboard.writeText(text).then(mark).catch(()=>{});
}
function renderActivityGroup(sid,key,items,isLive){
  const list=Array.isArray(items)?items:[];
  if(!list.length)return '';
  // 单段思考无工具：保持一级直开体验，点击即看思考正文
  if(list.length===1&&list[0].type==='thinking'){
    return renderReasonBlock(sid,key,list[0].text,isLive,list[0].duration_ms);
  }
  const rk=reasonKey(sid,key);
  if(chatState.reasonOpen[rk]&&chatState.activityOpen[rk]===undefined){
    chatState.activityOpen[rk]=true;
    delete chatState.reasonOpen[rk];
    if(list[0]?.type==='thinking')chatState.reasonOpen[reasonKey(sid,key+'_th_0')]=true;
  }
  const sum=chatActivitySummary(list,isLive);
  const open=chatActivityOpen(sid,key);
  const iconHtml=sum.running
    ?`<span class="act-ic is-spin">${CHAT_ICONS.spinner}</span>`
    :`<span class="act-icons">${sum.icons.map(ic=>`<span class="act-ic">${CHAT_ICONS[ic]||CHAT_ICONS.wrench}</span>`).join('')}</span>`;
  const rightMeta=sum.totalMs>0?chatFmtDuration(sum.totalMs):(sum.meta||'');
  let bodyHtml='';
  if(open){
    bodyHtml=`<div class="act-list">${list.map((item,idx)=>{
      if(item.type==='thinking'){
        const tKey=key+'_th_'+idx,tOpen=reasonIsOpen(sid,tKey),tChars=String(item.text||'').length;
        const tDur=chatFmtDuration(item.duration_ms);
        const tRight=tDur||(tChars>=1000?(Math.round(tChars/100)/10+'k 字'):(tChars+' 字'));
        const tScrollKey=reasonKey(sid,tKey),isLiveThink=!!(isLive&&idx===list.length-1);
        return `<div class="act-row ${tOpen?'open':''}">
          <button type="button" class="act-row-head" onclick="chatToggleReason('${esc(String(sid))}','${esc(String(tKey))}')">
            <span class="act-row-ic">${CHAT_ICONS.spark}</span>
            <span class="act-row-verb">思考</span>
            <span class="act-row-arg">${esc(chatClipSummary(item.text||'',56))}</span>
            <span class="act-row-right"><span class="act-row-stat">${esc(tRight)}</span><span class="act-row-caret">${CHAT_ICONS.chevron}</span></span>
          </button>
          ${tOpen?`<div class="act-detail"><div class="act-detail-pre" data-scroll-key="${esc(tScrollKey)}"${isLiveThink?' data-live-scroll="1"':''}>${esc(String(item.text||'').trim())}</div></div>`:''}
        </div>`;
      }
      const m=chatToolMeta(item.name,item.arguments);
      const tKey=key+'_tl_'+(item.call_id||idx),tOpen=chatToolOpen(sid,tKey);
      const stCls=item.status==='running'?'is-running':(item.status==='error'||item.status==='cancelled'||item.ok===false)?'is-error':'is-ok';
      const badge=chatToolResultBadge(item),dur=item.duration_ms?chatFmtDuration(item.duration_ms):'';
      const argFmt=chatFormatToolPayload(item.arguments),resText=String(item.result||'');
      return `<div class="act-row ${stCls} ${tOpen?'open':''}">
        <button type="button" class="act-row-head" onclick="chatToggleTool('${esc(String(sid))}','${esc(String(tKey))}')">
          <span class="act-row-ic">${item.status==='running'?CHAT_ICONS.spinner:(CHAT_ICONS[m.icon]||CHAT_ICONS.wrench)}</span>
          <span class="act-row-verb">${esc(m.verb)}</span>
          <span class="act-row-arg">${esc(m.detail)}</span>
          <span class="act-row-right"><span class="act-row-badge ${stCls}">${esc(badge)}</span>${dur?`<span class="act-row-dur">${esc(dur)}</span>`:''}<span class="act-row-caret">${CHAT_ICONS.chevron}</span></span>
        </button>
        ${tOpen?`<div class="act-detail">${argFmt?`<div class="act-detail-sec"><div class="act-detail-lbl">参数 · ${esc(item.name)}</div><pre class="act-detail-pre">${esc(argFmt)}</pre></div>`:''}<div class="act-detail-sec"><div class="act-detail-lbl">${item.ok===false||item.status==='error'?'错误输出':'返回结果'}</div><pre class="act-detail-pre">${esc(resText||'（无输出）')}</pre></div></div>`:''}
      </div>`;
    }).join('')}</div>`;
  }
  return `<div class="act-group ${open?'open':''} ${sum.running?'is-live':''}">
    <button type="button" class="act-head" onclick="chatToggleActivity('${esc(String(sid))}','${esc(String(key))}')">
      ${iconHtml}
      <span class="act-title">${esc(sum.title)}</span>
      <span class="act-right">${rightMeta?`<span class="act-dur">${esc(rightMeta)}</span>`:''}<span class="act-caret">${CHAT_ICONS.chevron}</span></span>
    </button>
    ${bodyHtml}
  </div>`;
}
function renderTurnSteps(sid,turnKey,steps,isLive){
  const groups=chatGroupSteps(steps);
  if(!groups.length)return '';
  const lastIdx=groups.length-1;
  return groups.map((g,gi)=>{
    if(g.kind==='prose')return `<div class="bubble turn-prose">${chatMarkdown(g.text)}</div>`;
    const activeGroup=!!(isLive&&gi===lastIdx&&(g.items.some(x=>x.status==='running')||!reqHasVisibleOutput(sid)));
    return renderActivityGroup(sid,`${turnKey}_g${gi}`,g.items,activeGroup);
  }).join('');
}
function reqHasVisibleOutput(sid){const r=chatRequest(sid);return !!(r&&String(r.partial||'').trim())}
// 新消息优先用落盘 ID；旧消息按内容指纹及同指纹序号区分，不能用全局数组下标。
// 完全相同的旧消息从尾部计数，头部历史被裁剪时，保留下来的气泡 key 不漂移。
function reasonKey(sid,idx){return String(sid||'')+':'+idx}
function msgReasonId(m,occurrence=0){
  if(m?.id)return 'id_'+encodeURIComponent(String(m.id)).replace(/'/g,'%27');
  const value=JSON.stringify([m?.role||'',m?.ts||0,m?.request_id||'',m?.content||'',m?.reasoning||'',!!m?.error,m?.attachments||[]]);
  let a=2166136261,b=5381;
  for(let i=0;i<value.length;i++){const code=value.charCodeAt(i);a=Math.imul(a^code,16777619);b=Math.imul(b,33)^code}
  return 'legacy_'+(a>>>0).toString(36)+'_'+(b>>>0).toString(36)+'_'+occurrence;
}
function chatMessageReasonIds(messages){
  const seen=new Map(),ids=new Array(messages.length);
  for(let i=messages.length-1;i>=0;i--){const m=messages[i],base=msgReasonId(m);if(m?.id){ids[i]=base;continue}const count=seen.get(base)||0;ids[i]=msgReasonId(m,count);seen.set(base,count+1)}
  return ids;
}
function reasonIsOpen(sid,idx){return !!chatState.reasonOpen[reasonKey(sid,idx)]}
function chatToggleReason(sid,idx){const k=reasonKey(sid,idx);chatState.reasonOpen[k]=!chatState.reasonOpen[k];chatRender(false)}
function renderReasonBlock(sid,idx,text,streaming,durationMs){
  const t=String(text||'');if(!t.trim())return '';
  const open=reasonIsOpen(sid,idx);
  const label=streaming?'思考中':'思考过程';
  const chars=t.length>=1000?(Math.round(t.length/100)/10+'k 字'):(t.length+' 字');
  const dur=durationMs?chatFmtDuration(durationMs):'';
  const right=dur?(chars+' · '+dur):chars;
  return `<div class="reason-block ${open?'open':''}"><button class="reason-head" onclick="chatToggleReason('${esc(String(sid))}','${esc(String(idx))}')"><span class="reason-spark">${streaming?CHAT_ICONS.spinner:CHAT_ICONS.spark}</span><span>${esc(label)}</span><span class="reason-meta">${esc(right)}</span><span class="reason-caret">${CHAT_ICONS.chevron}</span></button>${open?`<div class="reason-body" data-scroll-key="${esc(reasonKey(sid,idx))}"${streaming?' data-live-scroll="1"':''}>${esc(t.trim())}</div>`:''}</div>`;
}
const chatRequest=(id=chatState.current)=>id?chatState.requests[id]||null:null;
const chatAttachments=(id=chatState.current)=>id?(chatState.attachments[id]||(chatState.attachments[id]=[])):[];
function chatRememberDraft(value){if(chatState.current)chatState.drafts[chatState.current]=String(value||'')}
async function chatApi(path,opt={}){opt.headers=Object.assign({'Content-Type':'application/json','X-WebUI-Token':token()},opt.headers||{});const r=await fetch(path,opt),j=await r.json().catch(()=>({ok:false,error:'请求失败'}));if(r.status===401){localStorage.removeItem('webuiToken');location.href='/auth/login';throw new Error('请先登录')}if(!j.ok)throw new Error(j.error||'请求失败');return j.data??j}
function renderChatroom(){const req=chatRequest(),ownSending=!!req,error=chatState.errors[chatState.current]||'',errorHtml=error?`<div class="desc" style="margin-bottom:12px;color:var(--red,#f87171);white-space:pre-wrap">${esc(error)}</div>`:'';return `${errorHtml}<div class="chatroom"><div class="chat-side"><button class="btn primary" onclick="chatNew()">＋ 新建会话</button><div class="sess-list" id="sessList">${renderSessList()}</div></div><div class="chat-main" ondragenter="chatDragEnter(event)" ondragover="chatDragOver(event)" ondragleave="chatDragLeave(event)" ondrop="chatDrop(event)"><div class="chat-drop-mask" id="chatDropMask"><div class="chat-drop-box"><span class="chat-drop-ic">${CHAT_ICONS.paperclip}</span><div class="chat-drop-t">松开即可导入文件或图片</div><div class="chat-drop-sub">支持图片、PDF、DOCX、XLSX 及各类文本 / 代码文件</div></div></div><div class="chat-head"><select id="chatModel" onchange="chatPickModel(this.value)">${renderModelOptions()}</select><select id="chatPersona" onchange="chatPickPersona(this.value)" ${chatState.session?'':'disabled'} title="切换人格：下一条消息起用新人格，历史对话保留">${renderPersonaOptions()}</select><span class="pill" style="margin-left:auto">${chatState.session?esc(chatState.session.title||'新会话'):'未选择会话'}</span></div><div class="chat-msgs" id="chatMsgs">${renderChatMsgs()}</div><div class="chat-composer"><input id="chatFileInput" type="file" multiple style="display:none" onchange="chatPickFiles(this.files);this.value=''">${renderChatFiles()}<div class="chat-input"><button class="chat-icon" title="导入文件或图片（支持拖拽与粘贴）" onclick="el('chatFileInput')?.click()">＋</button><textarea id="chatInput" placeholder=" " oninput="chatRememberDraft(this.value);chatAutoGrow(this)" onpaste="chatPaste(event)" onkeydown="chatInputKey(event)" ${chatState.session?'':'disabled'}>${esc(chatState.drafts[chatState.current]||'')}</textarea><button class="chat-icon ${chatState.stream?'on':''}" title="${chatState.stream?'流式输出已开启':'流式输出已关闭'}" onclick="chatToggleStream()">~</button><button class="btn primary chat-send ${ownSending?'stop':''}" id="chatSendBtn" onclick="${ownSending?'chatStop()':'chatSend()'}" ${chatState.session?'':'disabled'} title="${ownSending?'停止':'发送'}">${ownSending?'■':'↑'}</button></div></div></div></div>`}
function renderModelOptions(){const ms=chatState.models||[];if(!ms.length)return '<option value="">未配置模型</option>';const cur=chatState.session?.model||ms[0].model;return ms.map(m=>`<option value="${esc(m.model)}" ${m.model===cur?'selected':''}>${esc(m.model)}${m.supports_multimodal?' · 识图':''}${m.in_rotation===false?' · 未轮换':''}</option>`).join('')}
function renderPersonaOptions(){const ps=chatState.personas||[];const list=ps.length?ps:[{id:'',name:'跟随默认人格'}];const cur=String(chatState.session?.persona||'');return list.map(p=>`<option value="${esc(p.id)}" ${String(p.id)===cur?'selected':''}>${esc(p.name)}</option>`).join('')}
function renderSessList(){const ss=chatState.sessions||[];if(!ss.length)return '<div class="desc" style="padding:8px">暂无会话，点击上方新建</div>';return ss.map(s=>{const busy=!!chatRequest(s.id);return `<div class="sess-item ${s.id===chatState.current?'active':''}" onclick="chatOpen('${s.id}')"><div class="t">${esc(s.title||'新会话')} ${busy?'<span class="pill">生成中</span>':''}</div><div class="m">${esc(s.model||'-')} · ${s.message_count||0} 次</div>${s.id===chatState.current?`<div class="sess-actions"><button class="btn" onclick="event.stopPropagation();chatRename('${s.id}')">重命名</button><button class="btn" onclick="event.stopPropagation();chatDelete('${s.id}')" ${busy?'disabled':''}>删除</button></div>`:''}</div>`}).join('')}
function copyMdCode(btn){const code=btn.closest('.md-code')?.querySelector('code')?.innerText||'';navigator.clipboard?.writeText(code).then(()=>{const old=btn.textContent;btn.textContent='已复制';setTimeout(()=>btn.textContent=old,900)}).catch(()=>toast('复制失败',false))}
function mdInline(t){return esc(t).replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noreferrer">$1</a>').replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>')}
function mdCells(line){let x=line.trim();if(x.startsWith('|'))x=x.slice(1);if(x.endsWith('|'))x=x.slice(0,-1);return x.split('|').map(v=>v.trim())}
function isMdTableSep(line){return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line||'')}
function chatMarkdown(s){s=String(s||'').replace(/\r\n/g,'\n');const lines=s.split('\n'),out=[];let i=0;function para(buf){if(buf.length)out.push('<p>'+mdInline(buf.join(' ').trim())+'</p>')}while(i<lines.length){let line=lines[i];if(!line.trim()){i++;continue}if(/^```/.test(line.trim())){const lang=line.trim().replace(/^```/,'').trim()||'code',buf=[];i++;while(i<lines.length&&!/^```/.test(lines[i].trim()))buf.push(lines[i++]);if(i<lines.length)i++;out.push(`<div class="md-code"><div class="md-code-head"><span>${esc(lang)}</span><button class="md-copy" onclick="copyMdCode(this)">复制</button></div><pre><code>${esc(buf.join('\n'))}</code></pre></div>`);continue}if(line.includes('|')&&lines[i+1]&&isMdTableSep(lines[i+1])){const heads=mdCells(line),rows=[];i+=2;while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()){rows.push(mdCells(lines[i++]));}out.push(`<div class="md-table-wrap"><table><thead><tr>${heads.map(h=>`<th>${mdInline(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${heads.map((_,idx)=>`<td>${mdInline(r[idx]||'')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);continue}if(/^---+$/.test(line.trim())){out.push('<hr>');i++;continue}const hm=line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);if(hm){const lv=Math.min(3,hm[1].length);out.push(`<h${lv}>${mdInline(hm[2])}</h${lv}>`);i++;continue}if(/^>\s?/.test(line)){const buf=[];while(i<lines.length&&/^>\s?/.test(lines[i]))buf.push(lines[i++].replace(/^>\s?/,''));out.push('<blockquote>'+mdInline(buf.join('\n')).replace(/\n/g,'<br>')+'</blockquote>');continue}if(/^\s*[-*+]\s+/.test(line)){const items=[];while(i<lines.length&&/^\s*[-*+]\s+/.test(lines[i]))items.push('<li>'+mdInline(lines[i++].replace(/^\s*[-*+]\s+/,''))+'</li>');out.push('<ul>'+items.join('')+'</ul>');continue}if(/^\s*\d+[.)]\s+/.test(line)){const items=[];while(i<lines.length&&/^\s*\d+[.)]\s+/.test(lines[i]))items.push('<li>'+mdInline(lines[i++].replace(/^\s*\d+[.)]\s+/,''))+'</li>');out.push('<ol>'+items.join('')+'</ol>');continue}const buf=[];while(i<lines.length&&lines[i].trim()&&!/^(\s{0,3}#{1,6})\s+/.test(lines[i])&&!/^```/.test(lines[i].trim())&&!/^---+$/.test(lines[i].trim())&&!/^>\s?/.test(lines[i])&&!/^\s*[-*+]\s+/.test(lines[i])&&!/^\s*\d+[.)]\s+/.test(lines[i])&&!(lines[i].includes('|')&&lines[i+1]&&isMdTableSep(lines[i+1])))buf.push(lines[i++]);para(buf)}return '<div class="md">'+out.join('')+'</div>'}
function chatPlainText(s){return esc(String(s||'')).replace(/\r\n|\r|\n/g,'<br>')}
function renderAssistantTurn(sid,items){
  const turnKey=reasonKey(sid,items[0].rid);
  const fullText=items.map(it=>String(it.m.content||'')).filter(Boolean).join('\n\n');
  _turnCopyCache[turnKey]=fullText;
  const turnSteps=items.map(it=>it.m.steps).find(arr=>Array.isArray(arr)&&arr.length);
  let stepsHtml='';
  if(turnSteps){
    stepsHtml=renderTurnSteps(sid,turnKey+'_st',turnSteps,false);
  }
  const seenReason=new Set();
  const bubblesHtml=items.map(it=>{
    const m=it.m;
    let rHtml='';
    if(!turnSteps&&m.reasoning&&String(m.reasoning).trim()){
      const rk=String(m.reasoning).trim();
      if(!seenReason.has(rk)){
        seenReason.add(rk);
        rHtml=renderReasonBlock(sid,it.rid,m.reasoning,false);
      }
    }
    return `${rHtml}<div class="bubble">${chatMarkdown(m.content||'')}</div>`;
  }).join('');
  const last=items[items.length-1].m;
  const durMs=items.reduce((mx,it)=>Math.max(mx,Number(it.m.duration_ms||0)),0);
  const timeTxt=chatFmtTime(last.ts),durTxt=chatFmtDurationCN(durMs);
  const metaText=[timeTxt,durTxt?`耗时 ${durTxt}`:''].filter(Boolean).join(' · ');
  const footHtml=`<div class="turn-foot">${fullText?`<button type="button" class="turn-copy-btn" title="复制回复" onclick="chatCopyTurn('${esc(turnKey)}',this)"><span class="turn-copy-ic">${CHAT_ICONS.copy}</span><span>复制</span></button>`:''}${metaText?`<span class="turn-meta">${esc(metaText)}</span>`:''}</div>`;
  return `<div class="msg assistant turn-group"><div class="msg-body">${chatBotHeaderHtml(false)}${stepsHtml}<div class="turn-bubbles">${bubblesHtml}</div>${footHtml}</div></div>`;
}
function renderLiveTurn(sid,req){
  const waiting=!req.partial?'<span class="chat-thinking-dots">思考中…</span>':'';
  const hasSteps=Array.isArray(req.steps)&&req.steps.length>0;
  const stepsHtml=hasSteps?renderTurnSteps(sid,'live',req.steps,true):'';
  const legacyReasonHtml=!hasSteps?`<div id="chatPartialReason">${renderReasonBlock(sid,'live',req.reasoning,true)}</div>`:'<div id="chatPartialReason"></div>';
  const showBubble=!!(req.partial||(!hasSteps&&!req.reasoning)||(!hasSteps&&req.reasoning));
  const partialHtml=`<div class="bubble${!req.partial&&hasSteps?' is-hidden-empty':''}" id="chatPartial">${req.partial?chatMarkdown(req.partial):waiting}</div>`;
  return `<div class="msg assistant turn-group is-live"><div class="msg-body">${chatBotHeaderHtml(false)}<div id="chatLiveSteps">${stepsHtml}</div>${legacyReasonHtml}<div class="turn-bubbles">${showBubble?partialHtml:'<div class="bubble is-hidden-empty" id="chatPartial"></div>'}</div></div></div>`;
}
function renderChatMsgs(){
  const s=chatState.session;if(!s)return '<div class="chat-empty">选择左侧会话，或新建一个开始对话吧</div>';
  const req=chatRequest(s.id),msgs=(s.messages||[]).filter(m=>m.role==='user'||m.role==='assistant');
  if(!msgs.length&&!req)return '<div class="chat-empty">发送第一条消息开始对话吧</div>';
  const reasonIds=chatMessageReasonIds(msgs);
  const blocks=[];
  let i=0;
  while(i<msgs.length){
    const m=msgs[i],rid=reasonIds[i];
    if(m.role==='user'){
      const hasAttChips=Array.isArray(m.attachments)&&m.attachments.length&&typeof m.user_text==='string';
      if(hasAttChips){
        const chips=m.attachments.map(a=>{
          const isImg=String(a?.type||'').startsWith('image/');
          const chars=Number(a?.chars||(typeof a?.text==='string'?a.text.length:0)||0);
          const meta=chars>=1000?(Math.round(chars/100)/10+'k 字'):(chars>0?(chars+' 字'):'');
          return `<span class="user-att-chip" title="${esc(a?.name||'附件')}"><span class="user-att-ic">${isImg?CHAT_ICONS.image:CHAT_ICONS.file}</span><span class="user-att-name">${esc(a?.name||'附件')}</span>${meta?`<span class="user-att-meta">${esc(meta)}</span>`:''}</span>`;
        }).join('');
        const ut=String(m.user_text||'').trim();
        blocks.push(`<div class="msg user"><div class="msg-body user-msg-body"><div class="user-att-list">${chips}</div>${ut?`<div class="bubble">${chatPlainText(ut)}</div>`:''}</div></div>`);
      }else{
        blocks.push(`<div class="msg user"><div class="msg-body"><div class="bubble">${chatPlainText(m.content)}</div></div></div>`);
      }
      i++;
      continue;
    }
    if(m.error){
      const errSteps=(Array.isArray(m.steps)&&m.steps.length)?renderTurnSteps(s.id,rid+'_err',m.steps,false):'';
      blocks.push(`<div class="msg assistant turn-group"><div class="msg-body">${chatBotHeaderHtml(true)}${errSteps}<div class="bubble" style="border-color:var(--red,#f87171);color:var(--red,#f87171);white-space:pre-wrap">${esc(m.content||'生成失败')}</div></div></div>`);
      i++;
      continue;
    }
    const group=[{m,rid}];
    const reqId=m.request_id||'';
    if(reqId){
      while(i+1<msgs.length){
        const nxt=msgs[i+1];
        if(nxt&&nxt.role==='assistant'&&!nxt.error&&(nxt.request_id||'')===reqId){
          i++;
          group.push({m:nxt,rid:reasonIds[i]});
        }else break;
      }
    }
    blocks.push(renderAssistantTurn(s.id,group));
    i++;
  }
  const live=req?renderLiveTurn(s.id,req):'';
  return blocks.join('')+live;
}
function renderChatFiles(){const fs=chatAttachments();if(!fs.length)return '<div class="chat-files" id="chatFiles" style="display:none"></div>';return `<div class="chat-files" id="chatFiles">${fs.map((f,i)=>`<span class="chat-file" title="${esc(f.name)}"><span class="chat-file-ic">${f.type&&f.type.startsWith('image/')?CHAT_ICONS.image:CHAT_ICONS.paperclip}</span><span>${esc(f.name)}</span><button onclick="chatRemoveFile(${i})">×</button></span>`).join('')}</div>`}
function chatCaptureInnerScroll(root){
  const map=Object.create(null);
  if(!root?.querySelectorAll)return map;
  root.querySelectorAll('[data-scroll-key]').forEach(n=>{
    const k=n.getAttribute?.('data-scroll-key')||n.dataset?.scrollKey;
    if(!k)return;
    const near=(n.scrollHeight-n.scrollTop-n.clientHeight)<36;
    map[k]=near?'bottom':n.scrollTop;
  });
  return map;
}
function chatRestoreInnerScroll(root,saved){
  if(!root?.querySelectorAll)return;
  root.querySelectorAll('[data-scroll-key]').forEach(n=>{
    const k=n.getAttribute?.('data-scroll-key')||n.dataset?.scrollKey;
    if(!k)return;
    const prev=saved?saved[k]:undefined;
    const isLive=(n.getAttribute?.('data-live-scroll')==='1'||n.dataset?.liveScroll==='1');
    if(isLive&&(prev===undefined||prev==='bottom'))n.scrollTop=n.scrollHeight;
    else if(typeof prev==='number')n.scrollTop=prev;
  });
}
function chatNearBottom(){const box=el('chatMsgs');return !box||(box.scrollHeight-box.scrollTop-box.clientHeight)<80}
function chatRender(stick){const box=el('chatMsgs');chatState._stickBottom=stick??chatNearBottom();chatState._restoreScroll=(!chatState._stickBottom&&box)?box.scrollTop:null;chatState._innerScroll=box?chatCaptureInnerScroll(box):null;render()}
async function chatReloadCurrent(){const id=chatState.current;if(!id)return;try{const obj=await chatApi('/api/chat/session?id='+encodeURIComponent(id));if(chatState.current===id)chatState.session=obj}catch(e){}}
function afterChatroomRender(){
  const room=document.querySelector('.chatroom');if(room)room.classList.add('ready');
  const box=el('chatMsgs');
  if(box){
    if(chatState._innerScroll)chatRestoreInnerScroll(box,chatState._innerScroll);
    const enter=!!chatState._enterBottom,stick=!!chatState._stickBottom,restore=chatState._restoreScroll;
    if(stick||enter){
      const snap=()=>{const b=el('chatMsgs');if(b){b.scrollTop=b.scrollHeight;b.scrollTo?.(0,b.scrollHeight)}};
      snap();
      if(typeof requestAnimationFrame==='function')requestAnimationFrame(snap);
      if(enter)setTimeout(snap,80);
    }else if(restore!=null){
      box.scrollTop=restore;
    }
  }
  chatState._enterBottom=false;chatState._stickBottom=false;chatState._restoreScroll=null;chatState._innerScroll=null;
  if(!chatState.session&&!chatState._booting&&!chatState._creating){chatBootstrap();return}
  const input=el('chatInput');if(input&&!input.disabled){if(input.value!==(chatState.drafts[chatState.current]||''))input.value=chatState.drafts[chatState.current]||'';chatAutoGrow(input)}
}
async function chatBootstrap(){if(chatState._booting)return chatState._booting;chatState._booting=(async()=>{try{const[models,personas,sessions]=await Promise.all([chatApi('/api/chat/models'),chatApi('/api/chat/personas').catch(()=>[]),chatApi('/api/chat/sessions')]);chatState.models=Array.isArray(models)?models:[];chatState.personas=Array.isArray(personas)?personas:[];chatState.sessions=Array.isArray(sessions)?sessions:[];if(!chatState.sessions.length){await chatCreateSession();return}const prefer=chatState.current&&chatState.sessions.some(s=>s.id===chatState.current)?chatState.current:chatState.sessions[0].id;if(!chatState.session||chatState.current!==prefer){await chatOpen(prefer);return}if(state.current==='chatroom')render()}catch(e){chatState._bootError=String(e.message||e);toast(chatState._bootError,false)}finally{chatState._loaded=true;chatState._booting=null}})();return chatState._booting}
async function chatRefreshSessions(){try{chatState.sessions=await chatApi('/api/chat/sessions')}catch(e){}}
// 进入聊天室时刷新模型与人格下拉：只局部刷新 <select>，不重绘整个消息列表破坏滚动位置。
async function chatRefreshHeaderOptions(){try{const[models,personas]=await Promise.all([chatApi('/api/chat/models').catch(()=>null),chatApi('/api/chat/personas').catch(()=>null)]);let changed=false;if(Array.isArray(models)){chatState.models=models;changed=true}if(Array.isArray(personas)){chatState.personas=personas;changed=true}if(changed&&state.current==='chatroom'){const sm=el('chatModel'),sp=el('chatPersona');if(sm||sp){if(sm)sm.innerHTML=renderModelOptions();if(sp)sp.innerHTML=renderPersonaOptions()}else chatRender()}}catch(e){}}
async function chatOpen(id){const target=String(id||'');if(!target||target===chatState.current&&chatState.session)return;const previous={id:chatState.current,session:chatState.session};try{chatState.current=target;chatState.session=null;chatRender(true);const obj=await chatApi('/api/chat/session?id='+encodeURIComponent(target));if(chatState.current!==target)return;chatState.session=obj;if(state.current==='chatroom')chatRender(true)}catch(e){if(chatState.current===target){chatState.current=previous.id;chatState.session=previous.session;chatState.errors[target]=String(e.message||e);toast(chatState.errors[target],false);if(state.current==='chatroom')chatRender(true)}}}
async function chatCreateSession(forceModel){if(chatState._creating)return chatState._creating;chatState._creating=(async()=>{try{if(!chatState.models.length){try{chatState.models=await chatApi('/api/chat/models')}catch(e){toast(String(e.message||e),false)}}const model=forceModel||(chatState.models[0]&&chatState.models[0].model)||'';const obj=await chatApi('/api/chat/new',{method:'POST',body:JSON.stringify({model})});chatState.current=obj.id;chatState.session=obj;chatState.attachments[obj.id]=[];chatState.drafts[obj.id]='';delete chatState.errors[obj.id];await chatRefreshSessions();if(state.current==='chatroom')chatRender(true);return obj}catch(e){toast(String(e.message||e),false);throw e}finally{chatState._creating=null}})();return chatState._creating}
async function chatNew(){// 用户点击新建：等初始化结束后再建，避免和默认会话抢
if(chatState._booting){try{await chatState._booting}catch(e){}}return chatCreateSession()}
async function chatPickModel(model){if(!chatState.session)return;const id=chatState.current;if(chatRequest(id)){const sel=el('chatModel');if(sel)sel.value=chatState.session.model||'';return toast('当前会话正在生成，完成或停止后再切换模型',false)}const previous=chatState.session.model;chatState.session.model=model;try{const obj=await chatApi('/api/chat/model',{method:'POST',body:JSON.stringify({id,model})});if(chatState.current===id)chatState.session=obj;await chatRefreshSessions()}catch(e){if(chatState.current===id&&chatState.session)chatState.session.model=previous;const sel=el('chatModel');if(sel)sel.value=previous||'';toast(e.message,false)}}
async function chatPickPersona(persona){if(!chatState.session)return;const id=chatState.current;if(chatRequest(id)){const sel=el('chatPersona');if(sel)sel.value=String(chatState.session.persona||'');return toast('当前会话正在生成，完成或停止后再切换人格',false)}const previous=String(chatState.session.persona||'');chatState.session.persona=persona;try{const obj=await chatApi('/api/chat/persona',{method:'POST',body:JSON.stringify({id,persona})});if(chatState.current===id)chatState.session=obj}catch(e){if(chatState.current===id&&chatState.session)chatState.session.persona=previous;const sel=el('chatPersona');if(sel)sel.value=previous;toast(e.message,false)}}
function chatAutoGrow(ta){if(!ta)return;ta.style.height='0';const h=ta.scrollHeight;ta.style.height=(h<=38||!ta.value?38:Math.min(180,h))+'px'}
function chatToggleStream(){chatState.stream=!chatState.stream;localStorage.webuiChatStream=chatState.stream?'1':'0';chatRender(false)}
function chatInputKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();chatSend()}}
function chatCloseLiveThinking(req){
  const cur=req?.activeThinking;if(!cur)return;
  if(cur._t0)cur.duration_ms=Math.max(0,Date.now()-cur._t0);
  req.activeThinking=null;
}
function chatStop(id=chatState.current){const req=chatRequest(id);if(!id||!req)return;
// 界面上立即停：把已流出的内容当作这条回复就地定稿，不再等服务端。
// 同时通知服务端中断 Agent，它会把内容写进会话（保证刷新后还在）。
// 不能反过来先 abort 连接——那样服务端写 SSE 失败会连提问一起删掉。
if(req.stopping)return;req.stopping=true;req.detached=true;
chatCloseLiveThinking(req);
(req.steps||[]).forEach(st=>{if(st&&st.type==='tool'&&st.status==='running'){st.status='cancelled';st.ok=false}});
fetch('/api/chat/stop',{method:'POST',headers:{'Content-Type':'application/json','X-WebUI-Token':token()},body:JSON.stringify({id}),keepalive:true}).catch(()=>{});
const partial=String(req.partial||'').trim();
const cleanSteps=(req.steps||[]).map(s=>{const c=Object.assign({},s);delete c._t0;return c});
if((partial||cleanSteps.length)&&chatState.session&&chatState.session.id===id){
  chatState.session.messages=chatState.session.messages||[];
  chatState.session.messages.push({
    id:'stop_'+Date.now(),
    role:'assistant',
    content:partial?(partial+'\n\n（已停止）'):'（已停止）',
    reasoning:String(req.reasoning||''),
    steps:cleanSteps,
    ts:Math.floor(Date.now()/1000),
    duration_ms:Math.max(0,Date.now()-(req.startedAt||Date.now())),
  });
  chatCarryLiveFoldToPersisted(id,chatState.session);
}
chatClearLiveFoldState(id);
if(req.timer){clearTimeout(req.timer);req.timer=null}
if(req.reasonTimer){clearTimeout(req.reasonTimer);req.reasonTimer=null}
if(req.stepsTimer){clearTimeout(req.stepsTimer);req.stepsTimer=null}
delete chatState.requests[id];
// 连接留给服务端自己收尾落盘；后台读到的 delta 已无处可去，函数会静默结束。
if(id===chatState.current)chatRender(false)}
function chatRemoveFile(i){chatAttachments().splice(i,1);chatRender(false)}
function chatReadDataUrl(f){
  return new Promise((res,rej)=>{
    const r=new FileReader();
    r.onload=()=>res(String(r.result||''));
    r.onerror=()=>rej(r.error||new Error('读取失败'));
    r.readAsDataURL(f);
  });
}
async function chatFileToAttachment(f){
  if(!f)return null;
  const rawName=String(f.name||'').trim();
  const rawType=String(f.type||'').toLowerCase();
  const isImage=/^(image\/(?!svg\+xml))/.test(rawType)||/\.(png|jpe?g|gif|webp|bmp)$/i.test(rawName);
  const name=rawName||('paste-'+Date.now()+(isImage?'.png':''));
  if(f.size>8*1024*1024){toast('文件过大（上限 8MB）：'+name,false);return null}
  let typ=rawType;
  if(!typ){
    if(/\.png$/i.test(name))typ='image/png';
    else if(/\.jpe?g$/i.test(name))typ='image/jpeg';
    else if(/\.gif$/i.test(name))typ='image/gif';
    else if(/\.webp$/i.test(name))typ='image/webp';
    else if(/\.bmp$/i.test(name))typ='image/bmp';
    else if(/\.pdf$/i.test(name))typ='application/pdf';
    else if(/\.docx$/i.test(name))typ='application/vnd.openxmlformats-officedocument.wordprocessingml.document';
    else if(/\.xlsx$/i.test(name))typ='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  }
  const item={name,type:typ||'application/octet-stream',size:Number(f.size||0)};
  const isDoc=typ==='application/pdf'
    ||typ==='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    ||typ==='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    ||/\.(pdf|docx|xlsx)$/i.test(name);
  if(isImage){
    let dataUrl=await chatReadDataUrl(f);
    if(!dataUrl.startsWith('data:image/')&&/^data:[^;]*;base64,/.test(dataUrl)){
      dataUrl='data:'+item.type+';base64,'+dataUrl.split(';base64,',2)[1];
    }
    item.data=dataUrl;
    return item;
  }
  if(isDoc){
    item.data=await chatReadDataUrl(f);
    return item;
  }
  const isKnownText=typ.startsWith('text/')
    ||/json|xml|javascript|ecmascript|yaml|sql|csv|sh|python/i.test(typ)
    ||/\.(txt|md|markdown|json|jsonl|csv|tsv|log|py|js|mjs|cjs|ts|tsx|jsx|html?|css|scss|less|svg|xml|ya?ml|toml|ini|cfg|conf|env|sh|bash|zsh|bat|cmd|ps1|sql|c|cc|cpp|cxx|h|hpp|java|kt|go|rs|rb|php|vue|svelte|swift|r|lua|properties|gitignore|dockerfile|makefile)$/i.test(name)
    ||/^(dockerfile|makefile|readme|license)$/i.test(name);
  if(isKnownText){
    const rawText=await f.text();
    item.text=rawText.slice(0,60000);
    if(!rawType)item.type='text/plain';
    return item;
  }
  if(f.size<=2*1024*1024){
    const buf=await f.arrayBuffer();
    const sample=new Uint8Array(buf,0,Math.min(4096,buf.byteLength));
    let hasNul=false,ctrl=0;
    for(let i=0;i<sample.length;i++){
      const b=sample[i];
      if(b===0){hasNul=true;break}
      if(b<32&&b!==9&&b!==10&&b!==13)ctrl++;
    }
    if(!hasNul&&(sample.length===0||(ctrl/sample.length)<0.05)){
      item.text=new TextDecoder('utf-8').decode(buf).slice(0,60000);
      if(!rawType)item.type='text/plain';
      return item;
    }
  }
  toast('暂不支持解析该格式：'+name+'（支持图片、PDF、DOCX、XLSX 及文本/代码文件）',false);
  return null;
}
async function chatPickFiles(files){
  const id=chatState.current,attachments=chatAttachments(id);
  const incoming=Array.from(files||[]);
  if(!incoming.length)return;
  const remain=Math.max(0,8-attachments.length);
  if(!remain){toast('单条消息最多导入 8 个文件',false);return}
  if(incoming.length>remain)toast('单条消息最多导入 8 个文件，已截取前 8 个',false);
  for(const f of incoming.slice(0,remain)){
    try{
      const item=await chatFileToAttachment(f);
      if(item)attachments.push(item);
    }catch(err){
      toast('读取文件失败：'+(f?.name||'文件')+'（'+(err?.message||err)+'）',false);
    }
  }
  if(chatState.current===id)chatRender(false);
}
function chatDragHasFiles(e){
  const types=Array.from(e?.dataTransfer?.types||[]);
  return types.includes('Files');
}
function chatDragEnter(e){
  if(!chatDragHasFiles(e))return;
  e.preventDefault();
  chatState._dragDepth=(chatState._dragDepth||0)+1;
  e.currentTarget?.classList?.add('is-dragover');
}
function chatDragOver(e){
  if(!chatDragHasFiles(e))return;
  e.preventDefault();
  if(e.dataTransfer)e.dataTransfer.dropEffect='copy';
  e.currentTarget?.classList?.add('is-dragover');
}
function chatDragLeave(e){
  if(!chatDragHasFiles(e))return;
  chatState._dragDepth=Math.max(0,(chatState._dragDepth||0)-1);
  if(chatState._dragDepth===0)e.currentTarget?.classList?.remove('is-dragover');
}
async function chatDrop(e){
  e.preventDefault();
  chatState._dragDepth=0;
  e.currentTarget?.classList?.remove('is-dragover');
  if(!chatState.session)return;
  const files=e.dataTransfer?.files;
  if(files&&files.length)await chatPickFiles(files);
}
async function chatPaste(e){const items=Array.from(e.clipboardData?.items||[]),files=[];for(const it of items){if(it.kind==='file'){const f=it.getAsFile();if(f)files.push(f)}}if(files.length){e.preventDefault();await chatPickFiles(files)}}
function chatClearLiveFoldState(sid){
  const exact=reasonKey(sid,'live'),pref=exact+'_';
  [chatState.reasonOpen,chatState.activityOpen,chatState.toolOpen].forEach(map=>{
    if(!map)return;
    Object.keys(map).forEach(k=>{if(k===exact||k.startsWith(pref))delete map[k]});
  });
}
function chatCarryLiveFoldToPersisted(sid,sessionObj){
  const s=sessionObj||(chatState.current===sid?chatState.session:null);
  if(!s||!Array.isArray(s.messages))return;
  const msgs=s.messages.filter(m=>m&&(m.role==='user'||m.role==='assistant'));
  if(!msgs.length)return;
  const last=msgs[msgs.length-1];
  if(!last||last.role!=='assistant'||last.error)return;
  const reqId=last.request_id||'';
  let start=msgs.length-1;
  if(reqId){
    while(start>0){
      const prev=msgs[start-1];
      if(prev&&prev.role==='assistant'&&!prev.error&&(prev.request_id||'')===reqId)start--;
      else break;
    }
  }
  const reasonIds=chatMessageReasonIds(msgs);
  const firstRid=reasonIds[start];
  const turnKey=reasonKey(sid,firstRid);
  const groupMsgs=msgs.slice(start);
  const turnSteps=groupMsgs.map(m=>m.steps).find(arr=>Array.isArray(arr)&&arr.length);
  if(turnSteps){
    const groups=chatGroupSteps(turnSteps);
    groups.forEach((g,gi)=>{
      if(!g||g.kind!=='activity')return;
      const liveG='live_g'+gi,savedG=turnKey+'_st_g'+gi;
      const liveOpenReason=reasonIsOpen(sid,liveG)||(gi===0&&reasonIsOpen(sid,'live'));
      const liveOpenAct=chatActivityOpen(sid,liveG);
      if(g.items.length===1&&g.items[0].type==='thinking'){
        if(liveOpenReason||liveOpenAct)chatState.reasonOpen[reasonKey(sid,savedG)]=true;
      }else{
        if(liveOpenAct||liveOpenReason)chatState.activityOpen[reasonKey(sid,savedG)]=true;
        const livePref=reasonKey(sid,liveG+'_');
        const savedPref=reasonKey(sid,savedG+'_');
        [chatState.reasonOpen,chatState.toolOpen].forEach(map=>{
          if(!map)return;
          Object.keys(map).forEach(k=>{
            if(map[k]&&k.startsWith(livePref))map[savedPref+k.slice(livePref.length)]=true;
          });
        });
        if(liveOpenReason&&!liveOpenAct&&g.items[0]?.type==='thinking'){
          chatState.reasonOpen[reasonKey(sid,savedG+'_th_0')]=true;
        }
      }
    });
  }else{
    if(reasonIsOpen(sid,'live')||reasonIsOpen(sid,'live_g0')||chatActivityOpen(sid,'live_g0')){
      chatState.reasonOpen[reasonKey(sid,firstRid)]=true;
    }
  }
}
async function chatSend(){if(!chatState.session)return;const sessionId=chatState.session.id;if(chatRequest(sessionId)){chatStop(sessionId);return}const ta=el('chatInput'),text=(ta?.value||'').trim(),currentAttachments=chatAttachments(sessionId);if(!text&&!currentAttachments.length)return;chatClearLiveFoldState(sessionId);const model=el('chatModel')?.value||chatState.session.model||'',attachments=currentAttachments.slice(),req={controller:new AbortController(),partial:'',reasoning:'',steps:[],toolsById:Object.create(null),activeThinking:null,startedAt:Date.now(),stopping:false,detached:false};chatState.requests[sessionId]=req;delete chatState.errors[sessionId];chatState.session.messages=chatState.session.messages||[];const uMsg={role:'user',content:text+(attachments.length?('\n'+attachments.map(a=>'[附件：'+a.name+']').join('\n')):''),attachments,ts:Math.floor(Date.now()/1000)};if(attachments.length)uMsg.user_text=text;chatState.session.messages.push(uMsg);chatState.attachments[sessionId]=[];chatState.drafts[sessionId]='';if(ta){ta.value='';chatAutoGrow(ta)}chatRender(true);
// 点过终止后这次请求已从 requests 摘除、界面也定稿了，后续 done/error 一律忽略，
// 不要再刷新会话把界面上那条"（已停止）"顶掉。
const finish=async()=>{if(req.detached)return;if(chatState.requests[sessionId]!==req)return;delete chatState.requests[sessionId];await chatRefreshSessions();if(chatState.current===sessionId){await chatReloadCurrent();chatCarryLiveFoldToPersisted(sessionId,chatState.session||req.session);chatClearLiveFoldState(sessionId);chatRender(chatNearBottom())}else{chatCarryLiveFoldToPersisted(sessionId,req.session);chatClearLiveFoldState(sessionId);renderSessListInPlace()}};
try{await chatSendStream(sessionId,model,text,attachments,chatState.stream,req);await finish()}catch(e){if(e.name!=='AbortError'&&!req.detached&&!req.stopping){chatState.errors[sessionId]=String(e.message||e);if(chatState.current===sessionId)toast(chatState.errors[sessionId],false)}await finish()}}
function renderSessListInPlace(){const list=el('sessList');if(list)list.innerHTML=renderSessList()}
function chatUpdatePartial(sessionId,force=false){const req=chatRequest(sessionId);if(!req||chatState.current!==sessionId)return;const node=el('chatPartial');if(!node)return;if(!force){const now=Date.now();if(req.timer||now-(req.updatedAt||0)<100){if(!req.timer)req.timer=setTimeout(()=>{req.timer=null;chatUpdatePartial(sessionId,true)},100);return}}const stick=chatNearBottom();req.updatedAt=Date.now();const hasSteps=Array.isArray(req.steps)&&req.steps.length>0;node.classList.toggle('is-hidden-empty',!req.partial&&hasSteps);node.innerHTML=req.partial?chatMarkdown(req.partial):'<span class="chat-thinking-dots">思考中…</span>';const box=el('chatMsgs');if(box&&stick)box.scrollTop=box.scrollHeight}
// 思维链与工具步骤增量重绘实时区，不动整个消息列表，避免打字或工具切换时页面抖动
function chatUpdateLiveSteps(sessionId,force=false){
  const req=chatRequest(sessionId);if(!req||chatState.current!==sessionId)return;
  const node=el('chatLiveSteps');
  if(!node){chatRender(false);return}
  if(!force){const now=Date.now();if(req.stepsTimer||now-(req.stepsAt||0)<120){if(!req.stepsTimer)req.stepsTimer=setTimeout(()=>{req.stepsTimer=null;chatUpdateLiveSteps(sessionId,true)},120);return}}
  const stick=chatNearBottom(),innerSaved=chatCaptureInnerScroll(node);req.stepsAt=Date.now();
  node.innerHTML=renderTurnSteps(sessionId,'live',req.steps||[],true);
  chatRestoreInnerScroll(node,innerSaved);
  const partialNode=el('chatPartial');
  if(partialNode)partialNode.classList.toggle('is-hidden-empty',!req.partial&&(req.steps||[]).length>0);
  const box=el('chatMsgs');if(box&&stick)box.scrollTop=box.scrollHeight
}
function chatUpdateReason(sessionId){
  const req=chatRequest(sessionId);if(!req||chatState.current!==sessionId)return;
  if(Array.isArray(req.steps)&&req.steps.length){chatUpdateLiveSteps(sessionId,false);return}
  const node=el('chatPartialReason');if(!node){chatRender(false);return}
  if(req.reasonTimer||Date.now()-(req.reasonAt||0)<150){if(!req.reasonTimer)req.reasonTimer=setTimeout(()=>{req.reasonTimer=null;chatUpdateReason(sessionId)},150);return}
  const stick=chatNearBottom(),innerSaved=chatCaptureInnerScroll(node);req.reasonAt=Date.now();node.innerHTML=renderReasonBlock(sessionId,'live',req.reasoning,true);chatRestoreInnerScroll(node,innerSaved);const box=el('chatMsgs');if(box&&stick)box.scrollTop=box.scrollHeight
}
async function chatSendStream(sessionId,model,text,attachments,stream,req){
  let completed=false;
  const r=await fetch('/api/chat/send-stream',{method:'POST',headers:{'Content-Type':'application/json','X-WebUI-Token':token()},signal:req.controller.signal,body:JSON.stringify({id:sessionId,model,text,attachments,stream:!!stream})});
  if(r.status===401){localStorage.removeItem('webuiToken');location.href='/auth/login';throw new Error('请先登录')}
  if(!r.ok)throw new Error('请求失败：'+r.status);
  const reader=r.body.getReader(),dec=new TextDecoder('utf-8');let buf='';
  while(true){
    const {value,done}=await reader.read();if(done)break;
    buf+=dec.decode(value,{stream:true});const chunks=buf.split('\n\n');buf=chunks.pop()||'';
    for(const raw of chunks){
      let ev='message',data='';
      raw.split('\n').forEach(line=>{if(line.startsWith('event:'))ev=line.slice(6).trim();else if(line.startsWith('data:'))data+=line.slice(5).trim()});
      if(!data)continue;
      const obj=JSON.parse(data);
      if(req.detached){if(ev==='done'||ev==='error')return;continue}
      if(ev==='delta'){
        const deltaTxt=obj.text||'',firstDelta=!req.partial&&!!deltaTxt,hadThinking=!!req.activeThinking;
        if(hadThinking)chatCloseLiveThinking(req);
        req.partial=(req.partial||'')+deltaTxt;
        if(hadThinking||firstDelta)chatUpdateLiveSteps(sessionId,true);
        chatUpdatePartial(sessionId,false);
      }else if(ev==='reasoning'){
        const chunk=String(obj.text||'');
        if(chunk){
          req.reasoning=(req.reasoning||'')+chunk;
          req.steps=req.steps||[];
          let cur=req.activeThinking;
          if(!cur){cur={type:'thinking',text:'',_t0:Date.now(),duration_ms:0};req.steps.push(cur);req.activeThinking=cur}
          cur.text+=chunk;cur.duration_ms=Math.max(0,Date.now()-cur._t0);
          chatUpdateLiveSteps(sessionId,false);
        }
      }else if(ev==='progress'){
        const progress=String(obj.text||'').trim();
        if(progress){
          chatCloseLiveThinking(req);
          req.partial='';req.reasoning='';
          req.steps=req.steps||[];
          req.steps.push({type:'text',text:progress});
          chatUpdateLiveSteps(sessionId,true);
          chatUpdatePartial(sessionId,true);
        }
      }else if(ev==='agent_event'){
        req.steps=req.steps||[];req.toolsById=req.toolsById||Object.create(null);
        const t=String(obj.type||'');
        if(t==='attempt_reset'){
          chatCloseLiveThinking(req);
          if(!req.steps.some(x=>x.type==='tool')){req.steps=[];req.reasoning=''}
          chatUpdateLiveSteps(sessionId,true);
        }else if(t==='round_start'){
          chatCloseLiveThinking(req);
          chatUpdateLiveSteps(sessionId,true);
        }else if(t==='tool_start'){
          chatCloseLiveThinking(req);
          const cid=String(obj.call_id||''),step={type:'tool',call_id:cid,round:obj.round||1,name:obj.name||'',arguments:obj.arguments||'',status:'running',duration_ms:0,result:'',result_chars:0};
          req.steps.push(step);if(cid)req.toolsById[cid]=step;
          chatUpdateLiveSteps(sessionId,true);
        }else if(t==='tool_end'){
          const cid=String(obj.call_id||''),existing=cid?req.toolsById[cid]:null;
          if(existing){
            existing.status=obj.status||(obj.ok?'ok':'error');existing.ok=!!obj.ok;existing.duration_ms=Number(obj.duration_ms||0);existing.result=String(obj.result||'');existing.result_chars=Number(obj.result_chars||existing.result.length);
          }else{
            const step={type:'tool',call_id:cid,round:obj.round||1,name:obj.name||'',arguments:obj.arguments||'',status:obj.status||(obj.ok?'ok':'error'),ok:!!obj.ok,duration_ms:Number(obj.duration_ms||0),result:String(obj.result||''),result_chars:Number(obj.result_chars||0)};
            req.steps.push(step);if(cid)req.toolsById[cid]=step;
          }
          chatUpdateLiveSteps(sessionId,true);
        }
      }else if(ev==='done'){
        completed=true;req.session=obj.session;
        if(chatState.current===sessionId){chatState.session=obj.session;chatUpdatePartial(sessionId,true)}
        return;
      }else if(ev==='error'){
        if(obj.session){completed=true;req.session=obj.session;if(chatState.current===sessionId){chatState.session=obj.session;req.partial='';req.reasoning='';chatUpdatePartial(sessionId,true)}return}
        throw new Error(obj.error||'请求失败');
      }
    }
  }
  if(!completed&&!req.detached)throw new Error(req.partial?'模型连接中断，未收到完整结果':'模型连接已结束，但没有返回内容')
}
async function chatRename(id){if(chatRequest(id))return toast('该会话正在生成，完成或停止后再重命名',false);const name=prompt('输入新的会话标题');if(name==null)return;try{const obj=await chatApi('/api/chat/rename',{method:'POST',body:JSON.stringify({id,title:name})});if(chatState.current===id)chatState.session=obj;await chatRefreshSessions();render()}catch(e){toast(e.message,false)}}
async function chatDelete(id){if(chatRequest(id))return toast('该会话正在生成，请先停止后再删除',false);if(!confirm('确定删除该会话？此操作不可恢复。'))return;try{await chatApi('/api/chat/delete',{method:'POST',body:JSON.stringify({id})});delete chatState.errors[id];delete chatState.attachments[id];delete chatState.drafts[id];if(chatState.current===id){chatState.current=null;chatState.session=null}await chatRefreshSessions();// list 接口在空目录时会自动补默认会话
if(!chatState.sessions.length){chatState.sessions=await chatApi('/api/chat/sessions')}if(chatState.sessions.length){await chatOpen(chatState.sessions[0].id);return}if(state.current==='chatroom')render()}catch(e){toast(e.message,false)}}
let _storeList=null;
// 插件管理双子页状态：'config' = 插件配置（astrbot 风格），'store' = 插件下载
let _pluginTab='config';
let _pluginSchemas=null;
// 仅会话 UI 状态；不进入业务配置、dirty 或持久草稿。
const pluginConfigOpen=new Set();
function rememberPluginConfigOpen(node){
  if(!node?.isConnected)return; // DOM 替换后迟到的 toggle 不能覆盖新节点状态。
  if(node.open)pluginConfigOpen.add(node.dataset.pluginName);else pluginConfigOpen.delete(node.dataset.pluginName);
}
function snapshotPluginConfigOpen(){
  el('pluginPanel')?.querySelectorAll?.('details.plugin-config-card').forEach(rememberPluginConfigOpen);
}
let _pluginSchemasFlight=null,_storeFlight=null;
function ensurePluginTabLoaded(){if(_pluginTab==='config'){if(_pluginSchemas===null)loadPluginSchemas()}else if(_storeList===null)loadStore()}
function switchPluginTab(tab){
  if(!['config','store'].includes(tab)||tab===_pluginTab)return;
  snapshotPluginConfigOpen();_pluginTab=tab;const panel=el('pluginPanel');
  if(panel){panel.innerHTML=tab==='config'?renderPluginConfig():renderPluginStore();el('pluginTabs')?.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('primary',b.dataset.tab===tab));animateSubtab(panel)}
  ensurePluginTabLoaded();
}
function refreshPluginPanel(tab){const panel=el('pluginPanel');if(state.current==='store'&&_pluginTab===tab&&panel){snapshotPluginConfigOpen();panel.innerHTML=tab==='config'?renderPluginConfig():renderPluginStore()}}
function renderStore(){
  return `<div class="plugin-root"><div class="subtab-nav" id="pluginTabs"><button class="btn ${_pluginTab==='config'?'primary':''}" data-tab="config" onclick="switchPluginTab(this.dataset.tab)">插件配置</button><button class="btn ${_pluginTab==='store'?'primary':''}" data-tab="store" onclick="switchPluginTab(this.dataset.tab)">插件下载</button></div><div class="subtab-panel plugin-panel" id="pluginPanel">${_pluginTab==='config'?renderPluginConfig():renderPluginStore()}</div></div>`;
}
function renderPluginConfig(){
  if(_pluginSchemas===null){
    return `<div class="card"><div class="section-head"><div><h3>插件配置</h3><p>已安装插件的可自定义配置项</p></div><button class="btn" onclick="loadPluginSchemas()">刷新</button></div><div class="desc" style="margin-top:16px">正在加载…</div></div>`;
  }
  if(_pluginSchemas.error){
    return `<div class="card"><div class="section-head"><div><h3>插件配置</h3></div><button class="btn" onclick="loadPluginSchemas()">重试</button></div><div class="desc" style="color:var(--red,#f87171)">${esc(_pluginSchemas.error)}</div></div>`;
  }
  const names=Object.keys(_pluginSchemas||{});
  if(!names.length){
    return `<div class="card"><div class="section-head"><div><h3>插件配置</h3><p>已安装插件的可自定义配置项</p></div><button class="btn" onclick="loadPluginSchemas()">刷新</button></div>
    <div class="desc" style="margin-top:16px;line-height:1.8">
      当前没有插件提供配置项。<br><br>
      <b>插件作者</b>：在 <code>metadata.yaml</code> 里加 <code>config_schema:</code> 字段，或在 <code>setup.py</code> 里定义 <code>CONFIG_SCHEMA = {...}</code> 即可让用户在此处配置。<br>
      <b>用户</b>：去「插件下载」子页安装提供配置项的插件。
    </div></div>`;
  }
  const cards=names.map(name=>{
    const info=_pluginSchemas[name]||{};
    const schema=info.schema||{};
    const values=state.bundle.form_values||{};
    const keys=Object.keys(schema);
    if(!keys.length)return `<div class="card"><div class="section-head" style="margin-bottom:0"><div><h3>${esc(name)}</h3><p>该插件未提供配置项</p></div></div></div>`;
    const fieldsHtml=keys.map(key=>{
      const spec=schema[key]||{};
      const typ=String(spec.type||'string').toLowerCase();
      const label=spec.label||key;
      const desc=spec.desc||'';
      const cur=values[`PluginConfigs.${name}.${key}`];
      const inputId=esc(`pluginCfg_${name}_${key}`);
      const data=`data-name="${esc(name)}" data-key="${esc(key)}"`;
      if(typ==='bool'||typ==='boolean'){
        return `<div class="setting-row plugin-setting-row"><div class="label"><span>${esc(label)}</span><div class="switch ${cur?'on':''}" ${data} onclick="pluginCfgToggle(this.dataset.name,this.dataset.key,this)"></div></div><div class="desc">${esc(desc)}</div></div>`;
      }
      if(typ==='number'||typ==='int'||typ==='float'){
        return `<div class="setting-row plugin-setting-row"><div class="label"><span>${esc(label)}</span></div><input type="number" id="${inputId}" value="${esc(cur??'')}" ${spec.min!=null?`min="${esc(spec.min)}"`:''} ${spec.max!=null?`max="${esc(spec.max)}"`:''} step="${esc(spec.step??'any')}" ${data} oninput="_pluginCfgSyncInput(this.dataset.name,this.dataset.key,this)"><div class="desc">${esc(desc)}</div></div>`;
      }
      if(typ==='select'&&Array.isArray(spec.options)){
        const opts=spec.options.map(o=>`<option value="${esc(o)}" ${o===cur?'selected':''}>${esc(o)}</option>`).join('');
        return `<div class="setting-row plugin-setting-row"><div class="label"><span>${esc(label)}</span></div><select id="${inputId}" ${data} oninput="_pluginCfgSyncInput(this.dataset.name,this.dataset.key,this)">${opts}</select><div class="desc">${esc(desc)}</div></div>`;
      }
      if(typ==='textarea'){
        return `<div class="setting-row plugin-setting-row"><div class="label"><span>${esc(label)}</span></div><textarea id="${inputId}" rows="4" ${data} oninput="_pluginCfgSyncInput(this.dataset.name,this.dataset.key,this)">${esc(cur??'')}</textarea><div class="desc">${esc(desc)}</div></div>`;
      }
      if(typ==='list'){
        const text=Array.isArray(cur)?cur.join('\n'):String(cur??'');
        return `<div class="setting-row plugin-setting-row"><div class="label"><span>${esc(label)}</span></div><textarea id="${inputId}" rows="4" placeholder="一行一条" ${data} oninput="_pluginCfgSyncInput(this.dataset.name,this.dataset.key,this)">${esc(text)}</textarea><div class="desc">${esc(desc)} 一行一条</div></div>`;
      }
      const isPwd=typ==='password'||spec.password;
      return `<div class="setting-row plugin-setting-row"><div class="label"><span>${esc(label)}</span></div><input type="${isPwd?'password':'text'}" id="${inputId}" value="${esc(cur??'')}" ${data} oninput="_pluginCfgSyncInput(this.dataset.name,this.dataset.key,this)"><div class="desc">${esc(desc)}</div></div>`;
    }).join('');
    return `<details class="card plugin-config-card" data-plugin-name="${esc(name)}" ontoggle="rememberPluginConfigOpen(this)"${pluginConfigOpen.has(name)?' open':''}><summary class="plugin-config-summary"><span><strong>${esc(name)}</strong><span class="desc">${keys.length} 个配置项</span></span><span class="plugin-config-caret" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span></summary><div class="plugin-settings-list">${fieldsHtml}</div></details>`;
  }).filter(s=>s).join('');
  return `<div class="card"><div class="section-head"><div><h3>插件配置</h3><p>已安装插件的可自定义配置项</p></div><button class="btn" onclick="loadPluginSchemas()">刷新</button></div></div><div class="plugin-config-cards">${cards}</div>`;
}
function seedPluginConfigValues(){
  const values=state.bundle?.form_values;if(!values||!_pluginSchemas||_pluginSchemas.error)return;
  for(const [name,info] of Object.entries(_pluginSchemas))for(const [key,spec] of Object.entries(info.schema||{})){
    const path=`PluginConfigs.${name}.${key}`;if(Object.hasOwn(values,path))continue;
    // schema 返回的服务端值仅作为缺失字段的初始值，不另存一份可编辑状态。
    const current=state.bundle.config_json?.PluginConfigs?.[name];
    if(current&&Object.hasOwn(current,key))values[path]=current[key];
    else if(Object.hasOwn(info.values||{},key)&&info.values[key]!==null&&info.values[key]!==undefined)values[path]=info.values[key];
    else if(Object.hasOwn(spec||{},'default')&&spec.default!==null&&spec.default!==undefined)values[path]=spec.default;
  }
}
function loadPluginSchemas(){
  if(_pluginSchemasFlight)return _pluginSchemasFlight;
  _pluginSchemasFlight=Promise.resolve().then(async()=>{
    try{_pluginSchemas=await api('/api/plugins/schemas')||{};seedPluginConfigValues()}
    catch(e){_pluginSchemas={error:e.message}}
    finally{_pluginSchemasFlight=null;refreshPluginPanel('config')}
  });return _pluginSchemasFlight;
}
function pluginCfgToggle(name,key,sw){
  const path=`PluginConfigs.${name}.${key}`,value=!state.bundle.form_values[path];
  setValue(path,value);sw.classList.toggle('on',value);saveDraft();
}
function _pluginCfgSyncInput(name,key,inp){
  if(!inp||!Object.hasOwn(_pluginSchemas||{},name)||!Object.hasOwn(_pluginSchemas[name].schema||{},key))return;
  const spec=_pluginSchemas[name].schema[key]||{},typ=String(spec.type||'string').toLowerCase();
  let val=inp.value;
  if(['number','int','float'].includes(typ))val=val===''?null:Number(val);
  else if(typ==='list')val=val.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
  const path=`PluginConfigs.${name}.${key}`;
  if(!sameDraftValues(state.bundle.form_values[path],val)){setValue(path,val);saveDraft()}
}
function renderPluginStore(){
  const list=_storeList;
  if(list===null)return `<div class="card"><div class="section-head"><div><h3>插件下载</h3><p>来自 Qzy327422/XcBot-Plugins</p></div><button class="btn" onclick="loadStore()">刷新</button></div><div class="desc" style="margin-top:16px">正在加载…</div></div>`;
  if(list&&list.error)return `<div class="card"><div class="section-head"><div><h3>插件下载</h3></div><button class="btn" onclick="loadStore()">重试</button></div><div class="desc" style="color:var(--red,#f87171)">${esc(list.error)}</div></div>`;
  return `<div class="card"><div class="section-head"><div><h3>插件下载</h3><p>来自 Qzy327422/XcBot-Plugins · ${list.length} 个插件</p></div><button class="btn" onclick="loadStore()">刷新</button></div><div style="display:grid;gap:12px;margin-top:8px">${(list||[]).map(p=>`<div class="field" style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center"><div><div style="font-weight:900">${esc(p.name)} <span class="pill">${esc('v'+p.version)}</span>${p.installed?'<span class="pill ok" style="margin-left:6px">已安装</span>':''}</div><div class="desc">${esc(p.description||'')} — 作者：${esc(p.author||'-')}</div></div>${p.installed?`<div style="display:flex;gap:6px"><button class="btn" id="storeReload_${esc(p.name)}" data-name="${esc(p.name)}" onclick="storeReload(this.dataset.name)">重载</button><button class="btn" id="storeBtn_${esc(p.name)}" data-name="${esc(p.name)}" onclick="storeUninstall(this.dataset.name)" style="color:var(--bad,#f87171)">卸载</button></div>`:`<button class="btn primary" id="storeBtn_${esc(p.name)}" data-name="${esc(p.name)}" data-path="${esc(p.path||'')}" onclick="storeInstall(this.dataset.name,this.dataset.path)">安装</button>`}</div>`).join('')||'<div class="desc">暂无插件</div>'}</div><details style="margin-top:16px"><summary class="desc" style="cursor:pointer;user-select:none">如何向插件商店提交插件？</summary><div class="desc" style="margin-top:8px;line-height:1.8"><div style="margin-bottom:12px"><button class="btn primary" onclick="el('submitModal').style.display='flex'">点我提交插件</button></div><div style="margin:8px 0 8px;font-weight:800;color:var(--text)">手动提交 ▼</div>1. Fork <b>https://github.com/Qzy327422/XcBot-Plugins</b><br>2. 在 <code>plugins/</code> 下新建文件夹，名称即插件名<br>3. 创建入口文件 <code>setup.py</code> 或 <code>main.py</code>，实现 <code>TRIGGHT_KEYWORD</code> 和 <code>on_message</code><br>4. 同目录添加 <code>metadata.yaml</code>：<br><pre style="background:var(--bg0);padding:8px;border-radius:8px;margin:4px 0">name: your_plugin\nversion: 1.0.0\ndescription: 插件描述\nauthor: 你的名字\nentry: setup.py</pre>5. （可选）让插件支持配置：在 metadata.yaml 加 <code>config_schema</code> 字段<br>6. 提交 Pull Request</div></details></div>`;
}
function loadStore(){
  if(_storeFlight)return _storeFlight;
  _storeFlight=Promise.resolve().then(async()=>{
    try{_storeList=await api('/api/plugins/store')}
    catch(e){_storeList={error:e.message}}
    finally{_storeFlight=null;refreshPluginPanel('store')}
  });return _storeFlight;
}
async function storeInstall(name,path){const btn=el('storeBtn_'+name);if(btn){btn.disabled=true;btn.textContent='安装中…'}try{const r=await api('/api/plugins/install',{method:'POST',body:JSON.stringify({name,path})});toast(r.message||'安装成功');await loadStore()}catch(e){toast(e.message,false);if(btn){btn.disabled=false;btn.textContent='安装'}}}
async function storeUninstall(name){const btn=el('storeBtn_'+name);if(btn){btn.disabled=true;btn.textContent='卸载中…'}try{const r=await api('/api/plugins/uninstall',{method:'POST',body:JSON.stringify({name})});toast(r.message||'卸载成功');await loadStore()}catch(e){toast(e.message,false);if(btn){btn.disabled=false;btn.textContent='卸载'}}}
async function storeReload(name){const btn=el('storeReload_'+name);if(btn){btn.disabled=true;btn.textContent='重载中…'}try{const r=await api('/api/plugins/reload',{method:'POST',body:JSON.stringify({name})});toast(r.message||'重载成功')}catch(e){toast(e.message,false)}finally{if(btn){btn.disabled=false;btn.textContent='重载'}}}
function storeSubmit(){const name=(el('submitName')||{}).value?.trim();const author=(el('submitAuthor')||{}).value?.trim();const desc=(el('submitDesc')||{}).value?.trim();if(!name||!author||!desc){toast('请填写插件名、作者和描述',false);return}const body=`## 插件提交\n\n**插件名**：${name}\n**作者**：${author}\n\n**功能描述**：\n${desc}\n\n**插件压缩包**：（请将 zip 文件拖拽到下方评论框上传）`;const params=new URLSearchParams({title:`[插件提交] ${name}`,body,labels:'plugin-submission'});window.open(`https://github.com/Qzy327422/XcBot-Plugins/issues/new?${params}`,'_blank')}
async function storeUploadFile(input){const file=input.files[0];if(!file)return;if(file.size>12*1024*1024){toast('插件包过大（上限 12MB）',false);input.value='';return}const name=file.name.replace(/\.zip$/i,'');const buf=await file.arrayBuffer();const bytes=new Uint8Array(buf);let bin='';const chunk=0x8000;for(let i=0;i<bytes.length;i+=chunk){bin+=String.fromCharCode.apply(null,bytes.subarray(i,i+chunk))}const b64=btoa(bin);input.value='';try{const r=await api('/api/plugins/upload',{method:'POST',body:JSON.stringify({name,zip_b64:b64})});toast(r.message||'安装成功');await loadStore()}catch(e){toast(e.message,false)}}
setTheme(localStorage.webuiTheme||'light');window.addEventListener('hashchange',()=>{if(state._navigating)return;if(state.dirty){const m=el('leaveModal');if(m){m.style.display='flex';const r=splitHashRoute();m._pending={k:normalizeRoutePage(r.page),section:r.section||''};history.replaceState(null,'',routeForPage(state.current,state.section));return}}applyRoute();render()});window.addEventListener('beforeunload',e=>{if(state.dirty||(state.current==='role-life'&&(roleLifeHasDraft()||roleLifeState.saving))){e.preventDefault();e.returnValue=''}});applyRoute();if(state.current==='chatroom')chatState._enterBottom=true;if(!location.hash)updateRoute(state.current,'',true);applyCachedAppearance();refreshAll(true).then(checkAuthState);setInterval(()=>refreshAll(false),5000);setInterval(()=>{if(state.current==='trace'&&!traceState.openId&&!traceState.loading&&!traceTogglePending())traceLoad(true)},5000);setInterval(()=>{if(state.current==='welcome'||state.current==='logs')scrollLogsToBottom()},500);

// ==================== 角色生活 ====================
// 设计约束（§2.1/§2.8/§3.6）：当前生活 > 日程与经历 > 用量与任务；每个视图仅一个主操作。
// 沿用主题 text/muted/accent/glass/line/radius；局部间距 8/12/16/24、字号 12/14/18/24。
// 先分层与留白，再用主题色提示状态；模型文本只经 esc() 展示，动态 ID 不拼进脚本。
let roleLifeState={data:null,selectorData:null,loading:false,error:'',account:'',role:'',tab:'overview',
  subTab:'activity',todayTab:'today',generation:0,sequence:0,updatedAt:0,saving:false,
  page:{activity:1,diary:1,dream:1,plans:1,jobs:1},
  records:{activity:null,diary:null,dream:null,plans:null,jobs:null},recordSequence:{},
  pending:Object.create(null),requests:Object.create(null),draft:null,
  planEditor:null,expEditor:null,noteEditor:false,noteDraft:'',message:'',messageKind:''};
const ROLE_LIFE_TABS=[['overview','概览'],['today','日程'],['experiences','生活记录'],['settings','设置'],['jobs','任务']];
function roleLifeHasDraft(){return !!(roleLifeState.draft&&Object.keys(roleLifeState.draft).length)}
function roleLifeIdentity(){return JSON.stringify([roleLifeState.account||'',roleLifeState.role||''])}
function roleLifeCurrent(generation,identity){return (roleLifeState.generation||0)===generation&&roleLifeIdentity()===identity}
function roleLifeLeave(){
  if(roleLifeBusy()){toast('操作进行中，完成后再切换',false);return false}
  if(roleLifeHasDraft()&&!confirm('角色生活设置尚未保存，离开会放弃这些修改，继续？'))return false;
  roleLifeState.draft=null;roleLifeState.message='';return true
}
function roleLifeRender(){
  if(state.current!=='role-life')return;
  const root=el('section_role_life');
  if(!root){render();return}
  const main=root.closest('.main'),scroll=main?.scrollTop;
  const focus=document.activeElement,focusId=root.contains(focus)?focus.id:'';
  let selection=null;try{if(focusId)selection=[focus.selectionStart,focus.selectionEnd]}catch(e){}
  const expanded=[...root.querySelectorAll('details[open][data-life-detail]')].map(n=>n.dataset.lifeDetail);
  const enteringId=root.querySelector?.('.subtab-enter')?.id||'';
  if('innerHTML'in root)root.innerHTML=renderRoleLifeInner();
  else if('outerHTML'in root)root.outerHTML=renderRoleLife();
  const replacement=el('section_role_life');
  replacement?.querySelectorAll('details[data-life-detail]').forEach(n=>{if(expanded.includes(n.dataset.lifeDetail))n.open=true});
  if(enteringId&&typeof animateSubtab==='function'){const panel=el(enteringId);if(panel)animateSubtab(panel)}
  if(focusId){const target=el(focusId);if(target){target.focus({preventScroll:true});if(selection&&selection[0]!=null)try{target.setSelectionRange(...selection)}catch(e){}}}
  if(main&&scroll!=null)main.scrollTop=scroll;
  if(typeof liquidGlassLensController!=='undefined')liquidGlassLensController.refresh();
}
function roleLifeTabKey(event){
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  const tabs=[...event.currentTarget.querySelectorAll('[role="tab"]')],index=tabs.indexOf(event.target);
  if(index<0)return;event.preventDefault();
  const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
  const id=tabs[next].id;tabs[next].click();el(id)?.focus({preventScroll:true})
}

function roleLifeRequestId(){
  try{if(globalThis.crypto&&globalThis.crypto.randomUUID)return String(globalThis.crypto.randomUUID())}catch(e){}
  return 'rl-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10)
}
function roleLifeUrl(path,extra={}){
  const d=roleLifeState.data||{},params=new URLSearchParams(Object.assign({
    account:roleLifeState.account||d.account||'',role:roleLifeState.role||d.role_id||''},extra));
  return path+'?'+params.toString()
}
const ROLE_LIFE_RECORD_TABS={activity:'activity',diary:'diary',dream:'dream',plans:'plans',jobs:'jobs'};
function roleLifeKindForTab(tab){return ROLE_LIFE_RECORD_TABS[tab]||null}
function roleLifeVersionPayload(extra={}){
  const s=(roleLifeState.data||{}).status||{};
  return Object.assign({
    account:roleLifeState.account||(roleLifeState.data||{}).account||'',
    role_id:roleLifeState.role||(roleLifeState.data||{}).role_id||'',
    expected_revision:String(s.revision||''),
    expected_epoch:Number(s.epoch??0),
    expected_state_version:Number(s.state_version??0),
    request_id:roleLifeRequestId()},extra)
}
function roleLifeEditable(){const d=roleLifeState.data;return !!(d&&d.editable&&!d.status?.corrupt&&!d.status?.read_error&&!roleLifeState.error&&!roleLifeState.loading&&!roleLifeBusy())}
function roleLifeVisibleRecord(){return roleLifeState.tab==='jobs'?'jobs':roleLifeState.tab==='experiences'?(roleLifeState.subTab||'activity'):roleLifeState.tab==='today'&&roleLifeState.todayTab==='plans'?'plans':null}
async function loadRoleLife(force=false,silent=false){
  if((roleLifeState.loading&&!force)||roleLifeState.saving)return;
  const generation=roleLifeState.generation||0,identity=roleLifeIdentity();
  const sequence=roleLifeState.sequence=(roleLifeState.sequence||0)+1;
  roleLifeState.loading=true;roleLifeState.error='';
  if(!silent)roleLifeRender();
  let accepted=false,changed=false;
  try{
    const q=new URLSearchParams();
    if(roleLifeState.account)q.set('account',roleLifeState.account);
    if(roleLifeState.role)q.set('role',roleLifeState.role);
    const data=await api('/api/role-life'+(q.toString()?'?'+q:''),{cache:'no-store'});
    if(!roleLifeCurrent(generation,identity)||roleLifeState.sequence!==sequence)return;
    changed=roleLifeState.data?.status?.state_version!==data.status?.state_version;
    if(roleLifeState.data&&(data.account!==roleLifeState.data.account||data.role_id!==roleLifeState.data.role_id)){
      roleLifeState.generation=(roleLifeState.generation||0)+1;
      roleLifeState.draft=null;roleLifeState.requests=Object.create(null);roleLifeState.pending=Object.create(null);
      roleLifeState.records={activity:null,diary:null,dream:null,plans:null,jobs:null};roleLifeState.page={activity:1,diary:1,dream:1,plans:1,jobs:1};
      changed=true;
    }
    roleLifeState.data=data;roleLifeState.selectorData=data;
    roleLifeState.account=data.account||'';roleLifeState.role=data.role_id||'';
    roleLifeState.updatedAt=Date.now();accepted=true;
    if(changed||force){
      for(const kind of Object.keys(ROLE_LIFE_RECORD_TABS)){
        (roleLifeState.recordSequence||(roleLifeState.recordSequence={}))[kind]=(roleLifeState.recordSequence[kind]||0)+1;
        if(roleLifeState.records[kind])roleLifeState.records[kind]={...roleLifeState.records[kind],stale:true,loading:false};
      }
    }
  }catch(e){if(roleLifeCurrent(generation,identity)&&roleLifeState.sequence===sequence)roleLifeState.error=String(e.message||e)}
  finally{
    if((accepted||(roleLifeCurrent(generation,identity)))&&roleLifeState.sequence===sequence){
      roleLifeState.loading=false;roleLifeRender();
      const kind=roleLifeVisibleRecord();
      if(accepted&&kind&&(force||changed||!roleLifeState.records[kind]))roleLifeLoadRecords(kind);
    }
  }
}
async function roleLifeSelect(account,role){
  if(!roleLifeLeave()){roleLifeRender();return}
  roleLifeState.selectorData=roleLifeState.data||roleLifeState.selectorData;
  roleLifeState.generation=(roleLifeState.generation||0)+1;
  roleLifeState.account=account;roleLifeState.role=role;roleLifeState.data=null;roleLifeState.error='';
  roleLifeState.requests=Object.create(null);roleLifeState.pending=Object.create(null);roleLifeState.message='';
  roleLifeState.planEditor=null;roleLifeState.expEditor=null;roleLifeState.noteEditor=false;roleLifeState.noteDraft='';
  roleLifeState.page={activity:1,diary:1,dream:1,plans:1,jobs:1};
  roleLifeState.records={activity:null,diary:null,dream:null,plans:null,jobs:null};roleLifeState.recordSequence={};
  await loadRoleLife(true)
}
function roleLifeSetTab(tab){
  if(!ROLE_LIFE_TABS.some(([key])=>key===tab))return;
  const changed=roleLifeState.tab!==tab;
  roleLifeState.tab=tab;roleLifeRender();
  const kind=roleLifeVisibleRecord();if(kind&&(!roleLifeState.records[kind]||roleLifeState.records[kind].stale))roleLifeLoadRecords(kind);
  if(changed){
    const panel=el('life-panel-'+tab);
    if(panel&&typeof animateSubtab==='function')animateSubtab(panel);
    el('section_role_life')?.querySelector('.life-main-tabs')?.scrollIntoView({block:'nearest'});
  }
}
function roleLifeSetSubTab(tab){
  if(!['activity','diary','dream'].includes(tab))return;
  const tabChanged=roleLifeState.tab!=='experiences',subChanged=roleLifeState.subTab!==tab;
  roleLifeState.tab='experiences';roleLifeState.subTab=tab;roleLifeRender();
  if(!roleLifeState.records[tab]||roleLifeState.records[tab].stale)roleLifeLoadRecords(tab);
  if(tabChanged||subChanged){
    const target=tabChanged?el('life-panel-experiences'):el('life-record-panel');
    if(target&&typeof animateSubtab==='function')animateSubtab(target);
  }
}
function roleLifeSetToday(tab){
  if(roleLifeState.tab!=='today'){roleLifeState.todayTab=tab;roleLifeSetTab('today');return}
  const changed=roleLifeState.todayTab!==tab;
  roleLifeState.todayTab=tab;roleLifeRender();
  const kind=roleLifeVisibleRecord();if(kind&&(!roleLifeState.records[kind]||roleLifeState.records[kind].stale))roleLifeLoadRecords(kind);
  if(changed){const panel=el('life-day-panel');if(panel&&typeof animateSubtab==='function')animateSubtab(panel)}
}
async function roleLifeLoadRecords(tab){
  const kind=roleLifeKindForTab(tab),status=roleLifeState.data?.status;
  if(!kind||!roleLifeState.data||status?.corrupt||status?.read_error||!roleLifeState.account||!roleLifeState.role)return;
  const page=roleLifeState.page[kind]||1,generation=roleLifeState.generation||0,identity=roleLifeIdentity();
  const sequences=roleLifeState.recordSequence||(roleLifeState.recordSequence={}),seq=sequences[kind]=(sequences[kind]||0)+1;
  roleLifeState.records[kind]={...(roleLifeState.records[kind]||{}),loading:true,error:''};roleLifeRender();
  const current=()=>roleLifeCurrent(generation,identity)&&roleLifeState.recordSequence[kind]===seq&&roleLifeState.page[kind]===page;
  try{
    const data=await api(roleLifeUrl('/api/role-life/records',{kind,page,page_size:20}),{cache:'no-store'});
    if(!current())return;
    if(data.corrupt||data.read_error||data.error)throw Error(data.error||(typeof data.read_error==='string'?data.read_error:'记录暂时无法读取'));
    roleLifeState.records[kind]={...data,loading:false,stale:false};
  }catch(e){if(current())roleLifeState.records[kind]={...roleLifeState.records[kind],loading:false,error:String(e.message||e),stale:true}}
  finally{if(current())roleLifeRender()}
}
function roleLifeSetPage(tab,delta){
  const kind=roleLifeKindForTab(tab);if(!kind)return;
  const cur=roleLifeState.records[kind]||{};
  const page=Math.max(1,(roleLifeState.page[kind]||cur.page||1)+delta);
  if(cur.pages&&page>cur.pages)return;
  roleLifeState.page[kind]=page;roleLifeLoadRecords(tab)
}
function roleLifeBaseConfig(){
  const d=roleLifeState.data||{};
  return JSON.parse(JSON.stringify(Object.assign({},d.default_config||{},d.config||{})))
}
// 草稿只存用户改过的键，渲染时叠加在服务端/默认配置之上；这样自动刷新
// 不会被“一渲染就生成草稿”误判成正在编辑。
function roleLifeDraftValues(){return Object.assign(roleLifeBaseConfig(),roleLifeState.draft||{})}
function roleLifeSetDraft(key,value){
  if(roleLifeState.saving)return;
  const draft={...(roleLifeState.draft||{})};
  if(value===roleLifeBaseConfig()[key])delete draft[key];else draft[key]=value;
  roleLifeState.draft=Object.keys(draft).length?draft:null;
  roleLifeMarkDraftDirty();
  if(['enabled','timezone','model_override','maintenance_enabled'].includes(key))roleLifeRender();
}
function roleLifeMarkDraftDirty(){
  const dirty=roleLifeHasDraft();
  roleLifeState.message=dirty?'设置有未保存修改，请点击右上角「保存设置」':'';roleLifeState.messageKind='';
  if(typeof state!=='undefined'&&state&&state.current==='role-life')state.dirty=dirty;
  const save=el('saveState');if(save)save.textContent=dirty?'有未保存修改':'已同步';
}
async function roleLifeSaveConfig(){
  if(!roleLifeEditable()||roleLifeState.saving)return;
  const d=roleLifeState.data||{},generation=roleLifeState.generation||0,identity=roleLifeIdentity();
  const cfg=Object.assign({},roleLifeState.draft||{});
  const expectedRev=String(d.config_revision||'');
  roleLifeState.saving=true;roleLifeState.message='正在保存…';roleLifeState.messageKind='';
  const saveEl=el('saveState');if(saveEl)saveEl.textContent='正在保存...';
  roleLifeRender();
  let conflict409=false;
  try{
    const data=await api('/api/role-life/config',{method:'POST',body:JSON.stringify({
      account:roleLifeState.account||d.account||'global',role_id:roleLifeState.role||d.role_id||'default',
      expected_config_revision:expectedRev,config:cfg})});
    if(!roleLifeCurrent(generation,identity))return;
    roleLifeState.data=data;roleLifeState.selectorData=data;roleLifeState.updatedAt=Date.now();
    roleLifeState.draft=null;roleLifeState.message='设置已保存';roleLifeState.messageKind='success';
    if(typeof state!=='undefined'&&state&&state.current==='role-life')state.dirty=false;
    const save=el('saveState');if(save)save.textContent='已同步';
    toast('角色生活设置已保存',true);
  }catch(e){if(roleLifeCurrent(generation,identity)){roleLifeState.message='保存失败：'+String(e.message||e);roleLifeState.messageKind='error';toast(roleLifeState.message,false);conflict409=Boolean(e&&Number(e.status)===409);const save=el('saveState');if(save)save.textContent=roleLifeHasDraft()?'有未保存修改':'已同步'}}
  finally{if(roleLifeCurrent(generation,identity)){roleLifeState.saving=false;roleLifeRender();if(conflict409)await loadRoleLife(true,true)}}
}
async function roleLifeAction(action,extra={}){
  // 损坏状态只允许“清空重建”（仍需页面二次确认）：其余操作一律只读。
  const d0=roleLifeState.data||{};
  const corruptClear=!!(action==='clear'&&d0.editable&&d0.status?.corrupt);
  if(!corruptClear&&!roleLifeEditable())return toast('当前只读，无法操作',false);
  const key=action+':'+(extra.job_id||extra.item_id||extra.experience_id||extra.note_id||extra.kind||'');
  if(Object.values(roleLifeState.pending).some(Boolean))return toast('操作进行中，请稍候',false);
  const generation=roleLifeState.generation||0,identity=roleLifeIdentity();
  roleLifeState.pending[key]=true;roleLifeState.message='正在提交…';roleLifeState.messageKind='';roleLifeRender();
  // 同一操作的网络失败重试必须复用同一个 request_id：服务端据此幂等，
  // 不会因为重发而重复入队或重复清空。成功后再丢弃。
  const requests=roleLifeState.requests||(roleLifeState.requests=Object.create(null));
  if(!requests[key])requests[key]=roleLifeRequestId();
  const okMessages={
    clear:'已清空该角色生活',upsert_plan_item:'已保存日程安排',delete_plan_item:'已删除日程安排',
    update_experience:'已保存生活记录',delete_experience:'已删除生活记录',
    add_life_note:'已添加生活备注',delete_life_note:'已删除备注',
  };
  try{
    await api('/api/role-life/action',{method:'POST',body:JSON.stringify(
      roleLifeVersionPayload(Object.assign({action,request_id:requests[key]},extra)))});
    if(!roleLifeCurrent(generation,identity))return;
    delete requests[key];roleLifeState.message=okMessages[action]||'已提交，任务完成后会更新在这里';roleLifeState.messageKind='success';
    toast(roleLifeState.message,true);
    if(action==='clear'){roleLifeState.records={activity:null,diary:null,dream:null,plans:null,jobs:null};roleLifeState.page={activity:1,diary:1,dream:1,plans:1,jobs:1};}
    if(action==='upsert_plan_item'||action==='delete_plan_item')roleLifeState.planEditor=null;
    if(action==='update_experience'||action==='delete_experience')roleLifeState.expEditor=null;
    if(action==='add_life_note'){roleLifeState.noteEditor=false;roleLifeState.noteDraft=''}
    await loadRoleLife(true);
    const vis=roleLifeVisibleRecord();
    if(vis&&(action==='update_experience'||action==='delete_experience'))await roleLifeLoadRecords(vis);
  }catch(e){if(roleLifeCurrent(generation,identity)){const rawErr=String(e.message||e||'请稍后重试').trim();roleLifeState.message=/^(操作失败|保存失败|删除失败|清空失败|未能|无法|请先|当前)/.test(rawErr)?rawErr:('操作失败：'+rawErr);roleLifeState.messageKind='error';toast(roleLifeState.message,false);if(e&&Number(e.status)===409){delete requests[key];await loadRoleLife(true,true)}}}
  finally{if(roleLifeCurrent(generation,identity)){delete roleLifeState.pending[key];roleLifeRender()}}
}
function roleLifeRegenerate(){return roleLifeAction('regenerate_plan')}
function roleLifeEnqueue(kind,args){return roleLifeAction('enqueue',args?{kind,args}:{kind})}
function roleLifeRetry(jobId){return roleLifeAction('retry',{job_id:String(jobId||'')})}
function roleLifeOpenPlanEditor(itemId=''){
  const items=(roleLifeState.data?.status?.plan?.items)||[];
  const found=itemId?items.find(i=>String(i.id)===String(itemId)):null;
  roleLifeState.expEditor=null;
  roleLifeState.planEditor=found?{
    item_id:String(found.id||''),title:String(found.title||''),
    purpose:String(found.purpose||''),
    start:roleLifeFmtTime(found.start,true),end:roleLifeFmtTime(found.end,true),
  }:{item_id:'',title:'',purpose:'',start:'09:00',end:'10:00'};
  roleLifeRender();
}
function roleLifeCancelPlanEditor(){roleLifeState.planEditor=null;roleLifeRender()}
async function roleLifeSavePlanEditor(){
  const title=String(el('lifePlanTitle')?.value??roleLifeState.planEditor?.title??'').trim();
  const start=String(el('lifePlanStart')?.value??roleLifeState.planEditor?.start??'').trim();
  const end=String(el('lifePlanEnd')?.value??roleLifeState.planEditor?.end??'').trim();
  const purpose=String(el('lifePlanPurpose')?.value??roleLifeState.planEditor?.purpose??'').trim();
  if(roleLifeState.planEditor){
    Object.assign(roleLifeState.planEditor,{title,start,end,purpose});
  }
  if(!title)return toast('请填写活动名称',false);
  if(!start||!end)return toast('请填写起止时间',false);
  await roleLifeAction('upsert_plan_item',{params:{
    item_id:String(roleLifeState.planEditor?.item_id||''),title,purpose,start,end,
  }});
}
async function roleLifeDeletePlanItem(itemId){
  if(!confirm('确定删除这个未开始的活动安排？'))return;
  await roleLifeAction('delete_plan_item',{item_id:String(itemId||'')});
}
async function roleLifeTriggerActivity(itemId){
  await roleLifeAction('trigger_activity',{item_id:String(itemId||'')});
}
function roleLifeFindExpText(expId){
  const recs=roleLifeState.records||{};
  for(const k of ['activity','diary','dream']){
    const hit=(recs[k]?.items||[]).find(r=>String(r.id)===String(expId));
    if(hit)return String(hit.text||'');
  }
  const recHit=(roleLifeState.data?.status?.recent||[]).find(r=>String(r.id)===String(expId));
  return recHit?String(recHit.text||''):'';
}
async function roleLifeOpenExpEditor(expId='',planItemId=''){
  roleLifeState.planEditor=null;
  let targetExpId=String(expId||'');
  if(!targetExpId&&planItemId){
    const item=(roleLifeState.data?.status?.plan?.items||[]).find(i=>String(i.id)===String(planItemId));
    if(item?.experience_id)targetExpId=String(item.experience_id||'');
  }
  let text=targetExpId?roleLifeFindExpText(targetExpId):'';
  if(targetExpId&&!text){
    try{
      let page=1,pages=1;
      do{
        const data=await api(roleLifeUrl('/api/role-life/records',{kind:'experiences',page,page_size:50}),{cache:'no-store'});
        if(data?.corrupt||data?.read_error||data?.error)throw Error(data.error||'读取记录失败');
        const hit=(data?.items||[]).find(r=>String(r.id)===targetExpId);
        if(hit){text=String(hit.text||'');break;}
        pages=Math.min(5,Math.max(1,Number(data?.pages||1)));
        page++;
      }while(page<=pages);
    }catch(e){
      toast('读取原经历失败，请稍后重试',false);
      return;
    }
    if(!text){toast('未找到原经历内容，请刷新后重试',false);return;}
  }
  roleLifeState.expEditor={experience_id:targetExpId,plan_item_id:String(planItemId||''),text};
  roleLifeRender();
}
function roleLifeCancelExpEditor(){roleLifeState.expEditor=null;roleLifeRender()}
async function roleLifeSaveExpEditor(){
  const text=String(el('lifeExpText')?.value??roleLifeState.expEditor?.text??'').trim();
  if(roleLifeState.expEditor)roleLifeState.expEditor.text=text;
  if(!text)return toast('内容不能为空',false);
  await roleLifeAction('update_experience',{params:{
    experience_id:String(roleLifeState.expEditor?.experience_id||''),
    plan_item_id:String(roleLifeState.expEditor?.plan_item_id||''),text,
  }});
}
async function roleLifeDeleteExperience(expId){
  if(!confirm('确定删除这条生活记录？'))return;
  await roleLifeAction('delete_experience',{experience_id:String(expId||'')});
}
function roleLifeOpenNoteEditor(){roleLifeState.noteEditor=true;roleLifeState.noteDraft='';roleLifeRender()}
function roleLifeCancelNoteEditor(){roleLifeState.noteEditor=false;roleLifeState.noteDraft='';roleLifeRender()}
async function roleLifeSaveNote(){
  const text=String(el('lifeNoteText')?.value??roleLifeState.noteDraft??'').trim();
  roleLifeState.noteDraft=text;
  if(!text)return toast('请填写备注内容',false);
  await roleLifeAction('add_life_note',{text});
}
async function roleLifeDeleteNote(noteId){
  await roleLifeAction('delete_life_note',{note_id:String(noteId||'')});
}
async function roleLifeClear(){
  const s=(roleLifeState.data||{}).status||{};
  const corrupt=!!s.corrupt;
  const text=corrupt
    ?'状态文件不可读，清空将先备份原文件再重建空白状态（不影响聊天记录）。请输入「清空」确认。'
    :'将清空此角色的日程、经历、睡眠、日记与梦（不影响聊天记录）。请输入「清空」确认。';
  const input=prompt(text);
  if(input!=='清空')return;
  await roleLifeAction('clear',corrupt?{allow_corrupt:true}:{})
}
function roleLifeTab(title,key){return `<button type="button" id="life-record-${key}" class="life-tab ${roleLifeState.subTab===key?'is-active':''}" role="tab" aria-controls="life-record-panel" tabindex="${roleLifeState.subTab===key?0:-1}" aria-selected="${roleLifeState.subTab===key}" onclick="roleLifeSetSubTab('${key}')">${title}</button>`}
function roleLifeZone(){return roleLifeState.data?.config?.timezone||roleLifeState.data?.default_config?.timezone||'Asia/Shanghai'}
function roleLifeFmtTime(ts,timeOnly=false){
  const n=Number(ts);if(!Number.isFinite(n)||n<=0)return '—';
  const opts={timeZone:roleLifeZone(),hour:'2-digit',minute:'2-digit',hourCycle:'h23'};
  if(!timeOnly)Object.assign(opts,{month:'2-digit',day:'2-digit'});
  try{return new Intl.DateTimeFormat('zh-CN',opts).format(new Date(n*1000))}catch(e){return '时间不可用'}
}
function roleLifeIcon(name){
  const shapes={sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',moon:'<path d="M20 14a8 8 0 0 1-10-10 8 8 0 1 0 10 10Z"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',book:'<path d="M12 5v15M3 4h5a4 4 0 0 1 4 2 4 4 0 0 1 4-2h5v15h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3Z"/>',refresh:'<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 14 6M4 12a8 8 0 0 0 14 6"/>',alert:'<path d="M12 3 2 21h20Z"/><path d="M12 9v5m0 3v1"/>',leaf:'<path d="M20 3C8 2 3 8 5 15s15 6 15-12Z"/><path d="M4 21 15 10"/>'};
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shapes[name]||shapes.book}</svg>`
}
function roleLifeEmpty(title,detail,action='',icon='book'){
  return `<div class="life-empty"><span class="life-empty-icon">${roleLifeIcon(icon)}</span><h3>${esc(title)}</h3><p>${esc(detail)}</p>${action?`<div class="life-actions">${action}</div>`:''}</div>`
}
function roleLifeNotice(text,type='info'){
  return `<div class="life-notice ${type==='error'?'is-error':type==='success'?'is-success':''}" role="${type==='error'?'alert':'status'}"><span class="life-notice-icon">${roleLifeIcon(type==='error'?'alert':'leaf')}</span><p>${esc(text)}</p></div>`
}
function roleLifeBusy(){return roleLifeState.saving||Object.values(roleLifeState.pending||{}).some(Boolean)}
function roleLifeActionButton(label,call,{primary=false,enabled=true,key=''}={}){
  const pending=!!roleLifeState.pending?.[key];
  return `<button type="button" class="btn ${primary?'primary':''}" ${!enabled||roleLifeBusy()?'disabled':''} aria-busy="${pending}" onclick="${call}">${pending?'正在提交…':esc(label)}</button>`
}
function roleLifeJobLabel(kind){return ({plan:'安排日程',activity:'记录经历',diary:'写日记',dream:'记录梦境',maintenance:'更新状态'})[kind]||'生活任务'}
function roleLifePager(kind,data){
  const page=Number(data.page||1),pages=Math.max(1,Number(data.pages||1));
  if(pages<=1)return '';
  return `<nav class="life-pager" aria-label="记录翻页"><button type="button" class="btn" ${page<=1||data.loading?'disabled':''} onclick="roleLifeSetPage('${kind}',-1)">上一页</button><span>第 ${page} / ${pages} 页</span><button type="button" class="btn" ${page>=pages||data.loading?'disabled':''} onclick="roleLifeSetPage('${kind}',1)">下一页</button></nav>`
}
function roleLifeRecordBody(record){
  const text=String(record.text||''),detail=esc(String(record.id||record.created_at||text.slice(0,24)));
  return text.length>320?`<details class="life-reading" data-life-detail="${detail}"><summary><span class="life-excerpt">${esc(text.slice(0,180))}…</span><span class="life-reading-toggle">展开全文</span></summary><div class="life-prose">${esc(text)}</div></details>`:`<div class="life-prose">${esc(text||'暂无正文')}</div>`
}
function roleLifeRecordCard(record,editableRecords=false){
  const dream=record.dream||record.kind==='dream';
  const recId=String(record.id||'');
  const editing=editableRecords&&roleLifeState.expEditor&&String(roleLifeState.expEditor.experience_id)===recId&&recId;
  const actions=editableRecords&&recId?`<div class="life-item-actions"><button type="button" class="life-mini-btn" data-exp-id="${esc(recId)}" onclick="roleLifeOpenExpEditor(this.dataset.expId,'')">编辑</button><button type="button" class="life-mini-btn is-danger" data-exp-id="${esc(recId)}" onclick="roleLifeDeleteExperience(this.dataset.expId)">删除</button></div>`:'';
  const editor=editing?roleLifeExpEditorHtml():'';
  return `<article class="life-record"><div class="life-record-head"><span class="life-record-kind">${roleLifeIcon(dream?'moon':'book')}${esc(roleLifeKindLabel(record.kind,dream))}</span><time>${esc(record.date||'')}${record.created_at?' · '+esc(roleLifeFmtTime(record.created_at,true)):''}</time></div>${editing?editor:roleLifeRecordBody(record)}${editing?'':actions}</article>`
}
function roleLifeExpEditorHtml(){
  const ed=roleLifeState.expEditor;if(!ed)return '';
  return `<div class="life-inline-form"><label for="lifeExpText">编辑内容</label><textarea id="lifeExpText" maxlength="1200" placeholder="写下这次的经过、感受或修改后的正文…" oninput="if(roleLifeState.expEditor)roleLifeState.expEditor.text=this.value">${esc(ed.text||'')}</textarea><div class="life-actions"><button type="button" class="btn life-btn-accent" onclick="roleLifeSaveExpEditor()">保存</button><button type="button" class="btn" onclick="roleLifeCancelExpEditor()">取消</button></div></div>`
}
function roleLifeTimeline(items,compact=false,allowManage=false){
  const sorted=[...items].sort((a,b)=>Number(a.start||0)-Number(b.start||0));
  const enabled=!!roleLifeState.data?.status?.enabled;
  const activeExp=roleLifeState.expEditor;
  return `<ol class="life-timeline ${compact?'is-compact':''}">${sorted.map(item=>{
    const itemId=String(item.id||'');
    const st=String(item.status||'');
    const canEditPlan=allowManage&&itemId&&st==='planned';
    const canGen=allowManage&&enabled&&itemId&&!item.experience_id&&['planned','active','interrupted'].includes(st);
    const canWriteExp=allowManage&&itemId;
    const itemActions=allowManage&&itemId?`<div class="life-item-actions">${canEditPlan?`<button type="button" class="life-mini-btn" data-item-id="${esc(itemId)}" onclick="roleLifeOpenPlanEditor(this.dataset.itemId)">编辑</button><button type="button" class="life-mini-btn is-danger" data-item-id="${esc(itemId)}" onclick="roleLifeDeletePlanItem(this.dataset.itemId)">删除</button>`:''}${canGen?`<button type="button" class="life-mini-btn" data-item-id="${esc(itemId)}" onclick="roleLifeTriggerActivity(this.dataset.itemId)">生成经历</button>`:''}${canWriteExp?`<button type="button" class="life-mini-btn" data-exp-id="${esc(item.experience_id||'')}" data-item-id="${esc(itemId)}" onclick="roleLifeOpenExpEditor(this.dataset.expId,this.dataset.itemId)">${item.experience_id?'编辑经历':'手写经历'}</button>`:''}</div>`:'';
    const inlineExp=allowManage&&activeExp&&String(activeExp.plan_item_id)===itemId&&itemId?roleLifeExpEditorHtml():'';
    return `<li class="life-timeline-item ${st==='active'?'is-current':''}"><div class="life-timeline-time"><time>${esc(roleLifeFmtTime(item.start,true))}</time><span>${esc(roleLifeFmtTime(item.end,true))}</span></div><span class="life-timeline-dot" aria-hidden="true"></span><div class="life-timeline-content"><div class="life-row-head"><h4>${esc(item.title||'未命名活动')}</h4>${roleLifeStatusPill(item.status)}</div>${item.purpose&&!compact?`<p>${esc(item.purpose)}</p>`:''}${item.experience_id?'<span class="life-meta">经历已保存</span>':''}${itemActions}${inlineExp}</div></li>`;
  }).join('')}</ol>`
}
// 用量未知字段既可能来自 status 顶层，也可能只在 usage 里；两处都认，避免
// 内核只在一个位置暴露时页面退化成“0 次”。
function roleLifeBudgetUnknown(s){
  s=s||{};const u=s.usage||{};
  // 完全读不出状态（corrupt）时用量天然未知，绝不当 0。
  return !!(s.budget_unknown||u.budget_unknown||s.corrupt||u.corrupt
    ||u.role_24h===null)
}
function roleLifeBudgetUnknownUntil(s){
  s=s||{};const u=s.usage||{};
  return Number(s.budget_unknown_until||u.budget_unknown_until||0)||0
}
function roleLifeCorruptBackup(s){
  s=s||{};return String(s.corrupt_backup||'')
}
function roleLifeStatusPill(status){
  const map={planned:['未开始',''],active:['进行中','ok'],completed:['已完成','ok'],
    interrupted:['已中断','bad'],skipped:['已跳过',''],pending:['排队中',''],running:['生成中','ok'],
    succeeded:['成功','ok'],failed:['失败','bad'],unknown:['结果未知','bad'],retried:['已重试','']};
  const [text,cls]=map[status]||[status||'未知',''];
  return `<span class="pill ${cls}">${esc(text)}</span>`
}
function roleLifeOverview(){
  const d=roleLifeState.data||{},s=d.status||{},next=s.next_node,activity=s.current_activity;
  if(s.corrupt){
    const until=roleLifeBudgetUnknownUntil(s);
    return `<section class="card">${roleLifeEmpty('状态不可读','文件损坏或身份不符，不会当成空数据继续运行。','','alert')}${roleLifeNotice(s.error||'状态文件不可读','error')}<p class="life-caption">${until?'生成已保护性暂停至 '+esc(roleLifeFmtTime(until))+'；':''}清空前会先备份原文件。</p><div class="life-actions">${roleLifeActionButton('清空并重建（二次确认）','roleLifeClear()',{enabled:d.editable,key:'clear:'})}</div></section>`
  }
  if(s.read_error)return `<section class="card">${roleLifeEmpty('暂时读不到状态','读取文件或连接超时，请稍后重新读取。','','alert')}${roleLifeNotice(s.error||(typeof s.read_error==='string'?s.read_error:'读取失败'),'error')}<div class="life-actions"><button type="button" class="btn" onclick="loadRoleLife(true)">重新读取</button></div></section>`;
  const items=Array.isArray(s.plan?.items)?s.plan.items:[],recent=Array.isArray(s.recent)?s.recent:[];
  const jobs=Array.isArray(s.jobs)?s.jobs:[],failed=jobs.filter(j=>['failed','unknown'].includes(j.status));
  const enabled=!!s.enabled,unknown=roleLifeBudgetUnknown(s),until=roleLifeBudgetUnknownUntil(s),backup=roleLifeCorruptBackup(s);
  const draftCfg=roleLifeDraftValues();
  const completedCount=items.filter(i=>i.status==='completed').length;
  const headline=!enabled?'角色生活已关闭':s.sleeping?'正在休息':activity?.title||'暂时空闲';
  const intro=!enabled?'打开上方「角色生活总开关」并点右上角「保存设置」后，机器人会按当前人设和作息默默过日子。':s.sleeping?'休息期间暂不主动搭话，发来的消息仍会照常回复。':activity?.purpose||'按作息安排推进，空余时间会慢慢留下新片段。';
  const openSettings='<button type="button" class="btn" onclick="roleLifeSetTab(\'settings\')">调整作息与偏好</button>';
  const sortedItems=items.slice().sort((a,b)=>Number(a.start)-Number(b.start));
  const heroMiddle=enabled?`<div class="life-hero-stats">
      <div class="life-stat"><span class="life-meta">今日进度</span><strong>${items.length?`已完成 ${completedCount} / ${items.length} 项`:'等待排程'}</strong></div>
      <div class="life-stat"><span class="life-meta">作息设置</span><strong>${draftCfg.sleep_enabled?`${esc(draftCfg.sleep_start||'00:30')} · 睡 ${esc(draftCfg.sleep_hours??8)} 小时`:'未开启睡眠'}</strong></div>
      <div class="life-stat"><span class="life-meta">后台运行</span><strong>${d.runtime_running===false?'仅 WebUI':'自动推进中'}</strong></div>
    </div>`:`<div class="life-guide-list">
      <div class="life-guide-item"><strong>1. 自己过日子</strong><span>按作息与偏好自动排日程、记录活动经过、晚上写简短日记，醒来偶有梦境。</span></div>
      <div class="life-guide-item"><strong>2. 聊天更有活人感</strong><span>正常聊天和主动搭话时适度带出近况与心情，不刷屏、不每轮生硬汇报行程。</span></div>
      <div class="life-guide-item"><strong>3. 随时可控</strong><span>总开关关闭时不运行后台任务、不消耗模型额度；切换人设时原有生活记录也会保留。</span></div>
    </div>`;
  const alerts=[
    unknown?roleLifeNotice('角色用量未知，已保护性暂停生成'+(until?'至 '+roleLifeFmtTime(until):'')+'。','error'):'',
    backup?roleLifeNotice('已从损坏状态恢复，原件已备份为 '+backup,'info'):'',
  ].filter(Boolean).join('');
  return `${alerts}<div class="life-overview-grid">
    <section class="card life-hero ${enabled?'is-enabled':'is-paused'}"><div class="life-eyebrow">${roleLifeIcon(s.sleeping?'moon':'sun')}<span>${enabled?(s.sleeping?'此刻 · 睡眠中':'此刻 · 清醒'):'尚未启用'}</span><span class="pill">${esc(s.date||'角色日常')}</span></div>
      <h3>${esc(headline)}</h3><p class="life-lead">${esc(intro)}</p>
      ${s.mood?.mood||s.mood?.energy?`<div class="life-mood"><span class="life-mood-dot" aria-hidden="true"></span><span>${esc(s.mood.mood||'')}${s.mood.energy?' · 精力 '+esc(s.mood.energy):''}</span></div>`:''}
      ${heroMiddle}
      <div class="life-hero-footer"><div class="life-next"><span class="life-meta">${s.sleeping?'预计醒来':'接下来'}</span><strong>${s.sleeping?esc(roleLifeFmtTime(s.sleep?.end)):next?esc(next.label):'等待新的安排'}</strong>${!s.sleeping&&next?.at?`<time>${esc(roleLifeFmtTime(next.at))}</time>`:''}</div>
      ${enabled?'<button type="button" class="btn primary" onclick="roleLifeSetTab(\'today\')">看看今天</button>':d.editable?openSettings:'<span class="pill">仅可查看</span>'}</div></section>
    <section class="card life-today-preview"><div class="section-head"><div><h3>今天的安排</h3><p>${items.length?'今日行程概览':'今天还没有安排。'}</p></div><span class="pill">${items.length} 项</span></div>
      ${items.length?roleLifeTimeline(sortedItems.slice(0,5),true):roleLifeEmpty('今天还没有安排',enabled?'后台会按时间生成，也可以去日程页重排。':'开启角色生活后，这里会显示今日安排。','','clock')}
      ${items.length>5?'<button type="button" class="life-text-button" onclick="roleLifeSetTab(\'today\')">查看完整日程 →</button>':''}</section>
    <section class="card life-recent-preview"><div class="section-head"><div><h3>最近留下的片段</h3><p>活动经历、日记与梦境</p></div><button type="button" class="life-text-button" onclick="roleLifeSetTab('experiences')">全部记录 →</button></div>
      ${recent.length?`<div class="life-recent-grid">${recent.slice(0,3).map(r=>roleLifeRecordCard(r,false)).join('')}</div>`:roleLifeEmpty('还没有生活片段','活动结束后，留下的经历、日记和梦境会出现在这里。')}
    </section>
    ${failed.length?`<section class="card life-alert-bar"><div class="life-row-head"><div><h4>有 ${failed.length} 个后台任务未能完成</h4><p class="life-caption">可在任务页查看原因或手动重试。</p></div><button type="button" class="btn" onclick="roleLifeSetTab('jobs')">查看任务</button></div></section>`:''}
  </div>`
}
function roleLifeKindLabel(kind,dream){
  if(dream||kind==='dream')return '梦境';
  if(kind==='diary')return '日记';
  if(kind==='activity')return '经历';
  return String(kind||'记录')
}
function roleLifePlanEditorHtml(){
  const ed=roleLifeState.planEditor;if(!ed)return '';
  return `<div class="life-inline-form"><h4>${ed.item_id?'编辑日程安排':'新增日程安排'}</h4><div class="life-inline-grid"><div class="life-setting-field"><label for="lifePlanTitle">活动名称</label><input id="lifePlanTitle" type="text" maxlength="80" value="${esc(ed.title||'')}" placeholder="例如：整理书桌" oninput="if(roleLifeState.planEditor)roleLifeState.planEditor.title=this.value"></div><div class="life-inline-times"><div class="life-setting-field"><label for="lifePlanStart">开始</label><input id="lifePlanStart" type="time" value="${esc(ed.start||'09:00')}" oninput="if(roleLifeState.planEditor)roleLifeState.planEditor.start=this.value"></div><div class="life-setting-field"><label for="lifePlanEnd">结束</label><input id="lifePlanEnd" type="time" value="${esc(ed.end||'10:00')}" oninput="if(roleLifeState.planEditor)roleLifeState.planEditor.end=this.value"></div></div></div><div class="life-setting-field"><label for="lifePlanPurpose">目的/备注（可选）</label><input id="lifePlanPurpose" type="text" maxlength="200" value="${esc(ed.purpose||'')}" placeholder="简短说明想做的事" oninput="if(roleLifeState.planEditor)roleLifeState.planEditor.purpose=this.value"></div><div class="life-actions"><button type="button" class="btn life-btn-accent" onclick="roleLifeSavePlanEditor()">保存安排</button><button type="button" class="btn" onclick="roleLifeCancelPlanEditor()">取消</button></div></div>`
}
function roleLifeNotesCard(editable){
  const notes=Array.isArray(roleLifeState.data?.status?.life_notes)?roleLifeState.data.status.life_notes:[];
  const open=!!roleLifeState.noteEditor;
  const editor=open?`<div class="life-inline-form"><label for="lifeNoteText">记一条生活备注（仅用于排日程和写日记）</label><input id="lifeNoteText" type="text" maxlength="120" value="${esc(roleLifeState.noteDraft||'')}" placeholder="例如：周末想去公园散步看枫叶" oninput="roleLifeState.noteDraft=this.value"><div class="life-actions"><button type="button" class="btn life-btn-accent" onclick="roleLifeSaveNote()">保存备注</button><button type="button" class="btn" onclick="roleLifeCancelNoteEditor()">取消</button></div></div>`:'';
  return `<section class="card life-notes-card"><div class="section-head"><div><h3>生活备注</h3><p>手动记下给角色的日常小提示，供排日程和写日记参考（不会自动抓取聊天内容）。</p></div>${editable&&!open?'<button type="button" class="btn" onclick="roleLifeOpenNoteEditor()">+ 记一条备注</button>':''}</div>${editor}${notes.length?`<ul class="life-notes-list">${notes.map(n=>`<li class="life-note-row"><div><span class="life-prose">${esc(n.text||'')}</span><span class="life-meta">${esc(n.date||'')} · 手动添加</span></div>${editable?`<button type="button" class="life-mini-btn is-danger" data-note-id="${esc(n.id||'')}" onclick="roleLifeDeleteNote(this.dataset.noteId)">删除</button>`:''}</li>`).join('')}</ul>`:!open?'<p class="life-caption">还没有备注，手动添加后会出现在这里。</p>':''}</section>`
}
function roleLifeTodayTab(){
  const s=roleLifeState.data?.status||{},items=Array.isArray(s.plan?.items)?s.plan.items:[];
  const history=roleLifeState.todayTab==='plans',editable=roleLifeEditable();
  const tabs=`<div class="life-subtabs" role="tablist" aria-label="日程范围" onkeydown="roleLifeTabKey(event)">${[['today','今天'],['plans','历史日程']].map(([key,title])=>`<button type="button" id="life-day-${key}" class="life-tab ${history===(key==='plans')?'is-active':''}" role="tab" aria-controls="life-day-panel" aria-selected="${history===(key==='plans')}" tabindex="${history===(key==='plans')?0:-1}" onclick="roleLifeSetToday('${key}')">${title}</button>`).join('')}</div>`;
  return tabs+`<div id="life-day-panel" role="tabpanel" aria-labelledby="life-day-${history?'plans':'today'}">${history?roleLifePlansTab():`<section class="card"><div class="section-head"><div><h3>${esc(s.date||'今天')} · 一天的安排</h3><p>时间按 ${esc(roleLifeZone())} 显示</p></div><div class="life-actions"><span class="pill">${items.length} 项</span>${editable?'<button type="button" class="btn" onclick="roleLifeOpenPlanEditor(\'\')">+ 添加安排</button>':''}</div></div>${roleLifePlanEditorHtml()}
    ${items.length?roleLifeTimeline(items,false,editable):roleLifeEmpty('今天还没有安排',s.enabled?'可等待自动安排、手动添加或重排今天剩余时段。':'开启角色生活后即可自动生成日程，也可以现在手动添加。','','clock')}
    <div class="life-actions life-section-footer">${roleLifeActionButton('重排今天剩余安排','roleLifeRegenerate()',{primary:true,enabled:editable&&s.enabled,key:'regenerate_plan:'})}<span class="life-caption">只调整还没开始的活动，保留正在进行和已完成的记录。</span></div></section>${roleLifeNotesCard(editable)}`}</div>`
}
function roleLifeRecordsState(kind){
  const data=roleLifeState.records[kind];
  if(!data||data.loading&&!Array.isArray(data.items))return '<div class="life-loading" role="status"><span class="life-loader" aria-hidden="true"></span>正在整理记录…</div>';
  if(data.error)return roleLifeNotice('记录读取失败：'+data.error,'error')+`<div class="life-actions"><button type="button" class="btn" onclick="roleLifeLoadRecords('${kind}')">重新读取</button></div>`;
  if(data.loading)return '<p class="life-caption" role="status">正在更新，下面暂为上次读取的内容。</p>';
  if(data.stale)return '<p class="life-caption">下面是上次读取的内容。</p>';
  return ''
}
function roleLifeRecordList(title,desc,kind){
  const data=roleLifeState.records[kind],items=Array.isArray(data?.items)?data.items:[];
  const canWrite=roleLifeEditable()&&roleLifeState.data?.status?.enabled;
  const editableRecords=roleLifeEditable();
  const action=kind==='diary'?roleLifeActionButton('写日记',"roleLifeEnqueue('diary')",{enabled:canWrite,key:'enqueue:diary'}):kind==='dream'?roleLifeActionButton('生成梦',"roleLifeEnqueue('dream')",{enabled:canWrite,key:'enqueue:dream'}):'';
  return `<section class="card" aria-busy="${!!data?.loading}"><div class="section-head"><div><h3>${esc(title)}</h3><p>${esc(desc)}</p></div>${action||`<span class="pill">${Number(data?.total||0)} 条</span>`}</div>${roleLifeRecordsState(kind)}
    ${items.length?`<div class="life-record-list">${items.map(r=>roleLifeRecordCard(r,editableRecords)).join('')}</div>`:data&&!data.loading&&!data.error?roleLifeEmpty('还没有'+title,kind==='dream'?'睡醒后生成的梦境会留在这里。':kind==='diary'?'一天的活动记录整理成日记后会显示在这里。':'活动结束后留下的片段会显示在这里。'):''}
    ${data?roleLifePager(kind,data):''}</section>`
}
function roleLifePlansTab(){
  const data=roleLifeState.records.plans,items=Array.isArray(data?.items)?data.items:[],days=new Map();
  for(const item of items){const day=item.date||'历史日程';if(!days.has(day))days.set(day,[]);days.get(day).push(item)}
  return `<section class="card" aria-busy="${!!data?.loading}"><div class="section-head"><div><h3>走过的日程</h3><p>按日期回顾过去的日程安排。</p></div></div>${roleLifeRecordsState('plans')}
    ${items.length?[...days].map(([day,records])=>`<section class="life-history-day"><h4>${esc(day)}</h4>${roleLifeTimeline(records)}</section>`).join(''):data&&!data.loading&&!data.error?roleLifeEmpty('还没有历史日程','开启生活后，过去的安排会按日期保留。','','clock'):''}${data?roleLifePager('plans',data):''}</section>`
}
function roleLifeJobsTab(){
  const data=roleLifeState.records.jobs,items=Array.isArray(data?.items)?data.items:[];
  return `<section class="card" aria-busy="${!!data?.loading}"><div class="section-head"><div><h3>后台任务</h3><p>查看日程、经历、日记与梦的生成记录。</p></div></div>${roleLifeRecordsState('jobs')}
    ${items.length?`<div class="life-job-list">${items.map(job=>`<article class="life-job"><div class="life-row-head"><div><h4>${esc(roleLifeJobLabel(job.kind))}</h4><p class="life-caption">${esc(roleLifeFmtTime(job.created_at))} · 尝试 ${esc(job.attempts||0)} 次</p></div>${roleLifeStatusPill(job.status)}</div>
      ${job.error?`<details class="life-job-error" data-life-detail="job-${esc(job.id)}"><summary>${job.status==='unknown'?'请求结果不明，不会自动重发':'查看失败原因'}</summary><pre>${esc(job.error)}</pre></details>`:''}
      ${['failed','unknown'].includes(job.status)?`<div class="life-actions"><button type="button" class="btn" ${roleLifeEditable()&&!roleLifeBusy()&&roleLifeState.data?.status?.enabled?'':'disabled'} data-job-id="${esc(job.id)}" onclick="roleLifeRetry(this.dataset.jobId)">${roleLifeState.pending?.['retry:'+job.id]?'正在提交…':'重试任务'}</button></div>`:''}</article>`).join('')}</div>`:data&&!data.loading&&!data.error?roleLifeEmpty('后台还很安静','开启生活后，生成的日程、经历、日记和梦会显示在这里。'):''}${data?roleLifePager('jobs',data):''}</section>`
}
function roleLifeBooleanField(label,key,desc){
  const value=!!roleLifeDraftValues()[key];
  return `<div class="life-toggle-row"><div><label id="life-label-${key}" for="life-field-${key}">${esc(label)}</label><p id="life-help-${key}" class="life-caption">${esc(desc)}</p></div><button type="button" id="life-field-${key}" class="switch ${value?'on':''}" role="switch" aria-checked="${value}" aria-labelledby="life-label-${key}" aria-describedby="life-help-${key}" ${roleLifeEditable()?'':'disabled'} onclick="roleLifeToggle('${key}',this)"></button></div>`
}
function roleLifeToggle(key,elNode){
  if(!roleLifeEditable())return;
  const next=!roleLifeDraftValues()[key];
  roleLifeSetDraft(key,next);
  if(elNode){elNode.classList.toggle('on',next);elNode.setAttribute('aria-checked',String(next))}
}
function roleLifeNumberField(label,key,desc,min,max,step){
  const v=roleLifeDraftValues()[key]??'';
  return `<div class="life-setting-field"><label for="life-field-${key}">${esc(label)}</label><input id="life-field-${key}" type="number" ${roleLifeEditable()?'':'disabled'} min="${min}" max="${max}" step="${step||1}" value="${esc(v)}" aria-describedby="life-help-${key}" oninput="roleLifeSetDraft('${key}',this.value===''?'':Number(this.value))"><p class="life-caption" id="life-help-${key}">${esc(desc)}</p></div>`
}
function roleLifeTextField(label,key,desc,placeholder){
  const v=roleLifeDraftValues()[key]??'',type=['sleep_start','diary_time'].includes(key)?'time':'text';
  return `<div class="life-setting-field"><label for="life-field-${key}">${esc(label)}</label><input id="life-field-${key}" type="${type}" ${roleLifeEditable()?'':'disabled'} value="${esc(v)}" placeholder="${esc(placeholder||'')}" aria-describedby="life-help-${key}" oninput="roleLifeSetDraft('${key}',this.value)"><p class="life-caption" id="life-help-${key}">${esc(desc)}</p></div>`
}
function roleLifeSelectField(label,key,desc,options){
  const v=String(roleLifeDraftValues()[key]??'');
  return `<div class="life-setting-field"><label for="life-field-${key}">${esc(label)}</label><select id="life-field-${key}" ${roleLifeEditable()?'':'disabled'} aria-describedby="life-help-${key}" onchange="roleLifeSetDraft('${key}',this.value)"><option value="">跟随现有判断/对话模型轮换</option>${options.map(o=>`<option value="${esc(o.value||o)}" ${String(o.value||o)===v?'selected':''}>${esc(o.label||o)}</option>`).join('')}${v&&!options.some(o=>String(o.value||o)===v)?`<option value="${esc(v)}" selected>${esc(v)}（当前保存值不可用，将暂停）</option>`:''}</select><p class="life-caption" id="life-help-${key}">${esc(desc)}</p></div>`
}
function roleLifeSettingsTab(){
  const editable=roleLifeEditable(),draft=roleLifeDraftValues();
  const models=(roleLifeState.data?.models||[]).map(m=>({value:m.label,label:m.label}));
  const tzOptions=['Asia/Shanghai','Asia/Tokyo','Asia/Singapore','Europe/London','America/New_York','America/Los_Angeles','UTC'];
  return `<div class="life-settings-layout"><div class="life-settings-main">
    <section class="card"><div class="section-head"><div><h3>日常里有什么</h3><p>按需开启各项日常内容，由页面顶部总开关统一启停。</p></div></div><div class="life-toggle-grid">
      ${roleLifeBooleanField('每日日程','plan_enabled','为当天安排符合人设的活动')}
      ${roleLifeBooleanField('活动经历','activity_enabled','活动结束后留下经过和感受')}
      ${roleLifeBooleanField('睡眠','sleep_enabled','按作息进入睡眠时段；睡着时不主动搭话，发来的消息仍照常回复')}
      ${roleLifeBooleanField('日记','diary_enabled','按当天经历写一篇简短日记')}
      ${roleLifeBooleanField('梦','dream_enabled','睡醒后生成一段梦境记录')}
      ${roleLifeBooleanField('情绪影响回复语气','mood_tone_enabled','把当前心情、精力和刚醒状态柔和融入日常回复')}
      ${roleLifeBooleanField('节假日与季节感知','env_awareness_enabled','安排日程和写日记时参考星期、季节和节假日')}
    </div></section>
    <section class="card"><div class="section-head"><div><h3>作息与节奏</h3><p>按角色自己的时区安排睡眠与日记时间。</p></div></div><div class="life-form-grid life-form-grid-auto">
      <div class="life-setting-field"><label for="life-field-timezone">时区</label><select id="life-field-timezone" ${editable?'':'disabled'} onchange="roleLifeSetDraft('timezone',this.value)">${tzOptions.map(t=>`<option value="${esc(t)}" ${draft.timezone===t?'selected':''}>${esc(t)}</option>`).join('')}${draft.timezone&&!tzOptions.includes(draft.timezone)?`<option value="${esc(draft.timezone)}" selected>${esc(draft.timezone)}</option>`:''}</select><p class="life-caption">日程和时间显示都按此时区。</p></div>
      ${roleLifeNumberField('每天安排几件事','activities_per_day','建议 4～6 项，最多 12 项。',1,12)}
      ${roleLifeTextField('准备睡觉的时间','sleep_start','24 小时制，例如 00:30。','00:30')}
      ${roleLifeNumberField('睡多久（小时）','sleep_hours','允许 1～12 小时。',1,12)}
      ${roleLifeTextField('写日记的时间','diary_time','整理当天已经留下的经历。','23:50')}
    </div></section>
    <section class="card"><div class="section-head"><div><h3>生活偏好</h3><p>给它一点方向，别把一天规定得太满。</p></div></div><div class="life-setting-field"><label for="roleLifePreference">喜欢怎样生活</label><textarea id="roleLifePreference" maxlength="500" ${editable?'':'disabled'} placeholder="例如：喜欢安静的早晨、做饭和散步，周末给自己多留一点空闲。" oninput="roleLifeSetDraft('preference',this.value)">${esc(draft.preference||'')}</textarea><p class="life-caption">最多 500 字，作为角色安排日常时的参考。</p></div></section>
    <details class="card life-advanced" data-life-detail="advanced"><summary><div><h3>高级设置</h3><p>调整使用的模型、状态更新与记录保留。</p></div><span class="life-advanced-caret" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span></summary><div class="life-advanced-content">
      <div class="life-form-grid">${roleLifeSelectField('生活模型','model_override','留空则沿用规划器/对话模型；指定模型不可用时暂不生成。',models)}${roleLifeNumberField('记录保留天数','retention_days','经历、日记、梦和备注的保留天数。',1,365)}</div>
      ${roleLifeBooleanField('定期状态更新','maintenance_enabled','默认关闭，开启后定期更新心情与精力。')}
      <div class="life-form-grid">${roleLifeNumberField('状态更新间隔（秒）','maintenance_interval_seconds','开启定期更新后生效，最短 300 秒。',300,86400)}</div>
    </div></details>
    <section class="life-danger-zone"><div><h3>清空角色生活</h3><p>删除日程、经历、日记、梦与备注，不影响聊天记录。</p></div><button type="button" class="life-text-button is-danger" ${editable&&!roleLifeBusy()?'':'disabled'} onclick="roleLifeClear()">清空生活…</button></section>
  </div></div>`
}
function roleLifeDraftReset(){
  if(roleLifeState.saving)return;
  roleLifeState.draft=null;roleLifeState.message='';roleLifeState.messageKind='';
  if(typeof state!=='undefined'&&state&&state.current==='role-life')state.dirty=false;
  const save=el('saveState');if(save)save.textContent='已同步';
  roleLifeRender()
}
function roleLifeSelector(){return ''}
function renderRoleLifeInner(){
  const d=roleLifeState.data;
  if(!d&&roleLifeState.loading)return `<div class="life-loading-page"><span class="life-loader"></span><strong>正在读取角色生活</strong><p>正在加载本地生活档案…</p></div>`;
  if(!d)return `<div class="life-error-page">${roleLifeNotice(roleLifeState.error||'角色生活暂时无法读取','error')}<button type="button" class="btn" onclick="loadRoleLife(true)">重新读取</button></div>`;
  const s=d.status||{},corrupt=s.corrupt||s.read_error,editable=roleLifeEditable(),draft=roleLifeDraftValues();
  const masterOn=!!draft.enabled;
  const masterDirty=roleLifeState.draft&&Object.prototype.hasOwnProperty.call(roleLifeState.draft,'enabled');
  const personaName=String(d.active_persona_name||'').trim()||(d.presets||[]).find(p=>String(p.id)===String(roleLifeState.role||d.role_id))?.name||'默认人设';
  const body=corrupt&&roleLifeState.tab!=='overview'?`<section class="card life-state-block"><div class="life-empty"><span class="life-empty-icon">${roleLifeIcon('alert')}</span><h3>${s.corrupt?'状态文件不可读':'暂时读取不到状态'}</h3><p>${esc(s.error||(typeof s.read_error==='string'?s.read_error:'请返回概览查看处理方式。'))}</p><button type="button" class="btn" onclick="roleLifeSetTab('overview')">返回概览</button></div></section>`:
    roleLifeState.tab==='overview'?roleLifeOverview():roleLifeState.tab==='today'?roleLifeTodayTab():roleLifeState.tab==='settings'?roleLifeSettingsTab():roleLifeState.tab==='jobs'?roleLifeJobsTab():
    `<div class="life-record-page"><div class="life-subtabs" role="tablist" aria-label="生活记录" onkeydown="roleLifeTabKey(event)">${roleLifeTab('活动经历','activity')}${roleLifeTab('日记','diary')}${roleLifeTab('梦','dream')}</div><div id="life-record-panel" role="tabpanel" aria-labelledby="life-record-${roleLifeState.subTab}">${roleLifeState.subTab==='diary'?roleLifeRecordList('日记','按日期阅读角色的日记。','diary'):roleLifeState.subTab==='dream'?roleLifeRecordList('梦','醒来后留下的梦境片段。','dream'):roleLifeRecordList('活动经历','活动结束后留下的日常片段。','activity')}</div></div>`;
  const runtimeNote=d.runtime_running===false?' · 仅 WebUI（启动主程序后自动推进）':'';
  const statusPill=masterDirty?(masterOn?'待保存 · 将开启':'待保存 · 将关闭'):(masterOn?'运行中':'已关闭');
  return `<header class="card role-life-bar"><div class="life-bar-top"><div class="life-bar-identity"><span class="role-life-mark">${roleLifeIcon('leaf')}</span><div class="life-bar-heading"><div class="life-bar-title"><h2>角色生活</h2><span class="pill ${masterOn&&!masterDirty?'ok':''}">${statusPill}</span></div><p class="life-bar-meta" role="status">当前人设：${esc(personaName)} · ${esc(roleLifeZone())} · ${roleLifeState.loading?'正在刷新…':roleLifeState.updatedAt?'上次读取 '+esc(roleLifeFmtTime(roleLifeState.updatedAt/1000,true)):'等待同步'}${runtimeNote}</p></div></div><div class="life-bar-actions"><div class="life-master-control"><label for="life-master-switch">角色生活总开关</label><button type="button" id="life-master-switch" class="switch ${masterOn?'on':''}" role="switch" aria-checked="${masterOn}" aria-label="角色生活总开关" ${editable?'':'disabled'} onclick="roleLifeSetDraft('enabled',!roleLifeDraftValues().enabled)"></button></div><button type="button" class="btn life-refresh-btn" onclick="loadRoleLife(true)" ${roleLifeState.loading?'disabled':''}>${roleLifeState.loading?'读取中…':'刷新'}</button></div></div><div class="life-bar-intro"><p class="life-intro-text">让机器人在后台按作息自己过日子（排日程、留经历、写日记、做梦），正常聊天和主动搭话时会自然带出近期状态，让对话更有活人感。</p></div></header>
    ${roleLifeState.error?roleLifeNotice('刷新失败，当前保留上次读取的内容：'+roleLifeState.error,'error'):''}
    ${roleLifeState.message?`<div class="life-page-message ${roleLifeState.messageKind==='error'?'is-error':roleLifeState.messageKind==='success'?'is-success':''}" role="status">${esc(roleLifeState.message)}</div>`:''}
    <nav class="life-main-tabs" role="tablist" aria-label="角色生活页面" onkeydown="roleLifeTabKey(event)">${ROLE_LIFE_TABS.map(([key,title])=>`<button type="button" id="life-tab-${key}" class="life-tab ${roleLifeState.tab===key?'is-active':''}" role="tab" aria-controls="life-panel-${key}" aria-selected="${roleLifeState.tab===key}" tabindex="${roleLifeState.tab===key?0:-1}" onclick="roleLifeSetTab('${key}')">${esc(title)}</button>`).join('')}</nav>
    <div id="life-panel-${roleLifeState.tab}" class="role-life-panel" role="tabpanel" aria-labelledby="life-tab-${roleLifeState.tab}">${body}</div>`
}
function renderRoleLife(){
  return `<section class="role-life" id="section_role_life">${renderRoleLifeInner()}</section>`
}
// 自动刷新：只在角色生活页且未编辑草稿时轻量重载，避免把正在输入的内容冲掉。
if(typeof setInterval==='function')setInterval(()=>{if(state.current==='role-life'&&!roleLifeState.loading&&!roleLifeHasDraft()&&!roleLifeBusy()&&!roleLifeState.planEditor&&!roleLifeState.expEditor&&!roleLifeState.noteEditor&&!document.hidden&&roleLifeState.tab!=='settings'&&!(el('section_role_life')?.contains(document.activeElement)&&['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)))loadRoleLife(false,true)},15000);

const AGENT_LEVEL_LABEL={user:'所有人',admin:'仅管理员'};
function renderAgent(){
  const tab=state.agentSettingsTab||'tools',tabs=[['tools','工具与权限'],['settings','执行与搜索'],['mcp','MCP 服务器']];
  return `<div class="card organized-settings"><div class="section-head"><div><h3>Agent</h3><p>选择 AI 能用的工具、执行方式和外部 MCP 连接。</p></div><span class="pill ${state.bundle.form_values?.['Agent.enabled']?'ok':''}">${state.bundle.form_values?.['Agent.enabled']?'已启用':'未启用'}</span></div><div class="subtab-nav settings-tabs" id="agentSettingsTabs" role="tablist" aria-label="Agent 设置">${tabs.map(([key,title])=>`<button class="btn ${tab===key?'primary':''}" role="tab" aria-selected="${tab===key}" data-tab="${key}" onclick="switchAgentSettingsTab(this.dataset.tab)">${title}</button>`).join('')}</div><div id="agentSettingsPanel" class="subtab-panel settings-panel-grid">${renderAgentContent(tab)}</div></div>`;
}
function switchAgentSettingsTab(tab){
  if(!['tools','settings','mcp'].includes(tab)||(state.agentSettingsTab||'tools')===tab)return;
  syncCurrentPageFieldsFromDom();state.agentSettingsTab=tab;
  const panel=el('agentSettingsPanel');if(!panel)return;panel.innerHTML=renderAgentContent(tab);
  el('agentSettingsTabs')?.querySelectorAll('[data-tab]').forEach(b=>{b.classList.toggle('primary',b.dataset.tab===tab);b.setAttribute('aria-selected',String(b.dataset.tab===tab))});
  animateSubtab(panel);liquidGlassLensController.refresh();
}
function renderAgentContent(tab){
  if(tab==='mcp')return renderMcpPanel();
  const m=meta(),fields=m.fields||[],vals=state.bundle.form_values||{};
  if(tab==='settings'){
    const categories={};fields.forEach(f=>{const category=f.category||'执行设置';(categories[category]||(categories[category]=[])).push(f)});
    return Object.entries(categories).map(([title,items])=>`<section class="settings-block"><div class="section-head"><div><h3>${esc(title)}</h3></div></div><div class="form-grid">${items.map(f=>renderField(f,vals[f.path])).join('')}</div></section>`).join('');
  }
  const tools=state.bundle.agent_tools||{},metaList=state.bundle.agent_tool_meta||[];
  const groups={};metaList.forEach(x=>(groups[x.group]||(groups[x.group]=[])).push(x));
  const order=(state.bundle.agent_tool_groups||Object.keys(groups)).filter(g=>groups[g]&&groups[g].length);
  const enabled=metaList.filter(x=>tools[x.key]&&tools[x.key].enabled).length;
  const body=order.map(g=>{
    const sid=g.replace(/[^a-zA-Z0-9_-]/g,'_');
    const items=groups[g].map(it=>{
      const cur=tools[it.key]||{enabled:false,level:it.level};
      const lv=cur.level||it.level;
      const sel=['user','admin'].map(o=>`<option value="${o}" ${o===lv?'selected':''}>${AGENT_LEVEL_LABEL[o]}</option>`).join('');
      const warn=lv!==it.level?`<span class="pill" title="已改为与默认不同的权限">默认 ${esc(AGENT_LEVEL_LABEL[it.level])}</span>`:'';
      return `<div class="feature"><h4>${esc(it.title)}</h4><p>${esc(it.desc)}</p>`
        +`<div class="feature-foot"><span class="pill">${esc(it.key)}</span>`
        +`<div class="switch ${cur.enabled?'on':''}" onclick="toggleAgentTool('${it.key}',this)"></div></div>`
        +`<div class="field" style="margin-top:12px"><div class="label"><span>调用权限</span></div>`
        +`<select onchange="setAgentToolLevel('${it.key}',this.value)">${sel}</select>`
        +`<div class="desc">${warn||'谁可以让 AI 调用这个工具'}</div></div></div>`;
    }).join('');
    return `<div class="card" id="section_${sid}" data-section="${esc(g)}"><div class="section-head"><div><h3>${esc(g)}</h3><p>工具开关与调用权限</p></div><span class="pill">${groups[g].length} 项</span></div><div class="feature-grid">${items}</div></div>`;
  }).join('');
  const mainSwitch=fields.find(f=>f.path==='Agent.enabled');
  return `<section class="settings-block agent-overview"><div class="section-head"><div><h3>工具总开关</h3><p>控制 AI 是否可使用下方已启用的工具</p></div><span class="pill">${enabled} 个工具已启用</span></div>${mainSwitch?renderField(mainSwitch,vals[mainSwitch.path]):''}</section>`+body;
}
function agentToolEntry(key){
  const tools=state.bundle.agent_tools||(state.bundle.agent_tools={});
  if(!tools[key]){const m=(state.bundle.agent_tool_meta||[]).find(x=>x.key===key)||{};tools[key]={enabled:!!m.default_enabled,level:m.level||'user'}}
  return tools[key];
}
function markAgentDirty(){state.dirty=true;state.lastInputAt=Date.now();const save=el('saveState');if(save)save.textContent='有未保存更改'}
function agentToolMembers(key){const m=(state.bundle.agent_tool_meta||[]).find(x=>x.key===key);return Array.isArray(m?.members)&&m.members.length?m.members:[key]}
function toggleAgentTool(key,sw){const next=!agentToolEntry(key).enabled;agentToolMembers(key).forEach(k=>{agentToolEntry(k).enabled=next});sw.classList.toggle('on',next);markAgentDirty()}
function setAgentToolLevel(key,level){agentToolMembers(key).forEach(k=>{agentToolEntry(k).level=level});markAgentDirty();forceRender()}

let mcpState={loaded:false,loading:false,data:null,draft:null,dirty:false};
function mcpDraft(){if(!mcpState.draft)mcpState.draft=JSON.parse(JSON.stringify(mcpState.data?.servers||{}));return mcpState.draft}
function markMcpDirty(){mcpState.dirty=true;markAgentDirty()}
function discardMcpDraft(){mcpState.draft=null;mcpState.dirty=false}
let _pendingRenderTimer=null;
function renderAfterAnim(){
  // 切页动画期间 render() 会跳过同页 DOM 替换（见 render 里的 animating 分支），
  // 异步数据恰好在这 1 秒内返回时更新就被静默丢掉了。等动画播完再渲染。
  const c=el('content');
  if(c&&c.classList.contains('page-anim')){
    clearTimeout(_pendingRenderTimer);
    _pendingRenderTimer=setTimeout(renderAfterAnim,120);
    return;
  }
  forceRender()
}
async function loadMcp(force=false){
  if(mcpState.loading||(mcpState.loaded&&!force))return;
  mcpState.loading=true;
  // 失败也要置 loaded：否则面板永远停在"正在加载"，而且每次 render 都会
  // 重新发一次请求。错误信息由 renderMcpPanel 的 warn 分支显示出来。
  try{mcpState.data=await api('/api/agent/mcp');if(!mcpState.dirty)mcpState.draft=null}
  catch(e){mcpState.data={available:false,error:'读取 MCP 状态失败：'+e.message,servers:{},registered_tools:[]}}
  finally{mcpState.loading=false;mcpState.loaded=true;if(state.current==='agent')renderAfterAnim()}
}
function renderMcpPanel(){
  const gate=(state.bundle.agent_tools||{}).mcp_tools;
  if(!mcpState.loaded){loadMcp();return `<div class="card"><div class="section-head"><div><h3>MCP 服务器</h3><p>正在加载...</p></div></div></div>`}
  const d=mcpState.data||{},servers=mcpDraft(),names=Object.keys(servers);
  const warn=!d.available?`<div class="desc" style="color:var(--bad,#e66)">⚠️ ${esc(d.error||'MCP 不可用')}</div>`
    :(!gate||!gate.enabled)?`<div class="desc">提示：上方「MCP 外部工具」开关尚未开启，配置好服务器后记得打开它。</div>`:'';
  const tools=(d.registered_tools||[]);
  const rows=names.map(n=>{
    const c=servers[n]||{},isUrl=Object.prototype.hasOwnProperty.call(c,'url')&&!c.command;
    return `<div class="feature" data-mcp-card="${esc(n)}"><h4>${esc(n)}</h4>
      <div class="feature-foot"><span class="pill">${isUrl?'SSE':'stdio'}</span>
      <div class="switch ${c.enabled!==false?'on':''}" data-mcp-act="toggle" data-mcp-name="${esc(n)}"></div></div>
      <div class="mcp-server-fields"><div class="field"><div class="label"><span>名称</span></div>
      <input value="${esc(n)}" data-mcp-act="rename" data-mcp-name="${esc(n)}"><div class="desc">工具会以 mcp__名称__工具名 注册</div></div>
      ${isUrl?`<div class="field"><div class="label"><span>SSE 地址</span></div><input value="${esc(c.url||'')}" data-mcp-act="field" data-mcp-key="url" data-mcp-name="${esc(n)}"></div>`
      :`<div class="field"><div class="label"><span>启动命令</span></div><input value="${esc(c.command||'')}" data-mcp-act="field" data-mcp-key="command" data-mcp-name="${esc(n)}"><div class="desc">如 npx、uvx、python</div></div>
        <div class="field"><div class="label"><span>命令参数</span></div><textarea style="min-height:70px" data-mcp-act="args" data-mcp-name="${esc(n)}">${esc((c.args||[]).join('\n'))}</textarea><div class="desc">一行一个参数</div></div>`}</div>
      <div style="margin-top:8px"><button class="btn" data-mcp-act="remove" data-mcp-name="${esc(n)}">删除</button></div></div>`;
  }).join('');
  return `<div class="card" id="section_MCP_" data-section="MCP"><div class="section-head">
    <div><h3>MCP 服务器</h3><p>接入外部 MCP 工具生态，配置文件 ${esc(d.config_path||'')}</p></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${mcpState.dirty?'<span class="pill" style="border-color:rgba(255,200,120,.5)">待保存</span>':''}<span class="pill ${tools.length?'ok':''}">已注册 ${tools.length} 个工具</span></div></div>
    ${warn}
    <div style="display:flex;gap:10px;margin-top:${warn?12:0}px;margin-bottom:14px;flex-wrap:wrap">
      <button class="btn" onclick="addMcpServer(false)">+ 添加 stdio 服务器</button>
      <button class="btn" onclick="addMcpServer(true)">+ 添加 SSE 服务器</button>
      <button class="btn" onclick="reloadMcp()">重新连接</button>
    </div>
    ${names.length?`<div class="feature-grid">${rows}</div>`:'<div class="desc">还没有配置 MCP 服务器。</div>'}
    ${tools.length?`<div class="desc" style="margin-top:12px">当前可用：${tools.map(esc).join('、')}</div>`:''}</div>`;
}
function addMcpServer(isUrl){const d=mcpDraft();let i=1;while(d['server'+i])i++;d['server'+i]=isUrl?{url:'',enabled:true}:{command:'npx',args:[],enabled:true};markMcpDirty();forceRender()}
function removeMcpServer(n){const d=mcpDraft();if(!Object.prototype.hasOwnProperty.call(d,n))return;delete d[n];markMcpDirty();forceRender()}
function toggleMcpServer(n,sw){const d=mcpDraft();if(!d[n])return;d[n].enabled=d[n].enabled===false;sw.classList.toggle('on',d[n].enabled);markMcpDirty()}
function setMcpField(n,k,v){const d=mcpDraft();if(d[n]){d[n][k]=v;markMcpDirty()}}
function setMcpArgs(n,v){const d=mcpDraft();if(d[n]){d[n].args=v.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);markMcpDirty()}}
function renameMcpServer(oldName,newName,inputEl){
  const d=mcpDraft();newName=(newName||'').trim();
  if(!newName||newName===oldName||!Object.prototype.hasOwnProperty.call(d,oldName)||Object.prototype.hasOwnProperty.call(d,newName))return;
  d[newName]=d[oldName];delete d[oldName];
  const card=inputEl?.closest?.('[data-mcp-card]')||document.querySelector?.(`[data-mcp-card="${CSS?.escape?CSS.escape(oldName):oldName}"]`);
  if(card){
    card.dataset.mcpCard=newName;
    const heading=card.querySelector('h4');if(heading)heading.textContent=newName;
    card.querySelectorAll('[data-mcp-name]').forEach(node=>{node.dataset.mcpName=newName});
  }
  markMcpDirty();
}
async function saveMcpPending(){
  if(!mcpState.dirty||!mcpState.draft)return;
  const r=await api('/api/agent/mcp',{method:'POST',body:JSON.stringify({action:'save',servers:mcpDraft()})});
  mcpState.data=r;mcpState.draft=null;mcpState.dirty=false;
}
async function saveMcp(){try{mcpState.dirty=true;await saveMcpPending();toast('MCP 配置已保存',true);forceRender()}catch(e){toast(e.message,false)}}
async function reloadMcp(){if(mcpState.dirty){toast('请先点击右上角「保存设置」保存 MCP 修改，再重新连接',false);return}try{const r=await api('/api/agent/mcp',{method:'POST',body:JSON.stringify({action:'reload'})});toast(r?.message||'已触发重连',true);setTimeout(()=>loadMcp(true),3000)}catch(e){toast(e.message,false)}}

// ==================== 首次设置访问 Token ====================
// Token 为空时后端所有接口都放行，等于把 LLM API Key 摊开。这里在页面加载后
// 弹出设置窗口，不提供跳过——机器人照常启动、功能不受影响，但这个窗口必须
// 填完才会关闭，避免一直停在无防护状态。
async function checkAuthState(){
  try{
    const d=await api('/api/auth/state');
    if(d&&d.has_token===false)showTokenModal(d);
  }catch(e){/* 已设 Token 时此接口会 401，正常 */}
}
function showTokenModal(d){
  const m=el('tokenModal');if(!m)return;
  // 清掉旧版本留下的跳过标记，避免升级后窗口不弹
  try{sessionStorage.removeItem('xcbotTokenPromptSkipped')}catch(e){}
  const hostEl=el('tokenModalHost');
  if(hostEl&&d&&d.loopback_only===false){
    hostEl.textContent='当前监听 '+(d.host||'')+'，不只本机可达 —— 强烈建议现在就设置。';
    hostEl.style.display='';
  }
  m.style.display='flex';
  setTimeout(()=>{const i=el('newToken');if(i)i.focus()},50);
}
// 把 Token 交给浏览器的密码管理器，让它弹「保存密码」。
// 首次设置和登录页两处都要调：这里是 fetch + JSON，没有原生表单提交，
// 浏览器那条「密码用对了要不要保存」的启发式根本不会触发。
// id 只是密码管理器条目里的显示名，不参与任何校验；不能传空串（浏览器会抛
// "'id' must not be empty"）。非安全上下文（http + 公网 IP/域名，localhost 除外）
// 下 PasswordCredential 不存在，直接跳过。store() 可能等用户处理气泡，加超时竞速。
async function rememberWebuiToken(t){
  try{
    if(!t||!window.PasswordCredential||!navigator.credentials?.store)return;
    const p=navigator.credentials.store(new PasswordCredential({id:'webui',password:t,name:'XcBot WebUI'}));
    await Promise.race([p,new Promise(r=>setTimeout(r,1200))]);
  }catch(e){}
}
async function submitNewToken(){
  const t=(el('newToken')?.value||'').trim(),t2=(el('newToken2')?.value||'').trim();
  const msg=el('tokenModalMsg'),btn=el('tokenModalBtn');
  const fail=s=>{if(msg)msg.textContent=s};
  if(!t)return fail('请输入访问 Token');
  if(t!==t2)return fail('两次输入不一致');
  if(btn){btn.disabled=true;btn.textContent='保存中...'}
  try{
    await api('/api/auth/set-token',{method:'POST',body:JSON.stringify({token:t})});
    localStorage.webuiToken=t;   // 立刻带上新 Token，避免下一次请求 401
    const m=el('tokenModal');if(m)m.style.display='none';
    fail('');
    // 趁刚设完就让浏览器记住，省得用户下次进来还要翻自己存的随机串。
    // 放在关窗之后：气泡不会被弹层挡住。
    await rememberWebuiToken(t);
    toast('访问 Token 已设置',true);
    await refreshAll(true);
  }catch(e){fail(e.message||'设置失败')}
  finally{if(btn){btn.disabled=false;btn.textContent='保存并使用'}}
}

// MCP 面板改用事件委托 + data 属性，不把服务器名拼进内联 on* 属性。
// esc() 只做 HTML 转义，而浏览器解析属性时会把 &#39; 还原成单引号 ——
// 放进 onclick="fn('...')" 里就能闭合字符串执行任意脚本（存储型 XSS）。
// dataset 走的是 DOM 属性通道，内容永远是数据。
(function bindMcpDelegates(){
  document.addEventListener('click', e => {
    const el = e.target.closest('[data-mcp-act]');
    if (!el) return;
    const act = el.dataset.mcpAct, name = el.dataset.mcpName || '';
    if (act === 'toggle') toggleMcpServer(name, el);
    else if (act === 'remove') removeMcpServer(name);
  });
  const onEdit = e => {
    const el = e.target.closest('[data-mcp-act]');
    if (!el) return;
    const act = el.dataset.mcpAct, name = el.dataset.mcpName || '';
    if (act === 'rename') renameMcpServer(name, el.value, el);
    else if (act === 'field') setMcpField(name, el.dataset.mcpKey, el.value);
    else if (act === 'args') setMcpArgs(name, el.value);
  };
  document.addEventListener('input', onEdit);
  document.addEventListener('change', onEdit);
})();

// 人格预设 ID 与追踪记录 ID 同样不拼进内联 on* 属性，理由同 MCP 面板。
(function bindDataDelegates(){
  document.addEventListener('click', e => {
    const pp = e.target.closest('[data-pp-act]');
    if (pp) {
      const act = pp.dataset.ppAct, id = pp.dataset.ppId || '';
      if (act === 'select') selectPersonaPreset(id);
      else if (act === 'rename') renamePersonaPreset(id);
      else if (act === 'remove') removePersonaPreset(id);
      return;
    }
    const tr = e.target.closest('[data-trace-open]');
    if (tr) traceOpen(tr.dataset.traceOpen || '');
  });
  document.addEventListener('input', e => {
    const pp = e.target.closest('[data-pp-act]');
    if (pp && pp.dataset.ppAct === 'prompt') updatePersonaPrompt(pp.dataset.ppId || '', pp.value);
  });
})();

// 抽屉的收起路径：Esc、点侧栏里的链接以外的地方、以及窗口放宽回桌面宽度。
// 放宽时必须清掉 nav-open，否则 body 上残留的类会让桌面布局带着抽屉态的 transform。
(function bindMobileNav(){
  document.addEventListener('keydown', e => {
    const isVisible = id => { const m = el(id); return !!(m && m.style.display && m.style.display !== 'none'); };
    if (e.key === 'Escape') {
      if (isVisible('modelInputModal')) { e.preventDefault(); closeModelInput(); return; }
      if (isVisible('submitModal')) { e.preventDefault(); closeSubmitModal(); return; }
      if (isVisible('leaveModal')) { e.preventDefault(); leaveCancel(); return; }
      if (document.body.classList.contains('nav-open')) closeMobileNav();
      return;
    }
    if (e.key === 'Enter' && isVisible('leaveModal')) {
      e.preventDefault();
      leaveConfirm();
    }
  });
  window.addEventListener('resize', () => {
    if (!isMobileNav() && document.body.classList.contains('nav-open')) closeMobileNav();
  }, { passive: true });
})();

// 日志手动上滑检测：开启自动滚动时，若用户主动向上翻看历史日志，自动暂停跟随；
// 程序触发的滚到底部会更新 window._logAutoScrollAt，220ms 内的滚动事件忽略。
(function bindLogManualScroll(){
  document.addEventListener('scroll', e => {
    const target = e.target;
    if (!target || typeof target.matches !== 'function' || !target.matches('pre.log')) return;
    if (localStorage.webuiAutoScroll === 'false') return;
    if (Date.now() - (window._logAutoScrollAt || 0) < 220) return;
    const remain = target.scrollHeight - target.scrollTop - target.clientHeight;
    if (remain > 48) setAutoScrollState(false, false);
  }, true);
})();
