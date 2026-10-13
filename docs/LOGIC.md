# IronMan engine — what every condition means

Documentation of the v16.1 scoring engine. **v16.2 does not change any of this** — it
only adds the `in_band` price gate described in [`PRICE_BAND.md`](PRICE_BAND.md).

The engine is written as a single function, `f_ironman_eval()`, with **no nested
`request.security()` calls** (nested security calls are the #1 cause of Pine
repainting bugs and runtime errors). It returns:

```pine
[active_name, max_score, trend_txt, in_band, close]
```

---

## 1. Candle anatomy

```
    high ─────────┬───  wick_up
                  │
        ┌─────────┴──┐
        │    body    │   body = |close - open|
        └─────────┬──┘
                  │
     low ─────────┴───  wick_down

    total_range = high - low
    body_ratio  = body / total_range      // 1.0 = pure marubozu, 0.0 = doji
```

## 2. Range expansion — "is this bar actually big?"

```pine
z_range = (total_range - SMA(total_range, 20)) / STDEV(total_range, 20)
```

How many standard deviations today's range is from its own 20-bar norm.

| Flag | Threshold | Reading |
| --- | --- | --- |
| `is_tier2_power` | `z_range >= 1.5` | Expansion bar — real participation. |
| `is_tier3_climax` | `z_range >= 3.5` | **Blow-off.** Excluded from every setup — this is where trends end, not continue. |

## 3. Chop detection

```pine
er_20 = |close - close[20]| / SUM(high - low, 20)      // Kaufman efficiency ratio
is_statistical_chop = er_20 < 0.25
```

Net displacement divided by total path travelled. Trending = close to 1.0.
Churning sideways = close to 0. Below `0.25` the market has gone nowhere while
travelling a long way, so trend-following setups are suppressed.

## 4. Trend structure

```pine
ema20  = EMA(close, 20)
ema100 = EMA(close, 100)

is_entangled      = count of EMA20/EMA100 crosses in last 40 bars > 1
z_dist            = (close - ema100) / STDEV(close, 100)
is_extended_*     = |z_dist| >= 2.0
```

| Regime (`trend_txt`) | Condition |
| --- | --- |
| `CHOP` | `is_statistical_chop` **or** `is_entangled` |
| `EXT BULL` | `z_dist >= 2.0` |
| `EXT BEAR` | `z_dist <= -2.0` |
| `BULLISH` | close > EMA100 **and** EMA20 > EMA100 **and** EMA20 rising **and** not entangled **and** not chop |
| `BEARISH` | mirror image |
| `SIDEWAYS` | none of the above |

`is_entangled` is the "whipsaw tax": if the two EMAs have crossed more than once in
40 bars, the trend is not trustworthy regardless of which side price is on.

## 5. Volume

```pine
high_vol = volume > SMA(volume, 20)
re_ratio = (total_range * volume) / (total_range[1] * volume[1])   // "bar energy"
```

`re_ratio` combines expansion *and* participation into one number. `>= 1.25` means
this bar has 25% more energy than the previous one.

## 6. Candle patterns

| Pattern | Rule |
| --- | --- |
| `is_strong_bull` | close > open, `body_ratio >= 0.80` (**0.60** if tier-2 power), closes in top 20% of range, not climax |
| `is_strong_bear` | mirror image, closes in bottom 20% |
| `is_open_eq_low` | open within 4% of the low **and** close > open — bullish drive from the open |
| `is_open_eq_high` | open within 4% of the high **and** close < open |
| `is_marubozu_bull/bear` | strong bar with both wicks ≤ 5% of range |
| `is_hammer` | lower wick ≥ 1.8× body, upper wick ≤ 0.3× body, close > open |
| `is_shooting_star` | mirror image |
| `is_bull_rejection_drive` | lower wick ≥ 25% of range, upper wick ≤ 10%, closes near high |
| `is_bear_rejection_drive` | mirror image |

## 7. EMA proximity

```pine
near_long_ema  = (low <= ema20 <= high)  or  |low - ema20| / ema20 <= 0.05%
                                         or  |low - ema100| / ema100 <= 0.05%
```

The bar is touching or within 5 basis points of a dynamic support. Cheap pullback
entries only — the engine will not score a chase.

## 8. Box breakout

```pine
box_high = HIGHEST(high[1], 20)          // note the [1] — excludes the current bar
box_low  = LOWEST(low[1], 20)
is_valid_box = (box_high - box_low) / close * 100 <= 1.50
```

A 20-bar consolidation no taller than **1.5%** of price. Breakout needs: valid box,
close beyond the boundary, candle in that direction, tier-2 power **or** a strong
candle **or** an open-equals-extreme drive, and above-average volume.

## 9. The 2A sweep (reversal)

Named after the "sweep the level, then nullify it" pattern.

```pine
// Bullish 2A
bar 1: swept yesterday's low (low[1] <= daily_l) on a RED candle
bar 2: extended downtrend (z_dist <= -2.0)
       AND power (re_ratio >= 1.25 or z_range >= 1.0 or is_open_eq_low) AND high_vol
       AND close > high[1] AND close > open
```

Liquidity below the prior day's low gets taken, then price immediately reclaims the
prior bar's high. The sweep failed; the trapped shorts become fuel. The bearish
version is the mirror image off the prior day's high.

`daily_h` / `daily_l` are tracked **inside** the function with `var` — a rolling
prior-day high/low that avoids a nested security call for daily data.

---

## 10. Scoring

Four independent scores are computed; the highest wins.

### `score_cont_long`

| Step | Points |
| --- | --- |
| `box_breakout_long` | **5** (flat, short-circuits the rest) |
| *else* `strict_bull_trend` **and** near EMA support **and** a valid bull candle | +2 |
| &nbsp;&nbsp;+ `high_vol` | +1 |
| &nbsp;&nbsp;+ `close > HIGHEST(high[1], 2)` | +1 |
| &nbsp;&nbsp;+ `body_ratio >= 0.65` **or** straddling EMA100 | +1 |

Valid bull candles: `is_tier2_power`, `is_strong_bull`, `is_open_eq_low`,
`is_marubozu_bull`, `is_hammer`, `ema_rejection_bull`, `is_bull_rejection_drive`.

Maximum **5**. `score_cont_short` is the exact mirror with `is_shooting_star`,
`is_marubozu_bear`, `is_open_eq_high`, `is_bear_rejection_drive`, `ema_rejection_bear`.

### `score_rev_long` / `score_rev_short`

Flat **5** if the corresponding 2A sweep fires, otherwise 0.

### Turning a score into a name

```pine
max_score = max(score_cont_short, score_cont_long, score_rev_short, score_rev_long)

score >= 4  ->  "BOX BREAKOUT" | "BOX BREAKDOWN"
               "LONG CONT"     | "SHORT CONT"
               "LONG 2A SWEEP" | "SHORT 2A SWEEP"
score <  4  ->  "None"
```

**≥ 4/5 is the actionable threshold** — it is what fires the alert and what turns a
table row green or red. A `3` means the structure was mostly there but one
confluence was missing.
