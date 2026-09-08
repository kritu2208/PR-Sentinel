/**
 * PR Sentinel - Modern AI Code Review Dashboard
 * Centralized API Client, State Management, and UI Controller
 */

// --- Centralized API Service Layer ---
class ApiClient {
  constructor(baseUrl = '') {
    this.baseUrl = baseUrl;
  }

  async request(endpoint, options = {}) {
    const config = {
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        ...options.headers,
      },
      ...options,
    };

    try {
      const response = await fetch(`${this.baseUrl}${endpoint}`, config);
      if (!response.ok) {
        let errorMsg = `HTTP ${response.status}: ${response.statusText}`;
        try {
          const errData = await response.json();
          if (errData.detail) errorMsg = typeof errData.detail === 'string' ? errData.detail : JSON.stringify(errData.detail);
        } catch (_) {}
        throw new Error(errorMsg);
      }
      return await response.json();
    } catch (err) {
      console.error(`[API Error] ${endpoint}:`, err);
      throw err;
    }
  }

  // GET /health/ready - Deep readiness check
  async getHealth() {
    return this.request('/health/ready');
  }

  // GET /jobs/stats - Operational metrics
  async getStats() {
    return this.request('/jobs/stats');
  }

  // GET /jobs - List recent reviews
  async getJobs(limit = 50) {
    return this.request(`/jobs?limit=${limit}`);
  }

  // GET /jobs/{job_id} or /reviews/{job_id} - Full review details with findings
  async getJobDetails(jobId) {
    return this.request(`/jobs/${jobId}`);
  }

  // POST /jobs/{job_id}/retry - Trigger review retry
  async retryJob(jobId) {
    return this.request(`/jobs/${jobId}/retry`, { method: 'POST' });
  }
}

const api = new ApiClient();

// --- Application State ---
const state = {
  health: { status: 'unknown', database: 'unknown', worker_alive: false },
  stats: { total_jobs: 0, completed: 0, in_progress: 0, queued: 0, failed: 0, worker_active: false },
  jobs: [],
  filteredJobs: [],
  severityTotals: { critical: 0, high: 0, medium: 0, low: 0, total: 0 },
  activeFilter: 'all', // 'all' | 'completed' | 'in_progress' | 'queued' | 'failed'
  activeVerdict: 'all',
  searchQuery: '',
  selectedJob: null,
  autoRefreshInterval: 5000,
  refreshTimer: null,
  isLoading: false,
  theme: localStorage.getItem('pr_sentinel_theme') || 'dark',
};

// --- DOM References ---
const DOM = {
  themeToggleBtn: document.getElementById('themeToggleBtn'),
  refreshBtn: document.getElementById('refreshBtn'),
  autoRefreshSelect: document.getElementById('autoRefreshSelect'),
  healthIndicator: document.getElementById('healthIndicator'),
  healthStatusText: document.getElementById('healthStatusText'),

  // Metric Elements
  statTotal: document.getElementById('statTotal'),
  statCompleted: document.getElementById('statCompleted'),
  statActive: document.getElementById('statActive'),
  statFailed: document.getElementById('statFailed'),
  statSuccessRate: document.getElementById('statSuccessRate'),
  statProgressBar: document.getElementById('statProgressBar'),

  // Severity Pills
  sevCritCount: document.getElementById('sevCritCount'),
  sevHighCount: document.getElementById('sevHighCount'),
  sevMedCount: document.getElementById('sevMedCount'),
  sevLowCount: document.getElementById('sevLowCount'),
  sevTotalFindings: document.getElementById('sevTotalFindings'),

  // Filter & Search Controls
  filterTabs: document.querySelectorAll('.tab-btn'),
  verdictSelect: document.getElementById('verdictSelect'),
  searchInput: document.getElementById('searchInput'),

  // Review List Table
  reviewsTableBody: document.getElementById('reviewsTableBody'),
  tableStateContainer: document.getElementById('tableStateContainer'),

  // Details Modal
  reviewModal: document.getElementById('reviewModal'),
  modalCloseBtn: document.getElementById('modalCloseBtn'),
  modalTitle: document.getElementById('modalTitle'),
  modalPrNumber: document.getElementById('modalPrNumber'),
  modalRepoName: document.getElementById('modalRepoName'),
  modalCommitSha: document.getElementById('modalCommitSha'),
  modalReviewKey: document.getElementById('modalReviewKey'),
  modalStatusBadge: document.getElementById('modalStatusBadge'),
  modalVerdictBadge: document.getElementById('modalVerdictBadge'),
  modalDuration: document.getElementById('modalDuration'),
  modalWorkerId: document.getElementById('modalWorkerId'),
  modalRetryBtn: document.getElementById('modalRetryBtn'),
  modalSummaryBox: document.getElementById('modalSummaryBox'),
  modalSummaryContent: document.getElementById('modalSummaryContent'),
  modalFindingsContainer: document.getElementById('modalFindingsContainer'),
  modalFindingsCount: document.getElementById('modalFindingsCount'),
  modalErrorBox: document.getElementById('modalErrorBox'),
  modalErrorMessage: document.getElementById('modalErrorMessage'),
  toastContainer: document.getElementById('toastContainer'),
};

// --- Utility Functions ---

function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span>${type === 'error' ? '⚠️' : type === 'success' ? '✅' : 'ℹ️'}</span>
    <span>${escapeHtml(message)}</span>
  `;
  DOM.toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function formatRelativeTime(timestamp) {
  if (!timestamp) return '—';
  const date = new Date(timestamp);
  if (isNaN(date.getTime())) return '—';

  const now = new Date();
  const diffSec = Math.floor((now - date) / 1000);

  if (diffSec < 5) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.floor(diffHour / 24);
  return `${diffDay}d ago`;
}

function formatExactDate(timestamp) {
  if (!timestamp) return '';
  const d = new Date(timestamp);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function renderSimpleMarkdown(md) {
  if (!md) return '<p class="text-muted">No summary provided.</p>';
  let html = escapeHtml(md);

  // Headers (## and ###)
  html = html.replace(/^### (.*$)/gim, '<h4 style="font-size:0.95rem; font-weight:700; margin:0.75rem 0 0.35rem; color:var(--text-primary);">$1</h4>');
  html = html.replace(/^## (.*$)/gim, '<h3 style="font-size:1.05rem; font-weight:700; margin:1rem 0 0.5rem; color:var(--text-primary);">$1</h3>');
  html = html.replace(/^# (.*$)/gim, '<h2 style="font-size:1.15rem; font-weight:700; margin:1rem 0 0.5rem; color:var(--text-primary);">$1</h2>');

  // Bold & Italic
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');

  // Inline Code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Bullet Lists
  html = html.replace(/^\* (.*$)/gim, '<li>$1</li>');
  html = html.replace(/^- (.*$)/gim, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>)/gims, '<ul style="padding-left:1.2rem; margin:0.5rem 0;">$1</ul>');

  // Paragraphs
  html = html.replace(/\n\n+/g, '</p><p style="margin-bottom:0.6rem;">');
  return `<div class="markdown-body"><p style="margin-bottom:0.6rem;">${html}</p></div>`;
}

// Compute severity counts from findings array
function computeJobSeverities(job) {
  const counts = { critical: 0, high: 0, medium: 0, low: 0, total: 0 };
  if (job.findings && Array.isArray(job.findings)) {
    job.findings.forEach(f => {
      const sev = (f.severity || '').toLowerCase();
      if (sev === 'critical') counts.critical++;
      else if (sev === 'high') counts.high++;
      else if (sev === 'medium') counts.medium++;
      else if (sev === 'low') counts.low++;
      counts.total++;
    });
  } else if (job.findings_count) {
    counts.total = job.findings_count;
  }
  return counts;
}

// --- Data Fetching and State Updates ---

async function fetchDashboardData() {
  try {
    // 1. Fetch Health
    try {
      state.health = await api.getHealth();
    } catch (_) {
      state.health = { status: 'unhealthy', database: 'disconnected', worker_alive: false };
    }

    // 2. Fetch Operational Stats
    try {
      state.stats = await api.getStats();
    } catch (_) {
      // Fallback
    }

    // 3. Fetch Recent Jobs
    try {
      const jobsList = await api.getJobs(50);
      state.jobs = jobsList;
    } catch (err) {
      console.warn('Failed to fetch jobs list:', err);
    }

    // Recompute finding severities totals across all loaded jobs
    let totalCrit = 0, totalHigh = 0, totalMed = 0, totalLow = 0, grandTotal = 0;
    state.jobs.forEach(job => {
      const s = computeJobSeverities(job);
      totalCrit += s.critical;
      totalHigh += s.high;
      totalMed += s.medium;
      totalLow += s.low;
      grandTotal += s.total;
    });
    state.severityTotals = { critical: totalCrit, high: totalHigh, medium: totalMed, low: totalLow, total: grandTotal };

    applyFilters();
    renderHeader();
    renderStats();
    renderSeveritySummary();
    renderTable();

  } catch (error) {
    console.error('Error refreshing dashboard:', error);
    showToast('Failed to sync review dashboard with backend.', 'error');
  }
}

// Filter and search logic
function applyFilters() {
  const query = state.searchQuery.trim().toLowerCase();

  state.filteredJobs = state.jobs.filter(job => {
    // Status Filter
    if (state.activeFilter !== 'all') {
      if (job.status !== state.activeFilter) return false;
    }

    // Verdict Filter
    if (state.activeVerdict !== 'all') {
      const v = (job.final_verdict || '').toLowerCase();
      if (v !== state.activeVerdict) return false;
    }

    // Search query filter (Repo, PR #, SHA, Review Key, Summary)
    if (query) {
      const repo = (job.repo_full_name || '').toLowerCase();
      const pr = String(job.pr_number || '');
      const sha = (job.head_sha || '').toLowerCase();
      const key = (job.review_key || '').toLowerCase();
      const verdict = (job.final_verdict || '').toLowerCase();

      const match = repo.includes(query) ||
                    pr.includes(query) ||
                    sha.includes(query) ||
                    key.includes(query) ||
                    verdict.includes(query);
      if (!match) return false;
    }

    return true;
  });
}

// --- UI Renderers ---

function renderHeader() {
  const isReady = state.health.status === 'ready' && state.health.database === 'connected';
  const isWorkerLive = state.health.worker_alive || state.stats.worker_active;

  DOM.healthIndicator.className = `health-dot ${isReady ? 'ready' : 'unhealthy'}`;
  DOM.healthStatusText.textContent = isReady
    ? `System Ready · Worker Active`
    : `System Status: Degraded (${state.health.status})`;
}

function renderStats() {
  const total = state.stats.total_jobs || state.jobs.length || 0;
  const completed = state.stats.completed || state.jobs.filter(j => j.status === 'completed').length || 0;
  const inProg = state.stats.in_progress || state.jobs.filter(j => j.status === 'in_progress').length || 0;
  const queued = state.stats.queued || state.jobs.filter(j => j.status === 'queued').length || 0;
  const failed = state.stats.failed || state.jobs.filter(j => j.status === 'failed').length || 0;

  DOM.statTotal.textContent = total;
  DOM.statCompleted.textContent = completed;
  DOM.statActive.textContent = inProg + queued;
  DOM.statFailed.textContent = failed;

  const successRate = total > 0 ? Math.round((completed / total) * 100) : 0;
  DOM.statSuccessRate.textContent = `${successRate}%`;
  DOM.statProgressBar.style.width = `${successRate}%`;
}

function renderSeveritySummary() {
  DOM.sevCritCount.textContent = state.severityTotals.critical;
  DOM.sevHighCount.textContent = state.severityTotals.high;
  DOM.sevMedCount.textContent = state.severityTotals.medium;
  DOM.sevLowCount.textContent = state.severityTotals.low;
  DOM.sevTotalFindings.textContent = `${state.severityTotals.total} findings identified across active reviews`;
}

function renderTable() {
  if (state.filteredJobs.length === 0) {
    DOM.reviewsTableBody.innerHTML = '';
    DOM.tableStateContainer.style.display = 'block';
    DOM.tableStateContainer.innerHTML = `
      <div class="state-box">
        <div class="state-icon">🔍</div>
        <div class="state-title">No Reviews Found</div>
        <div class="state-desc">${state.jobs.length === 0 ? 'No PR review jobs have been received yet. Once GitHub sends a pull_request webhook, jobs will appear here in real-time.' : 'No reviews match your current filter or search criteria.'}</div>
      </div>
    `;
    return;
  }

  DOM.tableStateContainer.style.display = 'none';

  DOM.reviewsTableBody.innerHTML = state.filteredJobs.map(job => {
    const severities = computeJobSeverities(job);
    const durationText = job.duration_seconds ? `${job.duration_seconds}s` : '—';
    const verdict = (job.final_verdict || '').toLowerCase();

    let verdictHtml = '<span class="verdict-badge verdict-pending">Pending</span>';
    if (verdict === 'approve') {
      verdictHtml = '<span class="verdict-badge verdict-approve">✓ Approved</span>';
    } else if (verdict === 'request_changes') {
      verdictHtml = '<span class="verdict-badge verdict-request_changes">✕ Changes Requested</span>';
    } else if (verdict === 'comment') {
      verdictHtml = '<span class="verdict-badge verdict-comment">💬 Comment</span>';
    }

    const isFailed = job.status === 'failed';

    return `
      <tr onclick="openReviewDetails(${job.job_id})">
        <td>
          <div class="repo-info-cell">
            <div class="repo-name">
              <span>${escapeHtml(job.repo_full_name)}</span>
              <span class="pr-tag">#${job.pr_number}</span>
            </div>
            <div class="commit-chip">
              <span>commit:</span>
              <code style="color:var(--text-secondary);">${escapeHtml((job.head_sha || '').slice(0, 7))}</code>
            </div>
          </div>
        </td>
        <td>
          <span class="badge badge-${escapeHtml(job.status)}">${escapeHtml(job.status.replace('_', ' '))}</span>
        </td>
        <td>${verdictHtml}</td>
        <td>
          <div class="sev-counter-group" title="Critical: ${severities.critical}, High: ${severities.high}, Medium: ${severities.medium}, Low: ${severities.low}">
            <span class="mini-sev-badge crit ${severities.critical === 0 ? 'zero' : ''}">${severities.critical}</span>
            <span class="mini-sev-badge high ${severities.high === 0 ? 'zero' : ''}">${severities.high}</span>
            <span class="mini-sev-badge med ${severities.medium === 0 ? 'zero' : ''}">${severities.medium}</span>
            <span class="mini-sev-badge low ${severities.low === 0 ? 'zero' : ''}">${severities.low}</span>
          </div>
        </td>
        <td style="font-family:var(--font-mono); font-size:0.8125rem; color:var(--text-secondary);">
          ${durationText}
        </td>
        <td>
          <div class="timestamp-cell">
            <span class="time-relative">${formatRelativeTime(job.created_at)}</span>
            <span class="time-exact">${formatExactDate(job.created_at)}</span>
          </div>
        </td>
        <td style="text-align:right;" onclick="event.stopPropagation();">
          <div style="display:inline-flex; gap:0.4rem;">
            <button class="btn btn-sm" onclick="openReviewDetails(${job.job_id})">
              Details
            </button>
            ${isFailed ? `
              <button class="btn btn-sm btn-primary" onclick="handleRetry(${job.job_id})">
                ↻ Retry
              </button>
            ` : ''}
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

// --- Modal Details View ---

async function openReviewDetails(jobId) {
  try {
    DOM.modalFindingsContainer.innerHTML = '<div class="skeleton skeleton-row" style="height:100px;"></div>';
    DOM.reviewModal.classList.add('active');
    document.body.style.overflow = 'hidden';

    // Fetch freshest detail from API
    const job = await api.getJobDetails(jobId);
    state.selectedJob = job;

    // Header & Meta
    DOM.modalTitle.textContent = `${job.repo_full_name} #${job.pr_number}`;
    DOM.modalRepoName.textContent = job.repo_full_name;
    DOM.modalPrNumber.textContent = `#${job.pr_number}`;
    DOM.modalCommitSha.textContent = (job.head_sha || '').slice(0, 8);
    DOM.modalReviewKey.textContent = job.review_key || '—';
    DOM.modalWorkerId.textContent = job.worker_id ? `Worker: ${job.worker_id}` : 'Worker: unassigned';
    DOM.modalDuration.textContent = job.duration_seconds ? `Duration: ${job.duration_seconds}s` : 'Duration: —';

    // Status Badge
    DOM.modalStatusBadge.className = `badge badge-${job.status}`;
    DOM.modalStatusBadge.textContent = job.status.replace('_', ' ');

    // Verdict Badge
    const verdict = (job.final_verdict || '').toLowerCase();
    if (verdict === 'approve') {
      DOM.modalVerdictBadge.className = 'verdict-badge verdict-approve';
      DOM.modalVerdictBadge.textContent = '✓ Approved';
      DOM.modalVerdictBadge.style.display = 'inline-flex';
    } else if (verdict === 'request_changes') {
      DOM.modalVerdictBadge.className = 'verdict-badge verdict-request_changes';
      DOM.modalVerdictBadge.textContent = '✕ Changes Requested';
      DOM.modalVerdictBadge.style.display = 'inline-flex';
    } else if (verdict === 'comment') {
      DOM.modalVerdictBadge.className = 'verdict-badge verdict-comment';
      DOM.modalVerdictBadge.textContent = '💬 Comment';
      DOM.modalVerdictBadge.style.display = 'inline-flex';
    } else {
      DOM.modalVerdictBadge.style.display = 'none';
    }

    // Retry Button Visibility
    if (job.status === 'failed') {
      DOM.modalRetryBtn.style.display = 'inline-flex';
      DOM.modalRetryBtn.onclick = () => handleRetry(job.job_id);
    } else {
      DOM.modalRetryBtn.style.display = 'none';
    }

    // Failure / Error Box
    if (job.error_message) {
      DOM.modalErrorBox.style.display = 'block';
      DOM.modalErrorMessage.textContent = job.error_message;
    } else {
      DOM.modalErrorBox.style.display = 'none';
    }

    // Executive Summary
    if (job.summary) {
      DOM.modalSummaryBox.style.display = 'block';
      DOM.modalSummaryContent.innerHTML = renderSimpleMarkdown(job.summary);
    } else {
      DOM.modalSummaryBox.style.display = 'none';
    }

    // Findings Breakdown
    const findings = job.findings || [];
    DOM.modalFindingsCount.textContent = `(${findings.length})`;

    if (findings.length === 0) {
      DOM.modalFindingsContainer.innerHTML = `
        <div style="padding:2rem; text-align:center; color:var(--text-muted); background:var(--bg-secondary); border-radius:var(--radius-md); border:1px solid var(--border-primary);">
          ✨ No defect findings or security vulnerabilities identified for this review.
        </div>
      `;
    } else {
      // Group findings by severity (Critical -> High -> Medium -> Low)
      const severityOrder = ['critical', 'high', 'medium', 'low', 'info'];
      const sortedFindings = [...findings].sort((a, b) => {
        const orderA = severityOrder.indexOf((a.severity || '').toLowerCase());
        const orderB = severityOrder.indexOf((b.severity || '').toLowerCase());
        return (orderA === -1 ? 99 : orderA) - (orderB === -1 ? 99 : orderB);
      });

      DOM.modalFindingsContainer.innerHTML = sortedFindings.map((f, idx) => {
        const sev = (f.severity || 'low').toLowerCase();
        const confidencePct = f.confidence ? `${Math.round(f.confidence * 100)}%` : '—';
        const fileLocation = f.file ? `${f.file}${f.line ? `:${f.line}` : ''}` : 'General';
        const investigationStatus = f.investigation_status ? f.investigation_status.replace(/_/g, ' ') : 'Verified';

        return `
          <div class="finding-card severity-${escapeHtml(sev)}">
            <div class="finding-card-header">
              <div class="finding-badges">
                <span class="sev-pill ${escapeHtml(sev)}">
                  ${escapeHtml(sev.toUpperCase())}
                </span>
                ${f.category ? `<span class="category-tag">${escapeHtml(f.category)}</span>` : ''}
                <span class="location-chip">📍 ${escapeHtml(fileLocation)}</span>
              </div>
              <div style="display:flex; align-items:center; gap:0.5rem;">
                <span class="investigation-tag">🔍 ${escapeHtml(investigationStatus)}</span>
                <span style="font-size:0.75rem; color:var(--text-muted); font-family:var(--font-mono);">
                  Confidence: <strong style="color:var(--text-primary);">${confidencePct}</strong>
                </span>
              </div>
            </div>

            <!-- Finding Main Description / Comment -->
            <div class="finding-comment">
              ${renderSimpleMarkdown(f.comment || f.description || 'No description provided.')}
            </div>

            <!-- Autonomous Root Cause & Investigation Details -->
            ${(f.root_cause || f.evidence || f.impact || f.recommendation) ? `
              <div class="investigation-grid">
                ${f.root_cause ? `
                  <div class="investigation-block">
                    <span class="investigation-label">🧠 Root Cause Analysis</span>
                    <div class="investigation-content">${escapeHtml(f.root_cause)}</div>
                  </div>
                ` : ''}

                ${f.evidence ? `
                  <div class="investigation-block">
                    <span class="investigation-label">🔎 Supporting Evidence</span>
                    <pre class="code-snippet">${escapeHtml(f.evidence)}</pre>
                  </div>
                ` : ''}

                ${f.impact ? `
                  <div class="investigation-block">
                    <span class="investigation-label">💥 Potential Impact</span>
                    <div class="investigation-content" style="color:var(--sev-high); font-weight:500;">
                      ${escapeHtml(f.impact)}
                    </div>
                  </div>
                ` : ''}

                ${f.recommendation ? `
                  <div class="investigation-block">
                    <span class="investigation-label">💡 Recommended Fix</span>
                    <div class="investigation-content" style="color:var(--status-success); font-weight:500;">
                      ${escapeHtml(f.recommendation)}
                    </div>
                  </div>
                ` : ''}
              </div>
            ` : ''}
          </div>
        `;
      }).join('');
    }

  } catch (err) {
    console.error('Failed to open job details:', err);
    showToast(`Failed to load review #${jobId}: ${err.message}`, 'error');
  }
}

function closeReviewModal() {
  DOM.reviewModal.classList.remove('active');
  document.body.style.overflow = '';
  state.selectedJob = null;
}

// --- Action Handlers ---

async function handleRetry(jobId) {
  try {
    showToast(`Triggering retry for review job #${jobId}...`, 'info');
    const result = await api.retryJob(jobId);
    showToast(`Success: Job #${result.job_id} scheduled for retry (Attempt ${result.attempt}).`, 'success');
    closeReviewModal();
    await fetchDashboardData();
  } catch (err) {
    showToast(`Retry failed: ${err.message}`, 'error');
  }
}

function handleThemeToggle() {
  const newTheme = state.theme === 'dark' ? 'light' : 'dark';
  state.theme = newTheme;
  document.documentElement.setAttribute('data-theme', newTheme);
  localStorage.setItem('pr_sentinel_theme', newTheme);
  DOM.themeToggleBtn.textContent = newTheme === 'dark' ? '🌙' : '☀️';
}

function setupEventListeners() {
  // Theme Toggle
  DOM.themeToggleBtn.addEventListener('click', handleThemeToggle);

  // Manual Refresh
  DOM.refreshBtn.addEventListener('click', async () => {
    DOM.refreshBtn.classList.add('loading');
    DOM.refreshBtn.disabled = true;
    await fetchDashboardData();
    DOM.refreshBtn.disabled = false;
    DOM.refreshBtn.classList.remove('loading');
    showToast('Dashboard data refreshed.', 'info');
  });

  // Auto Refresh Interval
  DOM.autoRefreshSelect.addEventListener('change', (e) => {
    const val = parseInt(e.target.value, 10);
    clearInterval(state.refreshTimer);
    if (val > 0) {
      state.refreshTimer = setInterval(fetchDashboardData, val);
      showToast(`Auto-refresh set to every ${val / 1000}s.`, 'info');
    } else {
      showToast('Auto-refresh paused.', 'info');
    }
  });

  // Status Filter Tabs
  DOM.filterTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      DOM.filterTabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      state.activeFilter = tab.dataset.filter;
      applyFilters();
      renderTable();
    });
  });

  // Verdict Filter
  DOM.verdictSelect.addEventListener('change', (e) => {
    state.activeVerdict = e.target.value;
    applyFilters();
    renderTable();
  });

  // Search Input
  DOM.searchInput.addEventListener('input', (e) => {
    state.searchQuery = e.target.value;
    applyFilters();
    renderTable();
  });

  // Modal Close Events
  DOM.modalCloseBtn.addEventListener('click', closeReviewModal);
  DOM.reviewModal.addEventListener('click', (e) => {
    if (e.target === DOM.reviewModal) closeReviewModal();
  });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && DOM.reviewModal.classList.contains('active')) {
      closeReviewModal();
    }
  });
}

// --- Initialization ---

async function initApp() {
  // Apply saved theme
  document.documentElement.setAttribute('data-theme', state.theme);
  DOM.themeToggleBtn.textContent = state.theme === 'dark' ? '🌙' : '☀️';

  setupEventListeners();

  // Initial load
  await fetchDashboardData();

  // Set default auto-refresh interval (5s)
  state.refreshTimer = setInterval(fetchDashboardData, state.autoRefreshInterval);
}

document.addEventListener('DOMContentLoaded', initApp);
