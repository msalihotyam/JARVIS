"""Opt-in visual fixture of the REAL QML, with an isolated fake backend.

Run from the repository root: python -m scripts.ui_visual --live --fixture
--reference PATH enables the existing Ctrl+Shift+R reference overlay.
Ctrl+Shift+S saves the current window; Ctrl+Shift+A toggles the non-actionable
approval mockup. No user's configuration, database or connector is used.
"""
from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--exercise", action="store_true", help="Bounded presentation-only transition/performance run")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--reference", default="")
    parser.add_argument("--theme", choices=("space", "dark", "command"), default="space")
    parser.add_argument("--capture-dir", type=Path)
    parser.add_argument("--capture-name", default="preview")
    parser.add_argument("--size", default="1920x1080")
    parser.add_argument("--page", choices=("home", "tasks", "automations", "memory", "history", "projects"), default="home")
    parser.add_argument("--peek", choices=("Tasks", "Updates", "Focus", "Plan"))
    parser.add_argument("--scene", choices=("home", "approval", "settings", "model", "nav", "edit", "focus", "running",
                                            "projects", "project", "badge", "archived",
                                            "instruction", "files", "purge", "selector", "attach"), default="home")
    args = parser.parse_args()
    if args.exercise and not args.fixture:
        parser.error("--exercise requires --fixture")
    os.environ["QT_QPA_PLATFORM"] = "windows"
    os.environ["QT_QUICK_BACKEND"] = "rhi"
    os.environ["QSG_RHI_BACKEND"] = "d3d11"
    os.environ["QT_SCALE_FACTOR"] = "1"
    os.environ["JARVIS_UI_DEV_FIXTURE"] = "1" if args.fixture else "0"
    os.environ["JARVIS_UI_DEV_REFERENCE"] = args.reference

    from PySide6.QtCore import QObject, QTimer, QMetaObject, Q_ARG, qInstallMessageHandler
    from PySide6.QtGui import QShortcut, QKeySequence
    from tests.test_ui import UiTestCase, _APP

    qInstallMessageHandler(lambda _t, _c, m: print(m, flush=True))
    case = UiTestCase()
    case.setUp()
    bridge = case.bridge()
    bridge.prefs.data["theme"] = args.theme
    window = case.load(bridge)
    window.setTitle("JARVIS — UI visual validation")
    bridge.setPage(args.page)
    width, height = (int(n) for n in args.size.split("x"))
    if (width, height) != (1920, 1080):
        window.showNormal()
        window.resize(width, height)
    if args.peek:
        QTimer.singleShot(300, lambda: QMetaObject.invokeMethod(window, "togglePeek", Q_ARG("QVariant", args.peek), Q_ARG("QVariant", 0)))
    # Presentation-only scenes in this isolated harness. There is no ticket,
    # task or approval authority behind the explicitly marked visual sample.
    if args.scene == "approval":
        if not args.fixture:
            parser.error("approval scene requires --fixture")
        window.setProperty("devApprovalVisible", True)
        # MAIN / DARK show approvals in the conversation's safety card (the same
        # display-only fixture Ctrl+Shift+A selects).
        bridge.setDevSafetyScenario("approval")
    elif args.scene in ("settings", "model", "nav"):
        QMetaObject.invokeMethod(window, {"settings":"toggleSettings", "model":"toggleModel", "nav":"toggleNav"}[args.scene])
    elif args.scene == "edit":
        bridge.setEditMode(True)
    elif args.scene == "focus":
        bridge.setFocusMode(True)
    elif args.scene in ("projects", "project", "badge", "archived", "instruction", "files", "purge", "selector",
                        "attach"):
        # V2.0 Gate 6A: sample projects in the ISOLATED store only (fixture copy,
        # nobody's data): two active, one archived, each with conversations.
        if not args.fixture:
            parser.error(f"{args.scene} scene requires --fixture")
        store = bridge._conversation_db()
        seeded = {}
        for name, titles in (("Home renovation", ("Kitchen plan", "Invoices to check")),
                             ("JARVIS development", ("Gate 6 interface notes",)),
                             ("Thesis 2025", ("Reading list draft",))):
            project = store.create_project(name)
            seeded[name] = (project, [store.create_conversation(project.id) for _ in titles])
            for record, title in zip(seeded[name][1], titles):
                store.set_conversation_title(record.id, title)
        store.set_project_active(seeded["Thesis 2025"][0].id, False)
        home = seeded["Home renovation"][0]
        if args.scene in ("instruction", "files", "purge"):
            # Gate 6B: a stored instruction and three files (one retired),
            # written through the same explicit-user paths, fixture store only.
            store.save_explicit_user_project_instruction(
                home.id, "Answer in Turkish. Keep estimates in TRY and say which quote they come from.", "internal")
            for name, text, classification in (("kitchen-quotes.md", "Quote A: 42,000 TRY\n" * 40, "internal"),
                                               ("floor-plan-notes.txt", "Living room 24 m2\n" * 60, "public"),
                                               ("old-budget.csv", "item,cost\ntiles,9000\n" * 30, "sensitive")):
                path = case.root / "fixture-files" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                bridge.backend.project_file("add", home.id, path=str(path), classification=classification)
            retired = store.list_project_files(home.id)[-1]
            bridge.backend.project_file("retire", home.id, file_id=retired["id"])
            bridge.showProject(home.id)
            page = window.findChild(QObject, "projectsPage")
            page.setProperty("section", "instruction" if args.scene == "instruction" else "files")
            if args.scene == "purge":
                row = next(r for r in bridge.projectFiles if r["id"] == retired["id"])
                QTimer.singleShot(400, lambda: QMetaObject.invokeMethod(
                    window.findChild(QObject, "purgeDialog"), "openFor", Q_ARG("QVariant", row)))
        elif args.scene == "selector":
            bridge.openConversation(seeded["JARVIS development"][1][0].id)
            bridge.setPage("home")
            QTimer.singleShot(400, lambda: QMetaObject.invokeMethod(window.findChild(QObject, "projectSelectorMenu"), "open"))
        elif args.scene == "attach":
            # Gate 6C: the open Global conversation (fixture store only), with a
            # title and two turns, previewed against "Home renovation".
            from jarvis.conversation import append_turn
            open_id = bridge.conversationId
            store.set_conversation_title(open_id, "Kitchen budget questions")
            for text in ("What did the tiler quote?", "Compare it with the second quote."):
                store.update_conversation_state(open_id, lambda state, text=text: append_turn(
                    state, role="user", kind="user", text=text, message=text, key=text)[0])
            bridge.openConversation(open_id)
            bridge.setPage("home")

            def show_attach() -> None:
                dialog = window.findChild(QObject, "attachDialog")
                QMetaObject.invokeMethod(dialog, "openDialog")
                dialog.setProperty("chosenId", home.id)
                bridge.previewAttach(home.id, "")
            QTimer.singleShot(400, show_attach)
        elif args.scene == "projects":
            bridge.setPage("projects")
            bridge.refreshProjects()
        elif args.scene == "project":
            bridge.showProject(seeded["Home renovation"][0].id)
        elif args.scene == "badge":
            bridge.openConversation(seeded["JARVIS development"][1][0].id)
            bridge.setPage("home")
        else:
            bridge.openConversation(seeded["Thesis 2025"][1][0].id)
            bridge.setPage("home")
    elif args.scene == "running":
        if not args.fixture:
            parser.error("running scene requires --fixture")
        bridge._core_state = "running"
        bridge.stateChanged.emit()
    output = args.capture_dir or Path(tempfile.mkdtemp(prefix="jarvis-ui-visual-"))
    output.mkdir(parents=True, exist_ok=True)
    counter = 0

    def capture() -> None:
        nonlocal counter
        counter += 1
        target = output / f"{args.capture_name}-{counter:02}.png"
        if not window.grabWindow().save(str(target)):
            raise RuntimeError("Window capture failed")
        print(f"CAPTURE: {target}", flush=True)

    shortcut = QShortcut(QKeySequence("Ctrl+Shift+S"), window)
    shortcut.activated.connect(capture)
    if args.exercise:
        def scene(name: str) -> None:
            QMetaObject.invokeMethod(window, "closeContexts")
            window.setProperty("devApprovalVisible", False)
            bridge.setEditMode(False)
            bridge.setFocusMode(False)
            bridge._core_state = "running" if name == "running" else "idle"
            bridge.stateChanged.emit()
            bridge.setTheme(name if name in ("space", "dark", "command") else "space")
            if name in ("settings", "model", "nav"):
                QMetaObject.invokeMethod(window, {"settings":"toggleSettings", "model":"toggleModel", "nav":"toggleNav"}[name])
            elif name == "approval":
                window.setProperty("devApprovalVisible", True)
            elif name == "edit":
                bridge.setEditMode(True)
            elif name == "focus":
                bridge.setFocusMode(True)
            print(f"EXERCISE: {name}", flush=True)

        steps = ("space", "running", "dark", "space", "command", "settings", "model", "nav", "approval", "edit", "focus", "space")
        for index, name in enumerate(steps):
            QTimer.singleShot(2000 + index * 4000, lambda name=name: scene(name))
        QTimer.singleShot(51000, _APP.quit)
    started = time.monotonic()
    cpu_started = time.process_time()
    frames = [0]
    window.frameSwapped.connect(lambda: frames.__setitem__(0, frames[0] + 1))
    print(f"VISUAL_PID: {os.getpid()}", flush=True)
    print(f"VISUAL_OUTPUT: {output}", flush=True)
    print(f"ISOLATED_BACKEND: {case.root}", flush=True)
    if not args.live and not args.exercise:
        QTimer.singleShot(1200, capture)
        QTimer.singleShot(1500, _APP.quit)
    result = _APP.exec()
    print(f"RENDER_SAMPLE: wall={time.monotonic() - started:.2f}s cpu={time.process_time() - cpu_started:.2f}s swaps={frames[0]}", flush=True)
    # Destroy QML while the bridge still exists (avoids shutdown-only
    # null-context diagnostics), then let the isolated test fixture close.
    import shiboken6
    shiboken6.delete(case.engine)
    case._cleanups = [entry for entry in case._cleanups if getattr(entry[0], "__name__", "") != "deleteLater"]
    case.doCleanups()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
