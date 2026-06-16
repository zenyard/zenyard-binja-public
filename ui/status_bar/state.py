"""Pure, Qt-free mapping from run-loop facts to widget view state.

This module is the unit-tested heart of the status-bar widget. It imports no
Qt and no Binary Ninja UI, so it can be exercised under the mocks in
``tests/conftest.py``. The widget (``widget.py``) is a pure projection of what
this module produces; the driver (``driver.py``) feeds it a ``RunSnapshot`` it
reads from the Coordinator each tick.

The 9 states come straight from the design handoff
(``~/workspace/design_handoff_status_bar_v2/README.md``):
``idle · changes · extracting · uploading · server · applying · applied ·
warning · paused``.

``unregistered`` is a local addition beyond that set: the clickable "Click to
analyze with Zenyard" resting state shown when a binary is open but not yet
registered (never analyzed, or the startup prompt was cancelled). It gives the
user a discoverable way back into analysis without the Zenyard menu.

``queued`` is likewise a local addition: the server reports the binary as
waiting for an analysis slot (``BinaryStateQueued``), so the label shows
"In queue (N remaining)" instead of claiming analysis is running.

``reconnecting`` is a local addition too: the run loop retries transient
backend failures forever (it never gives up on an outage), and after a short
grace this state says so — without it an outage mid-run is indistinguishable
from a hang.
"""

from __future__ import annotations

from dataclasses import dataclass

# The canonical states. Exposed so the widget and tests share one spelling.
STATES = (
    "unregistered",
    "idle",
    "changes",
    "extracting",
    "uploading",
    "queued",
    "server",
    "reconnecting",
    "auth_error",
    "stale",
    "ready",
    "applying",
    "applied",
    "warning",
    "paused",
)

# Consecutive transient failures before an active run shows "Reconnecting…".
# Two ≈ read-timeout + one backoff after a wake/disconnect — long enough to
# swallow a single blip, short enough that an outage doesn't read as a hang.
_RECONNECT_AFTER_FAILURES = 2

# The long-running pipeline states that surface a progress read-out (as opposed
# to instantaneous/terminal states like `changes`, `idle`). `server` is included
# now that we have a server-side fraction (``server_revision / target_revision``)
# to report during off-device analysis. The widget derives its own
# busy-vs-determinate split; this just names the set.
PROGRESSFUL_STATES = frozenset({"uploading", "server", "applying", "paused"})


@dataclass(frozen=True)
class RunSnapshot:
    """Raw facts about the current run, read from the Coordinator.

    Everything here is a plain value so it can cross the thread boundary
    (background Coordinator → Qt main thread) without exposing live task
    objects. ``derive_view_state`` turns it into a ``ViewState``.
    """

    binary_registered: bool = False
    bring_up_active: bool = False
    first_revision_done: bool = False
    download_active: bool = False  # actively fetching inference pages
    download_waiting: bool = False  # polling server, analysis not ready yet
    analysis_ready: bool = False  # server done; results ready, not yet applied
    apply_active: bool = False  # a batch is being applied / queue non-empty

    # Server-side analysis progress (drives the `server` state's % bar). The
    # ratio matches the poll loop's completion test, so it reaches 1.0 exactly
    # when analysis is declared ready. ``server_revision`` is the effective
    # analysed revision level (e.g. 2.375); ``target_revision`` the denominator.
    server_revision: float = 0.0
    target_revision: int = 0

    # Server queue position while analysis hasn't started (None once running).
    # Drives the `queued` state.
    queue_position: int | None = None

    # Consecutive transient backend failures in the active phase (bring-up or
    # download). Drives the `reconnecting` state once past the grace.
    connection_failures: int = 0

    # Permanent-disposition postures (see helpers.retry.classify). The
    # Coordinator latches these when a backend call fails with a non-transient
    # disposition: ``auth_blocked`` on 401/403 (bad/expired key — analysis is
    # disabled until fixed), ``stale_binary`` on 404 (binary gone server-side).
    # Each drives its own resting state, ranked above the run states.
    auth_blocked: bool = False
    stale_binary: bool = False

    objects_uploaded: int = 0
    objects_total: int = 0
    downloaded: int = 0
    applied: int = 0
    queued: int = 0
    applied_total: int = 0  # total inferences stored on the model

    dirty_count: int = 0  # changed objects pending upload (drives `changes`)

    warning_count: int = 0


@dataclass(frozen=True)
class UsageInfo:
    """Account usage for the current billing period (background poll result).

    ``kind`` mirrors the server's ``UsageResponse`` ``oneOf`` so the widget can
    render the three cases without importing the generated client:

    - ``"limited"`` — ``percent`` is the budget used (0–∞; ≥100 = over budget).
    - ``"unlimited"`` — no quota; rendered as ``∞``.
    - ``"expired"`` — plan lapsed; rendered as ``—`` and trips the pause.
    - ``"none"`` — not yet polled / unknown; rendered as ``—``.
    """

    kind: str = "none"
    percent: int | None = None


def usage_text(usage: UsageInfo | None) -> str:
    """The string shown in the usage read-out."""

    if usage is None or usage.kind in ("none", "expired"):
        return "—"
    if usage.kind == "unlimited":
        return "∞"
    return f"{usage.percent or 0}%"


def usage_tone(usage: UsageInfo | None) -> str:
    """Colour role for the usage value: ``dim`` | ``amber`` | ``crit``."""

    if usage is None or usage.kind != "limited" or usage.percent is None:
        return "dim"
    if usage.percent >= 100:
        return "crit"
    if usage.percent >= 85:
        return "amber"
    return "dim"


def quota_blocks(usage: UsageInfo | None) -> bool:
    """Whether usage forces the run into the paused state."""

    if usage is None:
        return False
    if usage.kind == "expired":
        return True
    return usage.kind == "limited" and (usage.percent or 0) >= 100


@dataclass(frozen=True)
class ViewState:
    """What the widget renders. A pure projection of a ``RunSnapshot``."""

    state: str
    pct: int | None  # None ⇒ indeterminate / no percentage read-out
    counts: dict[str, int]
    title: str  # tooltip title row
    subtitle: str  # tooltip subtitle line
    show_pipeline: bool  # render the download/apply/queue tooltip block
    warning_count: int = 0
    pause_reason: str | None = None  # "quota" | "expired" | "manual" | None


@dataclass(frozen=True)
class MenuItem:
    """One row of the click menu. ``is_separator`` rows render as dividers."""

    label: str = ""
    key: str = ""  # emitted via actionTriggered
    shortcut: str = ""
    destructive: bool = False
    is_separator: bool = False


_SEP = MenuItem(is_separator=True)


def _pct_from(done: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(100, round(100 * done / total)))


def _server_pct(server_revision: float, target_revision: int) -> int | None:
    """Server-analysis percentage from the (float) revision level over the
    target revision. ``None`` when no target is set yet (the brief window
    before the first poll returns) — the driver then leaves the bar at its
    current value rather than dividing by zero."""

    if target_revision <= 0:
        return None
    return max(0, min(100, round(100 * server_revision / target_revision)))


def derive_view_state(
    snap: RunSnapshot, usage: UsageInfo | None = None
) -> ViewState:
    """Select the active state and compute its display fields.

    Selection is a strict cascade over what the run loop can observe today,
    then ``usage`` (from the separate background poll) can override to
    ``paused`` when the account is over budget / expired. ``extracting``,
    ``warning`` and the *manual* ``paused`` are never produced here (the run
    loop has no surface for them yet); the widget still renders them when the
    host calls ``set_state`` directly.
    """

    counts = {
        "uploaded": snap.objects_uploaded,
        "total": snap.objects_total,
        "downloaded": snap.downloaded,
        "queued": snap.queued,
        "applied": snap.applied,
        "dirty": snap.dirty_count,
        # `queued` above counts inferences awaiting local apply; this is the
        # server queue position (only meaningful in the `queued` state).
        "queue_position": snap.queue_position or 0,
    }

    # An active run takes precedence; otherwise pending edits ("changes") rank
    # ahead of the terminal `applied`/`idle` — they are newer than the last
    # applied result, and `changes` is not gated on registration (it fires on
    # any binary, first run or re-run, the moment objects are dirty).
    # An outage outranks the run states it overlays: a frozen bar or stale
    # queue position is exactly the looks-like-a-hang impression this kills.
    if snap.auth_blocked:
        # Bad/expired key: analysis is disabled until fixed. Ranks above the
        # run/outage states — retrying can't help, so "Reconnecting…" would
        # mislead. (See the Coordinator's auth-blocked posture.)
        state = "auth_error"
    elif snap.stale_binary:
        # Binary gone server-side (404): the run can't proceed against it.
        state = "stale"
    elif snap.connection_failures >= _RECONNECT_AFTER_FAILURES and (
        snap.bring_up_active or snap.download_active or snap.download_waiting
    ):
        state = "reconnecting"
    elif snap.bring_up_active:
        state = "uploading"
    elif snap.download_active or snap.apply_active:
        state = "applying"
    elif snap.download_waiting and snap.queue_position is not None:
        state = "queued"
    elif snap.download_waiting:
        state = "server"
    elif snap.dirty_count > 0:
        state = "changes"
    elif snap.analysis_ready:
        # Server finished analysing but auto-apply is off: results are waiting.
        state = "ready"
    elif snap.first_revision_done and snap.applied_total > 0:
        state = "applied"
    elif not snap.binary_registered:
        # Open but never registered (or the startup prompt was cancelled):
        # offer a clickable "Click to analyze" rather than a dead `idle`.
        state = "unregistered"
    else:
        state = "idle"

    # Quota/expired usage wins over the run states — but not over a hard
    # disabled posture (bad key / stale binary), which the user must act on.
    pause_reason: str | None = None
    if quota_blocks(usage) and state not in ("auth_error", "stale"):
        state = "paused"
        pause_reason = (
            "expired" if usage and usage.kind == "expired" else ("quota")
        )

    if state == "uploading":
        pct = _pct_from(snap.objects_uploaded, snap.objects_total)
    elif state == "server":
        pct = _server_pct(snap.server_revision, snap.target_revision)
    else:
        pct = None

    title, subtitle = tooltip_copy(
        state,
        pct,
        counts,
        snap.applied_total,
        snap.warning_count,
        pause_reason,
        usage,
    )

    return ViewState(
        state=state,
        pct=pct,
        counts=counts,
        title=title,
        subtitle=subtitle,
        show_pipeline=(state == "applying"),
        warning_count=snap.warning_count,
        pause_reason=pause_reason,
    )


def tooltip_copy(
    state: str,
    pct: int | None,
    counts: dict[str, int],
    applied_total: int = 0,
    warning_count: int = 0,
    pause_reason: str | None = None,
    usage: UsageInfo | None = None,
) -> tuple[str, str]:
    """Per-state tooltip title + subtitle (design handoff §Interactions).

    Works from the plain contract values so both ``derive_view_state`` and the
    widget (which only has ``set_counts`` data) can produce identical copy.
    """

    total = counts.get("total", 0)
    uploaded = counts.get("uploaded", 0)
    applied = applied_total or counts.get("applied", 0)
    dirty = counts.get("dirty", 0)

    if state == "unregistered":
        return (
            "Analyze with Zenyard",
            "This binary hasn't been analyzed yet. Click to start analyzing "
            "it with Zenyard.",
        )
    if state == "changes":
        return (
            "Changes detected",
            f"{dirty} objects changed since the last run. "
            "Click to extract and upload."
            if dirty
            else "Changes detected. Click to extract and upload.",
        )
    if state == "extracting":
        return (
            "Extracting objects",
            "Reading changed functions and serializing objects for upload…",
        )
    if state == "uploading":
        return (
            f"Uploading objects · {pct or 0}%",
            f"Sending binary objects to Zenyard · {uploaded} / {total}",
        )
    if state == "queued":
        n = counts.get("queue_position", 0)
        return (
            "In queue",
            f"Analysis hasn't started yet · {n} ahead in the queue.",
        )
    if state == "server":
        head = "Analyzing on server"
        if pct is not None:
            head = f"{head} · {pct}%"
        return (
            head,
            f"{uploaded} objects uploaded. Waiting for inferences…",
        )
    if state == "reconnecting":
        return (
            "Connection lost",
            "Can't reach the Zenyard server. Retrying — the run resumes "
            "automatically once the connection is back.",
        )
    if state == "auth_error":
        return (
            "Authentication failed",
            "Zenyard couldn't authenticate — your API key is missing, "
            "invalid, or expired. Analysis is paused until you update it.",
        )
    if state == "stale":
        return (
            "Binary not found",
            "This binary no longer exists on the Zenyard server. Re-run "
            "analysis to register it again.",
        )
    if state == "ready":
        return (
            "Analysis ready",
            "Analysis complete. Click to download and apply inferences.",
        )
    if state == "applying":
        return (
            "Applying results",
            "Downloading and applying results to the database.",
        )
    if state == "applied":
        return (
            "Latest results applied",
            f"{applied} results applied · just now.",
        )
    if state == "warning":
        return (
            f"Finished with {warning_count} warnings",
            f"{warning_count} functions could not be fully analyzed.",
        )
    if state == "paused":
        if pause_reason == "expired":
            return (
                "Plan expired",
                "Your plan has expired. Runs are paused until you renew or "
                "upgrade your plan.",
            )
        if pause_reason == "quota":
            used = usage.percent if usage and usage.percent is not None else 0
            return (
                "Usage limit reached",
                f"You've used {used}% of your monthly quota. Runs are paused "
                "until usage resets or you upgrade your plan.",
            )
        return (
            f"Paused at {pct or 0}%" if pct is not None else "Paused",
            "Run held. Click to resume.",
        )
    # idle
    return (
        "Zenyard · Ready",
        f"No analysis running. Last run applied {applied} inferences."
        if applied
        else "No analysis running.",
    )


# ── State label / color hints used by the widget (design handoff §States) ────

# label text, label color role: "dim" | "normal" | "amber" | "accent"
STATE_LABEL: dict[str, tuple[str, str]] = {
    "unregistered": ("Click to analyze with Zenyard", "accent"),
    "idle": ("Ready", "dim"),
    "changes": ("Changes detected · click to upload", "accent"),
    "extracting": ("Extracting objects", "normal"),
    "uploading": ("Uploading objects", "normal"),
    "queued": ("In queue", "normal"),
    "server": ("Analyzing on server", "normal"),
    "reconnecting": ("Reconnecting…", "amber"),
    "auth_error": ("Auth error · check API key", "amber"),
    "stale": ("Binary not found on server", "amber"),
    "ready": ("Analysis ready · click to apply", "accent"),
    "applying": ("Applying results", "normal"),
    "applied": ("Latest results applied", "normal"),
    "warning": ("Finished with {n} warnings", "amber"),
    "paused": ("Paused", "dim"),
}


def state_label(
    state: str,
    pct: int | None = None,
    warning_count: int = 0,
    queue_position: int | None = None,
) -> tuple[str, str]:
    """Final (text, color-role) for the status label, dynamic suffixes folded in.

    Kept Qt-free here (alongside the rest of the copy logic) so the suffixing is
    unit-testable. The `server` state shows its progress as a label suffix —
    "Analyzing on server… 79%" — because its bar runs the indeterminate busy
    marquee and carries no value. The `queued` state likewise rides its queue
    position — "In queue (5 remaining)".
    """

    text, role = STATE_LABEL.get(state, ("Zenyard", "dim"))
    if state == "warning":
        text = text.format(n=warning_count)
    elif state == "server" and pct is not None:
        text = f"{text} {pct}%"
    elif state == "queued" and queue_position is not None:
        text = f"{text} ({queue_position} remaining)"
    return text, role
