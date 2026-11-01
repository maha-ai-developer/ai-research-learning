"""Lightweight, zero-external-framework Web Application UI for the Agent-First AI Learning Platform."""

from __future__ import annotations


def render_learning_web_app_html() -> str:
    """Return the self-contained HTML/CSS/JS web application served by LocalResearchLibraryAPI."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Agent-First AI Learning Platform</title>
  <style>
    :root {
      --bg: #0f172a;
      --panel: #1e293b;
      --panel-alt: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent-strong: #0284c7;
      --success: #22c55e;
      --warn: #f59e0b;
      --danger: #ef4444;
      --border: #475569;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    header {
      background: #020617;
      border-bottom: 1px solid var(--border);
      padding: 1rem 1.5rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 1rem;
    }
    header h1 {
      margin: 0;
      font-size: 1.25rem;
      color: var(--accent);
    }
    .subtitle {
      font-size: 0.85rem;
      color: var(--muted);
    }
    .layout {
      display: grid;
      grid-template-columns: 320px 1fr;
      min-height: calc(100vh - 72px);
    }
    @media (max-width: 960px) {
      .layout { grid-template-columns: 1fr; }
    }
    aside {
      background: #0b1120;
      border-right: 1px solid var(--border);
      padding: 1rem;
      overflow-y: auto;
    }
    main {
      padding: 1.25rem;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1rem 1.25rem;
    }
    .card h2, .card h3 {
      margin-top: 0;
      color: var(--accent);
    }
    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
    }
    @media (max-width: 900px) {
      .grid-2 { grid-template-columns: 1fr; }
    }
    label {
      display: block;
      font-size: 0.85rem;
      color: var(--muted);
      margin-bottom: 0.25rem;
    }
    input[type="text"], textarea, select {
      width: 100%;
      background: #0f172a;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.5rem 0.65rem;
      font-size: 0.9rem;
      margin-bottom: 0.65rem;
    }
    button {
      background: var(--accent-strong);
      color: white;
      border: none;
      border-radius: 6px;
      padding: 0.5rem 0.9rem;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
    }
    button:hover { background: var(--accent); color: #020617; }
    button.secondary {
      background: var(--panel-alt);
      color: var(--text);
    }
    .badge {
      display: inline-block;
      padding: 0.15rem 0.5rem;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
      background: var(--panel-alt);
      color: var(--text);
    }
    .badge.COMPLETED { background: #14532d; color: #86efac; }
    .badge.PRACTICING { background: #78350f; color: #fde68a; }
    .badge.IN_PROGRESS { background: #1e3a8a; color: #93c5fd; }
    .badge.NOT_STARTED { background: #334155; color: #cbd5e1; }
    .lesson-item {
      padding: 0.6rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      margin-bottom: 0.5rem;
      cursor: pointer;
      background: #111827;
    }
    .lesson-item:hover, .lesson-item.active {
      border-color: var(--accent);
      background: #1e293b;
    }
    .disclaimer {
      font-size: 0.8rem;
      color: #fde68a;
      background: rgba(245, 158, 11, 0.12);
      border-left: 3px solid var(--warn);
      padding: 0.5rem 0.75rem;
      margin: 0.5rem 0;
      border-radius: 4px;
    }
    .prereq-edge {
      font-family: monospace;
      font-size: 0.82rem;
      padding: 0.25rem 0.5rem;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 4px;
      margin-bottom: 0.35rem;
    }
    .citation-chip {
      font-size: 0.8rem;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 0.35rem 0.6rem;
      margin-bottom: 0.35rem;
    }
    ul { padding-left: 1.2rem; margin: 0.4rem 0; }
    pre {
      white-space: pre-wrap;
      background: #090d16;
      padding: 0.75rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      font-size: 0.85rem;
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Agent-First AI Learning Platform</h1>
      <div class="subtitle">Controlled Orchestration • Evidence-Grounded Curriculum • Teaching • Rubric Assessment • Persistent Progress</div>
    </div>
    <div>
      <span id="active-learner-badge" class="badge">Learner: default_learner</span>
      <span id="active-curriculum-badge" class="badge">No Curriculum Loaded</span>
    </div>
  </header>

  <div class="layout">
    <!-- Sidebar: Curriculum Overview & Prerequisite Graph -->
    <aside id="curriculum-overview-panel">
      <h3>1. Learning Dashboard</h3>
      <label for="learner-id-input">Learner ID</label>
      <input id="learner-id-input" type="text" value="default_learner" />
      <label for="goal-input">Learning Goal</label>
      <textarea id="goal-input" rows="3">I want to become an AI engineer and eventually an AI researcher, starting from mathematical and programming foundations.</textarea>
      <button id="generate-curriculum-btn" onclick="generateCurriculum()">Plan / Load Curriculum</button>

      <hr style="border-color: var(--border); margin: 1rem 0;" />
      <h3>2. Curriculum &amp; Prerequisites</h3>
      <div id="curriculum-tracks-list">
        <p class="subtitle">Click "Plan / Load Curriculum" to initialize your interactive learning path.</p>
      </div>
      <h4>Prerequisite Graph (DAG)</h4>
      <div id="prerequisite-graph-view"></div>
    </aside>

    <!-- Main Interactive Workspace -->
    <main>
      <!-- Recommendation & Progress Overview Banner -->
      <section class="card" id="learning-progress-view">
        <h2>6. Learning Progress &amp; Next Lesson Recommendation</h2>
        <div id="recommendation-banner" class="disclaimer">
          Generate or select a curriculum to compute your personalized next lesson recommendation.
        </div>
        <div id="progress-metrics" class="grid-2"></div>
      </section>

      <!-- Lesson Workspace -->
      <section class="card" id="lesson-workspace">
        <h2>3. Lesson Workspace</h2>
        <div id="lesson-detail-container">
          <p class="subtitle">Select a lesson from the curriculum sidebar or recommendation banner to open the Lesson Workspace.</p>
        </div>
      </section>

      <div class="grid-2">
        <!-- Teaching Agent Conversation -->
        <section class="card" id="teaching-agent-conversation">
          <h2>4. Teaching Agent Conversation</h2>
          <div class="disclaimer">
            Explanation depth controls pedagogical style only and does not imply learner competence or mastery.
          </div>
          <label for="explanation-depth-select">Explanation Depth Preference</label>
          <select id="explanation-depth-select">
            <option value="foundational_intuition">Foundational Intuition</option>
            <option value="beginner">Beginner</option>
            <option value="intermediate" selected>Intermediate</option>
            <option value="advanced">Advanced</option>
            <option value="research_level">Research Level</option>
          </select>
          <label for="teach-question-input">Ask the Teaching Agent</label>
          <textarea id="teach-question-input" rows="3" placeholder="Ask about core concepts, prerequisites, mathematical intuition, or implementation trade-offs..."></textarea>
          <button id="ask-teaching-agent-btn" onclick="askTeachingAgent()">Ask Teaching Agent</button>
          <div id="teaching-response-container" style="margin-top: 1rem;"></div>
        </section>

        <!-- Exercise & Assessment Interface -->
        <section class="card" id="exercise-assessment-interface">
          <h2>5. Exercise &amp; Rubric Assessment</h2>
          <div class="disclaimer">
            Passing a single exercise demonstrates objective practice only and does not equate to complete mastery.
          </div>
          <label for="exercise-select">Select Lesson Exercise</label>
          <select id="exercise-select" onchange="renderSelectedExercise()"></select>
          <div id="exercise-prompt-box" style="margin-bottom: 0.75rem;"></div>
          <label for="assessment-answer-input">Your Solution / Answer</label>
          <textarea id="assessment-answer-input" rows="4" placeholder="Write your conceptual, mathematical, or practical response addressing the rubric criteria..."></textarea>
          <button id="submit-assessment-btn" onclick="submitAssessment()">Submit for Rubric Evaluation</button>
          <div id="assessment-result-container" style="margin-top: 1rem;"></div>
        </section>
      </div>
    </main>
  </div>

  <script>
    let currentCurriculum = null;
    let currentLessonId = null;
    let currentExercises = [];
    let currentProgressMap = {};

    function getLearnerId() {
      const val = document.getElementById('learner-id-input').value.trim();
      return val || 'default_learner';
    }

    async function generateCurriculum() {
      const goal = document.getElementById('goal-input').value.trim();
      const learnerId = getLearnerId();
      document.getElementById('active-learner-badge').textContent = 'Learner: ' + learnerId;
      const resp = await fetch('/api/v1/curricula/generate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({learner_goal: goal})
      });
      const data = await resp.json();
      const curriculum = data.curriculum || (data.data && data.data.curriculum);
      if (curriculum) {
        currentCurriculum = curriculum;
        document.getElementById('active-curriculum-badge').textContent = 'Curriculum: ' + currentCurriculum.curriculum_id;
        await refreshProgress();
        renderCurriculumSidebar();
      }
    }

    async function refreshProgress() {
      if (!currentCurriculum) return;
      const learnerId = encodeURIComponent(getLearnerId());
      const cid = encodeURIComponent(currentCurriculum.curriculum_id);
      const resp = await fetch(`/api/v1/curricula/${cid}/progress?learner_id=${learnerId}`);
      const data = await resp.json();
      const progress = data.progress || (data.data && data.data.progress);
      if (!progress) return;
      currentProgressMap = {};
      for (const lp of progress.lesson_progress) {
        currentProgressMap[lp.lesson_id] = lp;
      }
      const rec = progress.next_recommendation;
      const banner = document.getElementById('recommendation-banner');
      if (rec && rec.recommended_lesson_id) {
        banner.innerHTML = `<strong>Recommended Next Lesson (${rec.recommendation_type}):</strong> <code>${rec.recommended_lesson_title}</code> (${rec.recommended_lesson_id}) — ${rec.rationale} <button class="secondary" style="margin-left:0.5rem;" onclick="openLesson('${rec.recommended_lesson_id}')">Open Recommended Lesson</button>`;
      } else if (rec) {
        banner.innerHTML = `<strong>Curriculum Complete:</strong> ${rec.rationale}`;
      }
      const gaps = progress.active_knowledge_gaps || [];
      document.getElementById('progress-metrics').innerHTML = `
        <div>
          <strong>Status Breakdown:</strong>
          <ul>
            <li>Completed: <strong>${progress.completed_count}</strong> / ${progress.total_lessons}</li>
            <li>Practicing: <strong>${progress.practicing_count}</strong></li>
            <li>In Progress: <strong>${progress.in_progress_count}</strong></li>
            <li>Not Started: <strong>${progress.not_started_count}</strong></li>
          </ul>
        </div>
        <div>
          <strong>Demonstrated Objectives:</strong>
          <div class="subtitle">${progress.total_demonstrated_objectives} / ${progress.total_curriculum_objectives} curriculum objectives demonstrated</div>
          <strong style="display:block;margin-top:0.4rem;">Active Knowledge Gaps (${gaps.length}):</strong>
          <div class="subtitle">${gaps.slice(0, 4).join(' • ') || 'No active gaps flagged'}</div>
        </div>
      `;
      renderCurriculumSidebar();
    }

    function renderCurriculumSidebar() {
      if (!currentCurriculum) return;
      const container = document.getElementById('curriculum-tracks-list');
      const stages = currentCurriculum.stages || currentCurriculum.tracks || [];
      let html = '';
      const prereqEdges = [];
      for (const stage of stages) {
        html += `<div style="margin-bottom:0.75rem;"><strong>${stage.title}</strong>`;
        const lessons = stage.lessons || [];
        for (const lesson of lessons) {
          const prog = currentProgressMap[lesson.lesson_id];
          const status = prog ? prog.status : 'NOT_STARTED';
          const activeCls = lesson.lesson_id === currentLessonId ? 'active' : '';
          html += `
            <div class="lesson-item ${activeCls}" onclick="openLesson('${lesson.lesson_id}')">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:0.4rem;">
                <span>${lesson.title}</span>
                <span class="badge ${status}">${status}</span>
              </div>
              <div class="subtitle">${lesson.lesson_id}</div>
            </div>
          `;
          for (const prereq of (lesson.prerequisites || [])) {
            prereqEdges.push({from: prereq, to: lesson.concept_id});
          }
        }
        html += `</div>`;
      }
      container.innerHTML = html;

      const graphDiv = document.getElementById('prerequisite-graph-view');
      graphDiv.innerHTML = prereqEdges.map(e =>
        `<div class="prereq-edge">${e.from} &rarr; ${e.to}</div>`
      ).join('');
    }

    async function openLesson(lessonId) {
      if (!currentCurriculum) return;
      currentLessonId = lessonId;
      const cid = encodeURIComponent(currentCurriculum.curriculum_id);
      const lid = encodeURIComponent(lessonId);
      const learnerId = getLearnerId();

      await fetch(`/api/v1/curricula/${cid}/lessons/${lid}/open`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({learner_id: learnerId})
      });

      const resp = await fetch(`/api/v1/curricula/${cid}/lessons/${lid}?learner_id=${encodeURIComponent(learnerId)}`);
      const payload = await resp.json();
      const detail = payload.lesson ? payload : payload.data;
      if (!detail || !detail.lesson) return;

      const lesson = detail.lesson;
      const progress = detail.progress;
      currentExercises = detail.exercises || [];

      const citations = lesson.source_citations || lesson.citations || [];
      const citationsHtml = citations.map(c =>
        `<div class="citation-chip"><code>${c.citation_id}</code> [${c.evidence_tier}] <strong>${c.title}</strong> (${c.source_id} / ${c.resource_id}${c.chunk_id ? ' / ' + c.chunk_id : ''})</div>`
      ).join('');

      document.getElementById('lesson-detail-container').innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <h3>${lesson.title}</h3>
          <span class="badge ${progress.status}">${progress.status}</span>
        </div>
        <p>${lesson.explanation}</p>
        <p class="subtitle"><strong>Ordering Rationale:</strong> ${lesson.rationale_for_ordering}</p>
        <div class="grid-2">
          <div>
            <strong>Learning Objectives:</strong>
            <ul>${(lesson.learning_objectives || []).map(o => `<li>${o}</li>`).join('')}</ul>
          </div>
          <div>
            <strong>Prerequisites:</strong>
            <ul>${(lesson.prerequisite_lesson_ids || []).map(p => `<li><code>${p}</code></li>`).join('') || '<li>Foundational entry point (no prerequisites)</li>'}</ul>
          </div>
        </div>
        <div style="margin-top:0.6rem;">
          <strong>Verified Evidence Citations:</strong>
          ${citationsHtml}
        </div>
      `;

      const exSelect = document.getElementById('exercise-select');
      exSelect.innerHTML = currentExercises.map((ex) =>
        `<option value="${ex.exercise_id}">[${ex.category.toUpperCase()}] ${ex.title}</option>`
      ).join('');
      renderSelectedExercise();
      await refreshProgress();
    }

    function renderSelectedExercise() {
      const exId = document.getElementById('exercise-select').value;
      const ex = currentExercises.find(e => e.exercise_id === exId) || currentExercises[0];
      const box = document.getElementById('exercise-prompt-box');
      if (!ex) {
        box.innerHTML = '<p class="subtitle">Open a lesson to view exercises.</p>';
        return;
      }
      const rubricList = ex.rubric || ex.rubric_criteria || [];
      const rubricHtml = rubricList.map(r =>
        `<li><strong>${r.description}</strong> (weight ${r.weight}) — key concepts: <code>${(r.required_concepts || []).join(', ')}</code></li>`
      ).join('');
      const targetObjs = (ex.targeted_objectives || []).join('; ');
      box.innerHTML = `
        <div><strong>Category:</strong> <span class="badge">${ex.category}</span></div>
        <p style="margin:0.4rem 0;"><strong>Prompt:</strong> ${ex.prompt}</p>
        <div class="subtitle"><strong>Target Objective:</strong> ${targetObjs}</div>
        <div style="margin-top:0.4rem;"><strong>Explicit Rubric Criteria:</strong><ul>${rubricHtml}</ul></div>
      `;
    }

    async function askTeachingAgent() {
      if (!currentCurriculum || !currentLessonId) return;
      const question = document.getElementById('teach-question-input').value.trim() || 'Please explain the core concepts of this lesson.';
      const depth = document.getElementById('explanation-depth-select').value;
      const cid = encodeURIComponent(currentCurriculum.curriculum_id);
      const lid = encodeURIComponent(currentLessonId);
      const resp = await fetch(`/api/v1/curricula/${cid}/lessons/${lid}/teach`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          learner_id: getLearnerId(),
          learner_question: question,
          explanation_depth: depth
        })
      });
      const payload = await resp.json();
      const t = payload.teaching_response || (payload.data && payload.data.teaching_response);
      if (!t) return;
      document.getElementById('teaching-response-container').innerHTML = `
        <div><strong>Evidence Status:</strong> <span class="badge">${t.status}</span></div>
        <pre>${t.core_explanation}</pre>
        <strong>Examples &amp; Analogies:</strong>
        <ul>${(t.examples_and_analogies || []).map(x => `<li>${x}</li>`).join('')}</ul>
        <strong>Socratic Questions for Deeper Reasoning:</strong>
        <ul>${(t.socratic_questions || []).map(q => `<li>${q}</li>`).join('')}</ul>
        <strong>Uncertainty &amp; Scope Notes:</strong>
        <p class="subtitle">${t.uncertainty_and_limits || ''}</p>
      `;
      await refreshProgress();
    }

    async function submitAssessment() {
      if (!currentCurriculum || !currentLessonId) return;
      const exId = document.getElementById('exercise-select').value;
      const answer = document.getElementById('assessment-answer-input').value;
      const cid = encodeURIComponent(currentCurriculum.curriculum_id);
      const lid = encodeURIComponent(currentLessonId);
      const resp = await fetch(`/api/v1/curricula/${cid}/lessons/${lid}/assess`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          learner_id: getLearnerId(),
          exercise_id: exId,
          submitted_answer: answer
        })
      });
      const payload = await resp.json();
      const box = document.getElementById('assessment-result-container');
      const ev = payload.evaluation || (payload.data && payload.data.evaluation);
      if (!ev) {
        box.innerHTML = `<div class="disclaimer" style="border-color:var(--danger);color:#fca5a5;">${payload.detail || (payload.error && payload.error.message) || 'Submission error'}</div>`;
        return;
      }
      const misconceptions = ev.detected_misconceptions || [];
      box.innerHTML = `
        <div>
          <strong>Score:</strong> ${(ev.score * 100).toFixed(0)}% (${ev.score_band})
          <span class="badge ${ev.passed ? 'COMPLETED' : 'PRACTICING'}">${ev.passed ? 'Objective Demonstrated' : 'Needs Practice'}</span>
        </div>
        <p>${ev.deterministic_feedback}</p>
        ${misconceptions.length ? `<strong>Errors &amp; Misconceptions:</strong><ul>${misconceptions.map(m => `<li>${m.explanation}</li>`).join('')}</ul>` : ''}
        ${(ev.objectives_needing_practice || []).length ? `<strong>Objectives Needing Further Practice:</strong><ul>${ev.objectives_needing_practice.map(o => `<li>${o}</li>`).join('')}</ul>` : ''}
        <div class="disclaimer">${ev.mastery_disclaimer}</div>
      `;
      await refreshProgress();
    }
  </script>
</body>
</html>
"""
