"""管理员监控服务：直连 PostgreSQL 只读 obs_* 表，提供观测指标分析与 REST 查询接口。

与 backend 运行时的观测写入（agent_nodes._observability / _obs_storage）解耦：
本服务不 import backend 的任何模块，仅读取共享的 obs_* 系列表做只读聚合。
"""
