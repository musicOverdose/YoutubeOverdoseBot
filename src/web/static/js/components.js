/**
 * YTDL UI Components
 * Toast notification system + Confirm dialog
 * No external dependencies. No emoji.
 */

// ============================================================
// Toast system
// ============================================================

const TOAST_SVG = {
  success: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`,
  error:   `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
  warning: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
  info:    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
};

const CLOSE_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;

/**
 * Show a toast notification.
 * @param {string} message - Primary message text.
 * @param {'success'|'error'|'warning'|'info'} type
 * @param {string} [title] - Optional bold title line.
 * @param {number} [duration=4000] - Auto-dismiss delay in ms. 0 = no auto-dismiss.
 */
function showToast(message, type = 'info', title = '', duration = 4000) {
  const region = document.getElementById('toast-region');
  if (!region) return;

  const toast = document.createElement('div');
  toast.className = `toast toast--${type}`;
  toast.setAttribute('role', 'status');
  toast.setAttribute('aria-atomic', 'true');

  const titleHtml = title
    ? `<div class="toast__title">${escapeHtmlSafe(title)}</div>`
    : '';

  toast.innerHTML = `
    <div class="toast__icon">${TOAST_SVG[type] || TOAST_SVG.info}</div>
    <div class="toast__body">
      ${titleHtml}
      <div class="toast__msg">${escapeHtmlSafe(message)}</div>
    </div>
    <button class="toast__close" aria-label="Dismiss notification">${CLOSE_SVG}</button>
  `;

  const closeBtn = toast.querySelector('.toast__close');
  const dismiss = () => {
    toast.classList.add('removing');
    toast.addEventListener('transitionend', () => toast.remove(), { once: true });
  };

  closeBtn.addEventListener('click', dismiss);

  region.appendChild(toast);

  if (duration > 0) {
    setTimeout(dismiss, duration);
  }

  return toast;
}

// Convenience wrappers
function toastSuccess(msg, title = '') { return showToast(msg, 'success', title); }
function toastError(msg, title = '')   { return showToast(msg, 'error',   title, 6000); }
function toastWarning(msg, title = '') { return showToast(msg, 'warning', title, 5000); }
function toastInfo(msg, title = '')    { return showToast(msg, 'info',    title); }

function escapeHtmlSafe(str) {
  if (typeof str !== 'string') str = String(str || '');
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ============================================================
// Confirm dialog
// ============================================================

let _confirmResolve = null;

function initConfirmDialog() {
  const dialog = document.getElementById('confirm-dialog');
  if (!dialog || dialog.dataset.bound === 'true') return;
  dialog.dataset.bound = 'true';

  const okBtn = document.getElementById('confirm-ok');
  const cancelBtn = document.getElementById('confirm-cancel');

  const close = (result) => {
    dialog.classList.remove('active');
    if (_confirmResolve) {
      const resolve = _confirmResolve;
      _confirmResolve = null;
      resolve(result);
    }
  };

  okBtn?.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    close(true);
  });

  cancelBtn?.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    close(false);
  });

  // Close on backdrop click
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog) close(false);
  });

  // Close on Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && dialog.classList.contains('active')) close(false);
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initConfirmDialog);
} else {
  initConfirmDialog();
}

/**
 * Show a confirmation dialog. Returns a Promise<boolean>.
 * @param {string} message
 * @param {string} [title='Confirm Action']
 * @param {string} [okLabel='Confirm']
 * @param {'btn-danger'|'btn-warning'|'btn-primary'} [okClass='btn-danger']
 */
function showConfirm(message, title = 'Confirm Action', okLabel = 'Confirm', okClass = 'btn-danger') {
  initConfirmDialog();
  const dialog = document.getElementById('confirm-dialog');
  if (!dialog) {
    return Promise.resolve(window.confirm(message));
  }

  return new Promise((resolve) => {
    const titleEl = document.getElementById('confirm-title');
    const msgEl = document.getElementById('confirm-message');
    const okBtn = document.getElementById('confirm-ok');

    // Clean up any previous resolve
    if (_confirmResolve) _confirmResolve(false);
    _confirmResolve = resolve;

    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = message;
    if (okBtn) {
      okBtn.textContent = okLabel;
      okBtn.className = `btn ${okClass}`;
    }

    dialog.classList.add('active');
  });
}

// ============================================================
// Alert strip helpers (inline feedback inside sections)
// ============================================================

const ALERT_DURATIONS = {
  success: 4000,
  info: 4000,
  warning: 5000,
  error: 7000,
};

const _alertTimers = {};

/**
 * Show an inline alert strip with automatic dismissal.
 * @param {string} id - Element ID
 * @param {string} message
 * @param {'success'|'error'|'warning'|'info'} type
 * @param {number|null} [autoClear=null] - ms to auto-hide; null uses default duration
 */
function showAlert(id, message, type = 'info', autoClear = null) {
  const el = document.getElementById(id);
  if (!el) return;

  if (_alertTimers[id]) {
    clearTimeout(_alertTimers[id]);
    delete _alertTimers[id];
  }

  const typeMap = {
    success: 'alert-strip--success',
    error:   'alert-strip--danger',
    warning: 'alert-strip--warning',
    info:    'alert-strip--info',
  };

  const icon = TOAST_SVG[type === 'error' ? 'error' : type] || TOAST_SVG.info;

  el.innerHTML = `${icon}<span>${escapeHtmlSafe(message)}</span>`;
  el.className = `alert-strip ${typeMap[type] || typeMap.info}`;
  el.classList.remove('hidden');

  const duration = (autoClear !== null && autoClear !== undefined && autoClear > 0)
    ? autoClear
    : (ALERT_DURATIONS[type] || 5000);

  _alertTimers[id] = setTimeout(() => {
    hideAlert(id);
  }, duration);
}

function hideAlert(id) {
  if (_alertTimers[id]) {
    clearTimeout(_alertTimers[id]);
    delete _alertTimers[id];
  }
  const el = document.getElementById(id);
  if (el) {
    el.classList.add('hidden');
    el.innerHTML = '';
  }
}

function clearAllAlerts() {
  document.querySelectorAll('.alert-strip').forEach((el) => {
    if (el.id && el.id !== 'migration-warning') {
      hideAlert(el.id);
    }
  });
}

function setupInputAlertClearers() {
  const mappings = [
    { containerId: 'sec-telegram', alertId: 'telegram-alert' },
    { containerId: 'tg-welcome-msg-area', alertId: 'tg-welcome-alert' },
    { containerId: 'sec-ai', alertId: 'ai-alert' },
    { containerId: 'sec-settings', alertId: 'settings-alert' },
    { containerId: 'sec-youtube', alertId: 'youtube-alert' },
    { containerId: 'sec-cookies', alertId: 'cookies-alert' },
    { containerId: 'sec-must-join', alertId: 'must-join-alert' },
    { containerId: 'sec-must-join', alertId: 'mj-exempt-alert' },
    { containerId: 'modal-add-channel', alertId: 'add-channel-alert' },
    { containerId: 'login-form', alertId: 'login-error' },
  ];

  mappings.forEach(({ containerId, alertId }) => {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.addEventListener('input', () => hideAlert(alertId), { passive: true });
    el.addEventListener('change', () => hideAlert(alertId), { passive: true });
  });
}

document.addEventListener('DOMContentLoaded', () => {
  setupInputAlertClearers();
});


// ============================================================
// Mobile sidebar toggle
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
  const menuBtn  = document.getElementById('mobile-menu-btn');
  const sidebar  = document.getElementById('sidebar');
  const overlay  = document.getElementById('sidebar-overlay');

  const openSidebar = () => {
    sidebar?.classList.add('open');
    overlay?.classList.add('active');
  };

  const closeSidebar = () => {
    sidebar?.classList.remove('open');
    overlay?.classList.remove('active');
  };

  menuBtn?.addEventListener('click', openSidebar);
  overlay?.addEventListener('click', closeSidebar);

  // Close sidebar on nav item click (mobile)
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.addEventListener('click', () => {
      if (window.innerWidth <= 768) closeSidebar();
    });
  });
});
