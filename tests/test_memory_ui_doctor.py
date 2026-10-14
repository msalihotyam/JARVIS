"""V2.1b: the Memory page's Memory Doctor panel (MemoryDoctorPanel).

* The shown project's findings are listed worst first, in words, with the
  stale / conflict / orphan counts.
* 'Repair' appears only on a finding with a repair proposal; it asks for
  confirmation first and only the confirmed click reaches the bridge slot
  (the app re-diagnoses and decides). One repair at a time.
* A project with no findings reads as an honest healthy line.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import unittest

from tests.test_g6k6 import K6Case
from tests.test_ui import settled


def finding(fid, code, severity, key, repair=None):
    return {"finding_id": fid, "code": code, "severity": severity, "record_ids": ["mem_1"], "summary_key": key,
            "repair": None if repair is None else {"action": repair, "record_ids": ["mem_1"]}}


class MemoryDoctorPanelTests(K6Case):
    def setUp(self) -> None:
        super().setUp()
        self.ui = self.bridge()
        self.app = self.factory(self.store.load().settings)   # the same TEMP database
        self.project = self.app.memory.create_project("Alpha").id

    def open_page(self):
        root = self.window(self.ui)
        self.page(self.ui, root, "memory")
        # The real Doctor's answer for an empty project: healthy.
        settled(800, lambda: self.assertTrue(self.item(root, "memoryDoctorHealthy").isVisible()))
        return root

    def texts(self, root, name):
        return [i.property("text") for i in self.named(root, name)]

    def report(self, findings):
        self.ui.memoryDoctorDone.emit({"ok": True, "project_id": self.project, "record_count": 3, "findings": findings})

    def test_empty_project_is_healthy(self) -> None:
        root = self.open_page()
        self.assertIn("Memory is healthy: the Doctor found nothing to fix.", self.shown_texts(root))
        self.assertEqual(["Stale: 0 · Conflicts: 0 · Orphaned: 0"], self.texts(root, "memoryDoctorCounts"))
        self.assertEqual([], self.named(root, "memoryDoctorFinding"))
        self.assertEqual([], self.named(root, "memoryDoctorRepair"))

    def test_findings_rendered_worst_first_and_repair_gated(self) -> None:
        root = self.open_page()
        self.report([
            finding("f_info", "STALE", "info", "memory_doctor.stale.unverified"),
            finding("f_warn", "ORPHAN_SCOPE", "warning", "memory_doctor.orphan_scope", "retract"),
            finding("f_err", "SUPERSESSION_CYCLE", "error", "memory_doctor.supersession_cycle"),
            finding("f_conf", "CONFLICT_UNRESOLVED", "warning", "memory_doctor.conflict_unresolved", "supersede"),
        ])
        settled(500, lambda: self.assertEqual(4, len(self.named(root, "memoryDoctorFinding"))))
        self.assertFalse(self.named(root, "memoryDoctorHealthy"))
        rows = sorted(self.named(root, "memoryDoctorSummary"), key=lambda i: self.box(i)[1])
        self.assertEqual(["Records replace each other in a loop.",
                          "A record belongs to a project or conversation that no longer exists.",
                          "Approved records on the same subject contradict each other.",
                          "A record has not been verified for a long time."], [i.property("text") for i in rows])
        self.assertEqual(["Stale: 1 · Conflicts: 1 · Orphaned: 1"], self.texts(root, "memoryDoctorCounts"))
        self.assertEqual(2, len(self.named(root, "memoryDoctorRepair")))      # only the two with a proposal
        for theme in ("dark", "space"):                                       # both themes draw the panel
            self.ui.setTheme(theme)
            settled(300, lambda: self.assertEqual(2, len(self.named(root, "memoryDoctorRepair"))))

    def test_repair_asks_first_then_calls_the_slot_once(self) -> None:
        root = self.open_page()
        answers = []
        self.ui.memoryRepairDone.connect(answers.append)
        self.report([finding("f_warn", "ORPHAN_SCOPE", "warning", "memory_doctor.orphan_scope", "retract")])
        settled(500, lambda: self.assertEqual(1, len(self.named(root, "memoryDoctorRepair"))))
        self.click(root, "memoryDoctorRepair", then=lambda: self.assertTrue(self.item(root, "memoryDoctorApply").isVisible()))
        self.assertIn("Repair: Retract the record. Nothing is deleted.", self.texts(root, "memoryDoctorConfirmText")[0])
        self.assertEqual([], answers)                                          # the first click only asks
        self.click(root, "memoryDoctorApply", until=lambda: len(answers) == 1)
        # The app re-diagnosed and refused the made-up finding; the panel reloads the real, healthy report.
        self.assertEqual("f_warn", answers[0]["finding_id"])
        self.assertFalse(answers[0]["ok"])
        settled(800, lambda: self.assertTrue(self.item(root, "memoryDoctorHealthy").isVisible()))
        self.assertEqual(1, len(answers))


if __name__ == "__main__":
    unittest.main()
