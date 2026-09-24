# 电厂调度与能源分析与机组分析准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录电力市场基准电价、电厂与变电站设施、送出线路、燃料批次、发电计划和负荷情景，并保留机组巡检传感器统计分析准入流程。系统面向电价连续波动、关键送电送出线路恢复、电量调度和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 电力市场基准电价按结算日和来源修订登记，历史版本不会被覆盖；
- 电厂、储罐、终端与储能站设施建档，送出线路保存日能力、在途时间和损耗规则；
- 送出线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 燃料批次保留电源类型、牌号、数量、单位成本和接收时间，可计算加权燃料库存成本；
- 交易方提名支持载荷级幂等、优先级分配、燃料库存扣减和在途交接；
- 燃料船到厂窗口按目的地设施的 IANA 时区解析本地日历时刻，冻结发运时的 tzdata 版本；签收扫描必须带时区偏移，缺失时刻顺延、重复时刻取第二次出现，迟到只结转新增数量，重复扫描不重复增加库存；
- 负荷情景保存电价变化、送出线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

机组分析准入子域位于 `plant_science` 包，负责机组巡检传感器的设备构建登记、不可变校准协议、测点分片导入、异常测点复核、统计任务租约、分析准入决定和审计报告。该子域不连接传感器硬件，只处理已经结构化的校准记录。

## 目录

- `src/power_dispatch/`：电价、设施、送出线路、燃料库存、提名、负荷情景、HTTP API 与离线验收；
- `src/plant_science/`：机组巡检传感器校准与统计分析准入；
- `fixtures/`：机组分析准入演示协议和结构化测点；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m power_dispatch.acceptance --workspace .
```

该命令会在内存数据库中登记六个结算日的峰谷电价，创建电厂、终端和送出线路，完成燃料库存入账、提名分配、送电及负荷情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

机组分析准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m plant_science.acceptance --workspace .
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m power_dispatch.api --database power_dispatch.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖电价、设施、送出线路、停运事件、燃料批次、提名、能力分配、送电、到厂窗口与签收、负荷情景和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。

### 到厂窗口与签收

燃料船发运后，调度为送电单登记到厂窗口，之后按实际到港时刻签收：

- `POST /transfers/{transfer_id}/delivery-window`：开窗。`eta_local` 是**目的地设施本地日历时间**（`YYYY-MM-DDTHH:MM:SS`，不带时区偏移），系统按设施建档时的 IANA 时区解析并冻结 `timezone`、`tz_version`（IANA tzdata 版本）、`eta_at` 与 `deadline_at`（ETA 加 `grace_hours`）。
- `POST /transfers/{transfer_id}/delivery-scans`：登记一次签收扫描。`scanned_at` **必须带时区偏移**（如 `2026-11-01T06:00:00Z`），`quantity_mwh` 为本次实到数量，`idempotency_key` 保证重放幂等。
- `GET /transfers/{transfer_id}/delivery`：核对全部窗口版本、扫描分类和目的地库存变化。

本地日历到 UTC 的解析规则（PEP 495 fold 语义，两种异常都取较晚候选瞬间，结果确定可审计）：

- 正常时刻唯一映射；
- **缺失时刻**（春令时向前跳变中空掉的本地时间）顺延到跳变后的第一个合法时刻，`eta_kind=gap`；
- **重复时刻**（秋令时回拨中出现两次的本地时间）取第二次出现，`eta_kind=repeated`。

超时与部分签收语义：

- 扫描时刻晚于冻结的 `deadline_at` 判为 `late`，否则为 `on_time`；
- 迟到时原窗口只冻结此前已准时签收数量并以 `closed_carried` 归档，**仅把剩余新增数量结转到新批次**，结转批继承同一冻结窗口（相同 ETA、截止时刻、时区与 tzdata 版本），已准时签收部分不会被误标为超时；
- 同时刻同数量的重复扫描记为 `duplicate`、`applied_mwh=0`，不重复增加库存；窗口全部关闭后的全新扫描会被拒绝。
