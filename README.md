# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

## 首次知情台账

来源录入、案例和国家报告接成一套首次知情台账：

- 每次接入（录入、补录、随访、重复来源）都记录 `aware_at`（来源知情时间），且不得晚于收到时间。
- 案例的 `clock_start_at`（时限基准）取全部来源中最早的知情时间；基准只会提前，不会推后。
- 监管期限一律从时限基准推算：严重 15 天、死亡 7 天、非严重 90 天。
- 来源时间变化使基准提前时：未提交报告期限立刻重算，逾期列表同步；已提交报告保留原提交记录（`submitted_at`/`submitted_by`/`late` 及审计日志）并转成 `correction_required`（待更正），医学审核员确认后才能重报。
- 重复 `dedupe_key` 的录入不再丢弃：挂到原案例作为 `duplicate` 来源，多人同时补录同一案例时只让最早时间生效。
- 旧库升级自动迁移：缺失的来源时间按首次收到时间回填，未提交报告期限按回填后的基准重算，已提交报告保持原记录，全部历史数据仍可查询。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则作为重复来源挂到已存在案例。
- `POST /api/cases/{id}/sources`：来源补录，记录来源知情时间；医学审核员及跨区域补录直接拒绝。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访，可携带 `aware_at`。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告。
- `POST /api/reports/{id}/confirm`：医学审核员确认待更正报告，确认后才允许重报。
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。
- `GET /api/corrections`：待更正报告清单（`/api/state` 中也包含）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
