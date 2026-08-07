/* ============================================================
   GED WAKATI — bascule du thème
   ============================================================ */

(function () {
  'use strict';

  var CLE = 'ged-theme';

  function appliquer(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    var lbl = document.getElementById('libelle-theme');
    var ico = document.getElementById('icone-theme');
    if (lbl) { lbl.textContent = theme === 'sombre' ? 'Mode clair' : 'Mode sombre'; }
    if (ico) { ico.textContent = theme === 'sombre' ? '☀' : '☾'; }
  }

  function themeInitial() {
    var enregistre = null;
    try { enregistre = localStorage.getItem(CLE); } catch (e) { /* stockage indisponible */ }
    if (enregistre) { return enregistre; }
    // À défaut de préférence enregistrée, on suit le réglage du système.
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return 'sombre';
    }
    return 'clair';
  }

  window.basculerTheme = function () {
    var actuel = document.documentElement.getAttribute('data-theme') || 'clair';
    var nouveau = actuel === 'sombre' ? 'clair' : 'sombre';
    appliquer(nouveau);
    try { localStorage.setItem(CLE, nouveau); } catch (e) { /* non mémorisé */ }
  };

  // Application immédiate : évite le clignotement blanc au chargement.
  appliquer(themeInitial());

  document.addEventListener('DOMContentLoaded', function () {
    appliquer(document.documentElement.getAttribute('data-theme') || 'clair');
  });
})();
