#!/usr/bin/env python3
"""
Stock Drop Alert System
Finds stocks that dropped 5%+ today, analyzes historical 5%+ drops,
and shows 10-day forward performance after each drop.
"""

import os
import sys
import argparse
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from dotenv import load_dotenv
from tabulate import tabulate

from stock_drop_errors import public_error, require_env

# Load environment variables
load_dotenv()

FMP_API_KEY = os.getenv("FMP_API_KEY")


class TeeOutput:
    """Class to write output to both console and file."""
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.file = open(filename, 'w')

    def write(self, message):
        self.terminal.write(message)
        self.file.write(message)

    def flush(self):
        self.terminal.flush()
        self.file.flush()

    def close(self):
        self.file.close()


BASE_URL = "https://financialmodelingprep.com/api/v3"

DROP_THRESHOLD = -0.05  # 5% drop
MIN_MARKET_CAP = 100_000_000  # $100 million
MIN_HISTORICAL_DROPS = 10
YEARS_LOOKBACK = 5
FORWARD_DAYS = 10
MAX_STOCKS_TO_ANALYZE = 30  # Limit to top 30 by market cap
MAX_WORKERS = 20  # Parallel API threads

# Suffixes that indicate warrants, units, rights, etc.
EXCLUDED_SUFFIXES = ("-WT", "-WS", "-UN", "-RT", "-R")
FMP_RETRY_DELAY_SECONDS = 15


def _extract_fmp_error(data):
    """Return an FMP error message from a JSON payload, if present."""
    if isinstance(data, dict):
        for key in ("Error Message", "Error", "error", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def fmp_get_json(url, *, timeout=None, context="FMP request"):
    """Fetch JSON from FMP, retrying after a 15-second pause on FMP errors."""
    while True:
        try:
            response = requests.get(url, timeout=timeout)
            status_code = response.status_code

            try:
                data = response.json()
            except ValueError:
                data = None

            fmp_error = _extract_fmp_error(data)
            if status_code >= 400:
                error_message = f"{status_code} {response.reason}"
                if fmp_error:
                    error_message = f"{error_message}: {fmp_error}"
                print(f"  FMP error during {context}: {error_message}. Waiting 15 seconds before retrying...")
                time.sleep(FMP_RETRY_DELAY_SECONDS)
                continue

            if fmp_error:
                print(f"  FMP error during {context}: {fmp_error}. Waiting 15 seconds before retrying...")
                time.sleep(FMP_RETRY_DELAY_SECONDS)
                continue

            if data is None:
                print(f"  FMP error during {context}: invalid JSON response. Waiting 15 seconds before retrying...")
                time.sleep(FMP_RETRY_DELAY_SECONDS)
                continue

            return data
        except requests.RequestException as exc:
            print(f"  FMP error during {context}: {public_error(exc)}. Waiting 15 seconds before retrying...")
            time.sleep(FMP_RETRY_DELAY_SECONDS)


def get_bulk_quotes():
    """Get quotes for all stocks on NYSE and NASDAQ."""
    nyse_url = f"{BASE_URL}/quotes/nyse?apikey={FMP_API_KEY}"
    nasdaq_url = f"{BASE_URL}/quotes/nasdaq?apikey={FMP_API_KEY}"

    nyse = fmp_get_json(nyse_url, context="fetching NYSE quotes")
    nasdaq = fmp_get_json(nasdaq_url, context="fetching NASDAQ quotes")
    return nyse + nasdaq


def get_active_stocks():
    """Get all actively trading stocks on NYSE/NASDAQ with market cap filter."""
    url = (f"{BASE_URL}/stock-screener?isActivelyTrading=true"
           f"&exchange=NYSE,NASDAQ&marketCapMoreThan={MIN_MARKET_CAP}"
           f"&limit=10000&apikey={FMP_API_KEY}")
    return fmp_get_json(url, context="fetching active stocks")


def get_historical_prices(symbol, years=5):
    """Get historical daily prices for a stock."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=years * 365)

    url = (f"{BASE_URL}/historical-price-full/{symbol}"
           f"?from={start_date.strftime('%Y-%m-%d')}"
           f"&to={end_date.strftime('%Y-%m-%d')}&apikey={FMP_API_KEY}")
    data = fmp_get_json(url, timeout=15, context=f"fetching historical prices for {symbol}")

    if "historical" in data:
        return data["historical"]
    return []


def find_historical_drops(historical_data, threshold=DROP_THRESHOLD):
    """Find all days where the stock dropped by threshold or more."""
    drops = []

    for i, day in enumerate(historical_data):
        if i + 1 < len(historical_data):
            prev_day = historical_data[i + 1]
            if prev_day["close"] > 0:
                daily_change = (day["close"] - prev_day["close"]) / prev_day["close"]
                if daily_change <= threshold:
                    drops.append({
                        "date": day["date"],
                        "close": day["close"],
                        "prev_close": prev_day["close"],
                        "change_pct": daily_change * 100,
                        "index": i
                    })

    return drops


def get_forward_performance(historical_data, drop_index, days=FORWARD_DAYS):
    """Get the performance for the next N trading days after a drop."""
    performances = []
    drop_close = historical_data[drop_index]["close"]

    for day_offset in range(1, days + 1):
        forward_index = drop_index - day_offset
        if forward_index >= 0:
            forward_day = historical_data[forward_index]
            change_from_drop = ((forward_day["close"] - drop_close) / drop_close) * 100
            performances.append({
                "day": day_offset,
                "date": forward_day["date"],
                "close": forward_day["close"],
                "change_from_drop_pct": change_from_drop
            })
        else:
            break

    return performances


def analyze_stock(symbol, name=None):
    """Analyze a stock for historical 5%+ drops and forward performance."""
    # Get historical prices
    historical = get_historical_prices(symbol, YEARS_LOOKBACK)
    if not historical:
        return None, f"No historical data available for {symbol}"

    # Find all 5%+ drops
    drops = find_historical_drops(historical, DROP_THRESHOLD)

    if len(drops) < MIN_HISTORICAL_DROPS:
        return None, (f"Only found {len(drops)} drops of 5%+ in the past "
                      f"{YEARS_LOOKBACK} years. Need at least {MIN_HISTORICAL_DROPS}. Skipping...")

    results = []
    for drop in drops[:MIN_HISTORICAL_DROPS]:
        forward_perf = get_forward_performance(historical, drop["index"], FORWARD_DAYS)
        if len(forward_perf) == FORWARD_DAYS:
            results.append({
                "drop_date": drop["date"],
                "drop_pct": drop["change_pct"],
                "drop_close": drop["close"],
                "forward_performance": forward_perf
            })

    if not results:
        return None, "No complete 10-day forward data available for drops."

    # Calculate aggregate statistics
    avg_performance_by_day = {}
    for day in range(1, FORWARD_DAYS + 1):
        day_performances = [
            perf["change_from_drop_pct"]
            for result in results
            for perf in result["forward_performance"]
            if perf["day"] == day
        ]
        if day_performances:
            avg_performance_by_day[day] = sum(day_performances) / len(day_performances)

    positive_after_10_days = sum(1 for r in results
                                if r["forward_performance"][-1]["change_from_drop_pct"] > 0)
    win_rate = (positive_after_10_days / len(results)) * 100

    return {
        "symbol": symbol,
        "name": name,
        "total_drops": len(drops),
        "analyzed_drops": len(results),
        "avg_10_day_return": avg_performance_by_day.get(10, 0),
        "win_rate": win_rate,
        "results": results,
        "avg_performance_by_day": avg_performance_by_day,
    }, None


def print_analysis(symbol, name, analysis):
    """Print the full analysis for a single stock."""
    print(f"\n{'='*80}")
    print(f"Analyzing {symbol}" + (f" ({name})" if name else ""))
    print('='*80)

    print(f"\n  Found {analysis['total_drops']} instances of 5%+ drops in the past {YEARS_LOOKBACK} years.")
    print(f"  Showing analysis for the {min(analysis['total_drops'], MIN_HISTORICAL_DROPS)} most recent drops:\n")

    for i, result in enumerate(analysis["results"], 1):
        print(f"\n  {symbol} Drop #{i}: {result['drop_date']} ({result['drop_pct']:.2f}% @ ${result['drop_close']:.2f})")
        print("  " + "-" * 60)

        table_data = []
        for perf in result["forward_performance"]:
            table_data.append([
                f"Day {perf['day']}",
                perf["date"],
                f"${perf['close']:.2f}",
                f"{perf['change_from_drop_pct']:+.2f}%"
            ])

        print(tabulate(table_data,
                      headers=["Trading Day", "Date", "Close Price", "Change from Drop"],
                      tablefmt="simple",
                      numalign="right"))

    print(f"\n\n  {'='*60}")
    print(f"  {symbol} AGGREGATE STATISTICS (Average across all analyzed drops)")
    print(f"  {'='*60}")

    agg_table = []
    for day, avg_change in analysis["avg_performance_by_day"].items():
        agg_table.append([f"Day {day}", f"{avg_change:+.2f}%"])

    print(tabulate(agg_table,
                  headers=["Trading Day After Drop", "Avg Change from Drop"],
                  tablefmt="simple",
                  numalign="right"))

    positive_after_10_days = sum(1 for r in analysis["results"]
                                if r["forward_performance"][-1]["change_from_drop_pct"] > 0)
    print(f"\n  Win Rate (positive after 10 days): {positive_after_10_days}/{analysis['analyzed_drops']} ({analysis['win_rate']:.1f}%)")


def check_stock_drop_on_date(symbol, target_date, start_date, end_date):
    """Check if a stock dropped 5%+ on the target date. Returns dict or None."""
    try:
        url = (f"{BASE_URL}/historical-price-full/{symbol}"
               f"?from={start_date}&to={end_date}&apikey={FMP_API_KEY}")
        data = fmp_get_json(
            url,
            timeout=10,
            context=f"checking {symbol} for a drop on {target_date}",
        )

        if "historical" not in data or len(data["historical"]) < 2:
            return None

        for j, day in enumerate(data["historical"]):
            if day["date"] == target_date and j + 1 < len(data["historical"]):
                prev_day = data["historical"][j + 1]
                if prev_day["close"] > 0:
                    change_pct = ((day["close"] - prev_day["close"]) / prev_day["close"]) * 100
                    if change_pct <= -5.0:
                        return {
                            "symbol": symbol,
                            "change_pct": change_pct,
                            "price": day["close"],
                        }
                break
    except (KeyError, TypeError, ValueError) as exc:
        print(f"  Could not check {symbol} on {target_date}: {public_error(exc)}")
    return None


def get_historical_drops_for_date(target_date):
    """Find stocks that dropped 5%+ on a specific historical date (parallelized)."""
    print(f"  Fetching actively trading stocks (market cap >= ${MIN_MARKET_CAP / 1e6:.0f}M)...")
    all_stocks = get_active_stocks()

    candidates = []
    for stock in all_stocks:
        symbol = stock.get("symbol", "")
        if not symbol:
            continue
        if any(symbol.upper().endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
            continue
        candidates.append(stock)

    print(f"  Checking {len(candidates)} stocks for drops on {target_date}...")

    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    start_date = (target_dt - timedelta(days=5)).strftime("%Y-%m-%d")
    end_date = (target_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    qualified = []
    checked = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(check_stock_drop_on_date, s["symbol"], target_date, start_date, end_date): s
            for s in candidates
        }
        for future in as_completed(futures):
            checked += 1
            stock_info = futures[future]
            result = future.result()
            if result:
                result["name"] = stock_info.get("companyName", "")
                result["market_cap"] = stock_info.get("marketCap") or 0
                qualified.append(result)
                print(f"    ✓ Found: {result['symbol']} ({result['change_pct']:.2f}%)")
            if checked % 200 == 0:
                print(f"    Processed {checked}/{len(candidates)} stocks...")

    print(f"    Found {len(qualified)} stocks matching criteria")
    return qualified


def main():
    """Main function to run the stock drop alert."""
    parser = argparse.ArgumentParser(description="Stock Drop Alert - Find and analyze 5%+ stock drops")
    parser.add_argument("-d", "--date", type=str,
                        help="Specific date to analyze (YYYY-MM-DD format). If not provided, uses today's data.")
    args = parser.parse_args()
    require_env('FMP_API_KEY')

    if args.date:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
            target_date = args.date
            is_historical = True
        except ValueError:
            print(f"Error: Invalid date format '{args.date}'. Please use YYYY-MM-DD format.")
            sys.exit(1)
    else:
        target_date = datetime.now().strftime("%Y-%m-%d")
        is_historical = False

    # Set up output file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, "Stock_Drop_Alert")
    os.makedirs(output_dir, exist_ok=True)
    output_filename = os.path.join(output_dir, f"Stock_Drop_Alert_{target_date}.txt")

    tee = TeeOutput(output_filename)
    sys.stdout = tee

    try:
        _run_analysis(target_date, is_historical, output_filename)
    finally:
        sys.stdout = tee.terminal
        tee.close()


def _run_analysis(target_date, is_historical, output_filename):
    """Run the main drop analysis."""
    print("\n" + "="*80)
    print("STOCK DROP ALERT - 5%+ Drop Analysis")
    print(f"Date: {target_date}" + (" (historical)" if is_historical else f" {datetime.now().strftime('%H:%M:%S')}"))
    print(f"Criteria: Drop >= 5%, Market Cap >= ${MIN_MARKET_CAP/1e6:.0f}M")
    print(f"Analyzing top {MAX_STOCKS_TO_ANALYZE} stocks by market cap")
    print("="*80)

    print("\nFetching stock data...")

    if is_historical:
        try:
            qualified_stocks = get_historical_drops_for_date(target_date)
        except Exception as e:
            print(f"  Error fetching historical data: {e}")
            sys.exit(1)
    else:
        try:
            print("  Fetching bulk quotes from NYSE and NASDAQ...")
            bulk_quotes = get_bulk_quotes()

            qualified_stocks = []
            for stock in bulk_quotes:
                symbol = stock.get("symbol", "")
                change_pct = stock.get("changesPercentage") or 0
                market_cap = stock.get("marketCap") or 0
                price = stock.get("price") or 0
                name = stock.get("name", "")

                if any(symbol.upper().endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
                    continue

                if symbol and change_pct <= -5.0 and market_cap >= MIN_MARKET_CAP:
                    qualified_stocks.append({
                        "symbol": symbol,
                        "name": name,
                        "change_pct": change_pct,
                        "market_cap": market_cap,
                        "price": price
                    })

            print(f"    Found {len(qualified_stocks)} stocks matching criteria")
        except Exception as e:
            print(f"  Error fetching bulk quotes: {e}")
            sys.exit(1)

    if not qualified_stocks:
        print("\nNo stocks found matching criteria (5%+ drop).")
        print("This is normal on days without significant market drops.")
        return

    # Sort by market cap (highest first) and take top N
    qualified_stocks.sort(key=lambda x: x["market_cap"], reverse=True)
    qualified_stocks = qualified_stocks[:MAX_STOCKS_TO_ANALYZE]

    print(f"\nTop {len(qualified_stocks)} stocks by market cap:")
    for stock in qualified_stocks:
        cap = stock['market_cap']
        if cap >= 1e12:
            cap_str = f"${cap/1e12:.1f}T"
        elif cap >= 1e9:
            cap_str = f"${cap/1e9:.1f}B"
        elif cap >= 1e6:
            cap_str = f"${cap/1e6:.0f}M"
        else:
            cap_str = f"${cap:,.0f}"
        print(f"  ✓ {stock['symbol']}: {stock['change_pct']:.2f}% drop @ ${stock['price']:.2f}, Market Cap: {cap_str}")

    print(f"\nFound {len(qualified_stocks)} stocks matching criteria.")

    # Analyze each stock in parallel (fetch historical prices concurrently)
    print(f"\nAnalyzing historical drops for {len(qualified_stocks)} stocks (parallel)...")

    all_analyses = []
    analysis_results = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(analyze_stock, stock["symbol"], stock["name"]): stock
            for stock in qualified_stocks
        }
        for future in as_completed(futures):
            stock = futures[future]
            try:
                analysis, error = future.result()
                analysis_results[stock["symbol"]] = (analysis, error, stock)
            except Exception as e:
                analysis_results[stock["symbol"]] = (None, str(e), stock)

    # Print results in original order
    for stock in qualified_stocks:
        analysis, error, stock_info = analysis_results[stock["symbol"]]
        if analysis:
            print_analysis(stock["symbol"], stock["name"], analysis)
            analysis["today_drop"] = stock_info["change_pct"]
            analysis["today_price"] = stock_info["price"]
            analysis["market_cap"] = stock_info["market_cap"]
            all_analyses.append(analysis)
        else:
            print(f"\n{'='*80}")
            print(f"Analyzing {stock['symbol']}" + (f" ({stock['name']})" if stock.get('name') else ""))
            print('='*80)
            print(f"  {error}")

    # Final summary
    if all_analyses:
        print("\n\n" + "="*80)
        print("FINAL SUMMARY")
        print("="*80)

        summary_table = []
        for analysis in all_analyses:
            cap = analysis['market_cap']
            if cap >= 1e12:
                cap_str = f"${cap/1e12:.1f}T"
            elif cap >= 1e9:
                cap_str = f"${cap/1e9:.1f}B"
            elif cap >= 1e6:
                cap_str = f"${cap/1e6:.0f}M"
            else:
                cap_str = f"${cap:,.0f}"
            summary_table.append([
                analysis["symbol"],
                f"{analysis['today_drop']:.2f}%",
                f"${analysis['today_price']:.2f}",
                cap_str,
                analysis["total_drops"],
                f"{analysis['avg_10_day_return']:+.2f}%",
                f"{analysis['win_rate']:.1f}%"
            ])

        print(tabulate(summary_table,
                      headers=["Symbol", "Today's Drop", "Close Price", "Market Cap", "5Y Drops", "Avg 10D Return", "Win Rate"],
                      tablefmt="grid",
                      numalign="right"))

    print("\n" + "="*80)
    print("Analysis Complete")
    print("="*80)
    print(f"\nOutput saved to: {output_filename}")
    print()


if __name__ == "__main__":
    main()
