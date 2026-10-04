"""端到端自测：时间精度（小数秒）修复。

覆盖（与 README「时间精度」一致）：
  * 带时区 ISO8601 输入按真实瞬间归一到 UTC；无时区输入按 UTC 解释；小数秒保留；
  * 保管链：采集 .100 → 移交 .200 → 签收 .300 同秒不同瞬间不被截断误判
    （签收严格晚于移交、事件不早于采集），结论 valid；
  * 同秒不同瞬间的测温读数不被误判为重复；真实同一瞬间（含不同时区表示）仍判重复/冲突；
  * 缺测区间严格大于最大间隔（小数秒精度）；
  * 校准证明左闭右开覆盖与重叠按真实瞬间判定（端点相邻允许，小数秒重叠拒绝）；
  * 整秒记录的同号重试/查询行为保持（整秒串字节一致、幂等 200）；
  * 无效时间仍按 422 拒绝；失败不留部分写入。

运行：python -m tests.test_time_precision
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_time_precision.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def post(path, **kw):
    return client.post(f"{API}{path}", **kw)


def get(path):
    return client.get(f"{API}{path}")


# ====================================================================== 1) 时间归一

def test_unit_normalization():
    print("1) 时间归一（时区、无时区、小数秒、真实瞬间）")
    from app.timefmt import to_utc_iso, parse_iso, instant_eq, instant_lt, seconds_between
    from datetime import datetime, timedelta, timezone

    tz8 = timezone(timedelta(hours=8))
    # 整秒：与历史整秒规范字节一致
    check("整秒串保持秒精度 Z",
          to_utc_iso(datetime(2026, 10, 1, 10, 0, 0)) == "2026-10-01T10:00:00Z")
    # 无时区按 UTC
    check("无时区输入按 UTC 解释",
          to_utc_iso(datetime(2026, 10, 1, 10, 0, 0, 100000)) == "2026-10-01T10:00:00.100000Z")
    # 带时区偏移按真实瞬间换算
    check("带 +08:00 偏移归 UTC",
          to_utc_iso(datetime(2026, 10, 1, 18, 0, 0, 100000, tzinfo=tz8))
          == "2026-10-01T10:00:00.100000Z")
    # 不同时区表示的同一瞬间相等
    a = parse_iso("2026-10-01T10:00:00.100000Z")
    b = parse_iso("2026-10-01T18:00:00.100000+08:00")
    check("不同时区表示同一瞬间相等", instant_eq(a, b))
    # 同秒不同小数 → 先后不同（关键回归点）
    check(".200 严格晚于 .100", instant_lt("2026-10-01T10:00:00.100000Z",
                                            "2026-10-01T10:00:00.200000Z"))
    check(".300 严格晚于 .200", instant_lt("2026-10-01T10:00:00.200000Z",
                                            "2026-10-01T10:00:00.300000Z"))
    check("整秒严格晚于同秒 .999999 为假",
          not instant_lt("2026-10-01T10:00:00.000000Z", "2026-10-01T10:00:00Z")
          and instant_eq("2026-10-01T10:00:00.000000Z", "2026-10-01T10:00:00Z"))
    # 小数秒间隔精度
    check("0.5 秒间隔精确计算",
          abs(seconds_between("2026-10-01T10:00:00.000000Z",
                              "2026-10-01T10:00:00.500000Z") - 0.5) < 1e-9)


# ====================================================================== 2) 保管链同秒

def test_chain_same_second():
    print("2) 保管链：采集 .100 → 移交 .200 → 签收 .300 同秒不同瞬间")
    code = "S-FRAC-1"
    r = post("/samples", json={
        "sample_code": code,
        "collected_at": "2026-10-01T10:00:00.100Z",
        "collector": "王采集", "seal_no": "SEAL-F1"})
    check("登记 201 且采集时间保留 .100",
          r.status_code == 201 and r.json()["sample"]["collected_at"] == "2026-10-01T10:00:00.100000Z",
          r.text)

    r = post("/events", json={
        "event_id": "EVT-FT1", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-10-01T10:00:00.200Z",
        "transfer_no": "TR-F1", "from_custodian": "王采集", "to_custodian": "李运输"})
    check("移交 .200 落库 201", r.status_code == 201, r.text)

    r = post("/events", json={
        "event_id": "EVT-FR1", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-10-01T10:00:00.300Z",
        "transfer_no": "TR-F1", "receiver": "李运输", "seal_no": "SEAL-F1"})
    check("签收 .300 被接受（严格晚于移交 .200，不再因截断误判）",
          r.status_code == 201, r.text)

    r = post("/events", json={
        "event_id": "EVT-FC1", "event_type": "seal_check", "sample_code": code,
        "occurred_at": "2026-10-01T10:00:00.400Z",
        "checked_seal_no": "SEAL-F1", "seal_intact": True})
    check("封签检查 .400 201", r.status_code == 201, r.text)

    r = post("/events", json={
        "event_id": "EVT-FS1", "event_type": "storage", "sample_code": code,
        "occurred_at": "2026-10-01T10:00:00.500Z",
        "custodian": "实验室"})
    check("入库 .500 201", r.status_code == 201, r.text)

    chain = get(f"/samples/{code}/chain").json()
    check("同秒小数链核验结论 valid（签收未被判不晚于移交）",
          chain["status"] == "valid", str(chain.get("reasons")))
    times = [e["occurred_at"] for e in chain["timeline"]]
    from app.timefmt import parse_iso as _p
    check("时间线按真实瞬间升序",
          times == sorted(times, key=_p),
          str(times))

    # 签收早于/等于移交的真实瞬间仍拒绝（不晚于）
    bad = post("/events", json={
        "event_id": "EVT-FR2", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-10-01T10:00:00.200Z",
        "transfer_no": "TR-F1", "receiver": "李运输", "seal_no": "SEAL-F1"})
    # 该单已签收，重复签收也是 invalid；这里关键是不产生 valid。再用一个新样本隔离验“不晚于”
    code2 = "S-FRAC-2"
    post("/samples", json={
        "sample_code": code2, "collected_at": "2026-10-01T10:00:00.100Z",
        "collector": "王采集", "seal_no": "SEAL-F2"})
    post("/events", json={
        "event_id": "EVT-FT2", "event_type": "transfer", "sample_code": code2,
        "occurred_at": "2026-10-01T10:00:00.200Z",
        "transfer_no": "TR-F2", "from_custodian": "王采集", "to_custodian": "李运输"})
    r = post("/events", json={
        "event_id": "EVT-FR2B", "event_type": "receive", "sample_code": code2,
        "occurred_at": "2026-10-01T10:00:00.200Z",
        "transfer_no": "TR-F2", "receiver": "李运输", "seal_no": "SEAL-F2"})
    check("签收瞬间==移交瞬间（不同读数号、同小数）仍为无效 receive（落库但核验 invalid）",
          r.status_code == 201, r.text)
    chain2 = get(f"/samples/{code2}/chain").json()
    check("同瞬间签收核验 invalid（不晚于移交）",
          chain2["status"] == "invalid"
          and any("不晚于移交时间" in x for x in chain2["reasons"]),
          str(chain2.get("reasons")))

    # 带时区表示：移交 18:00:00.200+08:00 == 10:00:00.200Z；签收 18:00:00.300+08:00
    code3 = "S-FRAC-3"
    post("/samples", json={
        "sample_code": code3, "collected_at": "2026-10-01T18:00:00.100+08:00",
        "collector": "王采集", "seal_no": "SEAL-F3"})
    r = get(f"/samples/{code3}").json()
    check("带时区采集归一并保留 .100",
          r["sample"]["collected_at"] == "2026-10-01T10:00:00.100000Z", r["sample"]["collected_at"])


# ====================================================================== 3) 测温读数去重 / 缺测

def test_readings_precision():
    print("3) 测温读数：同秒不同瞬间不重复；真实同瞬间仍冲突；缺测严格大于")
    code = "S-FRAC-T1"
    post("/samples", json={
        "sample_code": code, "collected_at": "2026-10-01T10:00:00Z",
        "collector": "王采集", "seal_no": "SEAL-T1"})
    post("/temperature-configs", json={
        "sample_code": code, "min_celsius": 2.0, "max_celsius": 8.0,
        "max_interval_seconds": 100})

    # 同秒不同瞬间：.100 / .200 / .300 三条均落库（旧逻辑会被截断为重复）
    r1 = post("/temperature-readings", json={
        "sample_code": code, "reading_id": "RD-F1", "device_id": "DEV-F1",
        "occurred_at": "2026-10-01T10:00:10.100Z", "value_celsius": 4.0})
    r2 = post("/temperature-readings", json={
        "sample_code": code, "reading_id": "RD-F2", "device_id": "DEV-F1",
        "occurred_at": "2026-10-01T10:00:10.200Z", "value_celsius": 5.0})
    r3 = post("/temperature-readings", json={
        "sample_code": code, "reading_id": "RD-F3", "device_id": "DEV-F1",
        "occurred_at": "2026-10-01T10:00:10.300Z", "value_celsius": 6.0})
    check("同秒 .100 读数 201", r1.status_code == 201, r1.text)
    check("同秒 .200 读数不被误判重复 201", r2.status_code == 201, r2.text)
    check("同秒 .300 读数不被误判重复 201", r3.status_code == 201, r3.text)

    # 真实同一瞬间（不同时区表示）→ 仍判 reading_time_duplicate 409
    rdup = post("/temperature-readings", json={
        "sample_code": code, "reading_id": "RD-F4", "device_id": "DEV-F1",
        "occurred_at": "2026-10-01T18:00:10.200+08:00", "value_celsius": 5.0})
    check("真实同一瞬间（+08:00 表示）仍 409 reading_time_duplicate",
          rdup.status_code == 409 and rdup.json()["error"]["code"] == "reading_time_duplicate",
          rdup.text)

    # 同读数号同内容（整秒归一后一致）重试幂等 200
    ridem = post("/temperature-readings", json={
        "sample_code": code, "reading_id": "RD-F1", "device_id": "DEV-F1",
        "occurred_at": "2026-10-01T18:00:10.100+08:00", "value_celsius": 4.0})
    check("同读数号同内容（不同时区表示同瞬间）重试 200 幂等",
          ridem.status_code == 200 and ridem.json()["idempotent"] is True, ridem.text)

    # 缺测：间隔恰好 100.000 秒不报；100.001 秒报（严格大于，小数精度）
    code2 = "S-FRAC-T2"
    post("/samples", json={
        "sample_code": code2, "collected_at": "2026-10-01T09:00:00Z",
        "collector": "王采集", "seal_no": "SEAL-T2"})
    post("/temperature-configs", json={
        "sample_code": code2, "min_celsius": 2.0, "max_celsius": 8.0,
        "max_interval_seconds": 100})
    post("/temperature-readings", json={
        "sample_code": code2, "reading_id": "RD-G1", "device_id": "DEV-F2",
        "occurred_at": "2026-10-01T10:00:00.000Z", "value_celsius": 4.0})
    # 恰好 100 秒整（等于限值）→ 不报缺测
    post("/temperature-readings", json={
        "sample_code": code2, "reading_id": "RD-G2", "device_id": "DEV-F2",
        "occurred_at": "2026-10-01T10:01:40.000Z", "value_celsius": 4.0})
    rev = get(f"/samples/{code2}/temperature").json()
    check("间隔恰好=限值（100s）不报缺测",
          all(g["before_reading_id"] != "RD-G1" for g in rev["gap_intervals"]),
          str(rev["gap_intervals"]))
    # 再加一条比 RD-G2 晚 100.001 秒 → 这一段严格大于 → 报缺测
    post("/temperature-readings", json={
        "sample_code": code2, "reading_id": "RD-G3", "device_id": "DEV-F2",
        "occurred_at": "2026-10-01T10:03:20.001Z", "value_celsius": 4.0})
    rev = get(f"/samples/{code2}/temperature").json()
    g23 = [g for g in rev["gap_intervals"]
           if g["before_reading_id"] == "RD-G2" and g["after_reading_id"] == "RD-G3"]
    check("间隔 100.001s 严格大于限值 → 报缺测（小数精度）",
          len(g23) == 1 and g23[0]["duration_seconds"] == 100,
          str(rev["gap_intervals"]))


# ====================================================================== 4) 校准证明边界

def test_calibration_precision():
    print("4) 校准证明：左闭右开覆盖与重叠按真实瞬间判定")
    code = "S-FRAC-C1"
    post("/samples", json={
        "sample_code": code, "collected_at": "2026-10-01T09:00:00Z",
        "collector": "王采集", "seal_no": "SEAL-C1"})
    post("/temperature-configs", json={
        "sample_code": code, "min_celsius": 2.0, "max_celsius": 8.0,
        "max_interval_seconds": 1800})
    post("/temperature-readings", json={
        "sample_code": code, "reading_id": "RD-C1", "device_id": "DEV-C1",
        "occurred_at": "2026-10-01T10:00:00.500Z", "value_celsius": 4.0})

    # [00.000, 00.500)：结束点不含 → 读数在 .500 不被覆盖（after/边界）
    r = post("/temperature-calibrations", json={
        "certificate_id": "CAL-C1", "device_id": "DEV-C1",
        "valid_from": "2026-10-01T10:00:00.000Z",
        "valid_to": "2026-10-01T10:00:00.500Z",
        "issued_by": "计量员"})
    check("证明1 左闭右开登记 201", r.status_code == 201, r.text)
    trusted = get(f"/samples/{code}/temperature/trusted").json()
    m = next(x for x in trusted["reading_matches"] if x["reading_id"] == "RD-C1")
    check("读数恰在 valid_to（.500）不被覆盖（左闭右开）",
          m["certified"] is False and m["missing_reason"]["code"] == "after_calibration_validity",
          str(m))

    # 端点相邻允许：新证明 [00.500, 01.000) 与前一个 valid_to 相邻 → 不重叠
    r = post("/temperature-calibrations", json={
        "certificate_id": "CAL-C2", "device_id": "DEV-C1",
        "valid_from": "2026-10-01T10:00:00.500Z",
        "valid_to": "2026-10-01T10:00:01.000Z",
        "issued_by": "计量员"})
    check("端点相邻（前 valid_to == 新 valid_from）允许", r.status_code == 201, r.text)
    trusted = get(f"/samples/{code}/temperature/trusted").json()
    m = next(x for x in trusted["reading_matches"] if x["reading_id"] == "RD-C1")
    check("读数 .500 被相邻新证明（左闭）覆盖",
          m["certified"] is True and m["certificate_id"] == "CAL-C2", str(m))

    # 小数秒重叠拒绝：与 CAL-C2 在 .700-.900 段重叠
    r = post("/temperature-calibrations", json={
        "certificate_id": "CAL-C3", "device_id": "DEV-C1",
        "valid_from": "2026-10-01T10:00:00.700Z",
        "valid_to": "2026-10-01T10:00:00.900Z",
        "issued_by": "计量员"})
    check("小数秒重叠 409 calibration_overlap",
          r.status_code == 409 and r.json()["error"]["code"] == "calibration_overlap",
          r.text)
    # 失败不留部分记录：证明号 CAL-C3 不存在
    check("重叠失败不留证明记录",
          get("/temperature-calibrations/CAL-C3").status_code == 404)


# ====================================================================== 5) 整秒兼容 / 无效时间

def test_whole_second_compat_and_invalid():
    print("5) 整秒记录兼容 + 无效时间拒绝")
    code = "S-FRAC-W1"
    post("/samples", json={
        "sample_code": code, "collected_at": "2026-10-01T10:00:00Z",
        "collector": "王采集", "seal_no": "SEAL-W1"})
    r = post("/events", json={
        "event_id": "EVT-W1", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-10-01T11:00:00Z",
        "transfer_no": "TR-W1", "from_custodian": "王采集", "to_custodian": "李运输"})
    check("整秒事件落库 201", r.status_code == 201, r.text)
    check("整秒 occurred_at 串保持秒精度（历史格式）",
          r.json()["event"]["occurred_at"] == "2026-10-01T11:00:00Z", r.text)
    # 同号同内容（整秒）重试 → 200 幂等
    r2 = post("/events", json={
        "event_id": "EVT-W1", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-10-01T11:00:00Z",
        "transfer_no": "TR-W1", "from_custodian": "王采集", "to_custodian": "李运输"})
    check("整秒同号同内容重试 200 幂等（行为保持）",
          r2.status_code == 200 and r2.json()["idempotent"] is True, r2.text)

    # 无效时间字符串仍 422
    r = post("/events", json={
        "event_id": "EVT-BAD", "event_type": "storage", "sample_code": code,
        "occurred_at": "not-a-time", "custodian": "实验室"})
    check("无效时间字符串 422", r.status_code == 422, r.text)
    # 失败不留部分写入：该事件号不存在（用同号再发一个合法 storage 应为 201 而非冲突）
    r = post("/events", json={
        "event_id": "EVT-BAD", "event_type": "storage", "sample_code": code,
        "occurred_at": "2026-10-01T12:00:00Z", "custodian": "实验室"})
    check("422 失败不占事件号（同号合法提交 201）", r.status_code == 201, r.text)


def main():
    test_unit_normalization()
    test_chain_same_second()
    test_readings_precision()
    test_calibration_precision()
    test_whole_second_compat_and_invalid()
    print(f"\n时间精度自测：通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
