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
$('#save').onclick = async () => {
  $('#msg').textContent = 'Saving…';
  const r = await browser.runtime.sendMessage({cmd: 'save-tab'});
  $('#msg').textContent = r && r.ok ? 'Sent.' : (r && r.error) || 'Could not save.';
};
for(const id of ['#opts', '#opts2']) $(id).onclick = e => { e.preventDefault(); browser.runtime.openOptionsPage(); window.close(); };
browser.storage.onChanged.addListener(show);
show();
