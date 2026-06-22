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
])

class ConfigClientError extends Error {
  constructor(message, { status = null, exitCode = 1 } = {}) {
    super(message)
    this.name = "ConfigClientError"
    this.status = status
    this.exitCode = exitCode
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
    tokenFile: process.env.SPREAD_AI_RUNTIME_TOKEN_FILE || null,
    tokenStdin: false,
    authFile: process.env.SPREAD_AI_RUNTIME_AUTH_FILE || null,
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
    "Resources: agent, mcp, llm-model, llm-provider, llm-policy, prompt, skill",
    "Actions: list, get, create, update, patch, delete, enable, disable, resolved, reorder",
    "Options: --id <id> --sub-path <path> --file <path|-> --runtime-url <url> --token-file <path> --token-stdin --auth-file <path>",
  ].join("\n")
}

async function execute(args) {
  const { method, relativePath, body } = await buildRequest(args)
  const runtimeUrl = (args.runtimeUrl || DEFAULT_RUNTIME_URL).replace(/\/+$/, "")
  const url = joinEndpoint(runtimeUrl, RESOURCE_PATHS[args.resource], relativePath)
  const headers = { Accept: "application/json" }
  const token = await readAuthToken(args)
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }
  const init = { method, headers }
  if (body !== null) {
    headers["Content-Type"] = "application/json; charset=utf-8"
    init.body = body
  }

  let response
  try {
    response = await fetch(url, init)
  } catch (error) {
    throw new ConfigClientError(error instanceof Error ? error.message : String(error))
  }
  const text = await response.text()
  if (!response.ok) {
    throw new ConfigClientError(errorMessageFromResponse(text, response.statusText), { status: response.status })
  }
  if (!text.trim()) {
    if (method === "DELETE") {
      return { status: "success", resource: args.resource, id: args.id }
    }
    return {}
  }
  return parseJsonOrText(text)
}

async function buildRequest(args) {
  const { resource, action, id, subPath } = args
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
    default:
      throw new ConfigClientError(`Unsupported action: ${action}`)
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
  if (!relative) {
    return `${runtimeUrl}${basePath}`
  }
  if (relative.startsWith("?")) {
    return `${runtimeUrl}${basePath}${relative}`
  }
  return `${runtimeUrl}${basePath}/${relative}`
}

async function readAuthToken(args) {
  if (args.tokenFile) {
    return stripUtf8Bom(await readUtf8File(args.tokenFile, "Token file not found")).trim()
  }
  if (args.tokenStdin) {
    return stripUtf8Bom(await readStdin()).trim()
  }
  if (args.authFile) {
    const text = await readUtf8File(args.authFile, "Auth file not found")
    const parsed = parseJsonObject(text, `Invalid auth file: ${args.authFile}`)
    const token = parsed.access_token || parsed.token
    if (typeof token === "string" && token.trim()) {
      return token.trim()
    }
    throw new ConfigClientError("Auth file must contain access_token or token.")
  }
  const envToken = process.env.SPREAD_AI_RUNTIME_TOKEN
  return envToken && envToken.trim() ? envToken.trim() : null
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

function errorMessageFromResponse(text, statusText) {
  if (!text.trim()) {
    return statusText || "Runtime request failed"
  }
  try {
    const parsed = JSON.parse(text)
    if (parsed && typeof parsed === "object") {
      if (typeof parsed.detail === "string") {
        return parsed.detail
      }
      if (typeof parsed.error === "string") {
        return parsed.error
      }
      return JSON.stringify(parsed)
    }
    return JSON.stringify(parsed)
  } catch {
    return text.trim()
  }
}

function writeJson(value, stream) {
  stream.write(`${JSON.stringify(value, null, 2)}\n`)
}

process.exitCode = await main(process.argv.slice(2))
