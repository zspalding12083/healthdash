'use strict';
// Runs on portal pages only while the plugin is on (it is registered and unregistered by the
// background script). Reads the page as you see it; sends nothing by itself except, in auto
// mode, a copy of a page that has settled, to the plugin's own background script.

(() => {
  if(window.__hdCapture) return;   // injected once per page
  window.__hdCapture = true;

  const WAIT_FOR_LOADING_MS = 20000;

  function visible(el){
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  }

  // portals fill pages in pieces; a visible "Loading..." or busy marker means not done yet
  function stillLoading(){
    const busy = [...document.querySelectorAll('[aria-busy="true"], [role="progressbar"], .loading, .spinner, [class*="Loading"]')]
      .some(visible);
    if(busy) return true;
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    for(let n = walker.nextNode(); n; n = walker.nextNode()){
      if(/^\s*Loading(\.\.\.|…)?\s*$/i.test(n.nodeValue) && n.parentElement && visible(n.parentElement)) return true;
    }
    return false;
  }

  // embedded frames from the same site (MyChart shows visit summaries in one) are copied in;
  // frames from other sites can't be read and are left out
  function frames(){
    return [...document.querySelectorAll('iframe')].map(f => {
      try{ const d = f.contentDocument; return d && d.body ? d : null; }catch(e){ return null; }
    });
  }

  function clean(root){
    root.querySelectorAll('script, noscript, iframe, object, embed, template, link[rel="preload"]').forEach(n => n.remove());
    root.querySelectorAll('input, textarea').forEach(n => { n.removeAttribute('value'); n.textContent = ''; });
    root.querySelectorAll('*').forEach(n => { for(const a of [...n.attributes]) if(/^on/i.test(a.name)) n.removeAttribute(a.name); });
  }

  function snapshot(){
    const doc = document.documentElement.cloneNode(true);
    const live = frames();
    const copies = doc.querySelectorAll('iframe');
    copies.forEach((f, i) => {
      const d = live[i];
      if(!d) return;
      const box = document.createElement('section');
      box.setAttribute('data-healthdash', 'embedded frame');
      box.innerHTML = d.body.innerHTML;
      clean(box);
      f.replaceWith(box);
    });
    clean(doc);
    const text = [(document.body?.innerText || ''), ...live.filter(Boolean).map(d => d.body.innerText || '')].join('\n').trim();
    return {
      title: document.title || location.hostname,
      heading: ((document.querySelector('main h1, h1') || {}).innerText || '').trim().slice(0, 80),
      url: location.href,
      html: '<!doctype html>\n<!-- saved from ' + location.href.replace(/--/g, '') + ' on ' + new Date().toISOString() + ' -->\n' + doc.outerHTML,
      textLength: text.length,
      textHash: hash(text),
    };
  }

  function hash(s){   // small, fast fingerprint to skip sending the same page twice
    let h = 2166136261;
    for(let i = 0; i < s.length; i++){ h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0).toString(16) + ':' + s.length;
  }

  async function whenLoaded(){
    const start = Date.now();
    while(stillLoading() && Date.now() - start < WAIT_FOR_LOADING_MS) await new Promise(r => setTimeout(r, 1000));
  }

  browser.runtime.onMessage.addListener(msg => {
    if(msg && msg.cmd === 'hd-snapshot') return whenLoaded().then(snapshot);
  });

  // auto mode: when the page stops changing for a moment and nothing is still loading, offer it
  // to the background script, which decides. A page that keeps filling in is offered again; the
  // dashboard keeps the fuller copy.
  let timer = null, firstChange = Date.now();
  function settled(){
    clearTimeout(timer);
    timer = setTimeout(() => {
      if(stillLoading() && Date.now() - firstChange < WAIT_FOR_LOADING_MS) return settled();
      firstChange = Date.now();
      const s = snapshot();
      if(s.textLength >= 300) browser.runtime.sendMessage({cmd: 'hd-auto', page: s}).catch(() => {});
    }, 3000);
  }
  new MutationObserver(() => settled()).observe(document.documentElement, {childList: true, subtree: true, characterData: true});
  if(document.readyState === 'complete') settled(); else addEventListener('load', settled);
})();
