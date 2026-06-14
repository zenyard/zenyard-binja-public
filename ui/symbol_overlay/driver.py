"""Tints applied symbols in Binary Ninja's "Symbols" sidebar.

Like the status bar, the Coordinator / Model has no notification surface, so a
~300ms ``QTimer`` on the Qt main thread polls the active BinaryView's applied
addresses, resolves them to the symbols' *current* names, and installs a thin
``QStyledItemDelegate`` on the Symbols tree that recolours the text of matching
rows. Everything here runs on the Qt main thread.

BN renders the Symbols list with a plain ``QStyledItemDelegate`` (icon, font,
and colour all come from the model's roles), so the delegate mirrors the base
paint (``initStyleOption`` + ``drawControl``) and only overrides the text brush
for applied symbols — see ``_OverlayDelegate.paint``.

The poller never calls ``Sidebar.activate("Symbols")``: it decorates the tree
only when the user already has the Symbols sidebar open, so it never hijacks the
sidebar focus.
"""

from __future__ import annotations

from binaryninja import (  # type: ignore[import]
    BinaryView,
    execute_on_main_thread,
)
from binaryninjaui import UIContext  # type: ignore[import]
from PySide6.QtCore import (  # type: ignore[import]
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    QTimer,
)
from PySide6.QtGui import QBrush, QColor, QPainter, QPalette  # type: ignore[import]
from PySide6.QtWidgets import (  # type: ignore[import]
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
)

from ...coordinator.coordinator import get_coordinator_for_bv
from ...helpers.log import log_debug, log_warn
from .colors import applied_text_rgb

_POLL_MS = 300

_controller: "_SymbolOverlayController | None" = None


def install_symbol_overlay() -> None:
    """Start the symbol-overlay poller. Idempotent; main-thread safe.

    Scheduled (non-blocking) onto the Qt main thread so the ``QTimer`` is created
    there, and so this is safe to call from plugin import on any thread.
    """

    def _setup() -> None:
        global _controller
        if _controller is not None:
            return
        _controller = _SymbolOverlayController()
        _controller.start()

    execute_on_main_thread(_setup)


class _OverlayDelegate(QStyledItemDelegate):
    """Renders the row natively, recolouring the text of applied symbols.

    We can't just modify ``option.palette`` and call ``super().paint`` —
    ``QStyledItemDelegate.paint`` re-runs ``initStyleOption`` internally, which
    resets ``palette.Text`` from the model's per-type ``ForegroundRole`` and
    would clobber our colour. So we mirror the base paint: build the option,
    override the text brush *after* ``initStyleOption``, then draw it ourselves.
    """

    def __init__(
        self, view: QTreeView, controller: "_SymbolOverlayController"
    ) -> None:
        super().__init__(view)  # parented to the view -> not GC'd
        self._controller = controller

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if index.data(Qt.ItemDataRole.DisplayRole) in (
            self._controller.applied_names
        ):
            # Resolve the tint per paint against the background each role is
            # drawn over (Base for normal rows, Highlight for selected), so it
            # stays readable on light themes and tracks live theme switches.
            base = opt.palette.color(QPalette.ColorRole.Base)  # type: ignore[attr-defined]
            highlight = opt.palette.color(QPalette.ColorRole.Highlight)  # type: ignore[attr-defined]
            text_color = QColor(
                *applied_text_rgb((base.red(), base.green(), base.blue()))
            )
            selected_color = QColor(
                *applied_text_rgb(
                    (highlight.red(), highlight.green(), highlight.blue())
                )
            )
            opt.palette.setBrush(QPalette.ColorRole.Text, QBrush(text_color))  # type: ignore[attr-defined]
            opt.palette.setBrush(  # type: ignore[attr-defined]
                QPalette.ColorRole.HighlightedText, QBrush(selected_color)
            )
        widget = opt.widget  # type: ignore[attr-defined]
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget
        )


class _SymbolOverlayController:
    def __init__(self) -> None:
        self.applied_names: frozenset[str] = frozenset()
        self._last_key: tuple[int, frozenset[int]] | None = None
        self._needs_repaint = False
        self._pin: tuple[object, ...] | None = None

        self._timer = QTimer()
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._timer.start()

    # ── Active-view lookup ───────────────────────────────────────────────────

    def _ui_context(self) -> object | None:
        """The window's UIContext, or None when no window exists yet.

        ``activeContext()`` tracks the OS-active *window*: it is None whenever
        BN isn't the frontmost app and it flaps during startup (verified live),
        so fall back to ``allContexts()``.
        """
        ctxs = UIContext.allContexts()
        return ctxs[0] if ctxs else None

    def _active_view_frame(self) -> object | None:
        """The active tab's ViewFrame, or None.

        ``getCurrentViewFrame`` resolves from the *focused* widget, so it's
        None until the user first focuses something inside the window. Fall
        back to the current tab's frame — tab tracking doesn't need focus.
        """
        ctx = self._ui_context()
        if ctx is None:
            return None
        vf = ctx.getCurrentViewFrame()  # type: ignore[attr-defined]
        if vf is not None:
            return vf
        tab = ctx.getCurrentTab()  # type: ignore[attr-defined]
        if tab is None:
            return None
        return ctx.getViewFrameForTab(tab)  # type: ignore[attr-defined]

    def _active_bv(self) -> BinaryView | None:
        vf = self._active_view_frame()
        if vf is None:
            return None
        return vf.getCurrentBinaryView()  # type: ignore[attr-defined]

    def _symbols_tree(self) -> QTreeView | None:
        """
        The active tab's Symbols-sidebar tree, or None if not open.
        """
        ctx = self._ui_context()
        if ctx is None:
            return None
        sidebar = ctx.sidebar()  # type: ignore[attr-defined]
        if sidebar is None:
            return None
        widget = sidebar.widget("Symbols")
        if widget is None:
            return None
        tree = widget.findChild(QTreeView)
        self._pin = (ctx, sidebar, widget, tree)
        return tree

    # ── Poll tick ────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        try:
            bv = self._active_bv()
            coord = get_coordinator_for_bv(bv) if bv is not None else None
            applied = (
                coord.model.applied_addresses_snapshot()
                if coord is not None
                else frozenset()
            )
            key = (id(coord) if coord is not None else 0, applied)
            if key != self._last_key:
                self._last_key = key
                self.applied_names = self._resolve_names(bv, applied)
                self._needs_repaint = True

            tree = self._symbols_tree()
            if tree is None:
                return
            just_installed = self._ensure_decorated(tree)
            if self._needs_repaint or just_installed:
                tree.viewport().update()
                self._needs_repaint = False

        except RuntimeError as e:
            log_debug(f"symbol-overlay tick skipped (stale Qt object): {e}")
        except Exception as e:
            log_warn(f"symbol-overlay tick failed: {e}")

    # def _refresh_view(self, tree: QTreeView) -> None:
    #     """Make the view re-render visible rows through our delegate.

    #     This ``componentTreeView`` caches item rendering: a bare
    #     ``viewport().repaint()`` is a no-op and ``doItemsLayout()`` tears the
    #     tree down. Emitting the model's ``dataChanged`` over the visible rows
    #     (top level + one level of children, to cover a sectioned list) makes the
    #     view re-render those rows through the delegate, without a relayout.
    #     """
    #     m = tree.model()
    #     if m is None:
    #         return
    #     cols = m.columnCount()
    #     if cols <= 0:
    #         return

    #     def emit_for(parent: QModelIndex) -> None:
    #         rows = m.rowCount(parent)
    #         if rows > 0:
    #             m.dataChanged.emit(  # type: ignore[attr-defined]
    #                 m.index(0, 0, parent),
    #                 m.index(rows - 1, cols - 1, parent),
    #             )

    #     emit_for(QModelIndex())
    #     for r in range(m.rowCount()):
    #         idx = m.index(r, 0)
    #         if m.hasChildren(idx):
    #             emit_for(idx)

    def _ensure_decorated(self, tree: QTreeView) -> bool:
        """Install our delegate on column 0 if absent. Idempotent.

        Re-installs if BN swaps the delegate back to a plain one (e.g. on a
        theme reload).
        """
        if isinstance(tree.itemDelegateForColumn(0), _OverlayDelegate):
            return False
        tree.setItemDelegateForColumn(0, _OverlayDelegate(tree, self))
        log_debug("symbol-overlay delegate installed on Symbols tree")
        return True

    def _resolve_names(
        self, bv: BinaryView | None, addrs: frozenset[int]
    ) -> frozenset[str]:
        """Map applied addresses to the symbols' current display names.

        Resolving live (rather than storing names) keeps a row tinted after the
        symbol is renamed — by the user or by the plugin itself.
        """
        if bv is None or not addrs:
            return frozenset()
        names: set[str] = set()
        for addr in addrs:
            try:
                sym = bv.get_symbol_at(addr)
                name = sym.name if sym is not None else None
                if name:
                    names.add(name)
            except Exception:
                continue
        return frozenset(names)
