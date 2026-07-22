"""Month 8 governed technical and external-data shadow specialists."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import timedelta

from app.agents.base import BaseAgent
from app.agents.specialists import CandleSpecialistAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal
from app.schemas.specialist_evaluation import SpecialistDomain


@dataclass(frozen=True)
class TechnicalDefinition:
    name: str
    capability: str
    metric_index: int


TECHNICAL_DEFINITIONS = (
    TechnicalDefinition("ADXProxyAgent", "adx-proxy", 0),
    TechnicalDefinition("BollingerPositionAgent", "bollinger-position", 1),
    TechnicalDefinition("StochasticAgent", "stochastic-oscillator", 2),
    TechnicalDefinition("WilliamsRAgent", "williams-r", 3),
    TechnicalDefinition("CCIAgent", "commodity-channel-index", 4),
    TechnicalDefinition("DonchianPositionAgent", "donchian-position", 5),
    TechnicalDefinition("KeltnerPositionAgent", "keltner-position", 6),
    TechnicalDefinition("OBVSlopeAgent", "obv-slope", 7),
    TechnicalDefinition("ChaikinMoneyFlowAgent", "chaikin-money-flow", 8),
    TechnicalDefinition("MoneyFlowIndexAgent", "money-flow-index", 9),
    TechnicalDefinition("ParkinsonVolatilityAgent", "parkinson-volatility", 10),
    TechnicalDefinition("GarmanKlassVolatilityAgent", "garman-klass-volatility", 11),
    TechnicalDefinition("UlcerIndexAgent", "ulcer-index", 12),
    TechnicalDefinition("HurstProxyAgent", "hurst-proxy", 13),
    TechnicalDefinition("VarianceRatioAgent", "variance-ratio", 14),
    TechnicalDefinition("PivotDistanceAgent", "pivot-distance", 15),
    TechnicalDefinition("TrueStrengthProxyAgent", "true-strength-proxy", 16),
    TechnicalDefinition("CoppockProxyAgent", "coppock-proxy", 17),
    TechnicalDefinition("AroonAgent", "aroon-oscillator", 18),
    TechnicalDefinition("PriceAccelerationAgent", "price-acceleration", 19),
)


@dataclass(frozen=True)
class ExternalDefinition:
    name: str
    domain: SpecialistDomain
    metric_name: str
    threshold: float
    positive_signal: Signal = Signal.BUY
    scope: str = "SYMBOL"
    max_age_seconds: int = 3_600
    minimum_quality: int = 80
    expected_unit: str = "ratio"

    @property
    def capability(self) -> str:
        return f"{self.domain.lower()}-{self.metric_name.replace('_', '-')}"


DERIVATIVES_DEFINITIONS = (
    ExternalDefinition("FundingRateAgent", "DERIVATIVES", "funding_rate", 0.0, Signal.SELL),
    ExternalDefinition("OpenInterestChangeAgent", "DERIVATIVES", "open_interest_change", 0.0),
    ExternalDefinition("BasisAgent", "DERIVATIVES", "basis", 0.0),
    ExternalDefinition("LongShortRatioAgent", "DERIVATIVES", "long_short_ratio", 1.0),
    ExternalDefinition("LiquidationImbalanceAgent", "DERIVATIVES", "liquidation_imbalance", 0.0),
    ExternalDefinition("TakerFlowAgent", "DERIVATIVES", "taker_buy_sell_ratio", 1.0),
    ExternalDefinition("PerpetualSpotSpreadAgent", "DERIVATIVES", "perpetual_spot_spread", 0.0),
    ExternalDefinition("OptionsSkewAgent", "DERIVATIVES", "options_skew", 0.0, Signal.SELL),
    ExternalDefinition("ImpliedVolatilityAgent", "DERIVATIVES", "implied_volatility", 0.0, Signal.SELL),
    ExternalDefinition("VolatilityTermStructureAgent", "DERIVATIVES", "term_structure", 0.0),
    ExternalDefinition("GammaExposureAgent", "DERIVATIVES", "gamma_exposure", 0.0),
    ExternalDefinition("PutCallRatioAgent", "DERIVATIVES", "put_call_ratio", 1.0, Signal.SELL),
    ExternalDefinition("OIConcentrationAgent", "DERIVATIVES", "open_interest_concentration", 0.5, Signal.SELL),
    ExternalDefinition("FundingDivergenceAgent", "DERIVATIVES", "funding_divergence", 0.0, Signal.SELL),
    ExternalDefinition("LiquidationIntensityAgent", "DERIVATIVES", "liquidation_intensity", 0.0, Signal.SELL),
)

MACRO_DEFINITIONS = (
    ExternalDefinition("DXYChangeAgent", "MACRO", "dxy_change", 0.0, Signal.SELL, "GLOBAL", 86_400),
    ExternalDefinition("PolicyRateChangeAgent", "MACRO", "policy_rate_change", 0.0, Signal.SELL, "GLOBAL", 2_678_400),
    ExternalDefinition("RealYieldChangeAgent", "MACRO", "real_yield_change", 0.0, Signal.SELL, "GLOBAL", 86_400),
    ExternalDefinition("VIXChangeAgent", "MACRO", "vix_change", 0.0, Signal.SELL, "GLOBAL", 86_400),
    ExternalDefinition("NasdaqChangeAgent", "MACRO", "nasdaq_change", 0.0, Signal.BUY, "GLOBAL", 86_400),
    ExternalDefinition("GlobalM2ChangeAgent", "MACRO", "global_m2_change", 0.0, Signal.BUY, "GLOBAL", 2_678_400),
    ExternalDefinition("CreditSpreadChangeAgent", "MACRO", "credit_spread_change", 0.0, Signal.SELL, "GLOBAL", 86_400),
    ExternalDefinition("OilChangeAgent", "MACRO", "oil_change", 0.0, Signal.SELL, "GLOBAL", 86_400),
    ExternalDefinition("GoldChangeAgent", "MACRO", "gold_change", 0.0, Signal.BUY, "GLOBAL", 86_400),
    ExternalDefinition("CryptoETFFlowAgent", "MACRO", "crypto_etf_flow", 0.0, Signal.BUY, "GLOBAL", 86_400),
)

ONCHAIN_DEFINITIONS = (
    ExternalDefinition("ExchangeNetflowAgent", "ONCHAIN", "exchange_netflow", 0.0, Signal.SELL, max_age_seconds=86_400),
    ExternalDefinition("ActiveAddressesAgent", "ONCHAIN", "active_addresses_change", 0.0, max_age_seconds=86_400),
    ExternalDefinition("MVRVAgent", "ONCHAIN", "mvrv", 1.0, max_age_seconds=86_400),
    ExternalDefinition("SOPRAgent", "ONCHAIN", "sopr", 1.0, max_age_seconds=86_400),
    ExternalDefinition("WhaleNetflowAgent", "ONCHAIN", "whale_netflow", 0.0, Signal.SELL, max_age_seconds=86_400),
    ExternalDefinition("RealizedCapAgent", "ONCHAIN", "realized_cap_change", 0.0, max_age_seconds=86_400),
    ExternalDefinition("StablecoinSupplyAgent", "ONCHAIN", "stablecoin_supply_change", 0.0, max_age_seconds=86_400),
    ExternalDefinition("MinerReserveAgent", "ONCHAIN", "miner_reserve_change", 0.0, max_age_seconds=86_400),
    ExternalDefinition("DormancyAgent", "ONCHAIN", "dormancy", 0.0, Signal.SELL, max_age_seconds=86_400),
    ExternalDefinition("NVTAgent", "ONCHAIN", "nvt", 0.0, Signal.SELL, max_age_seconds=86_400),
)

NEWS_DEFINITIONS = (
    ExternalDefinition("NewsSentimentAgent", "NEWS", "sentiment", 0.0, max_age_seconds=21_600),
    ExternalDefinition("NewsRelevanceAgent", "NEWS", "relevance", 0.5, max_age_seconds=21_600),
    ExternalDefinition("NewsNoveltyAgent", "NEWS", "novelty", 0.5, max_age_seconds=21_600),
    ExternalDefinition("NewsConsensusAgent", "NEWS", "source_consensus", 0.0, max_age_seconds=21_600),
    ExternalDefinition("NewsImpactAgent", "NEWS", "impact", 0.0, max_age_seconds=21_600),
)

EXTERNAL_DEFINITIONS = (
    *DERIVATIVES_DEFINITIONS,
    *MACRO_DEFINITIONS,
    *ONCHAIN_DEFINITIONS,
    *NEWS_DEFINITIONS,
)


class Month8TechnicalSpecialist(CandleSpecialistAgent):
    description = "Deterministic read-only Month 8 technical specialist"

    def __init__(self, store: CandleStore, definition: TechnicalDefinition) -> None:
        self.name = definition.name
        self.capabilities = (definition.capability,)
        self._metric_index = definition.metric_index
        super().__init__(store)

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input, limit=80)
        if len(candles) < 30:
            return self._insufficient(actual=len(candles), required=30)
        window = candles[-30:]
        metric = self._metric(window)
        signal = Signal.BUY if metric > 0.05 else Signal.SELL if metric < -0.05 else Signal.HOLD
        confidence = min(90, max(40, int(50 + min(abs(metric), 2) * 20)))
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            f"{self.capabilities[0]} normalized metric is {metric:.6f}",
            evidence={
                "metric": round(metric, 8),
                "metric_name": self.capabilities[0],
                "observations": len(window),
                "source": "normalized-ohlcv",
                "read_only": True,
            },
        )

    def _metric(self, candles) -> float:
        closes = [item.close for item in candles]
        highs = [item.high for item in candles]
        lows = [item.low for item in candles]
        volumes = [item.volume for item in candles]
        returns = [b / a - 1 for a, b in zip(closes, closes[1:])]
        mean = statistics.fmean(closes)
        stdev = statistics.pstdev(closes) or 1e-12
        last = candles[-1]
        position = (last.close - min(lows)) / (max(highs) - min(lows) + 1e-12)
        index = self._metric_index
        if index == 0:
            return (abs(closes[-1] - closes[0]) / (sum(abs(v) for v in returns) * mean + 1e-12)) - 0.5
        if index == 1:
            return (last.close - mean) / (2 * stdev)
        if index in {2, 3, 5}:
            return 2 * position - 1
        if index == 4:
            typical = [(c.high + c.low + c.close) / 3 for c in candles]
            return (typical[-1] - statistics.fmean(typical)) / (stdev + 1e-12)
        if index == 6:
            atr = statistics.fmean(c.high - c.low for c in candles)
            return (last.close - mean) / (2 * atr + 1e-12)
        if index == 7:
            obv = sum(v if r >= 0 else -v for v, r in zip(volumes[1:], returns))
            return obv / (sum(volumes[1:]) + 1e-12)
        if index == 8:
            flow = sum((((2*c.close-c.high-c.low)/(c.high-c.low+1e-12))*c.volume) for c in candles)
            return flow / (sum(volumes) + 1e-12)
        if index == 9:
            positive = sum(v for v, r in zip(volumes[1:], returns) if r > 0)
            negative = sum(v for v, r in zip(volumes[1:], returns) if r < 0)
            return (positive - negative) / (positive + negative + 1e-12)
        if index == 10:
            return -math.sqrt(statistics.fmean(math.log(h/l) ** 2 for h, l in zip(highs, lows)))
        if index == 11:
            return -math.sqrt(abs(statistics.fmean(0.5*math.log(c.high/c.low)**2-(2*math.log(2)-1)*math.log(c.close/c.open)**2 for c in candles)))
        if index == 12:
            peak = closes[0]
            drawdowns = []
            for close in closes:
                peak = max(peak, close)
                drawdowns.append((close / peak - 1) * 100)
            return -math.sqrt(statistics.fmean(value**2 for value in drawdowns)) / 10
        if index == 13:
            half = len(returns) // 2
            return statistics.pstdev(returns) / (statistics.pstdev(returns[:half]) + 1e-12) - 1
        if index == 14:
            one = statistics.pvariance(returns) or 1e-12
            two = [returns[i] + returns[i-1] for i in range(1, len(returns))]
            return statistics.pvariance(two) / (2 * one) - 1
        if index == 15:
            pivot = (last.high + last.low + last.close) / 3
            return (last.close - pivot) / (last.high - last.low + 1e-12)
        if index == 16:
            return statistics.fmean(returns[-5:]) / (statistics.fmean(abs(v) for v in returns[-5:]) + 1e-12)
        if index == 17:
            return (closes[-1] / closes[-11] - 1) + (closes[-1] / closes[0] - 1)
        if index == 18:
            high_age = len(highs) - 1 - max(range(len(highs)), key=highs.__getitem__)
            low_age = len(lows) - 1 - min(range(len(lows)), key=lows.__getitem__)
            return (low_age - high_age) / len(candles)
        return (returns[-1] - statistics.fmean(returns[-5:-1])) * 100


class ExternalEvidenceSpecialist(BaseAgent):
    """Single-metric agent that fails closed when governed evidence is absent."""

    description = "Read-only external evidence specialist"
    required_inputs = ("governed-specialist-evidence",)

    def __init__(self, evidence_service, definition: ExternalDefinition) -> None:
        super().__init__()
        self.name = definition.name
        self.capabilities = (definition.capability,)
        self._evidence_service = evidence_service
        self._definition = definition

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        scope = "GLOBAL" if self._definition.scope == "GLOBAL" else agent_input.symbol
        evidence = await self._evidence_service.latest(
            domain=self._definition.domain,
            metric_name=self._definition.metric_name,
            scope=scope,
            as_of=agent_input.timestamp,
        )
        if evidence is None:
            return self._output(
                AgentStatus.COMPLETED,
                Signal.WAIT,
                0,
                "Governed evidence is missing",
                evidence={
                    "domain": self._definition.domain,
                    "metric_name": self._definition.metric_name,
                    "scope": scope,
                    "read_only": True,
                },
                warnings=["MISSING_EVIDENCE"],
            )
        age_seconds = (agent_input.timestamp - evidence.observed_at).total_seconds()
        if age_seconds > self._definition.max_age_seconds:
            return self._output(
                AgentStatus.COMPLETED,
                Signal.WAIT,
                0,
                "Governed evidence is stale",
                evidence={"evidence_id": evidence.evidence_id, "age_seconds": age_seconds},
                warnings=["STALE_EVIDENCE"],
            )
        if evidence.quality_score < self._definition.minimum_quality:
            return self._output(
                AgentStatus.COMPLETED,
                Signal.WAIT,
                0,
                "Governed evidence quality is below threshold",
                evidence={
                    "evidence_id": evidence.evidence_id,
                    "quality_score": evidence.quality_score,
                },
                warnings=["LOW_QUALITY_EVIDENCE"],
            )
        if evidence.unit != self._definition.expected_unit:
            return self._output(
                AgentStatus.COMPLETED,
                Signal.WAIT,
                0,
                "Governed evidence unit does not match the metric contract",
                evidence={
                    "evidence_id": evidence.evidence_id,
                    "unit": evidence.unit,
                    "expected_unit": self._definition.expected_unit,
                },
                warnings=["UNIT_MISMATCH"],
            )
        positive = evidence.value > self._definition.threshold
        if evidence.value == self._definition.threshold:
            signal = Signal.HOLD
        elif positive:
            signal = self._definition.positive_signal
        else:
            signal = (
                Signal.SELL
                if self._definition.positive_signal == Signal.BUY
                else Signal.BUY
            )
        distance = abs(evidence.value - self._definition.threshold)
        confidence = min(90, max(40, int(40 + evidence.quality_score * 0.4 + min(distance, 1) * 10)))
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            f"{self._definition.metric_name} evidence passed governance gates",
            evidence={
                "evidence_id": evidence.evidence_id,
                "domain": evidence.domain,
                "metric_name": evidence.metric_name,
                "scope": evidence.scope,
                "source": evidence.source,
                "value": evidence.value,
                "unit": evidence.unit,
                "quality_score": evidence.quality_score,
                "observed_at": evidence.observed_at.isoformat(),
                "read_only": True,
            },
        )


def build_month8_shadow_specialists(store: CandleStore, evidence_service) -> list[BaseAgent]:
    technical = [
        Month8TechnicalSpecialist(store, definition)
        for definition in TECHNICAL_DEFINITIONS
    ]
    external = [
        ExternalEvidenceSpecialist(evidence_service, definition)
        for definition in EXTERNAL_DEFINITIONS
    ]
    return [*technical, *external]
