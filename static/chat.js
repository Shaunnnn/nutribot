document.addEventListener("DOMContentLoaded", function () {
  const chatbox = document.getElementById("chatbox");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const attachBtn = document.getElementById("attach");
  const attachMenu = document.getElementById("attachMenu");
  const imageInput = document.getElementById("imageInput");
  const clearBtn = document.getElementById("clearChat");

  // Elements only exist once the chat panel has been rendered on the
  // page (menu.html) — bail out quietly everywhere else.
  if (!chatbox || !inputEl || !sendBtn) return;

  let pendingAttachMode = null; // "pantry" | "meal"

  function clearEmptyState() {
    const empty = chatbox.querySelector(".chat-empty");
    if (empty) empty.remove();
  }

  function addMessage(sender, text) {
    clearEmptyState();
    const msgDiv = document.createElement("div");
    msgDiv.className = sender === "You" ? "message user" : "message bot";
    msgDiv.textContent = text;
    chatbox.appendChild(msgDiv);
    chatbox.scrollTop = chatbox.scrollHeight;
  }

  function showTypingIndicator() {
    if (document.getElementById("typing-indicator")) return;
    const msgDiv = document.createElement("div");
    msgDiv.id = "typing-indicator";
    msgDiv.className = "message bot typing-indicator";
    msgDiv.innerHTML = `<span class="typing-dots"><span>.</span><span>.</span><span>.</span></span>`;
    chatbox.appendChild(msgDiv);
    chatbox.scrollTop = chatbox.scrollHeight;
  }

  function hideTypingIndicator() {
    const el = document.getElementById("typing-indicator");
    if (el) el.remove();
  }

  function addMealLogPrompt(dish, calories) {
    clearEmptyState();
    const label = dish || "this meal";
    const wrap = document.createElement("div");
    wrap.className = "message bot meal-log-prompt";

    const text = document.createElement("div");
    text.textContent = calories
      ? `Log "${label}" (~${calories} kcal) to today's food log?`
      : `Log "${label}" to today's food log?`;
    wrap.appendChild(text);

    const actions = document.createElement("div");
    actions.className = "meal-log-actions";

    const logBtn = document.createElement("button");
    logBtn.type = "button";
    logBtn.className = "btn btn-sm btn-primary";
    logBtn.textContent = "Log it";

    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "btn btn-sm btn-outline";
    dismissBtn.textContent = "Just asking";

    actions.appendChild(logBtn);
    actions.appendChild(dismissBtn);
    wrap.appendChild(actions);

    chatbox.appendChild(wrap);
    chatbox.scrollTop = chatbox.scrollHeight;

    logBtn.addEventListener("click", async function () {
      logBtn.disabled = true;
      dismissBtn.disabled = true;
      try {
        const res = await fetch("/log_photo_meal", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ description: dish, calories: calories }),
        });
        const result = await res.json();
        text.textContent = result.calories
          ? `Logged "${result.description}" (~${result.calories} kcal) to today's food log.`
          : `Logged "${result.description}" to today's food log.`;
        actions.remove();
      } catch (err) {
        console.error(err);
        logBtn.disabled = false;
        dismissBtn.disabled = false;
      }
    });

    dismissBtn.addEventListener("click", function () {
      text.textContent = "Okay, not logged.";
      actions.remove();
    });
  }

  async function sendMessage() {
    const message = inputEl.value.trim();
    if (!message) return;

    addMessage("You", message);
    inputEl.value = "";

    const typingTimeout = setTimeout(showTypingIndicator, 1000);

    try {
      const res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const data = await res.json();
      clearTimeout(typingTimeout);
      hideTypingIndicator();
      addMessage("Bot", data.reply);
    } catch (err) {
      console.error(err);
      clearTimeout(typingTimeout);
      hideTypingIndicator();
      addMessage("Bot", "Sorry, something went wrong. Please try again.");
    }
  }

  sendBtn.addEventListener("click", sendMessage);
  inputEl.addEventListener("keypress", function (e) {
    if (e.key === "Enter") sendMessage();
  });

  if (clearBtn) {
    clearBtn.addEventListener("click", async function () {
      chatbox.innerHTML = '<div class="chat-empty">Start a conversation with NutriBot!</div>';
      try {
        await fetch("/clear_chat", { method: "POST" });
      } catch (err) {
        console.error(err);
      }
    });
  }

  // ---- attach-photo: two modes sharing one file input ----
  if (attachBtn && attachMenu && imageInput) {
    attachBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      attachMenu.classList.toggle("open");
    });

    document.addEventListener("click", function () {
      attachMenu.classList.remove("open");
    });
    attachMenu.addEventListener("click", function (e) {
      e.stopPropagation();
    });

    attachMenu.querySelectorAll("button[data-mode]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        pendingAttachMode = btn.getAttribute("data-mode");
        attachMenu.classList.remove("open");
        imageInput.click();
      });
    });

    imageInput.addEventListener("change", async function () {
      if (!imageInput.files.length || !pendingAttachMode) return;

      const file = imageInput.files[0];
      const mode = pendingAttachMode;
      pendingAttachMode = null;

      addMessage(
        "You",
        mode === "pantry" ? "📦 Uploaded a pantry photo" : "🍽️ Uploaded a meal photo"
      );

      const formData = new FormData();
      formData.append("photo", file);

      const endpoint = mode === "pantry" ? "/upload_grocery" : "/chat_photo";

      try {
        showTypingIndicator();
        const res = await fetch(endpoint, { method: "POST", body: formData });
        const data = await res.json();
        hideTypingIndicator();
        addMessage("Bot", data.reply || "Sorry, I couldn't process that image.");
        if (mode === "meal" && data.loggable) {
          addMealLogPrompt(data.dish, data.calories);
        }
      } catch (err) {
        console.error(err);
        hideTypingIndicator();
        addMessage("Bot", "Sorry, there was a problem uploading the image.");
      }
      imageInput.value = "";
    });
  }
});
