"""Optional structured-output LLM veto: can block but NEVER originate a trade."""
import json
import logging
from openai import OpenAI

LOG = logging.getLogger(__name__)


class AIReviewAgent:
    def __init__(self, cfg):
        self.enabled = cfg.ai_enabled
        self.model = cfg.ai_model
        self.client = OpenAI(timeout=20, max_retries=0) if self.enabled else None

    def allow(self, signal, last_bars) -> tuple[bool, str]:
        if not self.enabled:
            return True, "AI veto disabled (deterministic strategy only)"
        if signal.side == "HOLD":
            return False, "No signal"
        data = {"symbol": signal.symbol, "side": signal.side,
                "atr": round(signal.atr, 6), "rsi": round(signal.rsi, 3),
                "recent_closed_bars": last_bars[["open", "high", "low", "close"]].tail(12).round(5).to_dict("records")}
        try:
            response = self.client.responses.create(
                model=self.model, store=False,
                instructions=("You are a conservative trade quality VETO reviewer. You have no live news, "
                              "no market forecast certainty, and no authority to place or size trades. "
                              "Output approve=false for unclear patterns, abnormal volatility, or insufficient evidence. "
                              "Never propose changes to risk controls. This review alone cannot establish profitability."),
                input=json.dumps(data, ensure_ascii=False),
                text={"format": {"type": "json_schema", "name": "trade_review", "strict": True,
                                 "schema": {"type": "object", "properties": {
                                     "approve": {"type": "boolean"}, "reason": {"type": "string"}},
                                            "required": ["approve", "reason"], "additionalProperties": False}}},
            )
            result = json.loads(response.output_text)
            return result["approve"] is True, str(result["reason"])[:300]
        except Exception as exc:
            LOG.exception("AI veto failed (FAIL CLOSED): %s", exc)
            return False, "AI unavailable / rejected response: blocked for safety"
