import ccxt
import platform
import pandas as pd
import numpy as np
import asyncio
import time
from datetime import datetime
import ccxt.async_support as ccxt  # ? 使用异步模式

class BinanceQuant:

    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    def __init__(self, api_key, api_secret):
        # 初始化交易所连接
        self.exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'recvWindow': 5000,
            'timeDifference': 3000,
            'verbose': False,
            'options': {'defaultType': 'future'},
            'enableRateLimit': True
        })
        self.exchange.set_sandbox_mode(True)
        self.symbols = []
        self.positions = {}
        self.daily_pnl = 0
        self.max_daily_loss = -0.10
        self.trade_log = []

    async def initialize(self):
        """初始化交易系统"""
        await self.load_markets()
        # await self.update_top_symbols()
        await self.get_open_positions()
        print(f"系统初始化完成，当前时间：{datetime.utcnow().isoformat()}")

    async def generate_report(self):
        with open('daily_report.md', 'w') as f:
            f.write(f"# {datetime.today()} 交易报告\n")
            f.write(f"- 初始净值：{self.initial_equity:.2f}\n")
            f.write(f"- 最终净值：{self.get_usdt_balance():.2f}\n")
            f.write("## 交易明细\n")
            for trade in self.trade_log:
                f.write(f"### {trade['symbol']} | {trade['side']}\n")

    async def load_markets(self):
        """加载市场数据"""
        markets = await self.exchange.load_markets()
        self.usdt_futures = [
            s for s in markets
            if markets[s]['quote'] == 'USDT'
               and markets[s]['type'] == 'future'
        ]

    async def get_open_positions(self):
        """从 Binance 获取当前持仓"""
        try:
            positions = await self.exchange.fetch_positions()
            self.positions = {
                p['symbol']: {
                    "entry_price": float(p['entryPrice']),
                    "amount": float(p['contracts'])
                }
                for p in positions if float(p['contracts']) > 0
            }
            print("当前持仓：", self.positions)
        except Exception as e:
            print(f"获取持仓失败：{e}")
            return 0
    async def update_top_symbols(self):
        """更新前100涨幅标的"""
        tickers = await self.exchange.fetch_tickers(self.usdt_futures)
        sorted_symbols = sorted(
            tickers.items(),
            key=lambda x: x[1]['percentage'],
            reverse=True
        )[:100]
        self.symbols = [s[0] for s in sorted_symbols]
        print(f"标的更新完成，当前标的数量：{len(self.symbols)}")

    async def fetch_ohlcv(self, symbol):
        """获取1小时K线数据"""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol,
                '1h',
                limit=21
            )
            return pd.DataFrame(ohlcv, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume'
            ])
        except Exception as e:
            print(f"获取{symbol} K线数据失败：{str(e)}")
            return None

    def calculate_bollinger(self, df):
        """计算布林带指标"""
        df['MA20'] = df['close'].rolling(20).mean()
        df['STD'] = df['close'].rolling(20).std()
        df['Upper'] = df['MA20'] + 2 * df['STD']
        df['Lower'] = df['MA20'] - 2 * df['STD']
        return df

    def check_uptrend(self, df):
        """检查上升趋势"""
        # 检查最近5根K线中轨是否连续上升
        ma_values = df['MA20'].values[-5:]
        return all(ma_values[i + 1] > ma_values[i] for i in range(4))

    def check_entry_signal(self, df):
        """检查入场信号"""
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # 价格回踩中轨条件
        touch_condition = (
                (latest['low'] <= latest['MA20'] * 1.005) and
                (latest['close'] > latest['MA20']) and
                (prev['close'] < prev['MA20'])
        )
        return touch_condition

    async def execute_trade(self, symbol):
        """执行交易"""
        try:
            # 设置杠杆和保证金模式
            await self.exchange.set_leverage(10, symbol)
            await self.exchange.set_margin_mode('cross', symbol)

            # 获取最新价格
            ticker = await self.exchange.fetch_ticker(symbol)
            entry_price = ticker['last']

            # 计算合约数量
            amount = round(2000 / entry_price,
                           self.exchange.market(symbol)['precision']['amount'])

            # 下单逻辑
            order = await self.exchange.create_order(
                symbol=symbol,
                type='MARKET',
                side='buy',
                amount=amount,
                params={'positionSide': 'LONG'}
            )

            # 记录持仓
            self.positions[symbol] = {
                'entry_price': entry_price,
                'amount': amount,
                'tp1_executed': False,
                'sl_price': entry_price * 0.98,
                'entry_time': datetime.utcnow()
            }

            # 记录交易日志
            self.trade_log.append({
                'symbol': symbol,
                'action': 'BUY',
                'price': entry_price,
                'amount': amount,
                'timestamp': datetime.utcnow()
            })
            print(f"{symbol} 开仓成功 @ {entry_price}")

        except Exception as e:
            print(f"{symbol} 下单失败：{str(e)}")

    async def monitor_positions(self):
        """监控持仓状态"""
        while True:
            if not self.positions:
                await asyncio.sleep(5)
                continue

            for symbol in list(self.positions.keys()):
                position = self.positions[symbol]

                # 获取最新价格
                ticker = await self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']

                # 止损检查
                if current_price <= position['sl_price']:
                    await self.close_position(symbol, reason="止损")
                    continue

                # 第一阶段止盈检查
                if not position['tp1_executed'] and \
                        current_price >= position['entry_price'] * 1.05:
                    await self.close_partial(symbol, 0.5)
                    position['tp1_executed'] = True
                    print(f"{symbol} 第一阶段止盈触发")

                # 第二阶段止盈检查
                if position['tp1_executed']:
                    # 获取最新布林带上轨
                    df = await self.fetch_ohlcv(symbol)
                    upper_band = df['Upper'].iloc[-1]

                    if current_price >= upper_band:
                        await self.close_position(symbol, reason="上轨止盈")
                        continue

                    # 保本止损检查
                    if current_price <= position['entry_price']:
                        await self.close_position(symbol, reason="保本止损")

            await asyncio.sleep(3)  # 每3秒检查一次

    async def close_partial(self, symbol, ratio):
        """部分平仓"""
        try:
            position = self.positions[symbol]
            close_amount = round(position['amount'] * ratio,
                                 self.exchange.market(symbol)['precision']['amount'])

            await self.exchange.create_order(
                symbol=symbol,
                type='MARKET',
                side='sell',
                amount=close_amount,
                params={'positionSide': 'LONG'}
            )

            # 更新持仓记录
            position['amount'] -= close_amount
            self.trade_log.append({
                'symbol': symbol,
                'action': 'SELL_PARTIAL',
                'price': await self.get_current_price(symbol),
                'amount': close_amount,
                'timestamp': datetime.utcnow()
            })

        except Exception as e:
            print(f"{symbol} 部分平仓失败：{str(e)}")

    async def close_position(self, symbol, reason=""):
        """完全平仓"""
        try:
            position = self.positions.pop(symbol)
            await self.exchange.create_order(
                symbol=symbol,
                type='MARKET',
                side='sell',
                amount=position['amount'],
                params={'positionSide': 'LONG'}
            )

            self.trade_log.append({
                'symbol': symbol,
                'action': 'SELL_ALL',
                'price': await self.get_current_price(symbol),
                'amount': position['amount'],
                'timestamp': datetime.utcnow(),
                'reason': reason
            })
            print(f"{symbol} 平仓完成，原因：{reason}")

        except Exception as e:
            print(f"{symbol} 平仓失败：{str(e)}")
    # 新增的获取 USDT 余额方法
    async def run_balance_check(self):
        """定时余额检查任务"""
        while True:
            await self.get_usdt_balance()
            await asyncio.sleep(5)  # 等待3秒

    async def get_usdt_balance(self):
        """获取账户中的 USDT 余额"""
        try:
            balance = await self.exchange.fetch_balance()  # 获取余额
            usdt_balance = balance['total'].get('USDT', 0)  # 安全地访问USDT的余额
            print("当前usdt余额"+str(usdt_balance))
            return float(usdt_balance)  # 转换为浮动数值并返回
        except Exception as e:
            print(f"获取 USDT 余额失败: {str(e)}")
            return 0  # 如果发生异常，返回 0 作为默认值
    async def risk_management(self):
        """实时风控监控"""
        while True:
            # 计算当日盈亏
            equity = await self.get_usdt_balance()
            if equity < (self.initial_equity * (1 + self.max_daily_loss)):
                print("触发每日最大亏损，停止所有交易！")
                await self.close_all_positions()
                os._exit(1)

            # 单笔交易风控
            for symbol in self.positions:
                position = self.positions[symbol]
                current_price = await self.get_current_price(symbol)
                loss_pct = (current_price - position['entry_price']) / position['entry_price']
                if loss_pct <= -0.05:
                    await self.close_position(symbol, reason="单笔止损")

            await asyncio.sleep(10)

    async def main_loop(self):
        """主交易循环"""
        await self.initialize()

        self.initial_equity = await self.get_usdt_balance()

        # 启动监控任务
        asyncio.create_task(self.monitor_positions())
        asyncio.create_task(self.risk_management())

        while True:
            try:
                for symbol in self.symbols:
                    # 获取K线数据
                    df = await self.fetch_ohlcv(symbol)
                    if df is None or len(df) < 21:
                        continue

                    # 计算指标
                    df = self.calculate_bollinger(df)

                    # 检查入场条件
                    if self.check_uptrend(df) and self.check_entry_signal(df):
                        # 风控检查
                        if await self.pass_risk_check(symbol):
                            await self.execute_trade(symbol)

                # 每小时更新标的列表
                if datetime.utcnow().minute == 0:
                    await self.get_open_positions()
                    # await self.update_top_symbols()
                    await self.generate_report()

                await asyncio.sleep(60)  # 每分钟扫描一次

            except Exception as e:
                print(f"主循环错误：{str(e)}")
                await asyncio.sleep(30)

    async def pass_risk_check(self, symbol):
        """通过风控检查"""
        # 检查现有持仓数量
        if len(self.positions) >= 20:  # 最大同时持仓20个
            return False

        # 检查重复持仓
        if symbol in self.positions:
            return False

        # 检查保证金充足性
        balance = await self.exchange.fetch_balance()
        available = float(balance['USDT']['free'])
        if available < 200:
            return False

        return True


# 使用示例
if __name__ == "__main__":
    api_key = "4214f694f6a44619ab6454905d28eb5f3f86c24520b949e1333f323234b77888"
    api_secret = "31a0444fb10731c0c6393fe9f81b89c42871e37318b7f7e2bcb88989a0fd433d"

    quant = BinanceQuant(api_key, api_secret)

    try:
        asyncio.run(quant.main_loop())
    except KeyboardInterrupt:
        print("程序手动终止")
        asyncio.run(quant.close_all_positions())
        wait-exchange.close()
