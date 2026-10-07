"""复烧批次：超界明细生成待复烧批次、继承快照区间、双向追溯、并发裁决（临时 SQLite，结束后删除）。"""
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

# ---------- 准备：批次、已确认窑次、烧成安排、实绩 ----------
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

def post_result(sched_id, snap, actual_peak, actual_hold):
    return c.post(f"/api/schedules/{sched_id}/result",
                  json={**snap, "actual_peak": actual_peak, "actual_hold": actual_hold})

def refire(item_id, code):
    return c.post(f"/api/result-items/{item_id}/refire", json={"code": code})

kilns = c.get("/api/kilns").get_json()
k1, k2 = kilns[0]["id"], kilns[1]["id"]

pa = add_batch("PA", 1200, 1250, 30, 45)   # 峰温将超界
pb = add_batch("PB", 1220, 1260, 30, 40)   # 全部界内
pc = add_batch("PC", 1200, 1260, 30, 35)   # 保温将超界
load1 = confirm_load("RL1", k1, [(pa, 1), (pb, 2), (pc, 3)])
sched1 = add_schedule(load1["id"], "2026-10-08T08:00:00+08:00",
                      "2026-10-08T12:00:00+08:00", 1240, 35)
# 实测峰温 1255（PA 上限 1250 超界）、保温 38（PC 上限 35 超界）
r = post_result(sched1["id"], snap_of(sched1), 1255, 38)
assert r.status_code == 201, r.data
res1 = r.get_json()
items = {i["batch_code"]: i for i in res1["items"]}
check("预备判定：PA 峰温超界、PC 保温超界、PB 界内",
      items["PA"]["peak_ok"] is False and items["PA"]["hold_ok"] is True
      and items["PC"]["hold_ok"] is False and items["PC"]["peak_ok"] is True
      and items["PB"]["peak_ok"] is True and items["PB"]["hold_ok"] is True, items)
check("明细携带 result_item_id", all(isinstance(i["result_item_id"], int) for i in res1["items"]))
check("未生成时 refire 为空", all(i["refire"] is None for i in res1["items"]))
item_pa, item_pb, item_pc = items["PA"], items["PB"], items["PC"]

# ---------- 1. 非法申请：界内、空编号、明细不存在 ----------
r = refire(item_pb["result_item_id"], "PB-R")
check("界内批次不能生成复烧", r.status_code == 409 and "区间内" in r.get_json()["error"], r.data)
r = refire(item_pa["result_item_id"], "   ")
check("空编号被拒", r.status_code == 400)
r = refire(99999, "X-1")
check("明细不存在返回 404", r.status_code == 404)

# ---------- 2. 重复编号：拒绝且不留部分记录 ----------
nbefore = len(c.get("/api/batches").get_json())
r = refire(item_pa["result_item_id"], "PA")   # 与现有批次重名
check("重复编号被拒", r.status_code == 409 and "已存在" in r.get_json()["error"], r.data)
check("失败不留部分记录（批次未增加）", len(c.get("/api/batches").get_json()) == nbefore)
r = c.get(f"/api/schedules/{sched1['id']}/result")
check("失败不留部分记录（明细仍无复烧）",
      all(i["refire"] is None for i in r.get_json()["items"]))

# ---------- 3. 成功生成：继承快照区间、记录来源 ----------
r = refire(item_pa["result_item_id"], "PA-R")
check("超界明细生成复烧批次", r.status_code == 201, r.data)
nb = r.get_json()
check("新批次继承快照工艺闭区间",
      (nb["peak_min"], nb["peak_max"], nb["hold_min"], nb["hold_max"])
      == (1200.0, 1250.0, 30, 45), nb)
check("返回体记录来源实绩明细",
      nb["refire_source"]["result_item_id"] == item_pa["result_item_id"]
      and nb["refire_source"]["batch_code"] == "PA"
      and nb["refire_source"]["load_id"] == load1["id"]
      and nb["refire_source"]["pos_no"] == 1, nb)
check("新批次为待烧状态", nb["used_by_load"] is None)

# ---------- 4. 每条明细最多生成一个 ----------
r = refire(item_pa["result_item_id"], "PA-R2")
check("同一明细重复申请被拒", r.status_code == 409 and "最多生成一个" in r.get_json()["error"], r.data)
check("重复申请不产生新批次",
      not any(b["code"] == "PA-R2" for b in c.get("/api/batches").get_json()))

# ---------- 5. 双向追溯 ----------
batches = c.get("/api/batches").get_json()
pa_r = next(b for b in batches if b["code"] == "PA-R")
check("批次列表可溯源（新批次 → 来源明细/窑次）",
      pa_r["refire_source"]["result_item_id"] == item_pa["result_item_id"]
      and pa_r["refire_source"]["load_name"] == "RL1"
      and pa_r["refire_source"]["registered_at"] == res1["registered_at"], pa_r)
r = c.get(f"/api/schedules/{sched1['id']}/result")
items2 = {i["batch_code"]: i for i in r.get_json()["items"]}
check("实绩可查新批次（来源明细 → 新批次）",
      items2["PA"]["refire"]["batch_code"] == "PA-R"
      and items2["PA"]["refire"]["batch_id"] == pa_r["id"], items2)
check("界内明细无复烧关联", items2["PB"]["refire"] is None)
check("原实绩判定保持原样",
      items2["PA"]["peak_ok"] is False and items2["PC"]["hold_ok"] is False
      and items2["PB"]["peak_ok"] is True)

# ---------- 6. 原批次仍归属原窑次，确认清单不变 ----------
detail = c.get(f"/api/loads/{load1['id']}").get_json()
check("原确认清单明细不变",
      [i["batch_code"] for i in detail["items"]] == ["PA", "PB", "PC"], detail)
pa_now = next(b for b in c.get("/api/batches").get_json() if b["code"] == "PA")
check("原批次仍归属原窑次", pa_now["used_by_load"] == load1["id"])

# ---------- 7. 修改原批次范围不影响新批次 ----------
r = c.put(f"/api/batches/{pa_now['id']}", json={"peak_min": 1000, "peak_max": 1010,
                                                "hold_min": 5, "hold_max": 9,
                                                "version": pa_now["version"]})
check("原批次范围可修改", r.status_code == 200)
pa_r_now = next(b for b in c.get("/api/batches").get_json() if b["code"] == "PA-R")
check("新批次区间保持快照值（不受原批次修改影响）",
      (pa_r_now["peak_min"], pa_r_now["peak_max"], pa_r_now["hold_min"], pa_r_now["hold_max"])
      == (1200.0, 1250.0, 30, 45), pa_r_now)

# ---------- 8. 新批次即使尚未确认也不可删除 ----------
r = c.delete(f"/api/batches/{pa_r_now['id']}")
check("复烧批次未确认也不可删除", r.status_code == 409 and "复烧" in r.get_json()["error"], r.data)

# ---------- 9. 新批次走完整流程：草案 → 预览 → 确认 → 排程 → 实绩 ----------
load2 = confirm_load("RL2-复烧", k2, [(pa_r_now, 1)])
check("复烧批次可经草案预览确认入窑", load2["items"][0]["batch_code"] == "PA-R", load2)
sched2 = add_schedule(load2["id"], "2026-10-09T08:00:00+08:00",
                      "2026-10-09T12:00:00+08:00", 1240, 35)
r = post_result(sched2["id"], snap_of(sched2), 1245, 40)
check("复烧批次可登记实绩且判定在界内",
      r.status_code == 201 and r.get_json()["items"][0]["peak_ok"] is True
      and r.get_json()["items"][0]["hold_ok"] is True, r.data)
pa_r_used = next(b for b in c.get("/api/batches").get_json() if b["code"] == "PA-R")
check("复烧批次已用于新窑次", pa_r_used["used_by_load"] == load2["id"])
r = c.delete(f"/api/batches/{pa_r_used['id']}")
check("复烧批次确认后仍不可删除", r.status_code == 409)

# ---------- 10. 并发重复申请：只有一个成功，失败不留部分记录 ----------
results = {}
barrier = []
def race(key, code):
    client = app.test_client()
    barrier.append(1)
    while len(barrier) < 2:
        pass
    r = client.post(f"/api/result-items/{item_pc['result_item_id']}/refire",
                    json={"code": code})
    results[key] = r.status_code

t1 = Thread(target=race, args=("甲", "PC-R"))
t2 = Thread(target=race, args=("乙", "PC-R2"))
t1.start(); t2.start()
t1.join(); t2.join()
print("并发复烧申请:", results)
check("并发重复申请只有一个成功", sorted(results.values()) == [201, 409], results)
pc_refires = [b for b in c.get("/api/batches").get_json() if b["code"] in ("PC-R", "PC-R2")]
check("并发后只生成一个复烧批次", len(pc_refires) == 1, pc_refires)
r = c.get(f"/api/schedules/{sched1['id']}/result")
pc_item = next(i for i in r.get_json()["items"] if i["batch_code"] == "PC")
check("来源明细只挂一个新批次",
      pc_item["refire"]["batch_code"] in ("PC-R", "PC-R2"), pc_item)
check("并发生成的批次继承 PC 快照区间",
      (pc_refires[0]["peak_min"], pc_refires[0]["peak_max"],
       pc_refires[0]["hold_min"], pc_refires[0]["hold_max"])
      == (1200.0, 1260.0, 30, 35), pc_refires)

# ---------- 11. 重启后保留 ----------
app2 = create_app()
c2 = app2.test_client()
batches2 = c2.get("/api/batches").get_json()
pa_r2 = next(b for b in batches2 if b["code"] == "PA-R")
check("重启后来源追溯保留",
      pa_r2["refire_source"]["batch_code"] == "PA"
      and pa_r2["refire_source"]["load_id"] == load1["id"], pa_r2)
r = c2.get(f"/api/schedules/{sched1['id']}/result")
items3 = {i["batch_code"]: i for i in r.get_json()["items"]}
check("重启后实绩仍指向复烧批次", items3["PA"]["refire"]["batch_code"] == "PA-R")
check("重启后判定保持原样", items3["PA"]["peak_ok"] is False)
r = c2.delete(f"/api/batches/{pc_refires[0]['id']}")
check("重启后复烧批次仍不可删除", r.status_code == 409)

print(f"\n✅ 全部 {len(passed)} 项检查通过")
for n in passed:
    print("  -", n)
