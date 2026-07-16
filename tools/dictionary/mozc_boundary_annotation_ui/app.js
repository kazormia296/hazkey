"use strict";

const state = {
  token: new URLSearchParams(window.location.search).get("token")
    || window.sessionStorage.getItem("mozc-annotation-token")
    || "",
  meta: null,
  cases: [],
  detail: null,
  draft: null,
  currentId: null,
  activePathIndex: 0,
  activePathIdByCase: new Map(),
  dirty: false,
  saving: false,
  pendingAdvance: null,
  autosaveTimer: null,
  filterTimer: null,
  caseOpenedAt: 0,
  boundaryEdits: 0,
  splitChunkIndex: null,
  pathInputError: null,
  editGeneration: 0,
  caseRequestGeneration: 0,
  listRequestGeneration: 0,
  selectedCaseIds: new Set(),
  proposalQueue: null,
  proposalJobsByCase: new Map(),
  activeProposalJobsByCase: new Map(),
  proposalProgressJobIds: new Set(),
  proposalQueueError: null,
  proposalRefreshError: null,
  proposalQueueLoading: false,
  proposalQueueInitialized: false,
  proposalQueueRequestGeneration: 0,
  proposalQueueTimer: null,
  proposalEnqueueing: false,
  proposalsStaleForReading: false,
  proposalStaleMessage: null,
  readingChangeSavePending: false,
  llmSettingsInitialized: false,
  llmSettingsDirty: false,
  llmSettingsSaving: false,
  llmSettingsRevision: 0,
  llmCatalog: null,
  llmCatalogLoading: false,
  llmCatalogError: null,
  llmCatalogNotice: null,
  llmCatalogRequestGeneration: 0,
};

if (state.token) {
  window.sessionStorage.setItem("mozc-annotation-token", state.token);
  const cleanUrl = new URL(window.location.href);
  if (cleanUrl.searchParams.has("token")) {
    cleanUrl.searchParams.delete("token");
    window.history.replaceState(null, "", cleanUrl);
  }
}

const $ = (id) => document.getElementById(id);

function setTextContent(target, value) {
  if (target.textContent !== value) target.textContent = value;
}

const dom = {
  progressLabel: $("progress-label"),
  progressCount: $("progress-count"),
  progressBar: $("progress-bar"),
  connectionStatus: $("connection-status"),
  saveStatus: $("save-status"),
  alert: $("global-alert"),
  alertTitle: $("global-alert-title"),
  alertMessage: $("global-alert-message"),
  reloadConflict: $("reload-conflict"),
  search: $("search-input"),
  statusFilter: $("status-filter"),
  categoryFilter: $("category-filter"),
  longOnly: $("long-only"),
  adjudicationOnly: $("adjudication-only"),
  queueCount: $("queue-count"),
  selectVisibleProposals: $("select-visible-proposals"),
  clearProposalSelection: $("clear-proposal-selection"),
  queueSelectedProposals: $("queue-selected-proposals"),
  proposalSelectionCount: $("proposal-selection-count"),
  proposalQueueCounts: $("proposal-queue-counts"),
  proposalQueueProgress: $("proposal-queue-progress"),
  proposalQueueStatus: $("proposal-queue-status"),
  proposalQueueError: $("proposal-queue-error"),
  caseList: $("case-list"),
  emptyQueue: $("empty-queue"),
  editorLoading: $("editor-loading"),
  editorEmpty: $("editor-empty"),
  editorContent: $("editor-content"),
  previousCase: $("previous-case"),
  nextCase: $("next-case"),
  casePosition: $("case-position"),
  caseCategory: $("case-category"),
  caseId: $("case-id"),
  caseRevision: $("case-revision"),
  pathSetStatus: $("path-set-status"),
  needsAdjudication: $("needs-adjudication"),
  readingLength: $("reading-length"),
  sourceReading: $("source-reading"),
  editCorrectedReading: $("edit-corrected-reading"),
  correctedReadingSummary: $("corrected-reading-summary"),
  correctedReadingDisplay: $("corrected-reading-display"),
  correctedReadingEditor: $("corrected-reading-editor"),
  correctedReadingInput: $("corrected-reading-input"),
  correctedReadingError: $("corrected-reading-error"),
  resetCorrectedReading: $("reset-corrected-reading"),
  surfaceReferences: $("surface-references"),
  linderaConfidence: $("lindera-confidence"),
  linderaMarked: $("lindera-marked-reading"),
  linderaAmbiguity: $("lindera-ambiguity"),
  linderaTokens: $("lindera-tokens"),
  linderaNonapplicable: $("lindera-nonapplicable"),
  requestProposals: $("request-proposals"),
  currentProposalJobStatus: $("current-proposal-job-status"),
  llmSettings: $("llm-settings"),
  llmModel: $("llm-model"),
  llmModelCustomField: $("llm-model-custom-field"),
  llmModelCustom: $("llm-model-custom"),
  llmEffort: $("llm-effort"),
  llmEffortCustomField: $("llm-effort-custom-field"),
  llmEffortCustom: $("llm-effort-custom"),
  refreshLlmCatalog: $("refresh-llm-catalog"),
  llmCatalogStatus: $("llm-catalog-status"),
  llmCatalogError: $("llm-catalog-error"),
  saveLlmSettings: $("save-llm-settings"),
  llmSettingsStatus: $("llm-settings-status"),
  llmSettingsError: $("llm-settings-error"),
  proposalList: $("proposal-list"),
  proposalEmpty: $("proposal-empty"),
  proposalNonapplicable: $("proposal-nonapplicable"),
  pathTabs: $("path-tabs"),
  pathEmpty: $("path-empty"),
  pathEditor: $("path-editor"),
  surfaceSelect: $("path-surface-reference"),
  pathStatus: $("path-status"),
  alignmentStatus: $("path-alignment-status"),
  pathValidation: $("path-validation"),
  readingOnlyEditor: $("reading-only-editor"),
  readingGapEditor: $("reading-gap-editor"),
  markedReadingInput: $("marked-reading-input"),
  alignedEditor: $("aligned-editor"),
  alignedChunkList: $("aligned-chunk-list"),
  notes: $("case-notes"),
};

const CODEX_MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/u;
const CODEX_EFFORT = /^[A-Za-z0-9_-]{1,32}$/u;
const CUSTOM_LLM_VALUE = "<custom>";
const FALLBACK_LLM_EFFORTS = ["low", "medium", "high"];

function deepCopy(value) {
  return JSON.parse(JSON.stringify(value));
}

function codePoints(value) {
  return Array.from(value || "");
}

function codePointLength(value) {
  return codePoints(value).length;
}

function codePointSlice(value, start, end) {
  return codePoints(value).slice(start, end).join("");
}

function originalReading() {
  if (!state.detail) return "";
  return state.detail.case.reading
    || state.detail.case.original_reading
    || "";
}

function effectiveReading() {
  if (state.draft && Object.prototype.hasOwnProperty.call(state.draft, "corrected_reading")) {
    const corrected = state.draft.corrected_reading;
    return typeof corrected === "string" && corrected.length
      ? corrected
      : originalReading();
  }
  return state.detail?.case.annotation_reading || originalReading();
}

function hasCorrectedReading() {
  return effectiveReading() !== originalReading();
}

function renderReadingChangeLock() {
  const locked = state.readingChangeSavePending;
  for (const id of [
    "add-path",
    "create-first-path",
    "duplicate-path",
    "delete-path",
    "path-surface-reference",
    "path-status",
    "marked-reading-input",
    "start-surface-alignment",
    "return-reading-only",
  ]) {
    const control = $(id);
    if (control) control.disabled = locked;
  }
  dom.pathSetStatus.disabled = locked;
  dom.needsAdjudication.disabled = locked;
  dom.pathEditor.setAttribute("aria-busy", locked ? "true" : "false");
  dom.requestProposals.closest("section")?.setAttribute(
    "aria-busy",
    locked ? "true" : "false",
  );
  if (state.meta) renderLlmControls();
}

function ensureReadingChangeSaved() {
  if (!state.readingChangeSavePending) return true;
  toast("読み修正の保存完了後に編集できます");
  return false;
}

function element(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clear(node) {
  node.replaceChildren();
}

function setStatusPill(node, text, tone = "neutral") {
  node.textContent = text;
  node.className = `status-pill ${tone}`;
}

function showAlert(title, message, conflict = false) {
  dom.alertTitle.textContent = title;
  dom.alertMessage.textContent = message;
  dom.reloadConflict.hidden = !conflict;
  dom.alert.hidden = false;
}

function hideAlert() {
  dom.alert.hidden = true;
  dom.reloadConflict.hidden = true;
}

function toast(message) {
  const node = element("div", "toast", message);
  $("toast-region").append(node);
  window.setTimeout(() => node.remove(), 2600);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Annotation-Token", state.token);
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  let response;
  try {
    response = await fetch(path, {...options, headers});
  } catch (error) {
    setStatusPill(dom.connectionStatus, "接続エラー", "danger");
    throw error;
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload.error) message = payload.error;
    } catch (_error) {
      // Preserve the HTTP status when an error body is not JSON.
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  setStatusPill(dom.connectionStatus, "ローカル接続", "success");
  return response;
}

async function apiJson(path, options = {}) {
  const response = await api(path, options);
  return response.json();
}

function markedFromBoundaries(text, boundaries) {
  const chunks = [];
  let start = 0;
  for (const boundary of boundaries) {
    chunks.push(codePointSlice(text, start, boundary));
    start = boundary;
  }
  chunks.push(codePointSlice(text, start));
  return chunks.join("|");
}

function parseMarkedReading(marked, reading) {
  const chunks = marked.split("|");
  if (chunks.some((chunk) => !chunk)) {
    throw new Error("先頭・末尾・連続した | は使えません。");
  }
  if (chunks.join("") !== reading) {
    throw new Error("| 以外の読み文字は変更できません。");
  }
  const boundaries = [];
  let offset = 0;
  for (const chunk of chunks.slice(0, -1)) {
    offset += codePointLength(chunk);
    boundaries.push(offset);
  }
  return boundaries;
}

function currentPath() {
  if (!state.draft || !state.draft.acceptable_paths.length) {
    if (state.currentId) state.activePathIdByCase.delete(state.currentId);
    return null;
  }
  state.activePathIndex = Math.max(
    0,
    Math.min(state.activePathIndex, state.draft.acceptable_paths.length - 1),
  );
  const path = state.draft.acceptable_paths[state.activePathIndex];
  if (state.currentId) {
    state.activePathIdByCase.set(state.currentId, path.path_id);
  }
  return path;
}

function restoredPathIndex(caseId, paths) {
  const rememberedPathId = state.activePathIdByCase.get(caseId);
  if (!rememberedPathId) return 0;
  const rememberedIndex = paths.findIndex(
    (path) => path.path_id === rememberedPathId,
  );
  if (rememberedIndex >= 0) return rememberedIndex;
  state.activePathIdByCase.delete(caseId);
  return 0;
}

function reconcileSavedDraft(savedReview) {
  const savedDraft = deepCopy(savedReview);
  const currentPaths = state.draft?.acceptable_paths;
  const savedPaths = savedDraft.acceptable_paths;
  const canPreservePathObjects = (
    Array.isArray(currentPaths)
    && Array.isArray(savedPaths)
    && currentPaths.length === savedPaths.length
    && savedPaths.every(
      (savedPath, index) => currentPaths[index]?.path_id === savedPath.path_id,
    )
  );
  if (!canPreservePathObjects) {
    state.draft = savedDraft;
    return false;
  }

  savedDraft.acceptable_paths = savedPaths.map((savedPath, index) => {
    const current = currentPaths[index];
    for (const key of Object.keys(current)) {
      if (!Object.prototype.hasOwnProperty.call(savedPath, key)) {
        delete current[key];
      }
    }
    Object.assign(current, savedPath);
    return current;
  });
  state.draft = savedDraft;
  return true;
}

function currentSurface(path = currentPath()) {
  if (!path || !state.detail) return null;
  return state.detail.case.surface_references.find(
    (surface) => surface.id === path.surface_reference_id,
  ) || null;
}

function validateDraft() {
  if (!state.draft) return "レビューが読み込まれていません。";
  if (state.pathInputError) return state.pathInputError;
  const readingText = effectiveReading();
  if (!readingText) return "アノテーションに使う読みが空です。";
  if (readingText.includes("|")) return "読み自体に | は使えません。";
  const paths = state.draft.acceptable_paths;
  const ids = new Set();
  const semantic = new Set();
  for (const path of paths) {
    if (!path.path_id || ids.has(path.path_id)) return "経路IDが重複しています。";
    ids.add(path.path_id);
    const readingLength = codePointLength(readingText);
    const reading = path.reading_boundaries || [];
    if (reading.some((value, index) => (
      !Number.isInteger(value)
      || value <= 0
      || value >= readingLength
      || (index > 0 && reading[index - 1] >= value)
    ))) return "読み境界が昇順の内部位置になっていません。";
    const surface = state.detail.case.surface_references.find(
      (item) => item.id === path.surface_reference_id,
    );
    if (!surface) return "対応する表層が見つかりません。";
    if (path.alignment_status === "aligned") {
      const output = path.surface_boundaries;
      if (!Array.isArray(output) || output.length !== reading.length) {
        return "aligned経路の読みと表層の境界数が一致しません。";
      }
      const surfaceLength = codePointLength(surface.text);
      if (output.some((value, index) => (
        !Number.isInteger(value)
        || value <= 0
        || value >= surfaceLength
        || (index > 0 && output[index - 1] >= value)
      ))) return "表層境界が昇順の内部位置になっていません。";
    } else if (path.surface_boundaries !== null) {
      return "読みのみの経路に表層境界が残っています。";
    }
    if (path.status === "acceptable") {
      const key = JSON.stringify([
        path.surface_reference_id,
        reading,
        path.surface_boundaries,
      ]);
      if (semantic.has(key)) return "同じ許容経路が重複しています。";
      semantic.add(key);
    }
  }
  if (["open", "closed"].includes(state.draft.path_set_status) && !semantic.size) {
    return "作業中・完了には少なくとも1つの「許容する」経路が必要です。";
  }
  if (state.draft.path_set_status === "closed" && state.draft.needs_adjudication) {
    return "要裁定のまま完了にはできません。";
  }
  if (state.draft.path_set_status === "invalid" && paths.length) {
    return "無効入力には経路を残せません。";
  }
  return null;
}

function showPathValidation(message) {
  dom.pathValidation.textContent = message || "";
  dom.pathValidation.hidden = !message;
}

function markDirty(action = "edit") {
  if (!state.detail) return;
  state.dirty = true;
  state.editGeneration += 1;
  if (action.includes("boundary") || action.includes("split") || action.includes("merge")) {
    state.boundaryEdits += 1;
  }
  setStatusPill(dom.saveStatus, "未保存", "warning");
  window.clearTimeout(state.autosaveTimer);
  state.autosaveTimer = window.setTimeout(() => {
    saveCurrent({advance: false, actionType: "autosave", quiet: true});
  }, 1200);
}

function fillCategoryFilter() {
  const selected = dom.categoryFilter.value;
  const options = [new Option("すべて", "")];
  for (const [category, count] of Object.entries(state.meta.categories)) {
    options.push(new Option(`${category} (${count})`, category));
  }
  dom.categoryFilter.replaceChildren(...options);
  dom.categoryFilter.value = selected;
}

function applyMeta(nextMeta) {
  const currentRevision = Number(state.meta?.llm?.settings_revision);
  const nextRevision = Number(nextMeta?.llm?.settings_revision);
  if (
    Number.isInteger(currentRevision)
    && Number.isInteger(nextRevision)
    && nextRevision < currentRevision
  ) {
    nextMeta.llm = state.meta.llm;
  }
  state.meta = nextMeta;
}

function currentLlmModelValue() {
  if (dom.llmModel.value === CUSTOM_LLM_VALUE) {
    return dom.llmModelCustom.value.trim().normalize("NFC") || null;
  }
  return dom.llmModel.value.trim().normalize("NFC") || null;
}

function currentLlmEffortValue() {
  if (dom.llmEffort.value === CUSTOM_LLM_VALUE) {
    return dom.llmEffortCustom.value.trim().normalize("NFC");
  }
  return dom.llmEffort.value.trim().normalize("NFC");
}

function normalizedLlmCatalog(payload) {
  if (!payload || typeof payload !== "object" || !Array.isArray(payload.models)) {
    throw new Error("Codex App Serverのモデル一覧が不正です。");
  }
  const models = payload.models.map((entry, index) => {
    if (!entry || typeof entry !== "object" || !CODEX_MODEL_ID.test(entry.model || "")) {
      throw new Error(`モデル一覧の${index + 1}件目に不正なモデルIDがあります。`);
    }
    if (!Array.isArray(entry.supported_reasoning_efforts)) {
      throw new Error(`モデル「${entry.model}」のエフォート一覧が不正です。`);
    }
    const efforts = entry.supported_reasoning_efforts.map((item) => {
      const effort = item?.reasoning_effort;
      if (!CODEX_EFFORT.test(effort || "")) {
        throw new Error(`モデル「${entry.model}」に不正なエフォートがあります。`);
      }
      return {
        reasoning_effort: effort,
        description: typeof item.description === "string" ? item.description : "",
      };
    });
    const defaultEffort = entry.default_reasoning_effort;
    if (!CODEX_EFFORT.test(defaultEffort || "")) {
      throw new Error(`モデル「${entry.model}」の既定エフォートが不正です。`);
    }
    return {
      model: entry.model,
      display_name: typeof entry.display_name === "string" && entry.display_name.trim()
        ? entry.display_name.trim()
        : entry.model,
      description: typeof entry.description === "string" ? entry.description : "",
      is_default: entry.is_default === true,
      default_reasoning_effort: defaultEffort,
      supported_reasoning_efforts: efforts,
    };
  });
  return {...payload, models};
}

function catalogModels() {
  return state.llmCatalog?.models || [];
}

function catalogModelForValue(model) {
  if (model === null) {
    return catalogModels().find((entry) => entry.is_default) || null;
  }
  return catalogModels().find((entry) => entry.model === model) || null;
}

function selectedCatalogModel() {
  if (dom.llmModel.value === CUSTOM_LLM_VALUE) return null;
  return catalogModelForValue(currentLlmModelValue());
}

function modelOptionLabel(model) {
  return model.display_name === model.model
    ? model.model
    : `${model.display_name} (${model.model})`;
}

function renderLlmCustomFields() {
  dom.llmModelCustomField.hidden = dom.llmModel.value !== CUSTOM_LLM_VALUE;
  dom.llmEffortCustomField.hidden = dom.llmEffort.value !== CUSTOM_LLM_VALUE;
}

function renderLlmModelOptions(model) {
  const defaultModel = catalogModelForValue(null);
  const defaultLabel = defaultModel
    ? `Codex既定 · ${modelOptionLabel(defaultModel)}`
    : "Codex既定モデル";
  const options = [new Option(defaultLabel, "")];
  for (const entry of catalogModels()) {
    const option = new Option(modelOptionLabel(entry), entry.model);
    option.title = entry.description || option.textContent;
    options.push(option);
  }
  options.push(new Option("カスタムモデルID…", CUSTOM_LLM_VALUE));
  dom.llmModel.replaceChildren(...options);
  if (model === null) {
    dom.llmModel.value = "";
  } else if (catalogModelForValue(model)) {
    dom.llmModel.value = model;
  } else {
    dom.llmModel.value = CUSTOM_LLM_VALUE;
    dom.llmModelCustom.value = model;
  }
  renderLlmCustomFields();
}

function effortSpecsForSelectedModel() {
  const model = selectedCatalogModel();
  if (model) return model.supported_reasoning_efforts;
  return FALLBACK_LLM_EFFORTS.map((effort) => ({
    reasoning_effort: effort,
    description: "",
  }));
}

function renderLlmEffortOptions(effort) {
  const model = selectedCatalogModel();
  const efforts = effortSpecsForSelectedModel();
  const options = efforts.map((entry) => {
    const isDefault = entry.reasoning_effort === model?.default_reasoning_effort;
    const option = new Option(
      isDefault ? `${entry.reasoning_effort}（既定）` : entry.reasoning_effort,
      entry.reasoning_effort,
    );
    option.title = entry.description || option.textContent;
    return option;
  });
  options.push(new Option("カスタムエフォート…", CUSTOM_LLM_VALUE));
  dom.llmEffort.replaceChildren(...options);
  if (efforts.some((entry) => entry.reasoning_effort === effort)) {
    dom.llmEffort.value = effort;
  } else {
    dom.llmEffort.value = CUSTOM_LLM_VALUE;
    dom.llmEffortCustom.value = effort;
  }
  renderLlmCustomFields();
}

function setLlmControlValues(model, effort) {
  renderLlmModelOptions(model || null);
  renderLlmEffortOptions(effort || "low");
}

function readLlmSettingsControls() {
  const customModelSelected = dom.llmModel.value === CUSTOM_LLM_VALUE;
  const modelText = currentLlmModelValue();
  const customEffortSelected = dom.llmEffort.value === CUSTOM_LLM_VALUE;
  const effort = currentLlmEffortValue();
  if (customModelSelected && !modelText) {
    const error = new Error("カスタムモデルIDを入力してください。");
    error.control = dom.llmModelCustom;
    throw error;
  }
  if (modelText && !CODEX_MODEL_ID.test(modelText)) {
    const error = new Error(
      "モデルIDは英数字で始め、英数字・.・_・:・/・-を128文字以内で入力してください。",
    );
    error.control = customModelSelected ? dom.llmModelCustom : dom.llmModel;
    throw error;
  }
  if (customEffortSelected && !effort) {
    const error = new Error("カスタムエフォートを入力してください。");
    error.control = dom.llmEffortCustom;
    throw error;
  }
  if (!CODEX_EFFORT.test(effort)) {
    const error = new Error(
      "エフォートは英数字・_・-を使い、1〜32文字で入力してください。",
    );
    error.control = customEffortSelected ? dom.llmEffortCustom : dom.llmEffort;
    throw error;
  }
  return {model: modelText, effort};
}

function hydrateLlmSettings(llm) {
  setLlmControlValues(llm.model || null, llm.effort || "low");
  state.llmSettingsRevision = Number.isInteger(llm.settings_revision)
    ? llm.settings_revision
    : 0;
  state.llmSettingsInitialized = true;
  state.llmSettingsDirty = false;
  dom.llmSettingsError.hidden = true;
  dom.llmSettingsError.textContent = "";
}

function llmSettingsLabel() {
  const model = dom.llmModel.value === CUSTOM_LLM_VALUE && !currentLlmModelValue()
    ? "カスタムモデル未入力"
    : currentLlmModelValue() || "Codex既定モデル";
  const effort = dom.llmEffort.value === CUSTOM_LLM_VALUE && !currentLlmEffortValue()
    ? "カスタムエフォート未入力"
    : currentLlmEffortValue() || "—";
  return `${model} · ${effort}`;
}

function normalizedProposalQueue(payload) {
  if (
    !payload
    || typeof payload !== "object"
    || payload.schema !== "hazkey.mozc-boundary-proposal-queue.v1"
    || typeof payload.instance_id !== "string"
    || !Number.isInteger(payload.revision)
    || payload.revision < 0
    || !payload.counts
    || typeof payload.counts !== "object"
    || !Array.isArray(payload.jobs)
  ) {
    throw new Error("提案キューの応答形式が不正です");
  }
  const statuses = [
    "queued",
    "running",
    "succeeded",
    "stale",
    "failed",
    "cancelled",
  ];
  for (const status of statuses) {
    if (!Number.isInteger(payload.counts[status]) || payload.counts[status] < 0) {
      throw new Error("提案キューの件数が不正です");
    }
  }
  for (const job of payload.jobs) {
    if (
      !job
      || typeof job !== "object"
      || typeof job.job_id !== "string"
      || typeof job.batch_id !== "string"
      || typeof job.case_id !== "string"
      || !statuses.includes(job.status)
    ) {
      throw new Error("提案キューのジョブ形式が不正です");
    }
  }
  return payload;
}

function proposalQueueCounts() {
  return state.proposalQueue?.counts || {
    queued: 0,
    running: 0,
    succeeded: 0,
    stale: 0,
    failed: 0,
    cancelled: 0,
  };
}

function proposalQueueHasActiveJobs() {
  const counts = proposalQueueCounts();
  return counts.queued + counts.running > 0;
}

function latestProposalJob(caseId, {activeOnly = false} = {}) {
  const jobsByCase = activeOnly
    ? state.activeProposalJobsByCase
    : state.proposalJobsByCase;
  return jobsByCase.get(caseId) || null;
}

function indexProposalJobs(queue) {
  const latest = new Map();
  const active = new Map();
  for (const job of queue.jobs) {
    latest.set(job.case_id, job);
    if (["queued", "running"].includes(job.status)) {
      active.set(job.case_id, job);
    }
  }
  state.proposalJobsByCase = latest;
  state.activeProposalJobsByCase = active;
}

function syncProposalProgressScope(queue, previous) {
  if (previous && previous.instance_id !== queue.instance_id) {
    state.proposalProgressJobIds.clear();
  }
  const active = queue.jobs.filter(
    (job) => ["queued", "running"].includes(job.status),
  );
  if (active.length) {
    const previousBusy = Boolean(
      previous?.counts
      && (previous.counts.queued || previous.counts.running),
    );
    if (!previousBusy) state.proposalProgressJobIds.clear();
    const activeBatchIds = new Set(active.map((job) => job.batch_id));
    for (const job of queue.jobs) {
      if (activeBatchIds.has(job.batch_id)) {
        state.proposalProgressJobIds.add(job.job_id);
      }
    }
    return;
  }
  if (state.proposalProgressJobIds.size) return;
  const latestBatchId = queue.jobs.at(-1)?.batch_id;
  if (latestBatchId) {
    state.proposalProgressJobIds = new Set(
      queue.jobs
        .filter((job) => job.batch_id === latestBatchId)
        .map((job) => job.job_id),
    );
  }
}

function proposalJobFlag(job) {
  if (!job) return null;
  if (job.status === "running") return ["生成中", "queue-running"];
  if (job.status === "queued") {
    const position = job.queue_position ? `待${job.queue_position}` : "待機";
    return [position, "queue-queued"];
  }
  if (job.status === "succeeded") return ["案完了", "queue-succeeded"];
  if (job.status === "stale") return ["要再取得", "queue-stale"];
  if (job.status === "failed") return ["案失敗", "queue-failed"];
  return ["取消", "queue-cancelled"];
}

function renderProposalQueueControls() {
  const selectedCount = state.selectedCaseIds.size;
  const visibleIds = state.cases.map((item) => item.id);
  const selectedVisibleCount = visibleIds.filter(
    (caseId) => state.selectedCaseIds.has(caseId),
  ).length;
  dom.proposalSelectionCount.textContent = `${selectedCount.toLocaleString()}件選択`;
  dom.selectVisibleProposals.disabled = visibleIds.length === 0;
  dom.selectVisibleProposals.checked = (
    visibleIds.length > 0 && selectedVisibleCount === visibleIds.length
  );
  dom.selectVisibleProposals.indeterminate = (
    selectedVisibleCount > 0 && selectedVisibleCount < visibleIds.length
  );
  dom.clearProposalSelection.disabled = selectedCount === 0;

  const enabled = Boolean(state.meta?.llm.enabled);
  const selectedCurrent = Boolean(
    state.currentId && state.selectedCaseIds.has(state.currentId),
  );
  dom.queueSelectedProposals.disabled = !enabled
    || !state.proposalQueueInitialized
    || selectedCount === 0
    || state.proposalEnqueueing
    || state.llmSettingsSaving
    || state.llmCatalogLoading
    || (state.saving && selectedCurrent);
  dom.queueSelectedProposals.textContent = state.proposalEnqueueing
    ? "キューへ追加中…"
    : `選択した${selectedCount.toLocaleString()}件の提案を取得`;

  const counts = proposalQueueCounts();
  const scopedJobs = (state.proposalQueue?.jobs || []).filter(
    (job) => state.proposalProgressJobIds.has(job.job_id),
  );
  const scopedCounts = {
    queued: 0,
    running: 0,
    succeeded: 0,
    stale: 0,
    failed: 0,
    cancelled: 0,
  };
  for (const job of scopedJobs) scopedCounts[job.status] += 1;
  const terminalCount = scopedCounts.succeeded + scopedCounts.stale
    + scopedCounts.failed + scopedCounts.cancelled;
  const totalCount = terminalCount + scopedCounts.queued + scopedCounts.running;
  dom.proposalQueueProgress.max = Math.max(1, totalCount);
  dom.proposalQueueProgress.value = terminalCount;
  dom.proposalQueueCounts.textContent = (
    `待機 ${counts.queued.toLocaleString()} · 実行 ${counts.running.toLocaleString()}`
  );
  const running = state.proposalQueue?.jobs.find(
    (job) => job.status === "running",
  );
  if (state.proposalQueueLoading && !state.proposalQueue) {
    setTextContent(dom.proposalQueueStatus, "キューを確認しています…");
  } else if (counts.queued || counts.running) {
    const current = running ? ` · ${running.case_id}を生成中` : "";
    setTextContent(dom.proposalQueueStatus, (
      `選択分: 成功 ${scopedCounts.succeeded} · 要再取得 ${scopedCounts.stale}`
      + ` · 失敗 ${scopedCounts.failed}${current}`
    ));
  } else if (totalCount) {
    setTextContent(dom.proposalQueueStatus, (
      `キューは空です · 選択分: 成功 ${scopedCounts.succeeded}`
      + ` · 要再取得 ${scopedCounts.stale} · 失敗 ${scopedCounts.failed}`
    ));
  } else {
    setTextContent(dom.proposalQueueStatus, "キューは空です");
  }
  const proposalError = [
    state.proposalQueueError,
    state.proposalRefreshError,
  ].filter(Boolean).join(" / ");
  setTextContent(dom.proposalQueueError, proposalError);
  dom.proposalQueueError.hidden = !proposalError;

  const currentJob = state.currentId
    ? latestProposalJob(state.currentId)
    : null;
  if (!currentJob) {
    setTextContent(dom.currentProposalJobStatus, "");
  } else if (currentJob.status === "running") {
    setTextContent(
      dom.currentProposalJobStatus,
      "このケースの提案を生成中です。",
    );
  } else if (currentJob.status === "queued") {
    const position = currentJob.queue_position || "—";
    setTextContent(
      dom.currentProposalJobStatus,
      `このケースは生成待ち ${position}番目です。`,
    );
  } else if (currentJob.status === "succeeded") {
    setTextContent(
      dom.currentProposalJobStatus,
      "このケースの提案取得が完了しました。",
    );
  } else {
    setTextContent(dom.currentProposalJobStatus, currentJob.error?.message
      ? `このケースは再取得が必要です: ${currentJob.error.message}`
      : "このケースの提案取得は完了しませんでした。");
  }
}

function renderLlmCatalogStatus() {
  dom.llmCatalogError.textContent = state.llmCatalogError || "";
  dom.llmCatalogError.hidden = !state.llmCatalogError;
  if (state.llmCatalogLoading) {
    dom.llmCatalogStatus.textContent = "Codex App Serverからモデル一覧を取得中…";
  } else if (state.llmCatalogNotice) {
    dom.llmCatalogStatus.textContent = state.llmCatalogNotice;
  } else if (state.llmCatalog) {
    const count = state.llmCatalog.models.length.toLocaleString();
    dom.llmCatalogStatus.textContent = state.llmCatalogError
      ? `${count}件の取得済み一覧を表示しています。`
      : `${count}件のモデルと対応エフォートを取得しました。`;
  } else if (state.llmCatalogError) {
    dom.llmCatalogStatus.textContent = "一覧を取得できません。カスタム入力は利用できます。";
  } else {
    dom.llmCatalogStatus.textContent = "モデル一覧を準備しています…";
  }
}

function renderLlmControls() {
  if (!state.meta) return;
  const enabled = Boolean(state.meta.llm.enabled);
  const queueUnknown = !state.proposalQueueInitialized;
  const queueBusy = proposalQueueHasActiveJobs();
  const controlsBusy = queueUnknown
    || queueBusy
    || state.proposalEnqueueing
    || state.llmSettingsSaving
    || state.saving;
  const anyBusy = state.proposalEnqueueing
    || state.llmSettingsSaving
    || state.llmCatalogLoading
    || state.saving;
  dom.llmSettings.setAttribute("aria-busy", anyBusy ? "true" : "false");
  for (const control of [
    dom.llmModel,
    dom.llmModelCustom,
    dom.llmEffort,
    dom.llmEffortCustom,
  ]) {
    control.disabled = !enabled || controlsBusy;
  }
  dom.refreshLlmCatalog.disabled = !enabled
    || queueUnknown
    || state.llmCatalogLoading
    || queueBusy
    || state.proposalEnqueueing
    || state.llmSettingsSaving;
  dom.refreshLlmCatalog.textContent = state.llmCatalogLoading
    ? "一覧を取得中…"
    : "一覧を再取得";
  dom.saveLlmSettings.disabled = !enabled
    || controlsBusy
    || !state.llmSettingsDirty;
  dom.saveLlmSettings.textContent = state.llmSettingsSaving
    ? "設定を保存中…"
    : "設定を保存";
  dom.requestProposals.disabled = !enabled
    || queueUnknown
    || state.proposalEnqueueing
    || state.llmCatalogLoading
    || state.readingChangeSavePending
    || state.llmSettingsSaving
    || state.saving;
  dom.requestProposals.textContent = state.proposalEnqueueing
    ? "キューへ追加中…"
    : "提案を取得（キューへ）";
  if (!enabled) {
    dom.llmSettingsStatus.textContent = state.meta.llm.message
      || "Codex App Serverを利用できません";
  } else if (state.llmSettingsSaving) {
    dom.llmSettingsStatus.textContent = "設定を保存しています…";
  } else if (state.llmSettingsDirty) {
    dom.llmSettingsStatus.textContent = `未保存 · ${llmSettingsLabel()}`;
  } else {
    dom.llmSettingsStatus.textContent = `保存済み · ${llmSettingsLabel()}`;
  }
  dom.requestProposals.title = state.readingChangeSavePending
    ? "読み修正の保存完了後に候補を生成できます"
    : queueUnknown
      ? "提案キューの確認完了後に候補を生成できます"
    : state.llmCatalogLoading
      ? "モデル一覧の取得完了後に候補を生成できます"
      : enabled
        ? (
          `${llmSettingsLabel()}でCodex App Server候補を生成キューへ追加します。`
          + "同じ内容が待機中なら重複生成しません。"
        )
        : state.meta.llm.message
          || "認証済みCodex CLIが利用可能になると有効になります";
  renderLlmCatalogStatus();
  renderProposalQueueControls();
}

function handleLlmModelSelectionChange() {
  const currentEffort = currentLlmEffortValue();
  renderLlmCustomFields();
  const model = selectedCatalogModel();
  renderLlmEffortOptions(currentEffort);
  if (model) {
    const supported = model.supported_reasoning_efforts.map(
      (entry) => entry.reasoning_effort,
    );
    if (supported.length && !supported.includes(currentEffort)) {
      const nextEffort = supported.includes(model.default_reasoning_effort)
        ? model.default_reasoning_effort
        : supported[0];
      renderLlmEffortOptions(nextEffort);
      state.llmCatalogNotice = (
        `${model.display_name}では「${currentEffort || "未入力"}」を利用できないため、`
        + `既定の「${nextEffort}」へ変更しました。`
      );
    } else {
      state.llmCatalogNotice = null;
    }
  } else {
    state.llmCatalogNotice = null;
  }
  markLlmSettingsDirty();
  renderLlmCatalogStatus();
}

async function loadLlmCatalog() {
  if (!state.meta?.llm.enabled) {
    state.llmCatalogError = state.meta?.llm.message
      || "Codex App Serverを利用できません。";
    renderLlmControls();
    return false;
  }
  if (!state.proposalQueueInitialized || proposalQueueHasActiveJobs()) {
    state.llmCatalogNotice = state.proposalQueueInitialized
      ? "モデル一覧は提案キューが空になってから取得します。"
      : "提案キューを確認してからモデル一覧を取得します。";
    renderLlmCatalogStatus();
    renderLlmControls();
    return false;
  }
  const requestGeneration = ++state.llmCatalogRequestGeneration;
  state.llmCatalogLoading = true;
  state.llmCatalogError = null;
  state.llmCatalogNotice = null;
  renderLlmControls();
  try {
    const payload = normalizedLlmCatalog(await apiJson("/api/llm/models"));
    if (requestGeneration !== state.llmCatalogRequestGeneration) return false;
    const model = currentLlmModelValue();
    const effort = currentLlmEffortValue();
    const settingsDirty = state.llmSettingsDirty;
    const settingsRevision = state.llmSettingsRevision;
    state.llmCatalog = payload;
    setLlmControlValues(model, effort);
    state.llmSettingsDirty = settingsDirty;
    state.llmSettingsRevision = settingsRevision;
    return true;
  } catch (error) {
    if (requestGeneration !== state.llmCatalogRequestGeneration) return false;
    state.llmCatalogError = error.message;
    return false;
  } finally {
    if (requestGeneration === state.llmCatalogRequestGeneration) {
      state.llmCatalogLoading = false;
      renderLlmControls();
    }
  }
}

function markLlmSettingsDirty() {
  if (!state.llmSettingsInitialized || state.llmSettingsSaving) return;
  state.llmSettingsDirty = true;
  dom.llmSettingsError.hidden = true;
  dom.llmSettingsError.textContent = "";
  renderLlmControls();
}

function renderMeta() {
  const {total, reviewed, progress} = state.meta;
  const incomingSettingsRevision = state.meta.llm.settings_revision;
  if (
    !state.llmSettingsInitialized
    || (
      !state.llmSettingsDirty
      && !state.llmSettingsSaving
      && incomingSettingsRevision !== state.llmSettingsRevision
    )
  ) {
    hydrateLlmSettings(state.meta.llm);
  }
  dom.progressCount.textContent = `${reviewed.toLocaleString()} / ${total.toLocaleString()}`;
  dom.progressLabel.textContent = `${(progress * 100).toFixed(1)}% レビュー済み`;
  dom.progressBar.max = total;
  dom.progressBar.value = reviewed;
  renderLlmControls();
}

function caseQuery() {
  const params = new URLSearchParams();
  if (dom.statusFilter.value) params.set("status", dom.statusFilter.value);
  if (dom.categoryFilter.value) params.set("category", dom.categoryFilter.value);
  if (dom.search.value.trim()) params.set("q", dom.search.value.trim());
  if (dom.longOnly.checked) params.set("long", "1");
  if (dom.adjudicationOnly.checked) params.set("adjudication", "1");
  return params.toString();
}

async function loadCases({preserveSelection = true} = {}) {
  const requestGeneration = ++state.listRequestGeneration;
  const result = await apiJson(`/api/cases?${caseQuery()}`);
  if (requestGeneration !== state.listRequestGeneration) return;
  state.cases = result.cases;
  if (!preserveSelection) state.selectedCaseIds.clear();
  renderCaseList();
  renderProposalQueueControls();
  if (!preserveSelection && state.cases.length) {
    await selectCase(state.cases[0].id, {saveFirst: false});
  }
}

function renderCaseList() {
  clear(dom.caseList);
  dom.queueCount.textContent = `${state.cases.length.toLocaleString()}件`;
  dom.emptyQueue.hidden = state.cases.length !== 0;
  for (const summary of state.cases) {
    const item = element("li", "case-list-item");
    const selectLabel = element("label", "case-list-select");
    selectLabel.title = `${summary.id}を一括提案の対象に選択`;
    const select = document.createElement("input");
    select.type = "checkbox";
    select.checked = state.selectedCaseIds.has(summary.id);
    select.setAttribute(
      "aria-label",
      `${summary.id}を一括提案の対象に選択`,
    );
    select.addEventListener("change", () => {
      if (select.checked) state.selectedCaseIds.add(summary.id);
      else state.selectedCaseIds.delete(summary.id);
      renderProposalQueueControls();
    });
    selectLabel.append(select);
    const button = element("button", "case-list-button");
    button.type = "button";
    button.dataset.caseId = summary.id;
    button.setAttribute("aria-current", summary.id === state.currentId ? "true" : "false");
    const dot = element("span", `case-state-dot ${summary.path_set_status}`);
    dot.setAttribute("aria-hidden", "true");
    const main = element("span", "case-list-main");
    main.append(
      element("span", "case-list-id", summary.id),
      element("span", "case-list-reading", summary.annotation_reading || summary.reading),
    );
    const flags = element("span", "case-list-flags");
    if (
      summary.reading_corrected
      || (summary.annotation_reading && summary.annotation_reading !== summary.reading)
      || (summary.source_reading && summary.source_reading !== summary.reading)
    ) {
      flags.append(element("span", "mini-flag", "読修"));
    }
    if (summary.is_long) flags.append(element("span", "mini-flag", "長文"));
    if (summary.needs_adjudication) flags.append(element("span", "mini-flag", "要裁定"));
    if (summary.proposal_count) {
      flags.append(element("span", "mini-flag", `案${summary.proposal_count}`));
    }
    const proposalJob = latestProposalJob(summary.id);
    const jobFlag = proposalJobFlag(proposalJob);
    if (jobFlag && !(proposalJob.status === "succeeded" && summary.proposal_count)) {
      flags.append(element("span", `mini-flag ${jobFlag[1]}`, jobFlag[0]));
    }
    button.append(dot, main, flags);
    button.addEventListener("click", () => selectCase(summary.id));
    item.append(selectLabel, button);
    dom.caseList.append(item);
  }
}

function hideCorrectedReadingEditor({restoreFocus = false} = {}) {
  dom.correctedReadingEditor.hidden = true;
  dom.correctedReadingError.hidden = true;
  dom.correctedReadingError.textContent = "";
  if (restoreFocus && !dom.editorContent.hidden) {
    dom.editCorrectedReading.focus();
  }
}

function showCorrectedReadingEditor() {
  if (!ensureReadingChangeSaved()) return;
  dom.correctedReadingInput.value = effectiveReading();
  dom.correctedReadingError.hidden = true;
  dom.correctedReadingError.textContent = "";
  dom.correctedReadingEditor.hidden = false;
  dom.correctedReadingInput.focus();
  dom.correctedReadingInput.select();
}

function normalizedCorrectedReading(rawValue) {
  const value = rawValue.normalize("NFC");
  if (!value) throw new Error("正しい読みを入力してください。");
  if (value.includes("|")) throw new Error("読み自体に | は使えません。");
  if (/[\u0000-\u001f\u007f-\u009f]/u.test(value)) {
    throw new Error("読みには制御文字を使えません。");
  }
  return value === originalReading() ? null : value;
}

function changeCorrectedReading(correctedReading) {
  if (!ensureReadingChangeSaved()) return false;
  const nextReading = correctedReading || originalReading();
  if (nextReading === effectiveReading()) {
    hideCorrectedReadingEditor({restoreFocus: true});
    return false;
  }
  const pathCount = state.draft.acceptable_paths.length;
  const message = pathCount
    ? `読みを変更すると、現在の経路${pathCount}件を初期化します。続けますか？`
    : "アノテーションに使う読みを変更しますか？";
  if (!window.confirm(message)) return false;

  state.draft.corrected_reading = correctedReading;
  state.draft.acceptable_paths = [];
  state.draft.path_set_status = "pending";
  state.draft.needs_adjudication = false;
  state.activePathIndex = 0;
  state.pathInputError = null;
  state.splitChunkIndex = null;
  state.readingChangeSavePending = true;
  state.proposalsStaleForReading = true;
  state.proposalStaleMessage = (
    "読みを変更したため、以前の読み向けのLLM提案は使用しません。"
    + "修正読みを保存してから新しい提案を取得してください。"
  );
  state.detail.proposals = [];
  hideCorrectedReadingEditor({restoreFocus: true});
  renderSource();
  renderProposals();
  renderReview();
  markDirty("correct-reading");
  renderReadingChangeLock();
  const caseId = state.currentId;
  window.queueMicrotask(() => {
    if (state.currentId !== caseId || !state.readingChangeSavePending) return;
    void saveCurrent({
      advance: false,
      actionType: "correct-reading",
      quiet: false,
    });
  });
  return true;
}

function applyCorrectedReading() {
  try {
    const corrected = normalizedCorrectedReading(dom.correctedReadingInput.value);
    dom.correctedReadingError.hidden = true;
    dom.correctedReadingError.textContent = "";
    changeCorrectedReading(corrected);
  } catch (error) {
    dom.correctedReadingError.textContent = error.message;
    dom.correctedReadingError.hidden = false;
    dom.correctedReadingInput.focus();
  }
}

function applyOpenCorrectedReadingEditor() {
  if (dom.correctedReadingEditor.hidden) return true;
  applyCorrectedReading();
  return dom.correctedReadingEditor.hidden;
}

function renderSource() {
  const current = state.detail.case;
  const corrected = hasCorrectedReading();
  dom.caseCategory.textContent = current.category;
  dom.caseId.textContent = current.id;
  dom.caseRevision.textContent = `revision ${state.draft.revision}`;
  dom.sourceReading.textContent = originalReading();
  dom.correctedReadingSummary.hidden = !corrected;
  dom.correctedReadingDisplay.textContent = corrected ? effectiveReading() : "";
  dom.editCorrectedReading.textContent = corrected ? "修正を編集" : "読みを修正";
  dom.readingLength.textContent = corrected
    ? `${codePointLength(effectiveReading())}要素・修正読み`
    : `${codePointLength(effectiveReading())}要素`;
  clear(dom.surfaceReferences);
  for (const surface of current.surface_references) {
    const item = element("li", "surface-reference-item");
    item.append(
      element("span", "surface-reference-id", surface.id),
      element("span", "surface-reference-text", surface.text),
    );
    dom.surfaceReferences.append(item);
  }

  const preannotation = current.preannotation;
  dom.linderaNonapplicable.hidden = !corrected;
  dom.linderaConfidence.textContent = preannotation.confidence || "—";
  dom.linderaConfidence.className = `confidence-chip ${preannotation.confidence || ""}`;
  dom.linderaMarked.textContent = preannotation.marked_reading || "—";
  dom.linderaAmbiguity.textContent = preannotation.ambiguity?.length
    ? preannotation.ambiguity.join(" / ")
    : "なし";
  clear(dom.linderaTokens);
  const tokens = current.token_audit?.alternatives?.[0]?.tokens || [];
  for (const token of tokens) {
    const chip = element("span", "token-chip");
    chip.append(
      document.createTextNode(token.surface),
      element("span", "token-pos", token.pos_major || "—"),
    );
    dom.linderaTokens.append(chip);
  }
}

function flattenLatestProposalPaths() {
  if (state.proposalsStaleForReading || !state.detail?.proposals?.length) return [];
  const proposal = state.detail.proposals[state.detail.proposals.length - 1];
  return proposal.paths.map((path, index) => ({proposal, path, rank: index + 1}));
}

function renderProposals() {
  clear(dom.proposalList);
  const entries = flattenLatestProposalPaths();
  const nonapplicable = state.proposalsStaleForReading
    || (hasCorrectedReading() && !entries.length);
  dom.proposalNonapplicable.hidden = !nonapplicable;
  dom.proposalNonapplicable.textContent = state.proposalStaleMessage || (
    "修正読みには、元の読み向けのLLM提案を使用しません。"
    + "修正読みを保存してから新しい提案を取得してください。"
  );
  dom.proposalEmpty.hidden = entries.length !== 0 || nonapplicable;
  for (const {proposal, path, rank} of entries) {
    const surface = state.detail.case.surface_references.find(
      (item) => item.id === path.surface_reference_id,
    );
    const card = element("article", "proposal-card");
    const rankLine = element("div", "proposal-rank");
    const generatorLabel = [proposal.model, proposal.reasoning_effort]
      .filter(Boolean)
      .join(" · ");
    const discardedLabel = proposal.discarded_candidate_count
      ? ` · ${proposal.discarded_candidate_count}案除外`
      : "";
    let ambiguityLabel = "";
    if (proposal.ambiguous) {
      ambiguityLabel = proposal.paths.length > 1 ? "別解あり · " : "曖昧判定 · ";
    }
    rankLine.append(
      element("span", "", `候補 ${rank}`),
      element(
        "span",
        "",
        `${ambiguityLabel}${generatorLabel || "LLM"}${discardedLabel}`,
      ),
    );
    const reading = element(
      "p",
      "proposal-reading",
      markedFromBoundaries(effectiveReading(), path.reading_boundaries),
    );
    const output = element(
      "p",
      "proposal-surface",
      surface ? markedFromBoundaries(surface.text, path.surface_boundaries || []) : "—",
    );
    const reasons = element(
      "p",
      "proposal-reason",
      proposal.ambiguity_reasons?.length
        ? proposal.ambiguity_reasons.join(" / ")
        : "人手確認前の提案です",
    );
    const actions = element("div", "proposal-actions");
    const draftButton = element("button", "button secondary", `候補${rank}を下書きへ`);
    draftButton.type = "button";
    draftButton.addEventListener("click", () => copyProposal(path, false));
    const acceptButton = element("button", "button ghost", "許容経路として追加");
    acceptButton.type = "button";
    acceptButton.addEventListener("click", () => copyProposal(path, true));
    actions.append(draftButton, acceptButton);
    card.append(rankLine, reading, output, reasons, actions);
    dom.proposalList.append(card);
  }
}

function newPathId(prefix = "human") {
  const existing = new Set(state.draft.acceptable_paths.map((path) => path.path_id));
  let counter = 1;
  let candidate;
  do {
    candidate = `${prefix}-${Date.now().toString(36)}-${counter}`;
    counter += 1;
  } while (existing.has(candidate));
  return candidate;
}

function copyProposal(proposalPath, acceptable) {
  if (!ensureReadingChangeSaved()) return;
  const path = deepCopy(proposalPath);
  path.path_id = newPathId("llm");
  path.status = acceptable ? "acceptable" : "draft";
  state.draft.acceptable_paths.push(path);
  state.activePathIndex = state.draft.acceptable_paths.length - 1;
  state.pathInputError = null;
  if (acceptable && state.draft.path_set_status === "pending") {
    state.draft.path_set_status = "open";
    dom.pathSetStatus.value = "open";
  }
  renderPathArea();
  markDirty(acceptable ? "accept-proposal" : "copy-proposal");
}

function renderPathTabs() {
  clear(dom.pathTabs);
  state.draft.acceptable_paths.forEach((path, index) => {
    const button = element("button", "path-tab");
    button.type = "button";
    button.dataset.pathId = path.path_id;
    button.setAttribute("aria-pressed", index === state.activePathIndex ? "true" : "false");
    const dot = element("span", `tab-state ${path.status}`);
    const copiedLabel = path.provenance?.source_path_id ? " · 複製" : "";
    button.append(
      dot,
      document.createTextNode(`経路 ${index + 1}${copiedLabel}`),
    );
    button.addEventListener("click", () => {
      state.activePathIndex = index;
      state.splitChunkIndex = null;
      state.pathInputError = null;
      renderPathArea();
    });
    dom.pathTabs.append(button);
  });
}

function renderReadingGapEditor(path) {
  clear(dom.readingGapEditor);
  const readingText = effectiveReading();
  const characters = codePoints(readingText);
  const boundaries = new Set(path.reading_boundaries);
  characters.forEach((character, index) => {
    dom.readingGapEditor.append(element("span", "reading-element", character));
    const boundary = index + 1;
    if (boundary >= characters.length) return;
    const toggle = element("button", "gap-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-label", `${boundary}文字目の後の境界`);
    toggle.setAttribute("aria-pressed", boundaries.has(boundary) ? "true" : "false");
    toggle.addEventListener("click", () => {
      const values = new Set(path.reading_boundaries);
      if (values.has(boundary)) values.delete(boundary);
      else values.add(boundary);
      path.reading_boundaries = [...values].sort((left, right) => left - right);
      state.pathInputError = null;
      dom.markedReadingInput.value = markedFromBoundaries(
        readingText,
        path.reading_boundaries,
      );
      renderReadingGapEditor(path);
      showPathValidation(null);
      markDirty("boundary-toggle");
    });
    dom.readingGapEditor.append(toggle);
  });
  dom.markedReadingInput.value = markedFromBoundaries(
    readingText,
    path.reading_boundaries,
  );
}

function chunkRanges(boundaries, length) {
  const positions = [0, ...boundaries, length];
  return positions.slice(0, -1).map((start, index) => ({
    start,
    end: positions[index + 1],
  }));
}

function moveAlignedBoundary(path, kind, index, delta) {
  const boundaries = kind === "reading"
    ? path.reading_boundaries
    : path.surface_boundaries;
  const source = kind === "reading" ? effectiveReading() : currentSurface(path).text;
  const minimum = index === 0 ? 1 : boundaries[index - 1] + 1;
  const maximum = index === boundaries.length - 1
    ? codePointLength(source) - 1
    : boundaries[index + 1] - 1;
  const next = boundaries[index] + delta;
  if (next < minimum || next > maximum) return;
  boundaries[index] = next;
  state.splitChunkIndex = null;
  state.pathInputError = null;
  renderAlignedEditor(path);
  markDirty("boundary-resize");
}

function mergeAlignedBoundary(path, index) {
  path.reading_boundaries.splice(index, 1);
  path.surface_boundaries.splice(index, 1);
  state.splitChunkIndex = null;
  renderAlignedEditor(path);
  markDirty("merge-boundary");
}

function renderSplitPanel(path, chunkIndex, readingRange, surfaceRange) {
  const panel = element("div", "boundary-resizer");
  panel.append(element("span", "boundary-controls-label", "分割"));
  const controls = element("div", "boundary-controls");
  const readingSelect = element("select", "select-input");
  const surfaceSelect = element("select", "select-input");
  readingSelect.setAttribute("aria-label", "読み側の分割位置");
  surfaceSelect.setAttribute("aria-label", "表層側の分割位置");
  for (let offset = readingRange.start + 1; offset < readingRange.end; offset += 1) {
    readingSelect.append(new Option(
      `${codePointSlice(effectiveReading(), readingRange.start, offset)}｜${codePointSlice(effectiveReading(), offset, readingRange.end)}`,
      String(offset),
    ));
  }
  const surfaceText = currentSurface(path).text;
  for (let offset = surfaceRange.start + 1; offset < surfaceRange.end; offset += 1) {
    surfaceSelect.append(new Option(
      `${codePointSlice(surfaceText, surfaceRange.start, offset)}｜${codePointSlice(surfaceText, offset, surfaceRange.end)}`,
      String(offset),
    ));
  }
  const confirm = element("button", "button secondary", "この位置で分割");
  confirm.type = "button";
  confirm.addEventListener("click", () => {
    path.reading_boundaries.splice(chunkIndex, 0, Number(readingSelect.value));
    path.surface_boundaries.splice(chunkIndex, 0, Number(surfaceSelect.value));
    state.splitChunkIndex = null;
    renderAlignedEditor(path);
    markDirty("split-chunk");
  });
  controls.append(readingSelect, surfaceSelect, confirm);
  panel.append(controls);
  return panel;
}

function renderAlignedEditor(path) {
  clear(dom.alignedChunkList);
  const readingText = effectiveReading();
  const surface = currentSurface(path);
  if (!surface) return;
  const readingRanges = chunkRanges(path.reading_boundaries, codePointLength(readingText));
  const surfaceRanges = chunkRanges(path.surface_boundaries, codePointLength(surface.text));
  readingRanges.forEach((readingRange, index) => {
    const surfaceRange = surfaceRanges[index];
    const card = element("article", "chunk-pair");
    card.append(element("span", "chunk-number", String(index + 1)));
    const content = element("div", "chunk-content");
    const readingSide = element("div", "chunk-side");
    readingSide.append(
      element("span", "chunk-side-label", "読み"),
      element(
        "span",
        "chunk-side-text",
        codePointSlice(readingText, readingRange.start, readingRange.end),
      ),
    );
    const surfaceSide = element("div", "chunk-side");
    surfaceSide.append(
      element("span", "chunk-side-label", "表層"),
      element(
        "span",
        "chunk-side-text",
        codePointSlice(surface.text, surfaceRange.start, surfaceRange.end),
      ),
    );
    content.append(readingSide, surfaceSide);
    const actions = element("div", "chunk-actions");
    const canSplit = readingRange.end - readingRange.start > 1
      && surfaceRange.end - surfaceRange.start > 1;
    const split = element("button", "button ghost", "この対を分割");
    split.type = "button";
    split.disabled = !canSplit;
    split.addEventListener("click", () => {
      state.splitChunkIndex = state.splitChunkIndex === index ? null : index;
      renderAlignedEditor(path);
    });
    actions.append(split);
    card.append(content, actions);
    dom.alignedChunkList.append(card);
    if (state.splitChunkIndex === index && canSplit) {
      dom.alignedChunkList.append(
        renderSplitPanel(path, index, readingRange, surfaceRange),
      );
    }
    if (index < readingRanges.length - 1) {
      const boundary = element("div", "boundary-resizer");
      boundary.append(element("span", "boundary-line"));
      const controls = element("div", "boundary-controls");
      controls.append(element("span", "boundary-controls-label", `境界 ${index + 1}`));
      const actionsSpec = [
        ["読み ←", () => moveAlignedBoundary(path, "reading", index, -1)],
        ["読み →", () => moveAlignedBoundary(path, "reading", index, 1)],
        ["表層 ←", () => moveAlignedBoundary(path, "surface", index, -1)],
        ["表層 →", () => moveAlignedBoundary(path, "surface", index, 1)],
        ["結合", () => mergeAlignedBoundary(path, index)],
      ];
      for (const [label, handler] of actionsSpec) {
        const button = element("button", "boundary-button", label);
        button.type = "button";
        button.addEventListener("click", handler);
        controls.append(button);
      }
      boundary.append(controls);
      dom.alignedChunkList.append(boundary);
    }
  });
}

function renderPathArea() {
  renderPathTabs();
  const path = currentPath();
  dom.pathEmpty.hidden = Boolean(path);
  dom.pathEditor.hidden = !path;
  if (!path) return;

  clear(dom.surfaceSelect);
  for (const surface of state.detail.case.surface_references) {
    dom.surfaceSelect.append(new Option(surface.text, surface.id));
  }
  dom.surfaceSelect.value = path.surface_reference_id;
  dom.pathStatus.value = path.status;
  const aligned = path.alignment_status === "aligned";
  setStatusPill(
    dom.alignmentStatus,
    aligned ? "読み・表層対応済み" : "読みのみ",
    aligned ? "success" : "neutral",
  );
  dom.readingOnlyEditor.hidden = aligned;
  dom.alignedEditor.hidden = !aligned;
  if (aligned) renderAlignedEditor(path);
  else renderReadingGapEditor(path);
  showPathValidation(validateDraft());
}

function renderReview() {
  dom.pathSetStatus.value = state.draft.path_set_status;
  dom.needsAdjudication.checked = state.draft.needs_adjudication;
  dom.notes.value = state.draft.notes || "";
  renderPathArea();
  renderReadingChangeLock();
}

function renderCase() {
  dom.editorLoading.hidden = true;
  dom.editorEmpty.hidden = true;
  dom.editorContent.hidden = false;
  renderSource();
  renderProposals();
  renderReview();
  renderCaseList();
  renderProposalQueueControls();
  const index = state.cases.findIndex((item) => item.id === state.currentId);
  dom.casePosition.textContent = index >= 0
    ? `${index + 1} / ${state.cases.length}`
    : `${state.detail.case.index + 1} / ${state.meta.total}`;
  dom.previousCase.disabled = index <= 0;
  dom.nextCase.disabled = index < 0 || index >= state.cases.length - 1;
  setStatusPill(dom.saveStatus, "保存済み", "success");
}

async function selectCase(caseId, {saveFirst = true, force = false} = {}) {
  if (!force && caseId === state.currentId && state.detail) return;
  if (force) hideCorrectedReadingEditor();
  else if (!applyOpenCorrectedReadingEditor()) return;
  if (state.saving && caseId !== state.currentId) {
    toast("保存完了後に移動してください");
    return;
  }
  if (saveFirst && state.dirty) {
    const saved = await saveCurrent({advance: false, actionType: "navigate", quiet: true});
    if (!saved) return;
  }
  const requestGeneration = ++state.caseRequestGeneration;
  dom.editorLoading.hidden = false;
  dom.editorContent.hidden = true;
  hideAlert();
  try {
    const detail = await apiJson(`/api/cases/${encodeURIComponent(caseId)}`);
    if (requestGeneration !== state.caseRequestGeneration) return;
    state.currentId = caseId;
    state.detail = detail;
    state.draft = deepCopy(detail.review);
    if (!Object.prototype.hasOwnProperty.call(state.draft, "corrected_reading")) {
      state.draft.corrected_reading = null;
    }
    state.activePathIndex = restoredPathIndex(
      caseId,
      state.draft.acceptable_paths,
    );
    state.pathInputError = null;
    state.editGeneration = 0;
    state.dirty = false;
    state.boundaryEdits = 0;
    state.splitChunkIndex = null;
    state.proposalsStaleForReading = false;
    state.proposalStaleMessage = null;
    state.readingChangeSavePending = false;
    state.caseOpenedAt = performance.now();
    hideCorrectedReadingEditor();
    renderCase();
  } catch (error) {
    if (requestGeneration !== state.caseRequestGeneration) return;
    showAlert("ケースを読み込めません", error.message);
    dom.editorLoading.hidden = true;
    dom.editorEmpty.hidden = false;
  }
}

async function refreshMetaAndCases() {
  applyMeta(await apiJson("/api/meta"));
  renderMeta();
  fillCategoryFilter();
  await loadCases({preserveSelection: true});
}

function visibleAdvanceTarget(caseId) {
  const index = state.cases.findIndex((item) => item.id === caseId);
  return index >= 0 ? state.cases[index + 1]?.id || null : null;
}

async function advanceAfterSave(caseId, targetId) {
  if (state.currentId !== caseId) return false;
  if (targetId) {
    await selectCase(targetId);
    return true;
  }
  const pending = await apiJson("/api/cases?status=pending");
  const target = pending.cases.find((item) => item.id !== caseId);
  if (target) await selectCase(target.id);
  else toast("次の未確認ケースはありません");
  return true;
}

async function saveCurrent({
  advance = false,
  advanceTarget = undefined,
  actionType = "save",
  quiet = false,
} = {}) {
  if (!state.detail) return false;
  if (!dom.correctedReadingEditor.hidden && actionType === "autosave") {
    window.clearTimeout(state.autosaveTimer);
    state.autosaveTimer = window.setTimeout(() => {
      saveCurrent({advance: false, actionType: "autosave", quiet: true});
    }, 1200);
    return false;
  }
  if (!applyOpenCorrectedReadingEditor()) return false;
  const requestCaseId = state.currentId;
  let advanceIntent = state.pendingAdvance?.caseId === requestCaseId
    ? state.pendingAdvance
    : null;
  if (advance && !advanceIntent) {
    advanceIntent = {
      caseId: requestCaseId,
      targetId: advanceTarget === undefined
        ? visibleAdvanceTarget(requestCaseId)
        : advanceTarget,
      actionType,
      quiet,
      requiresOwnRound: state.saving,
    };
    state.pendingAdvance = advanceIntent;
  }
  if (state.saving) {
    if (!advance) return false;
    setStatusPill(dom.saveStatus, "保存後に移動…", "neutral");
    if (!quiet) toast("進行中の保存が終わり次第、次へ進みます");
    return true;
  }
  window.clearTimeout(state.autosaveTimer);
  const validation = validateDraft();
  showPathValidation(validation);
  if (validation) {
    if (advance && state.pendingAdvance === advanceIntent) {
      state.pendingAdvance = null;
    }
    setStatusPill(dom.saveStatus, "要修正", "danger");
    if (!quiet) showAlert("保存できません", validation);
    return false;
  }
  state.saving = true;
  renderLlmControls();
  setStatusPill(
    dom.saveStatus,
    state.readingChangeSavePending ? "読み修正を保存中…" : "保存中…",
    "neutral",
  );
  const caseId = state.currentId;
  const editGeneration = state.editGeneration;
  const savedBoundaryEdits = state.boundaryEdits;
  const readingChangePendingAtRequest = state.readingChangeSavePending;
  let roundCompleted = false;
  let roundError = null;
  const payload = deepCopy({
    base_revision: state.draft.revision,
    corrected_reading: state.draft.corrected_reading || null,
    path_set_status: state.draft.path_set_status,
    needs_adjudication: state.draft.needs_adjudication,
    acceptable_paths: state.draft.acceptable_paths,
    notes: state.draft.notes,
    reviewed_once: true,
    action: {
      type: actionType,
      active_ms: Math.max(0, Math.round(performance.now() - state.caseOpenedAt)),
      boundary_edits: state.boundaryEdits,
    },
  });
  try {
    const result = await apiJson(
      `/api/cases/${encodeURIComponent(caseId)}`,
      {method: "PATCH", body: JSON.stringify(payload)},
    );
    if (state.currentId !== caseId || !state.detail) return false;
    const editedBeforeResponse = state.editGeneration !== editGeneration;
    const desiredCorrectedReading = state.draft.corrected_reading || null;
    if (
      state.readingChangeSavePending
      && readingChangePendingAtRequest
      && payload.corrected_reading === desiredCorrectedReading
      && result.review.corrected_reading === desiredCorrectedReading
    ) {
      state.readingChangeSavePending = false;
    }
    state.detail.review = result.review;
    state.detail.proposals = state.detail.proposals.filter((proposal) => {
      if (proposal.review_revision !== null && proposal.review_revision !== undefined) {
        // review_revision records which review produced the proposal. An ordinary
        // save advances the review revision without changing the proposal input,
        // so it must not make the proposal disappear. Corrected-reading changes
        // explicitly clear proposals before saving.
        return true;
      }
      // Legacy proposals have no effective-reading hash and are safe only while
      // the immutable source reading remains active.
      return result.review.corrected_reading === null;
    });
    renderProposals();
    renderReadingChangeLock();
    if (editedBeforeResponse) {
      state.draft.revision = result.review.revision;
      state.dirty = true;
      state.boundaryEdits = Math.max(0, state.boundaryEdits - savedBoundaryEdits);
    } else {
      // Boundary controls close over the active path object. Preserve those
      // objects across an ordinary save so the next edit cannot mutate a
      // detached pre-save path that is no longer present in state.draft.
      const pathObjectsPreserved = reconcileSavedDraft(result.review);
      state.dirty = false;
      state.boundaryEdits = 0;
      if (!pathObjectsPreserved) renderReview();
    }
    state.caseOpenedAt = performance.now();
    dom.caseRevision.textContent = `revision ${result.review.revision}`;
    hideAlert();
    await refreshMetaAndCases();
    if (state.currentId !== caseId) return false;
    roundCompleted = true;
    const hasUnsavedEdits = state.editGeneration !== editGeneration;
    setStatusPill(
      dom.saveStatus,
      hasUnsavedEdits ? "未保存" : "保存済み",
      hasUnsavedEdits ? "warning" : "success",
    );
    if (hasUnsavedEdits) {
      state.dirty = true;
      window.clearTimeout(state.autosaveTimer);
      state.autosaveTimer = window.setTimeout(() => {
        saveCurrent({advance: false, actionType: "autosave", quiet: true});
      }, 1200);
      if (!quiet) toast("保存中の追加編集が残っています");
      return false;
    }
    if (!quiet) toast("保存しました");
    return true;
  } catch (error) {
    roundError = error;
    setStatusPill(dom.saveStatus, "保存失敗", "danger");
    if (error.status === 409) {
      showAlert("別タブで更新されています", error.message, true);
    } else if (!quiet || error.status !== 400) {
      showAlert("保存できません", error.message);
    }
    return false;
  } finally {
    state.saving = false;
    renderLlmControls();
    const pendingAdvance = state.pendingAdvance;
    if (pendingAdvance?.caseId === caseId) {
      if (!roundCompleted || state.currentId !== caseId) {
        state.pendingAdvance = null;
        if (
          roundError
          && quiet
          && !pendingAdvance.quiet
          && roundError.status === 400
        ) {
          showAlert("保存できません", roundError.message);
        }
      } else if (pendingAdvance.requiresOwnRound || state.dirty) {
        pendingAdvance.requiresOwnRound = false;
        window.clearTimeout(state.autosaveTimer);
        window.queueMicrotask(() => {
          if (
            state.currentId !== caseId
            || state.pendingAdvance !== pendingAdvance
          ) {
            if (state.pendingAdvance === pendingAdvance) {
              state.pendingAdvance = null;
            }
            return;
          }
          void saveCurrent({
            advance: true,
            actionType: pendingAdvance.actionType,
            quiet: pendingAdvance.quiet,
          });
        });
      } else {
        window.queueMicrotask(() => {
          if (
            state.currentId !== caseId
            || state.pendingAdvance !== pendingAdvance
          ) {
            if (state.pendingAdvance === pendingAdvance) {
              state.pendingAdvance = null;
            }
            return;
          }
          if (state.dirty) {
            void saveCurrent({
              advance: true,
              actionType: pendingAdvance.actionType,
              quiet: pendingAdvance.quiet,
            });
            return;
          }
          state.pendingAdvance = null;
          void advanceAfterSave(caseId, pendingAdvance.targetId).catch((error) => {
            showAlert("次のケースへ移動できません", error.message);
          });
        });
      }
    }
    if (
      state.readingChangeSavePending
      && !readingChangePendingAtRequest
      && state.currentId === caseId
      && state.pendingAdvance === null
    ) {
      window.clearTimeout(state.autosaveTimer);
      window.queueMicrotask(() => {
        if (
          state.currentId !== caseId
          || !state.readingChangeSavePending
          || state.saving
        ) return;
        void saveCurrent({
          advance: false,
          actionType: "correct-reading",
          quiet: false,
        });
      });
    }
  }
}

async function moveCase(delta, {saveFirst = true} = {}) {
  const index = state.cases.findIndex((item) => item.id === state.currentId);
  if (index < 0) return;
  const target = state.cases[index + delta];
  if (target) await selectCase(target.id, {saveFirst});
}

async function saveLlmSettings({quiet = false} = {}) {
  if (!state.meta?.llm.enabled) {
    dom.llmSettingsError.textContent = state.meta?.llm.message
      || "Codex App Serverを利用できません。";
    dom.llmSettingsError.hidden = false;
    return false;
  }
  if (state.llmSettingsSaving) return false;
  if (!state.proposalQueueInitialized || proposalQueueHasActiveJobs()) {
    if (!quiet) {
      toast("LLM設定は提案キューが空になってから保存してください");
    }
    return false;
  }
  if (state.saving) {
    if (!quiet) toast("レビューの保存完了後に設定を保存してください");
    return false;
  }
  let settings;
  try {
    settings = readLlmSettingsControls();
  } catch (error) {
    dom.llmSettingsError.textContent = error.message;
    dom.llmSettingsError.hidden = false;
    error.control?.focus();
    return false;
  }
  const desiredModel = settings.model;
  const desiredEffort = settings.effort;
  state.llmSettingsSaving = true;
  state.llmSettingsDirty = true;
  dom.llmSettingsError.hidden = true;
  dom.llmSettingsError.textContent = "";
  renderLlmControls();
  try {
    const result = await apiJson("/api/settings/llm", {
      method: "PATCH",
      body: JSON.stringify({
        base_revision: state.llmSettingsRevision,
        model: settings.model,
        effort: settings.effort,
      }),
    });
    state.meta.llm = result.llm;
    hydrateLlmSettings(result.llm);
    if (!quiet) toast("LLM設定を保存しました");
    return true;
  } catch (error) {
    if (error.status === 409) {
      try {
        const latestMeta = await apiJson("/api/meta");
        applyMeta(latestMeta);
        state.llmSettingsRevision = latestMeta.llm.settings_revision;
      } catch {
        // Keep the previous base revision; the next save will conflict safely.
      }
      setLlmControlValues(desiredModel, desiredEffort);
      state.llmSettingsDirty = true;
      dom.llmSettingsError.textContent = (
        "別タブでLLM設定が更新されました。入力内容は保持しています。"
        + "内容を確認し、もう一度保存してください。"
      );
    } else {
      dom.llmSettingsError.textContent = error.message;
    }
    dom.llmSettingsError.hidden = false;
    if (!quiet) toast("LLM設定を保存できませんでした");
    return false;
  } finally {
    state.llmSettingsSaving = false;
    renderLlmControls();
  }
}

function newProposalBatchId() {
  const suffix = window.crypto?.randomUUID
    ? window.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `proposal-batch-${suffix}`;
}

async function refreshCurrentProposals(caseId) {
  try {
    const detail = await apiJson(`/api/cases/${encodeURIComponent(caseId)}`);
    if (state.currentId !== caseId || !state.detail || !state.draft) return;
    if (detail.case.annotation_reading !== effectiveReading()) return;
    state.detail.proposals = detail.proposals;
    state.proposalsStaleForReading = false;
    state.proposalStaleMessage = null;
    state.proposalRefreshError = null;
    renderProposals();
  } catch (error) {
    state.proposalRefreshError = `完了した提案を表示できません: ${error.message}`;
    renderProposalQueueControls();
  }
}

function applyProposalQueue(payload, {announce = true} = {}) {
  const queue = normalizedProposalQueue(payload);
  const previous = state.proposalQueue;
  if (
    previous
    && previous.instance_id === queue.instance_id
    && queue.revision <= previous.revision
  ) {
    if (state.proposalQueueError) {
      state.proposalQueueError = null;
      renderProposalQueueControls();
    }
    return false;
  }
  const previousById = new Map(
    (previous?.jobs || []).map((job) => [job.job_id, job]),
  );
  const wasInitialized = state.proposalQueueInitialized;
  state.proposalQueue = queue;
  indexProposalJobs(queue);
  syncProposalProgressScope(queue, previous);
  state.proposalQueueInitialized = true;
  state.proposalQueueError = null;
  renderCaseList();
  renderLlmControls();

  if (!announce || !wasInitialized) return true;
  const terminal = new Set(["succeeded", "stale", "failed", "cancelled"]);
  const completed = queue.jobs.filter((job) => {
    if (!terminal.has(job.status)) return false;
    const oldStatus = previousById.get(job.job_id)?.status;
    return oldStatus !== job.status && !terminal.has(oldStatus);
  });
  if (!completed.length) return true;

  const succeeded = completed.filter((job) => job.status === "succeeded");
  const needsAttention = completed.length - succeeded.length;
  void loadCases({preserveSelection: true}).then(() => {
    state.proposalRefreshError = null;
    renderProposalQueueControls();
  }).catch((error) => {
    state.proposalRefreshError = `提案件数を一覧へ反映できません: ${error.message}`;
    renderProposalQueueControls();
  });
  if (state.currentId && succeeded.some((job) => job.case_id === state.currentId)) {
    void refreshCurrentProposals(state.currentId);
  }
  toast(
    needsAttention
      ? `LLM提案 ${succeeded.length}件完了 · ${needsAttention}件は再確認が必要です`
      : `LLM提案 ${succeeded.length}件の取得が完了しました`,
  );
  const previousCounts = previous?.counts;
  const previouslyBusy = Boolean(
    previousCounts && (previousCounts.queued || previousCounts.running),
  );
  if (
    previouslyBusy
    && !proposalQueueHasActiveJobs()
    && !state.llmCatalog
    && !state.llmCatalogLoading
  ) {
    void loadLlmCatalog();
  }
  return true;
}

function scheduleProposalQueuePoll() {
  window.clearTimeout(state.proposalQueueTimer);
  const activeDelay = document.hidden ? 2500 : 800;
  const delay = proposalQueueHasActiveJobs() ? activeDelay : 5000;
  state.proposalQueueTimer = window.setTimeout(() => {
    void loadProposalQueue();
  }, delay);
}

async function loadProposalQueue({announce = true} = {}) {
  if (state.proposalQueueLoading) return false;
  const requestGeneration = ++state.proposalQueueRequestGeneration;
  state.proposalQueueLoading = true;
  if (!state.proposalQueue) renderProposalQueueControls();
  try {
    const payload = await apiJson("/api/proposal-jobs");
    if (requestGeneration !== state.proposalQueueRequestGeneration) return false;
    applyProposalQueue(payload, {announce});
    return true;
  } catch (error) {
    if (requestGeneration !== state.proposalQueueRequestGeneration) return false;
    state.proposalQueueError = `提案キューを取得できません: ${error.message}`;
    return false;
  } finally {
    if (requestGeneration === state.proposalQueueRequestGeneration) {
      state.proposalQueueLoading = false;
      renderProposalQueueControls();
      scheduleProposalQueuePoll();
    }
  }
}

async function enqueueProposalCases(caseIds, {clearSelection = false} = {}) {
  if (!state.meta?.llm.enabled || state.proposalEnqueueing) return false;
  const requestedCaseIds = [...new Set(caseIds)].filter(Boolean);
  if (!requestedCaseIds.length) return false;
  const includesCurrent = requestedCaseIds.includes(state.currentId);
  if (includesCurrent && !applyOpenCorrectedReadingEditor()) return false;
  if (includesCurrent && state.saving) {
    toast("現在の保存完了後にもう一度キューへ追加してください");
    return false;
  }

  state.proposalEnqueueing = true;
  state.proposalQueueError = null;
  renderLlmControls();
  renderProposalQueueControls();
  try {
    const useSavedSettings = state.llmSettingsDirty
      && proposalQueueHasActiveJobs();
    if (state.llmSettingsDirty && !useSavedSettings) {
      const settingsSaved = await saveLlmSettings({quiet: true});
      if (!settingsSaved) return false;
    }
    if (includesCurrent && (state.dirty || state.readingChangeSavePending)) {
      const caseId = state.currentId;
      const saved = await saveCurrent({
        advance: false,
        actionType: "save-before-proposal-queue",
        quiet: false,
      });
      if (!saved || state.currentId !== caseId || state.pendingAdvance) return false;
    }
    const queueWasActive = proposalQueueHasActiveJobs();
    const result = await apiJson("/api/proposal-jobs", {
      method: "POST",
      body: JSON.stringify({
        case_ids: requestedCaseIds,
        llm_settings_revision: state.llmSettingsRevision,
        client_request_id: newProposalBatchId(),
      }),
    });
    if (!queueWasActive) state.proposalProgressJobIds.clear();
    for (const job of result.jobs || []) {
      state.proposalProgressJobIds.add(job.job_id);
    }
    applyProposalQueue(result.queue, {announce: false});
    if (clearSelection) {
      for (const caseId of requestedCaseIds) {
        state.selectedCaseIds.delete(caseId);
      }
    }
    renderCaseList();
    renderProposalQueueControls();
    hideAlert();
    if (result.enqueued_count) {
      toast(
        `${result.enqueued_count}件を提案キューへ追加しました`
        + (result.deduplicated_count
          ? `（${result.deduplicated_count}件は追加済み）`
          : ""),
      );
    } else {
      toast("選択したケースはすでに提案キューへ追加済みです");
    }
    if (useSavedSettings) {
      toast("未保存のLLM設定は今回のキューへ反映せず保持しています");
    }
    scheduleProposalQueuePoll();
    return true;
  } catch (error) {
    if (error.status === 409) {
      try {
        const latestMeta = await apiJson("/api/meta");
        applyMeta(latestMeta);
        state.llmSettingsRevision = latestMeta.llm.settings_revision;
        renderMeta();
      } catch {
        // Keep the original enqueue error when meta refresh also fails.
      }
    }
    state.proposalQueueError = error.message;
    showAlert("提案をキューへ追加できません", error.message);
    return false;
  } finally {
    state.proposalEnqueueing = false;
    renderLlmControls();
    renderProposalQueueControls();
  }
}

async function enqueueSelectedProposals() {
  const caseIds = [...state.selectedCaseIds];
  return enqueueProposalCases(caseIds, {clearSelection: true});
}

async function requestProposals() {
  if (!state.currentId) return false;
  return enqueueProposalCases([state.currentId]);
}

function addPath() {
  if (!ensureReadingChangeSaved()) return;
  const preannotation = state.detail.case.preannotation;
  state.draft.acceptable_paths.push({
    path_id: newPathId(),
    status: "draft",
    surface_reference_id: "surface-0",
    reading_boundaries: hasCorrectedReading()
      ? []
      : [...(preannotation.boundaries_after || [])],
    surface_boundaries: null,
    alignment_status: "reading_only",
    provenance: {kind: "human"},
  });
  state.activePathIndex = state.draft.acceptable_paths.length - 1;
  state.pathInputError = null;
  renderPathArea();
  markDirty("add-path");
}

function duplicatePath() {
  if (!ensureReadingChangeSaved()) return;
  const path = currentPath();
  if (!path) return;
  const copy = deepCopy(path);
  copy.path_id = newPathId("copy");
  copy.status = "draft";
  copy.provenance = {kind: "human", source_path_id: path.path_id};
  state.draft.acceptable_paths.push(copy);
  state.activePathIndex = state.draft.acceptable_paths.length - 1;
  state.pathInputError = null;
  renderPathArea();
  markDirty("duplicate-path");
}

function deletePath() {
  if (!ensureReadingChangeSaved()) return;
  const path = currentPath();
  if (!path) return;
  if (!window.confirm(`経路「${path.path_id}」を削除しますか？`)) return;
  state.draft.acceptable_paths.splice(state.activePathIndex, 1);
  state.activePathIndex = Math.max(0, state.activePathIndex - 1);
  state.pathInputError = null;
  renderPathArea();
  markDirty("delete-path");
}

function beginSurfaceAlignment() {
  if (!ensureReadingChangeSaved()) return;
  const path = currentPath();
  if (!path) return;
  const surfaceIndex = Number(path.surface_reference_id.replace("surface-", ""));
  const alternative = hasCorrectedReading()
    ? null
    : state.detail.case.preannotation.alternatives?.find(
      (item) => item.index === surfaceIndex
        && JSON.stringify(item.boundaries_after) === JSON.stringify(path.reading_boundaries),
    );
  if (alternative) {
    let offset = 0;
    path.surface_boundaries = alternative.segments.slice(0, -1).map((segment) => {
      offset += codePointLength(segment.surface);
      return offset;
    });
    path.alignment_status = "aligned";
    state.pathInputError = null;
    renderPathArea();
    markDirty("start-alignment");
    return;
  }
  if (!path.reading_boundaries.length) {
    path.surface_boundaries = [];
    path.alignment_status = "aligned";
    state.pathInputError = null;
    renderPathArea();
    markDirty("start-alignment");
    return;
  }
  showAlert(
    "表層位置を推測しません",
    "この読み境界に一致する検証済み表層対応がありません。LLM候補を下書きへコピーするか、境界なしの経路から対で分割してください。",
  );
}

function returnToReadingOnly() {
  if (!ensureReadingChangeSaved()) return;
  const path = currentPath();
  if (!path) return;
  if (!window.confirm("表層境界だけを外し、読み境界を残しますか？")) return;
  path.surface_boundaries = null;
  path.alignment_status = "reading_only";
  state.pathInputError = null;
  renderPathArea();
  markDirty("remove-alignment");
}

async function downloadExport(path, filename) {
  try {
    if (state.detail) {
      if (!applyOpenCorrectedReadingEditor()) return;
      if (state.saving) {
        toast("保存完了後にもう一度、書き出してください");
        return;
      }
      if (state.dirty || state.readingChangeSavePending) {
        const saved = await saveCurrent({
          advance: false,
          actionType: "save-before-export",
          quiet: false,
        });
        if (
          !saved
          || state.saving
          || state.dirty
          || state.readingChangeSavePending
        ) return;
      }
    }
    const response = await api(path);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast(`${filename}を書き出しました`);
  } catch (error) {
    showAlert("書き出せません", error.message);
  }
}

function scheduleFilterRefresh() {
  window.clearTimeout(state.filterTimer);
  state.filterTimer = window.setTimeout(() => {
    loadCases({preserveSelection: true}).catch((error) => {
      showAlert("一覧を更新できません", error.message);
    });
  }, 180);
}

function bindEvents() {
  $("dismiss-alert").addEventListener("click", hideAlert);
  dom.reloadConflict.addEventListener("click", () => {
    const id = state.currentId;
    state.dirty = false;
    selectCase(id, {saveFirst: false, force: true});
  });
  $("toggle-queue").addEventListener("click", (event) => {
    const body = $("queue-body");
    body.hidden = !body.hidden;
    event.currentTarget.textContent = body.hidden ? "+" : "−";
    event.currentTarget.setAttribute("aria-expanded", body.hidden ? "false" : "true");
    event.currentTarget.setAttribute(
      "aria-label",
      body.hidden ? "対象一覧を展開する" : "対象一覧を折りたたむ",
    );
  });
  for (const control of [
    dom.search,
    dom.statusFilter,
    dom.categoryFilter,
    dom.longOnly,
    dom.adjudicationOnly,
  ]) {
    control.addEventListener(control === dom.search ? "input" : "change", scheduleFilterRefresh);
  }
  dom.selectVisibleProposals.addEventListener("change", () => {
    for (const summary of state.cases) {
      if (dom.selectVisibleProposals.checked) {
        state.selectedCaseIds.add(summary.id);
      } else {
        state.selectedCaseIds.delete(summary.id);
      }
    }
    renderCaseList();
    renderProposalQueueControls();
  });
  dom.clearProposalSelection.addEventListener("click", () => {
    state.selectedCaseIds.clear();
    renderCaseList();
    renderProposalQueueControls();
  });
  dom.queueSelectedProposals.addEventListener("click", () => {
    void enqueueSelectedProposals();
  });
  $("next-pending").addEventListener("click", async () => {
    try {
      if (!applyOpenCorrectedReadingEditor()) return;
      if (state.dirty || state.readingChangeSavePending || state.saving) {
        await saveCurrent({
          advance: true,
          advanceTarget: null,
          actionType: "next-pending",
          quiet: false,
        });
        return;
      }
      const result = await apiJson("/api/cases?status=pending");
      const target = result.cases.find((item) => item.id !== state.currentId);
      if (!target) toast("次の未確認ケースはありません");
      else await selectCase(target.id, {saveFirst: false});
    } catch (error) {
      showAlert("次の未確認へ移動できません", error.message);
    }
  });
  dom.previousCase.addEventListener("click", () => moveCase(-1));
  dom.nextCase.addEventListener("click", () => moveCase(1));
  dom.pathSetStatus.addEventListener("change", () => {
    const previousStatus = state.draft.path_set_status;
    const nextStatus = dom.pathSetStatus.value;
    if (nextStatus === "invalid") {
      if (state.draft.acceptable_paths.length && !window.confirm("無効入力にすると編集中の経路を削除します。続けますか？")) {
        dom.pathSetStatus.value = previousStatus;
        return;
      }
    }
    state.draft.path_set_status = nextStatus;
    if (nextStatus === "invalid") {
      state.draft.acceptable_paths = [];
      state.draft.needs_adjudication = false;
      state.pathInputError = null;
      dom.needsAdjudication.checked = false;
      state.activePathIndex = 0;
    }
    renderPathArea();
    markDirty("status-change");
  });
  dom.needsAdjudication.addEventListener("change", () => {
    state.draft.needs_adjudication = dom.needsAdjudication.checked;
    renderPathArea();
    markDirty("adjudication-change");
  });
  dom.notes.addEventListener("input", () => {
    state.draft.notes = dom.notes.value || null;
    markDirty("notes");
  });
  dom.editCorrectedReading.addEventListener("click", showCorrectedReadingEditor);
  $("cancel-corrected-reading").addEventListener("click", () => {
    hideCorrectedReadingEditor({restoreFocus: true});
  });
  $("apply-corrected-reading").addEventListener("click", applyCorrectedReading);
  dom.resetCorrectedReading.addEventListener("click", () => {
    changeCorrectedReading(null);
  });
  dom.correctedReadingInput.addEventListener("input", () => {
    dom.correctedReadingError.hidden = true;
    dom.correctedReadingError.textContent = "";
  });
  dom.correctedReadingInput.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    hideCorrectedReadingEditor({restoreFocus: true});
  });
  dom.llmModel.addEventListener("change", handleLlmModelSelectionChange);
  dom.llmModelCustom.addEventListener("input", () => {
    state.llmCatalogNotice = null;
    markLlmSettingsDirty();
    renderLlmCatalogStatus();
  });
  dom.llmEffort.addEventListener("change", () => {
    renderLlmCustomFields();
    state.llmCatalogNotice = null;
    markLlmSettingsDirty();
    renderLlmCatalogStatus();
  });
  dom.llmEffortCustom.addEventListener("input", () => {
    state.llmCatalogNotice = null;
    markLlmSettingsDirty();
    renderLlmCatalogStatus();
  });
  dom.refreshLlmCatalog.addEventListener("click", () => {
    void loadLlmCatalog();
  });
  dom.saveLlmSettings.addEventListener("click", () => saveLlmSettings());
  dom.requestProposals.addEventListener("click", requestProposals);
  $("add-path").addEventListener("click", addPath);
  $("create-first-path").addEventListener("click", addPath);
  $("duplicate-path").addEventListener("click", duplicatePath);
  $("delete-path").addEventListener("click", deletePath);
  dom.surfaceSelect.addEventListener("change", () => {
    const path = currentPath();
    path.surface_reference_id = dom.surfaceSelect.value;
    if (path.alignment_status === "aligned") {
      path.alignment_status = "reading_only";
      path.surface_boundaries = null;
    }
    state.pathInputError = null;
    renderPathArea();
    markDirty("surface-reference-change");
  });
  dom.pathStatus.addEventListener("change", () => {
    const path = currentPath();
    path.status = dom.pathStatus.value;
    if (path.status === "acceptable" && state.draft.path_set_status === "pending") {
      state.draft.path_set_status = "open";
      dom.pathSetStatus.value = "open";
    }
    renderPathArea();
    markDirty("path-status-change");
  });
  dom.markedReadingInput.addEventListener("input", () => {
    const path = currentPath();
    try {
      path.reading_boundaries = parseMarkedReading(
        dom.markedReadingInput.value,
        effectiveReading(),
      );
      state.pathInputError = null;
      showPathValidation(null);
      renderReadingGapEditor(path);
      markDirty("marked-reading-boundary");
    } catch (error) {
      state.pathInputError = error.message;
      showPathValidation(state.pathInputError);
      setStatusPill(dom.saveStatus, "入力確認", "warning");
      markDirty("marked-reading-invalid");
    }
  });
  $("start-surface-alignment").addEventListener("click", beginSurfaceAlignment);
  $("return-reading-only").addEventListener("click", returnToReadingOnly);
  $("save-case").addEventListener("click", () => saveCurrent());
  $("save-next").addEventListener("click", () => saveCurrent({advance: true}));
  $("export-reviews").addEventListener("click", () => (
    downloadExport("/api/export/reviews.jsonl", "reviewed-paths.jsonl")
  ));
  $("export-manifest").addEventListener("click", () => (
    downloadExport("/api/export/manifest.json", "manifest.json")
  ));
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      if (!dom.correctedReadingEditor.hidden) {
        applyCorrectedReading();
        if (!dom.correctedReadingEditor.hidden) return;
      }
      saveCurrent({advance: true});
      return;
    }
    if (event.altKey && event.key === "ArrowLeft") {
      event.preventDefault();
      moveCase(-1);
      return;
    }
    if (event.altKey && event.key === "ArrowRight") {
      event.preventDefault();
      moveCase(1);
      return;
    }
    if (!["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)
      && ["1", "2", "3"].includes(event.key)) {
      const entry = flattenLatestProposalPaths()[Number(event.key) - 1];
      if (entry) {
        event.preventDefault();
        copyProposal(entry.path, false);
      }
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (
      dom.correctedReadingEditor.hidden
      && !state.dirty
      && !state.llmSettingsDirty
    ) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

async function initialize() {
  bindEvents();
  if (!state.token) {
    dom.editorLoading.hidden = true;
    dom.editorEmpty.hidden = false;
    showAlert("起動URLが不完全です", "サーバーが表示した ?token= 付きURLを開いてください。");
    return;
  }
  try {
    applyMeta(await apiJson("/api/meta"));
    renderMeta();
    fillCategoryFilter();
    await loadProposalQueue({announce: false});
    if (!proposalQueueHasActiveJobs()) void loadLlmCatalog();
    await loadCases({preserveSelection: false});
    if (!state.cases.length) {
      dom.editorLoading.hidden = true;
      dom.editorEmpty.hidden = false;
    }
  } catch (error) {
    dom.editorLoading.hidden = true;
    dom.editorEmpty.hidden = false;
    showAlert("アノテーションUIを開始できません", error.message);
  }
}

initialize();
