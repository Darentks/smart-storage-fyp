/* Separate URL-addressable pages share the existing camera controls and bridge client. */
// The former ESP32 CAMs page is now the "All 6 cameras" section (#all-cameras) at the bottom of Panel 2,
// on the Inventory Dashboard. Old links still load this page and are rewritten to that anchor.
function isLegacyEsp32Path(pathname) {
  return ['/esp32-cams','/local-dashboard/esp32-cams'].includes(pathname.replace(/\/+$/,''));
}
function dashboardPageForPath(pathname) {
  const path=pathname.replace(/\/+$/,'');
  if(['/cctv-configuration','/local-dashboard/cctv-configuration'].includes(path))return 'cctv';
  return 'dashboard';
}
function isLocalDashboardPath(pathname) {
  return /^\/local-dashboard(?:\/(?:cctv-configuration|esp32-cams))?\/?$/.test(pathname);
}
function dashboardPageUrl(page,{calibrate=false}={}) {
  const local=isLocalDashboardPath(location.pathname);
  const paths={dashboard:local?'/local-dashboard':'/',cctv:(local?'/local-dashboard':'')+'/cctv-configuration'};
  if(!Object.hasOwn(paths,page))throw Error('Unknown dashboard page');
  const query=new URLSearchParams(location.search);
  query.delete('calibrate');
  if(calibrate)query.set('calibrate','1');
  return paths[page]+(query.size?'?'+query:'');
}
// Same index.html, so rewrite an old ESP32 CAMs address in place (keeping ?bridge=) to Panel 2's anchor.
if(isLegacyEsp32Path(location.pathname) && typeof history!=='undefined' && typeof history.replaceState==='function')
  history.replaceState(null,'',dashboardPageUrl('dashboard')+'#all-cameras');
const DASHBOARD_PAGE=dashboardPageForPath(location.pathname);
const DASHBOARD_PAGE_TITLES={dashboard:'Inventory',cctv:'CCTV setup'};
// #all-cameras opens the section; scroll only once its tab (the Inventory Dashboard view) is showing.
function openAllCamerasSection() {
  if(location.hash!=='#all-cameras')return;
  const section=document.getElementById('all-cameras');
  if(!section || section.closest?.('[data-dashboard-view]')?.hidden)return;
  section.open=true;
  const scroll=()=>section.scrollIntoView({behavior:'smooth',block:'start'});
  if(typeof requestAnimationFrame==='function')requestAnimationFrame(scroll);else scroll();
}
if(typeof window!=='undefined' && typeof window.addEventListener==='function')window.addEventListener('hashchange',openAllCamerasSection);
document.documentElement.dataset.dashboardPage=DASHBOARD_PAGE;
document.title=`${DASHBOARD_PAGE_TITLES[DASHBOARD_PAGE]} · Smart Storage`;
document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('dashboardPageTitle').textContent=DASHBOARD_PAGE_TITLES[DASHBOARD_PAGE];
  for(const link of document.querySelectorAll('[data-dashboard-link]')) {
    const page=link.dataset.dashboardLink;
    link.href=dashboardPageUrl(page,{calibrate:link.hasAttribute('data-calibrate')});
    if(page===DASHBOARD_PAGE && !link.hasAttribute('data-calibrate'))link.setAttribute('aria-current','page');
  }
  for(const view of document.querySelectorAll('[data-dashboard-view]'))view.hidden=view.dataset.dashboardView!==DASHBOARD_PAGE;
  if(DASHBOARD_PAGE==='cctv' && new URLSearchParams(location.search).get('calibrate')==='1')toggleRoiCalibrationMode(true);
  openAllCamerasSection();
});
