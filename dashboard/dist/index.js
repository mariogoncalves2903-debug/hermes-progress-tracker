// Progress Tracker Dashboard Entry Point
// This file is loaded by the dashboard SPA when the progress-tracker tab is opened.

console.log('[progress-tracker] Loading dashboard entry point');

async function loadTracks() {
  try {
    const response = await fetch('/api/plugins/progress-tracker/snapshot?all=1', {
      credentials: 'include'
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderTracks(data.tracks || []);
  } catch (err) {
    console.error('[progress-tracker] Failed to load tracks:', err);
    document.getElementById('loading').style.display = 'none';
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = `Failed to load tracks: ${err.message}`;
  }
}

function formatDuration(ms) {
  if (!ms || !isFinite(ms)) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return `${m}m ${r.toString().padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${(m % 60).toString().padStart(2, '0')}m`;
}

function getTaskState(task, track) {
  if (task.status === 'completed') return task.isSkipped ? 'skipped' : 'done';
  if (task.status === 'in_progress') {
    if (track?.failedCommand) return 'failed';
    return 'active';
  }
  return 'pending';
}

function renderTracks(tracks) {
  document.getElementById('loading').style.display = 'none';
  const container = document.getElementById('tracks');
  if (tracks.length === 0) {
    document.getElementById('empty').style.display = 'block';
    return;
  }
  container.style.display = 'grid';
  container.innerHTML = tracks.map(track => {
    const tasksHtml = (track.tasks || []).map(task => {
      const state = getTaskState(task, track);
      const started = task.startedAt ? new Date(task.startedAt).toLocaleTimeString() : '—';
      const done = task.doneAt ? new Date(task.doneAt).toLocaleTimeString() : '—';
      return `
        <div class="task">
          <div class="task-state ${state}"></div>
          <div class="task-label">${task.label}</div>
          <div class="task-duration">${started} → ${done}</div>
        </div>
      `;
    }).join('');

    return `
      <div class="track">
        <div class="track-header">
          <div class="track-title">${track.title || track.label || 'Untitled'}</div>
          <span class="track-status ${track.status || 'running'}">${track.status || 'running'}</span>
        </div>
        <div class="track-meta">
          <span>Session: ${track.sessionId?.slice(0, 20)}...</span>
          <span>Elapsed: ${formatDuration(track.elapsedMs)}</span>
          <span>Tasks: ${track.tasks?.filter(t => t.status === 'completed').length || 0}/${track.tasks?.length || 0}</span>
          <span>Tokens: ${track.tokens?.input || 0} in / ${track.tokens?.output || 0} out</span>
        </div>
        <div class="tasks">${tasksHtml}</div>
      </div>
    `;
  }).join('');
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    loadTracks();
    setInterval(loadTracks, 2000);
  });
} else {
  loadTracks();
  setInterval(loadTracks, 2000);
}