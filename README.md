# 归并同一野生动物发现的多源记录基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/sighting_linkage/：多源野生动物目击记录的可解释关联候选、统一事件版本确认与拆回；
- fixtures/：离线验收使用的调查协议、结构化观察记录与凌晨东沟林麝多源上报；
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
PYTHONPATH=src python3 -m sighting_linkage.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析、风险处置，以及凌晨东沟三条林麝上报的关联归并、确认、调度脱敏与证据推翻后拆回，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m sighting_linkage.api --database sightings.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

## 多源目击关联归并（src/sighting_linkage/）

调度室判断“多条消息是不是同一只动物”时：

- 社区护林员、科研人员、巡护员通过 `POST /sight_records` 上报，各记录自带时间误差、
  位置精度（坐标可按敏感物种规程留空）、物种置信度、影像摘要与观察者身份；原始上报只追加、不可改。
- 生态专员 `POST /linkage/runs` 得到逐对的时间/位置/物种/影像/观察者五维评分、中文支持证据与
  排除理由；同内容输入永远返回同一运行（输入摘要 + 算法版本幂等）。
- 生态专员可 `POST /incidents/confirm` 把候选连通的记录确认为统一事件；重复确认稳定返回既有版本，
  加入新记录产生新关联运行后才允许新版本；证据推翻时 `POST /incidents/{id}/dissolve` 追加拆回版本，
  历次决定与全部来源始终保留。
- `GET /incidents/{id}` 沿事件查看全部来源记录、历史版本和未纳入记录的具体排除理由；
  `GET /sight_records/{id}/explain` 按记录查看其被排除在候选之外的理由。
- 敏感位置按每条记录自己的 `visibility`（submitter/ecology/dispatch）做字段级脱敏：
  归并与事件视图不会扩大坐标披露，无权查看坐标的角色连空间维度的距离数值也看不到，
  只能看到“服务端已按完整坐标判定相交/不相交”。
