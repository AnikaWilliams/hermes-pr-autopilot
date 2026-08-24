import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Loader,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  SearchField,
  StatusDot,
  Switch,
  host,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'pr-autopilot'
const ROUTE = '/pr-autopilot'
const OVERVIEW_QUERY_KEY = [ID, 'overview']

const pageStyle = {
  boxSizing: 'border-box',
  display: 'flex',
  flexDirection: 'column',
  gap: '28px',
  margin: '0 auto',
  maxWidth: '1180px',
  padding: '24px 28px',
  paddingBottom: '96px',
  width: '100%'
}

const sectionStyle = {
  borderTop: '1px solid var(--ui-stroke-tertiary)',
  display: 'flex',
  flexDirection: 'column',
  gap: '12px',
  paddingTop: '18px'
}

const listStyle = {
  display: 'flex',
  flexDirection: 'column'
}

const rowStyle = {
  alignItems: 'center',
  borderBottom: '1px solid var(--ui-stroke-tertiary)',
  display: 'grid',
  gap: '18px',
  gridTemplateColumns: 'minmax(220px, 1.2fr) minmax(260px, 1fr) auto',
  minHeight: '62px',
  padding: '10px 2px'
}

const mutedStyle = { color: 'var(--ui-text-secondary)' }
const tertiaryStyle = { color: 'var(--ui-text-tertiary)' }

function formatRelativeTime(value) {
  if (!value) return 'not yet'
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return 'time unavailable'
  const deltaSeconds = Math.round((timestamp - Date.now()) / 1000)
  const absoluteSeconds = Math.abs(deltaSeconds)
  const units = [
    ['day', 86_400],
    ['hour', 3_600],
    ['minute', 60]
  ]
  for (const [unit, seconds] of units) {
    if (absoluteSeconds >= seconds) {
      return new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
        .format(Math.round(deltaSeconds / seconds), unit)
    }
  }
  return 'just now'
}

function workflowStatus(pullRequest) {
  const status = pullRequest.workflow_status ?? pullRequest.observed_stage_status
  const labels = {
    archived: 'Worker complete',
    blocked: 'Blocked',
    completed: 'Worker complete',
    done: 'Worker complete',
    failed: 'Failed',
    ready: 'Ready',
    running: 'Worker running',
    scheduled: 'Queued',
    unknown: 'Needs attention'
  }
  if (status && labels[status]) return labels[status]
  if (pullRequest.requested_at) return 'Awaiting Codex review'
  if (pullRequest.pending_findings) return 'Findings ready'
  return 'Observed'
}

function statusTone(status) {
  if (['Blocked', 'Failed', 'Needs attention'].includes(status)) return 'bad'
  if (['Awaiting Codex review', 'Queued', 'Findings ready'].includes(status)) return 'warn'
  if (['Ready', 'Worker complete'].includes(status)) return 'good'
  return 'muted'
}

function stageState(stage) {
  if (stage.status) return stage.status
  return stage.bound ? 'waiting' : 'not_started'
}

function stageTone(state) {
  if (['done', 'completed', 'succeeded'].includes(state)) return 'good'
  if (['blocked', 'failed', 'interrupted'].includes(state)) return 'bad'
  if (['running', 'analyzing', 'fixing', 'verifying'].includes(state)) return 'warn'
  return 'muted'
}

function stageStateLabel(state) {
  return {
    blocked: 'blocked',
    completed: 'complete',
    done: 'complete',
    failed: 'failed',
    interrupted: 'interrupted',
    not_started: 'not started',
    ready: 'ready',
    running: 'running',
    scheduled: 'waiting',
    succeeded: 'complete',
    waiting: 'waiting'
  }[state] ?? 'unknown'
}

function mergeStatusLabel(status) {
  return {
    invalidated: 'Invalidated',
    pending_confirmation: 'Confirming exact head',
    pending_history: 'Saving audit record',
    recorded: 'Recorded'
  }[status] ?? 'Unknown'
}

function Section({ action, children, description, title }) {
  return jsxs('section', {
    style: sectionStyle,
    children: [
      jsxs('div', {
        style: { alignItems: 'end', display: 'flex', gap: '16px', justifyContent: 'space-between' },
        children: [
          jsxs('div', {
            children: [
              jsx('h2', { style: { fontSize: '17px', margin: 0 }, children: title }),
              description ? jsx('div', { style: { fontSize: '13px', marginTop: '3px', ...mutedStyle }, children: description }) : null
            ]
          }),
          action ?? null
        ]
      }),
      children
    ]
  })
}

function StageProgress({ pipeline }) {
  return jsx('div', {
    'aria-label': 'Analyze, Fix, Verify progress',
    style: { alignItems: 'center', display: 'flex', flexWrap: 'wrap', gap: '12px' },
    children: pipeline.map(stage => {
      const state = stageState(stage)
      return jsxs('span', {
        style: { alignItems: 'center', display: 'inline-flex', fontSize: '12px', gap: '6px' },
        children: [
          jsx(StatusDot, { tone: stageTone(state) }),
          jsx('span', { children: stage.role }),
          jsx('span', { style: tertiaryStyle, children: stageStateLabel(state) })
        ]
      }, stage.role)
    })
  })
}

function Pipeline({ openExternal, pullRequest, onRetry, retryDisabled }) {
  const status = workflowStatus(pullRequest)
  return jsxs('div', {
    style: rowStyle,
    children: [
      jsxs('div', {
        style: { minWidth: 0 },
        children: [
          jsx('a', {
            href: pullRequest.url,
            onClick: event => {
              event.preventDefault()
              void openExternal(pullRequest.url)
            },
            style: { color: 'var(--ui-accent)', fontWeight: 600, textDecoration: 'none' },
            children: `${pullRequest.repository} #${pullRequest.number}`
          }),
          jsxs('div', {
            style: { fontFamily: 'var(--font-mono)', fontSize: '12px', marginTop: '4px', ...mutedStyle },
            children: [pullRequest.head_short, ` · ${pullRequest.review_rounds} review ${pullRequest.review_rounds === 1 ? 'round' : 'rounds'}`]
          })
        ]
      }),
      jsx(StageProgress, { pipeline: pullRequest.pipeline }),
      jsxs('div', {
        style: { alignItems: 'flex-end', display: 'flex', flexDirection: 'column', gap: '4px', textAlign: 'right' },
        children: [
          jsxs('span', {
            style: { alignItems: 'center', display: 'inline-flex', fontSize: '13px', fontWeight: 600, gap: '7px' },
            children: [jsx(StatusDot, { tone: statusTone(status) }), status]
          }),
          jsx('span', {
            style: { fontSize: '12px', ...mutedStyle },
            children: pullRequest.requested_at
              ? `Review requested ${formatRelativeTime(pullRequest.requested_at)}`
              : 'No review request is active'
          }),
          pullRequest.resettable
            ? jsx(Button, {
              disabled: retryDisabled,
              onClick: () => void onRetry(pullRequest),
              type: 'button',
              variant: 'outline',
              children: 'Retry pipeline'
            })
            : null
        ]
      })
    ]
  })
}

function Repository({ disabled, hard_excluded: hardExcluded, mutable, repository, onRequest, pending }) {
  const active = !disabled && !hardExcluded
  return jsxs('div', {
    style: { ...rowStyle, gridTemplateColumns: 'minmax(280px, 1fr) auto' },
    children: [
      jsxs('div', {
        children: [
          jsx('div', { style: { fontWeight: 600 }, children: repository }),
          jsx('div', {
            style: { fontSize: '12px', marginTop: '3px', ...mutedStyle },
            children: hardExcluded
              ? 'Excluded by the controller policy'
              : (active ? 'New eligible pull requests can enter the review loop' : 'New controller work is paused for this repository')
          })
        ]
      }),
      hardExcluded
        ? jsx(Badge, { children: 'Policy exclusion' })
        : jsx(Switch, {
          'aria-label': `${active ? 'Disable' : 'Enable'} PR Autopilot for ${repository}`,
          checked: active,
          disabled: pending || !mutable,
          onCheckedChange: checked => void onRequest({ disabled: !checked, repository }),
          size: 'xs'
        })
    ]
  })
}

function MergeHistory({ openExternal, record }) {
  return jsxs('div', {
    style: { ...rowStyle, gridTemplateColumns: 'minmax(280px, 1fr) auto' },
    children: [
      jsxs('div', {
        style: { minWidth: 0 },
        children: [
          jsxs('div', {
            children: [
              jsx('a', {
                href: record.url,
                onClick: event => {
                  event.preventDefault()
                  void openExternal(record.url)
                },
                style: { color: 'var(--ui-accent)', fontWeight: 600, textDecoration: 'none' },
                children: `${record.repository} #${record.number}`
              }),
              record.title ? ` · ${record.title}` : ''
            ]
          }),
          jsx('div', {
            style: { fontFamily: 'var(--font-mono)', fontSize: '12px', marginTop: '4px', ...mutedStyle },
            children: `${record.head_short} · merged ${formatRelativeTime(record.merged_at)}`
          })
        ]
      }),
      jsx(Badge, { children: mergeStatusLabel(record.status) })
    ]
  })
}

function Summary({ data }) {
  const activeRepositories = data.repositories.filter(repository => !repository.disabled && !repository.hard_excluded).length
  const awaitingReview = data.pull_requests.filter(pullRequest => workflowStatus(pullRequest) === 'Awaiting Codex review').length
  const items = [
    ['Runtime', data.runtime?.status_label ?? 'Snapshot only'],
    ['Tracked PRs', data.pull_requests.length],
    ['Awaiting review', awaitingReview],
    ['Active repositories', activeRepositories]
  ]
  return jsx('div', {
    style: {
      borderBottom: '1px solid var(--ui-stroke-tertiary)',
      borderTop: '1px solid var(--ui-stroke-tertiary)',
      display: 'grid',
      gap: '18px',
      gridTemplateColumns: 'repeat(4, minmax(120px, 1fr))',
      padding: '15px 2px'
    },
    children: items.map(([label, value]) => jsxs('div', {
      children: [
        jsx('div', { style: { fontSize: '11px', letterSpacing: '0.06em', textTransform: 'uppercase', ...tertiaryStyle }, children: label }),
        jsx('div', { style: { fontSize: '16px', fontWeight: 600, marginTop: '4px' }, children: value })
      ]
    }, label))
  })
}

function PRAutopilotDashboard({ openExternal, rest }) {
  const queryClient = useQueryClient()
  const [pendingAction, setPendingAction] = useState(null)
  const [pendingPipelineRetry, setPendingPipelineRetry] = useState(null)
  const [controllerAction, setControllerAction] = useState(null)
  const [repositorySearch, setRepositorySearch] = useState('')
  const overview = useQuery({
    queryKey: OVERVIEW_QUERY_KEY,
    queryFn: () => rest('/overview', { timeoutMs: 10_000 }),
    refetchInterval: 15_000
  })
  const createIntent = useMutation({
    mutationFn: input => rest('/repository-intents', {
      body: input,
      method: 'POST',
      timeoutMs: 10_000
    })
  })
  const createPipelineRetry = useMutation({
    mutationFn: input => rest('/pipeline-reset-intents', {
      body: input,
      method: 'POST',
      timeoutMs: 10_000
    })
  })
  const requestCheck = useMutation({
    mutationFn: () => rest('/controller/check', { method: 'POST', timeoutMs: 10_000 })
  })
  const setPause = useMutation({
    mutationFn: paused => rest('/controller/pause', {
      body: { paused },
      method: 'POST',
      timeoutMs: 10_000
    })
  })

  async function checkNow() {
    try {
      await requestCheck.mutateAsync()
      await queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY_KEY })
      host.notify({
        kind: 'success',
        message: 'The controller will check eligible pull requests now.',
        title: 'PR Autopilot'
      })
    } catch {
      host.notify({
        kind: 'error',
        message: 'The controller check could not be requested.',
        title: 'PR Autopilot'
      })
    }
  }

  async function confirmControllerAction() {
    if (!controllerAction) return
    try {
      const result = await setPause.mutateAsync(controllerAction.paused)
      queryClient.setQueryData(OVERVIEW_QUERY_KEY, current => current
        ? {
          ...current,
          controller: { ...current.controller, paused: result.paused },
          runtime: {
            ...current.runtime,
            paused: result.paused,
            status_label: result.paused ? 'Paused' : 'Running'
          }
        }
        : current)
      void queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY_KEY })
      host.notify({
        kind: 'success',
        message: controllerAction.paused
          ? 'Automatic controller checks are paused. Active workers were not cancelled.'
          : 'Automatic controller checks are active.',
        title: 'PR Autopilot'
      })
    } catch {
      throw new Error('The controller state was not changed. Refresh the page and try again.')
    }
  }

  async function requestRepositoryAction(action) {
    try {
      const response = await createIntent.mutateAsync({
        ...action,
        idempotency_key: crypto.randomUUID()
      })
      setPendingAction(response.intent)
    } catch {
      host.notify({
        kind: 'error',
        message: 'The repository change could not be prepared. Refresh the page and try again.',
        title: 'PR Autopilot'
      })
    }
  }

  async function confirmRepositoryAction() {
    if (!pendingAction) return
    try {
      const result = await rest(`/repository-intents/${pendingAction.id}/confirm`, {
        method: 'POST',
        timeoutMs: 10_000
      })
      queryClient.setQueryData(OVERVIEW_QUERY_KEY, current => current
        ? {
          ...current,
          repositories: current.repositories.map(repository => (
            repository.repository.toLowerCase() === result.repository.toLowerCase()
              ? { ...repository, disabled: result.disabled }
              : repository
          ))
        }
        : current)
      void queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY_KEY })
      host.notify({
        kind: 'success',
        message: `${result.repository} is now ${result.disabled ? 'paused' : 'active'} locally.`,
        title: 'PR Autopilot'
      })
    } catch {
      throw new Error('The repository change was not applied. Refresh the page and try again.')
    }
  }

  async function requestPipelineRetry(pullRequest) {
    try {
      const response = await createPipelineRetry.mutateAsync({
        idempotency_key: crypto.randomUUID(),
        number: pullRequest.number,
        repository: pullRequest.repository
      })
      setPendingPipelineRetry(response.intent)
    } catch {
      host.notify({
        kind: 'error',
        message: 'The blocked pipeline cannot be retried. Pause the controller and refresh the page.',
        title: 'PR Autopilot'
      })
    }
  }

  async function confirmPipelineRetry() {
    if (!pendingPipelineRetry) return
    try {
      const result = await rest(`/pipeline-reset-intents/${pendingPipelineRetry.id}/confirm`, {
        method: 'POST',
        timeoutMs: 10_000
      })
      setPendingPipelineRetry(null)
      await queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY_KEY })
      host.notify({
        kind: 'success',
        message: `${result.repository} #${result.number} will retry when you resume PR Autopilot.`,
        title: 'PR Autopilot'
      })
    } catch {
      throw new Error('The pipeline was not retried. Refresh the page and try again.')
    }
  }

  if (overview.isLoading) {
    return jsxs('div', {
      style: { ...pageStyle, alignItems: 'center', minHeight: '320px', justifyContent: 'center' },
      children: [
        jsx(Loader, { label: 'Loading PR Autopilot', type: 'lemniscate-bloom' }),
        jsx('div', { style: mutedStyle, children: 'Reading controller state' })
      ]
    })
  }

  if (overview.error || !overview.data) {
    return jsxs('div', {
      style: pageStyle,
      children: [
        jsx('h1', { style: { margin: 0 }, children: 'PR Autopilot' }),
        jsx(ErrorState, {
          description: 'The local controller state could not be read. The review loop remains fail-closed.',
          title: 'PR Autopilot is unavailable'
        }),
        jsx(Button, {
          onClick: () => void overview.refetch(),
          type: 'button',
          variant: 'outline',
          children: 'Retry'
        })
      ]
    })
  }

  const data = overview.data
  const query = repositorySearch.trim().toLowerCase()
  const visibleRepositories = query
    ? data.repositories.filter(repository => repository.repository.toLowerCase().includes(query))
    : data.repositories
  const truncatedSections = Object.entries(data.truncated ?? {})
    .filter(([, truncated]) => truncated)
    .map(([section]) => section.replaceAll('_', ' '))
  const confirmationTitle = pendingAction?.disabled
    ? `Pause ${pendingAction.repository}?`
    : `Activate ${pendingAction?.repository ?? ''}?`
  const confirmationDescription = pendingAction?.disabled
    ? `PR Autopilot will not start new work for ${pendingAction.repository}. Work already in progress is not cancelled.`
    : `PR Autopilot can process new eligible pull requests for ${pendingAction?.repository ?? ''} under policy revision ${data.policy.revision}.`

  return jsxs('div', {
    style: pageStyle,
    children: [
      jsxs('header', {
        style: { alignItems: 'center', display: 'flex', flexWrap: 'wrap', gap: '14px', justifyContent: 'space-between' },
        children: [
          jsxs('div', {
            children: [
              jsxs('div', {
                style: { alignItems: 'center', display: 'flex', gap: '9px' },
                children: [
                  jsx(StatusDot, { tone: data.health?.overall === 'ok' ? 'good' : 'warn' }),
                  jsx('h1', { style: { fontSize: '25px', margin: 0 }, children: 'PR Autopilot' })
                ]
              }),
              jsx('div', {
                style: { fontSize: '13px', marginTop: '5px', ...mutedStyle },
                children: `Exact-head review policy ${data.policy.revision} · updated ${formatRelativeTime(data.generated_at)}`
              }),
              data.controller?.last_cycle
                ? jsx('div', {
                  style: { fontSize: '12px', marginTop: '3px', ...tertiaryStyle },
                  children: `Last check ${data.controller.last_cycle.outcome} ${formatRelativeTime(data.controller.last_cycle.finished_at)} · ${data.controller.last_cycle.event_count} state changes`
                })
                : null
            ]
          }),
          jsxs('div', {
            style: { alignItems: 'center', display: 'flex', gap: '8px' },
            children: [
              jsx(Button, {
                disabled: requestCheck.isPending || data.runtime?.paused,
                onClick: () => void checkNow(),
                type: 'button',
                variant: 'primary',
                children: requestCheck.isPending ? 'Requesting…' : 'Check now'
              }),
              jsx(Button, {
                disabled: setPause.isPending,
                onClick: () => setControllerAction({ paused: !data.runtime?.paused }),
                type: 'button',
                variant: 'outline',
                children: data.runtime?.paused ? 'Resume' : 'Pause'
              }),
              jsx(Button, {
                disabled: overview.isFetching,
                onClick: () => void overview.refetch(),
                type: 'button',
                variant: 'outline',
                children: overview.isFetching ? 'Refreshing…' : 'Refresh'
              })
            ]
          })
        ]
      }),
      jsx(Summary, { data }),
      truncatedSections.length
        ? jsx('div', {
          role: 'status',
          style: { fontSize: '12px', ...mutedStyle },
          children: `This is a bounded view. More ${truncatedSections.join(', ')} exist.`
        })
        : null,
      jsx(Section, {
        title: 'Review queue',
        description: 'Each stage is status, not a button. Open the PR to inspect the current exact head.',
        children: data.pull_requests.length
          ? jsx('div', {
            style: listStyle,
            children: data.pull_requests.map(pullRequest => jsx(Pipeline, {
              openExternal,
              onRetry: requestPipelineRetry,
              pullRequest,
              retryDisabled: createPipelineRetry.isPending || !data.runtime?.paused
            }, `${pullRequest.repository}#${pullRequest.number}`))
          })
          : jsx(EmptyState, {
            description: 'Eligible authored pull requests appear here after the next controller check.',
            title: 'No tracked pull requests'
          })
      }),
      jsx(Section, {
        title: 'Repositories',
        description: 'Use the switch to stop or allow new controller work for one repository.',
        action: data.repositories.length
          ? jsx(SearchField, {
            'aria-label': 'Search repositories',
            onChange: setRepositorySearch,
            placeholder: 'Search repositories',
            value: repositorySearch
          })
          : null,
        children: visibleRepositories.length
          ? jsx('div', {
            style: listStyle,
            children: visibleRepositories.map(repository => jsx(Repository, {
              ...repository,
              onRequest: requestRepositoryAction,
              pending: createIntent.isPending
            }, repository.repository))
          })
          : jsx(EmptyState, {
            description: query ? 'Clear the search to see every repository.' : 'Repository state appears after a pull request is observed.',
            title: query ? 'No repository matches' : 'No repository state'
          })
      }),
      jsx(Section, {
        title: 'Merged by PR Autopilot',
        description: 'Local exact-head audit records. These are not general GitHub merge history.',
        children: data.merge_history.length
          ? jsx('div', {
            style: listStyle,
            children: data.merge_history.map(record => jsx(MergeHistory, {
              openExternal,
              record
            }, `${record.repository}#${record.number}:${record.head_short}`))
          })
          : jsx(EmptyState, {
            description: 'A record appears only after an exact-head controller merge is confirmed.',
            title: 'No Autopilot merges'
          })
      }),
      jsx(ConfirmDialog, {
        busyLabel: 'Applying repository setting…',
        confirmLabel: pendingAction?.disabled ? 'Pause repository' : 'Activate repository',
        description: confirmationDescription,
        destructive: Boolean(pendingAction?.disabled),
        onClose: () => setPendingAction(null),
        onConfirm: confirmRepositoryAction,
        open: Boolean(pendingAction),
        title: confirmationTitle
      }),
      jsx(ConfirmDialog, {
        busyLabel: controllerAction?.paused ? 'Pausing controller…' : 'Resuming controller…',
        confirmLabel: controllerAction?.paused ? 'Pause controller' : 'Resume controller',
        description: controllerAction?.paused
          ? 'PR Autopilot will stop automatic checks. Active workers will continue under their current exact-head policy.'
          : 'PR Autopilot will resume automatic checks and process eligible authored pull requests.',
        destructive: Boolean(controllerAction?.paused),
        onClose: () => setControllerAction(null),
        onConfirm: confirmControllerAction,
        open: Boolean(controllerAction),
        title: controllerAction?.paused ? 'Pause PR Autopilot?' : 'Resume PR Autopilot?'
      }),
      jsx(ConfirmDialog, {
        busyLabel: 'Scheduling pipeline retry…',
        confirmLabel: 'Retry exact-head pipeline',
        description: 'This resets only the current blocked local pipeline to its first stage. The controller stays paused. Resume it only after you fix the blocking cause and confirm the pull request still has this exact head.',
        destructive: true,
        onClose: () => setPendingPipelineRetry(null),
        onConfirm: confirmPipelineRetry,
        open: Boolean(pendingPipelineRetry),
        title: pendingPipelineRetry
          ? `Retry ${pendingPipelineRetry.repository} #${pendingPipelineRetry.number}?`
          : 'Retry blocked pipeline?'
      })
    ]
  })
}

export default {
  id: ID,
  name: 'PR Autopilot',
  description: 'Observe a local PR review loop and manage repository off-switches.',
  defaultEnabled: false,
  register(ctx) {
    ctx.registerMany([
      {
        id: 'dashboard',
        area: ROUTES_AREA,
        title: 'PR Autopilot',
        data: { path: ROUTE },
        render: () => jsx(PRAutopilotDashboard, {
          openExternal: ctx.os.openExternal,
          rest: ctx.rest
        })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 51,
        data: {
          codicon: 'git-pull-request',
          label: 'PR Autopilot',
          path: ROUTE
        }
      },
      {
        id: 'open-dashboard',
        area: PALETTE_AREA,
        data: {
          id: 'pr-autopilot.open-dashboard',
          label: 'Open PR Autopilot',
          keywords: ['pull request', 'pr', 'autopilot', 'review'],
          run: () => host.navigate(ROUTE)
        }
      }
    ])
  }
}
