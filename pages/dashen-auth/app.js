const bridge = window.AstrBotPluginPage;
const DASHEN_ROLE_BODY = JSON.stringify({ appKey: "bn" });

const elements = {
  statusBadge: document.getElementById("statusBadge"),
  roleValue: document.getElementById("roleValue"),
  sourceValue: document.getElementById("sourceValue"),
  updatedValue: document.getElementById("updatedValue"),
  runtimeValue: document.getElementById("runtimeValue"),
  legacyWarning: document.getElementById("legacyWarning"),
  externalWarning: document.getElementById("externalWarning"),
  migrateButton: document.getElementById("migrateButton"),
  unbindButton: document.getElementById("unbindButton"),
  unbindConfirm: document.getElementById("unbindConfirm"),
  cancelUnbindButton: document.getElementById("cancelUnbindButton"),
  confirmUnbindButton: document.getElementById("confirmUnbindButton"),
  refreshButton: document.getElementById("refreshButton"),
  credentialForm: document.getElementById("credentialForm"),
  roleInput: document.getElementById("roleInput"),
  tokenInput: document.getElementById("tokenInput"),
  saveButton: document.getElementById("saveButton"),
  startQrButton: document.getElementById("startQrButton"),
  retryQrButton: document.getElementById("retryQrButton"),
  cancelQrButton: document.getElementById("cancelQrButton"),
  completeQrButton: document.getElementById("completeQrButton"),
  qrStage: document.getElementById("qrStage"),
  qrImage: document.getElementById("qrImage"),
  qrOverlay: document.getElementById("qrOverlay"),
  qrOverlayTitle: document.getElementById("qrOverlayTitle"),
  qrStateBadge: document.getElementById("qrStateBadge"),
  qrStateTitle: document.getElementById("qrStateTitle"),
  qrStateText: document.getElementById("qrStateText"),
  qrCountdown: document.getElementById("qrCountdown"),
  rolePanel: document.getElementById("rolePanel"),
  roleList: document.getElementById("roleList"),
  flowScan: document.getElementById("flowScan"),
  flowConfirm: document.getElementById("flowConfirm"),
  flowRole: document.getElementById("flowRole"),
  flowToken: document.getElementById("flowToken"),
  message: document.getElementById("message"),
};

let currentStatus = null;
let qrSessionId = "";
let qrPollTimer = null;
let qrPollGeneration = 0;
let currentRoles = [];
let completingQr = false;
let qrConfirmed = false;
let dashenSignerPromise = null;
let dashenSignerVersion = "";
let dashenLoginSigningContext = null;
let dashenSigningContext = null;

function dashenReportBody(roleId) {
  return JSON.stringify({
    appKey: "bn",
    roleId: String(roleId),
    server: "1",
    source: 1,
    type: "yearly",
  });
}

async function getDashenSigner() {
  if (!dashenSignerPromise) {
    dashenSignerPromise = (async () => {
      if (!window.sig || typeof window.sig.default !== "function") {
        throw new Error("大神签名组件未能加载，请刷新页面后重试");
      }
      if (!window.__dashenSigWasmUrl) {
        const payload = await bridge.apiGet("dashen-auth/signer/wasm");
        const encoded = String(payload?.wasm_base64 || "");
        if (!encoded) throw new Error("大神签名组件数据为空，请重新安装插件");
        const binary = window.atob(encoded);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) {
          bytes[index] = binary.charCodeAt(index);
        }
        window.__dashenSigWasmUrl = URL.createObjectURL(
          new Blob([bytes], { type: "application/wasm" }),
        );
      }
      const signer = await window.sig.default();
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

function normalizeDashenSigningContext(value) {
  const csrf = String(value?.csrf || "").trim();
  const cst = String(value?.cst || "").trim();
  const timeDiff = String(value?.time_diff ?? "0").trim();
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

function normalizeDashenLoginSigningContext(value) {
  const csrf = String(value?.csrf || "").trim();
  if (!csrf || csrf.length > 512) {
    throw new Error("大神登录未返回有效 XSRF 上下文，请重新扫码");
  }
  return { csrf };
}

async function getDashenSignerVersion() {
  const signer = await getDashenSigner();
  if (typeof signer.get_version !== "function") {
    throw new Error("大神签名组件缺少版本信息，请重新安装插件");
  }
  const version = String(signer.get_version() || "").trim();
  if (!version || version.length > 256 || /[\x00-\x20\x7f]/.test(version)) {
    throw new Error("大神签名组件版本无效，请刷新页面后重试");
  }
  return version;
}

async function createDashenSignature(body, signingContext, requireCst = true) {
  const context = requireCst
    ? normalizeDashenSigningContext(signingContext)
    : normalizeDashenLoginSigningContext(signingContext);
  const previousLocalData = window.__dashenSigLocalData;
  window.__dashenSigLocalData = requireCst
    ? { csrf: context.csrf, cst: context.cst, time_diff: context.time_diff }
    : { csrf: context.csrf };
  try {
    const signer = await getDashenSigner();
    const rawResult = signer.gen_sign(body);
    const result = typeof rawResult === "string" ? JSON.parse(rawResult) : rawResult;
    const sign = String(result?.sign || "").trim();
    const timestamp = Number(result?.timestamp);
    if (!sign || !Number.isSafeInteger(timestamp)) {
      throw new Error("大神签名结果无效，请刷新页面后重试");
    }
    return { sign, timestamp, user_agent: navigator.userAgent };
  } finally {
    if (previousLocalData === undefined) delete window.__dashenSigLocalData;
    else window.__dashenSigLocalData = previousLocalData;
  }
}

function setBusy(button, busy, busyText) {
  if (!button) return;
  if (busy && !button.dataset.label) {
    button.dataset.label = button.textContent;
    button.textContent = busyText;
  } else if (!busy && button.dataset.label) {
    button.textContent = button.dataset.label;
    delete button.dataset.label;
  }
  button.disabled = busy;
}

function showMessage(text, kind = "success") {
  elements.message.textContent = text;
  elements.message.className = `message message-${kind}`;
  elements.message.hidden = false;
}

function sourceLabel(source) {
  return {
    plugin_kv: "插件独立 KV",
    plugin_config: "旧版插件配置",
    none: "未配置",
  }[source] || "未知";
}

function formatTimestamp(timestamp) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(timestamp * 1000));
}

function renderStatus(status) {
  currentStatus = status;
  const configured = Boolean(status.configured);
  elements.statusBadge.textContent = configured ? "已配置" : "未配置";
  elements.statusBadge.className = configured ? "badge badge-ok" : "badge badge-muted";
  elements.roleValue.textContent = status.role_id_masked || "—";
  elements.sourceValue.textContent = sourceLabel(status.source);
  elements.updatedValue.textContent = formatTimestamp(status.updated_at);
  elements.runtimeValue.textContent = !status.embedded_overstats
    ? "外部服务模式"
    : status.runtime_applied
      ? "已热更新"
      : configured
        ? "等待服务启动"
        : "等待凭证";
  elements.legacyWarning.hidden = !status.legacy_config_present;
  elements.externalWarning.hidden = Boolean(status.embedded_overstats);
  elements.migrateButton.hidden = !status.can_migrate;
  elements.unbindButton.disabled = !configured && !status.legacy_config_present;
}

async function refreshStatus() {
  setBusy(elements.refreshButton, true, "刷新中…");
  try {
    renderStatus(await bridge.apiGet("dashen-auth/status"));
  } catch (error) {
    showMessage(error.message || "读取状态失败", "error");
  } finally {
    setBusy(elements.refreshButton, false);
  }
}

function stopQrPolling() {
  qrPollGeneration += 1;
  if (qrPollTimer) window.clearTimeout(qrPollTimer);
  qrPollTimer = null;
}

function setFlow(activeIndex) {
  const items = [elements.flowScan, elements.flowConfirm, elements.flowRole, elements.flowToken];
  items.forEach((item, index) => {
    item.classList.toggle("is-done", index < activeIndex);
    item.classList.toggle("is-current", index === activeIndex);
  });
}

function setQrProgress(state, expiresIn = 0) {
  if (
    [
      "processing",
      "login_signature_required",
      "signing_required",
      "roles_ready",
      "success",
    ].includes(state)
  ) {
    qrConfirmed = true;
  }
  const content = {
    waiting_scan: ["等待扫码", "打开网易大神 App 扫描二维码", "扫码后请在手机上确认登录，本页会自动继续。", 0],
    waiting_confirm: ["等待确认", "已扫码，请在手机上确认", "不要关闭本页，确认后会自动识别战网账号 ID。", 1],
    processing: ["处理中", "登录成功，正在读取战网账号 ID", "正在建立临时网页会话并查询已绑定的战网账号。", 2],
    login_signature_required: ["处理中", "正在建立签名会话", "正在使用官方签名组件完成大神网页登录。", 2],
    signing_required: ["处理中", "正在生成会话签名", "使用本次登录的临时上下文读取战网账号 ID。", 2],
    roles_ready: ["账号已找到", "请选择要使用的战网账号 ID", "选择后将转换 token 并安全保存。", 2],
    success: ["已完成", "授权与保存完成", "战网账号 ID 和 token 已保存，运行中的内置 Overstats 已同步更新。", 4],
    expired: ["已过期", "二维码已失效", "请重新获取二维码。", 0],
    error: ["授权失败", "扫码流程未完成", "请重新获取二维码后再试。", 0],
  }[state] || ["处理中", "正在处理", "请稍候。", 2];
  elements.qrStateBadge.textContent = content[0];
  elements.qrStateBadge.className = `badge ${state === "success" ? "badge-ok" : "badge-active"}`;
  elements.qrStateTitle.textContent = content[1];
  elements.qrStateText.textContent = content[2];
  elements.qrCountdown.textContent = expiresIn > 0 ? `二维码约 ${expiresIn} 秒后过期` : "";
  elements.qrImage.classList.toggle("is-obscured", qrConfirmed);
  elements.flowToken.textContent = state === "success" ? "已完成" : "转换并保存 token";
  setFlow(content[3]);
}

function resetQrView({ keepSuccess = false } = {}) {
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

function expireQr(title = "二维码已失效") {
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

function selectedRole() {
  const selected = elements.roleList.querySelector('input[name="dashen-role"]:checked');
  return currentRoles.find((role) => role.role_id === selected?.value) || null;
}

function renderRoles(roles) {
  currentRoles = Array.isArray(roles) ? roles : [];
  elements.roleList.replaceChildren();
  currentRoles.forEach((role, index) => {
    const label = document.createElement("label");
    label.className = "role-option";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "dashen-role";
    input.value = String(role.role_id);
    input.checked = index === 0;
    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = role.name || "战网账号";
    const meta = document.createElement("small");
    meta.textContent = `${role.server ? `${role.server} · ` : ""}战网账号 ID ${role.role_id}`;
    text.append(title, meta);
    label.append(input, text);
    elements.roleList.append(label);
  });
  elements.rolePanel.hidden = currentRoles.length <= 1;
}

async function completeQrRole(role) {
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
      dashenSigningContext,
    );
    const status = await bridge.apiPost("dashen-auth/qr/complete", {
      session_id: qrSessionId,
      role_id: role.role_id,
      signature,
    });
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
    showMessage(error.message || "token 转换失败，请重试", "error");
  }
}

async function handleQrState(state) {
  setQrProgress(state.state, state.expires_in);
  if (state.state === "login_signature_required") {
    dashenLoginSigningContext = normalizeDashenLoginSigningContext(state.signing_context);
    dashenSigningContext = null;
    return true;
  }
  if (state.state === "signing_required") {
    dashenLoginSigningContext = null;
    dashenSigningContext = normalizeDashenSigningContext(state.signing_context);
    // Mirror Dashen's signer reset after cst/time_diff changes.
    dashenSignerPromise = null;
    return true;
  }
  if (state.state === "roles_ready") {
    stopQrPolling();
    renderRoles(state.roles);
    if (currentRoles.length === 1) await completeQrRole(currentRoles[0]);
    return false;
  }
  if (state.state === "expired" || state.state === "error") {
    expireQr(state.state === "expired" ? "二维码已失效" : "本次授权失败");
    return false;
  }
  return true;
}

function scheduleQrPoll(delay, generation) {
  qrPollTimer = window.setTimeout(() => pollQr(generation), delay);
}

async function pollQr(generation) {
  if (!qrSessionId || generation !== qrPollGeneration) return;
  try {
    const requestPayload = {
      session_id: qrSessionId,
      signer_version: dashenSignerVersion,
    };
    if (dashenLoginSigningContext) {
      requestPayload.login_signature = await createDashenSignature(
        "null",
        dashenLoginSigningContext,
        false,
      );
    } else if (dashenSigningContext) {
      requestPayload.role_signature = await createDashenSignature(
        DASHEN_ROLE_BODY,
        dashenSigningContext,
      );
    }
    const state = await bridge.apiPost("dashen-auth/qr/status", requestPayload);
    if (generation !== qrPollGeneration) return;
    const shouldContinue = await handleQrState(state);
    if (shouldContinue && qrSessionId && generation === qrPollGeneration) {
      scheduleQrPoll(state.poll_interval_ms || 2000, generation);
    }
  } catch (error) {
    if (generation !== qrPollGeneration) return;
    expireQr("本次授权未完成");
    showMessage(error.message || "读取扫码状态失败", "error");
  }
}

async function startQr() {
  if (qrSessionId) {
    const oldSessionId = qrSessionId;
    resetQrView();
    try {
      await bridge.apiPost("dashen-auth/qr/cancel", { session_id: oldSessionId });
    } catch (_error) {
      // The previous session may already have expired.
    }
  } else {
    resetQrView();
  }
  setBusy(elements.startQrButton, true, "获取中…");
  elements.message.hidden = true;
  try {
    dashenSignerVersion = await getDashenSignerVersion();
    const state = await bridge.apiPost("dashen-auth/qr/start", {});
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
    showMessage(error.message || "获取二维码失败", "error");
  } finally {
    setBusy(elements.startQrButton, false);
    elements.startQrButton.textContent = qrSessionId ? "重新获取" : "获取二维码";
  }
}

async function cancelQr() {
  const sessionId = qrSessionId;
  resetQrView();
  if (!sessionId) return;
  try {
    await bridge.apiPost("dashen-auth/qr/cancel", { session_id: sessionId });
  } catch (error) {
    showMessage(error.message || "取消扫码失败", "warning");
  }
}

elements.startQrButton.addEventListener("click", startQr);
elements.retryQrButton.addEventListener("click", startQr);
elements.cancelQrButton.addEventListener("click", cancelQr);
elements.completeQrButton.addEventListener("click", () => completeQrRole(selectedRole()));

elements.credentialForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setBusy(elements.saveButton, true, "保存中…");
  try {
    const status = await bridge.apiPost("dashen-auth/save", {
      role_id: elements.roleInput.value.trim(),
      token: elements.tokenInput.value.trim(),
    });
    elements.credentialForm.reset();
    renderStatus(status);
    showMessage(
      status.legacy_config_cleared === false
        ? "凭证已保存并应用，但旧配置副本清理失败，请查看日志。"
        : "凭证已保存并应用到运行时。",
      status.legacy_config_cleared === false ? "warning" : "success",
    );
  } catch (error) {
    showMessage(error.message || "保存失败", "error");
  } finally {
    elements.tokenInput.value = "";
    setBusy(elements.saveButton, false);
  }
});

elements.migrateButton.addEventListener("click", async () => {
  setBusy(elements.migrateButton, true, "迁移中…");
  try {
    const status = await bridge.apiPost("dashen-auth/migrate", {});
    renderStatus(status);
    showMessage(
      status.legacy_config_cleared === false
        ? "凭证已迁移，但旧配置副本清理失败，请查看日志。"
        : "旧版插件配置已迁移到插件独立 KV。",
      status.legacy_config_cleared === false ? "warning" : "success",
    );
  } catch (error) {
    showMessage(error.message || "迁移失败", "error");
  } finally {
    setBusy(elements.migrateButton, false);
  }
});

elements.unbindButton.addEventListener("click", () => {
  elements.unbindConfirm.hidden = false;
});

elements.cancelUnbindButton.addEventListener("click", () => {
  elements.unbindConfirm.hidden = true;
});

elements.confirmUnbindButton.addEventListener("click", async () => {
  setBusy(elements.confirmUnbindButton, true, "删除中…");
  try {
    const status = await bridge.apiPost("dashen-auth/unbind", {});
    renderStatus(status);
    elements.unbindConfirm.hidden = true;
    showMessage("大神凭证已删除。", "success");
  } catch (error) {
    showMessage(error.message || "解绑失败", "error");
  } finally {
    setBusy(elements.confirmUnbindButton, false);
  }
});

elements.refreshButton.addEventListener("click", refreshStatus);

await bridge.ready();
await refreshStatus();
