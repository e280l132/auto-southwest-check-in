const POLL_INTERVAL_MS = 2000;

const DAEMON_WARNING =
    "The check-in daemon is running and uses its own browser session.\n\n" +
    "Running a check now may trip Southwest's rate limiting. Continue?";

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

    const interval = setInterval(async () => {
        let resp;
        try {
            resp = await fetch(`/api/jobs/${jobId}`);
        } catch (err) {
            clearInterval(interval);
            statusEl.textContent = `Lost contact with the server: ${err}`;
            onDone(false);
            return;
        }

        if (!resp.ok) {
            clearInterval(interval);
            statusEl.textContent = `Could not read job status (HTTP ${resp.status}).`;
            onDone(false);
            return;
        }

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
    if (button.dataset.daemonRunning === "true" && !window.confirm(DAEMON_WARNING)) {
        return;
    }

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
});
