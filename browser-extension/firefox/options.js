'use strict';
const $ = s => document.querySelector(s);

function addRow(d = {}){
  const row = $('#dashrow').content.firstElementChild.cloneNode(true);
  row.querySelector('.label').value = d.label || '';
  row.querySelector('.url').value = d.url || '';
  row.querySelector('.key').value = d.key || '';
  row.querySelector('.remove').onclick = () => row.remove();
  row.querySelector('.test').onclick = async () => {
    const msg = row.querySelector('.testmsg');
    const url = dashboardOrigin(row.querySelector('.url').value.trim()), key = row.querySelector('.key').value.trim();
    if(!url || !key){ msg.textContent = 'Enter the address and key first.'; return; }
    const granted = await browser.permissions.request({origins: [dashboardPattern(url)]});
    if(!granted){ msg.textContent = 'Firefox needs your OK to reach the dashboard.'; return; }
    const r = await browser.runtime.sendMessage({cmd: 'test', dashboard: url, key});
    msg.textContent = r.note;
  };
  $('#dashes').appendChild(row);
}

async function load(){
  const s = await getSettings();
  $('#dashes').innerHTML = '';
  (s.dashboards.length ? s.dashboards : [{}]).forEach(addRow);
  $('#sites').value = s.sites.join('\n');
}

function status(text, ok){ const el = $('#status'); el.textContent = text; el.className = ok ? 'ok' : 'bad'; }

function readForm(){
  const dashboards = [...document.querySelectorAll('#dashes .dash')].map(r => ({
    label: r.querySelector('.label').value.trim(),
    url: dashboardOrigin(r.querySelector('.url').value.trim()),
    key: r.querySelector('.key').value.trim()})).filter(d => d.url || d.key);
  const sites = $('#sites').value.split('\n').map(x => x.trim()).filter(Boolean);
  const bad = sites.filter(p => !/^(\*|https?):\/\/(\*|\*\.[^/*]+|[^/*]+)\/.*$/.test(p));
  return {dashboards, sites, bad};
}

$('#adddash').onclick = () => addRow();

$('#savebtn').onclick = async () => {
  const f = readForm();
  if(!f.dashboards.length || f.dashboards.some(d => !d.url)) return status('Each dashboard needs an address, like http://192.168.1.50:9380', false);
  if(f.bad.length) return status('These lines are not valid site patterns: ' + f.bad.join(', '), false);
  // Firefox only lets the plugin read a site (or reach a dashboard) after you allow it
  const granted = await browser.permissions.request({origins: [...f.sites, ...f.dashboards.map(d => dashboardPattern(d.url))]});
  const s = await getSettings();
  await browser.storage.local.set({dashboards: f.dashboards, current: Math.min(s.current, f.dashboards.length - 1),
                                   sites: f.sites, dashboard: '', key: ''});
  status(granted ? 'Saved.' : 'Saved, but Firefox was not allowed to use those sites; the plugin will not work on them.', granted);
};

document.getElementById('ver').textContent = 'v' + browser.runtime.getManifest().version;
load();
