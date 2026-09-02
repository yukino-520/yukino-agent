const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const elements = {
  shell: $(".club-shell"),
  chatRoom: $("#conversation-room"),
  activityClock: $("#activity-clock"),
  messages: $("#messages"),
  form: $("#composer"),
  input: $("#input"),
  send: $("#send"),
  attachmentButton: $("#attachment-button"),
  attachmentInput: $("#attachment-input"),
  attachmentTray: $("#attachment-tray"),
  attachmentHint: $("#attachment-hint"),
  characterList: $("#character-list"),
  conversationScopeLabel: $("#conversation-scope-label"),
  conversationList: $("#conversation-list"),
  newConversation: $("#new-conversation"),
  activeCharacter: $("#active-character"),
  arrivalSpeaker: $("#arrival-speaker"),
  modeKicker: $("#mode-kicker"),
  meetingLead: $("#meeting-lead"),
  activeMode: $("#active-mode"),
  activeEmotion: $("#active-emotion"),
  activeRoute: $("#active-route"),
  degradedState: $("#degraded-state"),
  capabilityToggle: $("#capability-toggle"),
  capabilityCount: $("#capability-count"),
  capabilityDialog: $("#capability-dialog"),
  settingsClose: $("#settings-close"),
  settingsForm: $("#settings-form"),
  settingsSummary: $("#settings-summary"),
  settingsOutput: $("#settings-output"),
  settingsSave: $("#settings-save"),
  settingModelStatus: $("#setting-model-status"),
  settingApiKeyMask: $("#setting-api-key-mask"),
  settingApiKey: $("#setting-api-key"),
  settingClearApiKey: $("#setting-clear-api-key"),
  settingBaseUrl: $("#setting-base-url"),
  settingModel: $("#setting-model"),
  settingFallbackModel: $("#setting-fallback-model"),
  settingEmbeddingApiKeyMask: $("#setting-embedding-api-key-mask"),
  settingEmbeddingApiKey: $("#setting-embedding-api-key"),
  settingClearEmbeddingApiKey: $("#setting-clear-embedding-api-key"),
  settingEmbeddingBaseUrl: $("#setting-embedding-base-url"),
  settingEmbeddingModel: $("#setting-embedding-model"),
  settingEmbeddingTimeout: $("#setting-embedding-timeout"),
  settingTimeout: $("#setting-timeout"),
  settingModelMaxConcurrent: $("#setting-model-max-concurrent"),
  settingModelCircuitFailures: $("#setting-model-circuit-failures"),
  settingModelCircuitCooldown: $("#setting-model-circuit-cooldown"),
  settingModelRuntime: $("#setting-model-runtime"),
  settingAgentStatus: $("#setting-agent-status"),
  settingAgentEnabled: $("#setting-agent-enabled"),
  settingAgentMaxSteps: $("#setting-agent-max-steps"),
  settingAgentWorkers: $("#setting-agent-workers"),
  settingAgentQueueCapacity: $("#setting-agent-queue-capacity"),
  settingAgentSessionQueueLimit: $("#setting-agent-session-queue-limit"),
  settingAgentQueueState: $("#setting-agent-queue-state"),
  settingPythonSandbox: $("#setting-python-sandbox"),
  settingPythonSandboxDetail: $("#setting-python-sandbox-detail"),
  settingPythonSandboxState: $("#setting-python-sandbox-state"),
  settingTaskWallTime: $("#setting-task-wall-time"),
  settingTaskModelTokens: $("#setting-task-model-tokens"),
  settingTaskModelCost: $("#setting-task-model-cost"),
  settingTaskToolCalls: $("#setting-task-tool-calls"),
  settingTaskModelCalls: $("#setting-task-model-calls"),
  settingModelMaxOutputTokens: $("#setting-model-max-output-tokens"),
  settingInputTokenCost: $("#setting-input-token-cost"),
  settingOutputTokenCost: $("#setting-output-token-cost"),
  settingCostCurrency: $("#setting-cost-currency"),
  settingTimezone: $("#setting-timezone"),
  settingAllowDesktop: $("#setting-allow-desktop"),
  settingAgentWriteDirs: $("#setting-agent-write-dirs"),
  settingAgentSystemTasks: $("#setting-agent-system-tasks"),
  settingAgentSystemTasksState: $("#setting-agent-system-tasks-state"),
  settingAgentWorkspace: $("#setting-agent-workspace"),
  settingAgentProject: $("#setting-agent-project"),
  settingAgentDesktop: $("#setting-agent-desktop"),
  settingStorageStatus: $("#setting-storage-status"),
  settingFactBackend: $("#setting-fact-backend"),
  settingFactLocation: $("#setting-fact-location"),
  settingVectorBackend: $("#setting-vector-backend"),
  settingMilvusUri: $("#setting-milvus-uri"),
  settingMilvusToken: $("#setting-milvus-token"),
  settingMilvusTokenMask: $("#setting-milvus-token-mask"),
  settingClearMilvusToken: $("#setting-clear-milvus-token"),
  settingMilvusDatabase: $("#setting-milvus-database"),
  settingMilvusPrefix: $("#setting-milvus-prefix"),
  settingGraphBackend: $("#setting-graph-backend"),
  settingNeo4jUri: $("#setting-neo4j-uri"),
  settingNeo4jUsername: $("#setting-neo4j-username"),
  settingNeo4jPassword: $("#setting-neo4j-password"),
  settingNeo4jPasswordMask: $("#setting-neo4j-password-mask"),
  settingClearNeo4jPassword: $("#setting-clear-neo4j-password"),
  settingNeo4jDatabase: $("#setting-neo4j-database"),
  settingStorageRuntime: $("#setting-storage-runtime"),
  settingPermissionStatus: $("#setting-permission-status"),
  settingPermissionRefresh: $("#setting-permission-refresh"),
  settingPermissionList: $("#setting-permission-list"),
  settingPermissionGrantCapability: $("#setting-permission-grant-capability"),
  settingPermissionGrantAction: $("#setting-permission-grant-action"),
  settingPermissionGrantTtl: $("#setting-permission-grant-ttl"),
  settingPermissionGrantUses: $("#setting-permission-grant-uses"),
  settingPermissionGrantCreate: $("#setting-permission-grant-create"),
  settingPermissionOutput: $("#setting-permission-output"),
  settingPermissionGrants: $("#setting-permission-grants"),
  settingVoiceStatus: $("#setting-voice-status"),
  settingTtsEnabled: $("#setting-tts-enabled"),
  settingTtsModel: $("#setting-tts-model"),
  settingVoiceTestCharacter: $("#setting-voice-test-character"),
  settingVoicePreview: $("#setting-voice-preview"),
  settingStickerStatus: $("#setting-sticker-status"),
  stickerCoverage: $("#sticker-coverage"),
  settingStickerCharacter: $("#setting-sticker-character"),
  settingStickerEmotion: $("#setting-sticker-emotion"),
  settingStickerFile: $("#setting-sticker-file"),
  settingStickerUpload: $("#setting-sticker-upload"),
  settingStickerOutput: $("#setting-sticker-output"),
  settingMcpStatus: $("#setting-mcp-status"),
  settingMcpEndpoints: $("#setting-mcp-endpoints"),
  settingMcpHeaders: $("#setting-mcp-headers"),
  settingMcpHeaderMask: $("#setting-mcp-header-mask"),
  settingClearMcpHeaders: $("#setting-clear-mcp-headers"),
  settingMcpStdioEnabled: $("#setting-mcp-stdio-enabled"),
  settingMcpStdioServers: $("#setting-mcp-stdio-servers"),
  settingMcpStdioEnv: $("#setting-mcp-stdio-env"),
  settingMcpStdioEnvMask: $("#setting-mcp-stdio-env-mask"),
  settingClearMcpStdioEnv: $("#setting-clear-mcp-stdio-env"),
  settingMcpPrivateHostnames: $("#setting-mcp-private-hostnames"),
  settingMcpTransportSecurity: $("#setting-mcp-transport-security"),
  settingMcpDiscovery: $("#setting-mcp-discovery"),
  settingMcpTestServer: $("#setting-mcp-test-server"),
  settingConnectionStatus: $("#setting-connection-status"),
  settingWebhook: $("#setting-webhook"),
  settingWebhookSecret: $("#setting-webhook-secret"),
  settingWebhookSecretMask: $("#setting-webhook-secret-mask"),
  settingClearWebhookSecret: $("#setting-clear-webhook-secret"),
  settingInboundSecret: $("#setting-inbound-secret"),
  settingInboundSecretMask: $("#setting-inbound-secret-mask"),
  settingClearInboundSecret: $("#setting-clear-inbound-secret"),
  settingBackgroundReminderDelivery: $("#setting-background-reminder-delivery"),
  settingBackgroundDeliveryState: $("#setting-background-delivery-state"),
  settingSmtpHost: $("#setting-smtp-host"),
  settingSmtpPort: $("#setting-smtp-port"),
  settingSmtpUsername: $("#setting-smtp-username"),
  settingSmtpSender: $("#setting-smtp-sender"),
  settingSmtpPassword: $("#setting-smtp-password"),
  settingSmtpPasswordMask: $("#setting-smtp-password-mask"),
  settingClearSmtpPassword: $("#setting-clear-smtp-password"),
  settingDispatchStatus: $("#setting-dispatch-status"),
  settingDispatchFilter: $("#setting-dispatch-filter"),
  settingDispatchRefresh: $("#setting-dispatch-refresh"),
  settingDispatchOutput: $("#setting-dispatch-output"),
  settingDispatchList: $("#setting-dispatch-list"),
  enableSticker: $("#enable-sticker"),
  enableVoice: $("#enable-voice"),
  clearMemory: $("#clear-memory"),
  memoryToggle: $("#memory-toggle"),
  memoryDialog: $("#memory-dialog"),
  memoryClose: $("#memory-close"),
  memoryLoadState: $("#memory-load-state"),
  memorySearchForm: $("#memory-search-form"),
  memorySearch: $("#memory-search"),
  memorySearchReset: $("#memory-search-reset"),
  memoryPermanentCount: $("#memory-permanent-count"),
  memoryLocalCount: $("#memory-local-count"),
  memoryReminderCount: $("#memory-reminder-count"),
  memoryQueryLabel: $("#memory-query-label"),
  memoryPermanentList: $("#memory-permanent-list"),
  memoryLocalList: $("#memory-local-list"),
  memoryConversationList: $("#memory-conversation-list"),
  memoryReminderArchiveList: $("#memory-reminder-list"),
  characterHint: $("#character-hint"),
  toastRegion: $("#toast-region"),
};

const modeButtons = $$('.mode-switch button[data-mode]');
const LEGACY_SESSION_ID_KEY = "agi-yukino:session";
const LEGACY_MESSAGES_KEY = "agi-yukino:messages";
const CONVERSATION_MIGRATION_KEY = "agi-yukino:conversation-migration-v1";
const ACTIVE_CONVERSATIONS_KEY = "agi-yukino:active-conversations";
const PREFERENCES_KEY = "agi-yukino:preferences";
const PENDING_CHAT_JOBS_KEY = "agi-yukino:pending-chat-jobs-v1";
const activeChatPolls = new Map();

const state = {
  sessionId: "",
  messages: [],
  chatMode: "club",
  character: "yukino",
  characters: [],
  conversations: [],
  chatOpen: false,
  activeConversations: restoreActiveConversations(),
  pendingChatJobs: restorePendingChatJobs(),
  scopeLoadToken: 0,
  capabilities: [],
  settings: null,
  pendingNode: null,
  attachments: [],
  attachmentUploading: false,
  conversationBusy: false,
  submitting: false,
  resumeTaskId: "",
  resumeGoal: "",
  allowUncertainReplay: false,
  runtimeSocket: null,
  dispatchReviewBusy: false,
  permissionPolicy: null,
  permissionPolicyBusy: false,
};

const emotionNames = {
  happy: "轻松", sad: "低落", anxious: "不安", angry: "生气",
  lonely: "孤独", shy: "害羞", thinking: "思考", neutral: "平静",
};

function restoreActiveConversations() {
  try {
    const stored = JSON.parse(localStorage.getItem(ACTIVE_CONVERSATIONS_KEY) || "{}");
    return stored && typeof stored === "object" && !Array.isArray(stored) ? stored : {};
  } catch {
    return {};
  }
}

function restorePendingChatJobs() {
  try {
    const stored = JSON.parse(localStorage.getItem(PENDING_CHAT_JOBS_KEY) || "{}");
    return stored && typeof stored === "object" && !Array.isArray(stored) ? stored : {};
  } catch {
    return {};
  }
}

function persistPendingChatJobs() {
  try {
    localStorage.setItem(PENDING_CHAT_JOBS_KEY, JSON.stringify(state.pendingChatJobs));
  } catch { /* in-memory polling still works when browser storage is unavailable */ }
}

function rememberPendingChatJob(record) {
  state.pendingChatJobs[record.sessionId] = record;
  persistPendingChatJobs();
}

function forgetPendingChatJob(sessionId, jobId = "") {
  const current = state.pendingChatJobs[sessionId];
  if (!current || (jobId && current.jobId !== jobId)) return;
  delete state.pendingChatJobs[sessionId];
  persistPendingChatJobs();
}

function conversationScopeKey() {
  return state.chatMode === "club" ? "club" : `solo:${state.character}`;
}

function persistActiveConversations() {
  localStorage.setItem(ACTIVE_CONVERSATIONS_KEY, JSON.stringify(state.activeConversations));
}

function savePreferences() {
  localStorage.setItem(PREFERENCES_KEY, JSON.stringify({
    chatMode: state.chatMode,
    character: state.character,
  }));
}

function restorePreferences() {
  try {
    const preferences = JSON.parse(localStorage.getItem(PREFERENCES_KEY) || "{}");
    if (["solo", "club"].includes(preferences.chatMode)) state.chatMode = preferences.chatMode;
    if (typeof preferences.character === "string") state.character = preferences.character;
  } catch { /* use product defaults */ }
}

function showToast(message) {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = message;
  elements.toastRegion.appendChild(toast);
  setTimeout(() => toast.remove(), 3200);
}

function updateActivityClock() {
  const now = new Date();
  const hours = String(now.getHours()).padStart(2, "0");
  const minutes = String(now.getMinutes()).padStart(2, "0");
  const currentTime = `${hours}:${minutes}`;
  elements.activityClock.textContent = currentTime;
  elements.activityClock.setAttribute("aria-label", `当前时间 ${currentTime}`);
  elements.activityClock.title = now.toLocaleString("zh-CN", { hour12: false });
}

function selectedProfile() {
  return state.characters.find((item) => item.id === state.character);
}

function addMessage(role, content, meta = "", options = {}) {
  const node = document.createElement("article");
  node.className = `message ${role}${options.pending ? " pending" : ""}`;
  node.textContent = content;
  if (meta) node.dataset.meta = meta;
  if (role === "assistant") {
    const speaker = state.characters.find((item) => item.id === options.character) || selectedProfile();
    node.style.setProperty("--speaker-accent", speaker?.accent_color || "#7193b6");
  }
  if (options.attachments?.length) {
    const attachmentList = document.createElement("div");
    attachmentList.className = "message-attachments";
    renderAttachmentChips(attachmentList, options.attachments);
    node.appendChild(attachmentList);
  }
  if (options.stickerUrl) {
    const sticker = document.createElement("img");
    sticker.className = "message-sticker";
    sticker.src = options.stickerUrl;
    sticker.alt = "角色情绪贴纸";
    node.appendChild(sticker);
  }
  if (options.audioUrl) {
    const audio = document.createElement("audio");
    audio.className = "message-audio";
    audio.controls = true;
    audio.preload = "metadata";
    audio.src = options.audioUrl;
    node.appendChild(audio);
  }
  if (options.toolResults?.length) renderToolResults(node, options.toolResults);
  if (options.execution?.status) renderExecutionStatus(node, options.execution);
  if (options.pending && options.requestId) {
    node.dataset.pendingSessionId = options.sessionId || state.sessionId;
    if (options.jobId) node.dataset.chatJobId = options.jobId;
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "pending-cancel";
    cancel.textContent = "停止任务";
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      cancel.textContent = "正在停止…";
      try {
        const sessionId = node.dataset.pendingSessionId || state.sessionId;
        const jobId = node.dataset.chatJobId;
        const result = jobId
          ? await requestJson(`/api/chat/jobs/${encodeURIComponent(jobId)}/cancel`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId, confirmed: true }),
          })
          : await requestJson("/api/agent/tasks/cancel", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId, request_id: options.requestId }),
          });
        cancel.textContent = result.ok ? "已请求停止" : "任务已结束";
        showToast(result.ok ? "已请求停止当前任务" : "当前任务已经结束");
      } catch {
        cancel.disabled = false;
        cancel.textContent = "停止任务";
        showToast("任务尚未建立，请稍后再试");
      }
    });
    node.appendChild(cancel);
  }
  if (options.pending && options.taskId) ensureAgentTimeline(node, options.taskId);
  elements.messages.appendChild(node);
  elements.messages.scrollTop = elements.messages.scrollHeight;
  return node;
}

function formatAttachmentSize(size) {
  const bytes = Number(size) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderAttachmentChips(target, attachments, { removable = false } = {}) {
  target.replaceChildren();
  attachments.forEach((attachment) => {
    const chip = document.createElement("span");
    chip.className = "attachment-chip";
    chip.dataset.kind = attachment.kind || "file";
    const icon = document.createElement("span");
    icon.textContent = attachment.kind === "image" ? "◇" : attachment.kind === "code" ? "⌘" : "▧";
    const name = document.createElement("strong");
    name.textContent = attachment.original_name || attachment.filename || "附件";
    name.title = name.textContent;
    const size = document.createElement("small");
    size.textContent = formatAttachmentSize(attachment.size);
    chip.append(icon, name, size);
    if (removable) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.removeAttachment = attachment.id;
      remove.setAttribute("aria-label", `移除附件 ${name.textContent}`);
      remove.textContent = "×";
      chip.appendChild(remove);
    }
    target.appendChild(chip);
  });
}

function syncComposerAvailability() {
  const hasPendingChat = Boolean(state.sessionId && state.pendingChatJobs[state.sessionId]);
  const blocked = state.conversationBusy || state.submitting || state.attachmentUploading || hasPendingChat;
  elements.send.disabled = blocked;
  elements.attachmentButton.disabled = blocked || !state.sessionId || state.attachments.length >= 4;
  elements.attachmentHint.textContent = state.attachmentUploading
    ? "正在安全上传……"
    : state.attachments.length
      ? `${state.attachments.length}/4 个附件已就绪`
      : "代码、文档、图片 · 最多 4 个";
}

function renderComposerAttachments() {
  renderAttachmentChips(elements.attachmentTray, state.attachments, { removable: true });
  syncComposerAvailability();
}

const toolNames = {
  analyze_code: "代码检查", analyze_file: "附件代码检查", write_file: "创建文件", edit_file: "修改文件",
  confirm_file_operation: "确认写入", cancel_file_operation: "取消写入",
  rollback_file_operation: "回滚文件",
  capability_call: "能力调用",
  confirm_capability_operation: "确认执行", cancel_capability_operation: "取消执行",
  read_file: "读取文件", list_files: "列出文件", search_files: "搜索文件",
  run_python: "Python 沙箱", web_search: "网页搜索", web_fetch: "读取网页",
  remember: "写入记忆", recall: "检索记忆",
};

function renderToolResults(messageNode, results) {
  const tray = document.createElement("div");
  tray.className = "tool-result-tray";
  results.filter((result) => !result.audit?.deduplicated).forEach((result) => {
    const needsConfirmation = Boolean(result.audit?.requires_confirmation);
    const backgroundPending = Boolean(result.audit?.background_job_pending);
    const card = document.createElement("section");
    card.className = `tool-result ${backgroundPending ? "background" : (result.success ? "success" : (needsConfirmation ? "confirm" : "error"))}`;
    const heading = document.createElement("strong");
    heading.textContent = `${backgroundPending ? "↻" : (result.success ? "✓" : (needsConfirmation ? "!" : "×"))} ${toolNames[result.action] || result.action}`;
    const detail = document.createElement("small");
    detail.textContent = String(result.content || result.error || (backgroundPending ? "已进入后台队列" : "工具已执行")).slice(0, 420);
    card.append(heading, detail);
    renderWorkflowProgress(card, result.audit);
    if (needsConfirmation && result.audit?.operation_id) {
      const confirmationAction = result.audit.confirmation_action || "confirm_file_operation";
      const cancelAction = result.audit.cancel_action || "cancel_file_operation";
      const isCapability = confirmationAction === "confirm_capability_operation";
      const actions = document.createElement("div");
      actions.className = "tool-confirm-actions";
      const confirm = document.createElement("button");
      confirm.type = "button";
      confirm.textContent = isCapability ? "确认执行" : "确认写入";
      confirm.addEventListener("click", () => submitToolConfirmation(confirmationAction, result.audit.operation_id));
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "取消";
      cancel.addEventListener("click", () => submitToolConfirmation(cancelAction, result.audit.operation_id));
      actions.append(confirm, cancel);
      card.appendChild(actions);
    }
    if (result.success && result.audit?.rollback_operation_id) {
      const actions = document.createElement("div");
      actions.className = "tool-confirm-actions";
      const rollback = document.createElement("button");
      rollback.type = "button";
      rollback.textContent = "回滚这次修改";
      rollback.addEventListener("click", () => submitToolConfirmation(
        "rollback_file_operation",
        result.audit.rollback_operation_id,
      ));
      actions.appendChild(rollback);
      card.appendChild(actions);
    }
    tray.appendChild(card);
  });
  if (tray.childElementCount) messageNode.appendChild(tray);
}

function renderWorkflowProgress(container, audit = {}) {
  const steps = Array.isArray(audit?.workflow_steps) ? audit.workflow_steps : [];
  if (!steps.length) return;
  const panel = document.createElement("div");
  panel.className = "workflow-progress";
  const heading = document.createElement("small");
  heading.textContent = `工作流 · ${audit.workflow_status || "unknown"} · ${audit.workflow_run_id || "-"}`;
  panel.appendChild(heading);
  const list = document.createElement("ol");
  steps.forEach((step) => {
    const item = document.createElement("li");
    item.dataset.state = step.status || "unknown";
    item.textContent = `${step.id || "步骤"} · ${step.capability || "能力"}.${step.operation || "操作"} · ${step.status || "unknown"}`;
    list.appendChild(item);
  });
  panel.appendChild(list);
  if (["failed", "interrupted", "cancelled"].includes(audit.workflow_status) && audit.workflow_run_id) {
    const resume = document.createElement("button");
    resume.type = "button";
    resume.className = "workflow-resume";
    resume.textContent = "恢复未完成步骤";
    resume.addEventListener("click", () => {
      elements.input.value = `请恢复工作流：${audit.workflow_run_id}`;
      elements.input.focus();
      showToast("恢复请求已放入输入框；发送后仍需确认，已完成步骤不会重放");
    });
    panel.appendChild(resume);
  }
  container.appendChild(panel);
}

function renderExecutionStatus(messageNode, execution) {
  const labels = {
    completed: "已验收", waiting_confirmation: "等待确认", failed: "验收失败",
    waiting_background: "后台执行中", degraded: "降级结束", cancelled: "已停止", interrupted: "服务中断",
  };
  const card = document.createElement("section");
  card.className = `execution-status ${execution.status || "degraded"}`;
  const heading = document.createElement("strong");
  heading.textContent = `任务状态 · ${labels[execution.status] || execution.status}`;
  const summary = document.createElement("small");
  summary.textContent = execution.summary || "任务状态已记录。";
  card.append(heading, summary);
  const checkpoint = execution.checkpoint || {};
  if (Number(checkpoint.revision) > 0) {
    const checkpointStates = {
      model_waiting: "等待模型",
      tool_decision: "工具决策",
      tool_results_committed: "工具结果已提交",
      response_ready: "回答就绪",
      step_limit_response_waiting: "整理最终回答",
      budget_exceeded: "预算停止点",
      cancelled: "停止边界",
      task_failed: "异常停止点",
      outcome_verified: "验收完成",
      outcome_verification_failed: "验收失败",
      outcome_waiting_confirmation: "等待确认",
      outcome_background_queued: "后台排队",
      outcome_degraded: "降级结束",
      outcome_cancelled: "已停止",
      outcome_budget_exceeded: "预算停止",
    };
    const checkpointLine = document.createElement("small");
    checkpointLine.className = "execution-checkpoint";
    const loop = Number(checkpoint.loop_index) + 1;
    checkpointLine.textContent = `恢复点 #${checkpoint.revision} · ${checkpointStates[checkpoint.state] || checkpoint.state || "已保存"} · 模型第 ${loop} 轮`;
    card.appendChild(checkpointLine);
  }
  const budget = execution.budget || {};
  const budgetPolicy = budget.policy || {};
  const budgetUsed = budget.used || {};
  if (Object.keys(budgetPolicy).length) {
    const budgetPanel = document.createElement("div");
    budgetPanel.className = "execution-budget";
    const items = [
      `时长 ${Number(budgetUsed.wall_time || 0).toFixed(1)}/${Number(budgetPolicy.wall_time_seconds || 0)}秒`,
      `Token ${Number(budgetUsed.model_tokens || 0).toLocaleString()}/${Number(budgetPolicy.model_token_limit || 0).toLocaleString()}`,
      `模型 ${Number(budgetUsed.model_calls || 0)}/${Number(budgetPolicy.model_call_limit || 0)}次`,
      `工具 ${Number(budgetUsed.tool_calls || 0)}/${Number(budgetPolicy.tool_call_limit || 0)}次`,
    ];
    if (budget.pricing_active) {
      items.push(
        `费用 ${budgetPolicy.currency || "USD"} ${Number(budgetUsed.model_cost || 0).toFixed(6)}/${Number(budgetPolicy.model_cost_limit || 0).toFixed(2)}`,
      );
    } else {
      items.push("费用上限待配置模型单价");
    }
    items.forEach((text) => {
      const item = document.createElement("span");
      item.textContent = text;
      budgetPanel.appendChild(item);
    });
    if (budget.exceeded) budgetPanel.dataset.state = "exceeded";
    card.appendChild(budgetPanel);
  }
  const plan = Array.isArray(execution.plan) ? execution.plan : [];
  if (plan.length) {
    const planList = document.createElement("ol");
    planList.className = "execution-plan";
    plan.slice(0, 8).forEach((item) => {
      const line = document.createElement("li");
      line.textContent = item;
      planList.appendChild(line);
    });
    card.appendChild(planList);
  }
  const evidence = Array.isArray(execution.checks?.required_evidence)
    ? execution.checks.required_evidence
    : [];
  if (evidence.length) {
    const evidenceList = document.createElement("div");
    evidenceList.className = "execution-evidence";
    evidence.forEach((item) => {
      const line = document.createElement("span");
      line.dataset.state = item.satisfied ? "verified" : "missing";
      line.textContent = `${item.satisfied ? "✓" : "!"} ${item.label}`;
      evidenceList.appendChild(line);
    });
    card.appendChild(evidenceList);
  }
  const steps = Array.isArray(execution.steps) ? execution.steps : [];
  if (steps.length) {
    const detail = document.createElement("small");
    const completed = steps.filter((item) => item.status === "completed").length;
    detail.textContent = `${steps.length} 个工具步骤 · ${completed} 个完成 · 任务号 ${execution.task_id || "-"}`;
    card.appendChild(detail);
    const reconciled = steps.filter((item) => item?.output?.audit?.reconciled_after_restart);
    if (reconciled.length) {
      const reconciliation = document.createElement("small");
      reconciliation.className = "execution-reconciliation";
      reconciliation.textContent = `重启后已核对 ${reconciled.length} 个副作用步骤，没有重复执行`;
      card.appendChild(reconciliation);
    }
    const dispatches = steps
      .map((item) => item?.output?.audit)
      .filter((audit) => audit?.external_dispatch_id);
    if (dispatches.length) {
      const latestDispatch = dispatches.at(-1);
      const dispatchLine = document.createElement("small");
      dispatchLine.className = "execution-dispatch";
      dispatchLine.dataset.state = latestDispatch.uncertain_side_effect
        ? "uncertain"
        : "recorded";
      dispatchLine.textContent = latestDispatch.uncertain_side_effect
        ? `外发结果待核对 · ${latestDispatch.external_dispatch_id} · 不会自动重发`
        : `外发回执 · ${latestDispatch.external_dispatch_id} · 第 ${latestDispatch.external_dispatch_attempt || 1} 次尝试`;
      card.appendChild(dispatchLine);
    }
    const workflowAudit = steps
      .map((item) => item?.output?.audit)
      .find((audit) => Array.isArray(audit?.workflow_steps));
    if (workflowAudit) renderWorkflowProgress(card, workflowAudit);
  }
  const events = Array.isArray(execution.events) ? execution.events : [];
  if (events.length) {
    const timeline = document.createElement("details");
    timeline.className = "agent-timeline completed";
    timeline.dataset.taskId = execution.task_id || "";
    const timelineSummary = document.createElement("summary");
    timelineSummary.textContent = `执行记录 · ${events.length} 条 · 点击展开`;
    const eventList = document.createElement("ol");
    events.slice(-30).forEach((item) => appendTaskEventLine(eventList, item));
    timeline.append(timelineSummary, eventList);
    card.appendChild(timeline);
  }
  if (execution.retryable && execution.task_id) {
    const restore = document.createElement("button");
    restore.type = "button";
    restore.className = "task-restore";
    restore.textContent = "恢复这条委托";
    restore.addEventListener("click", () => restoreAgentTask(execution.task_id, restore));
    card.appendChild(restore);
  }
  const backgroundJobId = execution.checks?.background_job_ids?.[0]
    || execution.checks?.background_job_id;
  if (execution.status === "waiting_background" && backgroundJobId) {
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "task-restore";
    cancel.textContent = "停止后台任务";
    cancel.addEventListener("click", async () => {
      if (!globalThis.confirm("确定停止这个后台任务吗？已经完成的步骤不会自动撤销。")) return;
      cancel.disabled = true;
      try {
        await requestJson(`/api/background/jobs/${encodeURIComponent(backgroundJobId)}/cancel`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: state.sessionId, confirmed: true }),
        });
        showToast("后台任务已经停止");
      } catch {
        cancel.disabled = false;
        showToast("后台任务未能停止，请刷新状态后重试");
      }
    });
    card.appendChild(cancel);
  }
  messageNode.appendChild(card);
  if (execution.status === "waiting_background" && execution.task_id) {
    watchBackgroundTask(execution.task_id);
  }
}

const backgroundTaskWatchers = new Set();

function taskExecution(task) {
  return {
    task_id: task.id,
    status: task.status,
    summary: task.outcome?.summary || task.error || "后台任务状态已更新。",
    retryable: ["failed", "degraded", "cancelled", "interrupted"].includes(task.status),
    plan: task.plan || [],
    contract: task.contract || {},
    checks: task.outcome?.checks || {},
    steps: task.steps || [],
    events: task.events || [],
    budget: task.budget_status || {},
    checkpoint: task.checkpoint || {},
    phase: task.phase,
  };
}

function watchBackgroundTask(taskId) {
  if (backgroundTaskWatchers.has(taskId)) return;
  backgroundTaskWatchers.add(taskId);
  const poll = async () => {
    try {
      const task = await requestJson(`/api/agent/tasks/${encodeURIComponent(taskId)}?session_id=${encodeURIComponent(state.sessionId)}`);
      if (task.status === "waiting_background") {
        globalThis.setTimeout(poll, 1200);
        return;
      }
      const execution = taskExecution(task);
      const text = task.status === "completed"
        ? "后台委托已经完成，并通过了 Agent 的证据验收。"
        : `后台委托结束：${execution.summary}`;
      state.messages.push({ role: "assistant", content: text, meta: "Agent · 后台状态", execution });
      addMessage("assistant", text, "Agent · 后台状态", { character: state.character, execution });
    } catch {
      globalThis.setTimeout(poll, 2500);
      return;
    }
    backgroundTaskWatchers.delete(taskId);
  };
  globalThis.setTimeout(poll, 500);
}

async function restoreAgentTask(taskId, button) {
  button.disabled = true;
  try {
    const params = new URLSearchParams({ session_id: state.sessionId });
    const task = await requestJson(`/api/agent/tasks/${encodeURIComponent(taskId)}?${params}`);
    elements.input.value = task.goal || "";
    state.resumeTaskId = task.id;
    state.resumeGoal = task.goal || "";
    const uncertainActions = new Set([
      "remember", "forget", "clear_memory", "set_reminder", "cancel_reminder",
      "write_file", "edit_file", "confirm_file_operation", "rollback_file_operation",
      "add_task", "capability_call", "confirm_capability_operation",
    ]);
    const uncertain = (task.steps || []).some((step) =>
      ["running", "interrupted"].includes(step.status) && uncertainActions.has(step.action)
    );
    state.allowUncertainReplay = uncertain
      ? globalThis.confirm(
        "这个任务在可能产生副作用的步骤中断，结果无法确定。请先检查文件或外部服务。确定仍要重放未完成步骤吗？",
      )
      : false;
    const requestedAttachmentIds = Array.isArray(task.request?.attachment_ids)
      ? task.request.attachment_ids
      : [];
    let restoredAttachmentCount = 0;
    let availableAttachmentCount = 0;
    if (requestedAttachmentIds.length) {
      const inventory = await requestJson(`/api/attachments?${params}`);
      const requested = new Set(requestedAttachmentIds);
      const merged = [...state.attachments];
      const available = inventory.filter((item) => requested.has(item.id));
      availableAttachmentCount = available.length;
      available
        .forEach((item) => {
          if (merged.length < 4 && !merged.some((existing) => existing.id === item.id)) {
            merged.push(item);
            restoredAttachmentCount += 1;
          }
        });
      state.attachments = merged;
      renderComposerAttachments();
    }
    elements.input.focus();
    const missingAttachments = requestedAttachmentIds.length - availableAttachmentCount;
    showToast(
      missingAttachments > 0
        ? `委托已恢复；${missingAttachments} 个附件已删除或未能恢复`
        : restoredAttachmentCount
          ? `委托和 ${restoredAttachmentCount} 个附件已接回原任务，请检查后继续`
          : uncertain && !state.allowUncertainReplay
            ? "委托已恢复，但不确定副作用尚未获准重放；再次点恢复可重新确认"
            : "委托已接回原任务账本；已完成步骤不会重放",
    );
    if (uncertain && !state.allowUncertainReplay) button.disabled = false;
  } catch {
    button.disabled = false;
    showToast("这条委托暂时无法恢复");
  }
}

async function renderInterruptedTasks() {
  const knownTaskStatus = new Map(
    state.messages
      .filter((item) => item.execution?.task_id)
      .map((item) => [item.execution.task_id, item.execution.status]),
  );
  try {
    const params = new URLSearchParams({ session_id: state.sessionId, limit: "20" });
    const tasks = await requestJson(`/api/agent/tasks?${params}`);
    tasks
      .filter((task) => ["interrupted", "waiting_background"].includes(task.status)
        && knownTaskStatus.get(task.id) !== task.status)
      .slice(0, 3)
      .forEach((task) => {
        const steps = Array.isArray(task.steps) ? task.steps : [];
        const reconciled = steps.filter((step) => step?.output?.audit?.reconciled_after_restart).length;
        const unresolved = steps.filter((step) => step.status === "interrupted").length;
        const recoveryText = reconciled
          ? `服务上次在完成委托前停止了。已根据持久证据核对 ${reconciled} 个步骤，没有重复执行${unresolved ? `；另有 ${unresolved} 个结果仍需你确认` : ""}。`
          : "服务上次在完成委托前停止了。为避免重复执行可能产生副作用的步骤，我没有自动重放。";
        addMessage(
          "assistant",
          task.status === "waiting_background"
            ? "这条委托仍在服务端后台执行，关闭页面不会中断它。"
            : recoveryText,
          task.status === "waiting_background" ? "Agent · 后台任务" : "Agent · 恢复检查",
          {
            character: state.character,
            execution: taskExecution(task),
          },
        );
      });
  } catch { /* task recovery is supplementary to conversation history */ }
}

function submitToolConfirmation(action, operationId) {
  const phrases = {
    confirm_file_operation: "确认文件操作",
    cancel_file_operation: "取消文件操作",
    confirm_capability_operation: "确认能力操作",
    cancel_capability_operation: "取消能力操作",
    rollback_file_operation: "回滚文件操作",
  };
  elements.input.value = `${phrases[action] || action}：${operationId}`;
  elements.form.requestSubmit();
}

function renderStoredMessages() {
  elements.messages.innerHTML = "";
  if (!state.messages.length) {
    renderEmptyConversation();
    return;
  }
  state.messages.forEach((item) => addMessage(item.role, item.content, item.meta || "", {
    stickerUrl: item.stickerUrl,
    audioUrl: item.audioUrl,
    character: item.character,
    toolResults: item.toolResults,
    execution: item.execution,
    attachments: item.attachments,
  }));
}

function renderEmptyConversation() {
  const profile = selectedProfile();
  const name = state.chatMode === "club" ? "侍奉部全员" : (profile?.display_name || "雪之下雪乃");
  const text = state.chatMode === "club"
    ? "新一轮部内会议开始了。把今天想讨论的事情写下来吧。"
    : `门没有锁。${profile?.short_name || "雪乃"}正在等你说这次的委托。`;
  const note = document.createElement("article");
  note.className = "arrival-note assistant";
  const label = document.createElement("div");
  label.className = "arrival-label";
  const speaker = document.createElement("span");
  speaker.textContent = name;
  const small = document.createElement("small");
  small.textContent = state.chatMode === "club" ? "CLUB MEETING" : "PRIVATE REQUEST";
  label.append(speaker, small);
  const copy = document.createElement("p");
  copy.textContent = text;
  const next = document.createElement("span");
  next.className = "dialogue-next";
  next.setAttribute("aria-hidden", "true");
  next.textContent = "▾";
  note.append(label, copy, next);
  elements.messages.appendChild(note);
}

function renderCharacters() {
  elements.characterList.innerHTML = "";
  state.characters.forEach((item) => {
    const active = state.chatOpen && state.chatMode === "solo" && item.id === state.character;
    const button = document.createElement("button");
    button.type = "button";
    button.className = active ? "character active" : "character";
    button.dataset.character = item.id;
    button.style.setProperty("--accent", item.accent_color);
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute("aria-expanded", String(active));
    button.setAttribute("aria-controls", "conversation-room");
    button.innerHTML = `
      <span class="character-monogram">${item.short_name.slice(0, 1)}</span>
      <span class="character-copy"><strong>${item.short_name}</strong><small>${item.role_summary}</small></span>
      <span class="character-arrow" aria-hidden="true">›</span>`;
    button.addEventListener("click", () => selectCharacter(item.id));
    elements.characterList.appendChild(button);
  });
}

function syncCharacterTriggers() {
  elements.characterList.querySelectorAll("button[data-character]").forEach((button) => {
    const active = state.chatOpen && state.chatMode === "solo" && button.dataset.character === state.character;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute("aria-expanded", String(active));
  });
}

async function selectCharacter(characterId, { loadScope = true } = {}) {
  if (state.chatOpen && state.chatMode === "solo" && state.character === characterId) {
    setChatOpen(false);
    return;
  }
  const previousScope = conversationScopeKey();
  state.chatMode = "solo";
  state.character = characterId;
  updateConversationIdentity();
  setChatOpen(true);
  savePreferences();
  if (loadScope && (previousScope !== conversationScopeKey() || !state.sessionId)) await activateConversationScope();
}

function syncChatTriggers() {
  elements.shell.dataset.chatOpen = String(state.chatOpen);
  elements.chatRoom.setAttribute("aria-hidden", String(!state.chatOpen));
  elements.chatRoom.inert = !state.chatOpen;
  modeButtons.forEach((button) => {
    const active = state.chatOpen && button.dataset.mode === state.chatMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute("aria-expanded", String(active));
    button.setAttribute("aria-controls", "conversation-room");
  });
  elements.activeMode.textContent = state.chatOpen
    ? (state.chatMode === "solo" ? "单独聊聊" : "活动室")
    : "等待选择";
}

function setChatOpen(open) {
  state.chatOpen = Boolean(open);
  syncChatTriggers();
  syncCharacterTriggers();
  if (state.chatOpen) requestAnimationFrame(() => elements.input.focus());
}

function updateConversationIdentity() {
  const profile = selectedProfile();
  const displayName = profile?.display_name || "雪之下雪乃";
  const shortName = profile?.short_name || "雪乃";
  const isClub = state.chatMode === "club";
  elements.shell.dataset.character = state.character;
  elements.shell.dataset.mode = state.chatMode;
  elements.activeCharacter.textContent = isClub ? "侍奉部 · 全员会议" : displayName;
  elements.modeKicker.textContent = isClub ? `CLUB MEETING · ${shortName}主持` : "NOW TALKING";
  elements.meetingLead.textContent = `${shortName}主持 · 其他成员会自然加入`;
  if (elements.arrivalSpeaker) elements.arrivalSpeaker.textContent = isClub ? "侍奉部全员" : displayName;
  elements.characterHint.textContent = isClub
    ? `${shortName}主持，其他成员会根据话题自然加入`
    : `${shortName}会单独听你说完`;
  elements.conversationScopeLabel.textContent = isClub ? "部内会议" : `${shortName}的单独委托`;
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const detail = await response.text();
    const error = new Error(detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function requestErrorDetail(error, fallback) {
  try {
    const parsed = JSON.parse(String(error?.message || ""));
    return parsed.detail || fallback;
  } catch {
    return fallback;
  }
}

function waitFor(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function updateChatRuntimeIndicators(data) {
  if (!data || typeof data !== "object") return;
  if (data.emotion) elements.activeEmotion.textContent = emotionNames[data.emotion] || data.emotion;
  elements.activeRoute.textContent = `route: ${data.route_reasoning || "direct"}`;
  elements.degradedState.textContent = data.degraded ? "文字陪伴" : "在线";
  elements.degradedState.dataset.state = data.degraded ? "degraded" : "ready";
}

function chatJobProgressText(job, fallback = "") {
  if (job?.status === "running") return "Agent worker 正在执行；执行期间会持续续租。";
  if (job?.status === "queued" && Number(job.queue_position) > 1) {
    return `委托已持久化，当前排在第 ${job.queue_position} 位；刷新或关闭页面都不会中断。`;
  }
  return fallback || "委托正在服务端执行；关闭或刷新页面都不会中断。";
}

const taskPhaseNames = {
  planning: "理解委托", planned: "计划就绪", background_queued: "持久队列",
  preparing_context: "准备上下文", model_waiting: "模型分析", executing: "调用工具",
  verifying: "核对结果", waiting_confirmation: "等待确认", waiting_background: "后台执行",
  checkpointing: "保存恢复点", resuming: "恢复任务",
  reconciling: "副作用对账",
  operation_interrupted: "外发结果待核对",
  verified: "完成验收", verification_failed: "验收失败", degraded: "降级结束",
  cancelling: "正在停止", cancelled: "已停止", interrupted: "服务中断",
  budget_exceeded: "资源预算已用完",
};

function ensureAgentTimeline(messageNode, taskId) {
  let timeline = messageNode.querySelector(".agent-timeline");
  if (timeline) return timeline;
  timeline = document.createElement("details");
  timeline.className = "agent-timeline";
  timeline.dataset.taskId = taskId;
  timeline.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "执行过程 · 正在建立任务";
  const list = document.createElement("ol");
  list.setAttribute("aria-live", "polite");
  timeline.append(summary, list);
  const cancel = messageNode.querySelector(".pending-cancel");
  messageNode.insertBefore(timeline, cancel || null);
  return timeline;
}

function appendTaskEventLine(list, item) {
  const eventId = Number(item.id) || 0;
  if (list.querySelector(`[data-event-id="${eventId}"]`)) return;
  const line = document.createElement("li");
  line.dataset.eventId = String(eventId);
  line.dataset.state = item.status || "running";
  const icon = ["failed", "degraded", "interrupted"].includes(item.status)
    ? "×"
    : ["cancelled", "cancel_requested"].includes(item.status)
      ? "■"
      : ["completed", "waiting_confirmation", "waiting_background"].includes(item.status)
        ? "✓"
        : "◆";
  const clock = item.created_at
    ? new Date(Number(item.created_at) * 1000).toLocaleTimeString("zh-CN", {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    })
    : "";
  line.textContent = `${icon} ${item.detail || taskPhaseNames[item.phase] || item.event}${clock ? ` · ${clock}` : ""}`;
  list.appendChild(line);
}

function taskProgressRecord(taskId) {
  return Object.values(state.pendingChatJobs).find((item) => item.taskId === taskId);
}

function renderTaskEvents(record, events = [], task = null) {
  if (state.sessionId !== record.sessionId || !state.pendingNode?.isConnected) return;
  const timeline = ensureAgentTimeline(state.pendingNode, record.taskId);
  const list = timeline.querySelector("ol");
  events.forEach((item) => appendTaskEventLine(list, item));
  while (list.childElementCount > 30) list.firstElementChild?.remove();
  const last = events.at(-1);
  const phase = last?.phase || task?.phase || "planning";
  const label = taskPhaseNames[phase] || phase;
  timeline.querySelector("summary").textContent = `执行过程 · ${label} · ${list.childElementCount} 条记录`;
  if (last?.detail) {
    const first = state.pendingNode.firstChild;
    if (first?.nodeType === Node.TEXT_NODE) first.textContent = last.detail;
  }
}

function applyTaskProgress(payload) {
  const taskId = String(payload?.task_id || "");
  const record = taskProgressRecord(taskId);
  if (!record) return;
  const events = Array.isArray(payload.events) ? payload.events : [];
  const cursor = Number(payload.cursor) || Number(events.at(-1)?.id) || 0;
  record.eventCursor = Math.max(Number(record.eventCursor) || 0, cursor);
  renderTaskEvents(record, events, payload.task || null);
}

function subscribeTaskProgress(record) {
  const socket = state.runtimeSocket;
  if (!record?.taskId || !socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({
    type: "subscribe_task",
    task_id: record.taskId,
    session_id: record.sessionId,
    after_id: Number(record.eventCursor) || 0,
  }));
}

async function pollTaskProgress(record) {
  if (!record.taskId) return;
  const params = new URLSearchParams({
    session_id: record.sessionId,
    after_id: String(Number(record.eventCursor) || 0),
    limit: "100",
  });
  const payload = await requestJson(
    `/api/agent/tasks/${encodeURIComponent(record.taskId)}/events?${params}`,
  );
  applyTaskProgress(payload);
}

function updatePendingChatJob(job, record) {
  if (state.sessionId !== record.sessionId || !state.pendingNode?.isConnected) return;
  const first = state.pendingNode.firstChild;
  if (first?.nodeType === Node.TEXT_NODE) {
    first.textContent = chatJobProgressText(job, record.pendingText);
  }
}

function renderPendingChatJob(record, job = {}) {
  addMessage("user", record.userText, "你 · 已提交", { attachments: record.attachments || [] });
  state.pendingNode = addMessage(
    "assistant",
    chatJobProgressText(job, record.pendingText),
    "Agent · 持久队列",
    {
      pending: true,
      requestId: record.requestId,
      jobId: record.jobId,
      sessionId: record.sessionId,
      taskId: record.taskId,
    },
  );
  record.eventCursor = 0;
  subscribeTaskProgress(record);
}

async function finishChatJob(job, record) {
  const current = state.pendingChatJobs[record.sessionId];
  if (!current || current.jobId !== record.jobId) return;
  forgetPendingChatJob(record.sessionId, record.jobId);
  const isCurrentConversation = state.sessionId === record.sessionId;
  if (isCurrentConversation) {
    state.pendingNode?.remove();
    state.pendingNode = null;
  }
  syncComposerAvailability();

  if (job.status === "completed") {
    const data = job.result || {};
    updateChatRuntimeIndicators(data);
    state.resumeTaskId = "";
    state.resumeGoal = "";
    state.allowUncertainReplay = false;
    await refreshConversationList();
    if (isCurrentConversation) {
      try {
        await switchConversation(record.sessionId);
      } catch {
        const profile = state.characters.find((item) => item.id === data.character);
        const meta = `${profile?.short_name || data.character || "侍奉部"} · ${emotionNames[data.emotion] || data.emotion || "平静"}`;
        addMessage("assistant", data.content || "委托已经完成。", meta, {
          stickerUrl: data.sticker_url,
          audioUrl: data.audio_url,
          character: data.character,
          toolResults: data.tool_results,
          execution: data.execution,
        });
      }
      if (elements.memoryDialog.open) await loadMemoryArchive(elements.memorySearch.value);
    }
    showToast("Agent 委托已经完成");
    return;
  }

  if (isCurrentConversation) {
    const stopped = job.status === "cancelled";
    addMessage(
      "assistant",
      stopped
        ? "这次委托已经停止，尚未执行的步骤不会继续。"
        : (job.error || "这次委托未能完成，也没有自动重放可能产生副作用的步骤。"),
      stopped ? "Agent · 已停止" : "Agent · 需要检查",
      { character: record.character || state.character },
    );
  }
  showToast(job.status === "cancelled" ? "Agent 委托已停止" : "Agent 委托未完成，请检查任务记录");
}

async function pollChatJob(record) {
  while (state.pendingChatJobs[record.sessionId]?.jobId === record.jobId) {
    try {
      const params = new URLSearchParams({ session_id: record.sessionId });
      const job = await requestJson(`/api/chat/jobs/${encodeURIComponent(record.jobId)}?${params}`);
      updatePendingChatJob(job, record);
      await pollTaskProgress(record).catch(() => {});
      if (["completed", "cancelled", "dead_letter"].includes(job.status)) {
        await finishChatJob(job, record);
        return;
      }
    } catch (error) {
      if (error.status === 404) {
        forgetPendingChatJob(record.sessionId, record.jobId);
        syncComposerAvailability();
        if (state.sessionId === record.sessionId) {
          state.pendingNode?.remove();
          state.pendingNode = null;
          addMessage("assistant", "服务端找不到这条排队委托，请从 Agent 任务记录中检查。", "Agent · 作业丢失");
        }
        return;
      }
    }
    await waitFor(700);
  }
}

function startChatJobPolling(record) {
  if (activeChatPolls.has(record.jobId)) return activeChatPolls.get(record.jobId);
  const polling = pollChatJob(record)
    .catch(() => {
      if (state.sessionId === record.sessionId) {
        showToast("暂时无法刷新 Agent 作业状态；服务恢复后会继续检查");
      }
    })
    .finally(() => activeChatPolls.delete(record.jobId));
  activeChatPolls.set(record.jobId, polling);
  return polling;
}

async function restorePendingChatJob(sessionId) {
  const record = state.pendingChatJobs[sessionId];
  if (!record) {
    syncComposerAvailability();
    return;
  }
  try {
    const params = new URLSearchParams({ session_id: sessionId });
    const job = await requestJson(`/api/chat/jobs/${encodeURIComponent(record.jobId)}?${params}`);
    if (["completed", "cancelled", "dead_letter"].includes(job.status)) {
      await finishChatJob(job, record);
      return;
    }
    if (state.sessionId === sessionId) renderPendingChatJob(record, job);
    syncComposerAvailability();
    startChatJobPolling(record);
  } catch (error) {
    if (error.status === 404) {
      forgetPendingChatJob(sessionId, record.jobId);
      syncComposerAvailability();
    } else {
      if (state.sessionId === sessionId) renderPendingChatJob(record);
      syncComposerAvailability();
      startChatJobPolling(record);
    }
  }
}

async function loadCharacters() {
  state.characters = await requestJson("/api/characters");
  if (!state.characters.some((item) => item.id === state.character)) state.character = "yukino";
  updateConversationIdentity();
  renderCharacters();
}

function formatConversationTime(timestamp) {
  if (!timestamp) return "刚刚";
  const date = new Date(Number(timestamp) * 1000);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

function messageMeta(item) {
  const time = formatConversationTime(item.created_at);
  if (item.role === "user") return `你 · ${time}`;
  const profile = state.characters.find((character) => character.id === item.character);
  const emotion = emotionNames[item.emotion] || item.emotion || "平静";
  return `${profile?.short_name || item.character || "侍奉部"} · ${emotion} · ${time}`;
}

function renderConversationList() {
  elements.conversationList.replaceChildren();
  if (!state.conversations.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-list-empty";
    empty.textContent = "还没有历史委托";
    elements.conversationList.appendChild(empty);
    return;
  }
  state.conversations.forEach((thread) => {
    const item = document.createElement("article");
    item.className = thread.id === state.sessionId ? "conversation-item active" : "conversation-item";
    item.dataset.conversationId = thread.id;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "conversation-open";
    open.dataset.openConversation = thread.id;
    const title = document.createElement("strong");
    title.textContent = thread.title;
    const preview = document.createElement("small");
    preview.textContent = thread.preview || (thread.turn_count ? `${thread.turn_count} 轮对话` : "等待第一句话");
    const time = document.createElement("time");
    time.textContent = formatConversationTime(thread.updated_at);
    open.append(title, preview, time);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "conversation-delete";
    remove.dataset.deleteConversation = thread.id;
    remove.dataset.conversationTitle = thread.title;
    remove.setAttribute("aria-label", `删除会话 ${thread.title}`);
    remove.textContent = "×";
    item.append(open, remove);
    elements.conversationList.appendChild(item);
  });
}

function setConversationBusy(busy) {
  state.conversationBusy = busy;
  elements.newConversation.disabled = busy;
  elements.conversationList.setAttribute("aria-busy", String(busy));
  syncComposerAvailability();
}

async function adoptLegacyConversation() {
  if (localStorage.getItem(CONVERSATION_MIGRATION_KEY)) return;
  const legacyId = localStorage.getItem(LEGACY_SESSION_ID_KEY);
  if (legacyId) {
    try {
      await requestJson("/api/conversations/adopt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: legacyId,
          chat_mode: state.chatMode,
          character: state.character,
        }),
      });
      state.activeConversations[conversationScopeKey()] = legacyId;
      persistActiveConversations();
    } catch { /* an invalid legacy id should not block the new conversation system */ }
  }
  localStorage.setItem(CONVERSATION_MIGRATION_KEY, "done");
  localStorage.removeItem(LEGACY_SESSION_ID_KEY);
  localStorage.removeItem(LEGACY_MESSAGES_KEY);
}

async function fetchConversationList() {
  const params = new URLSearchParams({ chat_mode: state.chatMode, character: state.character });
  return requestJson(`/api/conversations?${params}`);
}

async function switchConversation(conversationId, { token = state.scopeLoadToken } = {}) {
  const thread = await requestJson(`/api/conversations/${encodeURIComponent(conversationId)}`);
  if (token !== state.scopeLoadToken) return;
  state.sessionId = thread.id;
  state.pendingNode = null;
  state.resumeTaskId = "";
  state.resumeGoal = "";
  state.allowUncertainReplay = false;
  state.attachments = [];
  elements.attachmentInput.value = "";
  renderComposerAttachments();
  state.messages = (thread.messages || []).map((item) => ({
    ...item,
    meta: messageMeta(item),
    toolResults: item.tool_results || item.toolResults || [],
  }));
  state.activeConversations[conversationScopeKey()] = thread.id;
  persistActiveConversations();
  renderStoredMessages();
  await renderInterruptedTasks();
  await restorePendingChatJob(thread.id);
  renderConversationList();
  elements.input.focus();
}

async function activateConversationScope() {
  const token = ++state.scopeLoadToken;
  setConversationBusy(true);
  try {
    state.conversations = await fetchConversationList();
    if (token !== state.scopeLoadToken) return;
    const preferredId = state.activeConversations[conversationScopeKey()];
    let target = state.conversations.find((item) => item.id === preferredId) || state.conversations[0];
    if (!target) {
      target = await requestJson("/api/conversations/ensure", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_mode: state.chatMode, character: state.character }),
      });
      if (token !== state.scopeLoadToken) return;
      state.conversations = [target];
    }
    await switchConversation(target.id, { token });
  } catch {
    if (token === state.scopeLoadToken) showToast("历史会话暂时无法读取");
  } finally {
    if (token === state.scopeLoadToken) setConversationBusy(false);
  }
}

async function refreshConversationList() {
  state.conversations = await fetchConversationList();
  renderConversationList();
}

async function createNewConversation() {
  setChatOpen(true);
  setConversationBusy(true);
  try {
    const thread = await requestJson("/api/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_mode: state.chatMode, character: state.character }),
    });
    state.conversations.unshift(thread);
    await switchConversation(thread.id);
    showToast(state.chatMode === "club" ? "新的部内会议已建立" : "新的单独委托已建立");
  } catch {
    showToast("新会话没有建立成功");
  } finally {
    setConversationBusy(false);
  }
}

function formatMemoryTime(timestamp) {
  if (!timestamp) return "时间未记录";
  return new Date(Number(timestamp) * 1000).toLocaleString("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function renderMemoryItems(target, items, emptyText, renderer) {
  target.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "memory-empty";
    empty.textContent = emptyText;
    target.appendChild(empty);
    return;
  }
  items.forEach((item) => target.appendChild(renderer(item)));
}

function memoryItem({ title, content, meta = "", kind = "", id = "", key = "" }) {
  const node = document.createElement("article");
  node.className = "memory-item";
  const copy = document.createElement("div");
  copy.className = "memory-item-copy";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const body = document.createElement("p");
  body.textContent = content;
  copy.append(heading, body);
  if (meta) {
    const detail = document.createElement("small");
    detail.textContent = meta;
    copy.appendChild(detail);
  }
  node.appendChild(copy);
  if (kind) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "忘记";
    remove.dataset.memoryKind = kind;
    if (id) remove.dataset.memoryId = String(id);
    if (key) remove.dataset.memoryKey = key;
    remove.dataset.memoryLabel = content.slice(0, 48);
    node.appendChild(remove);
  }
  return node;
}

function conversationMemoryItem(item) {
  const node = memoryItem({
    title: `${item.character || "侍奉部"} · ${emotionNames[item.emotion] || item.emotion || "平静"}`,
    content: item.user_message || "（没有记录用户消息）",
    meta: formatMemoryTime(item.created_at),
  });
  node.classList.add("memory-conversation");
  const reply = document.createElement("p");
  reply.textContent = item.assistant_reply || "（没有记录角色回复）";
  node.querySelector(".memory-item-copy").insertBefore(reply, node.querySelector(".memory-item-copy small"));
  return node;
}

function renderMemoryArchive(data, query = "") {
  const normalizedQuery = query.trim().toLowerCase();
  const includesQuery = (...values) => !normalizedQuery || values.some((value) =>
    String(value || "").toLowerCase().includes(normalizedQuery)
  );
  const permanent = (data.permanent_memory?.items || []).filter((item) =>
    includesQuery(item.category, item.key, item.value)
  );
  const local = data.memory_retrieval || [];
  const conversations = (data.recent_conversations || []).filter((item) =>
    includesQuery(item.user_message, item.assistant_reply, item.character, item.emotion)
  );
  const reminders = (data.reminders || []).filter((item) => includesQuery(item.content));
  const categoryNames = { preference: "用户偏好", key_event: "关键事件", reflection: "重要片段" };
  const sourceNames = { explicit: "主动记住", reflection: "对话总结", profile: "用户资料" };

  elements.memoryPermanentCount.textContent = String(permanent.length);
  elements.memoryLocalCount.textContent = String(local.length);
  elements.memoryReminderCount.textContent = String(reminders.length);
  elements.memoryQueryLabel.textContent = query ? `“${query}”的结果` : "按重要度排列";

  renderMemoryItems(elements.memoryPermanentList, permanent, "还没有需要长期记住的事情。", (item) => memoryItem({
    title: categoryNames[item.category] || item.category || "长期记忆",
    content: item.value,
    meta: `${item.key} · ${formatMemoryTime(item.timestamp)}`,
    kind: "permanent",
    key: item.key,
  }));
  renderMemoryItems(elements.memoryLocalList, local, query ? "没有找到相关记忆。" : "还没有可检索的记忆片段。", (item) => memoryItem({
    title: sourceNames[item.source] || item.source || "对话记忆",
    content: item.content,
    meta: `重要度 ${Math.round(Number(item.importance || 0) * 100)}% · ${formatMemoryTime(item.created_at)}`,
    kind: "local",
    id: item.id,
  }));
  renderMemoryItems(elements.memoryConversationList, conversations, "还没有完成的对话。", conversationMemoryItem);
  const reminderStates = {
    scheduled: "等待触发", claimed: "正在送达", delivered: "已送达",
    cancelled: "已取消", unscheduled: "未定时",
  };
  renderMemoryItems(elements.memoryReminderArchiveList, reminders, "当前没有提醒。", (item) => {
    const due = item.due_at ? formatMemoryTime(item.due_at) : "没有明确时间";
    const recurrence = item.recurrence === "daily"
      ? " · 每天重复"
      : String(item.recurrence || "").startsWith("weekly:")
        ? " · 每周重复"
        : "";
    const node = memoryItem({
      title: `提醒 #${item.id} · ${reminderStates[item.status] || item.status}`,
      content: item.content,
      meta: `${due}${recurrence} · 创建于 ${formatMemoryTime(item.created_at)}`,
    });
    if (["scheduled", "claimed", "unscheduled"].includes(item.status)) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "取消";
      cancel.dataset.reminderCancel = String(item.id);
      node.appendChild(cancel);
    }
    return node;
  });
}

async function loadMemoryArchive(query = "") {
  elements.memoryLoadState.textContent = "正在整理档案……";
  elements.memoryLoadState.dataset.state = "loading";
  const params = new URLSearchParams({ session_id: state.sessionId });
  if (query.trim()) params.set("query", query.trim());
  try {
    const data = await requestJson(`/api/memory-state?${params}`);
    renderMemoryArchive(data, query.trim());
    elements.memoryLoadState.textContent = "档案已同步";
    elements.memoryLoadState.dataset.state = "ready";
  } catch {
    elements.memoryLoadState.textContent = "档案读取失败";
    elements.memoryLoadState.dataset.state = "error";
  }
}

async function claimDueCare() {
  if (!state.sessionId) return;
  try {
    const nudges = await requestJson("/api/nudges/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId }),
    });
    nudges.forEach((item) => {
      const content = item.content || "路过活动室时，想起你了。今天还好吗？";
      const meta = "侍奉部 · 主动关怀";
      state.messages.push({ role: "assistant", content, meta });
      addMessage("assistant", content, meta);
    });
  } catch { /* active care degrades silently */ }
}

async function claimDueReminders() {
  if (!state.sessionId) return;
  try {
    const reminders = await requestJson("/api/reminders/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId }),
    });
    for (const reminder of reminders) {
      const content = `提醒时间到了：${reminder.content}`;
      const meta = "侍奉部 · 定时提醒";
      state.messages.push({ role: "assistant", content, meta });
      addMessage("assistant", content, meta);
      showToast(content);
      await requestJson("/api/reminders/ack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: state.sessionId,
          reminder_id: reminder.id,
          claim_token: reminder.claim_token,
        }),
      });
    }
    if (reminders.length && elements.memoryDialog.open) {
      await loadMemoryArchive(elements.memorySearch.value);
    }
  } catch { /* reminder leases expire and can be delivered on the next poll */ }
}

function connectRuntimeEvents() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/runtime`);
  state.runtimeSocket = socket;
  socket.addEventListener("open", () => {
    Object.values(state.pendingChatJobs).forEach(subscribeTaskProgress);
  });
  socket.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.event === "task_events") applyTaskProgress(payload);
      if (payload.event === "task_subscribed" && payload.task_id) {
        const record = taskProgressRecord(payload.task_id);
        if (record) renderTaskEvents(record, [], payload.task || null);
      }
    } catch { /* malformed runtime events are ignored; polling remains active */ }
  });
  socket.addEventListener("close", () => {
    if (state.runtimeSocket === socket) state.runtimeSocket = null;
    setTimeout(connectRuntimeEvents, 2500);
  }, { once: true });
}

async function loadCapabilities() {
  state.capabilities = await requestJson("/api/capabilities");
  const restricted = state.capabilities.filter((item) => item.permission && item.permission !== "allowed").length;
  const degraded = state.capabilities.filter((item) => item.state !== "ready").length;
  elements.capabilityCount.textContent = restricted ? `${restricted} 限制` : (degraded ? `${degraded} 待配` : "已配置");
}

const characterSettingNames = {
  yukino: "雪乃", yui: "结衣", hachiman: "八幡", iroha: "一色", shizuka: "平冢老师",
};
const emotionSettingNames = {
  happy: "开心", sad: "难过", anxious: "不安", angry: "生气",
  lonely: "孤独", shy: "害羞", thinking: "思考", neutral: "平静",
};

function setSettingState(element, configured, readyText = "已配置") {
  element.textContent = configured ? readyText : "未配置";
  element.dataset.state = configured ? "ready" : "missing";
}

function renderStickerCoverage(stickers) {
  elements.stickerCoverage.innerHTML = "";
  Object.entries(stickers.coverage || {}).forEach(([characterId, counts]) => {
    const card = document.createElement("article");
    const total = Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0);
    card.className = total ? "sticker-character ready" : "sticker-character";
    const filled = Object.entries(counts)
      .filter(([, count]) => count)
      .map(([emotion, count]) => `${emotionSettingNames[emotion] || emotion} ${count}`)
      .join(" · ");
    card.innerHTML = `<strong>${characterSettingNames[characterId] || characterId}</strong><b>${total} 张</b><small>${filled || "还没有贴纸"}</small>`;
    elements.stickerCoverage.appendChild(card);
  });
}

function renderModelRuntime(model, modelRuntime = {}) {
  const primaryHealth = modelRuntime.health?.models?.[model.model] || {};
  const circuitNames = {
    closed: "正常",
    open: "熔断中",
    half_open: "正在探测",
    half_open_ready: "等待探测",
  };
  elements.settingModelRuntime.textContent = [
    `${model.model || "主模型"} · ${circuitNames[primaryHealth.circuit] || "尚无调用记录"}`,
    `${modelRuntime.active_calls || 0}/${modelRuntime.max_concurrent_calls || model.max_concurrent || 4} 个模型请求运行中`,
    primaryHealth.last_latency_ms ? `最近耗时 ${primaryHealth.last_latency_ms}ms` : "暂无成功延迟",
    primaryHealth.last_failure_code ? `最近失败 ${primaryHealth.last_failure_code}` : "暂无失败",
    model.fallback_model ? `备用 ${model.fallback_model}` : "未配置备用模型",
  ].join(" · ");
}

function renderSettings(settings) {
  state.settings = settings;
  const model = settings.model;
  const voice = settings.voice;
  const stickers = settings.stickers;
  const mcp = settings.mcp;
  const channels = settings.channels;
  const mail = settings.mail;
  const agent = settings.agent;
  const data = settings.data || {};
  elements.settingApiKey.value = "";
  elements.settingClearApiKey.checked = false;
  elements.settingApiKeyMask.textContent = model.api_key_masked || "未保存";
  elements.settingBaseUrl.value = model.base_url || "";
  elements.settingModel.value = model.model || "gpt-4.1-mini";
  elements.settingFallbackModel.value = model.fallback_model || "";
  elements.settingEmbeddingApiKey.value = "";
  elements.settingClearEmbeddingApiKey.checked = false;
  elements.settingEmbeddingApiKeyMask.textContent = model.embedding_api_key_set
    ? model.embedding_api_key_masked
    : model.api_key_set ? "复用聊天凭据" : "未保存";
  elements.settingEmbeddingBaseUrl.value = model.embedding_base_url || "";
  elements.settingEmbeddingModel.value = model.embedding_model || "";
  elements.settingEmbeddingTimeout.value = String(model.embedding_timeout_seconds || 8);
  elements.settingTimeout.value = String(model.timeout_seconds || 12);
  elements.settingModelMaxConcurrent.value = String(model.max_concurrent || 4);
  elements.settingModelCircuitFailures.value = String(model.circuit_failures || 2);
  elements.settingModelCircuitCooldown.value = String(model.circuit_cooldown_seconds || 30);
  renderModelRuntime(model, model.runtime || {});
  setSettingState(elements.settingModelStatus, model.configured);

  elements.settingAgentEnabled.checked = Boolean(agent.enabled);
  elements.settingAgentMaxSteps.value = String(agent.max_steps || 4);
  elements.settingAgentWorkers.value = String(agent.workers || 2);
  elements.settingAgentQueueCapacity.value = String(agent.queue_capacity || 64);
  elements.settingAgentSessionQueueLimit.value = String(agent.session_queue_limit || 3);
  const taskBudget = agent.budget || {};
  elements.settingTaskWallTime.value = String(taskBudget.wall_time_seconds || 180);
  elements.settingTaskModelTokens.value = String(taskBudget.model_token_limit || 32768);
  elements.settingTaskModelCost.value = String(taskBudget.model_cost_limit || 1);
  elements.settingTaskToolCalls.value = String(taskBudget.tool_call_limit || 16);
  elements.settingTaskModelCalls.value = String(taskBudget.model_call_limit || 8);
  elements.settingModelMaxOutputTokens.value = String(taskBudget.max_output_tokens || 2048);
  elements.settingInputTokenCost.value = String(taskBudget.input_cost_per_million || 0);
  elements.settingOutputTokenCost.value = String(taskBudget.output_cost_per_million || 0);
  elements.settingCostCurrency.value = taskBudget.currency || "USD";
  const agentQueue = agent.queue || {};
  const agentWorker = agentQueue.worker || {};
  elements.settingAgentQueueState.textContent = [
    `${agentWorker.live_workers || 0}/${agentWorker.configured_workers || agent.workers || 2} 个 worker 在线`,
    `${agentWorker.active_workers || 0} 个正在执行`,
    `${agentQueue.active || 0}/${agentQueue.capacity || agent.queue_capacity || 64} 条占用队列`,
    "同一会话顺序执行 · 执行中自动续租",
  ].join(" · ");
  const pythonCapability = (settings.capabilities || []).find((item) => item.id === "python") || {};
  const pythonSandbox = pythonCapability.sandbox || {};
  const pythonSandboxReady = pythonSandbox.ok === true && pythonSandbox.os_enforced === true;
  const pythonBackendNames = {
    "macos-sandbox-exec": "macOS sandbox-exec",
    unavailable: "无可用系统后端",
    language: "仅语言白名单",
  };
  elements.settingPythonSandbox.dataset.state = pythonSandboxReady ? "ready" : "missing";
  elements.settingPythonSandboxState.textContent = pythonSandboxReady ? "系统隔离已启用" : "已拒绝不安全降级";
  elements.settingPythonSandboxDetail.textContent = [
    pythonBackendNames[pythonSandbox.backend] || pythonSandbox.backend || "状态未知",
    pythonSandbox.fail_closed ? "后端不可用时拒绝执行" : "当前允许语言级降级",
    "禁止联网",
    "禁止读写用户文件",
    "禁止启动子进程",
    `输出上限 ${Math.round((pythonSandbox.max_stdout_bytes || 0) / 1024)} KiB`,
  ].join(" · ");
  elements.settingTimezone.value = agent.timezone || "Asia/Shanghai";
  elements.settingAllowDesktop.checked = Boolean(agent.allow_desktop);
  elements.settingAgentWriteDirs.value = (agent.allowed_write_dirs || []).join("\n");
  elements.settingAgentSystemTasks.value = JSON.stringify(agent.system_tasks || {}, null, 2);
  const systemTaskCount = Object.keys(agent.system_tasks || {}).length;
  elements.settingAgentSystemTasksState.dataset.state = agent.system_tasks_error ? "error" : "ready";
  elements.settingAgentSystemTasksState.textContent = agent.system_tasks_error
    ? `当前配置无效：${agent.system_tasks_error}`
    : systemTaskCount
      ? `已配置 ${systemTaskCount} 个固定任务；命令、目录和超时会写入确认目标指纹。`
      : "尚未配置本机任务；Agent 不会执行任意终端命令。";
  elements.settingAgentWorkspace.textContent = agent.file_access?.workspace_root || "未初始化";
  elements.settingAgentProject.textContent = agent.file_access?.project_root || "未初始化";
  elements.settingAgentDesktop.textContent = agent.file_access?.desktop_path || "未检测";
  setSettingState(
    elements.settingAgentStatus,
    agent.enabled,
    agent.file_access?.desktop_allowed
      ? `已启用 · ${agent.workers || 2} worker · 桌面可写`
      : `已启用 · ${agent.workers || 2} worker`,
  );

  const vectorData = data.vector || {};
  const graphData = data.graph || {};
  const factStore = data.fact_store || {};
  const vectorRuntime = vectorData.runtime || {};
  const graphRuntime = graphData.runtime || {};
  const graphProjection = graphRuntime.projection || {};
  const externalStates = [
    vectorRuntime.enabled ? vectorRuntime.connection_state || "unprobed" : "disabled",
    graphProjection.enabled ? graphProjection.connection_state || "unprobed" : "disabled",
  ];
  const storageHasError = vectorRuntime.ok === false || graphRuntime.ok === false || externalStates.includes("error");
  const storageNeedsProbe = !storageHasError && externalStates.includes("unprobed");
  const storageReady = !storageHasError;
  elements.settingFactBackend.textContent = factStore.backend || "unknown";
  elements.settingFactLocation.textContent = factStore.location || "位置未知";
  elements.settingVectorBackend.value = vectorData.backend || "relational";
  elements.settingMilvusUri.value = vectorData.uri || "";
  elements.settingMilvusToken.value = "";
  elements.settingMilvusTokenMask.textContent = vectorData.token_masked || "未保存";
  elements.settingClearMilvusToken.checked = false;
  elements.settingMilvusDatabase.value = vectorData.database || "default";
  elements.settingMilvusPrefix.value = vectorData.collection_prefix || "agi_yukino_memory";
  elements.settingGraphBackend.value = graphData.backend || "relational";
  elements.settingNeo4jUri.value = graphData.uri || "";
  elements.settingNeo4jUsername.value = graphData.username || "neo4j";
  elements.settingNeo4jPassword.value = "";
  elements.settingNeo4jPasswordMask.textContent = graphData.password_masked || "未保存";
  elements.settingClearNeo4jPassword.checked = false;
  elements.settingNeo4jDatabase.value = graphData.database || "neo4j";
  elements.settingStorageRuntime.textContent = [
    `向量：${vectorRuntime.enabled ? `${vectorRuntime.backend}（${vectorRuntime.connection_state === "connected" ? "已连接" : vectorRuntime.connection_state === "error" ? "故障" : "待测试"}）` : "关系库"}`,
    `图谱：${graphProjection.enabled ? `${graphProjection.backend}（${graphProjection.connection_state === "connected" ? "已连接" : graphProjection.connection_state === "error" ? "故障" : "待测试"}）` : "关系表"}`,
    `实体 ${graphRuntime.entities || 0}`,
    `关系 ${graphRuntime.relations || 0}`,
    "外部索引均可从关系事实重建",
  ].join(" · ");
  elements.settingStorageStatus.textContent = storageHasError ? "连接异常" : storageNeedsProbe ? "等待测试" : "状态正常";
  elements.settingStorageStatus.dataset.state = storageHasError ? "missing" : storageNeedsProbe ? "pending" : "ready";

  elements.settingTtsEnabled.checked = Boolean(voice.enabled);
  elements.settingTtsModel.value = voice.model || "gpt-4o-mini-tts";
  $$('[data-voice-character]').forEach((input) => {
    input.value = voice.voices?.[input.dataset.voiceCharacter] || "alloy";
  });
  setSettingState(elements.settingVoiceStatus, voice.configured);

  elements.settingStickerStatus.textContent = `${stickers.total || 0} 张`;
  elements.settingStickerStatus.dataset.state = stickers.total ? "ready" : "missing";
  renderStickerCoverage(stickers);

  elements.settingMcpEndpoints.value = JSON.stringify(mcp.endpoints || {}, null, 2);
  elements.settingMcpHeaders.value = "";
  elements.settingClearMcpHeaders.checked = false;
  elements.settingMcpStdioEnabled.checked = Boolean(mcp.stdio_enabled);
  elements.settingMcpStdioServers.value = JSON.stringify(mcp.stdio_servers || {}, null, 2);
  elements.settingMcpStdioEnv.value = "";
  elements.settingClearMcpStdioEnv.checked = false;
  elements.settingMcpStdioEnvMask.textContent = mcp.stdio_env_set
    ? `已保存（${(mcp.stdio_env_servers || []).length} 个服务）`
    : "未保存";
  elements.settingMcpPrivateHostnames.checked = Boolean(mcp.allow_private_hostnames);
  const mcpTransport = mcp.transport_security || {};
  elements.settingMcpTransportSecurity.textContent = (
    mcpTransport.dns_pinned && mcpTransport.redirects_forbidden && mcpTransport.jsonrpc_response_validated
      && mcpTransport.stdio_shell_disabled && mcpTransport.stdio_environment_allowlisted
      && mcpTransport.stdio_application_secrets_inherited === false
      ? "传输保护已启用：HTTP 固定 DNS/禁止重定向 · stdio 不使用 Shell/仅继承基础运行环境 · 应用密钥不自动传入 · 启动需确认"
      : "MCP 传输保护状态异常"
  );
  const discoveredTools = mcp.catalog?.tool_count || 0;
  const discoveredServers = mcp.catalog?.server_count || 0;
  elements.settingMcpDiscovery.textContent = discoveredTools
    ? `已发现 ${discoveredServers} 个服务、${discoveredTools} 个工具；Agent 会按名称、说明和参数进行选择。`
    : "尚未发现 MCP 工具；保存端点后点击下方 tools/list 进行发现。";
  elements.settingMcpHeaderMask.textContent = mcp.headers_set ? `已保存（${mcp.header_servers.length} 个服务）` : "未保存";
  const configuredMcpCount = Object.keys(mcp.endpoints || {}).length
    + (mcp.stdio_enabled ? Object.keys(mcp.stdio_servers || {}).length : 0);
  setSettingState(elements.settingMcpStatus, mcp.configured, `${configuredMcpCount} 个服务`);

  elements.settingWebhook.value = channels.outbound_webhook || "";
  elements.settingWebhookSecret.value = "";
  elements.settingClearWebhookSecret.checked = false;
  elements.settingWebhookSecretMask.textContent = channels.webhook_secret_masked || "未保存";
  elements.settingInboundSecret.value = "";
  elements.settingClearInboundSecret.checked = false;
  elements.settingInboundSecretMask.textContent = channels.inbound_secret_masked || "未保存";
  elements.settingBackgroundReminderDelivery.checked = Boolean(channels.background_reminder_delivery);
  const delivery = channels.delivery || {};
  elements.settingBackgroundDeliveryState.textContent = channels.background_reminder_delivery
    ? `运行中 · 待派送 ${delivery.pending || 0} · 死信 ${delivery.dead_letters || 0}`
    : "未开启（提醒仍由当前页面领取）";
  elements.settingSmtpHost.value = mail.host || "";
  elements.settingSmtpPort.value = String(mail.port || 465);
  elements.settingSmtpUsername.value = mail.username || "";
  elements.settingSmtpSender.value = mail.sender || "";
  elements.settingSmtpPassword.value = "";
  elements.settingClearSmtpPassword.checked = false;
  elements.settingSmtpPasswordMask.textContent = mail.password_masked || "未保存";
  setSettingState(
    elements.settingConnectionStatus,
    channels.configured || channels.inbound_secret_set || mail.configured,
    channels.background_reminder_delivery
      ? "后台提醒已开启"
      : channels.inbound_secret_set ? "入站验签已开启" : "已配置",
  );

  const configured = [model.configured, agent.enabled, storageReady, voice.configured, stickers.total > 0, mcp.configured, channels.configured || channels.inbound_secret_set || mail.configured]
    .filter(Boolean).length;
  elements.settingsSummary.classList.toggle("ready", configured === 7);
  elements.settingsSummary.querySelector("span").textContent = `${configured}/7 类已配置`;

  elements.enableSticker.disabled = !stickers.total;
  elements.enableSticker.closest("label").classList.toggle("unavailable", !stickers.total);
  if (!stickers.total) elements.enableSticker.checked = false;
  elements.enableVoice.disabled = !voice.configured;
  elements.enableVoice.closest("label").classList.toggle("unavailable", !voice.configured);
  if (!voice.configured) elements.enableVoice.checked = false;

  const restricted = (settings.capabilities || []).filter((item) => item.permission && item.permission !== "allowed").length;
  const degraded = (settings.capabilities || []).filter((item) => item.state !== "ready").length;
  elements.capabilityCount.textContent = restricted ? `${restricted} 限制` : (degraded ? `${degraded} 待配` : "已配置");
}

async function loadSettings() {
  const settings = await requestJson("/api/settings");
  renderSettings(settings);
  return settings;
}

const permissionActionNames = {
  list: "查看", read: "读取", search: "搜索", write: "写入", run: "运行",
  fetch: "抓取网页", snapshot: "读取状态", run_task: "运行任务", status: "查看状态",
  add: "新增", complete: "完成", delete: "删除", observe: "观察", add_entity: "新增实体",
  add_relation: "新增关系", record: "记录", update: "更新", register: "注册", enable: "启停",
  invoke: "调用", call: "调用", enqueue: "后台排队", resume: "恢复", send: "发送",
  upload: "上传", analyze: "分析", speak: "语音合成", generate_image: "生成图片",
  generate_video: "生成视频", list_experiments: "实验列表", record_experiment: "记录实验",
};

const permissionGrantStatusNames = {
  active: "有效", consumed: "次数已用完", expired: "已过期", revoked: "已撤销",
};

function permissionActionLabel(action) {
  return `${permissionActionNames[action] || action} · ${action}`;
}

function syncPermissionGrantActions() {
  const capability = (state.permissionPolicy?.capabilities || [])
    .find((item) => item.id === elements.settingPermissionGrantCapability.value);
  const previous = elements.settingPermissionGrantAction.value;
  elements.settingPermissionGrantAction.replaceChildren();
  (capability?.actions || []).forEach((action) => {
    const option = document.createElement("option");
    option.value = action.id;
    option.textContent = permissionActionLabel(action.id);
    elements.settingPermissionGrantAction.appendChild(option);
  });
  if ([...elements.settingPermissionGrantAction.options].some((item) => item.value === previous)) {
    elements.settingPermissionGrantAction.value = previous;
  }
}

function renderPermissionPolicy(payload) {
  state.permissionPolicy = payload;
  const capabilities = Array.isArray(payload?.capabilities) ? payload.capabilities : [];
  const grants = Array.isArray(payload?.grants) ? payload.grants : [];
  const deniedRules = Number(payload?.status?.denied_rules || 0);
  elements.settingPermissionStatus.textContent = deniedRules ? `${deniedRules} 条限制` : "默认放行";
  elements.settingPermissionStatus.dataset.state = deniedRules ? "missing" : "ready";
  elements.settingPermissionList.replaceChildren();

  capabilities.forEach((capability) => {
    const card = document.createElement("article");
    card.className = "permission-policy-card";
    card.dataset.state = capability.enabled ? "enabled" : "disabled";

    const head = document.createElement("div");
    head.className = "permission-policy-head";
    const copy = document.createElement("p");
    const title = document.createElement("strong");
    title.textContent = capability.label || capability.id;
    const description = document.createElement("small");
    description.textContent = capability.description || capability.id;
    copy.append(title, description);
    const toggle = document.createElement("label");
    toggle.className = "permission-toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(capability.enabled);
    input.dataset.permissionCapability = capability.id;
    input.dataset.permissionAction = "*";
    const track = document.createElement("span");
    const label = document.createElement("b");
    label.textContent = capability.enabled ? "已允许" : "已关闭";
    toggle.append(input, track, label);
    head.append(copy, toggle);

    const actions = document.createElement("div");
    actions.className = "permission-action-list";
    (capability.actions || []).forEach((action) => {
      const actionToggle = document.createElement("label");
      actionToggle.className = "permission-action-toggle";
      actionToggle.dataset.state = action.enabled ? "enabled" : "disabled";
      const actionInput = document.createElement("input");
      actionInput.type = "checkbox";
      actionInput.checked = Boolean(action.enabled);
      actionInput.disabled = !capability.enabled;
      actionInput.dataset.permissionCapability = capability.id;
      actionInput.dataset.permissionAction = action.id;
      const actionText = document.createElement("span");
      actionText.textContent = permissionActionLabel(action.id);
      actionToggle.append(actionInput, actionText);
      actions.appendChild(actionToggle);
    });
    card.append(head, actions);
    elements.settingPermissionList.appendChild(card);
  });

  const previousCapability = elements.settingPermissionGrantCapability.value;
  elements.settingPermissionGrantCapability.replaceChildren();
  capabilities.forEach((capability) => {
    const option = document.createElement("option");
    option.value = capability.id;
    option.textContent = `${capability.label} · ${capability.id}`;
    elements.settingPermissionGrantCapability.appendChild(option);
  });
  if ([...elements.settingPermissionGrantCapability.options]
    .some((item) => item.value === previousCapability)) {
    elements.settingPermissionGrantCapability.value = previousCapability;
  }
  syncPermissionGrantActions();

  elements.settingPermissionGrants.replaceChildren();
  if (!grants.length) {
    const empty = document.createElement("p");
    empty.className = "permission-grant-empty";
    empty.textContent = state.sessionId ? "当前会话还没有临时授权。" : "请先打开一个对话。";
    elements.settingPermissionGrants.appendChild(empty);
    return;
  }
  grants.forEach((grantItem) => {
    const row = document.createElement("article");
    row.className = "permission-grant-row";
    row.dataset.state = grantItem.status || "unknown";
    const copy = document.createElement("p");
    const title = document.createElement("strong");
    title.textContent = `${grantItem.capability}.${grantItem.action}`;
    const meta = document.createElement("small");
    meta.textContent = `${permissionGrantStatusNames[grantItem.status] || grantItem.status} · 剩余 ${Number(grantItem.remaining_uses || 0)} 次 · 到期 ${formatMemoryTime(grantItem.expires_at)}`;
    copy.append(title, meta);
    const badge = document.createElement("span");
    badge.textContent = permissionGrantStatusNames[grantItem.status] || grantItem.status;
    row.append(copy, badge);
    if (grantItem.status === "active") {
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.dataset.permissionGrantRevoke = grantItem.id;
      revoke.textContent = "撤销";
      row.appendChild(revoke);
    }
    elements.settingPermissionGrants.appendChild(row);
  });
}

async function loadPermissionPolicy() {
  const query = new URLSearchParams({ session_id: state.sessionId || "" });
  const payload = await requestJson(`/api/agent/capability-policy?${query}`);
  renderPermissionPolicy(payload);
  return payload;
}

async function updateCapabilityPermission(capability, action, enabled) {
  const scope = action === "*" ? `${capability} 的全部动作` : `${capability}.${action}`;
  if (!window.confirm(`${enabled ? "允许" : "关闭"} ${scope}？更改会立即影响正在使用这些能力的 Agent。`)) {
    renderPermissionPolicy(state.permissionPolicy);
    return;
  }
  state.permissionPolicyBusy = true;
  elements.settingPermissionOutput.textContent = "正在更新权限……";
  try {
    const payload = await requestJson("/api/agent/capability-policy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId || "",
        rules: [{ capability, action, enabled }],
        confirmed: true,
      }),
    });
    renderPermissionPolicy(payload);
    elements.settingPermissionOutput.textContent = `${scope} 已${enabled ? "允许" : "关闭"}。`;
    showToast("能力权限已更新");
  } catch (error) {
    elements.settingPermissionOutput.textContent = `权限更新失败：${String(error)}`;
    await loadPermissionPolicy().catch(() => {});
  } finally {
    state.permissionPolicyBusy = false;
  }
}

async function createTemporaryCapabilityGrant() {
  const capability = elements.settingPermissionGrantCapability.value;
  const action = elements.settingPermissionGrantAction.value;
  const ttlSeconds = Number(elements.settingPermissionGrantTtl.value || 3600);
  const uses = Number(elements.settingPermissionGrantUses.value || 1);
  if (!state.sessionId || !capability || !action) {
    elements.settingPermissionOutput.textContent = "请先打开一个对话并选择能力动作。";
    return;
  }
  if (!window.confirm(`为当前会话临时允许 ${capability}.${action}，最多 ${uses} 次？`)) return;
  state.permissionPolicyBusy = true;
  elements.settingPermissionGrantCreate.disabled = true;
  elements.settingPermissionOutput.textContent = "正在发放临时授权……";
  try {
    await requestJson("/api/agent/capability-grants", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId,
        capability,
        action,
        ttl_seconds: ttlSeconds,
        uses,
        confirmed: true,
      }),
    });
    await loadPermissionPolicy();
    elements.settingPermissionOutput.textContent = "临时授权已发放，只对当前会话有效。";
    showToast("当前会话已获得临时授权");
  } catch (error) {
    elements.settingPermissionOutput.textContent = `授权失败：${String(error)}`;
  } finally {
    state.permissionPolicyBusy = false;
    elements.settingPermissionGrantCreate.disabled = false;
  }
}

async function revokeTemporaryCapabilityGrant(grantId) {
  if (!window.confirm("撤销这条当前会话授权？正在等待执行的动作之后会被权限闸门拒绝。")) return;
  state.permissionPolicyBusy = true;
  elements.settingPermissionOutput.textContent = "正在撤销授权……";
  try {
    await requestJson(`/api/agent/capability-grants/${encodeURIComponent(grantId)}/revoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, confirmed: true }),
    });
    await loadPermissionPolicy();
    elements.settingPermissionOutput.textContent = "临时授权已撤销。";
    showToast("临时授权已撤销");
  } catch (error) {
    elements.settingPermissionOutput.textContent = `撤销失败：${String(error)}`;
  } finally {
    state.permissionPolicyBusy = false;
  }
}

const dispatchStatusNames = {
  prepared: "等待外发",
  dispatching: "外发中",
  completed: "已完成",
  failed: "失败",
  uncertain: "待核对",
  abandoned: "已放弃",
};

function shortDispatchKey(value) {
  const text = String(value || "");
  return text.length > 27 ? `${text.slice(0, 18)}…${text.slice(-6)}` : text;
}

function renderExternalDispatches(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const uncertain = Number(payload?.uncertain || 0);
  elements.settingDispatchList.replaceChildren();
  elements.settingDispatchStatus.textContent = uncertain ? `${uncertain} 条待核对` : `${items.length} 条记录`;
  elements.settingDispatchStatus.dataset.state = uncertain ? "missing" : "ready";
  elements.settingDispatchOutput.textContent = payload?.automatic_replay === false
    ? "自动重发已关闭 · 当前会话隔离"
    : "当前会话的外发记录";
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "dispatch-empty";
    empty.textContent = state.sessionId ? "当前会话还没有外发记录。" : "请先选择一个对话会话。";
    elements.settingDispatchList.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const card = document.createElement("article");
    card.className = "dispatch-review-card";
    card.dataset.state = item.status || "unknown";

    const head = document.createElement("div");
    head.className = "dispatch-card-head";
    const title = document.createElement("strong");
    title.textContent = `${item.capability || "external"} · ${item.action || "call"}`;
    const badge = document.createElement("span");
    badge.textContent = dispatchStatusNames[item.status] || item.status || "未知";
    head.append(title, badge);

    const meta = document.createElement("p");
    meta.className = "dispatch-meta";
    const entries = [
      ["外发", item.id],
      ["幂等键", shortDispatchKey(item.provider_idempotency_key)],
      ["尝试", `第 ${Number(item.attempts || 0)} 次`],
      ["更新", formatMemoryTime(item.updated_at)],
    ];
    entries.forEach(([label, value]) => {
      const part = document.createElement("span");
      part.append(`${label} `);
      const code = document.createElement("code");
      code.textContent = String(value || "-");
      if (label === "幂等键") code.title = String(item.provider_idempotency_key || "");
      part.appendChild(code);
      meta.appendChild(part);
    });
    card.append(head, meta);

    const providerReceipt = item.receipt?.provider_receipt;
    if (providerReceipt && typeof providerReceipt === "object" && Object.keys(providerReceipt).length) {
      const receipt = document.createElement("p");
      receipt.className = "dispatch-meta";
      receipt.textContent = `供应商回执 · ${Object.entries(providerReceipt).map(([key, value]) => `${key}: ${value}`).join(" · ")}`;
      card.appendChild(receipt);
    }
    if (item.error) {
      const error = document.createElement("p");
      error.className = "dispatch-error";
      error.textContent = item.error;
      card.appendChild(error);
    }
    if (item.status === "uncertain") {
      const actions = document.createElement("div");
      actions.className = "dispatch-actions";
      [
        ["confirm_succeeded", "确认外部已成功"],
        ["retry", "使用原标识重试"],
        ["abandon", "放弃本次外发"],
      ].forEach(([decision, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.dispatchId = item.id;
        button.dataset.dispatchDecision = decision;
        button.textContent = label;
        button.disabled = state.dispatchReviewBusy;
        actions.appendChild(button);
      });
      card.appendChild(actions);
    }
    elements.settingDispatchList.appendChild(card);
  });
}

async function loadExternalDispatches() {
  if (!state.sessionId) {
    renderExternalDispatches({ items: [], uncertain: 0, automatic_replay: false });
    return;
  }
  const filter = elements.settingDispatchFilter.value;
  const query = new URLSearchParams({ session_id: state.sessionId, limit: "100" });
  if (filter) query.set("status", filter);
  const payload = await requestJson(`/api/agent/external-dispatches?${query}`);
  renderExternalDispatches(payload);
}

async function reviewExternalDispatch(dispatchId, decision) {
  const prompts = {
    confirm_succeeded: "请确认：你已经在外部系统中核对到这次操作确实成功。系统将只认领原结果，不会再次发送。",
    retry: "将使用原 dispatch ID 和幂等键再次请求。若第三方不支持幂等，仍可能重复执行。确定重试吗？",
    abandon: "放弃后这次外发会关闭，原任务不会再自动继续。确定放弃吗？",
  };
  if (!window.confirm(prompts[decision] || "确定处理这条外发记录吗？")) return;
  state.dispatchReviewBusy = true;
  elements.settingDispatchOutput.textContent = decision === "retry" ? "正在使用原标识重试……" : "正在提交核对结果……";
  elements.settingDispatchList.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const result = await requestJson(`/api/agent/external-dispatches/${encodeURIComponent(dispatchId)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, decision, confirmed: true }),
    });
    const status = dispatchStatusNames[result.dispatch?.status] || result.dispatch?.status || "已处理";
    elements.settingDispatchOutput.textContent = `${status} · 原外发标识已保留`;
    showToast(decision === "retry" ? `外发重试结果：${status}` : `外发记录已更新：${status}`);
  } catch (error) {
    const detail = requestErrorDetail(error, "外发记录处理失败，请刷新后重试。");
    elements.settingDispatchOutput.textContent = detail;
    showToast(detail);
  } finally {
    state.dispatchReviewBusy = false;
    await loadExternalDispatches().catch(() => {});
  }
}

function parseObjectField(element, label, { allowBlank = false } = {}) {
  const raw = element.value.trim();
  if (!raw && allowBlank) return null;
  try {
    const parsed = JSON.parse(raw || "{}");
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error();
    return parsed;
  } catch {
    throw new Error(`${label}必须是 JSON 对象。`);
  }
}

elements.capabilityToggle.addEventListener("click", async () => {
  elements.capabilityDialog.showModal();
  elements.settingsOutput.textContent = "正在读取本机设置……";
  try {
    await Promise.all([loadSettings(), loadExternalDispatches(), loadPermissionPolicy()]);
    elements.settingsOutput.textContent = "修改只会保存在本机。";
  } catch {
    elements.settingsOutput.textContent = "设置读取失败，请检查服务状态。";
  }
});
elements.settingsClose.addEventListener("click", () => elements.capabilityDialog.close());

$$('[data-settings-section]').forEach((button) => button.addEventListener("click", () => {
  $$('[data-settings-section]').forEach((item) => item.classList.toggle("active", item === button));
  $$('[data-settings-panel]').forEach((panel) => panel.classList.toggle("active", panel.dataset.settingsPanel === button.dataset.settingsSection));
  if (button.dataset.settingsSection === "dispatches") {
    loadExternalDispatches().catch(() => {
      elements.settingDispatchOutput.textContent = "外发记录读取失败。";
    });
  }
  if (button.dataset.settingsSection === "permissions") {
    loadPermissionPolicy().catch(() => {
      elements.settingPermissionOutput.textContent = "能力权限读取失败。";
    });
  }
}));

elements.settingPermissionRefresh.addEventListener("click", () => {
  loadPermissionPolicy().then(() => {
    elements.settingPermissionOutput.textContent = "权限状态已刷新。";
  }).catch(() => {
    elements.settingPermissionOutput.textContent = "能力权限读取失败。";
  });
});
elements.settingPermissionGrantCapability.addEventListener("change", syncPermissionGrantActions);
elements.settingPermissionGrantCreate.addEventListener("click", createTemporaryCapabilityGrant);
elements.settingPermissionList.addEventListener("change", (event) => {
  const input = event.target.closest("input[data-permission-capability]");
  if (!input || state.permissionPolicyBusy) return;
  updateCapabilityPermission(
    input.dataset.permissionCapability,
    input.dataset.permissionAction,
    input.checked,
  );
});
elements.settingPermissionGrants.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-permission-grant-revoke]");
  if (!button || state.permissionPolicyBusy) return;
  revokeTemporaryCapabilityGrant(button.dataset.permissionGrantRevoke);
});

elements.settingDispatchRefresh.addEventListener("click", () => {
  loadExternalDispatches().catch(() => {
    elements.settingDispatchOutput.textContent = "外发记录读取失败。";
  });
});
elements.settingDispatchFilter.addEventListener("change", () => {
  loadExternalDispatches().catch(() => {
    elements.settingDispatchOutput.textContent = "外发记录读取失败。";
  });
});
elements.settingDispatchList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-dispatch-decision]");
  if (!button || state.dispatchReviewBusy) return;
  reviewExternalDispatch(button.dataset.dispatchId, button.dataset.dispatchDecision);
});

elements.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  let mcpEndpoints;
  let mcpHeaders;
  let mcpStdioServers;
  let mcpStdioEnv;
  let systemTasks;
  try {
    mcpEndpoints = parseObjectField(elements.settingMcpEndpoints, "MCP 服务");
    mcpHeaders = parseObjectField(elements.settingMcpHeaders, "MCP 请求头", { allowBlank: true });
    mcpStdioServers = parseObjectField(elements.settingMcpStdioServers, "stdio MCP 服务");
    mcpStdioEnv = parseObjectField(elements.settingMcpStdioEnv, "stdio MCP 环境变量", { allowBlank: true });
    systemTasks = parseObjectField(elements.settingAgentSystemTasks, "本机任务");
  } catch (error) {
    elements.settingsOutput.textContent = error.message;
    return;
  }
  const voices = {};
  $$('[data-voice-character]').forEach((input) => { voices[input.dataset.voiceCharacter] = input.value.trim() || "alloy"; });
  const payload = {
    openai_api_key: elements.settingApiKey.value.trim() || null,
    clear_openai_api_key: elements.settingClearApiKey.checked,
    openai_base_url: elements.settingBaseUrl.value.trim(),
    openai_model: elements.settingModel.value.trim() || "gpt-4.1-mini",
    openai_fallback_model: elements.settingFallbackModel.value.trim(),
    openai_embedding_api_key: elements.settingEmbeddingApiKey.value.trim() || null,
    clear_openai_embedding_api_key: elements.settingClearEmbeddingApiKey.checked,
    openai_embedding_base_url: elements.settingEmbeddingBaseUrl.value.trim(),
    openai_embedding_model: elements.settingEmbeddingModel.value.trim(),
    openai_embedding_timeout_seconds: Number(elements.settingEmbeddingTimeout.value || 8),
    openai_timeout_seconds: Number(elements.settingTimeout.value || 12),
    model_max_concurrent: Number(elements.settingModelMaxConcurrent.value || 4),
    model_circuit_failures: Number(elements.settingModelCircuitFailures.value || 2),
    model_circuit_cooldown_seconds: Number(elements.settingModelCircuitCooldown.value || 30),
    agent_enabled: elements.settingAgentEnabled.checked,
    agent_max_steps: Number(elements.settingAgentMaxSteps.value || 4),
    agent_workers: Number(elements.settingAgentWorkers.value || 2),
    agent_queue_capacity: Number(elements.settingAgentQueueCapacity.value || 64),
    agent_session_queue_limit: Number(elements.settingAgentSessionQueueLimit.value || 3),
    task_wall_time_seconds: Number(elements.settingTaskWallTime.value || 180),
    task_model_token_limit: Number(elements.settingTaskModelTokens.value || 32768),
    task_model_cost_limit: Number(elements.settingTaskModelCost.value || 1),
    task_tool_call_limit: Number(elements.settingTaskToolCalls.value || 16),
    task_model_call_limit: Number(elements.settingTaskModelCalls.value || 8),
    model_max_output_tokens: Number(elements.settingModelMaxOutputTokens.value || 2048),
    input_cost_per_million: Number(elements.settingInputTokenCost.value || 0),
    output_cost_per_million: Number(elements.settingOutputTokenCost.value || 0),
    cost_currency: elements.settingCostCurrency.value.trim() || "USD",
    allow_desktop: elements.settingAllowDesktop.checked,
    allowed_write_dirs: elements.settingAgentWriteDirs.value.split("\n").map((item) => item.trim()).filter(Boolean),
    system_tasks: systemTasks,
    timezone: elements.settingTimezone.value.trim() || "Asia/Shanghai",
    vector_backend: elements.settingVectorBackend.value,
    milvus_uri: elements.settingMilvusUri.value.trim(),
    milvus_token: elements.settingMilvusToken.value || null,
    clear_milvus_token: elements.settingClearMilvusToken.checked,
    milvus_database: elements.settingMilvusDatabase.value.trim() || "default",
    milvus_collection_prefix: elements.settingMilvusPrefix.value.trim() || "agi_yukino_memory",
    graph_backend: elements.settingGraphBackend.value,
    neo4j_uri: elements.settingNeo4jUri.value.trim(),
    neo4j_username: elements.settingNeo4jUsername.value.trim() || "neo4j",
    neo4j_password: elements.settingNeo4jPassword.value || null,
    clear_neo4j_password: elements.settingClearNeo4jPassword.checked,
    neo4j_database: elements.settingNeo4jDatabase.value.trim() || "neo4j",
    tts_enabled: elements.settingTtsEnabled.checked,
    openai_tts_model: elements.settingTtsModel.value.trim() || "gpt-4o-mini-tts",
    tts_voices: voices,
    mcp_endpoints: mcpEndpoints,
    mcp_headers: mcpHeaders,
    clear_mcp_headers: elements.settingClearMcpHeaders.checked,
    mcp_stdio_enabled: elements.settingMcpStdioEnabled.checked,
    mcp_stdio_servers: mcpStdioServers,
    mcp_stdio_env: mcpStdioEnv,
    clear_mcp_stdio_env: elements.settingClearMcpStdioEnv.checked,
    mcp_allow_private_hostnames: elements.settingMcpPrivateHostnames.checked,
    outbound_webhook: elements.settingWebhook.value.trim(),
    outbound_webhook_secret: elements.settingWebhookSecret.value || null,
    clear_outbound_webhook_secret: elements.settingClearWebhookSecret.checked,
    inbound_webhook_secret: elements.settingInboundSecret.value || null,
    clear_inbound_webhook_secret: elements.settingClearInboundSecret.checked,
    background_reminder_delivery: elements.settingBackgroundReminderDelivery.checked,
    smtp_host: elements.settingSmtpHost.value.trim(),
    smtp_port: Number(elements.settingSmtpPort.value || 465),
    smtp_username: elements.settingSmtpUsername.value.trim(),
    smtp_password: elements.settingSmtpPassword.value || null,
    clear_smtp_password: elements.settingClearSmtpPassword.checked,
    smtp_sender: elements.settingSmtpSender.value.trim(),
  };
  elements.settingsSave.disabled = true;
  elements.settingsOutput.textContent = "正在保存并刷新运行时……";
  try {
    const result = await requestJson("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderSettings(result.settings);
    elements.settingsOutput.textContent = "设置已保存并立即生效。";
    showToast("本机设置已更新");
    await loadCapabilities();
  } catch (error) {
    elements.settingsOutput.textContent = `保存失败：${String(error)}`;
  } finally {
    elements.settingsSave.disabled = false;
  }
});

$$('[data-settings-test]').forEach((button) => button.addEventListener("click", async () => {
  const area = button.dataset.settingsTest;
  const output = $(`#setting-${area}-test`);
  button.disabled = true;
  output.textContent = "正在测试……";
  try {
    const result = await requestJson("/api/settings/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        area,
        model_role: button.dataset.modelRole || "primary",
        character: elements.settingVoiceTestCharacter.value,
        server: elements.settingMcpTestServer.value.trim(),
      }),
    });
    output.textContent = result.message || (result.ok ? "测试成功" : "测试失败");
    output.dataset.state = result.ok ? "ready" : "error";
    if (area === "model" && result.runtime && state.settings?.model) {
      state.settings.model.runtime = result.runtime;
      renderModelRuntime(state.settings.model, result.runtime);
    }
    if (area === "voice" && result.audio_url) {
      elements.settingVoicePreview.src = result.audio_url;
      elements.settingVoicePreview.hidden = false;
    }
    if (area === "mcp" && result.ok) {
      await loadSettings();
    }
    if (area === "storage" && result.vector && result.graph && state.settings?.data) {
      state.settings.data.vector.runtime = result.vector;
      state.settings.data.graph.runtime = result.graph;
      renderSettings(state.settings);
    }
  } catch (error) {
    output.textContent = `测试失败：${String(error)}`;
    output.dataset.state = "error";
  } finally {
    button.disabled = false;
  }
}));

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result).split(",", 2)[1] || ""));
    reader.addEventListener("error", () => reject(new Error("文件读取失败")));
    reader.readAsDataURL(file);
  });
}

async function uploadChatAttachments(files) {
  const remaining = Math.max(0, 4 - state.attachments.length);
  const selected = [...files].slice(0, remaining);
  if (!selected.length) {
    showToast(state.attachments.length >= 4 ? "每次最多发送 4 个附件" : "没有选择附件");
    return;
  }
  if ([...files].length > remaining) showToast(`只会添加前 ${remaining} 个附件`);
  state.attachmentUploading = true;
  syncComposerAvailability();
  for (const file of selected) {
    if (file.size > 10 * 1024 * 1024) {
      showToast(`${file.name} 超过 10 MiB，未上传`);
      continue;
    }
    try {
      const encoded = await readFileBase64(file);
      const attachment = await requestJson("/api/attachments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: state.sessionId,
          filename: file.name,
          mime_type: file.type || "application/octet-stream",
          base64: encoded,
        }),
      });
      state.attachments.push(attachment);
      renderComposerAttachments();
    } catch {
      showToast(`${file.name} 不受支持或上传失败`);
    }
  }
  state.attachmentUploading = false;
  elements.attachmentInput.value = "";
  renderComposerAttachments();
}

elements.attachmentButton.addEventListener("click", () => elements.attachmentInput.click());
elements.attachmentInput.addEventListener("change", async () => {
  await uploadChatAttachments(elements.attachmentInput.files || []);
});
elements.attachmentTray.addEventListener("click", async (event) => {
  const remove = event.target.closest("button[data-remove-attachment]");
  if (!remove) return;
  remove.disabled = true;
  try {
    const params = new URLSearchParams({ session_id: state.sessionId });
    await requestJson(`/api/attachments/${encodeURIComponent(remove.dataset.removeAttachment)}?${params}`, {
      method: "DELETE",
    });
    state.attachments = state.attachments.filter((item) => item.id !== remove.dataset.removeAttachment);
    renderComposerAttachments();
  } catch {
    remove.disabled = false;
    showToast("附件没有移除，请稍后再试");
  }
});

elements.settingStickerUpload.addEventListener("click", async () => {
  const file = elements.settingStickerFile.files?.[0];
  if (!file) {
    elements.settingStickerOutput.textContent = "请先选择一张贴纸图片。";
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    elements.settingStickerOutput.textContent = "贴纸不能超过 5 MiB。";
    return;
  }
  elements.settingStickerUpload.disabled = true;
  elements.settingStickerOutput.textContent = "正在保存贴纸……";
  try {
    const encoded = await readFileBase64(file);
    const result = await requestJson("/api/settings/stickers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character: elements.settingStickerCharacter.value,
        emotion: elements.settingStickerEmotion.value,
        filename: file.name,
        base64: encoded,
      }),
    });
    state.settings.stickers = result.stickers;
    renderSettings(state.settings);
    elements.settingStickerOutput.textContent = `贴纸已保存：${result.path}`;
    elements.settingStickerFile.value = "";
    showToast("角色贴纸已安装");
  } catch (error) {
    elements.settingStickerOutput.textContent = `上传失败：${String(error)}`;
  } finally {
    elements.settingStickerUpload.disabled = false;
  }
});

async function setMode(mode, { loadScope = true } = {}) {
  if (state.chatOpen && state.chatMode === mode) {
    setChatOpen(false);
    return;
  }
  const previousScope = conversationScopeKey();
  state.chatMode = mode;
  updateConversationIdentity();
  setChatOpen(true);
  savePreferences();
  if (loadScope && (previousScope !== conversationScopeKey() || !state.sessionId)) await activateConversationScope();
}

modeButtons.forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));

elements.newConversation.addEventListener("click", createNewConversation);
elements.conversationList.addEventListener("click", async (event) => {
  const open = event.target.closest("button[data-open-conversation]");
  if (open) {
    setChatOpen(true);
    if (open.dataset.openConversation === state.sessionId) return;
    setConversationBusy(true);
    try {
      await switchConversation(open.dataset.openConversation);
    } catch {
      showToast("这段历史暂时无法打开");
    } finally {
      setConversationBusy(false);
    }
    return;
  }
  const remove = event.target.closest("button[data-delete-conversation]");
  if (!remove) return;
  const conversationId = remove.dataset.deleteConversation;
  const title = remove.dataset.conversationTitle || "这段会话";
  if (!globalThis.confirm(`确定删除“${title}”吗？这段对话和它的记忆都会被删除。`)) return;
  remove.disabled = true;
  try {
    await requestJson(`/api/conversations/${encodeURIComponent(conversationId)}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmed: true }),
    });
    if (conversationId === state.sessionId) {
      delete state.activeConversations[conversationScopeKey()];
      persistActiveConversations();
      await activateConversationScope();
    } else {
      await refreshConversationList();
    }
    showToast("会话已删除");
  } catch {
    showToast("会话没有删除，请稍后再试");
    remove.disabled = false;
  }
});

elements.memoryToggle.addEventListener("click", async () => {
  elements.memoryDialog.showModal();
  await loadMemoryArchive(elements.memorySearch.value);
});
elements.memoryClose.addEventListener("click", () => elements.memoryDialog.close());
elements.memoryDialog.addEventListener("click", (event) => {
  if (event.target === elements.memoryDialog) elements.memoryDialog.close();
});
elements.memorySearchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await loadMemoryArchive(elements.memorySearch.value);
});
elements.memorySearchReset.addEventListener("click", async () => {
  elements.memorySearch.value = "";
  await loadMemoryArchive();
});
elements.memoryDialog.addEventListener("click", async (event) => {
  const reminderButton = event.target.closest("button[data-reminder-cancel]");
  if (reminderButton) {
    reminderButton.disabled = true;
    try {
      const result = await requestJson("/api/reminders/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: state.sessionId,
          reminder_id: Number(reminderButton.dataset.reminderCancel),
        }),
      });
      if (!result.ok) throw new Error("not cancellable");
      showToast("提醒已经取消");
      await loadMemoryArchive(elements.memorySearch.value);
    } catch {
      reminderButton.disabled = false;
      showToast("提醒没有取消，请稍后再试");
    }
    return;
  }
  const button = event.target.closest("button[data-memory-kind]");
  if (!button) return;
  const label = button.dataset.memoryLabel || "这条记忆";
  if (!globalThis.confirm(`确定让侍奉部忘记“${label}”吗？`)) return;
  button.disabled = true;
  try {
    await requestJson("/api/memory/forget", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId,
        kind: button.dataset.memoryKind,
        memory_id: button.dataset.memoryId ? Number(button.dataset.memoryId) : null,
        key: button.dataset.memoryKey || "",
        confirmed: true,
      }),
    });
    showToast("这条记忆已经删除");
    await loadMemoryArchive(elements.memorySearch.value);
  } catch {
    showToast("记忆没有删除，请稍后再试");
    button.disabled = false;
  }
});

elements.clearMemory.addEventListener("click", async () => {
  if (!globalThis.confirm("确定清空这个会话的全部记忆吗？此操作无法撤销。")) return;
  try {
    await requestJson("/api/memory/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, confirmed: true }),
    });
    state.messages = [];
    state.attachments = [];
    elements.attachmentInput.value = "";
    renderComposerAttachments();
    renderStoredMessages();
    await refreshConversationList();
    showToast("这段会话的记忆已清空");
    if (elements.memoryDialog.open) await loadMemoryArchive(elements.memorySearch.value);
  } catch {
    showToast("记忆没有清空，请稍后再试");
  }
});

elements.input.addEventListener("input", () => {
  if (state.resumeTaskId && !elements.input.value.trim()) {
    state.resumeTaskId = "";
    state.resumeGoal = "";
    state.allowUncertainReplay = false;
    showToast("已退出原任务恢复；接下来会作为新委托处理");
  }
});

elements.input.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const originalText = elements.input.value.trim();
  if ((!originalText && !state.attachments.length) || elements.send.disabled) return;
  const text = originalText || "请查看我发送的附件。";
  const sentAttachments = [...state.attachments];
  const sessionId = state.sessionId;
  const character = state.character;
  const chatMode = state.chatMode;
  const resumeTaskId = state.resumeTaskId;
  const userEntry = { role: "user", content: text, meta: "你 · 刚刚", attachments: sentAttachments };
  state.messages.push(userEntry);
  const userNode = addMessage("user", text, "你 · 刚刚", { attachments: sentAttachments });
  elements.input.value = "";
  state.submitting = true;
  syncComposerAvailability();
  const requestId = globalThis.crypto?.randomUUID?.()
    || `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  state.pendingNode = addMessage(
    "assistant",
    resumeTaskId
      ? "正在接回原任务账本，并核对哪些步骤已经完成……"
      : "正在理解委托，并检查可以使用的工具……",
    "",
    { pending: true, requestId, sessionId },
  );

  try {
    const job = await requestJson("/api/chat/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: state.messages.map(({ role, content }) => ({ role, content })),
        chat_mode: chatMode,
        routing_mode: "adaptive",
        character,
        session_id: sessionId,
        request_id: requestId,
        resume_task_id: state.resumeTaskId,
        allow_uncertain_replay: state.allowUncertainReplay,
        attachment_ids: sentAttachments.map((item) => item.id),
        enable_sticker: elements.enableSticker.checked,
        enable_voice: elements.enableVoice.checked,
      }),
    });
    const record = {
      jobId: job.id,
      taskId: job.task_id,
      requestId,
      sessionId,
      userText: text,
      attachments: sentAttachments,
      character,
      chatMode,
      createdAt: Date.now(),
      eventCursor: 0,
      pendingText: resumeTaskId
        ? "正在接回原任务账本，并核对哪些步骤已经完成……"
        : "委托已进入服务端持久队列；刷新或关闭页面都不会中断。",
    };
    rememberPendingChatJob(record);
    if (state.pendingNode) {
      state.pendingNode.dataset.chatJobId = job.id;
      state.pendingNode.dataset.pendingSessionId = sessionId;
      ensureAgentTimeline(state.pendingNode, job.task_id);
      updatePendingChatJob(job, record);
    }
    subscribeTaskProgress(record);
    await pollTaskProgress(record).catch(() => {});
    state.attachments = [];
    state.resumeTaskId = "";
    state.resumeGoal = "";
    state.allowUncertainReplay = false;
    elements.attachmentInput.value = "";
    renderComposerAttachments();
    startChatJobPolling(record);
    showToast(job.status === "running" ? "Agent 正在执行委托" : "委托已进入持久队列");
  } catch (error) {
    state.pendingNode?.remove();
    if (state.messages.at(-1) === userEntry) state.messages.pop();
    userNode.remove();
    elements.input.value = originalText;
    state.attachments = sentAttachments;
    renderComposerAttachments();
    const fallback = "活动室的线路暂时没接通。你的文字和附件仍留在输入区，等一下可以直接重试。";
    const detail = requestErrorDetail(error, fallback);
    addMessage("assistant", detail, resumeTaskId ? "Agent · 恢复未开始" : "系统 · 降级回应");
    showToast(resumeTaskId ? "原任务没有重放，请检查恢复条件" : "连接中断，已经保留你的输入");
  } finally {
    state.submitting = false;
    if (!state.pendingChatJobs[sessionId]) state.pendingNode = null;
    syncComposerAvailability();
    elements.input.focus();
  }
});

restorePreferences();
updateActivityClock();
renderComposerAttachments();
syncChatTriggers();
loadCharacters()
  .then(async () => {
    await adoptLegacyConversation();
    await activateConversationScope();
    await claimDueCare();
    await claimDueReminders();
  })
  .catch(() => showToast("角色名册或历史会话暂时无法读取"));
loadCapabilities().catch(() => showToast("能力状态暂时无法读取"));
loadSettings().catch(() => showToast("本机设置暂时无法读取"));
setInterval(claimDueCare, 60_000);
setInterval(claimDueReminders, 30_000);
setInterval(updateActivityClock, 1_000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    updateActivityClock();
    claimDueReminders();
  }
});
connectRuntimeEvents();
