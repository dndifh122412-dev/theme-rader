#!/usr/bin/env python3
"""
Theme Surge Radar - collector
글로벌 주식 테마 키워드들의 구글 트렌드 관심도를 수집하고
'급등(surge)' 점수를 계산해서 docs/trends.json 으로 저장한다.

핵심 아이디어
  - 각 테마는 대표 검색어 1개로 추적한다.
  - timeframe='now 7-d' (7일 시간별, 168포인트) 을 받아서
    · 기준선(baseline): 최근 24시간을 제외한 이전 구간 평균
    · 최근(recent):     최근 24시간 평균
    · 추세(slope):      전체 구간 선형회귀 기울기 (정규화)
    · 가속(accel):      최근 24시간 추세 vs 그 직전 24시간 추세
    위 신호를 합쳐 0~100 급등 점수로 환산한다. 단기 모멘텀(투자 타이밍)에 초점.
  - 연관 검색어(rising/top)로 신규 세부 트렌드와 긍/부정 감성도 함께 뽑는다.
  - 자기 과거 대비 변화를 보므로, 키워드는 개별 호출한다.
    (한 payload에 여러 키워드를 넣으면 서로 상대정규화돼서 배치 간 비교가 깨짐)

사용
  python collector.py            # 실제 수집 (구글 트렌드 호출)
  python collector.py --mock     # 가짜 시계열로 로직만 검증 (네트워크 불필요)
  python collector.py --out path # 출력 경로 지정 (기본 docs/trends.json)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 추적할 글로벌 주식 테마 (대표 검색어 1개씩)
# keyword 는 구글 트렌드 검색어. 토픽 ID(/m/...)를 쓰고 싶으면 keyword 자리에 넣어도 동작한다.
# 마음대로 추가/삭제/수정해도 됨. 너무 많으면(>25) 호출이 늘어 차단 위험이 커진다.
# ---------------------------------------------------------------------------
THEMES = [
    {"theme": "AI & LLMs",            "keyword": "artificial intelligence"},
    {"theme": "Semiconductors",       "keyword": "semiconductor"},
    {"theme": "Quantum Computing",    "keyword": "quantum computing"},
    {"theme": "Nuclear / SMR",        "keyword": "nuclear power"},
    {"theme": "Humanoid Robots",      "keyword": "humanoid robot"},
    {"theme": "Weight-loss Drugs",    "keyword": "Ozempic"},
    {"theme": "Defense",              "keyword": "defense stocks"},
    {"theme": "Space",                "keyword": "space stocks"},
    {"theme": "EV & Batteries",       "keyword": "electric vehicle"},
    {"theme": "Solar Energy",         "keyword": "solar energy"},
    {"theme": "Cybersecurity",        "keyword": "cybersecurity"},
    {"theme": "Bitcoin / Crypto",     "keyword": "Bitcoin"},
    {"theme": "Cloud Computing",      "keyword": "cloud computing"},
    {"theme": "Gene Editing",         "keyword": "gene editing"},
    {"theme": "Data Centers",         "keyword": "data center"},
    {"theme": "Rare Earths",          "keyword": "rare earth"},
    {"theme": "Hydrogen",             "keyword": "hydrogen energy"},
    {"theme": "Autonomous Driving",   "keyword": "self driving car"},
    {"theme": "AR / VR",              "keyword": "virtual reality"},
    {"theme": "Drones",               "keyword": "drone"},
]

RECENT_POINTS = 24       # '최근' 구간 = 마지막 24포인트(시간별이라 약 1일)
TIMEFRAME = "now 7-d"    # 수집 기간: 7일 시간별(단기 급등 포착에 적합, 168포인트)
CHART_DOWNSAMPLE = 56    # 차트에 그릴 포인트 수(168 → 56, 약 3시간 간격)
GEO = ""                 # '' = 전세계(Worldwide)
SLEEP_BETWEEN = 4.0      # 키워드 호출 사이 대기(초). 차단 회피용. 줄이지 말 것 권장.
WITH_RELATED = True      # 각 테마의 '급부상 연관어'(rising queries) 수집. 호출이 늘어 차단 위험↑
EMERGING_BASELINE = 25   # 평소 이 값보다 관심이 낮던 테마가 급등하면 '신규 부상(NEW)'으로 표시

# 검색어 감성 사전 (주식/금융 맥락). 연관 검색어 단어를 매칭해 긍/부정 비율을 추정한다.
# 완벽한 감성 분석이 아니라 "사람들이 이 테마를 긍/부정 어느 쪽으로 검색하나"의 거친 신호.
POS_WORDS = {"buy", "bull", "bullish", "rally", "surge", "soar", "gain", "gains", "boom",
             "breakout", "upgrade", "beat", "beats", "growth", "rebound", "record", "high",
             "moon", "rocket", "win", "wins", "invest", "opportunity", "strong", "jump",
             "rise", "rises", "rising", "outperform", "target", "buyout", "bullrun"}
NEG_WORDS = {"crash", "bear", "bearish", "drop", "fall", "falls", "plunge", "sink", "sell",
             "short", "bubble", "warning", "warn", "risk", "lawsuit", "ban", "fraud", "scam",
             "layoff", "layoffs", "miss", "cut", "cuts", "downgrade", "loss", "losses",
             "collapse", "recession", "fear", "dump", "tumble", "slump", "weak", "crisis",
             "halt", "delay", "delisting", "bankruptcy", "overvalued", "selloff"}


# ---------------------------------------------------------------------------
# 급등 점수 계산 (수집 방식과 무관한 순수 함수 — 테스트 쉬움)
# ---------------------------------------------------------------------------
def _linear_slope(ys: list[float]) -> float:
    """단순 선형회귀 기울기. x는 0..n-1."""
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def compute_surge(series: list[float], recent_n: int = RECENT_POINTS) -> dict:
    """
    관심도 시계열(0~100) -> 급등 지표 묶음.
    'now 7-d'(시간별 168포인트)를 받아 '최근 recent_n포인트(≈1일) vs 그 이전' 단기 모멘텀을 본다.
    반환: baseline, recent, change_pct, surge_ratio, slope, accel, score, emerging
    """
    series = [float(v) for v in series if v is not None]
    n = len(series)
    if n < recent_n + 4:
        cur = series[-1] if series else 0.0
        return {
            "baseline": round(cur, 1), "recent": round(cur, 1),
            "change_pct": 0.0, "surge_ratio": 1.0,
            "slope": 0.0, "accel": 0.0, "score": 0.0, "emerging": False,
        }

    recent = series[-recent_n:]
    baseline_part = series[:-recent_n]

    recent_avg = sum(recent) / len(recent)
    baseline_avg = sum(baseline_part) / len(baseline_part)
    eps = 1.0  # 0 division 방지 + 저관심 키워드의 폭발적 비율 과장 억제

    surge_ratio = (recent_avg + eps) / (baseline_avg + eps)
    change_pct = (surge_ratio - 1.0) * 100.0

    # 전체 추세 기울기를 관심도 스케일로 정규화(포인트당 % 변화)
    slope_raw = _linear_slope(series)
    slope = slope_raw / (baseline_avg + eps) * 100.0

    # 가속도: 최근 recent_n 기울기 vs 직전 recent_n 기울기 (단기 모멘텀이 붙는 중인지)
    last_w = series[-recent_n:]
    prev_w = series[-2 * recent_n:-recent_n] if n >= 2 * recent_n else series[:-recent_n]
    accel = (_linear_slope(last_w) - _linear_slope(prev_w))

    # --- 종합 점수 -----------------------------------------------------------
    #  · 상승률(change_pct): 최근 하루가 평소(지난 6일) 대비 얼마나 떴나   (가중 1.0)
    #  · 추세(slope):        주 전체가 우상향인가                          (가중 0.6)
    #  · 가속(accel):        지금 막 가속 붙었나 (조기 신호)               (가중 4.0)
    score_raw = (
        1.0 * max(change_pct, 0.0)
        + 0.6 * max(slope, 0.0)
        + 4.0 * max(accel, 0.0)
    )
    score = 100.0 * math.tanh(score_raw / 80.0)

    # 신규 부상: 평소 관심이 낮았는데(baseline 낮음) 최근 확 뜬 경우.
    emerging = (
        baseline_avg < EMERGING_BASELINE
        and recent_avg > baseline_avg * 1.8
        and recent_avg > 12
    )

    return {
        "baseline": round(baseline_avg, 1),
        "recent": round(recent_avg, 1),
        "change_pct": round(change_pct, 1),
        "surge_ratio": round(surge_ratio, 2),
        "slope": round(slope, 2),
        "accel": round(accel, 2),
        "score": round(score, 1),
        "emerging": bool(emerging),
    }


def downsample(series: list[float], target: int = 30) -> list[float]:
    """스파크라인용으로 시계열을 target 포인트로 균등 다운샘플."""
    n = len(series)
    if n <= target:
        return [round(float(v), 1) for v in series]
    step = n / target
    out = []
    for i in range(target):
        idx = min(int(i * step), n - 1)
        out.append(round(float(series[idx]), 1))
    return out


def chart_series(series: list[float], dates: list[str], target: int = CHART_DOWNSAMPLE):
    """차트용: 시계열 전체(7일)를 target 포인트로 다운샘플하고 시작/끝 날짜를 반환."""
    vals = downsample(series, target)
    start = dates[0] if dates else ""
    end = dates[-1] if dates else ""
    return vals, start, end


def _sentiment(queries: list[str]) -> dict:
    """연관 검색어 단어를 긍/부정 사전과 매칭해 감성 비율을 추정."""
    pos = neg = 0
    for q in queries:
        words = set(str(q).lower().replace("'", " ").split())
        if words & POS_WORDS:
            pos += 1
        if words & NEG_WORDS:
            neg += 1
    tot = pos + neg
    bias = round((pos - neg) / tot, 2) if tot else None  # +1 긍정 ~ -1 부정, None=판단불가
    return {"pos": pos, "neg": neg, "bias": bias}


def fetch_related(pytrends, kw: str, top: int = 3):
    """현재 build_payload 된 키워드의 연관 검색어를 가져온다.
      · rising(급상승): 고정 키워드로 못 잡는 신규 세부 트렌드 — 표시용
      · top+rising 전체: 단어 감성 분석으로 긍/부정 비율 추정
    실패해도 전체 수집을 막지 않도록 빈 값을 반환한다.
    반환: (rising_list, sentiment_dict)"""
    try:
        rq = pytrends.related_queries()
        entry = rq.get(kw, {}) or {}
        rising_df = entry.get("rising")
        top_df = entry.get("top")

        rising_list = []
        if rising_df is not None and not getattr(rising_df, "empty", True):
            for _, row in rising_df.head(top).iterrows():
                raw = row.get("value", 0)
                try:
                    v = int(raw)
                except (TypeError, ValueError):
                    v = 0
                # 구글은 폭발적 급상승을 5000(+5000%) 또는 'Breakout'으로 표기
                label = "급등" if v >= 5000 else (f"+{v}%" if v > 0 else "신규")
                rising_list.append({"q": str(row.get("query", "")), "v": label})

        all_q = []
        for df in (rising_df, top_df):
            if df is not None and not getattr(df, "empty", True):
                all_q += df["query"].astype(str).tolist()
        return rising_list, _sentiment(all_q)
    except Exception:  # noqa: BLE001
        return [], {"pos": 0, "neg": 0, "bias": None}


# ---------------------------------------------------------------------------
# 가짜 시계열 (mock) — 로직 검증용
# ---------------------------------------------------------------------------
def _mock_series(kind: str, n: int = 168) -> list[float]:
    rnd = random.Random(hash(kind) & 0xFFFF)
    base = rnd.uniform(20, 60)
    out = []
    for i in range(n):
        t = i / n
        if kind == "surge":          # 후반부 급등
            level = base + (70 * max(0, t - 0.75)) * 4
        elif kind == "rising":       # 완만 상승
            level = base + 30 * t
        elif kind == "spike_fade":   # 떴다가 식음
            level = base + 40 * math.exp(-((t - 0.5) ** 2) / 0.01)
        elif kind == "falling":      # 하락
            level = base + 25 * (1 - t)
        else:                         # flat
            level = base
        out.append(max(0, min(100, level + rnd.uniform(-4, 4))))
    return out


def collect_mock() -> list[dict]:
    from datetime import datetime, timedelta
    kinds = ["surge", "rising", "spike_fade", "falling", "flat"]
    now = datetime.now()
    rows = []
    for i, th in enumerate(THEMES):
        kind = kinds[i % len(kinds)]
        series = _mock_series(kind)
        n = len(series)
        dates = [(now - timedelta(hours=n - 1 - j)).strftime("%Y-%m-%d") for j in range(n)]
        metrics = compute_surge(series)
        cvals, cstart, cend = chart_series(series, dates)
        row = {**th, **metrics, "spark": cvals,
               "spark_start": cstart, "spark_end": cend, "ok": True}
        if kind in ("surge", "rising"):
            row["rising"] = [{"q": th["keyword"] + " stocks", "v": "급등"},
                             {"q": th["keyword"] + " etf", "v": "+180%"}]
            row["sentiment"] = {"pos": 6, "neg": 2, "bias": 0.5}
        elif kind == "falling":
            row["rising"] = []
            row["sentiment"] = {"pos": 1, "neg": 5, "bias": -0.67}
        else:
            row["rising"] = []
            row["sentiment"] = {"pos": 3, "neg": 3, "bias": 0.0}
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 실제 수집 (구글 트렌드)
# ---------------------------------------------------------------------------
def collect_live() -> tuple[list[dict], list[dict]]:
    from pytrends_modern.request import TrendReq
    from pytrends_modern.exceptions import TooManyRequestsError

    pytrends = TrendReq(
        hl="en-US", tz=0,
        timeout=(10, 25),
        retries=3, backoff_factor=0.5,
        rotate_user_agent=True,      # User-Agent 로테이션으로 차단 완화
    )

    rows, errors = [], []
    for i, th in enumerate(THEMES):
        kw = th["keyword"]
        try:
            pytrends.build_payload([kw], cat=0, timeframe=TIMEFRAME, geo=GEO)
            df = pytrends.interest_over_time()
            if df is None or df.empty or kw not in df.columns:
                raise RuntimeError("empty result")
            series = df[kw].astype(float).tolist()
            dates = [d.strftime("%Y-%m-%d") for d in df.index]
            metrics = compute_surge(series)
            cvals, cstart, cend = chart_series(series, dates)   # 7일 차트 + 시작/끝 날짜
            row = {**th, **metrics, "spark": cvals,
                   "spark_start": cstart, "spark_end": cend, "ok": True}
            if WITH_RELATED:
                time.sleep(1.0)                            # 연관어 호출 전 짧은 텀
                row["rising"], row["sentiment"] = fetch_related(pytrends, kw)
            rows.append(row)
            tag = "  NEW" if metrics["emerging"] else ""
            print(f"[ok]   {th['theme']:<20} score={metrics['score']:>5}  "
                  f"({metrics['change_pct']:+.0f}%){tag}", file=sys.stderr)
        except TooManyRequestsError:
            print(f"[429]  {th['theme']:<20} rate-limited, 긴 대기 후 계속", file=sys.stderr)
            errors.append({**th, "error": "rate_limited"})
            time.sleep(60)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {th['theme']:<20} {e}", file=sys.stderr)
            errors.append({**th, "error": str(e)})
        time.sleep(SLEEP_BETWEEN)

    return rows, errors


def collect_trending_now() -> list[str]:
    """실시간 급등 검색어(글로벌 대용으로 US) 중 일부를 보너스로 가져온다. 실패해도 무시."""
    try:
        from pytrends_modern.request import TrendReq
        pytrends = TrendReq(hl="en-US", tz=0, rotate_user_agent=True)
        df = pytrends.trending_searches(pn="united_states")
        vals = df.iloc[:, 0].astype(str).tolist()
        return vals[:15]
    except Exception as e:  # noqa: BLE001
        print(f"[warn] trending_now 실패: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="가짜 데이터로 로직 검증")
    ap.add_argument("--out", default="docs/trends.json", help="출력 JSON 경로")
    ap.add_argument("--keep-stale", action="store_true",
                    help="수집 0건이면 기존 파일을 덮어쓰지 않음")
    ap.add_argument("--no-related", action="store_true",
                    help="급부상 연관어 수집 끄기(호출 절반, 차단 위험↓)")
    args = ap.parse_args()

    global WITH_RELATED
    if args.no_related:
        WITH_RELATED = False

    trending = []
    if args.mock:
        rows = collect_mock()
        errors = []
        status = "mock"
    else:
        rows, errors = collect_live()
        trending = collect_trending_now()
        status = "ok" if not errors else ("partial" if rows else "failed")

    # 수집 전멸 + keep-stale → 기존 파일 보존하고 종료
    if not rows and args.keep_stale and os.path.exists(args.out):
        print("수집 0건 — 기존 데이터 유지하고 종료", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: r["score"], reverse=True)
    for rank, r in enumerate(rows, 1):
        r["rank"] = rank

    # 시장 전반 감성 집계 (모든 테마의 긍/부정 단어 합)
    tot_pos = sum((r.get("sentiment") or {}).get("pos", 0) for r in rows)
    tot_neg = sum((r.get("sentiment") or {}).get("neg", 0) for r in rows)
    market_bias = (round((tot_pos - tot_neg) / (tot_pos + tot_neg), 2)
                   if (tot_pos + tot_neg) else None)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timeframe": TIMEFRAME,
        "geo": "Worldwide",
        "recent_points": RECENT_POINTS,
        "chart_span_days": 7,
        "sentiment": {"pos": tot_pos, "neg": tot_neg, "bias": market_bias},
        "status": status,
        "reliability": {
            "collected": len(rows),
            "total": len(THEMES),
            "quality": ("good" if len(rows) >= len(THEMES) * 0.8
                        else ("degraded" if rows else "failed")),
            "with_related": WITH_RELATED,
        },
        "themes": rows,
        "trending_now": trending,
        "errors": errors,
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n저장: {args.out}  (themes={len(rows)}, status={status})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
