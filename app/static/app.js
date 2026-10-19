let appState = {
  app_metadata: {},
  settings: {},
  monitor: {},
  transfers: [],
  trackers: [],
  default_rule: null,
  activity_log: [],
};

let currentTab = 'transfers';
let currentTransferGrouping = 'none';
let expandedTransferGroups = new Set();
let isTransfersHeaderCompact = false;
let oobPollTimer = null;

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function getTrackerDisplayName(trackerId) {
  if (!trackerId) return 'Untracked';
  if (trackerId === '__default__') {
    return (
      (appState.default_rule && appState.default_rule.display_name) ||
      'Default Fallback Rule (Unmatched Trackers)'
    );
  }
  const found = (appState.trackers || []).find((r) => r.tracker_id === trackerId);
  if (found && found.display_name && String(found.display_name).trim()) {
    return String(found.display_name).trim();
  }
  if (trackerId === 'untracked') {
    return 'Direct / DHT (No Tracker)';
  }
  return String(trackerId);
}

function formatBytes(bytes) {
  const num = Number(bytes) || 0;
  if (num <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(num) / Math.log(1024)), units.length - 1);
  return `${(num / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 2)} ${units[i]}`;
}

function formatDuration(seconds) {
  const secs = Math.max(0, Math.round(Number(seconds) || 0));
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (d > 0) return `${d}d ${String(h).padStart(2, '0')}h`;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

function formatDateTime(isoStr) {
  if (!isoStr) return '';
  const dt = new Date(isoStr);
  if (isNaN(dt.getTime())) return String(isoStr);
  const tz = (appState.settings && appState.settings.timezone) || 'Europe/Berlin';
  const locale = tz.toLowerCase().startsWith('america/') ? 'en-US' : 'de-DE';
  try {
    return new Intl.DateTimeFormat(locale, {
      timeZone: tz,
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(dt);
  } catch (_err) {
    return new Intl.DateTimeFormat('de-DE', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(dt);
  }
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  const colors = {
    success: 'bg-emerald-950/95 border-emerald-500/50 text-emerald-200',
    error: 'bg-red-950/95 border-red-500/50 text-red-200',
    info: 'bg-slate-900/95 border-amber-500/50 text-amber-200',
  };
  el.className = `pointer-events-auto px-4 py-3 rounded-xl border shadow-xl text-xs sm:text-sm font-medium transition-all ${colors[type] || colors.info}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 300);
  }, 4200);
}

function switchTab(tabName) {
  currentTab = tabName;
  ['transfers', 'trackers', 'logs', 'settings'].forEach((t) => {
    const sec = document.getElementById(`tab-${t}`);
    const btn = document.getElementById(`tab-btn-${t}`);
    if (t === tabName) {
      sec.classList.remove('hidden');
      btn.classList.add('border-amber-500', 'text-amber-400');
      btn.classList.remove('border-transparent', 'text-slate-400');
    } else {
      sec.classList.add('hidden');
      btn.classList.remove('border-amber-500', 'text-amber-400');
      btn.classList.add('border-transparent', 'text-slate-400');
    }
  });
  updateTransfersStickyHeaderState();
}

async function fetchState() {
  try {
    const resp = await fetch('/api/state');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    updateUI(data);
  } catch (err) {
    console.error('Failed to fetch state:', err);
  }
}

async function scanNow() {
  const btn = document.getElementById('btn-scan-now');
  const spinner = document.getElementById('scan-spinner');
  btn.disabled = true;
  spinner.classList.add('animate-spin');
  try {
    const resp = await fetch('/api/poll?execute_rules=true', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || 'Poll failed');
    }
    if (data.state) {
      updateUI(data.state);
    }
    const pollRes = data.poll_result || {};
    if (!pollRes.ok && pollRes.error) {
      showToast(pollRes.error, 'error');
    } else {
      const newCount = (pollRes.newly_discovered_trackers || []).length;
      const canceledCount = (pollRes.canceled || []).length;
      let msg = `Scanned ${(pollRes.transfers || []).length} active transfer(s).`;
      if (newCount > 0) msg += ` Discovered ${newCount} new tracker(s)!`;
      if (canceledCount > 0) msg += ` Triggered ${canceledCount} rule cancellation(s)!`;
      showToast(msg, 'success');
    }
  } catch (err) {
    showToast(`Scan error: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    spinner.classList.remove('animate-spin');
  }
}

function syncPrivacyTogglesUI(isHidden) {
  const chkTransfers = document.getElementById('chk-hide-transfer-names');
  const chkLogs = document.getElementById('chk-hide-transfer-names-logs');
  if (chkTransfers) chkTransfers.checked = Boolean(isHidden);
  if (chkLogs) chkLogs.checked = Boolean(isHidden);
}

async function toggleTransferNamesPrivacy(checked) {
  const hideVal = Boolean(checked);
  if (!appState.settings) appState.settings = {};
  appState.settings.hide_transfer_names = hideVal;
  syncPrivacyTogglesUI(hideVal);
  renderTransfers();
  renderLogs();
  try {
    await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hide_transfer_names: hideVal }),
    });
  } catch (_err) {
    // ignore transient error
  }
}

function doesDeviceAllowPinnedTransfersHeader() {
  const vh = window.innerHeight || 800;
  const vw = window.innerWidth || 1024;
  const mainHeader = document.getElementById('main-app-header');
  const mainHeaderHeight = mainHeader ? mainHeader.offsetHeight : 112;
  // On narrow screens (< 640px), the 3/4 + 1/4 boxes stack vertically and grouping buttons wrap,
  // requiring more vertical height than side-by-side desktop layouts.
  const estimatedCompactHeight = vw < 640 ? 185 : vw < 1024 ? 140 : 105;

  if (vh < 620) return false;
  if (vw < 640 && vh < 780) return false;
  if (mainHeaderHeight + estimatedCompactHeight > vh * 0.42) return false;
  return true;
}

function updateTransfersStickyHeaderState() {
  const stickyHeader = document.getElementById('transfers-sticky-header');
  const logsStickyHeader = document.getElementById('logs-sticky-header');

  const userPinEnabled = appState.settings?.pin_transfers_header !== false;
  const deviceAllows = doesDeviceAllowPinnedTransfersHeader();
  const effectivePinned = userPinEnabled && deviceAllows;

  // Sync Settings checkbox & device override warning
  const chkPinSettings = document.getElementById('chk-pin-transfers-header');
  if (chkPinSettings) {
    chkPinSettings.checked = userPinEnabled;
  }
  const overrideHint = document.getElementById('settings-pin-device-override-hint');
  if (overrideHint) {
    if (userPinEnabled && !deviceAllows) {
      overrideHint.classList.remove('hidden');
    } else {
      overrideHint.classList.add('hidden');
    }
  }

  // Sync in-tab Pin buttons across both Active Transfers and Cancellation Log tabs
  const pinControls = [
    {
      btn: document.getElementById('btn-pin-transfers-header'),
      label: document.getElementById('label-pin-transfers-header'),
    },
    {
      btn: document.getElementById('btn-pin-logs-header'),
      label: document.getElementById('label-pin-logs-header'),
    },
  ];

  pinControls.forEach(({ btn, label }) => {
    if (!btn || !label) return;
    if (userPinEnabled && !deviceAllows) {
      btn.setAttribute('aria-pressed', 'false');
      btn.className =
        'inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium border transition cursor-pointer bg-slate-900 border-amber-500/30 text-amber-300/80 shrink-0';
      btn.title =
        'Pinned header is enabled in options, but automatically overridden because device screen space is too small';
      label.textContent = 'Auto-Unpinned (Screen Size)';
    } else if (userPinEnabled) {
      btn.setAttribute('aria-pressed', 'true');
      btn.className =
        'inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer bg-amber-500/20 border-amber-500/50 text-amber-300 shadow-sm shrink-0';
      btn.title = 'Header is pinned and compacts when scrolling down (click to unpin)';
      label.textContent = 'Pinned';
    } else {
      btn.setAttribute('aria-pressed', 'false');
      btn.className =
        'inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium border transition cursor-pointer bg-slate-950 border-slate-800 text-slate-400 hover:border-slate-700 hover:text-white shrink-0';
      btn.title = 'Click to pin this toolbar to the top when scrolling down';
      label.textContent = 'Pin Header';
    }
  });

  const mainHeader = document.getElementById('main-app-header');
  const mainHeaderHeight = mainHeader ? mainHeader.offsetHeight : 112;

  if (effectivePinned) {
    [stickyHeader, logsStickyHeader].forEach((el) => {
      if (!el) return;
      el.style.position = 'sticky';
      el.style.top = `${mainHeaderHeight + 4}px`;
      el.style.zIndex = '20';
    });

    const scrollY = window.scrollY || window.pageYOffset || 0;
    if (!isTransfersHeaderCompact && scrollY > 125) {
      isTransfersHeaderCompact = true;
    } else if (isTransfersHeaderCompact && scrollY < 45) {
      isTransfersHeaderCompact = false;
    }
  } else {
    [stickyHeader, logsStickyHeader].forEach((el) => {
      if (!el) return;
      el.style.position = 'static';
      el.style.top = '';
      el.style.zIndex = '';
    });
    isTransfersHeaderCompact = false;
  }

  // Active Transfers tab elements
  const topRow = document.getElementById('transfers-top-row');
  const filterBox = document.getElementById('transfers-filter-box');
  const headerTitle = document.getElementById('transfers-header-title');
  const headerDesc = document.getElementById('transfers-header-desc');
  const privacyBox = document.getElementById('transfers-privacy-box');
  const privacyTitle = document.getElementById('transfers-privacy-title');
  const privacyDesc = document.getElementById('transfers-privacy-desc');
  const groupBox = document.getElementById('transfers-group-box');

  // Cancellation Log tab elements
  const logsTopRow = document.getElementById('logs-top-row');
  const logsFilterBox = document.getElementById('logs-filter-box');
  const logsHeaderTitle = document.getElementById('logs-header-title');
  const logsHeaderDesc = document.getElementById('logs-header-desc');
  const logsPrivacyBox = document.getElementById('logs-privacy-box');
  const logsPrivacyTitle = document.getElementById('logs-privacy-title');
  const logsPrivacyDesc = document.getElementById('logs-privacy-desc');

  if (isTransfersHeaderCompact) {
    if (stickyHeader)
      stickyHeader.className =
        'space-y-1.5 p-2 rounded-xl bg-slate-950/95 backdrop-blur-md border border-slate-700/80 shadow-2xl transition-all duration-200';
    if (topRow) topRow.className = 'grid grid-cols-1 lg:grid-cols-4 gap-2 transition-all duration-200';
    if (filterBox)
      filterBox.className =
        'lg:col-span-3 flex flex-col sm:flex-row sm:items-center justify-between gap-2 bg-slate-900/90 border border-slate-800/80 rounded-lg px-3 py-1.5 transition-all duration-200';
    if (headerTitle)
      headerTitle.className = 'text-xs sm:text-sm font-semibold text-white truncate transition-all duration-200';
    if (headerDesc) headerDesc.classList.add('hidden');
    if (privacyBox)
      privacyBox.className =
        'lg:col-span-1 flex items-center justify-between gap-2 bg-slate-900/90 border border-slate-800/80 rounded-lg px-3 py-1.5 transition-all duration-200';
    if (privacyTitle)
      privacyTitle.className = 'text-xs font-semibold text-white truncate transition-all duration-200';
    if (privacyDesc) privacyDesc.classList.add('hidden');
    if (groupBox)
      groupBox.className =
        'flex flex-col sm:flex-row sm:items-center justify-between gap-2 bg-slate-900/90 border border-slate-800/80 rounded-lg px-3 py-1.5 transition-all duration-200';

    if (logsStickyHeader)
      logsStickyHeader.className =
        'p-2 rounded-xl bg-slate-950/95 backdrop-blur-md border border-slate-700/80 shadow-2xl transition-all duration-200';
    if (logsTopRow) logsTopRow.className = 'grid grid-cols-1 lg:grid-cols-4 gap-2 transition-all duration-200';
    if (logsFilterBox)
      logsFilterBox.className =
        'lg:col-span-3 flex flex-col sm:flex-row sm:items-center justify-between gap-2 bg-slate-900/90 border border-slate-800/80 rounded-lg px-3 py-1.5 transition-all duration-200';
    if (logsHeaderTitle)
      logsHeaderTitle.className = 'text-xs sm:text-sm font-semibold text-white truncate transition-all duration-200';
    if (logsHeaderDesc) logsHeaderDesc.classList.add('hidden');
    if (logsPrivacyBox)
      logsPrivacyBox.className =
        'lg:col-span-1 flex items-center justify-between gap-2 bg-slate-900/90 border border-slate-800/80 rounded-lg px-3 py-1.5 transition-all duration-200';
    if (logsPrivacyTitle)
      logsPrivacyTitle.className = 'text-xs font-semibold text-white truncate transition-all duration-200';
    if (logsPrivacyDesc) logsPrivacyDesc.classList.add('hidden');
  } else {
    if (stickyHeader) stickyHeader.className = 'space-y-3 transition-all duration-200';
    if (topRow) topRow.className = 'grid grid-cols-1 lg:grid-cols-4 gap-4 transition-all duration-200';
    if (filterBox)
      filterBox.className =
        'lg:col-span-3 flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-slate-900/80 border border-slate-800/80 rounded-xl p-4 transition-all duration-200';
    if (headerTitle)
      headerTitle.className = 'text-base font-semibold text-white truncate transition-all duration-200';
    if (headerDesc) headerDesc.classList.remove('hidden');
    if (privacyBox)
      privacyBox.className =
        'lg:col-span-1 flex items-center justify-between gap-3 bg-slate-900/80 border border-slate-800/80 rounded-xl p-4 transition-all duration-200';
    if (privacyTitle)
      privacyTitle.className = 'text-sm font-semibold text-white truncate transition-all duration-200';
    if (privacyDesc) privacyDesc.classList.remove('hidden');
    if (groupBox)
      groupBox.className =
        'flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 bg-slate-900/80 border border-slate-800/80 rounded-xl px-4 py-3 transition-all duration-200';

    if (logsStickyHeader) logsStickyHeader.className = 'transition-all duration-200';
    if (logsTopRow) logsTopRow.className = 'grid grid-cols-1 lg:grid-cols-4 gap-4 transition-all duration-200';
    if (logsFilterBox)
      logsFilterBox.className =
        'lg:col-span-3 flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-slate-900/80 border border-slate-800/80 rounded-xl p-4 transition-all duration-200';
    if (logsHeaderTitle)
      logsHeaderTitle.className = 'text-base font-semibold text-white truncate transition-all duration-200';
    if (logsHeaderDesc) logsHeaderDesc.classList.remove('hidden');
    if (logsPrivacyBox)
      logsPrivacyBox.className =
        'lg:col-span-1 flex items-center justify-between gap-3 bg-slate-900/80 border border-slate-800/80 rounded-xl p-4 transition-all duration-200';
    if (logsPrivacyTitle)
      logsPrivacyTitle.className = 'text-sm font-semibold text-white truncate transition-all duration-200';
    if (logsPrivacyDesc) logsPrivacyDesc.classList.remove('hidden');
  }
}

async function togglePinTransfersHeader(explicitVal) {
  if (!appState.settings) appState.settings = {};
  const currentVal = appState.settings.pin_transfers_header !== false;
  const nextVal = typeof explicitVal === 'boolean' ? explicitVal : !currentVal;
  appState.settings.pin_transfers_header = nextVal;
  updateTransfersStickyHeaderState();

  const deviceAllows = doesDeviceAllowPinnedTransfersHeader();
  if (nextVal && !deviceAllows) {
    showToast(
      'Pin Transfers Header enabled in options, but currently overridden on this device due to limited screen space.',
      'info'
    );
  } else {
    showToast(
      nextVal
        ? 'Transfers header pinned (will stick and compact on scroll).'
        : 'Transfers header unpinned.',
      'info'
    );
  }

  try {
    await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin_transfers_header: nextVal }),
    });
  } catch (_err) {
    // ignore transient error
  }
}

function updateUI(state) {
  appState = state;
  const s = state.settings || {};
  const m = state.monitor || {};
  const transfers = state.transfers || [];
  const trackers = state.trackers || [];
  const logs = state.activity_log || [];
  const tz = s.timezone || 'Europe/Berlin';

  // Sync privacy toggle switches across Active Transfers and Cancellation Log tabs
  syncPrivacyTogglesUI(Boolean(s.hide_transfer_names));
  updateTransfersStickyHeaderState();

  // Badges
  document.getElementById('badge-transfers-count').textContent = transfers.length;
  document.getElementById('badge-trackers-count').textContent = trackers.length;
  document.getElementById('badge-logs-count').textContent = logs.length;

  const thLogTime = document.getElementById('th-log-time');
  if (thLogTime) {
    thLogTime.textContent = `Date & Time (${tz})`;
  }

  // Service status badge
  const statusBadge = document.getElementById('service-status-badge');
  const quickToggle = document.getElementById('btn-quick-toggle');
  if (s.monitoring_enabled) {
    statusBadge.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/30';
    statusBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> 24/7 Active';
    quickToggle.textContent = 'Pause Monitor';
  } else {
    statusBadge.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-slate-800 text-slate-400 border border-slate-700';
    statusBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-slate-500"></span> Monitor Paused';
    quickToggle.textContent = 'Resume Monitor';
  }

  const dryRunBadge = document.getElementById('dry-run-badge');
  if (s.dry_run) {
    dryRunBadge.classList.remove('hidden');
  } else {
    dryRunBadge.classList.add('hidden');
  }

  // Header labels (token is 100% anonymized)
  document.getElementById('last-poll-label').textContent = m.last_poll_at
    ? `Last scan: ${formatDateTime(m.last_poll_at)}`
    : 'Last scan: Pending';

  if (m.account_info && m.account_info.username) {
    document.getElementById('account-label').textContent = `Put.io: @${m.account_info.username}`;
  } else if (s.has_token) {
    document.getElementById('account-label').textContent = 'Token: ••••••••••••••••';
  } else {
    document.getElementById('account-label').textContent = 'No token configured';
  }

  // Alert Banner
  const alertBanner = document.getElementById('alert-banner');
  const alertText = document.getElementById('alert-banner-text');
  if (!s.has_token) {
    alertBanner.classList.remove('hidden');
    alertText.textContent = 'Put.io OAuth token is not configured yet. Connect your Put.io account to start monitoring transfers.';
  } else if (m.last_error) {
    alertBanner.classList.remove('hidden');
    alertText.textContent = m.last_error;
  } else {
    alertBanner.classList.add('hidden');
  }

  // KPIs
  const seedingCount = transfers.filter((t) => String(t.status || '').toUpperCase() === 'SEEDING').length;
  const enabledRulesCount = trackers.filter((t) => t.enabled).length + (state.default_rule && state.default_rule.enabled ? 1 : 0);
  document.getElementById('kpi-transfers').textContent = transfers.length;
  document.getElementById('kpi-seeding').textContent = `${seedingCount} seeding`;
  document.getElementById('kpi-trackers').textContent = trackers.length;
  document.getElementById('kpi-active-rules').textContent = `${enabledRulesCount} active rule${enabledRulesCount === 1 ? '' : 's'}`;
  document.getElementById('kpi-canceled').textContent = logs.length;
  document.getElementById('kpi-interval').textContent = `${s.poll_interval_minutes || 5}m`;

  renderTransfers();
  renderTrackers();
  renderLogs();
  renderSettingsForm();
}

function getStatusBadgeHtml(status) {
  const st = String(status || 'UNKNOWN').toUpperCase();
  const styles = {
    SEEDING: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
    DOWNLOADING: 'bg-sky-500/15 text-sky-400 border-sky-500/30',
    COMPLETED: 'bg-indigo-500/15 text-indigo-300 border-indigo-500/30',
    COMPLETING: 'bg-indigo-500/15 text-indigo-300 border-indigo-500/30',
    IN_QUEUE: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
    WAITING: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
    ERROR: 'bg-red-500/15 text-red-400 border-red-500/30',
  };
  const cls = styles[st] || 'bg-slate-800 text-slate-300 border-slate-700';
  return `<span class="px-2.5 py-0.5 rounded-full text-[11px] font-semibold border ${cls}">${escapeHtml(st)}</span>`;
}

function toggleTransferGroupBy(mode) {
  if (currentTransferGrouping === mode) {
    currentTransferGrouping = 'none';
  } else {
    currentTransferGrouping = mode;
  }
  // Collapse all groups by default whenever a grouping is selected
  expandedTransferGroups.clear();
  updateTransferGroupButtonsUI();
  renderTransfers();
}

function toggleTransferGroupExpand(groupId) {
  if (expandedTransferGroups.has(groupId)) {
    expandedTransferGroups.delete(groupId);
  } else {
    expandedTransferGroups.add(groupId);
  }
  renderTransfers();
}

function updateTransferGroupButtonsUI() {
  const modes = ['tracker', 'status', 'protection', 'notification'];
  modes.forEach((m) => {
    const btn = document.getElementById(`btn-group-${m}`);
    const dot = document.getElementById(`dot-group-${m}`);
    if (!btn) return;
    const active = currentTransferGrouping === m;
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    if (active) {
      btn.className =
        'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition cursor-pointer bg-amber-500/20 border-amber-500/50 text-amber-300 shadow-sm';
      if (dot) dot.className = 'w-2 h-2 rounded-full bg-amber-400 transition';
    } else {
      btn.className =
        'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition cursor-pointer bg-slate-950 border-slate-800 text-slate-300 hover:border-slate-700 hover:text-white';
      if (dot) dot.className = 'w-2 h-2 rounded-full bg-slate-600 transition';
    }
  });

  const hint = document.getElementById('transfer-group-active-hint');
  if (hint) {
    const hintLabels = {
      none: '(Ungrouped)',
      tracker: '(Grouped by Tracker — click a group to expand/collapse)',
      status: '(Grouped by Status — click a group to expand/collapse)',
      protection: '(Grouped by Protected / Unprotected — click a group to expand/collapse)',
      notification: '(Grouped by Notification Status — click a group to expand/collapse)',
    };
    hint.textContent = hintLabels[currentTransferGrouping] || '(Ungrouped)';
  }
}

function renderSingleTransferCardHtml(t, hideNames) {
  const ev = t.evaluation || {};
  const ratio = Number(t.current_ratio || 0).toFixed(2);
  const seedingSecs = Number(t.seconds_seeding || 0);
  const upSpeed = Number(t.up_speed || 0);
  const downSpeed = Number(t.down_speed || 0);
  const trackersList = t.trackers_list || [{ tracker_id: t.primary_tracker || 'untracked' }];
  const primaryTrackerLabel = getTrackerDisplayName(t.primary_tracker);
  const isNotified = Boolean(ev.notification_active);
  const statusUpper = String(t.status || '').toUpperCase();

  const trackerBadges = trackersList
    .map((tr) => {
      const trId = tr.tracker_id;
      const trLabel = getTrackerDisplayName(trId);
      const configuredRule = (appState.trackers || []).find((r) => r.tracker_id === trId);
      const isEnabled = configuredRule && configuredRule.enabled;
      return `
        <button
          onclick="openRuleModal(${JSON.stringify(trId).replace(/"/g, '&quot;')})"
          title="Click to configure rule for ${escapeHtml(trLabel)}"
          class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium border transition cursor-pointer ${
            isEnabled
              ? 'bg-amber-500/15 border-amber-500/40 text-amber-300 hover:bg-amber-500/25'
              : 'bg-slate-800/90 border-slate-700 text-slate-300 hover:border-amber-500/50 hover:text-amber-300'
          }"
        >
          <span class="w-1.5 h-1.5 rounded-full ${isEnabled ? 'bg-amber-400' : 'bg-slate-500'}"></span>
          <span>${escapeHtml(trLabel)}</span>
          <span class="text-[10px] text-slate-400 underline ml-1">Rule &⚙️</span>
        </button>`;
    })
    .join('');

  const protectionBadge = ev.protection_active && ev.protection_label
    ? `<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-sky-500/20 text-sky-300 border border-sky-500/40">
         ★ Protected: ${escapeHtml(ev.protection_label)}
       </span>`
    : '';

  const notificationBadge = isNotified
    ? `<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-orange-500/20 text-orange-300 border border-orange-500/50">
         ⚠ ${escapeHtml(ev.notification_label || 'Notified')}
       </span>`
    : '';

  const notificationBannerHtml = isNotified
    ? `<div class="mb-3 px-3 py-2 rounded-lg bg-orange-500/15 border border-orange-500/40 flex flex-wrap items-center justify-between gap-2 text-xs text-orange-200">
         <div class="flex items-center gap-2 font-semibold">
           <span class="text-orange-400">⚠ ${escapeHtml(ev.notification_label || 'Transfer Notification')}:</span>
           <span>${escapeHtml(ev.notification_reason || '')}</span>
         </div>
         <span class="px-2 py-0.5 rounded bg-orange-500/20 text-[10px] font-bold uppercase tracking-wider text-orange-300">Pinned to Top</span>
       </div>`
    : '';

  let ruleProgressHtml = '';
  if (ev.rule_enabled) {
    const isStalledDownloadEval = statusUpper === 'DOWNLOADING' && Boolean(ev.cancel_stalled_download_enabled);
    const barColor = ev.protection_active
      ? 'bg-sky-400'
      : isStalledDownloadEval
      ? 'bg-orange-500'
      : 'bg-amber-500';
    const timeLabel = ev.protection_active
      ? `Keep-Alive Progress (${formatDuration(ev.evaluated_seconds)} / ${formatDuration(ev.target_seconds)})`
      : isStalledDownloadEval
      ? `Stalled Download Cancel Progress (${formatDuration(ev.evaluated_seconds)} / ${formatDuration(ev.target_seconds)})`
      : ev.cancel_on_completed
      ? 'Immediate Cancel on Download Complete'
      : `Time Progress (${formatDuration(ev.evaluated_seconds)} / ${formatDuration(ev.target_seconds)})`;

    const timeBar = ev.use_time_rule
      ? `
        <div class="space-y-1">
          <div class="flex justify-between text-[11px]">
            <span class="text-slate-400">${timeLabel}</span>
            <span class="font-mono text-slate-200">${ev.time_progress_pct}% ${
              ev.remaining_seconds !== null && ev.remaining_seconds > 0
                ? `(${formatDuration(ev.remaining_seconds)} left)`
                : ev.remaining_seconds === 0
                ? '(Reached!)'
                : ''
            }</span>
          </div>
          <div class="w-full h-1.5 bg-slate-800 rounded-full overflow-hidden">
            <div class="h-full ${barColor} transition-all" style="width: ${Math.min(100, ev.time_progress_pct)}%"></div>
          </div>
        </div>`
      : '';

    const ratioBar = ev.use_ratio_rule
      ? `
        <div class="space-y-1">
          <div class="flex justify-between text-[11px]">
            <span class="text-slate-400">Ratio Progress (${ratio} / ${Number(ev.target_ratio).toFixed(2)})</span>
            <span class="font-mono text-slate-200">${ev.ratio_progress_pct}%</span>
          </div>
          <div class="w-full h-1.5 bg-slate-800 rounded-full overflow-hidden">
            <div class="h-full bg-emerald-500 transition-all" style="width: ${Math.min(100, ev.ratio_progress_pct)}%"></div>
          </div>
        </div>`
      : '';

    ruleProgressHtml = `
      <div class="mt-3 pt-3 border-t border-slate-800/80 space-y-2">
        <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
          <div class="flex flex-wrap items-center gap-2">
            <span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 font-semibold text-[11px]">Active Rule: ${escapeHtml(ev.rule_summary)}</span>
            ${protectionBadge}
            ${
              !ev.eligible_status
                ? '<span class="text-[11px] text-slate-500">(Waiting for SEEDING state)</span>'
                : ''
            }
          </div>
          ${
            ev.should_cancel
              ? '<span class="text-xs font-bold text-red-400">Threshold Reached</span>'
              : ''
          }
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          ${timeBar}
          ${ratioBar}
        </div>
      </div>`;
  } else {
    ruleProgressHtml = `
      <div class="mt-3 pt-2.5 border-t border-slate-800/60 flex items-center justify-between text-xs text-slate-500">
        <span>No active auto-cancel rule for <strong class="text-slate-300">${escapeHtml(primaryTrackerLabel)}</strong></span>
        <button onclick="openRuleModal(${JSON.stringify(t.primary_tracker).replace(/"/g, '&quot;')})" class="text-amber-400 hover:underline font-medium cursor-pointer">
          + Set Rule for ${escapeHtml(primaryTrackerLabel)}
        </button>
      </div>`;
  }

  const nameBlurClasses = hideNames ? 'blur-[6px] select-none' : '';
  const nameTitleAttr = hideNames ? '' : `title="${escapeHtml(t.name)}"`;
  const cardBorderClasses = isNotified
    ? 'bg-orange-950/20 border-2 border-orange-500/70 shadow-lg shadow-orange-500/10'
    : 'bg-slate-900 border border-slate-800 hover:border-slate-700/80';

  return `
    <div class="${cardBorderClasses} rounded-xl p-4 transition">
      ${notificationBannerHtml}
      <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-3">
        <div class="space-y-1.5 min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            ${getStatusBadgeHtml(t.status)}
            ${notificationBadge}
            <h3 class="font-semibold text-sm sm:text-base text-white truncate transition ${nameBlurClasses}" ${nameTitleAttr}>
              ${escapeHtml(t.name)}
            </h3>
          </div>
          <div class="flex flex-wrap items-center gap-2 pt-0.5">
            <span class="text-xs text-slate-400">Identified Tracker(s):</span>
            ${trackerBadges}
          </div>
        </div>

        <!-- Metrics & Manual Cancel -->
        <div class="flex flex-wrap items-center gap-4 text-xs sm:text-sm shrink-0">
          <div class="bg-slate-950 px-3 py-1.5 rounded-lg border border-slate-800">
            <span class="text-slate-400 text-xs block">Seeding Time</span>
            <span class="font-mono font-semibold text-white">${formatDuration(seedingSecs)}</span>
          </div>
          <div class="bg-slate-950 px-3 py-1.5 rounded-lg border border-slate-800">
            <span class="text-slate-400 text-xs block">Share Ratio</span>
            <span class="font-mono font-semibold text-emerald-400">${ratio}</span>
          </div>
          <div class="bg-slate-950 px-3 py-1.5 rounded-lg border border-slate-800">
            <span class="text-slate-400 text-xs block">Size / Speed</span>
            <span class="font-mono text-slate-200 text-xs">
              ${formatBytes(t.size)}
              ${upSpeed > 0 ? ` &bull; &uarr;${formatBytes(upSpeed)}/s` : ''}
              ${downSpeed > 0 ? ` &bull; &darr;${formatBytes(downSpeed)}/s` : ''}
            </span>
          </div>
          <button
            onclick="manualCancelTransfer(${Number(t.id)}, ${JSON.stringify(String(t.name || '')).replace(/"/g, '&quot;')})"
            class="px-3 py-2 rounded-lg bg-red-500/15 hover:bg-red-500/25 text-red-300 border border-red-500/30 text-xs font-semibold transition cursor-pointer"
          >
            Cancel &amp; Remove
          </button>
        </div>
      </div>
      ${ruleProgressHtml}
    </div>`;
}

function renderTransfers() {
  const container = document.getElementById('transfers-list');
  const filter = (document.getElementById('transfer-search')?.value || '').trim().toLowerCase();
  const hideNames = Boolean(appState.settings?.hide_transfer_names);
  updateTransferGroupButtonsUI();

  const transfers = (appState.transfers || [])
    .filter((t) => {
      if (!filter) return true;
      const nameMatch = String(t.name || '').toLowerCase().includes(filter);
      const trackerIdMatch = String(t.primary_tracker || '').toLowerCase().includes(filter);
      const trackerLabelMatch = getTrackerDisplayName(t.primary_tracker).toLowerCase().includes(filter);
      return nameMatch || trackerIdMatch || trackerLabelMatch;
    })
    .slice()
    .sort(
      (a, b) =>
        Number(Boolean(b.evaluation && b.evaluation.notification_active)) -
        Number(Boolean(a.evaluation && a.evaluation.notification_active))
    );

  if (!appState.settings?.has_token) {
    container.innerHTML = `
      <div class="bg-slate-900/70 border border-slate-800 rounded-xl p-8 text-center space-y-3">
        <div class="text-slate-300 font-semibold">Put.io Account Not Connected</div>
        <p class="text-xs text-slate-400 max-w-md mx-auto">
          Configure your Put.io OAuth token or link your device in the Settings tab to automatically detect trackers and monitor active transfers.
        </p>
        <button onclick="switchTab('settings')" class="px-4 py-2 rounded-lg bg-amber-500 hover:bg-amber-400 text-slate-950 font-semibold text-xs cursor-pointer">
          Connect Put.io Account
        </button>
      </div>`;
    return;
  }

  if (transfers.length === 0) {
    container.innerHTML = `
      <div class="bg-slate-900/70 border border-slate-800 rounded-xl p-8 text-center space-y-2">
        <div class="text-slate-300 font-semibold">No Matching Active Transfers on Put.io</div>
        <p class="text-xs text-slate-500">
          When transfers are active or seeding in your Put.io account, they will appear here along with their identified trackers.
        </p>
      </div>`;
    return;
  }

  if (currentTransferGrouping === 'none') {
    container.innerHTML = transfers
      .map((t) => renderSingleTransferCardHtml(t, hideNames))
      .join('');
    return;
  }

  // Build groups according to currentTransferGrouping
  let groups = [];

  if (currentTransferGrouping === 'tracker') {
    const map = new Map();
    transfers.forEach((t) => {
      const trId = t.primary_tracker || 'untracked';
      if (!map.has(trId)) {
        map.set(trId, []);
      }
      map.get(trId).push(t);
    });
    groups = Array.from(map.entries())
      .map(([trId, items]) => {
        const label = getTrackerDisplayName(trId);
        const configuredRule = (appState.trackers || []).find((r) => r.tracker_id === trId);
        const isEnabled = configuredRule && configuredRule.enabled;
        const actionHtml = `
          <button
            onclick="event.stopPropagation(); openRuleModal(${JSON.stringify(trId).replace(/"/g, '&quot;')})"
            class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-medium text-amber-400 border border-slate-700 cursor-pointer"
          >
            ${isEnabled ? 'Edit Active Rule' : '+ Configure Rule'}
          </button>`;
        return {
          key: trId,
          titleHtml: `
            <span class="inline-flex items-center gap-2 font-bold text-sm text-white">
              <span class="w-2 h-2 rounded-full ${isEnabled ? 'bg-amber-400' : 'bg-slate-500'}"></span>
              <span>${escapeHtml(label)}</span>
            </span>`,
          actionHtml,
          items,
        };
      })
      .sort((a, b) => b.items.length - a.items.length);
  } else if (currentTransferGrouping === 'status') {
    const statusOrder = ['SEEDING', 'DOWNLOADING', 'COMPLETING', 'COMPLETED', 'IN_QUEUE', 'WAITING', 'ERROR'];
    const map = new Map();
    transfers.forEach((t) => {
      const st = String(t.status || 'UNKNOWN').toUpperCase();
      if (!map.has(st)) {
        map.set(st, []);
      }
      map.get(st).push(t);
    });
    groups = Array.from(map.entries())
      .map(([st, items]) => ({
        key: st,
        titleHtml: `
          <span class="inline-flex items-center gap-2">
            ${getStatusBadgeHtml(st)}
            <span class="font-bold text-sm text-white">${escapeHtml(st)} Transfers</span>
          </span>`,
        actionHtml: '',
        items,
      }))
      .sort((a, b) => {
        const idxA = statusOrder.indexOf(a.key);
        const idxB = statusOrder.indexOf(b.key);
        return (idxA === -1 ? 999 : idxA) - (idxB === -1 ? 999 : idxB);
      });
  } else if (currentTransferGrouping === 'protection') {
    const protectedItems = transfers.filter((t) => Boolean(t.evaluation && t.evaluation.protection_active));
    const unprotectedItems = transfers.filter((t) => !t.evaluation || !t.evaluation.protection_active);
    groups = [
      {
        key: 'protected',
        titleHtml: `
          <span class="inline-flex items-center gap-2">
            <span class="px-2.5 py-0.5 rounded-full text-xs font-bold bg-sky-500/20 text-sky-300 border border-sky-500/40">★ Protected</span>
            <span class="font-bold text-sm text-white">Keep-Alive Protected Transfers (Top Seeded / Largest)</span>
          </span>`,
        actionHtml: '',
        items: protectedItems,
      },
      {
        key: 'unprotected',
        titleHtml: `
          <span class="inline-flex items-center gap-2">
            <span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-slate-800 text-slate-300 border border-slate-700">Unprotected</span>
            <span class="font-bold text-sm text-white">Standard Transfers (Not Protected by Keep-Alive)</span>
          </span>`,
        actionHtml: '',
        items: unprotectedItems,
      },
    ];
  } else if (currentTransferGrouping === 'notification') {
    const notifiedItems = transfers.filter((t) => Boolean(t.evaluation && t.evaluation.notification_active));
    const normalItems = transfers.filter((t) => !t.evaluation || !t.evaluation.notification_active);
    groups = [
      {
        key: 'notified',
        titleHtml: `
          <span class="inline-flex items-center gap-2">
            <span class="px-2.5 py-0.5 rounded-full text-xs font-bold bg-orange-500/20 text-orange-300 border border-orange-500/40">⚠ Notified / Highlighted</span>
            <span class="font-bold text-sm text-white">Stalled Downloads &amp; Long-Queued Alerts</span>
          </span>`,
        actionHtml: '',
        items: notifiedItems,
      },
      {
        key: 'normal',
        titleHtml: `
          <span class="inline-flex items-center gap-2">
            <span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-slate-800 text-slate-300 border border-slate-700">Normal</span>
            <span class="font-bold text-sm text-white">Transfers Without Active Alerts</span>
          </span>`,
        actionHtml: '',
        items: normalItems,
      },
    ];
  }

  container.innerHTML = groups
    .map((g) => {
      const groupId = `${currentTransferGrouping}:${g.key}`;
      const isExpanded = expandedTransferGroups.has(groupId);
      const totalSize = g.items.reduce((acc, item) => acc + (Number(item.size) || 0), 0);
      const totalUpSpeed = g.items.reduce((acc, item) => acc + (Number(item.up_speed) || 0), 0);
      const bodyHtml =
        g.items.length > 0
          ? g.items.map((t) => renderSingleTransferCardHtml(t, hideNames)).join('')
          : `<div class="text-xs text-slate-500 py-3 px-2 italic">No transfers currently in this group.</div>`;

      return `
        <div class="bg-slate-950/60 border border-slate-800/90 rounded-xl overflow-hidden transition">
          <div
            onclick="toggleTransferGroupExpand(${JSON.stringify(groupId).replace(/"/g, '&quot;')})"
            role="button"
            aria-expanded="${isExpanded ? 'true' : 'false'}"
            class="flex flex-wrap items-center justify-between gap-2 px-4 py-3.5 cursor-pointer select-none hover:bg-slate-900/80 transition"
          >
            <div class="flex flex-wrap items-center gap-2.5">
              <svg
                class="w-4 h-4 text-slate-400 transition-transform duration-200 ${isExpanded ? 'rotate-90 text-amber-400' : ''}"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"></path>
              </svg>
              ${g.titleHtml}
              <span class="px-2 py-0.5 rounded-full text-xs font-mono bg-slate-800 text-slate-300">
                ${g.items.length} transfer${g.items.length === 1 ? '' : 's'}
              </span>
              <span class="text-xs font-mono text-slate-400">
                Total: ${formatBytes(totalSize)}${totalUpSpeed > 0 ? ` &bull; &uarr;${formatBytes(totalUpSpeed)}/s` : ''}
              </span>
            </div>
            <div class="flex items-center gap-2.5">
              ${g.actionHtml}
              <span class="text-[11px] font-medium ${isExpanded ? 'text-amber-400' : 'text-slate-500'}">
                ${isExpanded ? 'Click to collapse' : 'Click to expand'}
              </span>
            </div>
          </div>
          ${
            isExpanded
              ? `<div class="p-4 pt-3 border-t border-slate-800/80 space-y-3 bg-slate-950/40">
                   ${bodyHtml}
                 </div>`
              : ''
          }
        </div>`;
    })
    .join('');
}

function renderTrackerCardHtml(r, isDefault = false) {
  const totalMinutes = Number(r.max_time_minutes || 0);
  const hours = Math.floor(totalMinutes / 60);
  const mins = Math.round(totalMinutes % 60);
  const timeText = r.cancel_on_completed
    ? 'Immediate on Download Complete'
    : `${hours}h ${mins}m (${r.time_metric === 'total_age' ? 'Total Age' : 'Seeding Time'})`;

  const displayLabel = isDefault
    ? 'Default Fallback Rule (Unmatched Trackers)'
    : getTrackerDisplayName(r.tracker_id);

  const keepParts = [];
  if (r.keep_top_seeded_enabled) {
    keepParts.push(`Top ${r.keep_top_seeded_count} Seeded`);
  }
  if (r.keep_largest_enabled) {
    keepParts.push(`Top ${r.keep_largest_count} Largest`);
  }
  const keepDurationText = formatDuration(Number(r.keep_alive_hours || 72) * 3600);

  return `
    <div class="bg-slate-900 border ${
      r.enabled ? 'border-amber-500/50 shadow-lg shadow-amber-500/5' : 'border-slate-800'
    } rounded-xl p-5 flex flex-col justify-between gap-4">
      <div class="space-y-3">
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-bold text-sm sm:text-base text-white truncate">
                ${escapeHtml(displayLabel)}
              </span>
              ${
                !isDefault && r.discovered_from_transfers
                  ? '<span class="px-2 py-0.5 text-[10px] rounded-full bg-sky-500/15 text-sky-300 border border-sky-500/30">Auto-Discovered</span>'
                  : ''
              }
              ${
                isDefault
                  ? '<span class="px-2 py-0.5 text-[10px] rounded-full bg-purple-500/15 text-purple-300 border border-purple-500/30">Global Fallback</span>'
                  : ''
              }
              ${
                r.cancel_completed_on_first_scan
                  ? '<span class="px-2 py-0.5 text-[10px] rounded-full bg-indigo-500/15 text-indigo-300 border border-indigo-500/30">Instant Completed Cancel</span>'
                  : ''
              }
              ${
                r.cancel_on_completed
                  ? '<span class="px-2 py-0.5 text-[10px] rounded-full bg-rose-500/15 text-rose-300 border border-rose-500/30">Cancel on Complete</span>'
                  : ''
              }
              ${
                r.cancel_stalled_download_enabled
                  ? `<span class="px-2 py-0.5 text-[10px] rounded-full bg-orange-500/15 text-orange-300 border border-orange-500/30">Cancel Stalled DL (${escapeHtml(formatDuration(Number(r.cancel_stalled_download_hours || 24) * 3600))})</span>`
                  : ''
              }
              ${
                r.notify_stalled_download_enabled
                  ? `<span class="px-2 py-0.5 text-[10px] rounded-full bg-amber-500/15 text-amber-300 border border-amber-500/30">Notify Stalled DL (${escapeHtml(formatDuration(Number(r.notify_stalled_download_hours || 6) * 3600))})</span>`
                  : ''
              }
              ${
                r.notify_long_queued_enabled
                  ? `<span class="px-2 py-0.5 text-[10px] rounded-full bg-yellow-500/15 text-yellow-300 border border-yellow-500/30">Notify Queued (${escapeHtml(formatDuration(Number(r.notify_long_queued_hours || 6) * 3600))})</span>`
                  : ''
              }
            </div>
            ${
              isDefault
                ? '<p class="text-xs text-slate-400 mt-0.5">Applies to any transfer whose tracker does not have its own specific rule enabled.</p>'
                : r.sample_announce_url
                ? `<p class="text-[11px] text-slate-500 font-mono truncate mt-0.5" title="${escapeHtml(r.sample_announce_url)}">${escapeHtml(r.sample_announce_url)}</p>`
                : ''
            }
          </div>

          <!-- Quick Enable Toggle -->
          <button
            onclick="quickToggleRule(${JSON.stringify(r.tracker_id).replace(/"/g, '&quot;')})"
            class="px-3 py-1 rounded-full text-xs font-bold border transition cursor-pointer shrink-0 ${
              r.enabled
                ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40 hover:bg-emerald-500/30'
                : 'bg-slate-800 text-slate-400 border-slate-700 hover:text-slate-200'
            }"
          >
            ${r.enabled ? '● Rule Enabled' : '○ Disabled'}
          </button>
        </div>

        <!-- Rule Conditions Summary Boxes -->
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
          <div class="p-2.5 rounded-lg bg-slate-950 border ${
            r.cancel_on_completed
              ? 'border-rose-500/40 text-rose-200'
              : r.use_time_rule
              ? 'border-amber-500/30 text-slate-200'
              : 'border-slate-800/80 text-slate-500'
          }">
            <div class="text-[10px] uppercase tracking-wider text-slate-400">Time / Complete Rule</div>
            <div class="font-semibold mt-0.5">${
              r.cancel_on_completed || r.use_time_rule ? escapeHtml(timeText) : 'Off'
            }</div>
          </div>

          <div class="p-2.5 rounded-lg bg-slate-950 border border-slate-800/80 flex flex-col justify-center items-center">
            <div class="text-[10px] uppercase tracking-wider text-slate-400">Condition Logic</div>
            <div class="font-bold text-amber-400 mt-0.5">
              ${
                r.cancel_on_completed
                  ? 'Immediate'
                  : r.use_time_rule && r.use_ratio_rule
                  ? `${r.condition_operator} (${r.condition_operator === 'AND' ? 'Both Required' : 'Either Triggers'})`
                  : 'Single Rule'
              }
            </div>
          </div>

          <div class="p-2.5 rounded-lg bg-slate-950 border ${
            r.use_ratio_rule && !r.cancel_on_completed
              ? 'border-emerald-500/30 text-slate-200'
              : 'border-slate-800/80 text-slate-500'
          }">
            <div class="text-[10px] uppercase tracking-wider text-slate-400">Ratio Rule</div>
            <div class="font-semibold mt-0.5">${
              r.use_ratio_rule && !r.cancel_on_completed ? `Ratio >= ${Number(r.max_ratio).toFixed(2)}` : 'Off'
            }</div>
          </div>
        </div>

        <!-- Keep-Alive Summary Banner -->
        <div class="p-2.5 rounded-lg bg-slate-950 border ${
          keepParts.length > 0 ? 'border-sky-500/40 text-sky-200' : 'border-slate-800/80 text-slate-500'
        } text-xs flex items-center justify-between">
          <span class="text-[10px] uppercase tracking-wider text-slate-400">Keep-Alive Exceptions</span>
          <span class="font-semibold">
            ${
              keepParts.length > 0
                ? `★ Keep ${escapeHtml(keepParts.join(' + '))} for ${escapeHtml(keepDurationText)}`
                : 'None configured'
            }
          </span>
        </div>
      </div>

      <div class="flex items-center justify-between pt-3 border-t border-slate-800/80 text-xs">
        <div class="text-slate-400 flex items-center gap-3">
          ${
            !isDefault
              ? `<span>Active transfers: <strong class="text-white">${r.matched_transfers_count || 0}</strong></span>
                 <span>Canceled: <strong class="text-amber-300">${r.canceled_transfers_count || 0}</strong></span>`
              : `<span>Summary: <strong class="text-amber-300">${escapeHtml(r.rule_summary)}</strong></span>`
          }
        </div>
        <div class="flex items-center gap-2">
          ${
            !isDefault
              ? `<button
                   onclick="deleteTrackerRule(${JSON.stringify(r.tracker_id).replace(/"/g, '&quot;')})"
                   class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-red-950/70 text-slate-400 hover:text-red-300 transition cursor-pointer"
                   title="Remove tracker from list"
                 >
                   Delete
                 </button>`
              : ''
          }
          <button
            onclick="openRuleModal(${JSON.stringify(r.tracker_id).replace(/"/g, '&quot;')})"
            class="px-3.5 py-1.5 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/40 text-amber-300 font-semibold transition cursor-pointer"
          >
            Edit Rule
          </button>
        </div>
      </div>
    </div>`;
}

function renderTrackers() {
  const defaultContainer = document.getElementById('default-rule-container');
  if (appState.default_rule) {
    defaultContainer.innerHTML = renderTrackerCardHtml(appState.default_rule, true);
  }

  const listEl = document.getElementById('trackers-list');
  const query = (document.getElementById('tracker-search')?.value || '').trim().toLowerCase();
  const trackers = (appState.trackers || []).filter((r) => {
    if (!query) return true;
    return (
      String(r.tracker_id || '').toLowerCase().includes(query) ||
      String(r.display_name || '').toLowerCase().includes(query) ||
      String(r.sample_announce_url || '').toLowerCase().includes(query)
    );
  });

  if (trackers.length === 0) {
    listEl.innerHTML = `
      <div class="col-span-full bg-slate-900/60 border border-slate-800 rounded-xl p-8 text-center space-y-3">
        <div class="text-slate-300 font-semibold">No Trackers Discovered Yet</div>
        <p class="text-xs text-slate-400 max-w-lg mx-auto">
          Click <strong>"Discover from Active Transfers"</strong> while you have active transfers on Put.io to automatically populate your trackers, or add a tracker hostname manually.
        </p>
        <div class="flex justify-center gap-2">
          <button onclick="scanNow()" class="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-semibold text-white cursor-pointer">
            Scan Active Transfers
          </button>
          <button onclick="openRuleModal(null)" class="px-4 py-2 rounded-lg bg-amber-500 hover:bg-amber-400 text-xs font-semibold text-slate-950 cursor-pointer">
            + Add Tracker Manually
          </button>
        </div>
      </div>`;
    return;
  }

  listEl.innerHTML = trackers.map((r) => renderTrackerCardHtml(r, false)).join('');
}

function renderLogs() {
  const tbody = document.getElementById('logs-table-body');
  const allLogs = appState.activity_log || [];
  const filter = (document.getElementById('log-search')?.value || '').trim().toLowerCase();
  const hideNames = Boolean(appState.settings?.hide_transfer_names);

  const logs = allLogs.filter((entry) => {
    if (!filter) return true;
    const nameMatch = String(entry.transfer_name || '').toLowerCase().includes(filter);
    const trackerIdMatch = String(entry.tracker_id || '').toLowerCase().includes(filter);
    const trackerLabelMatch = getTrackerDisplayName(entry.tracker_id).toLowerCase().includes(filter);
    const reasonMatch = String(entry.reason || '').toLowerCase().includes(filter);
    const ruleMatch = String(entry.rule_summary || '').toLowerCase().includes(filter);
    const statusMatch = String(entry.status_at_cancel || '').toLowerCase().includes(filter);
    return nameMatch || trackerIdMatch || trackerLabelMatch || reasonMatch || ruleMatch || statusMatch;
  });

  if (logs.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="6" class="py-8 text-center text-slate-500 text-xs">
          ${
            allLogs.length === 0
              ? 'No transfers have been canceled yet.'
              : 'No cancellation log entries match your filter.'
          }
        </td>
      </tr>`;
    return;
  }

  const nameBlurClasses = hideNames ? 'blur-[6px] select-none' : '';

  tbody.innerHTML = logs
    .map((entry) => {
      const timeFormatted = formatDateTime(entry.timestamp);
      const trackerDisplay = getTrackerDisplayName(entry.tracker_id);
      const nameTitleAttr = hideNames ? '' : `title="${escapeHtml(entry.transfer_name)}"`;
      return `
        <tr class="hover:bg-slate-800/40">
          <td class="py-3 px-4 font-mono text-xs text-slate-400 whitespace-nowrap">${escapeHtml(timeFormatted)}</td>
          <td class="py-3 px-4 max-w-xs">
            <div class="font-medium text-white truncate transition ${nameBlurClasses}" ${nameTitleAttr}>
              ${escapeHtml(entry.transfer_name)}
            </div>
            <div class="text-[11px] text-slate-500 font-mono">ID: ${entry.transfer_id} &bull; ${escapeHtml(entry.status_at_cancel)}</div>
          </td>
          <td class="py-3 px-4 font-semibold text-xs text-amber-300">${escapeHtml(trackerDisplay)}</td>
          <td class="py-3 px-4 text-xs text-slate-300 whitespace-nowrap">
            <div>Seeded: <span class="font-mono text-white">${formatDuration(entry.seeding_seconds)}</span></div>
            <div>Ratio: <span class="font-mono text-emerald-400">${Number(entry.ratio_at_cancel || 0).toFixed(2)}</span></div>
          </td>
          <td class="py-3 px-4 text-xs">
            <div class="font-semibold text-slate-200">${escapeHtml(entry.reason)}</div>
            <div class="text-[11px] text-slate-500">Rule: ${escapeHtml(entry.rule_summary)}</div>
          </td>
          <td class="py-3 px-4 text-xs whitespace-nowrap">
            ${
              entry.dry_run
                ? '<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">Dry-Run Simulated</span>'
                : entry.success
                ? '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">Canceled &amp; Removed</span>'
                : `<span class="px-2 py-0.5 rounded bg-red-500/20 text-red-300 border border-red-500/30" title="${escapeHtml(entry.error_message)}">Failed</span>`
            }
          </td>
        </tr>`;
    })
    .join('');
}

function renderSettingsForm() {
  const s = appState.settings || {};
  const m = appState.monitor || {};

  document.getElementById('chk-monitoring-enabled').checked = Boolean(s.monitoring_enabled);
  document.getElementById('chk-auto-discover').checked = Boolean(s.auto_discover_trackers);
  document.getElementById('chk-clean-after-cancel').checked = Boolean(s.clean_completed_after_cancel);
  const pinChk = document.getElementById('chk-pin-transfers-header');
  if (pinChk) {
    pinChk.checked = s.pin_transfers_header !== false;
  }
  document.getElementById('chk-dry-run').checked = Boolean(s.dry_run);
  document.getElementById('input-poll-interval').value = s.poll_interval_minutes || 5;

  const appIdInput = document.getElementById('input-putio-app-id');
  if (appIdInput && document.activeElement !== appIdInput) {
    appIdInput.value = s.putio_app_id || '';
  }

  const statusEl = document.getElementById('settings-conn-status');
  const detailEl = document.getElementById('settings-conn-detail');
  const appInfoEl = document.getElementById('settings-app-info');

  if (m.account_info && m.account_info.username) {
    statusEl.className = 'font-semibold text-emerald-400';
    statusEl.textContent = `Connected as @${m.account_info.username}`;
    detailEl.textContent = `Account Email: ${m.account_info.mail || 'N/A'} | Token: ••••••••••••••••`;
  } else if (s.has_token) {
    statusEl.className = 'font-semibold text-amber-300';
    statusEl.textContent = 'Token Saved (••••••••••••••••)';
    detailEl.textContent = m.last_error || 'Ready to poll Put.io API.';
  } else {
    statusEl.className = 'font-semibold text-red-400';
    statusEl.textContent = 'Not Configured';
    detailEl.textContent = 'Please enter a Put.io OAuth token or generate a PutWatch device code below.';
  }

  if (appInfoEl) {
    if (s.putio_app_id) {
      appInfoEl.innerHTML = `Registered Put.io OAuth App: <strong class="text-white">PutWatch</strong> (ID: <code class="text-amber-300">${s.putio_app_id}</code> &bull; <a href="https://github.com/ben68165/PutWatch" target="_blank" rel="noopener" class="text-amber-400 hover:underline">github.com/ben68165/PutWatch</a>)`;
    } else {
      appInfoEl.innerHTML = `OAuth App Metadata: <strong class="text-white">PutWatch</strong> (<a href="https://github.com/ben68165/PutWatch" target="_blank" rel="noopener" class="text-amber-400 hover:underline">github.com/ben68165/PutWatch</a>)`;
    }
  }
}

// Rule Modal Management
function openRuleModal(trackerId) {
  const modal = document.getElementById('rule-modal');
  const idInput = document.getElementById('modal-tracker-id');
  const nameInput = document.getElementById('modal-display-name');
  const sampleUrlEl = document.getElementById('modal-sample-url');
  const enabledChk = document.getElementById('modal-enabled');
  const cancelCompletedFirstScanChk = document.getElementById('modal-cancel-completed-first-scan');
  const cancelOnCompletedChk = document.getElementById('modal-cancel-on-completed');

  const notifyStalledDownloadChk = document.getElementById('modal-notify-stalled-download');
  const notifyStalledDaysInput = document.getElementById('modal-notify-stalled-days');
  const notifyStalledHoursInput = document.getElementById('modal-notify-stalled-hours');
  const cancelStalledDownloadChk = document.getElementById('modal-cancel-stalled-download');
  const cancelStalledDaysInput = document.getElementById('modal-cancel-stalled-days');
  const cancelStalledHoursInput = document.getElementById('modal-cancel-stalled-hours');
  const notifyLongQueuedChk = document.getElementById('modal-notify-long-queued');
  const notifyQueuedDaysInput = document.getElementById('modal-notify-queued-days');
  const notifyQueuedHoursInput = document.getElementById('modal-notify-queued-hours');

  const useTimeChk = document.getElementById('modal-use-time');
  const hoursInput = document.getElementById('modal-time-hours');
  const minsInput = document.getElementById('modal-time-minutes');
  const metricSelect = document.getElementById('modal-time-metric');
  const useRatioChk = document.getElementById('modal-use-ratio');
  const ratioInput = document.getElementById('modal-max-ratio');
  const keepTopSeededChk = document.getElementById('modal-keep-top-seeded');
  const keepTopSeededCountInput = document.getElementById('modal-keep-top-seeded-count');
  const topSeededMetricSelect = document.getElementById('modal-top-seeded-metric');
  const keepLargestChk = document.getElementById('modal-keep-largest');
  const keepLargestCountInput = document.getElementById('modal-keep-largest-count');
  const keepAliveDaysInput = document.getElementById('modal-keep-alive-days');
  const keepAliveHoursInput = document.getElementById('modal-keep-alive-hours');

  let existing = null;
  if (trackerId === '__default__') {
    existing = appState.default_rule;
  } else if (trackerId) {
    existing = (appState.trackers || []).find((t) => t.tracker_id === trackerId);
  }

  if (existing) {
    const label = getTrackerDisplayName(existing.tracker_id);
    document.getElementById('modal-title').textContent =
      trackerId === '__default__' ? 'Edit Default Fallback Rule' : `Configure Rule: ${label}`;
    idInput.value = existing.tracker_id;
    idInput.disabled = trackerId === '__default__';
    nameInput.value = existing.display_name || '';
    sampleUrlEl.textContent = existing.sample_announce_url ? `Sample Announce URL: ${existing.sample_announce_url}` : '';
    enabledChk.checked = Boolean(existing.enabled);
    cancelCompletedFirstScanChk.checked = Boolean(existing.cancel_completed_on_first_scan);
    cancelOnCompletedChk.checked = Boolean(existing.cancel_on_completed);

    notifyStalledDownloadChk.checked = Boolean(existing.notify_stalled_download_enabled);
    const totalNotifyStalledHours = Number(existing.notify_stalled_download_hours ?? 6);
    notifyStalledDaysInput.value = Math.floor(totalNotifyStalledHours / 24);
    notifyStalledHoursInput.value = Math.round(totalNotifyStalledHours % 24);

    cancelStalledDownloadChk.checked = Boolean(existing.cancel_stalled_download_enabled);
    const totalCancelStalledHours = Number(existing.cancel_stalled_download_hours ?? 24);
    cancelStalledDaysInput.value = Math.floor(totalCancelStalledHours / 24);
    cancelStalledHoursInput.value = Math.round(totalCancelStalledHours % 24);

    notifyLongQueuedChk.checked = Boolean(existing.notify_long_queued_enabled);
    const totalNotifyQueuedHours = Number(existing.notify_long_queued_hours ?? 6);
    notifyQueuedDaysInput.value = Math.floor(totalNotifyQueuedHours / 24);
    notifyQueuedHoursInput.value = Math.round(totalNotifyQueuedHours % 24);

    useTimeChk.checked = Boolean(existing.use_time_rule);
    const totalMins = Number(existing.max_time_minutes || 0);
    hoursInput.value = Math.floor(totalMins / 60);
    minsInput.value = Math.round(totalMins % 60);
    metricSelect.value = existing.time_metric || 'seeding_time';
    useRatioChk.checked = Boolean(existing.use_ratio_rule);
    ratioInput.value = Number(existing.max_ratio || 1.0).toFixed(2);

    keepTopSeededChk.checked = Boolean(existing.keep_top_seeded_enabled);
    keepTopSeededCountInput.value = Number(existing.keep_top_seeded_count || 3);
    topSeededMetricSelect.value = existing.top_seeded_metric || 'up_speed';
    keepLargestChk.checked = Boolean(existing.keep_largest_enabled);
    keepLargestCountInput.value = Number(existing.keep_largest_count || 3);

    const totalKeepHours = Number(existing.keep_alive_hours || 72);
    keepAliveDaysInput.value = Math.floor(totalKeepHours / 24);
    keepAliveHoursInput.value = Math.round(totalKeepHours % 24);

    const opRadios = document.getElementsByName('modal-operator');
    opRadios.forEach((r) => {
      r.checked = r.value === (existing.condition_operator || 'OR');
    });
  } else {
    document.getElementById('modal-title').textContent = trackerId
      ? `Configure Rule: ${getTrackerDisplayName(trackerId)}`
      : 'Add Custom Tracker Rule';
    idInput.value = trackerId || '';
    idInput.disabled = false;
    nameInput.value = '';
    sampleUrlEl.textContent = '';
    enabledChk.checked = true;
    cancelCompletedFirstScanChk.checked = false;
    cancelOnCompletedChk.checked = false;

    notifyStalledDownloadChk.checked = false;
    notifyStalledDaysInput.value = 0;
    notifyStalledHoursInput.value = 6;
    cancelStalledDownloadChk.checked = false;
    cancelStalledDaysInput.value = 1;
    cancelStalledHoursInput.value = 0;
    notifyLongQueuedChk.checked = false;
    notifyQueuedDaysInput.value = 0;
    notifyQueuedHoursInput.value = 6;

    useTimeChk.checked = true;
    hoursInput.value = 2;
    minsInput.value = 0;
    metricSelect.value = 'seeding_time';
    useRatioChk.checked = false;
    ratioInput.value = '1.00';
    keepTopSeededChk.checked = false;
    keepTopSeededCountInput.value = 3;
    topSeededMetricSelect.value = 'up_speed';
    keepLargestChk.checked = false;
    keepLargestCountInput.value = 3;
    keepAliveDaysInput.value = 3;
    keepAliveHoursInput.value = 0;
    const opRadios = document.getElementsByName('modal-operator');
    opRadios.forEach((r) => {
      r.checked = r.value === 'OR';
    });
  }

  updateModalConditionUI();
  modal.classList.remove('hidden');

  // Always reset scroll position of the modal settings container to the top
  const modalBody = document.getElementById('rule-modal-body');
  if (modalBody) {
    modalBody.scrollTop = 0;
  }
  modal.scrollTop = 0;
}

function closeRuleModal() {
  document.getElementById('rule-modal').classList.add('hidden');
}

function updateModalConditionUI() {
  const cancelOnComplete = document.getElementById('modal-cancel-on-completed').checked;
  const notifyStalledDownload = document.getElementById('modal-notify-stalled-download').checked;
  const cancelStalledDownload = document.getElementById('modal-cancel-stalled-download').checked;
  const notifyLongQueued = document.getElementById('modal-notify-long-queued').checked;
  const useTime = document.getElementById('modal-use-time').checked;
  const useRatio = document.getElementById('modal-use-ratio').checked;
  const keepTopSeeded = document.getElementById('modal-keep-top-seeded').checked;
  const keepLargest = document.getElementById('modal-keep-largest').checked;

  const notifyStalledFields = document.getElementById('modal-notify-stalled-fields');
  if (notifyStalledFields) notifyStalledFields.style.opacity = notifyStalledDownload ? '1' : '0.4';

  const cancelStalledFields = document.getElementById('modal-cancel-stalled-fields');
  if (cancelStalledFields) cancelStalledFields.style.opacity = cancelStalledDownload ? '1' : '0.4';

  const notifyQueuedFields = document.getElementById('modal-notify-queued-fields');
  if (notifyQueuedFields) notifyQueuedFields.style.opacity = notifyLongQueued ? '1' : '0.4';

  document.getElementById('modal-time-box').style.opacity = cancelOnComplete ? '0.4' : '1';
  document.getElementById('modal-ratio-box').style.opacity = cancelOnComplete ? '0.4' : '1';
  document.getElementById('modal-time-fields').style.opacity = useTime && !cancelOnComplete ? '1' : '0.4';
  document.getElementById('modal-ratio-fields').style.opacity = useRatio && !cancelOnComplete ? '1' : '0.4';
  document.getElementById('modal-operator-box').style.opacity =
    useTime && useRatio && !cancelOnComplete ? '1' : '0.45';

  document.getElementById('modal-top-seeded-fields').style.opacity = keepTopSeeded ? '1' : '0.4';
  document.getElementById('modal-largest-fields').style.opacity = keepLargest ? '1' : '0.4';
  document.getElementById('modal-keep-alive-duration-box').style.opacity =
    keepTopSeeded || keepLargest ? '1' : '0.45';
}

async function saveModalRule() {
  const trackerId = document.getElementById('modal-tracker-id').value.trim();
  if (!trackerId) {
    showToast('Please enter a tracker hostname or identifier.', 'error');
    return;
  }

  const enabled = document.getElementById('modal-enabled').checked;
  const cancelCompletedFirstScan = document.getElementById('modal-cancel-completed-first-scan').checked;
  const cancelOnCompleted = document.getElementById('modal-cancel-on-completed').checked;
  const notifyStalledDownload = document.getElementById('modal-notify-stalled-download').checked;
  const cancelStalledDownload = document.getElementById('modal-cancel-stalled-download').checked;
  const notifyLongQueued = document.getElementById('modal-notify-long-queued').checked;
  const useTime = document.getElementById('modal-use-time').checked;
  const useRatio = document.getElementById('modal-use-ratio').checked;
  const keepTopSeeded = document.getElementById('modal-keep-top-seeded').checked;
  const keepLargest = document.getElementById('modal-keep-largest').checked;

  if (
    enabled &&
    !cancelCompletedFirstScan &&
    !cancelOnCompleted &&
    !notifyStalledDownload &&
    !cancelStalledDownload &&
    !notifyLongQueued &&
    !useTime &&
    !useRatio &&
    !keepTopSeeded &&
    !keepLargest
  ) {
    showToast('Please enable at least one rule condition or detection option.', 'error');
    return;
  }

  const notifyStalledDays = Math.max(0, Number(document.getElementById('modal-notify-stalled-days').value) || 0);
  const notifyStalledHoursPart = Math.max(0, Number(document.getElementById('modal-notify-stalled-hours').value) || 0);
  const notifyStalledTotalHours = notifyStalledDays * 24 + notifyStalledHoursPart;
  if (notifyStalledDownload && notifyStalledTotalHours <= 0) {
    showToast('Please enter at least 1 hour or day for Notify & Highlight Stalled Downloads.', 'error');
    return;
  }

  const cancelStalledDays = Math.max(0, Number(document.getElementById('modal-cancel-stalled-days').value) || 0);
  const cancelStalledHoursPart = Math.max(0, Number(document.getElementById('modal-cancel-stalled-hours').value) || 0);
  const cancelStalledTotalHours = cancelStalledDays * 24 + cancelStalledHoursPart;
  if (cancelStalledDownload && cancelStalledTotalHours <= 0) {
    showToast('Please enter at least 1 hour or day for Cancel & Delete Stalled Downloads.', 'error');
    return;
  }

  const notifyQueuedDays = Math.max(0, Number(document.getElementById('modal-notify-queued-days').value) || 0);
  const notifyQueuedHoursPart = Math.max(0, Number(document.getElementById('modal-notify-queued-hours').value) || 0);
  const notifyQueuedTotalHours = notifyQueuedDays * 24 + notifyQueuedHoursPart;
  if (notifyLongQueued && notifyQueuedTotalHours <= 0) {
    showToast('Please enter at least 1 hour or day for Notify & Highlight Long-Queued Transfers.', 'error');
    return;
  }

  const hours = Math.max(0, Number(document.getElementById('modal-time-hours').value) || 0);
  const mins = Math.max(0, Number(document.getElementById('modal-time-minutes').value) || 0);
  const maxTimeMinutes = hours * 60 + mins;

  const keepDays = Math.max(0, Number(document.getElementById('modal-keep-alive-days').value) || 0);
  const keepHoursPart = Math.max(0, Number(document.getElementById('modal-keep-alive-hours').value) || 0);
  const keepAliveHours = Math.max(0.5, keepDays * 24 + keepHoursPart);

  let operator = 'OR';
  document.getElementsByName('modal-operator').forEach((r) => {
    if (r.checked) operator = r.value;
  });

  const displayNameVal = document.getElementById('modal-display-name').value.trim();
  const payload = {
    tracker_id: trackerId,
    display_name: displayNameVal,
    enabled,
    cancel_completed_on_first_scan: cancelCompletedFirstScan,
    cancel_on_completed: cancelOnCompleted,
    notify_stalled_download_enabled: notifyStalledDownload,
    notify_stalled_download_hours: Math.max(0.5, notifyStalledTotalHours || 6),
    cancel_stalled_download_enabled: cancelStalledDownload,
    cancel_stalled_download_hours: Math.max(0.5, cancelStalledTotalHours || 24),
    notify_long_queued_enabled: notifyLongQueued,
    notify_long_queued_hours: Math.max(0.5, notifyQueuedTotalHours || 6),
    use_time_rule: useTime,
    max_time_minutes: maxTimeMinutes,
    time_metric: document.getElementById('modal-time-metric').value,
    use_ratio_rule: useRatio,
    max_ratio: Math.max(0, Number(document.getElementById('modal-max-ratio').value) || 0),
    condition_operator: operator,
    keep_top_seeded_enabled: keepTopSeeded,
    keep_top_seeded_count: Math.max(1, Number(document.getElementById('modal-keep-top-seeded-count').value) || 3),
    top_seeded_metric: document.getElementById('modal-top-seeded-metric').value || 'up_speed',
    keep_largest_enabled: keepLargest,
    keep_largest_count: Math.max(1, Number(document.getElementById('modal-keep-largest-count').value) || 3),
    keep_alive_hours: keepAliveHours,
    include_downloading: false,
  };

  try {
    const resp = await fetch('/api/trackers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Failed to save rule');
    updateUI(data);
    closeRuleModal();
    showToast(`Saved rule for ${displayNameVal || getTrackerDisplayName(trackerId)}`, 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function quickToggleRule(trackerId) {
  let existing =
    trackerId === '__default__'
      ? appState.default_rule
      : (appState.trackers || []).find((t) => t.tracker_id === trackerId);
  if (!existing) return;

  const newEnabled = !existing.enabled;
  const payload = {
    tracker_id: existing.tracker_id,
    display_name: existing.display_name,
    enabled: newEnabled,
    cancel_completed_on_first_scan: Boolean(existing.cancel_completed_on_first_scan),
    cancel_on_completed: Boolean(existing.cancel_on_completed),
    notify_stalled_download_enabled: Boolean(existing.notify_stalled_download_enabled),
    notify_stalled_download_hours: existing.notify_stalled_download_hours || 6.0,
    cancel_stalled_download_enabled: Boolean(existing.cancel_stalled_download_enabled),
    cancel_stalled_download_hours: existing.cancel_stalled_download_hours || 24.0,
    notify_long_queued_enabled: Boolean(existing.notify_long_queued_enabled),
    notify_long_queued_hours: existing.notify_long_queued_hours || 6.0,
    use_time_rule:
      existing.use_time_rule ||
      (!existing.use_ratio_rule &&
        !existing.cancel_on_completed &&
        !existing.cancel_completed_on_first_scan &&
        !existing.notify_stalled_download_enabled &&
        !existing.cancel_stalled_download_enabled &&
        !existing.notify_long_queued_enabled &&
        newEnabled),
    max_time_minutes: existing.max_time_minutes,
    time_metric: existing.time_metric || 'seeding_time',
    use_ratio_rule: existing.use_ratio_rule,
    max_ratio: existing.max_ratio,
    condition_operator: existing.condition_operator || 'OR',
    keep_top_seeded_enabled: Boolean(existing.keep_top_seeded_enabled),
    keep_top_seeded_count: existing.keep_top_seeded_count || 3,
    top_seeded_metric: existing.top_seeded_metric || 'up_speed',
    keep_largest_enabled: Boolean(existing.keep_largest_enabled),
    keep_largest_count: existing.keep_largest_count || 3,
    keep_alive_hours: existing.keep_alive_hours || 72.0,
    include_downloading: Boolean(existing.include_downloading),
  };

  try {
    const resp = await fetch('/api/trackers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Failed to toggle rule');
    updateUI(data);
    showToast(`${getTrackerDisplayName(existing.tracker_id)}: Rule ${newEnabled ? 'enabled' : 'disabled'}`, 'info');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

let confirmModalResolver = null;

function showConfirmModal({
  title = 'Confirm Action',
  messageHtml = '',
  confirmText = 'Confirm',
  cancelText = 'Cancel',
  danger = true,
} = {}) {
  const modal = document.getElementById('confirm-modal');
  const titleEl = document.getElementById('confirm-modal-title');
  const msgEl = document.getElementById('confirm-modal-message');
  const okBtn = document.getElementById('confirm-modal-ok-btn');
  const cancelBtn = document.getElementById('confirm-modal-cancel-btn');

  if (!modal || !titleEl || !msgEl || !okBtn || !cancelBtn) {
    return Promise.resolve(false);
  }

  titleEl.textContent = title;
  msgEl.innerHTML = messageHtml;
  okBtn.textContent = confirmText;
  cancelBtn.textContent = cancelText;

  okBtn.className = danger
    ? 'px-4 py-2 rounded-lg bg-red-500 hover:bg-red-400 text-xs sm:text-sm font-semibold text-slate-950 cursor-pointer transition'
    : 'px-4 py-2 rounded-lg bg-amber-500 hover:bg-amber-400 text-xs sm:text-sm font-semibold text-slate-950 cursor-pointer transition';

  modal.classList.remove('hidden');

  return new Promise((resolve) => {
    confirmModalResolver = resolve;
  });
}

function resolveConfirmModal(confirmed) {
  const modal = document.getElementById('confirm-modal');
  if (modal) {
    modal.classList.add('hidden');
  }
  if (confirmModalResolver) {
    const fn = confirmModalResolver;
    confirmModalResolver = null;
    fn(Boolean(confirmed));
  }
}

async function deleteTrackerRule(trackerId) {
  const label = getTrackerDisplayName(trackerId);
  const confirmed = await showConfirmModal({
    title: 'Delete Tracker Rule',
    messageHtml: `<p>Remove tracker <strong class="text-white">${escapeHtml(label)}</strong> and its cancellation rule from your configuration?</p>`,
    confirmText: 'Delete Tracker Rule',
    cancelText: 'Cancel',
    danger: true,
  });
  if (!confirmed) return;
  try {
    const resp = await fetch(`/api/trackers/${encodeURIComponent(trackerId)}`, {
      method: 'DELETE',
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Delete failed');
    updateUI(data);
    showToast(`Deleted tracker ${label}`, 'info');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function manualCancelTransfer(transferId, transferName) {
  const hideNames = Boolean(appState.settings?.hide_transfer_names);
  const blurCls = hideNames ? 'blur-[6px] select-none inline-block' : '';
  const confirmed = await showConfirmModal({
    title: 'Cancel & Remove Transfer',
    messageHtml: `
      <p>Stop seeding and remove <strong class="text-white ${blurCls}">${escapeHtml(transferName)}</strong> from your Put.io transfer list?</p>
      <p class="text-xs text-slate-400">Downloaded files already stored in your Put.io account will be kept.</p>`,
    confirmText: 'Cancel & Remove',
    cancelText: 'Keep Transfer',
    danger: true,
  });
  if (!confirmed) {
    return;
  }
  try {
    const resp = await fetch(`/api/transfers/${transferId}/cancel`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Failed to cancel transfer');
    updateUI(data);
    showToast(
      hideNames ? 'Canceled and removed transfer' : `Canceled and removed "${transferName}"`,
      'success'
    );
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function saveToken() {
  const tokenVal = document.getElementById('input-putio-token').value.trim();
  if (!tokenVal) {
    showToast('Please paste a valid Put.io OAuth token.', 'error');
    return;
  }
  try {
    const resp = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ putio_token: tokenVal }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Failed to verify token');
    document.getElementById('input-putio-token').value = '';
    updateUI(data);
    showToast('Put.io token verified and PutWatch OAuth App configured!', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function saveServiceSettings() {
  const appIdRaw = document.getElementById('input-putio-app-id')?.value.trim();
  const pinChk = document.getElementById('chk-pin-transfers-header');
  const payload = {
    monitoring_enabled: document.getElementById('chk-monitoring-enabled').checked,
    auto_discover_trackers: document.getElementById('chk-auto-discover').checked,
    clean_completed_after_cancel: document.getElementById('chk-clean-after-cancel').checked,
    pin_transfers_header: pinChk ? pinChk.checked : true,
    dry_run: document.getElementById('chk-dry-run').checked,
    poll_interval_minutes: Math.max(1, Number(document.getElementById('input-poll-interval').value) || 5),
  };
  if (appIdRaw !== undefined && appIdRaw !== '') {
    payload.putio_app_id = Number(appIdRaw) || 0;
  }

  try {
    const resp = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Failed to update settings');
    updateUI(data);
    showToast('Background service settings saved!', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function toggleMonitoringQuick() {
  const nextVal = !Boolean(appState.settings?.monitoring_enabled);
  try {
    const resp = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ monitoring_enabled: nextVal }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Failed to toggle monitor');
    updateUI(data);
    showToast(`24/7 Background Monitor ${nextVal ? 'Resumed' : 'Paused'}`, 'info');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function startOobAuth() {
  if (oobPollTimer) {
    clearInterval(oobPollTimer);
    oobPollTimer = null;
  }
  const appIdInput = document.getElementById('input-putio-app-id')?.value.trim();
  const query = appIdInput && Number(appIdInput) > 0 ? `?app_id=${encodeURIComponent(appIdInput)}` : '';

  try {
    const resp = await fetch(`/api/auth/oob/start${query}`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Could not start OOB flow');

    const box = document.getElementById('oob-box');
    const codeDisplay = document.getElementById('oob-code-display');
    const linkEl = document.getElementById('oob-link');
    codeDisplay.textContent = data.code;
    linkEl.href = data.link_url || 'https://app.put.io/link';
    box.classList.remove('hidden');

    oobPollTimer = setInterval(async () => {
      try {
        const chk = await fetch(`/api/auth/oob/check/${encodeURIComponent(data.code)}`);
        if (!chk.ok) return;
        const chkData = await chk.json();
        if (chkData.linked) {
          clearInterval(oobPollTimer);
          oobPollTimer = null;
          box.classList.add('hidden');
          updateUI(chkData.state);
          showToast('Successfully linked Put.io account with PutWatch!', 'success');
        }
      } catch (_e) {
        // ignore transient polling errors
      }
    }, 3000);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function clearLogs() {
  const confirmed = await showConfirmModal({
    title: 'Clear Cancellation Log',
    messageHtml: '<p>Are you sure you want to permanently clear all cancellation log history?</p>',
    confirmText: 'Clear Log',
    cancelText: 'Cancel',
    danger: true,
  });
  if (!confirmed) return;
  try {
    const resp = await fetch('/api/logs', { method: 'DELETE' });
    const data = await resp.json();
    updateUI(data);
    showToast('Cleared cancellation logs.', 'info');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const confirmModal = document.getElementById('confirm-modal');
    if (confirmModal && !confirmModal.classList.contains('hidden')) {
      resolveConfirmModal(false);
      return;
    }
    const ruleModal = document.getElementById('rule-modal');
    if (ruleModal && !ruleModal.classList.contains('hidden')) {
      closeRuleModal();
    }
  }
});

window.addEventListener(
  'scroll',
  () => {
    updateTransfersStickyHeaderState();
  },
  { passive: true }
);

window.addEventListener(
  'resize',
  () => {
    updateTransfersStickyHeaderState();
  },
  { passive: true }
);

// Initial load & auto-refresh UI state every 10 seconds
document.addEventListener('DOMContentLoaded', () => {
  updateTransfersStickyHeaderState();
  fetchState();
  setInterval(fetchState, 10000);
});
