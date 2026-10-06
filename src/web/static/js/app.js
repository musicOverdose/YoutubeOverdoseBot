let activeSection = 'dashboard';
let pollInterval = null;

document.addEventListener('DOMContentLoaded', () => {
  initNavigation();
  checkAuth();
  loadSection(activeSection);
  startPolling();

  // Handle Login form
  document.getElementById('login-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const u = document.getElementById('login-username').value;
    const p = document.getElementById('login-password').value;
    const errBox = document.getElementById('login-error');
    hideAlert('login-error');
    try {
      await API.post('/api/auth/login', { username: u, password: p });
      document.getElementById('login-modal').classList.remove('active');
      loadSection(activeSection);
    } catch (err) {
      showAlert('login-error', err.message || 'Login failed', 'error');
    }
  });
});

function initNavigation() {
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.addEventListener('click', () => {
      clearAllAlerts();
      document.querySelectorAll('.nav-item').forEach((n) => {
        n.classList.remove('active');
        n.removeAttribute('aria-current');
      });
      document.querySelectorAll('.section').forEach((s) => s.classList.remove('active'));

      item.classList.add('active');
      item.setAttribute('aria-current', 'page');
      activeSection = item.getAttribute('data-section');
      document.getElementById(`sec-${activeSection}`)?.classList.add('active');
      document.getElementById('page-title').textContent = item.textContent.trim();
      loadSection(activeSection);
    });
  });

  document.getElementById('btn-logout')?.addEventListener('click', async () => {
    await API.post('/api/auth/logout', {});
    location.reload();
  });
}

async function checkAuth() {
  try {
    const me = await API.get('/api/auth/me');
    document.getElementById('admin-username-display').textContent = me.username;
  } catch (err) {
    document.getElementById('login-modal').classList.add('active');
  }
}

function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(() => {
    if (activeSection === 'dashboard') loadDashboard();
    else if (activeSection === 'queue') loadQueue();
    else if (activeSection === 'logs') loadLogs();
  }, 3000);
}

function loadSection(name) {
  switch (name) {
    case 'dashboard': loadDashboard(); break;
    case 'queue':     loadQueue();     break;
    case 'jobs':      loadJobs();      break;
    case 'cache':     loadCache();     break;
    case 'users':     loadUsers();     break;
    case 'telegram':  loadTelegramConfig(); break;
    case 'must-join': loadMustJoin();  break;
    case 'youtube':   loadYouTube();   break;
    case 'cookies':   loadCookies();   break;
    case 'ai':        loadAI();        break;
    case 'settings':  loadSettings();  break;
    case 'system':    loadSystem();    break;
    case 'logs':      loadLogs();      break;
    case 'audit':     loadAudit();     break;
  }
}

// 1. DASHBOARD
async function loadDashboard() {
  try {
    const data = await API.get('/api/dashboard/stats');
    document.getElementById('stat-cpu').textContent = `${data.system.cpu_percent}%`;
    document.getElementById('stat-ram').textContent = `${data.system.ram_used_gb} / ${data.system.ram_total_gb} GB (${data.system.ram_percent}%)`;
    document.getElementById('stat-disk').textContent = `${data.system.disk_used_gb} / ${data.system.disk_total_gb} GB`;
    document.getElementById('stat-temp').textContent = `${data.system.temp_used_gb} GB`;

    document.getElementById('stat-active-jobs').textContent = `${data.queue.active_count} / ${data.queue.max_active}`;
    document.getElementById('stat-queue-len').textContent = data.queue.queued_count;
    document.getElementById('stat-cache-entries').textContent = data.cache.entries_count;
    document.getElementById('stat-cache-hitrate').textContent = `${data.cache.hit_rate_pct}%`;
    document.getElementById('stat-completed').textContent = data.jobs.completed;
    document.getElementById('stat-failed').textContent = data.jobs.failed;

    if (document.getElementById('stat-max-size')) {
      document.getElementById('stat-max-size').textContent = data.settings.max_video_size_formatted || `${data.settings.max_video_file_size_mb_local || 1900} MB`;
    }
    if (document.getElementById('stat-max-dur')) {
      document.getElementById('stat-max-dur').textContent = data.settings.max_duration_formatted || '02:00:00';
    }
    document.getElementById('stat-ai-status').textContent = data.settings.ai_enabled
      ? (data.settings.ai_configured ? 'ON (Ready)' : 'ON (Not Configured)') : 'OFF';
    document.getElementById('stat-cookie-status').textContent = data.settings.cookies_enabled ? 'ON' : 'OFF';
    document.getElementById('stat-mj-status').textContent = data.settings.must_join_enabled
      ? `ON (${data.settings.required_channels_count} channels)` : 'OFF';

    document.getElementById('v-app').textContent    = data.tools.application;
    document.getElementById('v-py').textContent     = data.tools.python;
    document.getElementById('v-ytdlp').textContent  = data.tools.ytdlp;
    document.getElementById('v-ejs').textContent    = data.tools.ytdlp_ejs;
    document.getElementById('v-ffmpeg').textContent = data.tools.ffmpeg;
    document.getElementById('v-deno').textContent   = data.tools.deno;
  } catch (err) {}
}

// 2. QUEUE

const PAUSE_SVG  = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px" aria-hidden="true"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>`;
const RESUME_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px" aria-hidden="true"><polygon points="5 3 19 12 5 21 5 3"/></svg>`;

async function loadQueue() {
  try {
    const data = await API.get('/api/queue');
    const pauseBtn = document.getElementById('btn-queue-pause');
    if (data.is_paused) {
      pauseBtn.innerHTML = `${RESUME_SVG} Resume Queue`;
      pauseBtn.className = 'btn btn-success';
    } else {
      pauseBtn.innerHTML = `${PAUSE_SVG} Pause Queue`;
      pauseBtn.className = 'btn btn-warning';
    }
    document.getElementById('queue-max-active-input').value = data.max_active_jobs;

    // Active Jobs Table
    const activeTbody = document.getElementById('tbody-active-jobs');
    if (data.active_jobs.length === 0) {
      activeTbody.innerHTML = '<tr class="empty-row"><td colspan="6">No active jobs currently running</td></tr>';
    } else {
      activeTbody.innerHTML = data.active_jobs.map(j => `
        <tr>
          <td><code>${j.id.slice(0, 8)}</code></td>
          <td><b>${escapeHtml(j.title)}</b></td>
          <td>${j.operation}${j.codec ? ` · ${j.codec}` : ''}${j.resolution ? ` ${j.resolution}` : ''}</td>
          <td><span class="badge badge-info">${j.stage}</span></td>
          <td>
            <span class="font-mono font-sm">${j.progress}%${j.speed ? ` (${j.speed})` : ''}${j.eta ? ` ETA: ${j.eta}` : ''}</span>
            <div class="progress-wrap"><div class="progress-fill" style="width:${j.progress}%"></div></div>
          </td>
          <td><button class="btn btn-danger btn-sm" onclick="cancelJob('${j.id}')">Cancel</button></td>
        </tr>
      `).join('');
    }

    // Queued Jobs Table
    const queuedTbody = document.getElementById('tbody-queued-jobs');
    if (data.queued_jobs.length === 0) {
      queuedTbody.innerHTML = '<tr class="empty-row"><td colspan="6">Queue is empty</td></tr>';
    } else {
      queuedTbody.innerHTML = data.queued_jobs.map(j => `
        <tr>
          <td><b>#${j.position}</b></td>
          <td><code>${j.id.slice(0, 8)}</code></td>
          <td><b>${escapeHtml(j.title)}</b></td>
          <td>${j.operation}${j.codec ? ` · ${j.codec}` : ''}${j.resolution ? ` ${j.resolution}` : ''}</td>
          <td class="font-sm">${formatDate(j.queued_at)}</td>
          <td><button class="btn btn-danger btn-sm" onclick="cancelJob('${j.id}')">Cancel</button></td>
        </tr>
      `).join('');
    }
  } catch (err) {}
}

async function toggleQueuePause() {
  const data = await API.get('/api/queue');
  if (data.is_paused) {
    await API.post('/api/queue/resume', {});
  } else {
    await API.post('/api/queue/pause', {});
  }
  loadQueue();
}

async function updateConcurrency() {
  const val = parseInt(document.getElementById('queue-max-active-input').value, 10);
  try {
    const res = await API.post('/api/queue/concurrency', { max_active_jobs: val });
    if (res.warning) toastWarning(res.warning, 'Queue Warning');
    else toastSuccess(`Max concurrency set to ${val}`);
    loadQueue();
  } catch (err) {
    toastError(err.message, 'Concurrency Error');
  }
}

async function cancelJob(id) {
  const confirmed = await showConfirm(`Cancel job ${id.slice(0, 8)}…?`, 'Cancel Job', 'Cancel Job', 'btn-danger');
  if (confirmed) {
    await API.post(`/api/queue/cancel/${id}`, {});
    loadQueue();
  }
}

async function clearQueue() {
  const confirmed = await showConfirm(
    'Are you sure you want to cancel all waiting jobs and clear the queue? Currently active/processing jobs will NOT be interrupted.',
    'Clear Queue',
    'Clear Queue',
    'btn-danger'
  );
  if (!confirmed) return;

  try {
    const res = await API.post('/api/queue/clear', {});
    toastSuccess(res.message || `Cleared ${res.cancelled_jobs_count || 0} queued jobs`, 'Queue Cleared');
    loadQueue();
  } catch (err) {
    toastError(err.message || 'Failed to clear queue', 'Error');
  }
}

// 3. JOBS
async function loadJobs() {
  const search = document.getElementById('jobs-search')?.value || '';
  const status = document.getElementById('jobs-filter-status')?.value || '';
  try {
    const data = await API.get(`/api/jobs?limit=50&search=${encodeURIComponent(search)}&status=${encodeURIComponent(status)}`);
    const tbody = document.getElementById('tbody-jobs');
    if (data.items.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No jobs found</td></tr>';
    } else {
      tbody.innerHTML = data.items.map(j => `
        <tr>
          <td><code>${j.id.slice(0, 8)}</code></td>
          <td><b>${escapeHtml(j.title)}</b></td>
          <td class="font-sm">${j.operation}${j.codec ? ` · ${j.codec}` : ''}${j.resolution ? ` ${j.resolution}` : ''}</td>
          <td><span class="badge ${getBadgeClass(j.status)}">${j.status}</span></td>
          <td class="font-sm">${formatDate(j.created_at)}</td>
          <td class="font-sm">${formatDate(j.completed_at)}</td>
          <td>
            ${j.status === 'FAILED' ? `<button class="btn btn-secondary btn-sm" onclick="retryJob('${j.id}')">Retry</button>` : ''}
            ${['QUEUED', 'PREPARING', 'DOWNLOADING', 'PROCESSING', 'UPLOADING'].includes(j.status) ? `<button class="btn btn-danger btn-sm" onclick="cancelJob('${j.id}')">Cancel</button>` : ''}
          </td>
        </tr>
      `).join('');
    }
  } catch (err) {}
}

async function retryJob(id) {
  await API.post(`/api/queue/retry/${id}`, {});
  loadJobs();
}

async function clearJobs() {
  const confirmed = await showConfirm(
    'Are you sure you want to clear finished job history (completed, failed, cancelled)? Active and queued jobs will NOT be deleted.',
    'Clear Job History',
    'Clear Jobs',
    'btn-danger'
  );
  if (!confirmed) return;

  try {
    const res = await API.post('/api/jobs/clear', {});
    toastSuccess(res.message || `Deleted ${res.deleted_jobs_count || 0} finished jobs`, 'Jobs Cleared');
    loadJobs();
  } catch (err) {
    toastError(err.message || 'Failed to clear jobs', 'Error');
  }
}

// 4. CACHE
async function loadCache() {
  const search = document.getElementById('cache-search')?.value || '';
  try {
    const data = await API.get(`/api/cache?limit=50&search=${encodeURIComponent(search)}`);
    const tbody = document.getElementById('tbody-cache');
    if (data.items.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No cached items found</td></tr>';
    } else {
      tbody.innerHTML = data.items.map(c => `
        <tr>
          <td><code>${c.source_id}</code></td>
          <td><b>${escapeHtml(c.title)}</b></td>
          <td class="font-sm">${c.operation} ${c.codec || ''} ${c.resolution || c.subtitle_lang || ''}</td>
          <td>${c.hit_count} hits</td>
          <td class="font-mono font-sm">${c.file_size ? `${(c.file_size / (1024 * 1024)).toFixed(1)} MB` : 'N/A'}</td>
          <td><span class="badge ${c.is_valid ? 'badge-success' : 'badge-danger'}">${c.is_valid ? 'VALID' : 'INVALID'}</span></td>
          <td>
            ${c.is_valid ? `<button class="btn btn-warning btn-sm" onclick="invalidateCache(${c.id})">Invalidate</button>` : ''}
            <button class="btn btn-danger btn-sm" onclick="deleteCache(${c.id})">Delete</button>
          </td>
        </tr>
      `).join('');
    }
  } catch (err) {}
}

async function invalidateCache(id) {
  await API.post(`/api/cache/${id}/invalidate`, {});
  loadCache();
}

async function deleteCache(id) {
  const confirmed = await showConfirm('Delete this cache record from the database?', 'Delete Cache Record', 'Delete', 'btn-danger');
  if (confirmed) {
    await API.delete(`/api/cache/${id}`);
    loadCache();
  }
}

// 5. USERS
let currentUsersTab = 'all';
const cachedWhitelistSet = new Set();

function switchUsersTab(tab) {
  currentUsersTab = tab;
  const btnAll = document.getElementById('btn-users-tab-all');
  const btnBanned = document.getElementById('btn-users-tab-banned');
  const btnWl = document.getElementById('btn-users-tab-whitelist');
  const tableView = document.getElementById('users-view-table');
  const wlView = document.getElementById('users-view-whitelist');
  const searchContainer = document.getElementById('users-search-container');

  if (btnAll) btnAll.className = tab === 'all' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm';
  if (btnBanned) btnBanned.className = tab === 'banned' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm';
  if (btnWl) btnWl.className = tab === 'whitelist' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm';

  if (tab === 'whitelist') {
    if (tableView) tableView.classList.add('hidden');
    if (wlView) wlView.classList.remove('hidden');
    if (searchContainer) searchContainer.style.display = 'none';
    loadUsersWhitelist();
  } else {
    if (tableView) tableView.classList.remove('hidden');
    if (wlView) wlView.classList.add('hidden');
    if (searchContainer) searchContainer.style.display = '';
    loadUsers();
  }
}

async function fetchWhitelistSet() {
  try {
    const res = await API.get('/api/must-join/exempt-users');
    const raw = res.exempt_users || '';
    cachedWhitelistSet.clear();
    raw.split(/[\n,]+/).map(s => s.trim().toLowerCase().replace(/^@/, '')).filter(Boolean).forEach(x => cachedWhitelistSet.add(x));
    const badge = document.getElementById('badge-users-whitelist-count');
    if (badge) {
      badge.textContent = cachedWhitelistSet.size;
      badge.style.display = cachedWhitelistSet.size > 0 ? 'inline-block' : 'none';
    }
  } catch (e) {}
}

function isUserInWhitelist(id, username) {
  if (id && cachedWhitelistSet.has(String(id))) return true;
  if (username && cachedWhitelistSet.has(username.toLowerCase().replace(/^@/, ''))) return true;
  return false;
}

async function loadUsers() {
  const search = document.getElementById('users-search')?.value || '';
  const statusParam = currentUsersTab === 'banned' ? '&status=BANNED' : '';
  try {
    if (cachedWhitelistSet.size === 0) {
      fetchWhitelistSet();
    }
    const data = await API.get(`/api/users?limit=50&search=${encodeURIComponent(search)}${statusParam}`);
    const tbody = document.getElementById('tbody-users');
    if (!tbody) return;

    if (data.banned_count !== undefined) {
      const badge = document.getElementById('badge-users-banned-count');
      if (badge) {
        badge.textContent = data.banned_count;
        badge.style.display = data.banned_count > 0 ? 'inline-block' : 'none';
      }
    }

    if (data.items.length === 0) {
      const emptyMsg = currentUsersTab === 'banned' ? 'No banned users found' : 'No users found';
      tbody.innerHTML = `<tr class="empty-row"><td colspan="7">${emptyMsg}</td></tr>`;
    } else {
      tbody.innerHTML = data.items.map(u => {
        const isWhitelisted = isUserInWhitelist(u.id, u.username);
        const wlBadge = isWhitelisted ? '<span class="badge badge-info ml-1" title="Must-Join Whitelisted">⭐ Exempt</span>' : '';
        const wlBtn = isWhitelisted
          ? `<button class="btn btn-secondary btn-sm" onclick="toggleUserWhitelist(${u.id}, '${escapeHtml(u.username || '')}')" title="Remove from Must-Join Whitelist">Remove Whitelist</button>`
          : `<button class="btn btn-ghost btn-sm" onclick="toggleUserWhitelist(${u.id}, '${escapeHtml(u.username || '')}')" title="Add to Must-Join Whitelist">+ Whitelist</button>`;

        return `
          <tr>
            <td><code>${u.id}</code></td>
            <td><b>@${escapeHtml(u.username || 'N/A')}</b> <span class="text-muted font-sm">(${escapeHtml(u.first_name || '')})</span> ${wlBadge}</td>
            <td><span class="badge ${u.role === 'ADMIN' ? 'badge-info' : 'badge-secondary'}">${u.role}</span></td>
            <td><span class="badge ${u.status === 'ACTIVE' ? 'badge-success' : 'badge-danger'}">${u.status}</span></td>
            <td><span class="font-mono">${u.total_jobs}</span> <span class="text-muted font-sm">(${u.successful_jobs} ok / ${u.failed_jobs} fail)</span></td>
            <td class="font-sm">${formatDate(u.last_seen_at)}</td>
            <td>
              <div class="flex gap-1">
                ${u.status === 'ACTIVE'
                  ? `<button class="btn btn-danger btn-sm" onclick="setUserStatus(${u.id}, 'BANNED')">Ban</button>`
                  : `<button class="btn btn-success btn-sm" onclick="setUserStatus(${u.id}, 'ACTIVE')">Unban</button>`}
                ${wlBtn}
                <button class="btn btn-danger btn-sm" onclick="deleteUser(${u.id}, '${escapeHtml(u.username || '')}')" title="Delete User">Delete</button>
              </div>
            </td>
          </tr>
        `;
      }).join('');
    }
  } catch (err) {}
}

async function setUserStatus(id, st) {
  await API.post(`/api/users/${id}/status`, { status: st });
  loadUsers();
}

async function deleteUser(id, username) {
  const label = username ? `@${username} (${id})` : `User ${id}`;
  const confirmed = await showConfirm(
    `Are you sure you want to permanently delete ${label} and all associated records from the database? This cannot be undone.`,
    'Delete User',
    'Delete Permanently',
    'btn-danger'
  );
  if (!confirmed) return;

  try {
    const res = await API.delete(`/api/users/${id}`);
    toastSuccess(res.message || 'User deleted successfully', 'User Deleted');
    loadUsers();
  } catch (err) {
    toastError(err.message || 'Failed to delete user', 'Error');
  }
}

async function loadUsersWhitelist() {
  try {
    const res = await API.get('/api/must-join/exempt-users');
    const area = document.getElementById('users-whitelist-area');
    if (area) area.value = res.exempt_users || '';
    renderWhitelistTags(res.exempt_users || '');
    await fetchWhitelistSet();
  } catch (err) {
    console.error('Failed to load whitelist:', err);
  }
}

function renderWhitelistTags(rawText) {
  const tagsContainer = document.getElementById('users-whitelist-tags');
  if (!tagsContainer) return;
  const items = rawText.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
  if (items.length === 0) {
    tagsContainer.innerHTML = '<span class="text-secondary font-sm">No whitelisted users configured.</span>';
    return;
  }
  tagsContainer.innerHTML = items.map(item => `
    <span class="badge badge-info" style="display:inline-flex;align-items:center;gap:4px;padding:3px 8px;">
      <span>${escapeHtml(item)}</span>
      <button type="button" onclick="removeWhitelistItem('${escapeHtml(item)}')" style="background:none;border:none;color:inherit;cursor:pointer;font-size:13px;line-height:1;opacity:0.75;padding:0 2px;" title="Remove">×</button>
    </span>
  `).join('');
}

async function saveUsersWhitelist() {
  hideAlert('users-whitelist-alert');
  const area = document.getElementById('users-whitelist-area');
  const val = area ? area.value : '';
  try {
    await API.post('/api/must-join/exempt-users', { exempt_users: val });
    showAlert('users-whitelist-alert', 'Must-Join whitelist saved successfully', 'success');
    toastSuccess('Must-Join whitelist updated', 'Saved');
    renderWhitelistTags(val);
    await fetchWhitelistSet();
  } catch (err) {
    showAlert('users-whitelist-alert', `Failed to save: ${err.message}`, 'error');
    toastError(`Failed to save: ${err.message}`, 'Save Error');
  }
}

async function quickAddWhitelistUser() {
  const input = document.getElementById('users-wl-quick-add');
  if (!input) return;
  const val = input.value.trim();
  if (!val) return;
  const area = document.getElementById('users-whitelist-area');
  const current = area ? area.value.trim() : '';
  const updated = current ? `${current}, ${val}` : val;
  if (area) area.value = updated;
  input.value = '';
  await saveUsersWhitelist();
}

async function removeWhitelistItem(itemToRemove) {
  const area = document.getElementById('users-whitelist-area');
  if (!area) return;
  const items = area.value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
  const updatedItems = items.filter(it => it.toLowerCase() !== itemToRemove.toLowerCase());
  area.value = updatedItems.join(', ');
  await saveUsersWhitelist();
}

async function toggleUserWhitelist(id, username) {
  await fetchWhitelistSet();
  const isWl = isUserInWhitelist(id, username);
  const identifier = username ? `@${username}` : String(id);
  if (isWl) {
    await removeWhitelistItem(String(id));
    if (username) await removeWhitelistItem(`@${username}`);
    toastSuccess(`Removed ${identifier} from Must-Join whitelist`);
  } else {
    const res = await API.get('/api/must-join/exempt-users');
    const current = (res.exempt_users || '').trim();
    const updated = current ? `${current}, ${identifier}` : identifier;
    await API.post('/api/must-join/exempt-users', { exempt_users: updated });
    toastSuccess(`Added ${identifier} to Must-Join whitelist`);
  }
  await fetchWhitelistSet();
  loadUsers();
}

// 6. MUST JOIN
async function loadMustJoin() {
  try {
    const data = await API.get('/api/must-join');
    const toggleBtn = document.getElementById('btn-mj-toggle');
    if (data.enabled) {
      toggleBtn.textContent = 'Must Join: ENABLED';
      toggleBtn.className = 'btn btn-success';
    } else {
      toggleBtn.textContent = 'Must Join: DISABLED';
      toggleBtn.className = 'btn btn-secondary';
    }

    const tbody = document.getElementById('tbody-must-join');
    if (data.channels.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="8">No required channels configured</td></tr>';
    } else {
      tbody.innerHTML = data.channels.map(ch => {
        let linkHtml = '<span class="badge badge-warning">Missing Join Link</span>';
        if (ch.invite_url) {
          linkHtml = `<a href="${escapeHtml(ch.invite_url)}" target="_blank" rel="noopener" class="link">Invite Link</a>`;
        } else if (ch.username) {
          const u = ch.username.replace(/^@/, '');
          linkHtml = `<a href="https://t.me/${escapeHtml(u)}" target="_blank" rel="noopener" class="link">@${escapeHtml(u)}</a>`;
        }

        const isVerified = ch.bot_status === 'administrator';
        const botStatusHtml = isVerified
          ? '<span class="badge badge-success">Verified Admin</span>'
          : `<span class="badge badge-danger">${escapeHtml(ch.bot_status || 'Unverified')}</span>`;

        const errorHtml = ch.last_error
          ? `<span class="text-danger font-xs" title="${escapeHtml(ch.last_error)}">${escapeHtml(ch.last_error.length > 32 ? ch.last_error.substring(0, 30) + '...' : ch.last_error)}</span>`
          : '<span class="text-secondary font-xs">None</span>';

        return `
          <tr>
            <td><b>${escapeHtml(ch.title)}</b></td>
            <td><code>${ch.chat_id}</code></td>
            <td>${linkHtml}</td>
            <td>${botStatusHtml}</td>
            <td>${errorHtml}</td>
            <td><span class="badge ${ch.enabled ? 'badge-success' : 'badge-secondary'}">${ch.enabled ? 'Enabled' : 'Disabled'}</span></td>
            <td class="font-sm">${formatDate(ch.last_bot_check)}</td>
            <td>
              <button class="btn btn-secondary btn-sm" onclick="testChannel(${ch.id})" title="Verify Bot Permissions">Verify</button>
              <button class="btn btn-danger btn-sm" onclick="deleteChannel(${ch.id})" title="Remove Channel">Remove</button>
            </td>
          </tr>
        `;
      }).join('');
    }

    try {
      const msgData = await API.get('/api/must-join/message');
      const area = document.getElementById('mj-custom-msg-area');
      if (area && msgData.message) area.value = msgData.message;
    } catch (e) {}

    if (data.exempt_users !== undefined) {
      const exemptArea = document.getElementById('mj-exempt-users-area');
      if (exemptArea) exemptArea.value = data.exempt_users;
    }
  } catch (err) {}
}

async function toggleMustJoin() {
  const data = await API.get('/api/must-join');
  await API.post('/api/must-join/toggle', { enabled: !data.enabled });
  loadMustJoin();
}

async function addChannelModalSubmit(e) {
  e.preventDefault();
  hideAlert('add-channel-alert');
  const cid     = parseInt(document.getElementById('mj-chat-id').value, 10);
  const title   = document.getElementById('mj-title').value;
  const username = document.getElementById('mj-username').value;
  const invite  = document.getElementById('mj-invite').value;

  try {
    await API.post('/api/must-join/channels', {
      chat_id: cid,
      title,
      username: username || null,
      invite_url: invite || null,
      enabled: true,
    });
    document.getElementById('modal-add-channel').classList.remove('active');
    document.getElementById('form-add-channel').reset();
    loadMustJoin();
    toastSuccess(`Channel "${title}" added successfully`);
  } catch (err) {
    showAlert('add-channel-alert', err.message, 'error');
    toastError(err.message, 'Add Channel Failed');
  }
}

async function testChannel(id) {
  try {
    const res = await API.post(`/api/must-join/channels/${id}/test`, {});
    if (res.is_admin) {
      toastSuccess('Bot is an administrator in this channel', 'Channel Test');
    } else {
      toastError(res.detail || 'Bot is not an administrator', 'Channel Test Failed');
    }
  } catch (err) {
    toastError(err.message, 'Channel Test Error');
  }
  loadMustJoin();
}

async function deleteChannel(id) {
  const confirmed = await showConfirm('Remove this required channel?', 'Remove Channel', 'Remove', 'btn-danger');
  if (confirmed) {
    await API.delete(`/api/must-join/channels/${id}`);
    loadMustJoin();
    toastSuccess('Channel removed');
  }
}

// 7. YOUTUBE SETTINGS
async function loadYouTube() {
  const data = await API.get('/api/settings');
  document.getElementById('yt-max-height').value         = data.default_max_height;
  document.getElementById('yt-playlists-toggle').checked = data.playlists_enabled;
  document.getElementById('yt-h264-toggle').checked      = data.h264_enabled;
  document.getElementById('yt-h265-toggle').checked      = data.h265_enabled;
  document.getElementById('yt-mp3-toggle').checked       = data.mp3_enabled;
  document.getElementById('yt-subs-toggle').checked      = data.subtitles_enabled;
}

// 8. COOKIES
async function loadCookies() {
  try {
    const data = await API.get('/api/cookies');
    const toggleBtn = document.getElementById('btn-cookie-toggle');
    if (data.enabled) {
      toggleBtn.textContent = 'Cookies: ON';
      toggleBtn.className = 'btn btn-success';
    } else {
      toggleBtn.textContent = 'Cookies: OFF';
      toggleBtn.className = 'btn btn-secondary';
    }
    document.getElementById('cookie-configured-val').textContent = data.configured ? 'Configured' : 'Not Configured';
    document.getElementById('cookie-count-val').textContent      = data.cookie_count;
    document.getElementById('cookie-updated-val').textContent    = formatDate(data.last_updated);
  } catch (err) {}
}

async function toggleCookies() {
  const data = await API.get('/api/cookies');
  await API.post('/api/cookies/toggle', { enabled: !data.enabled });
  loadCookies();
}

async function pasteCookiesSubmit(e) {
  e.preventDefault();
  hideAlert('cookies-alert');
  const text = document.getElementById('cookie-paste-area').value;
  try {
    const res = await API.post('/api/cookies/paste', { content: text });
    showAlert('cookies-alert', `Saved and validated ${res.cookie_count} cookies`, 'success');
    toastSuccess(`Saved and validated ${res.cookie_count} cookies`, 'Cookies Saved');
    document.getElementById('cookie-paste-area').value = '';
    loadCookies();
  } catch (err) {
    showAlert('cookies-alert', err.message, 'error');
    toastError(err.message, 'Cookie Validation Failed');
  }
}

async function testCookiesSubmit() {
  const url = window.prompt('Enter a YouTube URL to test cookie extraction:', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ');
  if (!url) return;
  try {
    const res = await API.post('/api/cookies/test', { url });
    toastSuccess(res.message, 'Cookie Test');
  } catch (err) {
    toastError(err.message, 'Cookie Test Failed');
  }
}

async function deleteCookiesFile() {
  const confirmed = await showConfirm('Delete the saved cookies file?', 'Delete Cookies', 'Delete', 'btn-danger');
  if (confirmed) {
    await API.delete('/api/cookies');
    loadCookies();
    toastSuccess('Cookies file deleted');
  }
}

// 9. AI TRANSLATION
async function loadAI() {
  try {
    const data = await API.get('/api/ai');
    document.getElementById('ai-enabled-toggle').checked = data.enabled;
    document.getElementById('ai-provider').value         = data.provider;
    document.getElementById('ai-base-url').value         = data.base_url;
    document.getElementById('ai-model').value            = data.model;
    const maxChunksEl = document.getElementById('ai-max-chunks');
    if (maxChunksEl) maxChunksEl.value                   = data.max_chunks ?? 50;
    const aiKeyInput = document.getElementById('ai-api-key-input');
    const aiKeyDisplay = document.getElementById('ai-api-key-display');
    if (data.api_key_masked) {
      aiKeyDisplay.textContent = `Configured · ${data.api_key_masked} — Leave blank to keep existing value`;
      if (aiKeyInput) aiKeyInput.placeholder = 'Leave blank to keep existing value';
    } else {
      aiKeyDisplay.textContent = 'None';
      if (aiKeyInput) aiKeyInput.placeholder = 'sk-…';
    }
  } catch (err) {}
}

async function saveAISettings(e) {
  e.preventDefault();
  hideAlert('ai-alert');
  const enabled = document.getElementById('ai-enabled-toggle').checked;
  const provider = document.getElementById('ai-provider').value;
  const baseUrl  = document.getElementById('ai-base-url').value;
  const model    = document.getElementById('ai-model').value;
  const maxChunksEl = document.getElementById('ai-max-chunks');
  const maxChunks = maxChunksEl ? (parseInt(maxChunksEl.value, 10) || 0) : 50;
  const key      = document.getElementById('ai-api-key-input').value;

  try {
    await API.post('/api/ai', { enabled, provider, base_url: baseUrl, model, max_chunks: maxChunks, api_key: key || null });
    showAlert('ai-alert', 'AI translation settings saved and persisted to PostgreSQL', 'success');
    toastSuccess('AI settings saved and applied', 'AI Settings');
    document.getElementById('ai-api-key-input').value = '';
    loadAI();
  } catch (err) {
    showAlert('ai-alert', err.message, 'error');
    toastError(err.message, 'AI Settings Error');
  }
}

async function testAI() {
  try {
    const res = await API.post('/api/ai/test', {});
    toastSuccess(res.message, 'AI Connection Test');
  } catch (err) {
    toastError(err.message, 'AI Test Failed');
  }
}

// 10. SETTINGS
async function loadSettings() {
  const data = await API.get('/api/settings');
  const elLocal = document.getElementById('set-max-size-local');
  const elCloud = document.getElementById('set-max-size-cloud');
  if (elLocal) elLocal.value = data.max_video_file_size_mb_local ?? 1900;
  if (elCloud) elCloud.value = data.max_video_file_size_mb_cloud ?? 48;
  document.getElementById('set-max-per-user').value   = data.max_concurrent_per_user;
  document.getElementById('set-max-queued').value     = data.max_queued_per_user;
  document.getElementById('set-max-temp').value       = data.max_temp_storage_gb;
}

async function saveGlobalSettings(e) {
  e.preventDefault();
  hideAlert('settings-alert');
  hideAlert('youtube-alert');
  const payload = {
    max_video_file_size_mb_local:      parseInt(document.getElementById('set-max-size-local')?.value || '1900', 10),
    max_video_file_size_mb_cloud:      parseInt(document.getElementById('set-max-size-cloud')?.value || '48', 10),
    max_concurrent_per_user:           parseInt(document.getElementById('set-max-per-user').value, 10),
    max_queued_per_user:               parseInt(document.getElementById('set-max-queued').value, 10),
    max_temp_storage_gb:               parseInt(document.getElementById('set-max-temp').value, 10),
    default_max_height:                parseInt(document.getElementById('yt-max-height')?.value || '1080', 10),
    playlists_enabled:                 document.getElementById('yt-playlists-toggle')?.checked || false,
    h264_enabled:                      document.getElementById('yt-h264-toggle')?.checked !== false,
    h265_enabled:                      document.getElementById('yt-h265-toggle')?.checked !== false,
    mp3_enabled:                       document.getElementById('yt-mp3-toggle')?.checked !== false,
    subtitles_enabled:                 document.getElementById('yt-subs-toggle')?.checked !== false,
  };

  try {
    await API.post('/api/settings', payload);
    showAlert('settings-alert', 'Settings saved and persisted to PostgreSQL', 'success');
    showAlert('youtube-alert', 'Settings saved and persisted to PostgreSQL', 'success');
    toastSuccess('Settings saved and applied dynamically', 'Settings Saved');
    loadSettings();
  } catch (err) {
    showAlert('settings-alert', err.message, 'error');
    showAlert('youtube-alert', err.message, 'error');
    toastError(err.message, 'Settings Error');
  }
}

// 11. SYSTEM
async function loadSystem() {
  try {
    const ready = await API.get('/ready');
    document.getElementById('sys-ready-db').textContent    = ready.database ? 'CONNECTED' : 'ERROR';
    document.getElementById('sys-ready-db').className      = ready.database ? 'info-row__val text-success bold' : 'info-row__val text-danger bold';
    document.getElementById('sys-ready-redis').textContent = ready.redis ? 'CONNECTED' : 'ERROR';
    document.getElementById('sys-ready-redis').className   = ready.redis ? 'info-row__val text-success bold' : 'info-row__val text-danger bold';
  } catch (err) {}
}

// 12. LOGS
async function loadLogs() {
  try {
    const data = await API.get('/api/logs?limit=100');
    const logBox = document.getElementById('log-display');
    if (data.logs.length === 0) {
      logBox.textContent = 'No logs available.';
    } else {
      logBox.textContent = data.logs.map(l => `[${l.timestamp}] [${l.level}] [${l.service}] ${l.message}`).join('\n');
    }
  } catch (err) {}
}

// 13. AUDIT
async function loadAudit() {
  try {
    const data = await API.get('/api/audit?limit=50');
    const tbody = document.getElementById('tbody-audit');
    if (data.items.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="5">No audit logs</td></tr>';
    } else {
      tbody.innerHTML = data.items.map(a => `
        <tr>
          <td><code>${a.id}</code></td>
          <td><span class="badge badge-accent">${a.action}</span></td>
          <td><b>${escapeHtml(a.admin_username)}</b></td>
          <td class="font-sm">${escapeHtml(a.details || '')}</td>
          <td class="font-sm">${formatDate(a.created_at)}</td>
        </tr>
      `).join('');
    }
  } catch (err) {}
}

// Helpers
function escapeHtml(str) {
  if (!str) return '';
  return str
    .replace(/&/g,  '&amp;')
    .replace(/</g,  '&lt;')
    .replace(/>/g,  '&gt;')
    .replace(/"/g,  '&quot;');
}

function formatDate(isoStr) {
  if (!isoStr) return 'N/A';
  return new Date(isoStr).toLocaleString();
}

function getBadgeClass(status) {
  switch (status) {
    case 'COMPLETED': return 'badge-success';
    case 'FAILED':    return 'badge-danger';
    case 'CANCELLED': return 'badge-secondary';
    case 'QUEUED':    return 'badge-warning';
    default:          return 'badge-info';
  }
}

// =============================================================================
// TELEGRAM ARCHITECTURE & SETTINGS
// =============================================================================
let currentTelegramConfig = {};
let targetMigrationMode = 'local';

async function loadTelegramConfig() {
  try {
    const data = await API.get('/api/telegram/config');
    currentTelegramConfig = data;

    const radios = document.getElementsByName('tg-mode-radio');
    for (const r of radios) {
      if (r.value === data.mode) r.checked = true;
    }
    onTelegramModeChange();

    document.getElementById('tg-derived-endpoint').value = data.derived_endpoint || (data.mode === 'local' ? 'http://telegram-bot-api:8081' : 'https://api.telegram.org');

    const tokenInput = document.getElementById('tg-bot-token');
    const tokenDisplay = document.getElementById('tg-token-masked-display');
    if (data.bot_token_masked) {
      tokenDisplay.textContent = `Configured · ${data.bot_token_masked} — Leave blank to keep existing value`;
      if (tokenInput) tokenInput.placeholder = 'Leave blank to keep existing value';
    } else {
      tokenDisplay.textContent = 'Not Set';
      if (tokenInput) tokenInput.placeholder = '123456789:ABCdefGHIjklMNOpqrSTUvwxYZ';
    }

    document.getElementById('tg-cache-channel').value             = data.cache_channel_id || '';
    document.getElementById('stat-tg-version').textContent        = `v${data.config_version || 1}`;

    await loadWelcomeMessage();

    // Bot badge
    const badgeBot = document.getElementById('badge-tg-bot');
    if (data.is_configured) {
      badgeBot.className = 'badge badge-success';
      badgeBot.innerHTML = '<span class="dot dot-success"></span>Configured';
    } else {
      badgeBot.className = 'badge badge-danger';
      badgeBot.innerHTML = '<span class="dot dot-danger"></span>Not Configured';
    }

    // Mode badge
    const badgeMode = document.getElementById('badge-tg-mode');
    badgeMode.textContent = data.mode === 'local' ? 'Local Bot API' : 'Cloud Bot API';
    badgeMode.className   = data.mode === 'local' ? 'badge badge-info' : 'badge badge-warning';

    // Local API probe
    if (data.mode === 'local') {
      try {
        await API.post('/api/telegram/test-local-api', {});
        const badgeLocal = document.getElementById('badge-tg-local');
        badgeLocal.className = 'badge badge-success';
        badgeLocal.innerHTML = '<span class="dot dot-success"></span>Healthy';
      } catch (e) {
        const badgeLocal = document.getElementById('badge-tg-local');
        badgeLocal.className = 'badge badge-danger';
        badgeLocal.innerHTML = '<span class="dot dot-danger"></span>Unreachable';
      }
    } else {
      const badgeLocal = document.getElementById('badge-tg-local');
      badgeLocal.className = 'badge badge-secondary';
      badgeLocal.innerHTML = '<span class="dot dot-muted"></span>Standby';
    }

    // Disk telemetry
    try {
      const sysData = await API.get('/api/system');
      if (sysData.stats) {
        const workerTempEl = document.getElementById('stat-worker-temp');
        if (workerTempEl) workerTempEl.textContent = `${Math.round(sysData.stats.worker_temp_gb * 1024)} MB`;
        const hostFreeEl = document.getElementById('stat-host-free');
        if (hostFreeEl) hostFreeEl.textContent = `${sysData.stats.disk_free_gb} GB`;
      }
    } catch (e) {}
  } catch (err) {
    console.error('Failed to load telegram config:', err);
  }
}

function onTelegramModeChange() {
  const selectedMode = document.querySelector('input[name="tg-mode-radio"]:checked')?.value || 'cloud';
  const endpointInput = document.getElementById('tg-derived-endpoint');

  if (selectedMode === 'local') {
    if (endpointInput) endpointInput.value = currentTelegramConfig.local_base_url || 'http://telegram-bot-api:8081';
  } else {
    if (endpointInput) endpointInput.value = 'https://api.telegram.org';
  }
}

function toggleInputMask(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.type = el.type === 'password' ? 'text' : 'password';
}

function showTelegramAlert(msg, isSuccess = true) {
  showAlert('telegram-alert', msg, isSuccess ? 'success' : 'error');
}

async function saveTelegramConfig() {
  hideAlert('telegram-alert');
  const saveBtn = document.getElementById('btn-save-telegram');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';

  const mode         = document.querySelector('input[name="tg-mode-radio"]:checked')?.value || 'cloud';
  const token        = document.getElementById('tg-bot-token').value.trim();
  const cacheChannel = document.getElementById('tg-cache-channel').value.trim();

  const payload = {
    mode,
    bot_token:        token || undefined,
    cache_channel_id: cacheChannel || undefined,
  };

  try {
    const res = await API.post('/api/telegram/save', payload);
    showAlert('telegram-alert', res.message, 'success');
    toastSuccess(res.message, 'Telegram Saved');
    document.getElementById('tg-bot-token').value = '';
    await loadTelegramConfig();
  } catch (err) {
    if (err.message && err.message.includes('Conflict')) {
      showAlert('telegram-alert', 'Conflict: Another configuration update is in progress. Retry in a few seconds.', 'warning');
    } else {
      showAlert('telegram-alert', `Error: ${err.message}`, 'error');
    }
  } finally {
    saveBtn.disabled    = false;
    saveBtn.textContent = 'Save Configuration';
  }
}

async function testBotToken() {
  const mode  = document.querySelector('input[name="tg-mode-radio"]:checked')?.value || 'local';
  const token = document.getElementById('tg-bot-token').value.trim();
  try {
    const res = await API.post('/api/telegram/test-token', { bot_token: token || undefined, mode });
    toastSuccess(`Bot: @${res.username} (${res.first_name}) · ID: ${res.bot_id}`, 'Token Valid');
  } catch (err) {
    toastError(err.message, 'Token Test Failed');
  }
}

async function testLocalBotAPI() {
  try {
    const res = await API.post('/api/telegram/test-local-api', {});
    toastSuccess(`Local Bot API reachable at ${res.host}:${res.port}`, 'Local API');
  } catch (err) {
    toastError(err.message, 'Local API Unreachable');
  }
}

async function testCacheChannel() {
  const mode      = document.querySelector('input[name="tg-mode-radio"]:checked')?.value || 'local';
  const token     = document.getElementById('tg-bot-token').value.trim();
  const channelId = document.getElementById('tg-cache-channel').value.trim();
  if (!channelId) {
    toastWarning('Please enter a Cache Channel ID to test');
    return;
  }
  try {
    const res = await API.post('/api/telegram/test-channel', {
      channel_id: channelId,
      bot_token: token || undefined,
      mode,
    });
    toastSuccess(`Cache channel access verified · ID: ${res.channel_id}`, 'Channel OK');
  } catch (err) {
    toastError(err.message, 'Channel Test Failed');
  }
}

function openMigrationModal(targetMode) {
  targetMigrationMode = targetMode;
  const modal   = document.getElementById('modal-migration');
  const warnEl  = document.getElementById('migration-warning-text');
  const descEl  = document.getElementById('migration-desc');

  if (targetMode === 'local') {
    if (warnEl) warnEl.textContent = 'Switching to centralized Local Bot API will route requests to the external Local Bot API stack (http://telegram-bot-api:8081) on network telegram-bots.';
    if (descEl) descEl.textContent = 'This enables up to 2000 MB file uploads. Note: If this bot was previously logged into a local server, ensure it was properly logged out first.';
  } else {
    if (warnEl) warnEl.textContent = 'Cloud Bot API limits uploads to 50 MB.';
    if (descEl) descEl.textContent = 'This will transition the bot session to official Telegram Cloud API (https://api.telegram.org). Any media file exceeding 50 MB will be rejected.';
  }

  modal.classList.add('active');
}

async function executeMigration() {
  const btn = document.getElementById('btn-confirm-migration');
  btn.disabled    = true;
  btn.textContent = 'Migrating…';
  try {
    const res = await API.post('/api/telegram/migrate', { target_mode: targetMigrationMode });
    toastSuccess(res.message, 'Migration Complete');
    document.getElementById('modal-migration').classList.remove('active');
    await loadTelegramConfig();
  } catch (err) {
    toastError(err.message, 'Migration Failed');
  } finally {
    btn.disabled    = false;
    btn.textContent = 'Confirm & Migrate';
  }
}

async function saveMustJoinMessage() {
  hideAlert('must-join-alert');
  const msg = document.getElementById('mj-custom-msg-area').value;
  try {
    await API.post('/api/must-join/message', { message: msg });
    showAlert('must-join-alert', 'Custom Must-Join message saved', 'success');
    toastSuccess('Custom Must-Join message saved', 'Saved');
  } catch (err) {
    showAlert('must-join-alert', `Failed to save: ${err.message}`, 'error');
    toastError(`Failed to save: ${err.message}`, 'Save Error');
  }
}

async function resetMustJoinMessage() {
  hideAlert('must-join-alert');
  const confirmed = await showConfirm('Reset Must-Join template message to system default?', 'Reset Message', 'Reset', 'btn-warning');
  if (!confirmed) return;
  try {
    const res = await API.post('/api/must-join/message/reset', {});
    document.getElementById('mj-custom-msg-area').value = res.message;
    showAlert('must-join-alert', 'Reset to default template', 'success');
    toastSuccess('Reset to default template');
  } catch (err) {
    showAlert('must-join-alert', `Failed to reset: ${err.message}`, 'error');
    toastError(`Failed to reset: ${err.message}`, 'Reset Error');
  }
}

async function saveMustJoinExemptUsers() {
  await saveUsersWhitelist();
}

// =============================================================================
// TELEGRAM /start WELCOME MESSAGE
// =============================================================================

async function loadWelcomeMessage() {
  try {
    const res  = await API.get('/api/telegram/welcome-message');
    const area = document.getElementById('tg-welcome-msg-area');
    if (area) area.value = res.message || '';
  } catch (err) {
    console.error('Failed to load welcome message:', err);
  }
}

function showWelcomeAlert(msg, isSuccess = true) {
  showAlert('tg-welcome-alert', msg, isSuccess ? 'success' : 'error');
}

async function saveWelcomeMessage() {
  hideAlert('tg-welcome-alert');
  const btn  = document.getElementById('btn-save-welcome');
  const area = document.getElementById('tg-welcome-msg-area');

  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }

  try {
    const res = await API.post('/api/telegram/welcome-message', { message: area ? area.value : '' });
    if (area) area.value = res.message;
    showWelcomeAlert('Welcome message saved successfully', true);
    toastSuccess('Welcome message saved');
  } catch (err) {
    showWelcomeAlert(`Failed to save: ${err.message}`, false);
    toastError(err.message, 'Save Failed');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Save Welcome Message'; }
  }
}

async function resetWelcomeMessage() {
  hideAlert('tg-welcome-alert');
  const confirmed = await showConfirm('Reset /start welcome message to system default?', 'Reset Welcome Message', 'Reset', 'btn-warning');
  if (!confirmed) return;

  const btn  = document.getElementById('btn-reset-welcome');
  const area = document.getElementById('tg-welcome-msg-area');

  if (btn) { btn.disabled = true; btn.textContent = 'Resetting…'; }

  try {
    const res = await API.post('/api/telegram/welcome-message/reset', {});
    if (area) area.value = res.message || '';
    showWelcomeAlert('Welcome message reset to default template', true);
    toastSuccess('Welcome message reset to default');
  } catch (err) {
    showWelcomeAlert(`Failed to reset: ${err.message}`, false);
    toastError(err.message, 'Reset Failed');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Reset to Default'; }
  }
}
