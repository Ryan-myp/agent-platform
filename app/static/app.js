let videoJobPollTimer = null;
let videoJobPollInFlight = false;
let storageCleanupPreview = null;
const EXPECTED_API_CONTRACT_VERSION = 7;

const state = {
  config: null,
  projects: [],
  series: [],
  novels: [],
  activeNovel: null,
  activeSeriesAssets: [],
  activeNovelId: null,
  renderOrderProjectId: null,
  renderOrderDraft: [],
  selectedSeriesId: null,
  project: null,
  viewStage: null,
  selectedModel: null,
  modelDeletePending: null,
  chatMessages: [],
  chatHasMore: false,
  chatHistoryCursor: null,
  chatLoadingOlder: false,
  chatSending: false,
  pendingUploads: [],
  renameProjectId: null,
  recordFilter: "all",
  viewMode: "new",
  reviewHydrating: false,
  canvasProjectId: null,
  canvasDraft: [],
  canvasUpdatedAt: null,
  canvasSavedSnapshot: "[]",
  canvasRevision: 0,
  canvasSavedRevision: 0,
  canvasSaveTimer: null,
  canvasSaveInFlight: false,
  canvasSaveQueued: false,
  canvasSaveError: "",
  canvasConflictProject: null,
};

const workspace = document.querySelector("#workspace");
const notice = document.querySelector("#notice");
const projectList = document.querySelector("#project-list");
const modelSelect = document.querySelector("#model-select");
const modelsDialog = document.querySelector("#models-dialog");
const usageDialog = document.querySelector("#usage-dialog");
const confirmDialog = document.querySelector("#confirm-dialog");
const renameDialog = document.querySelector("#rename-dialog");

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function safeMarkdownHref(value) {
  if (!/^(https?:\/\/|mailto:|\/(?!\/))/i.test(value)) return "";
  try {
    const url = new URL(value, window.location.origin);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

function renderInlineMarkdown(value) {
  const text = String(value ?? "");
  const tokenPattern = /(`[^`\n]+`|\[[^\]\n]+\]\([^\s)]+\)|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\*[^*\n]+\*|_[^_\n]+_)/g;
  let html = "";
  let offset = 0;

  for (const match of text.matchAll(tokenPattern)) {
    html += escapeHTML(text.slice(offset, match.index));
    const token = match[0];
    if (token.startsWith("`")) {
      html += `<code>${escapeHTML(token.slice(1, -1))}</code>`;
    } else if (token.startsWith("[")) {
      const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      const href = link ? safeMarkdownHref(link[2]) : "";
      html += href
        ? `<a href="${escapeHTML(href)}" target="_blank" rel="noopener noreferrer">${renderInlineMarkdown(link[1])}</a>`
        : escapeHTML(token);
    } else if (token.startsWith("**") || token.startsWith("__")) {
      html += `<strong>${renderInlineMarkdown(token.slice(2, -2))}</strong>`;
    } else if (token.startsWith("~~")) {
      html += `<del>${renderInlineMarkdown(token.slice(2, -2))}</del>`;
    } else {
      html += `<em>${renderInlineMarkdown(token.slice(1, -1))}</em>`;
    }
    offset = match.index + token.length;
  }
  return html + escapeHTML(text.slice(offset));
}

function splitMarkdownTableRow(line) {
  let source = line.trim();
  if (source.startsWith("|")) source = source.slice(1);
  if (source.endsWith("|")) source = source.slice(0, -1);
  return source.split("|").map((cell) => cell.trim());
}

function isMarkdownTableDelimiter(line) {
  const cells = splitMarkdownTableRow(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function renderMarkdown(value) {
  const lines = String(value ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let paragraph = [];
  let listItems = [];
  let listTag = "";
  let quoteLines = [];
  let codeLines = null;
  let codeLanguage = "";

  const flushParagraph = () => {
    if (paragraph.length) blocks.push(`<p>${renderInlineMarkdown(paragraph.join(" ").trim())}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (listItems.length) blocks.push(`<${listTag}>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</${listTag}>`);
    listItems = [];
    listTag = "";
  };
  const flushQuote = () => {
    if (quoteLines.length) blocks.push(`<blockquote>${renderMarkdown(quoteLines.join("\n"))}</blockquote>`);
    quoteLines = [];
  };
  const flushCode = () => {
    const languageClass = /^[a-zA-Z0-9_+-]{1,24}$/.test(codeLanguage) ? ` class="language-${escapeHTML(codeLanguage)}"` : "";
    blocks.push(`<pre><code${languageClass}>${escapeHTML(codeLines.join("\n"))}</code></pre>`);
    codeLines = null;
    codeLanguage = "";
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (codeLines !== null) {
      if (/^\s*```\s*$/.test(line)) flushCode();
      else codeLines.push(line);
      continue;
    }

    const fence = /^\s*```([a-zA-Z0-9_+-]*)\s*$/.exec(line);
    if (fence) {
      flushParagraph();
      flushList();
      flushQuote();
      codeLines = [];
      codeLanguage = fence[1];
      continue;
    }
    if (index + 1 < lines.length && line.includes("|") && isMarkdownTableDelimiter(lines[index + 1])) {
      flushParagraph();
      flushList();
      flushQuote();
      const headers = splitMarkdownTableRow(line);
      const rows = [];
      index += 1;
      while (index + 1 < lines.length) {
        const nextLine = lines[index + 1];
        if (!nextLine.trim() || !nextLine.includes("|")) break;
        const cells = splitMarkdownTableRow(nextLine);
        if (cells.length < 2) break;
        index += 1;
        rows.push(Array.from({ length: headers.length }, (_, cellIndex) => (
          cellIndex === headers.length - 1
            ? cells.slice(cellIndex).join(" | ")
            : cells[cellIndex] || ""
        )));
      }
      const head = headers.map((cell) => `<th scope="col">${renderInlineMarkdown(cell)}</th>`).join("");
      const body = rows.map((row) => `<tr>${row.map((cell) => `<td>${renderInlineMarkdown(cell)}</td>`).join("")}</tr>`).join("");
      blocks.push(`<div class="chat-markdown-table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      flushQuote();
      continue;
    }

    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      flushQuote();
      const level = Math.min(heading[1].length, 3);
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2].trim())}</h${level}>`);
      continue;
    }
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushParagraph();
      flushList();
      flushQuote();
      blocks.push("<hr>");
      continue;
    }

    const quote = /^\s*>\s?(.*)$/.exec(line);
    if (quote) {
      flushParagraph();
      flushList();
      quoteLines.push(quote[1]);
      continue;
    }
    const listItem = /^\s*(?:([-+*])|(\d+)[.)])\s+(.*)$/.exec(line);
    if (listItem) {
      flushParagraph();
      flushQuote();
      const nextTag = listItem[2] ? "ol" : "ul";
      if (listTag && listTag !== nextTag) flushList();
      listTag = nextTag;
      listItems.push(listItem[3]);
      continue;
    }

    flushList();
    flushQuote();
    paragraph.push(line);
  }

  if (codeLines !== null) flushCode();
  flushParagraph();
  flushList();
  flushQuote();
  return blocks.join("");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload.detail === "string" ? payload.detail : `请求失败（${response.status}）`;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function showCompatibilityStatus(status) {
  const banner = document.querySelector("#compatibility-status");
  if (!banner) return;
  const required = ["series", "series_transfer", "story_continuity_ledger", "episode_outlines", "longform_novels", "shot_asset_links", "local_video_assembly", "local_subtitles", "assembly_presets", "local_visual_assets", "quality_checks", "story_quality_review", "scheduled_backups", "video_jobs", "content_revisions", "production_delivery", "production_reviews", "manual_storyboard_canvas"];
  const missing = required.filter((feature) => !status?.features?.includes(feature));
  const messages = [];
  const clientVersion = document.querySelector('meta[name="client-bundle-version"]')?.content || "未知";
  const diagnostics = document.querySelector("#system-diagnostics-details");
  if (diagnostics) {
    const startedAt = status?.server_started_at ? new Date(status.server_started_at).toLocaleString() : "未知";
    const rows = [
      ["页面资源", `v${clientVersion} · API ${EXPECTED_API_CONTRACT_VERSION}`],
      ["服务版本", status ? `${status.app_version || "未知"} · API ${status.api_contract_version ?? "未知"}` : "无法连接"],
      ["服务实例", status?.server_instance_id || "未知"],
      ["启动时间", startedAt],
      ["Python", status?.python_version || "未知"],
      ["视频任务线程", status ? (status.video_job_worker_running ? "运行中" : "未运行") : "未知"],
      ["功能标记", status ? `${(status.features || []).length} 项` : "未知"],
    ];
    diagnostics.innerHTML = rows.map(([label, value]) => `<dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd>`).join("");
  }
  if (!status) messages.push("无法读取本地服务版本信息。");
  else if (status.api_contract_version !== EXPECTED_API_CONTRACT_VERSION || missing.length) {
    messages.push(`前后端接口版本不兼容（服务端 ${status.api_contract_version ?? "未知"}，页面需要 ${EXPECTED_API_CONTRACT_VERSION}）。`);
  }
  if (missing.length) messages.push(`缺少功能：${missing.join("、")}。`);
  if (status && status.video_job_worker_running === false) messages.push("视频任务后台工作线程未运行，排队任务可能无法推进。");
  if (!messages.length) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.innerHTML = `${escapeHTML(messages.join(" "))} 请先重启本地服务，再刷新此页面；如确认服务已更新，可 <button class="compatibility-refresh" id="reload-page" type="button">重新加载页面</button>。本地单用户模式不会因此开放公网访问。`;
  banner.querySelector("#reload-page")?.addEventListener("click", () => window.location.reload());
}

function showNotice(message, isError = false) {
  notice.hidden = !message;
  notice.textContent = message || "";
  notice.classList.toggle("notice-error", isError);
}

function showChatStatus(message, isError = false) {
  const status = document.querySelector("#chat-status");
  if (!status) return;
  status.textContent = message || "";
  status.classList.toggle("is-error", isError);
}

function confirmInApp({ title, message, confirmLabel = "确认", cancelLabel = "取消" }) {
  if (!confirmDialog || confirmDialog.open) return Promise.resolve(false);
  const titleElement = document.querySelector("#confirm-title");
  const messageElement = document.querySelector("#confirm-message");
  const cancelButton = document.querySelector("#confirm-cancel");
  const acceptButton = document.querySelector("#confirm-accept");
  const trigger = document.activeElement;

  titleElement.textContent = title;
  messageElement.textContent = message;
  cancelButton.textContent = cancelLabel;
  acceptButton.textContent = confirmLabel;

  return new Promise((resolve) => {
    let accepted = false;
    const cleanup = () => {
      cancelButton.removeEventListener("click", cancel);
      acceptButton.removeEventListener("click", accept);
      confirmDialog.removeEventListener("close", close);
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus();
    };
    const cancel = () => confirmDialog.close();
    const accept = () => {
      accepted = true;
      confirmDialog.close();
    };
    const close = () => {
      cleanup();
      resolve(accepted);
    };

    cancelButton.addEventListener("click", cancel);
    acceptButton.addEventListener("click", accept);
    confirmDialog.addEventListener("close", close, { once: true });
    confirmDialog.showModal();
    cancelButton.focus();
  });
}

function stageLabel(stageId) {
  return state.config.stages.find((stage) => stage.id === stageId)?.label || stageId;
}

function modelName(modelId) {
  return state.config?.model_profiles?.find((item) => item.id === modelId)?.name || modelId;
}

function safeMediaUrl(value) {
  try {
    const parsed = new URL(value, window.location.origin);
    return ["http:", "https:"].includes(parsed.protocol) ? escapeHTML(parsed.href) : "";
  } catch {
    return "";
  }
}

function chatAttachmentKind(file) {
  const extension = String(file.name || "").split(".").pop().toLowerCase();
  if (file.type?.startsWith("image/") || ["jpg", "jpeg", "png", "webp", "gif"].includes(extension)) return "image";
  if (file.type?.startsWith("video/") || ["mp4", "m4v", "mov", "webm"].includes(extension)) return "video";
  return "file";
}

function formatFileSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function clearPendingUploads() {
  releaseUploadPreviews(state.pendingUploads);
  state.pendingUploads = [];
}

function renderPendingAttachments() {
  const tray = document.querySelector("#chat-pending-attachments");
  if (!tray) return;
  tray.hidden = state.pendingUploads.length === 0;
  tray.innerHTML = state.pendingUploads.map((item, index) => {
    const preview = item.kind === "image"
      ? `<img src="${escapeHTML(item.previewUrl)}" alt="${escapeHTML(item.name)}" />`
      : item.kind === "video"
        ? `<video src="${escapeHTML(item.previewUrl)}" muted preload="metadata" aria-label="${escapeHTML(item.name)}"></video>`
        : `<span class="chat-file-icon" aria-hidden="true">▤</span>`;
    return `<div class="chat-pending-file">${preview}<span><strong>${escapeHTML(item.name)}</strong><small>${formatFileSize(item.file.size)}</small></span>
      <button type="button" data-remove-pending="${index}" aria-label="移除附件：${escapeHTML(item.name)}">×</button></div>`;
  }).join("");
}

function addPendingUploads(fileList) {
  const files = [...fileList];
  const remaining = 8 - state.pendingUploads.length;
  let totalSize = state.pendingUploads.reduce((sum, item) => sum + item.file.size, 0);
  if (files.length > remaining) showChatStatus("一条消息最多添加 8 个附件。", true);
  for (const file of files.slice(0, Math.max(remaining, 0))) {
    if (file.size > 20 * 1024 * 1024) {
      showChatStatus(`${file.name} 超过 20 MB，未添加。`, true);
      continue;
    }
    if (totalSize + file.size > 40 * 1024 * 1024) {
      showChatStatus("一条消息的附件总大小不能超过 40 MB。", true);
      continue;
    }
    state.pendingUploads.push({
      file,
      name: file.name || "附件",
      kind: chatAttachmentKind(file),
      previewUrl: URL.createObjectURL(file),
    });
    totalSize += file.size;
  }
  renderPendingAttachments();
}

function renderProjects() {
  const recentProjects = state.projects.filter((project) => !project.archived);
  projectList.innerHTML = recentProjects.length
    ? recentProjects.map((project) => `
      <div class="project-entry">
        <button class="project-link ${state.project?.id === project.id ? "is-active" : ""}"
          type="button" data-project-id="${escapeHTML(project.id)}" ${state.project?.id === project.id ? 'aria-current="page"' : ""}>
          <span class="project-symbol" aria-hidden="true">${project.mode === "chat" ? "◌" : "▰"}</span>
          <span class="project-link-copy"><strong>${escapeHTML(project.title)}</strong><small>${project.mode === "chat" ? "普通对话" : escapeHTML(stageLabel(project.active_stage))}</small></span>
        </button>
        <span class="project-management">
          <button type="button" data-rename-project="${escapeHTML(project.id)}" aria-label="重命名：${escapeHTML(project.title)}" title="重命名">✎</button>
          <button type="button" data-archive-project="${escapeHTML(project.id)}" aria-label="从最近对话移除：${escapeHTML(project.title)}" title="从最近对话移除">×</button>
        </span>
      </div>`).join("")
    : '<p class="empty-projects">你的对话和创作流程会出现在这里。</p>';
  projectList.querySelectorAll("[data-project-id]").forEach((button) => {
    button.addEventListener("click", () => openProject(button.dataset.projectId));
  });
  projectList.querySelectorAll("[data-rename-project]").forEach((button) => {
    button.addEventListener("click", () => openRenameProject(button.dataset.renameProject));
  });
  projectList.querySelectorAll("[data-archive-project]").forEach((button) => {
    button.addEventListener("click", () => archiveProject(button.dataset.archiveProject));
  });
}

function syncModelControl() {
  const models = state.config?.model_profiles?.filter((item) => item.modality === "text") || [];
  modelSelect.innerHTML = models.map((model) =>
    `<option value="${escapeHTML(model.id)}">${escapeHTML(model.name)}${model.configured ? "" : "（未配置 Key）"}</option>`).join("");
  modelSelect.disabled = models.length === 0;
  modelSelect.value = state.project?.model || state.selectedModel || state.config?.default_model || "";
}

const TRACK_SETUP_COPY = {
  marketing: {
    business: "你的业务是什么？",
    product: "这次要介绍什么？",
    audience: "你希望触达谁？",
    platform: "发布平台",
    duration: "视频时长（秒）",
    style: "整体风格",
    note: "创建后进入品牌营销工作台；每一步可先审阅和确认，媒体生成只会在你明确触发时调用已配置的服务。",
    defaults: { platform: "抖音 / Reels / Shorts", style: "真实、清晰、有记忆点" },
    placeholders: {
      business: "例如：为小型咖啡馆提供会员运营工具",
      product: "产品、服务或活动名称",
      audience: "描述目标用户、他们关心的问题或使用场景",
      style: "真实、清晰、有记忆点",
    },
  },
  drama: {
    business: "故事背景 / 核心设定是什么？",
    product: "作品 / 项目名称",
    audience: "目标观众 / 题材偏好",
    platform: "发布平台 / 画幅方向",
    duration: "单集目标时长（秒）",
    style: "视觉风格 / 媒介",
    note: "创建后进入短剧 / 漫剧工作台；不会强加品牌卖点或 CTA，媒体生成仍须由你明确触发。",
    defaults: { platform: "竖屏短剧 / 漫剧平台", style: "真人短剧，克制写实" },
    placeholders: {
      business: "例如：被家族忽视的长女回到故乡，发现旧宅产权争议",
      product: "例如：旧宅回声 · EP001",
      audience: "例如：都市家庭悬疑 / 面向成年观众",
      style: "例如：真人短剧，克制写实；或二维漫剧，色彩鲜明",
    },
  },
};

function updateWorkflowTrackCopy(track) {
  const copy = TRACK_SETUP_COPY[track] || TRACK_SETUP_COPY.marketing;
  const seriesAssociation = document.querySelector("#series-association");
  if (seriesAssociation) {
    const dramaEnabled = track === "drama";
    seriesAssociation.hidden = !dramaEnabled;
    seriesAssociation.querySelectorAll("select, input").forEach((field) => { field.disabled = !dramaEnabled; });
  }
  for (const field of ["business", "product", "audience", "platform", "duration", "style"]) {
    const label = document.querySelector(`[data-track-label="${field}"]`);
    if (label) label.textContent = copy[field];
  }
  for (const [field, placeholder] of Object.entries(copy.placeholders)) {
    const input = document.querySelector(`[name="${field}"]`);
    if (input) input.placeholder = placeholder;
  }
  for (const [field, value] of Object.entries(copy.defaults)) {
    const input = document.querySelector(`[name="${field}"]`);
    if (input && !input.dataset.userEdited) input.value = value;
  }
  const durationInput = document.querySelector('[name="duration_seconds"]');
  if (durationInput && !durationInput.dataset.userEdited) {
    durationInput.value = track === "drama" ? "60" : "30";
  }
  const note = document.querySelector("#track-form-note");
  if (note) note.textContent = copy.note;
}

function renderWorkflowSetup() {
  state.project = null;
  state.viewStage = null;
  state.chatMessages = [];
  state.chatHasMore = false;
  state.chatHistoryCursor = null;
  state.chatLoadingOlder = false;
  state.viewMode = "workflow-setup";
  workspace.classList.remove("chat-workspace");
  document.querySelector("#top-title").textContent = "创作流程";
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = true;
  viewToggle.disabled = true;
  syncModelControl();
  const availableSeries = (state.series || []).filter((series) => !series.archived);
  const selectedSeriesId = availableSeries.some((series) => series.id === state.selectedSeriesId)
    ? state.selectedSeriesId
    : "";
  workspace.innerHTML = `
    <section class="welcome-panel">
      <div class="welcome-copy">
        <div class="overline"><span class="overline-mark" aria-hidden="true"></span>分阶段创作</div>
        <h1>先定义这次创作。</h1>
        <p>选择创作类型，补充故事设定或业务背景、目标观众和内容方向，再逐步生成脚本、视觉设定、分镜与逐镜任务。每一步都可以先审阅、编辑，再确认。</p>
      </div>
      <form id="create-form" class="brief-form">
        <label for="project-track"><span>创作类型</span>
          <select name="track" id="project-track">
            <option value="marketing">品牌营销视频</option>
            <option value="drama">短剧 / 漫剧</option>
          </select>
        </label>
        <div class="form-row">
          <label><span data-track-label="business">你的业务是什么？</span><input name="business" required maxlength="300" placeholder="例如：为小型咖啡馆提供会员运营工具" /></label>
          <label><span data-track-label="product">这次要介绍什么？</span><input name="product" required maxlength="300" placeholder="产品、服务或活动名称" /></label>
        </div>
        <label><span data-track-label="audience">你希望触达谁？</span><textarea name="audience" required maxlength="500" rows="2" placeholder="描述目标用户、他们关心的问题或使用场景"></textarea></label>
        <div class="form-row form-row-three">
          <label><span data-track-label="platform">发布平台</span><input name="platform" value="抖音 / Reels / Shorts" maxlength="100" /></label>
          <label><span data-track-label="duration">视频时长（秒）</span><input name="duration_seconds" type="number" min="6" max="180" value="30" required /></label>
          <label><span data-track-label="style">整体风格</span><input name="style" value="真实、清晰、有记忆点" maxlength="300" /></label>
        </div>
        <div class="form-row form-row-three assembly-setup-fields">
          <label for="create-output-aspect-ratio">最终合成画幅<select id="create-output-aspect-ratio" name="output_aspect_ratio">
            <option value="9:16" selected>9:16 · 竖屏</option><option value="16:9">16:9 · 横屏</option><option value="1:1">1:1 · 方形</option><option value="4:3">4:3</option><option value="3:4">3:4</option><option value="2:3">2:3</option><option value="3:2">3:2</option><option value="21:9">21:9 · 宽银幕</option>
          </select></label>
          <label for="create-output-frame-mode">素材适配<select id="create-output-frame-mode" name="output_frame_mode"><option value="pad" selected>补边适配 · 保留完整画面</option><option value="crop">裁切填满 · 可能裁掉边缘</option></select></label>
          <label for="create-output-preset">文件预设<select id="create-output-preset" name="output_preset"><option value="compact">紧凑 · 720p 上限</option><option value="standard" selected>标准 · 1080p 上限</option><option value="quality">高质量 · 1440p 上限</option></select></label>
        </div>
        <section id="series-association" class="series-association" aria-labelledby="series-association-title" hidden>
          <h2 id="series-association-title">加入故事系列</h2>
          <div class="form-row">
            <label for="series-select">所属系列<select id="series-select" name="series_id" aria-describedby="series-association-help">
              <option value="">独立分集，不关联系列</option>
              ${availableSeries.map((series) => `<option value="${escapeHTML(series.id)}" ${series.id === selectedSeriesId ? "selected" : ""}>${escapeHTML(series.title)}</option>`).join("")}
            </select></label>
            <label for="episode-number">集数（可选）<input id="episode-number" name="episode_number" type="number" min="1" max="9999" placeholder="自动分配" aria-describedby="series-association-help" /></label>
          </div>
          <p id="series-association-help" class="form-note">选择系列后，该系列的梗概、视觉基调和有效资产会作为后续生成参考。集数留空时自动使用下一个可用编号。</p>
        </section>
        <div class="form-footer">
          <span class="form-note" id="track-form-note" aria-live="polite">创建后进入品牌营销工作台；每一步可先审阅和确认，媒体生成只会在你明确触发时调用已配置的服务。</span>
          <button class="button button-primary" type="submit">创建创作流程 <span aria-hidden="true">↗</span></button>
        </div>
      </form>
    </section>
    <section class="workflow-intro" aria-label="分阶段创作">
      <p class="section-kicker">需要结构化产出时，可逐步确认</p>
      <div class="workflow-preview">${state.config.stages.map((stage, index) => `
        <div class="preview-step"><span class="preview-number">${String(index + 1).padStart(2, "0")}</span><span>${escapeHTML(stage.label)}</span></div>
      `).join('<span class="step-connector" aria-hidden="true"></span>')}</div>
      <p class="workflow-note">每一步都可以先审阅和修改，再确认进入下一步。</p>
    </section>`;

  const createForm = document.querySelector("#create-form");
  const trackSelect = document.querySelector("#project-track");
  const durationInput = createForm.querySelector('[name="duration_seconds"]');
  durationInput.addEventListener("input", () => { durationInput.dataset.userEdited = "true"; });
  for (const field of ["platform", "style"]) {
    const input = createForm.querySelector(`[name="${field}"]`);
    input.addEventListener("input", () => { input.dataset.userEdited = "true"; });
  }
  trackSelect.addEventListener("change", () => updateWorkflowTrackCopy(trackSelect.value));
  createForm.querySelector("#series-select").addEventListener("change", (event) => {
    state.selectedSeriesId = event.currentTarget.value || null;
  });
  updateWorkflowTrackCopy(trackSelect.value);
  createForm.addEventListener("submit", createProject);
}

function parseScenePlan(content) {
  try {
    const plan = JSON.parse(content);
    return plan && Array.isArray(plan.shots) ? plan : null;
  } catch {
    return null;
  }
}

function renderEditPlan(plan = {}) {
  const fields = [
    ["镜头顺序", Array.isArray(plan.sequence) ? plan.sequence.join(" → ") : ""],
    ["节奏", plan.pacing],
    ["转场", plan.transitions],
    ["音乐方向", plan.music_direction],
    ["声音设计", plan.sound_design],
    ["字幕方向", plan.subtitle_direction],
    ["制作备注", plan.notes],
  ].filter(([, value]) => String(value || "").trim());
  if (!fields.length) return "";
  return `<dl class="edit-plan-list">${fields.map(([label, value]) => `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd></div>`).join("")}</dl>`;
}

function renderScenePlanPreview(content) {
  const plan = parseScenePlan(content);
  if (!plan) return '<p class="scene-plan-invalid" role="status">目前不是有效的镜头任务 JSON。请切回编辑，修复格式后保存。</p>';
  const shots = plan.shots.map((shot, index) => `<article class="scene-plan-shot">
    <header><div><p class="section-kicker">${escapeHTML(shot.shot_id || `S${String(index + 1).padStart(2, "0")}`)} · ${escapeHTML(shot.duration_seconds || "—")} 秒 · ${escapeHTML(shot.aspect_ratio || "—")}</p><h3>${escapeHTML(shot.purpose || shot.visual_description || "镜头任务")}</h3></div></header>
    ${shot.visual_description ? `<p>${escapeHTML(shot.visual_description)}</p>` : ""}
    ${shot.voiceover ? `<p><strong>口播：</strong>${escapeHTML(shot.voiceover)}</p>` : ""}
    ${shot.screen_text ? `<p><strong>屏幕文字：</strong>${escapeHTML(shot.screen_text)}</p>` : ""}
    <details><summary>图片 / 视频提示词与连续性</summary><dl class="edit-plan-list">
      ${[["图片提示词", shot.image_prompt], ["视频提示词", shot.video_prompt], ["连续性", shot.continuity], ["避免项", shot.negative_prompt]].filter(([, value]) => String(value || "").trim()).map(([label, value]) => `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd></div>`).join("")}
    </dl></details>
  </article>`).join("");
  return `<div class="scene-plan-preview"><div class="scene-plan-heading"><strong>${plan.shots.length} 个镜头任务</strong><span>可在“编辑”中调整 JSON，再保存确认</span></div>${shots}${renderEditPlan(plan.edit_plan || {}) ? `<section class="edit-plan-card"><h3>AI 剪辑方案</h3>${renderEditPlan(plan.edit_plan || {})}</section>` : ""}</div>`;
}

function assetStatusLabel(asset = {}) {
  return ({
    pending: "待生成", queued: "后台排队中", submitting: "正在提交", generating: "生成中",
    ready: "已生成", failed: "生成失败", interrupted: "提交结果待核对", stale: "需重新生成",
    cancelled: "本地队列已取消", timed_out: "监控超时（可恢复查询）", stalled: "查询暂停（可恢复）",
  })[asset.status] || "待生成";
}

function renderAssetVersions(asset = {}) {
  const versions = Array.isArray(asset.versions) ? asset.versions : [];
  if (!versions.length) return "";
  return `<details class="asset-version-history"><summary>素材版本记录（${versions.length}）</summary><ol>${versions.map((version) => {
    const url = version.asset_url ? safeMediaUrl(version.asset_url) : "";
    return `<li><span>v${escapeHTML(version.version)}</span>${url ? `<a href="${url}" target="_blank" rel="noreferrer">打开素材</a>` : ""}<span>${escapeHTML(recordDate(version.completed_at))}</span><span>${version.cost == null ? "费用未知" : `供应商报告：${escapeHTML(version.cost)}`}</span>${version.task_id ? `<span>任务 ${escapeHTML(version.task_id)}</span>` : ""}</li>`;
  }).join("")}</ol></details>`;
}

function renderShotAssetAssignment(shot) {
  if (!state.project?.series_id) return "";
  const assets = Array.isArray(state.activeSeriesAssets) ? state.activeSeriesAssets : [];
  const selected = new Set(Array.isArray(shot.asset_ids) ? shot.asset_ids : []);
  return `<section class="shot-assignment" aria-label="系列资产关联">
    <label for="shot-assets-${escapeHTML(shot.shot_id)}">本镜头引用的系列资产<select id="shot-assets-${escapeHTML(shot.shot_id)}" data-shot-assets="${escapeHTML(shot.shot_id)}" multiple size="${Math.min(Math.max(assets.length, 2), 5)}" aria-describedby="shot-assets-help-${escapeHTML(shot.shot_id)}">
      ${assets.map((asset) => `<option value="${escapeHTML(asset.id)}" ${selected.has(asset.id) ? "selected" : ""}>${escapeHTML(asset.name)} · ${escapeHTML(seriesAssetKindLabel(asset.kind))}${asset.archived ? "（已归档，仅保留既有关联）" : ""}</option>`).join("")}
    </select></label>
    <small id="shot-assets-help-${escapeHTML(shot.shot_id)}">选中资产会作为该镜头的文字连续性参考；不会自动上传本地资产文件。</small>
    <button class="button button-subtle" type="button" data-save-shot-assets="${escapeHTML(shot.shot_id)}" ${assets.length ? "" : "disabled"}>保存镜头资产关联</button>
  </section>`;
}

const reviewDecisionLabels = { approved: "通过", revision_requested: "需修改", comment: "意见" };
const reviewCategoryLabels = { general: "综合", story: "故事", continuity: "连续性", visual: "画面", audio: "声音", editing: "剪辑", subtitle: "字幕", technical: "技术", other: "其他" };

function renderReviewPanel(project, targetType, targetId = "") {
  const key = targetType === "clip" ? `clip:${targetId}` : targetType;
  const target = project.review_targets?.[key];
  const records = (project.review_records || []).filter((item) => item.target_type === targetType && (item.target_id || "") === targetId);
  const titleId = `review-title-${targetType}-${targetId || "main"}`;
  const history = records.slice(0, 3).map((record) => `<li class="review-history-item ${record.is_current ? "is-current" : "is-historical"}">
    <div class="review-history-meta"><strong>${escapeHTML(reviewDecisionLabels[record.decision] || "意见")}</strong><span>${escapeHTML(reviewCategoryLabels[record.category] || record.category || "综合")}</span>${record.timecode ? `<span>时间码 ${escapeHTML(record.timecode)}</span>` : ""}<time>${escapeHTML(recordDate(record.created_at))}</time><span class="review-version-badge">${record.is_current ? "当前版本" : "旧版本"}</span></div>
    ${record.note ? `<p>${escapeHTML(record.note)}</p>` : ""}</li>`).join("");
  const disabled = !target?.available;
  return `<section class="review-panel" aria-labelledby="${escapeHTML(titleId)}">
    <div class="review-panel-heading"><div><p class="section-kicker">人工审阅 · 绑定内容版本</p><h3 id="${escapeHTML(titleId)}">审阅与修改意见</h3></div><span>${records.length ? `${records.length} 条记录` : "尚无审阅记录"}</span></div>
    ${disabled ? `<p class="review-unavailable" role="status">${escapeHTML(target?.reason || "当前版本尚不可审阅。")}</p>` : `<p class="review-version-note">提交后会绑定当前版本；内容或媒体更新后，旧意见仍保留并标记为历史记录。</p>`}
    ${history ? `<ol class="review-history">${history}</ol>` : records.length ? `<p class="record-output-empty">此目标暂无意见。</p>` : ""}
    ${records.length > 3 ? `<small class="review-more-note">此目标共 ${records.length} 条；完整历史见项目制作记录。</small>` : ""}
    <form class="review-form" data-review-form data-review-target-type="${escapeHTML(targetType)}" data-review-target-id="${escapeHTML(targetId)}">
      <input type="hidden" name="target_revision" value="${escapeHTML(target?.revision || "")}" />
      <div class="review-form-fields">
        <label>结论<select name="decision" aria-label="审阅结论" ${disabled ? "disabled" : ""}><option value="revision_requested">需修改</option><option value="approved">通过</option><option value="comment">仅记录意见</option></select></label>
        <label>问题类型<select name="category" aria-label="问题类型" ${disabled ? "disabled" : ""}>${Object.entries(reviewCategoryLabels).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select></label>
        ${targetType === "clip" || targetType === "final_video" ? `<label>时间码（可选）<input name="timecode" type="text" inputmode="numeric" maxlength="6" placeholder="00:12" aria-label="视频意见时间码，格式为分:秒" ${disabled ? "disabled" : ""} /></label>` : ""}
      </div>
      <label class="review-note-label">意见 / 修改要求<textarea name="note" rows="3" maxlength="2000" placeholder="描述具体问题、预期修改或通过依据" aria-label="审阅意见或修改要求" ${disabled ? "disabled" : ""}></textarea></label>
      <div class="review-form-footer"><small>“通过”可不填写意见；其他结论需要填写。</small><button class="button button-subtle" type="submit" ${disabled ? "disabled" : ""}>保存审阅记录</button></div>
    </form>
  </section>`;
}

function renderVideoShot(shot) {
  const image = shot.image || {};
  const video = shot.video || {};
  const imageUrl = image.asset_url ? safeMediaUrl(image.asset_url) : "";
  const videoUrl = video.asset_url ? safeMediaUrl(video.asset_url) : "";
  const imageBusy = image.status === "generating";
  const videoBusy = ["queued", "submitting", "generating"].includes(video.status);
  const videoNeedsRecovery = ["timed_out", "stalled"].includes(video.status);
  const videoCanGenerate = !videoNeedsRecovery;
  const imageReady = Boolean(imageUrl);
  const videoReady = Boolean(videoUrl);
  const imageConfigured = Boolean(state.config?.image_provider_ready);
  const videoConfigured = Boolean(state.config?.video_provider_ready || state.config?.media_provider_ready);
  const details = [shot.visual_description, shot.voiceover ? `口播：${shot.voiceover}` : "", shot.screen_text ? `屏幕文字：${shot.screen_text}` : ""].filter(Boolean).join(" · ");
  return `<article class="production-shot-card">
    <header class="production-shot-heading"><div><p class="section-kicker">${escapeHTML(shot.shot_id)} · ${escapeHTML(shot.duration_seconds)} 秒 · ${escapeHTML(shot.aspect_ratio)}</p><h3>${escapeHTML(shot.purpose || shot.visual_description || "镜头")}</h3></div>
      <span class="shot-status ${videoReady ? "is-ready" : ""}">${escapeHTML(assetStatusLabel(video))}</span></header>
    ${details ? `<p class="production-shot-description">${escapeHTML(details)}</p>` : ""}
    ${shot.stale ? `<p class="shot-stale-note" role="status">镜头任务已变化；过期素材仍保留供参考，需重新生成后再使用。</p>` : ""}
    ${shot.continuity ? `<p class="production-shot-continuity"><strong>连续性：</strong>${escapeHTML(shot.continuity)}</p>` : ""}
    ${renderShotAssetAssignment(shot)}
    <details class="production-shot-prompts"><summary>查看生成提示词</summary><dl class="edit-plan-list">
      <div><dt>图片提示词</dt><dd>${escapeHTML(shot.image_prompt)}</dd></div><div><dt>视频提示词</dt><dd>${escapeHTML(shot.video_prompt)}</dd></div>
      ${shot.negative_prompt ? `<div><dt>避免项</dt><dd>${escapeHTML(shot.negative_prompt)}</dd></div>` : ""}</dl></details>
    <div class="shot-assets-grid">
      <section class="shot-asset"><h4>关键帧 <span>${escapeHTML(assetStatusLabel(image))}</span></h4>
        ${imageUrl ? `<a class="shot-image-link" href="${imageUrl}" target="_blank" rel="noreferrer"><img src="${imageUrl}" alt="${escapeHTML(shot.shot_id)} 镜头关键帧" loading="lazy" /></a>` : `<div class="shot-asset-empty">${imageBusy ? "关键帧生成中…" : "尚未生成关键帧"}</div>`}
        ${image.error ? `<p class="shot-error" role="alert">${escapeHTML(image.error)}</p>` : ""}
        ${image.stale_reason ? `<small class="shot-help">${escapeHTML(image.stale_reason)}</small>` : ""}
        ${image.status !== "pending" ? `<small class="shot-help">${image.cost == null ? "费用信息未由供应商返回" : `供应商报告费用：${escapeHTML(image.cost)}`}</small>` : ""}
        <button class="button button-subtle" type="button" data-shot-action="image" data-shot-id="${escapeHTML(shot.shot_id)}" ${imageBusy || !imageConfigured ? "disabled" : ""}>${imageBusy ? "正在生成…" : imageReady ? "重新生成关键帧" : image.status === "failed" ? "重试关键帧" : "生成关键帧"}</button>
        ${renderAssetVersions(image)}
        ${!imageConfigured ? '<small class="shot-help">请先配置默认图像模型及 Key。</small>' : ""}
      </section>
      <section class="shot-asset"><h4>视频片段 <span>${escapeHTML(assetStatusLabel(video))}</span></h4>
        ${videoUrl ? `<video class="shot-video" controls preload="metadata" src="${videoUrl}" aria-label="${escapeHTML(shot.shot_id)} 镜头视频片段"></video>` : `<div class="shot-asset-empty">${videoBusy ? "视频片段生成中…" : "尚未生成视频片段"}</div>`}
        ${video.error ? `<p class="shot-error" role="alert">${escapeHTML(video.error)}</p>` : ""}
        ${video.stale_reason ? `<small class="shot-help">${escapeHTML(video.stale_reason)}</small>` : ""}
        ${video.status !== "pending" ? `<small class="shot-help">${video.cost == null ? "费用信息未由供应商返回" : `供应商报告费用：${escapeHTML(video.cost)}`}</small>` : ""}
        ${video.provider_status ? `<small class="shot-help">供应商状态：${escapeHTML(video.provider_status)}</small>` : ""}
        ${video.job_id ? `<small class="shot-help">本地任务：${escapeHTML(video.job_id)}</small>` : ""}
        ${video.task_id ? `<small class="shot-help">供应商任务：${escapeHTML(video.task_id)}</small>` : ""}
        ${renderAssetVersions(video)}
        <button class="button button-primary" type="button" data-shot-action="video" data-shot-id="${escapeHTML(shot.shot_id)}" ${videoBusy || !videoConfigured || !videoCanGenerate ? "disabled" : ""}>${videoBusy ? video.status === "queued" ? "后台排队中…" : video.status === "submitting" ? "正在提交…" : "视频生成中…" : video.status === "interrupted" ? "核对后重试" : videoReady ? "重新生成视频片段" : video.status === "failed" ? "重试视频片段" : "生成视频片段"}</button>
        ${video.status === "queued" ? `<button class="button button-subtle" type="button" data-shot-action="cancel-video" data-shot-id="${escapeHTML(shot.shot_id)}">取消本地排队</button>` : ""}
        ${videoNeedsRecovery ? `<button class="button button-subtle" type="button" data-shot-action="resume-video" data-shot-id="${escapeHTML(shot.shot_id)}">继续查询供应商状态</button><small class="shot-help">仅恢复状态查询，不会重新提交生成任务。供应商任务 ID：${escapeHTML(video.task_id || "未知")}</small>` : ""}
        ${["submitting", "generating"].includes(video.status) ? '<small class="shot-help">供应商可能已受理任务；当前不支持将此状态显示为“已取消”。</small>' : ""}
        ${!videoConfigured ? '<small class="shot-help">请先配置默认视频模型及 Key。</small>' : ""}
      </section>
    </div>
    ${renderReviewPanel(state.project || {}, "clip", shot.shot_id)}
  </article>`;
}

function renderKeyframeContactSheet(shots) {
  const frames = shots.flatMap((shot) => {
    const image = shot.image || {};
    if (shot.stale || image.stale_reason || image.status !== "ready" || !image.asset_url) return [];
    const url = safeMediaUrl(image.asset_url);
    return url ? [{ shot, url }] : [];
  });
  if (!frames.length) return "";
  return `<details class="keyframe-contact-sheet">
    <summary><strong>关键帧总览</strong><span>${frames.length} / ${shots.length} 张当前可用</span></summary>
    <p>仅展示当前有效关键帧；已过期素材不计入。可并排检查人物、道具与色调连续性，点击缩略图在新标签页查看原图。</p>
    <ol class="keyframe-contact-grid" aria-label="按镜头顺序排列的当前关键帧">${frames.map(({ shot, url }) => {
      const description = shot.purpose || shot.visual_description || "关键帧";
      return `<li><figure><a href="${url}" target="_blank" rel="noreferrer" aria-label="在新标签页打开 ${escapeHTML(shot.shot_id)} 镜头关键帧">
        <img src="${url}" alt="${escapeHTML(shot.shot_id)} · ${escapeHTML(description)}" loading="lazy" /></a>
        <figcaption><strong>${escapeHTML(shot.shot_id)}</strong><span>${escapeHTML(description)}</span></figcaption></figure></li>`;
    }).join("")}</ol>
  </details>`;
}

function renderLocalAssembly(project, shots, video) {
  const ids = shots.map((shot) => shot.shot_id);
  const validOrder = (order) => Array.isArray(order) && order.length === ids.length && new Set(order).size === ids.length && ids.every((id) => order.includes(id));
  if (state.renderOrderProjectId !== project.id || !validOrder(state.renderOrderDraft)) {
    state.renderOrderProjectId = project.id;
    const saved = video.render_order;
    const suggested = video.edit_plan?.sequence;
    state.renderOrderDraft = validOrder(saved) ? [...saved] : validOrder(suggested) ? [...suggested] : [...ids];
  }
  const order = state.renderOrderDraft;
  const byId = new Map(shots.map((shot) => [shot.shot_id, shot]));
  const needsSave = !validOrder(video.render_order) || order.some((id, index) => id !== video.render_order[index]);
  const clipsReady = shots.every((shot) => shot.video?.status === "ready" && !shot.video?.stale_reason && !shot.stale && shot.video?.asset_url);
  const busy = shots.some((shot) => ["queued", "submitting", "generating"].includes(shot.video?.status));
  const finalVideo = video.final_video || {};
  const finalUrl = finalVideo.asset_url ? safeMediaUrl(finalVideo.asset_url) : "";
  const brief = project.brief || {};
  const outputAspectRatio = brief.output_aspect_ratio || "9:16";
  const outputFrameMode = brief.output_frame_mode || "pad";
  const outputPreset = brief.output_preset || "standard";
  const scenePlan = parseScenePlan(project.workflow?.scenes?.content || "");
  const hasSubtitleText = Boolean(scenePlan?.shots?.some((shot) => shot.voiceover?.trim() || shot.screen_text?.trim()));
  const aspectOptions = [["9:16", "9:16 · 竖屏"], ["16:9", "16:9 · 横屏"], ["1:1", "1:1 · 方形"], ["4:3", "4:3"], ["3:4", "3:4"], ["2:3", "2:3"], ["3:2", "3:2"], ["21:9", "21:9 · 宽银幕"]];
  return `<section class="local-assembly-card" aria-labelledby="local-assembly-title">
    <div class="scene-plan-heading"><div><p class="section-kicker">本机后期</p><h3 id="local-assembly-title" tabindex="-1">最终 MP4 合成</h3></div><span>${clipsReady ? "素材齐备" : `${shots.filter((shot) => shot.video?.status === "ready").length} / ${shots.length} 段可用`}</span></div>
    <p>仅读取已保存的本地镜头视频；按项目画幅统一缩放并补边或裁切，使用选定的 MP4 预设后在本机合成。不会调用视频生成供应商，也不会覆盖原片段。</p>
    <div class="assembly-settings-grid">
      <label for="assembly-aspect-ratio">输出画幅<select id="assembly-aspect-ratio">${aspectOptions.map(([value, label]) => `<option value="${value}" ${outputAspectRatio === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      <label for="assembly-frame-mode">素材适配<select id="assembly-frame-mode"><option value="pad" ${outputFrameMode === "pad" ? "selected" : ""}>补边适配 · 不裁切</option><option value="crop" ${outputFrameMode === "crop" ? "selected" : ""}>裁切填满 · 可能裁边</option></select></label>
      <label for="assembly-preset">文件预设<select id="assembly-preset"><option value="compact" ${outputPreset === "compact" ? "selected" : ""}>紧凑 · 720p 上限</option><option value="standard" ${outputPreset === "standard" ? "selected" : ""}>标准 · 1080p 上限</option><option value="quality" ${outputPreset === "quality" ? "selected" : ""}>高质量 · 1440p 上限</option></select></label>
    </div>
    <div class="local-assembly-actions"><button class="button button-subtle" id="save-assembly-settings" type="button">保存合成设置</button></div>
    ${hasSubtitleText ? `<div class="subtitle-downloads"><span>字幕文件（旁白 + 屏幕文字）</span><a class="button button-subtle" href="/api/projects/${encodeURIComponent(project.id)}/subtitles.srt">下载 SRT</a><a class="button button-subtle" href="/api/projects/${encodeURIComponent(project.id)}/subtitles.ass">下载 ASS</a></div>` : '<p class="assembly-subtitle-note">已确认镜头任务没有旁白或屏幕文字；补充后可下载字幕，合成时会自动烧录。</p>'}
    <ol class="render-order-list">${order.map((shotId, index) => {
      const shot = byId.get(shotId) || {};
      return `<li><span class="render-order-number">${index + 1}</span><span class="render-order-name"><strong>${escapeHTML(shotId)}</strong><small>${escapeHTML(shot.purpose || shot.visual_description || "镜头")}</small></span>
        <span class="render-order-actions"><button class="button button-subtle" type="button" data-order-move="up" data-order-shot="${escapeHTML(shotId)}" aria-label="将 ${escapeHTML(shotId)} 上移" ${index === 0 ? "disabled" : ""}>↑</button>
        <button class="button button-subtle" type="button" data-order-move="down" data-order-shot="${escapeHTML(shotId)}" aria-label="将 ${escapeHTML(shotId)} 下移" ${index === order.length - 1 ? "disabled" : ""}>↓</button></span></li>`;
    }).join("")}</ol>
    <div class="local-assembly-actions"><button class="button button-subtle" id="save-render-order" type="button" ${needsSave ? "" : "disabled"}>保存镜头顺序</button>
      <button class="button button-primary" id="assemble-final-video" type="button" ${!clipsReady || busy || needsSave ? "disabled" : ""}>本机合成最终 MP4</button></div>
    ${finalVideo.status === "stale" ? `<p class="assembly-stale-note" role="status">${escapeHTML(finalVideo.stale_reason || "镜头素材或顺序已更改，请重新合成。")}</p>` : ""}
    ${finalUrl ? `<div class="video-preview"><video controls preload="metadata" src="${finalUrl}" aria-label="本地合成的最终视频"></video><p>${finalVideo.status === "ready" ? `本机合成 · ${escapeHTML(finalVideo.width || "")} × ${escapeHTML(finalVideo.height || "")} · ${escapeHTML(finalVideo.output_aspect_ratio || "")} · ${escapeHTML(finalVideo.preset || "标准")} · ${escapeHTML(finalVideo.duration_seconds || "")} 秒 · ${finalVideo.subtitles_burned ? `已烧录 ${escapeHTML(finalVideo.subtitle_cue_count || 0)} 条字幕 · ` : "未烧录字幕 · "}SHA-256 ${escapeHTML(finalVideo.sha256 || "")}` : "保留的旧版合成结果，仅供参考，当前镜头尚未重新合成。"}</p></div>` : ""}
    ${renderReviewPanel(project, "final_video")}
  </section>`;
}

function renderVideoStage(project) {
  const video = project.workflow?.video || {};
  const shots = Array.isArray(video.shots) ? video.shots : [];
  if (!shots.length) {
    const legacyUrl = video.asset_url ? safeMediaUrl(video.asset_url) : "";
    return legacyUrl
      ? `<div class="video-preview"><video controls preload="metadata" src="${legacyUrl}" aria-label="旧版单段视频"></video><p>这是旧版单段视频记录，不属于本机逐镜合成结果。</p></div>`
      : `<div class="provider-placeholder"><span class="placeholder-icon" aria-hidden="true">▧</span><div><strong>暂无逐镜任务</strong><p>先完成并确认“镜头任务”，再逐镜生成关键帧和视频片段。旧项目没有结构化镜头任务时，不会自动把整份分镜误当成单镜提示词。</p></div></div>`;
  }
  const completed = shots.filter((shot) => shot.video?.status === "ready" && shot.video?.asset_url).length;
  return `<div class="video-production"><section class="edit-plan-card"><div class="scene-plan-heading"><div><p class="section-kicker">AI 剪辑方案</p><h3>镜头片段顺序与声音方向</h3></div><span>${completed} / ${shots.length} 段已生成</span></div>
    ${renderEditPlan(video.edit_plan || {}) || '<p class="record-output-empty">暂无剪辑说明。</p>'}<p class="video-no-merge-note">可在下方调整最终顺序，并使用本机 FFmpeg 合成；供应商仍只负责生成逐镜片段。</p></section>
    ${renderKeyframeContactSheet(shots)}
    ${renderLocalAssembly(project, shots, video)}<div class="production-shot-list">${shots.map(renderVideoShot).join("")}</div></div>`;
}

async function saveShotAssetAssignments(shotId) {
  const select = workspace.querySelector(`[data-shot-assets="${CSS.escape(shotId)}"]`);
  if (!select || !state.project) return;
  const asset_ids = [...select.selectedOptions].map((option) => option.value);
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(state.project.id)}/shots/${encodeURIComponent(shotId)}/assets`, {
      method: "PUT", body: JSON.stringify({ asset_ids }),
    });
    showNotice("已保存镜头资产关联。后续生成会把所选系列资产作为文字连续性参考。", false);
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function saveRenderOrder() {
  if (!state.project) return;
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(state.project.id)}/render-order`, {
      method: "PUT", body: JSON.stringify({ shot_ids: state.renderOrderDraft }),
    });
    showNotice("已保存本地合成的镜头顺序。已有成片若使用旧顺序会标记为过期。", false);
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function saveAssemblySettings() {
  if (!state.project) return;
  const payload = {
    output_aspect_ratio: workspace.querySelector("#assembly-aspect-ratio")?.value || "9:16",
    output_frame_mode: workspace.querySelector("#assembly-frame-mode")?.value || "pad",
    output_preset: workspace.querySelector("#assembly-preset")?.value || "standard",
  };
  const button = workspace.querySelector("#save-assembly-settings");
  if (button) { button.disabled = true; button.textContent = "正在保存…"; }
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(state.project.id)}/assembly-settings`, {
      method: "PUT", body: JSON.stringify(payload),
    });
    showNotice("本地合成画幅、补边 / 裁切方式和文件预设已保存；已有成片如受影响会标记为过期。", false);
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
    if (button?.isConnected) { button.disabled = false; button.textContent = "保存合成设置"; }
  }
}

async function assembleFinalVideo() {
  if (!state.project) return;
  const confirmed = await confirmInApp({
    title: "在本机合成最终 MP4？",
    message: "仅使用项目中已保存的本地镜头片段。会在本机运行 FFmpeg、创建一个新文件，不覆盖镜头，也不会调用媒体供应商。",
    confirmLabel: "开始本机合成",
  });
  if (!confirmed) return;
  const projectId = state.project.id;
  const button = workspace.querySelector("#assemble-final-video");
  if (button) { button.disabled = true; button.textContent = "正在本机合成…"; }
  showNotice("正在本机合成视频；原始镜头片段不会修改。", false);
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(projectId)}/assemble`, { method: "POST" });
    await refreshProjects();
    showNotice("本机合成完成。最终 MP4 已保存为新的本地媒体文件，仍需人工审核。", false);
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    if (state.project?.id === projectId) renderProject();
  }
}

function statusLabel(item, index, activeIndex) {
  if (item.status === "approved") return "已确认";
  if (item.status === "ready") return "待确认";
  if (item.status === "stale") return "内容已过期";
  return index === activeIndex ? "待生成" : "未解锁";
}

function renderRevisionHistory(item) {
  const revisions = Array.isArray(item.revisions) ? item.revisions : [];
  if (!revisions.length) return "";
  return `<details class="stage-revision-history"><summary>历史草稿（${revisions.length}）</summary><ol>${revisions.map((revision, index) => `
    <li><span>${escapeHTML(recordDate(revision.archived_at))} · ${escapeHTML(revision.reason || "旧版")}</span>
      <div class="stage-footer-actions"><button class="button button-subtle" type="button" data-revision-index="${index}">载入编辑区</button>
        <button class="button button-subtle" type="button" data-revision-restore="${escapeHTML(revision.revision_id || "")}">恢复为当前版本</button></div></li>`).join("")}</ol></details>`;
}

function newCanvasCard(index) {
  const used = new Set((state.canvasDraft || []).map((card) => card.shot_id));
  let number = index + 1;
  while (used.has(`S${String(number).padStart(2, "0")}`)) number += 1;
  const makeId = globalThis.crypto?.randomUUID?.bind(globalThis.crypto);
  return {
    id: makeId ? makeId() : `shot-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
    shot_id: `S${String(number).padStart(2, "0")}`,
    title: `镜头 ${String(number).padStart(2, "0")}`,
    duration_seconds: 5,
    purpose: "",
    visual_description: "",
    camera: "",
    voiceover: "",
    screen_text: "",
    transition: "",
    continuity: "",
  };
}

function normalizeCanvasCards(cards) {
  if (!Array.isArray(cards)) return [];
  return cards.slice(0, 24).map((card, index) => ({
    ...newCanvasCard(index),
    ...(card && typeof card === "object" ? card : {}),
    id: typeof card?.id === "string" ? card.id : `shot-import-${index + 1}`,
    shot_id: typeof card?.shot_id === "string" ? card.shot_id : `S${String(index + 1).padStart(2, "0")}`,
    title: typeof card?.title === "string" ? card.title : `镜头 ${String(index + 1).padStart(2, "0")}`,
    duration_seconds: Number.isInteger(Number(card?.duration_seconds)) ? Number(card.duration_seconds) : 5,
    purpose: typeof card?.purpose === "string" ? card.purpose : "",
    visual_description: typeof card?.visual_description === "string" ? card.visual_description : "",
    camera: typeof card?.camera === "string" ? card.camera : "",
    voiceover: typeof card?.voiceover === "string" ? card.voiceover : "",
    screen_text: typeof card?.screen_text === "string" ? card.screen_text : "",
    transition: typeof card?.transition === "string" ? card.transition : "",
    continuity: typeof card?.continuity === "string" ? card.continuity : "",
  }));
}

function openStoryboardCanvas() {
  if (!state.project || state.project.mode !== "workflow") return;
  const board = state.project.storyboard_canvas || {};
  state.canvasProjectId = state.project.id;
  state.canvasDraft = normalizeCanvasCards(board.cards);
  state.canvasUpdatedAt = state.project.updated_at;
  state.canvasSavedSnapshot = JSON.stringify(state.canvasDraft);
  state.canvasRevision = 0;
  state.canvasSavedRevision = 0;
  state.canvasSaveError = "";
  state.canvasConflictProject = null;
  renderStoryboardCanvas();
}

function renderStoryboardCanvas() {
  const project = state.project;
  if (!project || project.id !== state.canvasProjectId) return;
  state.viewMode = "canvas";
  workspace.classList.remove("chat-workspace");
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = false;
  viewToggle.disabled = false;
  viewToggle.textContent = "返回创作流程";
  document.querySelector("#top-title").textContent = `镜头画布 · ${project.title}`;
  syncModelControl();

  const cards = state.canvasDraft || [];
  const totalDuration = cards.reduce((sum, card) => sum + (Number(card.duration_seconds) || 0), 0);
  const cardMarkup = cards.map((card, index) => {
    const id = `canvas-card-${index}`;
    const field = (name, label, { textarea = false, rows = 2, maxLength = 1200, required = false } = {}) => {
      const inputId = `${id}-${name}`;
      const common = `id="${inputId}" data-canvas-field="${name}" data-card-index="${index}" maxlength="${maxLength}" ${name === "shot_id" ? 'pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,39}"' : ""} ${required ? "required" : ""}`;
      return `<label class="canvas-field" for="${inputId}"><span>${label}</span>${textarea
        ? `<textarea ${common} rows="${rows}">${escapeHTML(card[name] ?? "")}</textarea>`
        : `<input ${common} type="text" value="${escapeHTML(card[name] ?? "")}" />`}</label>`;
    };
    return `<li class="canvas-card-item" data-canvas-card data-card-index="${index}">
      <article class="canvas-card">
        <header class="canvas-card-heading"><div><span class="canvas-shot-number">${String(index + 1).padStart(2, "0")}</span><strong>${escapeHTML(card.shot_id)}</strong></div>
          <div class="canvas-card-actions">
            <button class="button button-subtle canvas-icon-button" type="button" data-canvas-action="up" aria-label="将镜头 ${escapeHTML(card.shot_id)} 上移" ${index === 0 ? "disabled" : ""}>↑</button>
            <button class="button button-subtle canvas-icon-button" type="button" data-canvas-action="down" aria-label="将镜头 ${escapeHTML(card.shot_id)} 下移" ${index === cards.length - 1 ? "disabled" : ""}>↓</button>
            <button class="button button-subtle canvas-icon-button" type="button" data-canvas-action="remove" aria-label="删除镜头 ${escapeHTML(card.shot_id)}">×</button>
          </div>
        </header>
        <div class="canvas-card-fields">
          ${field("shot_id", "镜头编号", { maxLength: 40, required: true })}
          ${field("title", "镜头标题", { maxLength: 120 })}
          <label class="canvas-field" for="${id}-duration_seconds"><span>时长（秒，4–12）</span><input id="${id}-duration_seconds" data-canvas-field="duration_seconds" data-card-index="${index}" type="number" min="4" max="12" step="1" value="${escapeHTML(card.duration_seconds)}" required /></label>
          ${field("purpose", "叙事目的", { textarea: true, maxLength: 500 })}
          ${field("visual_description", "画面描述", { textarea: true, rows: 3, maxLength: 2000 })}
          ${field("camera", "镜头 / 运镜", { textarea: true, maxLength: 500 })}
          ${field("voiceover", "对白 / 旁白", { textarea: true, maxLength: 1200 })}
          ${field("screen_text", "屏幕文字", { maxLength: 500 })}
          ${field("transition", "转场", { maxLength: 300 })}
          ${field("continuity", "连续性备注", { textarea: true, maxLength: 1200 })}
        </div>
      </article>
    </li>`;
  }).join("");

  workspace.innerHTML = `<section class="storyboard-canvas-screen" aria-labelledby="canvas-title">
    <header class="canvas-page-heading"><div><p class="section-kicker">手工制作 · 本地保存 · 不调用模型</p><h1 id="canvas-title">镜头画布</h1>
      <p>直接拆镜、写画面和对白，再用上移 / 下移调整顺序。内容保存到当前项目，可导出 Markdown 交接；不请求文本、图片或视频生成服务。</p></div>
      <div class="canvas-page-actions"><button class="button button-subtle" type="button" data-open-production-canvas>制作总览</button>
        <button class="button button-subtle" id="export-canvas-markdown" type="button" ${cards.length ? "" : "disabled"}>下载分镜 Markdown</button>
        <button class="button button-primary" id="add-canvas-shot" type="button" ${cards.length >= 24 ? "disabled" : ""}>＋ 添加镜头</button></div>
    </header>
    <div class="canvas-summary" aria-label="画布统计"><span><strong id="canvas-shot-count">${cards.length}</strong> / 24 个镜头</span><span>预计总时长 <strong id="canvas-total-duration">${totalDuration}</strong> 秒</span><span>推荐片段长度 4–12 秒</span></div>
    <div class="canvas-save-row"><p id="canvas-save-status" role="status" aria-live="polite">${state.canvasSaveError ? escapeHTML(state.canvasSaveError) : state.canvasSavedRevision === state.canvasRevision ? "已保存到当前项目" : "有未保存的修改"}</p>
      <div class="canvas-save-actions"><button class="button button-subtle" id="canvas-retry-save" type="button" hidden>重试保存</button>
        <button class="button button-subtle" id="canvas-load-server" type="button" hidden>载入服务器版本</button>
        <button class="button button-subtle" id="canvas-overwrite-server" type="button" hidden>用本地版本覆盖</button>
        <button class="button button-subtle" id="canvas-save-now" type="button" ${state.canvasSavedRevision === state.canvasRevision ? "disabled" : ""}>立即保存</button></div>
    </div>
    ${cards.length ? `<ol class="storyboard-canvas-grid" aria-label="按剪辑顺序排列的镜头卡片">${cardMarkup}</ol>` : `<div class="canvas-empty"><span aria-hidden="true">＋</span><h2>从第一个镜头开始</h2><p>不需要先配置模型。添加一张卡片，手动写下画面、镜头运动、对白或屏幕文字。</p><button class="button button-primary" type="button" data-canvas-empty-add>添加第一个镜头</button></div>`}
    <p class="canvas-footnote">这是人工编排用的 2D 分镜板，不会自动生成媒体或替代人工审核。镜头数量最多 24 个；画面文字会按普通文本保存和导出。</p>
  </section>`;
  bindStoryboardCanvasEvents();
  updateCanvasSaveStatus();
}

function canvasIsDirty() {
  return state.canvasRevision !== state.canvasSavedRevision;
}

function updateCanvasSaveStatus() {
  const status = workspace.querySelector("#canvas-save-status");
  if (!status) return;
  const retry = workspace.querySelector("#canvas-retry-save");
  const loadServer = workspace.querySelector("#canvas-load-server");
  const overwrite = workspace.querySelector("#canvas-overwrite-server");
  const saveNow = workspace.querySelector("#canvas-save-now");
  const dirty = canvasIsDirty();
  if (state.canvasConflictProject) {
    status.textContent = "另一处已修改画布；当前本地编辑仍保留，但尚未覆盖服务器版本。请先载入服务器版本，或明确选择用本地版本覆盖。";
  } else if (state.canvasSaveError) {
    status.textContent = `保存失败：${state.canvasSaveError}`;
  } else if (state.canvasSaveInFlight) {
    status.textContent = "正在保存到本地项目…";
  } else if (dirty) {
    status.textContent = "有未保存的修改；正在自动保存。";
  } else {
    status.textContent = "已保存到当前项目。";
  }
  if (retry) retry.hidden = !state.canvasSaveError || Boolean(state.canvasConflictProject);
  if (loadServer) loadServer.hidden = !state.canvasConflictProject;
  if (overwrite) overwrite.hidden = !state.canvasConflictProject;
  if (saveNow) saveNow.disabled = !dirty || state.canvasSaveInFlight || Boolean(state.canvasConflictProject);
}

function scheduleCanvasSave(delay = 650) {
  if (state.canvasConflictProject) return;
  if (state.canvasSaveTimer) clearTimeout(state.canvasSaveTimer);
  state.canvasSaveTimer = setTimeout(() => {
    state.canvasSaveTimer = null;
    saveStoryboardCanvas();
  }, delay);
  updateCanvasSaveStatus();
}

function markCanvasDirty() {
  state.canvasRevision += 1;
  state.canvasSaveError = "";
  updateCanvasSaveStatus();
  scheduleCanvasSave();
}

async function saveStoryboardCanvas() {
  if (state.canvasSaveTimer) {
    clearTimeout(state.canvasSaveTimer);
    state.canvasSaveTimer = null;
  }
  if (!state.canvasProjectId || !state.project) return false;
  if (!canvasIsDirty()) return true;
  if (state.canvasConflictProject) return false;
  if (state.canvasSaveInFlight) {
    state.canvasSaveQueued = true;
    return false;
  }
  const invalidField = [...workspace.querySelectorAll("[data-canvas-field]")].find((field) => !field.checkValidity());
  if (invalidField) {
    state.canvasSaveError = "请先修正画布字段中的无效值。";
    updateCanvasSaveStatus();
    invalidField.reportValidity();
    invalidField.focus();
    return false;
  }

  const projectId = state.canvasProjectId;
  const expectedUpdatedAt = state.canvasUpdatedAt || state.project.updated_at;
  const revision = state.canvasRevision;
  const cards = JSON.parse(JSON.stringify(state.canvasDraft));
  state.canvasSaveInFlight = true;
  state.canvasSaveQueued = false;
  updateCanvasSaveStatus();
  try {
    const saved = await api(`/api/projects/${encodeURIComponent(projectId)}/storyboard-canvas`, {
      method: "PUT",
      body: JSON.stringify({ expected_updated_at: expectedUpdatedAt, cards }),
    });
    if (state.canvasProjectId === projectId) {
      state.canvasUpdatedAt = saved.updated_at;
      state.canvasSavedSnapshot = JSON.stringify(saved.storyboard_canvas?.cards || cards);
      state.canvasSavedRevision = revision;
      state.canvasSaveError = "";
      state.canvasConflictProject = null;
      if (state.project?.id === projectId) {
        state.project.updated_at = saved.updated_at;
        state.project.storyboard_canvas = saved.storyboard_canvas;
      }
    }
  } catch (error) {
    if (error.status === 409 && state.canvasProjectId === projectId) {
      try {
        const latest = await api(`/api/projects/${encodeURIComponent(projectId)}`);
        const latestCards = normalizeCanvasCards(latest.storyboard_canvas?.cards);
        if (JSON.stringify(latestCards) === state.canvasSavedSnapshot) {
          state.canvasUpdatedAt = latest.updated_at;
          if (state.project?.id === projectId) state.project.updated_at = latest.updated_at;
          state.canvasSaveError = "";
          state.canvasSaveQueued = true;
        } else {
          state.canvasConflictProject = latest;
          state.canvasSaveError = "";
        }
      } catch (refreshError) {
        state.canvasSaveError = refreshError.message || error.message;
      }
    } else if (state.canvasProjectId === projectId) {
      state.canvasSaveError = error.message;
    }
  } finally {
    state.canvasSaveInFlight = false;
    if (state.canvasProjectId === projectId) updateCanvasSaveStatus();
    const needsSave = state.canvasProjectId === projectId && canvasIsDirty() && !state.canvasConflictProject;
    const queued = state.canvasSaveQueued;
    state.canvasSaveQueued = false;
    if (needsSave && !state.canvasSaveError && (queued || state.canvasSavedRevision !== state.canvasRevision)) scheduleCanvasSave(queued ? 0 : 650);
  }
  return state.canvasProjectId !== projectId || !canvasIsDirty();
}

async function resolveCanvasConflict(action) {
  const latest = state.canvasConflictProject;
  if (!latest || state.canvasProjectId !== latest.id) return;
  if (action === "load") {
    const confirmed = await confirmInApp({
      title: "载入服务器画布并放弃本地修改？",
      message: "当前页面里未保存的镜头内容将被服务器版本替换。若要保留当前版本，请先下载 Markdown。",
      confirmLabel: "载入服务器版本",
    });
    if (!confirmed) return;
    state.project = latest;
    state.canvasDraft = normalizeCanvasCards(latest.storyboard_canvas?.cards);
    state.canvasUpdatedAt = latest.updated_at;
    state.canvasSavedSnapshot = JSON.stringify(state.canvasDraft);
    state.canvasRevision = 0;
    state.canvasSavedRevision = 0;
    state.canvasConflictProject = null;
    state.canvasSaveError = "";
    renderStoryboardCanvas();
    return;
  }
  const confirmed = await confirmInApp({
    title: "覆盖服务器上的分镜画布？",
    message: "服务器画布将被当前页面的本地编辑替换；镜头内容不会合并。",
    confirmLabel: "覆盖服务器画布",
  });
  if (!confirmed) return;
  state.canvasUpdatedAt = latest.updated_at;
  if (state.project?.id === latest.id) state.project.updated_at = latest.updated_at;
  state.canvasConflictProject = null;
  state.canvasSaveError = "";
  scheduleCanvasSave(0);
}

function escapeCanvasMarkdown(value) {
  const replacements = {
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\\": "\\\\", "`": "\\`", "*": "\\*",
    "_": "\\_", "{": "\\{", "}": "\\}", "[": "\\[", "]": "\\]", "(": "\\(", ")": "\\)",
    "#": "\\#", "+": "\\+", "-": "\\-", ".": "\\.", "!": "\\!", "|": "\\|", "~": "\\~",
  };
  return Array.from(String(value ?? "").replace(/\r\n?/g, "\n"), (character) => replacements[character] ?? character).join("");
}

function downloadStoryboardCanvas() {
  const cards = state.canvasDraft || [];
  if (!cards.length) return;
  const title = escapeCanvasMarkdown(state.project?.title || "未命名项目");
  const lines = [`# ${title} · 手工分镜`, "", `> ${cards.length} 个镜头 · 预计 ${cards.reduce((sum, card) => sum + Number(card.duration_seconds || 0), 0)} 秒 · 本地手工编排，未调用模型`, ""];
  const labels = [
    ["叙事目的", "purpose"], ["画面描述", "visual_description"], ["镜头 / 运镜", "camera"],
    ["对白 / 旁白", "voiceover"], ["屏幕文字", "screen_text"], ["转场", "transition"], ["连续性备注", "continuity"],
  ];
  cards.forEach((card, index) => {
    const shotId = escapeCanvasMarkdown(card.shot_id || `S${String(index + 1).padStart(2, "0")}`);
    const shotTitle = escapeCanvasMarkdown(card.title || "未命名镜头");
    lines.push(`## ${String(index + 1).padStart(2, "0")} · ${shotId} · ${shotTitle}`, "", `- 时长：${card.duration_seconds} 秒`);
    for (const [label, key] of labels) {
      const value = String(card[key] || "").trim();
      if (!value) continue;
      lines.push(`- ${label}：`, "");
      for (const line of escapeCanvasMarkdown(value).split("\n")) lines.push(line ? `> ${line}` : ">");
      lines.push("");
    }
    lines.push("");
  });
  const filename = (state.project?.title || "storyboard").replace(/[\\/:*?"<>|\s]+/g, "-").slice(0, 60) || "storyboard";
  const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${filename}-storyboard.md`;
  link.click();
  URL.revokeObjectURL(url);
}

function bindStoryboardCanvasEvents() {
  workspace.querySelectorAll("[data-canvas-field]").forEach((field) => {
    field.addEventListener("input", () => {
      const index = Number(field.dataset.cardIndex);
      const card = state.canvasDraft[index];
      if (!card) return;
      card[field.dataset.canvasField] = field.dataset.canvasField === "duration_seconds" ? Number(field.value) : field.value;
      const heading = field.closest("[data-canvas-card]")?.querySelector(".canvas-card-heading strong");
      if (heading && field.dataset.canvasField === "shot_id") heading.textContent = field.value;
      updateCanvasTotals();
      markCanvasDirty();
    });
  });
  workspace.querySelectorAll("[data-canvas-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const item = button.closest("[data-canvas-card]");
      const index = Number(item?.dataset.cardIndex);
      if (!Number.isInteger(index) || !state.canvasDraft[index]) return;
      const action = button.dataset.canvasAction;
      const card = state.canvasDraft[index];
      if (action === "remove") {
        const confirmed = await confirmInApp({
          title: `删除镜头 ${card.shot_id}？`,
          message: "此卡片会从分镜画布中移除；已下载的 Markdown 文件不会改变。",
          confirmLabel: "删除镜头",
        });
        if (!confirmed) return;
        state.canvasDraft.splice(index, 1);
      } else {
        const next = index + (action === "up" ? -1 : 1);
        if (next < 0 || next >= state.canvasDraft.length) return;
        [state.canvasDraft[index], state.canvasDraft[next]] = [state.canvasDraft[next], state.canvasDraft[index]];
      }
      markCanvasDirty();
      renderStoryboardCanvas();
      const focusIndex = Math.min(index, state.canvasDraft.length - 1);
      if (focusIndex >= 0) workspace.querySelector(`[data-canvas-card][data-card-index="${focusIndex}"] input[data-canvas-field="shot_id"]`)?.focus();
    });
  });
  const addCard = () => {
    if (state.canvasDraft.length >= 24) return;
    const card = newCanvasCard(state.canvasDraft.length);
    state.canvasDraft.push(card);
    markCanvasDirty();
    renderStoryboardCanvas();
    workspace.querySelector(`[data-canvas-card][data-card-index="${state.canvasDraft.length - 1}"] input[data-canvas-field="title"]`)?.focus();
  };
  workspace.querySelectorAll("#add-canvas-shot, [data-canvas-empty-add]").forEach((button) => button.addEventListener("click", addCard));
  workspace.querySelector("[data-open-production-canvas]")?.addEventListener("click", openProductionCanvas);
  workspace.querySelector("#export-canvas-markdown")?.addEventListener("click", downloadStoryboardCanvas);
  workspace.querySelector("#canvas-save-now")?.addEventListener("click", saveStoryboardCanvas);
  workspace.querySelector("#canvas-retry-save")?.addEventListener("click", saveStoryboardCanvas);
  workspace.querySelector("#canvas-load-server")?.addEventListener("click", () => resolveCanvasConflict("load"));
  workspace.querySelector("#canvas-overwrite-server")?.addEventListener("click", () => resolveCanvasConflict("overwrite"));
}

function updateCanvasTotals() {
  const count = workspace.querySelector("#canvas-shot-count");
  const duration = workspace.querySelector("#canvas-total-duration");
  if (count) count.textContent = String(state.canvasDraft.length);
  if (duration) duration.textContent = String(state.canvasDraft.reduce((sum, card) => sum + (Number(card.duration_seconds) || 0), 0));
  const add = workspace.querySelector("#add-canvas-shot");
  if (add) add.disabled = state.canvasDraft.length >= 24;
}

function productionCanvasStageClass(item, stageId, activeIndex) {
  if (item?.status === "approved") return "complete";
  if (item?.status === "ready") return "current";
  if (item?.status === "stale") return "stale";
  const index = state.config.stages.findIndex((stage) => stage.id === stageId);
  return index >= 0 && index <= activeIndex ? "current" : "waiting";
}

function productionCanvasStageButton(stageId, label, { disabled = false, focus = "", primary = false } = {}) {
  return `<button class="button ${primary ? "button-primary" : "button-subtle"}" type="button" data-production-stage="${escapeHTML(stageId)}" ${focus ? `data-production-focus="${escapeHTML(focus)}"` : ""} ${disabled ? "disabled" : ""}>${escapeHTML(label)}</button>`;
}

function productionCanvasStep({ number, title, status, summary, stateClass, actions }) {
  const validState = ["complete", "current", "stale", "waiting", "available"].includes(stateClass) ? stateClass : "waiting";
  return `<li class="production-flow-step is-${validState}"><article class="production-flow-node">
    <div class="production-flow-meta"><span class="production-flow-number">${String(number).padStart(2, "0")}</span><span class="production-flow-status">${escapeHTML(status)}</span></div>
    <h3>${escapeHTML(title)}</h3><p>${escapeHTML(summary)}</p><div class="production-flow-actions">${actions}</div>
  </article></li>`;
}

function renderProductionShotPreview(shot, index, manualCards, videoShots) {
  const shotId = String(shot.shot_id || `S${String(index + 1).padStart(2, "0")}`);
  const manual = manualCards.find((card) => card.shot_id === shotId) || {};
  const asset = videoShots.find((item) => item.shot_id === shotId) || shot;
  const title = manual.title || shot.purpose || `镜头 ${shotId}`;
  const description = manual.visual_description || shot.visual_description || manual.purpose || shot.purpose || "尚未填写画面描述。";
  const duration = Number(shot.duration_seconds || manual.duration_seconds || 0);
  const imageStatus = asset.image ? assetStatusLabel(asset.image) : "待生成";
  const videoStatus = asset.video ? assetStatusLabel(asset.video) : "待生成";
  return `<li class="production-shot-card"><div class="production-shot-frame" aria-hidden="true"><span>${escapeHTML(shotId)}</span><span>${duration ? `${escapeHTML(duration)} 秒` : "时长待定"}</span><i></i><b></b></div>
    <div class="production-shot-copy"><p class="section-kicker">镜头 ${String(index + 1).padStart(2, "0")}</p><h3>${escapeHTML(title)}</h3><p>${escapeHTML(description)}</p>
      <div class="production-shot-status"><span>关键帧 · ${escapeHTML(imageStatus)}</span><span>片段 · ${escapeHTML(videoStatus)}</span></div></div></li>`;
}

function renderProductionCanvas(project) {
  if (!project || project.mode !== "workflow") return;
  state.viewMode = "production-canvas";
  workspace.classList.remove("chat-workspace");
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = false;
  viewToggle.disabled = false;
  viewToggle.textContent = "返回创作流程";
  document.querySelector("#top-title").textContent = `2D 制作预演 · ${project.title}`;
  syncModelControl();

  const workflow = project.workflow || {};
  const brief = project.brief || {};
  const activeIndex = state.config.stages.findIndex((stage) => stage.id === project.active_stage);
  const unlocked = (stageId) => {
    const index = state.config.stages.findIndex((stage) => stage.id === stageId);
    return index >= 0 && index <= activeIndex;
  };
  const script = workflow.script || {};
  const prompt = workflow.prompt || {};
  const storyboard = workflow.storyboard || {};
  const scenes = workflow.scenes || {};
  const video = workflow.video || {};
  const videoShots = Array.isArray(video.shots) ? video.shots : [];
  const scenePlan = parseScenePlan(scenes.content || "");
  const sceneShots = scenePlan?.shots || [];
  const manualCards = Array.isArray(project.storyboard_canvas?.cards) ? project.storyboard_canvas.cards : [];
  const subtitleShotCount = sceneShots.filter((shot) => String(shot.voiceover || "").trim() || String(shot.screen_text || "").trim()).length;
  const imagesReady = videoShots.filter((shot) => shot.image?.status === "ready" && shot.image?.asset_url).length;
  const videosReady = videoShots.filter((shot) => shot.video?.status === "ready" && shot.video?.asset_url).length;
  const finalVideo = video.final_video || {};
  const finalReady = finalVideo.status === "ready" && Boolean(finalVideo.asset_url);
  const assetTarget = videoShots.length ? "video" : "scenes";
  const assetTargetLabel = videoShots.length ? "查看逐镜素材" : "准备镜头任务";
  const projectSummary = [brief.product, brief.platform, brief.duration_seconds ? `${brief.duration_seconds} 秒` : "", brief.style].filter(Boolean).join(" · ");
  const sceneSummary = `视觉设定：${productionStageStatus(project, "prompt")} · 分镜：${productionStageStatus(project, "storyboard")} · 镜头任务：${productionStageStatus(project, "scenes")}`;
  const assetSummary = videoShots.length
    ? `${videoShots.length} 个镜头 · 关键帧 ${imagesReady}/${videoShots.length} · 视频片段 ${videosReady}/${videoShots.length}`
    : sceneShots.length ? `${sceneShots.length} 个镜头任务已记录，尚未生成逐镜素材` : "确认镜头任务后，可逐镜准备关键帧和视频片段";
  const editStatus = finalReady ? "最终成片已就绪" : finalVideo.status === "stale" ? "最终成片需重新合成" : videosReady ? "素材可用于本机合成" : "等待逐镜视频片段";
  const editSummary = `${editStatus} · ${subtitleShotCount ? `${subtitleShotCount} 个镜头含口播或屏幕文字` : "当前镜头任务没有字幕文本"}`;
  const steps = [
    productionCanvasStep({ number: 1, title: "项目设定", status: "已建立", summary: projectSummary || project.title || "创作目标已保存", stateClass: "complete", actions: productionCanvasStageButton("script", "继续到脚本", { primary: true }) }),
    productionCanvasStep({ number: 2, title: "脚本", status: productionStageStatus(project, "script"), summary: script.content ? `当前稿件约 ${script.content.length} 字，可回到工作流继续编辑和审阅。` : "从项目目标开始起草，并在进入分镜前确认内容。", stateClass: productionCanvasStageClass(script, "script", activeIndex), actions: productionCanvasStageButton("script", script.content ? "查看 / 编辑脚本" : "打开脚本步骤", { disabled: !unlocked("script") }) }),
    productionCanvasStep({ number: 3, title: "分镜预演", status: productionStageStatus(project, "storyboard"), summary: `${sceneSummary} · 手工画布 ${manualCards.length} 镜`, stateClass: productionCanvasStageClass(storyboard, "storyboard", activeIndex), actions: `${productionCanvasStageButton("storyboard", "打开分镜步骤", { disabled: !unlocked("storyboard") })}<button class="button button-subtle" type="button" data-open-manual-storyboard>打开手工分镜</button>` }),
    productionCanvasStep({ number: 4, title: "逐镜素材", status: videoShots.length ? `${videosReady}/${videoShots.length} 段视频片段` : productionStageStatus(project, "scenes"), summary: assetSummary, stateClass: videosReady && videosReady === videoShots.length ? "complete" : videoShots.length || sceneShots.length ? "current" : productionCanvasStageClass(scenes, "scenes", activeIndex), actions: productionCanvasStageButton(assetTarget, assetTargetLabel, { disabled: !unlocked(assetTarget), primary: true }) }),
    productionCanvasStep({ number: 5, title: "剪辑 / 字幕", status: finalReady ? "本机合成已完成" : editStatus, summary: editSummary, stateClass: finalReady ? "complete" : finalVideo.status === "stale" ? "stale" : videosReady ? "current" : "waiting", actions: productionCanvasStageButton("video", "打开剪辑与字幕", { disabled: !unlocked("video"), focus: "assembly" }) }),
    productionCanvasStep({ number: 6, title: "交付", status: finalReady ? "成片可交付" : "可打包当前资料", summary: "导出本地制作交付包，包含项目资料、审阅记录与可用媒体；下载由你主动发起。", stateClass: finalReady ? "complete" : "available", actions: `<a class="button button-primary" href="/api/projects/${encodeURIComponent(project.id)}/delivery">下载制作交付包</a>` }),
  ].join("");
  const previewShots = videoShots.length ? videoShots : sceneShots.length ? sceneShots : manualCards;
  const shotSource = videoShots.length ? "当前逐镜素材任务" : sceneShots.length ? "已确认镜头任务" : manualCards.length ? "手工分镜卡片" : "等待分镜内容";
  const shotMarkup = previewShots.map((shot, index) => renderProductionShotPreview(shot, index, manualCards, videoShots)).join("");
  const previewShotIds = new Set(previewShots.map((shot) => shot.shot_id));
  const unlinkedManualCards = manualCards.filter((card) => !previewShotIds.has(card.shot_id));
  const unlinkedManualMarkup = unlinkedManualCards.map((card, index) => renderProductionShotPreview(card, index, manualCards, videoShots)).join("");

  workspace.innerHTML = `<section class="production-canvas-screen" aria-labelledby="production-canvas-title">
    <div class="production-canvas-toolbar"><button class="record-back-button" type="button" data-production-back><span aria-hidden="true">←</span> 返回创作流程</button><a class="button button-subtle" href="/api/projects/${encodeURIComponent(project.id)}/delivery">下载制作交付包</a></div>
    <header class="production-canvas-heading"><p class="section-kicker">2D 预演画布 · 项目本地数据</p><h1 id="production-canvas-title" tabindex="-1">从项目设定到交付</h1>
      <p>${escapeHTML(project.title)} 的制作状态总览。节点数据直接读取现有工作流；修改在原阶段保存，离开画布后可继续。这里不调用文本、图像或视频生成服务。</p></header>
    <section class="production-flow-panel" aria-labelledby="production-flow-title"><div class="production-panel-heading"><div><p class="section-kicker">全流程</p><h2 id="production-flow-title">制作路径</h2></div><span>按编号沿路径预演</span></div>
      <ol class="production-flow-map" aria-label="项目设定、脚本、分镜、逐镜素材、剪辑字幕与交付">${steps}</ol>
    </section>
    <section class="production-shot-panel" aria-labelledby="production-shot-title"><div class="production-panel-heading"><div><p class="section-kicker">镜头顺序 · ${escapeHTML(shotSource)}</p><h2 id="production-shot-title">2D 镜头预演带</h2></div>
      <button class="button button-subtle" type="button" data-open-manual-storyboard>打开手工分镜</button></div>
      ${shotMarkup ? `<ol class="production-shot-track" aria-label="按顺序排列的镜头与逐镜素材">${shotMarkup}</ol>` : `<div class="production-shot-empty"><span aria-hidden="true">▧</span><div><strong>镜头还没有进入预演带</strong><p>可先手工添加分镜卡片，或完成工作流中的镜头任务。手工分镜只保存在本项目，不会触发生成。</p></div><button class="button button-primary" type="button" data-open-manual-storyboard>添加手工分镜</button></div>`}
      ${unlinkedManualMarkup ? `<div class="production-manual-shot-panel"><h3>独立手工分镜 · 不自动同步</h3><p>这些卡片未与工作流逐镜任务按镜头编号关联；可在手工分镜画布继续编辑。</p><ol class="production-shot-track" aria-label="未关联到工作流的手工分镜">${unlinkedManualMarkup}</ol></div>` : ""}
    </section>
    <p class="production-canvas-footnote">状态是工作流数据的可视化摘要，不代表质量验收。手工分镜、逐镜素材、剪辑和交付仍按原流程分别保存；未生成的素材不会被自动补齐。</p>
  </section>`;
  workspace.querySelector("[data-production-back]")?.addEventListener("click", () => {
    state.viewStage = state.viewStage || project.active_stage;
    renderProject();
    workspace.querySelector("#stage-title")?.focus();
  });
  workspace.querySelectorAll("[data-production-stage]").forEach((button) => {
    button.addEventListener("click", () => {
      const stageId = button.dataset.productionStage;
      const stageIndex = state.config.stages.findIndex((stage) => stage.id === stageId);
      if (stageIndex < 0 || stageIndex > activeIndex) return;
      state.viewStage = stageId;
      renderProject();
      const focus = button.dataset.productionFocus;
      if (focus === "assembly") {
        const heading = workspace.querySelector("#local-assembly-title");
        heading?.focus();
        heading?.scrollIntoView({ behavior: "smooth", block: "start" });
      } else {
        workspace.querySelector("#stage-title")?.focus();
      }
    });
  });
  workspace.querySelectorAll("[data-open-manual-storyboard]").forEach((button) => button.addEventListener("click", openStoryboardCanvas));
  workspace.querySelector("#production-canvas-title")?.focus({ preventScroll: true });
}

async function openProductionCanvas() {
  if (!state.project || state.project.mode !== "workflow") return;
  if (!(await ensureCanvasSavedBeforeNavigation())) return;
  const editor = state.viewMode === "workflow" ? workspace.querySelector("#stage-content") : null;
  const currentContent = state.project.workflow?.[state.viewStage]?.content || "";
  if (editor && editor.value !== currentContent) {
    const discard = await confirmInApp({
      title: "放弃未保存的修改？",
      message: "进入制作预演画布前，当前阶段的未保存编辑将丢失。",
      confirmLabel: "放弃修改并继续",
    });
    if (!discard) return;
  }
  renderProductionCanvas(state.project);
}

function renderProject() {
  if (!state.project) return renderChatScreen();
  if (state.project.mode === "workflow" && !state.project.review_targets) hydrateReviewData();
  state.viewMode = "workflow";
  workspace.classList.remove("chat-workspace");
  const project = state.project;
  const activeIndex = state.config.stages.findIndex((stage) => stage.id === project.active_stage);
  const viewStage = state.viewStage || project.active_stage;
  const viewIndex = state.config.stages.findIndex((stage) => stage.id === viewStage);
  const item = project.workflow[viewStage];
  const linkedSeries = (state.series || []).find((series) => series.id === project.series_id);
  const isCurrent = viewStage === project.active_stage;
  const stage = state.config.stages[viewIndex];
  document.querySelector("#top-title").textContent = project.title;
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = false;
  viewToggle.disabled = false;
  viewToggle.textContent = "返回对话";
  syncModelControl();

  const stepButtons = state.config.stages.map((step, index) => {
    const status = project.workflow[step.id];
    const locked = index > activeIndex;
    return `<button class="stage-tab ${viewStage === step.id ? "is-current" : ""} ${status.status === "approved" ? "is-done" : ""} ${status.status === "stale" ? "is-stale" : ""}"
      type="button" data-stage="${escapeHTML(step.id)}" aria-current="${viewStage === step.id ? "step" : "false"}" ${locked ? "disabled" : ""}>
      <span class="stage-index">${status.status === "approved" ? "✓" : status.status === "stale" ? "!" : String(index + 1).padStart(2, "0")}</span>
      <span class="stage-tab-text"><strong>${escapeHTML(step.label)}</strong><small>${escapeHTML(statusLabel(status, index, activeIndex))}</small></span>
    </button>`;
  }).join('<span class="stage-connector" aria-hidden="true"></span>');

  const isVideo = viewStage === "video";
  const editorContent = item.content || "";
  const readOnlyContent = viewStage === "scenes"
    ? `<div class="approved-content">${renderScenePlanPreview(editorContent)}</div>`
    : `<div class="approved-content chat-markdown stage-markdown">${renderMarkdown(editorContent)}</div>`;
  let contentArea;
  if (isVideo) {
    contentArea = renderVideoStage(project);
  } else if (item.status === "approved") {
    contentArea = readOnlyContent;
  } else if ((item.status === "ready" || item.status === "stale") && isCurrent) {
    contentArea = `${item.status === "stale" ? `<p class="stage-stale-note" role="status">${escapeHTML(item.stale_reason || "上游内容已修改。")}</p>` : ""}
      ${item.validation_error ? `<p id="stage-validation-note" class="stage-stale-note" role="alert">镜头任务未通过 JSON 校验；原始输出已保留在编辑框中。请修正并保存后再确认。${escapeHTML(item.validation_error)}</p>` : ""}
      <div class="content-toolbar">
        <label class="content-label" for="stage-content">${viewStage === "scenes" ? "逐镜任务 JSON" : "审阅草稿"}</label>
        <div class="content-view-switch" role="group" aria-label="草稿显示方式">
          <button class="content-view-button is-active" id="edit-stage-view" type="button" aria-pressed="true">编辑</button>
          <button class="content-view-button" id="preview-stage-view" type="button" aria-pressed="false">预览</button>
        </div>
      </div>
      <textarea id="stage-content" class="content-editor" rows="18">${escapeHTML(editorContent)}</textarea>
      <div id="stage-preview" class="content-preview chat-markdown stage-markdown" hidden></div>
      <div class="editor-actions"><button class="button button-subtle" id="save-content" type="button">保存修改</button><span>保存后再确认；重新生成会覆盖当前草稿。</span></div>
      ${renderRevisionHistory(item)}`;
  } else if (item.status === "stale" && editorContent) {
    contentArea = `<div class="stage-stale-preview"><p class="stage-stale-note" role="status">${escapeHTML(item.stale_reason || "此内容依赖上游步骤，可能已过期。")}</p>${readOnlyContent}</div>`;
  } else {
    contentArea = `<div class="empty-stage"><span class="empty-stage-mark" aria-hidden="true">${String(viewIndex + 1).padStart(2, "0")}</span>
      <p>生成${escapeHTML(stage.label)}草稿后，会显示在这里。</p></div>`;
  }

  const videoShots = Array.isArray(item.shots) ? item.shots : [];
  const generatedShotVideos = videoShots.filter((shot) => shot.video?.status === "ready").length;
  const legacyDurationSupported = Number(project.brief?.duration_seconds) >= 4 && Number(project.brief?.duration_seconds) <= 12;
  const action = isVideo
    ? videoShots.length
      ? `<span class="approved-badge">${generatedShotVideos === videoShots.length ? "✓ 所有镜头片段已生成（未合并）" : `${generatedShotVideos} / ${videoShots.length} 段已生成`}</span>`
      : item.asset_url
        ? `<span class="approved-badge">✓ 旧版单段视频已生成</span>`
        : state.config.media_provider_ready && legacyDurationSupported
          ? `<button class="button button-primary" id="generate-stage" type="button">生成单段旧版视频</button>`
          : `<span class="footer-hint">${legacyDurationSupported ? "请配置视频模型后生成。" : "旧项目没有逐镜任务；单段视频仅支持 4–12 秒。"}</span>`
    : ((item.status === "approved" || item.status === "stale") && !isCurrent)
      ? `<button class="button button-primary" id="rollback-stage" type="button">回退到此步骤并修改</button>`
      : item.status === "approved"
        ? `<span class="approved-badge">✓ 已确认</span>`
        : item.status === "ready"
          ? `<div class="stage-footer-actions"><button class="button button-subtle" id="regenerate-stage" type="button">重新生成草稿</button><button class="button button-primary" id="approve-stage" type="button" ${item.validation_error ? 'disabled aria-describedby="stage-validation-note"' : ""}>确认${escapeHTML(stage.label)}，继续</button></div>`
          : item.status === "stale" && isCurrent
            ? `<button class="button button-primary" id="regenerate-stage" type="button">重新生成草稿</button>`
            : isCurrent
              ? `<button class="button button-primary" id="generate-stage" type="button">生成${escapeHTML(stage.label)}草稿 <span aria-hidden="true">↗</span></button>`
              : "";

  workspace.innerHTML = `
    <div class="project-heading">
      <div><p class="section-kicker">${escapeHTML(project.track === "drama" ? "短剧 / 漫剧" : "品牌营销视频")} · ${escapeHTML(project.brief.platform)} · ${escapeHTML(project.brief.duration_seconds)} 秒</p>
        <h1>${escapeHTML(project.title)}</h1>
        ${project.series_id ? `<p class="project-series-note">EP${String(project.episode_number || 0).padStart(2, "0")} · ${escapeHTML(linkedSeries?.title || "所属故事系列")}</p>` : ""}
        <p class="project-description">${escapeHTML(project.brief.business)} · 面向 ${escapeHTML(project.brief.audience)}</p></div>
      <div class="project-heading-actions"><div class="project-state"><span class="state-dot" aria-hidden="true"></span>当前步骤：${escapeHTML(stageLabel(project.active_stage))}</div>
        <button class="button button-subtle" type="button" data-open-production-canvas>2D 制作预演</button>
        <button class="button button-subtle" type="button" data-open-storyboard-canvas>打开手工分镜画布</button></div>
    </div>
    <nav class="stage-rail" aria-label="创作步骤">${stepButtons}</nav>
    <section class="stage-panel" aria-labelledby="stage-title">
      <div class="stage-panel-heading"><div><p class="section-kicker">${escapeHTML(statusLabel(item, viewIndex, activeIndex))}</p>
        <h2 id="stage-title" tabindex="-1">${escapeHTML(stage.label)}</h2><p>${escapeHTML(stage.purpose)}</p></div><span class="stage-time">${escapeHTML(project.brief.duration_seconds)} 秒${project.track === "drama" ? "单集目标" : "目标"}时长</span></div>
      ${viewStage === "script" ? `<div class="brief-strip"><strong>${project.track === "drama" ? "作品 / 项目名" : "产品 / 服务"}</strong><span>${escapeHTML(project.brief.product)}</span><strong>${project.track === "drama" ? "媒介 / 风格" : "风格"}</strong><span>${escapeHTML(project.brief.style)}</span></div>` : ""}
      <div class="stage-content">${contentArea}</div>
      ${viewStage === "script" || viewStage === "storyboard" ? renderReviewPanel(project, viewStage) : ""}
      ${editorContent && !isVideo ? '<div class="quality-check-bar"><button class="button button-subtle" id="run-quality-check" type="button">运行质量检查</button><div id="quality-results" class="quality-results" role="status" aria-live="polite"></div></div>' : ""}
      <div class="stage-footer"><span class="footer-hint">${isVideo ? "镜头片段由供应商单独生成（可能产生费用）；最终 MP4 可在本机合成，不会调用媒体供应商。" : "已确认步骤可回退修改；后续内容会保留并标记为过期，重新审阅后再继续。"}</span>${action}</div>
    </section>
    <p class="workspace-footnote">文本模型：${escapeHTML(modelName(project.model))} <span aria-hidden="true">·</span> 本地技能：${state.skillCount ?? 0} 个</p>`;

  workspace.querySelectorAll("[data-review-form]").forEach((form) => {
    form.addEventListener("submit", submitProjectReview);
  });
  workspace.querySelectorAll("[data-stage]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (stageEditor && stageEditor.value !== (item.content || "")) {
        const discard = await confirmInApp({
          title: "放弃未保存的修改？",
          message: "离开当前步骤后，尚未保存的编辑将丢失。",
          confirmLabel: "放弃修改并离开",
        });
        if (!discard) return;
      }
      state.viewStage = button.dataset.stage;
      renderProject();
    });
  });
  const stageEditor = workspace.querySelector("#stage-content");
  const stagePreview = workspace.querySelector("#stage-preview");
  workspace.querySelectorAll("[data-revision-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const revision = item.revisions?.[Number(button.dataset.revisionIndex)];
      if (!revision || !stageEditor) return;
      stageEditor.value = revision.content;
      stageEditor.focus();
      showNotice("旧版草稿已载入编辑区；保存后才会替换当前内容。", false);
    });
  });
  workspace.querySelectorAll("[data-revision-restore]").forEach((button) => {
    button.addEventListener("click", () => restoreStageRevision(viewStage, button.dataset.revisionRestore));
  });
  workspace.querySelector("[data-open-storyboard-canvas]")?.addEventListener("click", openStoryboardCanvas);
  workspace.querySelector("[data-open-production-canvas]")?.addEventListener("click", openProductionCanvas);
  workspace.querySelector("#run-quality-check")?.addEventListener("click", runStageQualityCheck);
  workspace.querySelector("#preview-stage-view")?.addEventListener("click", (event) => {
    stagePreview.innerHTML = viewStage === "scenes" ? renderScenePlanPreview(stageEditor.value) : renderMarkdown(stageEditor.value);
    stageEditor.hidden = true;
    stagePreview.hidden = false;
    event.currentTarget.classList.add("is-active");
    event.currentTarget.setAttribute("aria-pressed", "true");
    const editButton = workspace.querySelector("#edit-stage-view");
    editButton.classList.remove("is-active");
    editButton.setAttribute("aria-pressed", "false");
  });
  workspace.querySelector("#edit-stage-view")?.addEventListener("click", (event) => {
    stagePreview.hidden = true;
    stageEditor.hidden = false;
    event.currentTarget.classList.add("is-active");
    event.currentTarget.setAttribute("aria-pressed", "true");
    const previewButton = workspace.querySelector("#preview-stage-view");
    previewButton.classList.remove("is-active");
    previewButton.setAttribute("aria-pressed", "false");
  });
  document.querySelector("#generate-stage")?.addEventListener("click", requestGenerateStage);
  document.querySelector("#regenerate-stage")?.addEventListener("click", requestGenerateStage);
  document.querySelector("#rollback-stage")?.addEventListener("click", rollbackStage);
  workspace.querySelectorAll("[data-shot-action]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.shotAction === "cancel-video") {
        manageVideoJob(button.dataset.shotId, "cancel");
      } else if (button.dataset.shotAction === "resume-video") {
        manageVideoJob(button.dataset.shotId, "resume");
      } else {
        generateShotAsset(button.dataset.shotId, button.dataset.shotAction);
      }
    });
  });
  workspace.querySelectorAll("[data-save-shot-assets]").forEach((button) => {
    button.addEventListener("click", () => saveShotAssetAssignments(button.dataset.saveShotAssets));
  });
  workspace.querySelectorAll("[data-order-move]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = state.renderOrderDraft.indexOf(button.dataset.orderShot);
      const target = index + (button.dataset.orderMove === "up" ? -1 : 1);
      if (index < 0 || target < 0 || target >= state.renderOrderDraft.length) return;
      [state.renderOrderDraft[index], state.renderOrderDraft[target]] = [state.renderOrderDraft[target], state.renderOrderDraft[index]];
      renderProject();
    });
  });
  workspace.querySelector("#save-render-order")?.addEventListener("click", saveRenderOrder);
  workspace.querySelector("#save-assembly-settings")?.addEventListener("click", saveAssemblySettings);
  workspace.querySelector("#assemble-final-video")?.addEventListener("click", assembleFinalVideo);
  document.querySelector("#approve-stage")?.addEventListener("click", approveStage);
  document.querySelector("#save-content")?.addEventListener("click", saveContent);
  scheduleVideoJobRefresh();
}

function hasActiveVideoJobs(project = state.project) {
  return (project?.workflow?.video?.shots || []).some((shot) =>
    ["queued", "submitting", "generating"].includes(shot.video?.status));
}

function scheduleVideoJobRefresh() {
  if (!hasActiveVideoJobs() || videoJobPollTimer || videoJobPollInFlight) return;
  const projectId = state.project.id;
  videoJobPollTimer = setTimeout(async () => {
    videoJobPollTimer = null;
    if (state.project?.id !== projectId || state.viewMode !== "workflow") {
      if (state.viewMode === "workflow") scheduleVideoJobRefresh();
      return;
    }
    videoJobPollInFlight = true;
    try {
      const refreshedProject = await api(`/api/projects/${encodeURIComponent(projectId)}`);
      if (state.viewMode === "workflow" && state.project?.id === projectId) {
        state.project = refreshedProject;
        renderProject();
      }
    } catch (error) {
      if (state.project?.id === projectId) showNotice(error.message, true);
    } finally {
      videoJobPollInFlight = false;
      if (state.viewMode === "workflow") scheduleVideoJobRefresh();
    }
  }, 2500);
}

function recordDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function videoShotStats(project) {
  const video = project.workflow?.video || {};
  const shots = Array.isArray(video.shots) ? video.shots : [];
  const ready = shots.filter((shot) => shot.video?.status === "ready" && shot.video?.asset_url).length;
  return { shots, ready, complete: Boolean(video.asset_url) || (shots.length > 0 && ready === shots.length), hasAssets: Boolean(video.asset_url) || shots.some((shot) => shot.video?.asset_url || shot.image?.asset_url) };
}

function productionStageStatus(project, stageId) {
  const item = project.workflow?.[stageId] || {};
  if (item.status === "stale") return "上游有修改，需重新审阅";
  if (stageId === "video") {
    if (item.asset_url) return "旧版单段已生成";
    const stats = videoShotStats(project);
    if (stats.shots.length) return `${stats.ready} / ${stats.shots.length} 段已生成`;
    return "待生成镜头片段";
  }
  if (item.status === "approved") return "已确认";
  if (item.status === "ready") return "待确认";
  return "未生成";
}

function productionState(project) {
  const stats = videoShotStats(project);
  if (project.workflow?.video?.asset_url) return { label: "旧版单段视频已生成", complete: true };
  if (project.workflow?.video?.status === "stale") return { label: "镜头素材需更新", complete: false };
  if (stats.complete) return { label: "镜头片段已生成（未合并）", complete: true };
  if (stats.ready) return { label: `镜头片段 ${stats.ready} / ${stats.shots.length}`, complete: false };
  if (project.active_stage === "video") return { label: "待生成镜头片段", complete: false };
  return { label: `创作中 · ${stageLabel(project.active_stage)}`, complete: false };
}

function recordArtifactButton(project, stageId, label) {
  const ready = stageId === "video"
    ? videoShotStats(project).hasAssets
    : Boolean(project.workflow?.[stageId]?.content);
  return `<button class="record-artifact-link ${ready ? "is-ready" : ""}" type="button" ${ready ? `data-open-record="${escapeHTML(project.id)}"` : "disabled"}>
    <strong>${escapeHTML(label)}</strong><small>${escapeHTML(productionStageStatus(project, stageId))}</small>
  </button>`;
}

function filterProductionRecords() {
  const search = document.querySelector("#record-search")?.value.trim().toLocaleLowerCase() || "";
  let visibleCount = 0;
  document.querySelectorAll("[data-record-card]").forEach((card) => {
    const matchesSearch = !search || card.dataset.searchText.toLocaleLowerCase().includes(search);
    const matchesFilter = state.recordFilter === "all"
      || (state.recordFilter === "complete" && card.dataset.complete === "true")
      || (state.recordFilter === "active" && card.dataset.complete !== "true");
    card.hidden = !(matchesSearch && matchesFilter);
    if (!card.hidden) visibleCount += 1;
  });
  const count = document.querySelector("#record-count");
  if (count) count.textContent = `${visibleCount} 条记录`;
  const empty = document.querySelector("#record-filter-empty");
  if (empty) empty.hidden = visibleCount > 0;
}

async function importProjectBundleFromFile(input) {
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (file.size > 512 * 1024 * 1024) {
    showNotice("项目导入包超过 512 MiB 限制。", true);
    return;
  }
  const confirmed = await confirmInApp({
    title: "导入为新的项目副本？",
    message: "现有项目不会被覆盖。导入会复制项目记录、聊天附件和本地媒体；在途供应商任务不会恢复，系列会作为只含当前分集的副本导入。导出包可能包含敏感创作内容，请仅导入可信文件。",
    confirmLabel: "导入副本",
    cancelLabel: "取消",
  });
  if (!confirmed) return;

  const button = document.querySelector("#import-project");
  if (button) button.disabled = true;
  showNotice("正在校验并导入项目副本…");
  try {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch("/api/projects/import", { method: "POST", body: formData });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `导入失败（${response.status}）`);
    await Promise.all([refreshProjects(), refreshSeries()]);
    if (result.project.mode === "workflow") await openProductionRecord(result.project.id);
    else await openProject(result.project.id);
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    showNotice(`已导入新的项目副本。${warnings.length ? ` ${warnings.join(" ")}` : ""}`);
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    if (button?.isConnected) button.disabled = false;
  }
}

function renderProductionRecords() {
  state.viewMode = "records";
  workspace.classList.remove("chat-workspace");
  document.querySelector("#top-title").textContent = "制作记录";
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = true;
  viewToggle.disabled = true;
  syncModelControl();

  const records = state.projects.filter((project) => project.mode === "workflow");
  const cards = records.map((project) => {
    const status = productionState(project);
    const completedStages = Object.values(project.workflow || {}).filter((item) => item.status === "approved").length
      + (videoShotStats(project).complete ? 1 : 0);
    const brief = project.brief || {};
    const searchText = [project.track === "drama" ? "短剧 漫剧" : "品牌营销视频", project.title, brief.product, brief.business, brief.platform, brief.audience].filter(Boolean).join(" ");
    return `<article class="record-card" data-record-card data-complete="${status.complete}" data-search-text="${escapeHTML(searchText)}">
      <header class="record-card-heading">
        <div><p class="section-kicker">${escapeHTML(project.track === "drama" ? "短剧 / 漫剧" : "品牌营销视频")} · ${escapeHTML(brief.platform || "创作流程")} · ${escapeHTML(brief.duration_seconds || "—")} 秒</p>
          <h2>${escapeHTML(project.title || "未命名制作")}</h2></div>
        <span class="record-status ${status.complete ? "is-complete" : ""}">${escapeHTML(status.label)}</span>
      </header>
      <p class="record-card-summary">${escapeHTML(brief.business || "创作项目")}${brief.audience ? ` · 面向 ${escapeHTML(brief.audience)}` : ""}</p>
      <div class="record-progress"><span>流程进度</span><strong>${completedStages} / ${state.config.stages.length} 步已确认</strong></div>
      <div class="record-artifact-links" aria-label="产物状态">
        ${recordArtifactButton(project, "script", "脚本")}
        ${recordArtifactButton(project, "storyboard", "分镜")}
        ${recordArtifactButton(project, "video", "视频片段")}
      </div>
      <footer class="record-card-footer"><span>最近更新 ${escapeHTML(recordDate(project.updated_at))}${(project.review_count || project.review_records?.length) ? ` · ${project.review_count || project.review_records.length} 条审阅` : ""}</span>
        <button class="record-open-button" type="button" data-open-record="${escapeHTML(project.id)}">查看产物 <span aria-hidden="true">↗</span></button>
      </footer>
    </article>`;
  }).join("");

  workspace.innerHTML = `<section class="records-screen" aria-labelledby="records-title">
    <header class="records-heading"><div><p class="section-kicker">作品库</p><h1 id="records-title">制作记录</h1>
      <p>每次创作都会保留脚本、逐镜任务、视频片段和 AI 剪辑方案，随时回看或继续制作。</p></div>
      <div class="record-heading-actions"><button class="button button-subtle" id="import-project" type="button">导入项目副本</button>
        <input id="project-import-file" type="file" accept=".zip,application/zip" hidden />
        <button class="button button-primary" id="records-new-workflow" type="button">新建创作流程 <span aria-hidden="true">↗</span></button></div>
    </header>
    ${records.length ? `<div class="record-toolbar">
      <label class="record-search" for="record-search"><span class="sr-only">搜索制作记录</span><span aria-hidden="true">⌕</span><input id="record-search" type="search" placeholder="搜索项目名称、故事设定或业务" /></label>
      <div class="record-toolbar-meta"><span id="record-count"></span>
        <div class="record-filters" role="group" aria-label="筛选制作记录">
          <button type="button" data-record-filter="all" aria-pressed="${state.recordFilter === "all"}">全部</button>
          <button type="button" data-record-filter="active" aria-pressed="${state.recordFilter === "active"}">进行中</button>
          <button type="button" data-record-filter="complete" aria-pressed="${state.recordFilter === "complete"}">已完成</button>
        </div>
      </div>
    </div>
    <div class="records-grid">${cards}</div>
    <p id="record-filter-empty" class="record-filter-empty" hidden>没有符合条件的制作记录。</p>`
      : `<div class="records-empty"><span class="records-empty-mark" aria-hidden="true">▤</span><h2>还没有制作记录</h2>
        <p>创建创作流程后，确认过的脚本、分镜、逐镜片段和 AI 剪辑方案都会保存在这里。</p>
        <button class="button button-primary" id="record-empty-new-workflow" type="button">创建第一个项目 <span aria-hidden="true">↗</span></button></div>`}
  </section>`;

  workspace.querySelectorAll("[data-open-record]").forEach((button) => {
    button.addEventListener("click", () => openProductionRecord(button.dataset.openRecord));
  });
  workspace.querySelector("#import-project")?.addEventListener("click", () => workspace.querySelector("#project-import-file")?.click());
  workspace.querySelector("#project-import-file")?.addEventListener("change", (event) => importProjectBundleFromFile(event.currentTarget));
  workspace.querySelector("#records-new-workflow, #record-empty-new-workflow")?.addEventListener("click", async () => {
    if (!(await ensureCanvasSavedBeforeNavigation())) return;
    state.project = null;
    state.viewStage = null;
    showNotice("");
    renderWorkflowSetup();
    renderProjects();
  });
  workspace.querySelector("#record-search")?.addEventListener("input", filterProductionRecords);
  workspace.querySelectorAll("[data-record-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.recordFilter = button.dataset.recordFilter;
      workspace.querySelectorAll("[data-record-filter]").forEach((filter) => {
        filter.setAttribute("aria-pressed", String(filter === button));
      });
      filterProductionRecords();
    });
  });
  filterProductionRecords();
}

function recordContentCard(project, stageId, label) {
  const item = project.workflow?.[stageId] || {};
  const hasContent = Boolean(item.content);
  return `<article class="record-output-card">
    <header><div><p class="section-kicker">${escapeHTML(productionStageStatus(project, stageId))}</p><h2>${escapeHTML(label)}</h2></div>
      <button class="button button-subtle" type="button" data-record-stage="${escapeHTML(stageId)}" ${hasContent ? "" : "disabled"}>在工作流中打开</button></header>
    ${hasContent ? `<details class="record-content-disclosure"><summary>展开查看全文</summary><div class="chat-markdown record-markdown">${stageId === "scenes" ? renderScenePlanPreview(item.content) : renderMarkdown(item.content)}</div></details>`
      : `<p class="record-output-empty">这份内容还未生成。完成前序步骤后，可以回到工作流继续制作。</p>`}
  </article>`;
}

function renderProductionRecordDetail(project) {
  if (project.mode === "workflow" && !project.review_targets) hydrateReviewData();
  state.viewMode = "record-detail";
  workspace.classList.remove("chat-workspace");
  document.querySelector("#top-title").textContent = project.title || "制作记录";
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = true;
  viewToggle.disabled = true;
  syncModelControl();

  const brief = project.brief || {};
  const video = project.workflow?.video || {};
  const videoUrl = video.asset_url ? safeMediaUrl(video.asset_url) : "";
  const shotStats = videoShotStats(project);
  const recordedShotMedia = shotStats.shots.map((shot) => {
    const imageUrl = shot.image?.asset_url ? safeMediaUrl(shot.image.asset_url) : "";
    const shotVideoUrl = shot.video?.asset_url ? safeMediaUrl(shot.video.asset_url) : "";
    if (!imageUrl && !shotVideoUrl) return "";
    return `<article class="record-shot-card"><header><strong>${escapeHTML(shot.shot_id)} · ${escapeHTML(shot.duration_seconds)} 秒</strong><span>${escapeHTML(assetStatusLabel(shot.video || {}))}</span></header>
      <div class="record-shot-media">${imageUrl ? `<img src="${imageUrl}" alt="${escapeHTML(shot.shot_id)} 镜头关键帧" loading="lazy" />` : ""}${shotVideoUrl ? `<video controls preload="metadata" src="${shotVideoUrl}" aria-label="${escapeHTML(shot.shot_id)} 镜头视频片段"></video>` : ""}</div>
      ${shotVideoUrl && shot.video?.cost == null ? '<small class="shot-help">费用信息未由供应商返回</small>' : ""}${renderAssetVersions(shot.image || {})}${renderAssetVersions(shot.video || {})}</article>`;
  }).filter(Boolean).join("");
  const videoContent = videoUrl
    ? `<div class="record-video-wrap"><video class="record-video" controls preload="metadata" src="${videoUrl}" aria-label="${escapeHTML(project.title)} 的旧版单段视频"></video><p>旧版单段视频记录</p></div>`
    : recordedShotMedia
      ? `<div class="record-shot-list">${recordedShotMedia}</div>`
      : `<div class="record-output-empty">尚无已生成的关键帧或视频片段。确认镜头任务后，可逐镜调用模型生成。</div>`;
  const finalVideo = video.final_video || {};
  const finalVideoUrl = finalVideo.asset_url ? safeMediaUrl(finalVideo.asset_url) : "";
  const finalVideoCard = `<article class="record-output-card record-final-video-card"><header><div><p class="section-kicker">${escapeHTML(assetStatusLabel(finalVideo))}</p><h2>最终视频</h2></div>
      <button class="button button-subtle" type="button" data-record-stage="video" ${finalVideoUrl ? "" : "disabled"}>在工作流中打开</button></header>
      ${finalVideoUrl ? `<video controls preload="metadata" src="${finalVideoUrl}" aria-label="${escapeHTML(project.title)} 最终视频"></video><p>${escapeHTML(finalVideo.duration_seconds || "—")} 秒 · ${escapeHTML(finalVideo.width || "—")} × ${escapeHTML(finalVideo.height || "—")} · SHA-256 ${escapeHTML(finalVideo.sha256 || "未知")}</p>` : '<p class="record-output-empty">暂无当前可播放的本机合成成片。</p>'}
      ${renderReviewPanel(project, "final_video")}</article>`;
  const editPlan = renderEditPlan(video.edit_plan || project.workflow?.scenes?.edit_plan || {});
  const editPlanContent = editPlan ? `<section class="record-edit-plan"><h3>AI 剪辑方案（未合并）</h3>${editPlan}</section>` : "";

  workspace.innerHTML = `<section class="record-detail" aria-labelledby="record-detail-title">
    <div class="record-detail-nav"><button class="record-back-button" id="back-to-records" type="button"><span aria-hidden="true">←</span> 返回制作记录</button>
      <div class="record-detail-actions"><a class="button button-subtle" href="/api/projects/${encodeURIComponent(project.id)}/export">导出项目副本</a>
        <a class="button button-primary" href="/api/projects/${encodeURIComponent(project.id)}/delivery">下载制作交付包</a>
        <button class="button button-subtle" id="continue-production" type="button">打开创作流程</button></div></div>
    <header class="record-detail-heading"><div><p class="section-kicker">${escapeHTML(project.track === "drama" ? "短剧 / 漫剧" : "品牌营销视频")} · ${escapeHTML(brief.platform || "创作流程")} · ${escapeHTML(brief.duration_seconds || "—")} 秒 · 创建于 ${escapeHTML(recordDate(project.created_at))}</p>
      <h1 id="record-detail-title">${escapeHTML(project.title || "未命名制作")}</h1>
      <p>${escapeHTML(brief.business || "创作项目")}${brief.audience ? ` · 面向 ${escapeHTML(brief.audience)}` : ""}</p></div>
      <span class="record-status ${productionState(project).complete ? "is-complete" : ""}">${escapeHTML(productionState(project).label)}</span></header>
    <div class="record-detail-grid">
      ${recordContentCard(project, "script", "脚本")}
      ${recordContentCard(project, "storyboard", "分镜")}
      ${recordContentCard(project, "scenes", "逐镜任务与 AI 剪辑方案")}
      <article class="record-output-card record-video-card"><header><div><p class="section-kicker">${escapeHTML(productionStageStatus(project, "video"))}</p><h2>逐镜视频片段</h2></div>
        <button class="button button-subtle" type="button" data-record-stage="video" ${shotStats.hasAssets || videoUrl ? "" : "disabled"}>在工作流中打开</button></header>
        ${videoContent}${editPlanContent}</article>
      ${finalVideoCard}
    </div>
    ${renderReviewTimeline(project)}
    <p class="record-detail-updated">最近更新：${escapeHTML(recordDate(project.updated_at))}</p>
  </section>`;

  document.querySelector("#back-to-records").addEventListener("click", renderProductionRecords);
  document.querySelector("#continue-production").addEventListener("click", () => {
    state.viewStage = project.active_stage;
    renderProject();
  });
  workspace.querySelectorAll("[data-record-stage]").forEach((button) => {
    button.addEventListener("click", () => {
      state.viewStage = button.dataset.recordStage;
      renderProject();
    });
  });
  workspace.querySelectorAll("[data-review-form]").forEach((form) => {
    form.addEventListener("submit", submitProjectReview);
  });
  workspace.querySelectorAll("[data-review-open-target]").forEach((button) => {
    button.addEventListener("click", () => {
      state.viewStage = ["script", "storyboard"].includes(button.dataset.reviewOpenTarget) ? button.dataset.reviewOpenTarget : "video";
      renderProject();
    });
  });
}

function renderReviewTimeline(project) {
  const records = project.review_records || [];
  const entries = records.map((record) => `<li class="project-review-entry">
    <div class="review-history-meta"><strong>${escapeHTML(record.target_label || "项目内容")}</strong><span>${escapeHTML(reviewDecisionLabels[record.decision] || "意见")}</span><span>${escapeHTML(reviewCategoryLabels[record.category] || record.category || "综合")}</span>${record.timecode ? `<span>时间码 ${escapeHTML(record.timecode)}</span>` : ""}<time>${escapeHTML(recordDate(record.created_at))}</time><span class="review-version-badge ${record.is_current ? "" : "is-historical"}">${record.is_current ? "当前版本" : "历史版本"}</span></div>
    ${record.note ? `<p>${escapeHTML(record.note)}</p>` : "<p>确认通过。</p>"}
    <button class="button button-subtle" type="button" data-review-open-target="${escapeHTML(record.target_type)}" data-review-open-id="${escapeHTML(record.target_id || "")}">打开对应阶段</button>
  </li>`).join("");
  return `<section class="project-review-timeline" aria-labelledby="project-review-title"><div class="review-panel-heading"><div><p class="section-kicker">项目历史 · 按版本留存</p><h2 id="project-review-title">审阅记录</h2></div><span>${records.length} 条</span></div>
    ${entries ? `<ol class="project-review-list">${entries}</ol>` : '<p class="record-output-empty">还没有审阅记录。可在创作流程中对当前脚本、分镜、镜头片段或最终视频提交意见。</p>'}
  </section>`;
}

async function hydrateReviewData() {
  if (state.reviewHydrating || !state.project || state.project.review_targets) return;
  const projectId = state.project.id;
  const updatedAt = state.project.updated_at;
  let retryForCurrentProject = false;
  state.reviewHydrating = true;
  try {
    const fresh = await api(`/api/projects/${encodeURIComponent(projectId)}`);
    if (state.project?.id !== projectId || state.project.updated_at !== updatedAt) {
      retryForCurrentProject = Boolean(state.project?.mode === "workflow" && !state.project.review_targets);
      return;
    }
    state.project.review_targets = fresh.review_targets || {};
    state.project.review_records = fresh.review_records || [];
    if (state.viewMode === "record-detail") renderProductionRecordDetail(state.project);
    else if (state.viewMode === "workflow") renderProject();
  } catch (error) {
    console.warn("Unable to load project review history", error);
    const projectChanged = state.project?.id !== projectId || state.project.updated_at !== updatedAt;
    retryForCurrentProject = Boolean(projectChanged && state.project?.mode === "workflow" && !state.project.review_targets);
    if (state.project?.id === projectId) showNotice("审阅历史加载失败；可重新打开项目后重试。", true);
  } finally {
    state.reviewHydrating = false;
    if (retryForCurrentProject) queueMicrotask(hydrateReviewData);
  }
}

async function submitProjectReview(event) {
  event.preventDefault();
  if (!state.project) return;
  const form = event.currentTarget;
  const projectId = state.project.id;
  const button = form.querySelector('button[type="submit"]');
  const payload = {
    target_type: form.dataset.reviewTargetType,
    target_id: form.dataset.reviewTargetId || "",
    target_revision: form.elements.target_revision.value,
    decision: form.elements.decision.value,
    category: form.elements.category.value,
    note: form.elements.note.value,
    timecode: form.elements.timecode?.value.trim() || null,
  };
  if (payload.decision !== "approved" && !payload.note.trim()) {
    showNotice("请填写审阅意见或修改要求。", true);
    form.elements.note.focus();
    return;
  }
  if (button) { button.disabled = true; button.textContent = "正在保存…"; }
  try {
    const updatedProject = await api(`/api/projects/${encodeURIComponent(projectId)}/reviews`, {
      method: "POST", body: JSON.stringify(payload),
    });
    if (state.project?.id !== projectId) {
      showNotice("审阅记录已保存到原项目；当前打开的项目未更改。", false);
      return;
    }
    state.project = updatedProject;
    showNotice("审阅记录已保存，并绑定到提交时的内容版本。", false);
    if (state.viewMode === "record-detail") renderProductionRecordDetail(state.project);
    else renderProject();
  } catch (error) {
    showNotice(error.message, true);
    if (state.project?.id === projectId && (error.message.includes("已更新") || error.message.includes("不可审阅"))) {
      try {
        const refreshedProject = await api(`/api/projects/${encodeURIComponent(projectId)}`);
        if (state.project?.id !== projectId) return;
        state.project = refreshedProject;
        if (state.viewMode === "record-detail") renderProductionRecordDetail(state.project);
        else renderProject();
      } catch { /* Keep the submitted form available for retry. */ }
    } else if (button?.isConnected) {
      button.disabled = false;
      button.textContent = "保存审阅记录";
    }
  }
}

async function ensureCanvasSavedBeforeNavigation() {
  if (state.viewMode !== "canvas" || !canvasIsDirty()) return true;
  const saved = await saveStoryboardCanvas();
  if (saved || !canvasIsDirty()) return true;
  showNotice("分镜画布尚未保存；请先修复保存问题，或解决并发版本冲突后再离开。", true);
  return false;
}

async function openProductionRecord(projectId) {
  if (!(await ensureCanvasSavedBeforeNavigation())) return;
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(projectId)}`);
    state.viewStage = state.project.active_stage;
    showNotice("");
    renderProjects();
    renderProductionRecordDetail(state.project);
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function showProductionRecords() {
  if (!(await ensureCanvasSavedBeforeNavigation())) return;
  clearPendingUploads();
  state.project = null;
  state.viewStage = null;
  state.chatMessages = [];
  state.chatHasMore = false;
  state.chatHistoryCursor = null;
  state.chatLoadingOlder = false;
  showNotice("");
  renderProjects();
  renderProductionRecords();
}

function seriesAssetKindLabel(kind) {
  return ({ character: "人物", location: "场景", prop: "道具" })[kind] || "资产";
}

async function refreshSeries() {
  state.series = await api("/api/series");
}

async function showSeriesLibrary() {
  if (!(await ensureCanvasSavedBeforeNavigation())) return;
  clearPendingUploads();
  state.project = null;
  state.viewStage = null;
  state.chatMessages = [];
  state.chatHasMore = false;
  state.chatHistoryCursor = null;
  state.chatLoadingOlder = false;
  showNotice("");
  try {
    await refreshSeries();
    renderSeriesLibrary();
    renderProjects();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function exportSeriesBundle(seriesId) {
  showNotice("正在打包系列、分集、对话和引用的本地媒体…", false);
  try {
    const response = await fetch(`/api/series/${encodeURIComponent(seriesId)}/export`);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `系列导出失败（${response.status}）`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const filename = /filename="?([^";]+)"?/i.exec(disposition)?.[1] || "agent-platform-series.zip";
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showNotice("系列副本已准备下载；请在浏览器下载列表确认文件。", false);
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function importSeriesBundleFromFile(input) {
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (file.size > 512 * 1024 * 1024) {
    showNotice("系列导入包超过 512 MiB 限制。", true);
    return;
  }
  const confirmed = await confirmInApp({
    title: "导入为新的系列副本？",
    message: "现有系列不会被覆盖。导入会复制系列资产、分集、聊天附件和引用的本地媒体；在途供应商任务不会恢复。导入包可能包含敏感创作内容，请仅导入可信文件。",
    confirmLabel: "导入系列副本",
    cancelLabel: "取消",
  });
  if (!confirmed) return;
  const button = document.querySelector("#import-series");
  if (button) button.disabled = true;
  showNotice("正在校验并导入系列副本…", false);
  try {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch("/api/series/import", { method: "POST", body: formData });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `系列导入失败（${response.status}）`);
    await Promise.all([refreshSeries(), refreshProjects()]);
    await openSeriesDetail(result.series.id);
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    showNotice(`已导入新的系列副本。${warnings.length ? ` ${warnings.join(" ")}` : ""}`, false);
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    if (button?.isConnected) button.disabled = false;
  }
}

function renderSeriesLibrary() {
  state.viewMode = "series-library";
  workspace.classList.remove("chat-workspace");
  document.querySelector("#top-title").textContent = "剧集与资产";
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = true;
  viewToggle.disabled = true;
  syncModelControl();
  workspace.innerHTML = `
    <section class="series-screen">
      <header class="series-heading">
        <div><p class="section-kicker">短剧 / 漫剧 · 连续性资料</p><h1>剧集与创作资产</h1><p>按系列整理分集、人物、场景和道具。归档只会停止新增，不会删除已有内容。</p></div>
        <div class="series-heading-tools"><button class="button button-subtle" id="import-series" type="button">导入系列副本</button>
          <input id="series-import-file" type="file" accept=".zip,application/zip" hidden aria-label="选择系列导入包" />
          <button class="button button-primary" id="show-series-create" type="button">新建系列</button></div>
      </header>
      <form id="series-create-form" class="series-form series-create-form" hidden>
        <h2>建立一个故事系列</h2>
        <label>系列名称<input name="title" required maxlength="120" placeholder="例如：雾港来信" /></label>
        <label>一句话梗概<textarea name="logline" maxlength="3000" rows="2" placeholder="故事的核心冲突或贯穿线索"></textarea></label>
        <label>系列视觉基调<textarea name="visual_style" maxlength="2000" rows="2" placeholder="跨集保持一致的色彩、媒介与影像质感"></textarea></label>
        <label>系列备注<textarea name="notes" maxlength="5000" rows="2" placeholder="节奏、世界规则或其他长期设定"></textarea></label>
        <p id="series-create-status" class="series-form-status" role="status" aria-live="polite"></p>
        <div class="series-form-actions"><button class="button button-subtle" id="cancel-series-create" type="button">取消</button><button class="button button-primary" type="submit">保存系列</button></div>
      </form>
      ${state.series.length ? `<div class="series-list">${state.series.map((series) => `
        <article class="series-list-row ${series.archived ? "is-archived" : ""}">
          <div class="series-list-copy"><div class="series-list-title"><h2>${escapeHTML(series.title)}</h2>${series.archived ? '<span class="series-archive-badge">已归档</span>' : ""}</div>
            <p>${escapeHTML(series.logline || "尚未填写系列梗概。")}</p><small>${series.episode_count} 集 · ${series.asset_count} 项有效资产</small></div>
          <button class="button button-subtle" type="button" data-open-series="${escapeHTML(series.id)}">管理系列</button>
        </article>`).join("")}</div>` : `
        <div class="series-empty"><span class="series-empty-mark" aria-hidden="true">▤</span><h2>还没有故事系列</h2><p>先建立系列，再添加跨集复用的人物、场景和道具设定。</p><button class="button button-subtle" id="series-empty-create" type="button">建立第一个系列</button></div>`}
    </section>`;
  const createForm = workspace.querySelector("#series-create-form");
  workspace.querySelector("#import-series")?.addEventListener("click", () => workspace.querySelector("#series-import-file")?.click());
  workspace.querySelector("#series-import-file")?.addEventListener("change", (event) => importSeriesBundleFromFile(event.currentTarget));
  const openCreate = () => { createForm.hidden = false; createForm.querySelector('[name="title"]').focus(); };
  workspace.querySelector("#show-series-create")?.addEventListener("click", openCreate);
  workspace.querySelector("#series-empty-create")?.addEventListener("click", openCreate);
  workspace.querySelector("#cancel-series-create")?.addEventListener("click", () => { createForm.hidden = true; });
  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = workspace.querySelector("#series-create-status");
    const submit = createForm.querySelector('[type="submit"]');
    submit.disabled = true;
    status.textContent = "正在保存系列…";
    try {
      const created = await api("/api/series", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(createForm).entries())) });
      await refreshSeries();
      await openSeriesDetail(created.id);
      showNotice("系列已建立。", false);
    } catch (error) {
      status.textContent = error.message;
      submit.disabled = false;
    }
  });
  workspace.querySelectorAll("[data-open-series]").forEach((button) => {
    button.addEventListener("click", () => openSeriesDetail(button.dataset.openSeries));
  });
}

async function openSeriesDetail(seriesId) {
  try {
    const series = await api(`/api/series/${encodeURIComponent(seriesId)}`);
    renderSeriesDetail(series);
    renderProjects();
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderSeriesAssetForm(asset = null) {
  const prefix = asset ? `asset-${asset.id}` : "new-series-asset";
  const value = (field) => escapeHTML(asset?.[field] || "");
  const kind = asset?.kind || "character";
  const media = Array.isArray(asset?.media) ? asset.media : [];
  const mediaGallery = media.length ? `<div class="series-asset-media-grid">${media.map((item) => {
    const url = safeMediaUrl(item.asset_url);
    return url ? `<figure><img src="${url}" alt="${escapeHTML(asset.name)}参考图：${escapeHTML(item.filename || "本地图片")}" loading="lazy" /><figcaption>${escapeHTML(item.filename || "本地图片")} · ${escapeHTML(Math.ceil((item.size || 0) / 1024))} KB</figcaption></figure>` : "";
  }).join("")}</div>` : '<p class="series-form-hint">尚未上传本地参考图。</p>';
  return `<form class="series-form series-asset-form" ${asset ? `data-series-asset-form="${escapeHTML(asset.id)}"` : 'id="new-series-asset-form"'}>
    <div class="form-row"><label for="${prefix}-kind">资产类型<select id="${prefix}-kind" name="kind"><option value="character" ${kind === "character" ? "selected" : ""}>人物</option><option value="location" ${kind === "location" ? "selected" : ""}>场景</option><option value="prop" ${kind === "prop" ? "selected" : ""}>道具</option></select></label>
      <label for="${prefix}-name">名称<input id="${prefix}-name" name="name" required maxlength="120" value="${value("name")}" /></label></div>
    <label for="${prefix}-description">设定<textarea id="${prefix}-description" name="description" maxlength="3000" rows="2">${value("description")}</textarea></label>
    <label for="${prefix}-visual">视觉提示<textarea id="${prefix}-visual" name="visual_prompt" maxlength="3000" rows="2" placeholder="用于跨集保持外观、色彩或构图一致">${value("visual_prompt")}</textarea></label>
    <label for="${prefix}-continuity">连续性备注<textarea id="${prefix}-continuity" name="continuity_notes" maxlength="3000" rows="2" placeholder="例如：右手腕始终佩戴旧表">${value("continuity_notes")}</textarea></label>
    ${asset ? `<section class="series-asset-media" aria-label="本地参考图"><h3>本地参考图</h3>${mediaGallery}<label for="${prefix}-media-files">添加图片 <small>PNG / JPEG / WebP，单张不超过 20 MiB，每项最多 8 张</small><input id="${prefix}-media-files" type="file" accept="image/png,image/jpeg,image/webp" multiple ${asset.archived ? "disabled" : ""} /></label><button class="button button-subtle" type="button" data-upload-series-asset-media="${escapeHTML(asset.id)}" ${asset.archived ? "disabled" : ""}>上传选中的图片</button><p class="series-form-status" data-asset-media-status="${escapeHTML(asset.id)}" role="status" aria-live="polite"></p></section>` : '<p class="series-form-hint">保存资产后，可为其添加本地参考图。</p>'}
    ${asset ? `<label class="series-archive-toggle"><input type="checkbox" name="archived" ${asset.archived ? "checked" : ""} />归档此资产（保留已有分集）</label>` : ""}
    <p class="series-form-status" role="status" aria-live="polite"></p>
    <div class="series-form-actions"><button class="button button-primary" type="submit">${asset ? "保存资产" : "添加资产"}</button></div>
  </form>`;
}

function renderEpisodeOutlineCard(episode) {
  const outline = episode.episode_outline || {};
  const status = outline.status === "approved" ? "作者已确认" : outline.content ? "草稿 · 待审核" : "尚未创建";
  const revisions = Array.isArray(outline.revisions) ? outline.revisions : [];
  return `<details class="episode-outline-card"><summary>可选分集大纲 · ${status}</summary>
    <p>大纲不是新的工作流阶段；生成后可修改，只有点击“确认大纲”后才会作为脚本参考。</p>
    <label for="episode-outline-${escapeHTML(episode.id)}">分集大纲<textarea id="episode-outline-${escapeHTML(episode.id)}" data-episode-outline-content="${escapeHTML(episode.id)}" maxlength="12000" rows="6" placeholder="可以先生成，也可以直接写入自己的分集规划">${escapeHTML(outline.content || "")}</textarea></label>
    <div class="episode-outline-actions"><button class="button button-subtle" type="button" data-episode-outline-generate="${escapeHTML(episode.id)}">生成 / 重生成草稿</button>
      <button class="button button-subtle" type="button" data-episode-outline-save="draft" data-episode-id="${escapeHTML(episode.id)}">保存为草稿</button>
      <button class="button button-primary" type="button" data-episode-outline-save="approved" data-episode-id="${escapeHTML(episode.id)}">确认大纲</button></div>
    ${revisions.length ? `<details class="episode-outline-history"><summary>历史版本（${revisions.length}）</summary><ol>${[...revisions].reverse().map((revision) => `<li><small>${escapeHTML(recordDate(revision.archived_at))} · ${escapeHTML(revision.reason || "旧版")}</small><p>${escapeHTML(revision.content || "")}</p></li>`).join("")}</ol></details>` : ""}
  </details>`;
}

async function generateEpisodeOutline(projectId) {
  const confirmed = await confirmInApp({
    title: "生成分集大纲草稿？",
    message: "将使用当前系列设定、已确认连续性和此前已确认剧本调用所选文本模型。生成内容不会自动视为既定剧情。",
    confirmLabel: "生成大纲草稿",
  });
  if (!confirmed) return;
  try {
    await api(`/api/projects/${encodeURIComponent(projectId)}/episode-outline/generate`, {
      method: "POST", body: JSON.stringify({ model: state.selectedModel || state.config?.default_model }),
    });
    if (state.currentSeriesId) await openSeriesDetail(state.currentSeriesId);
    showNotice("分集大纲草稿已生成；请编辑并显式确认后，再用于脚本创作。", false);
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function saveEpisodeOutline(projectId, status) {
  const textarea = workspace.querySelector(`[data-episode-outline-content="${CSS.escape(projectId)}"]`);
  if (!textarea) return;
  try {
    await api(`/api/projects/${encodeURIComponent(projectId)}/episode-outline`, {
      method: "PUT", body: JSON.stringify({ content: textarea.value, status }),
    });
    const seriesId = state.currentSeriesId;
    if (seriesId) await openSeriesDetail(seriesId);
    showNotice(status === "approved" ? "分集大纲已确认，可作为本集剧本参考。" : "分集大纲草稿已保存。", false);
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderSeriesDetail(series) {
  state.currentSeriesId = series.id;
  state.viewMode = "series-detail";
  workspace.classList.remove("chat-workspace");
  document.querySelector("#top-title").textContent = series.title;
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = true;
  viewToggle.disabled = true;
  syncModelControl();
  const episodes = Array.isArray(series.episodes) ? series.episodes : [];
  const assets = Array.isArray(series.assets) ? series.assets : [];
  const continuityEntries = Array.isArray(series.continuity_entries) ? series.continuity_entries : [];
  const approvedSources = episodes.flatMap((episode) => ["script", "prompt", "storyboard", "scenes"]
    .filter((stageId) => episode.workflow?.[stageId]?.status === "approved" && episode.workflow?.[stageId]?.content)
    .map((stageId) => ({
      value: `${episode.id}::${stageId}`,
      label: `EP${String(episode.episode_number || 0).padStart(2, "0")} · ${episode.title} · ${stageLabel(stageId)}`,
    })));
  const continuityKinds = { fact: "既定事实", event: "已发生事件", relationship: "关系变化", clue: "线索", open_thread: "未解线索", constraint: "创作约束" };
  const continuityStatuses = { confirmed: "已确认", open: "待解决", resolved: "已解决", archived: "已归档" };
  workspace.innerHTML = `
    <section class="series-screen series-detail-screen">
      <div class="series-detail-nav"><button class="record-back-button" id="back-to-series-list" type="button">← 返回系列列表</button>${series.archived ? '<span class="series-archive-badge">已归档 · 仍可查看</span>' : ""}</div>
      <header class="series-heading series-detail-heading"><div><p class="section-kicker">短剧 / 漫剧系列</p><h1>${escapeHTML(series.title)}</h1><p>${escapeHTML(series.logline || "尚未填写系列梗概。")}</p></div>
        <div class="series-heading-actions"><button class="button button-subtle" id="export-series" type="button">导出系列副本</button>
          <button class="button button-primary" id="new-series-episode" type="button" ${series.archived ? "disabled" : ""}>新建分集</button></div></header>
      <div class="series-detail-grid">
        <section class="series-panel" aria-labelledby="series-settings-title"><h2 id="series-settings-title">系列设定</h2>
          <form id="series-update-form" class="series-form">
            <label for="series-title">系列名称<input id="series-title" name="title" required maxlength="120" value="${escapeHTML(series.title)}" /></label>
            <label for="series-logline">一句话梗概<textarea id="series-logline" name="logline" maxlength="3000" rows="3">${escapeHTML(series.logline)}</textarea></label>
            <label for="series-genre">题材<input id="series-genre" name="genre" maxlength="300" value="${escapeHTML(series.genre || "")}" /></label>
            <label for="series-world-setting">世界设定<textarea id="series-world-setting" name="world_setting" maxlength="5000" rows="4">${escapeHTML(series.world_setting || "")}</textarea></label>
            <label for="series-central-theme">核心主题<textarea id="series-central-theme" name="central_theme" maxlength="2000" rows="3">${escapeHTML(series.central_theme || "")}</textarea></label>
            <label for="series-season-arc">季 / 主线弧光<textarea id="series-season-arc" name="season_arc" maxlength="5000" rows="4">${escapeHTML(series.season_arc || "")}</textarea></label>
            <label for="series-character-arcs">主要人物弧光<textarea id="series-character-arcs" name="character_arcs" maxlength="5000" rows="4">${escapeHTML(series.character_arcs || "")}</textarea></label>
            <label for="series-ending">计划结局<textarea id="series-ending" name="ending" maxlength="3000" rows="3">${escapeHTML(series.ending || "")}</textarea></label>
            <label for="series-tone">叙事基调<input id="series-tone" name="tone" maxlength="1000" value="${escapeHTML(series.tone || "")}" /></label>
            <label for="series-boundaries">内容边界<textarea id="series-boundaries" name="content_boundaries" maxlength="2000" rows="3">${escapeHTML(series.content_boundaries || "")}</textarea></label>
            <label for="series-visual-style">系列视觉基调<textarea id="series-visual-style" name="visual_style" maxlength="2000" rows="3">${escapeHTML(series.visual_style)}</textarea></label>
            <label for="series-notes">系列备注<textarea id="series-notes" name="notes" maxlength="5000" rows="3">${escapeHTML(series.notes)}</textarea></label>
            <label class="series-archive-toggle"><input type="checkbox" name="archived" ${series.archived ? "checked" : ""} />归档系列（保留已有分集和资产）</label>
            <button class="button button-primary" type="submit">保存系列设定</button>
          </form>
        </section>
        <section class="series-panel" aria-labelledby="series-episodes-title"><div class="series-panel-heading"><div><h2 id="series-episodes-title">分集</h2><p>${episodes.length} 集，按集数排序</p></div></div>
          ${episodes.length ? `<ol class="series-episode-list">${episodes.map((episode) => `<li><button type="button" data-open-episode="${escapeHTML(episode.id)}"><span>EP${String(episode.episode_number || 0).padStart(2, "0")}</span><strong>${escapeHTML(episode.title)}</strong></button><small>${escapeHTML(stageLabel(episode.active_stage))}${episode.archived ? " · 已从最近对话移除" : ""}</small>${renderEpisodeOutlineCard(episode)}</li>`).join("")}</ol>` : '<p class="series-panel-empty">系列还没有分集。新建分集后可逐步确认脚本、视觉设定与镜头任务。</p>'}
        </section>
      </div>
      <section class="series-panel continuity-panel" aria-labelledby="series-continuity-title">
        <div class="series-panel-heading"><div><h2 id="series-continuity-title">连续性台账</h2><p>仅已确认内容会作为事实，未解线索会提示模型保留；分集来源必须引用已确认步骤中的原文。</p></div></div>
        <form id="new-continuity-entry" class="series-form" ${series.archived ? "hidden" : ""}>
          <div class="form-row"><label for="continuity-kind">记录类型<select id="continuity-kind" name="kind">${Object.entries(continuityKinds).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select></label>
            <label for="continuity-status">状态<select id="continuity-status" name="status"><option value="confirmed">已确认</option><option value="open">待解决</option></select></label></div>
          <label for="continuity-summary">事实 / 线索<textarea id="continuity-summary" name="summary" required maxlength="1200" rows="3" placeholder="例如：林澄已经知道录音带标签上的蓝色划痕与旧仓库有关。"></textarea></label>
          <label for="continuity-source">来源分集步骤（可选）<select id="continuity-source"><option value="">人工补充（无分集引用）</option>${approvedSources.map((item) => `<option value="${escapeHTML(item.value)}">${escapeHTML(item.label)}</option>`).join("")}</select></label>
          <label for="continuity-excerpt">已确认原文节选<textarea id="continuity-excerpt" maxlength="1000" rows="2" placeholder="需与所选步骤中的文字完全一致；不选来源时留空"></textarea></label>
          <p class="series-form-status" role="status" aria-live="polite"></p><div class="series-form-actions"><button class="button button-primary" type="submit">添加台账记录</button></div>
        </form>
        ${continuityEntries.length ? `<div class="continuity-entry-list">${continuityEntries.map((entry) => {
          const source = entry.source || {};
          const sourceLabel = source.type === "episode" ? `EP${String(source.episode_number || 0).padStart(2, "0")} · ${stageLabel(source.stage)}` : "人工补充";
          return `<form class="continuity-entry" data-continuity-entry="${escapeHTML(entry.id)}">
            <header><div><span class="continuity-kind">${escapeHTML(continuityKinds[entry.kind] || entry.kind || "记录")}</span><small>${escapeHTML(sourceLabel)}</small></div><small>${escapeHTML(continuityStatuses[entry.status] || entry.status || "")}</small></header>
            ${source.excerpt ? `<blockquote>${escapeHTML(source.excerpt)}</blockquote>` : ""}
            <label for="continuity-entry-${escapeHTML(entry.id)}-summary">记录内容<textarea id="continuity-entry-${escapeHTML(entry.id)}-summary" name="summary" required maxlength="1200" rows="2">${escapeHTML(entry.summary || "")}</textarea></label>
            <p class="series-form-status" role="status" aria-live="polite"></p><div class="form-row"><label for="continuity-entry-${escapeHTML(entry.id)}-status">状态<select id="continuity-entry-${escapeHTML(entry.id)}-status" name="status">${Object.entries(continuityStatuses).map(([value, label]) => `<option value="${value}" ${entry.status === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><button class="button button-subtle" type="submit">保存记录</button></div>
            ${(entry.versions || []).length ? `<details><summary>历史修改（${entry.versions.length}）</summary><ol>${entry.versions.map((version) => `<li>${escapeHTML(continuityStatuses[version.status] || version.status)} · ${escapeHTML(version.summary)}</li>`).join("")}</ol></details>` : ""}
          </form>`;
        }).join("")}</div>` : '<p class="series-panel-empty">尚无连续性记录。可先登记已确认事实、关系变化和待解决线索。</p>'}
      </section>
      <section class="series-panel series-assets-panel" aria-labelledby="series-assets-title"><div class="series-panel-heading"><div><h2 id="series-assets-title">人物、场景与道具</h2><p>有效资产会作为连续性参考，用于本系列后续生成；每次最多带入 20 项。</p></div><button class="button button-subtle" id="show-series-asset-form" type="button" ${series.archived ? "disabled" : ""}>添加资产</button></div>
        <div id="new-series-asset-host" hidden>${renderSeriesAssetForm()}</div>
        ${assets.length ? `<div class="series-asset-list">${assets.map((asset) => `<details class="series-asset-item" ${asset.archived ? "" : ""}><summary><span>${seriesAssetKindLabel(asset.kind)}</span><strong>${escapeHTML(asset.name)}</strong>${asset.archived ? '<small class="series-archive-badge">已归档</small>' : ""}</summary>${renderSeriesAssetForm(asset)}</details>`).join("")}</div>` : '<p class="series-panel-empty">还没有资产。可先登记主角外观、常驻场景或关键道具。</p>'}
      </section>
    </section>`;
  workspace.querySelector("#back-to-series-list").addEventListener("click", showSeriesLibrary);
  workspace.querySelector("#export-series").addEventListener("click", () => exportSeriesBundle(series.id));
  workspace.querySelector("#new-series-episode").addEventListener("click", () => {
    if (series.archived) return;
    state.selectedSeriesId = series.id;
    showNotice("");
    renderWorkflowSetup();
    renderProjects();
    document.querySelector("#project-track").value = "drama";
    updateWorkflowTrackCopy("drama");
    document.querySelector("#series-select").value = series.id;
  });
  workspace.querySelectorAll("[data-episode-outline-generate]").forEach((button) => {
    button.addEventListener("click", () => generateEpisodeOutline(button.dataset.episodeOutlineGenerate));
  });
  workspace.querySelectorAll("[data-episode-outline-save]").forEach((button) => {
    button.addEventListener("click", () => saveEpisodeOutline(button.dataset.episodeId, button.dataset.episodeOutlineSave));
  });
  workspace.querySelector("#series-update-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form).entries());
    payload.archived = form.elements.archived.checked;
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      await api(`/api/series/${encodeURIComponent(series.id)}`, { method: "PUT", body: JSON.stringify(payload) });
      await refreshSeries();
      await openSeriesDetail(series.id);
      showNotice("系列设定已保存。", false);
    } catch (error) {
      showNotice(error.message, true);
      submit.disabled = false;
    }
  });
  const newContinuityForm = workspace.querySelector("#new-continuity-entry");
  newContinuityForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form).entries());
    const sourceValue = workspace.querySelector("#continuity-source").value;
    if (sourceValue) {
      const [sourceProjectId, sourceStage] = sourceValue.split("::");
      payload.source_project_id = sourceProjectId;
      payload.source_stage = sourceStage;
      payload.source_excerpt = workspace.querySelector("#continuity-excerpt").value;
    }
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      await api(`/api/series/${encodeURIComponent(series.id)}/continuity`, { method: "POST", body: JSON.stringify(payload) });
      await refreshSeries();
      await openSeriesDetail(series.id);
      showNotice("连续性记录已保存；只有已确认内容会作为故事事实传入后续分集。", false);
    } catch (error) {
      form.querySelector(".series-form-status").textContent = error.message;
      submit.disabled = false;
    }
  });
  workspace.querySelectorAll("[data-continuity-entry]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const currentForm = event.currentTarget;
      const payload = Object.fromEntries(new FormData(currentForm).entries());
      const submit = currentForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        await api(`/api/series/${encodeURIComponent(series.id)}/continuity/${encodeURIComponent(currentForm.dataset.continuityEntry)}`, {
          method: "PUT", body: JSON.stringify(payload),
        });
        await refreshSeries();
        await openSeriesDetail(series.id);
        showNotice("连续性记录已更新，旧版内容保留在历史中。", false);
      } catch (error) {
        currentForm.querySelector(".series-form-status").textContent = error.message;
        submit.disabled = false;
      }
    });
  });
  const newAssetHost = workspace.querySelector("#new-series-asset-host");
  workspace.querySelector("#show-series-asset-form").addEventListener("click", () => {
    newAssetHost.hidden = false;
    newAssetHost.querySelector('[name="name"]').focus();
  });
  const createAssetForm = workspace.querySelector("#new-series-asset-form");
  createAssetForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      await api(`/api/series/${encodeURIComponent(series.id)}/assets`, { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
      await refreshSeries();
      await openSeriesDetail(series.id);
      showNotice("系列资产已添加。", false);
    } catch (error) {
      form.querySelector(".series-form-status").textContent = error.message;
      submit.disabled = false;
    }
  });
  workspace.querySelectorAll("[data-series-asset-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const currentForm = event.currentTarget;
      const payload = Object.fromEntries(new FormData(currentForm).entries());
      payload.archived = currentForm.elements.archived.checked;
      const submit = currentForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        await api(`/api/series/${encodeURIComponent(series.id)}/assets/${encodeURIComponent(currentForm.dataset.seriesAssetForm)}`, { method: "PUT", body: JSON.stringify(payload) });
        await refreshSeries();
        await openSeriesDetail(series.id);
        showNotice("系列资产已保存。", false);
      } catch (error) {
        currentForm.querySelector(".series-form-status").textContent = error.message;
        submit.disabled = false;
      }
    });
  });
  workspace.querySelectorAll("[data-upload-series-asset-media]").forEach((button) => {
    button.addEventListener("click", () => uploadSeriesAssetMedia(series.id, button.dataset.uploadSeriesAssetMedia, button));
  });
  workspace.querySelectorAll("[data-open-episode]").forEach((button) => {
    button.addEventListener("click", () => openProductionRecord(button.dataset.openEpisode));
  });
}

async function uploadSeriesAssetMedia(seriesId, assetId, button) {
  const form = button.closest("form");
  const input = form?.querySelector('input[type="file"]');
  const files = Array.from(input?.files || []);
  const status = form?.querySelector(`[data-asset-media-status="${CSS.escape(assetId)}"]`);
  if (!files.length) {
    if (status) status.textContent = "请先选择一张或多张图片。";
    return;
  }
  if (files.length > 8) {
    if (status) status.textContent = "每次最多选择 8 张图片。";
    return;
  }
  const formData = new FormData();
  for (const file of files) formData.append("files", file, file.name);
  button.disabled = true;
  if (status) status.textContent = "正在上传并校验本地图片…";
  try {
    const response = await fetch(`/api/series/${encodeURIComponent(seriesId)}/assets/${encodeURIComponent(assetId)}/media`, {
      method: "POST", body: formData, credentials: "same-origin",
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `上传失败（HTTP ${response.status}）`);
    if (input) input.value = "";
    await refreshSeries();
    await openSeriesDetail(seriesId);
    showNotice("本地参考图已关联到系列资产；图片不会自动发送给生成供应商。", false);
  } catch (error) {
    if (status?.isConnected) status.textContent = error.message;
    if (button.isConnected) button.disabled = false;
  }
}

async function refreshProjects() {
  state.projects = await api("/api/projects");
  renderProjects();
}

function openRenameProject(projectId) {
  const project = state.projects.find((item) => item.id === projectId);
  if (!project || renameDialog.open) return;
  state.renameProjectId = projectId;
  const input = document.querySelector("#rename-input");
  input.value = project.title || "";
  document.querySelector("#rename-error").textContent = "";
  renameDialog.showModal();
  input.focus();
  input.select();
}

async function saveProjectTitle(event) {
  event.preventDefault();
  if (!state.renameProjectId) return;
  const input = document.querySelector("#rename-input");
  const title = input.value.trim();
  if (!title) {
    document.querySelector("#rename-error").textContent = "名称不能为空。";
    input.focus();
    return;
  }
  try {
    const project = await api(`/api/projects/${encodeURIComponent(state.renameProjectId)}/title`, {
      method: "PUT", body: JSON.stringify({ title }),
    });
    state.projects = state.projects.map((item) => item.id === project.id ? project : item);
    if (state.project?.id === project.id) {
      state.project = project;
      document.querySelector("#top-title").textContent = project.title;
      if (state.viewMode === "records") renderProductionRecords();
      if (state.viewMode === "record-detail") renderProductionRecordDetail(project);
    }
    renameDialog.close();
    renderProjects();
  } catch (error) {
    document.querySelector("#rename-error").textContent = error.message;
  }
}

async function archiveProject(projectId) {
  const project = state.projects.find((item) => item.id === projectId);
  if (!project) return;
  const confirmed = await confirmInApp({
    title: "从最近对话移除？",
    message: `“${project.title}”只会从最近对话列表隐藏。聊天内容、制作记录、任务和本地媒体都会保留。之后可从制作记录继续打开创作流程。`,
    confirmLabel: "从列表移除",
  });
  if (!confirmed) return;
  try {
    const updated = await api(`/api/projects/${encodeURIComponent(projectId)}/archive`, { method: "POST" });
    state.projects = state.projects.map((item) => item.id === projectId ? updated : item);
    if (state.project?.id === projectId) state.project = updated;
    renderProjects();
    showNotice("已从最近对话移除；项目和制作记录仍保留。", false);
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function showNovelLibrary() {
  if (!(await ensureCanvasSavedBeforeNavigation())) return;
  try {
    state.novels = await api("/api/novels");
    renderNovelLibrary();
    renderProjects();
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderNovelLibrary() {
  state.viewMode = "novel-library";
  workspace.classList.remove("chat-workspace");
  document.querySelector("#top-title").textContent = "长篇作品";
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = true;
  viewToggle.disabled = true;
  syncModelControl();
  workspace.innerHTML = `<section class="novel-screen">
    <header class="series-heading"><div><p class="section-kicker">长篇写作 · 故事圣经与章节</p><h1>长篇作品</h1><p>先维护故事圣经，再按章节独立撰写、确认和修订。只有较早且已确认的章节正文会被用作后续生成参考。</p></div>
      <button class="button button-primary" id="show-novel-create" type="button">新建作品</button></header>
    <form id="novel-create-form" class="series-form novel-create-form" hidden>
      <h2>建立长篇作品</h2><label for="novel-create-title">作品名称<input id="novel-create-title" name="title" required maxlength="120" /></label>
      <label for="novel-create-logline">一句话梗概<textarea id="novel-create-logline" name="logline" maxlength="3000" rows="2"></textarea></label>
      <p class="series-form-status" role="status" aria-live="polite"></p><div class="series-form-actions"><button class="button button-subtle" id="cancel-novel-create" type="button">取消</button><button class="button button-primary" type="submit">创建作品</button></div>
    </form>
    ${state.novels.length ? `<div class="novel-list">${state.novels.map((novel) => `<article class="novel-list-row"><div><h2>${escapeHTML(novel.title)}</h2><p>${escapeHTML(novel.logline || "尚未填写梗概。")}</p><small>${novel.chapter_count} 章 · ${novel.approved_chapter_count} 章已确认</small></div><button class="button button-subtle" type="button" data-open-novel="${escapeHTML(novel.id)}">打开作品</button></article>`).join("")}</div>` : `<div class="series-empty"><span class="series-empty-mark" aria-hidden="true">▤</span><h2>还没有长篇作品</h2><p>建立作品后，可以集中维护人物、世界规则、主题和计划结局，再逐章规划与写作。</p><button class="button button-subtle" id="novel-empty-create" type="button">建立第一个作品</button></div>`}
  </section>`;
  const form = workspace.querySelector("#novel-create-form");
  const showCreate = () => { form.hidden = false; workspace.querySelector("#novel-create-title")?.focus(); };
  workspace.querySelector("#show-novel-create").addEventListener("click", showCreate);
  workspace.querySelector("#novel-empty-create")?.addEventListener("click", showCreate);
  workspace.querySelector("#cancel-novel-create").addEventListener("click", () => { form.hidden = true; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      const created = await api("/api/novels", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
      state.novels = await api("/api/novels");
      await openNovelDetail(created.id);
      showNotice("长篇作品已创建。", false);
    } catch (error) {
      form.querySelector(".series-form-status").textContent = error.message;
      submit.disabled = false;
    }
  });
  workspace.querySelectorAll("[data-open-novel]").forEach((button) => {
    button.addEventListener("click", () => openNovelDetail(button.dataset.openNovel));
  });
}

async function openNovelDetail(novelId) {
  try {
    const novel = await api(`/api/novels/${encodeURIComponent(novelId)}`);
    renderNovelDetail(novel);
    renderProjects();
  } catch (error) {
    showNotice(error.message, true);
  }
}

function novelChapterStatus(status) {
  return ({ planning: "规划中", draft: "草稿 · 待确认", approved: "作者已确认", stale: "前文有变更 · 待复核" })[status] || status || "规划中";
}

function renderNovelDetail(novel) {
  state.activeNovel = novel;
  state.activeNovelId = novel.id;
  state.viewMode = "novel-detail";
  workspace.classList.remove("chat-workspace");
  document.querySelector("#top-title").textContent = novel.title;
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = true;
  viewToggle.disabled = true;
  syncModelControl();
  const fields = [
    ["title", "作品名称", "input", 120], ["logline", "一句话梗概", "textarea", 3000],
    ["genre", "题材", "input", 300], ["world_setting", "世界与规则", "textarea", 5000],
    ["central_theme", "核心主题", "textarea", 2000], ["protagonist", "主角设定", "textarea", 3000],
    ["character_arcs", "主要人物弧光", "textarea", 5000], ["ending", "计划结局", "textarea", 3000],
    ["tone", "叙事基调", "input", 1000], ["content_boundaries", "内容边界", "textarea", 2000],
  ];
  const nextNumber = Math.max(0, ...novel.chapters.map((chapter) => Number(chapter.chapter_number) || 0)) + 1;
  workspace.innerHTML = `<section class="novel-screen novel-detail-screen">
    <div class="series-detail-nav"><button class="record-back-button" id="back-to-novel-library" type="button">← 返回长篇作品</button></div>
    <header class="series-heading"><div><p class="section-kicker">长篇故事圣经</p><h1>${escapeHTML(novel.title)}</h1><p>${escapeHTML(novel.logline || "尚未填写梗概。")}</p></div></header>
    <section class="series-panel novel-bible-panel"><h2>故事圣经</h2><p class="novel-guidance">修改故事圣经不会覆盖章节正文；后续生成会按需参考本设定和已确认的前文。</p>
      <form id="novel-bible-form" class="series-form novel-bible-form">${fields.map(([field, label, type, max]) => `<label for="novel-${field}">${label}${type === "input" ? `<input id="novel-${field}" name="${field}" maxlength="${max}" ${field === "title" ? "required" : ""} value="${escapeHTML(novel[field] || "")}" />` : `<textarea id="novel-${field}" name="${field}" maxlength="${max}" rows="${field === "world_setting" || field === "character_arcs" ? 4 : 3}">${escapeHTML(novel[field] || "")}</textarea>`}</label>`).join("")}
        <p class="series-form-status" role="status" aria-live="polite"></p><div class="series-form-actions"><button class="button button-primary" type="submit">保存故事圣经</button></div>
      </form>
    </section>
    <section class="series-panel novel-chapters-panel"><div class="series-panel-heading"><div><h2>章节</h2><p>${novel.chapters.length} 章 · 仅较早且已确认的正文会作为后续生成参考</p></div></div>
      <form id="novel-chapter-create" class="series-form novel-chapter-create"><h3>添加章节</h3><div class="form-row"><label for="new-chapter-number">章节编号<input id="new-chapter-number" name="chapter_number" type="number" min="1" max="9999" value="${nextNumber}" required /></label><label for="new-chapter-title">章节标题<input id="new-chapter-title" name="title" maxlength="160" required /></label></div>
        <label for="new-chapter-outline">初始大纲（可选）<textarea id="new-chapter-outline" name="outline" maxlength="12000" rows="3"></textarea></label><p class="series-form-status" role="status" aria-live="polite"></p><div class="series-form-actions"><button class="button button-primary" type="submit">添加章节</button></div></form>
      ${novel.chapters.length ? `<div class="novel-chapter-list">${novel.chapters.map((chapter) => `<article class="novel-chapter-card" data-novel-chapter="${escapeHTML(chapter.id)}">
        <header><div><p class="section-kicker">第 ${escapeHTML(chapter.chapter_number)} 章 · ${escapeHTML(novelChapterStatus(chapter.status))}</p><h3>${escapeHTML(chapter.title)}</h3>${chapter.stale_reason ? `<p class="novel-stale-note">${escapeHTML(chapter.stale_reason)}</p>` : ""}</div></header>
        <label for="chapter-title-${escapeHTML(chapter.id)}">章节标题<input id="chapter-title-${escapeHTML(chapter.id)}" data-novel-title value="${escapeHTML(chapter.title)}" maxlength="160" /></label>
        <label for="chapter-outline-${escapeHTML(chapter.id)}">章节大纲<textarea id="chapter-outline-${escapeHTML(chapter.id)}" data-novel-outline maxlength="12000" rows="4">${escapeHTML(chapter.outline || "")}</textarea></label>
        <label for="chapter-content-${escapeHTML(chapter.id)}">章节正文<textarea id="chapter-content-${escapeHTML(chapter.id)}" data-novel-content maxlength="100000" rows="16">${escapeHTML(chapter.content || "")}</textarea></label>
        <div class="novel-chapter-actions"><button class="button button-subtle" type="button" data-save-novel-chapter="${escapeHTML(chapter.id)}">保存章节</button><button class="button button-subtle" type="button" data-generate-novel="outline" data-chapter-id="${escapeHTML(chapter.id)}">生成大纲草稿</button><button class="button button-subtle" type="button" data-generate-novel="chapter" data-chapter-id="${escapeHTML(chapter.id)}">生成 / 重生成正文</button><button class="button button-primary" type="button" data-approve-novel-chapter="${escapeHTML(chapter.id)}">确认章节</button></div>
        ${(chapter.revisions || []).length ? `<details class="novel-revisions"><summary>历史版本（${chapter.revisions.length}）</summary><ol>${[...chapter.revisions].reverse().map((revision) => `<li><small>${escapeHTML(recordDate(revision.archived_at))} · ${escapeHTML(revision.reason || "旧版")}</small><p>${escapeHTML(revision.title || "")} · ${escapeHTML(novelChapterStatus(revision.status))}</p><button class="button button-subtle" type="button" data-load-novel-revision="${escapeHTML(revision.revision_id)}" data-chapter-id="${escapeHTML(chapter.id)}">载入历史版本到编辑区</button></li>`).join("")}</ol></details>` : ""}
      </article>`).join("")}</div>` : '<p class="series-panel-empty">还没有章节。先添加章节编号和标题，再填写或生成大纲、正文。</p>'}
    </section>
  </section>`;
  workspace.querySelector("#back-to-novel-library").addEventListener("click", showNovelLibrary);
  workspace.querySelector("#novel-bible-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      await api(`/api/novels/${encodeURIComponent(novel.id)}`, { method: "PUT", body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
      state.novels = await api("/api/novels");
      await openNovelDetail(novel.id);
      showNotice("故事圣经已保存；现有章节不会被覆盖。", false);
    } catch (error) {
      form.querySelector(".series-form-status").textContent = error.message;
      submit.disabled = false;
    }
  });
  workspace.querySelector("#novel-chapter-create").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form).entries());
    payload.chapter_number = Number(payload.chapter_number);
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      await api(`/api/novels/${encodeURIComponent(novel.id)}/chapters`, { method: "POST", body: JSON.stringify(payload) });
      await openNovelDetail(novel.id);
      showNotice("章节已添加。", false);
    } catch (error) {
      form.querySelector(".series-form-status").textContent = error.message;
      submit.disabled = false;
    }
  });
  workspace.querySelectorAll("[data-save-novel-chapter]").forEach((button) => button.addEventListener("click", () => saveNovelChapter(button.dataset.saveNovelChapter)));
  workspace.querySelectorAll("[data-generate-novel]").forEach((button) => button.addEventListener("click", () => generateNovelChapter(button.dataset.chapterId, button.dataset.generateNovel)));
  workspace.querySelectorAll("[data-approve-novel-chapter]").forEach((button) => button.addEventListener("click", () => approveNovelChapter(button.dataset.approveNovelChapter)));
  workspace.querySelectorAll("[data-load-novel-revision]").forEach((button) => button.addEventListener("click", () => loadNovelRevision(button.dataset.chapterId, button.dataset.loadNovelRevision)));
}

async function saveNovelChapter(chapterId) {
  const card = workspace.querySelector(`[data-novel-chapter="${CSS.escape(chapterId)}"]`);
  if (!card || !state.activeNovel) return;
  const payload = {
    title: card.querySelector("[data-novel-title]").value,
    outline: card.querySelector("[data-novel-outline]").value,
    content: card.querySelector("[data-novel-content]").value,
  };
  try {
    await api(`/api/novels/${encodeURIComponent(state.activeNovel.id)}/chapters/${encodeURIComponent(chapterId)}`, { method: "PUT", body: JSON.stringify(payload) });
    await openNovelDetail(state.activeNovel.id);
    showNotice("章节已保存。", false);
    return true;
  } catch (error) {
    showNotice(error.message, true);
    return false;
  }
}

async function generateNovelChapter(chapterId, kind) {
  if (!state.activeNovel) return;
  const label = kind === "outline" ? "章节大纲" : "章节正文";
  const confirmed = await confirmInApp({
    title: `生成${label}草稿？`,
    message: `将使用当前故事圣经、章节信息和较早的已确认正文调用所选文本模型。生成会归档旧版本，不会自动确认；模型使用可能产生费用。`,
    confirmLabel: `生成${label}`,
  });
  if (!confirmed) return;
  const novelId = state.activeNovel.id;
  if (!await saveNovelChapter(chapterId)) return;
  try {
    await api(`/api/novels/${encodeURIComponent(novelId)}/chapters/${encodeURIComponent(chapterId)}/generate`, {
      method: "POST", body: JSON.stringify({ kind, model: state.selectedModel || state.config?.default_model }),
    });
    await openNovelDetail(novelId);
    showNotice(`${label}草稿已生成，尚未确认。`, false);
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function approveNovelChapter(chapterId) {
  if (!state.activeNovel) return;
  const confirmed = await confirmInApp({
    title: "确认本章正文？",
    message: "确认前会先保存当前编辑。确认后，该章节正文可能作为编号更大的后续章节的连续性参考。",
    confirmLabel: "确认章节",
  });
  if (!confirmed) return;
  const novelId = state.activeNovel.id;
  if (!await saveNovelChapter(chapterId)) return;
  try {
    await api(`/api/novels/${encodeURIComponent(novelId)}/chapters/${encodeURIComponent(chapterId)}/approve`, { method: "POST" });
    await openNovelDetail(novelId);
    showNotice("本章已确认；后续生成仅会引用较早的已确认章节。", false);
  } catch (error) {
    showNotice(error.message, true);
  }
}

function loadNovelRevision(chapterId, revisionId) {
  const chapter = state.activeNovel?.chapters?.find((item) => item.id === chapterId);
  const revision = chapter?.revisions?.find((item) => item.revision_id === revisionId);
  const card = workspace.querySelector(`[data-novel-chapter="${CSS.escape(chapterId)}"]`);
  if (!revision || !card) return;
  card.querySelector("[data-novel-title]").value = revision.title || "";
  card.querySelector("[data-novel-outline]").value = revision.outline || "";
  card.querySelector("[data-novel-content]").value = revision.content || "";
  showNotice("历史章节已载入编辑区；保存后才会替换当前内容。", false);
}

async function openProject(projectId) {
  if (!(await ensureCanvasSavedBeforeNavigation())) return;
  try {
    if (state.project?.id !== projectId) clearPendingUploads();
    state.project = await api(`/api/projects/${encodeURIComponent(projectId)}`);
    state.activeSeriesAssets = [];
    if (state.project.series_id) {
      try {
        const linkedSeries = await api(`/api/series/${encodeURIComponent(state.project.series_id)}`);
        state.activeSeriesAssets = Array.isArray(linkedSeries.assets) ? linkedSeries.assets : [];
      } catch (error) {
        console.warn("Unable to load linked series assets", error);
      }
    }
    state.viewStage = state.project.active_stage;
    showNotice("");
    renderProjects();
    await openChat();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function createProject(event) {
  event.preventDefault();
  clearPendingUploads();
  const formData = new FormData(event.currentTarget);
  const payload = Object.fromEntries(formData.entries());
  payload.duration_seconds = Number(payload.duration_seconds);
  payload.model = state.selectedModel || state.config.default_model;
  if (!payload.series_id) {
    delete payload.series_id;
    delete payload.episode_number;
  } else if (payload.episode_number) {
    payload.episode_number = Number(payload.episode_number);
  } else {
    delete payload.episode_number;
  }
  try {
    state.project = await api("/api/projects", { method: "POST", body: JSON.stringify(payload) });
    state.activeSeriesAssets = [];
    if (state.project.series_id) {
      const linkedSeries = await api(`/api/series/${encodeURIComponent(state.project.series_id)}`);
      state.activeSeriesAssets = Array.isArray(linkedSeries.assets) ? linkedSeries.assets : [];
    }
    state.selectedSeriesId = null;
    state.viewStage = state.project.active_stage;
    showNotice("");
    await refreshProjects();
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function requestGenerateStage() {
  const item = state.project.workflow[state.project.active_stage];
  if (item?.content) {
    const regenerate = await confirmInApp({
      title: "重新生成草稿？",
      message: "当前草稿会先归档，再由新结果替换；未保存的编辑会丢失。旧版本可从“历史草稿”载入。",
      confirmLabel: "重新生成",
    });
    if (!regenerate) return;
  }
  generateStage();
}

async function rollbackStage() {
  const stage = state.viewStage;
  if (!stage) return;
  const rollback = await confirmInApp({
    title: `回退到${stageLabel(stage)}？`,
    message: "此步骤将重新开放编辑。后续内容和素材会保留并标记为过期，需要重新审阅或生成。",
    confirmLabel: "回退并修改",
    cancelLabel: "暂不回退",
  });
  if (!rollback) return;
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(state.project.id)}/rollback/${encodeURIComponent(stage)}`, { method: "POST" });
    state.viewStage = stage;
    showNotice(`已回退到${stageLabel(stage)}。后续内容已保留并标记为过期。`, false);
    await refreshProjects();
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function generateStage() {
  const stage = state.project.active_stage;
  const button = document.querySelector("#generate-stage, #regenerate-stage");
  if (button) {
    button.disabled = true;
    button.textContent = "正在准备草稿…";
  }
  showNotice("正在生成内容，请稍候。", false);
  try {
    state.project = await api(`/api/projects/${state.project.id}/generate/${stage}`, {
      method: "POST", body: JSON.stringify({ model: modelSelect.value }),
    });
    state.viewStage = stage;
    const validationError = state.project.workflow?.[stage]?.validation_error;
    showNotice(validationError
      ? "模型结果未通过镜头任务校验；原始输出已保留，请在编辑框修正并保存。"
      : "草稿已生成。审阅或修改后，再确认进入下一步。", Boolean(validationError));
    await refreshProjects();
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
    renderProject();
  }
}

async function manageVideoJob(shotId, action) {
  const projectId = state.project.id;
  if (action === "cancel") {
    const confirmed = await confirmInApp({
      title: "取消本地排队任务",
      message: "只会取消尚未提交给供应商的本地排队任务。正在提交或供应商已受理的任务不能在这里被视为已取消。",
      confirmLabel: "取消排队任务",
      cancelLabel: "继续等待",
    });
    if (!confirmed) return;
  }
  const endpoint = action === "cancel" ? "cancel" : "resume";
  const button = [...workspace.querySelectorAll("[data-shot-action]")]
    .find((item) => item.dataset.shotId === shotId && item.dataset.shotAction === `${endpoint}-video`);
  if (button) button.disabled = true;
  showNotice(action === "cancel" ? `正在取消 ${shotId} 的本地排队任务…` : `正在恢复 ${shotId} 的供应商状态查询…`, false);
  try {
    state.project = await api(
      `/api/projects/${encodeURIComponent(projectId)}/shots/${encodeURIComponent(shotId)}/video/${endpoint}`,
      { method: "POST" },
    );
    await refreshProjects();
    state.viewStage = "video";
    showNotice(action === "cancel" ? "本地排队任务已取消，未提交给供应商。" : "已恢复状态查询；不会重新提交生成任务。", false);
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
    try {
      state.project = await api(`/api/projects/${encodeURIComponent(projectId)}`);
      state.viewStage = "video";
      renderProject();
    } catch {
      if (button) button.disabled = false;
    }
  }
}

async function generateShotAsset(shotId, kind) {
  const projectId = state.project.id;
  const shot = state.project.workflow?.video?.shots?.find((item) => item.shot_id === shotId);
  const confirmUncertain = kind === "video" && shot?.video?.status === "interrupted";
  if (confirmUncertain) {
    const confirmed = await confirmInApp({
      title: "供应商任务状态待核对",
      message: "上次提交没有可靠的任务 ID，任务可能已被供应商受理。请先到供应商控制台核对；只有确认没有仍在处理的任务后，才继续重试，以免重复生成或计费。",
      confirmLabel: "我已核对，仍要重试",
      cancelLabel: "暂不重试",
    });
    if (!confirmed) return;
  }
  const button = [...workspace.querySelectorAll("[data-shot-action]")]
    .find((item) => item.dataset.shotId === shotId && item.dataset.shotAction === kind);
  if (button) {
    button.disabled = true;
    button.textContent = kind === "image" ? "正在生成关键帧…" : "正在提交视频任务…";
  }
  showNotice(`正在提交 ${shotId} ${kind === "image" ? "关键帧" : "视频片段"}任务…`, false);
  try {
    const confirmationQuery = confirmUncertain ? "?confirm_uncertain=true" : "";
    state.project = await api(
      `/api/projects/${encodeURIComponent(projectId)}/shots/${encodeURIComponent(shotId)}/${kind}${confirmationQuery}`,
      { method: "POST" },
    );
    state.viewStage = "video";
    await refreshProjects();
    const updatedShot = state.project.workflow?.video?.shots?.find((item) => item.shot_id === shotId);
    const status = kind === "video" ? updatedShot?.video?.status : updatedShot?.image?.status;
    if (kind === "video" && ["queued", "submitting", "generating"].includes(status)) {
      showNotice(`${shotId} 视频任务已进入后台处理；任务状态会自动更新。`, false);
    } else {
      showNotice(`${shotId} ${kind === "image" ? "关键帧" : "视频片段"}已生成。`, false);
    }
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
    try {
      state.project = await api(`/api/projects/${encodeURIComponent(projectId)}`);
      state.viewStage = "video";
      renderProject();
    } catch {
      if (button) button.disabled = false;
    }
  }
}

async function runStageQualityCheck() {
  if (!state.project || !state.viewStage) return;
  const button = workspace.querySelector("#run-quality-check");
  const results = workspace.querySelector("#quality-results");
  if (button) button.disabled = true;
  if (results) results.textContent = "正在运行启发式检查…";
  try {
    const report = await api(`/api/projects/${encodeURIComponent(state.project.id)}/quality/${encodeURIComponent(state.viewStage)}`, { method: "POST" });
    if (!results) return;
    const findings = Array.isArray(report.findings) ? report.findings : [];
    const narrative = report.narrative_review;
    const narrativeMarkup = narrative?.dimensions?.length
      ? `<section class="narrative-review"><strong>叙事结构线索 · 启发式</strong><ul>${narrative.dimensions.map((item) => `<li class="narrative-${escapeHTML(item.status)}"><strong>${escapeHTML(item.label)}</strong>：${item.status === "signal_found" ? `检出信号${item.evidence ? `（${escapeHTML(item.evidence)}）` : ""}` : "未检出明显信号；请人工确认"}</li>`).join("")}</ul><p>${escapeHTML(narrative.note || "检出信号不代表叙事有效，未检出也不代表缺失。")}</p></section>`
      : "";
    results.innerHTML = `<strong>${escapeHTML(report.grade)} · ${escapeHTML(report.score)}/100 · 仅供参考</strong>${findings.length
      ? `<ul>${findings.map((finding) => `<li class="is-${escapeHTML(finding.severity)}"><strong>${escapeHTML(finding.title)}</strong>：${escapeHTML(finding.message)}</li>`).join("")}</ul>`
      : "<p>未发现明显的格式或占位问题，请继续人工审阅内容质量。</p>"}${narrativeMarkup}`;
  } catch (error) {
    if (results) results.textContent = error.message;
  } finally {
    if (button?.isConnected) button.disabled = false;
  }
}

async function restoreStageRevision(stage, revisionId) {
  if (!state.project || !revisionId) return;
  const confirmed = await confirmInApp({
    title: "恢复历史内容？",
    message: "历史内容将替换当前步骤；当前内容会先归档，后续步骤会标记为过期并需要重新审阅。",
    confirmLabel: "恢复历史版本",
    cancelLabel: "保留当前内容",
  });
  if (!confirmed) return;
  const projectId = state.project.id;
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(projectId)}/content/${encodeURIComponent(stage)}/revisions/${encodeURIComponent(revisionId)}/restore`, { method: "POST" });
    state.viewStage = stage;
    await refreshProjects();
    renderProject();
    showNotice("历史内容已恢复；当前内容已留档，后续步骤已标记为过期。", false);
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function saveContent() {
  const content = document.querySelector("#stage-content").value;
  try {
    state.project = await api(`/api/projects/${state.project.id}/content/${state.project.active_stage}`, {
      method: "PUT", body: JSON.stringify({ content }),
    });
    showNotice("修改已保存。", false);
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function approveStage() {
  const stage = state.project.active_stage;
  const editor = document.querySelector("#stage-content");
  const savedContent = state.project.workflow[stage]?.content || "";
  if (editor && editor.value !== savedContent) {
    showNotice("草稿有未保存的修改，请先保存，再确认继续。", true);
    return;
  }
  try {
    state.project = await api(`/api/projects/${state.project.id}/approve/${stage}`, { method: "POST" });
    state.viewStage = state.project.active_stage;
    showNotice(`${stageLabel(stage)}已确认，可以继续下一步。`, false);
    await refreshProjects();
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function updateModel() {
  state.selectedModel = modelSelect.value;
  if (!state.project) return;
  try {
    state.project = await api(`/api/projects/${state.project.id}/model`, {
      method: "POST", body: JSON.stringify({ model: modelSelect.value }),
    });
    showNotice(`后续文本步骤将使用 ${modelName(state.project.model)}。`, false);
    if (state.viewMode === "workflow") renderProject();
    else renderChatScreen({ preserveDraft: true });
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderChatMessage(item) {
  const isUser = item.role === "user";
  const attachments = (item.attachments || []).map((asset) => {
    const url = asset.preview_url?.startsWith("blob:")
      ? escapeHTML(asset.preview_url)
      : safeMediaUrl(asset.url);
    if (!url) return "";
    if (asset.type === "image") return `<a class="chat-image-link" href="${url}" target="_blank" rel="noreferrer"><img src="${url}" alt="${escapeHTML(asset.filename || "对话图片")}" loading="lazy" /></a>`;
    if (asset.type === "video") return `<video class="chat-video" controls preload="metadata" src="${url}" aria-label="${escapeHTML(asset.filename || "对话视频")}"></video>`;
    if (asset.type === "audio") return `<audio class="chat-audio" controls preload="metadata" src="${url}" aria-label="AI 生成的语音"></audio>`;
    if (asset.type === "file") return `<a class="chat-file-link" href="${url}" download="${escapeHTML(asset.filename || "附件")}"><span aria-hidden="true">▤</span><span><strong>${escapeHTML(asset.filename || "附件")}</strong><small>${formatFileSize(Number(asset.size || 0))}</small></span></a>`;
    return "";
  }).join("");
  const content = isUser
    ? `<p>${escapeHTML(item.content || (item.attachments?.length ? "请查看附件" : ""))}</p>`
    : item.pending && !String(item.content ?? "").trim()
      ? '<div class="chat-thinking" role="status" aria-label="助手正在思考"><i></i><i></i><i></i><span>思考中…</span></div>'
      : `<div class="chat-markdown">${renderMarkdown(item.content)}${item.pending ? '<span class="stream-cursor" aria-hidden="true"></span>' : ""}</div>`;
  const classes = [
    "chat-message",
    isUser ? "chat-message-user" : "chat-message-assistant",
    item.failed ? "is-failed" : "",
  ].filter(Boolean).join(" ");
  return `<article id="chat-message-${escapeHTML(item.id)}" class="${classes}">
    <div class="chat-avatar" aria-hidden="true">${isUser ? "你" : "片"}</div>
    <div class="chat-bubble">${content}${attachments ? `<div class="chat-attachments">${attachments}</div>` : ""}</div>
  </article>`;
}

function renderChat({ preserveScroll = null } = {}) {
  const container = document.querySelector("#chat-messages");
  if (!container) return;
  const messages = state.chatMessages.filter((item) => !item.internal && item.role !== "tool");
  const loadOlder = state.chatHasMore
    ? `<button type="button" id="load-older-chat" class="chat-load-older" ${state.chatLoadingOlder ? "disabled" : ""}>${state.chatLoadingOlder ? "正在加载…" : "加载更早消息"}</button>`
    : "";
  const content = messages.length
    ? messages.map(renderChatMessage).join("")
    : `<div class="chat-empty"><span class="chat-empty-mark" aria-hidden="true">✳</span>
      <h3>从一句话开始</h3><p>聊脚本、改方向，或直接说“生成一张……图片”“帮我生成一个 5 秒短视频”。</p></div>`;
  container.innerHTML = `${loadOlder}${content}`;
  if (preserveScroll) {
    const targetScrollTop = preserveScroll.scrollTop + container.scrollHeight - preserveScroll.scrollHeight;
    const previousBehavior = container.style.scrollBehavior;
    container.style.scrollBehavior = "auto";
    container.scrollTop = targetScrollTop;
    requestAnimationFrame(() => { container.style.scrollBehavior = previousBehavior; });
  } else {
    container.scrollTop = container.scrollHeight;
  }
}

async function loadOlderChatMessages() {
  if (!state.project || !state.chatHasMore || !state.chatHistoryCursor || state.chatLoadingOlder) return;
  const projectId = state.project.id;
  const cursor = state.chatHistoryCursor;
  const container = document.querySelector("#chat-messages");
  const preserveScroll = {
    scrollTop: container.scrollTop,
    scrollHeight: container.scrollHeight,
  };
  const button = document.querySelector("#load-older-chat");
  state.chatLoadingOlder = true;
  if (button) {
    button.disabled = true;
    button.textContent = "正在加载…";
  }
  let loaded = false;
  try {
    const page = await api(
      `/api/projects/${encodeURIComponent(projectId)}/chat/history?limit=10&before=${encodeURIComponent(cursor)}`,
    );
    if (state.project?.id !== projectId || state.viewMode !== "chat") return;
    const existingIds = new Set(state.chatMessages.map((item) => item.id));
    state.chatMessages = [
      ...page.messages.filter((item) => !existingIds.has(item.id)),
      ...state.chatMessages,
    ];
    state.chatHasMore = Boolean(page.has_more);
    state.chatHistoryCursor = page.next_cursor || null;
    loaded = true;
  } catch (error) {
    if (state.project?.id === projectId) showNotice(error.message, true);
  } finally {
    state.chatLoadingOlder = false;
    if (loaded && state.project?.id === projectId && state.viewMode === "chat") {
      renderChat({ preserveScroll });
    } else if (!loaded && state.project?.id === projectId) {
      const currentButton = document.querySelector("#load-older-chat");
      if (currentButton) {
        currentButton.disabled = false;
        currentButton.textContent = "加载更早消息";
      }
    }
  }
}

function mergeChatMessages(existing, incoming) {
  const messagesById = new Map();
  for (const item of existing) {
    if (!String(item.id || "").startsWith("local-")) messagesById.set(item.id, item);
  }
  for (const item of incoming) messagesById.set(item.id, item);
  return [...messagesById.values()].sort((left, right) => {
    const timeOrder = String(left.created_at || "").localeCompare(String(right.created_at || ""));
    return timeOrder || String(left.id || "").localeCompare(String(right.id || ""));
  });
}

function updateStreamingMessage(message) {
  const article = document.getElementById(`chat-message-${message.id}`);
  if (article) article.outerHTML = renderChatMessage(message);
  else renderChat();
  const container = document.querySelector("#chat-messages");
  container.scrollTop = container.scrollHeight;
}

function renderChatScreen({ preserveDraft = false } = {}) {
  const draft = preserveDraft ? document.querySelector("#chat-input")?.value || "" : "";
  const project = state.project;
  const isWorkflowProject = project?.mode !== "chat" && Boolean(project);
  state.viewMode = "chat";
  workspace.classList.add("chat-workspace");
  document.querySelector("#top-title").textContent = project?.title || "新对话";
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = !isWorkflowProject;
  viewToggle.disabled = !isWorkflowProject;
  viewToggle.textContent = "查看工作流";
  syncModelControl();
  const conversationLabel = isWorkflowProject ? "项目对话" : "普通对话";
  workspace.innerHTML = `
    <section class="chat-screen" aria-label="${conversationLabel}">
      ${project ? `<div class="chat-project-tools"><span>${conversationLabel} · 本地副本</span><a href="/api/projects/${encodeURIComponent(project.id)}/export">导出项目副本</a></div>` : ""}
      <div id="chat-messages" class="chat-messages" role="log" aria-label="对话记录" aria-live="polite" aria-relevant="additions text"></div>
      <div class="chat-composer-area">
        <p id="chat-status" class="chat-status" role="status" aria-live="polite"></p>
        <form id="chat-form" class="chat-form">
          <div id="chat-pending-attachments" class="chat-pending-attachments" hidden></div>
          <label class="sr-only" for="chat-input">发送给创作助手</label>
          <textarea id="chat-input" rows="2" maxlength="10000" placeholder="说说你想聊什么，或描述想创作的内容……"></textarea>
          <div class="chat-form-footer">
            <div class="chat-composer-tools">
              <label class="chat-attach-button" for="chat-file-input" aria-label="添加图片、视频或文件">＋ 添加附件</label>
              <input id="chat-file-input" type="file" multiple accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime,video/webm,.m4v,.txt,.md,.csv,.json,.yaml,.yml,.xml,.html,.log,.pdf,.docx" hidden />
              <span>每个附件≤20 MB、单条合计≤40 MB，最多 8 个。视频最多分析 6 帧、不含声音；媒体生成前先确认。</span>
            </div>
            <button class="button button-primary" id="send-chat" type="submit" aria-label="发送消息">发送 <span aria-hidden="true">↗</span></button>
          </div>
        </form>
        <p class="chat-note">Enter 发送 · Shift + Enter 换行</p>
      </div>
    </section>`;
  renderChat();
  renderPendingAttachments();
  document.querySelector("#chat-messages").addEventListener("click", (event) => {
    if (event.target.closest("#load-older-chat")) loadOlderChatMessages();
  });
  document.querySelector("#chat-pending-attachments").addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove-pending]");
    if (!remove) return;
    const [item] = state.pendingUploads.splice(Number(remove.dataset.removePending), 1);
    if (item?.previewUrl) URL.revokeObjectURL(item.previewUrl);
    renderPendingAttachments();
  });
  document.querySelector("#chat-file-input").addEventListener("change", (event) => {
    addPendingUploads(event.target.files);
    event.target.value = "";
  });
  const chatForm = document.querySelector("#chat-form");
  chatForm.addEventListener("submit", sendChat);
  chatForm.addEventListener("dragover", (event) => {
    if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
  });
  chatForm.addEventListener("drop", (event) => {
    if (!event.dataTransfer?.files.length) return;
    event.preventDefault();
    if (!state.chatSending) addPendingUploads(event.dataTransfer.files);
  });
  if (draft) document.querySelector("#chat-input").value = draft;
  document.querySelector("#chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      document.querySelector("#chat-form").requestSubmit();
    }
  });
}

async function openChat() {
  if (!state.project) return;
  try {
    const page = await api(`/api/projects/${encodeURIComponent(state.project.id)}/chat/history?limit=10`);
    state.chatMessages = page.messages || [];
    state.chatHasMore = Boolean(page.has_more);
    state.chatHistoryCursor = page.next_cursor || null;
    state.chatLoadingOlder = false;
    showNotice("");
    renderChatScreen();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function toggleProjectView() {
  if (!state.project) return;
  if (state.viewMode === "canvas") {
    const saved = await saveStoryboardCanvas();
    if (!saved && canvasIsDirty()) return;
    state.viewStage = state.viewStage || state.project.active_stage;
    renderProject();
    return;
  }
  if (state.viewMode === "production-canvas") {
    state.viewStage = state.viewStage || state.project.active_stage;
    renderProject();
    workspace.querySelector("#stage-title")?.focus();
    return;
  }
  if (state.viewMode === "chat") {
    state.viewStage = state.project.active_stage;
    renderProject();
  } else {
    openChat();
  }
}

async function readChatStream(response, onEvent) {
  if (!response.body) throw new Error("浏览器无法读取模型响应流。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let receivedDone = false;
  const dispatchFrame = (frame) => {
    const data = frame.split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) return;
    const event = JSON.parse(data);
    if (event.type === "error") throw new Error(event.message || "对话请求失败。");
    if (event.type === "done") receivedDone = true;
    onEvent(event);
  };
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      dispatchFrame(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
    }
    if (done) break;
  }
  if (buffer.trim()) dispatchFrame(buffer);
  if (!receivedDone) throw new Error("连接已中断，模型回复尚未完成。");
}

async function uploadChatAttachments(projectId, pendingFiles) {
  const newFiles = pendingFiles.filter((item) => !item.storedAttachment);
  if (newFiles.length) {
    const formData = new FormData();
    for (const item of newFiles) formData.append("files", item.file, item.file.name);
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/chat/attachments`, {
      method: "POST", body: formData,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `附件上传失败（${response.status}）`);
    newFiles.forEach((item, index) => { item.storedAttachment = payload[index]; });
  }
  return pendingFiles.map((item) => item.storedAttachment);
}

function releaseUploadPreviews(uploads) {
  for (const item of uploads) {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  }
}

async function sendChat(event) {
  event.preventDefault();
  if (state.chatSending) return;
  const input = document.querySelector("#chat-input");
  const sendButton = document.querySelector("#send-chat");
  const message = input.value.trim();
  const pendingFiles = [...state.pendingUploads];
  if (!message && !pendingFiles.length) return;

  const sendStartedAt = Date.now();
  const localId = `local-${crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
  const userMessage = {
    id: localId,
    role: "user",
    content: message || "请查看附件",
    attachments: pendingFiles.map((item) => ({
      type: item.kind, filename: item.name, size: item.file.size, preview_url: item.previewUrl,
    })),
  };
  const assistantMessage = { id: `${localId}-reply`, role: "assistant", content: "", pending: true };
  state.chatMessages.push(userMessage, assistantMessage);
  state.pendingUploads = [];
  input.value = "";
  renderPendingAttachments();
  renderChat();

  state.chatSending = true;
  sendButton.disabled = true;
  document.querySelector("#chat-file-input").disabled = true;
  input.disabled = true;
  modelSelect.disabled = true;
  showChatStatus("正在准备对话…");
  const model = modelSelect.value || state.selectedModel || state.config.default_model;
  let completed = false;
  let uploadedAttachments = [];
  try {
    if (!state.project) {
      state.project = await api("/api/chat/sessions", {
        method: "POST", body: JSON.stringify({ model }),
      });
      await refreshProjects();
    }
    if (pendingFiles.length) {
      showChatStatus("正在上传附件…");
      uploadedAttachments = await uploadChatAttachments(state.project.id, pendingFiles);
      userMessage.attachments = uploadedAttachments;
      renderChat();
    }
    const response = await fetch(`/api/projects/${encodeURIComponent(state.project.id)}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ message, model, attachment_ids: uploadedAttachments.map((item) => item.id) }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `请求失败（${response.status}）`);
    }
    await readChatStream(response, (streamEvent) => {
      if (streamEvent.type === "status") {
        showChatStatus(streamEvent.message || "正在处理…");
      } else if (streamEvent.type === "delta") {
        assistantMessage.content += streamEvent.text || "";
        updateStreamingMessage(assistantMessage);
      } else if (streamEvent.type === "done") {
        completed = true;
        state.chatMessages = mergeChatMessages(state.chatMessages, streamEvent.messages || []);
        state.project = streamEvent.project || state.project;
        renderChat();
        document.querySelector("#top-title").textContent = state.project.title;
      }
    });
    releaseUploadPreviews(pendingFiles);
    await refreshProjects();
    showChatStatus("");
  } catch (error) {
    showChatStatus(error.message, true);
    try {
      if (state.project) {
        const projectId = state.project.id;
        const page = await api(`/api/projects/${encodeURIComponent(projectId)}/chat/history?limit=10`);
        const history = page.messages || [];
        state.project = await api(`/api/projects/${encodeURIComponent(projectId)}`);
        await refreshProjects();
        const userWasSaved = history.some((item) =>
          item.role === "user"
          && Date.parse(item.created_at || "") >= sendStartedAt - 5000
          && (uploadedAttachments.length
            ? uploadedAttachments.every((attachment) => item.attachments?.some((saved) => saved.id === attachment.id))
            : item.content === (message || "请结合附件和之前的对话理解我的需求；如果目标不明确，先向我确认需要做什么。")),
        );
        state.chatMessages = mergeChatMessages(state.chatMessages, history);
        if (userWasSaved) {
          releaseUploadPreviews(pendingFiles);
          if (!completed) {
            state.chatMessages.push({
              id: `${localId}-error`, role: "assistant", content: `请求未完成：${error.message}`, failed: true,
            });
          }
        } else {
          state.chatMessages = state.chatMessages.filter((item) => item.id !== localId && item.id !== assistantMessage.id);
          state.pendingUploads = pendingFiles;
          renderPendingAttachments();
          if (!input.value.trim()) input.value = message;
        }
        if (!state.chatHasMore) {
          state.chatHasMore = Boolean(page.has_more);
          state.chatHistoryCursor = page.next_cursor || null;
        }
        renderChat();
      } else {
        state.chatMessages = state.chatMessages.filter((item) => item.id !== localId && item.id !== assistantMessage.id);
        state.pendingUploads = pendingFiles;
        renderPendingAttachments();
        if (!input.value.trim()) input.value = message;
        renderChat();
      }
    } catch {
      if (!completed) {
        state.chatMessages = state.chatMessages.filter((item) => item.id !== localId && item.id !== assistantMessage.id);
        state.pendingUploads = pendingFiles;
        renderPendingAttachments();
        if (!input.value.trim()) input.value = message;
        renderChat();
      }
    }
  } finally {
    state.chatSending = false;
    sendButton.disabled = false;
    document.querySelector("#chat-file-input").disabled = false;
    input.disabled = false;
    syncModelControl();
    input.focus();
  }
}

const MODEL_GROUPS = [
  ["text", "文本"], ["image", "图像"], ["video", "视频"], ["audio", "音频"],
];
const MODEL_ADAPTERS = {
  text: [["openai_compatible", "OpenAI-compatible"], ["anthropic", "Anthropic"]],
  image: [["agnes_image", "Agnes Images"], ["openai_images", "OpenAI Images"]],
  video: [["agnes_video", "Agnes Video"]],
  audio: [["openai_audio", "OpenAI Audio"]],
};

function editableProfile(profile) {
  return {
    id: profile.id,
    name: profile.name,
    modality: profile.modality,
    adapter: profile.adapter,
    model_id: profile.model_id,
    base_url: profile.base_url || "",
    params: profile.params || {},
  };
}

function setModelConfigStatus(message, isError = false) {
  const status = document.querySelector("#model-config-status");
  status.textContent = message;
  status.classList.toggle("is-error", isError);
}

function renderModelSettings() {
  const host = document.querySelector("#model-profiles");
  host.innerHTML = MODEL_GROUPS.map(([modality, label]) => {
    const profiles = state.config.model_profiles.filter((profile) => profile.modality === modality);
    const defaultId = state.config.defaults?.[modality] || "";
    return `<section class="model-group"><h3>${label}</h3>${profiles.length ? profiles.map((profile) => {
      const isDefault = defaultId === profile.id;
      const confirmingDelete = state.modelDeletePending === profile.id;
      const onlyTextModel = modality === "text" && profiles.length === 1;
      return `<article class="model-row ${isDefault ? "is-default" : ""}">
        <div class="model-row-main">
          <div class="model-name-line"><strong>${escapeHTML(profile.name)}</strong>${isDefault ? '<span class="default-badge">默认</span>' : ""}</div>
          <small>${escapeHTML(profile.adapter)} · ${escapeHTML(profile.model_id)}</small>
          <small>${escapeHTML(profile.base_url || "Anthropic 官方 SDK")}</small>
        </div>
        <div class="model-row-actions">
          <span class="model-state ${profile.configured ? "is-ready" : "is-missing"}">${profile.configured ? "API Key 已配置" : "需填写 API Key"}</span>
          <button class="model-action" type="button" data-model-edit="${escapeHTML(profile.id)}">编辑</button>
          <button class="model-action" type="button" data-model-default="${escapeHTML(profile.id)}" ${isDefault ? "disabled" : ""}>${isDefault ? "当前默认" : "设为默认"}</button>
          ${confirmingDelete
            ? `<span class="model-delete-prompt">确定删除？</span><button class="model-action is-danger" type="button" data-model-delete-confirm="${escapeHTML(profile.id)}">确认删除</button><button class="model-action" type="button" data-model-delete-cancel>取消</button>`
            : `<button class="model-action is-danger" type="button" data-model-delete="${escapeHTML(profile.id)}" ${onlyTextModel ? 'disabled title="至少保留一个文本模型"' : ""}>删除</button>`}
        </div>
      </article>`;
    }).join("") : '<p class="model-empty">尚未配置此类模型</p>'}</section>`;
  }).join("");
}

function renderAdapterOptions(select, modality, selected) {
  select.innerHTML = (MODEL_ADAPTERS[modality] || []).map(([value, label]) =>
    `<option value="${escapeHTML(value)}" ${value === selected ? "selected" : ""}>${escapeHTML(label)}</option>`).join("");
}

async function openModelEditor(profile = null) {
  const existing = profile ? editableProfile(profile) : null;
  let apiKey = "";
  if (profile) {
    try {
      const credential = await api(`/api/config/models/${encodeURIComponent(profile.id)}/credential`);
      apiKey = credential.api_key || "";
    } catch (error) {
      setModelConfigStatus(error.message, true);
      return;
    }
  }
  const current = existing || {
    id: "", name: "", modality: "text", adapter: "openai_compatible",
    model_id: "", base_url: "", params: {},
  };
  const host = document.querySelector("#model-editor-host");
  host.innerHTML = `<form id="model-editor-form" class="model-editor" aria-labelledby="model-editor-title">
    <div class="model-editor-heading"><div><p class="section-kicker">${existing ? "编辑配置" : "新模型"}</p><h3 id="model-editor-title">${existing ? "编辑模型" : "添加模型"}</h3></div></div>
    <div class="model-form-grid">
      <label for="model-config-id">配置标识<input id="model-config-id" name="id" value="${escapeHTML(current.id)}" required maxlength="80" pattern="[a-z0-9][a-z0-9._-]*" placeholder="例如：agnes-text" ${existing ? "readonly" : ""}></label>
      <label for="model-config-name">显示名称<input id="model-config-name" name="name" value="${escapeHTML(current.name)}" required maxlength="100" placeholder="例如：Agnes 文本模型"></label>
      <label for="model-config-modality">模型类型<select id="model-config-modality" name="modality">${MODEL_GROUPS.map(([value, label]) => `<option value="${value}" ${value === current.modality ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      <label for="model-config-adapter">适配器<select id="model-config-adapter" name="adapter"></select></label>
      <label for="model-config-model-id">上游 model_id<input id="model-config-model-id" name="model_id" value="${escapeHTML(current.model_id)}" required maxlength="200" placeholder="供应商提供的模型 ID"></label>
      <label for="model-config-base-url">Base URL<input id="model-config-base-url" name="base_url" type="url" value="${escapeHTML(current.base_url)}" maxlength="2000" placeholder="https://api.example.com/v1"></label>
      <label class="model-field-wide" for="model-config-api-key">供应商 API Key<div class="model-secret-control"><input id="model-config-api-key" name="api_key" type="text" value="${escapeHTML(apiKey)}" maxlength="4096" placeholder="粘贴真实 API Key" autocomplete="off" spellcheck="false"><button class="model-action" id="toggle-model-key" type="button" aria-pressed="false">隐藏</button></div><small>编辑时会回填已保存的 Key。密钥仅保存在本机受限权限文件中，不会放入模型目录或普通配置接口。</small></label>
      <label class="model-field-wide" for="model-config-params">模型参数（JSON 对象）<textarea id="model-config-params" name="params_json" rows="6" spellcheck="false" required>${escapeHTML(JSON.stringify(current.params, null, 2))}</textarea></label>
    </div>
    <p class="model-editor-hint">适配器决定当前已实现的调用协议。Anthropic 使用官方 SDK；其他适配器请配置 HTTPS Base URL（本机服务可使用 HTTP）。清空 Key 并保存可移除此模型使用的凭据。</p>
    <p id="model-editor-error" class="model-editor-error" role="alert"></p>
    <div class="model-editor-actions"><button class="button button-subtle" id="cancel-model-edit" type="button">取消</button><button class="button button-primary" type="submit">保存模型</button></div>
  </form>`;

  const form = document.querySelector("#model-editor-form");
  const modalitySelect = form.elements.modality;
  const adapterSelect = form.elements.adapter;
  const baseUrlInput = form.elements.base_url;
  const keyInput = form.elements.api_key;
  const keyToggle = document.querySelector("#toggle-model-key");
  renderAdapterOptions(adapterSelect, current.modality, current.adapter);
  const updateBaseUrl = () => {
    const isAnthropic = adapterSelect.value === "anthropic";
    baseUrlInput.disabled = isAnthropic;
    if (isAnthropic) baseUrlInput.value = "";
    baseUrlInput.placeholder = isAnthropic ? "由 Anthropic 官方 SDK 管理" : "https://api.example.com/v1";
  };
  modalitySelect.addEventListener("change", () => {
    renderAdapterOptions(adapterSelect, modalitySelect.value, MODEL_ADAPTERS[modalitySelect.value][0][0]);
    updateBaseUrl();
  });
  adapterSelect.addEventListener("change", updateBaseUrl);
  keyToggle.addEventListener("click", () => {
    const showKey = keyInput.type === "password";
    keyInput.type = showKey ? "text" : "password";
    keyToggle.textContent = showKey ? "隐藏" : "显示";
    keyToggle.setAttribute("aria-pressed", String(showKey));
  });
  updateBaseUrl();
  document.querySelector("#cancel-model-edit").addEventListener("click", () => {
    host.innerHTML = "";
    document.querySelector("#add-model").focus();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = document.querySelector("#model-editor-error");
    error.textContent = "";
    let params;
    try {
      params = JSON.parse(form.elements.params_json.value);
      if (!params || Array.isArray(params) || typeof params !== "object") throw new Error("模型参数必须是 JSON 对象。");
    } catch (parseError) {
      error.textContent = parseError instanceof SyntaxError ? `模型参数 JSON 格式错误：${parseError.message}` : parseError.message;
      form.elements.params_json.focus();
      return;
    }
    const nextProfile = {
      id: form.elements.id.value.trim(),
      name: form.elements.name.value.trim(),
      modality: modalitySelect.value,
      adapter: adapterSelect.value,
      model_id: form.elements.model_id.value.trim(),
      base_url: baseUrlInput.value.trim(),
      api_key: keyInput.value,
      params,
    };
    const profiles = state.config.model_profiles.map(editableProfile);
    const index = profiles.findIndex((item) => item.id === nextProfile.id);
    if (index < 0) profiles.push(nextProfile);
    else profiles[index] = nextProfile;
    const defaults = Object.fromEntries(MODEL_GROUPS.map(([modality]) => [modality, state.config.defaults?.[modality] || ""]));
    try {
      await persistModelCatalog(profiles, defaults);
      host.innerHTML = "";
      setModelConfigStatus("模型配置已保存并立即生效。");
      renderModelSettings();
      focusModelEdit(nextProfile.id);
    } catch (saveError) {
      error.textContent = saveError.message;
    }
  });
  form.elements[existing ? "name" : "id"].focus();
}

function focusModelEdit(id) {
  const button = [...document.querySelectorAll("[data-model-edit]")].find((item) => item.dataset.modelEdit === id);
  (button || document.querySelector("#add-model")).focus();
}

async function persistModelCatalog(models, defaults) {
  state.config = await api("/api/config/models", {
    method: "PUT",
    body: JSON.stringify({ models, defaults }),
  });
  if (!state.project) state.selectedModel = state.config.default_model;
  syncModelControl();
  renderProjects();
  const configured = state.config.configured_modalities;
  const statusText = configured.text
    ? `已配置：文本${configured.image ? " · 图像" : ""}${configured.video ? " · 视频" : ""}${configured.audio ? " · 音频" : ""}`
    : "未配置文本模型 API Key · 请打开模型配置";
  document.querySelector("#provider-status").textContent = statusText;
  document.querySelector(".connection-dot").classList.toggle("is-unconfigured", state.config.provider_mode !== "connected");
}

async function setDefaultModel(id) {
  const profile = state.config.model_profiles.find((item) => item.id === id);
  if (!profile) return;
  const models = state.config.model_profiles.map(editableProfile);
  const defaults = Object.fromEntries(MODEL_GROUPS.map(([modality]) => [modality, state.config.defaults?.[modality] || ""]));
  defaults[profile.modality] = profile.id;
  try {
    await persistModelCatalog(models, defaults);
    setModelConfigStatus(`${profile.name} 已设为${MODEL_GROUPS.find(([modality]) => modality === profile.modality)?.[1]}默认模型。`);
    renderModelSettings();
  } catch (error) {
    setModelConfigStatus(error.message, true);
  }
}

async function deleteModel(id) {
  const profile = state.config.model_profiles.find((item) => item.id === id);
  if (!profile) return;
  const models = state.config.model_profiles.filter((item) => item.id !== id).map(editableProfile);
  const defaults = Object.fromEntries(MODEL_GROUPS.map(([modality]) => [modality, state.config.defaults?.[modality] || ""]));
  if (defaults[profile.modality] === id) {
    defaults[profile.modality] = models.find((item) => item.modality === profile.modality)?.id || "";
  }
  state.modelDeletePending = null;
  try {
    await persistModelCatalog(models, defaults);
    setModelConfigStatus(`${profile.name} 已删除。`);
    renderModelSettings();
  } catch (error) {
    setModelConfigStatus(error.message, true);
    renderModelSettings();
  }
}

function showModelSettings() {
  state.modelDeletePending = null;
  setModelConfigStatus("");
  renderModelSettings();
  if (!modelsDialog.open) modelsDialog.showModal();
  document.querySelector("#add-model").focus();
}

function formatStorageBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderUsageSummary(summary) {
  const host = document.querySelector("#usage-summary");
  if (!host) return;
  const limit = summary.daily_media_limit
    ? `${summary.media_calls_today} / ${summary.daily_media_limit}`
    : `${summary.media_calls_today} / 不限`;
  const remaining = summary.media_calls_remaining_today == null
    ? "不设每日上限"
    : `今日剩余 ${summary.media_calls_remaining_today} 次`;
  const rows = summary.totals || [];
  host.innerHTML = `
    <div class="usage-metrics">
      <div><span>今日媒体调用</span><strong>${escapeHTML(limit)}</strong><small>${escapeHTML(remaining)}</small></div>
      <div><span>本地保留记录</span><strong>${escapeHTML(summary.events?.length || 0)}</strong><small>最多展示最近 100 条</small></div>
    </div>
    ${rows.length ? `<div class="usage-table">${rows.map((row) => `
      <article class="usage-row">
        <div><strong>${escapeHTML(row.operation)}</strong><small>${escapeHTML(row.modality)} · ${escapeHTML(row.model_id)}</small></div>
        <div><strong>${escapeHTML(row.calls)} 次</strong><small>成功 ${escapeHTML(row.succeeded)} · 失败 ${escapeHTML(row.failed)}</small></div>
        <div><strong>${escapeHTML(row.input_tokens + row.output_tokens)} tokens</strong><small>已知费用 ${escapeHTML(Number(row.known_cost || 0).toFixed(4))}（${escapeHTML(row.cost_events)} 条供应商金额）</small></div>
      </article>`).join("")}</div>` : '<p class="usage-empty">尚无调用记录。</p>'}
  `;
}

async function refreshUsageSummary() {
  const host = document.querySelector("#usage-summary");
  if (host) host.textContent = "正在读取本地用量…";
  try {
    renderUsageSummary(await api("/api/usage"));
  } catch (error) {
    if (host) host.textContent = error.message;
  }
}

function renderVideoJobsSummary(data) {
  const host = document.querySelector("#video-jobs-summary");
  if (!host) return;
  const jobs = Array.isArray(data.jobs) ? data.jobs : [];
  const workerStatus = data.worker_running ? "后台工作线程运行中" : "后台工作线程未运行";
  host.innerHTML = `<p>${escapeHTML(workerStatus)} · ${jobs.length} 个需关注任务</p>${jobs.length
    ? `<div class="video-job-list">${jobs.slice().reverse().map((job) => `<article class="video-job-row">
      <strong>${escapeHTML(job.project_title || "未命名项目")} · ${escapeHTML(job.shot_id || "镜头未知")} · ${escapeHTML(job.status || "未知状态")}</strong>
      <small>更新于 ${escapeHTML(recordDate(job.updated_at || job.created_at))}${job.next_poll_at ? ` · 下次查询 ${escapeHTML(recordDate(job.next_poll_at))}` : ""}</small>
      ${job.error ? `<p>${escapeHTML(job.error)}</p>` : ""}</article>`).join("")}</div>`
    : '<p class="usage-empty">目前没有需关注的排队、失败或中断任务。</p>'}`;
}

async function refreshVideoJobsSummary() {
  const host = document.querySelector("#video-jobs-summary");
  if (host) host.textContent = "正在读取本地任务…";
  try {
    renderVideoJobsSummary(await api("/api/video-jobs"));
  } catch (error) {
    if (host) host.textContent = error.message;
  }
}

function selectedCleanupItems() {
  const host = document.querySelector("#storage-cleanup-list");
  if (!host) return { attachment_ids: [], storage_keys: [] };
  return {
    attachment_ids: [...host.querySelectorAll("[data-cleanup-attachment]:checked")].map((item) => item.dataset.cleanupAttachment),
    storage_keys: [...host.querySelectorAll("[data-cleanup-file]:checked")].map((item) => item.dataset.cleanupFile),
  };
}

function updateCleanupButton() {
  const button = document.querySelector("#confirm-storage-cleanup");
  if (!button) return;
  const selected = selectedCleanupItems();
  button.disabled = selected.attachment_ids.length + selected.storage_keys.length === 0;
}

function renderStorageCleanup(preview) {
  storageCleanupPreview = preview;
  const host = document.querySelector("#storage-cleanup-list");
  if (!host) return;
  const attachments = preview.attachments || [];
  const files = preview.files || [];
  if (!attachments.length && !files.length) {
    host.innerHTML = '<p class="usage-empty">没有发现可清理的未引用项目。</p>';
    updateCleanupButton();
    return;
  }
  host.innerHTML = `
    <p class="cleanup-total">${attachments.length} 个未引用附件记录 · ${files.length} 个未引用媒体文件 · ${formatStorageBytes(preview.total_bytes || 0)}</p>
    <div class="cleanup-candidates">
      ${attachments.map((item) => `<label class="cleanup-candidate"><input type="checkbox" data-cleanup-attachment="${escapeHTML(item.id)}" checked /><span><strong>${escapeHTML(item.filename)}</strong><small>附件记录 · ${formatStorageBytes(item.size)} · ${escapeHTML(item.project_id)}</small></span></label>`).join("")}
      ${files.map((item) => `<label class="cleanup-candidate"><input type="checkbox" data-cleanup-file="${escapeHTML(item.storage_key)}" checked /><span><strong>${escapeHTML(item.storage_key)}</strong><small>未引用媒体 · ${formatStorageBytes(item.size)}</small></span></label>`).join("")}
    </div>`;
  updateCleanupButton();
}

async function previewStorageCleanup() {
  const host = document.querySelector("#storage-cleanup-list");
  const status = document.querySelector("#storage-cleanup-status");
  const previewButton = document.querySelector("#preview-storage-cleanup");
  if (previewButton) previewButton.disabled = true;
  if (host) host.textContent = "正在检查本地引用…";
  if (status) status.textContent = "";
  try {
    renderStorageCleanup(await api("/api/storage/cleanup/preview"));
  } catch (error) {
    if (host) host.textContent = error.message;
  } finally {
    if (previewButton) previewButton.disabled = false;
  }
}

async function confirmStorageCleanup() {
  const selected = selectedCleanupItems();
  const count = selected.attachment_ids.length + selected.storage_keys.length;
  if (!count) return;
  const confirmed = await confirmInApp({
    title: "删除未引用的本地文件？",
    message: `将删除 ${selected.attachment_ids.length} 条未引用附件记录和 ${selected.storage_keys.length} 个未引用媒体文件。系统会在删除前再次检查引用；此操作无法撤销。`,
    confirmLabel: "确认删除",
    cancelLabel: "保留文件",
  });
  if (!confirmed) return;
  const button = document.querySelector("#confirm-storage-cleanup");
  if (button) button.disabled = true;
  const status = document.querySelector("#storage-cleanup-status");
  if (status) status.textContent = "正在再次检查引用并清理…";
  try {
    const result = await api("/api/storage/cleanup", {
      method: "POST",
      body: JSON.stringify({ confirm: true, ...selected }),
    });
    await previewStorageCleanup();
    if (status) status.textContent = `已删除 ${result.removed_attachments} 条附件记录、${result.removed_files.length} 个媒体文件（${formatStorageBytes(result.removed_bytes)}）。`;
  } catch (error) {
    if (status) status.textContent = error.message;
    updateCleanupButton();
  }
}

async function showUsageSettings() {
  if (!usageDialog.open) usageDialog.showModal();
  await Promise.all([refreshUsageSummary(), refreshVideoJobsSummary(), previewStorageCleanup()]);
}

function announceBackupExport() {
  const status = document.querySelector("#backup-export-status");
  if (status) status.textContent = "正在生成备份；生成完成后请在浏览器下载列表确认文件。";
}

async function initialize() {
  try {
    let compatibility;
    [state.config, state.projects, state.skills, state.series, state.novels, compatibility] = await Promise.all([
      api("/api/config"), api("/api/projects"), api("/api/skills"), api("/api/series"), api("/api/novels"),
      api("/api/system/status").catch(() => null),
    ]);
    showCompatibilityStatus(compatibility);
    state.skillCount = state.skills.length;
    state.selectedModel = state.config.default_model;
    const configured = state.config.configured_modalities;
    const statusText = configured.text
      ? `已配置：文本${configured.image ? " · 图像" : ""}${configured.video ? " · 视频" : ""}${configured.audio ? " · 音频" : ""}`
      : "未配置文本模型 API Key · 请打开模型配置";
    document.querySelector("#provider-status").textContent = statusText;
    document.querySelector(".connection-dot").classList.toggle("is-unconfigured", state.config.provider_mode !== "connected");
    renderProjects();
    syncModelControl();
    modelSelect.addEventListener("change", updateModel);
    document.querySelector("#view-toggle").addEventListener("click", toggleProjectView);
    document.querySelector("#open-models").addEventListener("click", showModelSettings);
    document.querySelector("#open-usage").addEventListener("click", showUsageSettings);
    document.querySelector("#export-backup").addEventListener("click", announceBackupExport);
    document.querySelector("#preview-storage-cleanup").addEventListener("click", previewStorageCleanup);
    document.querySelector("#confirm-storage-cleanup").addEventListener("click", confirmStorageCleanup);
    document.querySelector("#storage-cleanup-list").addEventListener("change", updateCleanupButton);
    document.querySelector("#refresh-video-jobs").addEventListener("click", refreshVideoJobsSummary);
    document.querySelector("#add-model").addEventListener("click", () => openModelEditor());
    document.querySelector("#model-profiles").addEventListener("click", (event) => {
      const edit = event.target.closest("[data-model-edit]");
      const setDefault = event.target.closest("[data-model-default]");
      const requestDelete = event.target.closest("[data-model-delete]");
      const confirmDelete = event.target.closest("[data-model-delete-confirm]");
      if (edit) {
        const profile = state.config.model_profiles.find((item) => item.id === edit.dataset.modelEdit);
        if (profile) openModelEditor(profile);
      } else if (setDefault) {
        setDefaultModel(setDefault.dataset.modelDefault);
      } else if (requestDelete) {
        state.modelDeletePending = requestDelete.dataset.modelDelete;
        setModelConfigStatus("");
        renderModelSettings();
        [...document.querySelectorAll("[data-model-delete-confirm]")]
          .find((button) => button.dataset.modelDeleteConfirm === state.modelDeletePending)?.focus();
      } else if (confirmDelete) {
        deleteModel(confirmDelete.dataset.modelDeleteConfirm);
      } else if (event.target.closest("[data-model-delete-cancel]")) {
        state.modelDeletePending = null;
        renderModelSettings();
      }
    });
    document.querySelector("#close-models").addEventListener("click", () => modelsDialog.close());
    document.querySelector("#close-usage").addEventListener("click", () => usageDialog.close());
    document.querySelector("#rename-form").addEventListener("submit", saveProjectTitle);
    document.querySelector("#rename-cancel").addEventListener("click", () => renameDialog.close());
    document.querySelector("#open-records")?.addEventListener("click", showProductionRecords);
    document.querySelector("#open-series")?.addEventListener("click", showSeriesLibrary);
    document.querySelector("#open-novels")?.addEventListener("click", showNovelLibrary);
    document.querySelector("#new-project").addEventListener("click", async () => {
      if (!(await ensureCanvasSavedBeforeNavigation())) return;
      state.activeSeriesAssets = [];
      state.selectedSeriesId = null;
      clearPendingUploads();
      state.project = null;
      state.viewStage = null;
      state.chatMessages = [];
      state.chatHasMore = false;
      state.chatHistoryCursor = null;
      state.chatLoadingOlder = false;
      showNotice("");
      renderChatScreen();
      renderProjects();
      document.querySelector("#chat-input")?.focus();
    });
    document.querySelector("#new-workflow").addEventListener("click", async () => {
      if (!(await ensureCanvasSavedBeforeNavigation())) return;
      state.activeSeriesAssets = [];
      state.selectedSeriesId = null;
      clearPendingUploads();
      showNotice("");
      renderWorkflowSetup();
      renderProjects();
    });
    const mostRecent = state.projects.find((project) => !project.archived);
    if (mostRecent) await openProject(mostRecent.id);
    else renderChatScreen();
  } catch (error) {
    showNotice(error.message, true);
    workspace.innerHTML = '<div class="load-error"><h1>工作台暂时无法启动</h1><p>请确认后端服务已运行，然后刷新页面。</p></div>';
  }
}

initialize();
