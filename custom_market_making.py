import logging
from decimal import Decimal
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import OrderFilledEvent
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory, CandlesConfig
from hummingbot.connector.connector_base import ConnectorBase

class MarketMaking(ScriptStrategyBase):
    create_ts = 0
    price_src = PriceType.MidPrice
    pair = "ETH-USDT"
    ex = "binance_paper_trade"
    
    candles = CandlesFactory.get_candle(CandlesConfig(
        connector="binance",
        trading_pair=pair,
        interval="1s",
        max_records=1000
    ))
    
    refresh_time = 1
    amount = Decimal("0.1")
    markup = Decimal("0.0045")
    markets = {ex: {pair}}
    
    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        self.create_ts = 0
        self.first_buy_price = None
        self.trend = "neutral"
        self.trade = []
        self.buys = []
        self.sells = []
        self.buy_count = 0
        self.sell_count = 0
        self.ping_pong = False
        self.last_price = None
        self.candles.start()
        self.logger().setLevel(logging.INFO)

    def on_tick(self):
        try:
            conn = self.connectors[self.ex]
            mid = Decimal(str(conn.get_mid_price(self.pair)))
            self.logger().info(f"Tick at timestamp: {self.current_timestamp}, Mid Price: {mid}")
            if self.create_ts <= self.current_timestamp:
                self.cancel_all_orders()
                prop = self.create_proposal()
                prop_adj = self.adjust_proposal_to_budget(prop)
                self.place_orders(prop_adj)
                self.create_ts = self.current_timestamp + self.refresh_time
                self.logger().info(f"Orders refreshed. Next refresh at: {self.create_ts}, Mid Price: {mid}")

                
                if self.current_timestamp % 60 == 0:
                    self.logger().info(f"Market state: {self.trend}")
                
        except Exception as e:
            self.logger().error(f"Error in on_tick: {e}", exc_info=True)

    def create_proposal(self):
        conn = self.connectors[self.ex]
        mid = Decimal(str(conn.get_mid_price(self.pair)))
        self.logger().info(f"Creating proposal at mid_price: {mid}")
        
        try:
            precision = Decimal(10) ** -conn.price_decimal_places(self.pair)
        except (AttributeError, NotImplementedError):
            precision = Decimal("0.01")
        
        orders = []
        
        if not hasattr(self, 'max_bid'):
            self.max_bid = Decimal("0")
        if not hasattr(self, 'min_ask'):
            self.min_ask = Decimal("999999999")
        if not hasattr(self, 'buffer'):
            self.buffer = Decimal("0.01")
        
        df = self.candles.candles_df
        mkt_trend = "neutral"
        short_trend = "neutral"
        if df is not None and len(df) >= 60:
            obi = self.OBI()
            self.logger().info(f"Order Book Imbalance: {obi}")
            
            vwi = self.volume_weighted_imbalance(df)
            self.logger().info(f"Volume Weighted Imbalance: {vwi}")
            
            spread = self.bid_ask_spread()
            self.logger().info(f"Bid-Ask Spread %: {spread}")
            
            vol = self.short_term_volatility(df)
            self.logger().info(f"Short-term volatility: {vol}")
            
            if obi < Decimal("-0.2") and vwi < -0.1:
                mkt_trend = "downtrend"
            elif obi > Decimal("0.2") and vwi > 0.1:
                mkt_trend = "uptrend"
            else:
                mkt_trend = "neutral"
            self.logger().info(f"Market trend: {mkt_trend}")
            
            if vol > Decimal("0.02") and spread < Decimal("0.5"):
                if obi > Decimal("0.1"):
                    short_trend = "up"
                elif obi < Decimal("-0.1"):
                    short_trend = "down"
                else:
                    short_trend = "neutral"
            self.logger().info(f"Short-term prediction: {short_trend}")
        else:
            self.logger().warning("Insufficient candle data for trend analysis")
        
        eth = conn.get_balance("ETH")
        usdt = conn.get_balance("USDT")
        vol_amt = Decimal("15")
        fees = Decimal("0.001")
        min_profit = Decimal("0.01")
        
        imbalance = self.buy_count - self.sell_count
        self.logger().info(f"Current buy-sell imbalance: {imbalance}")
        
        if not hasattr(self, 'max_price'):
            self.max_price = mid
        if not hasattr(self, 'min_price'):
            self.min_price = mid
        
        self.max_price = max(self.max_price, mid)
        self.min_price = min(self.min_price, mid)
        
        avg_buy = sum(self.buys) / len(self.buys) if self.buys else None
        avg_sell = sum(self.sells) / len(self.sells) if self.sells else None
        
        if avg_sell and avg_buy:
            if avg_sell > avg_buy:
                self.logger().info(f"PROFITABLE: Average sell price ({avg_sell}) is higher than average buy price ({avg_buy})")
            else:
                self.logger().warning(f"UNPROFITABLE: Average sell price ({avg_sell}) is lower than average buy price ({avg_buy})")
                self.buffer = max(self.buffer, Decimal("0.015"))
        
        if mkt_trend == "downtrend":
            if eth >= vol_amt:
                ask = max(mid, (avg_buy * (Decimal("1") + min_profit + fees)) if avg_buy else mid)
                
                if ask <= self.max_bid * (Decimal("1") + self.buffer):
                    ask = self.max_bid * (Decimal("1") + self.buffer)
                    self.logger().info(f"Adjusted ask price to maintain buffer above max bid: {ask}")
                
                if avg_buy and ask <= avg_buy * (Decimal("1") + fees * Decimal("2")):
                    ask = avg_buy * (Decimal("1") + fees * Decimal("2") + min_profit)
                    self.logger().info(f"Adjusted ask price to ensure profit: {ask}")
                
                self.min_ask = min(self.min_ask, ask)
                
                self.logger().info(f"Downtrend: Selling {vol_amt} ETH at {ask}")
                orders.append(OrderCandidate(
                    trading_pair=self.pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.SELL,
                    amount=vol_amt,
                    price=ask.quantize(precision)
                ))
        
        elif mkt_trend == "uptrend":
            if usdt >= vol_amt * mid:
                bid = min(mid, (avg_sell * (Decimal("1") - min_profit - fees)) if avg_sell else mid)
                
                if bid >= self.min_ask * (Decimal("1") - self.buffer):
                    bid = self.min_ask * (Decimal("1") - self.buffer)
                    self.logger().info(f"Adjusted bid price to maintain buffer below min ask: {bid}")
                
                self.max_bid = max(self.max_bid, bid)
                
                self.logger().info(f"Uptrend: Buying {vol_amt} ETH at {bid}")
                orders.append(OrderCandidate(
                    trading_pair=self.pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.BUY,
                    amount=vol_amt,
                    price=bid.quantize(precision)
                ))
        
        if mkt_trend == "neutral":
            target = vol_amt * 2
            if eth < target and usdt >= vol_amt * mid:
                buy_price = (mid * (Decimal("1") - fees - Decimal("0.001"))).quantize(precision)
                
                if buy_price >= self.min_ask * (Decimal("1") - self.buffer):
                    buy_price = self.min_ask * (Decimal("1") - self.buffer)
                    self.logger().info(f"Adjusted neutral buy price to maintain buffer: {buy_price}")
                
                self.max_bid = max(self.max_bid, buy_price)
                
                self.logger().info(f"Neutral market: Placing buy order at {buy_price} for inventory management")
                orders.append(OrderCandidate(
                    trading_pair=self.pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.BUY,
                    amount=vol_amt / 2,
                    price=buy_price
                ))
            
            if eth > target:
                sell_price = (mid * (Decimal("1") + fees + Decimal("0.001") + min_profit)).quantize(precision)
                
                if sell_price <= self.max_bid * (Decimal("1") + self.buffer):
                    sell_price = self.max_bid * (Decimal("1") + self.buffer)
                    self.logger().info(f"Adjusted neutral sell price to maintain buffer: {sell_price}")
                
                if avg_buy and sell_price <= avg_buy * (Decimal("1") + fees * Decimal("2")):
                    sell_price = avg_buy * (Decimal("1") + fees * Decimal("2") + min_profit)
                    self.logger().info(f"Adjusted neutral sell price to ensure profit: {sell_price}")
                
                self.min_ask = min(self.min_ask, sell_price)
                
                self.logger().info(f"Neutral market: Placing sell order at {sell_price} for inventory management")
                orders.append(OrderCandidate(
                    trading_pair=self.pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.SELL,
                    amount=vol_amt / 2,
                    price=sell_price
                ))
        
        if mid >= self.max_price * Decimal("0.99"):
            if eth > vol_amt:
                extreme_sell = mid
                
                if avg_buy is None or extreme_sell > avg_buy * (Decimal("1") + fees * Decimal("2") + min_profit):
                    if extreme_sell <= self.max_bid * (Decimal("1") + self.buffer):
                        extreme_sell = self.max_bid * (Decimal("1") + self.buffer)
                        self.logger().info(f"Adjusted extreme sell price to maintain buffer: {extreme_sell}")
                    
                    self.min_ask = min(self.min_ask, extreme_sell)
                    
                    self.logger().info(f"Extreme bullish price: Selling all available ETH at market price")
                    orders.append(OrderCandidate(
                        trading_pair=self.pair,
                        is_maker=False,
                        order_type=OrderType.MARKET,
                        order_side=TradeType.SELL,
                        amount=eth,
                        price=Decimal("0")
                    ))
                else:
                    self.logger().info(f"Skipped extreme sell due to unprofitability")
        elif mid <= self.min_price * Decimal("1.01"):
            if usdt >= vol_amt * mid:
                extreme_buy = mid
                
                if extreme_buy >= self.min_ask * (Decimal("1") - self.buffer):
                    extreme_buy = self.min_ask * (Decimal("1") - self.buffer)
                    self.logger().info(f"Adjusted extreme buy price to maintain buffer: {extreme_buy}")
                
                self.max_bid = max(self.max_bid, extreme_buy)
                
                self.logger().info(f"Extreme bearish price: Buying to maintain inventory at market price")
                orders.append(OrderCandidate(
                    trading_pair=self.pair,
                    is_maker=False,
                    order_type=OrderType.MARKET,
                    order_side=TradeType.BUY,
                    amount=vol_amt,
                    price=Decimal("0")
                ))
        
        self.logger().info(f"Current max bid price: {self.max_bid}")
        self.logger().info(f"Current min ask price: {self.min_ask}")
        self.logger().info(f"Current price buffer: {self.buffer}")
        self.logger().info(f"Proposal created with {len(orders)} orders.")
        
        return orders

    def bid_ask_spread(self):
        try:
            conn = self.connectors[self.ex]
            best_bid = conn.get_price(self.pair, True)
            best_ask = conn.get_price(self.pair, False)
            if best_bid and best_ask:
                spread = best_ask - best_bid
                spread_pct = (spread / best_ask) * Decimal("100")
                self.logger().debug(f"Bid: {best_bid}, Ask: {best_ask}, Spread %: {spread_pct}")
                return spread_pct
            self.logger().warning("No bid/ask data available")
            return Decimal("0")
        except Exception as e:
            self.logger().error(f"Error in bid_ask_spread: {e}")
            return Decimal("0")

    def OBI(self):
        try:
            book = self.connectors[self.ex].get_order_book(self.pair)
            bids = list(book.bid_entries())[:5]
            asks = list(book.ask_entries())[:5]
            if not bids or not asks:
                self.logger().warning("Insufficient order book data for OBI")
                return Decimal("0")
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            bid_vol = sum([b[1] * (1 - (best_bid - b[0]) / best_bid) for b in bids])
            ask_vol = sum([a[1] * (1 - (a[0] - best_ask) / best_ask) for a in asks])
            total_vol = bid_vol + ask_vol
            obi = (bid_vol - ask_vol) / total_vol if total_vol > 0 else Decimal("0")
            self.logger().debug(f"OBI calculated: Bid volume={bid_vol}, Ask volume={ask_vol}, OBI={obi}")
            return obi
        except Exception as e:
            self.logger().error(f"Error in OBI: {e}")
            return Decimal("0")

    def volume_weighted_imbalance(self, df):
        try:
            recent = df.tail(15).copy()
            if 'buy_volume' not in recent.columns:
                recent.loc[:, 'buy_volume'] = recent.apply(
                    lambda x: x['volume'] if x['close'] >= x['open'] else 0, axis=1)
                recent.loc[:, 'sell_volume'] = recent.apply(
                    lambda x: x['volume'] if x['close'] < x['open'] else 0, axis=1)
            total_vol = recent["volume"].sum()
            if total_vol == 0:
                self.logger().warning("Zero total volume for VWI calculation")
                return 0.0
            weights = np.linspace(0.5, 1.0, len(recent))
            w_buy = sum(recent["buy_volume"] * weights)
            w_sell = sum(recent["sell_volume"] * weights)
            w_total = sum(recent["volume"] * weights)
            vwi = ((w_buy - w_sell) / w_total) if w_total > 0 else 0.0
            self.logger().debug(f"VWI calculated: Buy volume={w_buy}, Sell volume={w_sell}, VWI={vwi}")
            return vwi if not pd.isna(vwi) else 0.0
        except Exception as e:
            self.logger().error(f"Error in volume_weighted_imbalance: {e}")
            return 0.0

    def short_term_volatility(self, df):
        try:
            returns = np.log(df["close"] / df["close"].shift(1)).dropna()
            if len(returns) < 2:
                self.logger().warning("Insufficient returns data for volatility calculation")
                return Decimal("0")
            sigma = returns.tail(50).ewm(span=20).std().iloc[-1] * np.sqrt(1000)
            self.logger().debug(f"Short-term volatility calculated: {sigma}")
            return Decimal(str(sigma)) if not pd.isna(sigma) else Decimal("0")
        except Exception as e:
            self.logger().error(f"Error in short_term_volatility: {e}")
            return Decimal("0")

    def adjust_proposal_to_budget(self, proposal):
        adjusted = self.connectors[self.ex].budget_checker.adjust_candidates(proposal, all_or_none=True)
        return adjusted
    
    def place_orders(self, proposal):
        for order in proposal:
            self.place_order(connector_name=self.ex, order=order)
            self.logger().info(f"Placed order: {order.order_side.name} {order.amount} {order.trading_pair} @ {order.price}")
    
    def place_order(self, connector_name, order):
        if order.order_side == TradeType.SELL:
            self.sell(connector_name=connector_name, trading_pair=order.trading_pair, 
                     amount=self.amount, order_type=order.order_type, price=order.price)
        elif order.order_side == TradeType.BUY:
            self.buy(connector_name=connector_name, trading_pair=order.trading_pair, 
                    amount=self.amount, order_type=order.order_type, price=order.price)
    
    def cancel_all_orders(self):
        active = self.get_active_orders(connector_name=self.ex)
        self.logger().info(f"Canceling {len(active)} active orders")
        for order in active:
            self.cancel(self.ex, order.trading_pair, order.client_order_id)
            self.logger().info(f"Cancelled order: {order.client_order_id}")

    def did_fill_order(self, event: OrderFilledEvent):
        self.logger().info(f"Order filled: {event.trade_type.name} {round(event.amount, 4)} {event.trading_pair} @ {round(event.price, 2)}")
        
        self.trade.append(event.trade_type)  # This should now work
        if len(self.trade) > 10:
            self.trade.pop(0)  # This should now work
        
        if event.trade_type == TradeType.BUY:
            self.buy_count += 1
            self.buys.append(event.price)
            if len(self.buys) > 20:
                self.buys.pop(0)
            if self.first_buy_price is None or event.price < self.first_buy_price:
                self.first_buy_price = event.price
            self.logger().info(f"Buy filled. Total buys: {self.buy_count}, Initial buy price updated to: {self.first_buy_price}")
        else:
            self.sell_count += 1
            self.sells.append(event.price)
            if len(self.sells) > 20:
                self.sells.pop(0)
            if self.buy_count == self.sell_count:
                self.first_buy_price = None
                self.ping_pong = False
            self.logger().info(f"Sell filled. Total sells: {self.sell_count}, Ping-pong reset: {not self.ping_pong}")

    def format_status(self):
        if not self.ready_to_trade:
            return "Market connectors not ready"
        conn = self.connectors[self.ex]
        mid = conn.get_mid_price(self.pair) or Decimal("0")
        eth = conn.get_balance("ETH")
        usdt = conn.get_balance("USDT")
        value = usdt + (eth * mid)
        
        lines = [
            f"MM Status: ETH={eth:.4f}, USDT={usdt:.2f}, Portfolio Value={value:.2f} USDT",
            f"Current Market: Mid Price={mid:.2f}, Trend={self.trend}",
            f"Initial Buy Price: {self.first_buy_price if self.first_buy_price else 'None'}",
            f"Inventory: Buys={self.buy_count}, Sells={self.sell_count}",
            f"Ping-Pong Active: {self.ping_pong}"
        ]
        
        active = self.get_active_orders(connector_name=self.ex)
        if active:
            lines.append(f"Active Orders: {len(active)}")
            for order in active:
                lines.append(f"  {order.trading_pair} {order.is_buy} {order.quantity} @ {order.price:.2f}")
        
        if self.trade:
            lines.append(f"Recent trades: {[t.name for t in self.trade[-5:]]}")
        
        if self.buys and self.sells:
            avg_buy = sum(self.buys) / len(self.buys)
            avg_sell = sum(self.sells) / len(self.sells)
            pnl = ((avg_sell / avg_buy) - 1) * 100
            lines.append(f"Avg Buy: {avg_buy:.2f}, Avg Sell: {avg_sell:.2f}, PnL: {pnl:.2f}%")
        
        self.logger().info("\n" + "\n".join(lines))
        return "\n".join(lines)
