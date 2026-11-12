'use strict';
// The switch, and everything that talks to your dashboard. Sends only to the dashboard address
// you set, with the plugin key you set, and only while switched on.

const SCRIPT_ID = 'hd-capture';
const seen = new Set();          // pages already sent this session (by text fingerprint)

// ---------- on / off ----------
async function apply(){
  const s = await getSettings();
  try{ await browser.scripting.unregisterContentScripts({ids: [SCRIPT_ID]}); }catch(e){ /* wasn't registered */ }
  if(s.on && s.sites.length){
    try{
      await browser.scripting.registerContentScripts([{id: SCRIPT_ID, matches: s.sites, js: ['capture.js'],
                                                       runAt: 'document_idle', persistAcrossSessions: false}]);
    }catch(e){ await log({state: 'error', name: 'Portal sites', note: 'Could not watch the portal sites: ' + e.message}); }
  }
  await browser.action.setBadgeText({text: s.on ? (s.auto ? 'AUTO' : 'ON') : ''});
  await browser.action.setBadgeBackgroundColor({color: s.on ? '#256c6c' : '#888888'});
  await browser.action.setTitle({title: 'Health Records Capture: ' + (s.on ? (s.auto ? 'on, saving every portal page' : 'on') : 'off')});
}
browser.runtime.onStartup.addListener(apply);
browser.runtime.onInstalled.addListener(apply);
browser.storage.onChanged.addListener(changes => {
  if(['on', 'auto', 'sites'].some(k => k in changes)) apply();
});

// ---------- activity log (shown in the popup) ----------
async function log(entry){
  const {log: old = []} = await browser.storage.local.get('log');
  const item = Object.assign({time: new Date().toISOString()}, entry);
  const out = [item, ...old.filter(x => !(entry.key && x.key === entry.key))].slice(0, 30);
  await browser.storage.local.set({log: out});
}

// ---------- sending to the dashboard ----------
function cleanName(s){
  return (s || 'page').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 100);
}
function portalName(url){
  try{
    const h = new URL(url).hostname;
    if(/followmyhealth/i.test(h)) return 'FollowMyHealth';
    if(/healtheintent|healthelife/i.test(h)) return 'HealtheLife';
    if(/mychart/i.test(url)) return 'MyChart';
    return h.replace(/^www\./, '');
  }catch(e){ return 'Portal'; }
}

async function send(name, body, kind){
  const t = (await getSettings()).target;
  if(!t.url || !t.key) throw new Error('Add a dashboard (address and plugin key) in the settings first.');
  const r = await fetch(`${t.url}/api/upload?source=plugin&p=${encodeURIComponent(t.person)}&name=${encodeURIComponent(name)}`,
                        {method: 'POST', headers: {'Authorization': 'Bearer ' + t.key, 'Content-Type': kind}, body});
  const j = await r.json().catch(() => ({}));
  if(r.status === 401) throw new Error(`${t.label || 'The dashboard'} did not accept the plugin key. Create a new one under Accounts.`);
  if(!r.ok) throw new Error(j.error || `The dashboard said ${r.status}`);
  j.target = t;       // check back with the same dashboard, even if "Save to" changes meanwhile
  return j;
}

// after sending: see how the dashboard filed it (it reads and sorts in the background)
async function follow(id, name, t){
  for(let i = 0; i < 40; i++){
    await new Promise(r => setTimeout(r, i < 5 ? 2000 : 5000));
    const r = await fetch(`${t.url}/api/documents/${id}?p=${encodeURIComponent(t.person)}`, {headers: {'Authorization': 'Bearer ' + t.key}}).catch(() => null);
    if(!r) continue;
    if(r.status === 404){ return log({key: id, state: 'skipped', name, note: 'Nothing new on this page; not kept.'}); }
    const d = await r.json().catch(() => null);
    if(!d || d.status === 'processing') continue;
    if(d.status === 'error') return log({key: id, id, state: 'error', name, note: d.review_note || 'Could not be read.'});
    const what = [d.title, d.doc_date, d.facility].filter(Boolean).join(' · ');
    return log({key: id, id, state: d.needs_review ? 'review' : 'filed', name,
                note: d.needs_review ? `Needs your answer on the dashboard. Best guess: ${what}` : `Filed: ${what}`});
  }
}

async function savePage(page, how){
  if(seen.has(page.textHash)){
    if(how === 'auto') return;
    return log({state: 'skipped', name: page.title, note: 'Already sent this page.'});
  }
  const portal = portalName(page.url);
  const title = (page.title || '').replace(new RegExp('^\\s*' + portal + '\\s*[-|:]\\s*', 'i'), '').trim();
  const name = cleanName([portal, page.heading, title].filter(Boolean)
                          .filter((x, i, a) => a.findIndex(y => y.toLowerCase() === x.toLowerCase()) === i).join(' - ')) + '.html';
  try{
    const res = await send(name, new Blob([page.html], {type: 'text/html'}), 'text/html');
    seen.add(page.textHash);
    if(res.duplicate) return log({key: res.id, id: res.id, state: 'skipped', name, note: `Already in the records as "${res.title}".`});
    await log({key: res.id, id: res.id, state: 'sent', name, note: (how === 'auto' ? 'Saved automatically' : 'Sent') + (res.target.label ? ` to ${res.target.label}` : '') + '; sorting…'});
    follow(res.id, name, res.target);
  }catch(e){ await log({state: 'error', name, note: e.message}); }
}

// ---------- messages from the page script and the popup ----------
browser.runtime.onMessage.addListener(async (msg, sender) => {
  const s = await getSettings();
  if(msg.cmd === 'hd-auto'){
    if(s.on && s.auto && sender.tab && matches(sender.tab.url, s.sites)) await savePage(msg.page, 'auto');
    return;
  }
  if(msg.cmd === 'save-tab'){   // the popup's "Save this page" button
    if(!s.on) return {ok: false, error: 'Turn the plugin on first.'};
    const tabId = msg.tabId ?? (await browser.tabs.query({active: true, currentWindow: true}))[0]?.id;
    if(tabId == null) return {ok: false, error: 'No page open.'};
    let page = null, why = '';
    // the page script is already running on your portal sites; ask it first
    try{ page = await browser.tabs.sendMessage(tabId, {cmd: 'hd-snapshot'}); }catch(e){ /* not there yet */ }
    if(!page){
      try{
        await browser.scripting.executeScript({target: {tabId}, files: ['capture.js']});
        page = await browser.tabs.sendMessage(tabId, {cmd: 'hd-snapshot'});
      }catch(e){ why = e.message; }
    }
    if(!page){
      const host = msg.host || 'this site';
      const allowed = msg.origin ? await browser.permissions.contains({origins: [msg.origin]}) : false;
      return {ok: false, error: allowed
        ? `Firefox allowed ${host} but still blocked reading the page (${why}). Reload the page and try again.`
        : `Firefox has not allowed the plugin on ${host}. Press Save again and choose Allow, or add it under Settings > Portal sites. (${why})`};
    }
    if(page.textLength < 50) return {ok: false, error: 'This page has almost no text to save.'};
    seen.delete(page.textHash);   // a deliberate click always sends
    await savePage(page, 'button');
    return {ok: true};
  }
  if(msg.cmd === 'people'){      // the popup's "Save to": who is on each dashboard
    const out = [];
    for(const [i, d] of s.dashboards.entries()){
      try{
        const r = await fetch(`${dashboardOrigin(d.url)}/api/people`, {headers: {'Authorization': 'Bearer ' + d.key}});
        const list = r.ok ? await r.json() : [{id: 'main', name: d.label || 'Main'}];
        list.forEach(p => out.push({dash: i, person: p.id, label: s.dashboards.length > 1 ? `${p.name} (${d.label || d.url})` : p.name}));
      }catch(e){ out.push({dash: i, person: 'main', label: d.label || d.url}); }
    }
    return out;
  }
  if(msg.cmd === 'test'){        // the settings page's "Test connection" button
    try{
      const r = await fetch(`${dashboardOrigin(msg.dashboard)}/api/auth`, {headers: {'Authorization': 'Bearer ' + msg.key}});
      const j = await r.json();
      if(!j.auth) return {ok: true, note: 'Connected (this dashboard has no sign-in).'};
      return j.user ? {ok: true, note: `Connected; files will be added as ${j.user}.`} : {ok: false, note: 'The dashboard answered but did not accept the key.'};
    }catch(e){ return {ok: false, note: 'Could not reach the dashboard: ' + e.message}; }
  }
});

// ---------- PDFs downloaded from the portal sites ----------
browser.downloads.onChanged.addListener(async delta => {
  if(!delta.state || delta.state.current !== 'complete') return;
  const s = await getSettings();
  if(!s.on || !s.pdfs) return;
  const [item] = await browser.downloads.search({id: delta.id});
  if(!item) return;
  const fromPortal = matches(item.url, s.sites) || matches(item.referrer || '', s.sites);
  const isPdf = /pdf/i.test(item.mime || '') || /\.pdf$/i.test(item.filename || '');
  if(!fromPortal || !isPdf) return;
  const name = cleanName((item.filename || 'document.pdf').split(/[\\/]/).pop());
  if(!/^https?:/.test(item.url)){
    return log({state: 'error', name, note: 'This portal made the PDF inside the page, so the plugin cannot fetch it. It is in your Downloads; drop it on the dashboard.'});
  }
  try{
    const r = await fetch(item.url, {credentials: 'include'});
    const blob = await r.blob();
    const head = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
    if(String.fromCharCode(...head) !== '%PDF') throw new Error('the portal did not hand the PDF over again');
    const res = await send(/\.pdf$/i.test(name) ? name : name + '.pdf', blob, 'application/pdf');
    if(res.duplicate) return log({key: res.id, id: res.id, state: 'skipped', name, note: `Already in the records as "${res.title}".`});
    await log({key: res.id, id: res.id, state: 'sent', name, note: 'PDF sent' + (res.target.label ? ` to ${res.target.label}` : '') + '; sorting…'});
    follow(res.id, name, res.target);
  }catch(e){
    await log({state: 'error', name, note: `Could not send this PDF automatically (${e.message}). It is in your Downloads; drop it on the dashboard.`});
  }
});

apply();
