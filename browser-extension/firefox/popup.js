'use strict';
const $ = s => document.querySelector(s);
const LABEL = {sent: 'Sent', filed: 'Filed', review: 'Needs review', skipped: 'Skipped', error: 'Problem'};

async function show(){
  const s = await getSettings();
  $('#on').checked = s.on; $('#auto').checked = s.auto; $('#pdfs').checked = s.pdfs;
  $('#auto').disabled = $('#pdfs').disabled = $('#save').disabled = !s.on;
  $('#state').textContent = s.on ? (s.auto ? 'On: saving every portal page' : 'On') : 'Off';
  $('#statehint').textContent = s.on ? 'Only on your portal sites; sends only to your dashboard.' : 'Nothing is read or sent while off.';
  $('#setup').hidden = !!(s.dashboard && s.key);
  $('#dash').href = s.dashboard || '#';
  const {log = []} = await browser.storage.local.get('log');
  $('#log').innerHTML = log.length ? '' : '<div class="muted">Nothing sent yet.</div>';
  for(const e of log.slice(0, 15)){
    const d = document.createElement('div');
    d.className = e.state;
    const t = new Date(e.time);
    d.innerHTML = `<b></b> <span class="muted"></span><br><span></span>`;
    d.querySelector('b').textContent = LABEL[e.state] || e.state;
    d.querySelector('.muted').textContent = `${t.toLocaleDateString()} ${t.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})} · ${e.name || ''}`;
    d.querySelector('span:last-child').textContent = e.note || '';
    $('#log').appendChild(d);
  }
}

$('#on').onchange = e => browser.storage.local.set({on: e.target.checked});
$('#auto').onchange = e => browser.storage.local.set({auto: e.target.checked});
$('#pdfs').onchange = e => browser.storage.local.set({pdfs: e.target.checked});
// The page you're on, looked up when the popup opens: Firefox only lets the plugin ask for a
// site's permission as the very first thing a click does.
let here = null, tabId = null;
browser.tabs.query({active: true, currentWindow: true}).then(([tab]) => {
  tabId = tab ? tab.id : null;
  try{ const u = new URL(tab.url); if(/^https?:$/.test(u.protocol)) here = u; }catch(e){}
});
document.getElementById('ver').textContent = 'v' + browser.runtime.getManifest().version;

$('#save').onclick = async () => {
  let origin = null;
  if(here){
    origin = `${here.protocol}//${here.hostname}/*`;
    // asking is a no-op when the site is already allowed
    const ok = await browser.permissions.request({origins: [origin]});
    if(!ok){ $('#msg').textContent = 'Firefox needs your OK to read this site.'; return; }
    const s = await getSettings();
    if(!matches(here.href, s.sites)){   // remember it, so auto-save works here too
      await browser.storage.local.set({sites: [...s.sites, `*://${here.hostname}/*`]});
    }
  }
  $('#msg').textContent = 'Saving…';
  const r = await browser.runtime.sendMessage({cmd: 'save-tab', tabId, origin, host: here ? here.hostname : null});
  $('#msg').textContent = r && r.ok ? 'Sent.' : (r && r.error) || 'Could not save.';
};
for(const id of ['#opts', '#opts2']) $(id).onclick = e => { e.preventDefault(); browser.runtime.openOptionsPage(); window.close(); };
browser.storage.onChanged.addListener(show);
show();
