'use strict';
// Runs on portal pages only while the plugin is on (it is registered and unregistered by the
// background script). Reads the page as you see it; sends nothing by itself except, in auto
// mode, a copy of a page that has settled, to the plugin's own background script.

(() => {
  if(window.__hdCapture) return;   // injected once per page
  window.__hdCapture = true;

  function snapshot(){
    const doc = document.documentElement.cloneNode(true);
    // scripts, embedded frames and form values are not part of the record
    doc.querySelectorAll('script, noscript, iframe, object, embed, template, link[rel="preload"]').forEach(n => n.remove());
    doc.querySelectorAll('input, textarea').forEach(n => { n.removeAttribute('value'); n.textContent = ''; });
    doc.querySelectorAll('*').forEach(n => { for(const a of [...n.attributes]) if(/^on/i.test(a.name)) n.removeAttribute(a.name); });
    const text = (document.body?.innerText || '').trim();
    return {
      title: document.title || location.hostname,
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

  browser.runtime.onMessage.addListener(msg => {
    if(msg && msg.cmd === 'hd-snapshot') return Promise.resolve(snapshot());
  });

  // auto mode: when the page stops changing for a moment (portals load results in pieces and
  // change pages without reloading), offer it to the background script, which decides
  let timer = null, lastUrl = location.href;
  function settled(){
    clearTimeout(timer);
    timer = setTimeout(() => {
      const s = snapshot();
      if(s.textLength >= 300) browser.runtime.sendMessage({cmd: 'hd-auto', page: s}).catch(() => {});
    }, 3000);
  }
  new MutationObserver(() => {
    if(location.href !== lastUrl){ lastUrl = location.href; }
    settled();
  }).observe(document.documentElement, {childList: true, subtree: true, characterData: true});
  if(document.readyState === 'complete') settled(); else addEventListener('load', settled);
})();
