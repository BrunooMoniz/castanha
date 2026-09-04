"""Controle de orçamento mensal de IA (Groq Whisper com teto em BRL e fallback)."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from castanha.config import get_state_dir, load_config

DEFAULT_BUDGET_FILE = get_state_dir() / "budget.json"

class BudgetManager:
    def __init__(self):
        cfg = load_config()
        b_cfg = cfg.get("budget", {})
        self.max_monthly_brl = float(b_cfg.get("max_monthly_brl", 5.0))
        self.usd_brl_rate = float(b_cfg.get("usd_brl_rate", 5.75))
        self.price_per_min_usd = float(b_cfg.get("groq_price_per_min_usd", 0.0007))
        self.file_path = DEFAULT_BUDGET_FILE
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_current_month(self) -> str:
        return datetime.now().strftime("%Y-%m")

    def read(self) -> Dict[str, Any]:
        curr_month = self._get_current_month()
        if not self.file_path.exists():
            return {
                "month": curr_month,
                "spent_brl": 0.0,
                "spent_usd": 0.0,
                "minutes_transcribed": 0.0,
                "max_monthly_brl": self.max_monthly_brl,
            }
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("month") != curr_month:
                data = {
                    "month": curr_month,
                    "spent_brl": 0.0,
                    "spent_usd": 0.0,
                    "minutes_transcribed": 0.0,
                    "max_monthly_brl": self.max_monthly_brl,
                }
                self.save(data)
            return data
        except Exception:
            return {
                "month": curr_month,
                "spent_brl": 0.0,
                "spent_usd": 0.0,
                "minutes_transcribed": 0.0,
                "max_monthly_brl": self.max_monthly_brl,
            }

    def save(self, data: Dict[str, Any]) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def can_use_groq(self, estimated_duration_sec: float = 60.0) -> bool:
        current = self.read()
        spent = current.get("spent_brl", 0.0)
        est_cost_brl = (estimated_duration_sec / 60.0) * self.price_per_min_usd * self.usd_brl_rate
        return (spent + est_cost_brl) <= self.max_monthly_brl

    def record_usage(self, duration_sec: float) -> Dict[str, Any]:
        current = self.read()
        duration_min = duration_sec / 60.0
        cost_usd = duration_min * self.price_per_min_usd
        cost_brl = cost_usd * self.usd_brl_rate

        current["minutes_transcribed"] = round(current.get("minutes_transcribed", 0.0) + duration_min, 2)
        current["spent_usd"] = round(current.get("spent_usd", 0.0) + cost_usd, 4)
        current["spent_brl"] = round(current.get("spent_brl", 0.0) + cost_brl, 2)
        self.save(current)
        return current
