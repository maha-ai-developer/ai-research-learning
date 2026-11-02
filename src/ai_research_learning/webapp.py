"""Unified Web Application UI for the Agent-First AI Research & Learning Platform (Task 036).

Visual Design System: "Scholarly Paper & Carbon Studio" (Editorial Research & Learning Design System)
-----------------------------------------------------------------------------------------------------
Independently researched and engineered for extended technical reading, mathematical/conceptual study,
and platform-wide observability — inspired by DeepLearning.AI, Distill.pub, Semantic Scholar, and
academic research monographs, while avoiding generic navy/cobalt templates:

1. Light Mode (`data-theme="light"` — Warm Archival Paper & Terracotta/Pine):
   - Canvas: Warm Alabaster Paper (`#FAF8F5`) to eliminate stark white glare during long study sessions.
   - Surfaces: Crisp Ivory (`#FFFFFF`) and Warm Parchment (`#F3EFEA`) with Stone borders (`#E4DFD3`).
   - Typography: Deep Charcoal Ink (`#18181B`, 15.8:1 AAA contrast) and Slate Umber (`#52525B`, 7.2:1 AAA).
   - Primary Brand Accent: Deep Terracotta (`#C84B31`, 5.3:1 AA contrast) for interactive actions & focus.
   - Secondary Research Accent: Botanical Viridian Pine (`#1E6F5C`) for verified citations & evidence tiers.

2. Dark Mode (`data-theme="dark"` — Carbon Obsidian Studio & Warm Ember/Jade):
   - Canvas: Neutral Carbon Obsidian (`#121316`) with zero navy/cobalt cast.
   - Surfaces: Graphite Slate (`#1A1C20`) and Elevated Charcoal (`#23262D`) with `#2E323B` borders.
   - Typography: Warm Parchment White (`#F4F3EF`) to prevent halation/astigmatism blur on dark surfaces.
   - Primary Brand Accent: Warm Coral-Ember (`#E26D52`) paired with Luminous Jade (`#34A88E`).

3. Persistent Theme Switching:
   - Synchronized via `localStorage` (`ai_learning_theme`), falls back to `prefers-color-scheme`,
     and updates `document.documentElement.dataset.theme` instantaneously without page reload.
"""

from __future__ import annotations

DESIGN_SYSTEM_METADATA = {
    "design_system_name": "Scholarly Paper & Carbon Studio",
    "inspiration_references": [
        "DeepLearning.AI (warm coral-terracotta energy, structured course & lab progression)",
        "Distill.pub (editorial typography, high-contrast scholarly legibility, citation clarity)",
        "Semantic Scholar & arXiv Labs (evidence tier badges, provenance chips, structured metadata)",
    ],
    "light_mode": {
        "bg_canvas": "#FAF8F5",
        "bg_surface": "#FFFFFF",
        "bg_surface_alt": "#F3EFEA",
        "text_primary": "#18181B",
        "text_secondary": "#52525B",
        "accent_primary": "#C84B31",
        "accent_secondary": "#1E6F5C",
        "border": "#E4DFD3",
    },
    "dark_mode": {
        "bg_canvas": "#121316",
        "bg_surface": "#1A1C20",
        "bg_surface_alt": "#23262D",
        "text_primary": "#F4F3EF",
        "text_secondary": "#A1A1AA",
        "accent_primary": "#E26D52",
        "accent_secondary": "#34A88E",
        "border": "#2E323B",
    },
    "primary_navigation_areas": [
        "HOME",
        "LEARN",
        "RESEARCH",
        "AI AGENTS",
        "LAB",
        "PLATFORM INTELLIGENCE",
    ],
}


def render_learning_web_app_html() -> str:
    """Return the self-contained HTML/CSS/JS unified web application served by LocalResearchLibraryAPI."""
    return """<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Agent-First AI Learning Platform — Unified Research &amp; Learning Workspace</title>
  <script>
    (function() {
      try {
        var saved = localStorage.getItem('ai_learning_theme');
        if (saved === 'dark' || saved === 'light') {
          document.documentElement.setAttribute('data-theme', saved);
        } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
          document.documentElement.setAttribute('data-theme', 'dark');
        } else {
          document.documentElement.setAttribute('data-theme', 'light');
        }
      } catch (e) {
        document.documentElement.setAttribute('data-theme', 'light');
      }
    })();
  </script>
  <style>
    /* ==========================================================================
       Scholarly Paper & Carbon Studio — Light & Dark Theme Tokens
       ========================================================================== */
    :root,
    html[data-theme="light"] {
      --bg-canvas: #FAF8F5;
      --bg-header: #F3EFEA;
      --bg-sidebar: #F5F2EB;
      --bg-surface: #FFFFFF;
      --bg-surface-alt: #F3EFEA;
      --bg-code: #F0ECE1;
      --text-primary: #18181B;
      --text-secondary: #52525B;
      --text-muted: #71717A;
      --accent-primary: #C84B31;
      --accent-primary-hover: #A83820;
      --accent-primary-soft: rgba(200, 75, 49, 0.10);
      --accent-secondary: #1E6F5C;
      --accent-secondary-soft: rgba(30, 111, 92, 0.10);
      --border: #E4DFD3;
      --border-strong: #CFC7B4;
      --success-bg: #DCFCE7;
      --success-text: #166534;
      --warn-bg: #FEF3C7;
      --warn-text: #92400E;
      --warn-border: #D97706;
      --info-bg: #E0F2FE;
      --info-text: #075985;
      --danger-bg: #FEE2E2;
      --danger-text: #991B1B;
      --danger-border: #DC2626;
      --shadow-card: 0 1px 3px rgba(24, 24, 27, 0.06), 0 1px 2px rgba(24, 24, 27, 0.04);
      --font-sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-serif: "Newsreader", "Georgia", "Cambria", "Times New Roman", serif;
      --font-mono: "JetBrains Mono", "Fira Code", "SFMono-Regular", Consolas, monospace;
    }

    html[data-theme="dark"] {
      --bg-canvas: #121316;
      --bg-header: #16181C;
      --bg-sidebar: #16181C;
      --bg-surface: #1A1C20;
      --bg-surface-alt: #23262D;
      --bg-code: #14161A;
      --text-primary: #F4F3EF;
      --text-secondary: #A1A1AA;
      --text-muted: #71717A;
      --accent-primary: #E26D52;
      --accent-primary-hover: #F08066;
      --accent-primary-soft: rgba(226, 109, 82, 0.15);
      --accent-secondary: #34A88E;
      --accent-secondary-soft: rgba(52, 168, 142, 0.15);
      --border: #2E323B;
      --border-strong: #3F4451;
      --success-bg: rgba(22, 101, 52, 0.35);
      --success-text: #86EFAC;
      --warn-bg: rgba(146, 64, 14, 0.30);
      --warn-text: #FDE68A;
      --warn-border: #F59E0B;
      --info-bg: rgba(30, 111, 92, 0.30);
      --info-text: #99F6E4;
      --danger-bg: rgba(153, 27, 27, 0.35);
      --danger-text: #FCA5A5;
      --danger-border: #EF4444;
      --shadow-card: 0 2px 6px rgba(0, 0, 0, 0.35);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: var(--font-sans);
      background: var(--bg-canvas);
      color: var(--text-primary);
      line-height: 1.6;
      transition: background-color 0.18s ease, color 0.18s ease;
    }

    /* Top Brand & Navigation Shell */
    header.platform-header {
      background: var(--bg-header);
      border-bottom: 1px solid var(--border);
      padding: 0.75rem 1.5rem;
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
      position: sticky;
      top: 0;
      z-index: 50;
    }
    .header-top-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 1rem;
    }
    .brand-title-group h1 {
      margin: 0;
      font-family: var(--font-serif);
      font-size: 1.35rem;
      font-weight: 700;
      color: var(--text-primary);
      letter-spacing: -0.01em;
    }
    .brand-title-group h1 span.brand-accent {
      color: var(--accent-primary);
    }
    .subtitle {
      font-size: 0.84rem;
      color: var(--text-secondary);
    }
    .header-controls {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      flex-wrap: wrap;
    }
    nav.primary-nav {
      display: flex;
      gap: 0.35rem;
      flex-wrap: wrap;
      border-top: 1px solid var(--border);
      padding-top: 0.55rem;
    }
    button.nav-tab {
      background: transparent;
      color: var(--text-secondary);
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 0.4rem 0.85rem;
      font-size: 0.83rem;
      font-weight: 600;
      letter-spacing: 0.02em;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    button.nav-tab:hover {
      background: var(--bg-surface-alt);
      color: var(--text-primary);
    }
    button.nav-tab.active {
      background: var(--accent-primary-soft);
      color: var(--accent-primary);
      border-color: var(--accent-primary);
    }
    button.theme-toggle {
      background: var(--bg-surface);
      color: var(--text-primary);
      border: 1px solid var(--border-strong);
      border-radius: 999px;
      padding: 0.35rem 0.85rem;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
    }
    button.theme-toggle:hover {
      border-color: var(--accent-primary);
      color: var(--accent-primary);
    }

    /* Views & Workspace Grid */
    .platform-view {
      display: none;
    }
    .platform-view.active-view {
      display: block;
    }
    .container-wide {
      max-width: 1440px;
      margin: 0 auto;
      padding: 1.25rem 1.5rem;
    }
    .layout {
      display: grid;
      grid-template-columns: 340px 1fr;
      min-height: calc(100vh - 118px);
    }
    @media (max-width: 980px) {
      .layout { grid-template-columns: 1fr; }
    }
    aside {
      background: var(--bg-sidebar);
      border-right: 1px solid var(--border);
      padding: 1.15rem;
      overflow-y: auto;
    }
    main {
      padding: 1.25rem;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }

    /* Cards, Grids, Typography */
    .card {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.1rem 1.35rem;
      box-shadow: var(--shadow-card);
    }
    .card h2, .card h3 {
      margin-top: 0;
      font-family: var(--font-serif);
      color: var(--text-primary);
      border-bottom: 1px solid var(--border);
      padding-bottom: 0.45rem;
    }
    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1.1rem;
    }
    .grid-3 {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 1rem;
    }
    .grid-4 {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 0.9rem;
    }
    @media (max-width: 960px) {
      .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; }
    }

    .stat-box {
      background: var(--bg-surface-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.85rem 1rem;
    }
    .stat-box .stat-label {
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--text-secondary);
      font-weight: 600;
    }
    .stat-box .stat-value {
      font-size: 1.45rem;
      font-weight: 700;
      color: var(--accent-primary);
      margin-top: 0.2rem;
    }

    /* Forms & Buttons */
    label {
      display: block;
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--text-secondary);
      margin-bottom: 0.25rem;
    }
    input[type="text"], textarea, select {
      width: 100%;
      background: var(--bg-canvas);
      color: var(--text-primary);
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      padding: 0.55rem 0.7rem;
      font-size: 0.9rem;
      font-family: var(--font-sans);
      margin-bottom: 0.65rem;
    }
    input[type="text"]:focus, textarea:focus, select:focus {
      outline: 2px solid var(--accent-primary);
      border-color: var(--accent-primary);
    }
    button {
      background: var(--accent-primary);
      color: #FFFFFF;
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 0.5rem 0.95rem;
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      transition: background-color 0.15s ease;
    }
    button:hover {
      background: var(--accent-primary-hover);
    }
    button.secondary {
      background: var(--bg-surface-alt);
      color: var(--text-primary);
      border: 1px solid var(--border-strong);
    }
    button.secondary:hover {
      border-color: var(--accent-primary);
      color: var(--accent-primary);
    }
    button.chip-btn {
      background: var(--bg-surface-alt);
      color: var(--text-primary);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0.3rem 0.75rem;
      font-size: 0.78rem;
      margin: 0.2rem;
    }
    button.chip-btn:hover {
      border-color: var(--accent-primary);
      color: var(--accent-primary);
    }

    /* Badges & Pills */
    .badge {
      display: inline-block;
      padding: 0.18rem 0.55rem;
      border-radius: 999px;
      font-size: 0.74rem;
      font-weight: 600;
      background: var(--bg-surface-alt);
      color: var(--text-primary);
      border: 1px solid var(--border);
    }
    .badge.COMPLETED, .badge.ACTIVE {
      background: var(--success-bg);
      color: var(--success-text);
      border-color: transparent;
    }
    .badge.PRACTICING, .badge.DISCOVERY_ONLY {
      background: var(--warn-bg);
      color: var(--warn-text);
      border-color: transparent;
    }
    .badge.IN_PROGRESS {
      background: var(--info-bg);
      color: var(--info-text);
      border-color: transparent;
    }
    .badge.NOT_STARTED {
      background: var(--bg-surface-alt);
      color: var(--text-secondary);
    }

    .lesson-item {
      padding: 0.65rem 0.75rem;
      border: 1px solid var(--border);
      border-radius: 8px;
      margin-bottom: 0.5rem;
      cursor: pointer;
      background: var(--bg-surface);
      transition: border-color 0.15s ease, transform 0.1s ease;
    }
    .lesson-item:hover, .lesson-item.active {
      border-color: var(--accent-primary);
      background: var(--accent-primary-soft);
    }
    .disclaimer {
      font-size: 0.82rem;
      color: var(--warn-text);
      background: var(--warn-bg);
      border-left: 3px solid var(--warn-border);
      padding: 0.55rem 0.8rem;
      margin: 0.5rem 0;
      border-radius: 4px;
    }
    .prereq-edge {
      font-family: var(--font-mono);
      font-size: 0.8rem;
      padding: 0.3rem 0.55rem;
      background: var(--bg-code);
      border: 1px solid var(--border);
      border-radius: 4px;
      margin-bottom: 0.35rem;
    }
    .citation-chip {
      font-size: 0.82rem;
      background: var(--accent-secondary-soft);
      border: 1px solid var(--accent-secondary);
      border-radius: 6px;
      padding: 0.4rem 0.65rem;
      margin-bottom: 0.4rem;
    }
    .pipeline-stage-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem;
      margin: 0.65rem 0;
    }
    .pipeline-stage-pill {
      background: var(--bg-surface-alt);
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      padding: 0.4rem 0.65rem;
      font-size: 0.78rem;
    }
    table.data-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      margin-top: 0.5rem;
    }
    table.data-table th, table.data-table td {
      border: 1px solid var(--border);
      padding: 0.5rem 0.65rem;
      text-align: left;
      vertical-align: top;
    }
    table.data-table th {
      background: var(--bg-surface-alt);
      font-weight: 600;
      color: var(--text-secondary);
    }
    ul { padding-left: 1.2rem; margin: 0.4rem 0; }
    code {
      font-family: var(--font-mono);
      font-size: 0.83em;
      background: var(--bg-code);
      padding: 0.1rem 0.35rem;
      border-radius: 4px;
    }
    pre {
      white-space: pre-wrap;
      font-family: var(--font-mono);
      background: var(--bg-code);
      padding: 0.8rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      font-size: 0.85rem;
    }
  </style>
</head>
<body>
  <header class="platform-header">
    <div class="header-top-row">
      <div class="brand-title-group">
        <h1><span class="brand-accent">AI Research</span> &amp; Agent-First Learning Platform</h1>
        <div class="subtitle">Governed Multi-Source Library • Controlled Agent Runtime • Evidence-Grounded Curriculum • Read-Only Platform Intelligence</div>
      </div>
      <div class="header-controls">
        <span id="active-learner-badge" class="badge">Learner: default_learner</span>
        <span id="active-curriculum-badge" class="badge">No Curriculum Loaded</span>
        <button id="theme-toggle-btn" class="theme-toggle" onclick="toggleTheme()" title="Switch between Scholarly Paper (Light) and Carbon Studio (Dark) modes">
          <span id="theme-toggle-label">Theme: Light / Dark</span>
        </button>
      </div>
    </div>
    <nav class="primary-nav" aria-label="Primary Platform Navigation">
      <button class="nav-tab" id="nav-btn-HOME" data-view="HOME" onclick="switchView('HOME')">HOME</button>
      <button class="nav-tab active" id="nav-btn-LEARN" data-view="LEARN" onclick="switchView('LEARN')">LEARN</button>
      <button class="nav-tab" id="nav-btn-RESEARCH" data-view="RESEARCH" onclick="switchView('RESEARCH')">RESEARCH</button>
      <button class="nav-tab" id="nav-btn-AI-AGENTS" data-view="AI AGENTS" onclick="switchView('AI AGENTS')">AI AGENTS</button>
      <button class="nav-tab" id="nav-btn-LAB" data-view="LAB" onclick="switchView('LAB')">LAB</button>
      <button class="nav-tab" id="nav-btn-PLATFORM-INTELLIGENCE" data-view="PLATFORM INTELLIGENCE" onclick="switchView('PLATFORM INTELLIGENCE')">PLATFORM INTELLIGENCE</button>
    </nav>
  </header>

  <!-- ====================================================================
       VIEW 1: HOME (Executive Platform Overview & Quick Launch)
       ==================================================================== -->
  <div id="view-HOME" class="platform-view container-wide">
    <div class="card" style="margin-bottom: 1.15rem;">
      <h2>Unified AI Research &amp; Learning Platform Overview</h2>
      <p>
        Welcome to the unified <strong>Agent-First AI Research Learning Platform</strong>. Every curriculum track,
        lesson explanation, rubric evaluation, and platform diagnostic is grounded in verified primary research
        and official engineering documentation with strict citation integrity.
      </p>
      <div id="home-kpi-grid" class="grid-4" style="margin-top: 0.9rem;">
        <div class="stat-box">
          <div class="stat-label">Governed Sources</div>
          <div class="stat-value" id="home-stat-sources">6</div>
          <div class="subtitle">5 Primary/Core + 1 Discovery Radar</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Syllabus Domains</div>
          <div class="stat-value" id="home-stat-domains">9</div>
          <div class="subtitle">Foundations to Research Frontiers</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Governed Agents</div>
          <div class="stat-value" id="home-stat-agents">7</div>
          <div class="subtitle">6 Learning + 1 Read-Only Intelligence</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Citation Integrity</div>
          <div class="stat-value" id="home-stat-integrity">100%</div>
          <div class="subtitle">Strict Abstention on Missing Evidence</div>
        </div>
      </div>
    </div>

    <div class="grid-3">
      <div class="card">
        <h3>Interactive Curriculum &amp; Teaching</h3>
        <p class="subtitle">Plan prerequisite-ordered curricula, study with the Socratic Teaching Agent across 5 explanation depths, and complete rubric-graded exercises.</p>
        <button onclick="switchView('LEARN')">Open LEARN Workspace</button>
      </div>
      <div class="card">
        <h3>Governed Research Library &amp; Control Plane</h3>
        <p class="subtitle">Inspect the 9-stage ingestion pipeline, explore arXiv/PyTorch/HuggingFace/Stanford/DeepLearning.AI sources, and audit syllabus coverage gaps.</p>
        <button onclick="switchView('RESEARCH')">Explore RESEARCH Library</button>
      </div>
      <div class="card">
        <h3>Read-Only Platform Intelligence</h3>
        <p class="subtitle">Query real-time platform state, RAG chunk health, learner telemetry, and workflow traces with strict separation of facts vs. recommendations.</p>
        <button onclick="switchView('PLATFORM INTELLIGENCE')">Launch PLATFORM INTELLIGENCE</button>
      </div>
    </div>
  </div>

  <!-- ====================================================================
       VIEW 2: LEARN (Interactive Curriculum, Lesson, Teaching & Assessment)
       ==================================================================== -->
  <div id="view-LEARN" class="platform-view active-view">
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
  </div>

  <!-- ====================================================================
       VIEW 3: RESEARCH (Source Ingestion Control Plane & Syllabus Matrix)
       ==================================================================== -->
  <div id="view-RESEARCH" class="platform-view container-wide">
    <div class="card" style="margin-bottom: 1.15rem;">
      <h2>Source Ingestion Control Plane &amp; Governed Research Library</h2>
      <p class="subtitle">
        9-stage governed pipeline ensuring only quality-checked, approved, and indexed primary/authoritative sources
        ground agent explanations. <strong>The Batch (DeepLearning.AI)</strong> is strictly governed as a
        <em>Discovery &amp; Trend Radar Only</em> source and cannot serve as sole citation for technical or mathematical claims.
      </p>
      <div id="control-plane-stages-bar" class="pipeline-stage-row"></div>
      <div id="control-plane-sources-table"></div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h3>Search Indexed Research &amp; Engineering Library</h3>
        <label for="research-search-input">Full-Text / Topic Query</label>
        <input id="research-search-input" type="text" value="transformer attention" placeholder="Search indexed resources (e.g., attention, backpropagation, tokenization)..." />
        <button onclick="searchResearchLibrary()">Search Library</button>
        <div id="research-search-results" style="margin-top: 0.85rem;"></div>
      </div>

      <div class="card">
        <h3>Syllabus Intelligence &amp; Depth Coverage Matrix</h3>
        <p class="subtitle">Coverage across the 9 canonical AI syllabus domains and 5 explanation depth tiers.</p>
        <div id="syllabus-coverage-container"></div>
      </div>
    </div>
  </div>

  <!-- ====================================================================
       VIEW 4: AI AGENTS (Governed Agent Catalog & Workflow Run Traces)
       ==================================================================== -->
  <div id="view-AI-AGENTS" class="platform-view container-wide">
    <div class="card" style="margin-bottom: 1.15rem;">
      <h2>Coordinated Agent Runtime &amp; Explicit Contracts</h2>
      <p class="subtitle">
        All 7 platform agent capabilities execute under bounded step budgets, explicit tool-permission allowlists,
        and deterministic validation guards.
      </p>
      <div id="agents-catalog-grid" class="grid-2"></div>
    </div>

    <div class="card">
      <h3>Recent Workflow Execution Runs &amp; Step Traces</h3>
      <button class="secondary" onclick="loadAgentsAndRuns()">Refresh Workflow Traces</button>
      <div id="workflow-runs-container" style="margin-top: 0.85rem;"></div>
    </div>
  </div>

  <!-- ====================================================================
       VIEW 5: LAB (Interactive Depth & Concept Exploration Bench)
       ==================================================================== -->
  <div id="view-LAB" class="platform-view container-wide">
    <div class="card" style="margin-bottom: 1.15rem;">
      <h2>Interactive Concept &amp; Pedagogical Depth Lab</h2>
      <p class="subtitle">
        Compare how the platform structures AI domains across all 5 pedagogical explanation levels
        (<code>foundational_intuition</code> &rarr; <code>beginner</code> &rarr; <code>intermediate</code> &rarr; <code>advanced</code> &rarr; <code>research_level</code>)
        and jump directly into hands-on exercises.
      </p>
      <div id="lab-domains-list"></div>
    </div>
  </div>

  <!-- ====================================================================
       VIEW 6: PLATFORM INTELLIGENCE (Read-Only Observability & Q&A Agent)
       ==================================================================== -->
  <div id="view-PLATFORM-INTELLIGENCE" class="platform-view container-wide">
    <div class="card" style="margin-bottom: 1.15rem;">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem;">
        <h2 style="border:none;margin:0;padding:0;">Platform Intelligence Agent (Strictly Read-Only)</h2>
        <span class="badge ACTIVE">Guardrail: Read-Only • Mutation Forbidden</span>
      </div>
      <p class="subtitle" style="margin-top:0.35rem;">
        Ask diagnostic questions about source ecosystem health, syllabus coverage gaps, RAG index readiness,
        curriculum prerequisite DAGs, agent workflow traces, and learner progress telemetry.
        Every answer separates <strong>Measured Facts</strong>, <strong>Derived Statistics</strong>,
        <strong>Recommendations</strong>, and <strong>Unknown / Unavailable Telemetry</strong>.
      </p>

      <div style="margin: 0.75rem 0;">
        <strong style="font-size:0.82rem;color:var(--text-secondary);">Canonical Diagnostic Questions (Click to Run):</strong>
        <div style="margin-top:0.35rem;">
          <button class="chip-btn" onclick="askPresetPlatformQuestion('What sources are currently approved and indexed?')">1. Approved &amp; Indexed Sources</button>
          <button class="chip-btn" onclick="askPresetPlatformQuestion('Which AI topics have weak source coverage?')">2. Weak Topic Coverage</button>
          <button class="chip-btn" onclick="askPresetPlatformQuestion('Why was this lesson recommended next?')">3. Next Lesson Recommendation Rationale</button>
          <button class="chip-btn" onclick="askPresetPlatformQuestion('Which lessons depend on linear algebra?')">4. Linear Algebra Dependents</button>
          <button class="chip-btn" onclick="askPresetPlatformQuestion('Which retrieval chunks supported the last Teaching Agent answer?')">5. Retrieval Chunk Provenance</button>
          <button class="chip-btn" onclick="askPresetPlatformQuestion('Which exercises are failing most often?')">6. Exercise Bottlenecks</button>
          <button class="chip-btn" onclick="askPresetPlatformQuestion('What is the status of the RAG index and knowledge store?')">7. RAG &amp; Knowledge Store Status</button>
          <button class="chip-btn" onclick="askPresetPlatformQuestion('Which agent workflows ran recently and did any fail?')">8. Recent Workflow Runs</button>
          <button class="chip-btn" onclick="askPresetPlatformQuestion('What gaps prevent a beginner from reaching LLM engineering?')">9. Beginner &rarr; LLM Engineering Gaps</button>
          <button class="chip-btn" onclick="askPresetPlatformQuestion('What should be ingested next to improve the curriculum?')">10. Next Source Ingestion Priorities</button>
        </div>
      </div>

      <label for="platform-intel-question-input">Ask Platform Intelligence</label>
      <textarea id="platform-intel-question-input" rows="2" placeholder="Ask about platform state, source coverage, RAG index, prerequisite chains, or workflow traces...">What is the status of the RAG index and knowledge store?</textarea>
      <button id="ask-platform-intel-btn" onclick="askPlatformIntelligence()">Ask Platform Intelligence Agent</button>

      <div id="platform-intel-response-box" style="margin-top: 1.1rem;"></div>
    </div>

    <div class="card">
      <h3>Live Platform Context Snapshot</h3>
      <button class="secondary" onclick="loadPlatformContextSnapshot()">Refresh Snapshot</button>
      <div id="platform-context-snapshot-box" style="margin-top: 0.85rem;"></div>
    </div>
  </div>

  <script>
    let currentCurriculum = null;
    let currentLessonId = null;
    let currentExercises = [];
    let currentProgressMap = {};
    let activeView = 'LEARN';

    /* Theme Management */
    function updateThemeToggleLabel() {
      const current = document.documentElement.getAttribute('data-theme') || 'light';
      const label = document.getElementById('theme-toggle-label');
      if (label) {
        label.textContent = current === 'dark' ? 'Theme: Carbon Dark (Switch to Light)' : 'Theme: Scholarly Light (Switch to Dark)';
      }
    }

    function toggleTheme() {
      const current = document.documentElement.getAttribute('data-theme') || 'light';
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try {
        localStorage.setItem('ai_learning_theme', next);
      } catch (e) {}
      updateThemeToggleLabel();
    }

    /* Navigation Management */
    function switchView(viewName) {
      activeView = viewName;
      const views = ['HOME', 'LEARN', 'RESEARCH', 'AI AGENTS', 'LAB', 'PLATFORM INTELLIGENCE'];
      for (const v of views) {
        const domId = 'view-' + v.replace(/\\s+/g, '-');
        const btnId = 'nav-btn-' + v.replace(/\\s+/g, '-');
        const el = document.getElementById(domId);
        const btn = document.getElementById(btnId);
        if (el) {
          if (v === viewName) el.classList.add('active-view');
          else el.classList.remove('active-view');
        }
        if (btn) {
          if (v === viewName) btn.classList.add('active');
          else btn.classList.remove('active');
        }
      }
      if (viewName === 'RESEARCH') {
        loadResearchControlPlaneAndSyllabus();
      } else if (viewName === 'AI AGENTS') {
        loadAgentsAndRuns();
      } else if (viewName === 'LAB') {
        loadLabView();
      } else if (viewName === 'PLATFORM INTELLIGENCE') {
        loadPlatformContextSnapshot();
      }
    }

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
        box.innerHTML = `<div class="disclaimer" style="border-color:var(--danger-border);color:var(--danger-text);">${payload.detail || (payload.error && payload.error.message) || 'Submission error'}</div>`;
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

    /* ====================================================================
       RESEARCH View Functions
       ==================================================================== */
    async function loadResearchControlPlaneAndSyllabus() {
      const cpResp = await fetch('/api/v1/platform/sources-control-plane');
      const cpData = await cpResp.json();
      const stagesBar = document.getElementById('control-plane-stages-bar');
      const stages = cpData.ingestion_pipeline_stages || cpData.pipeline_stages || [];
      if (stages.length && stagesBar) {
        stagesBar.innerHTML = stages.map((s) =>
          `<div class="pipeline-stage-pill"><strong>${s.title || s.stage_id}</strong> (${s.live_metric_value || 0})</div>`
        ).join('');
      }
      const tableBox = document.getElementById('control-plane-sources-table');
      const sources = cpData.canonical_sources || cpData.sources || [];
      if (sources.length && tableBox) {
        tableBox.innerHTML = `
          <table class="data-table">
            <thead>
              <tr>
                <th>Source ID</th>
                <th>Name</th>
                <th>Pedagogical Role</th>
                <th>Governance Status</th>
                <th>Indexed / Chunks</th>
                <th>Foundational Citation Policy</th>
              </tr>
            </thead>
            <tbody>
              ${sources.map(s => `
                <tr>
                  <td><code>${s.source_id}</code></td>
                  <td><strong>${s.name}</strong><div class="subtitle">${s.canonical_url || ''}</div></td>
                  <td>${s.pedagogical_role || s.platform_role || ''}</td>
                  <td><span class="badge ${s.allowed_for_foundational_teaching === false ? 'DISCOVERY_ONLY' : 'ACTIVE'}">${s.governance_status}</span></td>
                  <td>${s.extracted_resource_count || 0} extracted / ${s.active_chunk_count || 0} chunks (${s.approved_uncollected_count || 0} seed backlog)</td>
                  <td>${s.allowed_for_foundational_teaching ? '<span class="badge COMPLETED">Primary Citation Permitted</span>' : '<span class="badge DISCOVERY_ONLY">Discovery Radar Only (Corroboration Required)</span>'}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      }

      const sylResp = await fetch('/api/v1/platform/syllabus-coverage');
      const sylData = await sylResp.json();
      const sylBox = document.getElementById('syllabus-coverage-container');
      if (sylData.domains && sylBox) {
        sylBox.innerHTML = `
          <div class="subtitle" style="margin-bottom:0.5rem;">
            Total Domains: <strong>${sylData.total_domains}</strong> •
            Chunk-Backed Concepts: <strong>${sylData.chunk_backed_concept_count}</strong> / ${sylData.total_concepts_in_graph}
          </div>
          <table class="data-table">
            <thead>
              <tr>
                <th>Domain</th>
                <th>Concepts</th>
                <th>Evidence &amp; Chunks</th>
                <th>Readiness Status</th>
              </tr>
            </thead>
            <tbody>
              ${sylData.domains.map(d => `
                <tr>
                  <td><strong>${d.title}</strong><div class="subtitle">${d.domain_id}</div></td>
                  <td>${(d.concepts || []).map(c => `<code>${c.concept_id}</code>`).join(', ')}</td>
                  <td>${d.extracted_document_count} docs / ${d.active_chunk_count} chunks</td>
                  <td><span class="badge ${d.readiness_status === 'PRIMARY_CHUNK_BACKED' ? 'COMPLETED' : 'PRACTICING'}">${d.readiness_status}</span></td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      }
      await searchResearchLibrary();
    }

    async function searchResearchLibrary() {
      const q = document.getElementById('research-search-input').value.trim() || 'attention';
      const resp = await fetch(`/api/v1/search?q=${encodeURIComponent(q)}&limit=6`);
      const data = await resp.json();
      const box = document.getElementById('research-search-results');
      const items = data.items || [];
      if (!items.length) {
        box.innerHTML = '<p class="subtitle">No matching resources found for this query.</p>';
        return;
      }
      box.innerHTML = items.map(item => `
        <div class="citation-chip">
          <strong>${item.title}</strong> (<code>${item.resource_id}</code> • <code>${item.source_id}</code>)
          <div class="subtitle">${item.summary || item.snippet || ''}</div>
        </div>
      `).join('');
    }

    /* ====================================================================
       AI AGENTS View Functions
       ==================================================================== */
    async function loadAgentsAndRuns() {
      const agResp = await fetch('/api/v1/agents?include_platform_intelligence=true');
      const agData = await agResp.json();
      const agents = agData.agents || [];
      const grid = document.getElementById('agents-catalog-grid');
      if (grid) {
        grid.innerHTML = agents.map(a => `
          <div class="stat-box">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <strong>${a.display_name}</strong>
              <span class="badge ACTIVE">${a.capability}</span>
            </div>
            <p class="subtitle" style="margin:0.35rem 0;">${a.responsibility}</p>
            <div class="subtitle"><strong>Allowed Tools:</strong> ${(a.allowed_tools || []).map(t => `<code>${t}</code>`).join(', ')}</div>
          </div>
        `).join('');
      }

      const runsResp = await fetch('/api/v1/platform/runs?limit=15');
      const runsData = await runsResp.json();
      const runs = runsData.runs || [];
      const runsBox = document.getElementById('workflow-runs-container');
      if (runsBox) {
        if (!runs.length) {
          runsBox.innerHTML = '<p class="subtitle">No workflow runs recorded yet. Generate a curriculum or ask an agent to populate traces.</p>';
        } else {
          runsBox.innerHTML = `
            <table class="data-table">
              <thead>
                <tr>
                  <th>Run ID</th>
                  <th>Workflow</th>
                  <th>Status</th>
                  <th>Steps</th>
                  <th>Created At</th>
                </tr>
              </thead>
              <tbody>
                ${runs.map(r => `
                  <tr>
                    <td><code>${r.run_id}</code></td>
                    <td><strong>${r.workflow_name}</strong></td>
                    <td><span class="badge ${r.status === 'COMPLETED' ? 'COMPLETED' : 'PRACTICING'}">${r.status}</span></td>
                    <td>${(r.steps || []).length || r.step_count || 0} steps</td>
                    <td class="subtitle">${r.created_at || r.started_at || ''}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          `;
        }
      }
    }

    /* ====================================================================
       LAB View Functions
       ==================================================================== */
    async function loadLabView() {
      const sylResp = await fetch('/api/v1/platform/syllabus-coverage');
      const sylData = await sylResp.json();
      const box = document.getElementById('lab-domains-list');
      if (!box || !sylData.domains) return;
      box.innerHTML = sylData.domains.map(d => {
        return `
          <div class="card" style="margin-bottom:0.85rem;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <h3 style="margin:0;border:none;">${d.order}. ${d.title}</h3>
              <span class="badge ${d.readiness_status === 'PRIMARY_CHUNK_BACKED' ? 'COMPLETED' : 'PRACTICING'}">${d.readiness_status}</span>
            </div>
            <p class="subtitle">${d.description}</p>
            <div class="grid-2">
              <div>
                <strong>Supported Pedagogical Depths:</strong>
                <div>${(d.supported_depths || []).map(dep => `<code>${dep}</code>`).join(' &rarr; ')}</div>
              </div>
              <div>
                <strong>Linked DAG Concepts &amp; Sources:</strong>
                <div>Concepts: ${(d.concepts || []).map(c => `<code>${c.concept_id}</code> (${c.active_chunk_count} chunks)`).join(', ')}</div>
                <div class="subtitle">Sources: ${(d.primary_sources || []).join(', ')}</div>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    /* ====================================================================
       PLATFORM INTELLIGENCE View Functions
       ==================================================================== */
    function askPresetPlatformQuestion(questionText) {
      const input = document.getElementById('platform-intel-question-input');
      if (input) input.value = questionText;
      askPlatformIntelligence();
    }

    async function askPlatformIntelligence() {
      const question = document.getElementById('platform-intel-question-input').value.trim();
      const box = document.getElementById('platform-intel-response-box');
      const resp = await fetch('/api/v1/platform/intelligence/ask', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({question: question})
      });
      const data = await resp.json();
      if (!resp.ok) {
        box.innerHTML = `
          <div class="disclaimer" style="border-color:var(--danger-border);color:var(--danger-text);background:var(--danger-bg);">
            <strong>[${data.error ? data.error.code : 'REJECTED'}] ${data.title || 'Request Rejected'}:</strong>
            ${data.detail || (data.error && data.error.message) || 'Mutating or invalid request refused.'}
          </div>
        `;
        return;
      }
      const intel = data.intelligence_response || data.intelligence || data;
      box.innerHTML = `
        <div class="card" style="background:var(--bg-surface-alt);">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem;">
            <strong>Topics: ${(intel.detected_topics || []).map(t => `<code>${t}</code>`).join(', ')}</strong>
            <span class="badge COMPLETED">Read-Only Verified (${intel.run_id || 'trace'})</span>
          </div>
          <p style="margin-top:0.55rem;"><strong>Executive Summary:</strong> ${intel.executive_summary || intel.direct_answer || ''}</p>
          <div class="grid-2">
            <div>
              <strong>Measured Platform Facts:</strong>
              <ul>${(intel.measured_facts || []).map(f => `<li>${f}</li>`).join('')}</ul>
              <strong>Derived Statistics:</strong>
              <ul>${(intel.derived_statistics || []).map(s => `<li>${s}</li>`).join('')}</ul>
            </div>
            <div>
              <strong>Actionable Recommendations:</strong>
              <ul>${(intel.recommendations || []).map(r => `<li>${r}</li>`).join('')}</ul>
              <strong>Unknown / Unavailable Telemetry:</strong>
              <ul>${(intel.unknown_or_unavailable || []).map(u => `<li>${u}</li>`).join('')}</ul>
            </div>
          </div>
          <div class="subtitle" style="margin-top:0.5rem;">
            <strong>Evidence References:</strong> ${(intel.evidence_references || []).map(e => `<code>${e}</code>`).join(', ')}
          </div>
        </div>
      `;
    }

    async function loadPlatformContextSnapshot() {
      const resp = await fetch('/api/v1/platform/context');
      const snap = await resp.json();
      const box = document.getElementById('platform-context-snapshot-box');
      if (!box || !snap.knowledge_store) return;
      const ks = snap.knowledge_store;
      const vs = snap.vector_store || {};
      const lt = snap.learner_telemetry || {};
      box.innerHTML = `
        <div class="grid-3">
          <div class="stat-box">
            <div class="stat-label">Knowledge Store &amp; Sources</div>
            <div class="stat-value">${ks.total_sources} Sources</div>
            <div class="subtitle">${ks.extracted_primary_documents} extracted docs • ${ks.approved_uncollected_resources} seed backlog</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">RAG &amp; Vector Store</div>
            <div class="stat-value">${ks.curriculum_ready_chunks} Chunks</div>
            <div class="subtitle">Chroma Indexed: ${vs.indexed_chunk_count || 0} (${vs.collection_name || 'default'})</div>
          </div>
          <div class="stat-box">
            <div class="stat-label">Learner &amp; Assessment Telemetry</div>
            <div class="stat-value">${lt.tracked_learners_count || 0} Learners</div>
            <div class="subtitle">${lt.total_exercise_submissions || 0} exercise submissions</div>
          </div>
        </div>
      `;
    }

    updateThemeToggleLabel();
  </script>
</body>
</html>
"""
