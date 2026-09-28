# 归并同一野生动物发现的多源记录基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/sighting_correlation/：多源野生动物目击上报的可解释关联、统一事件确认与拆回，敏感位置按提交者可见范围投影；
- fixtures/：离线验收使用的调查协议、结构化观察记录与东沟林麝多源上报样例；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
PYTHONPATH=src python3 -m sighting_correlation.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析、风险处置，以及凌晨六点东沟三条林麝上报的关联、确认与拆回，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m sighting_correlation.api --database correlation.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

## 多源目击关联归并

巡护员、社区护林员和科研人员的目击上报写入后不可变，关联评估按记录自带的时间误差、
位置精度（点含不确定半径，或山谷多边形）、物种置信度、影像感知哈希和观察者关系五个
维度两两打分，并以时间、空间、高置信物种冲突三条硬阻断排除明显无关的记录；候选连边的
连通分量即建议归并的候选组。关联版本以算法版本号加全部记录内容摘要内容寻址：重复评估
稳定返回同一版本，只有纳入新记录才产生新版本。

生态专员把候选组确认为统一事件；证据被推翻时可部分或全部拆回，确认与每次拆回都作为
独立决定保留。查询时可沿统一事件列出全部来源（含被拆出的来源），也可通过
`/sightings/{id}/exclusion/{version}` 查看某条记录被排除在候选之外的具体理由。

归并不扩大敏感位置的披露：精确坐标只对提交者本人及其 `visible_to` 授权对象开放；其他
人看到的关联判断相同，但位置遮蔽为多边形顶点数或不确定半径，精确距离降级为公里级区间。
