const updateCrmMenuTabStop = () => {
  const toggle = document.getElementById("crm-menu-toggle");
  if (!toggle || !window.matchMedia) {
    return;
  }

  const mobileNavigation = window.matchMedia("(max-width: 980px)");
  const syncMenuTabStop = () => {
    toggle.tabIndex = mobileNavigation.matches ? 0 : -1;
  };

  syncMenuTabStop();
  if (mobileNavigation.addEventListener) {
    mobileNavigation.addEventListener("change", syncMenuTabStop);
  } else {
    mobileNavigation.addListener(syncMenuTabStop);
  }
};

if (document.readyState !== "loading") {
  updateCrmMenuTabStop();
} else {
  document.addEventListener("DOMContentLoaded", updateCrmMenuTabStop);
}
