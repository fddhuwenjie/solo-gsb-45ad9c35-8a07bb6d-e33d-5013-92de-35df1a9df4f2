"""烧成实绩登记：逐批判定、安排锁定、并发裁决验证（临时 SQLite，结束后删除）。"""
import os
import tempfile
from threading import Thread

_tmp = tempfile.mkdtemp()
os.environ["KILN_DB_PATH"] = os.path.join(_tmp, "result.db")

from app import create_app

app = create_app()
c = app.test_client()

passed = []
def check(name, cond, detail=""):
    assert cond, f"FAIL: {name} {detail}"
    passed.append(name)

# ---------- 准备：批次、已确认窑次、烧成安排 ----------
def add_batch(code, pmin, pmax, hmin, hmax):
    r = c.post("/api/batches", json={
        "code": code, "peak_min": pmin, "peak_max": pmax,
        "hold_min": hmin, "hold_max": hmax,
    })
    assert r.status_code == 201, r.data
    return r.get_json()

def confirm_load(name, kiln_id, batch_pos):
    d = c.post("/api/drafts", json={"name": name, "kiln_id": kiln_id}).get_json()
    for b, pos in batch_pos:
        c.put(f"/api/drafts/{d['id']}/items", json={"batch_id": b["id"], "pos_no": pos})
    p = c.get(f"/api/drafts/{d['id']}/preview").get_json()
    assert p["ready"], p
    snap = {"draft_version": p["draft_version"],
            "items": [{"batch_id": i["batch_id"], "batch_version": i["batch_version"],
                       "pos_no": i["pos_no"]} for i in p["items"]]}
    r = c.post(f"/api/drafts/{d['id']}/confirm", json=snap)
    assert r.status_code == 201, r.data
    return r.get_json()

def add_schedule(load_id, start, end, peak, hold):
    r = c.post("/api/schedules", json={
        "load_id": load_id, "start_at": start, "end_at": end,
        "peak_temp": peak, "hold_minutes": hold,
    })
    assert r.status_code == 201, r.data
    return r.get_json()

def snap_of(s):
    """页面读取的安排时段与拟用参数（登记时须携带）。"""
    return {"start_at": s["start_at"], "end_at": s["end_at"],
            "peak_temp": s["peak_temp"], "hold_minutes": s["hold_minutes"]}

def post_result(sched_id, snap, actual_peak, actual_hold):
    return c.post(f"/api/schedules/{sched_id}/result",
                  json={**snap, "actual_peak": actual_peak, "actual_hold": actual_hold})

kilns = c.get("/api/kilns").get_json()
k1, k2 = kilns[0]["id"], kilns[1]["id"]

ra = add_batch("RA", 1200, 1250, 30, 45)
rb = add_batch("RB", 1220, 1260, 30, 40)
load1 = confirm_load("RL1", k1, [(ra, 1), (rb, 2)])   # 共同区间：峰 1220~1250，保温 30~40
sched1 = add_schedule(load1["id"], "2026-10-08T08:00:00+08:00",
                      "2026-10-08T12:00:00+08:00", 1240, 35)

# ---------- 1. 未登记时查询 ----------
r = c.get(f"/api/schedules/{sched1['id']}/result")
check("未登记查询返回 404", r.status_code == 404)

# ---------- 2. 实测值合法性 ----------
r = post_result(sched1["id"], snap_of(sched1), -1, 35)
check("实际峰温为负被拒", r.status_code == 400)
r = post_result(sched1["id"], snap_of(sched1), "hot", 35)
check("实际峰温非数字被拒", r.status_code == 400)
r = post_result(sched1["id"], snap_of(sched1), 1240, 30.5)
check("实际保温非整数被拒", r.status_code == 400)
r = post_result(sched1["id"], snap_of(sched1), 1240, -5)
check("实际保温为负被拒", r.status_code == 400)
r = post_result(sched1["id"], snap_of(sched1), 1240, True)
check("实际保温布尔值被拒", r.status_code == 400)
r = post_result(99999, snap_of(sched1), 1240, 35)
check("安排不存在返回 404", r.status_code == 404)
bad_snap = {k: v for k, v in snap_of(sched1).items() if k != "start_at"}
r = c.post(f"/api/schedules/{sched1['id']}/result",
           json={**bad_snap, "actual_peak": 1240, "actual_hold": 35})
check("缺少携带的安排时段被拒", r.status_code == 400)
r = c.get(f"/api/schedules/{sched1['id']}/result")
check("校验失败不留部分记录", r.status_code == 404)

# ---------- 3. 成功登记：超界实测值如实保存并逐批判定 ----------
# RA 峰温区间 [1200,1250]，RB [1220,1260]；实测 1255 → RA 超界、RB 在界内
r = post_result(sched1["id"], snap_of(sched1), 1255, 35)
check("登记成功", r.status_code == 201, r.data)
res1 = r.get_json()
check("系统记下登记时间", bool(res1.get("registered_at")), res1)
check("超出区间的实测值如实保存", res1["actual_peak"] == 1255 and res1["actual_hold"] == 35)
check("返回拟用值供对照", res1["peak_temp"] == 1240 and res1["hold_minutes"] == 35)
items = {i["batch_code"]: i for i in res1["items"]}
check("逐批判定：RA 峰温超界、保温在界内",
      items["RA"]["peak_ok"] is False and items["RA"]["hold_ok"] is True, items)
check("逐批判定：RB 全部在界内",
      items["RB"]["peak_ok"] is True and items["RB"]["hold_ok"] is True)
check("判定区间取自确认清单快照",
      items["RA"]["peak_min"] == 1200 and items["RA"]["peak_max"] == 1250)

s1 = next(s for s in c.get("/api/schedules").get_json() if s["id"] == sched1["id"])
check("安排列表标记已登记", s1["result_id"] == res1["id"])

# ---------- 4. 每个安排只能登记一次 ----------
r = post_result(sched1["id"], snap_of(sched1), 1240, 35)
check("重复登记被拒", r.status_code == 409 and "只能登记一次" in r.get_json()["error"])

# ---------- 5. 登记成功后安排不得修改 ----------
r = c.put(f"/api/schedules/{sched1['id']}", json={
    "start_at": "2026-10-09T08:00:00+08:00", "end_at": "2026-10-09T12:00:00+08:00",
    "peak_temp": 1240, "hold_minutes": 35})
check("登记后修改安排被拒", r.status_code == 409 and "实绩" in r.get_json()["error"])
cur = next(s for s in c.get("/api/schedules").get_json() if s["id"] == sched1["id"])
check("被拒后原安排不变", cur["start_at"] == sched1["start_at"], cur)

# ---------- 6. 批次后续修改范围不改变判定 ----------
ra_now = next(x for x in c.get("/api/batches").get_json() if x["code"] == "RA")
r = c.put(f"/api/batches/{ra_now['id']}", json={"peak_min": 1200, "peak_max": 1300,
                                                "hold_min": 30, "hold_max": 45,
                                                "version": ra_now["version"]})
check("批次范围可修改", r.status_code == 200)
r = c.get(f"/api/schedules/{sched1['id']}/result")
items = {i["batch_code"]: i for i in r.get_json()["items"]}
check("批次改宽后判定不变（RA 仍超界）", items["RA"]["peak_ok"] is False)

# 登记发生在批次修改之后：判定仍以确认时快照为准
rc = add_batch("RC", 1200, 1250, 20, 50)
load2 = confirm_load("RL2", k2, [(rc, 1)])
rc_now = next(x for x in c.get("/api/batches").get_json() if x["code"] == "RC")
c.put(f"/api/batches/{rc_now['id']}", json={"peak_min": 1200, "peak_max": 1300,
                                            "hold_min": 20, "hold_max": 50,
                                            "version": rc_now["version"]})
sched2 = add_schedule(load2["id"], "2026-10-08T08:00:00+08:00",
                      "2026-10-08T12:00:00+08:00", 1240, 35)
r = post_result(sched2["id"], snap_of(sched2), 1280, 35)
check("按快照判定：1280 超出确认时区间 [1200,1250] 记超界（不取批次新范围）",
      r.status_code == 201 and r.get_json()["items"][0]["peak_ok"] is False, r.data)

# ---------- 7. 提交前安排有变动：提示刷新，不生成实绩 ----------
rd = add_batch("RD", 1200, 1260, 20, 50)
load3 = confirm_load("RL3", k1, [(rd, 1)])
sched3 = add_schedule(load3["id"], "2026-10-10T08:00:00+08:00",
                      "2026-10-10T12:00:00+08:00", 1240, 35)
old_snap = snap_of(sched3)
r = c.put(f"/api/schedules/{sched3['id']}", json={
    "start_at": "2026-10-11T08:00:00+08:00", "end_at": "2026-10-11T12:00:00+08:00",
    "peak_temp": 1250, "hold_minutes": 40})
check("登记前安排被修改", r.status_code == 200, r.data)
r = post_result(sched3["id"], old_snap, 1240, 35)
check("携带旧快照登记被拒并提示刷新",
      r.status_code == 409 and r.get_json()["stale"] is True, r.data)
r = c.get(f"/api/schedules/{sched3['id']}/result")
check("变动冲突不生成实绩", r.status_code == 404)
sched3_now = next(s for s in c.get("/api/schedules").get_json() if s["id"] == sched3["id"])
r = post_result(sched3["id"], snap_of(sched3_now), 1240, 35)
check("刷新后按最新安排登记成功", r.status_code == 201, r.data)

# ---------- 8. 并发登记同一安排：只有一个成功 ----------
re_ = add_batch("RE", 1200, 1260, 20, 50)
load4 = confirm_load("RL4", k2, [(re_, 1)])
sched4 = add_schedule(load4["id"], "2026-10-12T08:00:00+08:00",
                      "2026-10-12T12:00:00+08:00", 1240, 35)
snap4 = snap_of(sched4)
results = {}
barrier = []
def race_register(key):
    client = app.test_client()
    barrier.append(1)
    while len(barrier) < 2:
        pass
    r = client.post(f"/api/schedules/{sched4['id']}/result",
                    json={**snap4, "actual_peak": 1240, "actual_hold": 35})
    results[key] = r.status_code

t1 = Thread(target=race_register, args=("甲",))
t2 = Thread(target=race_register, args=("乙",))
t1.start(); t2.start()
t1.join(); t2.join()
print("并发登记:", results)
check("并发登记只有一个成功", sorted(results.values()) == [201, 409], results)
r = c.get(f"/api/schedules/{sched4['id']}/result")
check("并发后只有一份实绩且明细完整",
      r.status_code == 200 and len(r.get_json()["items"]) == 1)

# ---------- 9. 登记与排程修改交错：以数据库最新状态裁决，恰好一方成功 ----------
rf = add_batch("RF", 1200, 1260, 20, 50)
load5 = confirm_load("RL5", k1, [(rf, 1)])
sched5 = add_schedule(load5["id"], "2026-10-13T08:00:00+08:00",
                      "2026-10-13T12:00:00+08:00", 1240, 35)
snap5 = snap_of(sched5)
results = {}
barrier = []
def do_put():
    client = app.test_client()
    barrier.append(1)
    while len(barrier) < 2:
        pass
    r = client.put(f"/api/schedules/{sched5['id']}", json={
        "start_at": "2026-10-14T08:00:00+08:00", "end_at": "2026-10-14T12:00:00+08:00",
        "peak_temp": 1250, "hold_minutes": 40})
    results["put"] = r.status_code

def do_post():
    client = app.test_client()
    barrier.append(1)
    while len(barrier) < 2:
        pass
    r = client.post(f"/api/schedules/{sched5['id']}/result",
                    json={**snap5, "actual_peak": 1240, "actual_hold": 35})
    results["post"] = r.status_code

t1 = Thread(target=do_put)
t2 = Thread(target=do_post)
t1.start(); t2.start()
t1.join(); t2.join()
print("登记与修改交错:", results)
codes = sorted(results.values())
check("交错时恰好一方成功", codes == [200, 409] or codes == [201, 409], results)
final = next(s for s in c.get("/api/schedules").get_json() if s["id"] == sched5["id"])
has_result = c.get(f"/api/schedules/{sched5['id']}/result").status_code == 200
if results["post"] == 201:
    check("登记胜出：安排保持原样且已有实绩",
          has_result and final["start_at"] == sched5["start_at"], final)
else:
    check("修改胜出：安排已更新且无实绩",
          not has_result and final["start_at"] == "2026-10-14T00:00:00.000000Z", final)

# ---------- 10. 重启后仍可查询 ----------
app2 = create_app()
c2 = app2.test_client()
r = c2.get(f"/api/schedules/{sched1['id']}/result")
check("重启后实绩可查", r.status_code == 200)
body = r.get_json()
check("重启后逐批判定保留",
      {i["batch_code"]: (i["peak_ok"], i["hold_ok"]) for i in body["items"]}
      == {"RA": (False, True), "RB": (True, True)}, body)
check("重启后实测值与登记时间保留",
      body["actual_peak"] == 1255 and body["registered_at"] == res1["registered_at"])
s1 = next(s for s in c2.get("/api/schedules").get_json() if s["id"] == sched1["id"])
check("重启后安排仍标记已登记", s1["result_id"] == res1["id"])

print(f"\n✅ 全部 {len(passed)} 项检查通过")
for n in passed:
    print("  -", n)
