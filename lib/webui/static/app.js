const POLL_INTERVAL_MS = 2000;

// A check drives a real browser and retries hard against Southwest's intermittent rejections, so
// it can legitimately run for minutes. Stop polling well past that rather than never.
const MAX_POLL_MS = 15 * 60 * 1000;

// A single failed poll is usually a blip, not a dead server, so don't abandon a running check on
// the first one.
const MAX_CONSECUTIVE_POLL_FAILURES = 3;

const RELOAD_WARNING =
    "Reload config.json so the check-in daemon picks up your changes?\n\n" +
    "Any check-in or fare check the daemon has in progress will be interrupted and restarted " +
    "with the new configuration. The web UI itself is unaffected.";

function setBusy(button, busy) {
    button.disabled = busy;
    button.dataset.busy = busy ? "true" : "false";
}

function pollJob(jobId, statusEl, onDone) {
    statusEl.textContent = "Checking fares… this drives a real browser and takes 30-90s.";

    const startedAt = Date.now();
    let consecutiveFailures = 0;

    const interval = setInterval(async () => {
        if (Date.now() - startedAt > MAX_POLL_MS) {
            clearInterval(interval);
            statusEl.textContent =
                "This check is taking longer than expected. It may still be running — " +
                "refresh the page in a few minutes to see the result.";
            onDone(false);
            return;
        }

        let resp;
        try {
            resp = await fetch(`/api/jobs/${jobId}`);
        } catch (err) {
            if (++consecutiveFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
                clearInterval(interval);
                statusEl.textContent = `Lost contact with the server: ${err}`;
                onDone(false);
            }
            return;
        }

        if (!resp.ok) {
            if (++consecutiveFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
                clearInterval(interval);
                statusEl.textContent = `Could not read job status (HTTP ${resp.status}).`;
                onDone(false);
            }
            return;
        }

        consecutiveFailures = 0;
        const job = await resp.json();

        if (job.status === "done") {
            clearInterval(interval);
            statusEl.textContent = job.error
                ? `Finished with errors: ${job.error}`
                : "Done — refreshing…";
            onDone(true);
        } else if (job.status === "error") {
            clearInterval(interval);
            statusEl.textContent = `Failed: ${job.error || "unknown error"}`;
            onDone(false);
        }
    }, POLL_INTERVAL_MS);
}

async function startCheck(url, statusEl, button) {
    setBusy(button, true);
    statusEl.textContent = "Starting…";

    let resp;
    try {
        resp = await fetch(url, { method: "POST" });
    } catch (err) {
        statusEl.textContent = `Could not start the check: ${err}`;
        setBusy(button, false);
        return;
    }

    if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        statusEl.textContent = `Error: ${body.description || resp.statusText}`;
        setBusy(button, false);
        return;
    }

    const { job_id: jobId } = await resp.json();
    pollJob(jobId, statusEl, (succeeded) => {
        if (succeeded) {
            setTimeout(() => window.location.reload(), 800);
        } else {
            setBusy(button, false);
        }
    });
}

/** Render stored UTC timestamps in the viewer's own timezone. */
function localizeTimestamps() {
    document.querySelectorAll("[data-timestamp]").forEach((el) => {
        const raw = el.dataset.timestamp;
        if (!raw) return;

        const parsed = new Date(raw);
        if (Number.isNaN(parsed.getTime())) return;

        const formatted = parsed.toLocaleString(undefined, {
            dateStyle: "medium",
            timeStyle: "short",
        });
        el.textContent = el.classList.contains("checked-at")
            ? `Last checked: ${formatted}`
            : formatted;
    });
}

async function reloadConfig(button, statusEl) {
    if (!window.confirm(RELOAD_WARNING)) {
        return;
    }

    setBusy(button, true);
    statusEl.textContent = "Reloading configuration…";

    try {
        const resp = await fetch("/api/reload", { method: "POST" });
        statusEl.textContent = resp.ok
            ? "Configuration reload requested."
            : `Could not request a reload (HTTP ${resp.status}).`;
    } catch (err) {
        statusEl.textContent = `Could not reach the server: ${err}`;
    } finally {
        setBusy(button, false);
        setTimeout(() => {
            statusEl.textContent = "";
        }, 5000);
    }
}

document.addEventListener("DOMContentLoaded", () => {
    localizeTimestamps();

    const reloadBtn = document.getElementById("reload-config-btn");
    if (reloadBtn) {
        reloadBtn.addEventListener("click", () => {
            reloadConfig(reloadBtn, document.getElementById("reload-status"));
        });
    }

    document.querySelectorAll(".check-btn").forEach((button) => {
        button.addEventListener("click", () => {
            const confirmation = button.dataset.confirmation;
            startCheck(
                `/api/check/${confirmation}`,
                document.getElementById(`status-${confirmation}`),
                button,
            );
        });
    });

    const checkAllBtn = document.getElementById("check-all-btn");
    if (checkAllBtn) {
        checkAllBtn.addEventListener("click", () => {
            startCheck("/api/check-all", document.getElementById("check-all-status"), checkAllBtn);
        });
    }

    document.querySelectorAll(".watch-check-btn").forEach((button) => {
        button.addEventListener("click", () => {
            const watchId = button.dataset.watchId;
            startCheck(
                `/api/watch-check/${watchId}`,
                document.getElementById(`watch-status-${watchId}`),
                button,
            );
        });
    });

    const watchCheckAllBtn = document.getElementById("watch-check-all-btn");
    if (watchCheckAllBtn) {
        watchCheckAllBtn.addEventListener("click", () => {
            startCheck(
                "/api/watch-check-all",
                document.getElementById("watch-check-all-status"),
                watchCheckAllBtn,
            );
        });
    }

    // Built here (not as an inline onsubmit) so the confirmation number is only ever used as a
    // runtime string value, never interpolated into JS source text
    const deleteForm = document.querySelector(".delete-reservation-form");
    if (deleteForm) {
        deleteForm.addEventListener("submit", (event) => {
            const confirmation = deleteForm.dataset.confirmation;
            const message =
                `Remove ${confirmation} from config.json? ` +
                "Its saved fare results will also be cleared.";
            if (!window.confirm(message)) {
                event.preventDefault();
            }
        });
    }

    const deleteWatchForm = document.querySelector(".delete-watch-form");
    if (deleteWatchForm) {
        deleteWatchForm.addEventListener("submit", (event) => {
            const watchId = deleteWatchForm.dataset.watchId;
            const message =
                `Remove fare watch ${watchId} from config.json? ` +
                "Its saved results will also be cleared.";
            if (!window.confirm(message)) {
                event.preventDefault();
            }
        });
    }
});
