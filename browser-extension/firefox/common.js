'use strict';
// Settings shared by the background script, popup and options page.

const DEFAULTS = {
  on: false,                 // nothing runs on any site while off
  auto: false,               // save every portal page you open (otherwise only the "Save this page" button)
  pdfs: true,                // send PDFs you download from the portal sites
  dashboards: [],            // [{label, url, key}]: one per person's dashboard
  current: 0,                // which dashboard "Save to" sends to
  person: 'main',            // and which person on it
  dashboard: '', key: '',    // (1.0.x single-dashboard settings, read once and moved into the list)
  sites: [                   // match patterns: https://developer.mozilla.org/docs/Mozilla/Add-ons/WebExtensions/Match_patterns
    '*://*/MyChart/*',                 // Epic MyChart, on each health system's own domain
    '*://*/mychart/*',
    '*://*.followmyhealth.com/*',      // FollowMyHealth
    '*://*.healtheintent.com/*',       // Oracle Health (Cerner) HealtheLife / consumer portals
    '*://*.healthelife.com/*',
  ],
};

async function getSettings(){
  const s = Object.assign({}, DEFAULTS, await browser.storage.local.get(Object.keys(DEFAULTS)));
  if(!s.dashboards.length && s.dashboard) s.dashboards = [{label: 'Dashboard', url: s.dashboard, key: s.key}];
  s.current = Math.max(0, Math.min(s.current || 0, s.dashboards.length - 1));
  const t = s.dashboards[s.current] || {};
  s.target = {label: t.label || '', url: dashboardOrigin(t.url || ''), key: t.key || '', person: s.person || 'main'};
  return s;
}

// a match pattern for a dashboard's address (patterns can't name a port)
function dashboardPattern(origin){ const u = new URL(origin); return `${u.protocol}//${u.hostname}/*`; }

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
