/**
 * Progress Tracker — desktop pane + palette command
 * Matches SPEC rendering (pane, desktop bar, timeline gutter, finding pills, task rows)
 * Uses ctx.rest polling @ 1000ms; backend contract fixed.
 */
// ─── Progress Tracker — exact port of mjoyshuvo/claude-progress-tracker ───
// https://github.com/mjoyshuvo/claude-progress-tracker
// This plugin draws a live progress bar above the prompt and a Progress pane.

import { useState, useEffect, useRef } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'
import { host, useMutation, useValue, COMPOSER_AREAS, PALETTE_AREA } from '@hermes/plugin-sdk'

// ─── Constants matching upstream exactly ───

// Desktop bar SVG constants (from hooks/bar-svg.mjs)
const SVG_HEIGHT = 24
const DOT = 3
const PITCH = 4
const DOT_ROWS = 4
const MAX_COLUMNS = 150
const PILL_FONT = 11
const PILL_CHAR = 6.7

// Exact upstream colour palette (dark theme base)
const PALETTE = {
  working: { dot: '#b9adff', tick: '#e6e1ff', pill: '#a99cf5', ink: '#1d1a2e' },
  failed:  { dot: '#ff9a8a', tick: '#ffe1db', pill: '#ef6f5c', ink: '#2a0a05' },
  waiting: { dot: '#ffd27a', tick: '#fff0d1', pill: '#f0b34a', ink: '#2a1c05' },
  done:    { dot: '#9fe0ad', tick: '#e3f7e7', pill: '#7cc68a', ink: '#14261a' },
}

// Light theme palette (exact upstream)
const PALETTE_LIGHT = {
  working: { dot: '#6b5bd6', tick: '#3f3196', pill: '#7a6ae0', ink: '#ffffff' },
  failed:  { dot: '#d0503c', tick: '#8f2617', pill: '#d9553f', ink: '#ffffff' },
  waiting: { dot: '#b67c0f', tick: '#6e4906', pill: '#e3a234', ink: '#2a1c05' },
  done:    { dot: '#3d9752', tick: '#1f5c33', pill: '#45a35b', ink: '#ffffff' },
}

// Timeline SVG constants (from hooks/timeline-svg.mjs)
const TL_ROW_HEIGHT = 22
const TL_GUTTER_WIDTH = 30
const TIMELINE_WIDTH = 44
const TL_MAIN_X = 7
const TL_BRANCH_X = 21
const TL_LINE = '#3b5e47'
const TL_PENDING = '#5d5c68'
const TL_ACTIVE = '#a99cf5'
const TL_FAILED = '#ef6f5c'
const TL_DONE = '#7cc68a'
const TL_AGENT = '#a99cf5'
const TL_LINE_LIGHT = '#8fbf9c'
const TL_PENDING_LIGHT = '#a3a1ad'
const TL_STAGGER = 0.07
const TL_DRAW = 0.18

// Desktop bar height (band row)
const DESKTOP_BAR_HEIGHT = 24
const DONE_LINGER_MS = 60000
const POLL_MS = 1000
const BLINK_MS = 800

// Dark-mode surface colours
const DARK_SURFACE = '#1e1e22'
const DARK_SURFACE_2 = '#232328'
const DARK_STROKE = '#2e2e34'
const DARK_INK = '#e6e6e9'
const REMAINDER_COLOR = '#2a2a2f'

// State colours for task rows (kept from original for row glyphs)
const STATE_COLOR = {
  done: '#7cc68a',
  failed: '#ef6f5c',
  waiting: '#f0b34a',
  working: '#a99cf5',
  pending: '#6b7280',
}

// Finding colours
const FINDING_COLORS = {
  found:  { bg: '#d1fae5', fg: '#065f46', dot: '#7cc68a' },
  failed: { bg: '#fee2e2', fg: '#991b1b', dot: '#ef6f5c' },
}

// Block colours
const BLOCK_COLORS = {
  permission: { bg: '#fef3c7', fg: '#92400e', border: '#f59e0b' },
  question:   { bg: '#dbeafe', fg: '#1e40af', border: '#3b82f6' },
  approval:   { bg: '#e9d5ff', fg: '#6b21a8', border: '#a855f7' },
}

// Attempt pill colours
const ATTEMPT_COLORS = {
  unfinished: { bg: '#dbeafe', fg: '#1e40af' },
  failure:    { bg: '#fee2e2', fg: '#991b1b' },
}

// ─── Helpers ───

function getTrackProgress(track) {
  const tasks = track.tasks || []
  const done = tasks.filter(t => t.status === 'completed').length
  return { done, total: tasks.length }
}

function getTaskState(task, track) {
  if (task.status === 'completed') return 'done'
  if (task.status === 'in_progress') return 'active'
  if (task.isSkipped) return 'skipped'
  if (track.failedCommand && track.activeTaskId === task.id) return 'failed'
  return 'pending'
}

function formatDuration(ms) {
  if (ms < 1000) return `${ms}ms`
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}

function durationOf(ms) {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}

function countOf(n) {
  if (n < 1000) return String(n)
  if (n < 100000) return `${(n / 1000).toFixed(1)}k`
  if (n < 1000000) return `${Math.round(n / 1000)}k`
  return `${(n / 1000000).toFixed(2)}M`
}

function shortSession(sid) {
  if (!sid) return 'legacy'
  return sid.length > 12 ? `…${sid.slice(-8)}` : sid
}

function escapeXml(text) {
  return String(text).replace(/&/g, '&').replace(/</g, '<').replace(/>/g, '>')
}

function hash(n) {
  const s = Math.sin(n * 12.9898 + 78.233) * 43758.5453
  return s - Math.floor(s)
}

// ─── Main Pane Component ───
function ProgressPane({ ctx }) {
  const focusedSession = useValue(host.state.focusedStoredSessionId)
  const activeSession = useValue(host.state.activeSessionId)
  const session = focusedSession || activeSession || ''
  const profile = useValue(host.state.focusedSessionProfile) || ''

  const [snapshot, setSnapshot] = useState(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState('progress') // 'progress' | 'log'
  const [openEntryId, setOpenEntryId] = useState(null)
  const [blinkTick, setBlinkTick] = useState(0)
  const [titleText, setSessionTitle] = useState('')
  const [sessionTitles, setSessionTitles] = useState({})
  const genRef = useRef(0)
  const refreshRef = useRef(null)

  // Polling
  useEffect(() => {
    const currentGen = ++genRef.current
    let alive = true

    async function fetchSnapshot() {
      try {
        const res = await ctx.rest(
          `/snapshot?session=${encodeURIComponent(session)}&all=1`,
          { timeoutMs: 5000 }
        )
        if (alive && currentGen === genRef.current) {
          setSnapshot(res)
          setError('')
        }
      } catch (e) {
        if (alive && currentGen === genRef.current) {
          setError('Backend unreachable. Enable progress-tracker in Agent plugins and restart Hermes.')
        }
      }
    }

    refreshRef.current = fetchSnapshot
    fetchSnapshot()
    const id = setInterval(fetchSnapshot, POLL_MS)
    return () => { alive = false; clearInterval(id) }
  }, [ctx, session])

  // Blink timer for active tasks
  useEffect(() => {
    const id = setInterval(() => setBlinkTick(t => t + 1), BLINK_MS)
    return () => clearInterval(id)
  }, [])

  // The real conversation title. host.state carries session ids but no title, so read the
  // persisted session list. One fetch feeds both the header and the per-session section
  // titles; without it the header falls back to the track label and sections show raw ids.
  useEffect(() => {
    if (!session || !profile) return () => {}
    let alive = true
    host.listPersistedSessions(null, { profile, limit: 200 })
      .then(res => {
        if (!alive) return
        const map = {}
        for (const s of res?.sessions ?? []) map[s.id] = s.title || ''
        setSessionTitles(map)
        setSessionTitle(map[session] || '')
      })
      .catch(() => {})
    return () => { alive = false }
  }, [session, profile])

  // Hooks must stay above the early returns below — a conditional hook count
  // throws React #310 ("Rendered more hooks than during the previous render").
  const opMut = useMutation({
    mutationFn: ({ op, args }) =>
      ctx.rest('/op', {
        method: 'POST',
        body: { session, op, args },
        timeoutMs: 5000,
      }),
    onSuccess: () => { refreshRef.current?.() },
  })

  if (!session) {
    return jsx('div', {
      style: { padding: 12, fontSize: 13, color: 'var(--ui-text-tertiary)' },
      children: 'Open a chat to see progress.'
    })
  }
  if (error) {
    return jsx('div', {
      style: { padding: 12, fontSize: 13, color: 'var(--ui-error-foreground)' },
      children: error
    })
  }
  if (!snapshot) {
    return jsx('div', { style: { padding: 12, fontSize: 13 }, children: 'Loading…' })
  }

  const { tracks = [], turn, tokens = {}, log = { blocks: [], findings: [] }, timeline = [] } = snapshot
  // Counters and the bar describe this chat only; the pane body lists every session.
  const activeTrack = tracks.findLast(t => !t.isDismissed && t.tasks.length > 0 && (t.sessionId == null || t.sessionId === session))
  const { done, total } = activeTrack ? getTrackProgress(activeTrack) : { done: 0, total: 0 }
  const totalLogEntries = (log.blocks?.length ?? 0) + (log.findings?.length ?? 0)

  function handleOp(op, args) {
    opMut.mutate({ op, args })
  }

  // ─── Render helpers (defined below component to avoid redeclaration) ───
  // We'll inline render functions here for clarity

  // ─── Desktop Bar (top) ───
  const desktopBar = activeTrack ? jsx(DesktopBar, { track: activeTrack }) : null

  // ─── Session Title + Primary Pill ───
  const titleBlock = activeTrack
    ? jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 2, marginBottom: 8 },
        children: [
          jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: 8 },
            children: [
              jsx('span', {
                style: { fontWeight: 600, fontSize: 13, color: 'var(--ui-text-primary)', flex: 1, textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' },
                children: titleText || activeTrack.label || activeTrack.tasks[0]?.subject || 'Untitled'
              }),
              jsx(PrimaryPill, { track: activeTrack })
            ]
          }),
          (activeTrack.caption || activeTrack.description) && jsx('span', {
            style: { fontSize: 11, color: 'var(--ui-text-tertiary)', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' },
            children: activeTrack.caption || activeTrack.description
          })
        ]
      })
    : jsx('div', {
        style: { padding: 8, fontSize: 13, color: 'var(--ui-text-tertiary)' },
        children: 'No task list running.'
      })

  // ─── Tab Strip ───
  const tabStrip = jsxs('div', {
    style: { display: 'flex', gap: 4, marginBottom: 8, borderBottom: '1px solid var(--ui-stroke-secondary)', paddingBottom: 4 },
    children: [
      jsx(TabChip, {
        label: 'Progress',
        count: `${done}/${total}`,
        selected: tab === 'progress',
        onClick: () => setTab('progress')
      }),
      jsx(TabChip, {
        label: 'Log',
        count: String(totalLogEntries),
        selected: tab === 'log',
        onClick: () => setTab('log')
      })
    ]
  })

  // ─── Body ───
  const body = tab === 'progress'
    ? jsx(ProgressTab, {
        tracks,
        session,
        openEntryId,
        setOpenEntryId,
        blinkTick,
        handleOp,
        log,
        sessionTitles
      })
    : jsx(LogTab, {
        log,
        openEntryId,
        setOpenEntryId,
        blinkTick
      })

  // ─── Timeline Gutter (right side) ───
  const timelineGutter = activeTrack ? jsx(TimelineGutter, { track: activeTrack, log, turn, timelineWidth: TIMELINE_WIDTH }) : null

  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: 'column',
      height: '100%',
      overflow: 'auto',
      fontSize: 13,
      background: DARK_SURFACE,
      color: 'var(--ui-text-secondary)',
      padding: 12,
      gap: 8,
    },
    children: [
      desktopBar,
      titleBlock,
      jsx(Tiles, { track: activeTrack, context: snapshot.context }),
      tabStrip,
      jsxs('div', {
        style: { display: 'flex', flex: 1, minHeight: 0, gap: 8 },
        children: [
          jsx('div', { style: { flex: 1, minWidth: 0, overflow: 'auto' }, children: body }),
          timelineGutter
        ]
      })
    ]
  })
}

// ─── Sub-components ───

// ─── Stat tiles (upstream tilesOf / headerOf :1611-1690) ───
const TILE_LABEL = { fontSize: 9, letterSpacing: '.04em', color: 'var(--ui-text-tertiary)' }

function Tile({ label, value, note, color }) {
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 1, flexShrink: 0 },
    children: [
      jsx('span', { style: TILE_LABEL, children: label }),
      jsxs('span', {
        style: { display: 'flex', gap: 4, alignItems: 'baseline' },
        children: [
          jsx('span', { style: { fontSize: 12, fontWeight: 600, color: color || 'var(--ui-text-primary)' }, children: value }),
          note ? jsx('span', { style: { fontSize: 10, color: 'var(--ui-text-tertiary)' }, children: note }) : null
        ]
      })
    ]
  })
}

// A dim label over a bold value, with a dim note beside it — upstream's tile row, wrapping.
// Token tiles appear only once a call has been counted: zeros would be a number nobody measured.
function Tiles({ track, context }) {
  const tiles = []
  if (track) {
    const { done, total } = getTrackProgress(track)
    tiles.push({ label: 'PROGRESS', value: `${total > 0 ? Math.round((done / total) * 100) : 0}%`, note: `${done} of ${total}` })
    if (track.taskMs > 0) tiles.push({ label: 'THIS TASK', value: durationOf(track.taskMs) })
    tiles.push({ label: 'WORKED', value: durationOf(track.workMs || 0) })
    tiles.push({ label: 'WAITED ON YOU', value: (track.waitMs || 0) >= 1000 ? durationOf(track.waitMs) : 'none' })
    const tk = track.tokens
    if (tk && tk.calls) {
      tiles.push({ label: 'TOKENS IN', value: countOf(tk.input || 0), note: `${Math.round(((tk.cached || 0) / Math.max(1, tk.input || 0)) * 100)}% cached` })
      tiles.push({ label: 'TOKENS OUT', value: countOf(tk.output || 0) })
      tiles.push({ label: 'MODEL CALLS', value: String(tk.calls) })
    }
  }
  if (context && context.percent != null) {
    tiles.push({ label: 'CONTEXT', value: `${context.percent}%`, color: context.percent >= 80 ? '#f59e0b' : undefined })
  }
  if (!tiles.length) return null
  return jsxs('div', {
    style: { display: 'flex', flexWrap: 'wrap', columnGap: 12, rowGap: 4, marginBottom: 8 },
    children: tiles.map(t => jsx(Tile, { ...t, key: `tile:${t.label}` }))
  })
}

function DesktopBar({ track }) {
  const { done, total } = getTrackProgress(track)
  const pct = total > 0 ? Math.round((done / total) * 100) : 0
  const segments = track.tasks.map((task) => {
    const state = getTaskState(task, track)
    return { state, width: total > 0 ? 100 / total : 0 }
  })

  return jsxs('div', {
    style: { marginBottom: 8 },
    children: [
      jsx('div', {
        role: 'progressbar',
        'aria-valuenow': pct,
        'aria-valuemin': 0,
        'aria-valuemax': 100,
        'aria-label': `${track.label || 'Progress'} ${done}/${total}`,
        title: `${track.label || 'Tasks'} · ${done}/${total} (${pct}%)`,
        style: {
          height: DESKTOP_BAR_HEIGHT,
          borderRadius: '9999px',
          background: REMAINDER_COLOR,
          overflow: 'hidden',
        },
        children: jsx('div', {
          style: { display: 'flex', width: `${pct}%`, height: '100%', transition: 'width 0.2s' },
          children: segments.map((seg, i) =>
            jsxs('div', {
              title: track.tasks[i]?.subject,
              style: {
                width: `${seg.width}%`,
                height: '100%',
                background: STATE_COLOR[seg.state] || STATE_COLOR.pending,
              },
              children: seg.state === 'failed' && jsx('div', {
                style: {
                  width: '100%',
                  height: '100%',
                  // stripe pattern (not a color gradient) marks the failed stretch
                  background: 'repeating-linear-gradient(45deg, rgba(0,0,0,0.2) 0 4px, transparent 4px 8px)',
                }
              })
            }, i)
          )
        })
      }),
      jsxs('div', {
        style: { display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--ui-text-tertiary)', marginTop: 2 },
        children: [
          jsx('span', { children: `${pct}%` }),
          jsx('span', { style: { textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap', maxWidth: '70%' }, children: track.label || track.tasks[0]?.subject || '' })
        ]
      })
    ]
  })
}

function PrimaryPill({ track }) {
  const { done, total } = getTrackProgress(track)
  const current = track.tasks.find(t => t.status === 'in_progress')
  const state = current ? getTaskState(current, track) : (done === total && total > 0 ? 'done' : 'pending')
  const colors = {
    done: { bg: '#7cc68a', fg: '#14261a' },
    failed: { bg: '#ef6f5c', fg: '#2a0a05' },
    waiting: { bg: '#f0b34a', fg: '#2a1c05' },
    working: { bg: '#a99cf5', fg: '#1d1a2e' },
    pending: { bg: '#6b7280', fg: '#f3f4f6' },
  }
  const c = colors[state] || colors.pending
  let label = ''
  if (state === 'done') label = `✓ Done ${done}/${total}`
  else if (state === 'failed') label = `✕ ${current?.subject || 'Failed'} ${done}/${total}`
  else if (state === 'waiting') label = track.isPlanning ? 'Waiting for approval' : 'Waiting for you'
  else label = `${current?.subject || 'Tasks'} ${done}/${total}`

  return jsx('span', {
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      height: 22,
      padding: '0 11px',
      borderRadius: 11,
      background: c.bg,
      color: c.fg,
      fontSize: 11,
      fontWeight: 600,
      whiteSpace: 'nowrap',
      fontFamily: 'var(--font-mono, ui-monospace)',
    },
    children: label
  })
}

function TabChip({ label, count, selected, onClick }) {
  return jsx('button', {
    type: 'button',
    onClick,
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 4,
      padding: '4px 8px',
      borderRadius: 6,
      border: 'none',
      background: selected ? '#2a2a2f' : 'transparent',
      color: selected ? '#e6e6e9' : 'var(--ui-text-secondary)',
      fontSize: 12,
      fontWeight: selected ? 600 : 400,
      cursor: 'pointer',
    },
    children: `${label}  ${count}`
  })
}

// ─── Progress Tab ───

function ProgressTab({ tracks, session, openEntryId, setOpenEntryId, blinkTick, handleOp, log, sessionTitles }) {
  const live = tracks.filter(t => !t.isDismissed && t.tasks.length > 0)
  if (live.length === 0) {
    return jsx('div', {
      style: { padding: 16, color: 'var(--ui-text-tertiary)', fontSize: 13 },
      children: 'No active track. Create one with the input fields below.'
    })
  }

  // One section per session; inside a section the most recent track renders last (expanded).
  const order = []
  const bySession = new Map()
  for (const t of live) {
    const key = t.sessionId ?? null
    if (!bySession.has(key)) { bySession.set(key, []); order.push(key) }
    bySession.get(key).push(t)
  }
  // Deterministic: "this chat" first, then stable alphabetic by session id (null → 'legacy')
  order.sort((a, b) => (b === session ? 1 : 0) - (a === session ? 1 : 0) || String(a ?? '').localeCompare(String(b ?? '')))

  const renderTrack = (track, isActive, isLast) => jsx(TrackGroup, {
    track, isActive, isLast, openEntryId, setOpenEntryId, blinkTick, handleOp, log, key: track.id
  })

  return jsxs('div', { children: order.map((sid, gi) => {
    const list = bySession.get(sid)
    const active = list[list.length - 1]
    return jsxs('div', {
      style: { marginBottom: gi === order.length - 1 ? 0 : 12 },
      key: sid ?? 'legacy',
      children: [
        jsx('div', {
          style: { fontSize: 10, letterSpacing: '.08em', textTransform: 'uppercase', color: 'var(--ui-text-tertiary)', padding: '2px 0 6px' },
          children: sid === session ? 'this chat' : (sessionTitles[sid] || shortSession(sid))
        }),
        ...list.slice(0, -1).map(t => renderTrack(t, false, false)),
        renderTrack(active, true, gi === order.length - 1)
      ]
    })
  })})
}

function TrackGroup({ track, isActive, isLast, blinkTick, log, handleOp }) {
  const { done, total } = getTrackProgress(track)
  const currentTask = track.tasks.find(t => t.status === 'in_progress')
  const [isExpanded, setExpanded] = useState(isActive || !!track.isExpanded)
  const opRef = useRef(handleOp)
  opRef.current = handleOp
  // Upstream stampDone: once every task is completed and the group is collapsed, dismiss
  // it after the linger. Expanded groups stay until the user presses ✕.
  const complete = track.tasks.length > 0 && track.tasks.every(t => t.status === 'completed')
  useEffect(() => {
    if (!complete || isExpanded) return undefined
    const lastClosed = Math.max(...track.tasks.map(t => t.closedAt || 0))
    const wait = Math.max(0, DONE_LINGER_MS - (Date.now() - lastClosed))
    const id = setTimeout(() => opRef.current('dismiss', { trackId: track.id }), wait)
    return () => clearTimeout(id)
  }, [complete, isExpanded, track])
  const rows = timelineRows(track)
  // The SVG string is built only from our own constants and numeric track fields — no
  // user/task text reaches it, so dangerouslySetInnerHTML is safe here.
  const gutterFor = (row, i) => jsx('span', {
    style: { lineHeight: 0, display: 'inline-flex', flexShrink: 0 },
    dangerouslySetInnerHTML: {
      __html: timelineSvg({ ...row, isFirst: i === 0, isLast: i === rows.length - 1, index: i }),
    },
  })

  return jsxs('div', {
    style: { marginBottom: isLast ? 0 : 12, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, overflow: 'hidden' },
    children: [
      // Track header
      jsxs('div', {
        style: {
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 10px',
          background: isActive ? '#1e1e22' : 'transparent',
          fontWeight: 600,
          fontSize: 12,
          color: 'var(--ui-text-secondary)',
          cursor: 'pointer',
        },
        onClick: () => setExpanded(v => !v),
        children: [
          isExpanded ? '▼' : '▶',
          jsx('span', { style: { flex: 1, textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' }, children: track.label || track.tasks[0]?.subject || 'Untitled' }),
          jsx('span', { style: { fontWeight: 400, color: 'var(--ui-text-tertiary)' }, children: `${done}/${total}` }),
          jsx('span', {
            title: 'Dismiss',
            style: { cursor: 'pointer', color: 'var(--ui-text-tertiary)', padding: '0 2px' },
            onClick: (e) => { e.stopPropagation(); handleOp('dismiss', { trackId: track.id }) },
            children: '✕',
          }),
        ]
      }),
      // Task rows
      isExpanded && jsxs('div', {
        style: { padding: '0 10px 8px', display: 'flex', flexDirection: 'column', gap: 0 },
        children: rows.map((row, i) => row.isSubAgent
          ? jsx('div', {
              key: row.key,
              title: row.subject,
              style: { height: TL_ROW_HEIGHT, display: 'flex', alignItems: 'center' },
              children: gutterFor(row, i),
            })
          : jsx(TaskRow, {
              task: row.task,
              track,
              isActiveTask: row.task === currentTask,
              blinkTick,
              log,
              gutter: gutterFor(row, i),
              key: row.key,
            })).concat([
          // Measured totals for the whole list — upstream totalsOf, the breakdown's last line.
          jsx('div', {
            key: 'totals',
            style: { paddingTop: 6, fontSize: 11, color: 'var(--ui-text-tertiary)' },
            children: [
              `${durationOf(track.workMs || 0)} work`,
              (track.waitMs || 0) >= 1000 ? `${durationOf(track.waitMs)} waited on you` : null,
              (track.agentRuns?.length || 0) > 0 ? `${track.agentRuns.length} agent${track.agentRuns.length === 1 ? '' : 's'}` : null,
            ].filter(Boolean).join(' · ')
          })
        ])
      }),
    ]
  })
}

function TaskRow({ task, track, isActiveTask, blinkTick, log, gutter }) {
  const state = getTaskState(task, track)
  const isBlinking = isActiveTask && state === 'active' && (blinkTick % 2 === 0)

  // State glyph (SPEC 3.4: pending ○, active ▸, done ✓, skipped –, failed ✕)
  const glyphs = {
    pending: '○',
    active: '▸',
    done: '✓',
    skipped: '–',
    failed: '✕',
  }
  const glyph = glyphs[state] || '○'

  // Right-side items in priority order: finding pill, block badge, note, ticket link
  const taskFindings = (log?.findings || []).filter(f => f.task === task.subject)
  const openBlock = (log?.blocks || []).find(b => b.task === task.subject && b.answeredAt == null)
  const right = []
  taskFindings.slice(0, 1).forEach(f => right.push(jsx(FindingPill, { finding: f, key: `f-${f.id}` })))
  if (openBlock) {
    const c = BLOCK_COLORS[openBlock.kind] || BLOCK_COLORS.permission
    right.push(jsx('span', {
      key: 'block',
      title: openBlock.text,
      style: {
        display: 'inline-flex', alignItems: 'center', height: 16, padding: '0 6px',
        borderRadius: 8, border: `1px solid ${c}`, color: c, fontSize: 10, fontWeight: 600,
        whiteSpace: 'nowrap',
      },
      children: openBlock.kind
    }))
  }
  if (task.note) {
    right.push(jsx('span', {
      key: 'note',
      title: task.note,
      style: { fontSize: 11, color: 'var(--ui-text-tertiary)', maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
      children: task.note
    }))
  }
  if (task.ticket) {
    right.push(jsx('a', {
      key: 'ticket',
      href: task.ticket,
      target: '_blank',
      rel: 'noreferrer',
      style: { fontSize: 11, color: '#9a9fa8', textDecoration: 'none', whiteSpace: 'nowrap' },
      children: 'ticket'
    }))
  }

  return jsxs('div', {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      // Fixed row height so the per-row timeline SVGs join into one continuous line.
      height: TL_ROW_HEIGHT,
      opacity: task.isSkipped ? 0.6 : 1,
      textDecoration: task.isSkipped ? 'line-through' : 'none',
    },
    children: [
      gutter ?? jsx('span', {
        style: { fontSize: 14, width: 20, textAlign: 'center', opacity: isActiveTask && state === 'active' ? (isBlinking ? 0.4 : 1) : 1 },
        children: glyph
      }),
      isActiveTask && jsx('span', {
        style: { fontSize: 10, color: 'var(--ui-text-tertiary)', flexShrink: 0 },
        children: '▼'
      }),
      jsx('span', {
        style: { flex: 1, fontSize: 13, color: 'var(--ui-text-secondary)', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap', fontWeight: isActiveTask ? 700 : 400 },
        children: task.subject
      }),
      // Attempt pills (f0/f1/f2 cyan for unfinished, red failure count when failures > unfinished)
      (task.attempts > 0 || task.failures > 0) && jsx(AttemptPills, { task }),
      right,
    ]
  })
}

function AttemptPills({ task }) {
  // SPEC state model has `failures` per task, not `attempts`; the current run counts as one more
  const failures = task.failures || 0
  const attempts = task.attempts ?? (failures + 1)
  const unfinished = Math.max(0, attempts - failures)

  const pills = []
  for (let i = 0; i < unfinished; i++) {
    pills.push(jsx('span', {
      key: `f${i}`,
      style: {
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        minWidth: 18,
        height: 16,
        padding: '0 4px',
        borderRadius: 8,
        background: ATTEMPT_COLORS.unfinished.bg,
        color: ATTEMPT_COLORS.unfinished.fg,
        fontSize: 10,
        fontWeight: 600,
        fontFamily: 'var(--font-mono, ui-monospace)',
        marginRight: 2,
      },
      children: `f${i}`
    }))
  }
  if (failures > unfinished) {
    const count = String(failures)
    const display = count.length === 2 ? count.split('').join('\u2009') : count // thin space
    pills.push(jsx('span', {
      key: 'fail',
      style: {
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        minWidth: 18,
        height: 16,
        padding: '0 4px',
        borderRadius: 8,
        background: ATTEMPT_COLORS.failure.bg,
        color: ATTEMPT_COLORS.failure.fg,
        fontSize: 10,
        fontWeight: 600,
        fontFamily: 'var(--font-mono, ui-monospace)',
      },
      children: display
    }))
  }
  return jsx('span', { style: { display: 'flex', alignItems: 'center', gap: 2 }, children: pills })
}

function FindingPill({ finding }) {
  const isFound = finding.kind === 'found'
  const c = isFound ? FINDING_COLORS.found : FINDING_COLORS.failed
  const dot = isFound ? '◆' : '✕'
  const label = `${dot} ${finding.task || finding.text.slice(0, 30)}`

  return jsx('span', {
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 4,
      padding: '2px 8px',
      borderRadius: 8,
      background: c.bg,
      color: c.fg,
      fontSize: 11,
      fontWeight: 500,
      whiteSpace: 'nowrap',
    },
    title: finding.kind === 'failed' ? finding.command : finding.text,
    children: label
  })
}

// ─── Log Tab ───
function LogTab({ log, openEntryId, setOpenEntryId, blinkTick }) {
  const openBlocks = log.blocks.filter(b => b.answeredAt == null).reverse() // newest first
  const answeredBlocks = log.blocks.filter(b => b.answeredAt != null).reverse()
  const findings = log.findings.slice().reverse() // newest first

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 12 },
    children: [
      // WAITING NOW section
      jsx('div', {
        style: { fontSize: 11, fontWeight: 600, color: 'var(--ui-text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.04em' },
        children: openBlocks.length ? 'WAITING NOW' : 'Nothing waits on you.'
      }),
      openBlocks.length ? jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 4 },
        children: openBlocks.map((block, i) => jsx(BlockChip, { block, openEntryId, setOpenEntryId, blinkTick, isNewest: i === 0, key: block.id }))
      }) : jsx('div', {
        style: { padding: 12, color: 'var(--ui-text-tertiary)', fontSize: 12, lineHeight: 1.5 },
        children: 'Permission prompts, questions and plan approvals show up here.'
      }),

      // ANSWERED section
      answeredBlocks.length && jsxs('div', { children: [
        jsx('div', {
          style: { fontSize: 11, fontWeight: 600, color: 'var(--ui-text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.04em', marginTop: 8 },
          children: openBlocks.length ? ' ANSWERED' : 'ANSWERED'
        }),
        jsxs('div', {
          style: { display: 'flex', flexDirection: 'column', gap: 4 },
          children: answeredBlocks.map(block => jsx(BlockRow, { block, key: block.id }))
        })
      ]}),

      // FINDINGS section
      jsx('div', {
        style: { fontSize: 11, fontWeight: 600, color: 'var(--ui-text-tertiary)', textTransform: findings.length ? 'uppercase' : 'none', letterSpacing: '0.04em', marginTop: 8 },
        children: findings.length ? 'Found' : 'Nothing found yet.'
      }),
      findings.length ? jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 4 },
        children: findings.map(f => jsx(FindingCard, { finding: f, key: f.id }))
      }) : jsx('div', {
        style: { padding: 12, color: 'var(--ui-text-tertiary)', fontSize: 12, lineHeight: 1.5 },
        children: 'Failing checks and Claude\'s findings show up here.'
      }),
    ]
  })
}

function BlockChip({ block, openEntryId, setOpenEntryId, blinkTick, isNewest }) {
  const isOpen = openEntryId === block.id
  const color = BLOCK_COLORS[block.kind] || BLOCK_COLORS.permission
  const kindLabel = { permission: 'Permission', question: 'Question', approval: 'Plan approval' }[block.kind] || 'Block'

  const detail = [
    kindLabel,
    block.task,
    `waiting ${formatDuration(Date.now() - block.at)}`
  ].filter(Boolean).join(' · ')

  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
      padding: '8px 10px',
      borderRadius: 8,
      border: `2px solid ${color}`,
      background: isOpen ? `color-mix(in srgb, ${color} 10%, transparent)` : 'transparent',
      opacity: block.answeredAt ? 0.75 : 1,
      position: 'relative',
      overflow: 'visible',
    },
    children: [
      // Pulsing ring for newest
      isNewest && !block.answeredAt && jsx('div', {
        style: {
          position: 'absolute',
          inset: -4,
          borderRadius: 12,
          border: `2px solid ${color}`,
          animation: 'pulse-ring 2s ease-out infinite',
          pointerEvents: 'none',
        }
      }),
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 8, cursor: isOpen ? 'default' : 'pointer' },
        onClick: () => setOpenEntryId(isOpen ? null : block.id),
        children: [
          jsx('span', {
            style: { width: 10, height: 10, borderRadius: '50%', background: color, flexShrink: 0 },
          }),
          jsx('span', {
            style: { flex: 1, fontWeight: 600, fontSize: 12, color: 'var(--ui-text-secondary)', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' },
            children: block.text
          }),
          jsx('span', { style: { fontSize: 11, color: 'var(--ui-text-tertiary)' }, children: formatDuration(Date.now() - block.at) }),
          jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: isOpen ? '▾' : '▸' }),
        ]
      }),
      isOpen && jsx('div', {
        style: { paddingLeft: 22, fontSize: 12, color: 'var(--ui-text-secondary)', lineHeight: 1.6 },
        children: jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: 4 }, children: [
          jsx('div', { children: jsxs('span', { children: [jsx('strong', { children: 'Task: ' }), block.task || '—'] }) }),
          jsx('div', { children: jsxs('span', { children: [jsx('strong', { children: 'Kind: ' }), kindLabel] }) }),
          jsx('div', { children: jsxs('span', { children: [jsx('strong', { children: 'Waiting: ' }), formatDuration(Date.now() - block.at)] }) }),
          block.details && jsx('details', { children: [
            jsx('summary', { style: { cursor: 'pointer', color: 'var(--ui-text-tertiary)' }, children: 'Details' }),
            jsx('pre', { style: { marginTop: 4, fontSize: 11, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }, children: JSON.stringify(block.details, null, 2) })
          ]}),
        ]})
      }),
    ]
  })
}

function BlockRow({ block }) {
  const color = BLOCK_COLORS[block.kind] || BLOCK_COLORS.permission
  const kindLabel = { permission: 'Permission', question: 'Question', approval: 'Plan approval' }[block.kind] || 'Block'
  const waited = block.answeredAt ? formatDuration(block.answeredAt - block.at) : '—'

  return jsxs('div', {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '6px 8px',
      opacity: 0.75,
      fontSize: 12,
      color: 'var(--ui-text-tertiary)',
    },
    children: [
      jsx('span', { style: { color: '#7cc68a' }, children: '✓' }),
      jsx('span', { style: { flex: 1, textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' }, children: block.text }),
      jsx('span', { children: `· waited ${waited}${block.task ? ` · ${block.task}` : ''}` }),
    ]
  })
}

function FindingCard({ finding }) {
  const isFound = finding.kind === 'found'
  const c = isFound ? FINDING_COLORS.found : FINDING_COLORS.failed
  const label = isFound ? 'Finding' : 'Check failed'

  const detailParts = [
    label,
    finding.task
  ].filter(Boolean).join(' · ')

  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
      padding: '8px 10px',
      borderRadius: 8,
      border: `2px solid ${c.dot}`,
      background: 'transparent',
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 8 },
        children: [
          jsx('span', { style: { width: 10, height: 10, borderRadius: '50%', background: c.dot, flexShrink: 0 } }),
          jsx('span', { style: { fontWeight: 600, fontSize: 12, color: 'var(--ui-text-secondary)' }, children: finding.text }),
        ]
      }),
      jsx('div', { style: { paddingLeft: 20, fontSize: 11, color: 'var(--ui-text-tertiary)' }, children: detailParts }),
      finding.kind === 'failed' && finding.command && jsx('div', {
        style: { paddingLeft: 20, fontSize: 11, color: 'var(--ui-text-tertiary)', fontFamily: 'var(--font-mono, ui-monospace)', opacity: 0.7 },
        children: `Check ${finding.command}`
      }),
      finding.kind === 'failed' && finding.lines && finding.lines.length > 0 && jsx('div', {
        style: { paddingLeft: 20, fontSize: 11, color: 'var(--ui-text-tertiary)', fontFamily: 'var(--font-mono, ui-monospace)' },
        children: finding.lines.map((line, i) => jsx('div', { key: i, children: `│ ${line}` }))
      }),
      jsx('div', { style: { paddingLeft: 20, fontSize: 11, color: 'var(--ui-text-tertiary)' }, children: `At ${new Date(finding.at).toLocaleTimeString()}` }),
    ]
  })
}

// ─── Timeline Gutter ───
function TimelineGutter({ track, log, turn, timelineWidth }) {
  const openBlock = log.blocks.find(b => b.answeredAt == null)
  const agentRuns = track.agentRuns || []
  const turnStart = turn?.startedAt ?? turn?.start ?? null
  const trackStart = track.startedAt ?? track.turnStartedAt ?? null
  const elapsed = trackStart != null ? Math.max(1, (turn?.endedAt ?? Date.now()) - trackStart) : 1
  const pct = (t) => (t == null || trackStart == null ? null : Math.min(100, Math.max(0, ((t - trackStart) / elapsed) * 100)))

  return jsx('div', {
    style: {
      width: timelineWidth,
      flexShrink: 0,
      position: 'relative',
      borderLeft: openBlock ? `2px solid #ef4444` : 'none',
    },
    children: [
      // Dashed vertical line
      jsx('div', {
        style: {
          position: 'absolute',
          left: '50%',
          top: 0,
          bottom: 0,
          width: 1,
          borderLeft: 'dashed 1px rgba(255,255,255,0.4)',
          transform: 'translateX(-50%)',
          pointerEvents: 'none',
        }
      }),
      // Open turn start dot
      turnStart && pct(turnStart) != null && jsx('div', {
        style: {
          position: 'absolute',
          left: '50%',
          top: `${pct(turnStart)}%`,
          transform: 'translate(-50%, -50%)',
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: '#a78bfa',
          pointerEvents: 'none',
        }
      }),
      // Agent run ticks
      agentRuns.map((run, i) => {
        const p = pct(run.startedAt) ?? i * 10
        return jsx('div', {
          style: {
            position: 'absolute',
            left: '50%',
            top: `${Math.min(100, Math.max(0, p))}%`,
            transform: 'translate(-50%, -50%)',
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: '#a99cf5',
            pointerEvents: 'none',
          }
        }, `agent-${run.taskId}-${i}`)
      }),
      // Task dots
      track.tasks.map((task, i) => {
        const p = pct(task.startedAt) ?? i * 15
        const color = task.status === 'completed' ? '#7cc68a' : task.status === 'in_progress' ? '#a99cf5' : task.isSkipped ? '#f59e0b' : '#6b7280'
        return jsx('div', {
          style: {
            position: 'absolute',
            left: '50%',
            top: `${Math.min(100, Math.max(0, p))}%`,
            transform: 'translate(-50%, -50%)',
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: color,
            border: task.status === 'in_progress' ? '2px solid #fff' : 'none',
            pointerEvents: 'none',
          }
        }, `task-${task.id}`)
      }),
    ]
  })
}

// ─── Task-list timeline: ported from upstream hooks/timeline-svg.mjs ───
// One small SVG per row, joined edge to edge into a single connected line. SMIL draws it
// once on mount (rows staggered, dots pop, one ring each) and then holds still.

function timelineSvg({ pos, isFirst, isLast, isAgent, isSkipped, status = 'completed', failures = 0, index = 0 }) {
  const h = TL_ROW_HEIGHT
  const mid = h / 2
  const isBranch = pos !== 'main' && pos !== 'fork'
  const x = isBranch ? TL_BRANCH_X : TL_MAIN_X
  const drawAt = index * TL_STAGGER
  // A hollow dot (pending/skipped) is a ring with nothing inside: the line is cut around it
  // by a mask so the band shows through.
  const isHollow = !isAgent && (isSkipped || status === 'pending')
  const parts = [
    `<style>:root{color-scheme:light dark}.ln{stroke:${TL_LINE}}.pd{stroke:${TL_PENDING}}`,
    `@media (prefers-color-scheme:light){.ln{stroke:${TL_LINE_LIGHT}}.pd{stroke:${TL_PENDING_LIGHT}}}</style>`,
    isHollow
      ? `<defs><mask id="cut" maskUnits="userSpaceOnUse" x="0" y="0" width="${TL_GUTTER_WIDTH}" height="${h}"><rect width="${TL_GUTTER_WIDTH}" height="${h}" fill="#fff"/><circle cx="${x}" cy="${mid}" r="5" fill="#000"/></mask></defs>`
      : '',
  ]
  const draw = `<animate attributeName="stroke-dashoffset" from="1" to="0" begin="${drawAt}s" dur="${TL_DRAW}s" fill="freeze"/>`
  const stroke = (shape) =>
    parts.push(`${shape} class="ln"${isHollow ? ' mask="url(#cut)"' : ''} fill="none" stroke-width="1.5" pathLength="1" stroke-dasharray="1" stroke-dashoffset="1">${draw}</path>`)
  const line = (x1, y1, x2, y2) => stroke(`<path d="M${x1} ${y1} L${x2} ${y2}"`)
  const curve = (d) => stroke(`<path d="${d}"`)

  // The main line runs through every row; it starts at the first dot and ends at the last.
  line(TL_MAIN_X, isFirst && !isBranch ? mid : 0, TL_MAIN_X, isLast && !isBranch ? mid : h)

  if (pos === 'fork') curve(`M${TL_MAIN_X} ${mid} Q${TL_BRANCH_X} ${mid} ${TL_BRANCH_X} ${h}`)
  if (pos === 'start' || pos === 'only') curve(`M${TL_MAIN_X} 1 Q${TL_BRANCH_X} 1 ${TL_BRANCH_X} ${mid}`)
  if (pos === 'middle' || pos === 'end') line(TL_BRANCH_X, 0, TL_BRANCH_X, mid)
  if (pos === 'start' || pos === 'middle') line(TL_BRANCH_X, mid, TL_BRANCH_X, h)
  if (pos === 'end' || pos === 'only') curve(`M${TL_BRANCH_X} ${mid} Q${TL_BRANCH_X} ${h - 1} ${TL_MAIN_X} ${h - 1}`)

  const popAt = drawAt + TL_DRAW * 0.6
  parts.push(isAgent ? tlRobot(x, mid, popAt, index) : tlDot(x, mid, { isSkipped, status, failures }, popAt))
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${TL_GUTTER_WIDTH}" height="${h}" viewBox="0 0 ${TL_GUTTER_WIDTH} ${h}">${parts.join('')}</svg>`
}

// Done: a green dot that rings once. A step that failed on the way keeps a coral ring;
// the running step is purple; pending and skipped steps are hollow.
function tlDot(x, y, { isSkipped, status, failures }, popAt) {
  const pop = `<animate attributeName="r" values="0;5;4" keyTimes="0;.7;1" begin="${popAt}s" dur=".25s" fill="freeze"/>`
  if (isSkipped || status === 'pending') {
    const edge = isSkipped ? `stroke="${TL_DONE}"` : 'class="pd"'
    return `<circle cx="${x}" cy="${y}" r="0" fill="none" ${edge} stroke-opacity=".7" stroke-width="1.5">${pop}</circle>`
  }
  const fill = status === 'in_progress' ? TL_ACTIVE : TL_DONE
  const scar = failures > 0 ? `<circle cx="${x}" cy="${y}" r="7" fill="none" stroke="${TL_FAILED}" stroke-width="1.5" stroke-opacity=".75"/>` : ''
  const ring = [
    `<circle cx="${x}" cy="${y}" r="4" fill="none" stroke="${fill}" stroke-width="1.2" stroke-opacity="0">`,
    `<animate attributeName="r" values="4;9" begin="${popAt}s" dur=".6s" fill="freeze"/>`,
    `<animate attributeName="stroke-opacity" values=".8;0" begin="${popAt}s" dur=".6s" fill="freeze"/>`,
    '</circle>',
  ].join('')
  return `${scar}${ring}<circle cx="${x}" cy="${y}" r="0" fill="${fill}">${pop}</circle>`
}

// A robot head: antenna with a blinking light, rounded face, two eyes that blink.
function tlRobot(x, y, popAt, index) {
  const blinkAt = (index * 0.37) % 2.8
  const eye = (ex) =>
    `<circle cx="${ex}" cy="${y}" r="1.4" fill="#1d1a2e"><animate attributeName="r" values="1.4;1.4;.25;1.4" keyTimes="0;.9;.95;1" dur="2.8s" begin="${blinkAt}s" repeatCount="3"/></circle>`
  return [
    `<g opacity="0"><animate attributeName="opacity" from="0" to="1" begin="${popAt}s" dur=".25s" fill="freeze"/>`,
    `<line x1="${x}" y1="${y - 8}" x2="${x}" y2="${y - 5}" stroke="${TL_AGENT}" stroke-width="1.5"/>`,
    `<circle cx="${x}" cy="${y - 8.5}" r="1.4" fill="${TL_AGENT}"><animate attributeName="fill-opacity" values="1;.25;1" dur="1.2s" repeatCount="5"/></circle>`,
    `<rect x="${x - 6}" y="${y - 5}" width="12" height="10" rx="3" fill="${TL_AGENT}"/>`,
    eye(x - 2.5),
    eye(x + 2.5),
    '</g>',
  ].join('')
}

// Rows of one track's list, in order, with the lane each one occupies. Steps that ran at
// the same time take two lanes; a task's sub-agents hang on the branch with robot heads.
// shortcut: agentRuns carry no subject/status in this store, so sub-rows are unlabelled
// connectors (runId in the title); upgrade when progress_store.py records run subject.
function timelineRows(track) {
  const rows = []
  for (const task of track.tasks) {
    rows.push(tlEntry(task, false))
    for (const run of task.agentRuns ?? []) {
      rows.push(tlEntry(run, true))
    }
  }
  let i = 0
  while (i < rows.length) {
    let j = i
    if (rows[i].isSubAgent) {
      while (rows[j + 1]?.isSubAgent) j++
    } else {
      let end = rows[i].doneAt ?? 0
      while (rows[j + 1] && !rows[j + 1].isSubAgent && rows[j + 1].startedAt != null && rows[j + 1].startedAt < end) {
        j++
        end = Math.max(end, rows[j].doneAt ?? 0)
      }
    }
    if (rows[i].isSubAgent) {
      for (let k = i; k <= j; k++) rows[k].pos = i === j ? 'only' : k === i ? 'start' : k === j ? 'end' : 'middle'
    } else if (j > i) {
      // Simultaneous steps: the first keeps the main line, the branch forks from its dot.
      for (let k = i; k <= j; k++) rows[k].pos = k === i ? 'fork' : k === j ? 'end' : 'middle'
    }
    i = j + 1
  }
  return rows
}

function tlEntry(item, isSubAgent) {
  const isAgent = isSubAgent
  return {
    key: isAgent ? `run-${item.runId}` : item.id,
    task: isAgent ? null : item,
    subject: isAgent ? item.altId || item.runId : item.subject,
    status: item.status ?? (item.endedAt ? 'completed' : item.startedAt ? 'in_progress' : 'pending'),
    startedAt: item.startedAt ?? null,
    doneAt: item.endedAt ?? item.doneAt ?? null,
    pos: 'main',
    isAgent,
    isSubAgent,
    isSkipped: !isAgent && item.isSkipped === true,
    failures: isAgent ? 0 : (item.failures ?? 0),
  }
}

// ─── Composer band (the Claude Code above-prompt strip) ───
// ─── The animated bar: ported from the upstream hooks/bar-svg.mjs ───
// Same geometry (dot grid, fill-edge pill, task ticks) and the same SMIL motion:
// shine sweep while moving, twinkle groups, slow glitter when done.

// Ported verbatim from upstream hooks/bar-svg.mjs — colours live on classes so the same
// picture follows the app's light/dark mode. `.dt` fills the dot group, `.tr` tints the
// track at low opacity so the band behind it shows through.
function schemeStyle(status) {
  const rules = (p, empty, trackOpacity) =>
    `.tr{fill:${p.dot};fill-opacity:${trackOpacity}}.dt{fill:${p.dot}}.tk{fill:${p.tick}}` +
    `.et{fill:${empty};fill-opacity:.3}.pl{fill:${p.pill}}.ik{fill:${p.ink}}`
  return [
    `<style>:root{color-scheme:light dark}`,
    rules(PALETTE[status] || PALETTE.working, '#fff', 0.16),
    `@media (prefers-color-scheme:light){${rules(PALETTE_LIGHT[status] || PALETTE_LIGHT.working, '#000', 0.14)}}</style>`
  ].join('')
}

function barSvg({ width, done, total, status, pill, isMoving, skipped = [] }) {
  const h = SVG_HEIGHT
  const r = h / 2
  const pillW = Math.min(width, Math.ceil(pill.length * PILL_CHAR) + 22)
  const fillX = Math.round((done / total) * width)
  const pillX = Math.max(0, Math.min(width - pillW, fillX - pillW / 2))
  const fillEnd = Math.max(pillX + r, fillX)

  const veils = skipped.map((i) => {
    const x0 = Math.round((i / total) * width)
    const x1 = Math.round(((i + 1) / total) * width)
    return `<rect class="skip" x="${x0}" width="${x1 - x0}" height="${h}" fill="#000" fill-opacity=".65"/>`
  })
  const parts = [
    schemeStyle(status),
    `<defs><clipPath id="track"><rect width="${width}" height="${h}" rx="${r}"/></clipPath>`,
    `<clipPath id="fill"><rect width="${fillEnd}" height="${h}"/></clipPath>`,
    veils.length ? `<mask id="veil" maskUnits="userSpaceOnUse" x="0" y="0" width="${width}" height="${h}"><rect width="${width}" height="${h}" fill="#fff"/>${veils.join('')}</mask>` : '',
    `<linearGradient id="shine" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/>`,
    `<stop offset=".5" stop-color="#fff" stop-opacity=".22"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient></defs>`,
    `<rect class="tr" width="${width}" height="${h}" rx="${r}"/>`,
    `<g clip-path="url(#track)"><g clip-path="url(#fill)"${veils.length ? ` mask="url(#veil)"` : ''}>`,
    dots(fillEnd, h, status, isMoving),
  ]
  if (isMoving && status === 'working' && fillEnd > 40) {
    const band = Math.max(40, fillEnd * 0.25)
    parts.push(
      `<rect x="${-band}" width="${band}" height="${h}" fill="url(#shine)">`,
      `<animate attributeName="x" from="${-band}" to="${fillEnd}" dur="1.1s" repeatCount="indefinite"/></rect>`
    )
  }
  parts.push(`</g>`)
  for (let k = 1; k < total; k++) {
    const x = Math.round((k / total) * width)
    if (x > pillX - 2 && x < pillX + pillW + 2) continue
    const isFilled = x < fillX
    parts.push(
      `<rect class="${isFilled ? 'tk' : 'et'}" x="${x - 0.5}" y="${h * 0.25}" width="1" height="${h * 0.5}" rx=".5"${isFilled ? ` fill-opacity=".9"` : ''}/>`
    )
  }
  parts.push(`</g>`)
  parts.push(
    `<rect class="pl" x="${pillX}" y="1" width="${pillW}" height="${h - 2}" rx="${(h - 2) / 2}"/>`,
    `<text class="ik" x="${pillX + pillW / 2}" y="${h / 2}" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="${PILL_FONT}" font-weight="600" text-anchor="middle" dominant-baseline="central">${escapeXml(pill)}</text>`
  )
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${h}" viewBox="0 0 ${width} ${h}">${parts.join('')}</svg>`
}

const TWINKLE_GROUPS = 8
const SPARKLE_GROUPS = { working: 5, done: 8 }

function dots(fillEnd, h, status, isMoving) {
  const top = (h - (DOT_ROWS - 1) * PITCH - DOT) / 2
  const step = Math.max(PITCH, fillEnd / MAX_COLUMNS)
  const twinkles = Array.from({ length: TWINKLE_GROUPS }, () => [])
  const sparkleGroups = SPARKLE_GROUPS[status] ?? 0
  const sparkles = Array.from({ length: sparkleGroups }, () => [])
  for (let x = PITCH; x < fillEnd - 2; x += step) {
    const ramp = Math.pow(x / Math.max(1, fillEnd), 1.6)
    for (let row = 0; row < DOT_ROWS; row++) {
      const seed = hash(x * 31 + row * 7)
      if (seed > 0.3 + ramp * 0.7) continue
      const opacity = Math.min(1, (0.3 + ramp * 0.8) * (0.75 + 0.35 * hash(seed * 997)))
      const at = `x="${Math.round(x)}" y="${top + row * PITCH}" width="${DOT}" height="${DOT}" rx=".4"`
      twinkles[Math.floor(hash(seed * 13) * TWINKLE_GROUPS)].push(`<rect ${at} fill-opacity="${opacity.toFixed(2)}"/>`)
      const glitter = status === 'done' ? 0.22 + ramp * 0.3 : 0.08 + ramp * 0.22
      if (isMoving && sparkleGroups > 0 && hash(seed * 53) < glitter) {
        sparkles[Math.floor(hash(seed * 71) * sparkleGroups)].push(`<rect ${at}/>`)
      }
    }
  }
  if (!isMoving) return `<g class="dt">${twinkles.flat().join('')}</g>`
  const slow = status === 'failed' ? 1.8 : status === 'done' ? 1.5 : 1
  const out = twinkles.map((group, i) => {
    if (group.length === 0) return ''
    const dur = ((0.35 + (i / TWINKLE_GROUPS) * 0.55) * slow).toFixed(2)
    const begin = (-hash(i * 29 + 1) * Number(dur)).toFixed(2)
    const fade = `<animate attributeName="opacity" values="1;.15;1" dur="${dur}s" begin="${begin}s" repeatCount="indefinite"/>`
    return `<g class="dt">${fade}${group.join('')}</g>`
  })
  sparkles.forEach((group, i) => {
    if (group.length === 0) return
    const dur = (0.9 + (i / sparkleGroups) * 1.3).toFixed(2)
    const begin = (-hash(i * 89 + 3) * Number(dur)).toFixed(2)
    const flash = `<animate attributeName="opacity" values="0;1;0;0" keyTimes="0;.06;.16;1" dur="${dur}s" begin="${begin}s" repeatCount="indefinite"/>`
    out.push(`<g fill="#ffffff" opacity="0">${flash}${group.join('')}</g>`)
  })
  return out.join('')
  }

  // The band injects the bar as markup on purpose: SMIL animations must live inside inline
  // SVG, so CSS cannot cover them. Safe here because every string comes from barSvg() below,
  // built from numbers plus escapeXml'd pill text — no task or LLM text ever reaches it.
  const MAX_ROWS = 3

function ProgressBand({ ctx }) {
  const session = useValue(
    host.state.focusedStoredSessionId || host.state.activeSessionId
  )
  const [snap, setSnap] = useState(null)
  const [expandedIds, setExpandedIds] = useState(new Set())

  useEffect(() => {
    if (!session) { setSnap(null); return }
    let alive = true
    async function tick() {
      try {
        const res = await ctx.rest(
          `/snapshot?session=${encodeURIComponent(session)}`,
          { timeoutMs: 4000 }
        )
        if (alive) setSnap(res)
      } catch { /* backend down — band stays blank */ }
    }
    tick()
    const id = setInterval(tick, POLL_MS)
    return () => { alive = false; clearInterval(id) }
  }, [ctx, session])

  const tracks = snap?.tracks ?? []
  // Filter: non-dismissed, has tasks, session-scoped (defense-in-depth; API may return all tracks pre-patch)
  const sessionMatch = (t) => t.sessionId === session
  const eligibleTracks = tracks.filter(t => !t.isDismissed && t.tasks?.length > 0 && sessionMatch(t))
  // Active track is the last one
  const activeTrack = eligibleTracks[eligibleTracks.length - 1]
  // Other tracks (all but the last)
  const otherTracks = eligibleTracks.slice(0, -1)
  // Take last MAX_ROWS, but ensure active track is included if it exists
  const displayTracks = [...otherTracks, ...(activeTrack ? [activeTrack] : [])].slice(-MAX_ROWS)

  const [barWidth, setBarWidth] = useState(0)
  const barRef = useRef(null)
  useEffect(() => {
    const el = barRef.current
    if (!el) return
    setBarWidth(el.clientWidth)
    const ro = new ResizeObserver(() => setBarWidth(el.clientWidth))
    ro.observe(el)
    return () => ro.disconnect()
  }, [displayTracks.map(t => t.id).join(',')])

  if (displayTracks.length === 0) return null

  // Dismiss uses the exact ctx.rest shape from handleOp/opMut (line 126-132)
  const dismissTrack = (track) => {
    ctx.rest('/op', {
      method: 'POST',
      body: { session, op: 'dismiss', args: { trackId: track.id } },
      timeoutMs: 5000,
    })
  }

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 0 },
    children: displayTracks.map((track, trackIndex) => {
      const { done, total } = getTrackProgress(track)
      const active = track.tasks.find(t => t.status === 'in_progress')
      // upstream bar states: working / failed / waiting / done (progress-tracker.mjs:1220)
      const isDone = done === total
      const status = track.isWaiting ? 'waiting'
        : track.status === 'failed' ? 'failed'
        : isDone ? 'done'
        : active ? 'working' : 'waiting'
      const isCounter = !!track.isCounter || !!track.isAutoSteps
      const skipped = isCounter ? [] : track.tasks
        .map((t, i) => (t.status === 'skipped' || t.isSkipped ? i : -1))
        .filter(i => i >= 0)
      // upstream: a live bar moves, and a finished one keeps twinkling
      const isMoving = status === 'working' || status === 'done'

      // Pill text (upstream rowOf :1221-1231; agentsOf :1205; shorten :1583)
      const currentTask = active ?? track.tasks.find(t => t.status !== 'completed')
      const stepNo = done
      const where = isCounter ? ` · ${done} ${done === 1 ? 'step' : 'steps'}` : ` ${stepNo}/${total}`
      const tries = (currentTask?.failures ?? 0) >= 2 ? ` ×${currentTask.failures}` : ''
      const short = (text, max) => (text.length <= max ? text : `${text.slice(0, max - 1)}…`)
      const agentCount = track.agentRuns?.length ?? 0
      const agents = agentCount ? ` · ${agentCount} agent${agentCount === 1 ? '' : 's'}` : ''
      let pill = ''
      if (status === 'waiting') {
        pill = track.isPlanning ? 'Waiting for approval' : 'Waiting for you'
      } else if (status === 'failed') {
        pill = `✕ ${short(currentTask?.subject ?? 'Failed', 18)}${where}${tries}`
      } else if (isDone) {
        pill = `✓ Done${where}`
      } else {
        pill = `${short(currentTask?.subject ?? 'Tasks', 18)}${where}${agents}`
      }

      // Stats: upstream statsOf (:1236-1244). Work/wait are measured store-side; token and
      // context parts appear only once a real call has been counted, so nothing is invented.
      const percent = isCounter ? null : Math.round((done / total) * 100)
      const tk = track.tokens?.calls ? track.tokens : null
      const ctxPct = snap?.context?.percent ?? 0
      const stats = [
        percent != null ? `${percent}%` : null,
        `${durationOf(track.workMs || 0)} work`,
        (track.waitMs || 0) >= 1000 ? `${durationOf(track.waitMs)} wait` : null,
        tk ? `${countOf(tk.input || 0)} in · ${countOf(tk.output || 0)} out` : null,
        ctxPct >= 80 ? `context ${ctxPct}%` : null,
      ].filter(Boolean).join(' · ')

      const isExpanded = expandedIds.has(track.id)
      const toggleExpand = () => {
        setExpandedIds(prev => {
          const next = new Set(prev)
          if (next.has(track.id)) next.delete(track.id)
          else next.add(track.id)
          return next
        })
      }

      const handlePane = () => {
        host.revealPane?.('progress-tracker:pane')
      }

      const barSvgMarkup = barWidth > 0
        ? barSvg({
          width: barWidth,
          // an automatic bar has no total: full and moving, no ticks (upstream :1250-1252)
          done: isCounter ? 1 : done,
          total: isCounter ? 1 : total,
          status, pill, isMoving, skipped,
        })
        : ''

      // Timeline rows for expanded view (reuse verified timelineRows/timelineSvg)
      const rows = timelineRows(track)
      const gutterFor = (row, i) => jsx('span', {
        style: { lineHeight: 0, display: 'inline-flex', flexShrink: 0 },
        dangerouslySetInnerHTML: {
          __html: timelineSvg({ ...row, isFirst: i === 0, isLast: i === rows.length - 1, index: i }),
        },
      })

      return jsxs('div', {
        key: track.id,
        style: { display: 'flex', flexDirection: 'column', gap: 0 },
        children: [
          // Main row
          jsxs('div', {
            style: {
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '3px 12px',
              borderBottom: '1px solid var(--ui-stroke-secondary, #2a2a2a)',
              background: DARK_SURFACE_2,
              color: 'var(--ui-text-secondary, inherit)',
              fontSize: 11,
            },
            children: [
              jsx('span', {
                style: {
                  width: 6, height: 6, borderRadius: '50%',
                  background: PALETTE[status].dot, flex: '0 0 auto'
                }
              }),
              jsx('span', {
                style: { flex: '0 1 auto', minWidth: 0, maxWidth: '38%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', opacity: 0.85 },
                children: track.label || track.tasks[0]?.subject || 'Untitled'
              }),
              jsx('span', {
                ref: trackIndex === 0 ? barRef : undefined,
                style: { flex: '1 1 auto', minWidth: 140, height: SVG_HEIGHT, lineHeight: 0 },
                dangerouslySetInnerHTML: barWidth > 0 ? { __html: barSvgMarkup } : undefined,
              }),
              jsx('span', {
                style: { flex: '0 0 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', opacity: 0.7, fontSize: 10 },
                children: stats
              }),
              jsxs('div', {
                style: { display: 'flex', gap: 4, flexShrink: 0 },
                children: [
                  jsx('button', {
                    type: 'button',
                    onClick: toggleExpand,
                    style: {
                      padding: '2px 6px', fontSize: 10, border: 'none', background: 'transparent',
                      color: 'var(--ui-text-secondary)', cursor: 'pointer',
                    },
                    children: isExpanded ? '▼ Tasks' : '▶ Tasks'
                  }),
                  jsx('button', {
                    type: 'button',
                    onClick: handlePane,
                    style: {
                      padding: '2px 6px', fontSize: 10, border: 'none', background: 'transparent',
                      color: 'var(--ui-text-tertiary)', cursor: 'pointer',
                    },
                    children: '☰'
                  }),
                  jsx('button', {
                    type: 'button',
                    onClick: () => dismissTrack(track),
                    style: {
                      padding: '2px 6px', fontSize: 10, border: 'none', background: 'transparent',
                      color: 'var(--ui-text-tertiary)', cursor: 'pointer',
                    },
                    children: '✕'
                  }),
                ]
              }),
            ]
          }),
          // Expanded task lines (reuse timeline gutter + inline task rendering)
          isExpanded && jsxs('div', {
            style: { padding: '0 12px 8px', display: 'flex', flexDirection: 'column', gap: 0 },
            children: rows.map((row, i) => row.isSubAgent
              ? jsx('div', {
                  key: row.key,
                  title: row.subject,
                  style: { height: TL_ROW_HEIGHT, display: 'flex', alignItems: 'center', paddingLeft: 28 },
                  children: gutterFor(row, i),
                })
              : jsxs('div', {
                  key: row.key,
                  style: { height: TL_ROW_HEIGHT, display: 'flex', alignItems: 'center', gap: 8, paddingLeft: 8 },
                  children: [
                    gutterFor(row, i),
                    jsx('span', {
                      style: { fontSize: 14, width: 20, textAlign: 'center' },
                      children: row.isSkipped ? '–' : row.status === 'completed' ? '✓' : row.status === 'in_progress' ? '▸' : '○'
                    }),
                    jsx('span', {
                      style: { flex: 1, fontSize: 12, textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' },
                      children: row.subject
                    }),
                    row.failures > 0 && jsx('span', {
                      style: { fontSize: 11, color: 'var(--ui-error-foreground)', fontFamily: 'var(--font-mono, ui-monospace)' },
                      children: `×${row.failures}`
                    }),
                    jsx('span', {
                      style: { fontSize: 10, color: 'var(--ui-text-tertiary)', fontFamily: 'var(--font-mono, ui-monospace)' },
                      children: row.startedAt ? new Date(row.startedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—'
                    }),
                  ]
                })
            )
          }),
        ]
      })
    })
  })
}

// ─── Status bar chip (always visible, no pane needed) ───
function ProgressChip({ ctx }) {
  const session = useValue(
    host.state.focusedStoredSessionId || host.state.activeSessionId
  )
  const [snap, setSnap] = useState(null)
  useEffect(() => {
    if (!session) { setSnap(null); return }
    let alive = true
    async function tick() {
      try {
        const res = await ctx.rest(
          `/snapshot?session=${encodeURIComponent(session)}`,
          { timeoutMs: 4000 }
        )
        if (alive) setSnap(res)
      } catch (e) {
        if (alive) setSnap(null)
      }
    }
    tick()
    const id = setInterval(tick, 3000)
    return () => { alive = false; clearInterval(id) }
  }, [ctx, session])

  const track = (snap?.tracks ?? []).findLast(t => !t.isDismissed && t.tasks?.length > 0)
  if (!track) return null

  const { done, total } = getTrackProgress(track)
  const task = track.tasks.find(t => t.id === track.activeTaskId)
  const failed = Boolean(track.failedCommand)
  const color = failed ? '#ef6f5c' : (total > 0 && done === total ? '#8fd19e' : '#aa9df6')
  const label = task ? (task.subject || task.label || '') : (total > 0 && done === total ? 'done' : 'idle')

  return jsx('button', {
    onClick: () => {
      if (typeof host.revealPane === 'function') host.revealPane('progress-tracker:pane')
      else host.notify({ kind: 'info', message: 'Progress: open the pane from the layout tabs (right side).' })
    },
    title: `${done}/${total} — ${label}`,
    style: {
      display: 'inline-flex', alignItems: 'center', gap: 6, maxWidth: 220,
      padding: '2px 6px', background: 'transparent', border: 'none',
      color: 'inherit', font: 'inherit', fontSize: 11, cursor: 'pointer',
    },
    children: [
      jsx('span', { key: 'd', style: { width: 6, height: 6, borderRadius: '50%', background: color, flex: '0 0 auto' } }),
      jsx('span', { key: 'n', style: { fontVariantNumeric: 'tabular-nums', opacity: 0.85, flex: '0 0 auto' }, children: `${done}/${total}` }),
      jsx('span', { key: 't', style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', opacity: 0.7 }, children: label }),
    ],
  })
}

// ─── Palette command + pane registration ───
export default {
  id: 'progress-tracker',
  name: 'Progress Tracker',
  defaultEnabled: true,
  register(ctx) {
    // Claude Code style band above the chat input
    ctx.register({
      id: 'band',
      area: COMPOSER_AREAS.top,
      order: 10,
      render: () => jsx(ProgressBand, { ctx })
    })

    // Status bar chip: the always-on readout (survives a closed pane)
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 60,
      render: () => jsx(ProgressChip, { ctx })
    })

    // Sidebar pane (right rail)
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Progress',
      data: { placement: 'right', width: '360px' },
      render: () => jsx(ProgressPane, { ctx })
    })

    // Palette command: Show Progress
    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: 'progress-tracker.open',
        label: 'Show Progress',
        keywords: ['progress', 'tracker', 'tasks', 'turns'],
        run: () => {
          if (typeof host.revealPane === 'function') {
            host.revealPane('progress-tracker:pane')
          } else {
            host.notify({ kind: 'info', message: 'Open the Progress pane from the layout tabs (right side).' })
          }
        }
      }
    })

    // Palette command: Play progress demo
    ctx.register({
      id: 'demo',
      area: PALETTE_AREA,
      data: {
        id: 'progress-tracker.demo',
        label: 'Play progress demo',
        keywords: ['progress', 'demo', 'tracker'],
        run: async () => {
          const session = host.state.focusedStoredSessionId || host.state.activeSessionId
          if (!session) {
            host.notify({ kind: 'warning', message: 'Open a chat first to run the demo.' })
            return
          }
          try {
            const demo = await ctx.rest(`/demo`, { timeoutMs: 5000 })
            // Create the demo track
            await ctx.rest('/op', {
              method: 'POST',
              body: { session, op: 'title', args: { title: demo.title, tasks: demo.tasks } },
              timeoutMs: 5000,
            })
            // Play each step with its delay
            for (const step of demo.steps) {
              if (step.delayMs > 0) {
                await new Promise(r => setTimeout(r, step.delayMs))
              }
              await ctx.rest('/op', {
                method: 'POST',
                body: { session, op: step.op, args: step.args },
                timeoutMs: 5000,
              })
            }
            // Track auto-dismisses after linger (handled by TrackGroup effect)
          } catch (e) {
            host.notify({ kind: 'error', message: `Demo failed: ${e.message}` })
          }
        }
      }
    })
  }
}

// ─── Global style injection for pulse-ring animation ───
if (typeof document !== 'undefined' && !document.getElementById('progress-tracker-styles')) {
  const style = document.createElement('style')
  style.id = 'progress-tracker-styles'
  style.textContent = `
@keyframes pulse-ring {
  0% { transform: scale(1); opacity: 0.8; }
  100% { transform: scale(1.12); opacity: 0; }
}
`
  document.head.appendChild(style)
}