from __future__ import annotations

import os
import hashlib
import pathlib
import threading
import typing as ty
from uuid import UUID
from binaryninja import (
    BinaryView,
    execute_on_main_thread_and_wait,
)
from binaryninja.log import Logger
from ..configuration import get_preferences, save_show_initial_upload_message
from ..api_client import LARGE_UPLOAD_TIMEOUT
from ..helpers.retry import (
    Disposition,
    RetryPolicy,
    _GaveUp,
    call_backend,
)
from ..model import Model
from ..helpers.revision_api import submit_revision
from ..helpers.sections import (
    IgnoredSections,
    get_section_compressed,
    get_platform,
    is_swift_binary,
)
from ..helpers.log import (
    log_debug,
    log_error,
    log_info,
    log_warn,
)
from ..zenyard_client import BinariesApi
from ..zenyard_client.models import (
    BinaryDetails,
    FinishAndAnalyzeCurrentRevisionBody,
    OriginalLanguages,
    PostBinaryBody,
)
from .base import CancellableTask, TaskCancelled
from .revision_upload import RevisionUploader
from ..objects import (
    seq_number_for_cursor,
    extract_sections,
    partition_addrs,
)
from ..ui.dialogs import (
    ZenyardProgressDialog,
    prompt_binary_instructions,
    prompt_intro_message,
    show_upload_complete,
)


# Backoff for transient upload errors (same curve as the download cycle).
_UPLOAD_BACKOFF_BASE = 2.0
_UPLOAD_BACKOFF_MAX = 60.0


class BringUpTask(CancellableTask):
    """
    One-shot per request. Drives a BinaryView from cold (or dirty) to a
    finalized Revision: ensure setup → register (if needed) → upload sections
    (if needed) → upload revision. Whether the revision is a full upload or a
    dirty-only one is inferred from persisted state (``last_completed_revision``), so
    boot and create-revision share one pipeline.
    """

    def __init__(
        self,
        *,
        bv: BinaryView,
        api: BinariesApi,
        model: Model,
        stop: threading.Event,
        prompt_intro: bool = True,
        logger: Logger | None = None,
        on_permanent_error: ty.Callable[[Disposition], None] | None = None,
    ) -> None:
        # A real title: BN's task panel is the visible (and cancellable)
        # surface while an upload retries through an outage.
        super().__init__("Zenyard upload", stop=stop, logger=logger)
        self._bv = bv
        self._api = api
        self._model = model
        self._prompt_intro = prompt_intro
        # Notified (with the Disposition) when an upload gives up on a
        # permanent error so the Coordinator can disable/surface it — the
        # cold-start counterpart to the download task's callback.
        self._on_permanent_error = on_permanent_error
        self._instructions: str | None = None
        self.objects_total = 0
        self.objects_uploaded = 0
        # Live extraction progress, written by RevisionUploader and read by the
        # ZenyardProgressDialog's poll timer (atomic int access under the GIL).
        self.objects_extracted = 0
        self.objects_extract_total = 0
        # Consecutive transient upload failures, read GIL-atomically by the
        # status bar (Coordinator.progress_snapshot) to surface
        # "Reconnecting…" during bring-up.
        self.connection_failures = 0

    def _run(self) -> None:
        self._ensure_binary_id()
        if self._model.binary_id is None:
            return
        self.check_cancelled()
        self._ensure_sections_uploaded()
        self.check_cancelled()
        self._ensure_revision_uploaded()

    def _upload_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_retries=None,
            base_delay=_UPLOAD_BACKOFF_BASE,
            max_delay=_UPLOAD_BACKOFF_MAX,
            stop=self._stop,
            should_stop=self.is_cancelled,
            on_permanent=self._on_permanent_error,
            on_failure_count=lambda n: setattr(self, "connection_failures", n),
        )

    # ── Step 1: registration ──────────────────────────────────────────────────

    def _ensure_binary_id(self) -> None:
        # Already registered? Reuse it. The id is persisted in the BV metadata
        # and reloaded by Model.create on reopen, so there is nothing to do.
        if self._model.binary_id is not None:
            return

        # Not registered yet — prompt the user, then POST /binaries.
        if self._prompt_intro:
            # ``None`` means the user cancelled; a bool is the per-binary
            # auto-apply choice. execute_on_main_thread_and_wait can't return
            # a value, so capture it via a container (mirrors the instructions
            # prompt below). Persist the choice on the model (BNDB-backed).
            choice: list[bool | None] = [True]
            execute_on_main_thread_and_wait(
                lambda: choice.__setitem__(0, prompt_intro_message())
            )
            auto_apply = choice[0]
            if auto_apply is None:
                log_warn("user chose to CANCEL!")
                return
            self._model.auto_apply = auto_apply
        # ``None`` means the user cancelled the instructions prompt; an empty
        # string means they accepted without adding any. Only the former aborts.
        instr_holder: list[str | None] = [None]
        execute_on_main_thread_and_wait(
            lambda: instr_holder.__setitem__(0, prompt_binary_instructions())
        )
        instructions = instr_holder[0]
        if instructions is None:
            log_warn("user chose to CANCEL!")
            return
        self._instructions = instructions or None
        self._do_register()

    def _do_register(self) -> None:
        filename = self._bv.file.filename
        try:
            sha = hashlib.sha256()
            with open(filename, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha.update(chunk)
        except Exception as e:
            log_error(f"failed to hash binary: {e}")
            return

        is_swift = is_swift_binary(self._bv)
        body = PostBinaryBody(
            name=os.path.basename(filename),
            details=BinaryDetails(
                input_file_sha256=sha.hexdigest(),
                instructions=self._instructions,
                original_languages=OriginalLanguages(swift=is_swift),
                platform=get_platform(self._bv),
            ),
        )
        self.check_cancelled()
        log_debug(
            f"POST /binaries name='{body.name}' sha256={sha.hexdigest()[:16]}…"
        )

        result = call_backend(
            f"POST /binaries ('{body.name}')",
            lambda: self._api.create_binary(post_binary_body=body),
            self._upload_policy(),
        )
        if isinstance(result, _GaveUp):
            return
        log_info(f"registered binary {result.binary_id}")
        self._model.binary_id = UUID(str(result.binary_id))

    # ── Step 2: sections ──────────────────────────────────────────────────────

    def _ensure_sections_uploaded(self) -> None:
        if self._model.sections_uploaded_revision > 0:
            return
        if self._model.binary_id is None:
            return

        sections = extract_sections(self._bv)
        if not sections:
            log_warn("no sections to upload")
            return

        bv = self._bv
        api = self._api
        binary_id = self._model.binary_id
        uploaded: set[str] = set()

        def _upload_section_data() -> bool:
            for binja_sec in bv.sections.values():
                self.check_cancelled()
                addr = f"{binja_sec.start:016x}"
                if addr in uploaded:
                    continue
                uploaded.add(addr)
                compressed = get_section_compressed(bv, binja_sec)
                if not compressed:
                    continue
                # This phase runs outside the extraction progress dialog —
                # the task-panel text is its only visible surface.
                self.progress = f"Zenyard: uploading section {binja_sec.name}…"

                def _upload_one(
                    addr: str = addr,
                    data: bytes = compressed,
                ) -> None:
                    api.set_large_data_to_object(
                        addr,
                        str(binary_id),
                        compressed_data=data,
                        # Section bytes can be MBs; see LARGE_UPLOAD_TIMEOUT.
                        _request_timeout=LARGE_UPLOAD_TIMEOUT,
                    )

                if isinstance(
                    call_backend(
                        f"PUT set_large_data_to_object ({binja_sec.name})",
                        _upload_one,
                        self._upload_policy(),
                    ),
                    _GaveUp,
                ):
                    return False
            return True

        ok, new_rev = submit_revision(
            binary_id,
            api,
            self._model.last_submitted_revision,
            sections,  # type: ignore[arg-type]
            FinishAndAnalyzeCurrentRevisionBody(
                analyze_dependents=False,
                perform_global_analysis=False,
                swift_only=False,
            ),
            label="sections",
            policy=self._upload_policy(),
            post_add=_upload_section_data,
        )
        if not ok:
            return
        log_info(f"sections uploaded (revision {new_rev})")
        self._model.sections_uploaded_revision = new_rev
        self._model.last_submitted_revision = new_rev

    # ── Step 3: revision ──────────────────────────────────────────────────────

    def _ensure_revision_uploaded(self) -> None:
        if self._model.binary_id is None:
            return

        (
            dirty_fns,
            dirty_gls,
            removed_fns,
            removed_gls,
            prev_hashes,
        ) = self._model.clear_dirty_for_upload()

        # Removed objects' content hashes are stale regardless of the
        # upload outcome — drop them eagerly.
        self._model.drop_uploaded_hashes(removed_fns | removed_gls)

        addrs = self._plan_objects(dirty_fns, dirty_gls)
        if addrs is None:
            return

        # Status-bar denominator (read by Coordinator.progress_snapshot). The
        # planned count; the uploader drops thunks/unchanged objects, so the
        # final tally may be a touch lower — close enough for the progress bar.
        self.objects_total = len(addrs)

        # Pop the progress dialog while objects are extracted + uploaded. It
        # polls the live counters and its Cancel button calls request_cancel,
        # which trips check_cancelled() in the extraction loop. Capture the
        # reference synchronously via a container (execute_on_main_thread_and_wait
        # can't return a value), then let extraction proceed on this thread.
        dialog_holder: list[ZenyardProgressDialog | None] = [None]

        def _open_dialog() -> None:
            dlg = ZenyardProgressDialog(
                get_progress=lambda: (
                    self.objects_extracted,
                    self.objects_extract_total,
                ),
                on_cancel=self.request_cancel,
            )
            dialog_holder[0] = dlg
            dlg.show()

        execute_on_main_thread_and_wait(_open_dialog)

        try:
            result = RevisionUploader(
                self,
                planned_addrs=addrs,
                last_uploaded_hashes=prev_hashes,
                inference_seq=seq_number_for_cursor(
                    self._model.inference_cursor
                ),
            ).run()
        except TaskCancelled:
            # Cancelled mid-extraction: restore the dirty marks cleared above so
            # the pending changes aren't silently lost. (A full/cold upload
            # re-runs full anyway since last_completed_revision stays 0; this
            # matters for the dirty-only path.)
            for a in dirty_fns:
                self._model.mark_function_dirty(a)
            for a in dirty_gls:
                self._model.mark_global_dirty(a)
            return
        finally:
            # Tear the dialog down before any later modal (show_upload_complete):
            # an app-modal dialog left visible would block input to it. Blocking
            # close so teardown completes before the next dialog opens.
            dlg = dialog_holder[0]
            if dlg is not None:
                execute_on_main_thread_and_wait(dlg.close)

        if result.failed:
            # Re-queue every planned object the backend never durably saw
            # (includes the unsent batch) so the next upload retries it.
            # Thunks are never uploaded, so they need no re-queue.
            fn_addrs, gl_addrs, _thunks = partition_addrs(self._bv, addrs)
            for a in set(fn_addrs) - result.uploaded_function_addrs:
                self._model.mark_function_dirty(a)
            for a in set(gl_addrs) - result.uploaded_global_addrs:
                self._model.mark_global_dirty(a)
            return

        self.objects_uploaded = result.uploaded_count
        log_info(
            f"upload complete — {result.uploaded_count} objects across"
            f" {result.batches} revision(s)"
        )
        if get_preferences().show_initial_upload_message:
            # execute_on_main_thread_and_wait doesn't propagate return values,
            # so capture the "Don't show this again" choice via a container
            # (mirrors prompt_intro_message). Persist it per-binary when ticked.
            suppressed: list[bool] = [False]
            execute_on_main_thread_and_wait(
                lambda: suppressed.__setitem__(0, show_upload_complete())
            )
            if suppressed[0]:
                save_show_initial_upload_message(False)
        self._model.last_completed_revision = result.revision
        self._model.last_submitted_revision = result.revision

    def _plan_objects(
        self,
        dirty_fns: frozenset[int],
        dirty_gls: frozenset[int],
    ) -> list[int] | None:
        """Decide which object addresses to upload.

        Returns a single flat ``list[int]`` of addresses — functions, globals,
        and thunks intermixed — for the uploader to partition and stream. The
        type of each address is resolved downstream by ``partition_addrs``.
        """
        full = self._model.last_completed_revision == 0
        container = pathlib.Path(self._bv.file.filename).name
        ignored = IgnoredSections(container)

        # A binary with no section table at all (e.g. section-header-stripped)
        # makes "no section" true for every address — so absence of a section
        # can't be a drop signal there, or we'd discard the whole upload.
        sections_present = bool(self._bv.sections)

        # step 1: gather all symbols
        all_fns = {f.start for f in self._bv.functions}
        all_gls = {dv.address for dv in self._bv.data_vars.values()}

        # step 2: remove symbols in ignored / sectionless scaffolding regions
        fn_addrs = self._keep_object_addrs(all_fns, ignored, sections_present)
        gl_addrs = self._keep_object_addrs(all_gls, ignored, sections_present)

        # step 3: remove non-dirty (first upload treats everything as dirty)
        if not full:
            fn_addrs &= dirty_fns
            gl_addrs &= dirty_gls
            if not fn_addrs and not gl_addrs:
                log_warn("no dirty objects to upload")
                return None

        addrs = [*fn_addrs, *gl_addrs]
        if not addrs:
            log_warn("no objects to upload")
            return None
        log_debug(f"dirty objects [{addrs}]")
        return addrs

    def _keep_object_addrs(
        self,
        addrs: set[int],
        ignored: IgnoredSections,
        sections_present: bool,
    ) -> set[int]:
        """Keep only object addresses worth uploading.

        - In a section: drop if that section (or its library) is blacklisted.
        - Sectionless, binary has sections: drop — it's loader/cache
          scaffolding (GOT, objc-opt, stub islands, cache metadata); a symbol
          name is no signal here, real globals live in the image's own
          data sections.
        - Sectionless, binary has no section table: keep (no section info to
          filter on).
        """
        kept: set[int] = set()
        for a in addrs:
            sec_names = [sec.name for sec in self._bv.get_sections_at(a)]
            if sec_names:
                if not ignored.contains(sec_names):
                    kept.add(a)
            elif not sections_present:
                kept.add(a)
        return kept
