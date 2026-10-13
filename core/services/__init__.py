"""core/services —— 路由层业务编排服务（T7：从 routes/investor.py 下沉）。

模块按业务域划分，供 Flask 路由薄封装调用；纯策略计算仍在 strategy/，
本包承接「读库 + 组装响应」类编排逻辑，不持有 Flask request 上下文。
"""
