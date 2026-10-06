// Switching layout reloads the page: ask first if the form below has unsaved edits.
(function () {
  var main = document.querySelector('form[action$="/admin/save"]');
  var lf = document.getElementById('layout-form');
  if (!main || !lf) return;
  var dirty = false;
  main.addEventListener('input', function () { dirty = true; });
  lf.addEventListener('submit', function (e) {
    if (dirty && !confirm(lf.dataset.confirm)) e.preventDefault();
  });
})();
