# 航班时刻运行台

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18219。测试：docker compose exec -T app pytest -q

## 模块

- M1 航段采信：页面 /legs，接口 /api/legs/import
- M2 机场时区：页面 /airports，接口 /api/airports
- M3 当地时刻：页面 /local-time，接口 /api/local-time/resolve
- M4 UTC 航链：页面 /itineraries，接口 /api/itineraries/build
- M5 衔接判定：页面 /connections，接口 /api/connections/check
- M6 批次导出：页面 /batches，接口 /api/reconcile
- M7 索赔结算：页面 /claims，接口 /api/claims/register、/revise、/settle、/settle-batch、/withdraw，回读 GET /api/claims/{claim_id}

## M7 误机索赔结算

接入实际航段后，以「行程标识 + 失接点（机场/前后航班）」为索赔自然键，跨批次重复登记不重复赔。
未结算单按航司补发的新事实重算应赔；已结算单原凭证保持原样，差额走关联补赔或追偿凭证；
撤回用反向分录。正常衔接或证据不全拒付，金额一律整数分。写接口支持请求幂等键（X-Request-Key），
回执丢失后同键重试查回原分录，不重复赔款；修订与结算带 expected_version 乐观锁，并发旧版本拒绝。
系统只入账、不出真实支付，不调整时区规则。字段与错误码见 docs/M7-CLAIMS.md，陷阱映射见 docs/DEPTH.md。

端到端演示（不依赖服务运行）：

    python3 -I scripts/demo_m7_claims.py

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

