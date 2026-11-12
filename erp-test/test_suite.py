# -*- coding: utf-8 -*-
"""
jshERP 系统功能测试 + 数据库造数套件
用法:
    MYSQL_PWD=<数据库口令> python test_suite.py basic      # 基础资料
    MYSQL_PWD=<数据库口令> python test_suite.py bills      # 进销存单据
    MYSQL_PWD=<数据库口令> python test_suite.py finance    # 财务单据
    MYSQL_PWD=<数据库口令> python test_suite.py query      # 查询与报表
    MYSQL_PWD=<数据库口令> python test_suite.py negative   # 异常/边界用例
    MYSQL_PWD=<数据库口令> python test_suite.py verify     # 数据库落库校验
    MYSQL_PWD=<数据库口令> python test_suite.py all
说明: 数据库口令仅从环境变量 MYSQL_PWD 读取，不写入任何文件。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

from erp_client import ErpClient, dump_log

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS_PATH = os.path.join(ROOT, "test_results.json")
STATE_PATH = os.path.join(ROOT, "test_state.json")
BATCH = "T" + datetime.now().strftime("%m%d%H%M")
TODAY = datetime.now().strftime("%Y-%m-%d")


def day(offset_days, hour=10):
    d = datetime.now() - timedelta(days=offset_days)
    return d.strftime("%Y-%m-%d") + " %02d:00:00" % hour


class Suite(object):
    def __init__(self, reset=False):
        self.client = ErpClient("jsh")
        self.results = [] if reset else self._load(RESULTS_PATH, [])
        self.state = {} if reset else self._load(STATE_PATH, {})
        global BATCH
        if not self.state.get("batch"):
            self.state["batch"] = BATCH
        else:
            BATCH = self.state["batch"]
        self.state["run_date"] = TODAY
        self._last_write_ts = 0

    @staticmethod
    def _load(path, default):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return default
        return default

    def save(self):
        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        dump_log(self.client, os.path.join(ROOT, "request_log.json"))

    def case(self, cid, name, method, path, req, resp, http_status, expect, passed, note=""):
        item = {
            "id": cid,
            "name": name,
            "method": method,
            "path": path,
            "http_status": http_status,
            "request": self._brief(req),
            "response": self._brief(resp),
            "expect": expect,
            "result": "PASS" if passed else "FAIL",
            "note": note,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.results.append(item)
        flag = "PASS" if passed else "FAIL"
        print("[%s] %-4s %-46s http=%s" % (flag, cid, name[:46], http_status))
        if not passed:
            print("       resp=%s" % json.dumps(self._brief(resp), ensure_ascii=False)[:400])
        return resp

    @staticmethod
    def _brief(obj, limit=1200):
        text = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
        if len(text) > limit:
            return text[:limit] + "...(truncated)"
        try:
            return json.loads(text)
        except Exception:
            return text

    def pace(self):
        """写操作节流，避开系统同秒重复操作防刷机制"""
        elapsed = time.time() - self._last_write_ts
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        self._last_write_ts = time.time()

    def call(self, cid, name, method, path, params=None, body=None, expect="code=200", checker=None, note=""):
        # 系统 LogService 会拦截「同一用户+同一模块+同一 IP+同一秒」的重复写操作并注销会话，
        # 因此写请求之间做 1.1 秒节流，避免测试脚本自身触发该防刷机制。
        if method in ("POST", "PUT", "DELETE"):
            self.pace()
        fn = {"GET": self.client.get, "POST": self.client.post,
              "PUT": self.client.put, "DELETE": self.client.delete}[method]
        if method in ("GET", "DELETE"):
            resp, st = fn(path, params=params)
        else:
            resp, st = fn(path, body=body)
        # 若会话被注销则重新登录并重试一次，保证后续用例继续执行
        if st == 500 and "loginOut" in str(resp):
            self.client.login()
            if method in ("GET", "DELETE"):
                resp, st = fn(path, params=params)
            else:
                resp, st = fn(path, body=body)
            note = (note + " 会话被注销后自动重新登录重试").strip()
        if checker:
            passed, extra = checker(resp, st)
            note = (note + " " + extra).strip()
        else:
            passed = st == 200 and isinstance(resp, dict) and str(resp.get("code")) == "200"
        self.case(cid, name, method, path,
                  {"params": params, "body": body} if body is not None else {"params": params},
                  resp, st, expect, passed, note)
        return resp, st, passed

    def ok_code(self, resp, st):
        ok = st == 200 and isinstance(resp, dict) and str(resp.get("code")) == "200"
        return ok, ""

    def ok_data_rows(self, resp, st):
        if st != 200:
            return False, "HTTP 状态异常"
        if isinstance(resp, list):
            return True, "返回数组, 长度=%d" % len(resp)
        if not (isinstance(resp, dict) and str(resp.get("code")) == "200"):
            return False, "响应码异常"
        data = resp.get("data")
        if isinstance(data, dict) and "rows" in data:
            return True, "total=%s rows=%s" % (data.get("total"), len(data.get("rows") or []))
        if isinstance(data, (list, dict)):
            return True, "data 类型=%s" % type(data).__name__
        return True, ""

    # ---------------- 阶段 1: 基础资料 ----------------
    def basic(self):
        c = self.client
        created = self.state.setdefault("basic", {})
        depots, suppliers, customers, accounts, persons, items, materials = (created.setdefault(k, [])
            for k in ("depots", "suppliers", "customers", "accounts", "persons", "items", "materials"))

        for i, (nm, addr) in enumerate([("自动化测试-原料仓", "深圳市南山区科技园"),
                                        ("自动化测试-成品仓", "东莞市长安镇")], 1):
            body = {"name": nm + "-" + BATCH, "address": addr, "warehousing": 5, "truckage": 8,
                    "principal": 131, "sort": str(10 + i), "remark": "接口自动化测试新增仓库"}
            r, st, ok = self.call("B%02d" % i, "新增仓库 " + nm, "POST", "/depot/add", body=body)
            depots.append(nm + "-" + BATCH)

        for i, nm in enumerate(["自动化测试-供应商甲", "自动化测试-供应商乙", "自动化测试-供应商丙"], 1):
            body = {"supplier": nm + "-" + BATCH, "type": "供应商", "contacts": "联系人%d" % i,
                    "phoneNum": "1380000%04d" % i, "email": "test%d@example.com" % i,
                    "beginNeedPay": 0, "beginNeedGet": 0, "taxRate": 13,
                    "address": "深圳市福田区", "description": "接口自动化测试新增供应商"}
            self.call("B%02d" % (10 + i), "新增供应商 " + nm, "POST", "/supplier/add", body=body)
            suppliers.append(nm + "-" + BATCH)

        for i, nm in enumerate(["自动化测试-客户甲", "自动化测试-客户乙", "自动化测试-客户丙"], 1):
            body = {"supplier": nm + "-" + BATCH, "type": "客户", "contacts": "采购负责人%d" % i,
                    "phoneNum": "1390000%04d" % i, "beginNeedGet": 0, "beginNeedPay": 0,
                    "address": "广州市天河区", "description": "接口自动化测试新增客户"}
            self.call("B%02d" % (20 + i), "新增客户 " + nm, "POST", "/supplier/add", body=body)
            customers.append(nm + "-" + BATCH)

        for i, nm in enumerate(["自动化测试-基本户", "自动化测试-现金户"], 1):
            body = {"name": nm + "-" + BATCH, "serialNo": BATCH + "ACC%d" % i,
                    "initialAmount": 50000 if i == 1 else 10000, "remark": "接口自动化测试新增结算账户"}
            self.call("B%02d" % (30 + i), "新增结算账户 " + nm, "POST", "/account/add", body=body)
            accounts.append(nm + "-" + BATCH)

        for i, (tp, nm) in enumerate([("销售员", "自动化测试-销售甲"), ("采购员", "自动化测试-采购乙"),
                                      ("财务员", "自动化测试-财务丙")], 1):
            body = {"type": tp, "name": nm + "-" + BATCH, "sort": str(i), "enabled": True}
            self.call("B%02d" % (40 + i), "新增经手人 " + nm, "POST", "/person/add", body=body)
            persons.append(nm + "-" + BATCH)

        for i, (nm, tp) in enumerate([("自动化测试-运费收入", "收入"), ("自动化测试-水电费", "支出"),
                                      ("自动化测试-服务费", "收入")], 1):
            body = {"name": nm + "-" + BATCH, "type": tp, "remark": "接口自动化测试新增收支项目"}
            self.call("B%02d" % (50 + i), "新增收支项目 " + nm, "POST", "/inOutItem/add", body=body)
            items.append(nm + "-" + BATCH)

        # 商品（含条码与价格体系）
        goods = [
            ("测试-A4复印纸", "包", 12, 20, 16, 10),
            ("测试-中性笔", "支", 2, 5, 3.5, 2),
            ("测试-办公笔记本", "本", 8, 18, 13, 9),
            ("测试-无线鼠标", "个", 35, 79, 59, 40),
            ("测试-机械键盘", "个", 120, 259, 199, 140),
            ("测试-显示器支架", "个", 45, 99, 75, 50),
            ("测试-文件夹", "个", 3, 8, 6, 4),
            ("测试-记号笔", "支", 4, 9, 7, 5),
            ("测试-订书机", "个", 15, 35, 26, 18),
            ("测试-打印纸托盘", "个", 22, 49, 36, 25),
        ]
        for i, (nm, unit, buy, retail, wholesale, low) in enumerate(goods, 1):
            barcode = "9%06d%s" % (i, BATCH[-4:])
            body = {
                "name": nm + "-" + BATCH, "categoryId": 17, "unit": unit,
                "model": "M%d" % i, "standard": "标准规格", "color": "黑色", "brand": "测试品牌",
                "mnemonic": "TEST%d" % i, "enableSerialNumber": "0", "enableBatchNumber": "0",
                "remark": "接口自动化测试新增商品",
                "meList": [{"barCode": barcode, "commodityUnit": unit,
                            "purchaseDecimal": buy, "commodityDecimal": retail,
                            "wholesaleDecimal": wholesale, "lowDecimal": low, "defaultFlag": "1"}],
                "sortList": "[]", "meDeleteIdList": [], "stock": [],
            }
            r, st, ok = self.call("B%02d" % (60 + i), "新增商品 " + nm, "POST", "/material/add", body=body)
            materials.append({"name": nm + "-" + BATCH, "barCode": barcode, "unit": unit,
                              "buy": buy, "retail": retail, "wholesale": wholesale, "low": low})
        self.state["materials"] = materials
        self.save()

    # ---------------- 阶段 2: 进销存单据 ----------------
    def _bill(self, cid, name, number, type_, sub_type, rows, organ_id, account_id,
              total_price, change_amount, oper_time, extra=None, expect="code=200"):
        total_abs = abs(total_price)
        info = {
            "type": type_, "subType": sub_type, "number": number,
            "operTime": oper_time, "organId": organ_id, "accountId": account_id,
            "changeAmount": change_amount, "totalPrice": total_price,
            "discount": 0, "discountMoney": 0, "discountLastMoney": total_abs,
            "otherMoney": 0, "deposit": 0, "debt": abs(change_amount),
            "status": "0", "payType": "现付", "fileName": "",
            "salesMan": str((((self.state.get("idmap") or {}).get("persons")) or [18])[0]),
            "remark": "接口自动化测试单据 " + number,
        }
        if extra:
            info.update(extra)
        body = {"info": json.dumps(info, ensure_ascii=False),
                "rows": json.dumps(rows, ensure_ascii=False)}
        return self.call(cid, name, "POST", "/depotHead/addDepotHeadAndDetail", body=body, expect=expect)

    def _row(self, goods, depot_id, qty, price, another_depot_id=None, remark=""):
        row = {"barCode": goods["barCode"], "name": goods["name"], "unit": goods["unit"],
               "depotId": depot_id, "operNumber": qty, "unitPrice": price,
               "allPrice": round(qty * price, 2), "taxRate": 0, "taxMoney": 0,
               "taxLastMoney": round(qty * price, 2), "remark": remark}
        if another_depot_id:
            row["anotherDepotId"] = another_depot_id
        return row

    def bills(self):
        st = self.state
        materials = st.get("materials") or []
        if not materials:
            print("缺少商品数据，请先执行 basic 阶段")
            return
        # 通过接口反查新增基础资料的 id
        idmap = self._resolve_ids()
        depot_ids = idmap["depots"] or [14, 15, 17]
        vend = idmap["suppliers"] or [57]
        cust = idmap["customers"] or [58]
        accounts = idmap["accounts"] or [17]
        hands = idmap["persons"]
        st["idmap"] = idmap

        created_numbers = st.setdefault("bill_numbers", [])
        idx = 0

        seq = [0]

        def next_no(prefix):
            seq[0] += 1
            return "%s-%s-%03d" % (prefix, BATCH, seq[0])

        def add(prefix, name, type_, sub_type, rows, organ, account, sign, oper_time, extra=None):
            number = next_no(prefix)
            total = sum(r["allPrice"] for r in rows)
            total_price = -total if sign < 0 else total
            change = -total if sign < 0 else total
            r, http, ok = self._bill("D%03d" % (seq[0] + 100), name + " " + number, number,
                                     type_, sub_type, rows, organ, account,
                                     total_price, change, oper_time, extra)
            created_numbers.append({"number": number, "type": type_, "subType": sub_type,
                                    "amount": total, "passed": ok})
            return number

        g = materials
        # 1) 采购入库 4 单（先建立库存）
        for i in range(4):
            rows = [self._row(g[i % len(g)], depot_ids[i % len(depot_ids)], 100 + 20 * i, g[i % len(g)]["buy"]),
                    self._row(g[(i + 3) % len(g)], depot_ids[i % len(depot_ids)], 50 + 10 * i, g[(i + 3) % len(g)]["buy"])]
            add("CGRK", "新增采购入库单", "入库", "采购", rows, vend[i % len(vend)],
                accounts[i % len(accounts)], -1, day(15 - i, 9))

        # 2) 采购订单 2 单（不产生库存）
        for i in range(2):
            rows = [self._row(g[(i + 1) % len(g)], "", 200, g[(i + 1) % len(g)]["buy"])]
            add("CGDD", "新增采购订单", "其它", "采购订单", rows, vend[i % len(vend)],
                accounts[0], -1, day(8 - i, 9))

        # 3) 销售订单 2 单
        for i in range(2):
            rows = [self._row(g[(i + 2) % len(g)], "", 60, g[(i + 2) % len(g)]["wholesale"])]
            add("XSDD", "新增销售订单", "其它", "销售订单", rows, cust[i % len(cust)],
                accounts[0], 1, day(7 - i, 11))

        # 4) 销售出库 4 单
        for i in range(4):
            rows = [self._row(g[i % len(g)], depot_ids[i % len(depot_ids)], 20 + 5 * i, g[i % len(g)]["wholesale"]),
                    self._row(g[(i + 5) % len(g)], depot_ids[i % len(depot_ids)], 10, g[(i + 5) % len(g)]["wholesale"])]
            add("XSCK", "新增销售出库单", "出库", "销售", rows, cust[i % len(cust)],
                accounts[i % len(accounts)], 1, day(6 - i, 14))

        # 5) 其它入库 / 其它出库
        for i in range(2):
            rows = [self._row(g[(i + 6) % len(g)], depot_ids[i % len(depot_ids)], 30, g[(i + 6) % len(g)]["buy"])]
            add("QTRK", "新增其它入库单", "入库", "其它", rows, None, None, -1, day(5 - i, 9))
        for i in range(2):
            rows = [self._row(g[(i + 7) % len(g)], depot_ids[i % len(depot_ids)], 8, g[(i + 7) % len(g)]["wholesale"])]
            add("QTCK", "新增其它出库单", "出库", "其它", rows, None, None, 1, day(4 - i, 15))

        # 6) 调拨出库（调出仓库 -> 调入仓库）
        for i in range(2):
            rows = [self._row(g[(i + 1) % len(g)], depot_ids[0], 10, g[(i + 1) % len(g)]["buy"],
                              another_depot_id=depot_ids[1])]
            add("DBCK", "新增调拨出库单", "出库", "调拨", rows, None, None, 1, day(3 - i, 10))

        # 7) 零售出库 2 单
        for i in range(2):
            rows = [self._row(g[(i + 4) % len(g)], depot_ids[0], 5, g[(i + 4) % len(g)]["retail"])]
            add("LSCK", "新增零售出库单", "出库", "零售", rows, None, accounts[0], 1, day(2 - i, 16))

        # 8) 采购退货 / 销售退货
        rows = [self._row(g[0], depot_ids[0], 5, g[0]["buy"])]
        add("CGTH", "新增采购退货单", "出库", "采购退货", rows, vend[0], accounts[0], 1, day(1, 10))
        rows = [self._row(g[1], depot_ids[0], 3, g[1]["wholesale"])]
        add("XSTH", "新增销售退货单", "入库", "销售退货", rows, cust[0], accounts[0], -1, day(1, 11))

        self.save()

    def audit(self):
        """按单号查询单据详情并批量审核/反审核"""
        numbers = [b["number"] for b in self.state.get("bill_numbers", []) if b["passed"]]
        audit_ids = []
        for n in numbers:
            resp, http = self.client.get("/depotHead/getDetailByNumber", params={"number": n})
            data = resp.get("data") or {}
            if isinstance(data, dict) and data.get("id"):
                audit_ids.append(str(data["id"]))
        self.case("D210", "按单号查询单据详情（%d/%d 成功）" % (len(audit_ids), len(numbers)),
                  "GET", "/depotHead/getDetailByNumber",
                  {"params": {"number": numbers[0] if numbers else ""}},
                  {"matched": len(audit_ids), "total": len(numbers)}, 200,
                  "全部单据可通过单号查询到详情", len(audit_ids) == len(numbers) and bool(numbers),
                  "salesMan 需为经手人 ID 字符串，否则服务端 Long.parseLong 抛异常")
        self.state["audit_ids"] = audit_ids
        if audit_ids:
            self.call("D200", "批量审核进销存单据（%d 张）" % len(audit_ids), "POST",
                      "/depotHead/batchSetStatus",
                      body={"status": "1", "ids": ",".join(audit_ids)},
                      expect="code=200", note="审核会触发库存与账户余额更新")
            self.call("D201", "批量反审核（抽查首张单据）", "POST", "/depotHead/batchSetStatus",
                      body={"status": "0", "ids": audit_ids[0]}, expect="code=200")
            self.call("D202", "重新审核首张单据", "POST", "/depotHead/batchSetStatus",
                      body={"status": "1", "ids": audit_ids[0]}, expect="code=200")
        self.save()

    def _resolve_ids(self):
        """通过列表接口把名称/条码换成数据库主键"""
        out = {}
        for key, path, field in [("depots", "/depot/list", "name"),
                                 ("suppliers", "/supplier/list", "supplier"),
                                 ("customers", "/supplier/list", "supplier"),
                                 ("accounts", "/account/list", "name"),
                                 ("persons", "/person/list", "name"),
                                 ("items", "/inOutItem/list", "name")]:
            resp, st = self.client.get(path, params={"search": "{}", "currentPage": 1, "pageSize": 200})
            rows = ((resp.get("data") or {}).get("rows") or []) if isinstance(resp, dict) else []
            want_type = {"suppliers": "供应商", "customers": "客户"}.get(key)
            hit = []
            for r in rows:
                name = r.get(field) or ""
                if BATCH in name and (not want_type or r.get("type") == want_type):
                    hit.append(r["id"])
            out[key] = hit
        return out

    # ---------------- 阶段 3: 财务单据 ----------------
    def finance(self):
        st = self.state
        idmap = st.get("idmap") or self._resolve_ids()
        st["idmap"] = idmap
        vend = idmap["suppliers"] or [57]
        cust = idmap["customers"] or [58]
        accounts = idmap["accounts"] or [17]
        items = idmap["items"] or [21, 22, 23]
        hands = idmap["persons"] or [15]
        bills = [b["number"] for b in st.get("bill_numbers", []) if b["passed"]]
        in_bills = [b["number"] for b in st.get("bill_numbers", [])
                    if b["passed"] and b["subType"] == "采购"]
        out_bills = [b["number"] for b in st.get("bill_numbers", [])
                     if b["passed"] and b["subType"] == "销售"]
        seq = [0]
        finance_numbers = st.setdefault("finance_numbers", [])

        def add_fin(cid, name, ftype, rows, organ, account, total, change, bill_time, extra=None):
            seq[0] += 1
            bill_no = "FIN-%s-%03d" % (BATCH, seq[0])
            info = {"type": ftype, "organId": organ, "handsPersonId": hands[0],
                    "changeAmount": change, "totalPrice": total, "accountId": account,
                    "billNo": bill_no, "billTime": bill_time, "status": "0",
                    "discountMoney": 0, "fileName": "",
                    "remark": "接口自动化测试财务单据 " + bill_no}
            if extra:
                info.update(extra)
            body = {"info": json.dumps(info, ensure_ascii=False),
                    "rows": json.dumps(rows, ensure_ascii=False)}
            r, http, ok = self.call(cid, name + " " + bill_no, "POST",
                                    "/accountHead/addAccountHeadAndDetail", body=body)
            finance_numbers.append({"billNo": bill_no, "type": ftype, "passed": ok})
            return bill_no

        # 收款单（关联销售出库单）
        for i in range(2):
            rows = [{"billNumber": out_bills[i % len(out_bills)] if out_bills else "",
                     "needDebt": 500, "finishDebt": 300, "eachAmount": 300,
                     "remark": "自动化测试收款"}]
            if not rows[0]["billNumber"]:
                rows[0].pop("billNumber")
            add_fin("F%02d" % (10 + i), "新增收款单", "收款", rows,
                    cust[i % len(cust)], accounts[0], 300, 300, day(5 - i, 10))

        # 付款单（关联采购入库单）
        for i in range(2):
            rows = [{"billNumber": in_bills[i % len(in_bills)] if in_bills else "",
                     "needDebt": 800, "finishDebt": 500, "eachAmount": 500,
                     "remark": "自动化测试付款"}]
            if not rows[0]["billNumber"]:
                rows[0].pop("billNumber")
            add_fin("F%02d" % (20 + i), "新增付款单", "付款", rows,
                    vend[i % len(vend)], accounts[0], -500, -500, day(4 - i, 11))

        # 收入单 / 支出单
        for i in range(2):
            rows = [{"inOutItemId": items[i % len(items)], "eachAmount": 1200 + i * 300,
                     "remark": "自动化测试收入"}]
            add_fin("F%02d" % (30 + i), "新增收入单", "收入", rows, None, accounts[0],
                    1200 + i * 300, 1200 + i * 300, day(3 - i, 9))
            rows = [{"inOutItemId": items[1], "eachAmount": 400 + i * 100,
                     "remark": "自动化测试支出"}]
            add_fin("F%02d" % (35 + i), "新增支出单", "支出", rows, None, accounts[0],
                    -(400 + i * 100), -(400 + i * 100), day(3 - i, 15))

        # 转账单：从 accounts[0] 转入 accounts[1]
        if len(accounts) >= 2:
            rows = [{"accountId": accounts[1], "eachAmount": 2000, "remark": "自动化测试账户转账"}]
            add_fin("F40", "新增转账单", "转账", rows, None, accounts[0], -2000, -2000, day(2, 10))

        # 收预付款
        rows = [{"eachAmount": 1000, "remark": "自动化测试收预付款"}]
        add_fin("F41", "新增收预付款单", "收预付款", rows, cust[0], accounts[0], 1000, 1000, day(1, 10))

        self.save()

    def audit_finance(self):
        """按单号查询财务单据并批量审核"""
        finance_numbers = self.state.get("finance_numbers", [])
        audit_ids = []
        for rec in finance_numbers:
            if not rec["passed"]:
                continue
            resp, http = self.client.get("/accountHead/getDetailByNumber",
                                         params={"billNo": rec["billNo"]})
            data = resp.get("data") or {}
            if isinstance(data, dict) and data.get("id"):
                audit_ids.append(str(data["id"]))
        self.case("F49", "按单号查询财务单据详情（%d/%d 成功）" % (len(audit_ids), len(finance_numbers)),
                  "GET", "/accountHead/getDetailByNumber",
                  {"params": {"billNo": finance_numbers[0]["billNo"] if finance_numbers else ""}},
                  {"matched": len(audit_ids), "total": len(finance_numbers)}, 200,
                  "全部财务单据可查询到详情", len(audit_ids) == len(finance_numbers) and bool(finance_numbers))
        self.state["finance_audit_ids"] = audit_ids
        if audit_ids:
            self.call("F50", "批量审核财务单据（%d 张）" % len(audit_ids), "POST",
                      "/accountHead/batchSetStatus",
                      body={"status": "1", "ids": ",".join(audit_ids)},
                      expect="code=200", note="审核后更新账户余额与往来欠款")
        self.save()

    # ---------------- 阶段 4: 查询与报表 ----------------
    def query(self):
        search_cases = [
            ("Q01", "仓库列表", "/depot/list", {"search": "{}", "currentPage": 1, "pageSize": 50}),
            ("Q02", "供应商/客户列表", "/supplier/list", {"search": "{}", "currentPage": 1, "pageSize": 100}),
            ("Q03", "商品列表", "/material/list", {"search": "{}", "currentPage": 1, "pageSize": 100}),
            ("Q04", "结算账户列表", "/account/list", {"search": "{}", "currentPage": 1, "pageSize": 50}),
            ("Q05", "经手人列表", "/person/list", {"search": "{}", "currentPage": 1, "pageSize": 50}),
            ("Q06", "收支项目列表", "/inOutItem/list", {"search": "{}", "currentPage": 1, "pageSize": 50}),
            ("Q07", "单位列表", "/unit/list", {"search": "{}", "currentPage": 1, "pageSize": 50}),
            ("Q08", "用户下拉列表", "/user/getUserList", {}),
            ("Q09", "进销存单据列表（采购入库）", "/depotHead/list",
             {"search": json.dumps({"type": "入库", "subType": "采购"}),
              "currentPage": 1, "pageSize": 100}),
            ("Q10", "财务单据列表", "/accountHead/list",
             {"search": "{}", "currentPage": 1, "pageSize": 100}),
            ("Q11", "用户列表", "/user/list", {"search": "{}", "currentPage": 1, "pageSize": 50}),
            ("Q12", "角色列表", "/role/list", {"search": "{}", "currentPage": 1, "pageSize": 50}),
            ("Q13", "操作日志列表", "/log/list", {"search": "{}", "currentPage": 1, "pageSize": 50}),
        ]
        for cid, name, path, params in search_cases:
            self.call(cid, name, "GET", path, params=params, checker=self.ok_data_rows,
                      expect="code=200 且返回 rows/data")
        self.call("Q09B", "进销存单据列表（销售出库）", "GET", "/depotHead/list",
                  params={"search": json.dumps({"type": "出库", "subType": "销售"}),
                          "currentPage": 1, "pageSize": 100},
                  checker=self.ok_data_rows, expect="code=200 且返回 rows/data")
        self.call("Q14", "菜单权限树（POST + pNumber=0）", "POST", "/function/findMenuByPNumber",
                  body={"pNumber": "0", "userId": "63"}, checker=self.ok_data_rows,
                  expect="code=200 或返回菜单数组")

        stat_cases = [
            ("Q20", "商品库存查询", "/material/getListWithStock",
             {"currentPage": 1, "pageSize": 50, "materialParam": "", "zeroStock": 0,
              "depotIds": ""}),
            ("Q21", "入库出库明细", "/depotHead/findInOutDetail",
             {"currentPage": 1, "pageSize": 50, "number": "", "materialParam": "",
              "beginTime": day(40)[:10], "endTime": TODAY, "type": "入库",
              "remark": "", "column": "createTime", "order": "desc"}),
            ("Q22", "入库出库汇总", "/depotHead/findInOutMaterialCount",
             {"currentPage": 1, "pageSize": 50, "materialParam": "",
              "beginTime": day(40)[:10], "endTime": TODAY, "type": "出库"}),
            ("Q23", "调拨明细统计", "/depotHead/findAllocationDetail",
             {"currentPage": 1, "pageSize": 50, "number": "", "materialParam": "",
              "beginTime": day(40)[:10], "endTime": TODAY, "subType": "调拨", "remark": ""}),
            ("Q24", "客户对账单", "/depotHead/getStatementAccount",
             {"currentPage": 1, "pageSize": 50, "beginTime": day(40)[:10],
              "endTime": TODAY, "supplierType": "客户"}),
            ("Q25", "供应商对账单", "/depotHead/getStatementAccount",
             {"currentPage": 1, "pageSize": 50, "beginTime": day(40)[:10],
              "endTime": TODAY, "supplierType": "供应商"}),
            ("Q26", "采购销售统计", "/depotHead/getBuyAndSaleStatistics", {}),
            ("Q27", "欠款单据列表", "/depotHead/debtList",
             {"search": "{}", "currentPage": 1, "pageSize": 50}),
            ("Q28", "待出入库单据数量", "/depotHead/waitBillCount", {"search": "{}"}),
            ("Q29", "待收/待付款条数", "/depotHead/getNeedCount", {"type": "customer"}),
            ("Q30", "账户收支统计", "/account/findBySelect", {}),
            ("Q31", "单据明细查询", "/depotItem/getDetailList",
             {"headerId": self.state.get("audit_ids", [277])[0], "mpList": "", "linkType": "basic"}),
            ("Q32", "当前用户信息", "/user/getUserSession", {}),
            ("Q33", "当前用户租户信息", "/user/infoWithTenant", {}),
            ("Q34", "商品库存分布", "/material/getMaterialDepotStock",
             {"materialId": 568}),
        ]
        for cid, name, path, params in stat_cases:
            self.call(cid, name, "GET", path, params=params, checker=self.ok_data_rows,
                      expect="接口正常返回统计数据")
        self.save()

    # ---------------- 阶段 5: 异常 / 边界 ----------------
    def negative(self):
        # N01 未登录访问受保护接口
        resp, http = self.client.get("/depot/list", params={"search": "{}"}, token="")
        self.case("N01", "未登录访问受保护接口", "GET", "/depot/list", {"params": {"search": "{}"}},
                  resp, http, "拒绝访问(HTTP 500 / loginOut)",
                  http == 500 or "loginOut" in str(resp), "验证登录拦截器")

        # N02 错误密码登录
        payload = {"loginName": "jsh", "password": "00000000000000000000000000000000",
                   "code": "xxxx", "uuid": "fake-uuid"}
        resp, http = self.client.post("/user/login", body=payload, token="")
        self.case("N02", "错误密码登录", "POST", "/user/login", {"body": payload},
                  resp, http, "登录失败且不返回 token",
                  isinstance(resp, dict) and not (isinstance(resp.get("data"), dict) and resp["data"].get("token")),
                  "同时覆盖验证码错误场景")

        # N03 重复单号
        self.pace()
        numbers = [b["number"] for b in self.state.get("bill_numbers", []) if b["passed"]]
        if numbers:
            info = {"type": "入库", "subType": "采购", "number": numbers[0],
                    "operTime": day(0), "organId": 57, "accountId": 17,
                    "changeAmount": -100, "totalPrice": -100, "discountLastMoney": 100, "status": "0"}
            rows = []
            body = {"info": json.dumps(info, ensure_ascii=False), "rows": json.dumps(rows)}
            resp, http = self.client.post("/depotHead/addDepotHeadAndDetail", body=body)
            self.case("N03", "重复单据编号提交", "POST", "/depotHead/addDepotHeadAndDetail",
                      {"body": {"number": numbers[0]}}, resp, http,
                      "系统拒绝重复单号",
                      http != 200 or str(resp.get("code")) != "200", "验证单号唯一性约束")

        # N04 不存在的商品条码
        self.pace()
        goods = (self.state.get("materials") or [{}])[0]
        info = {"type": "入库", "subType": "采购", "number": "ERR-%s-001" % BATCH,
                "operTime": day(0), "organId": 57, "accountId": 17,
                "changeAmount": -100, "totalPrice": -100, "status": "0"}
        rows = [{"barCode": "NOT-EXIST-" + BATCH, "unit": "个", "depotId": 14,
                 "operNumber": 1, "unitPrice": 100, "allPrice": 100}]
        body = {"info": json.dumps(info, ensure_ascii=False), "rows": json.dumps(rows)}
        resp, http = self.client.post("/depotHead/addDepotHeadAndDetail", body=body)
        self.case("N04", "不存在的商品条码", "POST", "/depotHead/addDepotHeadAndDetail",
                  {"body": {"barCode": rows[0]["barCode"]}}, resp, http,
                  "系统提示条码不存在",
                  http != 200 or str(resp.get("code")) != "200", "验证明细条码校验")

        # N05 采购单缺少结算账户
        self.pace()
        info = {"type": "入库", "subType": "采购", "number": "ERR-%s-002" % BATCH,
                "operTime": day(0), "organId": 57, "changeAmount": -100,
                "totalPrice": -100, "status": "0"}
        body = {"info": json.dumps(info, ensure_ascii=False), "rows": "[]"}
        resp, http = self.client.post("/depotHead/addDepotHeadAndDetail", body=body)
        self.case("N05", "采购单缺少结算账户", "POST", "/depotHead/addDepotHeadAndDetail",
                  {"body": {"subType": "采购", "accountId": None}}, resp, http,
                  "系统提示必须选择结算账户",
                  http != 200 or str(resp.get("code")) != "200", "验证必填项校验")

        # N06 重复提交防重（相同单号 2 秒内再次提交）
        self.pace()
        if numbers:
            info = {"type": "入库", "subType": "采购", "number": "DUP-%s-001" % BATCH,
                    "operTime": day(0), "organId": 57, "accountId": 17,
                    "changeAmount": -100, "totalPrice": -100, "discountLastMoney": 100, "status": "0"}
            rows = [{"barCode": goods.get("barCode", "1001"), "unit": "个", "depotId": 14,
                     "operNumber": 1, "unitPrice": 100, "allPrice": 100}]
            body = {"info": json.dumps(info, ensure_ascii=False), "rows": json.dumps(rows)}
            r1, h1 = self.client.post("/depotHead/addDepotHeadAndDetail", body=body)
            r2, h2 = self.client.post("/depotHead/addDepotHeadAndDetail", body=body)
            self.case("N06", "2 秒内重复提交同一单据", "POST", "/depotHead/addDepotHeadAndDetail",
                      {"body": {"number": "DUP-%s-001" % BATCH}}, {"first": r1, "second": r2}, h2,
                      "第二次提交被 Redis 防重拦截",
                      (h1 == 200 and str(r1.get("code")) == "200") and
                      (h2 != 200 or str(r2.get("code")) != "200"),
                      "第一次请求用于占用防重键")

        # N07 越权/不存在的 id 查询
        resp, http = self.client.get("/depotHead/info", params={"id": 999999999})
        code = resp.get("code") if isinstance(resp, dict) else None
        data = resp.get("data") if isinstance(resp, dict) else None
        no_info = not (isinstance(data, dict) and (data.get("info") or {}).get("id")) if isinstance(data, dict) else True
        self.case("N07", "查询不存在的单据 id", "GET", "/depotHead/info",
                  {"params": {"id": 999999999}}, resp, http,
                  "返回业务失败或空数据",
                  str(code) in ("200", "500", "300", "None") and no_info,
                  "验证空结果处理")

        # N08 【缺陷探测】进销存列表不传 subType 时的空值容错
        resp, http = self.client.get("/depotHead/list",
                                     params={"search": "{}", "currentPage": 1, "pageSize": 10})
        self.case("N08", "【缺陷探测】单据列表缺省 subType", "GET", "/depotHead/list",
                  {"params": {"search": "{}"}}, resp, http,
                  "系统应对空 subType 容错并返回列表",
                  isinstance(resp, dict) and str(resp.get("code")) == "200",
                  "发现缺陷: DepotHeadService.getBillCategory(null) 抛 NullPointerException, "
                  "接口返回 code=300 数据查询异常")

        # N09 【缺陷探测】不存在的 headerId 查询单据明细
        resp, http = self.client.get("/depotItem/getDetailList",
                                     params={"headerId": 999999999, "mpList": "", "linkType": "basic"})
        self.case("N09", "【缺陷探测】不存在的单据明细 id", "GET", "/depotItem/getDetailList",
                  {"params": {"headerId": 999999999}}, resp, http,
                  "系统应返回空明细而非 500",
                  isinstance(resp, dict) and str(resp.get("code")) == "200",
                  "发现缺陷: DepotItemController.getDetailList 对不存在的单据 id 抛 NullPointerException")
        self.save()

    # ---------------- 阶段 7: 数据一致性与缺陷复核 ----------------
    def sql_rows(self, query):
        env = dict(os.environ)
        proc = subprocess.run(
            ["mysql", "-uroot", "-h127.0.0.1", "-P3306", "-D", "jsh_erp",
             "--default-character-set=utf8mb4", "-N", "-B", "-e", query],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return []
        return [line.split("\t") for line in proc.stdout.strip().splitlines() if line]

    def consistency(self):
        if not os.environ.get("MYSQL_PWD"):
            print("需要环境变量 MYSQL_PWD")
            return
        q1 = ("SELECT COUNT(*) FROM jsh_depot_head h WHERE h.total_price IS NOT NULL "
              "AND ABS(h.total_price) <> (SELECT IFNULL(SUM(d.all_price),0) FROM jsh_depot_item d "
              "WHERE d.header_id=h.id AND IFNULL(d.delete_flag,'0')='0')")
        q2 = ("SELECT COUNT(*) FROM jsh_account_head h WHERE h.total_price IS NOT NULL "
              "AND ABS(h.total_price) <> ABS((SELECT IFNULL(SUM(i.each_amount),0) FROM jsh_account_item i "
              "WHERE i.header_id=h.id AND IFNULL(i.delete_flag,'0')='0'))")
        q3 = ("SELECT COUNT(*), IFNULL(SUM(current_number),0) FROM jsh_material_current_stock "
              "WHERE IFNULL(delete_flag,'0')='0'")
        r1 = self.sql_rows(q1)
        r2 = self.sql_rows(q2)
        r3 = self.sql_rows(q3)
        m1 = int(r1[0][0]) if r1 else -1
        m2 = int(r2[0][0]) if r2 else -1
        stock_rows = int(r3[0][0]) if r3 else 0
        stock_qty = r3[0][1] if r3 else "0"
        self.case("C01", "进销存单据主表与明细金额一致", "SQL", "jsh_erp",
                  {"sql": "ABS(total_price) 与 SUM(all_price) 比对"},
                  {"mismatch": m1}, 200, "不一致记录数 = 0", m1 == 0,
                  "验证新增单据时金额汇总逻辑")
        self.case("C02", "财务单据主表与明细金额一致", "SQL", "jsh_erp",
                  {"sql": "ABS(total_price) 与 ABS(SUM(each_amount)) 比对"},
                  {"mismatch": m2}, 200, "不一致记录数 = 0", m2 == 0,
                  "付款/支出类明细金额在库内为负数，故取绝对值口径")
        self.case("C03", "审核后当前库存已生成", "SQL", "jsh_erp",
                  {"sql": "COUNT/SUM(jsh_material_current_stock)"},
                  {"rows": stock_rows, "totalStock": stock_qty}, 200,
                  "库存记录数 > 0 且数量合计 > 0", stock_rows > 0 and float(stock_qty) > 0,
                  "验证审核触发 updateCurrentStock")
        resp, http = self.client.get("/account/list",
                                     params={"search": "{}", "currentPage": 1, "pageSize": 20})
        rows = ((resp.get("data") or {}).get("rows") or []) if isinstance(resp, dict) else []
        has_amount = bool(rows) and all(x.get("currentAmount") is not None for x in rows)
        self.case("C04", "结算账户动态余额查询", "GET", "/account/list",
                  {"params": {"search": "{}"}}, {"accountCount": len(rows), "hasAmount": has_amount},
                  http, "每个账户返回动态计算的 currentAmount", has_amount,
                  "账户余额由 AccountService 按单据汇总动态计算，物理字段 current_amount 不随审核改写")
        resp, http = self.client.get("/materialCategory/list",
                                     params={"search": "{}", "currentPage": 1, "pageSize": 20})
        self.case("C05", "【缺陷探测】商品分类列表", "GET", "/materialCategory/list",
                  {"params": {"search": "{}"}}, resp, http,
                  "系统应正常返回商品分类列表",
                  isinstance(resp, dict) and str(resp.get("code")) == "200",
                  "发现缺陷: MaterialCategoryMapperEx 参数 offset 未绑定, "
                  "MyBatis 报 BindingException, 接口返回 code=300 数据查询异常")
        self.state["consistency"] = {
            "depot_amount_mismatch": m1, "finance_amount_mismatch": m2,
            "stock_rows": stock_rows, "stock_total": stock_qty,
        }
        self.save()

    # ---------------- 阶段 6: 数据库落库校验 ----------------
    def verify(self):
        pwd = os.environ.get("MYSQL_PWD")
        if not pwd:
            print("需要环境变量 MYSQL_PWD 才能执行数据库校验")
            return
        tables = ["jsh_depot", "jsh_supplier", "jsh_account", "jsh_person", "jsh_in_out_item",
                  "jsh_material", "jsh_material_extend", "jsh_material_current_stock",
                  "jsh_depot_head", "jsh_depot_item", "jsh_account_head", "jsh_account_item",
                  "jsh_serial_number", "jsh_log", "jsh_user_business"]
        sql = " UNION ALL ".join(
            ["SELECT '%s' AS t, COUNT(*) AS c FROM %s" % (t, t) for t in tables])
        env = dict(os.environ)
        proc = subprocess.run(
            ["mysql", "-uroot", "-h127.0.0.1", "-P3306", "-D", "jsh_erp",
             "--default-character-set=utf8mb4", "-N", "-B", "-e", sql],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            print("数据库校验失败:", proc.stderr[:500])
            return
        counts = {}
        for line in proc.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                counts[parts[0]] = int(parts[1])
        self.state["db_counts_after"] = counts
        print("\n=== 数据库表行数（测试后） ===")
        for t in tables:
            print("  %-32s %6d" % (t, counts.get(t, -1)))
        # 关键一致性检查
        checks = [
            ("V01", "进销存单据主表有数据", counts.get("jsh_depot_head", 0) > 0),
            ("V02", "进销存单据子表有数据", counts.get("jsh_depot_item", 0) > 0),
            ("V03", "财务单据主表有数据", counts.get("jsh_account_head", 0) > 0),
            ("V04", "财务单据子表有数据", counts.get("jsh_account_item", 0) > 0),
            ("V05", "商品扩展(条码)表有新增数据", counts.get("jsh_material_extend", 0) > 14),
            ("V06", "操作日志已记录", counts.get("jsh_log", 0) > 1),
        ]
        for cid, name, passed in checks:
            self.case(cid, name, "SQL", "jsh_erp", {"sql": "COUNT(*) 汇总"},
                      counts, 200, "对应表行数大于 0", passed,
                      "通过 MySQL 直连复核接口造数是否真实落库")
        self.save()


def main():
    args = sys.argv[1:]
    phase = args[0] if args else "all"
    reset = "--reset" in args
    if reset:
        global BATCH
        BATCH = "T" + datetime.now().strftime("%m%d%H%M%S")
    suite = Suite(reset=reset)
    login = suite.client.login()
    if not suite.client.token:
        print("登录失败:", json.dumps(login, ensure_ascii=False))
        return
    print("登录成功: jsh / tenant=63 / batch=%s\n" % BATCH)
    phases = {
        "basic": [suite.basic],
        "bills": [suite.bills],
        "audit": [suite.audit],
        "finance": [suite.finance],
        "audit_finance": [suite.audit_finance],
        "query": [suite.query],
        "negative": [suite.negative],
        "verify": [suite.verify],
        "consistency": [suite.consistency],
    }
    if phase == "all":
        todo = [suite.basic, suite.bills, suite.audit, suite.finance, suite.audit_finance,
                suite.query, suite.negative, suite.verify, suite.consistency]
    else:
        todo = phases.get(phase, [])
    if not todo:
        print("未知阶段:", phase)
        return
    for fn in todo:
        print("\n--- 阶段: %s ---" % fn.__name__)
        fn()
    suite.save()
    total = len(suite.results)
    passed = sum(1 for r in suite.results if r["result"] == "PASS")
    print("\n=== 汇总: %d 条用例, PASS %d, FAIL %d ===" % (total, passed, total - passed))


if __name__ == "__main__":
    main()
