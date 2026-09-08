from __future__ import annotations

from typing import Any

from ..errors import ContractError


class ConfigPublicationBinding:
    """Typed single-use publication authorization for a paper execution config."""

    def __init__(
        self,
        *,
        decision: dict[str, Any],
        config_generation_id: str,
        require_executable: bool = True,
    ) -> None:
        if require_executable:
            _validate_decision(decision)
        if decision.get("binding_config_generation_id") != config_generation_id:
            raise ContractError("risk decision is not bound to this execution config generation")
        self._decision = decision
        self._config_generation_id = config_generation_id
        self._released = False

    @property
    def decision(self) -> dict[str, Any]:
        return dict(self._decision)

    def assert_can_publish_config(self) -> None:
        if self._released:
            raise ContractError("risk decision already authorized a config publication")
        self._released = True


def _validate_decision(decision: dict[str, Any]) -> None:
    if not isinstance(decision, dict) or decision.get("action") not in {"allow", "warn"}:
        raise ContractError("config publication risk decision is not executable")
