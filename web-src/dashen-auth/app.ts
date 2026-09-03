/*! Source: web-src/dashen-auth/app.ts. pages/dashen-auth/app.js is generated. */

type MessageKind = "success" | "warning" | "error";
type FlowIndex = 0 | 1 | 2 | 3 | 4;
type QrStateName =
  | "waiting_scan"
  | "waiting_confirm"
  | "processing"
  | "login_signature_required"
  | "signing_required"
  | "roles_ready"
  | "success"
  | "expired"
  | "error";
type QrServerStateName = Exclude<QrStateName, "success">;

interface DashenStatus {
  configured: boolean;
  source: string;
  role_id_masked: string;
  updated_at: number;
  embedded_overstats: boolean;
  runtime_applied: boolean;
  legacy_config_present: boolean;
  can_migrate: boolean;
  legacy_config_cleared?: boolean;
}

interface DashenRole {
  role_id: string;
  name: string;
  server: string;
}

interface DashenLoginSigningContext {
  csrf: string;
}

interface DashenSigningContext extends DashenLoginSigningContext {
  cst: string;
  time_diff: string;
}

interface DashenSignature {
  sign: string;
  timestamp: number;
  user_agent: string;
}

interface QrStateBase {
  state: QrServerStateName;
  expires_in: number;
  poll_interval_ms: number;
}

type QrState =
  | (QrStateBase & {
      state: "login_signature_required";
      signing_context: DashenLoginSigningContext;
    })
  | (QrStateBase & {
      state: "signing_required";
      signing_context: DashenSigningContext;
    })
  | (QrStateBase & { state: "roles_ready"; roles: DashenRole[] })
  | (QrStateBase & {
      state: Exclude<
        QrServerStateName,
        "login_signature_required" | "signing_required" | "roles_ready"
      >;
    });

interface QrStartState extends QrStateBase {
  state: "waiting_scan";
  session_id: string;
  qr_image: string;
}

interface QrProgressContent {
  badge: string;
  title: string;
  text: string;
  flow: FlowIndex;
}

type ElementConstructor<T extends HTMLElement> = new () => T;
type JsonRecord = Record<string, unknown>;

const bridge = window.AstrBotPluginPage;
const DASHEN_ROLE_BODY = JSON.stringify({ appKey: "bn" });

const QR_PROGRESS_CONTENT: Readonly<Record<QrStateName, QrProgressContent>> = {
  waiting_scan: {
    badge: "等待扫码",
    title: "打开网易大神 App 扫描二维码",
    text: "扫码后请在手机上确认登录，本页会自动继续。",
    flow: 0,
  },
  waiting_confirm: {
    badge: "等待确认",
    title: "已扫码，请在手机上确认",
    text: "不要关闭本页，确认后会自动识别战网账号 ID。",
    flow: 1,
  },
  processing: {
    badge: "处理中",
    title: "登录成功，正在读取战网账号 ID",
    text: "正在建立临时网页会话并查询已绑定的战网账号。",
    flow: 2,
  },
  login_signature_required: {
    badge: "处理中",
    title: "正在建立签名会话",
    text: "正在使用官方签名组件完成大神网页登录。",
    flow: 2,
  },
  signing_required: {
    badge: "处理中",
    title: "正在生成会话签名",
    text: "使用本次登录的临时上下文读取战网账号 ID。",
    flow: 2,
  },
  roles_ready: {
    badge: "账号已找到",
    title: "请选择要使用的战网账号 ID",
    text: "选择后将转换 token 并安全保存。",
    flow: 2,
  },
  success: {
    badge: "已完成",
    title: "授权与保存完成",
    text: "战网账号 ID 和 token 已保存，运行中的内置 Overstats 已同步更新。",
    flow: 4,
  },
  expired: {
    badge: "已过期",
    title: "二维码已失效",
    text: "请重新获取二维码。",
    flow: 0,
  },
  error: {
    badge: "授权失败",
    title: "扫码流程未完成",
    text: "请重新获取二维码后再试。",
    flow: 0,
  },
};

const SOURCE_LABELS: Readonly<Record<string, string>> = {
  plugin_data: "插件私有数据目录",
  plugin_kv: "旧版插件 KV",
  plugin_config: "旧版插件配置",
  none: "未配置",
};

function requireElement<T extends HTMLElement>(
  id: string,
  constructor: ElementConstructor<T>,
): T {
  const element = document.getElementById(id);
  if (!(element instanceof constructor)) {
    throw new Error(`页面缺少必需元素 #${id}`);
  }
  return element;
}

const elements = {
  statusBadge: requireElement("statusBadge", HTMLElement),
  roleValue: requireElement("roleValue", HTMLElement),
  sourceValue: requireElement("sourceValue", HTMLElement),
  updatedValue: requireElement("updatedValue", HTMLElement),
  runtimeValue: requireElement("runtimeValue", HTMLElement),
  legacyWarning: requireElement("legacyWarning", HTMLElement),
  externalWarning: requireElement("externalWarning", HTMLElement),
  migrateButton: requireElement("migrateButton", HTMLButtonElement),
  unbindButton: requireElement("unbindButton", HTMLButtonElement),
  unbindConfirm: requireElement("unbindConfirm", HTMLElement),
  cancelUnbindButton: requireElement("cancelUnbindButton", HTMLButtonElement),
  confirmUnbindButton: requireElement("confirmUnbindButton", HTMLButtonElement),
  refreshButton: requireElement("refreshButton", HTMLButtonElement),
  credentialForm: requireElement("credentialForm", HTMLFormElement),
  roleInput: requireElement("roleInput", HTMLInputElement),
  tokenInput: requireElement("tokenInput", HTMLInputElement),
  saveButton: requireElement("saveButton", HTMLButtonElement),
  startQrButton: requireElement("startQrButton", HTMLButtonElement),
  retryQrButton: requireElement("retryQrButton", HTMLButtonElement),
  cancelQrButton: requireElement("cancelQrButton", HTMLButtonElement),
  completeQrButton: requireElement("completeQrButton", HTMLButtonElement),
  qrStage: requireElement("qrStage", HTMLElement),
  qrImage: requireElement("qrImage", HTMLImageElement),
  qrOverlay: requireElement("qrOverlay", HTMLElement),
  qrOverlayTitle: requireElement("qrOverlayTitle", HTMLElement),
  qrStateBadge: requireElement("qrStateBadge", HTMLElement),
  qrStateTitle: requireElement("qrStateTitle", HTMLElement),
  qrStateText: requireElement("qrStateText", HTMLElement),
  qrCountdown: requireElement("qrCountdown", HTMLElement),
  rolePanel: requireElement("rolePanel", HTMLElement),
  roleList: requireElement("roleList", HTMLElement),
  flowScan: requireElement("flowScan", HTMLElement),
  flowConfirm: requireElement("flowConfirm", HTMLElement),
  flowRole: requireElement("flowRole", HTMLElement),
  flowToken: requireElement("flowToken", HTMLElement),
  message: requireElement("message", HTMLElement),
} as const;

let qrSessionId = "";
let qrPollTimer: number | null = null;
let qrPollGeneration = 0;
let currentRoles: DashenRole[] = [];
let completingQr = false;
let qrConfirmed = false;
let dashenSignerPromise: Promise<DashenSigner> | null = null;
let dashenSignerVersion = "";
let dashenLoginSigningContext: DashenLoginSigningContext | null = null;
let dashenSigningContext: DashenSigningContext | null = null;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, responseName: string): JsonRecord {
  if (!isRecord(value)) {
    throw new Error(`${responseName}响应格式无效`);
  }
  return value;
}

function requireString(
  record: JsonRecord,
  key: string,
  responseName: string,
): string {
  const value = record[key];
  if (typeof value !== "string") {
    throw new Error(`${responseName}响应缺少 ${key}`);
  }
  return value;
}

function requireBoolean(
  record: JsonRecord,
  key: string,
  responseName: string,
): boolean {
  const value = record[key];
  if (typeof value !== "boolean") {
    throw new Error(`${responseName}响应缺少 ${key}`);
  }
  return value;
}

function requireNonNegativeInteger(
  record: JsonRecord,
  key: string,
  responseName: string,
): number {
  const value = record[key];
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new Error(`${responseName}响应中的 ${key} 无效`);
  }
  return Number(value);
}

function parseStatus(value: unknown): DashenStatus {
  const record = requireRecord(value, "凭证状态");
  const legacyConfigCleared = record["legacy_config_cleared"];
  if (
    legacyConfigCleared !== undefined &&
    typeof legacyConfigCleared !== "boolean"
  ) {
    throw new Error("凭证状态响应中的 legacy_config_cleared 无效");
  }
  const status: DashenStatus = {
    configured: requireBoolean(record, "configured", "凭证状态"),
    source: requireString(record, "source", "凭证状态"),
    role_id_masked: requireString(record, "role_id_masked", "凭证状态"),
    updated_at: requireNonNegativeInteger(record, "updated_at", "凭证状态"),
    embedded_overstats: requireBoolean(
      record,
      "embedded_overstats",
      "凭证状态",
    ),
    runtime_applied: requireBoolean(record, "runtime_applied", "凭证状态"),
    legacy_config_present: requireBoolean(
      record,
      "legacy_config_present",
      "凭证状态",
    ),
    can_migrate: requireBoolean(record, "can_migrate", "凭证状态"),
  };
  if (legacyConfigCleared !== undefined) {
    status.legacy_config_cleared = legacyConfigCleared;
  }
  return status;
}

function parseRole(value: unknown): DashenRole {
  const record = requireRecord(value, "战网账号");
  const roleId = requireString(record, "role_id", "战网账号").trim();
  const name = requireString(record, "name", "战网账号").trim();
  const server = requireString(record, "server", "战网账号").trim();
  if (!/^[0-9]{1,20}$/.test(roleId) || !/[1-9]/.test(roleId)) {
    throw new Error("战网账号响应中的 role_id 无效");
  }
  return { role_id: roleId, name: name || "战网账号", server };
}

function parseLoginSigningContext(value: unknown): DashenLoginSigningContext {
  const record = requireRecord(value, "大神登录签名上下文");
  const csrf = requireString(record, "csrf", "大神登录签名上下文").trim();
  if (!csrf || csrf.length > 512) {
    throw new Error("大神登录未返回有效 XSRF 上下文，请重新扫码");
  }
  return { csrf };
}

function parseSigningContext(value: unknown): DashenSigningContext {
  const record = requireRecord(value, "大神签名上下文");
  const csrf = requireString(record, "csrf", "大神签名上下文").trim();
  const cst = requireString(record, "cst", "大神签名上下文").trim();
  const timeDiff = requireString(
    record,
    "time_diff",
    "大神签名上下文",
  ).trim();
  if (
    !csrf ||
    csrf.length > 512 ||
    !cst ||
    cst.length > 4096 ||
    !/^-?\d+$/.test(timeDiff)
  ) {
    throw new Error("大神登录未返回有效签名上下文，请重新扫码");
  }
  return { csrf, cst, time_diff: timeDiff };
}

function parseQrStateName(value: unknown): QrServerStateName {
  switch (value) {
    case "waiting_scan":
    case "waiting_confirm":
    case "processing":
    case "login_signature_required":
    case "signing_required":
    case "roles_ready":
    case "expired":
    case "error":
      return value;
    default:
      throw new Error("扫码状态响应包含未知状态");
  }
}

function parseQrState(value: unknown): QrState {
  const record = requireRecord(value, "扫码状态");
  const state = parseQrStateName(record["state"]);
  const common = {
    expires_in: requireNonNegativeInteger(record, "expires_in", "扫码状态"),
    poll_interval_ms: requireNonNegativeInteger(
      record,
      "poll_interval_ms",
      "扫码状态",
    ),
  };
  if (state === "login_signature_required") {
    return {
      state,
      ...common,
      signing_context: parseLoginSigningContext(record["signing_context"]),
    };
  }
  if (state === "signing_required") {
    return {
      state,
      ...common,
      signing_context: parseSigningContext(record["signing_context"]),
    };
  }
  if (state === "roles_ready") {
    const rawRoles = record["roles"];
    if (!Array.isArray(rawRoles) || rawRoles.length === 0) {
      throw new Error("扫码状态响应未包含战网账号 ID");
    }
    return { state, ...common, roles: rawRoles.map(parseRole) };
  }
  return { state, ...common };
}

function parseQrStart(value: unknown): QrStartState {
  const record = requireRecord(value, "二维码");
  const state = parseQrStateName(record["state"]);
  const sessionId = requireString(record, "session_id", "二维码").trim();
  const qrImage = requireString(record, "qr_image", "二维码").trim();
  if (state !== "waiting_scan" || !sessionId) {
    throw new Error("二维码响应缺少有效会话");
  }
  if (!qrImage.startsWith("data:image/png;base64,")) {
    throw new Error("二维码响应未包含有效 PNG 图片");
  }
  return {
    state,
    session_id: sessionId,
    qr_image: qrImage,
    expires_in: requireNonNegativeInteger(record, "expires_in", "二维码"),
    poll_interval_ms: requireNonNegativeInteger(
      record,
      "poll_interval_ms",
      "二维码",
    ),
  };
}

function parseSignerWasm(value: unknown): string {
  const record = requireRecord(value, "大神签名组件");
  const encoded = requireString(record, "wasm_base64", "大神签名组件").trim();
  if (!encoded) {
    throw new Error("大神签名组件数据为空，请重新安装插件");
  }
  return encoded;
}

function parseSignatureResult(value: unknown): Omit<DashenSignature, "user_agent"> {
  let parsed: unknown = value;
  if (typeof value === "string") {
    try {
      parsed = JSON.parse(value) as unknown;
    } catch (error) {
      throw new Error("大神签名结果不是有效 JSON", { cause: error });
    }
  }
  const record = requireRecord(parsed, "大神签名");
  const sign = requireString(record, "sign", "大神签名").trim();
  const timestamp = Number(record["timestamp"]);
  if (!sign || !Number.isSafeInteger(timestamp)) {
    throw new Error("大神签名结果无效，请刷新页面后重试");
  }
  return { sign, timestamp };
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function dashenReportBody(roleId: string): string {
  return JSON.stringify({
    appKey: "bn",
    roleId,
    server: "1",
    source: 1,
    type: "yearly",
  });
}

async function getDashenSigner(): Promise<DashenSigner> {
  if (!dashenSignerPromise) {
    dashenSignerPromise = (async () => {
      const signerNamespace = window.sig;
      if (!signerNamespace || typeof signerNamespace.default !== "function") {
        throw new Error("大神签名组件未能加载，请刷新页面后重试");
      }
      if (!window.__dashenSigWasmUrl) {
        const encoded = parseSignerWasm(
          await bridge.apiGet("dashen-auth/signer/wasm"),
        );
        const binary = window.atob(encoded);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) {
          bytes[index] = binary.charCodeAt(index);
        }
        window.__dashenSigWasmUrl = URL.createObjectURL(
          new Blob([bytes], { type: "application/wasm" }),
        );
      }
      const signer = await signerNamespace.default();
      if (!signer || typeof signer.gen_sign !== "function") {
        throw new Error("大神签名组件初始化失败，请刷新页面后重试");
      }
      return signer;
    })();
  }
  try {
    return await dashenSignerPromise;
  } catch (error) {
    dashenSignerPromise = null;
    throw error;
  }
}

async function getDashenSignerVersion(): Promise<string> {
  const signer = await getDashenSigner();
  if (typeof signer.get_version !== "function") {
    throw new Error("大神签名组件缺少版本信息，请重新安装插件");
  }
  const version = String(signer.get_version() ?? "").trim();
  if (!version || version.length > 256 || /[\x00-\x20\x7f]/.test(version)) {
    throw new Error("大神签名组件版本无效，请刷新页面后重试");
  }
  return version;
}

async function createDashenSignature(
  body: string,
  signingContext: DashenLoginSigningContext | DashenSigningContext,
): Promise<DashenSignature> {
  const previousLocalData = window.__dashenSigLocalData;
  window.__dashenSigLocalData =
    "cst" in signingContext
      ? {
          csrf: signingContext.csrf,
          cst: signingContext.cst,
          time_diff: signingContext.time_diff,
        }
      : { csrf: signingContext.csrf };
  try {
    const signer = await getDashenSigner();
    return {
      ...parseSignatureResult(signer.gen_sign(body)),
      user_agent: navigator.userAgent,
    };
  } finally {
    if (previousLocalData === undefined) {
      delete window.__dashenSigLocalData;
    } else {
      window.__dashenSigLocalData = previousLocalData;
    }
  }
}

function setBusy(
  button: HTMLButtonElement,
  busy: boolean,
  busyText?: string,
): void {
  const storedLabel = button.dataset["label"];
  if (busy && storedLabel === undefined) {
    button.dataset["label"] = button.textContent ?? "";
    button.textContent = busyText ?? button.textContent;
  } else if (!busy && storedLabel !== undefined) {
    button.textContent = storedLabel;
    delete button.dataset["label"];
  }
  button.disabled = busy;
}

function showMessage(text: string, kind: MessageKind = "success"): void {
  elements.message.textContent = text;
  elements.message.className = `message message-${kind}`;
  elements.message.hidden = false;
}

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? "未知";
}

function formatTimestamp(timestamp: number): string {
  if (timestamp === 0) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(timestamp * 1000));
}

function renderStatus(status: DashenStatus): void {
  elements.statusBadge.textContent = status.configured ? "已配置" : "未配置";
  elements.statusBadge.className = status.configured
    ? "badge badge-ok"
    : "badge badge-muted";
  elements.roleValue.textContent = status.role_id_masked || "—";
  elements.sourceValue.textContent = sourceLabel(status.source);
  elements.updatedValue.textContent = formatTimestamp(status.updated_at);
  elements.runtimeValue.textContent = !status.embedded_overstats
    ? "外部服务模式"
    : status.runtime_applied
      ? "已热更新"
      : status.configured
        ? "等待服务启动"
        : "等待凭证";
  elements.legacyWarning.hidden = !status.legacy_config_present;
  elements.externalWarning.hidden = status.embedded_overstats;
  elements.migrateButton.hidden = !status.can_migrate;
  elements.unbindButton.disabled =
    !status.configured && !status.legacy_config_present;
}

async function refreshStatus(): Promise<void> {
  setBusy(elements.refreshButton, true, "刷新中…");
  try {
    renderStatus(parseStatus(await bridge.apiGet("dashen-auth/status")));
  } catch (error) {
    showMessage(errorMessage(error, "读取状态失败"), "error");
  } finally {
    setBusy(elements.refreshButton, false);
  }
}

function stopQrPolling(): void {
  qrPollGeneration += 1;
  if (qrPollTimer !== null) window.clearTimeout(qrPollTimer);
  qrPollTimer = null;
}

function setFlow(activeIndex: FlowIndex): void {
  const items = [
    elements.flowScan,
    elements.flowConfirm,
    elements.flowRole,
    elements.flowToken,
  ] as const;
  items.forEach((item, index) => {
    item.classList.toggle("is-done", index < activeIndex);
    item.classList.toggle("is-current", index === activeIndex);
  });
}

function setQrProgress(state: QrStateName, expiresIn = 0): void {
  if (
    state === "processing" ||
    state === "login_signature_required" ||
    state === "signing_required" ||
    state === "roles_ready" ||
    state === "success"
  ) {
    qrConfirmed = true;
  }
  const content = QR_PROGRESS_CONTENT[state];
  elements.qrStateBadge.textContent = content.badge;
  elements.qrStateBadge.className = `badge ${state === "success" ? "badge-ok" : "badge-active"}`;
  elements.qrStateTitle.textContent = content.title;
  elements.qrStateText.textContent = content.text;
  elements.qrCountdown.textContent =
    expiresIn > 0 ? `二维码约 ${expiresIn} 秒后过期` : "";
  elements.qrImage.classList.toggle("is-obscured", qrConfirmed);
  elements.flowToken.textContent =
    state === "success" ? "已完成" : "转换并保存 token";
  setFlow(content.flow);
}

function resetQrView({ keepSuccess = false }: { keepSuccess?: boolean } = {}): void {
  stopQrPolling();
  qrSessionId = "";
  currentRoles = [];
  completingQr = false;
  qrConfirmed = false;
  dashenSignerVersion = "";
  dashenLoginSigningContext = null;
  dashenSigningContext = null;
  elements.roleList.replaceChildren();
  elements.rolePanel.hidden = true;
  setBusy(elements.completeQrButton, false);
  elements.cancelQrButton.disabled = false;
  if (!keepSuccess) {
    elements.qrStage.hidden = true;
    elements.qrImage.removeAttribute("src");
    elements.qrImage.classList.remove("is-obscured");
    elements.qrOverlay.hidden = true;
    elements.startQrButton.textContent = "获取二维码";
  }
}

function expireQr(title = "二维码已失效"): void {
  stopQrPolling();
  setQrProgress("expired", 0);
  elements.qrOverlayTitle.textContent = title;
  elements.qrOverlay.hidden = false;
  elements.cancelQrButton.disabled = true;
  qrSessionId = "";
  dashenSignerVersion = "";
  dashenLoginSigningContext = null;
  dashenSigningContext = null;
}

function selectedRole(): DashenRole | null {
  const selected = elements.roleList.querySelector<HTMLInputElement>(
    'input[name="dashen-role"]:checked',
  );
  return currentRoles.find((role) => role.role_id === selected?.value) ?? null;
}

function renderRoles(roles: DashenRole[]): void {
  currentRoles = roles;
  elements.roleList.replaceChildren();
  currentRoles.forEach((role, index) => {
    const label = document.createElement("label");
    label.className = "role-option";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "dashen-role";
    input.value = role.role_id;
    input.checked = index === 0;
    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = role.name;
    const meta = document.createElement("small");
    meta.textContent = `${role.server ? `${role.server} · ` : ""}战网账号 ID ${role.role_id}`;
    text.append(title, meta);
    label.append(input, text);
    elements.roleList.append(label);
  });
  elements.rolePanel.hidden = currentRoles.length <= 1;
}

function requireRoleSigningContext(): DashenSigningContext {
  if (!dashenSigningContext) {
    throw new Error("大神签名上下文已失效，请重新扫码");
  }
  return dashenSigningContext;
}

async function completeQrRole(role: DashenRole | null): Promise<void> {
  if (!role || !qrSessionId || completingQr) return;
  completingQr = true;
  stopQrPolling();
  setFlow(3);
  elements.rolePanel.hidden = currentRoles.length <= 1;
  setBusy(elements.completeQrButton, true, "转换中…");
  elements.qrStateBadge.textContent = "转换中";
  elements.qrStateTitle.textContent = "正在转换并保存 token";
  elements.qrStateText.textContent = "插件后端正在转换并安全保存凭证。";
  try {
    const signature = await createDashenSignature(
      dashenReportBody(role.role_id),
      requireRoleSigningContext(),
    );
    const status = parseStatus(
      await bridge.apiPost("dashen-auth/qr/complete", {
        session_id: qrSessionId,
        role_id: role.role_id,
        signature,
      }),
    );
    renderStatus(status);
    setQrProgress("success", 0);
    elements.qrOverlay.hidden = true;
    elements.rolePanel.hidden = true;
    elements.cancelQrButton.disabled = true;
    elements.startQrButton.textContent = "重新授权";
    qrSessionId = "";
    dashenSignerVersion = "";
    dashenLoginSigningContext = null;
    dashenSigningContext = null;
    showMessage("已完成：战网账号 ID 和 token 已安全保存并应用。", "success");
  } catch (error) {
    completingQr = false;
    setBusy(elements.completeQrButton, false);
    elements.rolePanel.hidden = false;
    setQrProgress("roles_ready", 0);
    showMessage(errorMessage(error, "token 转换失败，请重试"), "error");
  }
}

async function handleQrState(state: QrState): Promise<boolean> {
  setQrProgress(state.state, state.expires_in);
  if (state.state === "login_signature_required") {
    dashenLoginSigningContext = state.signing_context;
    dashenSigningContext = null;
    return true;
  }
  if (state.state === "signing_required") {
    dashenLoginSigningContext = null;
    dashenSigningContext = state.signing_context;
    // Mirror Dashen's signer reset after cst/time_diff changes.
    dashenSignerPromise = null;
    return true;
  }
  if (state.state === "roles_ready") {
    stopQrPolling();
    renderRoles(state.roles);
    const onlyRole = currentRoles.length === 1 ? currentRoles[0] : undefined;
    if (onlyRole) await completeQrRole(onlyRole);
    return false;
  }
  if (state.state === "expired" || state.state === "error") {
    expireQr(state.state === "expired" ? "二维码已失效" : "本次授权失败");
    return false;
  }
  return true;
}

function scheduleQrPoll(delay: number, generation: number): void {
  qrPollTimer = window.setTimeout(() => void pollQr(generation), delay);
}

async function pollQr(generation: number): Promise<void> {
  if (!qrSessionId || generation !== qrPollGeneration) return;
  try {
    const requestPayload: JsonRecord = {
      session_id: qrSessionId,
      signer_version: dashenSignerVersion,
    };
    if (dashenLoginSigningContext) {
      requestPayload["login_signature"] = await createDashenSignature(
        "null",
        dashenLoginSigningContext,
      );
    } else if (dashenSigningContext) {
      requestPayload["role_signature"] = await createDashenSignature(
        DASHEN_ROLE_BODY,
        dashenSigningContext,
      );
    }
    const state = parseQrState(
      await bridge.apiPost("dashen-auth/qr/status", requestPayload),
    );
    if (generation !== qrPollGeneration) return;
    const shouldContinue = await handleQrState(state);
    if (shouldContinue && qrSessionId && generation === qrPollGeneration) {
      scheduleQrPoll(state.poll_interval_ms || 2000, generation);
    }
  } catch (error) {
    if (generation !== qrPollGeneration) return;
    expireQr("本次授权未完成");
    showMessage(errorMessage(error, "读取扫码状态失败"), "error");
  }
}

async function startQr(): Promise<void> {
  if (qrSessionId) {
    const oldSessionId = qrSessionId;
    resetQrView();
    try {
      await bridge.apiPost("dashen-auth/qr/cancel", {
        session_id: oldSessionId,
      });
    } catch {
      // The previous session may already have expired.
    }
  } else {
    resetQrView();
  }
  setBusy(elements.startQrButton, true, "获取中…");
  elements.message.hidden = true;
  try {
    dashenSignerVersion = await getDashenSignerVersion();
    const state = parseQrStart(
      await bridge.apiPost("dashen-auth/qr/start", {}),
    );
    qrSessionId = state.session_id;
    elements.qrImage.src = state.qr_image;
    elements.qrStage.hidden = false;
    elements.qrOverlay.hidden = true;
    elements.rolePanel.hidden = true;
    elements.cancelQrButton.disabled = false;
    elements.startQrButton.textContent = "重新获取";
    setQrProgress("waiting_scan", state.expires_in);
    stopQrPolling();
    const generation = qrPollGeneration;
    scheduleQrPoll(state.poll_interval_ms || 2000, generation);
  } catch (error) {
    resetQrView();
    showMessage(errorMessage(error, "获取二维码失败"), "error");
  } finally {
    setBusy(elements.startQrButton, false);
    elements.startQrButton.textContent = qrSessionId ? "重新获取" : "获取二维码";
  }
}

async function cancelQr(): Promise<void> {
  const sessionId = qrSessionId;
  resetQrView();
  if (!sessionId) return;
  try {
    await bridge.apiPost("dashen-auth/qr/cancel", { session_id: sessionId });
  } catch (error) {
    showMessage(errorMessage(error, "取消扫码失败"), "warning");
  }
}

elements.startQrButton.addEventListener("click", () => void startQr());
elements.retryQrButton.addEventListener("click", () => void startQr());
elements.cancelQrButton.addEventListener("click", () => void cancelQr());
elements.completeQrButton.addEventListener("click", () => {
  void completeQrRole(selectedRole());
});

elements.credentialForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void (async () => {
    setBusy(elements.saveButton, true, "保存中…");
    try {
      const status = parseStatus(
        await bridge.apiPost("dashen-auth/save", {
          role_id: elements.roleInput.value.trim(),
          token: elements.tokenInput.value.trim(),
        }),
      );
      elements.credentialForm.reset();
      renderStatus(status);
      showMessage(
        status.legacy_config_cleared === false
          ? "凭证已保存并应用，但旧配置副本清理失败，请查看日志。"
          : "凭证已保存并应用到运行时。",
        status.legacy_config_cleared === false ? "warning" : "success",
      );
    } catch (error) {
      showMessage(errorMessage(error, "保存失败"), "error");
    } finally {
      elements.tokenInput.value = "";
      setBusy(elements.saveButton, false);
    }
  })();
});

elements.migrateButton.addEventListener("click", () => {
  void (async () => {
    setBusy(elements.migrateButton, true, "迁移中…");
    try {
      const status = parseStatus(
        await bridge.apiPost("dashen-auth/migrate", {}),
      );
      renderStatus(status);
      showMessage(
        status.legacy_config_cleared === false
          ? "凭证已迁移，但旧配置副本清理失败，请查看日志。"
          : "旧版插件配置已迁移到插件私有数据目录。",
        status.legacy_config_cleared === false ? "warning" : "success",
      );
    } catch (error) {
      showMessage(errorMessage(error, "迁移失败"), "error");
    } finally {
      setBusy(elements.migrateButton, false);
    }
  })();
});

elements.unbindButton.addEventListener("click", () => {
  elements.unbindConfirm.hidden = false;
});

elements.cancelUnbindButton.addEventListener("click", () => {
  elements.unbindConfirm.hidden = true;
});

elements.confirmUnbindButton.addEventListener("click", () => {
  void (async () => {
    setBusy(elements.confirmUnbindButton, true, "删除中…");
    try {
      const status = parseStatus(
        await bridge.apiPost("dashen-auth/unbind", {}),
      );
      renderStatus(status);
      elements.unbindConfirm.hidden = true;
      showMessage("大神凭证已删除。", "success");
    } catch (error) {
      showMessage(errorMessage(error, "解绑失败"), "error");
    } finally {
      setBusy(elements.confirmUnbindButton, false);
    }
  })();
});

elements.refreshButton.addEventListener("click", () => void refreshStatus());

await bridge.ready();
await refreshStatus();
