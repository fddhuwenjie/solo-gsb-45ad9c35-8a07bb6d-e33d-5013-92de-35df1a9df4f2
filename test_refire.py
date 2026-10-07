"""复烧批次：超界明细生成、区间继承、唯一约束、并发裁决与双向追溯验证。

（临时 SQLite，结束后删除；不依赖 pytest，直接 `python test_refire.py` 运行）
"""
import os
import tempfile
from threading import Thread

_tmp = tempfile.mkdtemp()
os.environ["KILN_DB_PATH"] = os.path.join(_tmp, "refire.db")

from app import create_app

app = create_app()
c = app.test_client()

passed = []
def check(name, cond, detail=""):
    assert cond, f"FAIL: {name} {detail}"
    passed.append(name)

# ---------- 准备 ----------
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
    return {"start_at": s["start_at"], "end_at": s["end_at"],
            "peak_temp": s["peak_temp"], "hold_minutes": s["hold_minutes"]}

def register(sched, actual_peak, actual_hold):
    r = c.post(f"/api/schedules/{sched['id']}/result",
               json={**snap_of(sched), "actual_peak": actual_peak,
                     "actual_hold": actual_hold})
    assert r.status_code == 201, r.data
    return r.get_json()

def refire(item_id, code):
    return c.post(f"/api/result-items/{item_id}/refire", json={"code": code})

kilns = c.get("/api/kilns").get_json()
k1, k2 = kilns[0]["id"], kilns[1]["id"]

# 实绩 1：RA 峰温区间 [1200,1250]、保温 [30,45]；RB 峰温 [1220,1260]、保温 [30,40]
# 实测峰温 1255、保温 50 → RA 峰温超界+保温超界，RB 仅保温超界
ra = add_batch("RA", 1200, 1250, 30, 45)
rb = add_batch("RB", 1220, 1260, 30, 40)
load1 = confirm_load("RL1", k1, [(ra, 1), (rb, 2)])
sched1 = add_schedule(load1["id"], "2026-10-08T08:00:00+08:00",
                      "2026-10-08T12:00:00+08:00", 1240, 35)
res = register(sched1, 1255, 50)
items = {i["batch_code"]: i for i in res["items"]}
item_ra, item_rb = items["RA"], items["RB"]
check("实绩明细带 id（用于在其旁生成）", isinstance(item_ra["result_item_id"], int))
assert item_ra["peak_ok"] is False and item_ra["hold_ok"] is False
assert item_rb["peak_ok"] is True and item_rb["hold_ok"] is False

# ---------- 1. 入参校验 ----------
r = refire(999999, "NEW-X")
check("实绩明细不存在返回 404", r.status_code == 404)
r = refire(item_ra["result_item_id"], "   ")
check("编号为空被拒", r.status_code == 400)

# 实绩 2：RC 全部在界内，不能生成
rc = add_batch("RC", 1200, 1260, 20, 50)
load2 = confirm_load("RL2", k2, [(rc, 1)])
sched2 = add_schedule(load2["id"], "2026-10-09T08:00:00+08:00",
                      "2026-10-09T12:00:00+08:00", 1240, 35)
res2 = register(sched2, 1240, 35)
item_rc = res2["items"][0]
assert item_rc["peak_ok"] and item_rc["hold_ok"]
r = refire(item_rc["result_item_id"], "RC-R")
check("界内明细不能生成复烧批次", r.status_code == 409 and "界内" in r.get_json()["error"])

# ---------- 2. 成功生成：继承确认清单中该批的工艺闭区间 ----------
r = refire(item_ra["result_item_id"], "RA-REFIRE")
check("生成成功", r.status_code == 201, r.data)
body = r.get_json()
nb = body["batch"]
src = body["source"]
check("新批次编号", nb["code"] == "RA-REFIRE")
check("继承峰温闭区间（取自确认清单快照）",
      (nb["peak_min"], nb["peak_max"]) == (1200.0, 1250.0), nb)
check("继承保温闭区间", (nb["hold_min"], nb["hold_max"]) == (30, 45), nb)
check("来源指向原批次与原明细",
      src["source_batch_id"] == ra["id"] and src["source_batch_code"] == "RA"
      and src["pos_no"] == 1, src)
check("来源记录实绩/窑次/安排",
      src["result_id"] == res["id"] and src["schedule_id"] == sched1["id"]
      and src["load_id"] == load1["id"] and src["kiln_code"] == "K1", src)
check("来源固化实测值与判定",
      src["actual_peak"] == 1255 and src["actual_hold"] == 50
      and src["peak_ok"] is False and src["hold_ok"] is False, src)
check("生成动作记录时间", bool(nb.get("created_at")) and "created_at" in body.get("batch", {}))

# ---------- 3. 每条超界明细最多一个 ----------
r = refire(item_ra["result_item_id"], "RA-REFIRE-2")
check("同一明细重复生成被拒",
      r.status_code == 409 and "最多生成一个" in r.get_json()["error"])
codes = {b["code"] for b in c.get("/api/batches").get_json()}
check("被拒不产生新批次", "RA-REFIRE-2" not in codes, codes)

# ---------- 4. 重复编号不能生成 ----------
r = refire(item_rb["result_item_id"], "RA-REFIRE")
check("新编号与已有批次重复被拒", r.status_code == 409 and "已存在" in r.get_json()["error"])
# 该明细仍可生成（前一次失败不占用它）
r = refire(item_rb["result_item_id"], "RB-REFIRE")
check("失败后该明细仍可生成", r.status_code == 201, r.data)
rb_ref = r.get_json()["batch"]
check("RB 复烧继承其自身区间",
      (rb_ref["peak_min"], rb_ref["peak_max"], rb_ref["hold_min"], rb_ref["hold_max"])
      == (1220.0, 1260.0, 30, 40))

# ---------- 5. 失败不留部分记录：refire_batches 与 batches 一致 ----------
import sqlite3
raw = sqlite3.connect(os.environ["KILN_DB_PATH"])
n_ref = raw.execute("SELECT COUNT(*) FROM refire_batches").fetchone()[0]
n_orphan_batch = raw.execute(
    "SELECT COUNT(*) FROM refire_batches rb WHERE NOT EXISTS "
    "(SELECT 1 FROM batches b WHERE b.id = rb.batch_id)").fetchone()[0]
n_orphan_item = raw.execute(
    "SELECT COUNT(*) FROM refire_batches rb WHERE NOT EXISTS "
    "(SELECT 1 FROM firing_result_items fri WHERE fri.id = rb.result_item_id)").fetchone()[0]
check("成功的复烧关系数=2", n_ref == 2, n_ref)
check("无悬空来源记录/批次", n_orphan_batch == 0 and n_orphan_item == 0)
raw.close()

# ---------- 6. 原批次仍归属原窑次；新批次可走草案→确认→排程→实绩 ----------
ra_after = next(b for b in c.get("/api/batches").get_json() if b["code"] == "RA")
check("原批次仍归属原窑次", ra_after["used_by_load"] == load1["id"])
check("新批次未被占用，可进草案", nb["id"] != ra["id"] and next(
    b for b in c.get("/api/batches").get_json() if b["code"] == "RA-REFIRE")["used_by_load"] is None)

d = c.post("/api/drafts", json={"name": "D-REFIRE", "kiln_id": k2}).get_json()
r = c.put(f"/api/drafts/{d['id']}/items", json={"batch_id": nb["id"], "pos_no": 1})
check("复烧批次可放入草案", r.status_code == 200)
p = c.get(f"/api/drafts/{d['id']}/preview").get_json()
check("复烧批次草案预览就绪", p["ready"] is True, p)
snap = {"draft_version": p["draft_version"],
        "items": [{"batch_id": i["batch_id"], "batch_version": i["batch_version"],
                   "pos_no": i["pos_no"]} for i in p["items"]]}
r = c.post(f"/api/drafts/{d['id']}/confirm", json=snap)
check("复烧批次可确认生成新窑次", r.status_code == 201, r.data)
ref_load = r.get_json()
r = c.post("/api/schedules", json={
    "load_id": ref_load["id"], "start_at": "2026-10-20T08:00:00+08:00",
    "end_at": "2026-10-20T12:00:00+08:00", "peak_temp": 1230, "hold_minutes": 35})
check("复烧窑次可排程", r.status_code == 201, r.data)
ref_sched = r.get_json()
r = c.post(f"/api/schedules/{ref_sched['id']}/result",
           json={**snap_of(ref_sched), "actual_peak": 1230, "actual_hold": 35})
check("复烧窑次可登记实绩", r.status_code == 201, r.data)

# ---------- 7. 原批次后来修改范围不影响新批次 ----------
ra_now = next(b for b in c.get("/api/batches").get_json() if b["code"] == "RA")
r = c.put(f"/api/batches/{ra_now['id']}", json={"peak_min": 1100, "peak_max": 1290,
                                                "hold_min": 10, "hold_max": 90,
                                                "version": ra_now["version"]})
check("原批次范围可修改（版本递增）", r.status_code == 200)
nb_now = next(b for b in c.get("/api/batches").get_json() if b["code"] == "RA-REFIRE")
check("新批次区间不随原批次修改而变化",
      (nb_now["peak_min"], nb_now["peak_max"], nb_now["hold_min"], nb_now["hold_max"])
      == (1200.0, 1250.0, 30, 45), nb_now)

# 生成发生在原批次修改之后：仍按确认清单快照继承，不取当前范围
rd = add_batch("RD", 1200, 1250, 20, 40)
load3 = confirm_load("RL3", k1, [(rd, 3)])
sched3 = add_schedule(load3["id"], "2026-10-10T08:00:00+08:00",
                      "2026-10-10T12:00:00+08:00", 1240, 35)
res3 = register(sched3, 1300, 35)
item_rd = res3["items"][0]
rd_now = next(b for b in c.get("/api/batches").get_json() if b["code"] == "RD")
c.put(f"/api/batches/{rd_now['id']}", json={"peak_min": 1290, "peak_max": 1310,
                                            "hold_min": 20, "hold_max": 40,
                                            "version": rd_now["version"]})
r = refire(item_rd["result_item_id"], "RD-REFIRE")
check("生成时继承确认时区间（不取原批次后来改宽的范围）",
      r.status_code == 201
      and (r.get_json()["batch"]["peak_min"], r.get_json()["batch"]["peak_max"])
      == (1200.0, 1250.0), r.data)

# ---------- 8. 复烧批次即使尚未确认也不可删除 ----------
r = c.delete(f"/api/batches/{nb['id']}")
check("已确认的复烧批次按已用批次规则不可删", r.status_code == 409)
unconf = next(b for b in c.get("/api/batches").get_json() if b["code"] == "RD-REFIRE")
check("未确认复烧批次不在任何窑次", unconf["used_by_load"] is None)
r = c.delete(f"/api/batches/{unconf['id']}")
check("未确认的复烧批次也不可删除（避免来源断链）",
      r.status_code == 409 and "复烧批次" in r.get_json()["error"], r.data)
still = c.get("/api/batches").get_json()
check("删除被拒后批次仍在", any(b["code"] == "RD-REFIRE" for b in still))

# ---------- 9. 原确认清单与实绩判定保持原样 ----------
load1_now = c.get(f"/api/loads/{load1['id']}").get_json()
check("原确认清单明细不变",
      [(i["batch_code"], i["peak_min"], i["peak_max"], i["hold_min"], i["hold_max"])
       for i in load1_now["items"]]
      == [("RA", 1200.0, 1250.0, 30, 45), ("RB", 1220.0, 1260.0, 30, 40)], load1_now)
res_now = c.get(f"/api/schedules/{sched1['id']}/result").get_json()
check("原实绩判定不变",
      {i["batch_code"]: (i["peak_ok"], i["hold_ok"]) for i in res_now["items"]}
      == {"RA": (False, False), "RB": (True, False)}, res_now)

# ---------- 10. 页面可从实绩查看新批次 ----------
view = c.get(f"/api/schedules/{sched1['id']}/result").get_json()
v_items = {i["batch_code"]: i for i in view["items"]}
check("实绩明细带出复烧批次（RA）",
      v_items["RA"]["refire_batch_code"] == "RA-REFIRE"
      and v_items["RA"]["refire_batch_id"] == nb["id"], v_items["RA"])
check("实绩明细带出复烧批次（RB）",
      v_items["RB"]["refire_batch_code"] == "RB-REFIRE", v_items["RB"])
# 界内明细（另一窑次）不携带复烧批次
view2 = c.get(f"/api/schedules/{sched2['id']}/result").get_json()
check("界内明细没有复烧批次",
      view2["items"][0]["batch_code"] == "RC"
      and view2["items"][0]["refire_batch_id"] is None, view2["items"][0])

# ---------- 11. 从新批次追溯来源 ----------
r = c.get(f"/api/batches/{nb['id']}/refire-source")
check("追溯来源成功", r.status_code == 200, r.data)
s = r.get_json()["source"]
check("追溯到 RA 原批次与实绩",
      s["source_batch_code"] == "RA" and s["actual_peak"] == 1255
      and s["actual_hold"] == 50 and s["schedule_id"] == sched1["id"], s)
r = c.get(f"/api/batches/{ra['id']}/refire-source")
check("非复烧批次追溯返回 404", r.status_code == 404)
r = c.get("/api/batches/999999/refire-source")
check("不存在批次追溯返回 404", r.status_code == 404)
# 批次列表也带来源标记
list_map = {b["code"]: b for b in c.get("/api/batches").get_json()}
check("批次列表标记复烧来源",
      list_map["RA-REFIRE"]["refire_source_batch_code"] == "RA"
      and list_map["RA-REFIRE"]["refire_source_schedule_id"] == sched1["id"]
      and list_map["RA"].get("refire_id") is None)

# ---------- 12. 并发重复申请：只有一个成功，失败不留部分记录 ----------
rx = add_batch("RX", 1200, 1260, 20, 50)
loadx = confirm_load("RLX", k2, [(rx, 1)])
schedx = add_schedule(loadx["id"], "2026-10-11T08:00:00+08:00",
                      "2026-10-11T12:00:00+08:00", 1240, 35)
resx = register(schedx, 1290, 35)
item_x = resx["items"][0]
results = {}
barrier = []
def race_refire(key, code):
    client = app.test_client()
    barrier.append(1)
    while len(barrier) < 2:
        pass
    r = client.post(f"/api/result-items/{item_x['result_item_id']}/refire", json={"code": code})
    results[key] = r.status_code

t1 = Thread(target=race_refire, args=("甲", "X-REFIRE"))
t2 = Thread(target=race_refire, args=("乙", "X-REFIRE"))
t1.start(); t2.start()
t1.join(); t2.join()
print("并发生成:", results)
check("并发重复申请只有一个成功", sorted(results.values()) == [201, 409], results)
raw = sqlite3.connect(os.environ["KILN_DB_PATH"])
n = raw.execute(
    "SELECT COUNT(*) FROM refire_batches WHERE result_item_id = ?",
    (item_x["result_item_id"],)).fetchone()[0]
n_batch = raw.execute("SELECT COUNT(*) FROM batches WHERE code = 'X-REFIRE'").fetchone()[0]
raw.close()
check("并发后该明细只对应一个复烧批次", n == 1 and n_batch == 1, (n, n_batch))

# 不同编号并发同一明细：仍只有一个成功，败方编号也不留批次
results2 = {}
barrier2 = []
ry = add_batch("RY", 1200, 1260, 20, 50)
loady = confirm_load("RLY", k1, [(ry, 2)])
schedy = add_schedule(loady["id"], "2026-10-12T08:00:00+08:00",
                      "2026-10-12T12:00:00+08:00", 1240, 35)
resy = register(schedy, 1290, 35)
item_y = resy["items"][0]
def race_refire2(key, code):
    client = app.test_client()
    barrier2.append(1)
    while len(barrier2) < 2:
        pass
    r = client.post(f"/api/result-items/{item_y['result_item_id']}/refire", json={"code": code})
    results2[key] = r.status_code

t1 = Thread(target=race_refire2, args=("甲", "Y-1"))
t2 = Thread(target=race_refire2, args=("乙", "Y-2"))
t1.start(); t2.start()
t1.join(); t2.join()
print("并发不同编号:", results2)
check("并发不同编号也只有一个成功", sorted(results2.values()) == [201, 409], results2)
codes_now = {b["code"] for b in c.get("/api/batches").get_json()}
check("败方编号不留批次记录", not ("Y-1" in codes_now and "Y-2" in codes_now), codes_now)

# ---------- 13. 重启后仍可使用（关系保留、删除仍被拒、追溯正常） ----------
app2 = create_app()
c2 = app2.test_client()
r = c2.get(f"/api/batches/{nb['id']}/refire-source")
check("重启后仍可追溯来源",
      r.status_code == 200 and r.get_json()["source"]["source_batch_code"] == "RA")
r = c2.delete(f"/api/batches/{unconf['id']}")
check("重启后未确认复烧批次仍不可删", r.status_code == 409)
v2 = c2.get(f"/api/schedules/{sched1['id']}/result").get_json()
v2_ra = next(i for i in v2["items"] if i["batch_code"] == "RA")
check("重启后实绩仍可查看复烧批次",
      v2_ra["refire_batch_code"] == "RA-REFIRE", v2_ra)
n_ref2 = sqlite3.connect(os.environ["KILN_DB_PATH"]).execute(
    "SELECT COUNT(*) FROM refire_batches").fetchone()[0]
check("重启后复烧关系全部保留", n_ref2 == 5, n_ref2)  # RA, RB, RD, RX, RY

print(f"\n✅ 全部 {len(passed)} 项检查通过")
for n in passed:
    print("  -", n)
