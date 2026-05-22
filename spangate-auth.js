/**
 * spangate-auth.js
 * Shared authentication utility for all SpanGate product pages.
 *
 * USAGE — add to any product page:
 *   <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js"></script>
 *   <script src="../spangate-auth.js"></script>
 *
 * Then call:
 *   SpanGate.isLoggedIn()              → Promise<boolean>
 *   SpanGate.hasAccess('configwizard') → Promise<boolean>
 *   SpanGate.getUser()                 → Promise<User|null>
 *   SpanGate.requireAccess('configwizard') → redirects to login if no access
 *   SpanGate.signOut()                 → signs out + redirects to login
 *   SpanGate.getLoginUrl('configwizard')   → string URL with ?return= param
 *
 * PRODUCT IDs:
 *   'configwizard'   → Config Wizard
 *   'netmonitor'     → Network Monitor
 *   'assettracker'   → Asset Tracker
 *
 * BACKWARD COMPATIBILITY:
 *   isProUser() is re-exported as SpanGate.isProUser() for Config Wizard pages.
 *   When Stripe is integrated, update product_access in user metadata
 *   via Stripe webhook — no changes needed in product pages.
 */

(function () {
  'use strict';

  const SUPABASE_URL  = 'https://tiviipaamwvfjvoscnyk.supabase.co';
  const SUPABASE_ANON = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRpdmlpcGFhbXd2Zmp2b3NjbnlrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg1NDEyODIsImV4cCI6MjA5NDExNzI4Mn0.wBicrt3P5QzQw8sYA87CYxEv--I-KlhnqrsMfcw2irs';

  // Detect path depth so login URL is correct regardless of nesting
  function getLoginUrl(productId) {
    // Count slashes to determine relative depth
    const path  = window.location.pathname;
    const depth = (path.match(/\//g) || []).length;
    // spangate.com/configwizard/wizard.html → depth 2 → ../login.html
    // spangate.com/login.html               → depth 1 → ./login.html
    const prefix = depth >= 2 ? '../' : './';
    return productId
      ? `${prefix}login.html?return=${productId}`
      : `${prefix}login.html`;
  }

  // Init Supabase client (share with any existing window.supabase client if present)
  let _client;
  function getClient() {
    if (_client) return _client;
    if (typeof supabase !== 'undefined') {
      _client = supabase.createClient(SUPABASE_URL, SUPABASE_ANON);
      return _client;
    }
    console.error('[SpanGate Auth] Supabase JS not loaded. Add the CDN script before spangate-auth.js');
    return null;
  }

  // Core: get current session
  async function getSession() {
    const client = getClient();
    if (!client) return null;
    const { data } = await client.auth.getSession();
    return data.session || null;
  }

  // Core: check product access
  async function hasAccess(productId) {
    const session = await getSession();
    if (!session) return false;

    const access = session.user.user_metadata?.product_access || {};

    // Backward compat: existing users with no metadata → CW Pro
    if (!Object.keys(access).length) {
      return productId === 'configwizard';
    }

    return !!(access[productId] && access[productId].active);
  }

  // Core: get user plan for a product
  async function getPlan(productId) {
    const session = await getSession();
    if (!session) return null;
    const access = session.user.user_metadata?.product_access || {};
    return access[productId]?.plan || null;
  }

  /**
   * requireAccess(productId)
   * Call at the top of any protected page.
   * If user is not logged in → redirect to login with ?return=productId
   * If user is logged in but no access → redirect to login with ?return=productId
   * If user has access → resolves with user object
   */
  async function requireAccess(productId) {
    const session = await getSession();

    if (!session) {
      window.location.href = getLoginUrl(productId);
      return null;
    }

    const ok = await hasAccess(productId);
    if (!ok) {
      window.location.href = getLoginUrl(productId);
      return null;
    }

    return session.user;
  }

  /**
   * requireLogin()
   * Simpler version — just requires being logged in, not product-specific.
   * Used for pages like dashboard.html that any logged-in user can see.
   */
  async function requireLogin(productId) {
    const session = await getSession();
    if (!session) {
      window.location.href = getLoginUrl(productId || null);
      return null;
    }
    return session.user;
  }

  /**
   * signOut(redirectUrl)
   * Signs out and redirects to login page.
   */
  async function signOut(redirectUrl) {
    const client = getClient();
    if (client) await client.auth.signOut();
    window.location.href = redirectUrl || getLoginUrl(null);
  }

  /**
   * renderNavAuth(containerId, productId, accentColor)
   * Renders a sign-in / avatar nav button into a container element.
   * accentColor: e.g. '#58a6ff' for CW, '#00d4b8' for NM, '#c49a4a' for AT
   */
  async function renderNavAuth(containerId, productId, accentColor) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const session = await getSession();
    const color   = accentColor || '#58a6ff';

    if (!session) {
      el.innerHTML = `
        <a href="${getLoginUrl(productId)}" style="
          color: #8b949e; font-size: 0.82rem; font-weight: 500;
          text-decoration: none; padding: 6px 12px;
          transition: color .15s;
          font-family: 'Inter', sans-serif;
        " onmouseover="this.style.color='#e6edf3'" onmouseout="this.style.color='#8b949e'">
          Sign in
        </a>
        <a href="${getLoginUrl(productId)}" style="
          background: ${color}; color: #0d1117; border-radius: 20px;
          padding: 6px 16px; font-size: 0.82rem; font-weight: 700;
          text-decoration: none; font-family: 'Inter', sans-serif;
        ">
          Get started
        </a>
      `;
    } else {
      const email = session.user.email || '';
      const initials = email.substring(0, 2).toUpperCase();
      const access = session.user.user_metadata?.product_access || {};
      const hasThisProduct = !Object.keys(access).length
        ? productId === 'configwizard'
        : !!(access[productId] && access[productId].active);

      el.innerHTML = `
        <span style="
          color: ${hasThisProduct ? color : '#8b949e'};
          font-size: 0.8rem; font-family: 'JetBrains Mono', monospace;
          margin-right: 8px;
        ">
          ${hasThisProduct ? '● Active' : '○ No access'}
        </span>
        <div style="position:relative; display:inline-block;">
          <div onclick="document.getElementById('sg-user-menu').classList.toggle('visible')" style="
            width: 30px; height: 30px; border-radius: 50%;
            background: ${color}22; border: 1.5px solid ${color}66;
            display: flex; align-items: center; justify-content: center;
            font-size: 0.7rem; font-weight: 700; color: ${color};
            cursor: pointer; font-family: 'Inter', sans-serif;
            user-select: none;
          ">${initials}</div>
          <div id="sg-user-menu" style="
            display: none; position: absolute; right: 0; top: 38px;
            background: #1a1a1a; border: 1px solid rgba(255,255,255,0.12);
            border-radius: 10px; padding: 8px; min-width: 200px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.5);
            font-family: 'Inter', sans-serif;
            z-index: 999;
          ">
            <div style="padding: 6px 10px 10px; border-bottom: 1px solid rgba(255,255,255,0.08); margin-bottom: 6px;">
              <div style="font-size: 0.75rem; font-weight: 600; color: #e6edf3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${email}</div>
              <div style="font-size: 0.68rem; color: #8b949e; font-family: 'JetBrains Mono', monospace; margin-top: 2px;">
                ${hasThisProduct ? '● Active' : '○ Not subscribed'}
              </div>
            </div>
            <a href="${getLoginUrl(null)}" style="
              display: block; padding: 7px 10px; font-size: 0.8rem;
              color: #8b949e; text-decoration: none; border-radius: 6px;
              transition: background .15s;
            " onmouseover="this.style.background='rgba(255,255,255,0.05)'" onmouseout="this.style.background='transparent'">
              My Account
            </a>
            <div onclick="window.SpanGate.signOut()" style="
              padding: 7px 10px; font-size: 0.8rem; color: #8b949e;
              cursor: pointer; border-radius: 6px; transition: background .15s;
            " onmouseover="this.style.background='rgba(255,255,255,0.05)'" onmouseout="this.style.background='transparent'">
              Sign out
            </div>
          </div>
        </div>
      `;

      // Close menu on outside click
      document.addEventListener('click', function(e) {
        const menu = document.getElementById('sg-user-menu');
        if (menu && !el.contains(e.target)) menu.classList.remove('visible');
      });

      // Add .visible CSS toggle (display:block)
      const style = document.createElement('style');
      style.textContent = '#sg-user-menu.visible { display: block !important; }';
      document.head.appendChild(style);
    }
  }

  // Backward compat: isProUser for Config Wizard pages
  async function isProUser() {
    return hasAccess('configwizard');
  }

  // Public API
  window.SpanGate = {
    isLoggedIn:   async () => !!(await getSession()),
    getUser:      async () => { const s = await getSession(); return s?.user || null; },
    getSession,
    hasAccess,
    getPlan,
    requireAccess,
    requireLogin,
    signOut,
    getLoginUrl,
    renderNavAuth,
    isProUser,         // backward compat
  };

})();
