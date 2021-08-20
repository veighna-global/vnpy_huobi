import re
import urllib
import base64
import json
import zlib
import hashlib
import hmac
from copy import copy
from datetime import datetime, timedelta
from threading import Lock
from typing import Sequence
import pytz
from typing import Dict, List, Any
from time import sleep

from vnpy.event import Event, EventEngine
from vnpy_rest import RestClient, Request, Response
from vnpy_websocket import WebsocketClient
from vnpy.trader.constant import (
    Direction,
    Offset,
    Exchange,
    Product,
    Status,
    OrderType,
    Interval
)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    TickData,
    OrderData,
    TradeData,
    BarData,
    AccountData,
    PositionData,
    ContractData,
    OrderRequest,
    CancelRequest,
    SubscribeRequest,
    HistoryRequest
)
from vnpy.trader.event import EVENT_TIMER


# 中国时区
CHINA_TZ = pytz.timezone("Asia/Shanghai")

# 实盘REST API地址
REST_HOST: str = "https://api.hbdm.com"

# 实盘Websocket API地址
INVERSE_WEBSOCKET_DATA_HOST: str = "wss://api.hbdm.com/swap-ws"
INVERSE_WEBSOCKET_TRADE_HOST: str = "wss://api.hbdm.com/swap-notification"

# 委托状态映射
STATUS_HUOBIS2VT: Dict[int, Status] = {
    3: Status.NOTTRADED,
    4: Status.PARTTRADED,
    5: Status.CANCELLED,
    6: Status.ALLTRADED,
    7: Status.CANCELLED,
}

# 委托类型映射
ORDERTYPE_VT2HUOBIS: Dict[OrderType, Any] = {
    OrderType.MARKET: "opponent",
    OrderType.LIMIT: "limit",
    OrderType.FOK: "fok",
    OrderType.FAK: "ioc"
}
ORDERTYPE_HUOBIS2VT: Dict[Any, OrderType] = {v: k for k, v in ORDERTYPE_VT2HUOBIS.items()}
ORDERTYPE_HUOBIS2VT[1] = OrderType.LIMIT
ORDERTYPE_HUOBIS2VT[3] = OrderType.MARKET
ORDERTYPE_HUOBIS2VT[4] = OrderType.MARKET
ORDERTYPE_HUOBIS2VT[5] = OrderType.STOP
ORDERTYPE_HUOBIS2VT[6] = OrderType.LIMIT
ORDERTYPE_HUOBIS2VT["lightning"] = OrderType.MARKET
ORDERTYPE_HUOBIS2VT["optimal_5"] = OrderType.MARKET
ORDERTYPE_HUOBIS2VT["optimal_10"] = OrderType.MARKET
ORDERTYPE_HUOBIS2VT["optimal_20"] = OrderType.MARKET

# 买卖方向映射
DIRECTION_VT2HUOBIS: Dict[Direction, str] = {
    Direction.LONG: "buy",
    Direction.SHORT: "sell",
}
DIRECTION_HUOBIS2VT: Dict[str, Direction] = {v: k for k, v in DIRECTION_VT2HUOBIS.items()}

# 开平方向映射
OFFSET_VT2HUOBIS: Dict[Offset, str] = {
    Offset.OPEN: "open",
    Offset.CLOSE: "close",
}
OFFSET_HUOBIS2VT: Dict[str, Offset] = {v: k for k, v in OFFSET_VT2HUOBIS.items()}

# 数据频率映射
INTERVAL_VT2HUOBIS: Dict[Interval, str] = {
    Interval.MINUTE: "1min",
    Interval.HOUR: "60min",
    Interval.DAILY: "1day"
}

# 时间间隔映射
TIMEDELTA_MAP: Dict[Interval, timedelta] = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}


class HuobiInverseGateway(BaseGateway):
    """
    vn.py用于对接火币币本位永续合约账户的交易接口。
    """

    default_setting: Dict[str, Any] = {
        "key": "",
        "secret": "",
        "代理地址": "",
        "代理端口": "",
    }

    exchanges: Exchange = [Exchange.HUOBI]

    def __init__(self, event_engine: EventEngine, gateway_name: str = "HUOBI_INVERSE") -> None:
        """构造函数"""
        super().__init__(event_engine, gateway_name)

        self.rest_api: "HuobiInverseRestApi" = HuobiInverseRestApi(self)
        self.trade_ws_api: "HuobiInverseTradeWebsocketApi" = HuobiInverseTradeWebsocketApi(self)
        self.market_ws_api: "HuobiInverseDataWebsocketApi" = HuobiInverseDataWebsocketApi(self)

    def connect(self, setting: dict) -> None:
        """连接交易接口"""
        key: str = setting["key"]
        secret: str = setting["secret"]
        proxy_host: str = setting["代理地址"]
        proxy_port: str = setting["代理端口"]

        if proxy_port.isdigit():
            proxy_port = int(proxy_port)
        else:
            proxy_port = 0

        self.rest_api.connect(key, secret,proxy_host, proxy_port)
        self.trade_ws_api.connect(key, secret, proxy_host, proxy_port)
        self.market_ws_api.connect(key, secret, proxy_host, proxy_port)

        self.init_query()

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        self.market_ws_api.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        return self.rest_api.send_order(req)

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        self.rest_api.cancel_order(req)

    def send_orders(self, reqs: Sequence[OrderRequest]) -> str:
        """批量下单"""
        return self.rest_api.send_orders(reqs)

    def query_account(self) -> None:
        """查询资金"""
        self.rest_api.query_account()

    def query_position(self) -> None:
        """查询持仓"""
        self.rest_api.query_position()

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        return self.rest_api.query_history(req)

    def close(self) -> None:
        """关闭连接"""
        self.rest_api.stop()
        self.trade_ws_api.stop()
        self.market_ws_api.stop()

    def process_timer_event(self, event: Event) -> None:
        """定时事件处理"""
        self.count += 1
        if self.count < 3:
            return

        self.query_account()
        self.query_position()

    def init_query(self) -> None:
        """初始化查询任务"""
        self.count: int = 0
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)


class HuobiInverseRestApi(RestClient):
    """火币币本位永续合约REST API"""

    def __init__(self, gateway: HuobiInverseGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: HuobiInverseGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.host: str = ""
        self.key: str = ""
        self.secret: str = ""
        self.account_id: str = ""

        self.order_count: int = 10000
        self.order_count_lock: Lock = Lock()
        self.connect_time: int = 0

        self.positions: Dict[str, PositionData] = {}
        self.contract_codes: set = set()

    def sign(self, request: Request) -> Request:
        """生成火币签名"""
        request.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.71 Safari/537.36",
            "Connection": "close"
        }
        params_with_signature = create_signature(
            self.key,
            request.method,
            self.host,
            request.path,
            self.secret,
            request.params
        )
        request.params = params_with_signature

        if request.method == "POST":
            request.headers["Content-Type"] = "application/json"

            if request.data:
                request.data = json.dumps(request.data)

        return request

    def connect(
        self,
        key: str,
        secret: str,
        proxy_host: str,
        proxy_port: int
    ) -> None:
        """连接REST服务器"""
        self.key = key
        self.secret = secret
        self.host, _ = _split_url(REST_HOST)
        self.connect_time = int(datetime.now(CHINA_TZ).strftime("%y%m%d%H%M%S"))

        self.init(REST_HOST, proxy_host, proxy_port)
        self.start()

        self.gateway.write_log("REST API启动成功")

        self.query_contract()

    def query_account(self) -> None:
        """查询资金"""
        self.add_request(
            method="POST",
            path="/swap-api/v1/swap_account_info",
            callback=self.on_query_account
        )

    def query_position(self) -> None:
        """查询未成交委托"""
        self.add_request(
            method="POST",
            path="/swap-api/v1/swap_position_info",
            callback=self.on_query_position
        )

    def query_order(self, contract_code: str) -> None:
        """查询合约信息"""
        data: dict = {"contract_code": contract_code}

        self.add_request(
            method="POST",
            path="/swap-api/v1/swap_openorders",
            callback=self.on_query_order,
            data=data,
            extra=contract_code
        )

    def query_contract(self) -> None:
        """查询合约信息"""
        self.add_request(
            method="GET",
            path="/swap-api/v1/swap_contract_info",
            callback=self.on_query_contract
        )

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        history: List[BarData] = []
        count: int = 1999
        start: datetime = req.start
        time_delta: timedelta = TIMEDELTA_MAP[req.interval]

        # 合约名转换
        buf: list = [i for i in req.symbol if not i.isdigit()]
        symbol: str = "".join(buf)

        ws_symbol: str = f"{symbol}"

        while True:
            # 计算结束时间
            end: datetime = start + time_delta * count

            # 创建查询参数
            params: dict = {
                "contract_code": ws_symbol,
                "period": INTERVAL_VT2HUOBIS[req.interval],
                "from": int(start.timestamp()),
                "to": int(end.timestamp())
            }

            resp: Response = self.request(
                "GET",
                path="/swap-ex/market/history/kline",
                params=params
            )

            # 如果请求失败则终止循环
            if resp.status_code // 100 != 2:
                msg: str = f"获取历史数据失败，状态码：{resp.status_code}，信息：{resp.text}"
                self.gateway.write_log(msg)
                break
            else:
                data: dict = resp.json()
                if not data:
                    msg: str = f"获取历史数据为空"
                    self.gateway.write_log(msg)
                    break

                if not data["data"]:
                    msg: str = f"获取历史数据为空"
                    self.gateway.write_log(msg)
                    break

                buf: List[BarData] = []
                for row in data["data"]:
                    dt: datetime = generate_datetime(row["id"])

                    bar: BarData = BarData(
                        symbol=req.symbol,
                        exchange=req.exchange,
                        datetime=dt,
                        interval=req.interval,
                        volume=row["vol"],
                        open_price=row["open"],
                        high_price=row["high"],
                        low_price=row["low"],
                        close_price=row["close"],
                        gateway_name=self.gateway_name
                    )
                    buf.append(bar)

                history.extend(buf)

                begin: datetime = buf[0].datetime
                end: datetime = buf[-1].datetime
                msg: str = f"获取历史数据成功，{req.symbol} - {req.interval.value}，{begin} - {end}"
                self.gateway.write_log(msg)

                # 更新开始时间
                start: datetime = bar.datetime

                # 如果收到了最后一批数据则终止循环
                if len(buf) < count:
                    break

        return history

    def new_local_orderid(self) -> str:
        """生成本地委托号"""
        with self.order_count_lock:
            self.order_count += 1
            local_orderid: str = f"{self.connect_time}{self.order_count}"
            return local_orderid

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        local_orderid: str = self.new_local_orderid()
        order: OrderData = req.create_order_data(
            local_orderid,
            self.gateway_name
        )
        order.datetime = datetime.now(CHINA_TZ)

        data: dict = {
            "contract_code": req.symbol,
            "client_order_id": int(local_orderid),
            "price": req.price,
            "volume": int(req.volume),
            "direction": DIRECTION_VT2HUOBIS.get(req.direction, ""),
            "offset": OFFSET_VT2HUOBIS.get(req.offset, ""),
            "order_price_type": ORDERTYPE_VT2HUOBIS.get(req.type, ""),
            "lever_rate": 20
        }

        self.add_request(
            method="POST",
            path="/swap-api/v1/swap_order",
            callback=self.on_send_order,
            data=data,
            extra=order,
            on_error=self.on_send_order_error,
            on_failed=self.on_send_order_failed
        )

        self.gateway.on_order(order)
        return order.vt_orderid

    def send_orders(self, reqs: Sequence[OrderRequest]) -> List[str]:
        """批量下单"""
        orders_data: List[Dict] = []
        orders: List[OrderData] = []
        vt_orderids: List[str] = []

        for req in reqs:
            local_orderid: str = self.new_local_orderid()

            order: OrderData = req.create_order_data(
                local_orderid,
                self.gateway_name
            )
            order.datetime = datetime.now(CHINA_TZ)
            self.gateway.on_order(order)

            d: dict = {
                "contract_code": req.symbol,
                "client_order_id": int(local_orderid),
                "price": req.price,
                "volume": int(req.volume),
                "direction": DIRECTION_VT2HUOBIS.get(req.direction, ""),
                "offset": OFFSET_VT2HUOBIS.get(req.offset, ""),
                "order_price_type": ORDERTYPE_VT2HUOBIS.get(req.type, ""),
                "lever_rate": 20
            }

            orders_data.append(d)
            orders.append(order)
            vt_orderids.append(order.vt_orderid)

        data: dict = {
            "orders_data": orders_data
        }
        self.add_request(
            method="POST",
            path="/swap-api/v1/swap_batchorder",
            callback=self.on_send_orders,
            data=data,
            extra=orders,
            on_error=self.on_send_orders_error,
            on_failed=self.on_send_orders_failed
        )
        return vt_orderids

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        data: dict = {
            "contract_code": req.symbol,
        }

        orderid: int = int(req.orderid)
        if orderid > 1000000:
            data["client_order_id"] = orderid
        else:
            data["order_id"] = orderid

        self.add_request(
            method="POST",
            path="/swap-api/v1/swap_cancel",
            callback=self.on_cancel_order,
            on_failed=self.on_cancel_order_failed,
            data=data,
            extra=req
        )

    def on_query_account(self, data: dict, request: Request) -> None:
        """资金查询回报"""
        if self.check_error(data, "查询账户"):
            return

        for d in data["data"]:
            account: AccountData = AccountData(
                accountid=d["symbol"],
                balance=d["margin_balance"],
                frozen=d["margin_frozen"],
                gateway_name=self.gateway_name,
            )
            self.gateway.on_account(account)

    def on_query_position(self, data: dict, request: Request) -> None:
        """持仓查询回报"""
        if self.check_error(data, "查询持仓"):
            return

        for position in self.positions.values():
            position.volume = 0
            position.frozen = 0
            position.price = 0
            position.pnl = 0

        for d in data["data"]:
            key: str = f"{d['contract_code']}_{d['direction']}"
            position: PositionData = self.positions.get(key, None)

            if not position:
                position: PositionData = PositionData(
                    symbol=d["contract_code"],
                    exchange=Exchange.HUOBI,
                    direction=DIRECTION_HUOBIS2VT[d["direction"]],
                    gateway_name=self.gateway_name
                )
                self.positions[key] = position

            position.volume = d["volume"]
            position.frozen = d["frozen"]
            position.price = d["cost_hold"]
            position.pnl = d["profit"]

        for position in self.positions.values():
            self.gateway.on_position(position)

    def on_query_order(self, data: dict, request: Request) -> None:
        """未成交委托查询回报"""
        if self.check_error(data, "查询活动委托"):
            return

        for d in data["data"]["orders"]:
            timestamp: float = d["created_at"]
            dt: datetime = generate_datetime(timestamp / 1000)

            if d["client_order_id"]:
                orderid: int = d["client_order_id"]
            else:
                orderid: int = d["order_id"]

            order: OrderData = OrderData(
                orderid=orderid,
                symbol=d["contract_code"],
                exchange=Exchange.HUOBI,
                price=d["price"],
                volume=d["volume"],
                type=ORDERTYPE_HUOBIS2VT[d["order_price_type"]],
                direction=DIRECTION_HUOBIS2VT[d["direction"]],
                offset=OFFSET_HUOBIS2VT[d["offset"]],
                traded=d["trade_volume"],
                status=STATUS_HUOBIS2VT[d["status"]],
                datetime=dt,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_order(order)

        self.gateway.write_log(f"{request.extra}活动委托信息查询成功")

        if self.order_codes:
            sleep(0.1)
            contract_code = self.order_codes.pop()
            self.query_order(contract_code)

    def on_query_contract(self, data: dict, request: Request) -> None:
        """合约信息查询回报"""
        if self.check_error(data, "查询合约"):
            return

        for d in data["data"]:
            # 只支持全仓模式
            self.contract_codes.add(d["contract_code"])

            contract: ContractData = ContractData(
                symbol=d["contract_code"],
                exchange=Exchange.HUOBI,
                name=d["contract_code"],
                pricetick=d["price_tick"],
                size=d["contract_size"],
                min_volume=1,
                product=Product.FUTURES,
                history_data=True,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_contract(contract)

        self.gateway.write_log("合约信息查询成功")

        # 开始查询未成交委托信息
        self.order_codes = copy(self.contract_codes)
        contract_code = self.order_codes.pop()
        self.query_order(contract_code)

    def on_send_order(self, data: dict, request: Request) -> None:
        """委托下单回报"""
        order: OrderData = request.extra

        if self.check_error(data, "委托"):
            order.status = Status.REJECTED
            self.gateway.on_order(order)

    def on_send_order_failed(self, status_code: str, request: Request) -> None:
        """委托下单失败服务器报错回报"""
        order: OrderData = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        msg: str = f"委托失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_send_order_error(self, exception_type: type, exception_value: Exception, tb, request: Request
    ) -> None:
        """委托下单回报函数报错回报"""
        order: OrderData = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tb, request)

    def on_cancel_order(self, data: dict, request: Request) -> None:
        """委托撤单回报"""
        self.check_error(data, "撤单")

    def on_cancel_order_failed(self, status_code: str, request: Request) -> None:
        """委托撤单失败服务器报错回报"""
        msg: str = f"撤单失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_send_orders(self, data: dict, request: Request) -> None:
        """批量下单回报"""
        orders: List[OrderData] = request.extra

        errors: dict = data.get("errors", None)
        if errors:
            for d in errors:
                ix: int = d["index"]
                code: int = d["err_code"]
                msg: str = d["err_msg"]

                order: OrderData = orders[ix]
                order.status = Status.REJECTED
                self.gateway.on_order(order)

                msg: str = f"批量委托失败，状态码：{code}，信息：{msg}"
                self.gateway.write_log(msg)

    def on_send_orders_failed(self, status_code: str, request: Request) -> None:
        """批量下单失败服务器报错回报"""
        orders: List[OrderData] = request.extra

        for order in orders:
            order.status = Status.REJECTED
            self.gateway.on_order(order)

        msg: str = f"批量委托失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_send_orders_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ) -> None:
        """批量下单回报函数报错回报"""
        orders: List[OrderData] = request.extra

        for order in orders:
            order.status = Status.REJECTED
            self.gateway.on_order(order)

        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tb, request)

    def check_error(self, data: dict, func: str = "") -> bool:
        """回报状态检查"""
        if data["status"] != "error":
            return False

        error_code: int = data["err_code"]
        error_msg: str = data["err_msg"]

        self.gateway.write_log(f"{func}请求出错，代码：{error_code}，信息：{error_msg}")
        return True


class HuobiInverseWebsocketApiBase(WebsocketClient):
    """火币币本位永续合约Websocket APIBase"""

    def __init__(self, gateway: HuobiInverseGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: HuobiInverseGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.key: str = ""
        self.secret: str = ""
        self.sign_host: str = ""
        self.path: str = ""

    def connect(
        self,
        key: str,
        secret: str,
        url: str,
        proxy_host: str,
        proxy_port: int
    ) -> None:
        """连接Websocket频道"""
        self.key = key
        self.secret = secret

        host, path = _split_url(url)
        self.sign_host = host
        self.path = path

        self.init(url, proxy_host, proxy_port)
        self.start()

    def login(self) -> int:
        """用户登录"""
        params: dict = {
            "op": "auth",
            "type": "api"
        }
        params.update(
            create_signature(
                self.key,
                "GET",
                self.sign_host,
                self.path,
                self.secret
            )
        )
        return self.send_packet(params)

    def on_login(self, packet: dict) -> None:
        """用户登录回报"""
        pass

    @staticmethod
    def unpack_data(data) -> json.JSONDecoder:
        """"""
        return json.loads(zlib.decompress(data, 31))

    def on_packet(self, packet: dict) -> None:
        """推送数据回报"""
        if "ping" in packet:
            req: dict = {"pong": packet["ping"]}
            self.send_packet(req)
        elif "op" in packet and packet["op"] == "ping":
            req: dict = {
                "op": "pong",
                "ts": packet["ts"]
            }
            self.send_packet(req)
        elif "err-msg" in packet:
            return self.on_error_msg(packet)
        elif "op" in packet and packet["op"] == "auth":
            return self.on_login()
        else:
            self.on_data(packet)

    def on_error_msg(self, packet: dict) -> None:
        """推送错误信息回报"""
        msg: str = packet["err-msg"]
        if msg == "invalid pong":
            return

        self.gateway.write_log(packet["err-msg"])


class HuobiInverseTradeWebsocketApi(HuobiInverseWebsocketApiBase):
    """火币币本位永续合约交易Websocket API"""

    def __init__(self, gateway):
        """构造函数"""
        super().__init__(gateway)

    def connect(
        self,
        key: str,
        secret: str,
        proxy_host: str,
        proxy_port: int
    ) -> None:
        """连接Websocket交易频道"""
        super().connect(
            key,
            secret,
            INVERSE_WEBSOCKET_TRADE_HOST,
            proxy_host,
            proxy_port
        )

    def subscribe(self) -> None:
        """订阅委托推送"""
        req: dict = {
            "op": "sub",
            "topic": f"orders.*"
        }
        self.send_packet(req)

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("交易Websocket API连接成功")
        self.login()

    def on_login(self) -> None:
        """登录成功回报"""
        self.gateway.write_log("交易Websocket API登录成功")
        self.subscribe()

    def on_data(self, packet: dict) -> None:
        """推送数据回报"""
        op: str = packet.get("op", None)
        if op != "notify":
            return

        topic: str = packet["topic"]
        if "orders" in topic:
            self.on_order(packet)

    def on_order(self, data: dict) -> None:
        """委托更新推送"""
        dt: datetime = generate_datetime(data["created_at"] / 1000)

        if data["client_order_id"]:
            orderid: int = data["client_order_id"]
        else:
            orderid: int = data["order_id"]

        order: OrderData = OrderData(
            symbol=data["contract_code"],
            exchange=Exchange.HUOBI,
            orderid=orderid,
            type=ORDERTYPE_HUOBIS2VT[data["order_price_type"]],
            direction=DIRECTION_HUOBIS2VT[data["direction"]],
            offset=OFFSET_HUOBIS2VT[data["offset"]],
            price=data["price"],
            volume=data["volume"],
            traded=data["trade_volume"],
            status=STATUS_HUOBIS2VT[data["status"]],
            datetime=dt,
            gateway_name=self.gateway_name
        )
        self.gateway.on_order(order)

        # 成交事件推送
        trades = data["trade"]
        if not trades:
            return

        for d in trades:
            dt: datetime = generate_datetime(d["created_at"] / 1000)

            trade: TradeData = TradeData(
                symbol=order.symbol,
                exchange=Exchange.HUOBI,
                orderid=order.orderid,
                tradeid=str(d["id"]),
                direction=order.direction,
                offset=order.offset,
                price=d["trade_price"],
                volume=d["trade_volume"],
                datetime=dt,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_trade(trade)


class HuobiInverseDataWebsocketApi(HuobiInverseWebsocketApiBase):
    """火币币本位永续合约行情Websocket API"""

    def __init__(self, gateway):
        """构造函数"""
        super().__init__(gateway)

        self.ticks: Dict[str, TickData] = {}
        self.subscribed: Dict[str, SubscribeRequest] = {}

    def connect(
        self,
        key: str,
        secret: str,
        proxy_host: str,
        proxy_port: int
    ) -> None:
        """连接Websocket行情频道"""
        super().connect(
            key,
            secret,
            INVERSE_WEBSOCKET_DATA_HOST,
            proxy_host,
            proxy_port
        )

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("行情Websocket API连接成功")

        for req in list(self.subscribed.values()):
            self.subscribe(req)

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""

        # 缓存订阅记录
        self.subscribed[req.vt_symbol] = req

        # 创建TICK对象
        tick = TickData(
            symbol=req.symbol,
            name=req.symbol,
            exchange=Exchange.HUOBI,
            datetime=datetime.now(CHINA_TZ),
            gateway_name=self.gateway_name,
        )
        self.ticks[req.symbol] = tick

        self.subscribe_data(req.symbol)

        req_dict = {
            "sub": f"market.{req.symbol}.depth.step0"
        }
        self.send_packet(req_dict)

        req_dict = {
            "sub": f"market.{req.symbol}.detail"
        }
        self.send_packet(req_dict)

    def on_data(self, packet) -> None:
        """推送数据回报"""
        channel: str = packet.get("ch", None)
        if channel:
            if "depth.step" in channel:
                self.on_market_depth(packet)
            elif "detail" in channel:
                self.on_market_detail(packet)
        elif "err_code" in packet:
            code: int = packet["err_code"]
            msg: str = packet["err_msg"]
            self.gateway.write_log(f"错误代码：{code}, 错误信息：{msg}")

    def on_market_depth(self, data: dict) -> None:
        """行情深度推送 """
        ws_symbol: str = data["ch"].split(".")[1]
        tick: TickData = self.ticks[ws_symbol]
        tick.datetime = generate_datetime(data["ts"] / 1000)

        tick_data : dict= data["tick"]
        if "bids" not in tick_data or "asks" not in tick_data:
            return

        bids: list = tick_data["bids"]
        for n in range(min(5, len(bids))):
            price, volume = bids[n]
            tick.__setattr__("bid_price_" + str(n + 1), float(price))
            tick.__setattr__("bid_volume_" + str(n + 1), float(volume))

        asks = tick_data["asks"]
        for n in range(min(5, len(asks))):
            price, volume = asks[n]
            tick.__setattr__("ask_price_" + str(n + 1), float(price))
            tick.__setattr__("ask_volume_" + str(n + 1), float(volume))

        if tick.last_price:
            self.gateway.on_tick(copy(tick))

    def on_market_detail(self, data: dict) -> None:
        """市场细节推送"""
        ws_symbol: str = data["ch"].split(".")[1]
        tick: TickData = self.ticks[ws_symbol]
        tick.datetime = generate_datetime(data["ts"] / 1000)

        tick_data = data["tick"]
        tick.open_price = tick_data["open"]
        tick.high_price = tick_data["high"]
        tick.low_price = tick_data["low"]
        tick.last_price = tick_data["close"]
        tick.volume = tick_data["vol"]

        if tick.bid_price_1:
            self.gateway.on_tick(copy(tick))


def _split_url(url) -> str:
    """
    将url拆分为host和path
    :return: host, path
    """
    result = re.match("\w+://([^/]*)(.*)", url)  # noqa
    if result:
        return result.group(1), result.group(2)


def create_signature(
    api_key: str,
    method: str,
    host: str,
    path: str,
    secret_key: str,
    get_params=None
) -> Dict[str, str]:
    """
    创建Rest接口签名
    """
    sorted_params: list = [
        ("AccessKeyId", api_key),
        ("SignatureMethod", "HmacSHA256"),
        ("SignatureVersion", "2"),
        ("Timestamp", datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"))
    ]

    if get_params:
        sorted_params.extend(list(get_params.items()))
        sorted_params = list(sorted(sorted_params))
    encode_params = urllib.parse.urlencode(sorted_params)

    payload: list = [method, host, path, encode_params]
    payload: str = "\n".join(payload)
    payload: str = payload.encode(encoding="UTF8")

    secret_key: str = secret_key.encode(encoding="UTF8")

    digest: bytes = hmac.new(secret_key, payload, digestmod=hashlib.sha256).digest()
    signature: bytes = base64.b64encode(digest)

    params: dict = dict(sorted_params)
    params["Signature"] = signature.decode("UTF8")
    return params


def generate_datetime(timestamp: float) -> datetime:
    """生成时间"""
    dt: datetime = datetime.fromtimestamp(timestamp)
    dt: datetime = CHINA_TZ.localize(dt)
    return dt