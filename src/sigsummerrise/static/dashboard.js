(function () {
  "use strict";

  var POLL_MS = 3000;
  var TICK_MS = 1000;
  var script = document.currentScript;
  var botName = script && script.getAttribute("data-bot-name") ? script.getAttribute("data-bot-name") : "Bot";
  var statusEl = document.getElementById("bot-status");
  var messageEl = document.getElementById("bot-status-message");
  var draftPanel = document.getElementById("live-draft");
  var draftText = document.getElementById("live-draft-text");
  var subtitleEl = document.querySelector(".subtitle");
  if (!statusEl || !messageEl) {
    return;
  }

  var pollTimer = null;
  var tickTimer = null;
  var lastPayload = null;
  var elapsedBase = null;
  var elapsedAt = null;

  function formatElapsed(seconds) {
    var s = Math.max(0, seconds);
    var m = Math.floor(s / 60);
    var r = s % 60;
    return m + "m " + r + "s";
  }

  function updateElapsedMessage() {
    if (!lastPayload || lastPayload.status.state !== "working" || elapsedBase === null || elapsedAt === null) {
      return;
    }
    var seconds = elapsedBase + Math.floor((Date.now() - elapsedAt) / 1000);
    var text = lastPayload.status.message;
    var match = text.match(/\(elapsed: [^)]+\)/);
    if (match) {
      messageEl.textContent = text.replace(match[0], "(elapsed: " + formatElapsed(seconds) + ").");
    }
  }

  function setStatus(payload) {
    lastPayload = payload;
    var working = payload.status.state === "working";
    statusEl.classList.toggle("status-working", working);
    statusEl.classList.toggle("status-idle", !working);
    messageEl.textContent = payload.status.message;
    if (working && payload.status.elapsed_seconds !== null && payload.status.elapsed_seconds !== undefined) {
      elapsedBase = payload.status.elapsed_seconds;
      elapsedAt = Date.now();
      if (!tickTimer) {
        tickTimer = window.setInterval(updateElapsedMessage, TICK_MS);
      }
    } else {
      elapsedBase = null;
      elapsedAt = null;
      if (tickTimer) {
        window.clearInterval(tickTimer);
        tickTimer = null;
      }
    }
  }

  function updateStats(stats) {
    Object.keys(stats).forEach(function (key) {
      var el = document.querySelector('[data-live="' + key + '"]');
      if (!el) {
        return;
      }
      if (key === "redaction_pct_last_n") {
        el.textContent = stats[key] + "%";
      } else {
        el.textContent = stats[key];
      }
    });
  }

  function updateQuota(quota) {
    var used = document.querySelector('[data-live="quota_used"]');
    var limit = document.querySelector('[data-live="quota_limit"]');
    if (used) {
      used.textContent = quota.used;
    }
    if (limit) {
      limit.textContent = quota.limit;
    }
  }

  function updateMembers(members) {
    members.forEach(function (member) {
      var row = document.querySelector('tr[data-member="' + CSS.escape(member.display_name) + '"]');
      if (!row) {
        return;
      }
      if (!member.opted_in) {
        return;
      }
      ["body_count", "llm_calls_24h", "llm_calls_7d", "cost_display", "rank_display"].forEach(function (field) {
        var cell = row.querySelector('[data-live-member="' + field + '"]');
        if (!cell) {
          return;
        }
        if (field === "rank_display" && member.rank_display.indexOf("hog") !== -1) {
          var parts = member.rank_display.split(" ");
          cell.innerHTML = parts[0] + ' <span class="roast">this week\'s hog</span>';
        } else {
          cell.textContent = member[field];
        }
      });
    });
  }

  function updateDraft(draft) {
    if (!draftPanel || !draftText) {
      return;
    }
    if (draft) {
      draftPanel.classList.remove("hidden");
      draftText.textContent = draft;
    } else {
      draftPanel.classList.add("hidden");
      draftText.textContent = "";
    }
  }

  function updateSubtitle(payload) {
    if (!subtitleEl) {
      return;
    }
    var model = payload.model || "—";
    var provider = payload.last_provider || "—";
    subtitleEl.innerHTML =
      "Model: <strong></strong> · Last provider: <strong></strong>";
    var strongs = subtitleEl.querySelectorAll("strong");
    if (strongs.length >= 2) {
      strongs[0].textContent = model;
      strongs[1].textContent = provider;
    }
  }

  function poll() {
    fetch("/api/live", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (response.status === 401) {
          if (pollTimer) {
            window.clearInterval(pollTimer);
            pollTimer = null;
          }
          messageEl.textContent = botName + " is awaiting messages.";
          statusEl.classList.remove("status-working");
          statusEl.classList.add("status-idle");
          return null;
        }
        if (!response.ok) {
          return null;
        }
        return response.json();
      })
      .then(function (payload) {
        if (!payload) {
          return;
        }
        setStatus(payload);
        updateSubtitle(payload);
        updateDraft(payload.draft || null);
        updateStats(payload.stats);
        updateQuota(payload.quota);
        updateMembers(payload.members);
      })
      .catch(function () {
        /* ignore transient network errors */
      });
  }

  poll();
  pollTimer = window.setInterval(poll, POLL_MS);
})();
