const state = {
  config: null,
  projects: [],
  project: null,
  viewStage: null,
  selectedModel: null,
  modelDeletePending: null,
  chatMessages: [],
  chatSending: false,
  recordFilter: "all",
  viewMode: "new",
};

const workspace = document.querySelector("#workspace");
const notice = document.querySelector("#notice");
const projectList = document.querySelector("#project-list");
const modelSelect = document.querySelector("#model-select");
const modelsDialog = document.querySelector("#models-dialog");
const confirmDialog = document.querySelector("#confirm-dialog");

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
  if (!response.ok) throw new Error(payload.detail || `请求失败（${response.status}）`);
  return payload;
}

function showNotice(message, isError = false) {
  notice.hidden = !message;
  notice.textContent = message || "";
  notice.classList.toggle("notice-error", isError);
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

function renderProjects() {
  projectList.innerHTML = state.projects.length
    ? state.projects.map((project) => `
      <button class="project-link ${state.project?.id === project.id ? "is-active" : ""}"
        type="button" data-project-id="${escapeHTML(project.id)}">
        <span class="project-symbol" aria-hidden="true">${project.mode === "chat" ? "◌" : "▰"}</span>
        <span class="project-link-copy"><strong>${escapeHTML(project.title)}</strong><small>${project.mode === "chat" ? "普通对话" : escapeHTML(stageLabel(project.active_stage))}</small></span>
      </button>`).join("")
    : '<p class="empty-projects">你的对话和创作流程会出现在这里。</p>';
  projectList.querySelectorAll("[data-project-id]").forEach((button) => {
    button.addEventListener("click", () => openProject(button.dataset.projectId));
  });
}

function syncModelControl() {
  const models = state.config?.model_profiles?.filter((item) => item.modality === "text") || [];
  modelSelect.innerHTML = models.map((model) =>
    `<option value="${escapeHTML(model.id)}">${escapeHTML(model.name)}${model.configured ? "" : "（未配置 Key）"}</option>`).join("");
  modelSelect.disabled = models.length === 0;
  modelSelect.value = state.project?.model || state.selectedModel || state.config?.default_model || "";
}

function renderWorkflowSetup() {
  state.project = null;
  state.viewStage = null;
  state.chatMessages = [];
  state.viewMode = "workflow-setup";
  workspace.classList.remove("chat-workspace");
  document.querySelector("#top-title").textContent = "创作流程";
  const viewToggle = document.querySelector("#view-toggle");
  viewToggle.hidden = true;
  viewToggle.disabled = true;
  syncModelControl();
  workspace.innerHTML = `
    <section class="welcome-panel">
      <div class="welcome-copy">
        <div class="overline"><span class="overline-mark" aria-hidden="true"></span>分阶段创作</div>
        <h1>先定义这次创作。</h1>
        <p>补充业务、受众和内容方向，再逐步生成脚本、提示词、分镜与场景任务。每一步都可以先审阅、编辑，再确认。</p>
      </div>
      <form id="create-form" class="brief-form">
        <div class="form-row">
          <label>你的业务是什么？<input name="business" required maxlength="300" placeholder="例如：为小型咖啡馆提供会员运营工具" /></label>
          <label>这次要介绍什么？<input name="product" required maxlength="300" placeholder="产品、服务或活动名称" /></label>
        </div>
        <label>你希望触达谁？<textarea name="audience" required maxlength="500" rows="2" placeholder="描述目标用户、他们关心的问题或使用场景"></textarea></label>
        <div class="form-row form-row-three">
          <label>发布平台<input name="platform" value="抖音 / Reels / Shorts" maxlength="100" /></label>
          <label>视频时长（秒）<input name="duration_seconds" type="number" min="6" max="180" value="30" required /></label>
          <label>整体风格<input name="style" value="真实、清晰、有记忆点" maxlength="300" /></label>
        </div>
        <div class="form-footer">
          <span class="form-note">创建后进入脚本工作台；媒体生成仍只会在你明确提出时调用已配置的服务。</span>
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

  document.querySelector("#create-form").addEventListener("submit", createProject);
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
  return ({ pending: "待生成", generating: "生成中", ready: "已生成", failed: "生成失败", stale: "需重新生成" })[asset.status] || "待生成";
}

function renderAssetVersions(asset = {}) {
  const versions = Array.isArray(asset.versions) ? asset.versions : [];
  if (!versions.length) return "";
  return `<details class="asset-version-history"><summary>素材版本记录（${versions.length}）</summary><ol>${versions.map((version) => {
    const url = version.asset_url ? safeMediaUrl(version.asset_url) : "";
    return `<li><span>v${escapeHTML(version.version)}</span>${url ? `<a href="${url}" target="_blank" rel="noreferrer">打开素材</a>` : ""}<span>${escapeHTML(recordDate(version.completed_at))}</span><span>${version.cost == null ? "费用未知" : `供应商报告：${escapeHTML(version.cost)}`}</span>${version.task_id ? `<span>任务 ${escapeHTML(version.task_id)}</span>` : ""}</li>`;
  }).join("")}</ol></details>`;
}

function renderVideoShot(shot) {
  const image = shot.image || {};
  const video = shot.video || {};
  const imageUrl = image.asset_url ? safeMediaUrl(image.asset_url) : "";
  const videoUrl = video.asset_url ? safeMediaUrl(video.asset_url) : "";
  const imageBusy = image.status === "generating";
  const videoBusy = video.status === "generating";
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
        ${renderAssetVersions(video)}
        <button class="button button-primary" type="button" data-shot-action="video" data-shot-id="${escapeHTML(shot.shot_id)}" ${videoBusy || !videoConfigured ? "disabled" : ""}>${videoBusy ? "正在生成…" : videoReady ? "重新生成视频片段" : video.status === "failed" ? "重试视频片段" : "生成视频片段"}</button>
        ${!videoConfigured ? '<small class="shot-help">请先配置默认视频模型及 Key。</small>' : ""}
      </section>
    </div>
  </article>`;
}

function renderVideoStage(project) {
  const video = project.workflow?.video || {};
  const shots = Array.isArray(video.shots) ? video.shots : [];
  if (!shots.length) {
    const legacyUrl = video.asset_url ? safeMediaUrl(video.asset_url) : "";
    return legacyUrl
      ? `<div class="video-preview"><video controls preload="metadata" src="${legacyUrl}" aria-label="旧版单段视频"></video><p>这是旧版单段视频记录；新项目会按镜头保存片段和 AI 剪辑方案，不会在本地合并。</p></div>`
      : `<div class="provider-placeholder"><span class="placeholder-icon" aria-hidden="true">▧</span><div><strong>暂无逐镜任务</strong><p>先完成并确认“镜头任务”，再逐镜生成关键帧和视频片段。旧项目没有结构化镜头任务时，不会自动把整份分镜误当成单镜提示词。</p></div></div>`;
  }
  const completed = shots.filter((shot) => shot.video?.status === "ready").length;
  return `<div class="video-production"><section class="edit-plan-card"><div class="scene-plan-heading"><div><p class="section-kicker">AI 剪辑方案</p><h3>镜头片段顺序与声音方向</h3></div><span>${completed} / ${shots.length} 段已生成</span></div>
    ${renderEditPlan(video.edit_plan || {}) || '<p class="record-output-empty">暂无剪辑说明。</p>'}<p class="video-no-merge-note">交付为逐镜片段与剪辑方案，不会自动拼接成一个 MP4。</p></section>
    <div class="production-shot-list">${shots.map(renderVideoShot).join("")}</div></div>`;
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
      <button class="button button-subtle" type="button" data-revision-index="${index}">载入此版本</button></li>`).join("")}</ol></details>`;
}

function renderProject() {
  if (!state.project) return renderChatScreen();
  state.viewMode = "workflow";
  workspace.classList.remove("chat-workspace");
  const project = state.project;
  const activeIndex = state.config.stages.findIndex((stage) => stage.id === project.active_stage);
  const viewStage = state.viewStage || project.active_stage;
  const viewIndex = state.config.stages.findIndex((stage) => stage.id === viewStage);
  const item = project.workflow[viewStage];
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
          ? `<div class="stage-footer-actions"><button class="button button-subtle" id="regenerate-stage" type="button">重新生成草稿</button><button class="button button-primary" id="approve-stage" type="button">确认${escapeHTML(stage.label)}，继续</button></div>`
          : item.status === "stale" && isCurrent
            ? `<button class="button button-primary" id="regenerate-stage" type="button">重新生成草稿</button>`
            : isCurrent
              ? `<button class="button button-primary" id="generate-stage" type="button">生成${escapeHTML(stage.label)}草稿 <span aria-hidden="true">↗</span></button>`
              : "";

  workspace.innerHTML = `
    <div class="project-heading">
      <div><p class="section-kicker">${escapeHTML(project.brief.platform)} · ${escapeHTML(project.brief.duration_seconds)} 秒</p>
        <h1>${escapeHTML(project.title)}</h1><p class="project-description">${escapeHTML(project.brief.business)} · 面向 ${escapeHTML(project.brief.audience)}</p></div>
      <div class="project-state"><span class="state-dot" aria-hidden="true"></span>当前步骤：${escapeHTML(stageLabel(project.active_stage))}</div>
    </div>
    <nav class="stage-rail" aria-label="创作步骤">${stepButtons}</nav>
    <section class="stage-panel" aria-labelledby="stage-title">
      <div class="stage-panel-heading"><div><p class="section-kicker">${escapeHTML(statusLabel(item, viewIndex, activeIndex))}</p>
        <h2 id="stage-title">${escapeHTML(stage.label)}</h2><p>${escapeHTML(stage.purpose)}</p></div><span class="stage-time">${escapeHTML(project.brief.duration_seconds)} 秒目标时长</span></div>
      ${viewStage === "script" ? `<div class="brief-strip"><strong>产品 / 服务</strong><span>${escapeHTML(project.brief.product)}</span><strong>风格</strong><span>${escapeHTML(project.brief.style)}</span></div>` : ""}
      <div class="stage-content">${contentArea}</div>
      <div class="stage-footer"><span class="footer-hint">${isVideo ? "媒体生成按镜头单独触发；费用以供应商实际返回为准，当前不在本地拼接视频。" : "已确认步骤可回退修改；后续内容会保留并标记为过期，重新审阅后再继续。"}</span>${action}</div>
    </section>
    <p class="workspace-footnote">文本模型：${escapeHTML(modelName(project.model))} <span aria-hidden="true">·</span> 本地技能：${state.skillCount ?? 0} 个</p>`;

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
    button.addEventListener("click", () => generateShotAsset(button.dataset.shotId, button.dataset.shotAction));
  });
  document.querySelector("#approve-stage")?.addEventListener("click", approveStage);
  document.querySelector("#save-content")?.addEventListener("click", saveContent);
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
    const searchText = [project.title, brief.product, brief.business, brief.platform, brief.audience].filter(Boolean).join(" ");
    return `<article class="record-card" data-record-card data-complete="${status.complete}" data-search-text="${escapeHTML(searchText)}">
      <header class="record-card-heading">
        <div><p class="section-kicker">${escapeHTML(brief.platform || "创作流程")} · ${escapeHTML(brief.duration_seconds || "—")} 秒</p>
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
      <footer class="record-card-footer"><span>最近更新 ${escapeHTML(recordDate(project.updated_at))}</span>
        <button class="record-open-button" type="button" data-open-record="${escapeHTML(project.id)}">查看产物 <span aria-hidden="true">↗</span></button>
      </footer>
    </article>`;
  }).join("");

  workspace.innerHTML = `<section class="records-screen" aria-labelledby="records-title">
    <header class="records-heading"><div><p class="section-kicker">作品库</p><h1 id="records-title">制作记录</h1>
      <p>每次创作都会保留脚本、逐镜任务、视频片段和 AI 剪辑方案，随时回看或继续制作。</p></div>
      <button class="button button-primary" id="records-new-workflow" type="button">新建创作流程 <span aria-hidden="true">↗</span></button>
    </header>
    ${records.length ? `<div class="record-toolbar">
      <label class="record-search" for="record-search"><span class="sr-only">搜索制作记录</span><span aria-hidden="true">⌕</span><input id="record-search" type="search" placeholder="搜索项目名称、产品或业务" /></label>
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
        <p>创建创作流程后，确认过的脚本、分镜和生成的成片都会保存在这里。</p>
        <button class="button button-primary" id="records-new-workflow" type="button">创建第一个项目 <span aria-hidden="true">↗</span></button></div>`}
  </section>`;

  workspace.querySelectorAll("[data-open-record]").forEach((button) => {
    button.addEventListener("click", () => openProductionRecord(button.dataset.openRecord));
  });
  workspace.querySelector("#records-new-workflow")?.addEventListener("click", () => {
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
  const editPlan = renderEditPlan(video.edit_plan || project.workflow?.scenes?.edit_plan || {});
  const editPlanContent = editPlan ? `<section class="record-edit-plan"><h3>AI 剪辑方案（未合并）</h3>${editPlan}</section>` : "";

  workspace.innerHTML = `<section class="record-detail" aria-labelledby="record-detail-title">
    <div class="record-detail-nav"><button class="record-back-button" id="back-to-records" type="button"><span aria-hidden="true">←</span> 返回制作记录</button>
      <button class="button button-subtle" id="continue-production" type="button">打开创作流程</button></div>
    <header class="record-detail-heading"><div><p class="section-kicker">${escapeHTML(brief.platform || "创作流程")} · ${escapeHTML(brief.duration_seconds || "—")} 秒 · 创建于 ${escapeHTML(recordDate(project.created_at))}</p>
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
    </div>
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
}

async function openProductionRecord(projectId) {
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
  state.project = null;
  state.viewStage = null;
  state.chatMessages = [];
  showNotice("");
  renderProjects();
  renderProductionRecords();
}

async function refreshProjects() {
  state.projects = await api("/api/projects");
  renderProjects();
}

async function openProject(projectId) {
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(projectId)}`);
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
  const formData = new FormData(event.currentTarget);
  const payload = Object.fromEntries(formData.entries());
  payload.duration_seconds = Number(payload.duration_seconds);
  payload.model = state.selectedModel || state.config.default_model;
  try {
    state.project = await api("/api/projects", { method: "POST", body: JSON.stringify(payload) });
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
    showNotice("草稿已生成。审阅或修改后，再确认进入下一步。", false);
    await refreshProjects();
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
    renderProject();
  }
}

async function generateShotAsset(shotId, kind) {
  const button = [...workspace.querySelectorAll("[data-shot-action]")]
    .find((item) => item.dataset.shotId === shotId && item.dataset.shotAction === kind);
  if (button) {
    button.disabled = true;
    button.textContent = kind === "image" ? "正在生成关键帧…" : "正在生成视频片段…";
  }
  showNotice(`正在生成 ${shotId} ${kind === "image" ? "关键帧" : "视频片段"}，请稍候。`, false);
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(state.project.id)}/shots/${encodeURIComponent(shotId)}/${kind}`, { method: "POST" });
    state.viewStage = "video";
    await refreshProjects();
    showNotice(`${shotId} ${kind === "image" ? "关键帧" : "视频片段"}已生成。`, false);
    renderProject();
  } catch (error) {
    showNotice(error.message, true);
    try {
      state.project = await api(`/api/projects/${encodeURIComponent(state.project.id)}`);
      state.viewStage = "video";
      renderProject();
    } catch {
      if (button) button.disabled = false;
    }
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
    const url = safeMediaUrl(asset.url);
    if (!url) return "";
    if (asset.type === "image") return `<a class="chat-image-link" href="${url}" target="_blank" rel="noreferrer"><img src="${url}" alt="AI 生成的图片" loading="lazy" /></a>`;
    if (asset.type === "video") return `<video class="chat-video" controls preload="metadata" src="${url}" aria-label="AI 生成的视频"></video>`;
    if (asset.type === "audio") return `<audio class="chat-audio" controls preload="metadata" src="${url}" aria-label="AI 生成的语音"></audio>`;
    return "";
  }).join("");
  const content = isUser
    ? `<p>${escapeHTML(item.content)}</p>`
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

function renderChat() {
  const container = document.querySelector("#chat-messages");
  const messages = state.chatMessages.filter((item) => !item.internal && item.role !== "tool");
  if (!messages.length) {
    container.innerHTML = `<div class="chat-empty"><span class="chat-empty-mark" aria-hidden="true">✳</span>
      <h3>从一句话开始</h3><p>聊脚本、改方向，或直接说“生成一张……图片”“帮我生成一个 5 秒短视频”。</p></div>`;
  } else {
    container.innerHTML = messages.map(renderChatMessage).join("");
  }
  container.scrollTop = container.scrollHeight;
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
      <div id="chat-messages" class="chat-messages" role="log" aria-label="对话记录" aria-live="polite" aria-relevant="additions text"></div>
      <div class="chat-composer-area">
        <p id="chat-status" class="chat-status" role="status" aria-live="polite"></p>
        <form id="chat-form" class="chat-form">
          <label class="sr-only" for="chat-input">发送给创作助手</label>
          <textarea id="chat-input" rows="2" maxlength="10000" required placeholder="说说你想聊什么，或描述想创作的内容……"></textarea>
          <div class="chat-form-footer">
            <span>明确要求生成图片、视频或语音时，才会调用对应模型。</span>
            <button class="button button-primary" id="send-chat" type="submit" aria-label="发送消息">发送 <span aria-hidden="true">↗</span></button>
          </div>
        </form>
        <p class="chat-note">Enter 发送 · Shift + Enter 换行</p>
      </div>
    </section>`;
  renderChat();
  if (draft) document.querySelector("#chat-input").value = draft;
  document.querySelector("#chat-form").addEventListener("submit", sendChat);
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
    state.chatMessages = await api(`/api/projects/${state.project.id}/chat`);
    showNotice("");
    renderChatScreen();
  } catch (error) {
    showNotice(error.message, true);
  }
}

function toggleProjectView() {
  if (!state.project) return;
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

async function sendChat(event) {
  event.preventDefault();
  if (state.chatSending) return;
  const input = document.querySelector("#chat-input");
  const sendButton = document.querySelector("#send-chat");
  const status = document.querySelector("#chat-status");
  const message = input.value.trim();
  if (!message) return;

  const previousUserCount = state.chatMessages.filter((item) => item.role === "user").length;
  const localId = `local-${crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
  const userMessage = { id: localId, role: "user", content: message };
  const assistantMessage = { id: `${localId}-reply`, role: "assistant", content: "", pending: true };
  state.chatMessages.push(userMessage, assistantMessage);
  input.value = "";
  renderChat();

  state.chatSending = true;
  sendButton.disabled = true;
  modelSelect.disabled = true;
  status.textContent = "正在连接模型…";
  const model = modelSelect.value || state.selectedModel || state.config.default_model;
  let completed = false;
  try {
    if (!state.project) {
      state.project = await api("/api/chat/sessions", {
        method: "POST", body: JSON.stringify({ model }),
      });
      await refreshProjects();
    }
    const response = await fetch(`/api/projects/${encodeURIComponent(state.project.id)}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ message, model }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `请求失败（${response.status}）`);
    }
    await readChatStream(response, (streamEvent) => {
      if (streamEvent.type === "status") {
        status.textContent = streamEvent.message || "正在处理…";
      } else if (streamEvent.type === "delta") {
        assistantMessage.content += streamEvent.text || "";
        updateStreamingMessage(assistantMessage);
      } else if (streamEvent.type === "done") {
        completed = true;
        state.chatMessages = streamEvent.messages || [];
        state.project = streamEvent.project || state.project;
        renderChat();
        document.querySelector("#top-title").textContent = state.project.title;
      }
    });
    await refreshProjects();
    status.textContent = "";
  } catch (error) {
    status.textContent = error.message;
    try {
      if (state.project) {
        const history = await api(`/api/projects/${encodeURIComponent(state.project.id)}/chat`);
        state.project = await api(`/api/projects/${encodeURIComponent(state.project.id)}`);
        await refreshProjects();
        const userCount = history.filter((item) => item.role === "user").length;
        if (userCount > previousUserCount) {
          state.chatMessages = [...history, {
            id: `${localId}-error`, role: "assistant", content: `请求未完成：${error.message}`, failed: true,
          }];
        } else {
          state.chatMessages = history;
          if (!input.value.trim()) input.value = message;
        }
        renderChat();
      } else {
        state.chatMessages = state.chatMessages.filter((item) => item.id !== localId && item.id !== assistantMessage.id);
        if (!input.value.trim()) input.value = message;
        renderChat();
      }
    } catch {
      if (!completed) {
        state.chatMessages = state.chatMessages.filter((item) => item.id !== localId && item.id !== assistantMessage.id);
        if (!input.value.trim()) input.value = message;
        renderChat();
      }
    }
  } finally {
    state.chatSending = false;
    sendButton.disabled = false;
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

async function initialize() {
  try {
    [state.config, state.projects, state.skills] = await Promise.all([
      api("/api/config"), api("/api/projects"), api("/api/skills"),
    ]);
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
    document.querySelector("#open-records")?.addEventListener("click", showProductionRecords);
    document.querySelector("#new-project").addEventListener("click", () => {
      state.project = null;
      state.viewStage = null;
      state.chatMessages = [];
      showNotice("");
      renderChatScreen();
      renderProjects();
      document.querySelector("#chat-input")?.focus();
    });
    document.querySelector("#new-workflow").addEventListener("click", () => {
      showNotice("");
      renderWorkflowSetup();
      renderProjects();
    });
    if (state.projects.length) await openProject(state.projects[0].id);
    else renderChatScreen();
  } catch (error) {
    showNotice(error.message, true);
    workspace.innerHTML = '<div class="load-error"><h1>工作台暂时无法启动</h1><p>请确认后端服务已运行，然后刷新页面。</p></div>';
  }
}

initialize();
