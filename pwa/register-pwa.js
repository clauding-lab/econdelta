// EconDelta — PWA registration + iOS install hint
// ------------------------------------------------------------
// 1. Registers sw.js on first load (only over https / localhost — file:// is a no-op).
// 2. On iOS Safari, shows a one-time "Add to Home Screen" hint that dismisses
//    permanently after the user taps the close button or installs.
//
// iOS does NOT support the beforeinstallprompt event, so the only path is
// the user manually choosing Share → Add to Home Screen. The hint banner
// just nudges them in that direction.

(function(){
  // -------------------------------------------------- 1. Register SW
  if('serviceWorker' in navigator){
    window.addEventListener('load', () => {
      // Resolve sw.js relative to the current page so it works under any
      // subpath (e.g. /econdelta/ on GitHub Pages).
      navigator.serviceWorker.register('sw.js').catch(err => {
        console.warn('[pwa] sw register failed', err);
      });
    });
  }

  // -------------------------------------------------- 2. iOS install hint
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  const isStandalone = window.navigator.standalone === true;
  const dismissedAt = +localStorage.getItem('ed_a2hs_dismissed_at') || 0;
  const TWO_WEEKS = 14 * 24 * 3600 * 1000;
  const recentlyDismissed = dismissedAt && (Date.now() - dismissedAt) < TWO_WEEKS;

  if(isIOS && !isStandalone && !recentlyDismissed){
    document.addEventListener('DOMContentLoaded', showA2HSHint);
  }

  function showA2HSHint(){
    const hint = document.createElement('div');
    hint.className = 'ed-a2hs';
    hint.innerHTML = `
      <div class="ed-a2hs-inner">
        <div class="ed-a2hs-msg">
          <b>Install EconDelta on your home screen</b>
          <span>Tap <span class="ed-a2hs-icon">${SHARE_SVG}</span> Share, then <i>Add to Home Screen</i></span>
        </div>
        <button class="ed-a2hs-close" aria-label="Dismiss">×</button>
      </div>
    `;
    hint.querySelector('.ed-a2hs-close').addEventListener('click', () => {
      localStorage.setItem('ed_a2hs_dismissed_at', String(Date.now()));
      hint.remove();
    });
    document.body.appendChild(hint);
  }

  const SHARE_SVG = `
    <svg viewBox="0 0 16 22" width="11" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M8 14V2"/>
      <path d="M3 7l5-5 5 5"/>
      <path d="M2 10v9a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1v-9"/>
    </svg>
  `;

  // -------------------------------------------------- 3. Hint styles
  const css = `
    .ed-a2hs{
      position: fixed;
      left: 12px; right: 12px; bottom: 12px;
      z-index: 9999;
      pointer-events: none;
      animation: ed-a2hs-in .35s ease-out;
    }
    @keyframes ed-a2hs-in {
      from { opacity: 0; transform: translateY(20px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    .ed-a2hs-inner{
      background: #0e1418;
      color: #fdfbf3;
      border-radius: 14px;
      box-shadow: 0 10px 30px rgba(0,0,0,.4);
      padding: 14px 16px;
      display: flex;
      align-items: center;
      gap: 12px;
      pointer-events: auto;
      font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, sans-serif;
    }
    .ed-a2hs-msg{
      flex: 1; line-height: 1.4;
      font-size: 13px;
    }
    .ed-a2hs-msg b{
      display: block; font-weight: 600; margin-bottom: 2px;
    }
    .ed-a2hs-msg span{
      color: #b8b0a8; font-size: 12px;
    }
    .ed-a2hs-msg i{ font-style: italic; }
    .ed-a2hs-icon{
      display: inline-flex;
      align-items: center;
      vertical-align: -2px;
      color: #4d8fec;
      margin: 0 1px;
    }
    .ed-a2hs-close{
      background: none; border: none;
      color: #7a7470; font-size: 22px; line-height: 1;
      cursor: pointer; padding: 4px 8px;
      flex-shrink: 0;
    }
    .ed-a2hs-close:hover{ color: #fdfbf3; }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);
})();
