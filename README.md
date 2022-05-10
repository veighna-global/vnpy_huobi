# vn.py框架的火币交易接口

<p align="center">
  <img src ="https://vnpy.oss-cn-shanghai.aliyuncs.com/vnpy-logo.png"/>
</p>

<p align="center">
    <img src ="https://img.shields.io/badge/version-2021.9.24-blueviolet.svg"/>
    <img src ="https://img.shields.io/badge/platform-windows|linux|macos-yellow.svg"/>
    <img src ="https://img.shields.io/badge/python-3.7-blue.svg" />
    <img src ="https://img.shields.io/github/license/vnpy/vnpy.svg?color=orange"/>
</p>

关于使用VeighNa框架进行Crypto交易的话题，新开了一个[Github Discussions论坛](https://github.com/vn-crypto/vnpy_crypto/discussions)，欢迎通过这里来进行讨论交流。


## 说明

基于火币交易所的API开发，支持账户下的现货、期货、永续合约（正反向）交易。

**注意：本接口只支持全仓保证金模式**。

请在火币网站完成账户的相应设置后使用。

## 安装

安装需要基于2.6.0版本以上的[VN Studio](https://www.vnpy.com)。

直接使用pip命令：

```
pip install vnpy_huobi
```

下载解压后在cmd中运行

```
python setup.py install
```

## 使用

以脚本方式启动（script/run.py）：

```
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_binance import (
    HuobiSpotGateway,
    HuobiFuturesGateway,
    HuobiUsdtGateway,
    HuobiInverseGateway
)


def main():
    """主入口函数"""
    qapp = create_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(HuobiSpotGateway)
    main_engine.add_gateway(HuobiFuturesGateway)
    main_engine.add_gateway(HuobiUsdtGateway)
    main_engine.add_gateway(HuobiInverseGateway)

    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
```
