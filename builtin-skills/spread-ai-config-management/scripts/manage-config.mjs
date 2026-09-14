#!/usr/bin/env node
import fs from "node:fs/promises"
import process from "node:process"

const DEFAULT_RUNTIME_URL = "http://127.0.0.1:8000"

const RESOURCE_PATHS = Object.freeze({
  agent: "/api/agents",
  mcp: "/api/mcp-config",
  "llm-model": "/api/llm-registry/models",
  "llm-provider": "/api/llm-registry/providers",
  "llm-policy": "/api/llm-registry/execution-policies",
  prompt: "/api/prompt-snippets",
  skill: "/api/skills",
  tool: "/api/tools/",
})

const RESOURCES = new Set(Object.keys(RESOURCE_PATHS))
const ACTIONS = new Set([
  "list",
  "get",
  "create",
  "update",
  "patch",
  "delete",
  "enable",
  "disable",
  "resolved",
  "reorder",
  "edit-content",
  "edit",
])

class ConfigClientError extends Error {
  constructor(message, { status = null, exitCode = 1, code = null } = {}) {
    super(message)
    this.name = "ConfigClientError"
    this.status = status
    this.exitCode = exitCode
    this.code = code
  }
}

async function main(argv) {
  try {
    ensureSupportedNodeVersion()
    const args = parseArgs(argv)
    const result = await execute(args)
    writeJson(result, process.stdout)
    return 0
  } catch (error) {
    if (error instanceof ConfigClientError) {
      const payload = { error: error.message }
      if (error.code !== null) {
        payload.code = error.code
      }
      if (error.status !== null) {
        payload.status = error.status
      }
      writeJson(payload, process.stderr)
      return error.exitCode
    }
    writeJson({ error: error instanceof Error ? error.message : String(error) }, process.stderr)
    return 1
  }
}

function parseArgs(argv) {
  const positional = []
  const options = {
    id: null,
    subPath: null,
    file: null,
    runtimeUrl: process.env.SPREAD_AI_RUNTIME_URL || null,
    tokenFile: null,
    tokenStdin: false,
    authFile: null,
    loginFile: firstNonBlankEnv("SPREAD_AI_RUNTIME_LOGIN_FILE", "SPREAD_AI_MANAGEMENT_LOGIN_FILE"),
  }

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (!arg.startsWith("--")) {
      positional.push(arg)
      continue
    }
    switch (arg) {
      case "--id":
        options.id = requireValue(argv, ++index, arg)
        break
      case "--sub-path":
        options.subPath = requireValue(argv, ++index, arg)
        break
      case "--file":
        options.file = requireValue(argv, ++index, arg)
        break
      case "--runtime-url":
        options.runtimeUrl = requireValue(argv, ++index, arg)
        break
      case "--token-file":
        options.tokenFile = requireValue(argv, ++index, arg)
        break
      case "--token-stdin":
        options.tokenStdin = true
        break
      case "--auth-file":
        options.authFile = requireValue(argv, ++index, arg)
        break
      case "--login-file":
        options.loginFile = requireValue(argv, ++index, arg)
        break
      case "--help":
      case "-h":
        throw new ConfigClientError(helpText(), { exitCode: 0 })
      default:
        throw new ConfigClientError(`Unknown option: ${arg}`)
    }
  }

  if (positional.length !== 2) {
    throw new ConfigClientError(helpText())
  }
  const [resource, action] = positional
  if (!RESOURCES.has(resource)) {
    throw new ConfigClientError(`Unsupported resource: ${resource}`)
  }
  if (!ACTIONS.has(action)) {
    throw new ConfigClientError(`Unsupported action: ${action}`)
  }
  if (options.tokenStdin && options.file === "-") {
    throw new ConfigClientError("--token-stdin and --file - both consume stdin; use a token file/env or a payload file.")
  }
  return { resource, action, ...options }
}

function requireValue(argv, index, optionName) {
  const value = argv[index]
  if (!value || value.startsWith("--")) {
    throw new ConfigClientError(`${optionName} requires a value.`)
  }
  return value
}

function ensureSupportedNodeVersion() {
  const [majorText] = process.versions.node.split(".")
  const major = Number.parseInt(majorText, 10)
  if (!Number.isFinite(major) || major < 18 || typeof fetch !== "function") {
    throw new ConfigClientError(`Node.js 18+ with the standard fetch API is required. Current Node: ${process.versions.node}`)
  }
}

function helpText() {
  return [
    "Usage: node scripts/manage-config.mjs <resource> <action> [options]",
    "Resources: agent, mcp, llm-model, llm-provider, llm-policy, prompt, skill, tool",
    "Actions: list, get, create, update, patch, delete, enable, disable, resolved, reorder, edit-content (or edit)",
    "Read-only metadata: skill list/get and tool list/get. Tool execution is not exposed.",
    "Options: --id <id> --sub-path <path> --file <path|-> --runtime-url <url> --login-file <path> --token-file <path> --token-stdin --auth-file <path>",
    "Auth priority: explicit token sources first, then login properties file, then advanced token environment fallbacks. Never pass passwords or tokens as command-line values.",
  ].join("\n")
}

async function execute(args) {
  const { method, relativePath, body } = await buildRequest(args)
  const explicitTokenRequested = Boolean(args.tokenFile || args.tokenStdin || args.authFile)
  const loginProperties = args.loginFile && !explicitTokenRequested ? await readLoginProperties(args.loginFile) : null
  const runtimeUrl = (args.runtimeUrl || loginProperties?.runtime_url || DEFAULT_RUNTIME_URL).replace(/\/+$/, "")
  const url = joinEndpoint(runtimeUrl, RESOURCE_PATHS[args.resource], relativePath)
  const headers = { Accept: "application/json" }
  const auth = await resolveAuth(args, runtimeUrl, loginProperties)
  headers.Authorization = `Bearer ${auth.token}`
  const init = { method, headers }
  if (body !== null) {
    headers["Content-Type"] = "application/json; charset=utf-8"
    init.body = body
  }

  try {
    let response
    try {
      response = await fetch(url, init)
    } catch (error) {
      throw new ConfigClientError(error instanceof Error ? error.message : String(error))
    }
    const text = await response.text()
    if (!response.ok) {
      throw configErrorFromResponse(text, response.statusText, response.status)
    }
    if (!text.trim()) {
      if (method === "DELETE") {
        return { status: "success", resource: args.resource, id: args.id }
      }
      return {}
    }
    return parseJsonOrText(text)
  } finally {
    if (auth.logoutAfterUse) {
      await logoutToken(runtimeUrl, auth.token)
    }
  }
}

async function buildRequest(args) {
  const { resource, action, id, subPath } = args
  if (resource === "tool") {
    return buildToolReadRequest(args)
  }
  switch (action) {
    case "list":
      return { method: "GET", relativePath: id ? relativePath(id, subPath) : "?summary=true", body: null }
    case "get":
      requireId(id, "get")
      return { method: "GET", relativePath: relativePath(id, subPath), body: null }
    case "create": {
      rejectSkillWrite(resource)
      let method = "POST"
      if (resource.startsWith("llm-") && !subPath) {
        requireId(id, "LLM create/save")
        method = "PUT"
      }
      return { method, relativePath: relativePath(id, subPath), body: await readPayload(args.file) }
    }
    case "update":
      rejectSkillWrite(resource)
      requireId(id, "update")
      return { method: resource === "mcp" ? "PATCH" : "PUT", relativePath: relativePath(id, subPath), body: await readPayload(args.file) }
    case "patch":
      rejectSkillWrite(resource)
      requireId(id, "patch")
      return { method: "PATCH", relativePath: relativePath(id, subPath), body: await readPayload(args.file) }
    case "delete":
      rejectSkillWrite(resource)
      requireId(id, "delete")
      return { method: "DELETE", relativePath: relativePath(id, subPath), body: null }
    case "enable":
      if (!["agent", "mcp"].includes(resource)) {
        throw new ConfigClientError("enable is only supported for agent and mcp.")
      }
      requireId(id, "enable")
      return { method: "POST", relativePath: relativePath(id, "enable"), body: null }
    case "disable":
      if (!["agent", "mcp"].includes(resource)) {
        throw new ConfigClientError("disable is only supported for agent and mcp.")
      }
      requireId(id, "disable")
      return { method: "POST", relativePath: relativePath(id, "disable"), body: null }
    case "resolved":
      if (resource !== "llm-policy") {
        throw new ConfigClientError("resolved is only supported for llm-policy.")
      }
      requireId(id, "resolved")
      return { method: "GET", relativePath: relativePath(id, "resolved"), body: null }
    case "reorder":
      rejectSkillWrite(resource)
      requireId(id, "reorder")
      if (!subPath) {
        throw new ConfigClientError("reorder requires --sub-path.")
      }
      return { method: "POST", relativePath: relativePath(id, `${subPath.replace(/[\\/]+$/, "")}/reorder`), body: await readPayload(args.file) }
    case "edit-content":
    case "edit":
      if (resource !== "prompt") {
        throw new ConfigClientError(`${action} is only supported for prompt.`)
      }
      requireId(id, action)
      if (!args.file) {
        throw new ConfigClientError(`${action} requires --file.`)
      }
      if (subPath) {
        throw new ConfigClientError(`${action} does not support --sub-path.`)
      }
      return { method: "POST", relativePath: relativePath(id, "content/edits"), body: await readPayload(args.file) }
    default:
      throw new ConfigClientError(`Unsupported action: ${action}`)
  }
}

function buildToolReadRequest(args) {
  const { action, id, subPath } = args
  if (subPath) {
    throw new ConfigClientError("Tool metadata only supports top-level list/get; --sub-path is not supported.")
  }
  switch (action) {
    case "list":
      if (id) {
        throw new ConfigClientError("tool list does not accept --id; use tool get --id <tool_name> for detail.")
      }
      return { method: "GET", relativePath: "", body: null }
    case "get":
      requireId(id, "tool get")
      return { method: "GET", relativePath: relativePath(id), body: null }
    default:
      throw new ConfigClientError(`${action} is not supported for read-only tool metadata. Use tool list or tool get --id <tool_name>.`)
  }
}

function rejectSkillWrite(resource) {
  if (resource === "skill") {
    throw new ConfigClientError("Skill metadata is read-only.")
  }
}

function requireId(id, action) {
  if (!id) {
    throw new ConfigClientError(`${action} requires --id.`)
  }
}

function relativePath(primaryId, childPath = null) {
  const segments = []
  if (primaryId) {
    segments.push(escapeSegment(primaryId))
  }
  if (childPath) {
    for (const segment of childPath.replaceAll("\\", "/").split("/")) {
      if (segment) {
        segments.push(escapeSegment(segment))
      }
    }
  }
  return segments.join("/")
}

function escapeSegment(value) {
  return encodeURIComponent(value)
}

function joinEndpoint(runtimeUrl, basePath, relative) {
  const normalizedBasePath = basePath.replace(/\/+$/, "")
  if (!relative) {
    return `${runtimeUrl}${basePath}`
  }
  if (relative.startsWith("?")) {
    return `${runtimeUrl}${normalizedBasePath}/${relative}`
  }
  return `${runtimeUrl}${normalizedBasePath}/${relative}`
}

async function resolveAuth(args, runtimeUrl, loginProperties) {
  if (args.tokenFile) {
    return { token: await readTokenFile(args.tokenFile, "Token file not found"), logoutAfterUse: false }
  }
  if (args.tokenStdin) {
    const token = stripUtf8Bom(await readStdin()).trim()
    if (token) {
      return { token, logoutAfterUse: false }
    }
    throw missingAuthError("Token stdin was empty.")
  }
  if (args.authFile) {
    return { token: await readTokenFromAuthFile(args.authFile, "Auth file"), logoutAfterUse: false }
  }

  if (loginProperties) {
    return { token: await loginForToken(runtimeUrl, loginProperties), logoutAfterUse: true }
  }

  const fallbackTokenFile = firstNonBlankEnv("SPREAD_AI_MANAGEMENT_TOKEN_FILE", "SPREAD_AI_RUNTIME_TOKEN_FILE")
  if (fallbackTokenFile) {
    return { token: await readTokenFile(fallbackTokenFile, "Token file not found"), logoutAfterUse: false }
  }

  const fallbackEnvToken = firstNonBlankEnv("SPREAD_AI_MANAGEMENT_TOKEN", "SPREAD_AI_RUNTIME_TOKEN")
  if (fallbackEnvToken) {
    return { token: fallbackEnvToken, logoutAfterUse: false }
  }

  const fallbackAuthFile = firstNonBlankEnv("SPREAD_AI_MANAGEMENT_AUTH_FILE", "SPREAD_AI_RUNTIME_AUTH_FILE")
  if (fallbackAuthFile) {
    return { token: await readTokenFromAuthFile(fallbackAuthFile, "Auth file"), logoutAfterUse: false }
  }

  throw missingAuthError("No Runtime authentication source was provided.")
}

function firstNonBlankEnv(...names) {
  for (const name of names) {
    const value = process.env[name]
    if (typeof value === "string" && value.trim()) {
      return value.trim()
    }
  }
  return null
}

async function readTokenFile(filePath, missingPrefix) {
  const token = stripUtf8Bom(await readUtf8File(filePath, missingPrefix)).trim()
  if (token) {
    return token
  }
  throw missingAuthError(`${missingPrefix}: token file is empty.`)
}

async function readTokenFromAuthFile(authFile, label) {
  const text = await readUtf8File(authFile, `${label} not found`)
  const parsed = parseJsonObject(text, `Invalid ${label.toLowerCase()}: ${authFile}`)
  const token = parsed.access_token || parsed.token
  if (typeof token === "string" && token.trim()) {
    return token.trim()
  }
  throw missingAuthError(`${label} must contain access_token or token.`)
}

function missingAuthError(detail) {
  return new ConfigClientError(
    `${detail} Provide a UTF-8 login properties file with username and password via --login-file or SPREAD_AI_RUNTIME_LOGIN_FILE. Explicit token sources (--token-file, --token-stdin, --auth-file) and token environment variables are advanced fallback paths only.`,
    { status: 401, code: "auth_required" },
  )
}

async function readLoginProperties(filePath) {
  const text = stripUtf8Bom(await readUtf8File(filePath, "Login file not found"))
  const properties = parseProperties(text, "login file")
  const username = stringProperty(properties, "username")
  const password = stringProperty(properties, "password")
  if (!username || !password) {
    throw new ConfigClientError("Login file must contain non-empty username and password fields.", { code: "auth_required", status: 401 })
  }
  return {
    username,
    password,
    runtime_url: stringProperty(properties, "runtime_url"),
    device_id: stringProperty(properties, "device_id") || "spread-ai-config-management",
    client_kind: stringProperty(properties, "client_kind") || "builtin-management-skill",
  }
}

function parseProperties(text, label) {
  const result = {}
  const lines = text.split(/\r?\n/)
  for (let index = 0; index < lines.length; index += 1) {
    const raw = lines[index]
    const trimmed = raw.trim()
    if (!trimmed || trimmed.startsWith("#") || trimmed.startsWith(";")) {
      continue
    }
    const separatorIndex = raw.indexOf("=")
    if (separatorIndex <= 0) {
      throw new ConfigClientError(`Invalid ${label}: line ${index + 1} must use key=value syntax.`)
    }
    const key = raw.slice(0, separatorIndex).trim()
    if (!key) {
      throw new ConfigClientError(`Invalid ${label}: line ${index + 1} has an empty key.`)
    }
    result[key] = raw.slice(separatorIndex + 1).trim()
  }
  return result
}

function stringProperty(properties, key) {
  const value = properties[key]
  return typeof value === "string" && value.trim() ? value.trim() : null
}

async function loginForToken(runtimeUrl, loginProperties) {
  const loginUrl = `${runtimeUrl}/api/auth/login`
  const response = await fetchJson(
    loginUrl,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({
        username: loginProperties.username,
        password: loginProperties.password,
        device_id: loginProperties.device_id,
        client_kind: loginProperties.client_kind,
      }),
    },
    "Runtime login request failed",
  )
  const parsed = parseJsonObject(response.text, "Invalid Runtime login response")
  const token = parsed.access_token
  if (typeof token === "string" && token.trim()) {
    return token.trim()
  }
  throw new ConfigClientError("Runtime login response did not contain access_token.", { status: response.status, code: "auth_required" })
}

async function logoutToken(runtimeUrl, token) {
  try {
    await fetchJson(
      `${runtimeUrl}/api/auth/logout`,
      {
        method: "POST",
        headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
      },
      "Runtime logout request failed",
      { ignoreHttpError: true },
    )
  } catch {
    // Logout is best-effort cleanup for this script-owned login session. Do not surface it because
    // doing so would hide the management operation result and must never reveal token material.
  }
}

async function fetchJson(url, init, networkErrorPrefix, { ignoreHttpError = false } = {}) {
  let response
  try {
    response = await fetch(url, init)
  } catch (error) {
    throw new ConfigClientError(`${networkErrorPrefix}: ${error instanceof Error ? error.message : String(error)}`)
  }
  const text = await response.text()
  if (!response.ok && !ignoreHttpError) {
    const error = configErrorFromResponse(text, response.statusText, response.status)
    const loginCode = response.status === 429 ? "login_rate_limited" : "login_failed"
    throw new ConfigClientError(`Runtime login failed: ${error.message}`, { status: response.status, code: loginCode })
  }
  return { status: response.status, text }
}

async function readPayload(filePath) {
  if (!filePath) {
    return null
  }
  const rawText = filePath === "-" ? await readStdin() : await readUtf8File(filePath, "Payload file not found")
  const text = stripUtf8Bom(rawText)
  if (!text.trim()) {
    throw new ConfigClientError("Payload file/stdin is empty.")
  }
  try {
    JSON.parse(text)
    return text
  } catch (jsonError) {
    try {
      return JSON.stringify(parseSimpleYaml(text))
    } catch (yamlError) {
      throw new ConfigClientError(`Payload must be UTF-8 JSON or supported YAML: ${jsonError.message}; ${yamlError.message}`)
    }
  }
}

function parseSimpleYaml(text) {
  const lines = text.split(/\r?\n/)
  const state = { index: 0 }
  const result = parseYamlBlock(lines, state, 0)
  skipYamlBlankAndComments(lines, state)
  if (state.index < lines.length) {
    throw new Error(`unsupported YAML structure near line ${state.index + 1}`)
  }
  return result
}

function skipYamlBlankAndComments(lines, state) {
  while (state.index < lines.length) {
    const trimmed = lines[state.index].trim()
    if (trimmed && !trimmed.startsWith("#")) {
      return
    }
    state.index += 1
  }
}

function yamlIndent(line) {
  const match = line.match(/^\s*/)
  return match ? match[0].length : 0
}

function nextYamlContentIndent(lines, startIndex) {
  for (let index = startIndex; index < lines.length; index += 1) {
    const trimmed = lines[index].trim()
    if (trimmed && !trimmed.startsWith("#")) {
      return yamlIndent(lines[index])
    }
  }
  return -1
}

function parseYamlBlock(lines, state, minIndent) {
  skipYamlBlankAndComments(lines, state)
  if (state.index >= lines.length) {
    return null
  }
  const lineIndent = yamlIndent(lines[state.index])
  if (lineIndent < minIndent) {
    return null
  }
  const trimmed = lines[state.index].slice(lineIndent).trimEnd()
  if (trimmed.startsWith("-")) {
    return parseYamlList(lines, state, lineIndent)
  }
  return parseYamlMap(lines, state, lineIndent)
}

function parseYamlMap(lines, state, blockIndent) {
  const map = {}
  while (true) {
    skipYamlBlankAndComments(lines, state)
    if (state.index >= lines.length) {
      break
    }
    const line = lines[state.index]
    const lineIndent = yamlIndent(line)
    if (lineIndent < blockIndent || lineIndent !== blockIndent) {
      break
    }
    const trimmed = line.slice(lineIndent).trimEnd()
    if (trimmed.startsWith("-")) {
      break
    }
    const match = trimmed.match(/^([^:]+):(.*)$/)
    if (!match) {
      throw new Error(`expected key/value mapping near line ${state.index + 1}`)
    }
    const key = yamlKey(match[1])
    const rest = match[2].trim()
    state.index += 1
    map[key] = readYamlValue(rest, lines, state, lineIndent)
  }
  return map
}

function parseYamlList(lines, state, blockIndent) {
  const list = []
  while (true) {
    skipYamlBlankAndComments(lines, state)
    if (state.index >= lines.length) {
      break
    }
    const line = lines[state.index]
    const lineIndent = yamlIndent(line)
    if (lineIndent < blockIndent || lineIndent !== blockIndent) {
      break
    }
    const trimmed = line.slice(lineIndent).trimEnd()
    if (!trimmed.startsWith("-")) {
      break
    }
    const itemText = trimmed.slice(1).trimStart()
    state.index += 1
    let item
    if (!itemText.trim()) {
      const nextIndent = nextYamlContentIndent(lines, state.index)
      item = nextIndent > lineIndent ? parseYamlBlock(lines, state, nextIndent) : null
    } else {
      const match = itemText.match(/^([^:]+):(.*)$/)
      if (match) {
        item = {}
        item[yamlKey(match[1])] = readYamlValue(match[2].trim(), lines, state, lineIndent)
        const nextIndent = nextYamlContentIndent(lines, state.index)
        if (nextIndent > lineIndent) {
          Object.assign(item, parseYamlMap(lines, state, nextIndent))
        }
      } else {
        item = parseYamlScalar(itemText)
      }
    }
    list.push(item)
  }
  return list
}

function readYamlValue(rest, lines, state, parentIndent) {
  if (!rest.trim()) {
    const nextIndent = nextYamlContentIndent(lines, state.index)
    return nextIndent > parentIndent ? parseYamlBlock(lines, state, nextIndent) : null
  }
  if (rest === "|" || rest === ">") {
    return readYamlBlockScalar(lines, state, parentIndent, rest)
  }
  return parseYamlScalar(rest)
}

function readYamlBlockScalar(lines, state, parentIndent, style) {
  const scalarLines = []
  const stripIndent = parentIndent + 2
  while (state.index < lines.length) {
    const line = lines[state.index]
    if (!line.trim()) {
      scalarLines.push("")
      state.index += 1
      continue
    }
    const lineIndent = yamlIndent(line)
    if (lineIndent <= parentIndent) {
      break
    }
    const removeCount = Math.min(lineIndent < stripIndent ? lineIndent : stripIndent, line.length)
    scalarLines.push(line.slice(removeCount))
    state.index += 1
  }
  if (style === ">") {
    return scalarLines.map(line => line.trim()).join(" ")
  }
  return scalarLines.join("\n")
}

function yamlKey(value) {
  return value.trim().replace(/^['\"]|['\"]$/g, "")
}

function parseYamlScalar(value) {
  const trimmed = value.trim()
  if (trimmed === "null" || trimmed === "~") return null
  if (trimmed === "true") return true
  if (trimmed === "false") return false
  if ((trimmed.startsWith("\"") && trimmed.endsWith("\"")) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    return trimmed.slice(1, -1)
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return JSON.parse(trimmed)
    } catch {
      // Fall through to string.
    }
  }
  if (/^-?\d+$/.test(trimmed)) {
    return Number.parseInt(trimmed, 10)
  }
  if (/^-?(?:\d+\.\d*|\d*\.\d+)(?:e[+-]?\d+)?$/i.test(trimmed)) {
    return Number.parseFloat(trimmed)
  }
  return trimmed
}

async function readUtf8File(filePath, missingPrefix) {
  try {
    return await fs.readFile(filePath, "utf8")
  } catch (error) {
    if (error && error.code === "ENOENT") {
      throw new ConfigClientError(`${missingPrefix}: ${filePath}`)
    }
    throw error
  }
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = ""
    process.stdin.setEncoding("utf8")
    process.stdin.on("data", chunk => {
      data += chunk
    })
    process.stdin.on("end", () => resolve(data))
    process.stdin.on("error", reject)
  })
}

function stripUtf8Bom(text) {
  return text.replace(/^\uFEFF/, "")
}

function parseJsonObject(text, message) {
  let parsed
  try {
    parsed = JSON.parse(stripUtf8Bom(text))
  } catch (error) {
    throw new ConfigClientError(`${message}: ${error.message}`)
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new ConfigClientError(`${message}: expected a JSON object`)
  }
  return parsed
}

function parseJsonOrText(text) {
  try {
    return JSON.parse(text)
  } catch {
    return { text }
  }
}

function configErrorFromResponse(text, statusText, status) {
  const parsed = parseResponseBody(text)
  const message = responseErrorMessage(parsed, text, statusText)
  const explicitCode = parsed && typeof parsed === "object" && typeof parsed.code === "string"
    ? parsed.code
    : null
  const code = explicitCode || authErrorCode(status, message, parsed)
  return new ConfigClientError(message, { status, code })
}

function parseResponseBody(text) {
  if (!text.trim()) {
    return null
  }
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

function responseErrorMessage(parsed, text, statusText) {
  if (!text.trim()) {
    return statusText || "Runtime request failed"
  }
  if (parsed && typeof parsed === "object") {
    if (typeof parsed.detail === "string") {
      return parsed.detail
    }
    if (typeof parsed.error === "string") {
      return parsed.error
    }
    return JSON.stringify(parsed)
  }
  if (parsed !== null) {
    return JSON.stringify(parsed)
  }
  return text.trim()
}

function authErrorCode(status, message, parsed) {
  if (status !== 401 && status !== 403) {
    return null
  }
  const text = `${message} ${JSON.stringify(parsed || {})}`.toLowerCase()
  if (text.includes("expired") || text.includes("过期")) {
    return "token_expired"
  }
  if (status === 401) {
    return "auth_required"
  }
  return "permission_denied"
}

function writeJson(value, stream) {
  stream.write(`${JSON.stringify(value, null, 2)}\n`)
}

process.exitCode = await main(process.argv.slice(2))
