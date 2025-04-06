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
    create_timestamp = 0
    price_source = PriceType.MidPrice
    trading_pair = "ETH-USDT"
    exchange = "binance_paper_trade"
    
    candles = CandlesFactory.get_candle(CandlesConfig(
        connector="binance",
        trading_pair=trading_pair,
        interval="1s",
        max_records=1000
    ))
    
    # Strategy parameters
    order_refresh_time = 1
    order_amount = Decimal("0.1")
    min_markup = Decimal("0.0045")  # 0.3% to cover 0.2% fees + 0.1% profit
    markets = {exchange: {trading_pair}}
    
    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        self.create_timestamp = 0
        self.initial_buy_price = None
        self.trend_state = "neutral"
        self.trade_history = []
        self.filled_buy_prices = []
        self.filled_sell_prices = []
        self.total_buys = 0
        self.total_sells = 0
        self.ping_pong_active = False
        self.last_mid_price = None
        self.candles.start()
        self.logger().setLevel(logging.INFO)
        self.logger().info("MarketMaking strategy initialized")

    def on_tick(self):
        try:
            connector = self.connectors[self.exchange]
            mid_price = Decimal(str(connector.get_mid_price(self.trading_pair)))
            self.logger().info(f"Tick at timestamp: {self.current_timestamp}, Mid Price: {mid_price}")
            if self.create_timestamp <= self.current_timestamp:
                self.cancel_all_orders()
                proposal = self.create_proposal()
                proposal_adjusted = self.adjust_proposal_to_budget(proposal)
                self.place_orders(proposal_adjusted)
                self.create_timestamp = self.current_timestamp + self.order_refresh_time
                self.logger().info(f"Orders refreshed. Next refresh at: {self.create_timestamp}, Mid Price: {mid_price}")

                
                if self.current_timestamp % 60 == 0:
                    self.logger().info(f"Market state: {self.trend_state}")
                
        except Exception as e:
            self.logger().error(f"Error in on_tick: {e}", exc_info=True)

    def create_proposal(self):
        connector = self.connectors[self.exchange]
        mid_price = Decimal(str(connector.get_mid_price(self.trading_pair)))
        self.logger().info(f"Creating proposal at mid_price: {mid_price}")
        
        try:
            price_precision = Decimal(10) ** -connector.price_decimal_places(self.trading_pair)
        except (AttributeError, NotImplementedError):
            price_precision = Decimal("0.01")
        
        orders = []
        
        # Initialize tracking variables if they don't exist
        if not hasattr(self, 'max_bid_price'):
            self.max_bid_price = Decimal("0")
        if not hasattr(self, 'min_ask_price'):
            self.min_ask_price = Decimal("999999999")
        if not hasattr(self, 'price_buffer'):
            self.price_buffer = Decimal("0.01")  # Increased buffer to 1% from 0.2%
        
        # Calculate market indicators
        df = self.candles.candles_df
        market_trend = "neutral"
        short_term_trend = "neutral"
        if df is not None and len(df) >= 60:
            # Use OBI for order book analysis
            obi = self.OBI()
            self.logger().info(f"Order Book Imbalance: {obi}")
            
            # Use Volume Weighted Imbalance for trend analysis
            vwi = self.volume_weighted_imbalance(df)
            self.logger().info(f"Volume Weighted Imbalance: {vwi}")
            
            # Calculate bid-ask spread
            spread_pct = self.bid_ask_spread()
            self.logger().info(f"Bid-Ask Spread %: {spread_pct}")
            
            # Calculate short-term volatility
            volatility = self.short_term_volatility(df)
            self.logger().info(f"Short-term volatility: {volatility}")
            
            # Determine market trend based on OBI and VWI
            if obi < Decimal("-0.2") and vwi < -0.1:
                market_trend = "downtrend"
            elif obi > Decimal("0.2") and vwi > 0.1:
                market_trend = "uptrend"
            else:
                market_trend = "neutral"
            self.logger().info(f"Market trend: {market_trend}")
            
            # Calculate short-term prediction based on volatility and spread
            if volatility > Decimal("0.02") and spread_pct < Decimal("0.5"):
                if obi > Decimal("0.1"):
                    short_term_trend = "up"
                elif obi < Decimal("-0.1"):
                    short_term_trend = "down"
                else:
                    short_term_trend = "neutral"
            self.logger().info(f"Short-term prediction: {short_term_trend}")
        else:
            self.logger().warning("Insufficient candle data for trend analysis")
        
        current_eth = connector.get_balance("ETH")
        current_usdt = connector.get_balance("USDT")
        volume_amount = Decimal("15")
        fees_per_volume = Decimal("0.001")
        min_profit_margin = Decimal("0.01")  # 1% minimum profit margin
        
        buy_sell_imbalance = self.total_buys - self.total_sells
        self.logger().info(f"Current buy-sell imbalance: {buy_sell_imbalance}")
        
        # Track extreme prices
        if not hasattr(self, 'max_price'):
            self.max_price = mid_price
        if not hasattr(self, 'min_price'):
            self.min_price = mid_price
        
        self.max_price = max(self.max_price, mid_price)
        self.min_price = min(self.min_price, mid_price)
        
        # Calculate average buy and sell prices
        avg_buy_price = sum(self.filled_buy_prices) / len(self.filled_buy_prices) if self.filled_buy_prices else None
        avg_sell_price = sum(self.filled_sell_prices) / len(self.filled_sell_prices) if self.filled_sell_prices else None
        
        if avg_sell_price and avg_buy_price:
            if avg_sell_price > avg_buy_price:
                self.logger().info(f"PROFITABLE: Average sell price ({avg_sell_price}) is higher than average buy price ({avg_buy_price})")
            else:
                self.logger().warning(f"UNPROFITABLE: Average sell price ({avg_sell_price}) is lower than average buy price ({avg_buy_price})")
                # Adjust buffer if we're unprofitable
                self.price_buffer = max(self.price_buffer, Decimal("0.015"))  # Increase buffer to at least 1.5%
        
        # STRICT ENFORCEMENT: Never allow sell price below max bid price + buffer
        # Never allow buy price above min ask price - buffer
        
        if market_trend == "downtrend":
            if current_eth >= volume_amount:
                potential_ask_price = max(mid_price, (avg_buy_price * (Decimal("1") + min_profit_margin + fees_per_volume)) if avg_buy_price else mid_price)
                
                # CRITICAL: Ensure ask price is above max bid price plus buffer
                if potential_ask_price <= self.max_bid_price * (Decimal("1") + self.price_buffer):
                    potential_ask_price = self.max_bid_price * (Decimal("1") + self.price_buffer)
                    self.logger().info(f"Adjusted ask price to maintain buffer above max bid: {potential_ask_price}")
                
                # Ensure we're not selling at a loss compared to our average buy price
                if avg_buy_price and potential_ask_price <= avg_buy_price * (Decimal("1") + fees_per_volume * Decimal("2")):
                    potential_ask_price = avg_buy_price * (Decimal("1") + fees_per_volume * Decimal("2") + min_profit_margin)
                    self.logger().info(f"Adjusted ask price to ensure profit: {potential_ask_price}")
                
                self.min_ask_price = min(self.min_ask_price, potential_ask_price)
                
                self.logger().info(f"Downtrend: Selling {volume_amount} ETH at {potential_ask_price}")
                orders.append(OrderCandidate(
                    trading_pair=self.trading_pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.SELL,
                    amount=volume_amount,
                    price=potential_ask_price.quantize(price_precision)
                ))
        
        elif market_trend == "uptrend":
            if current_usdt >= volume_amount * mid_price:
                potential_bid_price = min(mid_price, (avg_sell_price * (Decimal("1") - min_profit_margin - fees_per_volume)) if avg_sell_price else mid_price)
                
                # CRITICAL: Ensure bid price is below min ask price minus buffer
                if potential_bid_price >= self.min_ask_price * (Decimal("1") - self.price_buffer):
                    potential_bid_price = self.min_ask_price * (Decimal("1") - self.price_buffer)
                    self.logger().info(f"Adjusted bid price to maintain buffer below min ask: {potential_bid_price}")
                
                self.max_bid_price = max(self.max_bid_price, potential_bid_price)
                
                self.logger().info(f"Uptrend: Buying {volume_amount} ETH at {potential_bid_price}")
                orders.append(OrderCandidate(
                    trading_pair=self.trading_pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.BUY,
                    amount=volume_amount,
                    price=potential_bid_price.quantize(price_precision)
                ))
        
        # Neutral market - inventory management with buffer enforcement
        if market_trend == "neutral":
            inventory_target = volume_amount * 2
            if current_eth < inventory_target and current_usdt >= volume_amount * mid_price:
                buy_price = (mid_price * (Decimal("1") - fees_per_volume - Decimal("0.001"))).quantize(price_precision)
                
                # Ensure buy price doesn't overlap with min ask
                if buy_price >= self.min_ask_price * (Decimal("1") - self.price_buffer):
                    buy_price = self.min_ask_price * (Decimal("1") - self.price_buffer)
                    self.logger().info(f"Adjusted neutral buy price to maintain buffer: {buy_price}")
                
                self.max_bid_price = max(self.max_bid_price, buy_price)
                
                self.logger().info(f"Neutral market: Placing buy order at {buy_price} for inventory management")
                orders.append(OrderCandidate(
                    trading_pair=self.trading_pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.BUY,
                    amount=volume_amount / 2,
                    price=buy_price
                ))
            
            if current_eth > inventory_target:
                # Only place sell orders if they're profitable
                sell_price = (mid_price * (Decimal("1") + fees_per_volume + Decimal("0.001") + min_profit_margin)).quantize(price_precision)
                
                # Ensure sell price doesn't overlap with max bid
                if sell_price <= self.max_bid_price * (Decimal("1") + self.price_buffer):
                    sell_price = self.max_bid_price * (Decimal("1") + self.price_buffer)
                    self.logger().info(f"Adjusted neutral sell price to maintain buffer: {sell_price}")
                
                # Ensure we're not selling at a loss compared to our average buy price
                if avg_buy_price and sell_price <= avg_buy_price * (Decimal("1") + fees_per_volume * Decimal("2")):
                    sell_price = avg_buy_price * (Decimal("1") + fees_per_volume * Decimal("2") + min_profit_margin)
                    self.logger().info(f"Adjusted neutral sell price to ensure profit: {sell_price}")
                
                self.min_ask_price = min(self.min_ask_price, sell_price)
                
                self.logger().info(f"Neutral market: Placing sell order at {sell_price} for inventory management")
                orders.append(OrderCandidate(
                    trading_pair=self.trading_pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.SELL,
                    amount=volume_amount / 2,
                    price=sell_price
                ))
        
        # Extreme price actions - MODIFIED to respect buffer and profitability
        if mid_price >= self.max_price * Decimal("0.99"):  # Within 1% of max price
            if current_eth > volume_amount:
                extreme_sell_price = mid_price
                
                # Only sell at extreme prices if it's profitable
                if avg_buy_price is None or extreme_sell_price > avg_buy_price * (Decimal("1") + fees_per_volume * Decimal("2") + min_profit_margin):
                    # Ensure extreme sell price doesn't overlap with max bid
                    if extreme_sell_price <= self.max_bid_price * (Decimal("1") + self.price_buffer):
                        extreme_sell_price = self.max_bid_price * (Decimal("1") + self.price_buffer)
                        self.logger().info(f"Adjusted extreme sell price to maintain buffer: {extreme_sell_price}")
                    
                    self.min_ask_price = min(self.min_ask_price, extreme_sell_price)
                    
                    self.logger().info(f"Extreme bullish price: Selling all available ETH at market price")
                    orders.append(OrderCandidate(
                        trading_pair=self.trading_pair,
                        is_maker=False,
                        order_type=OrderType.MARKET,
                        order_side=TradeType.SELL,
                        amount=current_eth,
                        price=Decimal("0")
                    ))
                else:
                    self.logger().info(f"Skipped extreme sell due to unprofitability")
        elif mid_price <= self.min_price * Decimal("1.01"):  # Within 1% of min price
            if current_usdt >= volume_amount * mid_price:
                extreme_buy_price = mid_price
                
                # Ensure extreme buy price doesn't overlap with min ask
                if extreme_buy_price >= self.min_ask_price * (Decimal("1") - self.price_buffer):
                    extreme_buy_price = self.min_ask_price * (Decimal("1") - self.price_buffer)
                    self.logger().info(f"Adjusted extreme buy price to maintain buffer: {extreme_buy_price}")
                
                self.max_bid_price = max(self.max_bid_price, extreme_buy_price)
                
                self.logger().info(f"Extreme bearish price: Buying to maintain inventory at market price")
                orders.append(OrderCandidate(
                    trading_pair=self.trading_pair,
                    is_maker=False,
                    order_type=OrderType.MARKET,
                    order_side=TradeType.BUY,
                    amount=volume_amount,
                    price=Decimal("0")
                ))
        
        # Log the current bid-ask tracking values
        self.logger().info(f"Current max bid price: {self.max_bid_price}")
        self.logger().info(f"Current min ask price: {self.min_ask_price}")
        self.logger().info(f"Current price buffer: {self.price_buffer}")
        self.logger().info(f"Proposal created with {len(orders)} orders.")
        
        return orders

    def bid_ask_spread(self):
        try:
            connector = self.connectors[self.exchange]
            best_bid = connector.get_price(self.trading_pair, True)
            best_ask = connector.get_price(self.trading_pair, False)
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
            order_book = self.connectors[self.exchange].get_order_book(self.trading_pair)
            bids = list(order_book.bid_entries())[:5]
            asks = list(order_book.ask_entries())[:5]
            if not bids or not asks:
                self.logger().warning("Insufficient order book data for OBI")
                return Decimal("0")
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            bid_volume = sum([b[1] * (1 - (best_bid - b[0]) / best_bid) for b in bids])
            ask_volume = sum([a[1] * (1 - (a[0] - best_ask) / best_ask) for a in asks])
            total_volume = bid_volume + ask_volume
            obi = (bid_volume - ask_volume) / total_volume if total_volume > 0 else Decimal("0")
            self.logger().debug(f"OBI calculated: Bid volume={bid_volume}, Ask volume={ask_volume}, OBI={obi}")
            return obi
        except Exception as e:
            self.logger().error(f"Error in OBI: {e}")
            return Decimal("0")

    def volume_weighted_imbalance(self, df):
        try:
            recent_candles = df.tail(15).copy()
            if 'buy_volume' not in recent_candles.columns:
                recent_candles.loc[:, 'buy_volume'] = recent_candles.apply(
                    lambda x: x['volume'] if x['close'] >= x['open'] else 0, axis=1)
                recent_candles.loc[:, 'sell_volume'] = recent_candles.apply(
                    lambda x: x['volume'] if x['close'] < x['open'] else 0, axis=1)
            total_volume = recent_candles["volume"].sum()
            if total_volume == 0:
                self.logger().warning("Zero total volume for VWI calculation")
                return 0.0
            weights = np.linspace(0.5, 1.0, len(recent_candles))
            weighted_buy_volume = sum(recent_candles["buy_volume"] * weights)
            weighted_sell_volume = sum(recent_candles["sell_volume"] * weights)
            weighted_total = sum(recent_candles["volume"] * weights)
            vwi = ((weighted_buy_volume - weighted_sell_volume) / weighted_total) if weighted_total > 0 else 0.0
            self.logger().debug(f"VWI calculated: Buy volume={weighted_buy_volume}, Sell volume={weighted_sell_volume}, VWI={vwi}")
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
        adjusted_proposal = self.connectors[self.exchange].budget_checker.adjust_candidates(proposal, all_or_none=True)
        # self.logger().info(f"Adjusted proposal to budget. Original orders: {len(proposal)}, Adjusted orders: {len(adjusted_proposal)}")
        return adjusted_proposal
    
    def place_orders(self, proposal):
        # self.logger().info(f"Placing {len(proposal)} orders")
        for order in proposal:
            self.place_order(connector_name=self.exchange, order=order)
            self.logger().info(f"Placed order: {order.order_side.name} {order.amount} {order.trading_pair} @ {order.price}")
    
    def place_order(self, connector_name, order):
        if order.order_side == TradeType.SELL:
            self.sell(connector_name=connector_name, trading_pair=order.trading_pair, 
                     amount=self.order_amount, order_type=order.order_type, price=order.price)
        elif order.order_side == TradeType.BUY:
            self.buy(connector_name=connector_name, trading_pair=order.trading_pair, 
                    amount=self.order_amount, order_type=order.order_type, price=order.price)
    
    def cancel_all_orders(self):
        active_orders = self.get_active_orders(connector_name=self.exchange)
        self.logger().info(f"Canceling {len(active_orders)} active orders")
        for order in active_orders:
            self.cancel(self.exchange, order.trading_pair, order.client_order_id)
            self.logger().info(f"Cancelled order: {order.client_order_id}")

    def did_fill_order(self, event: OrderFilledEvent):
        self.logger().info(f"Order filled: {event.trade_type.name} {round(event.amount, 4)} {event.trading_pair} @ {round(event.price, 2)}")
        
        self.trade_history.append(event.trade_type)
        if len(self.trade_history) > 10:
            self.trade_history.pop(0)
        
        if event.trade_type == TradeType.BUY:
            self.total_buys += 1
            self.filled_buy_prices.append(event.price)
            if len(self.filled_buy_prices) > 20:
                self.filled_buy_prices.pop(0)
            if self.initial_buy_price is None or event.price < self.initial_buy_price:
                self.initial_buy_price = event.price
            self.logger().info(f"Buy filled. Total buys: {self.total_buys}, Initial buy price updated to: {self.initial_buy_price}")
        else:  # SELL
            self.total_sells += 1
            self.filled_sell_prices.append(event.price)
            if len(self.filled_sell_prices) > 20:
                self.filled_sell_prices.pop(0)
            if self.total_buys == self.total_sells:
                self.initial_buy_price = None
                self.ping_pong_active = False
            self.logger().info(f"Sell filled. Total sells: {self.total_sells}, Ping-pong reset: {not self.ping_pong_active}")

    def format_status(self):
        if not self.ready_to_trade:
            return "Market connectors not ready"
        connector = self.connectors[self.exchange]
        mid_price = connector.get_mid_price(self.trading_pair) or Decimal("0")
        current_eth = connector.get_balance("ETH")
        current_usdt = connector.get_balance("USDT")
        portfolio_value = current_usdt + (current_eth * mid_price)
        
        lines = [
            f"MM Status: ETH={current_eth:.4f}, USDT={current_usdt:.2f}, Portfolio Value={portfolio_value:.2f} USDT",
            f"Current Market: Mid Price={mid_price:.2f}, Trend={self.trend_state}",
            f"Initial Buy Price: {self.initial_buy_price if self.initial_buy_price else 'None'}",
            f"Inventory: Buys={self.total_buys}, Sells={self.total_sells}",
            f"Ping-Pong Active: {self.ping_pong_active}"
        ]
        
        active_orders = self.get_active_orders(connector_name=self.exchange)
        if active_orders:
            lines.append(f"Active Orders: {len(active_orders)}")
            for order in active_orders:
                lines.append(f"  {order.trading_pair} {order.is_buy} {order.quantity} @ {order.price:.2f}")
        
        if self.trade_history:
            lines.append(f"Recent trades: {[t.name for t in self.trade_history[-5:]]}")
        
        if self.filled_buy_prices and self.filled_sell_prices:
            avg_buy = sum(self.filled_buy_prices) / len(self.filled_buy_prices)
            avg_sell = sum(self.filled_sell_prices) / len(self.filled_sell_prices)
            pnl_pct = ((avg_sell / avg_buy) - 1) * 100
            lines.append(f"Avg Buy: {avg_buy:.2f}, Avg Sell: {avg_sell:.2f}, PnL: {pnl_pct:.2f}%")
        
        self.logger().info("\n" + "\n".join(lines))
        return "\n".join(lines)
