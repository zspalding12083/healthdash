'use strict';
const $ = s => document.querySelector(s);

async function load(){
  const s = await getSettings();
  $('#dashboard').value = s.dashboard;
  $('#key').value = s.key;
  $('#sites').value = s.sites.join('\n');
}

function status(text, ok){ const el = $('#status'); el.textContent = text; el.className = ok ? 'ok' : 'bad'; }

function readForm(){
  const dashboard = dashboardOrigin($('#dashboard').value.trim());
  const sites = $('#sites').value.split('\n').map(x => x.trim()).filter(Boolean);
  const bad = sites.filter(p => !/^(\*|https?):\/\/(\*|\*\.[^/*]+|[^/*]+)\/.*$/.test(p));
  return {dashboard, key: $('#key').value.trim(), sites, bad};
}

// Firefox only lets the plugin read a site (or reach the dashboard) after you allow it.
// Site patterns can't name a port, so the dashboard's is "http://host/*".
function dashboardPattern(origin){ const u = new URL(origin); return `${u.protocol}//${u.hostname}/*`; }
async function askPermission(f){
  const origins = [...f.sites, dashboardPattern(f.dashboard)];
  return browser.permissions.request({origins});
}

$('#savebtn').onclick = async () => {
  const f = readForm();
  if(!f.dashboard) return status('Enter the dashboard address, like http://192.168.1.50:9380', false);
  if(f.bad.length) return status('These lines are not valid site patterns: ' + f.bad.join(', '), false);
  const granted = await askPermission(f);   // must run straight from the click
  await browser.storage.local.set({dashboard: f.dashboard, key: f.key, sites: f.sites});
  status(granted ? 'Saved.' : 'Saved, but Firefox was not allowed to use those sites; the plugin will not work on them.', granted);
};

$('#test').onclick = async () => {
  const f = readForm();
  if(!f.dashboard || !f.key){ $('#testmsg').textContent = 'Enter the address and key first.'; return; }
  const granted = await browser.permissions.request({origins: [dashboardPattern(f.dashboard)]});
  if(!granted){ $('#testmsg').textContent = 'Firefox needs your OK to reach the dashboard.'; return; }
  const r = await browser.runtime.sendMessage({cmd: 'test', dashboard: f.dashboard, key: f.key});
  $('#testmsg').textContent = r.note;
};

load();
