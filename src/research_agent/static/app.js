'use strict';
const toast = message => { const node = document.getElementById('toast'); if (!node) return; node.textContent = message; clearTimeout(node.hideTimer); node.hideTimer = setTimeout(() => {node.textContent='';}, 4000); };
const toggle = document.querySelector('[data-menu-toggle]');
const sidebar = document.querySelector('.sidebar');
if (toggle && sidebar) {
  const mobile=window.matchMedia('(max-width:900px)');
  const syncMenu=()=>{sidebar.inert=mobile.matches&&!sidebar.classList.contains('open');toggle.setAttribute('aria-expanded',String(sidebar.classList.contains('open')));};
  const close=()=>{sidebar.classList.remove('open');syncMenu();toggle.focus();};
  toggle.addEventListener('click', () => { sidebar.classList.toggle('open'); syncMenu(); });
  document.querySelector('[data-menu-close]').addEventListener('click',close);
  document.addEventListener('keydown', event => { if (event.key==='Escape' && sidebar.classList.contains('open')) close(); });
  mobile.addEventListener('change',syncMenu);syncMenu();
}
document.querySelectorAll('[data-print]').forEach(button => button.addEventListener('click', () => window.print()));
document.querySelectorAll('[data-refresh]').forEach(button => button.addEventListener('click', () => location.reload()));
document.querySelectorAll('[data-copy-report]').forEach(button => button.addEventListener('click', async () => {
  try { const response = await fetch(button.dataset.copyReport, {cache:'no-store'}); if (!response.ok) throw new Error('download unavailable'); await navigator.clipboard.writeText(await response.text()); toast('Markdown 报告已复制'); }
  catch { toast('复制未完成，请使用 Markdown 下载'); }
}));
document.addEventListener('keydown', event => { if (event.key==='/' && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) { const input=document.querySelector('input[name="q"]'); if (input) {event.preventDefault(); input.focus();} } });
async function refreshStatus() {
  const root=document.body; if (document.hidden) return;
  try {
    const request=root.dataset.pollUrl; if (!request) return;
    const response=await fetch(request,{cache:'no-store'}); if (!response.ok) return;
    const data=await response.json();
    document.querySelectorAll('[data-live-metric]').forEach(node => { const value=data[node.dataset.liveMetric]; if (value!==undefined) node.textContent=String(value); });
    const notice=document.querySelector('.update-notice');
    if (notice && root.dataset.snapshot && data.snapshot_at && data.snapshot_at!==root.dataset.snapshot) notice.classList.add('visible');
  } catch { /* Preserve the readable snapshot during a service restart. */ }
}
if (document.body.dataset.pollUrl) setInterval(refreshStatus,20000);
