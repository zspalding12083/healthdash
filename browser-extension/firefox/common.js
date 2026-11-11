'use strict';
// Settings shared by the background script, popup and options page.

const DEFAULTS = {
  on: false,                 // nothing runs on any site while off
  auto: false,               // save every portal page you open (otherwise only the "Save this page" button)
  pdfs: true,                // send PDFs you download from the portal sites
  dashboard: '',             // e.g. http://192.168.1.50:9380
  key: '',                   // a plugin key from the dashboard (Accounts > Browser plugin keys)
  sites: [                   // match patterns: https://developer.mozilla.org/docs/Mozilla/Add-ons/WebExtensions/Match_patterns
    '*://*/MyChart/*',                 // Epic MyChart, on each health system's own domain
    '*://*/mychart/*',
    '*://*.followmyhealth.com/*',      // FollowMyHealth
    '*://*.healtheintent.com/*',       // Oracle Health (Cerner) HealtheLife / consumer portals
    '*://*.healthelife.com/*',
  ],
};

async function getSettings(){
  const s = await browser.storage.local.get(Object.keys(DEFAULTS));
  return Object.assign({}, DEFAULTS, s);
}

function dashboardOrigin(url){
  try{ const u = new URL(url); return `${u.protocol}//${u.host}`; }catch(e){ return ''; }
}

// does a URL fall under one of the match patterns?
function matches(url, patterns){
  let u; try{ u = new URL(url); }catch(e){ return false; }
  return patterns.some(p => {
    const m = p.match(/^(\*|https?):\/\/([^/]+)(\/.*)$/); if(!m) return false;
    const [, scheme, host, path] = m;
    if(scheme !== '*' && scheme + ':' !== u.protocol) return false;
    if(!['http:', 'https:'].includes(u.protocol)) return false;
    const hostOk = host === '*' || (host.startsWith('*.') ? (u.hostname === host.slice(2) || u.hostname.endsWith(host.slice(1))) : u.hostname === host);
    const rx = new RegExp('^' + path.split('*').map(x => x.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
    return hostOk && rx.test(u.pathname + u.search);
  });
}
