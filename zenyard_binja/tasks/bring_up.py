from __future__ import annotations

import os
import hashlib
import pathlib
import threading
import typing as ty
from uuid import UUID
from binaryninja import (  # type: ignore[import]
    BinaryView,
    execute_on_main_thread_and_wait,
)
from binaryninja.log import Logger  # type: ignore[import]
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
from ..helpers.main_thread import run_on_main_thread
from ..helpers.sections import (
    IgnoredSections,
    get_section_compressed,
    get_platform,
    is_dsc_view,
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
    One-shot per request. Drives a BinaryView from cold to a finalized
    Revision: ensure setup → register (if needed) → upload sections (if needed)
    → upload revision. A binary is analyzed exactly once: once a revision has
    completed (``last_completed_revision > 0``) the revision step is a no-op, so
    reopening an already-analyzed binary never re-uploads or re-triggers
    analysis.
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
        super().__init__("Zenyard upload", stop=stop, logger=logger)
        self._bv = bv
        self._api = api
        self._model = model
        self._prompt_intro = prompt_intro
        self._on_permanent_error = on_permanent_error
        self._instructions: str | None = None
        self.objects_total = 0
        self.objects_uploaded = 0
        self.objects_extracted = 0
        self.objects_extract_total = 0
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
            stop=self._stop_event,
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
            auto_apply = run_on_main_thread(prompt_intro_message)
            if auto_apply is None:
                log_warn("Not runnig analysis. user cancel!")
                return
            self._model.auto_apply = auto_apply

        self._instructions = run_on_main_thread(prompt_binary_instructions)
        if self._instructions is None:
            log_warn("Not runnig analysis. user cancel!")
            return

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

        prev_hashes = self._model.uploaded_hash_snapshot()

        addrs = self._plan_objects()
        if addrs is None:
            return

        self.objects_total = len(addrs)

        def _open_dialog() -> ZenyardProgressDialog:
            dlg = ZenyardProgressDialog(
                get_progress=lambda: (
                    self.objects_extracted,
                    self.objects_extract_total,
                ),
                on_cancel=self.request_cancel,
            )
            dlg.show()
            return dlg

        dlg = run_on_main_thread(_open_dialog)

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
            # Cancelled mid-extraction: leave it. last_completed_revision stays
            # 0, so the next open re-runs the full upload from scratch.
            return
        finally:
            # Tear the dialog down before any later modal (show_upload_complete):
            # an app-modal dialog left visible would block input to it. Blocking
            # close so teardown completes before the next dialog opens.
            execute_on_main_thread_and_wait(dlg.close)

        self.objects_uploaded = result.uploaded_count
        log_info(
            f"upload complete — {result.uploaded_count} objects across"
            f" {result.batches} revision(s)"
        )
        if get_preferences().show_initial_upload_message:
            # The "Don't show this again" choice; persist it per-binary when
            # ticked.
            suppressed = run_on_main_thread(show_upload_complete)
            if suppressed:
                save_show_initial_upload_message(False)
        self._model.last_completed_revision = result.revision
        self._model.last_submitted_revision = result.revision

    def _plan_objects(
        self,
    ) -> list[int] | None:
        """Decide which object addresses to upload.

        Returns a single flat ``list[int]`` of addresses — functions, globals,
        and thunks intermixed — for the uploader to partition and stream. The
        type of each address is resolved downstream by ``partition_addrs``.

        ``None`` once the binary has already been analyzed
        (``last_completed_revision > 0``): a binary is analyzed exactly once, so
        a reopen must not re-upload and re-trigger analysis.
        """
        if self._model.last_completed_revision > 0:
            return None

        container = pathlib.Path(self._bv.file.filename).name
        ignored = IgnoredSections(container)

        # in dsc view (apple shared cache file) we want to drop
        # "Nameless" sections, in other file format, we keep them
        # as they just might have been stripped.
        keep_nameless_sections = not is_dsc_view(self._bv)

        # step 1: gather all symbols
        all_fns = {f.start for f in self._bv.functions}
        all_gls = {dv.address for dv in self._bv.data_vars.values()}

        # step 2: remove symbols in ignored / sectionless scaffolding regions
        fn_addrs = self._keep_object_addrs(
            all_fns, ignored, keep_nameless_sections
        )
        gl_addrs = self._keep_object_addrs(
            all_gls, ignored, keep_nameless_sections
        )

        addrs = [*fn_addrs, *gl_addrs]
        if not addrs:
            log_warn("no objects to upload")
            return None
        return addrs

    def _keep_object_addrs(
        self,
        addrs: set[int],
        ignored: IgnoredSections,
        keep_nameless_sections: bool,
    ) -> set[int]:
        """Keep only object addresses worth uploading."""
        kept: set[int] = set()
        for a in addrs:
            sec_names = [sec.name for sec in self._bv.get_sections_at(a)]
            if sec_names:
                if not ignored.contains(sec_names):
                    kept.add(a)
            elif keep_nameless_sections:
                kept.add(a)
        return kept
