# Market Making Strategy for Hummingbot

## Overview
This is a market making strategy implemented as a `ScriptStrategyBase` subclass for the Hummingbot algorithmic trading framework. The strategy is designed to provide liquidity on the ETH-USDT trading pair on Binance (paper trading mode) by placing limit orders based on market trends and volatility analysis. It dynamically adjusts bid and ask prices using order book imbalance (OBI), volume-weighted imbalance (VWI), bid-ask spread, and short-term volatility metrics.

## Features
- **Trading Pair**: ETH-USDT
- **Exchange**: Binance (paper trading mode)
- **Order Types**: Primarily maker limit orders, with market orders in extreme price conditions
- **Refresh Rate**: Orders are refreshed every 1 second
- **Amount**: Default trade size of 0.1 ETH per order
- **Markup**: 0.45% adjustment from mid-price as a base
- **Trend Detection**: Identifies uptrends, downtrends, and neutral markets using real-time candle data
- **Inventory Management**: Adjusts orders to maintain a balanced inventory of ETH and USDT
- **Profit Protection**: Ensures sell prices cover fees and provide a minimum profit when possible
- **Extreme Conditions**: Switches to market orders during significant price movements
- **Logging**: Detailed logging of market conditions, order placements, and fills

## Key Components
### Market Data
- Utilizes 1-second candlestick data from Binance with a maximum of 1000 records
- Calculates mid-price dynamically using `PriceType.MidPrice`

### Trend Analysis
- **Order Book Imbalance (OBI)**: Measures the imbalance between bid and ask volumes in the order book
- **Volume Weighted Imbalance (VWI)**: Assesses buying vs. selling pressure over the last 15 candles
- **Bid-Ask Spread**: Monitors the percentage spread to gauge market liquidity
- **Short-Term Volatility**: Uses exponential weighted moving average (EWMA) of log returns to assess price volatility

### Order Placement Logic
- **Downtrend**: Places sell orders if sufficient ETH is available, ensuring profitability over average buy price
- **Uptrend**: Places buy orders if sufficient USDT is available, targeting prices below average sell price
- **Neutral**: Places both buy and sell orders to maintain inventory, with a buffer to avoid overlap
- **Extreme Price Movements**: Executes market orders to capitalize on or protect against rapid price changes

### Risk Management
- Maintains a buffer between max bid and min ask prices to prevent overlapping orders
- Adjusts order prices to account for fees (0.1%) and a minimum profit margin (1%)
- Cancels all active orders before placing new ones to avoid stale orders

## Metrics and Logging
- Tracks buy/sell counts, average buy/sell prices, and portfolio value
- Logs detailed information on market state, order placements, and filled trades
- Provides a formatted status output with current market conditions and active orders

## Dependencies
- Hummingbot core libraries (`hummingbot.core`, `hummingbot.strategy`, etc.)
- Pandas and NumPy for data analysis
- Python standard libraries (`logging`, `decimal`, `typing`)

## Notes
- This script is designed for paper trading on Binance (`binance_paper_trade`). Adjustments are needed for live trading.
- The strategy assumes sufficient initial balances of ETH and USDT in the paper trading account.
- The candle data feed (`CandlesFactory`) starts automatically upon initialization.
