# 调度放行与杆侧补报

宽限过后原车仍未到场时，调度把泊位可用时段临时放出；晚到的杆侧记录可能补证原车其实准时。本模块在放行与补报之间做分钟级协调，凭据全程留痕，规则版本 `DISPATCH-1.0`。

## 数据与入口

| 动作 | 入口 | 说明 |
|---|---|---|
| 发布放行 | `POST /api/dispatch/releases` | `sample` 或 `bundle` 提供预约上下文，另带 `berth,start,end,grace_minutes,source_reservation?` |
| 查看清单 | `GET /api/dispatch/releases`、`GET /api/dispatch/releases/{credential}` | 分钟台账含 `PUBLISHED/BLOCKED/REVOKED` 与撤回它的补报编号 |
| 提交补报 | `POST /api/dispatch/backfills` | `event_id,berth,plate,start,end,reservation?`，按 `event_id` 幂等 |
| 补报留痕 | `GET /api/dispatch/backfills` | 每条事件的结论、分段核价与撤回凭据 |
| 对账页面 | `GET /dispatch` | 发布放行、查看当前清单与补报核价 |

放行库默认与清算库同文件，可用环境变量 `CURB_RELEASE` 单独指定。

## 规则

1. **重叠分钟留给下笔有效预约。** 发布时取同窗同泊位的 `ACTIVE` 预约（`source_reservation` 指向的原预约除外），与放行窗相交的分钟标 `BLOCKED`，其余标 `PUBLISHED`。半开区间按 UTC 分钟对齐。
2. **补报只撤相撞分钟。** 补报证实有车时，仅把相交凭据上仍是 `PUBLISHED` 的分钟改为 `REVOKED` 并登记 `backfill_id`；`BLOCKED` 与其余分钟不动，凭据本体保留可查。已撤回分钟不会被第二个事件重复撤回。
3. **首免与临停。** 补报到达时刻不晚于匹配预约起点加宽限，记 `HONORED`，预约窗内每次停靠最多首免 10 分钟；实际迟到或无生效预约而停进已放行时段，记 `TEMPORARY`，无首免，按临停核价。
4. **同车同日档位延续。** 临停付费分钟按 UTC+08:00 自然日累计，前 15 分钟每分钟 20 分、之后每分钟 40 分，午夜重置；免费分钟不消耗档位。
5. **两班同提交只出一份凭据。** 凭据键由 `berth+start+end` 摘要生成，唯一约束加进程内互斥锁保证并发下一份凭据；响应以 `reused` 标识是否复用。
6. **重跑旧清单不覆盖补报。** 补报按 `event_id` 去重，查重—撤回—入账在同一把锁内完成；重复提交即使内容不同也只返回首次结果，撤回不重复计数。

## 凭据结构

```json
{
  "credential": "9d8a26d0…",
  "berth": "B-01",
  "published_minutes": 40,
  "blocked_minutes": 0,
  "revoked_minutes": 10,
  "published_window": [{"start": "…", "end": "…"}],
  "minutes": [{"at": "…", "state": "REVOKED", "backfill_id": "E-1001"}]
}
```

车辆只出现 HMAC 代号，与清算流水线共用站点密钥；时间统一存 UTC，页面按 UTC+08:00 展示。
