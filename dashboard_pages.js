/* Separate URL-addressable pages share the existing camera controls and bridge client. */
function dashboardPageForPath(pathname) {
  const path=pathname.replace(/\/+$/,'');
  if(['/cctv-configuration','/local-dashboard/cctv-configuration'].includes(path))return 'cctv';
  if(['/esp32-cams','/local-dashboard/esp32-cams'].includes(path))return 'esp32';
  return 'dashboard';
}
function isLocalDashboardPath(pathname) {
  return /^\/local-dashboard(?:\/(?:cctv-configuration|esp32-cams))?\/?$/.test(pathname);
}
function dashboardPageUrl(page,{calibrate=false}={}) {
  const local=isLocalDashboardPath(location.pathname);
  const paths={dashboard:local?'/local-dashboard':'/',cctv:(local?'/local-dashboard':'')+'/cctv-configuration',
    esp32:(local?'/local-dashboard':'')+'/esp32-cams'};
  if(!Object.hasOwn(paths,page))throw Error('Unknown dashboard page');
  const query=new URLSearchParams(location.search);
  query.delete('calibrate');
  if(calibrate)query.set('calibrate','1');
  return paths[page]+(query.size?'?'+query:'');
}
function navigateToDashboardPage(page,options={}) {
  if(page==='cctv' && dashboardPageForPath(location.pathname)==='cctv') {
    if(options.calibrate)toggleRoiCalibrationMode(true);
    document.getElementById('cctvCameraManagerPanel').scrollIntoView({behavior:'smooth'});
    return;
  }
  location.assign(dashboardPageUrl(page,options));
}
const DASHBOARD_PAGE=dashboardPageForPath(location.pathname);
const DASHBOARD_PAGE_TITLES={dashboard:'Inventory Dashboard',cctv:'CCTV Configuration',esp32:'ESP32 CAMs'};
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
});
