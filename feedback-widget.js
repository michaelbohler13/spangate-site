/**
 * SpanGate Feedback Widget
 * Self-contained modal — inject on any page with:
 *   <script src="/feedback-widget.js"></script>
 *
 * Opens with:  openFeedback()  or  openFeedback(event)
 * Closes with: closeFeedback() or Escape or clicking outside
 *
 * Spam protection (layered):
 *   1. Cloudflare Turnstile CAPTCHA — invisible managed challenge
 *   2. Honeypot field — bots fill hidden inputs; humans can't see them
 *   3. localStorage TTL — 10-minute client-side cooldown per browser
 *   4. Server-side IP rate limit — max 3/IP/hour enforced by the backend
 *
 * ─── SETUP ───────────────────────────────────────────────────────────────────
 * Replace TURNSTILE_SITE_KEY_HERE with your real Cloudflare Turnstile site key.
 * Get one free at: https://dash.cloudflare.com → Turnstile → Add site
 * Also set TURNSTILE_SECRET as an env var in your Vercel backend project.
 * ─────────────────────────────────────────────────────────────────────────────
 */
(function () {
  'use strict';

  var TURNSTILE_SITE_KEY = '0x4AAAAAADXEZ0I36bOp0XzS';
  var API      = 'https://spangate-site-r81b.vercel.app/api/v1/feedback';
  var RATE_KEY = 'sg_fb_last';
  var RATE_MS  = 10 * 60 * 1000; // 10 minutes

  var _tsToken    = '';   // current Turnstile token
  var _tsWidgetId = null; // Turnstile widget ID (for reset)

  // ── Inject CSS ─────────────────────────────────────────────────────────────
  var css = document.createElement('style');
  css.textContent = [
    '#sg-fb-overlay{display:none;position:fixed;inset:0;z-index:9999;',
      'background:rgba(0,0,0,.72);backdrop-filter:blur(6px);',
      '-webkit-backdrop-filter:blur(6px);',
      'align-items:center;justify-content:center;}',
    '#sg-fb-overlay.open{display:flex;}',
    '#sg-fb-box{background:#161b22;border:0.5px solid rgba(255,255,255,.1);',
      'border-radius:16px;padding:2rem;width:100%;max-width:480px;',
      'margin:1rem;position:relative;box-shadow:0 24px 64px rgba(0,0,0,.6);}',
    '#sg-fb-close{position:absolute;top:1rem;right:1rem;background:none;',
      'border:none;color:#6e6e73;font-size:1.4rem;line-height:1;cursor:pointer;',
      'padding:2px 8px;border-radius:6px;transition:color .15s;}',
    '#sg-fb-close:hover{color:#f5f5f7;}',
    '#sg-fb-title{font-size:1.05rem;font-weight:600;color:#f5f5f7;',
      'margin:0 0 .2rem;letter-spacing:-.02em;}',
    '#sg-fb-sub{font-size:.76rem;color:#6e6e73;margin:0 0 1.2rem;}',
    '.sg-fb-field{display:flex;flex-direction:column;gap:4px;margin-bottom:11px;}',
    '.sg-fb-field label{font-size:.7rem;color:#8b949e;font-weight:500;',
      'letter-spacing:.02em;text-transform:uppercase;}',
    '.sg-fb-field input,.sg-fb-field select,.sg-fb-field textarea{',
      'background:rgba(255,255,255,.05);border:0.5px solid rgba(255,255,255,.12);',
      'border-radius:8px;color:#f5f5f7;font-size:.82rem;padding:8px 11px;',
      'outline:none;transition:border-color .15s;font-family:inherit;width:100%;',
      'box-sizing:border-box;}',
    '.sg-fb-field input:focus,.sg-fb-field select:focus,',
      '.sg-fb-field textarea:focus{border-color:rgba(0,212,184,.5);}',
    '.sg-fb-field textarea{resize:vertical;min-height:96px;}',
    '.sg-fb-field select option{background:#161b22;}',
    '#sg-fb-hp{position:absolute;left:-9999px;opacity:0;height:0;overflow:hidden;}',
    '#sg-fb-turnstile{margin:10px 0 4px;min-height:65px;display:flex;',
      'align-items:center;}',
    '#sg-fb-submit{width:100%;background:rgba(0,212,184,.12);',
      'border:0.5px solid rgba(0,212,184,.3);border-radius:8px;color:#00d4b8;',
      'font-size:.82rem;font-weight:600;padding:10px;cursor:pointer;',
      'transition:all .15s;font-family:inherit;letter-spacing:-.01em;}',
    '#sg-fb-submit:hover:not(:disabled){background:rgba(0,212,184,.22);}',
    '#sg-fb-submit:disabled{opacity:.45;cursor:not-allowed;}',
    '#sg-fb-msg{font-size:.75rem;text-align:center;margin-top:9px;min-height:18px;}',
    '#sg-fb-msg.ok{color:#00d4b8;}#sg-fb-msg.err{color:#ff6b6b;}',
    '#sg-fb-success{display:none;text-align:center;padding:.75rem 0 .25rem;}',
    '#sg-fb-success-icon{font-size:2.2rem;margin-bottom:.4rem;color:#00d4b8;}',
    '#sg-fb-success h3{font-size:1rem;font-weight:600;color:#f5f5f7;margin:0 0 .2rem;}',
    '#sg-fb-success p{font-size:.76rem;color:#6e6e73;margin:0 0 1rem;}',
    '#sg-fb-done{background:rgba(0,212,184,.12);border:0.5px solid rgba(0,212,184,.3);',
      'border-radius:8px;color:#00d4b8;font-size:.8rem;font-weight:600;',
      'padding:8px 22px;cursor:pointer;font-family:inherit;transition:all .15s;}',
    '#sg-fb-done:hover{background:rgba(0,212,184,.22);}',
    /* light-theme overrides */
    '[data-theme=light] #sg-fb-box{background:#f5f5f7;',
      'border:0.5px solid rgba(0,0,0,.12);}',
    '[data-theme=light] #sg-fb-title{color:#1a1a1a;}',
    '[data-theme=light] .sg-fb-field label{color:#555;}',
    '[data-theme=light] .sg-fb-field input,',
    '[data-theme=light] .sg-fb-field select,',
    '[data-theme=light] .sg-fb-field textarea{background:rgba(0,0,0,.04);',
      'border-color:rgba(0,0,0,.15);color:#1a1a1a;}',
    '[data-theme=light] .sg-fb-field select option{background:#f5f5f7;}',
    '[data-theme=light] #sg-fb-close{color:#999;}',
    '[data-theme=light] #sg-fb-close:hover{color:#1a1a1a;}',
  ].join('');
  document.head.appendChild(css);

  // ── Inject HTML ────────────────────────────────────────────────────────────
  var overlay = document.createElement('div');
  overlay.id = 'sg-fb-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', 'Feedback form');
  overlay.innerHTML =
    '<div id="sg-fb-box">' +
      '<button id="sg-fb-close" aria-label="Close">×</button>' +

      '<div id="sg-fb-form-wrap">' +
        '<h2 id="sg-fb-title">Send Feedback</h2>' +
        '<p id="sg-fb-sub">Questions, bugs, ideas — we read everything.</p>' +
        '<form id="sg-fb-form" novalidate autocomplete="off">' +
          '<div class="sg-fb-field">' +
            '<label for="sg-fb-name">Name</label>' +
            '<input type="text" id="sg-fb-name" name="name" placeholder="Your name" maxlength="100" required>' +
          '</div>' +
          '<div class="sg-fb-field">' +
            '<label for="sg-fb-email">Email</label>' +
            '<input type="email" id="sg-fb-email" name="email" placeholder="you@example.com" maxlength="255" required>' +
          '</div>' +
          '<div class="sg-fb-field">' +
            '<label for="sg-fb-subject">Subject</label>' +
            '<select id="sg-fb-subject" name="subject">' +
              '<option>General Feedback</option>' +
              '<option>Bug Report</option>' +
              '<option>Feature Request</option>' +
              '<option>Help / Support</option>' +
              '<option>Billing</option>' +
              '<option>Other</option>' +
            '</select>' +
          '</div>' +
          '<div class="sg-fb-field">' +
            '<label for="sg-fb-message">Message</label>' +
            '<textarea id="sg-fb-message" name="message" placeholder="Tell us what\'s on your mind…" maxlength="2000" required></textarea>' +
          '</div>' +
          /* Turnstile CAPTCHA widget — Cloudflare renders here */
          '<div id="sg-fb-turnstile"></div>' +
          /* honeypot — hidden from real users via absolute off-screen positioning */
          '<div id="sg-fb-hp" aria-hidden="true">' +
            '<label>Website<input type="text" name="website" tabindex="-1" autocomplete="off" value=""></label>' +
          '</div>' +
          '<button type="submit" id="sg-fb-submit">Send Message</button>' +
          '<div id="sg-fb-msg" role="status" aria-live="polite"></div>' +
        '</form>' +
      '</div>' +

      '<div id="sg-fb-success">' +
        '<div id="sg-fb-success-icon">✓</div>' +
        '<h3>Message sent!</h3>' +
        '<p>Thanks for reaching out. We\'ll get back to you soon.</p>' +
        '<button id="sg-fb-done">Done</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);

  // ── Turnstile helpers ──────────────────────────────────────────────────────
  function _tsTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  function _renderTurnstile() {
    if (typeof window.turnstile === 'undefined') return;
    var container = document.getElementById('sg-fb-turnstile');
    if (!container) return;

    if (_tsWidgetId !== null) {
      // Already rendered — just reset the challenge
      try { window.turnstile.reset(_tsWidgetId); } catch (e) {}
      _tsToken = '';
      return;
    }

    _tsToken    = '';
    _tsWidgetId = window.turnstile.render('#sg-fb-turnstile', {
      sitekey:           TURNSTILE_SITE_KEY,
      theme:             _tsTheme(),
      callback:          function (token) { _tsToken = token; },
      'expired-callback':  function ()      { _tsToken = ''; },
      'error-callback':    function ()      { _tsToken = ''; },
    });
  }

  function _loadTurnstile() {
    if (typeof window.turnstile !== 'undefined') {
      _renderTurnstile();
      return;
    }
    // Already injecting? Wait for it.
    if (document.getElementById('sg-ts-script')) return;

    var s    = document.createElement('script');
    s.id     = 'sg-ts-script';
    s.src    = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    s.async  = true;
    s.onload = function () { _renderTurnstile(); };
    document.head.appendChild(s);
  }

  // ── General helpers ────────────────────────────────────────────────────────
  function setMsg(text, cls) {
    var el = document.getElementById('sg-fb-msg');
    if (!el) return;
    el.textContent = text;
    el.className   = cls || '';
  }

  function resetForm() {
    var f = document.getElementById('sg-fb-form');
    if (f) f.reset();
    setMsg('', '');
    document.getElementById('sg-fb-form-wrap').style.display = '';
    document.getElementById('sg-fb-success').style.display   = 'none';
    var btn = document.getElementById('sg-fb-submit');
    if (btn) btn.disabled = false;
    // Reset Turnstile token + challenge
    _tsToken = '';
    if (_tsWidgetId !== null && typeof window.turnstile !== 'undefined') {
      try { window.turnstile.reset(_tsWidgetId); } catch (e) {}
    }
  }

  // ── Open / close ───────────────────────────────────────────────────────────
  function openFeedback(e) {
    if (e && e.preventDefault) e.preventDefault();
    overlay.classList.add('open');
    _loadTurnstile();
    setTimeout(function () {
      var n = document.getElementById('sg-fb-name');
      if (n) n.focus();
    }, 60);
  }

  function closeFeedback() {
    overlay.classList.remove('open');
    resetForm();
  }

  document.getElementById('sg-fb-close').addEventListener('click', closeFeedback);
  document.getElementById('sg-fb-done').addEventListener('click',  closeFeedback);

  overlay.addEventListener('click', function (e) {
    if (e.target === overlay) closeFeedback();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && overlay.classList.contains('open')) closeFeedback();
  });

  // ── Submit ─────────────────────────────────────────────────────────────────
  document.getElementById('sg-fb-form').addEventListener('submit', function (e) {
    e.preventDefault();

    var name    = (document.getElementById('sg-fb-name').value    || '').trim();
    var email   = (document.getElementById('sg-fb-email').value   || '').trim();
    var subject =  document.getElementById('sg-fb-subject').value;
    var message = (document.getElementById('sg-fb-message').value || '').trim();
    var hp      =  document.querySelector('#sg-fb-hp input').value || '';

    // Basic validation
    if (!name)                        { setMsg('Please enter your name.',       'err'); return; }
    if (!email || !email.includes('@')) { setMsg('Please enter a valid email.', 'err'); return; }
    if (!message)                     { setMsg('Please enter a message.',       'err'); return; }
    if (hp)                           { return; } // honeypot — silently discard

    // CAPTCHA check
    if (!_tsToken) {
      setMsg('Please complete the CAPTCHA above.', 'err');
      return;
    }

    // Client-side rate limit
    var last = parseInt(localStorage.getItem(RATE_KEY) || '0', 10);
    var now  = Date.now();
    if (now - last < RATE_MS) {
      var mins = Math.ceil((RATE_MS - (now - last)) / 60000);
      setMsg('Please wait ' + mins + ' minute' + (mins !== 1 ? 's' : '') + ' before sending another message.', 'err');
      return;
    }

    var btn = document.getElementById('sg-fb-submit');
    btn.disabled = true;
    setMsg('Sending…', '');

    fetch(API, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        name:     name,
        email:    email,
        subject:  subject,
        message:  message,
        website:  hp,
        cf_token: _tsToken,
      }),
    })
    .then(function (res) {
      if (res.ok) {
        localStorage.setItem(RATE_KEY, String(now));
        document.getElementById('sg-fb-form-wrap').style.display = 'none';
        document.getElementById('sg-fb-success').style.display   = 'block';
      } else if (res.status === 429) {
        setMsg('Too many requests. Please try again later.', 'err');
        btn.disabled = false;
      } else if (res.status === 400) {
        setMsg('CAPTCHA failed. Please refresh and try again.', 'err');
        btn.disabled = false;
        if (_tsWidgetId !== null && typeof window.turnstile !== 'undefined') {
          try { window.turnstile.reset(_tsWidgetId); _tsToken = ''; } catch (e) {}
        }
      } else {
        setMsg('Something went wrong. Please try again.', 'err');
        btn.disabled = false;
      }
    })
    .catch(function () {
      setMsg('Network error. Please check your connection and try again.', 'err');
      btn.disabled = false;
    });
  });

  // ── Expose globally ────────────────────────────────────────────────────────
  window.openFeedback  = openFeedback;
  window.closeFeedback = closeFeedback;

}());
