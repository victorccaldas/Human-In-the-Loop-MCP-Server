"""
Telegram Mini App HTML template for the Human-in-the-Loop MCP Server.

The template is a single self-contained HTML page that:
  - Displays the prompt text (title + body)
  - Shows clickable chip buttons for each active custom prompt
  - Provides a textarea for free-form response
  - On submit, POSTs the answer to the local Python HTTP server

SESSION_PLACEHOLDER is replaced at serve time with a JSON object containing:
  { token, title, prompt, customPrompts, submitUrl }
"""

MINIAPP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Human-in-the-Loop</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script>
    // ── Injected by Python at serve time ──────────────────────────────────
    const SESSION = __SESSION_JSON__;
    // ─────────────────────────────────────────────────────────────────────
  </script>
  <style>
    :root {
      --tg-bg: var(--tg-theme-bg-color, #ffffff);
      --tg-text: var(--tg-theme-text-color, #222222);
      --tg-hint: var(--tg-theme-hint-color, #888888);
      --tg-link: var(--tg-theme-link-color, #0088cc);
      --tg-btn: var(--tg-theme-button-color, #0088cc);
      --tg-btn-text: var(--tg-theme-button-text-color, #ffffff);
      --tg-secondary-bg: var(--tg-theme-secondary-bg-color, #f4f4f5);
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--tg-bg);
      color: var(--tg-text);
      font-size: 15px;
      line-height: 1.5;
      padding: 16px;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    /* Title */
    #title {
      font-size: 18px;
      font-weight: 700;
      color: var(--tg-text);
    }

    /* Agent role badge */
    #agent-role {
      display: none;
      font-size: 12px;
      font-weight: 600;
      color: var(--tg-btn);
      background: var(--tg-secondary-bg);
      border-radius: 999px;
      padding: 3px 10px;
      width: fit-content;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    /* Prompt text block */
    #prompt-box {
      background: var(--tg-secondary-bg);
      border-radius: 10px;
      padding: 12px 14px;
      font-size: 14px;
      color: var(--tg-text);
      word-break: break-word;
      max-height: 200px;
      overflow-y: auto;
      flex-shrink: 0;
      line-height: 1.5;
      user-select: text;
      -webkit-user-select: text;
    }
    /* Markdown rendering styles for agent prompt */
    #prompt-box h1, #prompt-box h2, #prompt-box h3,
    #prompt-box h4, #prompt-box h5, #prompt-box h6 {
      margin: 8px 0 4px 0;
      line-height: 1.3;
    }
    #prompt-box h1 { font-size: 1.3em; }
    #prompt-box h2 { font-size: 1.15em; }
    #prompt-box h3 { font-size: 1.05em; }
    #prompt-box p {
      margin: 4px 0;
    }
    #prompt-box ul, #prompt-box ol {
      margin: 4px 0;
      padding-left: 20px;
    }
    #prompt-box li {
      margin: 2px 0;
    }
    #prompt-box code {
      background: rgba(0, 0, 0, 0.06);
      border-radius: 4px;
      padding: 1px 5px;
      font-size: 0.92em;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    }
    #prompt-box pre {
      background: rgba(0, 0, 0, 0.06);
      border-radius: 6px;
      padding: 8px 10px;
      margin: 6px 0;
      overflow-x: auto;
    }
    #prompt-box pre code {
      background: transparent;
      padding: 0;
    }
    #prompt-box blockquote {
      border-left: 3px solid var(--tg-hint);
      margin: 6px 0;
      padding: 4px 10px;
      color: var(--tg-hint);
    }
    #prompt-box strong {
      font-weight: 700;
    }
    #prompt-box em {
      font-style: italic;
    }
    #prompt-box a {
      color: var(--tg-link);
      text-decoration: none;
    }
    #prompt-box a:hover {
      text-decoration: underline;
    }
    #prompt-box hr {
      border: none;
      border-top: 1px solid var(--tg-hint);
      margin: 8px 0;
    }
    #prompt-box table {
      border-collapse: collapse;
      margin: 6px 0;
      font-size: 0.92em;
    }
    #prompt-box table th, #prompt-box table td {
      border: 1px solid var(--tg-hint);
      padding: 4px 8px;
    }
    #prompt-box table th {
      background: rgba(0, 0, 0, 0.04);
      font-weight: 600;
    }

    /* Textarea */
    #answer {
      width: 100%;
      flex: 1 1 auto;
      min-height: 120px;
      resize: vertical;
      border: 1.5px solid var(--tg-hint);
      border-radius: 10px;
      padding: 12px 14px;
      font-size: 14px;
      font-family: inherit;
      background: var(--tg-bg);
      color: var(--tg-text);
      outline: none;
      transition: border-color 0.15s;
    }

    #answer:focus {
      border-color: var(--tg-btn);
    }

    /* Divider between textarea and checkboxes */
    .section-divider {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--tg-hint);
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .section-divider::before,
    .section-divider::after {
      content: "";
      flex: 1;
      height: 1px;
      background: var(--tg-hint);
      opacity: 0.35;
    }

    /* Checkboxes container */
    #checkboxes {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    /* Individual checkbox row */
    .check-row {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 8px 12px;
      border-radius: 8px;
      cursor: pointer;
      transition: background 0.12s;
      -webkit-tap-highlight-color: transparent;
      user-select: none;
    }

    .check-row:active,
    .check-row.checked {
      background: var(--tg-secondary-bg);
    }

    .check-row input[type="checkbox"] {
      appearance: none;
      -webkit-appearance: none;
      width: 20px;
      height: 20px;
      min-width: 20px;
      border: 2px solid var(--tg-hint);
      border-radius: 5px;
      background: var(--tg-bg);
      cursor: pointer;
      position: relative;
      margin-top: 1px;
      transition: background 0.12s, border-color 0.12s;
    }

    .check-row input[type="checkbox"]:checked {
      background: var(--tg-btn);
      border-color: var(--tg-btn);
    }

    .check-row input[type="checkbox"]:checked::after {
      content: "";
      position: absolute;
      left: 4px;
      top: 1px;
      width: 8px;
      height: 12px;
      border: 2px solid var(--tg-btn-text);
      border-top: none;
      border-left: none;
      transform: rotate(45deg);
    }

    .check-label {
      font-size: 14px;
      line-height: 1.5;
      word-break: break-word;
      cursor: pointer;
    }

    /* Submit button */
    #submit-btn {
      width: 100%;
      background: var(--tg-btn);
      color: var(--tg-btn-text);
      border: none;
      border-radius: 10px;
      padding: 14px;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      transition: opacity 0.15s;
      -webkit-tap-highlight-color: transparent;
    }

    #submit-btn:active { opacity: 0.75; }
    #submit-btn:disabled { opacity: 0.45; cursor: not-allowed; }

    /* Error toast */
    #error-toast {
      display: none;
      background: #d93025;
      color: #fff;
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 13px;
      text-align: center;
    }

    /* Success state */
    #success-msg {
      display: none;
      text-align: center;
      font-size: 16px;
      font-weight: 600;
      color: var(--tg-btn);
      padding: 12px;
    }
  </style>
</head>
<body>

  <div id="agent-role"></div>
  <div id="title"></div>
  <div id="prompt-box"></div>

  <!-- Free-form textarea -->
  <textarea id="answer" placeholder="Type your response here… (optional if selecting below)"></textarea>

  <!-- Divider + checkboxes for active custom prompts -->
  <div id="presets-section" style="display:none; flex-direction:column; gap:8px;">
    <div class="section-divider">Quick select</div>
    <div id="checkboxes"></div>
  </div>

  <div id="error-toast"></div>
  <div id="success-msg">✅ Response submitted!</div>
  <button id="submit-btn">Submit</button>

  <script>
    (function () {
      var tgApp = window.Telegram && window.Telegram.WebApp;
      if (tgApp) {
        tgApp.ready();
        tgApp.expand();
      }

      // ── Populate UI ──────────────────────────────────────────────────
      document.getElementById("title").textContent = SESSION.title || "";

      var roleEl = document.getElementById("agent-role");
      if (SESSION.agentRole) {
        roleEl.textContent = SESSION.agentRole;
        roleEl.style.display = "block";
      }

      // Render agent prompt with markdown formatting
      var promptText = SESSION.prompt || "";
      if (typeof marked !== "undefined" && promptText) {
        marked.setOptions({ breaks: true, gfm: true });
        document.getElementById("prompt-box").innerHTML = marked.parse(promptText);
      } else {
        document.getElementById("prompt-box").textContent = promptText;
      }

      var textarea  = document.getElementById("answer");
      var presetsSection = document.getElementById("presets-section");
      var checkboxesContainer = document.getElementById("checkboxes");
      var checkboxEls = [];

      if (SESSION.prompts && SESSION.prompts.length > 0) {
        presetsSection.style.display = "flex";

        SESSION.prompts.forEach(function (p, idx) {
          var text    = p.text;
          var checked = !!p.checked;

          var row = document.createElement("label");
          row.className = "check-row" + (checked ? " checked" : "");
          row.htmlFor = "cb-" + idx;

          var cb = document.createElement("input");
          cb.type = "checkbox";
          cb.id = "cb-" + idx;
          cb.dataset.text = text;
          cb.checked = checked;

          cb.addEventListener("change", function () {
            row.classList.toggle("checked", cb.checked);
          });

          var label = document.createElement("span");
          label.className = "check-label";
          label.textContent = text;

          row.appendChild(cb);
          row.appendChild(label);
          checkboxesContainer.appendChild(row);
          checkboxEls.push(cb);
        });
      }

      // Auto-focus textarea
      textarea.focus();

      // ── Submit ───────────────────────────────────────────────────────
      var submitBtn   = document.getElementById("submit-btn");
      var errorToast  = document.getElementById("error-toast");
      var successMsg  = document.getElementById("success-msg");

      function showError(msg) {
        errorToast.textContent = msg;
        errorToast.style.display = "block";
        submitBtn.disabled = false;
        setTimeout(function () { errorToast.style.display = "none"; }, 4000);
      }

      function buildAnswer() {
        var parts = [];
        var typed = textarea.value.trim();
        if (typed) {
          parts.push(typed);
        }
        checkboxEls.forEach(function (cb) {
          if (cb.checked) {
            parts.push(cb.dataset.text);
          }
        });
        return parts.join("\\n\\n");
      }

      submitBtn.addEventListener("click", function () {
        var answer = buildAnswer();
        if (!answer) {
          showError("Please type a response or select at least one option below.");
          return;
        }

        submitBtn.disabled = true;
        errorToast.style.display = "none";

        fetch(SESSION.submitUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Token": SESSION.token
          },
          body: JSON.stringify({ answer: answer })
        })
        .then(function (resp) {
          if (!resp.ok) {
            return resp.text().then(function (t) {
              throw new Error("Server error " + resp.status + ": " + t);
            });
          }
          // Success path — show confirmation and close
          textarea.style.display = "none";
          presetsSection.style.display = "none";
          submitBtn.style.display = "none";
          successMsg.style.display = "block";
          if (tgApp) {
            // Note: sendData() is only for ReplyKeyboard Mini Apps.
            // For InlineKeyboard Mini Apps (our case) we just close.
            setTimeout(function () { tgApp.close(); }, 800);
          }
        })
        .catch(function (err) {
          showError("Failed to submit: " + err.message);
        });
      });

      // Ctrl+Enter / Cmd+Enter → submit
      textarea.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          submitBtn.click();
        }
      });

    })();
  </script>
</body>
</html>
"""
