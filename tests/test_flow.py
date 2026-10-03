import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, parse_time, report_deadline, utcnow


class PharmacovigilanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, dedupe="intake-1", **overrides):
        body = {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
                "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow()), "serious": False}
        body.update(overrides)
        return self.svc.create_case("reporter-a", "reporter", "CN", body)["case"]

    def test_full_case_and_deduplication_flow(self):
        case = self.create()
        self.assertEqual(case["revision"], 1)
        followed = self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "住院并出现死亡转归", "source": "phone", "expected_revision": 1,
             "received_at": iso(utcnow())},
        )
        self.assertEqual(followed["revision"], 2)
        reviewed = self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": 2, "serious": True, "fatal": True, "causality": "possibly_related",
             "rationale": "住院记录和死亡证明已核验", "received_at": iso(utcnow())},
        )
        self.assertEqual(reviewed["case"]["revision"], 3)
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        submitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(submitted["report"]["status"], "submitted")
        duplicate = self.svc.create_case(
            "reporter-b", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "fax", "dedupe_key": "intake-1", "received_at": iso(utcnow())},
        )
        self.assertTrue(duplicate["deduplicated"])
        detail = self.svc.get_case(case["id"], "global_admin", "")
        # 重复录入不再丢弃，而是挂到原案例作为重复来源
        self.assertEqual(len(detail["intakes"]), 2)
        self.assertEqual(detail["intakes"][0]["kind"], "initial")
        self.assertEqual(detail["intakes"][1]["kind"], "duplicate")
        self.assertGreaterEqual(len(detail["audit"]), 5)
        self.assertEqual(submitted["report"]["late"], 0)

    def test_permissions_and_stale_revision(self):
        case = self.create("intake-2")
        with self.assertRaises(ApiError) as ctx:
            self.svc.get_case(case["id"], "reporter", "US")
        self.assertEqual(ctx.exception.status, 403)
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "第一次更新", "source": "email", "expected_revision": 1})
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                  {"content": "过期修改", "source": "email", "expected_revision": 1})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        with self.assertRaises(ApiError) as ctx:
            self.svc.medical_review(case["id"], "lead-cn", "regional_lead",
                                    {"expected_revision": 2, "serious": True, "fatal": False,
                                     "causality": "related", "rationale": "x", "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.status, 403)

    def test_first_awareness_is_deadline_basis(self):
        received = iso(utcnow())
        aware = iso(utcnow() - timedelta(days=10))
        case = self.create("aware-1", received_at=received, aware_at=aware)
        self.assertEqual(case["clock_start_at"], aware)
        expected_due = iso(report_deadline(parse_time(aware), False, False))
        self.assertEqual(case["report_due_at"], expected_due)
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "US"})
        self.assertEqual(report["due_at"], expected_due)
        # 来源知情时间晚于收到时间直接拒绝
        with self.assertRaises(ApiError) as ctx:
            self.create("aware-2", aware_at=iso(utcnow() + timedelta(days=1)))
        self.assertEqual(ctx.exception.code, "invalid_awareness_time")

    def test_source_supplement_recomputes_and_overdue_syncs(self):
        case = self.create("src-1")
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        old_due = report["due_at"]
        aware = iso(utcnow() - timedelta(days=100))
        result = self.svc.add_source(case["id"], "reporter-a", "reporter", "CN",
                                     {"source": "hospital", "dedupe_key": "src-2", "aware_at": aware})
        self.assertTrue(result["clock_updated"])
        self.assertEqual(result["case"]["clock_start_at"], aware)
        detail = self.svc.get_case(case["id"], "global_admin", "")
        recomputed = detail["reports"][0]
        self.assertNotEqual(recomputed["due_at"], old_due)
        self.assertEqual(recomputed["due_at"], iso(report_deadline(parse_time(aware), False, False)))
        # 新期限已过，逾期列表同步出现该报告
        overdue_ids = [r["id"] for r in self.svc.overdue("regional_lead", "CN")]
        self.assertIn(report["id"], overdue_ids)
        # 幂等：同一 dedupe_key 重复补录不重复入账
        again = self.svc.add_source(case["id"], "reporter-a", "reporter", "CN",
                                    {"source": "hospital", "dedupe_key": "src-2", "aware_at": aware})
        self.assertTrue(again["deduplicated"])
        self.assertEqual(len(self.svc.get_case(case["id"], "global_admin", "")["intakes"]), 2)

    def test_submitted_report_correction_workflow(self):
        aware = iso(utcnow() - timedelta(days=10))
        case = self.create("corr-1", aware_at=aware)
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        submitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(submitted["report"]["status"], "submitted")
        original_submitted_at = submitted["report"]["submitted_at"]
        # 更早的来源知情时间到达
        self.svc.add_source(case["id"], "reporter-a", "reporter", "CN",
                            {"source": "literature", "dedupe_key": "corr-2",
                             "aware_at": iso(utcnow() - timedelta(days=120))})
        detail = self.svc.get_case(case["id"], "global_admin", "")
        corrected = detail["reports"][0]
        self.assertEqual(corrected["status"], "correction_required")
        # 原提交记录保留
        self.assertEqual(corrected["submitted_at"], original_submitted_at)
        self.assertEqual(corrected["submitted_by"], "lead-cn")
        # 待更正报告出现在更正清单而不是逾期清单
        self.assertIn(report["id"], [r["id"] for r in self.svc.corrections("medical_reviewer", "")])
        self.assertNotIn(report["id"], [r["id"] for r in self.svc.overdue("medical_reviewer", "")])
        # 医学审核员确认前禁止重报
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(ctx.exception.code, "correction_unconfirmed")
        # 非医学审核员不能确认
        with self.assertRaises(ApiError) as ctx:
            self.svc.confirm_report_correction(report["id"], "lead-cn", "regional_lead", {})
        self.assertEqual(ctx.exception.status, 403)
        confirmed = self.svc.confirm_report_correction(report["id"], "reviewer-1", "medical_reviewer",
                                                       {"note": "首次知情时间已核实"})
        self.assertEqual(confirmed["report"]["status"], "pending")
        self.assertEqual(confirmed["report"]["confirmed_by"], "reviewer-1")
        resubmitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(resubmitted["report"]["status"], "submitted")
        self.assertEqual(resubmitted["report"]["late"], 1)  # 新期限早已过去
        # 原提交记录留在审计日志中可查
        detail = self.svc.get_case(case["id"], "global_admin", "")
        actions = [a["action"] for a in detail["audit"]]
        self.assertIn("report_correction_required", actions)
        self.assertIn("report_correction_confirmed", actions)

    def test_concurrent_supplements_earliest_wins(self):
        case = self.create("shared-key")
        earlier = iso(utcnow() - timedelta(days=30))
        later = iso(utcnow() - timedelta(days=5))
        base = {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
                "dedupe_key": "shared-key", "received_at": iso(utcnow())}
        # 两条补录先后到达（并发下由事务串行化，语义相同）
        dup1 = self.svc.create_case("reporter-b", "reporter", "CN", {**base, "source": "fax", "aware_at": earlier})
        self.assertTrue(dup1["deduplicated"])
        dup2 = self.svc.create_case("reporter-c", "reporter", "CN", {**base, "source": "phone", "aware_at": later})
        self.assertTrue(dup2["deduplicated"])
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(len(detail["intakes"]), 3)
        self.assertEqual(sorted(i["kind"] for i in detail["intakes"]), ["duplicate", "duplicate", "initial"])
        # 只让最早时间生效
        self.assertEqual(detail["case"]["clock_start_at"], earlier)

    def test_source_supplement_permissions(self):
        case = self.create("perm-1")
        # 医学审核员不能补录来源
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_source(case["id"], "reviewer-1", "medical_reviewer", "",
                                {"source": "x", "dedupe_key": "perm-2"})
        self.assertEqual(ctx.exception.status, 403)
        # 跨区域补录直接拒绝
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_source(case["id"], "reporter-us", "reporter", "US",
                                {"source": "x", "dedupe_key": "perm-3"})
        self.assertEqual(ctx.exception.status, 403)
        # 跨区域重复提交同一 dedupe_key 同样拒绝，不再泄露案例
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_case("reporter-us", "reporter", "US",
                                 {"patient_ref": "P-1", "region": "US", "product": "DrugA",
                                  "event_term": "肝损伤", "source": "fax", "dedupe_key": "perm-1"})
        self.assertEqual(ctx.exception.status, 403)

    def test_followup_with_earlier_awareness_moves_clock(self):
        case = self.create("fu-1")
        early = iso(utcnow() - timedelta(days=20))
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "医院更早已知悉该事件", "source": "hospital",
                               "expected_revision": 1, "aware_at": early})
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(detail["case"]["clock_start_at"], early)
        self.assertEqual(detail["followups"][0]["aware_at"], early)

    def test_legacy_migration_backfill(self):
        path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_no TEXT NOT NULL UNIQUE, patient_ref TEXT NOT NULL, region TEXT NOT NULL,
                product TEXT NOT NULL, event_term TEXT NOT NULL, onset_at TEXT,
                received_at TEXT NOT NULL, serious INTEGER NOT NULL DEFAULT 0,
                fatal INTEGER NOT NULL DEFAULT 0, causality TEXT, report_due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', revision INTEGER NOT NULL DEFAULT 1,
                merged_into INTEGER, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE intakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, source TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, content TEXT NOT NULL,
                source TEXT NOT NULL, received_at TEXT NOT NULL, revision INTEGER NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(case_id, revision)
            );
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, country TEXT NOT NULL,
                due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TEXT, submitted_by TEXT, late INTEGER NOT NULL DEFAULT 0,
                UNIQUE(case_id, country)
            );
            CREATE TABLE medical_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL,
                case_revision INTEGER NOT NULL, serious INTEGER NOT NULL, fatal INTEGER NOT NULL,
                causality TEXT NOT NULL, rationale TEXT NOT NULL, reviewer TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(case_id, case_revision)
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, actor TEXT NOT NULL,
                role TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """INSERT INTO cases(case_no,patient_ref,region,product,event_term,onset_at,received_at,
               serious,fatal,causality,report_due_at,status,revision,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("PV-2020-000001", "P-9", "CN", "DrugA", "皮疹", None, "2020-01-10T00:00:00Z",
             0, 0, None, "2020-04-09T00:00:00Z", "open", 1, "reporter-a",
             "2020-01-10T00:00:00Z", "2020-01-10T00:00:00Z"),
        )
        case_id = conn.execute("SELECT id FROM cases").fetchone()[0]
        conn.execute(
            "INSERT INTO intakes(case_id,source,dedupe_key,payload_json,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (case_id, "email", "legacy-1", "{}", "2020-01-05T00:00:00Z", "reporter-a", "2020-01-10T00:00:00Z"),
        )
        conn.execute("INSERT INTO reports(case_id,country,due_at,status) VALUES(?,?,?,?)",
                     (case_id, "CN", "2020-04-09T00:00:00Z", "pending"))
        conn.commit()
        conn.close()

        svc = PharmacovigilanceService(path)
        detail = svc.get_case(case_id, "global_admin", "")
        # 缺来源时间的旧数据按首次收到时间回填，时限基准取最早
        self.assertEqual(detail["intakes"][0]["aware_at"], "2020-01-05T00:00:00Z")
        self.assertEqual(detail["intakes"][0]["kind"], "initial")
        self.assertEqual(detail["case"]["clock_start_at"], "2020-01-05T00:00:00Z")
        # 未提交报告期限按首次知情重算：2020-01-05 + 90 天
        self.assertEqual(detail["reports"][0]["due_at"], "2020-04-04T00:00:00Z")
        self.assertIn("confirmed_by", detail["reports"][0])
        # 升级后原记录仍可查，迁移动作留痕
        self.assertEqual(detail["case"]["case_no"], "PV-2020-000001")
        actions = [a["action"] for a in svc.repo.conn.execute(
            "SELECT action FROM audit_log WHERE case_id IS NULL").fetchall()]
        self.assertIn("schema_migrated", actions)
        # 迁移幂等：再次启动不重复回填
        svc2 = PharmacovigilanceService(path)
        self.assertEqual(svc2.get_case(case_id, "global_admin", "")["case"]["clock_start_at"],
                         "2020-01-05T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
