(() => {
  const API = "/api";
  const LOCAL_KEY = "classroom-medical-local-courses-v1";
  const SUBJECTS = [
    { id: "pathology", label: "病理學", icon: "◉" },
    { id: "pharmacology", label: "藥理學", icon: "⚗" },
    { id: "clinical", label: "臨床醫學", icon: "✚" },
    { id: "laboratory", label: "檢驗醫學", icon: "⌕" }
  ];
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = {
    courses: [], currentId: null, detail: null, job: null, jobTimer: null,
    previewRevision: 0, previewJobId: null, activePreviewSegment: null,
    localCourses: readLocalCourses(), localAssets: {}, currentFiles: {}, audio: null,
    uploadedAssets: readUploadedAssets(), skippedByCourse: readSkippedByCourse(),
    objectUrls: {}, virtualTime: 0, virtualPlaying: false, virtualTimer: null,
    lastTime: 0, seeking: false, followTranscript: true, activeSegment: null, followedSegment: null,
    transcriptReadingMode: false, transcriptNativeFullscreen: false, transcriptFontScale: 2,
    skipIds: new Set(), saveTimer: null, quizQueue: [], quizIndex: 0,
    quizQuestion: null, quizSelected: null, quizAnswer: null, quizMode: "playback", practiceShouldResume: false,
    questionPage: 1, questionPageSize: 20, questionCategory: "unanalysed",
    apiOnline: true, courseLoading: false, searchText: "", lastUserScroll: 0,
    handoutBusy: {}, handoutErrors: {}, selectedHandouts: {},
    assetUploading: {}, assetUploadErrors: {}, audioQueues: {}, audioPartBusy: {}, audioPartErrors: {}, transcriptionSettingsSaving: {}, transcriptionSettingsSavePromises: {}, subjectFilter: null, page: "courses",
    bankManager: null, wrongReview: null, listening: null, listeningQueue: Promise.resolve(), routeToken: 0
  };

  function readLocalCourses() {
    try { return JSON.parse(localStorage.getItem(LOCAL_KEY) || "[]"); } catch { return []; }
  }
  function readUploadedAssets() {
    try { return JSON.parse(localStorage.getItem(`${LOCAL_KEY}:uploads`) || "{}"); } catch { return {}; }
  }
  function readSkippedByCourse() {
    try { return JSON.parse(localStorage.getItem(`${LOCAL_KEY}:skipped`) || "{}"); } catch { return {}; }
  }
  function persistSkipped() {
    state.skippedByCourse[state.currentId] = [...state.skipIds];
    try { localStorage.setItem(`${LOCAL_KEY}:skipped`, JSON.stringify(state.skippedByCourse)); } catch { /* current session still retains skip state */ }
  }
  function writeLocalCourses() {
    try {
      const clean = state.localCourses.map(c => ({ ...c, audio: c.audio ? { ...c.audio, object_url: undefined } : null }));
      localStorage.setItem(LOCAL_KEY, JSON.stringify(clean));
    } catch (error) { console.warn("Could not save local course state", error); }
  }
  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }
  function subjectLabel(id) { return SUBJECTS.find(subject => subject.id === id)?.label || "未分類科目"; }
  function subjectId(course) { return SUBJECTS.some(subject => subject.id === course?.subject_id) ? course.subject_id : "pathology"; }
  function coursesForSubject(id) { return state.courses.filter(course => subjectId(course) === id); }
  function num(value, fallback = 0) {
    if (value == null || value === "") return fallback;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  function seconds(value) {
    if (typeof value === "number") return value;
    if (value == null || value === "") return NaN;
    const raw = String(value).trim();
    if (/^\d+(?:\.\d+)?$/.test(raw)) return Number(raw);
    const parts = raw.split(":").map(Number);
    if (parts.some(part => !Number.isFinite(part)) || parts.length > 3) return NaN;
    return parts.reduce((acc, part) => acc * 60 + part, 0);
  }
  function clock(value, withHours = false) {
    const total = Math.max(0, Math.floor(num(value)));
    const hours = Math.floor(total / 3600), minutes = Math.floor((total % 3600) / 60), secs = total % 60;
    return withHours || hours ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  function toast(message, kind = "") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    $("#toast-region").append(node);
    setTimeout(() => node.remove(), 3600);
  }
  async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, options);
    const raw = await response.text();
    let body = null;
    if (raw) { try { body = JSON.parse(raw); } catch { body = { message: raw }; } }
    if (!response.ok) {
      const error = new Error(body?.error || body?.message || `請求失敗（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    state.apiOnline = true;
    return body;
  }
  function localCourse(id = state.currentId) { return state.localCourses.find(course => String(course.id) === String(id)); }
  function isLocalId(id = state.currentId) { return String(id || "").startsWith("local-"); }
  function normalizeCourses(data) {
    const remote = Array.isArray(data) ? data : (data?.courses || data?.items || []);
    const local = state.localCourses;
    const byId = new Map([...local, ...remote].map(course => [String(course.id), course]));
    return [...byId.values()].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  }
  function normalizeDetail(detail) {
    const result = detail || {};
    result.handouts = Array.isArray(result.handouts) ? result.handouts : [];
    result.segments = Array.isArray(result.segments) ? result.segments : [];
    result.questions = Array.isArray(result.questions) ? result.questions : [];
    result.matches = Array.isArray(result.matches) ? result.matches : [];
    result.attempts = Array.isArray(result.attempts) ? result.attempts : [];
    result.audio_parts = Array.isArray(result.audio_parts) ? result.audio_parts : [];
    result.playback = result.playback || { position_seconds: 0 };
    result.audio = result.audio || null;
    result.transcription_preview = result.transcription_preview || null;
    result.learning = result.learning || { round_id: null, round_number: 1, listened_seconds: 0, duration_seconds: 0, progress: 0, completed: false, completed_rounds: 0 };
    return result;
  }
  function setPreviewBaseline(preview) {
    state.previewJobId = preview?.job_id == null ? null : String(preview.job_id);
    state.previewRevision = num(preview?.revision, 0);
  }
  function previewSegments(preview = state.detail?.transcription_preview) {
    return Array.isArray(preview?.segments) ? [...preview.segments].sort((a, b) => num(a.start) - num(b.start)) : [];
  }
  function previewStatusFailed(preview = state.detail?.transcription_preview) {
    const statuses = [preview?.status, state.job?.status].map(status => String(status || "").toLowerCase());
    return statuses.some(status => ["partial", "error", "failed", "failure"].includes(status));
  }
  function previewIsWorking() {
    if (String(state.job?.kind || "").startsWith("medical_")) return false;
    const status = String(state.job?.status || state.detail?.processing_status || "").toLowerCase();
    return ["processing", "queued", "pending", "running", "started"].includes(status) && !isMatchAnalysisRunning();
  }
  function shouldShowTranscriptionPreview(detail = state.detail) {
    if (String(state.job?.kind || "").startsWith("medical_")) return false;
    const preview = detail?.transcription_preview;
    const courseStatus = String(detail?.processing_status || "").toLowerCase();
    const jobStatus = String(state.job?.status || "").toLowerCase();
    if (["completed", "complete", "done", "succeeded", "success"].includes(jobStatus)) return false;
    if (preview && !["completed", "complete", "done", "succeeded", "success"].includes(String(preview.status || "").toLowerCase())) return true;
    if (["completed", "complete", "done", "succeeded", "success"].includes(courseStatus)) return false;
    if (previewIsWorking()) return true;
    return !(detail?.segments?.length);
  }
  function getCourseMatches(detail = state.detail) {
    return detail?.matches || [];
  }
  function matchFor(question, detail = state.detail) {
    if (!question) return null;
    return getCourseMatches(detail).find(match => String(match.question_id) === String(question.id)) || null;
  }
  const QUESTION_CATEGORIES = [
    { id: "unanalysed", label: "尚未分析" },
    { id: "matched", label: "已配對時間" },
    { id: "excluded", label: "非本堂課" },
    { id: "review", label: "待確認" }
  ];
  function hasQuestionTime(match) {
    return match?.question_time != null && match.question_time !== "" && Number.isFinite(seconds(match.question_time));
  }
  function questionBucket(question) {
    const match = matchFor(question);
    if (match?.status === "disabled" || (["openai", "handout_local", "codex"].includes(match?.provider) && match?.status === "pending_confirmation")) return "review";
    if (["confirmed", "active", "skipped"].includes(match?.status) && hasQuestionTime(match)) return "matched";
    if (["openai", "codex"].includes(match?.provider) && match?.status === "unmatched") return "excluded";
    return "unanalysed";
  }
  function questionView(questions = state.detail?.questions || []) {
    const counts = Object.fromEntries(QUESTION_CATEGORIES.map(category => [category.id, 0]));
    questions.forEach(question => { counts[questionBucket(question)] += 1; });
    const visible = questions.filter(question => questionBucket(question) === state.questionCategory);
    const pageCount = Math.max(1, Math.ceil(visible.length / state.questionPageSize));
    state.questionPage = Math.max(1, Math.min(state.questionPage, pageCount));
    const first = (state.questionPage - 1) * state.questionPageSize;
    return { counts, visible, pageItems: visible.slice(first, first + state.questionPageSize), pageCount, first, last: Math.min(first + state.questionPageSize, visible.length) };
  }
  function questionFilterHtml(questions = state.detail?.questions || []) {
    const { counts } = questionView(questions);
    return QUESTION_CATEGORIES.map(category => `<button class="tiny-button filter-chip ${state.questionCategory === category.id ? "active" : ""}" role="tab" data-question-filter="${category.id}" aria-selected="${state.questionCategory === category.id}" aria-pressed="${state.questionCategory === category.id}">${category.label} ${counts[category.id]}</button>`).join("");
  }
  function questionCategoryHint() {
    return {
      unanalysed: "尚未分析題目會留在這裡；逐字稿找不到候選片段的題目也可能留在此分類。",
      matched: "顯示已確認出題時間的題目。",
      excluded: "依目前提供的逐字稿判定為非本堂課內容。",
      review: "顯示候選時間仍待確認，或配對已停用的題目；僅用講義配對時，尚無逐字稿時間證據。"
    }[state.questionCategory];
  }
  function questionAnalysisActionHtml() {
    if (state.questionCategory !== "unanalysed") return "";
    const { counts } = questionView();
    const hasEvidence = Boolean(state.detail?.segments?.length || state.detail?.handouts?.length);
    const disabled = isLocalId() || state.handoutBusy[state.currentId] || counts.unanalysed === 0 || isAnyJobRunning() || !hasEvidence;
    const title = isLocalId() ? "試用課程不會呼叫 OpenAI。" : !hasEvidence ? "請先整理逐字稿或上傳講義。" : "每次最多分析 20 題；已分析過的題目會略過。";
    return `<button class="primary-button" id="analyze-matches" title="${title}" ${disabled ? "disabled" : ""}>${isMatchAnalysisRunning() ? "配對中…" : "API 配對"}</button>`;
  }
  function syncQuestionAnalysisAction() {
    const slot = $("#question-analysis-action");
    if (!slot) return;
    slot.innerHTML = questionAnalysisActionHtml();
    $("#analyze-matches")?.addEventListener("click", analyzeMatches);
  }
  function questionFooterHtml() {
    const { visible, pageCount, first, last } = questionView();
    return `<footer class="question-footer"><span>${state.detail.attempts.length} 筆作答 · ${state.skipIds.size} 題已跳過 · 顯示 ${visible.length ? first + 1 : 0}–${last} / ${visible.length}</span><div class="question-pagination"><button class="page-button" id="question-prev" aria-label="上一頁" ${state.questionPage <= 1 ? "disabled" : ""}>‹</button><span>第 ${state.questionPage} / ${pageCount} 頁</span><button class="page-button" id="question-next" aria-label="下一頁" ${state.questionPage >= pageCount ? "disabled" : ""}>›</button></div></footer>`;
  }
  function attemptFor(question, detail = state.detail) {
    if (!question) return null;
    const roundId = detail?.learning?.round_id;
    return [...(detail?.attempts || [])].reverse().find(attempt => String(attempt.question_id ?? attempt.questionId) === String(question.id)
      && (roundId == null || attempt.round_id == null || String(attempt.round_id) === String(roundId))) || null;
  }
  function optionList(question) {
    const options = question?.options;
    if (Array.isArray(options)) return options.map((item, index) => typeof item === "string"
      ? { key: String.fromCharCode(65 + index), text: item }
      : { key: String(item.key ?? item.label ?? String.fromCharCode(65 + index)), text: String(item.text ?? item.value ?? "") });
    if (options && typeof options === "object") return Object.entries(options).map(([key, text]) => ({ key, text: String(text) }));
    return [];
  }
  function usableQuizContent(question) {
    if (!String(question?.stem || "").trim()) return false;
    const options = optionList(question);
    const keys = options.map(option => String(option.key || "").trim().toUpperCase());
    if (options.length < 2 || options.some(option => !String(option.text || "").trim() || !String(option.key || "").trim())) return false;
    if (new Set(keys).size !== keys.length) return false;
    const answerKey = String(question.correct_option ?? question.answer_key ?? "").trim().toUpperCase();
    if (answerKey && !keys.includes(answerKey)) return false;
    // Remote answers stay hidden until a learner submits; local quizzes need a key to score safely.
    return !isLocalId() || Boolean(answerKey && keys.includes(answerKey));
  }
  function validQuestionTime(match) {
    const time = num(match?.question_time, NaN);
    const duration = courseDuration();
    return Number.isFinite(time) && time >= 0 && (duration <= 0 || time <= duration + 0.05);
  }
  function quizReady(question) {
    if (!question || question.needs_review || !usableQuizContent(question)) return false;
    const match = matchFor(question);
    return validQuestionTime(match) && ["confirmed", "skipped", "active"].includes(match?.status);
  }
  function courseAudioSource(detail = state.detail) {
    const id = detail?.id;
    if (detail?.audio?.object_url) return detail.audio.object_url;
    const localFile = state.currentFiles.audio;
    if (isLocalId(id) && localFile) {
      state.objectUrls[id] ||= URL.createObjectURL(localFile);
      return state.objectUrls[id];
    }
    if (isLocalId(id)) return "";
    if (detail?.audio?.url) return detail.audio.url;
    if (detail?.audio?.available === false) return "";
    if (detail?.audio?.filename || detail?.audio?.duration_seconds || detail?.audio?.duration) return `${API}/courses/${encodeURIComponent(id)}/audio`;
    return "";
  }
  function hasAudio(detail = state.detail) {
    return Boolean(courseAudioSource(detail));
  }
  function courseDuration(detail = state.detail) {
    const recordedDuration = num(detail?.duration_seconds);
    if (recordedDuration > 0) return recordedDuration;
    if (num(detail?.audio?.duration_seconds) > 0) return num(detail.audio.duration_seconds);
    if (num(detail?.audio?.duration) > 0) return num(detail.audio.duration);
    if (num(state.audio?.duration) > 0 && Number.isFinite(state.audio.duration)) return state.audio.duration;
    const last = [...(detail?.segments || [])].sort((a, b) => num(b.end) - num(a.end))[0];
    const lastMatch = [...(detail?.matches || [])].filter(match => ["confirmed", "skipped", "active"].includes(match.status)).sort((a, b) => num(b.question_time) - num(a.question_time))[0];
    return Math.max(0, num(last?.end), num(lastMatch?.question_time));
  }
  function audioDuration(detail = state.detail) {
    const values = [detail?.duration_seconds, detail?.audio?.duration_seconds, detail?.audio?.duration,
      detail?.assets?.audio?.duration_seconds, detail?.assets?.audio?.duration];
    for (const value of values) {
      const duration = Number(value);
      if (value != null && value !== "" && Number.isFinite(duration) && duration > 0) return duration;
    }
    return Number.isFinite(state.audio?.duration) && state.audio.duration > 0 ? state.audio.duration : 0;
  }
  function timelineAvailable(detail = state.detail) {
    return hasAudio(detail) || Boolean(detail?.segments?.length) || (detail?.matches || []).some(match => ["confirmed", "skipped", "active"].includes(match.status) && Number.isFinite(num(match.question_time, NaN)));
  }
  function activeTime() {
    return hasAudio() && state.audio ? num(state.audio.currentTime) : state.virtualTime;
  }
  function elapsedFromCourse(course) {
    const date = course.created_at ? new Date(course.created_at) : null;
    return date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString("zh-TW", { month: "short", day: "numeric" }) : "新建立";
  }
  function friendlyStatus(status) {
    const key = String(status || "").toLowerCase();
    if (["processing", "queued", "pending", "running", "started"].includes(key)) return ["處理中", "processing"];
    if (["error", "failed", "failure"].includes(key)) return ["需處理", "error"];
    if (["ready", "completed", "complete", "done", "succeeded", "success"].includes(key)) return ["已整理", ""];
    return ["待匯入", ""];
  }
  function stopJobPolling() { clearTimeout(state.jobTimer); state.jobTimer = null; }

  async function loadCourses() {
    try {
      const data = await request("/courses");
      state.courses = normalizeCourses(data);
    } catch (error) {
      state.apiOnline = false;
      state.courses = normalizeCourses([]);
      if (!state.courses.length) state.listError = error.message;
    }
    renderCourseList();
  }
  function renderCourseList() {
    $$('[data-main-page]').forEach(button => {
      const active = button.dataset.mainPage === state.page;
      button.classList.toggle("active", active);
      button.setAttribute("aria-current", active ? "page" : "false");
    });
  }
  async function goOverview() {
    void exitTranscriptReadingMode();
    const routeToken = ++state.routeToken;
    const courseId = state.currentId;
    if (state.audio && !state.audio.paused) state.audio.pause();
    if (state.virtualPlaying) { state.virtualPlaying = false; clearInterval(state.virtualTimer); }
    clearTimeout(state.saveTimer); endListeningTracking(); await state.listeningQueue;
    if (routeToken !== state.routeToken) return;
    if (courseId && state.detail) await savePlayback(true);
    if (routeToken !== state.routeToken) return;
    stopJobPolling(); clearInterval(state.virtualTimer); state.virtualPlaying = false;
    state.currentId = null; state.detail = null; state.audio = null; state.currentFiles = {};
    renderOverview(); renderCourseList();
  }
  async function pauseCourseForModal() {
    const courseId = state.currentId;
    if (state.audio && !state.audio.paused) state.audio.pause();
    if (state.virtualPlaying) { state.virtualPlaying = false; clearInterval(state.virtualTimer); $("#play-toggle").textContent = "▶"; }
    clearTimeout(state.saveTimer); endListeningTracking(); await state.listeningQueue;
    if (courseId && String(state.currentId) === String(courseId) && state.detail) await savePlayback(true);
  }
  function setHeader(title) { $("#breadcrumb-current").textContent = title || "課程總覽"; }
  function renderOverview() {
    state.page = "courses";
    $("#app-content").classList.remove("minimal-course");
    state.detail = null; state.audio = null; state.currentFiles = {}; state.searchText = "";
    setHeader("課程"); renderCourseList();
    const courses = state.subjectFilter ? coursesForSubject(state.subjectFilter) : state.courses;
    $("#app-content").innerHTML = `<div class="minimal-heading"><h1>課程</h1><button class="primary-button" id="overview-create">＋ 新增</button></div>
      <nav class="minimal-tabs subject-tabs" aria-label="篩選科目">${[{id:"all",label:"全部"},...SUBJECTS].map(s => `<button data-filter="${s.id}" class="${(state.subjectFilter || "all") === s.id ? "active" : ""}">${s.label}</button>`).join("")}</nav>
      <div class="minimal-course-list">${courses.map(course => `<div class="minimal-course-entry"><button class="minimal-course-row" data-open-course="${esc(course.id)}"><span>${esc(course.title || "未命名課程")}</span><span class="row-meta">${esc(subjectLabel(subjectId(course)))}<span aria-hidden="true">›</span></span></button><button class="course-delete" data-delete-course="${esc(course.id)}" aria-label="刪除 ${esc(course.title)}">刪除</button></div>`).join("") || '<p class="minimal-empty">尚無課程</p>'}</div>`;
    $("#overview-create").onclick = showCreateCourse;
    $$('[data-filter]').forEach(button => button.onclick = () => { state.subjectFilter = button.dataset.filter === "all" ? null : button.dataset.filter; renderOverview(); });
    $$('[data-open-course]', $("#app-content")).forEach(button => button.onclick = () => openCourse(button.dataset.openCourse));
    $$('[data-delete-course]', $("#app-content")).forEach(button => button.onclick = () => {
      const id=button.dataset.deleteCourse, course=state.courses.find(c=>String(c.id)===id);
      const root=showModal("刪除課程", "", `<div class="modal-body"><p>刪除「${esc(course.title)}」及其逐字稿、作答紀錄？</p><div class="modal-foot"><button class="secondary-button" data-close-modal>取消</button><button class="primary-button" id="confirm-delete-course">刪除</button></div><p class="form-error" id="delete-course-error" hidden></p></div>`);
      $("#confirm-delete-course",root).onclick=async event=>{
        event.currentTarget.disabled=true;
        try {
          if(id.startsWith("local-")){
            const remaining=state.localCourses.filter(c=>String(c.id)!==id);
            localStorage.setItem(LOCAL_KEY,JSON.stringify(remaining));state.localCourses=remaining;
          }else await request(`/courses/${encodeURIComponent(id)}`,{method:"DELETE"});
          root.innerHTML="";await loadCourses();renderOverview();
        }catch(error){$("#delete-course-error",root).hidden=false;$("#delete-course-error",root).textContent=error.message;$("#confirm-delete-course",root).disabled=false;}
      };
    });
  }

  async function openCourse(id) {
    state.page = "courses";
    void exitTranscriptReadingMode();
    const routeToken = ++state.routeToken;
    const previousId = state.currentId;
    if (state.audio && !state.audio.paused) state.audio.pause();
    clearTimeout(state.saveTimer); endListeningTracking();
    await state.listeningQueue;
    if (routeToken !== state.routeToken) return;
    if (previousId && state.detail) await savePlayback(true);
    if (routeToken !== state.routeToken) return;
    stopJobPolling(); clearInterval(state.virtualTimer); state.virtualPlaying = false; state.audio = null;
    state.followTranscript = true; state.activeSegment = null; state.followedSegment = null;
    if (String(state.currentId) !== String(id)) { state.questionPage = 1; state.questionCategory = "unanalysed"; }
    state.currentId = String(id); state.courseLoading = true; state.job = null; state.quizQuestion = null; state.quizQueue = [];
    state.previewRevision = 0; state.previewJobId = null; state.activePreviewSegment = null;
    state.currentFiles = state.localAssets[state.currentId] || { ...(state.uploadedAssets[state.currentId] || {}) };
    renderCourseList();
    if (isLocalId(id)) {
      state.detail = normalizeDetail(structuredCloneSafe(localCourse(id)));
      state.courseLoading = false;
      state.virtualTime = num(state.detail.playback?.position_seconds);
      state.skipIds = new Set([...(state.skippedByCourse[state.currentId] || []), ...state.detail.matches.filter(match => match.status === "skipped").map(match => String(match.question_id))]);
      updateCourseSummary(state.detail);
      renderCourse();
      return;
    }
    $("#app-content").innerHTML = `<div class="loading-state"><span class="spinner"></span>正在開啟課程資料…</div>`;
    try {
      const detail = await request(`/courses/${encodeURIComponent(id)}`);
      if (routeToken !== state.routeToken || String(state.currentId) !== String(id)) return;
    state.detail = normalizeDetail(detail);
    setPreviewBaseline(state.detail.transcription_preview);
      state.courseLoading = false;
      state.virtualTime = num(state.detail.playback?.position_seconds);
      state.skipIds = new Set([...(state.skippedByCourse[state.currentId] || []), ...state.detail.matches.filter(match => match.status === "skipped").map(match => String(match.question_id))]);
      updateCourseSummary(state.detail);
      try {
        const latestJob = await request(`/courses/${encodeURIComponent(id)}/job`);
        if (routeToken !== state.routeToken || String(state.currentId) !== String(id)) return;
        state.job = latestJob?.job || latestJob;
      } catch { /* Course content remains available if job status cannot load. */ }
      renderCourse();
      if (["processing", "queued", "pending", "running", "started"].includes(String(state.job?.status || detail.processing_status || "").toLowerCase())) pollJob();
    } catch (error) {
      if (routeToken !== state.routeToken) return;
      state.courseLoading = false;
      toast(`無法載入課程：${error.message}`, "error");
      await loadCourses(); renderOverview();
    }
  }
  function updateCourseSummary(detail) {
    const course = state.courses.find(item => String(item.id) === String(detail?.id));
    if (!course || !detail) return;
    course.title = detail.title || course.title;
    course.learning = detail.learning || course.learning;
    course.processing_status = detail.processing_status || course.processing_status;
    course.transcription_chunk_minutes = transcriptionChunkMinutes(detail.transcription_chunk_minutes);
    course.question_count = detail.questions?.length ?? course.question_count;
    course.segment_count = detail.segments?.length ?? course.segment_count;
    renderCourseList();
  }
  function structuredCloneSafe(value) { return value ? JSON.parse(JSON.stringify(value)) : null; }
  function persistLocalDetail() {
    if (!isLocalId() || !state.detail) return;
    const index = state.localCourses.findIndex(course => String(course.id) === String(state.currentId));
    if (index >= 0) state.localCourses[index] = structuredCloneSafe(state.detail);
    writeLocalCourses();
    state.courses = normalizeCourses([]); renderCourseList();
  }
  function isRangeTranscriptionJob(job = state.job) {
    return String(job?.kind || job?.type || "").toLowerCase().includes("range");
  }
  function effectiveRangeLabel(job) {
    const range = job?.effective_range;
    let start = job?.effective_start_seconds ?? range?.start_seconds ?? range?.start;
    let end = job?.effective_end_seconds ?? range?.end_seconds ?? range?.end;
    if (typeof range === "string") return range;
    if (start != null && end != null && Number.isFinite(Number(start)) && Number.isFinite(Number(end))) return `${clock(Number(start))}–${clock(Number(end))}`;
    return "";
  }
  function statusBanner() {
    const status = String(state.detail?.processing_status || "").toLowerCase();
    const jobStatus = String(state.job?.status || "").toLowerCase();
    const failed = ["error", "failed", "failure"].includes(jobStatus || status);
    const working = ["processing", "queued", "pending", "running", "started"].includes(jobStatus || status);
    const done = ["completed", "complete", "done", "succeeded", "success"].includes(jobStatus || status);
    const rangeTranscription = isRangeTranscriptionJob(state.job);
    const matchAnalysis = state.job?.kind === "match_analysis";
    const medicalJob = String(state.job?.kind || "").startsWith("medical_");
    const audioFinalize = ["audio_finalize", "audio_merge"].includes(String(state.job?.kind || state.job?.type || "").toLowerCase());
    const rawProgress = num(state.job?.progress, done ? 1 : 0);
    const progress = Math.max(0, Math.min(100, rawProgress <= 1 ? rawProgress * 100 : rawProgress));
    const rawStage = String(state.job?.stage || "").toLowerCase();
    const transcribing = working && rawStage === "transcription";
    const waitingForTranscription = working && rawStage === "transcription_wait";
    const stageLabels = { suggest: "本地醫學校訂中", align: "校訂文字對齊中", rate_limit_wait: "等待 API 速率限制解除", candidate_search: "搜尋逐字稿候選片段", analyzing_matches: "比對題目與課堂內容", match_analysis: "分析題目對位", audio_merge: "合併錄音中" };
    const stage = rangeTranscription && working ? "局部重新辨識中" : audioFinalize && working && rawStage === "audio_merge" ? "合併錄音中" : transcribing ? "課堂錄音轉錄中" : waitingForTranscription ? "等待轉錄資源" : stageLabels[rawStage] || state.job?.stage || (working ? (matchAnalysis ? "使用 OpenAI 分析題目對位" : "整理課程資料") : done ? (medicalJob ? "本地校訂工作結束" : rangeTranscription ? "局部重新辨識完成" : matchAnalysis ? "本輪題目分析結束" : audioFinalize ? "錄音處理完成" : "課程已整理完成") : "課程資料");
    const rawMessage = state.job?.message || state.job?.error || (working ? "完成後會自動更新逐字稿與題目。" : "錄音上傳後會自動開始轉錄，並比對這個科目的題庫。");
    const message = matchAnalysis ? String(rawMessage).replace(/^分析完成/, "本輪分析結束") : rawMessage;
    if (!working && !failed && !done && !state.detail?.segments?.length && !state.detail?.questions?.length) return "";
    const transcriptionProgress = transcribing ? String(state.job?.message || "").match(/轉錄\s*(\d+)%/) : null;
    const visibleProgress = transcriptionProgress ? Math.max(0, Math.min(100, Number(transcriptionProgress[1]))) : progress;
    const progressLabel = transcribing && transcriptionProgress ? `轉錄 ${visibleProgress}%` : `${Math.round(progress)}%`;
    const effectiveRange = effectiveRangeLabel(state.job);
    const rangeNote = effectiveRange && !String(message).includes(effectiveRange) ? ` · 實際範圍 ${effectiveRange}` : "";
    const retryId = medicalJob ? "retry-medical-review" : audioFinalize ? "retry-finalize-audio" : matchAnalysis ? "retry-match-analysis" : rangeTranscription ? "retry-range-transcription" : "retry-process";
    const retryLabel = medicalJob ? "查看校訂紀錄" : audioFinalize ? "重新處理已上傳錄音" : matchAnalysis ? "繼續分析相關題目" : rangeTranscription ? "重新選擇時段" : "重試處理";
    return `<div class="process-banner ${failed ? "error" : done ? "done" : ""}"><span class="process-symbol">${failed ? "!" : done ? "✓" : working ? "◷" : "↗"}</span><div class="process-copy"><strong>${failed ? "處理遇到問題" : working ? esc(stage) : done ? (medicalJob ? "本地校訂工作結束" : rangeTranscription ? "局部重新辨識完成" : matchAnalysis ? "本輪題目分析結束" : audioFinalize ? "錄音處理完成" : "課程整理完成") : "課程資料"}</strong><small class="${failed ? "process-error" : ""}">${esc(message)}${esc(rangeNote)}</small>${working ? `<div class="progress-line"><span style="width:${visibleProgress}%"></span></div>` : ""}</div>${working ? `<span class="process-percent">${progressLabel}</span>` : failed ? `<button class="tiny-button" id="${retryId}">${retryLabel}</button>` : ""}</div>`;
  }
  function updateStatusBanner() {
    const markup = statusBanner();
    const current = $(".process-banner");
    if (current) { if (markup) current.outerHTML = markup; else current.remove(); }
    else if (markup) $(".asset-row")?.insertAdjacentHTML("beforebegin", markup);
    $("#retry-process")?.addEventListener("click", () => startProcessing($("#retry-process")));
    $("#retry-match-analysis")?.addEventListener("click", analyzeMatches);
    $("#retry-range-transcription")?.addEventListener("click", showRangeRetranscribeModal);
    $("#retry-medical-review")?.addEventListener("click", () => showMedicalReview());
    $("#retry-finalize-audio")?.addEventListener("click", () => { void finalizeAudioParts(); });
    const rematchButton = $("#rematch-local-text");
    if (rematchButton) {
      rematchButton.disabled = isLocalId() || isAnyJobRunning() || !state.detail?.segments?.length;
      rematchButton.innerHTML = isLocalId() ? "題庫配對" : "⌕ 重新配對題庫";
      rematchButton.title = isLocalId() ? "試用課程不會配對科目題庫。" : "以課程逐字稿重新比對目前科目的題庫。";
    }
    syncQuestionAnalysisAction();
    syncAssetCard();
    syncHandoutsPanel();
    syncTranscriptionSettings();
  }
  function assetState() {
    const audioAsset = state.detail?.assets?.audio || state.detail?.audio;
    const localFile = state.currentFiles?.audio;
    return { audio: Boolean(localFile?.uploaded || (isLocalId() && localFile instanceof File) || (!isLocalId() && (state.detail?.audio_parts?.length || audioAsset?.filename || audioAsset?.available))) };
  }
  function transcriptionChunkMinutes(value) { return Number(value) === 10 ? 10 : 5; }
  function isCourseJobRunning(courseId = state.currentId) {
    const active = ["queued", "pending", "running", "processing", "started"];
    const jobStatus = String(String(state.currentId) === String(courseId) ? state.job?.status || "" : "").toLowerCase();
    const detailStatus = String(String(state.detail?.id) === String(courseId) ? state.detail?.processing_status || "" : "").toLowerCase();
    return active.includes(jobStatus) || active.includes(detailStatus);
  }
  function transcriptionSettingsLocked(courseId = state.currentId) {
    const token = `${courseId}:audio`;
    return isLocalId(courseId) || isCourseJobRunning(courseId) || Boolean(state.assetUploading[token]) || Boolean(state.transcriptionSettingsSaving[courseId]);
  }
  function transcriptionSettingsHtml(detail) {
    const minutes = transcriptionChunkMinutes(detail.transcription_chunk_minutes);
    const hasAudio = assetState().audio;
    const isLocal = isLocalId(detail.id);
    const locked = transcriptionSettingsLocked(detail.id);
    const canRangeRetranscribe = hasAudio && audioDuration(detail) > 0;
    return `<section class="transcription-settings" aria-label="錄音轉錄設定"><div class="transcription-setting-copy"><label for="transcription-chunk-minutes">錄音分段轉錄</label><small id="transcription-setting-status">${isLocal ? "試用課程不會轉錄錄音。" : state.transcriptionSettingsSaving[detail.id] ? "設定儲存中…" : "5 分鐘會較頻繁重新辨識；10 分鐘可減少模型重新載入次數。分段仍可能有辨識錯誤；逐字稿會合併成同一條時間軸。"}</small></div><select id="transcription-chunk-minutes" data-chunk-setting="${esc(detail.id)}" aria-label="轉錄音訊分段長度" ${locked ? "disabled" : ""}><option value="5" ${minutes === 5 ? "selected" : ""}>每 5 分鐘</option><option value="10" ${minutes === 10 ? "selected" : ""}>每 10 分鐘</option></select>${!isLocal && hasAudio ? `<button class="secondary-button" id="retranscribe-audio" ${locked ? "disabled" : ""}>重新轉錄錄音</button><button class="secondary-button" id="retranscribe-range" ${locked || !canRangeRetranscribe ? "disabled" : ""} title="${canRangeRetranscribe ? "選取時間範圍重新辨識" : "需要已上傳錄音和可用的音檔時長"}">局部重新辨識</button>` : ""}</section>`;
  }
  function syncTranscriptionSettings() {
    const select = $(`[data-chunk-setting="${CSS.escape(String(state.currentId))}"]`);
    if (!select) return;
    select.disabled = transcriptionSettingsLocked(state.currentId);
    const note = $("#transcription-setting-status");
    if (note) note.textContent = state.transcriptionSettingsSaving[state.currentId] ? "設定儲存中…" : "5 分鐘會較頻繁重新辨識；10 分鐘可減少模型重新載入次數。分段仍可能有辨識錯誤；逐字稿會合併成同一條時間軸。";
    const button = $("#retranscribe-audio");
    if (button) { button.disabled = transcriptionSettingsLocked(state.currentId); button.textContent = "重新轉錄錄音"; }
    const rangeButton = $("#retranscribe-range");
    if (rangeButton) {
      const durationAvailable = audioDuration(state.detail) > 0;
      rangeButton.disabled = transcriptionSettingsLocked(state.currentId) || !assetState().audio || !durationAvailable;
      rangeButton.title = !assetState().audio ? "請先上傳課堂錄音" : !durationAvailable ? "需要可用的音檔時長" : "選取時間範圍重新辨識";
    }
  }
  async function saveTranscriptionChunkSetting(value) {
    const courseId = String(state.currentId);
    const minutes = transcriptionChunkMinutes(value);
    if (isLocalId(courseId) || transcriptionSettingsLocked(courseId)) return;
    state.transcriptionSettingsSaving[courseId] = true;
    syncTranscriptionSettings(); syncAssetCard("audio", courseId);
    const savePromise = (async () => {
      try {
        const result = await request(`/courses/${encodeURIComponent(courseId)}/transcription-settings`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ transcription_chunk_minutes: minutes }) });
        if (String(state.currentId) === courseId && state.detail) {
          state.detail.transcription_chunk_minutes = transcriptionChunkMinutes(result?.transcription_chunk_minutes ?? result?.course?.transcription_chunk_minutes ?? minutes);
          updateCourseSummary(state.detail);
          const select = $(`[data-chunk-setting="${CSS.escape(courseId)}"]`);
          if (select) select.value = String(state.detail.transcription_chunk_minutes);
        }
        return true;
      } finally {
        delete state.transcriptionSettingsSaving[courseId];
        delete state.transcriptionSettingsSavePromises[courseId];
        if (String(state.currentId) === courseId) { syncTranscriptionSettings(); syncAssetCard("audio", courseId); }
      }
    })();
    state.transcriptionSettingsSavePromises[courseId] = savePromise;
    try { await savePromise; }
    catch (error) {
      if (String(state.currentId) === courseId) {
        const select = $(`[data-chunk-setting="${CSS.escape(courseId)}"]`);
        if (select) select.value = String(transcriptionChunkMinutes(state.detail?.transcription_chunk_minutes));
        const note = $("#transcription-setting-status");
        if (note) note.textContent = "設定儲存失敗；請重試後再上傳錄音。";
        toast(`轉錄設定儲存失敗：${error.message}`, "error");
      }
      throw error;
    }
  }
  function assetLabel() {
    const file = state.currentFiles?.audio;
    if (file?.name) return isLocalId() && file instanceof File ? `僅供本機播放：${file.name}` : file.name;
    if (!isLocalId() && state.detail?.audio_parts?.length) return `已儲存 ${state.detail.audio_parts.length} 段錄音`;
    const asset = state.detail?.assets?.audio || state.detail?.audio;
    return asset?.filename || (asset?.available ? "錄音已上傳" : "尚未上傳錄音");
  }
  function learningPanelHtml() {
    const learning = state.detail?.learning || {};
    const progress = Math.max(0, Math.min(1, num(learning.progress))) * 100;
    const duration = num(learning.duration_seconds, courseDuration());
    const listened = num(learning.listened_seconds);
    const rounds = num(learning.completed_rounds);
    return `<section class="learning-panel"><div class="learning-summary"><span class="learning-icon">◷</span><div class="learning-copy"><strong>課程學習進度</strong><small>第 ${esc(num(learning.round_number, 1))} 輪 · 已完成 ${rounds} 輪</small></div><span class="learning-percent">${Math.round(progress)}%</span></div><div class="learning-progress"><span style="width:${progress}%"></span></div><div class="learning-meta"><span>本輪已聽 ${clock(listened)}${duration ? ` / ${clock(duration)}` : ""}</span><div><button class="text-action" id="learning-history">查看學習紀錄</button><button class="tiny-button" id="restart-round">重新學習</button></div></div></section>`;
  }
  function renderCourse() {
    if (!state.detail) return renderOverview();
    state.activeSegment = null; state.followedSegment = null;
    const detail = state.detail;
    const count = detail.questions.length;
    const duration = courseDuration(detail);
    const uploadState = assetState();
    const detailStatus = friendlyStatus(detail.processing_status)[0];
    const courseSubject = subjectId(detail);
    setHeader(detail.title || "課程");
    $("#app-content").innerHTML = `<div class="page-heading course-page-heading"><div><div class="course-title-row"><button class="course-back" id="back-overview" aria-label="返回課程總覽">‹</button><div><div class="course-heading-title"><h1>${esc(detail.title || "未命名課程")}</h1><button class="tiny-button" id="edit-course-title" aria-label="編輯課程名稱">編輯名稱</button></div><div class="course-heading-sub"><span class="subject-tag">${esc(subjectLabel(courseSubject))}</span><span>${esc(detailStatus)}</span><span class="sub-dot">${esc(detail.segments.length)} 個逐字稿段落</span><span class="sub-dot">${esc(count)} 道題目</span></div></div></div></div><div class="heading-actions"><button class="secondary-button" id="open-course-bank">管理科目題庫</button><button class="secondary-button" id="add-segments-top"><span class="button-icon">＋</span> 新增逐字稿</button></div></div>
      ${statusBanner()}
      ${transcriptionSettingsHtml(detail)}
      <section class="asset-row audio-assets" aria-label="課程錄音"><div class="audio-workspace">${assetCard("audio", "♫", "課堂錄音", uploadState.audio)}${isLocalId() ? "" : audioPartsPanelHtml(detail)}</div></section>
      <div class="upload-strip"><p>${isLocalId() ? "試用課程中的錄音只供目前分頁播放。" : "可一次選取多個音訊檔，先調整順序再上傳；新錄音會接在既有錄音與逐字稿之後。上傳完成後會自動開始轉錄。每堂最多 32 段，每個檔案最大 750 MB。"}</p></div>
      ${handoutsPanelHtml()}
      ${learningPanelHtml()}
      <div class="review-guide"><strong>播放作答</strong>　播放遇到已確認的未作答題目時會暫停。拖曳時間軸或點逐字稿跳播時不會累計學習時間；錯題可從側邊欄的「錯題複習」集中練習。</div>
      <div class="workspace-grid" style="margin-top:17px">
        <section class="panel transcript-panel" id="transcript-panel"><header class="panel-header"><div class="panel-heading"><span class="panel-icon">≋</span><h2>課堂逐字稿 <small>${detail.segments.length} 段</small></h2></div><div class="panel-tools"><input class="search-mini" id="transcript-search" placeholder="搜尋逐字稿" value="${esc(state.searchText)}" /><button class="tiny-button" id="follow-toggle">${state.followTranscript ? "跟隨播放" : "手動瀏覽"}</button><button class="tiny-button" id="medical-review">醫學校訂</button><button class="tiny-button" id="transcription-review">辨識複核／字幕</button><button class="tiny-button" id="download-transcript" ${detail.segments.length ? "" : "disabled"} title="下載整份逐字稿（含時間戳 TXT）">下載逐字稿</button><button class="tiny-button" id="add-segment">＋</button><span class="transcript-font-controls" id="transcript-font-controls" hidden><button class="tiny-button" id="transcript-font-decrease" aria-label="縮小逐字稿字體" title="縮小字體">A−</button><button class="tiny-button" id="transcript-font-increase" aria-label="放大逐字稿字體" title="放大字體">A＋</button></span><button class="tiny-button" id="transcript-fullscreen" aria-pressed="false">⛶ 全螢幕</button></div></header>
          <p class="player-sub">逐字辨識仍可能漏字或誤認；每五分鐘顯示時間標題；請按 ▶ 重聽，保留語助詞、重複與口誤，聽不清楚處可標記［聽不清］。</p>
          ${transcriptionPreviewHtml(detail)}
          <div class="transcript-list" id="transcript-list">${renderTranscript(detail.segments)}</div>
          <div class="player-panel"><div class="player-top"><button class="play-button" id="play-toggle" aria-label="播放" ${timelineAvailable(detail) ? "" : "disabled"}>▶</button><span class="player-time"><span id="player-current">${duration > 0 ? clock(num(detail.playback?.position_seconds)) : "--:--"}</span><span class="duration">/</span><span class="duration" id="player-duration">${duration > 0 ? clock(duration) : "--:--"}</span></span><div class="player-range-wrap"><input class="player-range" id="player-range" type="range" min="0" max="${duration}" step="0.1" value="${num(detail.playback?.position_seconds)}" ${timelineAvailable(detail) ? "" : "disabled"} /></div><select id="speed-select" class="speed-select" aria-label="播放速度"><option>0.75</option><option selected>1</option><option>1.1</option><option>1.25</option><option>1.5</option><option>1.7</option><option>1.75</option></select><span style="font-size:9px;color:#9aa49f">×</span></div><p class="player-sub">${hasAudio(detail) ? "播放錄音時同步反白；拖曳時間軸會略過中間的未作答題目。" : detail.segments.length ? "目前是靜音試用時間軸；上傳錄音後將同步實際播放。" : timelineAvailable(detail) ? "目前使用靜音時間軸試用出題流程。" : "上傳錄音或新增逐字稿，開始整理課堂內容。"}</p><audio class="audio-hidden" id="course-audio" preload="metadata"></audio></div>
        </section>
        <section class="panel questions-panel"><header class="panel-header"><div class="panel-heading"><span class="panel-icon">◇</span><h2>題庫 <small>${count} 題</small></h2></div><div class="panel-tools question-tools"><div class="question-filters" id="question-filters" role="tablist" aria-label="題目分析分類">${questionFilterHtml(detail.questions)}</div><button class="tiny-button" id="add-question">＋ 新增題目</button></div></header><div class="match-analysis-strip"><span>${esc(questionCategoryHint())}</span><div class="match-actions"><button class="secondary-button" id="course-bank-upload">上傳題庫</button><button class="secondary-button" id="load-course-bank" ${isLocalId()?"disabled":""}>載入題庫</button><button class="primary-button" id="rematch-local-text" title="以課程逐字稿重新比對目前科目的題庫。" ${isLocalId() || !detail.segments.length || isAnyJobRunning() ? "disabled" : ""}>⌕ 重新配對題庫</button><span id="question-analysis-action">${questionAnalysisActionHtml()}</span>${!isLocalId() ? `<button class="secondary-button openai-settings-button" id="openai-settings" title="設定 OpenAI API 金鑰與模型。">API 設定</button>` : ""}</div></div><div class="question-list" id="question-list">${renderQuestions(detail.questions)}</div>${questionFooterHtml()}</section>
      </div>`;
    wireCoursePage(); setupAudio(); syncTranscript(); syncTranscriptReadingMode();
    simplifyCourseUI();
    $("#edit-course-title")?.addEventListener("click", showEditCourseTitle);
    $("#course-bank-upload")?.addEventListener("click", () => openBankManager(courseSubject));
    $("#load-course-bank")?.addEventListener("click", () => void loadCourseBank());
    $("#open-course-bank")?.addEventListener("click", () => openBankManager(courseSubject));
    $("#learning-history")?.addEventListener("click", showLearningHistory);
    $("#restart-round")?.addEventListener("click", restartLearningRound);
  }
  function handoutsPanelHtml() {
    const id = String(state.currentId);
    const documents = state.detail?.handouts || [];
    const selected = documents.find(doc=>doc.id===state.selectedHandouts[id]) || documents[0];
    const busy = state.handoutBusy[id];
    const disabled = isLocalId() || isAnyJobRunning() || busy;
    return `<section class="panel handouts-panel" id="handouts-panel" aria-label="上課講義"><header class="panel-header"><div class="panel-heading"><span class="panel-icon">▤</span><h2>上課講義 <small>${documents.length} 份</small></h2></div><button class="secondary-button" id="upload-handouts" ${disabled ? "disabled" : ""}>${busy ? "處理講義中…" : "＋ 上傳講義 PDF"}</button><input id="handout-files" type="file" accept="application/pdf,.pdf" multiple hidden /></header><div class="handouts-body"><p>先參考講義章節篩選相關題目，再用逐字稿確認出題時間。可上傳多份可選取文字的 PDF，每份最多 50 MB。</p><p class="field-hint">新增或移除講義後，先前未配對及待確認的 OpenAI 結果會回到「尚未分析」；已確認時間和作答紀錄會保留。上傳不會自動啟動付費分析。</p>${isLocalId() ? '<p class="field-hint">請在正式課程上傳講義。</p>' : ""}${selected ? `<div class="handout-viewer-tools"><select id="handout-select" aria-label="選擇講義">${documents.map(doc=>`<option value="${esc(doc.id)}" ${doc.id===selected.id?"selected":""}>${esc(doc.filename)}</option>`).join("")}</select><button class="text-action" data-remove-handout="${esc(selected.id)}" ${disabled?"disabled":""}>移除</button></div><div class="handout-pages" tabindex="0" aria-label="講義 PDF 頁面">${Array.from({length:selected.page_count},(_,index)=>`<figure><img loading="lazy" decoding="async" src="/api/courses/${encodeURIComponent(id)}/handouts/${encodeURIComponent(selected.id)}/pages/${index+1}" alt="${esc(selected.filename)} 第 ${index+1} 頁" /><figcaption>${index+1} / ${selected.page_count}</figcaption></figure>`).join("")}</div>` : '<p class="handouts-empty">尚未上傳講義</p>'}${busy ? `<p role="status">${esc(busy)}</p>` : ""}${state.handoutErrors[id] ? `<p class="form-error" role="alert">${esc(state.handoutErrors[id])}</p>` : ""}</div></section>`;
  }
  function syncHandoutsPanel() {
    const panel = $("#handouts-panel");
    if (!panel || !state.detail) return;
    panel.outerHTML = handoutsPanelHtml();
    wireHandoutsPanel();
  }
  function wireHandoutsPanel() {
    $("#handout-select")?.addEventListener("change", event=>{state.selectedHandouts[state.currentId]=event.target.value;syncHandoutsPanel();});
    $("#upload-handouts")?.addEventListener("click", () => $("#handout-files")?.click());
    $("#handout-files")?.addEventListener("change", event => {
      const files = [...event.target.files];
      event.target.value = "";
      if (files.length) void uploadHandouts(files);
    });
    $$("[data-remove-handout]").forEach(button => button.addEventListener("click", () => {
      void removeHandout(button.dataset.removeHandout);
    }));
  }
  async function refreshHandoutData(courseId) {
    const detail = normalizeDetail(await request(`/courses/${encodeURIComponent(courseId)}`));
    if (String(state.currentId) !== courseId) return;
    state.detail.handouts = detail.handouts;
    state.detail.questions = detail.questions;
    state.detail.matches = detail.matches;
    state.questionCategory = "unanalysed";
    state.questionPage = 1;
    syncHandoutsPanel();
    renderQuestionListOnly();
  }
  async function uploadHandouts(files) {
    const courseId = String(state.currentId);
    if (isLocalId() || isAnyJobRunning() || state.handoutBusy[courseId]) return;
    state.handoutErrors[courseId] = "";
    let uploaded = 0;
    try {
      for (const [index, file] of files.entries()) {
        if (!/\.pdf$/i.test(file.name)) throw new Error("講義目前支援 PDF；請先將其他格式匯出為 PDF。");
        if (!file.size || file.size > 50 * 1024 * 1024) throw new Error(`${file.name}：檔案不可為空，且每份最多 50 MB。`);
        state.handoutBusy[courseId] = `正在上傳與擷取第 ${index + 1} / ${files.length} 份：${file.name}`;
        if (String(state.currentId) === courseId) { syncHandoutsPanel(); syncQuestionAnalysisAction(); }
        await request(`/courses/${encodeURIComponent(courseId)}/handouts`, {
          method: "PUT", headers: { "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name) }, body: file
        });
        uploaded++;
      }
    } catch (error) {
      state.handoutErrors[courseId] = `${uploaded ? `已完成 ${uploaded} 份；` : ""}${error.message}`;
    } finally {
      delete state.handoutBusy[courseId];
      if (String(state.currentId) === courseId) {
        try { await refreshHandoutData(courseId); }
        catch { state.handoutErrors[courseId] = "講義清單更新失敗，請重新整理頁面確認。"; }
        if (String(state.currentId) === courseId) { syncHandoutsPanel(); syncQuestionAnalysisAction(); }
      }
      if (uploaded && String(state.currentId) === courseId) toast(`已加入 ${uploaded} 份講義。請按「繼續分析尚未分析題目」，套用新的講義內容。`);
    }
  }
  async function removeHandout(handoutId) {
    const courseId = String(state.currentId);
    if (isLocalId() || isAnyJobRunning() || state.handoutBusy[courseId]) return;
    state.handoutBusy[courseId] = "正在移除講義…";
    state.handoutErrors[courseId] = "";
    syncHandoutsPanel(); syncQuestionAnalysisAction();
    try {
      await request(`/courses/${encodeURIComponent(courseId)}/handouts/${encodeURIComponent(handoutId)}`, { method: "DELETE" });
      await refreshHandoutData(courseId);
    } catch (error) { state.handoutErrors[courseId] = error.message; }
    finally {
      delete state.handoutBusy[courseId];
      if (String(state.currentId) === courseId) { syncHandoutsPanel(); syncQuestionAnalysisAction(); }
    }
  }
  function assetCard(kind, icon, title, ready) {
    const local = isLocalId();
    const token = `${state.currentId}:${kind}`;
    const uploading = Boolean(state.assetUploading[token]);
    const settingsSaving = Boolean(state.transcriptionSettingsSaving[String(state.currentId)]);
    const error = state.assetUploadErrors[token];
    const action = local ? (ready ? "更換試聽錄音" : "選取錄音試聽") : "＋ 新增錄音";
    const status = uploading ? " · 上傳中" : local && ready ? " · 僅本機播放" : ready ? " · 已就緒" : "";
    const locked = audioReplacementBlocked(state.currentId);
    const description = assetLabel();
    const errorNote = error ? `<small class="asset-upload-error" role="alert">上傳失敗：${esc(error)}</small>` : "";
    const accept = "audio/*,.m4a,.mp3,.wav,.aac,.flac,.ogg";
    const lockedNote = locked ? '<small class="asset-locked-note">課程處理完成後即可追加錄音。</small>' : "";
    return `<article class="asset-card"><span class="asset-symbol">${icon}</span><span class="asset-copy"><strong>${esc(title + status)}</strong><small title="${esc(description)}">${esc(description)}</small>${errorNote}${lockedNote}</span><button class="asset-action" data-choose-asset="audio" ${uploading || locked || settingsSaving ? "disabled" : ""}>${uploading ? '<span class="spinner"></span>上傳中' : settingsSaving ? "設定儲存中" : locked ? "處理中" : action}</button><input class="asset-input" data-asset-input="audio" type="file" accept="${accept}" ${local ? "" : "multiple"} ${uploading || locked || settingsSaving ? "disabled" : ""} /></article>`;
  }
  function audioReplacementBlocked(courseId = state.currentId) {
    const jobRunning = String(state.currentId) === String(courseId) && isAnyJobRunning();
    return Boolean(jobRunning);
  }
  function audioQueue(courseId = state.currentId) {
    return state.audioQueues[String(courseId)] ||= [];
  }
  function audioPartStatusLabel(status) {
    const key = String(status || "pending").toLowerCase();
    if (["ready", "completed", "complete", "done", "succeeded", "success"].includes(key)) return "已處理";
    if (["processing", "queued", "running"].includes(key)) return "處理中";
    if (["error", "failed", "failure"].includes(key)) return "處理失敗，可重試";
    return "已上傳，等待處理";
  }
  function audioPartsPanelHtml(detail = state.detail) {
    const courseId = String(detail?.id || state.currentId);
    const queue = audioQueue(courseId);
    const parts = [...(detail?.audio_parts || [])].sort((a, b) => num(a.ordinal) - num(b.ordinal));
    const busy = state.audioPartBusy[courseId];
    const blocked = audioReplacementBlocked(courseId) || Boolean(state.transcriptionSettingsSaving[courseId]);
    const pending = parts.some(part => String(part.status || "pending").toLowerCase() === "pending");
    const saved = parts.length ? `<ol class="audio-parts-list">${parts.map((part, index) => `<li><span class="audio-part-order">${index + 1}</span><span class="audio-part-copy"><strong title="${esc(part.filename || "錄音檔")}">${esc(part.filename || "錄音檔")}</strong><small>${audioPartStatusLabel(part.status)}${num(part.duration_seconds) > 0 ? ` · ${clock(part.duration_seconds)}` : ""}</small></span></li>`).join("")}</ol>` : '<p class="audio-parts-empty">尚未儲存錄音。</p>';
    const queueMarkup = queue.length ? `<div class="audio-queue-block"><div class="audio-parts-heading"><strong>待上傳順序</strong><small>${queue.length} 個檔案</small></div><ol class="audio-parts-list audio-queue-list" id="audio-queue">${queue.map((item, index) => `<li data-queue-item="${esc(item.id)}"><span class="audio-part-order">${index + 1}</span><span class="audio-part-copy"><strong title="${esc(item.file.name)}">${esc(item.file.name)}</strong><small>${item.status === "uploading" ? "上傳中…" : item.status === "failed" ? `上傳失敗：${esc(item.error || "請重試")}` : "待上傳"}</small></span><span class="audio-queue-actions"><button type="button" data-move-audio="${esc(item.id)}" data-direction="-1" aria-label="${esc(item.file.name)} 上移" title="上移" ${index === 0 || busy || blocked ? "disabled" : ""}>↑</button><button type="button" data-move-audio="${esc(item.id)}" data-direction="1" aria-label="${esc(item.file.name)} 下移" title="下移" ${index === queue.length - 1 || busy || blocked ? "disabled" : ""}>↓</button><button type="button" data-remove-audio="${esc(item.id)}" aria-label="移除 ${esc(item.file.name)}" title="移除" ${busy || blocked ? "disabled" : ""}>×</button></span></li>`).join("")}</ol><div class="audio-queue-footer"><span>${busy?.phase === "upload" ? `正在上傳 ${busy.index} / ${busy.total}` : "確認順序後上傳；每個檔案會依序加入課程。"}</span><button class="primary-button" id="upload-audio-queue" ${busy || blocked ? "disabled" : ""}>${busy?.phase === "upload" ? '<span class="spinner"></span>正在上傳' : "依此順序上傳"}</button></div>${busy?.phase === "upload" ? `<div class="audio-upload-progress" role="progressbar" aria-valuemin="0" aria-valuemax="${busy.total}" aria-valuenow="${busy.completed}"><span style="width:${Math.round((busy.completed / Math.max(1, busy.total)) * 100)}%"></span></div>` : ""}</div>` : "";
    const canFinalize = pending && queue.length === 0 && !busy && !blocked;
    const error = state.audioPartErrors[courseId];
    return `<section class="audio-parts-panel" id="audio-parts-panel"><div class="audio-parts-heading"><strong>已儲存錄音</strong><small>${parts.length} 段，依播放順序排列</small></div>${saved}${queueMarkup}${pending ? `<div class="audio-finalize-row"><span>${busy?.phase === "finalize" ? "正在合併並處理已上傳錄音…" : "有已上傳但尚未處理的錄音。"}</span><button class="secondary-button" id="finalize-audio-parts" ${canFinalize ? "" : "disabled"}>${busy?.phase === "finalize" ? '<span class="spinner"></span>處理中' : "開始處理已上傳錄音"}</button></div>` : ""}${error ? `<p class="audio-parts-error" role="alert">${esc(error)}</p>` : ""}</section>`;
  }
  function syncAudioPartsPanel() {
    const panel = $("#audio-parts-panel");
    if (!panel || !state.detail) return;
    panel.outerHTML = audioPartsPanelHtml(state.detail);
    wireAudioPartsPanel();
  }
  function wireAudioPartsPanel() {
    $$("[data-move-audio]").forEach(button => button.addEventListener("click", () => {
      const queue = audioQueue();
      const index = queue.findIndex(item => item.id === button.dataset.moveAudio);
      const target = index + Number(button.dataset.direction);
      if (index < 0 || target < 0 || target >= queue.length) return;
      [queue[index], queue[target]] = [queue[target], queue[index]];
      syncAudioPartsPanel();
    }));
    $$("[data-remove-audio]").forEach(button => button.addEventListener("click", () => {
      const courseId = String(state.currentId);
      state.audioQueues[courseId] = audioQueue(courseId).filter(item => item.id !== button.dataset.removeAudio);
      syncAudioPartsPanel();
    }));
    $("#upload-audio-queue")?.addEventListener("click", () => { void uploadAudioQueue(); });
    $("#finalize-audio-parts")?.addEventListener("click", () => { void finalizeAudioParts(); });
  }
  function addFilesToAudioQueue(files, courseId = state.currentId) {
    const queue = audioQueue(courseId);
    for (const file of files || []) {
      if (!(file instanceof File)) continue;
      const duplicate = queue.some(item => item.file.name === file.name && item.file.size === file.size && item.file.lastModified === file.lastModified);
      if (!duplicate) queue.push({ id: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`, file, status: "queued", error: "" });
    }
    if (String(state.currentId) === String(courseId)) syncAudioPartsPanel();
  }
  async function uploadAudioQueue() {
    const courseId = String(state.currentId);
    const token = `${courseId}:audio`;
    const queue = audioQueue(courseId);
    if (!queue.length || state.assetUploading[token] || audioReplacementBlocked(courseId)) return;
    const settingsSave = state.transcriptionSettingsSavePromises[courseId];
    if (settingsSave) {
      try { await settingsSave; }
      catch { toast("轉錄設定尚未儲存，錄音未上傳。", "warning"); return; }
    }
    state.assetUploading[token] = true;
    delete state.assetUploadErrors[token];
    delete state.audioPartErrors[courseId];
    try {
      let index = 0;
      while (audioQueue(courseId).length) {
        const item = audioQueue(courseId)[0];
        index += 1;
        item.status = "uploading";
        state.audioPartBusy[courseId] = { phase: "upload", index, completed: index - 1, total: index + audioQueue(courseId).length - 1 };
        if (String(state.currentId) === courseId) syncAssetCard("audio", courseId);
        try {
          const result = await request(`/courses/${encodeURIComponent(courseId)}/audio-parts`, {
            method: "POST", headers: { "Content-Type": item.file.type || "application/octet-stream", "X-Filename": encodeURIComponent(item.file.name) }, body: item.file
          });
          if (Array.isArray(result?.audio_parts) && String(state.detail?.id) === courseId) state.detail.audio_parts = result.audio_parts;
          state.audioQueues[courseId] = audioQueue(courseId).filter(entry => entry.id !== item.id);
          if (state.audioPartBusy[courseId]) state.audioPartBusy[courseId].completed = index;
          if (String(state.currentId) === courseId) syncAudioPartsPanel();
        } catch (error) {
          item.status = "failed"; item.error = error.message || "請檢查檔案後重試。";
          state.audioPartErrors[courseId] = `「${item.file.name}」上傳失敗；已成功上傳的檔案已保留，請重試此檔案後再開始處理。`;
          if (String(state.currentId) === courseId) syncAudioPartsPanel();
          toast(`錄音上傳失敗：${error.message}`, "error");
          return;
        }
      }
    } finally {
      delete state.assetUploading[token];
      delete state.audioPartBusy[courseId];
      if (String(state.currentId) === courseId) syncAssetCard("audio", courseId);
    }
    await finalizeAudioParts(courseId, true);
  }
  async function finalizeAudioParts(courseIdValue = state.currentId, uploadedBatchComplete = false) {
    const courseId = String(courseIdValue);
    const isCurrentCourse = String(state.currentId) === courseId;
    if (isLocalId(courseId) || state.audioPartBusy[courseId] || (isCurrentCourse && audioReplacementBlocked(courseId)) || (!isCurrentCourse && !uploadedBatchComplete)) return;
    const hasPending = (state.detail?.audio_parts || []).some(part => String(part.status || "pending").toLowerCase() === "pending");
    if (!uploadedBatchComplete && (!isCurrentCourse || !hasPending)) { toast("目前沒有等待處理的錄音。", "warning"); return; }
    state.audioPartBusy[courseId] = { phase: "finalize" };
    delete state.audioPartErrors[courseId];
    if (isCurrentCourse) syncAssetCard("audio", courseId);
    try {
      const result = await request(`/courses/${encodeURIComponent(courseId)}/audio-parts/finalize`, { method: "POST" });
      if (String(state.currentId) !== courseId) return;
      state.job = result?.job || result || { kind: "audio_finalize", status: "queued", progress: 0, message: "錄音已排入處理佇列" };
      toast("錄音已依序儲存，正在合併並轉錄。");
      updateStatusBanner();
      pollJob(courseId);
    } catch (error) {
      if (String(state.currentId) === courseId) {
        state.audioPartErrors[courseId] = error.message || "無法啟動錄音處理。";
        syncAudioPartsPanel();
        toast(`無法開始處理已上傳錄音：${error.message}`, "error");
      } else state.audioPartErrors[courseId] = error.message || "無法啟動錄音處理。";
    } finally {
      delete state.audioPartBusy[courseId];
      if (String(state.currentId) === courseId) syncAssetCard("audio", courseId);
    }
  }
  function isMatchAnalysisRunning() {
    return state.job?.kind === "match_analysis" && ["queued", "pending", "running", "processing"].includes(String(state.job.status || "").toLowerCase());
  }
  function isAnyJobRunning() {
    return isCourseJobRunning(state.currentId);
  }
  function renderTranscript(segments) {
    const query = state.searchText.trim().toLowerCase();
    const filtered = segments.filter(segment => !query || String(segment.text || "").toLowerCase().includes(query));
    if (!segments.length) return `<div class="transcript-empty"><span class="empty-illustration">≋</span><strong>逐字稿會顯示在這裡</strong><p>上傳錄音開始整理，或先手動輸入逐字稿試用時間軸與同步反白。</p><button class="secondary-button" id="empty-add-segment">手動新增逐字稿</button></div>`;
    if (!filtered.length) return `<div class="transcript-empty"><strong>找不到符合的段落</strong><p>試試其他關鍵字。</p></div>`;
    let bucket = null;
    return filtered.map(segment => {
      const next = Math.floor(num(segment.start) / 300) * 300;
      const heading = next !== bucket ? `<div class="transcript-five-minute"><button class="tiny-button" data-seek-block="${next}">${clock(next, true)}</button><span>五分鐘區間</span></div>` : "";
      bucket = next;
      return `${heading}<article class="transcript-row" data-segment-row="${esc(segment.id)}"><button class="segment-time" data-seek-segment="${esc(segment.id)}" aria-label="重聽這一句" title="重聽這一句">▶</button><div>${segment.inferred ? '<span class="inferred-label">含推定校訂 · 待聽核</span>' : ''}<textarea class="segment-text" rows="2" data-edit-segment="${esc(segment.id)}" aria-label="修正逐字稿段落">${esc(segment.text || "")}</textarea></div><span class="segment-edit-mark" title="離開欄位即儲存">✎</span></article>`;
    }).join("");
  }
  function previewStatusText(detail = state.detail) {
    const preview = detail?.transcription_preview;
    if (!preview) {
      if (previewStatusFailed()) return "辨識中斷，尚無已完成段落可預覽";
      return previewIsWorking() ? "辨識中，完成的段落會顯示在這裡" : "上傳錄音後，已辨識段落會顯示在這裡";
    }
    const completed = Math.max(0, num(preview.completed_chunks));
    const total = Math.max(0, num(preview.total_chunks));
    if (previewStatusFailed(preview)) return `辨識中斷；以下為已完成 ${completed}/${total} 段的部分預覽`;
    if (total > 0 && completed >= total && previewIsWorking()) return "所有段落已辨識，正在整理課程";
    if (total > 0) return `已完成 ${completed}/${total} 段，後續仍在辨識`;
    return "辨識中，完成的段落會顯示在這裡";
  }
  function previewNote(detail = state.detail) {
    if (detail?.segments?.length) {
      return previewStatusFailed(detail.transcription_preview)
        ? "既有正式逐字稿仍在下方；以下為本次辨識的部分預覽。"
        : "辨識中預覽；完成後更新正式逐字稿";
    }
    return "點擊時間戳可跳到錄音對應位置。";
  }
  function previewBucketLabel(segment, segments = previewSegments()) {
    const index = segments.findIndex(s => String(s.id) === String(segment.id));
    const bucket = Math.floor(num(segment.start) / 300);
    return index <= 0 || Math.floor(num(segments[index - 1].start) / 300) !== bucket ? clock(bucket * 300) : "▶";
  }
  function renderPreviewRows(segments = previewSegments()) {
    return segments.map(segment => `<article class="transcription-preview-row" data-preview-segment-row="${esc(segment.id)}"><button class="preview-segment-time" data-seek-preview="${esc(segment.id)}" aria-label="跳到 ${clock(num(segment.start))}">${previewBucketLabel(segment, segments)}</button><p>${esc(segment.text || "")}</p></article>`).join("");
  }
  function transcriptionPreviewHtml(detail) {
    const preview = detail.transcription_preview;
    const segments = previewSegments(preview);
    const hidden = !shouldShowTranscriptionPreview(detail);
    const emptyText = previewIsWorking() ? "第一段辨識完成後會立即顯示在這裡。" : "開始轉錄後，已完成的段落會即時顯示在這裡。";
    return `<section class="transcription-preview" id="transcription-preview" aria-label="逐字稿辨識預覽" ${hidden ? "hidden" : ""}><header class="transcription-preview-header"><div><strong>辨識預覽</strong><small id="transcription-preview-status">${esc(previewStatusText(detail))}</small></div><span class="preview-live-mark" id="transcription-preview-mark" aria-hidden="true">即時</span></header><p class="transcription-preview-note" id="transcription-preview-note">${esc(previewNote(detail))}</p><div class="transcription-preview-list" id="transcription-preview-list">${renderPreviewRows(segments)}</div><p class="transcription-preview-empty" id="transcription-preview-empty" ${segments.length ? "hidden" : ""}>${esc(emptyText)}</p></section>`;
  }
  function createPreviewRow(segment) {
    const row = document.createElement("article");
    row.className = "transcription-preview-row";
    row.dataset.previewSegmentRow = String(segment.id);
    const time = document.createElement("button");
    time.className = "preview-segment-time";
    time.dataset.seekPreview = String(segment.id);
    time.setAttribute("aria-label", `跳到 ${clock(num(segment.start))}`);
    const text = document.createElement("p");
    row.append(time, text);
    updatePreviewRow(row, segment);
    return row;
  }
  function updatePreviewRow(row, segment) {
    const time = $("[data-seek-preview]", row);
    const text = $("p", row);
    if (time) {
      time.textContent = previewBucketLabel(segment);
      time.setAttribute("aria-label", `跳到 ${clock(num(segment.start))}`);
      time.dataset.seekPreview = String(segment.id);
    }
    if (text) text.textContent = String(segment.text || "");
    row.dataset.previewSegmentRow = String(segment.id);
  }
  function updateTranscriptionPreviewDom() {
    const panel = $("#transcription-preview");
    const detail = state.detail;
    if (!panel || !detail) return;
    panel.hidden = !shouldShowTranscriptionPreview(detail);
    const status = $("#transcription-preview-status", panel);
    const note = $("#transcription-preview-note", panel);
    const mark = $("#transcription-preview-mark", panel);
    const list = $("#transcription-preview-list", panel);
    const empty = $("#transcription-preview-empty", panel);
    if (!status || !note || !list || !empty) return;
    status.textContent = previewStatusText(detail);
    note.textContent = previewNote(detail);
    if (mark) {
      const failed = previewStatusFailed(detail.transcription_preview);
      mark.textContent = failed ? "部分" : "即時";
      mark.classList.toggle("partial", failed);
    }
    const segments = previewSegments(detail.transcription_preview);
    const scrollTop = list.scrollTop;
    const focused = document.activeElement;
    const hadPreviewFocus = panel.contains(focused);
    const focusedRowId = focused?.closest?.("[data-preview-segment-row]")?.dataset.previewSegmentRow;
    const rows = new Map($$('[data-preview-segment-row]', list).map(row => [row.dataset.previewSegmentRow, row]));
    let cursor = list.firstElementChild;
    for (const segment of segments) {
      const key = String(segment.id);
      let row = rows.get(key);
      if (row) rows.delete(key);
      else row = createPreviewRow(segment);
      updatePreviewRow(row, segment);
      if (row !== cursor) list.insertBefore(row, cursor);
      cursor = row.nextElementSibling;
    }
    while (cursor) {
      const next = cursor.nextElementSibling;
      cursor.remove();
      cursor = next;
    }
    for (const row of rows.values()) row.remove();
    empty.hidden = segments.length > 0;
    empty.textContent = previewIsWorking() ? "第一段辨識完成後會立即顯示在這裡。" : "開始轉錄後，已完成的段落會即時顯示在這裡。";
    list.scrollTop = scrollTop;
    if (hadPreviewFocus && focusedRowId) {
      const focusedButton = `[data-preview-segment-row="${CSS.escape(focusedRowId)}"] [data-seek-preview]`;
      const target = $(focusedButton, list);
      if (target && document.activeElement !== target) target.focus({ preventScroll: true });
    }
    syncPreviewTranscript();
  }
  function syncPreviewTranscript(forceScroll = false) {
    if (!state.detail) return;
    const time = activeTime();
    const active = previewSegments().find(segment => time >= num(segment.start) && time <= num(segment.end, num(segment.start) + 12));
    const activeId = active ? String(active.id) : null;
    const activeChanged = state.activePreviewSegment !== activeId;
    if (activeChanged) {
      state.activePreviewSegment = activeId;
      $$('[data-preview-segment-row]').forEach(row => row.classList.toggle("active", row.dataset.previewSegmentRow === activeId));
    }
    if (activeId && state.followTranscript && (forceScroll || activeChanged)) {
      const row = $(`[data-preview-segment-row="${CSS.escape(activeId)}"]`);
      const list = $("#transcription-preview-list");
      const previewFocused = $("#transcription-preview")?.contains(document.activeElement);
      if (row && list && !previewFocused) {
        const rowBox = row.getBoundingClientRect(), listBox = list.getBoundingClientRect();
        const rowCenter = rowBox.top - listBox.top + list.scrollTop + rowBox.height / 2;
        list.scrollTop = Math.max(0, Math.min(list.scrollHeight - list.clientHeight, rowCenter - list.clientHeight / 2));
      }
    }
  }
  function renderQuestions(questions) {
    if (!questions.length) return `<div class="question-empty"><strong>題目會在這裡出現</strong>先到科目題庫管理匯入考古題，或手動新增一道題目試用自動暫停出題。<br /><button class="tiny-button" id="empty-add-question" style="margin-top:9px">＋ 手動新增題目</button></div>`;
    const { pageItems, visible } = questionView(questions);
    if (!visible.length) {
      const emptyCopy = {
        unanalysed: ["目前沒有尚未分析的題目", "之後新增的題目會列在這個分類。"],
        matched: ["目前沒有已配對時間的題目", "分析後確認的出題時間會列在這個分類。"],
        excluded: ["目前沒有判定為非本堂課的題目", "這項判定依目前提供的逐字稿；補充逐字稿後可再檢視。"],
        review: ["目前沒有待確認的題目", "候選時間待確認或配對停用的題目會列在這個分類。"]
      }[state.questionCategory];
      return `<div class="question-empty"><strong>${emptyCopy[0]}</strong>${emptyCopy[1]}</div>`;
    }
    return pageItems.map(question => {
      const match = matchFor(question);
      const attempt = attemptFor(question);
      const category = questionBucket(question);
      const confirmed = category === "matched";
      const hasCandidateTime = match?.status === "pending_confirmation" && hasQuestionTime(match);
      const time = confirmed || hasCandidateTime ? seconds(match?.question_time) : NaN;
      const skipped = state.skipIds.has(String(question.id)) || match?.status === "skipped";
      const localTextMatch = confirmed && match?.provider === "local_text";
      const status = category === "matched" ? (attempt ? "已配對時間・已作答" : skipped ? "已配對時間・已跳過" : localTextMatch ? "已配對時間・本機對位" : "已配對時間")
        : category === "excluded" ? "依目前逐字稿判定：非本堂課"
          : category === "review" ? (match?.status === "disabled" ? "配對已停用・待確認" : "候選時間待確認") : "尚未分析";
      const needsReview = Boolean(question.needs_review);
      const stateClass = category === "review" ? "review" : attempt ? "answered" : skipped ? "skipped" : confirmed ? "ready" : category === "excluded" ? "excluded" : "";
      const options = optionList(question);
      const section = question.section || (question.source_page ? `第 ${question.source_page} 頁` : "未分類");
      return `<article class="question-row" data-question-row="${esc(question.id)}"><div class="question-row-top"><span class="question-number">Q ${esc(question.number ?? "—")}</span>${question.bank_title ? `<span class="question-bank" title="題庫">${esc(question.bank_title)}</span>` : ""}<span class="question-section" title="題目章節">${esc(section)}</span><span class="question-state ${stateClass}">${status}</span>${needsReview ? `<span class="question-review-badge">題目／答案待校對</span>` : ""}<label class="question-time" title="出題時間（儲存時會確認此出題點）">時間 <input data-match-time="${esc(question.id)}" type="text" placeholder="--:--" value="${Number.isFinite(time) ? clock(time) : ""}" aria-label="${esc(section)} 第 ${esc(question.number ?? "")} 題出題時間" /></label></div><p class="question-stem">${esc(question.stem || "（題目內容尚未辨識）")}</p><div class="question-options-mini">${options.slice(0, 5).map(option => `<span>${esc(option.key)}. ${esc(option.text)}</span>`).join("")}</div><div class="question-row-actions"><button class="text-action" data-edit-question="${esc(question.id)}">修正題目</button>${attempt ? `<button class="text-action" data-practice-question="${esc(question.id)}">查看作答</button>` : confirmed ? `<button class="text-action" data-practice-question="${esc(question.id)}">立即練習</button>` : `<button class="text-action" data-confirm-question="${esc(question.id)}">${match ? "確認出題點" : "加入出題時間"}</button>`}</div></article>`;
    }).join("");
  }
  function wireCoursePage() {
    wireHandoutsPanel();
    $("#retry-match-analysis")?.addEventListener("click", analyzeMatches);
    $("#retry-process")?.addEventListener("click", () => startProcessing($("#retry-process")));
    $("#retry-range-transcription")?.addEventListener("click", showRangeRetranscribeModal);
    $("#retry-medical-review")?.addEventListener("click", () => showMedicalReview());
    $("#retry-finalize-audio")?.addEventListener("click", () => { void finalizeAudioParts(); });
    $("#back-overview")?.addEventListener("click", () => { void goOverview(); });
    $("#download-transcript")?.addEventListener("click", downloadTranscript);
    $("#medical-review")?.addEventListener("click", () => showMedicalReview());
    $("#transcription-review")?.addEventListener("click", showTranscriptionReview);
    $("#transcript-fullscreen")?.addEventListener("click", () => {
      if (state.transcriptReadingMode) void exitTranscriptReadingMode();
      else void enterTranscriptReadingMode();
    });
    $("#transcript-font-decrease")?.addEventListener("click", () => adjustTranscriptFont(-0.15));
    $("#transcript-font-increase")?.addEventListener("click", () => adjustTranscriptFont(0.15));
    $("#add-segment")?.addEventListener("click", showAddSegments);
    $("#add-segments-top")?.addEventListener("click", showAddSegments);
    $("#empty-add-segment")?.addEventListener("click", showAddSegments);
    $("#add-question")?.addEventListener("click", showAddQuestion);
    syncQuestionAnalysisAction();
    $("#openai-settings")?.addEventListener("click", () => { void showOpenAISettings(); });
    $("#rematch-local-text")?.addEventListener("click", rematchLocalText);
    wireQuestionFilters();
    $("#question-prev")?.addEventListener("click", () => { state.questionPage -= 1; renderQuestionListOnly(); });
    $("#question-next")?.addEventListener("click", () => { state.questionPage += 1; renderQuestionListOnly(); });
    $("#empty-add-question")?.addEventListener("click", showAddQuestion);
    $("#question-help")?.addEventListener("click", () => showHelp("match"));
    $("#follow-toggle")?.addEventListener("click", () => { state.followTranscript = !state.followTranscript; $("#follow-toggle").textContent = state.followTranscript ? "跟隨播放" : "手動瀏覽"; if (state.followTranscript) syncTranscript(true); });
    $("#transcript-search")?.addEventListener("input", event => {
      state.searchText = event.target.value;
      const list = $("#transcript-list");
      list.innerHTML = renderTranscript(state.detail.segments);
      wireTranscriptRows();
      state.activeSegment = null; syncTranscript(true);
    });
    $("#transcription-preview")?.addEventListener("click", event => {
      const button = event.target.closest("[data-seek-preview]");
      if (!button) return;
      const segment = previewSegments().find(item => String(item.id) === button.dataset.seekPreview);
      if (segment) { jumpTo(num(segment.start), { seek: true }); state.audio?.play().catch(() => {}); }
    });
    wireTranscriptRows();
    wireQuestions();
    wireAssetInputs();
    wireAudioPartsPanel();
    $("#transcription-chunk-minutes")?.addEventListener("change", event => {
      void saveTranscriptionChunkSetting(event.target.value).catch(() => {});
    });
    $("#retranscribe-audio")?.addEventListener("click", () => {
      const confirmed = window.confirm("重新處理會更新系統自動產生的逐字稿與出題時間配對，歷史作答紀錄會保留。若逐字稿是手動新增或修正，伺服器可能會略過轉錄。要重新轉錄這段錄音嗎？");
      if (confirmed) void startProcessing($("#retranscribe-audio"));
    });
    $("#retranscribe-range")?.addEventListener("click", showRangeRetranscribeModal);
    $("#play-toggle")?.addEventListener("click", togglePlayback);
    $("#player-range")?.addEventListener("input", event => {
      const target = num(event.target.value);
      jumpTo(target, { seek: true, fromSlider: true });
      $("#player-current").textContent = clock(target);
    });
    $("#speed-select")?.addEventListener("change", event => { if (state.audio) state.audio.playbackRate = num(event.target.value, 1); });
  }
  function resizeTranscriptEditors() {
    const scale = state.transcriptReadingMode ? state.transcriptFontScale : 1;
    $$('[data-edit-segment]').forEach(textarea => {
      const textLength = String(textarea.value || "").length;
      textarea.rows = Math.min(12, Math.max(state.transcriptReadingMode ? 2 : 1, Math.ceil(textLength / (48 / scale))));
    });
  }
  function syncTranscriptReadingMode() {
    const panel = $("#transcript-panel");
    document.documentElement.classList.toggle("transcript-reading-mode", state.transcriptReadingMode);
    if (!panel) return;
    panel.style.setProperty("--transcript-font-scale", state.transcriptReadingMode ? String(state.transcriptFontScale) : "1");
    const fontControls = $("#transcript-font-controls", panel);
    if (fontControls) fontControls.hidden = !state.transcriptReadingMode;
    const button = $("#transcript-fullscreen", panel);
    if (button) {
      button.textContent = state.transcriptReadingMode ? "退出全螢幕" : "⛶ 全螢幕";
      button.setAttribute("aria-pressed", String(state.transcriptReadingMode));
      button.setAttribute("aria-label", state.transcriptReadingMode ? "退出全螢幕逐字稿" : "全螢幕顯示逐字稿");
    }
    resizeTranscriptEditors();
  }
  function adjustTranscriptFont(change) {
    state.transcriptFontScale = Math.max(1.5, Math.min(3.2, Math.round((state.transcriptFontScale + change) * 100) / 100));
    syncTranscriptReadingMode();
    if (state.transcriptReadingMode) syncTranscript(true);
  }
  async function enterTranscriptReadingMode() {
    if (!$("#transcript-panel")) return;
    state.transcriptReadingMode = true;
    state.transcriptNativeFullscreen = false;
    syncTranscriptReadingMode();
    syncTranscript(true);
    const root = document.documentElement;
    if (root.requestFullscreen && !document.fullscreenElement) {
      try {
        await root.requestFullscreen();
        if (document.fullscreenElement === root && state.transcriptReadingMode) state.transcriptNativeFullscreen = true;
        else if (document.fullscreenElement === root && document.exitFullscreen) await document.exitFullscreen();
      } catch { /* The fixed reading panel remains available if browser fullscreen is blocked. */ }
    }
  }
  async function exitTranscriptReadingMode() {
    const wasNativeFullscreen = state.transcriptNativeFullscreen || document.fullscreenElement === document.documentElement;
    state.transcriptReadingMode = false;
    state.transcriptNativeFullscreen = false;
    syncTranscriptReadingMode();
    if (wasNativeFullscreen && document.fullscreenElement && document.exitFullscreen) {
      try { await document.exitFullscreen(); } catch { /* The reading panel is already closed in CSS. */ }
    }
  }
  document.addEventListener("fullscreenchange", () => {
    if (document.fullscreenElement === document.documentElement) {
      state.transcriptNativeFullscreen = state.transcriptReadingMode;
      if (state.transcriptReadingMode) syncTranscript(true);
      return;
    }
    if (state.transcriptNativeFullscreen && state.transcriptReadingMode) {
      state.transcriptNativeFullscreen = false;
      state.transcriptReadingMode = false;
      syncTranscriptReadingMode();
    }
  });
  function wireQuestionFilters() {
    $$('[data-question-filter]').forEach(button => button.addEventListener("click", () => {
      state.questionCategory = button.dataset.questionFilter;
      state.questionPage = 1;
      renderQuestionListOnly();
    }));
  }
  async function showTranscriptionReview() {
    if (isLocalId()) { toast("試用課程沒有辨識複核紀錄。", "warning"); return; }
    const courseId = String(state.currentId);
    const endpoint = `/courses/${encodeURIComponent(courseId)}/transcription-review`;
    try {
      const report = await request(endpoint);
      if (String(state.currentId) !== courseId) return;
      if (!report.available) { toast("這堂課尚無新版辨識紀錄；下次轉錄後即可查看。", "warning"); return; }
      const offset = report.offset_ms || 0;
      const range = row => `${clock((row.start_ms + offset) / 1000, true)}–${clock((row.end_ms + offset) / 1000, true)}`;
      const root = showModal("辨識複核與原始字幕", "顯示最近一次辨識範圍；第二輪文字僅供比較，不會自動取代初稿。", `<div class="modal-body"><p>範圍 ${esc(range(report))}。本次第二輪複核 ${report.reviews.length} 個短段；其餘疑點仍需人工重聽。</p><p>分數偏低、文字相同或沒有文字都不代表一定有錯。兩輪一致也不保證正確；數字、否定詞及專有名詞請對照錄音。</p><div class="modal-foot">${["txt", "vtt", "srt", "json"].map(kind => `<button class="tiny-button" data-raw-export="${kind}">原始 ${kind.toUpperCase()}</button>`).join("")}</div><h3>第二輪比對</h3>${report.reviews.map((row, i) => `<div class="transcription-review-card"><strong>${esc(range(row))} · ${row.status === "failed" ? "複核失敗，初稿已保留" : row.different ? "兩輪有差異，請重聽" : "兩輪文字相同"}</strong><p>第一輪：${esc(row.first_text || "（未辨識出文字）")}</p><p>第二輪：${esc(row.second_text || "（未辨識出文字）")}</p><button class="tiny-button" data-review-play="${i}">重聽</button><button class="tiny-button" data-review-retry="${i}">選取此段重新辨識</button></div>`).join("") || "<p>未執行第二輪複核。</p>"}<h3>待核對區間（${report.flags.length}）</h3>${report.flags.map((row, i) => `<p><button class="tiny-button" data-flag-play="${i}">${esc(range(row))}</button> ${esc(row.detail)}</p>`).join("") || "<p>沒有偵測到規則所涵蓋的疑點，仍需對照錄音。</p>"}</div>`, { wide: true });
      const listenAt = time => { jumpTo(time, { seek: true }); if (state.audio) state.audio.play().catch(() => toast('請使用播放器開始播放。', 'warning')); };
      $$('[data-review-play]', root).forEach(button => button.addEventListener('click', () => listenAt((report.reviews[Number(button.dataset.reviewPlay)].start_ms + offset) / 1000)));
      $$('[data-flag-play]', root).forEach(button => button.addEventListener('click', () => listenAt((report.flags[Number(button.dataset.flagPlay)].start_ms + offset) / 1000)));
      $$('[data-review-retry]', root).forEach(button => button.addEventListener('click', () => {
        const row = report.reviews[Number(button.dataset.reviewRetry)];
        showRangeRetranscribeModal({ start: (row.start_ms + offset) / 1000, end: (row.end_ms + offset) / 1000 });
      }));
      $$('[data-raw-export]', root).forEach(button => button.addEventListener('click', async () => {
        try {
          const file = await request(`${endpoint}?format=${button.dataset.rawExport}`);
          const url = URL.createObjectURL(new Blob([file.text], { type: 'text/plain;charset=utf-8' }));
          const link = document.createElement('a'); link.href = url; link.download = file.filename;
          document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
        } catch (error) { toast(error.message, 'warning'); }
      }));
    } catch (error) { toast(error.message, 'warning'); }
  }
  async function showMedicalReview(selectedBlock = null, savedContext = "", historyFilter = "pending") {
    if (isLocalId()) { toast("請先建立正式課程並上傳錄音。", "warning"); return; }
    const courseId = String(state.currentId);
    const endpoint = `/courses/${encodeURIComponent(courseId)}/medical-review`;
    try {
      const report = await request(endpoint);
      if (String(state.currentId) !== courseId) return;
      const blocks = [...new Set(state.detail.segments.map(s => Math.floor(num(s.start) / 300) * 300))].sort((a,b) => a-b);
      const block = selectedBlock ?? Math.floor(num(state.detail.playback?.position_seconds) / 300) * 300;
      const visibleRevisions = historyFilter === "all" ? report.revisions : report.revisions.filter(row => row.status === "pending" && Math.floor(row.before.start_ms / 300000) * 300 === block);
      const labels = {pending:"待決定 · 推定", applied:"已採用", rejected:"已略過", failed:"失敗，原時間保留"};
      const root = showModal("醫學校訂與音訊對齊", "完全本地執行。語境建議不等於錄音原話；採用後仍標示推定。", `<div class="modal-body medical-review-body"><div class="medical-controls"><label>處理區間（每次五分鐘）<select id="medical-block">${blocks.map(b => `<option value="${b}" ${b === block ? "selected" : ""}>${clock(b,true)}–${clock(b+300,true)}</option>`).join("")}</select></label><label>醫學背景與術語（選填）<textarea id="medical-context" maxlength="4000" rows="3" placeholder="例如：膽素能藥物；acetylcholine、antagonist。只作候選詞參考，不會把講義補成原話。">${esc(savedContext)}</textarea></label><p>${report.suggest_ready ? `${esc(report.model)} 已就緒` : "本地校訂模型尚未安裝完成"} · ${report.align_ready ? "音訊對齊工具已就緒" : "音訊對齊工具尚未安裝完成"}</p><div class="modal-foot"><button class="primary-button" id="medical-suggest" ${!report.suggest_ready || !blocks.length || isAnyJobRunning() ? "disabled" : ""}>產生校正建議</button><button class="secondary-button" id="medical-align" ${!report.align_ready || !blocks.length || isAnyJobRunning() ? "disabled" : ""}>對齊此區間的校訂文字</button><button class="tiny-button" id="medical-export">下載全部校訂紀錄</button><button class="tiny-button" id="medical-refresh">重新整理</button></div><p>先產生建議，再重聽並逐項採用；已校訂文字可重新對齊，推定標記會保留。</p><details><summary>本地流程與限制</summary><p>有錄音時先以 Large v3 重跑短段辨識。每批參考前後約 90 秒上下文，並擷取已上傳講義詞彙及建立本堂課的推定詞彙表，僅作拼字參考；同一本地模型產生候選後再審核一次。保留中英語言與口語，攔截整句改寫及矛盾替換。先重聽並逐項採用；採用或手動修正後可重新對齊。對齊只更新時間，不改文字；推定標記會保留。超過 60 秒的單一段落需先拆短。</p></details></div><label>檢視<select id="medical-history-filter"><option value="pending" ${historyFilter === "pending" ? "selected" : ""}>此區間的待採用建議</option><option value="all" ${historyFilter === "all" ? "selected" : ""}>全部校訂紀錄</option></select></label><h3>校訂紀錄（${visibleRevisions.length}）</h3>${visibleRevisions.map(row => `<article class="transcription-review-card"><strong>${row.kind === "suggest" ? "醫學術語建議 · 推定" : row.kind === "align" ? "音訊重新對齊" : "手動校訂"} · ${esc(labels[row.status] || row.status)}</strong><p>原文：${esc(row.before.text)}</p><p>${row.kind === "align" ? "對齊文字" : "候選／校訂文字"}：${esc(row.after.text)}</p><p>${esc(row.reason)}</p>${row.after.term_edits?.length || row.after.term_edit ? `<p>詞彙變更：${(row.after.term_edits || [row.after.term_edit]).map(edit => `<del>${esc(edit.original)}</del> → <strong>${esc(edit.replacement)}</strong>`).join("；")}</p>` : ""}${row.after.spelling_references?.length ? `<p>拼字參考：${esc(row.after.spelling_references.map(r => `${r.term}（${r.source}）`).join("；"))}</p>` : ""}${row.after.vocabulary_note ? `<p>${esc(row.after.vocabulary_note)}</p>` : ""}${row.after.audio_evidence?.segments ? `<p>${row.after.second_asr_agrees ? "短段辨識也出現此候選詞，仍需人工聽核。" : "短段辨識未支持所有候選拼字，請優先重聽。"}</p><details><summary>查看短段重新辨識參考（仍需聽核）</summary><p>${esc(row.after.audio_evidence.segments.map(s => s.text).join(" "))}</p></details>` : row.after.audio_evidence?.error ? `<p>${esc(row.after.audio_evidence.error)}</p>` : ""}<small>${esc(row.created_at)}${row.kind === "align" ? ` · ${clock(row.before.start_ms/1000)} → ${clock(row.after.start_ms/1000)}（估計時間）` : ""}</small><div class="modal-foot"><button class="tiny-button" data-medical-play="${row.before.start_ms/1000}">重聽</button>${row.kind === "suggest" && row.status === "pending" ? `<button class="tiny-button" data-medical-apply="${esc(row.id)}" ${isAnyJobRunning() ? "disabled" : ""}>採用並標示推定</button><button class="tiny-button" data-medical-reject="${esc(row.id)}" ${isAnyJobRunning() ? "disabled" : ""}>略過</button>` : ""}</div></article>`).join("") || "<p>目前沒有符合條件的紀錄。可產生新建議，或切換到全部校訂紀錄。</p>"}</div>`, {wide:true});
      simplifyMedicalUI(root);
      const selection = () => Number($("#medical-block", root).value);
      const context = () => $("#medical-context", root).value;
      const filter = () => $("#medical-history-filter", root).value;
      const refreshReview = () => showMedicalReview(selection(), context(), filter());
      $("#medical-refresh",root).onclick = refreshReview;
      $("#medical-history-filter",root).onchange = refreshReview;
      $("#medical-block",root).onchange = refreshReview;
      for (const mode of ["suggest", "align"]) $("#medical-" + mode, root).onclick = async () => {
        const button = $("#medical-" + mode, root); button.disabled = true;
        try {
          const response = await request(endpoint, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({mode, start_seconds:selection(), context:context()})});
          if (String(state.currentId) !== courseId) return;
          state.job = response.job; $("#modal-root").innerHTML = ""; updateStatusBanner(); pollJob(courseId);
          toast(mode === "suggest" ? "已開始本地校訂，完成後請開啟醫學校訂查看建議。" : "已開始校訂文字對齊，結果會保存到校訂紀錄。");
        } catch (error) { button.disabled = false; toast(error.message, "error"); }
      };
      $$('[data-medical-play]', root).forEach(button => button.onclick = () => { jumpTo(Number(button.dataset.medicalPlay), {seek:true}); state.audio?.play().catch(() => {}); });
      for (const action of ["apply", "reject"]) $$(`[data-medical-${action}]`, root).forEach(button => button.onclick = async () => {
        const b = selection(), c = context(), f = filter(); button.disabled = true;
        try {
          await request(`${endpoint}/${encodeURIComponent(button.getAttribute(`data-medical-${action}`))}`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action})});
          if (String(state.currentId) !== courseId) return;
          await refreshDetail(); await showMedicalReview(b,c,f);
        } catch (error) { button.disabled = false; toast(error.message, "error"); }
      });
      $("#medical-export", root).onclick = () => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(report.revisions,null,2)], {type:"application/json;charset=utf-8"}));
        const link = document.createElement("a"); link.href=url; link.download="逐字稿校訂紀錄.json"; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      };
    } catch (error) { toast(error.message,"error"); }
  }
  function downloadTranscript() {
    const detail = state.detail;
    if (!detail?.segments?.length) { toast("目前還沒有可下載的逐字稿。", "warning"); return; }
    const title = detail.title || "未命名課程";
    let lastBlock = null;
    const lines = [...detail.segments].sort((a, b) => num(a.start) - num(b.start)).map(segment => {
      const block = Math.floor(num(segment.start) / 300) * 300;
      const heading = block !== lastBlock ? `[${clock(block, true)}]\r\n` : "";
      lastBlock = block;
      return `${heading}${segment.inferred ? "［推定校訂，待聽核］" : ""}${segment.text || ""}`;
    });
    const blob = new Blob(["\uFEFF", `${title}\r\n\r\n${lines.join("\r\n\r\n")}\r\n`], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").slice(0, 100)}_逐字稿.txt`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  function wireTranscriptRows() {
    $$('[data-seek-block]').forEach(button => button.addEventListener("click", () => jumpTo(Number(button.dataset.seekBlock), { seek: true })));
    $$('[data-seek-segment]').forEach(button => button.addEventListener("click", () => {
      const segment = state.detail.segments.find(item => String(item.id) === button.dataset.seekSegment);
      if (segment) jumpTo(num(segment.start), { seek: true });
    }));
    $$('[data-edit-segment]').forEach(textarea => {
      let previous = textarea.value;
      const save = async () => {
        const text = textarea.value.trim();
        if (!text || text === previous) return;
        const segment = state.detail.segments.find(item => String(item.id) === textarea.dataset.editSegment);
        if (!segment) return;
        try {
          if (isLocalId()) segment.text = text;
          else await request(`/segments/${encodeURIComponent(segment.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
          segment.text = text; previous = text; persistLocalDetail(); toast("逐字稿段落已儲存");
        } catch (error) { toast(`段落儲存失敗：${error.message}`, "error"); }
      };
      textarea.addEventListener("blur", () => { void save(); requestAnimationFrame(() => syncTranscript()); });
      textarea.addEventListener("keydown", event => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") textarea.blur(); });
    });
    resizeTranscriptEditors();
  }
  async function saveQuestionMatch(input, time) {
    if (!Number.isFinite(time) || time < 0) { toast("請輸入有效時間，例如 03:25", "warning"); return; }
    if (input.dataset.saving === "true") return;
    const question = state.detail.questions.find(item => String(item.id) === input.dataset.matchTime);
    if (!question) return;
    input.dataset.saving = "true";
    const existing = matchFor(question);
    try {
      if (isLocalId()) {
        const updated = existing || { id: `local-match-${Date.now()}`, question_id: question.id };
        Object.assign(updated, { question_time: time, start: time, end: time, status: "confirmed" });
        if (!existing) state.detail.matches.push(updated);
      } else {
        const response = await request(`/questions/${encodeURIComponent(question.id)}/match`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question_time: time, status: "confirmed" }) });
        const updated = response?.match || response;
        if (existing) Object.assign(existing, updated || {}, { question_time: time, status: "confirmed" });
        else state.detail.matches.push({ ...(updated || {}), question_id: question.id, question_time: time, status: "confirmed" });
      }
      state.skipIds.delete(String(question.id)); persistSkipped(); persistLocalDetail(); renderQuestionListOnly(); toast(`第 ${question.number ?? ""} 題出題時間已確認`);
    } catch (error) { toast(`出題時間儲存失敗：${error.message}`, "error"); }
    finally { input.dataset.saving = "false"; }
  }
  function wireQuestions() {
    $$('[data-match-time]').forEach(input => {
      const initialValue = input.value.trim();
      input.addEventListener("keydown", event => { if (event.key === "Enter") input.blur(); });
      input.addEventListener("blur", async () => {
        const value = input.value.trim();
        if (value === initialValue || !value) return;
        const time = seconds(value);
        if (!Number.isFinite(time) || time < 0) { toast("請輸入有效時間，例如 03:25", "warning"); return; }
        await saveQuestionMatch(input, time);
      });
    });
    $$('[data-confirm-question]').forEach(button => button.addEventListener("click", () => {
      const input = $(`[data-match-time="${CSS.escape(button.dataset.confirmQuestion)}"]`);
      if (input) { if (!input.value.trim()) input.value = clock(activeTime()); saveQuestionMatch(input, seconds(input.value.trim())); }
    }));
    $$('[data-edit-question]').forEach(button => button.addEventListener("click", () => showEditQuestion(button.dataset.editQuestion)));
    $$('[data-practice-question]').forEach(button => button.addEventListener("click", () => {
      const question = state.detail.questions.find(item => String(item.id) === button.dataset.practiceQuestion);
      if (!question) return;
      state.practiceShouldResume = hasAudio() ? Boolean(state.audio && !state.audio.paused) : state.virtualPlaying;
      if (state.audio && !state.audio.paused) state.audio.pause();
      if (state.virtualPlaying) { state.virtualPlaying = false; clearInterval(state.virtualTimer); $("#play-toggle").textContent = "▶"; }
      state.quizMode = "practice"; state.quizQueue = [question]; state.quizIndex = 0; state.quizAnswer = null; state.quizSelected = null; openQuizQuestion(question);
    }));
  }
  function wireAssetInputs() {
    $$('[data-choose-asset="audio"]').forEach(button => button.addEventListener("click", () => {
      if (!button.disabled) $('[data-asset-input="audio"]')?.click();
    }));
    $$('[data-asset-input="audio"]').forEach(input => input.addEventListener("change", () => {
      const files = [...(input.files || [])];
      if (!files.length) return;
      if (isLocalId()) void uploadAsset("audio", files[0], input);
      else addFilesToAudioQueue(files);
      input.value = "";
    }));
  }
  function syncAssetCard(kind = "audio", courseId = state.currentId) {
    if (String(courseId) !== String(state.currentId)) return;
    const input = $('[data-asset-input="audio"]');
    const card = input?.closest(".asset-card");
    if (!card) return;
    const token = `${courseId}:${kind}`;
    const uploading = Boolean(state.assetUploading[token]);
    const settingsSaving = Boolean(state.transcriptionSettingsSaving[String(courseId)]);
    const locked = audioReplacementBlocked(courseId);
    const local = isLocalId(courseId);
    const ready = assetState().audio;
    const title = card.querySelector(".asset-copy strong");
    const small = card.querySelector(".asset-copy small:not(.asset-upload-error)");
    const button = card.querySelector("[data-choose-asset]");
    title.textContent = `課堂錄音${uploading ? " · 上傳中" : local && ready ? " · 僅本機播放" : ready ? " · 已就緒" : ""}`;
    small.textContent = assetLabel(); small.title = assetLabel();
    let error = card.querySelector(".asset-upload-error");
    if (state.assetUploadErrors[token]) {
      if (!error) { error = document.createElement("small"); error.className = "asset-upload-error"; error.setAttribute("role", "alert"); card.querySelector(".asset-copy").append(error); }
      error.textContent = `上傳失敗：${state.assetUploadErrors[token]}`;
    } else error?.remove();
    let lockedNote = card.querySelector(".asset-locked-note");
    if (locked) {
      if (!lockedNote) { lockedNote = document.createElement("small"); lockedNote.className = "asset-locked-note"; card.querySelector(".asset-copy").append(lockedNote); }
      lockedNote.textContent = "課程處理完成後即可追加錄音。";
    } else lockedNote?.remove();
    button.disabled = uploading || locked || settingsSaving;
    button.innerHTML = uploading ? '<span class="spinner"></span>上傳中' : settingsSaving ? "設定儲存中" : locked ? "處理中" : (local ? (ready ? "更換試聽錄音" : "選取錄音試聽") : "＋ 新增錄音");
    input.disabled = uploading || locked || settingsSaving;
    syncAudioPartsPanel();
  }
  async function uploadAsset(kind, file, input) {
    const courseId = String(state.currentId);
    const token = `${courseId}:${kind}`;
    if (state.assetUploading[token]) return;
    const settingsSave = state.transcriptionSettingsSavePromises[courseId];
    if (settingsSave) {
      try { await settingsSave; }
      catch { if (input) input.value = ""; toast("轉錄設定尚未儲存，錄音未上傳。", "warning"); return; }
    }
    if (audioReplacementBlocked(courseId)) { toast("課程開始轉錄後無法更換錄音；請建立新課程。", "warning"); return; }
    state.assetUploading[token] = true;
    delete state.assetUploadErrors[token];
    syncAssetCard(kind, courseId);
    try {
      if (isLocalId(courseId)) {
        state.localAssets[courseId] ||= {};
        state.localAssets[courseId].audio = file;
        const previousUrl = state.objectUrls[courseId];
        if (previousUrl) URL.revokeObjectURL(previousUrl);
        const objectUrl = URL.createObjectURL(file);
        state.objectUrls[courseId] = objectUrl;
        if (String(state.currentId) === courseId && state.detail) {
          state.currentFiles.audio = file;
          state.detail.audio = { ...(state.detail.audio || {}), filename: file.name, object_url: objectUrl, duration_seconds: 0 };
          state.virtualTime = 0; renderCourse();
        }
        toast("錄音已載入試用播放器；正式課程會自動轉錄並配對題庫。", "warning");
        return;
      }
      const result = await request(`/courses/${encodeURIComponent(courseId)}/assets/audio`, {
        method: "PUT", headers: { "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(file.name) }, body: file
      });
      const metadata = result?.asset || { filename: file.name };
      const courseUploads = { ...(state.uploadedAssets[courseId] || {}), audio: { name: metadata.filename || file.name, uploaded: true } };
      state.uploadedAssets[courseId] = courseUploads;
      try { localStorage.setItem(LOCAL_KEY + ":uploads", JSON.stringify(state.uploadedAssets)); } catch { /* server metadata remains authoritative */ }
      if (String(state.currentId) === courseId && state.detail) {
        state.currentFiles.audio = courseUploads.audio;
        state.detail.assets = { ...(state.detail.assets || {}), audio: metadata };
        state.detail.audio = { ...(state.detail.audio || {}), ...metadata, filename: metadata.filename || file.name };
        state.job = result?.job || (result?.processing_error ? { status: "failed", error: String(result.processing_error) } : null);
        renderCourse();
        if (state.job) pollJob(courseId);
      }
      toast((metadata.filename || file.name) + (result?.job ? " 已上傳，正在轉錄" : " 已上傳"));
    } catch (error) {
      state.assetUploadErrors[token] = error.message || "請檢查檔案後再試一次。";
      syncAssetCard(kind, courseId);
      if (String(state.currentId) === courseId) toast("錄音上傳失敗：" + state.assetUploadErrors[token], "error");
    } finally {
      delete state.assetUploading[token];
      if (input) input.value = "";
      syncAssetCard(kind, courseId);
    }
  }
  function setupAudio() {
    state.audio = $("#course-audio");
    state.virtualTime = num(state.detail.playback?.position_seconds, state.virtualTime);
    if (hasAudio() && state.audio) {
      const src = courseAudioSource();
      if (src && state.audio.src !== new URL(src, location.href).href) state.audio.src = src;
      state.audio.addEventListener("loadedmetadata", () => {
        const duration = Number.isFinite(state.audio.duration) ? state.audio.duration : courseDuration();
        $("#player-range").max = duration;
        $("#player-duration").textContent = clock(duration);
        const position = Math.min(num(state.detail.playback?.position_seconds), duration || Infinity);
        if (position > 0) state.audio.currentTime = position;
        state.lastTime = state.audio.currentTime;
        updatePlayerDisplay(); syncTranscript(true); syncTranscriptionSettings();
      }, { once: true });
      state.audio.addEventListener("timeupdate", onPlaybackTime);
      state.audio.addEventListener("play", () => { $("#play-toggle").textContent = "Ⅱ"; state.lastTime = state.audio.currentTime; startListeningTracking(); });
      state.audio.addEventListener("pause", () => { $("#play-toggle").textContent = "▶"; trackListening(state.audio.currentTime, true); endListeningTracking(); queuePlaybackSave(); });
      state.audio.addEventListener("seeking", () => { state.seeking = true; endListeningTracking(); });
      state.audio.addEventListener("seeked", () => { state.seeking = false; state.lastTime = state.audio.currentTime; if (!state.audio.paused) startListeningTracking(); updatePlayerDisplay(); syncTranscript(true); queuePlaybackSave(); });
      state.audio.addEventListener("ratechange", () => { const speed = $("#speed-select"); if (speed) speed.value = String(state.audio.playbackRate); });
      state.audio.addEventListener("ended", () => { state.lastTime = state.audio.currentTime; trackListening(state.audio.currentTime, true); endListeningTracking(); savePlayback(true); });
      state.audio.addEventListener("error", () => { if (state.audio?.src) toast("錄音載入失敗，請確認檔案已上傳並重新整理課程。", "error"); });
    }
    updatePlayerDisplay();
  }
  async function togglePlayback() {
    if (state.quizQuestion && state.quizMode === "playback") return;
    if (hasAudio() && state.audio) {
      if (state.audio.paused) {
        try { await state.audio.play(); } catch (error) { toast(`無法播放錄音：${error.message}`, "error"); }
      } else state.audio.pause();
      return;
    }
    if (!state.detail?.segments?.length) { toast("先上傳錄音或新增逐字稿，再開始播放。", "warning"); return; }
    state.virtualPlaying = !state.virtualPlaying;
    $("#play-toggle").textContent = state.virtualPlaying ? "Ⅱ" : "▶";
    clearInterval(state.virtualTimer);
    if (state.virtualPlaying) {
      state.lastTime = state.virtualTime;
      state.virtualTimer = setInterval(() => {
        if (!state.virtualPlaying || state.quizQuestion) return;
        const speed = num($("#speed-select")?.value, 1);
        const next = Math.min(courseDuration(), state.virtualTime + 0.1 * speed);
        state.virtualTime = next;
        onPlaybackTime(next, true);
        if (next >= courseDuration()) { state.virtualPlaying = false; $("#play-toggle").textContent = "▶"; clearInterval(state.virtualTimer); }
      }, 100);
    }
  }
  function jumpTo(time, { seek = false, fromSlider = false } = {}) {
    const oldTime = activeTime();
    const target = Math.max(0, Math.min(num(time), courseDuration() || num(time)));
    if (seek && target > oldTime + 0.5) markSkipped(oldTime, target);
    state.lastTime = target;
    if (hasAudio() && state.audio) state.audio.currentTime = target;
    else state.virtualTime = target;
    updatePlayerDisplay(); syncTranscript(true); queuePlaybackSave();
    if (!fromSlider) { /* explicit transcript click moves position without starting playback */ }
  }
  function onPlaybackTime(eventOrTime, virtual = false) {
    const now = typeof eventOrTime === "number" ? eventOrTime : num(state.audio?.currentTime);
    const previous = state.lastTime;
    const isAdvancing = hasAudio() ? Boolean(state.audio && !state.audio.paused) : state.virtualPlaying;
    if (!state.seeking && isAdvancing && now > previous + 0.015) {
      const crossed = state.detail.questions.filter(question => {
        const match = matchFor(question), point = num(match?.question_time, NaN);
        return Number.isFinite(point) && point >= previous - 0.015 && point <= now + 0.02 && quizReady(question) && !attemptFor(question);
      }).sort((a, b) => num(matchFor(a)?.question_time) - num(matchFor(b)?.question_time));
      if (crossed.length) {
        const questionPoint = num(matchFor(crossed[0])?.question_time);
        if (!virtual) trackListening(questionPoint);
        state.lastTime = questionPoint;
        pauseForQuiz(crossed);
        updatePlayerDisplay(); syncTranscript(); return;
      }
    }
    if (!virtual) trackListening(now);
    state.lastTime = now;
    updatePlayerDisplay(); syncTranscript();
    if (Math.abs(now - num(state.lastSavedTime)) > 2) queuePlaybackSave();
  }
  function pauseForQuiz(questions) {
    if (state.audio && !state.audio.paused) state.audio.pause();
    if (state.virtualPlaying) { state.virtualPlaying = false; clearInterval(state.virtualTimer); $("#play-toggle").textContent = "▶"; }
    state.quizMode = "playback"; state.quizQueue = questions; state.quizIndex = 0; state.quizQuestion = questions[0]; state.quizSelected = null; state.quizAnswer = null;
    state.virtualTime = num(matchFor(questions[0])?.question_time, state.virtualTime);
    if (state.audio) state.audio.currentTime = state.virtualTime;
    state.lastTime = state.virtualTime;
    queuePlaybackSave(); openQuizQuestion(state.quizQuestion);
  }
  function markSkipped(oldTime, newTime) {
    const skipped = state.detail.questions.filter(question => {
      const time = num(matchFor(question)?.question_time, NaN);
      return Number.isFinite(time) && time > oldTime + 0.1 && time <= newTime && !state.skipIds.has(String(question.id)) && !attemptFor(question) && quizReady(question);
    });
    skipped.forEach(question => {
      state.skipIds.add(String(question.id));
    });
    if (skipped.length) { persistSkipped(); renderQuestionListOnly(); toast(`${skipped.length} 道未作答題目已標示為跳過`, "warning"); }
  }
  function updatePlayerDisplay() {
    const current = activeTime();
    const currentEl = $("#player-current"), range = $("#player-range"), durationEl = $("#player-duration");
    const duration = courseDuration();
    const available = timelineAvailable();
    if (currentEl) currentEl.textContent = duration > 0 ? clock(current) : "--:--";
    if (range && !range.matches(":active")) range.value = current;
    if (range) { range.disabled = !available; range.max = duration; }
    if (durationEl) durationEl.textContent = duration > 0 ? clock(duration) : "--:--";
    const playButton = $("#play-toggle"); if (playButton) playButton.disabled = !available;
  }
  function syncTranscript(forceScroll = false) {
    if (!state.detail) return;
    const time = activeTime();
    const active = state.detail.segments.find(segment => time >= num(segment.start) && time <= num(segment.end, num(segment.start) + 12));
    const activeId = active ? String(active.id) : null;
    const activeChanged = state.activeSegment !== activeId;
    if (activeChanged) {
      state.activeSegment = activeId;
      $$("[data-segment-row]").forEach(row => row.classList.toggle("active", row.dataset.segmentRow === activeId));
    }
    if (activeId && state.followTranscript) {
      const row = $(`[data-segment-row="${CSS.escape(activeId)}"]`);
      const list = $("#transcript-list");
      const editorFocused = list?.contains(document.activeElement) && document.activeElement.matches("[data-edit-segment]");
      if (row && list && !editorFocused && (forceScroll || activeChanged || activeId !== state.followedSegment)) {
        const rowBox = row.getBoundingClientRect(), listBox = list.getBoundingClientRect();
        const rowCenter = rowBox.top - listBox.top + list.scrollTop + rowBox.height / 2;
        list.scrollTop = Math.max(0, Math.min(list.scrollHeight - list.clientHeight, rowCenter - list.clientHeight / 2));
        state.followedSegment = activeId;
      }
    }
    syncPreviewTranscript(forceScroll);
  }
  function queuePlaybackSave() {
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(() => savePlayback(false), 900);
  }
  async function savePlayback(force) {
    if (!state.detail) return;
    const position = activeTime(); state.lastSavedTime = position;
    state.detail.playback = { ...(state.detail.playback || {}), position_seconds: position };
    if (isLocalId()) { persistLocalDetail(); return; }
    try { await request(`/courses/${encodeURIComponent(state.currentId)}/playback`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ position_seconds: position }) }); }
    catch (error) { if (force) toast(`播放位置儲存失敗：${error.message}`, "error"); }
  }
  function renderQuestionListOnly() {
    const list = $("#question-list"); if (!list) return;
    const filters = $("#question-filters");
    if (filters) { filters.innerHTML = questionFilterHtml(); wireQuestionFilters(); }
    const hint = $(".match-analysis-strip > span"); if (hint) hint.textContent = questionCategoryHint();
    syncQuestionAnalysisAction();
    list.innerHTML = renderQuestions(state.detail.questions); wireQuestions();
    const oldFooter = $(".question-footer"); if (oldFooter) oldFooter.outerHTML = questionFooterHtml();
    $("#question-prev")?.addEventListener("click", () => { state.questionPage -= 1; renderQuestionListOnly(); });
    $("#question-next")?.addEventListener("click", () => { state.questionPage += 1; renderQuestionListOnly(); });
  }

  function showModal(title, subtitle, body, { wide = false, close = true } = {}) {
    const root = $("#modal-root");
    root.innerHTML = `<div class="modal-backdrop" data-dismiss-modal><section class="modal-card ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}"><header class="modal-head"><div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div>${close ? `<button class="close-button" data-close-modal aria-label="關閉">×</button>` : ""}</header>${body}</section></div>`;
    $$("[data-close-modal]").forEach(button => button.addEventListener("click", () => root.innerHTML = ""));
    $(".modal-backdrop", root)?.addEventListener("click", event => { if (event.target.matches("[data-dismiss-modal]") && close) root.innerHTML = ""; });
    return root;
  }
  function openAISettingsStatus(settings = {}) {
    const source = ["session", "environment", "none"].includes(settings.source) ? settings.source : (settings.configured ? "environment" : "none");
    const configured = Boolean(settings.configured);
    const detail = source === "session"
      ? "目前使用本次伺服器執行階段的設定；伺服器重新啟動後需要重新輸入金鑰。"
      : source === "environment"
        ? "目前使用伺服器環境變數中的金鑰。若要改用其他金鑰，請在下方輸入並儲存。"
        : "尚未設定 OpenAI API 金鑰。設定後即可進行題目對位分析。";
    return { ...settings, source, configured, detail };
  }
  async function showOpenAISettings() {
    const root = showModal("OpenAI 設定", "設定 PBL 與題庫配對使用的 OpenAI API。", '<div class="modal-body loading-state"><span class="spinner"></span>載入 OpenAI 設定…</div>');
    const requestId = String(Date.now()) + Math.random();
    $(".modal-card", root).dataset.openaiSettingsRequest = requestId;
    try {
      const settings = await request("/settings/openai");
      if ($(".modal-card", root)?.dataset.openaiSettingsRequest !== requestId) return;
      renderOpenAISettings(settings, requestId);
    } catch (error) {
      if ($(".modal-card", root)?.dataset.openaiSettingsRequest !== requestId) return;
      const body = `<div class="modal-body"><p class="form-error" role="alert">無法載入 OpenAI 設定：${esc(error.message)}</p><div class="modal-foot"><button class="secondary-button" type="button" data-close-modal>關閉</button><button class="primary-button" type="button" id="retry-openai-settings">重新載入</button></div></div>`;
      showModal("OpenAI 設定", "設定 PBL 與題庫配對使用的 OpenAI API。", body);
      $("#retry-openai-settings")?.addEventListener("click", () => { void showOpenAISettings(); });
    }
  }
  function renderOpenAISettings(rawSettings, requestId) {
    const settings = openAISettingsStatus(rawSettings);
    const statusLabel = settings.configured ? "已設定" : "尚未設定";
    const sourceLabel = settings.source === "session" ? "本次伺服器執行階段" : settings.source === "environment" ? "伺服器環境變數" : "尚無金鑰";
    const body = `<form id="openai-settings-form" class="modal-body">
      <div class="openai-settings-status ${settings.configured ? "configured" : "unconfigured"}" role="status"><strong>${statusLabel} · ${sourceLabel}</strong><p>${esc(settings.detail)}</p></div>
      <div class="form-field"><label for="openai-api-key">OpenAI API 金鑰${settings.configured ? "（留空以保留目前金鑰）" : ""}</label><input id="openai-api-key" name="api_key" type="password" autocomplete="new-password" autocapitalize="off" spellcheck="false" placeholder="sk-…" ${settings.configured ? "" : "required"} /><div class="field-hint">金鑰只會送到伺服器記憶體，不會保存在瀏覽器；設定頁不會顯示金鑰。</div></div>
      <div class="form-field"><label for="openai-model">模型 <span class="optional">（選填）</span></label><input id="openai-model" name="model" type="text" value="${esc(settings.model || "")}" placeholder="留空使用伺服器預設模型" autocomplete="off" /><div class="field-hint">目前模型名稱由伺服器提供，可直接修改。</div></div>
      <div class="openai-settings-note"><p>分析時會將逐字稿文字片段與題目送至 OpenAI API，使用 API 可能產生費用。</p><p><a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener noreferrer">前往 OpenAI API 金鑰頁面</a></p></div>
      <p class="form-error" id="openai-settings-error" hidden role="alert"></p><p class="openai-settings-success" id="openai-settings-success" hidden role="status"></p>
      <div class="modal-foot">${settings.source === "session" ? '<button class="secondary-button" type="button" id="clear-openai-settings">清除本次設定</button>' : ""}<button class="secondary-button" type="button" data-close-modal>取消</button><button class="primary-button" type="submit">儲存設定</button></div>
    </form>`;
    const root = showModal("OpenAI 設定", "設定 PBL 與題庫配對使用的 OpenAI API。", body);
    $(".modal-card", root).dataset.openaiSettingsRequest = requestId;
    const form = $("#openai-settings-form", root);
    form?.addEventListener("submit", async event => {
      event.preventDefault();
      const apiKey = $("#openai-api-key", form).value.trim();
      const model = $("#openai-model", form).value.trim();
      if (!settings.configured && !apiKey) {
        const error = $("#openai-settings-error", form);
        error.textContent = "請輸入 OpenAI API 金鑰。"; error.hidden = false; $("#openai-api-key", form).focus(); return;
      }
      const saveButton = $("button[type=submit]", form);
      const clearButton = $("#clear-openai-settings", form);
      saveButton.disabled = true; if (clearButton) clearButton.disabled = true;
      $("#openai-settings-error", form).hidden = true;
      try {
        const updated = await request("/settings/openai", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ api_key: apiKey, model }) });
        if ($(".modal-card", root)?.dataset.openaiSettingsRequest !== requestId) return;
        renderOpenAISettings(updated, requestId);
        const success = $("#openai-settings-success", root);
        if (success) { success.textContent = "設定已儲存。請再次點擊「繼續分析尚未分析題目」開始分析。"; success.hidden = false; }
      } catch (error) {
        if ($(".modal-card", root)?.dataset.openaiSettingsRequest !== requestId) return;
        const message = $("#openai-settings-error", form);
        message.textContent = `儲存失敗：${error.message}`; message.hidden = false;
        saveButton.disabled = false; if (clearButton) clearButton.disabled = false;
      }
    });
    $("#clear-openai-settings", root)?.addEventListener("click", async event => {
      const button = event.currentTarget;
      button.disabled = true;
      const saveButton = $("button[type=submit]", form); if (saveButton) saveButton.disabled = true;
      $("#openai-settings-error", form).hidden = true;
      try {
        const updated = await request("/settings/openai", { method: "DELETE" });
        if ($(".modal-card", root)?.dataset.openaiSettingsRequest !== requestId) return;
        renderOpenAISettings(updated, requestId);
        const success = $("#openai-settings-success", root);
        if (success) { success.textContent = "本次伺服器執行階段的設定已清除。"; success.hidden = false; }
      } catch (error) {
        if ($(".modal-card", root)?.dataset.openaiSettingsRequest !== requestId) return;
        const message = $("#openai-settings-error", form);
        message.textContent = `清除失敗：${error.message}`; message.hidden = false;
        button.disabled = false; if (saveButton) saveButton.disabled = false;
      }
    });
  }
  function bankAsset(bank, kind) { return bank?.assets?.[kind] || bank?.[`${kind}_asset`] || null; }
  function bankHasAsset(bank, kind) { const asset = bankAsset(bank, kind); return Boolean(asset?.filename || asset?.available); }
  function bankStatusLabel(bank) {
    const status = String(bank?.status || "").toLowerCase();
    if (num(bank?.question_count) > 0 || ["ready", "imported", "completed", "complete"].includes(status)) return "已匯入";
    if (["draft", "pending", "empty", ""].includes(status)) return "未匯入";
    return bank.status;
  }
  function bankIsLocked(bank) { return bankStatusLabel(bank) === "已匯入"; }
  function openBankManager(subject = "pathology") {
    void pauseCourseForModal();
    state.bankManager = { subjectId: subject, banks: [], selectedBank: null, loading: true, error: "", showSync: false, selectedCourseIds: [], syncRunning: false, syncProgress: "" };
    renderBankManager();
    void loadBanks(subject);
  }
  async function loadBanks(subject = state.bankManager?.subjectId) {
    const manager = state.bankManager;
    if (!manager || manager.subjectId !== subject) return;
    manager.loading = true; manager.error = ""; renderBankManager();
    try {
      const data = await request(`/banks?subject_id=${encodeURIComponent(subject)}`);
      if (state.bankManager !== manager) return;
      manager.banks = Array.isArray(data?.banks) ? data.banks : [];
      manager.loading = false;
      const preferred = manager.selectedBank?.id || manager.banks[0]?.id;
      if (preferred) await selectBank(preferred, manager);
      else renderBankManager();
    } catch (error) {
      if (state.bankManager !== manager) return;
      manager.loading = false; manager.error = error.message; renderBankManager();
    }
  }
  async function selectBank(id, manager = state.bankManager) {
    if (!manager || state.bankManager !== manager) return;
    try {
      const bank = await request(`/banks/${encodeURIComponent(id)}`);
      if (state.bankManager !== manager) return;
      manager.selectedBank = bank?.bank || bank;
      renderBankManager();
    } catch (error) { manager.error = error.message; renderBankManager(); }
  }
  function renderBankManager() {
    const manager = state.bankManager; if (!manager) return;
    const selected = manager.selectedBank;
    const locked = selected && bankIsLocked(selected);
    const busy = manager.loading || manager.uploading || manager.importing || manager.creating;
    const body = `<div class="modal-body minimal-bank-manager">
      <nav class="minimal-bank-tabs bank-subjects" aria-label="科目">${SUBJECTS.map(subject=>`<button data-bank-subject="${subject.id}" class="${subject.id===manager.subjectId?"active":""}" ${busy?"disabled":""}>${subject.label}</button>`).join("")}</nav>
      ${manager.banks.length ? `<nav class="minimal-bank-tabs" aria-label="題庫">${manager.banks.map((bank,i)=>`<button data-select-bank="${esc(bank.id)}" class="${String(bank.id)===String(selected?.id)?"active":""}" aria-label="${esc(bank.title)}" ${busy?"disabled":""}>${i+1}</button>`).join("")}<button id="new-bank" aria-label="新增題庫" ${busy?"disabled":""}>＋</button></nav>` : ""}
      <div class="minimal-bank-uploads">${["questions","answers"].map(kind=>{
        const asset=bankAsset(selected,kind), label=kind==="questions"?"題目":"詳解";
        return `<button class="minimal-bank-upload" data-bank-choose="${kind}" ${busy||locked?"disabled":""} aria-label="上傳${label}"><span aria-hidden="true">${manager.uploading===kind?'◌':asset?.filename?'✓':'＋'}</span><strong>${label}</strong>${asset?.filename?`<small>${esc(asset.filename)}</small>`:""}</button><input type="file" data-bank-file="${kind}" accept=".pdf,application/pdf" hidden />`;
      }).join("")}</div>
      ${selected&&!locked&&bankHasAsset(selected,"questions")&&bankHasAsset(selected,"answers")?`<div class="minimal-bank-actions"><button class="primary-button" id="import-bank" aria-label="匯入題庫" title="匯入題庫" ${busy?"disabled":""}>${manager.importing?'◌':'✓'}</button></div>`:""}
      ${manager.error?`<p class="form-error" role="alert">${esc(manager.error)}</p>`:""}</div>`;
    const root=showModal(subjectLabel(manager.subjectId),"",body);
    $$('[data-bank-subject]',root).forEach(button=>button.onclick=()=>openBankManager(button.dataset.bankSubject));
    $$('[data-select-bank]',root).forEach(button=>button.onclick=()=>void selectBank(button.dataset.selectBank,manager));
    $("#new-bank",root)?.addEventListener("click",()=>{manager.selectedBank=null;manager.error="";renderBankManager();});
    $$('[data-bank-choose]',root).forEach(button=>button.onclick=()=>$(`[data-bank-file="${button.dataset.bankChoose}"]`,root).click());
    $$('[data-bank-file]',root).forEach(input=>input.addEventListener("change",async()=>{
      const file=input.files?.[0];if(!file||manager.creating||manager.uploading)return;
      try {
        if(!manager.selectedBank){
          manager.creating=true;renderBankManager();
          const result=await request("/banks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:file.name.replace(/\.pdf$/i,""),subject_id:manager.subjectId})});
          if(state.bankManager!==manager)return;
          manager.selectedBank=result?.bank||result;manager.banks.unshift(manager.selectedBank);
        }
        manager.creating=false;
        await uploadBankFile(manager.selectedBank.id,input.dataset.bankFile,file,manager);
      }catch(error){manager.creating=false;manager.error=error.message;if(state.bankManager===manager)renderBankManager();}
    }));
    $("#import-bank",root)?.addEventListener("click",()=>void importBank(manager));
  }
  async function uploadBankFile(bankId, kind, file, manager) {
    if (manager.uploading) return;
    manager.uploading = kind; manager.error = ""; renderBankManager();
    try {
      await request(`/banks/${encodeURIComponent(bankId)}/assets/${kind}`, { method: "PUT", headers: { "Content-Type": file.type || "application/pdf", "X-Filename": encodeURIComponent(file.name) }, body: file });
      if (state.bankManager !== manager) return;
      toast(`${file.name} 已上傳`); await selectBank(bankId, manager);
      await loadBanks(manager.subjectId);
    } catch (error) { manager.error = error.message; renderBankManager(); }
    finally { if (state.bankManager === manager) { manager.uploading = null; renderBankManager(); } }
  }
  async function importBank(manager) {
    const bankId = manager.selectedBank?.id; if (!bankId || manager.importing) return;
    manager.importing = true; manager.error = ""; renderBankManager();
    try {
      const result = await request(`/banks/${encodeURIComponent(bankId)}/import`, { method: "POST" });
      if (state.bankManager !== manager) return;
      manager.selectedBank = result?.bank || result; manager.importing = false; toast(`題庫已匯入，共 ${num(manager.selectedBank.question_count, manager.selectedBank.questions?.length || 0)} 題`);
      await loadBanks(manager.subjectId);
    } catch (error) { manager.importing = false; manager.error = error.message; renderBankManager(); }
  }
  async function rematchSelectedCourses(manager) {
    if (manager.syncRunning) return;
    const ids = [...manager.selectedCourseIds]; if (!ids.length) return;
    manager.syncRunning = true; manager.syncProgress = `準備配對 ${ids.length} 堂課…`; renderBankManager();
    const failures = [];
    for (let index = 0; index < ids.length; index += 1) {
      const id = ids[index];
      manager.syncProgress = `正在配對第 ${index + 1} / ${ids.length} 堂課…`; renderBankManager();
      try { await request(`/courses/${encodeURIComponent(id)}/rematch`, { method: "POST" }); }
      catch (error) { failures.push(`${state.courses.find(course => String(course.id) === id)?.title || id}：${error.message}`); }
    }
    manager.syncRunning = false; manager.syncProgress = failures.length ? `完成 ${ids.length - failures.length} 堂，${failures.length} 堂失敗` : `已完成 ${ids.length} 堂課的題庫配對`;
    await loadCourses();
    if (ids.includes(String(state.currentId)) && !isLocalId()) await refreshDetail().catch(error => toast(`課程更新失敗：${error.message}`, "error"));
    renderBankManager();
    if (failures.length) toast(failures.join("；"), "error"); else toast(manager.syncProgress);
  }

  async function openWrongReview() {
    await pauseCourseForModal();
    state.wrongReview = { subjectId: state.subjectFilter || "all", courseId: "all", history: false, questions: [], index: 0, selected: null, answer: null, loading: true, error: "" };
    renderWrongReview(); await loadWrongQuestions();
  }
  async function loadWrongQuestions() {
    const review = state.wrongReview; if (!review) return;
    review.loading = true; review.error = ""; renderWrongReview();
    const params = new URLSearchParams({ history: review.history ? "1" : "0" });
    if (review.subjectId !== "all") params.set("subject_id", review.subjectId);
    if (review.courseId !== "all") params.set("course_id", review.courseId);
    try {
      const data = await request(`/wrong-questions?${params.toString()}`);
      if (state.wrongReview !== review) return;
      review.questions = Array.isArray(data?.questions) ? data.questions : [];
      review.index = Math.min(review.index, Math.max(0, review.questions.length - 1));
      review.loading = false; review.selected = null; review.answer = null; renderWrongReview();
    } catch (error) { if (state.wrongReview === review) { review.loading = false; review.error = error.message; renderWrongReview(); } }
  }
  function renderWrongReview() {
    const review = state.wrongReview; if (!review) return;
    const courses = state.courses.filter(course => review.subjectId === "all" || subjectId(course) === review.subjectId);
    const question = review.questions[review.index];
    const options = optionList(question);
    const filters = `<div class="wrong-review-filters"><label>科目<select id="wrong-subject"><option value="all">全部科目</option>${SUBJECTS.map(subject => `<option value="${subject.id}" ${review.subjectId === subject.id ? "selected" : ""}>${subject.label}</option>`).join("")}</select></label><label>課程<select id="wrong-course"><option value="all">全部課程</option>${courses.map(course => `<option value="${esc(course.id)}" ${review.courseId === String(course.id) ? "selected" : ""}>${esc(course.title || "未命名課程")}</option>`).join("")}</select></label><label class="history-toggle"><input id="wrong-history" type="checkbox" ${review.history ? "checked" : ""} />查看所有曾答錯的題目</label></div>`;
    const questionBody = review.loading ? `<div class="loading-state"><span class="spinner"></span>載入錯題…</div>` : review.error ? `<p class="form-error" role="alert">${esc(review.error)}</p>` : question ? `<div class="wrong-review-card"><div class="wrong-review-meta"><span>${esc(question.course_title || "課程")}${question.bank_title ? ` · ${esc(question.bank_title)}` : ""}</span><span>Q ${esc(question.number ?? "—")} · 第 ${review.index + 1} / ${review.questions.length} 題</span></div><h3>${esc(question.stem || "（題目內容尚未辨識）")}</h3><div class="quiz-options">${options.map(option => {
      let klass = "";
      if (review.selected === option.key) klass = "selected";
      if (review.answer && option.key === review.answer.correct_option) klass = "correct";
      else if (review.answer && review.selected === option.key) klass = "incorrect";
      return `<button class="quiz-option ${klass}" data-wrong-option="${esc(option.key)}" ${review.answer ? "disabled" : ""}><span class="option-key">${esc(option.key)}</span><span>${esc(option.text)}</span></button>`;
    }).join("")}</div>${review.answer ? `<div class="explanation-box"><div class="answer-result ${review.answer.correct ? "" : "wrong"}">${review.answer.correct ? (review.history ? "✓ 答對了" : "✓ 答對了，已從待複習清單移除") : `↻ 正確答案：${esc(review.answer.correct_option)}`}</div><p>${esc(review.answer.explanation || "目前沒有提供解析。")}</p></div>` : ""}<footer class="quiz-foot"><small>${review.history ? "歷史作答仍會保留在學習紀錄中。" : "作答後會立即顯示正解與解析。"}</small><button class="primary-button" id="wrong-submit" ${review.answer ? "" : review.selected ? "" : "disabled"}>${review.answer ? (review.index + 1 < review.questions.length ? "下一題" : "完成") : "送出答案"} <span>→</span></button></footer></div>` : `<div class="wrong-empty"><span class="empty-illustration">✓</span><strong>${review.history ? "目前沒有作答紀錄" : "目前沒有待複習錯題"}</strong><p>選擇其他科目或課程查看題目。</p></div>`;
    const root = showModal("錯題複習", "依科目或課程查看需要加強的考題。", `<div class="modal-body wrong-review-body">${filters}${questionBody}</div>`, { wide: true });
    $("#wrong-subject", root)?.addEventListener("change", event => { review.subjectId = event.target.value; review.courseId = "all"; review.index = 0; void loadWrongQuestions(); });
    $("#wrong-course", root)?.addEventListener("change", event => { review.courseId = event.target.value; review.index = 0; void loadWrongQuestions(); });
    $("#wrong-history", root)?.addEventListener("change", event => { review.history = event.target.checked; review.index = 0; void loadWrongQuestions(); });
    $$('[data-wrong-option]', root).forEach(button => button.addEventListener("click", () => { review.selected = button.dataset.wrongOption; renderWrongReview(); }));
    $("#wrong-submit", root)?.addEventListener("click", () => void submitWrongReview());
  }
  async function submitWrongReview() {
    const review = state.wrongReview; const question = review?.questions[review.index]; if (!question) return;
    if (review.answer) {
      if (review.index + 1 < review.questions.length) { review.index += 1; review.selected = null; review.answer = null; renderWrongReview(); }
      else { state.wrongReview = null; $("#modal-root").innerHTML = ""; }
      return;
    }
    if (!review.selected) return;
    try {
      const result = await request(`/questions/${encodeURIComponent(question.id)}/attempt`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ selected_option: review.selected }) });
      review.answer = { correct: Boolean(result.correct), correct_option: result.correct_option, explanation: result.explanation };
      if (state.detail && String(state.detail.id) === String(question.course_id)) {
        state.detail.attempts.push({ question_id: question.id, selected_option: review.selected, correct: Boolean(result.correct), attempted_at: new Date().toISOString(), round_id: state.detail.learning?.round_id });
      }
      renderWrongReview();
    } catch (error) { review.error = error.message; renderWrongReview(); }
  }

  function updateLearningPanel() {
    const panel = $(".learning-panel"); if (!panel) return;
    panel.outerHTML = learningPanelHtml();
    $("#learning-history")?.addEventListener("click", showLearningHistory);
    $("#restart-round")?.addEventListener("click", restartLearningRound);
  }
  async function showLearningHistory() {
    await pauseCourseForModal();
    const courseId = String(state.currentId); if (!state.detail) return;
    if (isLocalId()) {
      const detail = state.detail;
      renderLearningHistory({ rounds: [{ round_number: num(detail.learning?.round_number, 1), listened_seconds: num(detail.learning?.listened_seconds), duration_seconds: courseDuration(), progress: num(detail.learning?.progress), completed: false }], attempts: detail.attempts || [] });
      return;
    }
    const root = showModal("學習紀錄", "每輪聆聽進度與作答紀錄都會保留。", `<div class="modal-body loading-state"><span class="spinner"></span>載入紀錄…</div>`, { wide: true });
    try {
      const data = await request(`/courses/${encodeURIComponent(courseId)}/history`);
      if (String(state.currentId) !== courseId || !$(".modal-card", root)) return;
      root.innerHTML = ""; renderLearningHistory(data || {});
    } catch (error) { if (String(state.currentId) === courseId) showModal("學習紀錄", "", `<div class="modal-body"><p class="form-error">${esc(error.message)}</p></div>`, { wide: true }); }
  }
  function renderLearningHistory(history) {
    const rounds = Array.isArray(history.rounds) ? history.rounds : [];
    const attempts = Array.isArray(history.attempts) ? history.attempts : [];
    const roundById = new Map(rounds.map(round => [String(round.round_id), round]));
    const questionById = new Map((state.detail?.questions || []).map(question => [String(question.id), question]));
    const roundRows = rounds.length ? rounds.map(round => {
      const rawProgress = num(round.progress);
      const progress = Math.round(Math.max(0, Math.min(1, rawProgress <= 1 ? rawProgress : rawProgress / 100)) * 100);
      return `<div class="history-round"><strong>第 ${esc(round.round_number ?? "—")} 輪</strong><span>${clock(num(round.listened_seconds))} / ${clock(num(round.duration_seconds))}</span><span>${round.completed ? "已完成" : `${progress}%`}</span></div>`;
    }).join("") : `<p class="history-empty">尚無學習輪次。</p>`;
    const attemptRows = attempts.length ? attempts.slice().reverse().map(attempt => {
      const round = roundById.get(String(attempt.round_id));
      const question = questionById.get(String(attempt.question_id));
      const questionLabel = question ? `Q ${question.number ?? "—"}${question.bank_title ? ` · ${question.bank_title}` : ""}` : `題目 ${String(attempt.question_id).slice(0, 8)}`;
      return `<div class="history-attempt"><span>第 ${esc(round?.round_number ?? "—")} 輪</span><span title="${esc(question?.stem || "")}">${esc(questionLabel)}</span><span>${esc(attempt.selected_option)} · ${attempt.correct ? "答對" : "答錯"}</span><time>${esc(attempt.attempted_at || "")}</time></div>`;
    }).join("") : `<p class="history-empty">尚無作答紀錄。</p>`;
    showModal("學習紀錄", "每輪聆聽進度與作答紀錄都會保留。", `<div class="modal-body learning-history"><h3>聆聽輪次</h3><div class="history-round-list">${roundRows}</div><h3>作答紀錄</h3><div class="history-attempt-list">${attemptRows}</div></div>`, { wide: true });
  }
  async function restartLearningRound() {
    const courseId = String(state.currentId);
    if (isLocalId()) { toast("本機試用課程沒有伺服器學習輪次。", "warning"); return; }
    const button = $("#restart-round"); if (button) { button.disabled = true; button.textContent = "重設中…"; }
    try {
      const previousRound = num(state.detail?.learning?.round_number, 1);
      if (state.audio && !state.audio.paused) state.audio.pause();
      clearTimeout(state.saveTimer); endListeningTracking(); await state.listeningQueue; await savePlayback(true);
      if (state.audio) state.audio.currentTime = 0;
      await request(`/courses/${encodeURIComponent(courseId)}/restart`, { method: "POST" });
      if (String(state.currentId) === courseId) {
        state.virtualPlaying = false; clearInterval(state.virtualTimer); state.quizQuestion = null; state.quizQueue = []; $("#modal-root").innerHTML = "";
        state.skipIds.clear(); state.skippedByCourse[courseId] = []; persistSkipped();
        await openCourse(courseId); toast(`已開始第 ${num(state.detail?.learning?.round_number, previousRound + 1)} 輪，歷史保留。`);
      }
    } catch (error) { toast(`無法重新開始本輪：${error.message}`, "error"); updateLearningPanel(); }
  }
  function startListeningTracking() {
    if (isLocalId() || !hasAudio() || !state.audio) return;
    const learning = state.detail?.learning || {};
    state.listening = { courseId: String(state.currentId), roundId: learning.round_id, lastPosition: num(state.audio.currentTime), lastWall: performance.now() / 1000, start: null, end: null };
  }
  function enqueueListening(courseId, start, end, roundId) {
    let cursor = start;
    while (end - cursor > 0.01) {
      const chunkEnd = Math.min(end, cursor + 30);
      const intervalStart = cursor, intervalEnd = chunkEnd;
      state.listeningQueue = state.listeningQueue.then(async () => {
        const result = await request(`/courses/${encodeURIComponent(courseId)}/listening`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ start_seconds: intervalStart, end_seconds: intervalEnd, round_id: roundId }), keepalive: true });
        const learning = result?.learning || result;
        if (String(state.currentId) === String(courseId) && learning && (learning.round_id == null || String(learning.round_id) === String(state.detail?.learning?.round_id))) {
          state.detail.learning = { ...(state.detail.learning || {}), ...learning }; updateLearningPanel();
          const course = state.courses.find(item => String(item.id) === String(courseId));
          if (course) { course.learning = { ...(course.learning || {}), ...learning }; renderCourseList(); }
        }
      }).catch(error => { if (String(state.currentId) === String(courseId)) toast(`學習時間儲存失敗：${error.message}`, "error"); });
      cursor = chunkEnd;
    }
  }
  function trackListening(now, allowPaused = false) {
    if (isLocalId() || !hasAudio() || !state.audio || (!allowPaused && state.audio.paused) || state.seeking || num(state.detail?.learning?.duration_seconds) <= 0) return;
    const courseId = String(state.currentId);
    const roundId = state.detail?.learning?.round_id;
    let tracker = state.listening;
    if (!tracker || tracker.courseId !== courseId || String(tracker.roundId) !== String(roundId)) {
      if (tracker?.start != null && tracker.end > tracker.start) enqueueListening(tracker.courseId, tracker.start, tracker.end, tracker.roundId);
      startListeningTracking(); tracker = state.listening; return;
    }
    const wallNow = performance.now() / 1000;
    const wallDelta = Math.max(0, wallNow - tracker.lastWall);
    const mediaDelta = now - tracker.lastPosition;
    const speed = num(state.audio.playbackRate, 1);
    if (mediaDelta <= 0 || mediaDelta > wallDelta * speed + Math.max(0.8, speed * 0.4)) {
      if (tracker.start != null && tracker.end > tracker.start) enqueueListening(courseId, tracker.start, tracker.end, roundId);
      tracker.start = null; tracker.end = null;
      tracker.lastPosition = now; tracker.lastWall = wallNow; return;
    }
    if (tracker.start == null) tracker.start = tracker.lastPosition;
    tracker.end = now; tracker.lastPosition = now; tracker.lastWall = wallNow;
    while (tracker.end - tracker.start >= 30) {
      enqueueListening(courseId, tracker.start, tracker.start + 30, roundId);
      tracker.start += 30;
    }
    if (tracker.start != null && tracker.end <= tracker.start + 0.01) tracker.start = tracker.end = null;
  }
  function endListeningTracking() {
    const tracker = state.listening;
    if (tracker?.start != null && tracker.end > tracker.start) enqueueListening(tracker.courseId, tracker.start, tracker.end, tracker.roundId);
    state.listening = null;
  }
  function showEditCourseTitle() {
    if (!state.detail) return;
    const courseId = String(state.currentId);
    const root = showModal("編輯課程名稱", "新名稱會立即更新側邊欄與課程頁標題。", `<form id="course-title-form" class="modal-body"><div class="form-field"><label for="edit-course-title-input">課程名稱</label><input id="edit-course-title-input" name="title" maxlength="200" value="${esc(state.detail.title || "")}" required autofocus /></div><p class="form-error" id="course-title-error" hidden></p><div class="modal-foot"><button class="secondary-button" type="button" data-close-modal>取消</button><button class="primary-button" type="submit">儲存名稱</button></div></form>`);
    $("#course-title-form", root).addEventListener("submit", async event => {
      event.preventDefault();
      const title = $("#edit-course-title-input", root).value.trim();
      const errorNode = $("#course-title-error", root);
      const submit = $("button[type=submit]", root);
      if (!title || title.length > 200) {
        errorNode.hidden = false; errorNode.textContent = "課程名稱不能空白，且最多 200 個字元。"; return;
      }
      if (title === String(state.detail?.title || "")) { root.innerHTML = ""; return; }
      submit.disabled = true; submit.innerHTML = '<span class="spinner"></span>儲存中';
      try {
        let savedTitle = title;
        if (isLocalId(courseId)) {
          const local = localCourse(courseId);
          if (!local) throw new Error("找不到這堂本機試用課程。");
          local.title = title;
          if (String(state.currentId) === courseId && state.detail) state.detail.title = title;
          writeLocalCourses();
          state.courses = normalizeCourses([]);
        } else {
          const result = await request(`/courses/${encodeURIComponent(courseId)}`, {
            method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title })
          });
          savedTitle = result?.course?.title || result?.title || title;
          const course = state.courses.find(item => String(item.id) === courseId);
          if (course) course.title = savedTitle;
          if (String(state.currentId) === courseId && state.detail) state.detail.title = savedTitle;
        }
        renderCourseList();
        if (String(state.currentId) === courseId) {
          setHeader(savedTitle);
          const heading = $(".course-heading-title h1");
          if (heading) heading.textContent = savedTitle;
        }
        root.innerHTML = "";
        toast("課程名稱已更新。");
      } catch (error) {
        errorNode.hidden = false; errorNode.textContent = error.message;
        submit.disabled = false; submit.textContent = "儲存名稱";
      }
    });
  }
  function parseRetranscribeTime(value) {
    const raw = String(value ?? "").trim();
    if (!raw) return NaN;
    if (/^\d+(?:\.\d+)?$/.test(raw)) return Number(raw);
    const parts = raw.split(":");
    if (parts.length !== 2 && parts.length !== 3) return NaN;
    if (parts.some(part => !/^\d+$/.test(part))) return NaN;
    const values = parts.map(Number);
    const secondsPart = values.at(-1);
    if (secondsPart > 59) return NaN;
    if (parts.length === 2) return values[0] * 60 + secondsPart;
    if (values[1] > 59) return NaN;
    return values[0] * 3600 + values[1] * 60 + secondsPart;
  }
  function showRangeRetranscribeModal(suggested = null) {
    const courseId = String(state.currentId);
    const detail = state.detail;
    const duration = audioDuration(detail);
    if (isLocalId(courseId) || !detail || !assetState().audio || duration <= 0 || transcriptionSettingsLocked(courseId)) {
      toast(!assetState().audio ? "請先上傳課堂錄音。" : duration <= 0 ? "目前沒有可用的音檔時長。" : "課程正在處理，請稍後再試。", "warning");
      return;
    }
    const minutes = transcriptionChunkMinutes(detail.transcription_chunk_minutes);
    const root = showModal("局部重新辨識", `使用本機 Whisper 重新辨識指定時間範圍。音檔長度 ${clock(duration, true)}。`, `<form id="retranscribe-range-form" class="modal-body"><div class="form-grid"><div class="form-field"><label for="range-start">開始時間</label><input id="range-start" name="start" type="text" inputmode="text" autocomplete="off" placeholder="例如 26:06 或 00:26:06" required /></div><div class="form-field"><label for="range-end">結束時間</label><input id="range-end" name="end" type="text" inputmode="text" autocomplete="off" placeholder="例如 30:00 或 00:30:00" required /></div></div><div class="range-retranscribe-note"><strong>這次辨識設定</strong><p>使用本機 Whisper，沿用目前每 ${minutes} 分鐘的分段設定。完整句子邊界可能讓實際範圍稍微擴展，工作進度會回報實際範圍。</p><p>範圍外的逐字稿與所有作答紀錄會保留。若選取範圍重疊手動新增或修正的逐字稿，伺服器會拒絕這次工作。</p></div><div class="field-hint range-time-hint">可輸入 mm:ss、hh:mm:ss 或純秒數（例如 1566）。冒號格式的秒數欄位需在 0–59；hh:mm:ss 的分鐘也需在 0–59。選取範圍須位於音檔內。</div><p class="form-error" id="range-error" hidden role="alert"></p><div class="modal-foot"><button class="secondary-button" type="button" data-close-modal>取消</button><button class="primary-button" type="submit">開始局部重新辨識</button></div></form>`, { wide: true });
    if (suggested && Number.isFinite(suggested.start) && Number.isFinite(suggested.end)) {
      $("#range-start", root).value = clock(suggested.start, true);
      $("#range-end", root).value = clock(suggested.end, true);
    }
    $("#retranscribe-range-form", root).addEventListener("submit", async event => {
      event.preventDefault();
      const start = parseRetranscribeTime($("#range-start", root).value);
      const end = parseRetranscribeTime($("#range-end", root).value);
      const errorNode = $("#range-error", root);
      const submit = $("button[type=submit]", root);
      let validationError = "";
      if (!Number.isFinite(start) || !Number.isFinite(end)) validationError = "請輸入有效時間，可用 mm:ss、hh:mm:ss 或純秒數。";
      else if (start < 0 || end <= start) validationError = "結束時間必須晚於開始時間。";
      else if (end > duration) validationError = `時間範圍不能超過音檔長度 ${clock(duration, true)}。`;
      else if (transcriptionSettingsLocked(courseId) || !assetState().audio || audioDuration(state.detail) <= 0) validationError = "課程正在處理，或錄音與音檔時長目前不可用。請稍後再試。";
      if (validationError) { errorNode.hidden = false; errorNode.textContent = validationError; return; }
      errorNode.hidden = true;
      submit.disabled = true; submit.innerHTML = '<span class="spinner"></span>啟動中';
      try {
        const result = await request(`/courses/${encodeURIComponent(courseId)}/retranscribe-range`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ start_seconds: start, end_seconds: end })
        });
        if (String(state.currentId) !== courseId) { root.innerHTML = ""; return; }
        const job = result?.job || result || { status: "queued" };
        state.job = { ...job, kind: job.kind || "range_transcription" };
        root.innerHTML = "";
        updateStatusBanner(); pollJob(courseId);
        toast("局部重新辨識已開始；完成後會更新這段逐字稿。");
      } catch (error) {
        errorNode.hidden = false; errorNode.textContent = error.message;
        submit.disabled = false; submit.textContent = "開始局部重新辨識";
      }
    });
  }
  function showCreateCourse() {
    const selectedSubject = state.subjectFilter || "pathology";
    const subjectOptions = SUBJECTS.map(subject => `<option value="${subject.id}" ${subject.id === selectedSubject ? "selected" : ""}>${subject.label}</option>`).join("");
    const root = showModal("建立一堂課", "先選擇科目並命名課程。建立後可上傳錄音，系統會自動開始轉錄。", `<form id="create-course-form" class="modal-body"><div class="form-field"><label for="course-title">課程名稱</label><input id="course-title" name="title" maxlength="100" placeholder="例如：心臟內科・心衰竭" required autofocus /></div><div class="form-field"><label for="course-subject">科目</label><select id="course-subject" name="subject_id">${subjectOptions}</select></div><div class="form-field"><label for="create-transcription-chunk-minutes">錄音分段轉錄</label><select id="create-transcription-chunk-minutes" name="transcription_chunk_minutes"><option value="5" selected>每 5 分鐘</option><option value="10">每 10 分鐘</option></select><div class="field-hint">5 分鐘會較頻繁重新辨識；10 分鐘可減少模型重新載入次數。分段仍可能有辨識錯誤；逐字稿會合併成同一條時間軸。</div></div><div class="form-field"><label>開始方式</label><div class="radio-options"><label class="radio-option"><input type="radio" name="startMode" value="files" checked /> 稍後上傳課堂錄音</label><label class="radio-option"><input type="radio" name="startMode" value="manual" /> 先手動輸入逐字稿</label></div><div class="field-hint">不需要先準備題目或答案檔案；科目題庫可在題庫管理中重複使用。</div></div><p class="form-error" id="create-error" hidden></p><div class="modal-foot"><button class="secondary-button" type="button" data-close-modal>取消</button><button class="primary-button" type="submit">建立課程 <span>→</span></button></div></form>`);
    $("#create-course-form", root).addEventListener("submit", async event => {
      event.preventDefault();
      const title = $("#course-title", root).value.trim();
      const subject = $("#course-subject", root).value;
      const transcriptionChunk = transcriptionChunkMinutes($("#create-transcription-chunk-minutes", root).value);
      if (!title) return;
      const startMode = $("input[name=startMode]:checked", root)?.value || "files";
      const submit = $("button[type=submit]", root); submit.disabled = true; submit.innerHTML = `<span class="spinner"></span>建立中`;
      let course;
      try {
        const result = await request("/courses", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, subject_id: subject, transcription_chunk_minutes: transcriptionChunk }) });
        course = result?.course || result;
      } catch (error) {
        if (error.status) { $("#create-error", root).hidden = false; $("#create-error", root).textContent = error.message; submit.disabled = false; submit.textContent = "建立課程 →"; return; }
        course = { id: `local-${Date.now()}`, title, subject_id: subject, transcription_chunk_minutes: transcriptionChunk, created_at: new Date().toISOString(), processing_status: "pending", audio: null, segments: [], questions: [], matches: [], attempts: [], playback: { position_seconds: 0 }, learning: { round_number: 1, listened_seconds: 0, progress: 0, completed_rounds: 0 }, _local: true };
        state.localCourses.unshift(course); writeLocalCourses(); toast("本機服務尚未連線，已建立瀏覽器試用課程。", "warning");
      }
      root.innerHTML = ""; state.subjectFilter = subject; state.localAssets[String(course.id)] ||= {};
      await loadCourses(); await openCourse(course.id);
      if (startMode === "manual") showAddSegments();
    });
  }
  function showAddSegments() {
    const root = showModal("手動新增逐字稿", "每行會建立一個有起訖時間的段落；稍後可直接在逐字稿中修正文句。", `<form id="segments-form" class="modal-body"><div class="form-grid"><div class="form-field"><label for="segment-start">第一段開始時間</label><input id="segment-start" name="start" value="${clock(state.detail.segments.reduce((max, segment) => Math.max(max, num(segment.end)), 0))}" placeholder="00:00" /></div><div class="form-field"><label for="segment-length">每段時間（秒）</label><input id="segment-length" name="length" type="number" min="1" max="120" value="8" /></div></div><div class="form-field"><label for="segment-lines">逐字稿內容</label><textarea id="segment-lines" name="lines" rows="8" placeholder="第一句課堂內容\n第二句課堂內容\n第三句課堂內容" required></textarea><div class="field-hint">一行一段。系統依每段秒數順序編排；也可以直接在逐字稿列表修正每段起訖前的文字。</div></div><p class="form-error" id="segments-error" hidden></p><div class="modal-foot"><button class="secondary-button" type="button" data-close-modal>取消</button><button class="primary-button" type="submit">新增段落</button></div></form>`, { wide: true });
    $("#segments-form", root).addEventListener("submit", async event => {
      event.preventDefault();
      const lines = $("#segment-lines", root).value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      if (!lines.length) return;
      const start = Math.max(0, seconds($("#segment-start", root).value) || 0), length = Math.max(1, num($("#segment-length", root).value, 8));
      const segments = lines.map((text, index) => ({ start: Number((start + length * index).toFixed(2)), end: Number((start + length * (index + 1)).toFixed(2)), text }));
      const submit = $("button[type=submit]", root); submit.disabled = true; submit.innerHTML = `<span class="spinner"></span>儲存中`;
      try {
        if (isLocalId()) state.detail.segments.push(...segments.map((segment, i) => ({ ...segment, id: `local-segment-${Date.now()}-${i}` })));
        else await request(`/courses/${encodeURIComponent(state.currentId)}/segments`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ segments }) });
        root.innerHTML = ""; await refreshDetail(); toast(`${segments.length} 個逐字稿段落已新增`);
      } catch (error) { const fieldError = $("#segments-error", root); fieldError.hidden = false; fieldError.textContent = error.message; submit.disabled = false; submit.textContent = "新增段落"; }
    });
  }
  function showAddQuestion() {
    const root = showModal("手動新增題目", "新增一題單選題、正解與解析，並設定播放時自動暫停的時間。", questionFormHtml());
    wireQuestionForm(root, null);
  }
  function questionFormHtml(question = null) {
    const editing = Boolean(question);
    const options = optionList(question || { options: ["", "", "", ""] });
    const initialKeys = [...new Set([...options.map(option => option.key), "A", "B", "C", "D"])];
    const optionsText = options.map(option => `${option.key}) ${option.text}`).join("\n");
    const matchTime = matchFor(question)?.question_time;
    const timeText = Number.isFinite(num(matchTime, NaN)) ? clock(num(matchTime)) : "";
    const titleNumber = question?.number ?? (state.detail.questions.length + 1);
    const correctOptions = initialKeys.map(key => `<option value="${key}">${key}</option>`).join("");
    const answerFields = `<div class="form-field"><label for="question-correct">正確答案</label><select id="question-correct" name="correct_option" ${editing ? "" : "required"}><option value="">請選擇</option>${correctOptions}</select></div>
      <div class="form-field"><label for="question-explanation">答案解析</label><textarea id="question-explanation" name="explanation" rows="2" placeholder="為什麼這個選項正確？">${esc(question?.explanation || "")}</textarea></div>`;
    return `<form id="question-form" class="modal-body">
      <div class="form-grid">
        <div class="form-field"><label for="question-number">題號</label><input id="question-number" name="number" value="${esc(titleNumber)}" /></div>
        <div class="form-field"><label for="question-time-input">建議出題時間 <span class="optional">可稍後設定</span></label><input id="question-time-input" name="question_time" placeholder="例如 12:35" value="${esc(timeText)}" /></div>
      </div>
      <div class="form-field"><label for="question-stem">題目</label><textarea id="question-stem" name="stem" rows="3" required>${esc(question?.stem || "")}</textarea></div>
      <div class="form-field"><label>選項 <span class="optional">一行一個選項</span></label><textarea id="question-options" name="options" rows="4" placeholder="A) 選項一\nB) 選項二\nC) 選項三\nD) 選項四" required>${esc(optionsText)}</textarea><div class="field-hint">格式可用「A) 選項內容」，或直接一行輸入一個選項。</div></div>
      <div class="form-field"><label for="question-source-page">來源頁碼 <span class="optional">選填</span></label><input id="question-source-page" name="source_page" type="number" min="1" value="${esc(question?.source_page || "")}" /></div>
      ${editing ? `<label class="question-answer-toggle"><input id="update-question-answer" type="checkbox" />更新標準答案與解析</label><div id="question-answer-fields" hidden>${answerFields}</div><p class="field-hint answer-preserve-hint">未勾選時，只儲存題幹與選項，保留原答案與解析。</p>` : answerFields}
      <p class="form-error" id="question-error" hidden></p>
      <div class="modal-foot"><button class="secondary-button" type="button" data-close-modal>取消</button><button class="primary-button" type="submit">${question ? "儲存修正" : "新增題目"}</button></div>
    </form>`;
  }
  function wireQuestionForm(root, existing) {
    const optionsField = $("#question-options", root);
    const correctSelect = $("#question-correct", root);
    const answerToggle = $("#update-question-answer", root);
    const answerFields = $("#question-answer-fields", root);
    const initialMatchTimeText = existing && Number.isFinite(num(matchFor(existing)?.question_time, NaN))
      ? clock(num(matchFor(existing).question_time)) : "";
    if (answerToggle && answerFields) {
      answerToggle.addEventListener("change", () => {
        answerFields.hidden = !answerToggle.checked;
        correctSelect.required = answerToggle.checked;
        if (answerToggle.checked) correctSelect.value = "";
      });
    }
    const syncCorrectOptions = () => {
      const previous = correctSelect.value;
      const lines = optionsField.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      const keys = [...new Set(lines.map((line, index) => line.match(/^([A-Z])\s*[).、:：-]/i)?.[1]?.toUpperCase() || String.fromCharCode(65 + index)))];
      correctSelect.innerHTML = `<option value="">請選擇</option>${keys.map(key => `<option value="${esc(key)}">${esc(key)}</option>`).join("")}`;
      correctSelect.value = keys.includes(previous) ? previous : "";
    };
    optionsField.addEventListener("input", syncCorrectOptions);
    $("#question-form", root).addEventListener("submit", async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const optionLines = $("#question-options", form).value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      const options = optionLines.map((line, index) => {
        const matched = line.match(/^([A-Z])\s*[).、:：-]\s*(.*)$/i);
        return { key: matched?.[1]?.toUpperCase() || String.fromCharCode(65 + index), text: matched?.[2] || line };
      });
      if (options.length < 2) { $("#question-error", form).hidden = false; $("#question-error", form).textContent = "請至少輸入兩個選項。"; return; }
      const optionKeys = new Set(options.map(option => option.key));
      const updateAnswer = existing ? Boolean(answerToggle?.checked) : true;
      const correctOption = $("#question-correct", form).value;
      if (updateAnswer && (!correctOption || !optionKeys.has(correctOption))) {
        $("#question-error", form).hidden = false;
        $("#question-error", form).textContent = "請從目前的有效選項中明確選擇正確答案。";
        return;
      }
      const payload = { number: $("#question-number", form).value.trim() || String(state.detail.questions.length + 1), stem: $("#question-stem", form).value.trim(), options, source_page: num($("#question-source-page", form).value, null) };
      const answerPayload = { correct_option: correctOption, explanation: $("#question-explanation", form).value.trim() };
      const submit = $("button[type=submit]", form); submit.disabled = true; submit.innerHTML = `<span class="spinner"></span>儲存中`;
      try {
        let created;
        if (existing) {
          if (isLocalId()) {
            Object.assign(existing, payload);
            if (updateAnswer) Object.assign(existing, answerPayload, { needs_review: false });
          } else {
            const patch = { ...payload };
            if (updateAnswer) Object.assign(patch, answerPayload, { needs_review: false });
            await request(`/questions/${encodeURIComponent(existing.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
          }
          created = existing;
        } else if (isLocalId()) {
          created = { ...payload, ...answerPayload, id: `local-question-${Date.now()}` };
          state.detail.questions.push(created);
        } else {
          created = await request(`/courses/${encodeURIComponent(state.currentId)}/questions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...payload, ...answerPayload }) });
          created = created?.question || created?.questions?.[0] || created;
        }
        const questionTime = seconds($("#question-time-input", form).value);
        const questionTimeText = $("#question-time-input", form).value.trim();
        const changedQuestionTime = Number.isFinite(questionTime) && (!existing || questionTimeText !== initialMatchTimeText);
        if (changedQuestionTime) {
          if (isLocalId()) {
            const match = matchFor(created);
            if (match) Object.assign(match, { question_time: questionTime, start: questionTime, end: questionTime, status: "confirmed" });
            else state.detail.matches.push({ id: `local-match-${Date.now()}`, question_id: created.id, question_time: questionTime, start: questionTime, end: questionTime, status: "confirmed" });
          }
          else await request(`/questions/${encodeURIComponent(created.id)}/match`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question_time: questionTime, status: "confirmed" }) });
        } else if (!existing) { state.questionCategory = "unanalysed"; state.questionPage = 1; }
        root.innerHTML = ""; await refreshDetail(); toast(existing ? "題目修正已儲存" : "題目已新增；正解與解析只會在作答後顯示");
      } catch (error) { $("#question-error", form).hidden = false; $("#question-error", form).textContent = error.message; submit.disabled = false; submit.textContent = existing ? "儲存修正" : "新增題目"; }
    });
  }
  function showEditQuestion(id) {
    const question = state.detail.questions.find(item => String(item.id) === String(id)); if (!question) return;
    const root = showModal(`修正第 ${question.number ?? ""} 題`, "可校正 PDF 辨識出的題幹、選項與來源頁碼。", questionFormHtml(question), { wide: true });
    wireQuestionForm(root, question);
  }
  async function refreshDetail() {
    if (isLocalId()) { persistLocalDetail(); renderCourse(); return; }
    const courseId = String(state.currentId);
    const position = activeTime();
    const audioWasPlaying = Boolean(hasAudio() && state.audio && !state.audio.paused);
    const virtualWasPlaying = state.virtualPlaying;
    if (audioWasPlaying) state.audio.pause();
    clearTimeout(state.saveTimer); endListeningTracking();
    if (virtualWasPlaying) { state.virtualPlaying = false; clearInterval(state.virtualTimer); }
    await state.listeningQueue;
    if (String(state.currentId) !== courseId) return;
    if (state.detail) await savePlayback(true);
    const detail = await request(`/courses/${encodeURIComponent(courseId)}`);
    if (String(state.currentId) !== courseId) return;
    state.detail = normalizeDetail(detail);
    setPreviewBaseline(state.detail.transcription_preview);
    state.detail.playback = { ...(state.detail.playback || {}), position_seconds: position };
    state.virtualTime = position;
    updateCourseSummary(state.detail);
    renderCourse();
    if (audioWasPlaying && state.audio) state.audio.play().catch(() => {});
    else if (virtualWasPlaying) togglePlayback();
  }
  async function refreshTranscriptionPreview(courseId, revision, expectedJobId = null) {
    const id = String(courseId);
    const routeToken = state.routeToken;
    if (String(state.currentId) !== id || String(state.detail?.id) !== id) return;
    const detail = await request(`/courses/${encodeURIComponent(id)}`);
    if (String(state.currentId) !== id || routeToken !== state.routeToken || String(state.detail?.id) !== id) return;
    if (expectedJobId && state.previewJobId && expectedJobId !== state.previewJobId) return;
    const preview = detail?.transcription_preview || null;
    if (expectedJobId && preview?.job_id != null && String(preview.job_id) !== expectedJobId) return;
    state.detail.transcription_preview = preview;
    if (preview?.job_id != null) state.previewJobId = String(preview.job_id);
    state.previewRevision = num(revision, num(preview?.revision, state.previewRevision));
    updateTranscriptionPreviewDom();
  }
  async function startProcessing(button = $("#retry-process")) {
    const courseId = String(state.currentId);
    if (isLocalId(courseId)) { toast("本機試用課程不會轉錄錄音，請建立正式課程。", "warning"); return; }
    if (Object.keys(state.assetUploading).some(key => key.startsWith(`${courseId}:`))) {
      toast("錄音仍在上傳，請稍後再重試。", "warning"); return;
    }
    if (!assetState().audio) { toast("請先上傳課堂錄音。", "warning"); return; }
    if (button) { button.disabled = true; button.innerHTML = '<span class="spinner"></span>啟動中'; }
    try {
      const result = await request(`/courses/${encodeURIComponent(courseId)}/process`, { method: "POST" });
      if (String(state.currentId) !== courseId) return;
      state.job = result?.job || result || { status: "queued", progress: 0, message: "工作已排入佇列" };
      updateStatusBanner(); pollJob(courseId);
    } catch (error) {
      if (String(state.currentId) === courseId) {
        toast(`無法重新處理：${error.message}`, "error");
        updateStatusBanner();
      }
    }
  }
  async function analyzeMatches() {
    const courseId = String(state.currentId);
    if (state.handoutBusy[courseId]) { toast("請先等候講義上傳完成。", "warning"); return; }
    if (isLocalId(courseId)) { toast("試用課程不會呼叫 OpenAI；請在正式課程手動確認出題時間。", "warning"); return; }
    const { counts } = questionView();
    if (!counts.unanalysed) {
      toast("目前沒有尚未分析的題目。", "warning"); return;
    }
    if (!state.detail?.segments?.length && !state.detail?.handouts?.length) {
      toast("請先整理逐字稿或上傳講義，再繼續分析題目。", "warning"); return;
    }
    const button = $("#analyze-matches");
    if (button) { button.disabled = true; button.textContent = "正在檢查 OpenAI 設定…"; }
    try {
      const settings = await request("/settings/openai");
      if (String(state.currentId) !== courseId) return;
      if (!settings?.configured) {
        syncQuestionAnalysisAction();
        void showOpenAISettings();
        return;
      }
      if (button) button.textContent = "正在啟動 OpenAI 分析…";
      const result = await request(`/courses/${encodeURIComponent(courseId)}/analyze-matches`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ max_questions: 20 })
      });
      if (String(state.currentId) !== courseId) return;
      state.job = result?.job || result;
      updateStatusBanner(); pollJob(courseId);
    } catch (error) {
      if (String(state.currentId) === courseId) {
        syncQuestionAnalysisAction();
        toast(`無法啟動 OpenAI 題目對位分析：${error.message}`, "error");
      }
    }
  }
  async function loadCourseBank() {
    const courseId=String(state.currentId), button=$("#load-course-bank");
    if(button)button.disabled=true;
    try {
      const result=await request(`/courses/${encodeURIComponent(courseId)}/sync-bank`,{method:"POST"});
      if(String(state.currentId)!==courseId)return;
      await refreshDetail();
      toast(`已載入 ${result.total} 題`);
    } catch(error) { toast(error.message,"error"); }
    finally { if(String(state.currentId)===courseId && button)button.disabled=false; }
  }
  async function rematchLocalText() {
    const courseId = String(state.currentId);
    if (isLocalId()) { toast("試用課程不會配對科目題庫。", "warning"); return; }
    if (!state.detail?.segments?.length) {
      toast("請先整理出逐字稿，再執行科目題庫配對。", "warning"); return;
    }
    const button = $("#rematch-local-text");
    if (button) { button.disabled = true; button.textContent = "正在配對題庫…"; }
    try {
      const result = await request(`/courses/${encodeURIComponent(courseId)}/rematch`, { method: "POST" });
      if (String(state.currentId) !== courseId) return;
      await refreshDetail();
      if (String(state.currentId) !== courseId) return;
      const matched = num(result?.matched, 0), unmatched = num(result?.unmatched, Math.max(0, state.detail.questions.length - matched));
      toast(`科目題庫配對完成：${matched} 題找到出題位置，${unmatched} 題待校對。`);
    } catch (error) {
      if (String(state.currentId) === courseId && button) { button.disabled = false; button.textContent = "⌕ 重新配對題庫"; }
      if (String(state.currentId) === courseId) toast(`題庫配對失敗：${error.message}`, "error");
    }
  }
  async function pollJob(courseId = state.currentId) {
    stopJobPolling();
    const id = String(courseId);
    const check = async () => {
      if (String(state.currentId) !== String(id)) return;
      try {
        const data = await request(`/courses/${encodeURIComponent(id)}/job`);
        if (String(state.currentId) !== id) return;
        state.job = data?.job || data;
        const status = String(state.job?.status || "").toLowerCase();
        updateStatusBanner();
        const jobKind = String(state.job?.kind || state.job?.type || "").toLowerCase();
        const previewRelevant = !jobKind.includes("match_analysis") && !jobKind.startsWith("medical_");
        if (previewRelevant) {
          const incomingJobId = state.job?.job_id ?? state.job?.id ?? data?.job_id ?? null;
          const normalizedJobId = incomingJobId == null ? null : String(incomingJobId);
          const currentPreviewId = state.detail?.transcription_preview?.job_id == null ? null : String(state.detail.transcription_preview.job_id);
          if (normalizedJobId && normalizedJobId !== state.previewJobId) {
            state.previewJobId = normalizedJobId;
            state.previewRevision = normalizedJobId === currentPreviewId ? num(state.detail?.transcription_preview?.revision) : 0;
            if (currentPreviewId && currentPreviewId !== normalizedJobId) {
              state.detail.transcription_preview = null;
              updateTranscriptionPreviewDom();
            }
          }
          const revision = num(data?.preview_revision ?? state.job?.preview_revision, state.previewRevision);
          const failed = ["failed", "failure", "error"].includes(status);
          if (revision !== state.previewRevision || failed) {
            try { await refreshTranscriptionPreview(id, revision, normalizedJobId); } catch { /* A later poll can retry the preview fetch. */ }
            if (String(state.currentId) !== id) return;
          }
        }
        if (["completed", "complete", "done", "succeeded", "success", "failed", "failure", "error"].includes(status)) {
          stopJobPolling();
          if (["completed", "complete", "done", "succeeded", "success"].includes(status)) {
            const isAnalysis = state.job?.kind === "match_analysis";
            const analysisSummary = state.job?.message;
            const isRangeTranscription = isRangeTranscriptionJob(state.job);
            await refreshDetail();
            if (String(state.currentId) !== id) return;
            toast(jobKind.startsWith("medical_") ? analysisSummary : isRangeTranscription ? "局部重新辨識完成；已更新選取範圍並保留其他逐字稿與作答紀錄。" : isAnalysis ? (analysisSummary || "OpenAI 分析結束，請查看配對結果。") : "課程處理完成，可以校對逐字稿與出題時間。");
          }
          else {
            toast(state.job?.error || "課程處理失敗，請檢查檔案後重試。", "error");
          }
          return;
        }
      } catch (error) { if (String(state.currentId) !== id) return; state.job = { status: "error", progress: 0, error: error.message }; updateStatusBanner(); updateTranscriptionPreviewDom(); stopJobPolling(); return; }
      state.jobTimer = setTimeout(check, 2200);
    };
    state.jobTimer = setTimeout(check, 500);
  }

  function openQuizQuestion(question) {
    state.quizQuestion = question; state.quizSelected = null; state.quizAnswer = null;
    renderQuiz();
  }
  function renderQuiz() {
    const question = state.quizQuestion; if (!question) { $("#modal-root").innerHTML = ""; return; }
    const options = optionList(question);
    const answer = state.quizAnswer;
    const root = $("#modal-root");
    root.innerHTML = `<div class="modal-backdrop" style="background:rgba(17,39,31,.58);backdrop-filter:blur(4px)"><section class="quiz-card" role="dialog" aria-modal="true" aria-label="課堂快問快答"><div class="quiz-body"><h2>${esc(question.stem || "（題目內容尚未辨識）")}</h2><div class="quiz-options">${options.map(option => {
      let klass = "";
      if (state.quizSelected === option.key) klass = "selected";
      if (answer && option.key === answer.correct_option) klass = "correct";
      else if (answer && state.quizSelected === option.key) klass = "incorrect";
      return `<button class="quiz-option ${klass}" data-quiz-option="${esc(option.key)}" ${answer ? "disabled" : ""}><span class="option-key">${esc(option.key)}</span><span>${esc(option.text)}</span></button>`;
    }).join("")}</div><footer class="quiz-foot"><button class="primary-button" id="quiz-submit" ${answer ? "" : state.quizSelected ? "" : "disabled"}>${answer ? "繼續播放" : "送出答案"} <span>→</span></button></footer></div></section></div>`;
    $$('[data-quiz-option]', root).forEach(button => button.addEventListener("click", () => { state.quizSelected = button.dataset.quizOption; renderQuiz(); }));
    $("#quiz-submit", root).addEventListener("click", submitQuiz);
  }
  async function submitQuiz() {
    if (state.quizAnswer) {
      if (state.quizIndex + 1 < state.quizQueue.length) {
        state.quizIndex += 1; openQuizQuestion(state.quizQueue[state.quizIndex]); return;
      }
      const resume = state.quizMode === "playback" || state.practiceShouldResume;
      state.practiceShouldResume = false;
      const lastPoint = num(matchFor(state.quizQuestion)?.question_time, activeTime());
      state.quizQuestion = null; state.quizAnswer = null; state.quizQueue = []; state.quizIndex = 0; $("#modal-root").innerHTML = "";
      if (resume) { state.lastTime = Math.max(state.lastTime, lastPoint); await togglePlayback(); }
      renderQuestionListOnly(); return;
    }
    if (!state.quizSelected) return;
    const question = state.quizQuestion;
    try {
      let result;
      if (isLocalId()) {
        const correct = String(question.correct_option ?? question.answer_key ?? "A").trim().toUpperCase();
        result = { correct: state.quizSelected === correct, correct_option: correct, explanation: question.explanation || "這是手動輸入的試用題目，目前沒有補充解析。" };
        state.detail.attempts.push({ question_id: question.id, selected_option: state.quizSelected, correct: result.correct, attempted_at: new Date().toISOString(), round_id: state.detail.learning?.round_id });
      } else {
        result = await request(`/questions/${encodeURIComponent(question.id)}/attempt`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ selected_option: state.quizSelected }) });
        state.detail.attempts.push({ question_id: question.id, selected_option: state.quizSelected, correct: result.correct, attempted_at: new Date().toISOString(), round_id: state.detail.learning?.round_id });
        question.correct_option = result.correct_option; question.explanation = result.explanation;
      }
      state.quizAnswer = { correct: Boolean(result.correct), correct_option: result.correct_option, explanation: result.explanation };
      state.skipIds.delete(String(question.id)); persistSkipped();
      persistLocalDetail(); renderQuiz(); renderQuestionListOnly();
    } catch (error) { toast(`答案送出失敗：${error.message}`, "error"); }
  }

  function showHelp(section = "all") {
    const body = `<div class="modal-body help-content"><p>課間依科目管理可重複使用的考古題題庫，並把課堂錄音轉成逐字稿與出題位置。</p><ul><li><b>科目與課程：</b>從側邊欄選擇病理學、藥理學、臨床醫學或檢驗醫學，課程清單會依科目篩選。建立課程時選擇所屬科目。</li><li><b>管理題庫：</b>在科目旁按「題庫」，建立具名題庫並上傳題目 PDF、答案解析 PDF。兩份檔案備妥後按「匯入題庫」；既有課程可批次重新配對該科目題庫。</li><li><b>整理錄音：</b>課程頁只需上傳錄音，系統會自動轉錄並依科目題庫整理考題。逐字稿可直接編輯；點段落時間可跳播。</li><li><b>播放作答：</b>播放遇到已確認的未作答題目時會暫停。拖曳或點逐字稿跳播不會累計學習時間；重新開始本輪會將目前進度歸零並保留歷史。</li><li><b>錯題複習：</b>從側邊欄開啟，可依科目或課程篩選未答對題目，作答後立即看到正解與解析。已答錯的題目保留在歷史紀錄中。</li><li><b>手動補充：</b>課程仍可手動新增逐字稿或題目；離開逐字稿欄位即會儲存。</li></ul></div>`;
    showModal("快速使用說明", "", body);
  }

  const semester = window.BLOCKADE_SEMESTER;
  let timetableWeek = 1;
  const timetableStorageKey = `blockade:heard:${semester.id}`;
  const timetableHeard = (() => { try { const ids=JSON.parse(localStorage.getItem(timetableStorageKey)||"[]"); return new Set(Array.isArray(ids)?ids:[]); } catch { return new Set(); } })();
  function highlightTimetableToday(now = new Date()) {
    const parts=new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Taipei",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(now);
    const today=["year","month","day"].map(kind=>parts.find(p=>p.type===kind).value).join("-");
    $$('.semester-timetable [data-day-date]').forEach(cell=>{
      const current=cell.dataset.dayDate===today;
      cell.classList.toggle("timetable-today",current);
      cell.classList.toggle("timetable-past",cell.dataset.dayDate<today);
      cell.classList.toggle("timetable-future",cell.dataset.dayDate>today);
      if(cell.tagName==="TH") {
        if(current)cell.setAttribute("aria-current","date");else cell.removeAttribute("aria-current");
      }
    });
  }
  function examCountdownHtml(now = new Date()) {
    const parts=new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Taipei",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(now);
    const part=kind=>parts.find(p=>p.type===kind).value;
    const today=Date.UTC(Number(part("year")),Number(part("month"))-1,Number(part("day")));
    return semester.weeks.flatMap(w=>w.days.flatMap(day=>day.lessons.filter(l=>/整合考試\d+/.test(l.title)).map(l=>{
      const [year,month,date]=day.date.split("-").map(Number);
      const days=Math.round((Date.UTC(year,month-1,date)-today)/86400000);
      return `<div class="exam-countdown ${days<0?'past':''}"><span>${esc(l.title.match(/整合考試\d+/)[0])}<small>${day.date.slice(5).replace("-","/")}</small></span><strong>${days<0?'已結束':days===0?'今天':`${days}<small>天</small>`}</strong></div>`;
    }))).join("");
  }
  setInterval(()=>{
    const panel=$(".exam-countdowns");if(panel)panel.innerHTML=examCountdownHtml();
    highlightTimetableToday();
  },60000);
  function renderTimetable() {
    state.page="timetable"; renderCourseList(); setHeader("課程時間表");
    const week=semester.weeks[timetableWeek-1], lessons=week.days.flatMap(day=>day.lessons);
    const times=[...new Set(lessons.map(lesson=>lesson.start))].sort();
    const dayLabel=day=>`週${["一","二","三","四","五","六","日"][day.weekday]}`;
    const card=lesson=>`<div class="timetable-lesson ${timetableHeard.has(lesson.id)?"heard":""} ${lesson.title.includes("考試")?"exam":""}"><button class="timetable-open" data-timetable-lesson="${lesson.id}" aria-label="開啟 ${esc(lesson.title)}"><strong>${esc(lesson.subject)}</strong><span class="lesson-title">${esc(lesson.title)}</span><span>${lesson.start}–${lesson.end}</span>${lesson.teacher?`<span>${esc(lesson.teacher)}</span>`:""}</button><label class="lesson-check"><input type="checkbox" data-heard-lesson="${lesson.id}" ${timetableHeard.has(lesson.id)?"checked":""} aria-label="${lesson.id} ${esc(lesson.title)} 已聽"/>已聽</label></div>`;
    $("#app-content").innerHTML=`<div class="minimal-heading"><div class="minimal-title"><h1>課程時間表</h1></div></div>
      <nav class="timetable-weeks" aria-label="十六週課表">${semester.weeks.map(w=>`<button data-timetable-week="${w.number}" class="${timetableWeek===w.number?"active":""}" aria-label="第 ${w.number} 週" aria-pressed="${timetableWeek===w.number}">${w.number}</button>`).join("")}</nav>
      <div class="timetable-toolbar"><h2>第 ${timetableWeek} 週</h2></div>
      <div class="timetable-with-exams"><div class="timetable-scroll"><table class="semester-timetable"><thead><tr><th scope="col">時間</th>${week.days.map(day=>`<th scope="col" data-day-date="${day.date}">${dayLabel(day)}<small>${day.date.slice(5).replace("-","/")}</small></th>`).join("")}</tr></thead><tbody>${week.days.some(day=>day.note)?`<tr class="timetable-notes"><th scope="row">備註</th>${week.days.map(day=>`<td data-day-date="${day.date}">${esc(day.note||"")}</td>`).join("")}</tr>`:""}${times.map(time=>`<tr><th scope="row">${time}</th>${week.days.map(day=>{
        const visible=day.lessons;
        if(visible.some(l=>l.start<time&&l.end>time))return "";
        const lesson=visible.find(l=>l.start===time);
        if(!lesson)return `<td data-day-date="${day.date}"></td>`;
        const span=times.filter(t=>t>=lesson.start&&t<lesson.end).length;
        return `<td rowspan="${span}" data-day-date="${day.date}">${card(lesson)}</td>`;
      }).join("")}</tr>`).join("")}</tbody></table></div><aside class="exam-countdowns" aria-label="整合考試倒數">${examCountdownHtml()}</aside></div>`;
    highlightTimetableToday();
    $$('[data-timetable-lesson]').forEach(button=>button.onclick=()=>void openTimetableCourse(lessons.find(l=>l.id===button.dataset.timetableLesson)));
    $$('[data-timetable-week]').forEach(button=>button.onclick=()=>{timetableWeek=Number(button.dataset.timetableWeek);renderTimetable();});
    $$('[data-heard-lesson]').forEach(input=>input.onchange=()=>{
      const next=new Set(timetableHeard); if(input.checked)next.add(input.dataset.heardLesson);else next.delete(input.dataset.heardLesson);
      try {localStorage.setItem(timetableStorageKey,JSON.stringify([...next]));timetableHeard.clear();next.forEach(id=>timetableHeard.add(id));}
      catch {toast("無法儲存勾選，請確認瀏覽器儲存空間。","error");}
      renderTimetable();
    });
  }

  async function openTimetableCourse(lesson) {
    try {
      await loadCourses();
      const key=`${semester.id}:${lesson.id}`;
      const title=`${lesson.id.slice(0,10)} ${lesson.start} ${lesson.title}`;
      const course=state.courses.find(c=>c.timetable_key===key)||state.courses.find(c=>c.title===title);
      if(course)await openCourse(course.id);else showTimetableImport(lesson);
    }catch(error){toast(error.message,"error");}
  }

  function showTimetableImport(lesson) {
    const root=showModal(lesson.title,"",`<form class="modal-body" id="timetable-import"><div class="minimal-bank-uploads"><label class="minimal-bank-upload">錄音<input id="lesson-audio" type="file" accept="audio/*,.m4a,.wav,.mp3" multiple /></label><label class="minimal-bank-upload">投影片 PDF<input id="lesson-slides" type="file" accept=".pdf,application/pdf" multiple /></label></div><p class="form-error" id="lesson-error" hidden></p><div class="modal-foot"><button type="submit" class="primary-button">匯入</button></div></form>`);
    $("#timetable-import",root).onsubmit=async event=>{
      event.preventDefault();
      const audio=[...$("#lesson-audio",root).files],slides=[...$("#lesson-slides",root).files];
      if(!audio.length&&!slides.length)return;
      const submit=$("button[type=submit]",root);submit.disabled=true;
      try {
        const key=`blockade:lesson:${semester.id}:${lesson.id}`;
        const title=`${lesson.id.slice(0,10)} ${lesson.start} ${lesson.title}`;
        const data=await request("/courses");
        let savedId;try{savedId=localStorage.getItem(key);}catch{}
        let course=data.courses.find(c=>c.timetable_key===`${semester.id}:${lesson.id}`)||data.courses.find(c=>String(c.id)===savedId)||data.courses.find(c=>c.title===title);
        if(!course){
          const subject=/藥理/.test(lesson.subject)?"pharmacology":/病理/.test(lesson.subject)?"pathology":/檢驗/.test(lesson.subject)?"laboratory":"clinical";
          const result=await request("/courses",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title,subject_id:subject,transcription_chunk_minutes:5,timetable_key:`${semester.id}:${lesson.id}`})});course=result.course||result;
        }
        try{localStorage.setItem(key,course.id);}catch{}
        await loadCourses();await openCourse(course.id);
        root.innerHTML="";
        if(slides.length){
          $('[data-course-tab="handouts"]')?.click();
          await uploadHandouts(slides);
          if(state.handoutErrors[String(course.id)]){if(audio.length)addFilesToAudioQueue(audio);return;}
        }
        if(audio.length){
          $('.course-more').open=true;
          addFilesToAudioQueue(audio);await uploadAudioQueue();
        }
      }catch(error){
        if($("#lesson-error",root)){$("#lesson-error",root).hidden=false;$("#lesson-error",root).textContent=error.message;submit.disabled=false;}
        else toast(error.message,"error");
      }
    };
  }

  let pblMatches = window.BLOCKADE_PBL_MATCHES || {}, pblMatching = false, pblMatchError = "";
  try {pblMatches=JSON.parse(localStorage.getItem("blockade:pbl-matches:v1")||JSON.stringify(pblMatches));} catch {}
  let pblSavedLoaded = false;
  async function loadPBLAPIResults() {
    if(pblSavedLoaded)return;
    pblSavedLoaded=true;
    try {
      const data=await request("/pbl/matches");
      for(const [week,result] of Object.entries(data.weeks||{}))pblMatches[week]=result.matches;
      if(state.page==="pbl")renderPBL();
    } catch(error) {pblSavedLoaded=false;toast(error.message,"error");}
  }
  async function matchPBLAPI(week) {
    if(!week||pblMatching)return;
    pblMatching=true;pblMatchError="";renderPBL();
    try {
      const data=await request("/pbl/matches",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({week:week.number,subjects:[...week.subjects]})});
      pblMatches[week.number]=data.matches;
      toast(`第 ${week.number} 週：${data.matches.length} 堂相關課程`);
    }catch(error){pblMatchError=error.message;}
    finally{pblMatching=false;if(state.page==="pbl")renderPBL();}
  }
  async function matchPBL() {
    if(pblMatching)return;
    pblMatching=true;pblMatchError="";renderPBL();
    try {
      const response=await fetch("/pbl-materials/search.json");if(!response.ok)throw new Error("教案讀取失敗");
      const texts=await response.json();
      const data=await request("/courses");const courses=[];
      for(const course of data.courses){
        const detail=normalizeDetail(await request(`/courses/${encodeURIComponent(course.id)}`));
        courses.push({...course,segments:detail.segments});
      }
      const results={};for(const w of window.BLOCKADE_PBL.weeks)results[w.number]=window.BlockAdePBLMatch.match(texts[w.number],courses);
      pblMatches=results;
      try{localStorage.setItem("blockade:pbl-matches:v1",JSON.stringify(results));}catch{}
    }catch(error){pblMatchError=error.message;}
    finally {pblMatching=false;if(state.page==="pbl")renderPBL();}
  }
  function pblMatchHtml(week) {
    if(pblMatching)return '<div class="pbl-match-empty" role="status">配對中…</div>';
    if(pblMatchError)return `<p class="form-error" role="alert">${esc(pblMatchError)}</p>`;
    const rows=(pblMatches[week.number]||[]).filter(r=>week.subjects.includes(r.subject_id));
    return rows.length?`<div class="pbl-matches">${SUBJECTS.filter(s=>week.subjects.includes(s.id)).map(s=>{
      const matches=rows.filter(r=>r.subject_id===s.id);if(!matches.length)return "";
      return `<section><h3>${s.label}</h3>${matches.map(r=>`<button class="pbl-matched-course" data-pbl-course="${esc(r.id)}"><strong>${esc(r.title)}</strong><small>${r.evidence === "逐字稿" ? "逐字稿 · " : ""}${esc(r.topics.join("、"))}</small>${r.excerpts.map(e=>`<span>${clock(e.start)}　${esc(e.text)}</span>`).join("")}</button>`).join("")}</section>`;
    }).join("")}</div>`:'<div class="pbl-match-empty">尚無相關內容</div>';
  }
  const pblDrafts = window.BLOCKADE_PBL.weeks.map(week => ({...week, files:[], subjects:SUBJECTS.map(s => s.id)}));
  let pblWeek = null;
  function renderPBL() {
    void loadPBLAPIResults();
    state.page = "pbl"; renderCourseList(); setHeader("PBL");
    const week = pblWeek === null ? null : pblDrafts[pblWeek];
    $("#app-content").innerHTML = `<div class="minimal-heading"><div class="minimal-title"><h1>${week ? `<button class="course-back" id="pbl-back" aria-label="返回十六週">‹</button>第 ${pblWeek+1} 週` : "PBL"}</h1></div><button class="secondary-button" id="pbl-api">API 設定</button></div>${week ? `
      <section class="pbl-document"><div class="minimal-section-heading"><div><h2>${esc(week.title)}</h2><time>${esc(week.date)}</time></div><a class="secondary-button" href="${window.BLOCKADE_PBL.url}#page=${week.pages[0] || 7}" target="_blank" rel="noopener">原檔</a></div><div class="pbl-reader">${week.pages.map(page=>`<a href="/pbl-materials/page-${page}.png" target="_blank" rel="noopener" aria-label="放大教案第 ${page} 頁"><img src="/pbl-materials/page-${page}.png" alt="${esc(week.title)}，原檔第 ${page} 頁" loading="lazy" width="1072" height="1516" /></a>`).join("")}</div></section>
      <section class="pbl-learning"><div class="minimal-section-heading"><h2>跨科目學習</h2><button class="secondary-button" id="pbl-match" ${pblMatching?"disabled":""}>本機配對</button><button class="primary-button" id="pbl-api-match" ${pblMatching?"disabled":""}>API 配對</button></div><div class="pbl-subjects">${SUBJECTS.map(subject=>`<label class="pbl-subject"><input type="checkbox" data-pbl-subject="${subject.id}" ${week.subjects.includes(subject.id)?"checked":""}/>${subject.label}</label>`).join("")}</div>${pblMatchHtml(week)}</section>` : `<div class="pbl-week-grid">${pblDrafts.map((draft,i)=>`<button class="pbl-week" data-week="${i}"><span class="week-number">第 ${i+1} 週</span><span class="week-status">${esc(draft.title)}</span><time class="pbl-week-date">${draft.date.slice(5).replace("-","/")}</time></button>`).join("")}</div>`}`;
    $("#pbl-api").onclick = () => void showOpenAISettings();
    $$('[data-week]').forEach(button=>button.onclick=()=>{pblWeek=Number(button.dataset.week);renderPBL();});
    $("#pbl-back")?.addEventListener("click",()=>{pblWeek=null;renderPBL();});
    $("#pbl-upload")?.addEventListener("click",()=>$("#pbl-file").click());
    $("#pbl-file")?.addEventListener("change",event=>{week.files.push(...[...event.target.files].map(file=>file.name));renderPBL();});
    $$('[data-remove-pbl]').forEach(button=>button.onclick=()=>{week.files.splice(Number(button.dataset.removePbl),1);renderPBL();});
    $$('[data-pbl-subject]').forEach(input=>input.onchange=()=>{week.subjects=$$('[data-pbl-subject]:checked').map(item=>item.dataset.pblSubject);renderPBL();});
    $("#pbl-api-match")?.addEventListener("click",()=>void matchPBLAPI(week));
    $("#pbl-match")?.addEventListener("click",()=>void matchPBL());
    $$('[data-pbl-course]').forEach(button=>button.onclick=()=>void openCourse(button.dataset.pblCourse));
  }

  function simplifyCourseUI() {
    const content = $("#app-content"); content.classList.add("minimal-course");
    const title = $(".course-heading-title",content);
    if (title) { const edit=$("#edit-course-title"); edit.textContent="改名"; }
    const heading=$(".course-page-heading",content);
    const more=document.createElement("details"); more.className="course-more";
    more.innerHTML='<summary>更多</summary><div class="course-more-content"></div>';
    const moreBody=$(".course-more-content",more);
    [".heading-actions",".transcription-settings",".audio-assets",".upload-strip",".learning-panel",".review-guide"].forEach(selector=>{const node=$(selector,content);if(node)moreBody.append(node);});
    heading.after(more);
    const workspace=$(".workspace-grid",content);
    const transcript=$(".transcript-panel",content), handouts=$("#handouts-panel",content), questions=$(".questions-panel",content);
    [transcript,handouts,questions].forEach(panel=>{if(panel){panel.hidden=false;workspace.append(panel);}});
    $(".transcript-panel h2",content).textContent="逐字稿";
    $(".handouts-panel h2",content).textContent="講義";
    $(".questions-panel h2",content).textContent="題目";
    const tools=$(".transcript-panel .panel-tools",content);
    const extra=document.createElement("details");extra.className="transcript-more";extra.innerHTML='<summary aria-label="更多逐字稿操作">⋯</summary>';
    ["#follow-toggle","#transcription-review","#add-segment","#transcript-fullscreen"].forEach(selector=>{const node=$(selector,content);if(node)extra.append(node);});tools.append(extra);
    $("#medical-review").textContent="校訂";$("#download-transcript").textContent="匯出";
    const upload=document.createElement("button");upload.className="tiny-button";upload.textContent="上傳錄音";
    upload.onclick=()=>{more.open=true;$("[data-choose-asset='audio']",content)?.click();};tools.prepend(upload);
  }
  function simplifyMedicalUI(root) {
    root.classList.add("minimal-medical");
    $(".modal-head h2",root).textContent="校訂";
    const subtitle=$(".modal-head p",root); if(subtitle)subtitle.textContent="";
    const controls=$(".medical-controls",root);
    const details=document.createElement("details");details.className="medical-extra";details.innerHTML='<summary>設定與紀錄</summary>';
    const context=$("#medical-context",root).closest("label");details.append(context);
    [...controls.children].filter(node=>node.tagName==="P"||node.tagName==="DETAILS").forEach(node=>details.append(node));
    ["#medical-align","#medical-export","#medical-refresh"].forEach(selector=>details.append($(selector,root)));
    $("#medical-align",details).textContent="重新對齊";
    $("#medical-export",details).textContent="匯出紀錄";
    const filter=$("#medical-history-filter",root).closest("label");details.append(filter);controls.after(details);
    $("#medical-suggest",root).textContent="產生建議";
    const cards=$$(".transcription-review-card",root);
    cards.forEach(card=>{
      const heading=$("strong",card); heading.textContent=heading.textContent.includes("推定")?"推定":"紀錄";
      const candidate=card.children[2]; if(candidate?.tagName === "P") candidate.textContent=candidate.textContent.replace("候選／校訂文字：", "建議：");
      const info=document.createElement("details");info.innerHTML='<summary>詳細資料</summary>';
      [...card.children].filter((node,index)=>index>2&&!node.classList.contains("modal-foot")).forEach(node=>info.append(node));
      card.insertBefore(info,$(".modal-foot",card));
      const apply=$("[data-medical-apply]",card);if(apply)apply.textContent="採用";
    });
    const counter=$("h3",$(".medical-review-body",root));let position=0;
    if(cards.length){const pager=document.createElement("div");pager.className="medical-pager";pager.innerHTML='<button class="tiny-button" aria-label="上一項">‹</button><span></span><button class="tiny-button" aria-label="下一項">›</button>';counter.replaceWith(pager);const buttons=$$("button",pager);const paint=()=>{cards.forEach((card,i)=>card.hidden=i!==position);$("span",pager).textContent=`${position+1} / ${cards.length}`;buttons[0].disabled=position===0;buttons[1].disabled=position===cards.length-1;};buttons[0].onclick=()=>{position--;paint();};buttons[1].onclick=()=>{position++;paint();};paint();}else{counter.textContent="尚無建議";counter.nextElementSibling?.remove();}
  }
  $$('[data-main-page]').forEach(button=>button.addEventListener("click",async()=>{
    const page=button.dataset.mainPage;
    await goOverview(); $("#app-content").classList.remove("minimal-course");
    state.page=page; renderCourseList();
    if(page==="timetable")renderTimetable();
    if(page==="pbl"){pblWeek=null;renderPBL();}
    if(page==="banks")openBankManager(state.subjectFilter||"pathology");
    if(page==="wrong")void openWrongReview();
  }));
  $(".brand").addEventListener("click",event=>{event.preventDefault();void goOverview();});

  $("#new-course-button")?.addEventListener("click", showCreateCourse);
  $("#new-course-shortcut")?.addEventListener("click", showCreateCourse);
  $("#help-button")?.addEventListener("click", () => showHelp());
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      const modalIsOpen = Boolean($("#modal-root")?.firstElementChild);
      if (state.transcriptReadingMode && !modalIsOpen) void exitTranscriptReadingMode();
      if (!state.quizQuestion) $("#modal-root").innerHTML = "";
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#transcript-search")?.focus(); }
  });
  window.addEventListener("beforeunload", () => {
    if (state.audio) trackListening(state.audio.currentTime, true);
    endListeningTracking();
    if (state.detail) savePlayback(true);
  });

  loadCourses().then(() => renderOverview());
})();
