import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const pluginPath = path.join(projectRoot, 'desktop', 'plugin.js')

function dataModule(source) {
  return `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
}

const sdkStub = dataModule(`
  export const PALETTE_AREA = 'palette'
  export const ROUTES_AREA = 'routes'
  export const SIDEBAR_NAV_AREA = 'sidebar.nav'
  export const testState = globalThis.__prAutopilotSdkTestState ??= {}
  export function reset(data) {
    Object.assign(testState, {
      data,
      invalidateCalls: [],
      navigationCalls: [],
      notifications: [],
      restCalls: [],
      setQueryDataCalls: []
    })
    testState.query = {
      data,
      error: null,
      isFetching: false,
      isLoading: false,
      refetch: async () => { testState.refetched = true }
    }
  }
  export const host = {
    navigate: route => testState.navigationCalls.push(route),
    notify: notice => testState.notifications.push(notice)
  }
  export const Badge = props => ({ type: 'badge', props })
  export const Button = props => ({ type: 'button', props })
  export const ConfirmDialog = props => ({ type: 'confirm-dialog', props })
  export const EmptyState = props => ({ type: 'empty-state', props })
  export const ErrorState = props => ({ type: 'error-state', props })
  export const Loader = props => ({ type: 'loader', props })
  export const SearchField = props => ({ type: 'search-field', props })
  export const StatusDot = props => ({ type: 'status-dot', props })
  export const Switch = props => ({ type: 'switch', props })
  export const useMutation = options => ({
    isPending: false,
    mutateAsync: async input => options.mutationFn(input)
  })
  export const useQuery = () => testState.query
  export const useQueryClient = () => ({
    invalidateQueries: async input => testState.invalidateCalls.push(input),
    setQueryData: (key, updater) => {
      testState.setQueryDataCalls.push(key)
      testState.query.data = updater(testState.query.data)
    }
  })
`)
const reactStub = dataModule(`
  export const testState = globalThis.__prAutopilotReactTestState ??= {}
  export function reset() { testState.values = [] }
  export function beginRender() { testState.cursor = 0 }
  export const useState = initial => {
    const index = testState.cursor++
    if (testState.values[index] === undefined) testState.values[index] = initial
    return [
      testState.values[index],
      value => { testState.values[index] = typeof value === 'function' ? value(testState.values[index]) : value }
    ]
  }
`)
const jsxStub = dataModule(`
  export const jsx = (type, props) => ({ type, props })
  export const jsxs = jsx
`)

function importPlugin(source) {
  const rewritten = source
    .replace(/from\s+(['"])@hermes\/plugin-sdk\1/g, `from '${sdkStub}'`)
    .replace(/from\s+(['"])react\1/g, `from '${reactStub}'`)
    .replace(/from\s+(['"])react\/jsx-runtime\1/g, `from '${jsxStub}'`)
  return import(dataModule(rewritten))
}

function renderTree(element) {
  if (Array.isArray(element)) return element.map(renderTree)
  if (element === null || typeof element !== 'object') return element
  if (typeof element.type === 'function') return renderTree(element.type(element.props ?? {}))
  return {
    ...element,
    props: {
      ...element.props,
      children: renderTree(element.props?.children)
    }
  }
}

function findAll(element, predicate, matches = []) {
  if (Array.isArray(element)) {
    for (const child of element) findAll(child, predicate, matches)
    return matches
  }
  if (element === null || typeof element !== 'object') return matches
  if (predicate(element)) matches.push(element)
  findAll(element.props?.children, predicate, matches)
  return matches
}

function dashboardData(paused) {
  return {
    controller: {},
    generated_at: null,
    health: { overall: 'ok' },
    merge_history: [],
    policy: { revision: 'test-policy' },
    pull_requests: [],
    repositories: [],
    runtime: { paused, status_label: paused ? 'Paused' : 'Running' },
    truncated: {}
  }
}

function retryDashboardData(paused) {
  const data = dashboardData(paused)
  data.pull_requests = [{
    repository: 'AnikaWilliams/example',
    number: 18,
    url: 'https://github.com/AnikaWilliams/example/pull/18',
    head_short: 'aaaaaaaaaaaa',
    review_rounds: 2,
    requested_at: null,
    observed_stage_status: 'blocked',
    pending_findings: true,
    resettable: true,
    pipeline: [
      { role: 'Analyze', bound: true, status: 'done' },
      { role: 'Fix', bound: true, status: 'blocked' },
      { role: 'Verify', bound: true, status: 'blocked' }
    ]
  }]
  return data
}

function repositoryDashboardData(disabled = false) {
  const data = dashboardData(false)
  data.repositories = [{
    repository: 'AnikaWilliams/example',
    disabled,
    hard_excluded: false,
    mutable: true
  }]
  return data
}

async function dashboard(source, data) {
  const [sdk, react, pluginModule] = await Promise.all([
    import(sdkStub),
    import(reactStub),
    importPlugin(source)
  ])
  sdk.reset(data)
  react.reset()
  const registrations = []
  let repositoryIntent
  const rest = async (endpoint, options = {}) => {
    sdk.testState.restCalls.push({ endpoint, options })
    if (endpoint === '/controller/pause') return { paused: options.body.paused }
    if (endpoint === '/repository-intents') {
      repositoryIntent = {
        repository: options.body.repository,
        disabled: options.body.disabled
      }
      return {
        intent: {
          id: 'repository-intent',
          ...repositoryIntent
        }
      }
    }
    if (endpoint === '/repository-intents/repository-intent/confirm') {
      return { ...repositoryIntent, changed: true }
    }
    if (endpoint === '/pipeline-reset-intents') {
      return { intent: { id: 'retry-intent', repository: 'AnikaWilliams/example', number: 18 } }
    }
    if (endpoint === '/pipeline-reset-intents/retry-intent/confirm') {
      return { retried: true, repository: 'AnikaWilliams/example', number: 18 }
    }
    return {}
  }
  pluginModule.default.register({
    os: { openExternal: () => Promise.resolve(true) },
    registerMany(entries) { registrations.push(...entries) },
    rest
  })
  const route = registrations.find(entry => entry.area === 'routes')
  return {
    registrations,
    sdk,
    render() {
      react.beginRender()
      return renderTree(route.render())
    }
  }
}

function control(tree, type, label) {
  const match = findAll(tree, element => (
    element.type === type && element.props?.children === label
  ))
  assert.equal(match.length, 1, `expected one ${label} ${type}`)
  return match[0]
}

function openControllerDialog(tree) {
  const match = findAll(tree, element => (
    element.type === 'confirm-dialog' && element.props?.open
  ))
  assert.equal(match.length, 1, 'expected one open controller confirmation dialog')
  return match[0]
}

test('registers the PR Autopilot workspace route, Kanban-adjacent navigation, and palette command', async () => {
  const source = await readFile(pluginPath, 'utf8')
  const imports = [...source.matchAll(/from\s+['"]([^'"]+)['"]/g)].map(match => match[1])
  assert.deepEqual([...new Set(imports)].sort(), [
    '@hermes/plugin-sdk',
    'react',
    'react/jsx-runtime'
  ])

  const plugin = (await importPlugin(source)).default
  const sdk = await import(sdkStub)
  sdk.reset(dashboardData(false))
  assert.equal(plugin.id, 'pr-autopilot')
  assert.equal(plugin.defaultEnabled, false)

  const registrations = []
  const openExternal = () => Promise.resolve(true)
  plugin.register({
    registerMany(entries) {
      registrations.push(...entries)
    },
    os: { openExternal }
  })

  const route = registrations.find(entry => entry.area === 'routes')
  assert.deepEqual(route.data, { path: '/pr-autopilot' })
  assert.equal(typeof route.render, 'function')
  assert.equal(route.render().props.openExternal, openExternal)

  const navigation = registrations.find(entry => entry.area === 'sidebar.nav')
  assert.equal(navigation.order, 51)
  assert.deepEqual(navigation.data, {
    codicon: 'git-pull-request',
    label: 'PR Autopilot',
    path: '/pr-autopilot'
  })

  const palette = registrations.find(entry => entry.area === 'palette')
  assert.equal(palette.data.label, 'Open PR Autopilot')
  assert.equal(typeof palette.data.run, 'function')
  palette.data.run()
  assert.deepEqual(sdk.testState.navigationCalls, ['/pr-autopilot'])
})

test('imports the desktop module when its imports use double quotes', async () => {
  const source = await readFile(pluginPath, 'utf8')
  const doubleQuotedImports = source
    .replace(/from '@hermes\/plugin-sdk'/g, 'from "@hermes/plugin-sdk"')
    .replace(/from 'react'/g, 'from "react"')
    .replace(/from 'react\/jsx-runtime'/g, 'from "react/jsx-runtime"')

  const plugin = (await importPlugin(doubleQuotedImports)).default

  assert.equal(plugin.id, 'pr-autopilot')
})

test('Check now invokes the controller check mutation from the rendered control', async () => {
  const source = await readFile(pluginPath, 'utf8')
  const page = await dashboard(source, dashboardData(false))
  const checkNow = control(page.render(), 'button', 'Check now')

  assert.equal(checkNow.props.disabled, false)
  checkNow.props.onClick()
  await new Promise(resolve => setImmediate(resolve))

  assert.deepEqual(page.sdk.testState.restCalls, [{
    endpoint: '/controller/check',
    options: { method: 'POST', timeoutMs: 10_000 }
  }])
  assert.deepEqual(page.sdk.testState.invalidateCalls, [{
    queryKey: ['pr-autopilot', 'overview']
  }])
  assert.equal(page.sdk.testState.notifications[0].kind, 'success')
})

test('Pause and Resume confirm controls invoke controller pause mutations', async () => {
  const source = await readFile(pluginPath, 'utf8')
  const page = await dashboard(source, dashboardData(false))

  control(page.render(), 'button', 'Pause').props.onClick()
  const pauseDialog = openControllerDialog(page.render())
  assert.equal(pauseDialog.props.open, true)
  assert.equal(pauseDialog.props.title, 'Pause PR Autopilot?')
  await pauseDialog.props.onConfirm()
  assert.deepEqual(page.sdk.testState.restCalls, [{
    endpoint: '/controller/pause',
    options: { body: { paused: true }, method: 'POST', timeoutMs: 10_000 }
  }])
  assert.deepEqual(page.sdk.testState.setQueryDataCalls, [['pr-autopilot', 'overview']])

  page.sdk.reset(dashboardData(true))
  const react = await import(reactStub)
  react.reset()
  control(page.render(), 'button', 'Resume').props.onClick()
  const resumeDialog = openControllerDialog(page.render())
  assert.equal(resumeDialog.props.open, true)
  assert.equal(resumeDialog.props.title, 'Resume PR Autopilot?')
  await resumeDialog.props.onConfirm()
  assert.deepEqual(page.sdk.testState.restCalls, [{
    endpoint: '/controller/pause',
    options: { body: { paused: false }, method: 'POST', timeoutMs: 10_000 }
  }])
  assert.equal(page.sdk.testState.notifications[0].kind, 'success')
})

test('repository switch confirms the mutation and updates rendered state', async () => {
  const source = await readFile(pluginPath, 'utf8')
  const page = await dashboard(source, repositoryDashboardData())
  const switches = findAll(page.render(), element => element.type === 'switch')
  assert.equal(switches.length, 1)
  assert.equal(switches[0].props.checked, true)
  assert.equal(
    switches[0].props['aria-label'],
    'Disable PR Autopilot for AnikaWilliams/example'
  )

  switches[0].props.onCheckedChange(false)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(page.sdk.testState.restCalls.length, 1)
  assert.equal(page.sdk.testState.restCalls[0].endpoint, '/repository-intents')
  assert.equal(page.sdk.testState.restCalls[0].options.body.repository, 'AnikaWilliams/example')
  assert.equal(page.sdk.testState.restCalls[0].options.body.disabled, true)
  assert.match(
    page.sdk.testState.restCalls[0].options.body.idempotency_key,
    /^[0-9a-f-]{16,}$/i
  )

  const dialog = openControllerDialog(page.render())
  assert.equal(dialog.props.title, 'Pause AnikaWilliams/example?')
  await dialog.props.onConfirm()
  assert.deepEqual(page.sdk.testState.restCalls.map(call => call.endpoint), [
    '/repository-intents',
    '/repository-intents/repository-intent/confirm'
  ])
  assert.equal(page.sdk.testState.query.data.repositories[0].disabled, true)
  assert.deepEqual(page.sdk.testState.setQueryDataCalls, [['pr-autopilot', 'overview']])
  assert.equal(page.sdk.testState.notifications[0].kind, 'success')

  const updatedSwitches = findAll(page.render(), element => element.type === 'switch')
  assert.equal(updatedSwitches.length, 1)
  assert.equal(updatedSwitches[0].props.checked, false)
  assert.equal(
    updatedSwitches[0].props['aria-label'],
    'Enable PR Autopilot for AnikaWilliams/example'
  )
})

test('disabled repository switch enables PR Autopilot and updates rendered state', async () => {
  const source = await readFile(pluginPath, 'utf8')
  const page = await dashboard(source, repositoryDashboardData(true))
  const switches = findAll(page.render(), element => element.type === 'switch')
  assert.equal(switches.length, 1)
  assert.equal(switches[0].props.checked, false)
  assert.equal(
    switches[0].props['aria-label'],
    'Enable PR Autopilot for AnikaWilliams/example'
  )

  switches[0].props.onCheckedChange(true)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(page.sdk.testState.restCalls.length, 1)
  assert.equal(page.sdk.testState.restCalls[0].endpoint, '/repository-intents')
  assert.equal(page.sdk.testState.restCalls[0].options.body.repository, 'AnikaWilliams/example')
  assert.equal(page.sdk.testState.restCalls[0].options.body.disabled, false)

  const dialog = openControllerDialog(page.render())
  assert.equal(dialog.props.title, 'Activate AnikaWilliams/example?')
  await dialog.props.onConfirm()
  assert.deepEqual(page.sdk.testState.restCalls.map(call => call.endpoint), [
    '/repository-intents',
    '/repository-intents/repository-intent/confirm'
  ])
  assert.equal(page.sdk.testState.query.data.repositories[0].disabled, false)
  assert.deepEqual(page.sdk.testState.setQueryDataCalls, [['pr-autopilot', 'overview']])

  const updatedSwitches = findAll(page.render(), element => element.type === 'switch')
  assert.equal(updatedSwitches.length, 1)
  assert.equal(updatedSwitches[0].props.checked, true)
  assert.equal(
    updatedSwitches[0].props['aria-label'],
    'Disable PR Autopilot for AnikaWilliams/example'
  )
})

test('Retry pipeline uses the paused two-step confirmation flow', async () => {
  const source = await readFile(pluginPath, 'utf8')
  const pausedPage = await dashboard(source, retryDashboardData(true))
  const retry = control(pausedPage.render(), 'button', 'Retry pipeline')

  assert.equal(retry.props.disabled, false)
  retry.props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(pausedPage.sdk.testState.restCalls.length, 1)
  assert.equal(pausedPage.sdk.testState.restCalls[0].endpoint, '/pipeline-reset-intents')
  assert.deepEqual(
    pausedPage.sdk.testState.restCalls[0].options,
    {
      body: {
        idempotency_key: pausedPage.sdk.testState.restCalls[0].options.body.idempotency_key,
        number: 18,
        repository: 'AnikaWilliams/example'
      },
      method: 'POST',
      timeoutMs: 10_000
    }
  )
  assert.match(pausedPage.sdk.testState.restCalls[0].options.body.idempotency_key, /^[0-9a-f-]{16,}$/i)

  const dialog = openControllerDialog(pausedPage.render())
  assert.equal(dialog.props.title, 'Retry AnikaWilliams/example #18?')
  assert.equal(dialog.props.confirmLabel, 'Retry exact-head pipeline')
  await dialog.props.onConfirm()
  assert.deepEqual(pausedPage.sdk.testState.restCalls.map(call => call.endpoint), [
    '/pipeline-reset-intents',
    '/pipeline-reset-intents/retry-intent/confirm'
  ])
  assert.deepEqual(pausedPage.sdk.testState.invalidateCalls, [{
    queryKey: ['pr-autopilot', 'overview']
  }])
  assert.equal(pausedPage.sdk.testState.notifications[0].kind, 'success')

  const runningPage = await dashboard(source, retryDashboardData(false))
  assert.equal(control(runningPage.render(), 'button', 'Retry pipeline').props.disabled, true)
})

test('uses native, honest dashboard affordances instead of button-shaped stage badges', async () => {
  const source = await readFile(pluginPath, 'utf8')

  assert.match(source, /\bLoader\b/)
  assert.match(source, /\bStatusDot\b/)
  assert.match(source, /\bSwitch\b/)
  assert.match(source, /paddingBottom:\s*'96px'/)
  assert.match(source, /function formatRelativeTime\(/)
  assert.match(source, /function StageProgress\(/)
  assert.doesNotMatch(source, /pipeline\.map\(stage\s*=>\s*jsx\(Badge/)
  assert.doesNotMatch(source, /'Loading controller state…'/)
})
