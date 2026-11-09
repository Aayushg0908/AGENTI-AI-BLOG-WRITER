const runForm = document.getElementById("runForm");
const topicInput = document.getElementById("topicInput");
const audienceInput = document.getElementById("audienceInput");
const toneInput = document.getElementById("toneInput");
const lengthInput = document.getElementById("lengthInput");
const approvalInput = document.getElementById("approvalInput");
const runButton = document.getElementById("runButton");
const cancelButton = document.getElementById("cancelButton");
const newRunButton = document.getElementById("newRunButton");

const timeline = document.getElementById("timeline");
const activityEmpty = document.getElementById("activityEmpty");

const planList = document.getElementById("planList");
const planEmpty = document.getElementById("planEmpty");
const planMeta = document.getElementById("planMeta");
const taskCount = document.getElementById("taskCount");
const planReview = document.getElementById("planReview");
const planFeedback = document.getElementById("planFeedback");
const approveButton = document.getElementById("approveButton");
const regenerateButton = document.getElementById("regenerateButton");

const progressRing = document.getElementById("progressRing");
const progressText = document.getElementById("progressText");
const runBadge = document.getElementById("runBadge");

const articlePreview = document.getElementById("articlePreview");
const markdownOutput = document.getElementById("markdownOutput");
const previewTab = document.getElementById("previewTab");
const markdownTab = document.getElementById("markdownTab");
const copyButton = document.getElementById("copyButton");
const downloadButton = document.getElementById("downloadButton");

const seoCard = document.getElementById("seoCard");
const seoFields = document.getElementById("seoFields");

const healthDot = document.getElementById("healthDot");
const healthText = document.getElementById("healthText");
const toast = document.getElementById("toast");

let abortController = null;
let finalMarkdown = "";
let totalTasks = 0;
let completedTasks = 0;
let currentRunId = null;
let currentPlan = null;
let awaitingApproval = false;

const stageProgress = {
    router: 8,
    research: 16,
    orchestrator: 26,
    human_review: 30,
    workers: 62,
    editor: 75,
    assemble: 82,
    diagrams: 90,
    seo: 96,
    finalize: 100,
};


if (window.mermaid) {
    window.mermaid.initialize({
        startOnLoad: false,
        theme: "dark",
        securityLevel: "strict",
        suppressErrorRendering: true,
    });
}


function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


function showToast(message) {
    toast.textContent = message;
    toast.classList.add("show");

    window.setTimeout(() => {
        toast.classList.remove("show");
    }, 2200);
}


function updateProgress(value) {
    const progress = Math.max(0, Math.min(100, Math.round(value)));

    progressRing.style.setProperty("--progress", progress);
    progressText.textContent = `${progress}%`;
}


function emptyArticleState() {
    return `
        <div class="empty-state">
            <div class="empty-icon">✎</div>
            <h3>Your article will appear here</h3>
            <p>The final result appears after every section is written, reviewed and verified.</p>
        </div>
    `;
}


function resetInterface() {
    finalMarkdown = "";
    totalTasks = 0;
    completedTasks = 0;
    currentRunId = null;
    currentPlan = null;
    awaitingApproval = false;

    timeline.innerHTML = "";
    planList.innerHTML = "";

    activityEmpty.hidden = false;
    planEmpty.hidden = false;

    timeline.appendChild(activityEmpty);
    planList.appendChild(planEmpty);

    planMeta.innerHTML = "";
    planMeta.hidden = true;
    planReview.hidden = true;
    planFeedback.value = "";

    taskCount.textContent = "0 tasks";

    articlePreview.innerHTML = emptyArticleState();
    markdownOutput.textContent = "";
    markdownOutput.hidden = true;
    articlePreview.hidden = false;

    previewTab.classList.add("active");
    markdownTab.classList.remove("active");

    copyButton.disabled = true;
    downloadButton.href = "#";
    downloadButton.classList.add("disabled");
    downloadButton.setAttribute("aria-disabled", "true");

    seoCard.hidden = true;
    seoFields.innerHTML = "";

    runBadge.textContent = "Ready";
    runBadge.className = "run-badge";

    updateProgress(0);
}


function setRunningState(isRunning) {
    [topicInput, audienceInput, toneInput, lengthInput, approvalInput].forEach(input => {
        input.disabled = isRunning || awaitingApproval;
    });

    runButton.disabled = isRunning || awaitingApproval;
    approveButton.disabled = isRunning;
    regenerateButton.disabled = isRunning;
    cancelButton.hidden = !isRunning;

    if (isRunning) {
        runBadge.textContent = "Running";
        runBadge.className = "run-badge running";
    }
}


function getStageElement(stageId) {
    return timeline.querySelector(`[data-stage-id="${stageId}"]`);
}


function createStageElement(stageId) {
    const element = document.createElement("div");

    element.className = "timeline-item";
    element.dataset.stageId = stageId;
    element.innerHTML = `
        <div class="timeline-marker"><span></span></div>
        <div class="timeline-content">
            <div class="timeline-title"></div>
            <div class="timeline-detail"></div>
        </div>
    `;

    timeline.appendChild(element);
    return element;
}


function updateStage(event) {
    activityEmpty.hidden = true;

    const element = getStageElement(event.id) || createStageElement(event.id);

    element.classList.remove("running", "completed", "failed", "waiting");
    element.classList.add(event.status || "running");

    element.querySelector(".timeline-title").textContent = event.label;
    element.querySelector(".timeline-detail").textContent = event.detail || "";

    if (
        (event.status === "completed" || event.status === "waiting")
        && stageProgress[event.id] !== undefined
    ) {
        updateProgress(stageProgress[event.id]);
    }
}


function appendToStage(stageId, node) {
    const stage = getStageElement(stageId);

    if (stage) {
        stage.querySelector(".timeline-content").appendChild(node);
    }
}


function showRouting(event) {
    const modeLabels = {
        closed_book: "Evergreen topic",
        hybrid: "Research-assisted topic",
        open_book: "Current information topic",
    };

    const element = getStageElement("router");

    if (element) {
        const recency = event.needs_research && event.recency_days
            ? ` Window: last ${event.recency_days} days.`
            : "";

        element.querySelector(".timeline-detail").textContent =
            `${modeLabels[event.mode] || event.mode}. Research: ${
                event.needs_research ? "required" : "not required"
            }.${recency}`;
    }

    if (Array.isArray(event.queries) && event.queries.length) {
        const queriesElement = document.createElement("div");

        queriesElement.className = "query-list";
        queriesElement.innerHTML = event.queries
            .map(query => `<span>${escapeHtml(query)}</span>`)
            .join("");

        appendToStage("router", queriesElement);
    }
}


function showResearch(event) {
    if (!event.evidence?.length) {
        return;
    }

    const sources = document.createElement("div");

    sources.className = "source-list";
    sources.innerHTML = event.evidence
        .slice(0, 5)
        .map(source => `
            <a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">
                ${escapeHtml(source.title || source.url)}
                ${source.published_at ? `<small>${escapeHtml(source.published_at)}</small>` : ""}
            </a>
        `)
        .join("");

    appendToStage("research", sources);
}


function showReview(event) {
    const review = event.review || {};
    const flagged = (review.sections || []).filter(item => item.verdict === "revise");

    const box = document.createElement("div");
    box.className = "review-list";

    box.innerHTML = `
        <p class="review-overall">
            <strong>Round ${escapeHtml(review.round || 1)}:</strong>
            ${escapeHtml(review.overall || "")}
        </p>
        ${flagged.map(item => `
            <details>
                <summary>${escapeHtml(item.title)} <span>${item.issues.length} note(s)</span></summary>
                <ul>${item.issues.map(issue => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>
            </details>
        `).join("")}
    `;

    appendToStage("editor", box);

    flagged.forEach(item => {
        if (!event.will_revise) {
            return;
        }

        const taskElement = planList.querySelector(`[data-task-id="${item.task_id}"]`);

        if (taskElement) {
            taskElement.classList.add("revising");
            taskElement.querySelector(".plan-status").textContent = "Revising";
        }
    });
}


function markSectionRevised(event) {
    const taskElement = planList.querySelector(`[data-task-id="${event.task_id}"]`);

    if (taskElement) {
        taskElement.classList.remove("revising");
        taskElement.querySelector(".plan-status").textContent = "Revised";
    }
}


function showCitations(event) {
    const removed = event.report?.removed_urls || [];

    if (!removed.length) {
        return;
    }

    const box = document.createElement("details");
    box.className = "review-list";
    box.innerHTML = `
        <summary>Removed links <span>${removed.length}</span></summary>
        <ul>${removed.map(url => `<li>${escapeHtml(url)}</li>`).join("")}</ul>
    `;

    appendToStage("assemble", box);
}


function renderPlanMeta(plan) {
    planMeta.hidden = false;
    planMeta.innerHTML = `
        <div><span>Audience</span><strong>${escapeHtml(plan.audience || "Developers")}</strong></div>
        <div><span>Format</span><strong>${escapeHtml((plan.blog_kind || "explainer").replaceAll("_", " "))}</strong></div>
        <div><span>Tone</span><strong>${escapeHtml(plan.tone || "Technical")}</strong></div>
    `;
}


function taskTags(task) {
    const tags = [];

    if (task.requires_research) tags.push("Research");
    if (task.requires_citations) tags.push("Citations");
    if (task.requires_code) tags.push("Code");

    return tags;
}


function showPlan(plan) {
    currentPlan = plan;
    planEmpty.hidden = true;
    planList.innerHTML = "";

    const tasks = plan.tasks || [];

    totalTasks = tasks.length;
    completedTasks = 0;
    taskCount.textContent = `${tasks.length} ${tasks.length === 1 ? "task" : "tasks"}`;

    renderPlanMeta(plan);

    const titleElement = document.createElement("h3");
    titleElement.className = "plan-blog-title";
    titleElement.textContent = plan.blog_title || "";
    planList.appendChild(titleElement);

    tasks.forEach((task, index) => {
        const item = document.createElement("article");

        item.className = "plan-item";
        item.dataset.taskId = String(task.id);
        item.innerHTML = `
            <div class="plan-number">${String(index + 1).padStart(2, "0")}</div>
            <div class="plan-content">
                <div class="plan-title-row">
                    <h3>${escapeHtml(task.title)}</h3>
                    <span class="plan-status">Waiting</span>
                </div>
                <p>${escapeHtml(task.goal)}</p>
                <div class="plan-tags">
                    <span>${escapeHtml(task.target_words)} words</span>
                    ${taskTags(task).map(tag => `<span>${escapeHtml(tag)}</span>`).join("")}
                </div>
                <div class="task-progress"><span></span></div>
            </div>
        `;

        planList.appendChild(item);
    });
}


function editableTaskHtml(task, index) {
    return `
        <div class="plan-number">${String(index + 1).padStart(2, "0")}</div>
        <div class="plan-content">
            <input class="edit-input edit-title" type="text" maxlength="200"
                value="${escapeHtml(task.title)}" aria-label="Section title">
            <textarea class="edit-input edit-goal" rows="2" maxlength="500"
                aria-label="Section goal">${escapeHtml(task.goal)}</textarea>
            <textarea class="edit-input edit-bullets" rows="4"
                aria-label="Bullets, one per line">${escapeHtml((task.bullets || []).join("\n"))}</textarea>
            <div class="edit-row">
                <label>Words
                    <input class="edit-input edit-words" type="number" min="80" max="800" step="10"
                        value="${escapeHtml(task.target_words || 300)}">
                </label>
                <label class="edit-check">
                    <input class="edit-code" type="checkbox" ${task.requires_code ? "checked" : ""}>
                    Code
                </label>
                <div class="edit-buttons">
                    <button type="button" class="icon-button" data-move="-1" title="Move up">↑</button>
                    <button type="button" class="icon-button" data-move="1" title="Move down">↓</button>
                    <button type="button" class="icon-button danger" data-remove title="Remove section">✕</button>
                </div>
            </div>
        </div>
    `;
}


function renumberEditablePlan() {
    const items = planList.querySelectorAll(".plan-item.editable");

    items.forEach((item, index) => {
        item.querySelector(".plan-number").textContent = String(index + 1).padStart(2, "0");
    });

    taskCount.textContent = `${items.length} ${items.length === 1 ? "task" : "tasks"}`;
}


function addEditableTask(task) {
    const item = document.createElement("article");

    item.className = "plan-item editable";
    item.taskData = task;
    item.innerHTML = editableTaskHtml(task, 0);

    planList.insertBefore(item, planList.querySelector(".add-section-button"));
    renumberEditablePlan();
}


function showEditablePlan(plan) {
    currentPlan = plan;
    awaitingApproval = true;

    planEmpty.hidden = true;
    planList.innerHTML = "";
    renderPlanMeta(plan);

    const titleInput = document.createElement("input");
    titleInput.className = "edit-input edit-blog-title";
    titleInput.type = "text";
    titleInput.maxLength = 200;
    titleInput.value = plan.blog_title || "";
    titleInput.setAttribute("aria-label", "Blog title");
    planList.appendChild(titleInput);

    const addButton = document.createElement("button");
    addButton.type = "button";
    addButton.className = "secondary-button add-section-button";
    addButton.textContent = "+ Add section";
    addButton.addEventListener("click", () => {
        addEditableTask({
            title: "New section",
            goal: "What the reader should take away from this section.",
            bullets: ["First point", "Second point", "Third point"],
            target_words: 250,
            tags: [],
            requires_research: false,
            requires_citations: false,
            requires_code: false,
        });
    });
    planList.appendChild(addButton);

    (plan.tasks || []).forEach(task => addEditableTask(task));

    planReview.hidden = false;
    runBadge.textContent = "Awaiting approval";
    runBadge.className = "run-badge waiting";
}


planList.addEventListener("click", event => {
    const button = event.target.closest("button");
    const item = event.target.closest(".plan-item.editable");

    if (!button || !item) {
        return;
    }

    if (button.hasAttribute("data-remove")) {
        if (planList.querySelectorAll(".plan-item.editable").length <= 1) {
            showToast("The outline needs at least one section.");
            return;
        }
        item.remove();
    } else if (button.dataset.move === "-1") {
        const previous = item.previousElementSibling;
        if (previous?.classList.contains("editable")) {
            planList.insertBefore(item, previous);
        }
    } else if (button.dataset.move === "1") {
        const next = item.nextElementSibling;
        if (next?.classList.contains("editable")) {
            planList.insertBefore(next, item);
        }
    }

    renumberEditablePlan();
});


function collectEditedPlan() {
    const tasks = [...planList.querySelectorAll(".plan-item.editable")].map((item, index) => {
        const original = item.taskData || {};
        const bullets = item.querySelector(".edit-bullets").value
            .split("\n")
            .map(line => line.replace(/^\s*[-*•]\s*/, "").trim())
            .filter(Boolean);

        return {
            ...original,
            id: index + 1,
            title: item.querySelector(".edit-title").value.trim(),
            goal: item.querySelector(".edit-goal").value.trim(),
            bullets,
            target_words: Number(item.querySelector(".edit-words").value) || 300,
            requires_code: item.querySelector(".edit-code").checked,
        };
    });

    const problems = [];

    tasks.forEach((task, index) => {
        if (!task.title) problems.push(`Section ${index + 1} needs a title.`);
        if (!task.goal) problems.push(`Section ${index + 1} needs a goal.`);
        if (!task.bullets.length) problems.push(`Section ${index + 1} needs at least one bullet.`);
        if (task.bullets.length > 8) problems.push(`Section ${index + 1} has more than 8 bullets.`);
    });

    const blogTitle = planList.querySelector(".edit-blog-title")?.value.trim();

    if (!blogTitle) {
        problems.push("The blog needs a title.");
    }

    if (problems.length) {
        throw new Error(problems[0]);
    }

    return {
        ...currentPlan,
        blog_title: blogTitle,
        tasks,
    };
}


function completeSection(event) {
    const taskElement = planList.querySelector(`[data-task-id="${event.task_id}"]`);

    if (taskElement) {
        taskElement.classList.add("completed");
        taskElement.querySelector(".plan-status").textContent = "Written";
        taskElement.querySelector(".task-progress span").style.width = "100%";
    }

    completedTasks = event.completed || completedTasks + 1;
    totalTasks = event.total || totalTasks;

    updateProgress(totalTasks ? 30 + (completedTasks / totalTasks) * 32 : 30);

    const workerStage = getStageElement("workers");

    if (workerStage) {
        workerStage.querySelector(".timeline-detail").textContent =
            `${completedTasks} of ${totalTasks} sections written.`;
    }
}


async function renderMermaid(container) {
    if (!window.mermaid) {
        return;
    }

    const blocks = container.querySelectorAll("pre > code.language-mermaid");
    let index = 0;

    for (const code of blocks) {
        const pre = code.parentElement;

        try {
            const { svg } = await window.mermaid.render(
                `mermaid-${Date.now()}-${index++}`,
                code.textContent,
            );
            const figure = document.createElement("div");

            figure.className = "mermaid-diagram";
            figure.innerHTML = svg;
            pre.replaceWith(figure);
        } catch (error) {
            pre.classList.add("mermaid-error");
            pre.title = "This diagram could not be rendered; showing the Mermaid source.";
            console.warn("Mermaid render failed:", error);
        }
    }
}


async function displayFinalResult(event) {
    finalMarkdown = event.markdown || "";
    markdownOutput.textContent = finalMarkdown;
    articlePreview.innerHTML = DOMPurify.sanitize(marked.parse(finalMarkdown));

    copyButton.disabled = false;
    downloadButton.href = event.download_url;
    downloadButton.classList.remove("disabled");
    downloadButton.setAttribute("aria-disabled", "false");

    runBadge.textContent = "Completed";
    runBadge.className = "run-badge completed";
    updateProgress(100);

    document.getElementById("resultCard").scrollIntoView({ behavior: "smooth", block: "start" });

    await renderMermaid(articlePreview);
}


function showSeo(seo) {
    const fields = [
        ["SEO title", seo.seo_title],
        ["Meta description", seo.meta_description],
        ["Slug", seo.slug],
        ["Keywords", (seo.keywords || []).join(", ")],
        ["Tags", (seo.tags || []).join(", ")],
        ["Summary", seo.summary],
        ["Social post", seo.social_post],
    ].filter(([, value]) => value);

    if (!fields.length) {
        return;
    }

    seoFields.innerHTML = fields.map(([label, value]) => `
        <div class="seo-field">
            <div class="seo-field-header">
                <span>${escapeHtml(label)}</span>
                <button type="button" class="copy-chip" data-copy="${escapeHtml(value)}">Copy</button>
            </div>
            <p>${escapeHtml(value)}</p>
        </div>
    `).join("");

    seoCard.hidden = false;
}


seoFields.addEventListener("click", async event => {
    const button = event.target.closest("[data-copy]");

    if (!button) {
        return;
    }

    await navigator.clipboard.writeText(button.dataset.copy);
    showToast("Copied.");
});


function displayError(message) {
    runBadge.textContent = "Failed";
    runBadge.className = "run-badge failed";

    const errorItem = document.createElement("div");

    errorItem.className = "timeline-item failed";
    errorItem.innerHTML = `
        <div class="timeline-marker"><span></span></div>
        <div class="timeline-content">
            <div class="timeline-title">Agent execution failed</div>
            <div class="timeline-detail">${escapeHtml(message)}</div>
        </div>
    `;

    timeline.appendChild(errorItem);
    showToast("Agent execution failed");
}


function handleEvent(event) {
    switch (event.type) {
        case "run_started":
            currentRunId = event.run_id;
            runBadge.textContent = "Running";
            break;

        case "run_resumed":
            runBadge.textContent = "Running";
            runBadge.className = "run-badge running";
            break;

        case "stage":
            updateStage(event);
            break;

        case "routing":
            showRouting(event);
            break;

        case "research_complete":
            showResearch(event);
            break;

        case "plan":
            showPlan(event.plan || {});
            break;

        case "plan_review":
            currentRunId = event.run_id;
            showEditablePlan(event.plan || {});
            break;

        case "paused":
            showToast("Review the outline, then approve it.");
            break;

        case "section_complete":
            completeSection(event);
            break;

        case "review":
            showReview(event);
            break;

        case "section_revised":
            markSectionRevised(event);
            break;

        case "citations":
            showCitations(event);
            break;

        case "diagrams":
            if (event.count) {
                showToast(`${event.count} diagram${event.count === 1 ? "" : "s"} added`);
            }
            break;

        case "seo":
            showSeo(event.seo || {});
            break;

        case "final":
            displayFinalResult(event);
            break;

        case "error":
            displayError(event.message);
            break;

        case "done":
            setRunningState(false);
            break;
    }
}


function parseSSEChunk(buffer) {
    const events = buffer.split("\n\n");
    const remaining = events.pop() || "";

    events.forEach(block => {
        const dataLines = block
            .split("\n")
            .filter(line => line.startsWith("data:"))
            .map(line => line.slice(5).trim());

        if (!dataLines.length) {
            return;
        }

        const rawData = dataLines.join("\n");

        try {
            handleEvent(JSON.parse(rawData));
        } catch (error) {
            console.error("Could not parse stream event:", rawData, error);
        }
    });

    return remaining;
}


async function streamRequest(url, body) {
    abortController = new AbortController();

    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: abortController.signal,
    });

    if (!response.ok) {
        let message = await response.text();

        try {
            const detail = JSON.parse(message).detail;
            message = typeof detail === "string" ? detail : JSON.stringify(detail);
        } catch {
            // keep plain text
        }

        throw new Error(message || "Request failed.");
    }

    if (!response.body) {
        throw new Error("Streaming is not supported by this browser.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();

        if (done) {
            break;
        }

        buffer = parseSSEChunk(buffer + decoder.decode(value, { stream: true }));
    }

    buffer += decoder.decode();

    if (buffer.trim()) {
        parseSSEChunk(`${buffer}\n\n`);
    }
}


async function runStream(url, body) {
    setRunningState(true);

    try {
        await streamRequest(url, body);
    } catch (error) {
        if (error.name === "AbortError") {
            runBadge.textContent = "Stopped";
            runBadge.className = "run-badge stopped";
            showToast("Generation stopped.");
        } else {
            displayError(error.message || "Unexpected error.");
        }
    } finally {
        abortController = null;
        setRunningState(false);
    }
}


runForm.addEventListener("submit", async event => {
    event.preventDefault();

    const topic = topicInput.value.trim();

    if (topic.length < 3) {
        showToast("Enter a more detailed topic.");
        return;
    }

    resetInterface();
    activityEmpty.hidden = true;

    await runStream("/api/run", {
        topic,
        audience: audienceInput.value.trim(),
        tone: toneInput.value,
        length: lengthInput.value,
        require_approval: approvalInput.checked,
    });
});


approveButton.addEventListener("click", async () => {
    if (!currentRunId || !awaitingApproval) {
        return;
    }

    let plan;

    try {
        plan = collectEditedPlan();
    } catch (error) {
        showToast(error.message);
        return;
    }

    awaitingApproval = false;
    planReview.hidden = true;
    showPlan(plan);

    await runStream(`/api/runs/${currentRunId}/resume`, { action: "approve", plan });
});


regenerateButton.addEventListener("click", async () => {
    if (!currentRunId || !awaitingApproval) {
        return;
    }

    const feedback = planFeedback.value.trim();

    if (!feedback) {
        showToast("Describe what should change in the outline.");
        planFeedback.focus();
        return;
    }

    awaitingApproval = false;
    planReview.hidden = true;
    planFeedback.value = "";

    await runStream(`/api/runs/${currentRunId}/resume`, { action: "regenerate", feedback });
});


cancelButton.addEventListener("click", () => {
    if (abortController) {
        abortController.abort();
    }
});


newRunButton.addEventListener("click", () => {
    if (abortController) {
        abortController.abort();
    }

    resetInterface();
    setRunningState(false);
    topicInput.value = "";
    topicInput.focus();
});


document.querySelectorAll("[data-topic]").forEach(button => {
    button.addEventListener("click", () => {
        topicInput.value = button.dataset.topic || "";
        topicInput.focus();
    });
});


previewTab.addEventListener("click", () => {
    previewTab.classList.add("active");
    markdownTab.classList.remove("active");
    articlePreview.hidden = false;
    markdownOutput.hidden = true;
});


markdownTab.addEventListener("click", () => {
    markdownTab.classList.add("active");
    previewTab.classList.remove("active");
    markdownOutput.hidden = false;
    articlePreview.hidden = true;
});


copyButton.addEventListener("click", async () => {
    if (!finalMarkdown) {
        return;
    }

    await navigator.clipboard.writeText(finalMarkdown);
    showToast("Markdown copied.");
});


downloadButton.addEventListener("click", event => {
    if (downloadButton.classList.contains("disabled")) {
        event.preventDefault();
    }
});


async function checkHealth() {
    try {
        const response = await fetch("/api/health");

        if (!response.ok) {
            throw new Error("Server unavailable");
        }

        const data = await response.json();

        healthDot.classList.add("online");
        healthText.textContent = "Server online";
        healthText.title = `LLM: ${data.llm} · checkpoints: ${data.checkpointer}`;
    } catch {
        healthDot.classList.remove("online");
        healthText.textContent = "Server offline";
    }
}


resetInterface();
checkHealth();
