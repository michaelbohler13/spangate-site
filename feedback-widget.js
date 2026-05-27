/**
 * SpanGate Feedback Widget
 * Self-contained modal — inject on any page with:
 *   <script src="/feedback-widget.js"></script>
 *
 * Opens with:  openFeedback()  or  openFeedback(event)
 * Closes with: closeFeedback() or Escape or clicking outside
 *
 * Spam protection:
 *   1. Honeypot field   — bots fill hidden inputs; humans can't see them
 *   2. localStorage TTL — 10-minute client-side cooldown per browser
 *   3. Server-side rate limit — max 3/IP/hour enforced by the backend
 */
(function () {
  'use strict';

  const API       = 'https://spangate-site-r81b.vercel.app/api/v1/feedback';
  const RATE_KEY  = 'sg_fb_last';
  const RATE_MS   = 10 * 60 * 1000; // 10 minutes

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
    '#sg-fb-submit{width:100%;background:rgba(0,212,184,.12);',
      'border:0.5px solid rgba(0,212,184,.3);border-radius:8px;color:#00d4b8;',
      'font-size:.82rem;font-weight:600;padding:10px;cursor:pointer;',
      'transition:all .15s;margin-top:2px;font-family:inherit;letter-spacing:-.01em;}',
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
        '<p>Thanks for reaching out. We’ll get back to you soon.</p>' +
        '<button id="sg-fb-done">Done</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);

  // ── Helpers ────────────────────────────────────────────────────────────────
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
  }

  // ── Open / close ───────────────────────────────────────────────────────────
  function openFeedback(e) {
    if (e && e.preventDefault) e.preventDefault();
    overlay.classList.add('open');
    // Delay focus until the overlay is visible
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

  // ── Submit ────────────────────────────────────────────────────────────────
  document.getElementById('sg-fb-form').addEventListener('submit', function (e) {
    e.preventDefault();

    var name    = (document.getElementById('sg-fb-name').value    || '').trim();
    var email   = (document.getElementById('sg-fb-email').value   || '').trim();
    var subject =  document.getElementById('sg-fb-subject').value;
    var message = (document.getElementById('sg-fb-message').value || '').trim();
    var hp      =  document.querySelector('#sg-fb-hp input').value || '';

    // Client-side validation
    if (!name)                        { setMsg('Please enter your name.',         'err'); return; }
    if (!email || !email.includes('@')) { setMsg('Please enter a valid email.',   'err'); return; }
    if (!message)                     { setMsg('Please enter a message.',         'err'); return; }
    if (hp)                           { return; } // honeypot — silently discard

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
      body:    JSON.stringify({ name: name, email: email, subject: subject, message: message, website: hp }),
    })
    .then(function (res) {
      if (res.ok) {
        localStorage.setItem(RATE_KEY, String(now));
        document.getElementById('sg-fb-form-wrap').style.display = 'none';
        document.getElementById('sg-fb-success').style.display   = 'block';
      } else if (res.status === 429) {
        setMsg('Too many requests. Please try again later.', 'err');
        btn.disabled = false;
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
