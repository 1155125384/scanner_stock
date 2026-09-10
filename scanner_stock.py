import hashlib
import hmac
import base64
import json
import os
import uuid
import urllib.parse
from datetime import datetime, timezone
from webullsdkcore.client import ApiClient
from webullsdktrade.api import API
from webullsdkcore.common.region import Region
from webullsdkmdata.common.category import Category

import requests
import yfinance as yf
import pandas as pd
import time
from tqdm import tqdm
import sys
from io import StringIO
import numpy as np
import pandas as pd
import logging

# =============================================================================
# Configuration
# =============================================================================
# Edit values in this section to change credentials, API behavior, scan rules,
# progress output, or the technical scoring model. Account credentials are used
# only by the trading SDK to read holdings. Sandbox credentials are used for
# token creation and market-data requests.
CONFIG = {
    "webull": {
        "sandbox_app_key": "42bd186fb65ea76de309d69cf12f024e",
        "sandbox_app_secret": "29feb64b59d6b1b6b2d2aa8cea8a1b8d",
        # "sandbox_app_key": os.getenv("SANDBOX_KEY"),
        # "sandbox_app_secret": os.getenv("SANDBOX_SECRET"),
        "sandbox_host": "api.sandbox.webull.hk",
        "account_app_key": os.getenv("APP_KEY"),
        "account_app_secret": os.getenv("APP_SECRET"),
        "region": Region.HK.value,
        "token_endpoint": "/auth/tokens/create",
        "ratings_endpoint": "/market-data/fundamentals/analysis/ratings/get",
        "ratings_category": "US_STOCK",
        "api_version": "v2",
        "token_api_version": "v2",
        "signature_algorithm": "HMAC-SHA1",
        "signature_version": "1.0",
        "account_page_size": 100,
    },
    "progress": {
        "update_interval_seconds": 1.0,
        "poll_interval_seconds": 0.1,
        "bar_length": 30,
        "line_width": 160,
    },
    "ratings": {
        "request_delay_seconds": 0.1,
        "retry_wait_seconds": 10,
        "max_retries": 5,
        "retry_backoff_base_seconds": 2,
        "retry_backoff_max_seconds": 60,
        "rate_limit_status_code": 429,
        "freshness_days": 3,
        "weights": {
            "strong_buy": 100.0, "buy": 75.0, "hold": 50.0,
            "under_perform": 25.0, "sell": 0.0,
        },
    },
    "screen": {
        "timeframe": "hourly",
        "holdings_metadata_delay_seconds": 0.3,
        "auto_adjust_prices": True,
        "request_delay_seconds": 1.5,
        "max_retries": 3,
        "retry_backoff_seconds": 5,
        "analyst_weight": 0.6,
        "technical_weight": 0.4,
        "annualized_trading_days": 252,
        "annualized_bars_per_day": 6.5,
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "rsi": {"ideal": 55, "overbought": 70, "oversold": 30, "distance": 45},
        "score_ranges": {
            "trend": (-10, 10), "volume": (0.5, 2.5),
            "price_momentum": (-40, 40), "relative_strength": (-20, 20),
            "risk_volatility": (15, 80), "risk_drawdown": (0, 50),
        },
        "benchmarks": {"SPY": "SPY", "DJI": "^DJI", "SPX": "^GSPC", "IXIC": "^IXIC"},
        "timeframes": {
            "hourly": {
                "interval": "1h", "period": "730d", "ma_short": 50,
                "ma_long": 200, "rsi_period": 14, "vol_recent_bars": 7,
                "vol_baseline_bars": 130, "mom_windows": {"1D%": 7, "1W%": 33, "1M%": 140},
                "bench_bars": 33,
            },
        },
        "score_weights": {
            "trend": 20, "momentum": 10, "price_momentum": 15,
            "volume": 20, "relative_strength": 20, "risk_adjustment": 15,
        },
        "rating_baseline_weight": 10,
        "rating_baseline_score": 50.0,
        "rating_levels": [75, 60, 50, 40],
        "rating_thresholds": [85.0, 83.0, 80.0, 78.0, 75.0],
        "minimum_final_ratings": 250,
    },
    "output": {"stock_csv": "stock_scanner.csv", "display_width": 220},
    "index_sources": [
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "S&P 500"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "S&P MidCap 400"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "S&P SmallCap 600"),
    ],
}

WEBULL_CONFIG = CONFIG["webull"]
PROGRESS_CONFIG = CONFIG["progress"]
RATINGS_CONFIG = CONFIG["ratings"]
SCREEN_CONFIG = CONFIG["screen"]
OUTPUT_CONFIG = CONFIG["output"]

SANDBOX_APP_KEY = WEBULL_CONFIG["sandbox_app_key"]
SANDBOX_APP_SECRET = WEBULL_CONFIG["sandbox_app_secret"]
SANDBOX_HOST = WEBULL_CONFIG["sandbox_host"]
ACCOUNT_APP_KEY = WEBULL_CONFIG["account_app_key"]
ACCOUNT_APP_SECRET = WEBULL_CONFIG["account_app_secret"]
SANDBOX_BASE_URL = f"https://{SANDBOX_HOST}"


def generate_signature(path, query_params, body_string, app_key, app_secret, host, timestamp, nonce):
    signing_headers = {
        "x-app-key": app_key,
        "x-timestamp": timestamp,
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-version": "1.0",
        "x-signature-nonce": nonce,
        "host": host,
    }

    all_params = {}
    all_params.update(query_params)
    all_params.update(signing_headers)
    str1 = "&".join(f"{k}={all_params[k]}" for k in sorted(all_params.keys()))
    if body_string:
        str2 = hashlib.md5(body_string.encode("utf-8")).hexdigest().upper()
        str3 = f"{path}&{str1}&{str2}"
    else:
        str3 = f"{path}&{str1}"
    encoded_string = urllib.parse.quote(str3, safe="")

    signing_key = f"{app_secret}&"
    signature = base64.b64encode(
        hmac.new(signing_key.encode("utf-8"), encoded_string.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")

    return signature


def call_api(
    method,
    path,
    query_params=None,
    body=None,
    access_token=None,
    app_key=SANDBOX_APP_KEY,
    app_secret=SANDBOX_APP_SECRET,
    host=SANDBOX_HOST,
    base_url=SANDBOX_BASE_URL,
    api_version=None,
):
    """Sign and send a Webull REST request with the selected credentials."""
    query_params = query_params or {}
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = uuid.uuid4().hex

    body_string = json.dumps(body, separators=(",", ":")) if body else None

    signature = generate_signature(
        path, query_params, body_string,
        app_key, app_secret, host, timestamp, nonce,
    )

    headers = {
        "Accept": "application/json",
        "x-app-key": app_key,
        "x-timestamp": timestamp,
        "x-signature": signature,
        "x-signature-algorithm": WEBULL_CONFIG["signature_algorithm"],
        "x-signature-version": WEBULL_CONFIG["signature_version"],
        "x-signature-nonce": nonce,
        "x-version": api_version or WEBULL_CONFIG["api_version"],
    }
    if access_token is not None:
        if not access_token:
            raise ValueError("Webull access token is empty; create a token before this request.")
        headers["x-access-token"] = access_token

    url = f"{base_url}{path}"

    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, params=query_params)
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(url, headers=headers, data=body_string)

    return resp


def create_access_token():
    """Create a fresh short-lived token with sandbox credentials on every run."""
    if not SANDBOX_APP_KEY or not SANDBOX_APP_SECRET:
        raise RuntimeError("SANDBOX_KEY and SANDBOX_SECRET must be configured.")

    response = call_api(
        "POST",
        WEBULL_CONFIG["token_endpoint"],
        body={},
        app_key=SANDBOX_APP_KEY,
        app_secret=SANDBOX_APP_SECRET,
        host=SANDBOX_HOST,
        base_url=SANDBOX_BASE_URL,
        api_version=WEBULL_CONFIG["token_api_version"],
    )
    try:
        token_data = response.json()
    except ValueError:
        token_data = None

    if not response.ok or not isinstance(token_data, dict):
        raise RuntimeError(
            f"Unable to create Webull access token: HTTP {response.status_code} "
            f"{response.text[:500]}"
        )

    token = (
        token_data.get("access_token")
        or token_data.get("accessToken")
        or token_data.get("token")
    )
    if not token:
        raise RuntimeError(f"Token field not found in response: {token_data}")
    return token


# A new token is deliberately generated for every execution because it expires quickly.
ACCESS_TOKEN = create_access_token()
print("Access token created successfully.")


# The account app credentials are isolated to the SDK call that reads holdings.
if not ACCOUNT_APP_KEY or not ACCOUNT_APP_SECRET:
    raise RuntimeError("APP_KEY and APP_SECRET must be configured for account holdings.")
api_client = ApiClient(ACCOUNT_APP_KEY, ACCOUNT_APP_SECRET, WEBULL_CONFIG["region"])
api = API(api_client)

res_acct = api.account.get_app_subscriptions()
account_id = None

result = res_acct.json()
account_id = result[0]['account_id']

res_stock = api.account.get_account_position(
    account_id, page_size=WEBULL_CONFIG["account_page_size"]
)
account_position = res_stock.json()

holdings = account_position.get("holdings", [])
current_holdings_list = [item['symbol'] for item in holdings]

print("My Current Holdings:", current_holdings_list)

holdings = current_holdings_list

results = []

for ticker in tqdm(
    holdings,
    desc="Fetching ticker data",
    unit="ticker",
    mininterval=PROGRESS_CONFIG["update_interval_seconds"],
    miniters=1,
    leave=False,
):
    try:
        info = yf.Ticker(ticker).info
        quote_type = info.get('quoteType', 'UNKNOWN')
        long_name = info.get('longName', info.get('shortName', ''))
        results.append({'Ticker': ticker, 'Type': quote_type, 'Name': long_name})
    except Exception as e:
        results.append({'Ticker': ticker, 'Type': 'ERROR', 'Name': str(e)})
    time.sleep(SCREEN_CONFIG["holdings_metadata_delay_seconds"])

df = pd.DataFrame(results)

stocks = df[df['Type'] == 'EQUITY']
etfs = df[df['Type'] == 'ETF']
other = df[~df['Type'].isin(['EQUITY', 'ETF'])]

current_stock_holdings = stocks['Ticker'].tolist()
print(current_stock_holdings)


def fetch_sp_index(url: str, index_label: str) -> pd.DataFrame:
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
          " Safari/537.36"
      )
  }
  response = requests.get(url, headers=headers)
  tables = pd.read_html(StringIO(response.text))
  df = tables[0].copy()

  symbol_col = next(
      c for c in df.columns if "Symbol" in str(c) or "Ticker" in str(c)
  )
  security_col = next(
      c for c in df.columns if "Security" in str(c) or "Company" in str(c)
  )
  sector_col = next((c for c in df.columns if "Sector" in str(c)), None)
  sub_industry_col = next(
      (c for c in df.columns if "Sub-Industry" in str(c)), None
  )

  rename_map = {symbol_col: "Symbol", security_col: "Security"}
  if sector_col:
    rename_map[sector_col] = "GICS Sector"
  if sub_industry_col:
    rename_map[sub_industry_col] = "GICS Sub-Industry"

  df = df.rename(columns=rename_map)

  df["Symbol"] = (
      df["Symbol"]
      .astype(str)
      .str.replace(".", " ", regex=False)
      .str.replace("-", " ", regex=False)
  )

  core_cols = [
      c
      for c in ["Symbol", "Security", "GICS Sector", "GICS Sub-Industry"]
      if c in df.columns
  ]
  df = df[core_cols].copy()
  df["Index"] = index_label

  return df

sp_sources = [
    *CONFIG["index_sources"],
]

sp_composite_df = pd.concat(
    [fetch_sp_index(url, label) for url, label in sp_sources], ignore_index=True
)

tickers = sp_composite_df["Symbol"].tolist()
space_tickers = [symbol for symbol in tickers if " " in symbol]

tickers = list(dict.fromkeys(list(tickers) + current_stock_holdings))

rating_rows = []
rating_errors = []
request_delay_seconds = RATINGS_CONFIG["request_delay_seconds"]
retry_wait_seconds = RATINGS_CONFIG["retry_wait_seconds"]
max_retries = RATINGS_CONFIG["max_retries"]
total_symbols = len(tickers)
started_at = time.time()
last_progress_update = 0.0


def show_progress(completed, current_symbol, state="requesting"):
    global last_progress_update
    now = time.time()
    if (
        completed != total_symbols
        and now - last_progress_update < PROGRESS_CONFIG["update_interval_seconds"]
    ):
        return
    last_progress_update = now
    elapsed = time.time() - started_at
    rate = completed / elapsed if elapsed > 0 and completed else 0
    remaining = (total_symbols - completed) / rate if rate > 0 else 0
    bar_length = PROGRESS_CONFIG["bar_length"]
    filled = int(bar_length * completed / total_symbols) if total_symbols else 0
    progress_bar = "#" * filled + "-" * (bar_length - filled)
    eta = f"ETA {remaining / 60:.1f} min" if rate else "ETA calculating"
    message = (
        f"\r[{progress_bar}] {completed:>3}/{total_symbols} "
        f"({completed / total_symbols:.1%}) | {state:<18} | "
        f"{current_symbol:<6} | OK {len(rating_rows):>3} | "
        f"Failed {len(rating_errors):>3} | {eta}"
    )
    line_width = PROGRESS_CONFIG["line_width"]
    sys.stdout.write(message[:line_width].ljust(line_width))
    sys.stdout.flush()


def wait_with_progress(seconds, completed, symbol, reason):
    """Wait without flooding the terminal; progress is refreshed at most once per second."""
    end_time = time.time() + seconds
    while True:
        seconds_left = max(0, int(end_time - time.time() + 0.999))
        show_progress(completed, symbol, f"{reason}, wait {seconds_left}s")
        if seconds_left == 0:
            break
        time.sleep(min(PROGRESS_CONFIG["poll_interval_seconds"], seconds_left))


print(f"Starting ratings download for {total_symbols} symbols at {datetime.now():%H:%M:%S}")
show_progress(0, "-", "starting")

for completed, symbol in enumerate(tickers, start=1):
    show_progress(completed - 1, symbol, "requesting")
    for attempt in range(max_retries + 1):
        try:
            rating_response = call_api(
                "GET",
                WEBULL_CONFIG["ratings_endpoint"],
                query_params={
                    "symbol": symbol,
                    "category": WEBULL_CONFIG["ratings_category"],
                },
                access_token=ACCESS_TOKEN,
            )

            if rating_response.status_code == RATINGS_CONFIG["rate_limit_status_code"]:
                if attempt == max_retries:
                    raise requests.HTTPError("Rate limit remained active after retries")

                retry_after = rating_response.headers.get("Retry-After")
                try:
                    wait_seconds = max(retry_wait_seconds, float(retry_after)) if retry_after else retry_wait_seconds
                except ValueError:
                    wait_seconds = retry_wait_seconds
                wait_with_progress(wait_seconds, completed - 1, symbol, "rate limited")
                continue

            rating_response.raise_for_status()
            rating = rating_response.json()
            rating_rows.append({
                "symbol": symbol,
                "number": rating.get("number", 0),
                "strong_buy": rating.get("strong_buy", 0),
                "buy": rating.get("buy", 0),
                "hold": rating.get("hold", 0),
                "sell": rating.get("sell", 0),
                "under_perform": rating.get("under_perform", 0),
                "effective_start_date": rating.get("effective_start_date"),
            })
            break
        except (requests.RequestException, ValueError, TypeError) as error:
            if attempt == max_retries:
                rating_errors.append({"symbol": symbol, "error": str(error)})
                break
            wait_with_progress(
                min(
                    RATINGS_CONFIG["retry_backoff_max_seconds"],
                    2 ** attempt * RATINGS_CONFIG["retry_backoff_base_seconds"],
                ),
                completed - 1,
                symbol,
                "retrying",
            )

    wait_with_progress(request_delay_seconds, completed, symbol, "throttling")
    show_progress(completed, symbol, "completed")

sys.stdout.write("\n")
ratings_df = pd.DataFrame(rating_rows)
rating_columns = [
    "number",
    "strong_buy",
    "buy",
    "hold",
    "sell",
    "under_perform",
]
for column in rating_columns:
    ratings_df[column] = pd.to_numeric(ratings_df[column], errors="coerce").fillna(0).astype(int)

ratings_df = ratings_df.sort_values(
    by=["strong_buy", "buy", "hold", "sell", "under_perform", "number"],
    ascending=[False, False, False, True, True, False],
    ignore_index=True,
)

print(f"Finished at {datetime.now():%H:%M:%S}")
print(f"Ratings received: {len(ratings_df)} / {total_symbols}")
if rating_errors:
    print(f"Ratings failed: {len(rating_errors)}")
    print(pd.DataFrame(rating_errors))

print(ratings_df)

ratings_df["effective_start_date"] = pd.to_datetime(
    ratings_df["effective_start_date"], utc=True
)

cutoff_time = pd.Timestamp.now(tz="UTC") - pd.Timedelta(
    days=RATINGS_CONFIG["freshness_days"]
)

ratings_df_filtered = ratings_df[
    (ratings_df["effective_start_date"] >= cutoff_time) |
    (ratings_df["symbol"].isin(current_stock_holdings))
].reset_index(drop=True)

weights = RATINGS_CONFIG["weights"]
m = SCREEN_CONFIG["rating_baseline_weight"]
C = SCREEN_CONFIG["rating_baseline_score"]

ratings_df_filtered["raw_score"] = (
    sum(ratings_df_filtered[col] * weight for col, weight in weights.items())
    / ratings_df_filtered["number"]
)

ratings_df_filtered["total_mark"] = (
    (ratings_df_filtered["number"] * ratings_df_filtered["raw_score"] + m * C)
    / (ratings_df_filtered["number"] + m)
).round(2)

ratings_df_filtered = ratings_df_filtered.sort_values(
    by=["total_mark", "raw_score"], 
    ascending=[False, False]
).reset_index(drop=True)

thresholds = SCREEN_CONFIG["rating_thresholds"]

held_mask = ratings_df_filtered["symbol"].isin(current_stock_holdings)
held_df = ratings_df_filtered[held_mask]

for threshold in thresholds:
    above_threshold_df = ratings_df_filtered[ratings_df_filtered["total_mark"] >= threshold]
    final_ratings_df = pd.concat([above_threshold_df, held_df]).drop_duplicates().reset_index(drop=True)
    
    if len(final_ratings_df) >= SCREEN_CONFIG["minimum_final_ratings"]:
        break

print(
    final_ratings_df[
        ["symbol", "number", "raw_score", "total_mark", "effective_start_date"]
    ]
)

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# ----------------------------------------------------------------------------
# 1. Prepare the technical screen from the analyst-rated symbols.
# ----------------------------------------------------------------------------
TICKERS = final_ratings_df["symbol"].tolist()
RATING_SCORES = final_ratings_df.set_index("symbol")[
    ["raw_score", "total_mark", "number", "strong_buy", "buy", "hold", "sell", "under_perform"]
].to_dict("index")

BENCHMARKS = SCREEN_CONFIG["benchmarks"]
TIMEFRAME = SCREEN_CONFIG["timeframe"]
REQUEST_DELAY_SEC = SCREEN_CONFIG["request_delay_seconds"]
MAX_RETRIES = SCREEN_CONFIG["max_retries"]
RETRY_BACKOFF_SEC = SCREEN_CONFIG["retry_backoff_seconds"]
TIMEFRAME_CONFIG = SCREEN_CONFIG["timeframes"]
SCORE_WEIGHTS = SCREEN_CONFIG["score_weights"]
assert sum(SCORE_WEIGHTS.values()) == 100


# ----------------------------------------------------------------------------
# 2. Indicator helpers used by the technical scoring model.
# ----------------------------------------------------------------------------
def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(series, fast=None, slow=None, signal=None):
    settings = SCREEN_CONFIG["macd"]
    fast = fast or settings["fast"]
    slow = slow or settings["slow"]
    signal = signal or settings["signal"]
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def pct_change_over(close, bars):
    if len(close) > bars:
        return float((close.iloc[-1] / close.iloc[-bars] - 1) * 100)
    return np.nan


def max_drawdown(close):
    running_max = close.cummax()
    drawdown = (close / running_max - 1) * 100
    return float(drawdown.min())


def clip_scale(value, lo, hi, out_max):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0.0
    v = min(max(value, lo), hi)
    return (v - lo) / (hi - lo) * out_max


def inverse_clip_scale(value, lo, hi, out_max):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0.0
    v = min(max(value, lo), hi)
    return (hi - v) / (hi - lo) * out_max


# ----------------------------------------------------------------------------
# 3. Download market history with retries for transient Yahoo Finance errors.
# ----------------------------------------------------------------------------
def safe_download(ticker, interval, period):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            hist = yf.Ticker(ticker).history(
                period=period,
                interval=interval,
                auto_adjust=SCREEN_CONFIG["auto_adjust_prices"],
            )
            if not hist.empty and "Close" in hist.columns:
                return hist
            last_err = "empty response or missing Close column"
        except Exception as error:
            last_err = error
        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_SEC * attempt
            tqdm.write(
                f"    -> {ticker}: attempt {attempt} failed ({last_err}); "
                f"retrying in {wait}s"
            )
            time.sleep(wait)
    tqdm.write(f"    -> {ticker}: giving up after {MAX_RETRIES} attempts ({last_err})")
    return pd.DataFrame()


# ----------------------------------------------------------------------------
# 4. Calculate indicators, scores, and human-readable signal flags per ticker.
# ----------------------------------------------------------------------------
def analyze_ticker(ticker, cfg, bench_rets, rating_scores):
    hist = safe_download(ticker, cfg["interval"], cfg["period"])
    if hist.empty or len(hist) < max(cfg["ma_short"], 30):
        return None

    close = hist["Close"]
    volume = hist["Volume"]
    price = float(close.iloc[-1])

    ma_short = close.rolling(cfg["ma_short"]).mean().iloc[-1]
    ma_long = (close.rolling(cfg["ma_long"]).mean().iloc[-1]
               if len(close) >= cfg["ma_long"] else np.nan)

    r = float(rsi(close, cfg["rsi_period"]).iloc[-1])
    macd_line, signal_line = macd(close)
    macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])

    vol_recent_bars = cfg["vol_recent_bars"]
    vol_baseline_bars = cfg["vol_baseline_bars"]
    if len(volume) >= vol_baseline_bars:
        vol_recent = volume.iloc[-vol_recent_bars:].mean()
        vol_baseline = volume.iloc[-vol_baseline_bars:-vol_recent_bars].mean()
        vol_ratio = float(vol_recent / vol_baseline) if vol_baseline else np.nan
    else:
        vol_ratio = np.nan

    mom_returns = {label: pct_change_over(close, bars)
                   for label, bars in cfg["mom_windows"].items()}

    daily_ret = close.pct_change().dropna()
    ann_vol = float(
        daily_ret.std()
        * np.sqrt(
            SCREEN_CONFIG["annualized_trading_days"]
            * SCREEN_CONFIG["annualized_bars_per_day"]
        )
        * 100
    )
    dd = max_drawdown(close)

    primary_ret = pct_change_over(close, cfg["bench_bars"])

    rel_strength_by_bench = {}
    if not np.isnan(primary_ret):
        for name, b_ret in bench_rets.items():
            rel_strength_by_bench[name] = (
                primary_ret - b_ret if not np.isnan(b_ret) else np.nan
            )
    else:
        rel_strength_by_bench = {name: np.nan for name in bench_rets}

    valid_rel = [v for v in rel_strength_by_bench.values() if not np.isnan(v)]
    rel_strength = float(np.mean(valid_rel)) if valid_rel else np.nan

    vs_short_pct = (price / ma_short - 1) * 100 if ma_short else np.nan
    vs_long_pct = (price / ma_long - 1) * 100 if not np.isnan(ma_long) else np.nan

    trend_range = SCREEN_CONFIG["score_ranges"]["trend"]
    score_short = clip_scale(vs_short_pct, *trend_range, SCORE_WEIGHTS["trend"] / 2)
    score_long = (clip_scale(vs_long_pct, *trend_range, SCORE_WEIGHTS["trend"] / 2)
                  if not np.isnan(vs_long_pct) else 0.0)
    trend_score = score_short + score_long

    rsi_settings = SCREEN_CONFIG["rsi"]
    score_rsi = SCORE_WEIGHTS["momentum"] / 2 * max(
        0, 1 - abs(r - rsi_settings["ideal"]) / rsi_settings["distance"]
    )
    score_macd = SCORE_WEIGHTS["momentum"] / 2 if macd_bullish else 0
    momentum_score = score_rsi + score_macd

    volume_score = clip_scale(
        vol_ratio, *SCREEN_CONFIG["score_ranges"]["volume"], SCORE_WEIGHTS["volume"]
    )

    valid_mom = [v for v in mom_returns.values() if not np.isnan(v)]
    avg_mom = float(np.mean(valid_mom)) if valid_mom else np.nan
    price_momentum_score = clip_scale(
        avg_mom,
        *SCREEN_CONFIG["score_ranges"]["price_momentum"],
        SCORE_WEIGHTS["price_momentum"],
    )

    rel_strength_score = clip_scale(
        rel_strength,
        *SCREEN_CONFIG["score_ranges"]["relative_strength"],
        SCORE_WEIGHTS["relative_strength"],
    )
    risk_vol_score = inverse_clip_scale(
        ann_vol,
        *SCREEN_CONFIG["score_ranges"]["risk_volatility"],
        SCORE_WEIGHTS["risk_adjustment"] / 2,
    )
    risk_dd_score = inverse_clip_scale(
        abs(dd),
        *SCREEN_CONFIG["score_ranges"]["risk_drawdown"],
        SCORE_WEIGHTS["risk_adjustment"] / 2,
    )
    risk_score = risk_vol_score + risk_dd_score

    total_score = round(trend_score + momentum_score + volume_score
                        + price_momentum_score + rel_strength_score + risk_score)
    technical_score = int(min(max(total_score, 0), 100))

    analyst_data = rating_scores.get(ticker, {})
    analyst_weighted_score = analyst_data.get("total_mark", np.nan)
    final_score = round(
        analyst_weighted_score * SCREEN_CONFIG["analyst_weight"]
        + technical_score * SCREEN_CONFIG["technical_weight"],
        2,
    )

    rating_levels = SCREEN_CONFIG["rating_levels"]
    if final_score >= rating_levels[0]:
        rating = "Strong hold"
    elif final_score >= rating_levels[1]:
        rating = "Hold"
    elif final_score >= rating_levels[2]:
        rating = "Neutral"
    elif final_score >= rating_levels[3]:
        rating = "Sell"
    else:
        rating = "Strong sell"

    flags = []
    if not np.isnan(vs_long_pct) and price > ma_short > ma_long:
        flags.append("Uptrend")
    elif not np.isnan(vs_long_pct) and price < ma_short < ma_long:
        flags.append("Downtrend")
    if r >= rsi_settings["overbought"]:
        flags.append("Overbought(RSI)")
    elif r <= rsi_settings["oversold"]:
        flags.append("Oversold(RSI)")
    if not np.isnan(vol_ratio) and vol_ratio >= 1.5:
        flags.append("VolumeSurge")
    if macd_bullish:
        flags.append("MACD+")
    if not np.isnan(rel_strength) and rel_strength > 0:
        flags.append("BeatingAvgBench")
    beaten_count = sum(1 for v in rel_strength_by_bench.values()
                       if not np.isnan(v) and v > 0)
    if valid_rel and beaten_count == len(valid_rel):
        flags.append("BeatingAllBench")

    row = {
        "Ticker": ticker,
        "final_score": final_score,
        "rating": rating,
        "price": round(price, 2),
        "analyst_weighted_score": analyst_weighted_score,
        "analyst_number": analyst_data.get("number", np.nan),
        "analyst_strong_buy": analyst_data.get("strong_buy", np.nan),
        "analyst_buy": analyst_data.get("buy", np.nan),
        "analyst_hold": analyst_data.get("hold", np.nan),
        "analyst_sell": analyst_data.get("sell", np.nan),
        "analyst_under_perform": analyst_data.get("under_perform", np.nan),
        "technical_score": technical_score,
        f"vs{cfg['ma_short']}MA%": round(vs_short_pct, 1) if not np.isnan(vs_short_pct) else np.nan,
        f"vs{cfg['ma_long']}MA%": round(vs_long_pct, 1) if not np.isnan(vs_long_pct) else np.nan,
        "RSI": round(r, 1),
        "VolSurge": round(vol_ratio, 2) if not np.isnan(vol_ratio) else np.nan,
    }
    for label, val in mom_returns.items():
        row[label] = round(val, 1) if not np.isnan(val) else np.nan
    row["AnnVol%"] = round(ann_vol, 1)
    row["MaxDD%"] = round(dd, 1)
    row["RelStrength_AvgBench"] = round(rel_strength, 1) if not np.isnan(rel_strength) else np.nan
    for name, val in rel_strength_by_bench.items():
        row[f"RelStrength_vs_{name}"] = round(val, 1) if not np.isnan(val) else np.nan
    row["Flags"] = ", ".join(flags) if flags else "-"
    return row


# ----------------------------------------------------------------------------
# 5. RUN THE SCREEN (single pass)
# ----------------------------------------------------------------------------
def run_stock_screen():
    cfg = TIMEFRAME_CONFIG[TIMEFRAME]
    run_time = datetime.now(timezone.utc)

    bench_rets = {}
    for name, symbol in BENCHMARKS.items():
        bench_hist = safe_download(symbol, cfg["interval"], cfg["period"])
        if bench_hist.empty or "Close" not in bench_hist.columns:
            tqdm.write(f"    -> WARNING: unable to download benchmark {name} ({symbol}); "
                       f"it will be excluded from this run's relative-strength calc")
            bench_rets[name] = np.nan
            continue
        bench_rets[name] = pct_change_over(bench_hist["Close"], cfg["bench_bars"])
        time.sleep(REQUEST_DELAY_SEC)

    if all(np.isnan(v) for v in bench_rets.values()):
        raise RuntimeError(
            "Unable to download any benchmark (SPY/DJI/SPX/IXIC). "
            "Yahoo Finance may be temporarily rate-limiting requests."
        )

    rows = []
    bar = tqdm(
        TICKERS,
        desc="Screening",
        unit="ticker",
        mininterval=PROGRESS_CONFIG["update_interval_seconds"],
        miniters=1,
        leave=False,
    )
    for ticker in bar:
        bar.set_description(f"Analyzing {ticker}")
        row = analyze_ticker(ticker, cfg, bench_rets, RATING_SCORES)
        if row:
            rows.append(row)
        else:
            tqdm.write(f"    -> not enough data for {ticker}, skipping")
        time.sleep(REQUEST_DELAY_SEC)
    bar.set_description("Done")

    if not rows:
        raise RuntimeError("No ticker data was downloaded; no output was generated.")

    df = pd.DataFrame(rows).sort_values("final_score", ascending=False).reset_index(drop=True)

    pd.set_option("display.width", OUTPUT_CONFIG["display_width"])
    pd.set_option("display.max_columns", None)
    
    print(df.to_string(index=False))
    df.to_csv(OUTPUT_CONFIG["stock_csv"], index=False)
    return df


# ----------------------------------------------------------------------------
# 6. Execution
# ----------------------------------------------------------------------------
run_stock_screen()
